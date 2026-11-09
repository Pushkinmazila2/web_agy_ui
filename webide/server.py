#!/usr/bin/env python3
"""webide: минимальная веб-IDE для agy. Запускается ВНУТРИ контейнера с agy.

Дерево файлов + поиск + Monaco-редактор + терминал с agy (pty, сессии
переживают перезагрузку страницы). Зависимость одна: aiohttp.
"""
import asyncio
import base64
import fcntl
import hmac
import json
import os
import pty
import re
import secrets
import shutil
import signal
import struct
import sys
import termios
import time
from pathlib import Path
from urllib.parse import urlsplit

from aiohttp import WSMsgType, web

try:  # виртуальный терминал для «снимков экрана» (Telegram / API); без него режем ANSI регуляркой
    import pyte
except ImportError:
    pyte = None

BASE = Path(__file__).resolve().parent
WORKSPACE = Path(os.environ.get("WORKSPACE", "/workspace")).resolve()
PROJECTS_FILE = Path(os.environ.get("PROJECTS_FILE", BASE / "projects.json"))
import shlex

# {AGY_FLAGS} заменяется флагами проекта из окна «Настройки агента». agy-run умеет «продолжить диалог» (-c).
DEFAULT_CMD = os.environ.get("AGY_CMD", f"bash {shlex.quote(str(Path(__file__).resolve().parent / 'agy-run'))} {{AGY_FLAGS}}; exec bash -l")
SETTINGS_PATH = Path(os.environ.get("AGY_SETTINGS", Path.home() / ".gemini" / "antigravity-cli" / "settings.json"))
STATE_PATH = Path(os.environ.get("WEBIDE_STATE", Path(__file__).resolve().parent / "webide-state.json"))
HOST = os.environ.get("HOST", "0.0.0.0")
PORT = int(os.environ.get("PORT", "8080"))
AUTH_USER = os.environ.get("AUTH_USER", "agy")
AUTH_PASS = os.environ.get("AUTH_PASS", "")
GENERATED_PASS = not AUTH_PASS
if GENERATED_PASS:
    AUTH_PASS = secrets.token_urlsafe(12)

VENDOR = Path(os.environ.get("VENDOR_DIR", BASE / "vendor"))
MAX_FILE = 5 * 1024 * 1024
SCROLLBACK = 256 * 1024
SEARCH_LIMIT = 500
SKIP_DIRS = {".git", "node_modules", "__pycache__", ".venv", "venv", ".cache", "dist", "build"}

sessions = {}  # project id -> Session


# ───────────────────────── проекты ─────────────────────────

def load_projects():
    items = {}
    if WORKSPACE.is_dir():
        for p in sorted(WORKSPACE.iterdir()):
            if p.is_dir() and not p.name.startswith("."):
                items[p.name] = {"id": p.name, "name": p.name, "path": str(p), "cmd": DEFAULT_CMD}
    try:
        cfg = json.loads(PROJECTS_FILE.read_text())
    except FileNotFoundError:
        cfg = []
    except ValueError as e:
        print(f"[webide] projects.json: {e}", flush=True)
        cfg = []
    for c in cfg:
        try:
            path = Path(c["path"])
            if not path.is_absolute():
                path = WORKSPACE / path
            pid = str(c.get("id") or c.get("name") or path.name)
            items[pid] = {
                "id": pid,
                "name": c.get("name", pid),
                "path": str(path),
                "cmd": c.get("cmd", DEFAULT_CMD),
            }
        except (KeyError, TypeError):
            print(f"[webide] пропущен некорректный пресет: {c!r}", flush=True)
    if WORKSPACE.is_dir():
        items.setdefault("_root", {"id": "_root", "name": "(весь workspace)", "path": str(WORKSPACE), "cmd": DEFAULT_CMD})
    return [p for p in items.values() if Path(p["path"]).is_dir()]


def get_project(pid):
    for p in load_projects():
        if p["id"] == pid:
            return p
    raise web.HTTPNotFound(text="unknown project")


def resolve(proj, rel):
    root = Path(proj["path"]).resolve()
    p = (root / (rel or "").lstrip("/")).resolve()
    if p != root and root not in p.parents:
        raise web.HTTPForbidden(text="path outside project")
    return root, p


def mtime_of(p):
    return p.stat().st_mtime_ns // 1000


# ───────────────────────── авторизация ─────────────────────────

@web.middleware
async def guard(request, handler):
    ok = False
    h = request.headers.get("Authorization", "")
    if h.startswith("Basic "):
        try:
            u, _, p = base64.b64decode(h[6:]).decode().partition(":")
            ok = hmac.compare_digest(u.encode(), AUTH_USER.encode()) & hmac.compare_digest(p.encode(), AUTH_PASS.encode())
        except Exception:
            ok = False
    if not ok:
        raise web.HTTPUnauthorized(headers={"WWW-Authenticate": 'Basic realm="agy webide"'})
    origin = request.headers.get("Origin")
    if origin and (request.method != "GET" or request.path.startswith("/ws/")):
        if urlsplit(origin).netloc != request.headers.get("Host"):
            raise web.HTTPForbidden(text="bad origin")
    return await handler(request)


# ───────────────────────── файлы ─────────────────────────

async def api_projects(request):
    return web.json_response([{"id": p["id"], "name": p["name"], "path": p["path"]} for p in load_projects()])


async def api_tree(request):
    proj = get_project(request.query.get("project"))
    _, d = resolve(proj, request.query.get("path", ""))
    if not d.is_dir():
        raise web.HTTPNotFound(text="not a directory")
    out = []
    with os.scandir(d) as it:
        for e in it:
            if e.name == ".git":
                continue
            out.append({"name": e.name, "dir": e.is_dir()})
    out.sort(key=lambda x: (not x["dir"], x["name"].lower()))
    return web.json_response(out)


async def api_file_get(request):
    proj = get_project(request.query.get("project"))
    _, p = resolve(proj, request.query.get("path", ""))
    if not p.is_file():
        raise web.HTTPNotFound(text="file not found")
    size = p.stat().st_size
    if size > MAX_FILE:
        raise web.HTTPRequestEntityTooLarge(max_size=MAX_FILE, actual_size=size, text="файл больше 5 МБ")
    data = p.read_bytes()
    if b"\0" in data[:8192]:
        raise web.HTTPUnsupportedMediaType(text="бинарный файл")
    return web.json_response({"content": data.decode("utf-8", "replace"), "mtime": mtime_of(p)})


async def api_file_put(request):
    body = await request.json()
    proj = get_project(body.get("project"))
    _, p = resolve(proj, body.get("path", ""))
    if p.exists():
        if not p.is_file():
            raise web.HTTPBadRequest(text="not a file")
        if not body.get("force") and body.get("mtime") != mtime_of(p):
            return web.json_response({"error": "changed"}, status=409)
    elif not p.parent.is_dir():
        raise web.HTTPBadRequest(text="parent directory missing")
    p.write_bytes(body.get("content", "").encode("utf-8"))
    return web.json_response({"mtime": mtime_of(p)})


async def api_stat(request):
    proj = get_project(request.query.get("project"))
    out = {}
    for rel in request.query.getall("path", []):
        try:
            _, p = resolve(proj, rel)
            out[rel] = mtime_of(p) if p.is_file() else None
        except (web.HTTPException, OSError):
            out[rel] = None
    return web.json_response(out)


# ───────────────────────── поиск ─────────────────────────

def do_search(root, q, regex, case):
    try:
        rx = re.compile(q if regex else re.escape(q), 0 if case else re.I)
    except re.error as e:
        return {"error": str(e)}
    ql = q.lower()
    files, matches = [], []
    for dp, dns, fns in os.walk(root):
        dns[:] = sorted(d for d in dns if d not in SKIP_DIRS)
        for fn in sorted(fns):
            fp = os.path.join(dp, fn)
            rel = os.path.relpath(fp, root)
            if ql in fn.lower() and len(files) < 50:
                files.append(rel)
            try:
                if os.path.islink(fp) or os.path.getsize(fp) > 1_000_000:
                    continue
                with open(fp, "rb") as fh:
                    data = fh.read()
            except OSError:
                continue
            if b"\0" in data[:4096]:
                continue
            for i, line in enumerate(data.decode("utf-8", "replace").splitlines(), 1):
                if rx.search(line):
                    matches.append({"path": rel, "line": i, "text": line.strip()[:300]})
                    if len(matches) >= SEARCH_LIMIT:
                        return {"files": files, "matches": matches, "truncated": True}
    return {"files": files, "matches": matches, "truncated": False}


async def api_search(request):
    proj = get_project(request.query.get("project"))
    root, _ = resolve(proj, "")
    q = request.query.get("q", "")
    if len(q) < 2:
        return web.json_response({"files": [], "matches": [], "truncated": False})
    res = await asyncio.get_running_loop().run_in_executor(
        None, do_search, root, q, request.query.get("regex") == "1", request.query.get("case") == "1"
    )
    return web.json_response(res)


# ───────────────────────── терминал ─────────────────────────

def _reap(pid):
    for _ in range(30):
        try:
            if os.waitpid(pid, os.WNOHANG)[0]:
                return
        except ChildProcessError:
            return
        time.sleep(0.1)
    try:
        os.killpg(pid, signal.SIGKILL)
    except OSError:
        pass
    try:
        os.waitpid(pid, 0)
    except ChildProcessError:
        pass


class Session:
    """Процесс в pty. Живёт независимо от браузера: можно закрыть вкладку и вернуться."""

    def __init__(self, key, cwd, cmd, cols, rows, extra_env=None):
        self.key = key
        self.clients = set()  # asyncio.Queue подключённых браузеров
        self.buf = bytearray()
        self.out = bytearray()
        self.alive = True
        self.done = asyncio.Event()
        self.cols, self.rows = cols, rows
        self.last_out = time.monotonic()
        self.screen = self.stream = None
        if pyte:
            self.screen = pyte.Screen(cols, rows)
            self.stream = pyte.ByteStream(self.screen)
        env = dict(os.environ, TERM="xterm-256color", COLORTERM="truecolor", **(extra_env or {}))
        shell = shutil.which("bash") or "/bin/sh"
        self.pid, self.fd = pty.fork()
        if self.pid == 0:
            try:
                os.chdir(cwd)
                os.execvpe(shell, [shell, "-ic", cmd], env)
            except BaseException:
                pass
            os._exit(127)
        os.set_blocking(self.fd, False)
        self.resize(cols, rows)
        asyncio.get_running_loop().add_reader(self.fd, self._read)

    def _read(self):
        try:
            data = os.read(self.fd, 65536)
        except BlockingIOError:
            return
        except OSError:
            data = b""
        if not data:
            self._close()
            return
        self.buf += data
        self.last_out = time.monotonic()
        if self.stream:
            try:
                self.stream.feed(data)
            except Exception:
                pass
        if len(self.buf) > SCROLLBACK:
            del self.buf[: len(self.buf) - SCROLLBACK]
        for q in self.clients:
            q.put_nowait(data)

    def write(self, data):
        if self.alive:
            self.out += data
            self._flush()

    def _flush(self):
        if not self.alive:
            return
        loop = asyncio.get_running_loop()
        while self.out:
            try:
                n = os.write(self.fd, self.out)
            except BlockingIOError:
                loop.add_writer(self.fd, self._flush)
                return
            except OSError:
                self.out.clear()
                break
            del self.out[:n]
        try:
            loop.remove_writer(self.fd)
        except Exception:
            pass

    def resize(self, cols, rows):
        if self.alive and cols > 0 and rows > 0:
            fcntl.ioctl(self.fd, termios.TIOCSWINSZ, struct.pack("HHHH", rows, cols, 0, 0))
            if self.screen and (cols, rows) != (self.cols, self.rows):
                self.screen.resize(rows, cols)
            self.cols, self.rows = cols, rows

    def _close(self):
        if not self.alive:
            return
        self.alive = False
        loop = asyncio.get_running_loop()
        for fn in (loop.remove_reader, loop.remove_writer):
            try:
                fn(self.fd)
            except Exception:
                pass
        try:
            os.close(self.fd)
        except OSError:
            pass
        try:
            os.killpg(self.pid, signal.SIGHUP)
        except OSError:
            pass
        loop.run_in_executor(None, _reap, self.pid)
        if sessions.get(self.key) is self:
            del sessions[self.key]
        for q in self.clients:
            q.put_nowait(None)
        self.done.set()

    async def kill(self):
        try:
            os.killpg(self.pid, signal.SIGHUP)
        except OSError:
            pass
        try:
            await asyncio.wait_for(self.done.wait(), 3)
        except asyncio.TimeoutError:
            try:
                os.killpg(self.pid, signal.SIGKILL)
            except OSError:
                pass
            self._close()


def _int(v, default):
    try:
        return max(1, min(int(v), 1000))
    except (TypeError, ValueError):
        return default


def ensure_session(proj, cols=120, rows=40, cont=False):
    """Возвращает живую сессию проекта, при необходимости запускает (cmd пресета, cwd = папка проекта).
    cont=True: agy-run сначала попробует продолжить последний диалог (-c)."""
    s = sessions.get(proj["id"])
    if not s or not s.alive:
        s = Session(proj["id"], proj["path"], build_cmd(proj), cols, rows, {"AGY_CONTINUE": "1"} if cont else None)
        sessions[proj["id"]] = s
    return s


# ───────────────────────── настройки агента ─────────────────────────
# Схема взята из документации agy (settings.json). Значения по умолчанию agy в файл не пишет, мы тоже.

_SEL = lambda key, label, opts, default, group, help="": dict(key=key, label=label, type="select", options=opts, default=default, group=group, help=help)
_BOOL = lambda key, label, default, group, help="": dict(key=key, label=label, type="bool", default=default, group=group, help=help)
SCHEMA = [
    _SEL("agentMode", "Режим агента", ["default", "auto", "accept-edits", "plan"], "default", "Поведение", "auto недоступен для business/enterprise-аккаунтов"),
    _SEL("toolPermission", "Подтверждение инструментов", ["request-review", "proceed-in-sandbox", "strict", "always-proceed"], "request-review", "Поведение"),
    _SEL("artifactReviewPolicy", "Ревью артефактов", ["asks-for-review", "agent-decides", "always-proceed"], "asks-for-review", "Поведение"),
    _BOOL("enableTerminalSandbox", "Песочница терминала", False, "Поведение"),
    _BOOL("allowNonWorkspaceAccess", "Доступ вне рабочей папки", False, "Поведение"),
    dict(key="model", label="Модель", type="text", default="", group="Поведение", help="название как в /model; пусто = по умолчанию"),
    dict(key="modelProvider", label="Провайдер модели", type="text", default="", group="Поведение", help='"gemini" = напрямую через GEMINI_API_KEY'),
    _SEL("queuedMessages", "Сообщения во время работы", ["queue", "send-immediately"], "queue", "Поведение"),
    dict(key="trustedWorkspaces", label="Доверенные папки", type="lines", default=None, group="Поведение", help="по одной в строке"),
    _SEL("colorScheme", "Цветовая схема", ["terminal", "light", "solarized light", "colorblind-friendly light", "dark", "solarized dark", "colorblind-friendly dark", "tokyo night"], "terminal", "Интерфейс"),
    _SEL("verbosity", "Подробность вывода", ["high", "medium", "low"], "medium", "Интерфейс"),
    _SEL("runningLightSpeed", "Анимация «работает»", ["fast", "medium", "slow", "off"], "medium", "Интерфейс"),
    _SEL("altScreenMode", "Alt-screen", ["default", "always", "never"], "default", "Интерфейс", "never = история остаётся в буфере терминала"),
    _SEL("pickerGrouping", "Группировка в пикерах", ["none", "grouped"], "none", "Интерфейс"),
    _BOOL("hideActiveItems", "Скрывать активные элементы", False, "Интерфейс"),
    _BOOL("copyOnSelect", "Копировать при выделении", True, "Интерфейс"),
    _BOOL("useTerminalCursor", "Курсор терминала", False, "Интерфейс"),
    _BOOL("clearScrollbackOnResize", "Очищать историю при смене размера", True, "Интерфейс"),
    _BOOL("showTips", "Подсказки", True, "Интерфейс"),
    _BOOL("showFeedbackSurvey", "Опрос об отзывах", True, "Интерфейс"),
    _BOOL("notifications", "Уведомления", False, "Интерфейс"),
    dict(key="editor", label="Редактор", type="text", default="auto", group="Ввод", help="auto, vim, emacs или своя команда"),
    _SEL("editorMode", "Режим ввода", ["default", "vim"], "default", "Ввод"),
    _BOOL("vimInsertFirst", "Vim: начинать в insert", False, "Ввод"),
    dict(key="historySize", label="Размер истории ввода", type="int", default=2000, group="Ввод", help="-1 = хранить всё, 0 = очищать"),
    _BOOL("disableSlashCommands", "Отключить слэш-команды", False, "Ввод"),
    _BOOL("useG1Credits", "Использовать G1-кредиты", False, "Прочее"),
] + [dict(key=k, label=k, type="json", default=None, group="Расширенные (JSON-объекты)", help=h)
     for k, h in [("permissions", '{"allow": [], "ask": [], "deny": []}'), ("statusLine", "command, type, enabled, padding, stack_with_default"),
                  ("title", "command, type, enabled"), ("sandboxProxy", "http: address, certFile, учётные данные"),
                  ("customModelsConfig", ""), ("gcp", "project, location")]]
SCHEMA_BY_KEY = {f["key"]: f for f in SCHEMA}
MODES = ["", "accept-edits", "auto", "plan"]


def read_settings():
    """(dict, exists, error)"""
    try:
        data = json.loads(SETTINGS_PATH.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}, False, None
    except (ValueError, OSError) as e:
        return {}, True, str(e)
    if not isinstance(data, dict):
        return {}, True, "в файле не JSON-объект"
    return data, True, None


def coerce(f, v):
    t = f["type"]
    if v is None or v == "":
        return None
    if t == "select":
        if v not in f["options"]:
            raise ValueError(f"{f['key']}: недопустимое значение {v!r}")
        return v
    if t == "bool":
        if not isinstance(v, bool):
            raise ValueError(f"{f['key']}: ожидается true/false")
        return v
    if t == "int":
        if isinstance(v, bool) or not isinstance(v, int):
            raise ValueError(f"{f['key']}: ожидается целое число")
        return v
    if t == "text":
        if not isinstance(v, str) or "\n" in v or len(v) > 500:
            raise ValueError(f"{f['key']}: ожидается строка в одну строку")
        return v.strip()
    if t == "lines":
        if not isinstance(v, list) or not all(isinstance(x, str) and "\n" not in x for x in v):
            raise ValueError(f"{f['key']}: ожидается список строк")
        return [x.strip() for x in v if x.strip()] or None
    if t == "json":
        if not isinstance(v, dict):
            raise ValueError(f"{f['key']}: ожидается JSON-объект")
        return v or None
    raise ValueError("неизвестный тип")


def write_settings(cfg):
    SETTINGS_PATH.parent.mkdir(parents=True, exist_ok=True)
    if SETTINGS_PATH.exists():
        shutil.copy2(SETTINGS_PATH, SETTINGS_PATH.with_name(SETTINGS_PATH.name + ".webide-bak"))
    tmp = SETTINGS_PATH.with_name(SETTINGS_PATH.name + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        json.dump(cfg, fh, ensure_ascii=False, indent=2)
        fh.write("\n")
    os.replace(tmp, SETTINGS_PATH)


def load_state():
    try:
        d = json.loads(STATE_PATH.read_text(encoding="utf-8"))
        return d if isinstance(d, dict) else {}
    except (FileNotFoundError, ValueError):
        return {}


def project_flags(pid):
    return load_state().get("flags", {}).get(pid, {})


def flag_args(fl):
    a = []
    if fl.get("model"):
        a += ["--model", fl["model"]]
    if fl.get("mode"):
        a += ["--mode", fl["mode"]]
    if fl.get("effort"):
        a += ["--effort", fl["effort"]]
    if fl.get("sandbox"):
        a.append("--sandbox")
    if fl.get("skip"):
        a.append("--dangerously-skip-permissions")
    for d in fl.get("addDirs", []):
        a += ["--add-dir", d]
    if fl.get("extra"):
        a += shlex.split(fl["extra"])
    return a


def build_cmd(proj):
    """Шаблон cmd проекта + флаги проекта вместо {AGY_FLAGS}."""
    tpl = proj["cmd"]
    if "{AGY_FLAGS}" not in tpl:
        return tpl
    try:
        args = flag_args(project_flags(proj["id"]))
    except ValueError:
        args = []
    return tpl.replace("{AGY_FLAGS}", " ".join(shlex.quote(x) for x in args))


async def api_agy_get(request):
    cfg, exists, err = read_settings()
    out = {"path": str(SETTINGS_PATH), "exists": exists, "error": err, "schema": SCHEMA, "values": cfg}
    pid = request.query.get("project")
    if pid:
        proj = get_project(pid)
        s = sessions.get(pid)
        out["project"] = {"id": pid, "flags": project_flags(pid), "hasPlaceholder": "{AGY_FLAGS}" in proj["cmd"],
                          "alive": bool(s and s.alive), "modes": MODES}
    return web.json_response(out)


async def api_agy_put(request):
    b = await request.json()
    cfg, exists, err = read_settings()
    if err and not b.get("force"):
        raise web.HTTPConflict(text=f"settings.json повреждён ({err}). Исправьте файл вручную или сохраните с перезаписью.")
    try:
        for key, v in (b.get("values") or {}).items():
            f = SCHEMA_BY_KEY.get(key)
            if not f:
                raise ValueError(f"неизвестная настройка: {key}")
            v = coerce(f, v)
            if v is None or v == f["default"]:
                cfg.pop(key, None)  # agy хранит только отличия от значений по умолчанию
            else:
                cfg[key] = v
    except ValueError as e:
        raise web.HTTPBadRequest(text=str(e))
    try:
        write_settings(cfg)
    except OSError as e:
        raise web.HTTPInternalServerError(text=f"не удалось записать {SETTINGS_PATH}: {e}")
    return web.json_response({"ok": True, "values": cfg})


async def api_agy_flags(request):
    b = await request.json()
    pid = get_project(b.get("project"))["id"]
    raw, fl = b.get("flags") or {}, {}
    try:
        for k in ("model", "effort", "extra"):
            v = raw.get(k, "")
            if not isinstance(v, str) or "\n" in v or len(v) > 500:
                raise ValueError(f"{k}: ожидается строка в одну строку")
            if v.strip():
                fl[k] = v.strip()
        if raw.get("mode", "") not in MODES:
            raise ValueError("mode: недопустимое значение")
        if raw.get("mode"):
            fl["mode"] = raw["mode"]
        for k in ("sandbox", "skip"):
            if raw.get(k) is True:
                fl[k] = True
        dirs = raw.get("addDirs") or []
        if not isinstance(dirs, list) or not all(isinstance(x, str) and "\n" not in x for x in dirs):
            raise ValueError("addDirs: ожидается список строк")
        if [x.strip() for x in dirs if x.strip()]:
            fl["addDirs"] = [x.strip() for x in dirs if x.strip()]
        flag_args(fl)  # проверка, что extra разбирается shlex
    except ValueError as e:
        raise web.HTTPBadRequest(text=str(e))
    st = load_state()
    flags = st.setdefault("flags", {})
    if fl:
        flags[pid] = fl
    else:
        flags.pop(pid, None)
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = STATE_PATH.with_name(STATE_PATH.name + ".tmp")
    tmp.write_text(json.dumps(st, ensure_ascii=False, indent=1), encoding="utf-8")
    tmp.replace(STATE_PATH)
    return web.json_response({"ok": True, "flags": fl})


async def api_agy_restart(request):
    """Перезапуск агента проекта. continue=true: agy -c (продолжить последний диалог)."""
    b = await request.json()
    proj = get_project(b.get("project"))
    s = sessions.get(proj["id"])
    if s and s.alive:
        await s.kill()
    ensure_session(proj, cont=bool(b.get("continue", True)))
    return web.json_response({"ok": True})


# ───────────────────────── API для агента (Telegram и любые скрипты) ─────────────────────────

KEYS = {
    "enter": b"\r", "esc": b"\x1b", "tab": b"\t", "shift-tab": b"\x1b[Z", "space": b" ", "backspace": b"\x7f",
    "up": b"\x1b[A", "down": b"\x1b[B", "right": b"\x1b[C", "left": b"\x1b[D",
    "ctrl-c": b"\x03", "ctrl-d": b"\x04", "ctrl-l": b"\x0c", "ctrl-r": b"\x12",
}
ANSI_RE = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)|\x1b[()][A-Za-z0-9]|\x1b[=>]|\r")


def snapshot(s, lines=40):
    """Текущий вид терминала как обычный текст (последние `lines` непустых строк экрана)."""
    if s.screen:
        rows = [r.rstrip() for r in s.screen.display]
    else:
        rows = [r.rstrip() for r in ANSI_RE.sub("", bytes(s.buf[-16384:]).decode("utf-8", "replace")).split("\n")]
    while rows and not rows[-1]:
        rows.pop()
    while rows and not rows[0]:
        rows.pop(0)
    return "\n".join(rows[-lines:])


async def agent_send(s, text, enter=True):
    """Печатает текст в сессию. Enter отправляется отдельным нажатием: TUI принимает
    «текст+\\r» одним куском за вставку и не отправляет сообщение. Многострочный текст идёт как bracketed paste."""
    s.last_out = time.monotonic()
    text = text.replace("\r\n", "\n")
    if "\n" in text:
        s.write(b"\x1b[200~" + text.replace("\n", "\r").encode() + b"\x1b[201~")
    elif text:
        s.write(text.encode())
    if enter:
        await asyncio.sleep(0.2)
        s.write(b"\r")
    s.last_out = time.monotonic()


def agent_keys(s, names):
    for n in names:
        k = KEYS.get(n.lower())
        if k is None:
            raise web.HTTPBadRequest(text=f"неизвестная клавиша: {n}")
        s.write(k)
    s.last_out = time.monotonic()


async def wait_idle(s, quiet=4.0, timeout=120.0):
    """Ждёт, пока вывод не затихнет на `quiet` секунд (или процесс не завершится / не истечёт timeout)."""
    t0 = time.monotonic()
    await asyncio.sleep(min(0.5, quiet))
    while s.alive and time.monotonic() - s.last_out < quiet and time.monotonic() - t0 < timeout:
        await asyncio.sleep(0.2)


def _agent_reply(s, lines):
    return {"alive": s.alive, "screen": snapshot(s, lines), "idle": round(time.monotonic() - s.last_out, 1)}


async def api_agent_send(request):
    b = await request.json()
    s = ensure_session(get_project(b.get("project")))
    await agent_send(s, str(b.get("text", "")), enter=b.get("enter", True))
    if b.get("wait"):
        await wait_idle(s, float(b.get("quiet", 4)), float(b.get("timeout", 120)))
    return web.json_response(_agent_reply(s, _int(b.get("lines"), 40)))


async def api_agent_key(request):
    b = await request.json()
    s = ensure_session(get_project(b.get("project")))
    keys = b.get("keys") or [b.get("key", "")]
    agent_keys(s, keys)
    if b.get("wait"):
        await wait_idle(s, float(b.get("quiet", 4)), float(b.get("timeout", 120)))
    return web.json_response(_agent_reply(s, _int(b.get("lines"), 40)))


async def api_agent_screen(request):
    proj = get_project(request.query.get("project"))
    s = sessions.get(proj["id"])
    if not s or not s.alive:
        return web.json_response({"alive": False, "screen": "", "idle": None})
    return web.json_response(_agent_reply(s, _int(request.query.get("lines"), 40)))


async def ws_term(request):
    proj = get_project(request.query.get("project"))
    cols, rows = _int(request.query.get("cols"), 80), _int(request.query.get("rows"), 24)
    s = ensure_session(proj, cols, rows)
    s.resize(cols, rows)

    ws = web.WebSocketResponse(heartbeat=20, max_msg_size=8 * 1024 * 1024)
    await ws.prepare(request)
    q = asyncio.Queue()
    if s.buf:
        q.put_nowait(bytes(s.buf))
    s.clients.add(q)

    async def pump():
        while True:
            item = await q.get()
            if item is None:
                await ws.send_json({"type": "exit"})
                return
            await ws.send_bytes(item)

    task = asyncio.create_task(pump())
    try:
        async for msg in ws:
            if msg.type == WSMsgType.BINARY:
                s.write(msg.data)
            elif msg.type == WSMsgType.TEXT:
                try:
                    m = json.loads(msg.data)
                except ValueError:
                    continue
                if m.get("type") == "resize":
                    s.resize(_int(m.get("cols"), 80), _int(m.get("rows"), 24))
    finally:
        s.clients.discard(q)
        task.cancel()
    return ws


async def api_term_kill(request):
    proj = get_project(request.query.get("project"))
    s = sessions.get(proj["id"])
    if s and s.alive:
        await s.kill()
    return web.json_response({"ok": True})


# ───────────────────────── создание / переименование / удаление ─────────────────────────

async def api_fs(request):
    b = await request.json()
    proj = get_project(b.get("project"))
    root, p = resolve(proj, b.get("path", ""))
    op = b.get("op")
    if p == root and op in ("rename", "delete"):
        raise web.HTTPBadRequest(text="нельзя изменить корень проекта")
    try:
        if op in ("file", "dir"):
            if p.exists():
                raise web.HTTPConflict(text="уже существует")
            if op == "dir":
                p.mkdir(parents=True)
            else:
                p.parent.mkdir(parents=True, exist_ok=True)
                p.touch()
        elif op == "rename":
            _, q = resolve(proj, b.get("to", ""))
            if q.exists():
                raise web.HTTPConflict(text="уже существует")
            if q == p or p in q.parents:
                raise web.HTTPBadRequest(text="нельзя переместить папку в саму себя")
            q.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(p), str(q))
        elif op == "delete":
            if not p.exists():
                raise web.HTTPNotFound(text="не найдено")
            if p.is_dir():
                shutil.rmtree(p)
            else:
                p.unlink()
        else:
            raise web.HTTPBadRequest(text="unknown op")
    except OSError as e:
        raise web.HTTPBadRequest(text=str(e))
    return web.json_response({"ok": True})


# ───────────────────────── git ─────────────────────────

URL_RE = re.compile(r"^(https?://|ssh://|git://|git@[\w.-]+:)\S+$")
REF_RE = re.compile(r"^[\w][\w./-]{0,200}$")


async def run_git(cwd, *args, stdin=None, timeout=60, check=True):
    env = dict(
        os.environ, GIT_TERMINAL_PROMPT="0", GIT_EDITOR="true", LC_ALL="C.UTF-8",
        GIT_CONFIG_COUNT="1", GIT_CONFIG_KEY_0="safe.directory", GIT_CONFIG_VALUE_0="*",
    )
    sp = asyncio.subprocess
    try:
        proc = await asyncio.create_subprocess_exec(
            "git", *args, cwd=str(cwd), env=env,
            stdin=sp.PIPE if stdin is not None else sp.DEVNULL, stdout=sp.PIPE, stderr=sp.PIPE,
        )
    except FileNotFoundError:
        raise web.HTTPInternalServerError(text="git не установлен в контейнере")
    try:
        out, err = await asyncio.wait_for(proc.communicate(stdin), timeout)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        raise web.HTTPGatewayTimeout(text=f"git {args[0]}: таймаут {timeout} с")
    out, err = out.decode("utf-8", "replace"), err.decode("utf-8", "replace")
    if check and proc.returncode:
        raise web.HTTPBadRequest(text=(err or out).strip()[-1500:] or f"git {args[0]} завершился с ошибкой")
    return proc.returncode, out, err


def proj_root(request, key="project"):
    return Path(get_project(request.query.get(key))["path"]).resolve()


async def is_repo(root):
    rc, out, _ = await run_git(root, "rev-parse", "--show-toplevel", check=False)
    return rc == 0 and Path(out.strip()).resolve() == root


def parse_branch(line):
    info = {"name": "", "upstream": None, "ahead": 0, "behind": 0, "gone": False, "detached": False}
    s = line[3:]
    if s.startswith("No commits yet on "):
        info["name"] = s[len("No commits yet on "):]
        return info
    if s.startswith("HEAD (no branch)"):
        info.update(name="HEAD", detached=True)
        return info
    m = re.match(r"^(.+?)(?:\.\.\.(\S+))?(?: \[(.*)\])?$", s)
    if m:
        info["name"], info["upstream"] = m.group(1), m.group(2)
        br = m.group(3) or ""
        if "gone" in br:
            info["gone"] = True
        a, bh = re.search(r"ahead (\d+)", br), re.search(r"behind (\d+)", br)
        info["ahead"], info["behind"] = int(a.group(1)) if a else 0, int(bh.group(1)) if bh else 0
    return info


async def api_git_status(request):
    root = proj_root(request)
    if not await is_repo(root):
        return web.json_response({"repo": False})
    _, out, _ = await run_git(root, "status", "--porcelain=v1", "-b", "-z", "--untracked-files=all")
    parts, files, branch, i = out.split("\0"), [], {}, 0
    while i < len(parts):
        e = parts[i]
        i += 1
        if not e:
            continue
        if e.startswith("## "):
            branch = parse_branch(e)
            continue
        x, y, path = e[0], e[1], e[3:]
        if x in "RC":
            i += 1  # следующее поле: старый путь
        files.append({"path": path, "x": x, "y": y})
    _, rem, _ = await run_git(root, "remote", check=False)
    rc, _, _ = await run_git(root, "rev-parse", "--verify", "-q", "HEAD", check=False)
    return web.json_response({
        "repo": True, "branch": branch, "remotes": rem.split(), "hasCommits": rc == 0,
        "files": files[:3000], "truncated": len(files) > 3000,
    })


async def api_git_branches(request):
    root = proj_root(request)
    _, loc, _ = await run_git(root, "branch", "--format=%(refname:short)", check=False)
    _, rem, _ = await run_git(root, "branch", "-r", "--format=%(refname:short)", check=False)
    local = [x for x in loc.split("\n") if x and not x.startswith("(")]
    remote = [x for x in rem.split("\n") if x and "/HEAD" not in x and "/" in x]
    return web.json_response({"local": local, "remote": remote})


async def api_git_log(request):
    root = proj_root(request)
    rc, out, _ = await run_git(root, "log", "-n", "30", "--format=%h%x1f%s%x1f%an%x1f%ar", check=False)
    items = [dict(zip(("hash", "subject", "author", "when"), l.split("\x1f"))) for l in out.split("\n") if l] if rc == 0 else []
    return web.json_response(items)


async def api_git_show(request):
    proj = get_project(request.query.get("project"))
    root, p = resolve(proj, request.query.get("path", ""))
    rel = p.relative_to(root).as_posix()
    rc, out, _ = await run_git(root, "show", f"HEAD:{rel}", check=False)
    return web.json_response({"content": out if rc == 0 else "", "exists": rc == 0})


def _msg(out, err):
    return (out + "\n" + err).strip()[-1500:]


async def git_push(root):
    rc, _, _ = await run_git(root, "rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{u}", check=False)
    if rc == 0:
        return await run_git(root, "push", timeout=300)
    _, rem, _ = await run_git(root, "remote")
    remotes = rem.split()
    if not remotes:
        raise web.HTTPBadRequest(text="нет remote: сначала добавьте адрес репозитория")
    remote = "origin" if "origin" in remotes else remotes[0]
    return await run_git(root, "push", "-u", remote, "HEAD", timeout=300)


async def api_git_op(request):
    proj = get_project(request.query.get("project"))
    root = Path(proj["path"]).resolve()
    b = await request.json()
    op = b.get("op")
    if op == "init":
        _, out, err = await run_git(root, "init", "-b", "main")
        return web.json_response({"output": _msg(out, err)})
    if not await is_repo(root):
        raise web.HTTPBadRequest(text="это не git-репозиторий")
    rels = [resolve(proj, x)[1].relative_to(root).as_posix() or "." for x in b.get("paths", [])]
    out = err = ""
    if op == "stage":
        _, out, err = await run_git(root, "add", "-A", "--", *(rels or ["."]))
    elif op == "unstage":
        rc, _, _ = await run_git(root, "rev-parse", "--verify", "-q", "HEAD", check=False)
        if rc == 0:
            _, out, err = await run_git(root, "reset", "-q", "HEAD", "--", *(rels or ["."]))
        else:
            _, out, err = await run_git(root, "rm", "--cached", "-r", "-q", "--", *(rels or ["."]), check=False)
    elif op == "discard":
        for r in rels:
            rc, _, _ = await run_git(root, "ls-files", "--error-unmatch", "--", r, check=False)
            if rc == 0:
                await run_git(root, "restore", "--worktree", "--", r)
            else:
                _, t = resolve(proj, r)
                if t.is_dir():
                    shutil.rmtree(t)
                elif t.exists():
                    t.unlink()
    elif op == "commit":
        msg = (b.get("message") or "").strip()
        if not msg:
            raise web.HTTPBadRequest(text="пустое сообщение коммита")
        if b.get("all"):
            await run_git(root, "add", "-A")
        _, out, err = await run_git(root, "commit", "-m", msg)
    elif op == "fetch":
        _, out, err = await run_git(root, "fetch", "--all", "--prune", timeout=300)
    elif op == "pull":
        _, out, err = await run_git(root, "pull", "--no-rebase", "--no-edit", timeout=300)
    elif op == "push":
        _, out, err = await git_push(root)
    elif op == "sync":
        rc, _, _ = await run_git(root, "rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{u}", check=False)
        if rc == 0:
            _, o1, e1 = await run_git(root, "pull", "--no-rebase", "--no-edit", timeout=300)
        else:
            o1 = e1 = ""
        _, o2, e2 = await git_push(root)
        out, err = o1 + "\n" + o2, e1 + "\n" + e2
    elif op == "checkout":
        name = b.get("branch", "")
        if not REF_RE.match(name):
            raise web.HTTPBadRequest(text="некорректное имя ветки")
        args = ["checkout", "-b", name] if b.get("create") else ["checkout", name]
        _, out, err = await run_git(root, *args)
    elif op == "remote_add":
        url = (b.get("url") or "").strip()
        if not URL_RE.match(url):
            raise web.HTTPBadRequest(text="ожидается URL вида https://…, ssh://… или git@host:path")
        _, rem, _ = await run_git(root, "remote")
        await run_git(root, "remote", "set-url" if "origin" in rem.split() else "add", "origin", url)
    else:
        raise web.HTTPBadRequest(text="unknown op")
    return web.json_response({"output": _msg(out, err)})


async def api_project_new(request):
    b = await request.json()
    return web.json_response({"id": await create_project(b.get("url"), b.get("name"))})


async def create_project(url, name):
    """Новая пустая папка или git clone в WORKSPACE. Возвращает id проекта. Ошибки: web.HTTPException."""
    url, name = (url or "").strip(), (name or "").strip()
    if url:
        if not URL_RE.match(url):
            raise web.HTTPBadRequest(text="ожидается URL вида https://…, ssh://… или git@host:path")
        if not name:
            name = re.sub(r"\.git$", "", url.rstrip("/").split("/")[-1].split(":")[-1])
    if not re.fullmatch(r"[A-Za-z0-9][\w.-]{0,63}", name):
        raise web.HTTPBadRequest(text="имя проекта: латиница, цифры, . _ - (до 64 символов)")
    dest = WORKSPACE / name
    if dest.exists():
        raise web.HTTPConflict(text="такая папка уже есть")
    WORKSPACE.mkdir(parents=True, exist_ok=True)
    if url:
        await run_git(WORKSPACE, "clone", "--", url, name, timeout=900)
    else:
        dest.mkdir()
    return name


async def api_git_credentials(request):
    b = await request.json()
    host, user, token = (b.get("host") or "").strip(), (b.get("username") or "").strip(), b.get("token") or ""
    if not re.fullmatch(r"[\w.-]+(:\d+)?", host) or not user or not token or "\n" in user + token:
        raise web.HTTPBadRequest(text="нужны host, логин и токен")
    data = f"protocol=https\nhost={host}\nusername={user}\npassword={token}\n\n".encode()
    await run_git(BASE, "credential", "approve", stdin=data)
    return web.json_response({"ok": True})


# ───────────────────────── запуск ─────────────────────────

CDN = {
    "MONACO": "https://cdn.jsdelivr.net/npm/monaco-editor@0.52.0/min",
    "XTERM": "https://cdn.jsdelivr.net/npm/@xterm/xterm@5.5.0",
    "FIT": "https://cdn.jsdelivr.net/npm/@xterm/addon-fit@0.10.0",
}
LOCAL = {"MONACO": "/vendor/monaco-editor/min", "XTERM": "/vendor/xterm", "FIT": "/vendor/addon-fit"}
LOCAL_MARKERS = ["monaco-editor/min/vs/loader.js", "xterm/lib/xterm.js", "addon-fit/lib/addon-fit.js"]


async def index(request):
    """Если библиотеки скачаны в VENDOR_DIR, отдаём их локально, иначе берём с CDN."""
    local = all((VENDOR / m).is_file() for m in LOCAL_MARKERS)
    html = (BASE / "index.html").read_text(encoding="utf-8")
    for k, v in (LOCAL if local else CDN).items():
        html = html.replace("{{" + k + "}}", v)
    return web.Response(text=html, content_type="text/html", headers={"Cache-Control": "no-cache"})


def main():
    app = web.Application(middlewares=[guard], client_max_size=MAX_FILE + 2 ** 20)
    app.add_routes([
        web.get("/", index),
        web.get("/favicon.ico", lambda r: web.Response(status=204)),
        web.get("/api/projects", api_projects),
        web.get("/api/tree", api_tree),
        web.get("/api/file", api_file_get),
        web.put("/api/file", api_file_put),
        web.get("/api/stat", api_stat),
        web.get("/api/search", api_search),
        web.post("/api/term/kill", api_term_kill),
        web.get("/api/agy/settings", api_agy_get),
        web.put("/api/agy/settings", api_agy_put),
        web.put("/api/agy/flags", api_agy_flags),
        web.post("/api/agy/restart", api_agy_restart),
        web.post("/api/agent/send", api_agent_send),
        web.post("/api/agent/key", api_agent_key),
        web.get("/api/agent/screen", api_agent_screen),
        web.post("/api/fs", api_fs),
        web.get("/api/git/status", api_git_status),
        web.get("/api/git/branches", api_git_branches),
        web.get("/api/git/log", api_git_log),
        web.get("/api/git/show", api_git_show),
        web.post("/api/git/op", api_git_op),
        web.post("/api/git/credentials", api_git_credentials),
        web.post("/api/project/new", api_project_new),
        web.get("/ws/term", ws_term),
    ])
    import tgbot  # Telegram-мост; включается переменной TG_TOKEN
    tgbot.setup(app, sys.modules[__name__])
    if VENDOR.is_dir():
        app.router.add_static("/vendor", VENDOR)
    print(f"[webide] http://{HOST}:{PORT}  workspace={WORKSPACE}  vendor={'local' if VENDOR.is_dir() else 'CDN'}", flush=True)
    if GENERATED_PASS:
        print(f"[webide] AUTH_PASS не задан, сгенерирован: {AUTH_USER} / {AUTH_PASS}", flush=True)
    web.run_app(app, host=HOST, port=PORT, print=None)


if __name__ == "__main__":
    main()

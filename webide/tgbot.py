"""Telegram-мост для webide.

Каждый проект = отдельная тема (topic) в супергруппе-форуме. Текст в теме уходит в терминал agy этого
проекта, ответом приходит «снимок экрана» с кнопками (Enter, Esc, стрелки, Ctrl+C, 1/2/3, y/n).
Бот ходит наружу только через getUpdates (long polling), входящий порт не нужен.

Включается переменной TG_TOKEN. Доступ только пользователям из TG_ALLOWED_USERS.
"""
import asyncio
import html
import json
import os
import re
import time
from pathlib import Path

import aiohttp

TOKEN = os.environ.get("TG_TOKEN", "").strip()
API = os.environ.get("TG_API_BASE", "https://api.telegram.org").rstrip("/")
PROXY = os.environ.get("TG_PROXY", "").strip()
ALLOWED = {int(x) for x in re.findall(r"-?\d+", os.environ.get("TG_ALLOWED_USERS", ""))}
STATE_FILE = Path(os.environ.get("TG_STATE_FILE", "telegram.json"))
QUIET = float(os.environ.get("TG_QUIET", "5"))          # сколько секунд тишины = «агент закончил»
MAX_WAIT = float(os.environ.get("TG_MAX_WAIT", "900"))  # дольше этого не следим за одним запросом
EDIT_EVERY = float(os.environ.get("TG_EDIT_EVERY", "4"))
LINES = int(os.environ.get("TG_LINES", "40"))

COMMANDS = {"start", "help", "id", "projects", "setup", "bind", "unbind", "screen", "key", "stop",
            "restart", "git", "newproject"}
HELP = (
    "<b>agy-бот</b>: проект = тема форума, сообщение в теме уходит агенту.\n\n"
    "/setup: создать темы для всех проектов (в супергруппе-форуме, бот должен быть админом с правом управлять темами)\n"
    "/projects: список проектов\n"
    "/bind &lt;проект&gt;: привязать эту тему/чат к проекту, /unbind: отвязать\n"
    "/newproject &lt;имя&gt; [git-url]: создать проект (или клонировать) и тему для него\n"
    "/screen: показать экран агента, /stop: Ctrl+C, /restart: перезапустить сессию\n"
    "/key &lt;клавиши&gt;: например <code>/key down down enter</code> (enter esc tab up down left right ctrl-c ctrl-d space backspace)\n"
    "/git: git status проекта\n\n"
    "Чтобы отправить агенту текст, совпадающий с командой бота (например /screen), поставьте перед ним &gt;: <code>&gt;/screen</code>."
)
KEYBOARD = {"inline_keyboard": [
    [{"text": "↵ Enter", "callback_data": "k:enter"}, {"text": "Esc", "callback_data": "k:esc"},
     {"text": "↑", "callback_data": "k:up"}, {"text": "↓", "callback_data": "k:down"}],
    [{"text": "1", "callback_data": "t:1"}, {"text": "2", "callback_data": "t:2"}, {"text": "3", "callback_data": "t:3"},
     {"text": "y", "callback_data": "t:y"}, {"text": "n", "callback_data": "t:n"}],
    [{"text": "Tab", "callback_data": "k:tab"}, {"text": "Ctrl+C", "callback_data": "k:ctrl-c"},
     {"text": "🔄 Экран", "callback_data": "screen"}],
]}


class TgError(Exception):
    pass


def log(*a):
    print("[telegram]", *a, flush=True)


class Bot:
    def __init__(self, core):
        self.core = core
        self.http = None
        self.me = ""
        self.watchers = {}  # (chat, thread) -> Task
        try:
            self.state = json.loads(STATE_FILE.read_text())
        except (FileNotFoundError, ValueError):
            self.state = {}
        self.state.setdefault("topics", {})

    # ───────── низкий уровень ─────────

    def save(self):
        STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
        tmp = STATE_FILE.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.state, ensure_ascii=False, indent=1))
        tmp.replace(STATE_FILE)

    def make_http(self):
        if PROXY.startswith("socks"):
            try:
                from aiohttp_socks import ProxyConnector
            except ImportError:
                raise SystemExit("[telegram] для socks-прокси нужен пакет aiohttp-socks (apt: python3-aiohttp-socks)")
            conn = ProxyConnector.from_url(re.sub(r"^socks5h://", "socks5://", PROXY), rdns=True)
            return aiohttp.ClientSession(connector=conn)
        return aiohttp.ClientSession()

    async def call(self, method, _timeout=30, **params):
        url = f"{API}/bot{TOKEN}/{method}"
        extra = {"proxy": PROXY} if PROXY.startswith("http") else {}
        for attempt in (1, 2):
            async with self.http.post(url, json=params, timeout=aiohttp.ClientTimeout(total=_timeout), **extra) as r:
                try:
                    j = await r.json(content_type=None)
                except ValueError:
                    raise TgError(f"{method}: HTTP {r.status}")
            if j.get("ok"):
                return j["result"]
            wait = (j.get("parameters") or {}).get("retry_after")
            if j.get("error_code") == 429 and wait and attempt == 1:
                await asyncio.sleep(min(int(wait) + 1, 30))
                continue
            raise TgError(f"{method}: {j.get('description')}")

    async def send(self, chat, thread, text, markup=None):
        p = {"chat_id": chat, "text": text, "parse_mode": "HTML", "disable_web_page_preview": True}
        if thread:
            p["message_thread_id"] = thread
        if markup:
            p["reply_markup"] = markup
        return (await self.call("sendMessage", **p))["message_id"]

    async def edit(self, chat, mid, text, markup=None):
        p = {"chat_id": chat, "message_id": mid, "text": text, "parse_mode": "HTML"}
        if markup:
            p["reply_markup"] = markup
        try:
            await self.call("editMessageText", **p)
        except TgError as e:
            if "not modified" not in str(e):
                raise

    async def delete(self, chat, mid):
        try:
            await self.call("deleteMessage", chat_id=chat, message_id=mid)
        except TgError:
            pass

    # ───────── привязки ─────────

    @staticmethod
    def key(chat, thread):
        return f"{chat}:{thread or 0}"

    def project_of(self, chat, thread):
        pid = self.state["topics"].get(self.key(chat, thread))
        if not pid:
            return None
        for p in self.core.load_projects():
            if p["id"] == pid:
                return p
        return None

    def bind(self, chat, thread, pid):
        self.state["topics"][self.key(chat, thread)] = pid
        self.save()

    def topic_for(self, chat, pid):
        for k, v in self.state["topics"].items():
            if v == pid and k.startswith(f"{chat}:") and not k.endswith(":0"):
                return k
        return None

    # ───────── отображение ─────────

    def render(self, proj, status, s):
        text = self.core.snapshot(s, LINES) if s else ""
        if len(text) > 3500:
            text = "…" + text[-3500:]
        body = html.escape(text) if text.strip() else "(экран пуст)"
        return f"<b>{html.escape(proj['name'])}</b> · {status}\n<pre>{body}</pre>"

    async def show(self, chat, thread, proj, status="🟢 жду ввода", mid=None):
        s = self.core.sessions.get(proj["id"])
        if not s or not s.alive:
            status, s = "⚪ сессия не запущена", None
        text = self.render(proj, status, s)
        if mid:
            await self.edit(chat, mid, text, KEYBOARD)
        else:
            await self.send(chat, thread, text, KEYBOARD)

    # ───────── слежение за агентом ─────────

    def watch(self, chat, thread, proj):
        k = (chat, thread or 0)
        t = self.watchers.get(k)
        if t and not t.done():
            return  # уже следим: новые нажатия просто попадут в тот же вывод
        self.watchers[k] = asyncio.create_task(self._watch(chat, thread, proj))

    async def _watch(self, chat, thread, proj):
        s = self.core.sessions.get(proj["id"])
        if not s:
            return
        progress = last = None
        try:
            progress = await self.send(chat, thread, f"<b>{html.escape(proj['name'])}</b> · ⏳ работает…")
        except TgError as e:
            log("send:", e)
        t0 = last_edit = time.monotonic()
        step, status = max(0.2, min(1.0, QUIET / 4)), "🟢 жду ввода"
        try:
            await asyncio.sleep(min(0.5, QUIET))
            while True:
                now = time.monotonic()
                if not s.alive:
                    status = "⛔ процесс завершён"
                    break
                if now - s.last_out >= QUIET:
                    break
                if now - t0 > MAX_WAIT:
                    status = "⏱ всё ещё работает (лимит ожидания), нажмите 🔄"
                    break
                if progress and now - last_edit >= EDIT_EVERY:
                    last_edit = now
                    text = self.render(proj, "⏳ работает…", s)
                    if text != last:
                        last = text
                        try:
                            await self.edit(chat, progress, text)
                        except TgError as e:
                            log("edit:", e)
                await asyncio.sleep(step)
        finally:
            if progress:
                await self.delete(chat, progress)
            try:
                await self.show(chat, thread, proj, status)
            except TgError as e:
                log("final send:", e)

    async def forward(self, chat, thread, proj, text):
        s = self.core.ensure_session(proj)
        await self.core.agent_send(s, text)
        self.watch(chat, thread, proj)

    # ───────── команды ─────────

    async def reply(self, chat, thread, text):
        try:
            await self.send(chat, thread, text)
        except TgError as e:
            log("reply:", e)

    async def cmd(self, name, arg, chat, thread, uid, is_group):
        r = lambda t: self.reply(chat, thread, t)
        proj = self.project_of(chat, thread)
        if name in ("start", "help"):
            return await r(HELP)
        if name == "id":
            return await r(f"Ваш Telegram ID: <code>{uid}</code>\nЧат: <code>{chat}</code>")
        if name == "projects":
            ps = [p for p in self.core.load_projects() if p["id"] != "_root"]
            lines = [("🔗 " if self.topic_for(chat, p["id"]) or (proj and proj["id"] == p["id"]) else "▫️ ")
                     + html.escape(p["id"]) for p in ps]
            return await r("Проекты:\n" + ("\n".join(lines) or "нет") + "\n\n🔗 = есть тема/привязка")
        if name == "setup":
            if not is_group:
                return await r("/setup выполняется в супергруппе с включёнными темами (Forum).")
            made, errors = [], []
            for p in self.core.load_projects():
                if p["id"] == "_root" or self.topic_for(chat, p["id"]):
                    continue
                try:
                    res = await self.call("createForumTopic", chat_id=chat, name=p["name"][:128])
                    th = res["message_thread_id"]
                    self.bind(chat, th, p["id"])
                    made.append(p["id"])
                    await self.send(chat, th, f"Тема проекта <b>{html.escape(p['name'])}</b>. Пишите агенту сюда.\n/screen: экран, /help: команды")
                except TgError as e:
                    errors.append(f"{p['id']}: {e}")
                    break
            self.state["chat_id"] = chat
            self.save()
            msg = f"Создано тем: {len(made)}" + (f" ({', '.join(made)})" if made else "")
            if errors:
                msg += "\n⚠️ " + html.escape(errors[0]) + "\nНужна супергруппа с темами и бот-админ с правом «Управление темами»."
            return await r(msg)
        if name == "bind":
            pid = arg.strip()
            if not any(p["id"] == pid for p in self.core.load_projects()):
                return await r("Нет такого проекта. Список: /projects")
            self.bind(chat, thread, pid)
            return await r(f"Привязано к проекту <b>{html.escape(pid)}</b>")
        if name == "unbind":
            self.state["topics"].pop(self.key(chat, thread), None)
            self.save()
            return await r("Привязка снята")
        if name == "newproject":
            parts = arg.split()
            if not parts:
                return await r("Использование: /newproject &lt;имя&gt; [git-url]")
            try:
                pid = await self.core.create_project(parts[1] if len(parts) > 1 else "", parts[0])
            except Exception as e:  # web.HTTPException с текстом причины
                return await r("⚠️ " + html.escape(getattr(e, "text", None) or str(e)))
            if is_group:
                try:
                    th = (await self.call("createForumTopic", chat_id=chat, name=pid))["message_thread_id"]
                    self.bind(chat, th, pid)
                    return await self.send(chat, th, f"Проект <b>{html.escape(pid)}</b> создан. Пишите агенту сюда.")
                except TgError as e:
                    return await r(f"Проект создан, но тему создать не удалось: {html.escape(str(e))}\nПривяжите вручную: /bind {pid}")
            self.bind(chat, thread, pid)
            return await r(f"Проект <b>{html.escape(pid)}</b> создан и выбран.")
        # ниже нужен привязанный проект
        if not proj:
            return await r("Эта тема/чат не привязаны к проекту: /bind &lt;проект&gt; или /setup")
        if name == "screen":
            return await self.show(chat, thread, proj)
        if name == "git":
            _, out, err = await self.core.run_git(Path(proj["path"]), "status", "-sb", check=False)
            return await r(f"<pre>{html.escape((out or err).strip()[-3500:] or '(пусто)')}</pre>")
        if name == "restart":
            s = self.core.sessions.get(proj["id"])
            if s and s.alive:
                await s.kill()
            s = self.core.ensure_session(proj)
            self.watch(chat, thread, proj)
            return
        if name in ("stop", "key"):
            keys = ["ctrl-c"] if name == "stop" else arg.split()
            if not keys:
                return await r("Использование: /key down down enter")
            s = self.core.ensure_session(proj)
            try:
                self.core.agent_keys(s, keys)
            except Exception as e:
                return await r("⚠️ " + html.escape(getattr(e, "text", None) or str(e)))
            return self.watch(chat, thread, proj)

    # ───────── входящие ─────────

    async def on_message(self, m):
        text = m.get("text")
        if not text:
            return
        uid = (m.get("from") or {}).get("id")
        chat = m["chat"]["id"]
        thread = m.get("message_thread_id") if m.get("is_topic_message") else 0
        is_group = m["chat"].get("type") in ("group", "supergroup")
        mt = re.match(r"^/(\w+)(?:@(\w+))?(?:\s+(.*))?$", text, re.S)
        cname = mt.group(1).lower() if mt and mt.group(1).lower() in COMMANDS and (not mt.group(2) or mt.group(2).lower() == self.me.lower()) else None
        if uid not in ALLOWED:
            if cname == "id" or (cname == "start" and not ALLOWED):
                await self.reply(chat, thread, f"Ваш Telegram ID: <code>{uid}</code>\nДобавьте его в TG_ALLOWED_USERS и перезапустите контейнер.")
            return
        if cname:
            return await self.cmd(cname, (mt.group(3) or "").strip(), chat, thread, uid, is_group)
        if text.startswith(">"):
            text = text[1:].lstrip()
        proj = self.project_of(chat, thread)
        if not proj:
            return await self.reply(chat, thread, "Эта тема/чат не привязаны к проекту: /bind &lt;проект&gt; или /setup")
        await self.forward(chat, thread, proj, text)

    async def on_callback(self, cb):
        uid = (cb.get("from") or {}).get("id")
        if uid not in ALLOWED:
            return await self.call("answerCallbackQuery", callback_query_id=cb["id"])
        msg = cb.get("message") or {}
        chat = (msg.get("chat") or {}).get("id")
        thread = msg.get("message_thread_id") if msg.get("is_topic_message") else 0
        proj = self.project_of(chat, thread)
        data = cb.get("data", "")
        note = None
        try:
            if not proj:
                note = "Тема не привязана"
            elif data == "screen":
                await self.show(chat, thread, proj, mid=msg["message_id"])
            elif data.startswith("k:") or data.startswith("t:"):
                s = self.core.ensure_session(proj)
                if data[0] == "k":
                    self.core.agent_keys(s, [data[2:]])
                else:
                    await self.core.agent_send(s, data[2:], enter=False)
                self.watch(chat, thread, proj)
        except Exception as e:
            note = (getattr(e, "text", None) or str(e))[:150]
        await self.call("answerCallbackQuery", callback_query_id=cb["id"], **({"text": note} if note else {}))

    async def handle(self, u):
        try:
            if "message" in u:
                await self.on_message(u["message"])
            elif "callback_query" in u:
                await self.on_callback(u["callback_query"])
        except Exception as e:
            log("handler error:", repr(e))

    async def run(self):
        self.http = self.make_http()
        offset, backoff = 0, 2
        try:
            while True:
                try:
                    if not self.me:
                        self.me = (await self.call("getMe")).get("username", "")
                        log(f"запущен как @{self.me}; разрешённых пользователей: {len(ALLOWED)}; прокси: {PROXY or 'нет'}")
                    ups = await self.call("getUpdates", _timeout=45, offset=offset, timeout=30,
                                          allowed_updates=["message", "callback_query"])
                    backoff = 2
                    for u in ups:
                        offset = u["update_id"] + 1
                        await self.handle(u)
                except asyncio.CancelledError:
                    raise
                except Exception as e:
                    log(f"ошибка связи: {e!r}; повтор через {backoff} с")
                    await asyncio.sleep(backoff)
                    backoff = min(backoff * 2, 30)
        finally:
            for t in self.watchers.values():
                t.cancel()
            await self.http.close()


def setup(app, core):
    if not TOKEN:
        return
    if not ALLOWED:
        log("ВНИМАНИЕ: TG_ALLOWED_USERS пуст, бот никому не отвечает (кроме /id). Узнайте свой ID командой /id и впишите в .env")
    bot = Bot(core)

    async def start(app):
        app["tg"] = asyncio.create_task(bot.run())

    async def stop(app):
        app["tg"].cancel()
        try:
            await app["tg"]
        except (asyncio.CancelledError, SystemExit):
            pass

    app.on_startup.append(start)
    app.on_cleanup.append(stop)

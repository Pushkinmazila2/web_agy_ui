#!/bin/bash
# Идемпотентная подготовка контейнера, затем запуск webide.
# Всё сетевое идёт через socks5h-прокси (PROXY_URL).
export DEBIAN_FRONTEND=noninteractive
PROXY="${PROXY_URL:-socks5h://vless-client:1080}"
AGY_URL="${AGY_DWNL_URL:-}"
log() { echo "[entrypoint] $*"; }

# ── прокси для curl ──
echo "proxy = \"$PROXY\"" > /root/.curlrc

# Настройка прокси для apt (используем socks5, так как socks5h утилитой apt не поддерживается)
APT_PROXY=$(echo "$PROXY" | sed 's/socks5h/socks5/')
printf 'Acquire::http::Proxy "%s";\nAcquire::https::Proxy "%s";\n' "$APT_PROXY" "$APT_PROXY" > /etc/apt/apt.conf.d/80proxy

# ── пакеты (пропускается, если всё уже стоит) ──
need=0
for c in python3 git curl nc ssh tar; do command -v "$c" >/dev/null 2>&1 || need=1; done
python3 -c 'import aiohttp, pyte, aiohttp_socks' 2>/dev/null || need=1

if [ "$need" = 1 ]; then
  log "устанавливаю пакеты через прокси…"
  # Обновляем apt и ставим системные пакеты (исправлена опечатка в имени пакета)
  if ! (apt-get update && apt-get install -yq --no-install-recommends \
        python3 python3-aiohttp python3-pyte python3-aiohttp-socks git curl ca-certificates tar netcat-openbsd openssh-client less procps nano); then
    log "Предупреждение: не удалось установить системные пакеты через apt, пробуем через pip..."
  fi
fi

# ── Доустановка через pip (pip отлично поддерживает socks5h) ──
if ! python3 -c 'import aiohttp, pyte, aiohttp_socks' 2>/dev/null; then
  log "python-пакеты не нашлись в apt, ставлю через pip…"
  command -v pip3 >/dev/null 2>&1 || apt-get install -yq --no-install-recommends python3-pip >/dev/null 2>&1
  pip3 install --break-system-packages -q aiohttp pyte aiohttp-socks 2>&1 | tail -2
fi

# ── agy ──
mkdir -p /root/.local/bin /root/.antigravity
if [ ! -x /root/.local/bin/agy ]; then
  if [ -z "$AGY_URL" ]; then
    log "AGY_DWNL_URL не задан, agy не установлен"
  else
    log "скачиваю agy…"
    if curl -fsSL "$AGY_URL" -o /tmp/agy.tar.gz \
       && tar -xzf /tmp/agy.tar.gz -C /root/.local/bin antigravity \
       && mv /root/.local/bin/antigravity /root/.local/bin/agy \
       && chmod +x /root/.local/bin/agy; then
      /root/.local/bin/agy install || true
    else
      log "ОШИБКА: не удалось скачать/распаковать agy"
    fi
    rm -f /tmp/agy.tar.gz
  fi
fi

# ── Monaco и xterm.js ──
V="${VENDOR_DIR:-/opt/webide/vendor}"
if [ ! -f "$V/monaco-editor/min/vs/loader.js" ] || [ ! -f "$V/xterm/lib/xterm.js" ] || [ ! -f "$V/addon-fit/lib/addon-fit.js" ]; then
  log "скачиваю Monaco и xterm.js в $V…"
  mkdir -p "$V"
  fetch_pkg() { 
    local url="$1" name="$2"; shift 2
    local tmp; tmp=$(mktemp -d)
    if curl -fsSL "$url" | tar -xz -C "$tmp" --wildcards "$@"; then
      rm -rf "${V:?}/$name" && mv "$tmp/package" "$V/$name"
    else
      log "не удалось скачать $name, будет использован CDN"
    fi
    rm -rf "$tmp"
  }
  fetch_pkg https://registry.npmjs.org/monaco-editor/-/monaco-editor-0.52.0.tgz monaco-editor 'package/min/*'
  fetch_pkg https://registry.npmjs.org/@xterm/xterm/-/xterm-5.5.0.tgz xterm 'package/lib/*' 'package/css/*'
  fetch_pkg https://registry.npmjs.org/@xterm/addon-fit/-/addon-fit-0.10.0.tgz addon-fit 'package/lib/*'
fi

# ── git ──
git config --global http.proxy "$PROXY"
git config --global --replace-all safe.directory '*'
git config --global credential.helper "store --file=/root/.antigravity/git-credentials"
[ -n "${GIT_USER_NAME:-}" ]  && git config --global user.name  "$GIT_USER_NAME"
[ -n "${GIT_USER_EMAIL:-}" ] && git config --global user.email "$GIT_USER_EMAIL"

# ── ssh-ключи ──
mkdir -p /root/.ssh && chmod 700 /root/.ssh
chmod 600 /root/.ssh/id_* 2>/dev/null || true

log "запускаю webide на порту ${PORT:-8080}"
exec python3 /opt/webide/server.py

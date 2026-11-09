#!/bin/bash
export DEBIAN_FRONTEND=noninteractive
PROXY="${PROXY_URL:-socks5h://vless-client:1080}"
AGY_URL="${AGY_DWNL_URL:-}"

log() { echo "$(date -u +"%Y-%m-%dT%H:%M:%SZ") [entrypoint] $*"; }
log_err() { echo "$(date -u +"%Y-%m-%dT%H:%M:%SZ") [entrypoint] [ERROR] $*" >&2; }

log "=== Инициализация контейнера ==="

PROXY_HOST=$(echo "$PROXY" | sed -e 's/.*\/\///' -e 's/:.*//')
PROXY_PORT=$(echo "$PROXY" | sed -e 's/.*://')

log "Ожидание готовности прокси-сервера $PROXY_HOST:$PROXY_PORT..."
while ! nc -z -w 2 "$PROXY_HOST" "$PROXY_PORT" 2>/dev/null; do
  log "Прокси еще не отвечает на порту $PROXY_PORT, ждем 2 секунды..."
  sleep 2
done
log "Прокси-контейнер ответил! Туннель доступен."

echo "proxy = \"$PROXY\"" > /root/.curlrc

APT_PROXY=$(echo "$PROXY" | sed 's/socks5h/socks5/')
printf 'Acquire::http::Proxy "%s";\nAcquire::https::Proxy "%s";\n' "$APT_PROXY" "$APT_PROXY" > /etc/apt/apt.conf.d/80proxy

unset HTTP_PROXY HTTPS_PROXY ALL_PROXY http_proxy https_proxy all_proxy

log "Проверка внешнего IP через прокси..."
ip=$(curl -fsSL --connect-timeout 5 https://ipify.org 2>/dev/null)
if [ -n "$ip" ]; then
  log "Успех! Внешний IP через vless прокси: $ip"
else
  log_err "Прокси открыт, но трафик через него не идет (проверьте настройки vless-client/Xray)"
fi

log "=== Инициализация контейнера ==="

# ── Настройка прокси для curl ──
echo "proxy = \"$PROXY\"" > /root/.curlrc

# ── Настройка прокси для apt (трансформируем socks5h в socks5, чтобы apt не падал) ──
APT_PROXY=$(echo "$PROXY" | sed 's/socks5h/socks5/')
printf 'Acquire::http::Proxy "%s";\nAcquire::https::Proxy "%s";\n' "$APT_PROXY" "$APT_PROXY" > /etc/apt/apt.conf.d/80proxy

# 2. Проверяем IP ПОСЛЕ настройки прокси (теперь curl пойдет через .curlrc)
check_external_ip "ПОСЛЕ НАСТРОЙКИ ПРОКСИ" ""

# ── пакеты (пропускается, если всё уже стоит) ──
need=0
for c in python3 git curl nc ssh tar; do command -v "$c" >/dev/null 2>&1 || need=1; done

# Проверяем библиотеки (убран pyte, так как его нет в базовом Dockerfile, добавлен python3-aiohttp-socks)
python3 -c 'import aiohttp, aiohttp_socks' 2>/dev/null || need=1

if [ "$need" = 1 ]; then
  log "Некоторые пакеты отсутствуют. Устанавливаю зависимости через apt…"
  
  # Запуск apt-get update в тихом режиме
  if apt-get update -yq >/dev/null 2>&1; then
    if apt-get install -yq --no-install-recommends \
        python3 python3-aiohttp python3-aiohttp-socks python3-pip git curl ca-certificates \
        tar netcat-openbsd openssh-client less procps nano >/dev/null 2>&1; then
      log "Системные пакеты и python3-aiohttp-socks успешно установлены."
    else
      log_err "Не удалось установить пакеты через apt. Пробуем резервный вариант с pip..."
      need_pip=1
    fi
  else
    log_err "apt-get update завершился с ошибкой. Проверяем доступность vless-client."
    need_pip=1
  fi
fi

# ── запасной путь через pip, если apt подвел (pip отлично переваривает socks5h) ──
if ! python3 -c 'import aiohttp, aiohttp_socks' 2>/dev/null || [ "$need_pip" = 1 ]; then
  log "Установка необходимых Python-модулей через pip3…"
  command -v pip3 >/dev/null 2>&1 || apt-get install -yq --no-install-recommends python3-pip >/dev/null 2>&1
  if pip3 install --break-system-packages -q aiohttp aiohttp-socks 2>/dev/null; then
    log "Python-пакеты успешно доустановлены через pip."
  else
    log_err "КРИТИЧЕСКАЯ ОШИБКА: pip не смог установить aiohttp-socks. Проверьте сеть бота."
  fi
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
      log "agy успешно установлен."
    else
      log_err "не удалось скачать/распаковать agy"
    fi
    rm -f /tmp/agy.tar.gz
  fi
fi

# ── Monaco и xterm.js: качаем из npm-реестра через прокси и отдаём локально ──
V="${VENDOR_DIR:-/opt/webide/vendor}"
if [ ! -f "$V/monaco-editor/min/vs/loader.js" ] || [ ! -f "$V/xterm/lib/xterm.js" ] || [ ! -f "$V/addon-fit/lib/addon-fit.js" ]; then
  log "скачиваю Monaco и xterm.js в $V…"
  mkdir -p "$V"
  fetch_pkg() { 
    local url="$1" name="$2"; shift 2
    local tmp; tmp=$(mktemp -d)
    if curl -fsSL "$url" | tar -xz -C "$tmp" --wildcards "$@"; then
      rm -rf "${V:?}/$name" && mv "$tmp/package" "$V/$name"
      log "Компонент $name успешно скачан."
    else
      log_err "не удалось скачать $name, будет использован CDN"
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

# ── ssh-ключи (если смонтированы) ──
mkdir -p /root/.ssh && chmod 700 /root/.ssh
chmod 600 /root/.ssh/id_* 2>/dev/null || true

log "запускаю webide на порту ${PORT:-8080}"
exec python3 /opt/webide/server.py

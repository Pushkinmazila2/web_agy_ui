# Сборка не ходит в сеть: прокси vless-client доступен только внутри сети xray_network,
# поэтому пакеты и agy ставятся при первом старте контейнера (см. entrypoint.sh).
FROM ubuntu:24.04

ENV LANG=C.UTF-8 \
    DEBIAN_FRONTEND=noninteractive \
    PYTHONUNBUFFERED=1

COPY webide/ /opt/webide/
COPY entrypoint.sh /usr/local/bin/entrypoint.sh
RUN chmod +x /usr/local/bin/entrypoint.sh && mkdir -p /workspace

WORKDIR /workspace
EXPOSE 7681
ENTRYPOINT ["/usr/local/bin/entrypoint.sh"]

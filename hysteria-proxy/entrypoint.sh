#!/bin/sh
# Парсит ссылку вида hysteria2://AUTH@HOST:PORT/?insecure=1&sni=SNI#name
# из переменной окружения HYSTERIA2_URL и собирает /etc/hysteria/config.yaml
# перед запуском самого hysteria-клиента. Так не нужно руками разбирать
# ссылку на составляющие — просто вставляешь её целиком.
set -e

if [ -z "$HYSTERIA2_URL" ]; then
    echo "[entrypoint] HYSTERIA2_URL не задан — прокси не запущен."
    echo "[entrypoint] Впиши ссылку hysteria2://... в docker-compose.yml (переменная HYSTERIA2_URL) и перезапусти."
    # Не падаем полностью — просто спим, чтобы контейнер не перезапускался в цикле
    sleep infinity
fi

echo "[entrypoint] Разбираю ссылку..."

URL="$HYSTERIA2_URL"

# отрезаем схему
rest="${URL#hysteria2://}"
# отрезаем имя после # (необязательное)
rest="${rest%%#*}"

# authpart@hostportquery
authpart="${rest%%@*}"
hostportquery="${rest#*@}"

# hostport / ?query
hostport="${hostportquery%%\?*}"
hostport="${hostport%/}"
query="${hostportquery#*\?}"

sni=$(printf '%s' "$query" | tr '&' '\n' | grep '^sni=' | head -1 | cut -d= -f2-)
insecure_raw=$(printf '%s' "$query" | tr '&' '\n' | grep '^insecure=' | head -1 | cut -d= -f2-)

if [ "$insecure_raw" = "1" ] || [ "$insecure_raw" = "true" ]; then
    insecure_yaml="true"
else
    insecure_yaml="false"
fi

if [ -z "$hostport" ] || [ -z "$authpart" ]; then
    echo "[entrypoint] ОШИБКА: не удалось разобрать ссылку. Проверь формат:"
    echo "[entrypoint]   hysteria2://AUTH@HOST:PORT/?insecure=1&sni=example.com#name"
    echo "[entrypoint] Получено HYSTERIA2_URL (без изменений): $HYSTERIA2_URL"
    sleep infinity
fi

mkdir -p /etc/hysteria
cat > /etc/hysteria/config.yaml << EOF
server: ${hostport}
auth: ${authpart}

tls:
  sni: ${sni}
  insecure: ${insecure_yaml}

socks5:
  listen: 0.0.0.0:1080
EOF

echo "[entrypoint] Конфиг собран: server=${hostport}, sni=${sni:-<пусто>}, insecure=${insecure_yaml}"
echo "[entrypoint] (пароль/auth в лог намеренно не печатаю)"
echo "[entrypoint] Запускаю hysteria client..."

exec hysteria client -c /etc/hysteria/config.yaml

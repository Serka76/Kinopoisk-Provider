# Берём готовый образ с бинарником hysteria (образ на базе Alpine, есть
# busybox sh/grep/cut/tr, которые нужны entrypoint.sh) и просто добавляем
# свой entrypoint поверх — сам бинарник не трогаем.
FROM tobyxdd/hysteria:latest

COPY entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

ENTRYPOINT ["/entrypoint.sh"]

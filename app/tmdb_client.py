# -*- coding: utf-8 -*-
"""
Клиент к TMDB (The Movie Database) — используется только для фото
персон в фильмах НЕ российского производства (см. поле countries у
фильма). Для российских фильмов фото по-прежнему берутся с Кинопоиска
через poiskkino_client.py.

Получить бесплатный API-ключ: https://www.themoviedb.org/settings/api
(v3 auth, обычный API Key, не Read Access Token).
"""
import os
import asyncio
import httpx

TMDB_API_KEY = os.environ.get("TMDB_API_KEY", "").strip()
TMDB_PROXY_URL = os.environ.get("TMDB_PROXY_URL", "").strip()
# Адрес, по которому САМ Plex достучится до нашего контейнера (не
# localhost и не внутреннее docker-имя — то, что реально прописано в
# Plex как URL провайдера). Нужен, чтобы отдавать Plex'у картинки через
# наш /image-proxy, а не напрямую с image.tmdb.org — тот домен у Plex
# тоже, скорее всего, заблокирован (Plex ходит за картинками сам, в
# обход нашего Hysteria2-прокси).
PUBLIC_BASE_URL = os.environ.get("PUBLIC_BASE_URL", "").strip().rstrip("/")
BASE = "https://api.themoviedb.org/3"
IMAGE_BASE = "https://image.tmdb.org/t/p/w500"

TIMEOUT = httpx.Timeout(10.0, connect=5.0)

# У контейнера на Synology нет рабочего исходящего IPv6 — при попытке
# подключиться к хостам, у которых есть AAAA-запись (как у TMDB),
# получаем "Cannot assign requested address". poiskkino.dev это не
# задевало (видимо, только IPv4-запись), а TMDB — задевает. Форсируем
# IPv4 через local_address="0.0.0.0" — стандартный трюк для httpx/httpcore.
# Оказалось недостаточно для доступа к TMDB (он блокируется целиком, не
# только по IPv6) — используется только как fallback, если прокси не
# настроен, вреда не приносит.
IPV4_TRANSPORT = httpx.AsyncHTTPTransport(local_address="0.0.0.0")


class TmdbClient:
    def __init__(self, api_key: str | None = None):
        self.api_key = api_key or TMDB_API_KEY

    async def search_person_photo(self, client: httpx.AsyncClient, name: str) -> str | None:
        """Ищет персону по имени, возвращает URL фото первого результата
        с непустым profile_path, либо None если ничего не найдено.

        Поддерживает оба формата ключа TMDB:
        - v3 API Key (короткая строка, ~32 символа) -> query-параметр api_key
        - v4 Read Access Token (длинный JWT, начинается на "eyJ") -> заголовок Authorization: Bearer
        """
        if not self.api_key or not name:
            return None
        try:
            if self.api_key.startswith("eyJ"):
                headers = {"Authorization": f"Bearer {self.api_key}"}
                params = {"query": name, "language": "en-US"}
            else:
                headers = {}
                params = {"api_key": self.api_key, "query": name, "language": "en-US"}

            resp = await client.get(f"{BASE}/search/person", params=params, headers=headers)
            if resp.status_code != 200:
                # раньше это молча проглатывалось — теперь видно причину
                # (например 401 = неверный ключ, 404/422 = что-то не так с запросом)
                print(f"[tmdb_client] search_person_photo({name!r}) -> HTTP {resp.status_code}: {resp.text[:200]}")
                return None
            results = (resp.json() or {}).get("results", [])
            for r in results:
                if r.get("profile_path"):
                    tmdb_url = IMAGE_BASE + r["profile_path"]
                    if PUBLIC_BASE_URL:
                        # Отдаём Plex'у ссылку на НАШ /image-proxy, а не
                        # напрямую на TMDB — Plex сам качает картинки в
                        # обход нашего прокси и упрётся в ту же блокировку.
                        from urllib.parse import quote
                        return f"{PUBLIC_BASE_URL}/image-proxy?url={quote(tmdb_url, safe='')}"
                    return tmdb_url
            return None
        except httpx.HTTPError as e:
            print(f"[tmdb_client] search_person_photo({name!r}) -> exception: {e}")
            return None

    def _build_client_kwargs(self) -> dict:
        if TMDB_PROXY_URL:
            return {"timeout": TIMEOUT, "proxy": TMDB_PROXY_URL}
        return {"timeout": TIMEOUT, "transport": IPV4_TRANSPORT}

    async def fetch_image_bytes(self, url: str) -> tuple[bytes, str] | None:
        """Скачивает картинку (обычно с image.tmdb.org) через тот же
        прокси, что и поиск персон. Используется эндпоинтом
        /image-proxy в main.py, чтобы Plex получал картинку от НАС, а
        не пытался сам сходить на заблокированный домен TMDB."""
        try:
            async with httpx.AsyncClient(**self._build_client_kwargs()) as client:
                resp = await client.get(url)
                if resp.status_code != 200:
                    print(f"[tmdb_client] fetch_image_bytes({url!r}) -> HTTP {resp.status_code}")
                    return None
                content_type = resp.headers.get("content-type", "image/jpeg")
                return resp.content, content_type
        except httpx.HTTPError as e:
            print(f"[tmdb_client] fetch_image_bytes({url!r}) -> exception: {e}")
            return None

    async def enrich_photos(self, persons: list[dict]) -> dict[str, str]:
        """Параллельно ищет фото для списка персон {name, enName}.
        Возвращает словарь {исходное_имя: url_фото} только для тех, кого
        удалось найти. Использует enName (английское имя), если есть —
        по нему TMDB ищет надёжнее, чем по кириллице."""
        if not self.api_key:
            print("[tmdb_client] enrich_photos: TMDB_API_KEY не задан (пусто) — пропускаю TMDB совсем")
            return {}
        print(f"[tmdb_client] enrich_photos: ключ задан (длина {len(self.api_key)} символов), "
              f"ищу фото для {len([p for p in persons if p.get('name')])} персон")

        if TMDB_PROXY_URL:
            print(f"[tmdb_client] enrich_photos: используем прокси ({TMDB_PROXY_URL.split('@')[-1]})")
        client_kwargs = self._build_client_kwargs()

        async with httpx.AsyncClient(**client_kwargs) as client:
            names = [(p.get("name"), p.get("enName") or p.get("name")) for p in persons if p.get("name")]
            tasks = [self.search_person_photo(client, en_name) for _, en_name in names]
            results = await asyncio.gather(*tasks, return_exceptions=True)

        photo_map = {}
        for (orig_name, _), result in zip(names, results):
            if isinstance(result, str):
                photo_map[orig_name] = result
        return photo_map

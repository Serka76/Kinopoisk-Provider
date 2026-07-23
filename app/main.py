# -*- coding: utf-8 -*-
"""
Kinopoisk (via poiskkino.dev) Custom Metadata Provider для Plex Media
Server (PMS 1.43+).

Реализует три эндпоинта, которые ожидает PMS от Custom Metadata Provider
для типа "movie" (см. README.md — как это регистрируется в Plex):

  GET  /movie                              -> описание провайдера (MediaProvider)
  POST /movie/library/metadata/matches     -> поиск/сопоставление (Match)
  GET  /movie/library/metadata/{ratingKey} -> полные метаданные по одному фильму

Порт по умолчанию 8000 (см. docker-compose.yml).
"""
import time
from typing import Optional

from fastapi import FastAPI, HTTPException
from fastapi.responses import Response
from pydantic import BaseModel

from app.poiskkino_client import PoiskKinoClient
from app.poiskkino_mapper import map_search_result, build_full_metadata, PROVIDER_ID
from app.tmdb_client import TmdbClient
from app.version import VERSION, BUILD_DATE, AUTHOR

app = FastAPI(
    title="Kinopoisk (poiskkino.dev) Custom Metadata Provider",
    description="Автор: Sergey Sychev",
)
client = PoiskKinoClient()
tmdb_client = TmdbClient()

print(f"[startup] Kinopoisk (poiskkino.dev) provider — version {VERSION} (build {BUILD_DATE}), автор: {AUTHOR}")

# простейший in-memory кэш, чтобы не жечь дневную квоту API при
# повторных Refresh Metadata / Match — на free-тарифе poiskkino.dev это
# особенно важно (200 запросов/сутки, а на карточку уходит 2-3 запроса)
_cache: dict[str, tuple[float, dict]] = {}
CACHE_TTL_SECONDS = 60 * 60 * 12  # 12 часов


def cache_get(key: str) -> Optional[dict]:
    entry = _cache.get(key)
    if not entry:
        return None
    ts, value = entry
    if time.time() - ts > CACHE_TTL_SECONDS:
        _cache.pop(key, None)
        return None
    return value


def cache_set(key: str, value: dict) -> None:
    _cache[key] = (time.time(), value)


class MatchRequest(BaseModel):
    type: Optional[int] = None
    title: Optional[str] = None
    year: Optional[int] = None
    guid: Optional[str] = None
    manual: Optional[int] = 0


@app.get("/movie")
async def media_provider():
    """Дескриптор провайдера — то, что PMS запрашивает при добавлении
    провайдера по URL в Settings -> Metadata Agents -> Add Provider."""
    return {
        "MediaProvider": {
            "identifier": PROVIDER_ID,
            "title": f"Кинопоиск (poiskkino.dev) v{VERSION}",
            "version": VERSION,
            "Types": [
                {
                    "type": 1,  # 1 = movie в нумерации типов Plex
                    "Scheme": [{"scheme": PROVIDER_ID}],
                }
            ],
            "Feature": [
                {"type": "metadata", "key": "/library/metadata"},
                {"type": "match", "key": "/library/metadata/matches"},
            ],
        }
    }


@app.post("/movie/library/metadata/matches")
async def match(req: MatchRequest):
    """Поиск фильма по названию/году (или по guid, если уже есть matchId
    из другого провайдера, выступающего первым в списке)."""

    if req.guid and req.guid.startswith(PROVIDER_ID):
        movie_id = req.guid.rsplit("/", 1)[-1]
        results = [map_search_result({"id": int(movie_id), "name": req.title, "year": req.year})]
        return _match_container(results)

    if not req.title:
        raise HTTPException(status_code=400, detail="title или guid обязательны")

    cache_key = f"search:{req.title}:{req.year}"
    cached = cache_get(cache_key)
    if cached:
        return cached

    max_results = 10 if req.manual else 5
    data = await client.movie_search(req.title, page=1, limit=max_results)
    docs = (data or {}).get("docs", [])
    print(f"[main] search title={req.title!r}: got {len(docs)} docs. "
          f"posters present: {[bool((d.get('poster') or {}).get('url') or (d.get('poster') or {}).get('previewUrl')) for d in docs]}")

    if req.year:
        docs = sorted(docs, key=lambda d: 0 if d.get("year") == req.year else 1)

    results = [map_search_result(d) for d in docs if d.get("id")]
    print(f"[main] mapped {len(results)} results. Has 'Image' field: {[('Image' in r) for r in results]}")

    container = _match_container(results)
    cache_set(cache_key, container)
    return container


def _match_container(results: list[dict]) -> dict:
    return {
        "MediaContainer": {
            "offset": 0,
            "totalSize": len(results),
            "identifier": PROVIDER_ID,
            "size": len(results),
            "Metadata": results,
        }
    }


@app.get("/movie/library/metadata/{rating_key}")
async def metadata(rating_key: str):
    """Полные метаданные по одному фильму — вызывается при Refresh Metadata."""
    try:
        movie_id = int(rating_key)
    except ValueError:
        raise HTTPException(status_code=400, detail="Некорректный ratingKey")

    cache_key = f"meta:{movie_id}"
    cached = cache_get(cache_key)
    if cached:
        return cached

    result = await build_full_metadata(movie_id, client, tmdb_client)
    if not result:
        raise HTTPException(status_code=404, detail="Фильм не найден на poiskkino.dev")

    container = {
        "MediaContainer": {
            "identifier": PROVIDER_ID,
            "size": 1,
            "Metadata": [result],
        }
    }
    cache_set(cache_key, container)
    return container


@app.get("/movie/library/metadata/{rating_key}/extras")
async def extras(rating_key: str):
    """Экспериментально: Plex сам запрашивает этот путь (видно в логах
    как 404 до этой версии) — пробуем отдать трейлеры. Схема ответа
    не подтверждена официально, проверяется по факту."""
    try:
        movie_id = int(rating_key)
    except ValueError:
        raise HTTPException(status_code=400, detail="Некорректный ratingKey")

    from app.poiskkino_mapper import map_trailers
    details = await client.movie_by_id(movie_id)
    if not details:
        return {"MediaContainer": {"identifier": PROVIDER_ID, "size": 0, "Metadata": []}}

    trailers = map_trailers(movie_id, details)
    print(f"[main] extras for movie {movie_id}: {len(trailers)} trailers found")
    return {
        "MediaContainer": {
            "identifier": PROVIDER_ID,
            "size": len(trailers),
            "Metadata": trailers,
        }
    }


@app.get("/image-proxy")
async def image_proxy(url: str):
    """Скачивает картинку (обычно с image.tmdb.org) через настроенный
    прокси и отдаёт её Plex'у от своего имени — см. комментарий в
    tmdb_client.py про то, зачем это нужно."""
    result = await tmdb_client.fetch_image_bytes(url)
    if not result:
        raise HTTPException(status_code=502, detail="Не удалось получить картинку через прокси")
    content, content_type = result
    return Response(content=content, media_type=content_type)


@app.get("/healthz")
async def healthz():
    return {"status": "ok", "version": VERSION, "build": BUILD_DATE, "author": AUTHOR}

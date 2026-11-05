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
import os
import time
from typing import Optional

from urllib.parse import urlparse

from fastapi import FastAPI, HTTPException
from fastapi.responses import Response
from pydantic import BaseModel

from app.poiskkino_client import PoiskKinoClient
from app.poiskkino_mapper import map_search_result, build_full_metadata, PROVIDER_ID
from app.tmdb_client import TmdbClient
from app.tmdb_fallback_mapper import (
    is_tmdb_fallback_key,
    tmdb_id_from_rating_key,
    build_tmdb_guid,
    map_tmdb_search_result,
    build_tmdb_fallback_metadata,
)
from app.version import VERSION, BUILD_DATE, AUTHOR

app = FastAPI(
    title="Kinopoisk (poiskkino.dev) Custom Metadata Provider",
    description="Автор: Sergey Sychev",
)
client = PoiskKinoClient()
tmdb_client = TmdbClient()

print(f"[startup] Kinopoisk (poiskkino.dev) provider — version {VERSION} (build {BUILD_DATE}), автор: {AUTHOR}")
if tmdb_client.api_key:
    print("[startup] TMDB_API_KEY задан — фолбэк на исчерпание квоты poiskkino.dev включён "
          "(v0.17.0+, см. app/tmdb_fallback_mapper.py)")
else:
    print("[startup] TMDB_API_KEY не задан — при исчерпании квоты poiskkino.dev карточки "
          "новых фильмов снова будут приходить пустыми, фолбэк недоступен без ключа")

# простейший in-memory кэш, чтобы не жечь дневную квоту API при
# повторных Refresh Metadata / Match — на free-тарифе poiskkino.dev это
# особенно важно (200 запросов/сутки, а на карточку уходит 2-3 запроса)
_cache: dict[str, tuple[float, dict]] = {}
CACHE_TTL_SECONDS = 60 * 60 * 12  # 12 часов
# Ключ поиска складывается из названия, которое пришло в запросе, то есть
# его задаёт клиент. Без потолка кэш растёт, пока не кончится память у
# контейнера. 2000 записей — это заведомо больше любой домашней
# библиотеки и при этом пара мегабайт.
MAX_CACHE_ENTRIES = 2000


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
    if len(_cache) >= MAX_CACHE_ENTRIES and key not in _cache:
        for stale_key in [k for k, (ts, _) in _cache.items() if time.time() - ts > CACHE_TTL_SECONDS]:
            _cache.pop(stale_key, None)
        while len(_cache) >= MAX_CACHE_ENTRIES:
            _cache.pop(next(iter(_cache)), None)   # dict хранит порядок вставки — уходит самая старая
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
        if is_tmdb_fallback_key(movie_id):
            # Plex переспрашивает карточку, уже найденную через TMDB-фолбэк
            # (например, при повторном ручном Match) — id/название/год у
            # нас уже есть в самом guid/req, заново ходить никуда не надо.
            results = [map_tmdb_search_result({
                "id": tmdb_id_from_rating_key(movie_id),
                "title": req.title,
                "release_date": f"{req.year}-01-01" if req.year else "",
            })]
        else:
            try:
                results = [map_search_result({"id": int(movie_id), "name": req.title, "year": req.year})]
            except ValueError:
                raise HTTPException(status_code=400, detail="Некорректный id в guid")
        return _match_container(results)

    if not req.title:
        raise HTTPException(status_code=400, detail="title или guid обязательны")

    cache_key = f"search:{req.title}:{req.year}"
    cached = cache_get(cache_key)
    if cached:
        return cached

    max_results = 10 if req.manual else 5
    try:
        data = await client.movie_search(req.title, page=1, limit=max_results)
    except RuntimeError as e:
        print(f"[main] search title={req.title!r}: провайдер недоступен ({e}) — пробую TMDB-фолбэк вместо пустого результата")
        tmdb_docs = await tmdb_client.search_movie(req.title, req.year)
        if req.year:
            tmdb_docs = sorted(tmdb_docs, key=lambda d: 0 if str(req.year) == (d.get("release_date") or "")[:4] else 1)
        tmdb_results = [map_tmdb_search_result(d) for d in tmdb_docs[:max_results] if d.get("id")]
        print(f"[main] search title={req.title!r}: TMDB-фолбэк вернул {len(tmdb_results)} результатов")
        # Намеренно НЕ кэшируем — квота poiskkino.dev может освободиться уже
        # к следующему запросу, и не хочется залипать на урезанном TMDB-пути.
        return _match_container(tmdb_results)
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
    if is_tmdb_fallback_key(rating_key):
        try:
            tmdb_id_from_rating_key(rating_key)
        except ValueError:
            raise HTTPException(status_code=400, detail="Некорректный ratingKey")
        return await _metadata_tmdb_fallback(rating_key)

    try:
        movie_id = int(rating_key)
    except ValueError:
        raise HTTPException(status_code=400, detail="Некорректный ratingKey")

    cache_key = f"meta:{movie_id}"
    cached = cache_get(cache_key)
    if cached:
        return cached

    try:
        result = await build_full_metadata(movie_id, client, tmdb_client)
    except RuntimeError as e:
        print(f"[main] metadata movie_id={movie_id}: провайдер недоступен ({e}) — отдаю 404 вместо 500")
        raise HTTPException(status_code=404, detail=str(e))
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


async def _metadata_tmdb_fallback(rating_key: str) -> dict:
    """Карточка, изначально найденная через TMDB-фолбэк в match() (см.
    выше — срабатывает, когда poiskkino.dev недоступен на момент первого
    добавления фильма в Plex).

    При каждом запросе метаданных (в т.ч. force-рефреше из
    refresh_incomplete.py) СНАЧАЛА пробуем "повысить" карточку до полной
    версии с Кинопоиска — квота могла уже освободиться. Если поиск на
    poiskkino.dev находит фильм — отдаём его полные данные (с отзывами и
    рейтингом), это и есть самоисцеление, ради которого всё затевалось.
    ratingKey/guid при этом остаются "tmdb-..." — Plex уже привязал
    карточку к этому guid при первом матче, и подменять его нельзя,
    иначе Plex решит, что это другой объект, и создаст дубль вместо
    обновления существующей карточки. Если poiskkino всё ещё недоступен —
    как и раньше, отдаём урезанную карточку из TMDB."""
    tmdb_id = tmdb_id_from_rating_key(rating_key)

    cache_key = f"meta:{rating_key}"
    cached = cache_get(cache_key)
    if cached:
        return cached

    tmdb_details = await tmdb_client.movie_details(tmdb_id)
    if not tmdb_details:
        raise HTTPException(status_code=404, detail="Фильм не найден на TMDB")

    title = tmdb_details.get("title") or tmdb_details.get("original_title")
    release_year = (tmdb_details.get("release_date") or "")[:4]
    year = int(release_year) if release_year.isdigit() else None

    if title:
        try:
            search_data = await client.movie_search(title, page=1, limit=5)
            docs = (search_data or {}).get("docs", [])
            if year:
                docs = sorted(docs, key=lambda d: 0 if d.get("year") == year else 1)
            if docs and docs[0].get("id"):
                poiskkino_id = docs[0]["id"]
                full = await build_full_metadata(poiskkino_id, client, tmdb_client)
                if full:
                    full["ratingKey"] = rating_key
                    full["guid"] = build_tmdb_guid(tmdb_id)
                    print(f"[main] metadata {rating_key}: квота poiskkino.dev освободилась, "
                          f"апгрейд до полных данных (poiskkino movie_id={poiskkino_id})")
                    container = {"MediaContainer": {"identifier": PROVIDER_ID, "size": 1, "Metadata": [full]}}
                    cache_set(cache_key, container)
                    return container
        except RuntimeError as e:
            print(f"[main] metadata {rating_key}: попытка апгрейда до poiskkino не удалась ({e}) — "
                  f"остаюсь на TMDB-версии")

    result = build_tmdb_fallback_metadata(tmdb_id, tmdb_details)
    container = {"MediaContainer": {"identifier": PROVIDER_ID, "size": 1, "Metadata": [result]}}
    cache_set(cache_key, container)
    return container


@app.get("/movie/library/metadata/{rating_key}/extras")
async def extras(rating_key: str):
    """Экспериментально: Plex сам запрашивает этот путь (видно в логах
    как 404 до этой версии) — пробуем отдать трейлеры. Схема ответа
    не подтверждена официально, проверяется по факту."""
    if is_tmdb_fallback_key(rating_key):
        # Трейлеры этот провайдер отдаёт только с Кинопоиска (см.
        # map_trailers) — у TMDB-фолбэка их взять неоткуда, отдаём пустой
        # список вместо ошибки на int(rating_key).
        return {"MediaContainer": {"identifier": PROVIDER_ID, "size": 0, "Metadata": []}}

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


# Хосты, картинки с которых /image-proxy согласен скачивать. v0.18.0:
# до этого параметр url не проверялся вообще — эндпоинт скачал бы любой
# адрес, который ему передали, включая адреса внутри домашней сети, и
# отдал бы ответ наружу. Сам провайдер подставляет в url только TMDB
# (см. tmdb_client.search_person_photo), так что ограничение ничего не
# ломает. При необходимости список расширяется через IMAGE_PROXY_HOSTS
# (хосты через запятую).
ALLOWED_IMAGE_HOSTS = {
    h.strip().lower()
    for h in os.environ.get("IMAGE_PROXY_HOSTS", "image.tmdb.org").split(",")
    if h.strip()
}


@app.get("/image-proxy")
async def image_proxy(url: str):
    """Скачивает картинку (обычно с image.tmdb.org) через настроенный
    прокси и отдаёт её Plex'у от своего имени — см. комментарий в
    tmdb_client.py про то, зачем это нужно."""
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https") or (parsed.hostname or "").lower() not in ALLOWED_IMAGE_HOSTS:
        print(f"[main] image_proxy: отказ, хост не разрешён — {url!r}")
        raise HTTPException(status_code=400, detail="Этот хост не разрешён для /image-proxy")

    result = await tmdb_client.fetch_image_bytes(url)
    if not result:
        raise HTTPException(status_code=502, detail="Не удалось получить картинку через прокси")
    content, content_type = result
    # Content-Type приходит с чужого сервера. Отдавать его как есть — значит
    # согласиться показать с нашего адреса что угодно, вплоть до HTML со
    # скриптом. Пропускаем только картинки, остальное заворачиваем.
    if not content_type.split(";", 1)[0].strip().lower().startswith("image/"):
        print(f"[main] image_proxy: отказ, ответ не картинка ({content_type!r}) — {url!r}")
        raise HTTPException(status_code=502, detail="По этому адресу вернулась не картинка")
    return Response(
        content=content,
        media_type=content_type,
        headers={"X-Content-Type-Options": "nosniff"},
    )


@app.get("/healthz")
async def healthz():
    return {"status": "ok", "version": VERSION, "build": BUILD_DATE, "author": AUTHOR}

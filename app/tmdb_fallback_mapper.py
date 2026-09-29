# -*- coding: utf-8 -*-
"""
Фолбэк-преобразование ответа TMDB (The Movie Database) в JSON-формат Plex
Custom Metadata Provider — используется ТОЛЬКО когда poiskkino.dev
недоступен (исчерпана суточная квота 200 запросов/день free-тарифа, или
лимит demo-пагинации — см. RuntimeError в poiskkino_client.py). Карточка
получается урезанной: нет отзывов и рейтинга Кинопоиска (TMDB их не
отдаёт), зато есть описание/постер/жанры/каст — карточка не остаётся
пустой, как было до v0.17.0.

Пространство id. Фильмы, найденные через этот фолбэк, помечаются в
ratingKey/guid префиксом "tmdb-" вместо "голого" числового id, которым
пользуются обычные poiskkino-карточки (see poiskkino_mapper.build_guid).
Так main.py всегда может отличить, какой источник использовать при
повторном запросе метаданных (Refresh Metadata), не путая при этом два
разных пространства числовых id (у poiskkino.dev и у TMDB могут случайно
совпасть числовые id — они никак не согласованы друг с другом).

v0.17.0: добавлено вместе с фолбэком на исчерпание квоты poiskkino.dev
(см. CHANGELOG.md).
"""
from app.poiskkino_mapper import PROVIDER_ID

TMDB_PREFIX = "tmdb-"
TMDB_IMAGE_BASE = "https://image.tmdb.org/t/p/w500"

# Соответствие TMDB job (в credits.crew) -> поле карточки Plex. Для
# poiskkino.dev аналогичная таблица называется PROFESSION_MAP
# (poiskkino_mapper.py) — здесь своя, потому что у TMDB другой словарь
# значений (английский, "Director"/"Screenplay"/... вместо русских
# "режиссёр"/"сценарист"/...).
CREW_JOB_MAP = {
    "director": "Director",
    "writer": "Writer",
    "screenplay": "Writer",
    "story": "Writer",
    "producer": "Producer",
    "executive producer": "Producer",
}


def is_tmdb_fallback_key(rating_key: str) -> bool:
    return rating_key.startswith(TMDB_PREFIX)


def tmdb_id_from_rating_key(rating_key: str) -> int:
    return int(rating_key[len(TMDB_PREFIX):])


def build_tmdb_guid(tmdb_id: int) -> str:
    return f"{PROVIDER_ID}://movie/{TMDB_PREFIX}{tmdb_id}"


def _extract_year(release_date: str | None) -> int | None:
    if not release_date or len(release_date) < 4:
        return None
    try:
        return int(release_date[:4])
    except ValueError:
        return None


def map_tmdb_search_result(item: dict) -> dict:
    """Один элемент TMDB /search/movie -> результат для /matches. Та же
    форма, что poiskkino_mapper.map_search_result, но с префиксом
    "tmdb-" в ratingKey/guid — см. комментарий в шапке файла."""
    tmdb_id = item.get("id")
    title = item.get("title") or item.get("original_title") or "Unknown"
    poster_path = item.get("poster_path")

    result = {
        "type": "movie",
        "ratingKey": f"{TMDB_PREFIX}{tmdb_id}",
        "guid": build_tmdb_guid(tmdb_id),
        "title": title,
        "year": _extract_year(item.get("release_date")),
    }
    if poster_path:
        poster_url = TMDB_IMAGE_BASE + poster_path
        result["thumb"] = poster_url
        result["Image"] = [{"type": "coverPoster", "url": poster_url}]
    return result


def _split_tmdb_credits(credits: dict) -> dict:
    cast = (credits or {}).get("cast") or []
    crew = (credits or {}).get("crew") or []
    people: dict[str, list] = {"Role": [], "Director": [], "Writer": [], "Producer": []}

    # top-30 по cast — у TMDB порядок уже приоритетный (по "order"),
    # обрезаем, чтобы не тащить в карточку эпизодических персонажей без
    # надобности (poiskkino-путь берёт персон без ограничения, но там
    # они уже отфильтрованы на стороне источника)
    for p in cast[:30]:
        name = p.get("name")
        if not name:
            continue
        entry = {"tag": name}
        if p.get("character"):
            entry["role"] = p["character"]
        if p.get("profile_path"):
            entry["thumb"] = TMDB_IMAGE_BASE + p["profile_path"]
        people["Role"].append(entry)

    for p in crew:
        name = p.get("name")
        job = (p.get("job") or "").strip().lower()
        key = CREW_JOB_MAP.get(job)
        if not name or not key:
            continue
        entry = {"tag": name}
        if p.get("profile_path"):
            entry["thumb"] = TMDB_IMAGE_BASE + p["profile_path"]
        people[key].append(entry)

    return people


def build_tmdb_fallback_metadata(tmdb_id: int, details: dict) -> dict:
    """Урезанная карточка из TMDB — используется, пока poiskkino.dev
    недоступен. Без отзывов и рейтинга Кинопоиска (TMDB их не знает),
    зато с описанием/постером/жанрами/кастом — карточка не пустая.
    ratingKey/guid держим с префиксом "tmdb-", как и в
    map_tmdb_search_result — они должны совпадать между /matches и
    /library/metadata/{ratingKey}, иначе Plex потеряет карточку."""
    genres = [g["name"] for g in (details.get("genres") or []) if g.get("name")]
    people = _split_tmdb_credits(details.get("credits") or {})

    summary = (details.get("overview") or "").strip()
    note = ("*(временно из TMDB — карточка автоматически обновится до "
            "полной версии с Кинопоиска, когда освободится суточная "
            "квота poiskkino.dev — см. daily_backfill.sh)*")
    summary = f"{summary}\n\n{note}".strip() if summary else note

    images = []
    if details.get("poster_path"):
        images.append({"type": "coverPoster", "url": TMDB_IMAGE_BASE + details["poster_path"]})
    if details.get("backdrop_path"):
        images.append({"type": "background", "url": TMDB_IMAGE_BASE + details["backdrop_path"]})

    release_date = details.get("release_date") or ""
    title = details.get("title") or details.get("original_title")
    original_title = details.get("original_title")

    metadata = {
        "type": "movie",
        "ratingKey": f"{TMDB_PREFIX}{tmdb_id}",
        "guid": build_tmdb_guid(tmdb_id),
        "title": title,
        "originalTitle": original_title if original_title != title else None,
        "year": _extract_year(release_date),
        "tagline": details.get("tagline") or "",
        "summary": summary,
        "originallyAvailableAt": release_date[:10] if release_date else None,
        "audienceRating": float(details["vote_average"]) if details.get("vote_average") else None,
        "Genre": [{"tag": g} for g in genres],
        "Role": people["Role"],
        "Director": people["Director"],
        "Writer": people["Writer"],
        "Producer": people["Producer"],
        "Image": images,
    }
    if details.get("imdb_id"):
        metadata["Guid"] = [{"id": f"imdb://{details['imdb_id']}"}]

    return {k: v for k, v in metadata.items() if v not in (None, "", [])}

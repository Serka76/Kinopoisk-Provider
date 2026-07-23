# -*- coding: utf-8 -*-
"""
Преобразование ответа poiskkino.dev (MovieDtoV1_4) в JSON-формат
Plex Custom Metadata Provider.

Схема источника (poiskkino.dev) подтверждена настоящим OpenAPI-спеком
пользователя — см. app/poiskkino_client.py.
Схема назначения (Plex) по-прежнему best-effort по аналогии с общей
схемой объектов Plex — см. README.md, раздел "Проверка схемы".

v0.7.0: добавлены голоса, факты/ошибки в фильме, бюджет/сборы (все три —
текстом в описание, т.к. у карточки Plex нет отдельных полей под них),
похожие фильмы, полный каст без потерь на фильтрации профессии.
"""
import re

from app.poiskkino_client import PoiskKinoClient
from app.tmdb_client import TmdbClient

PROVIDER_ID = "tv.plex.agents.custom.kinoplex.poiskkino"

# profession в PersonInMovie не имеет enum в спеке. Расширил список
# известных вариантов (RU/EN), но самое важное — теперь ЛЮБАЯ
# неопознанная профессия попадает в Role (Актёры) вместо того, чтобы
# молча теряться. Так весь список persons долетает до карточки, даже
# если какие-то конкретные строки profession я не угадал.
PROFESSION_MAP = {
    "актеры": "Role", "актёры": "Role", "actor": "Role", "actors": "Role",
    "режиссеры": "Director", "режиссёры": "Director", "director": "Director", "directors": "Director",
    "сценаристы": "Writer", "writer": "Writer", "writers": "Writer",
    "продюсеры": "Producer", "producer": "Producer", "producers": "Producer",
    "продюсер": "Producer", "режиссер": "Director", "режиссёр": "Director",
    "сценарист": "Writer", "актер": "Role", "актёр": "Role",
}

CURRENCY_LABELS = {"world": "Мир", "russia": "Россия", "usa": "США"}


def _clean_html_text(text: str) -> str:
    """Кинопоиск отдаёт текст (рецензии, факты) с HTML-тегами (<b>, <i>,
    <a href>, переносы и т.п.), а Plex просит markdown
    (X-Plex-Text-Format=markdown в запросах). Конвертирую самые частые
    теги, ссылки схлопываю до текста (без URL — они всё равно ведут на
    сам Кинопоиск, не на что-то полезное в контексте Plex)."""
    if not text:
        return text
    text = re.sub(r"<a\b[^>]*>(.*?)</a>", r"\1", text, flags=re.IGNORECASE | re.DOTALL)
    text = re.sub(r"<b>(.*?)</b>", r"**\1**", text, flags=re.IGNORECASE | re.DOTALL)
    text = re.sub(r"<i>(.*?)</i>", r"*\1*", text, flags=re.IGNORECASE | re.DOTALL)
    text = re.sub(r"<br\s*/?>", "\n", text, flags=re.IGNORECASE)
    text = re.sub(r"<[^>]+>", "", text)  # любые прочие теги — просто убираем
    return text.strip()


def build_guid(movie_id: int) -> str:
    return f"{PROVIDER_ID}://movie/{movie_id}"


def map_search_result(item: dict) -> dict:
    """Один элемент SearchMovieDtoV1_4 -> результат для /matches."""
    movie_id = item.get("id")
    name = item.get("name") or item.get("enName") or item.get("alternativeName") or "Unknown"
    poster = item.get("poster") or {}
    poster_url = poster.get("url") or poster.get("previewUrl")

    result = {
        "type": "movie",
        "ratingKey": str(movie_id),
        "guid": build_guid(movie_id),
        "title": name,
        "year": item.get("year"),
    }
    if poster_url:
        result["thumb"] = poster_url
        result["Image"] = [{"type": "coverPoster", "url": poster_url}]
    return result


def _split_persons(persons: list, photo_overrides: dict | None = None) -> dict:
    photo_overrides = photo_overrides or {}
    people: dict[str, list] = {"Role": [], "Director": [], "Writer": [], "Producer": []}
    for p in persons or []:
        key = PROFESSION_MAP.get((p.get("profession") or "").strip().lower()) \
            or PROFESSION_MAP.get((p.get("enProfession") or "").strip().lower()) \
            or "Role"  # неопознанная профессия -> не теряем персону, кладём в актёры
        entry = {"tag": p.get("name") or p.get("enName") or ""}
        if not entry["tag"]:
            continue
        if key == "Role" and p.get("description"):
            entry["role"] = p["description"]
        # Приоритет: TMDB-фото (для нероссийских фильмов, если нашлось),
        # иначе — фото с Кинопоиска как раньше.
        photo = photo_overrides.get(p.get("name")) or p.get("photo")
        if photo:
            entry["thumb"] = photo
        people[key].append(entry)
    return people


def _format_currency(cv: dict | None) -> str | None:
    if not cv or cv.get("value") is None:
        return None
    value = cv["value"]
    currency = cv.get("currency", "")
    return f"{value:,.0f} {currency}".replace(",", " ")


def _build_extra_summary_block(details: dict, rating: dict, votes: dict) -> str:
    """Собирает markdown-блок с рейтингом+голосами, фактами и сборами —
    Plex явно просит X-Plex-Text-Format=markdown в запросах метаданных,
    так что используем **жирный** и списки. Раз бейдж рейтинга у Plex
    для custom-провайдеров не рисуется (подтверждённое ограничение),
    это единственное место, где сам балл вообще виден пользователю."""
    parts = []

    rating_bits = []
    if rating.get("kp"):
        count = votes.get("kp")
        rating_bits.append(f"🎬 **КП:** {rating['kp']}" + (f" ({count} голосов)" if count else ""))
    if rating.get("imdb"):
        count = votes.get("imdb")
        rating_bits.append(f"🎥 **IMDb:** {rating['imdb']}" + (f" ({int(count)} голосов)" if count else ""))
    if rating_bits:
        parts.append("\n".join(rating_bits))

    fees = details.get("fees") or {}
    budget = details.get("budget") or {}
    money_bits = []
    budget_str = _format_currency(budget)
    if budget_str:
        money_bits.append(f"Бюджет: {budget_str}")
    for key, label in CURRENCY_LABELS.items():
        fee_str = _format_currency(fees.get(key))
        if fee_str:
            money_bits.append(f"Сборы ({label}): {fee_str}")
    if money_bits:
        parts.append("**Бюджет и сборы:** " + " · ".join(money_bits))

    facts = [f for f in (details.get("facts") or []) if f.get("value") and not f.get("spoiler")]
    if facts:
        trivia = [f for f in facts if (f.get("type") or "").upper() != "BLOOPER"]
        bloopers = [f for f in facts if (f.get("type") or "").upper() == "BLOOPER"]
        if trivia:
            parts.append("**Знаете ли вы, что:**\n" + "\n".join(f"- {_clean_html_text(f['value'])}" for f in trivia[:5]))
        if bloopers:
            parts.append("**Ошибки в фильме:**\n" + "\n".join(f"- {_clean_html_text(f['value'])}" for f in bloopers[:5]))

    return "\n\n".join(parts)


async def build_full_metadata(movie_id: int, client: PoiskKinoClient, tmdb_client: TmdbClient | None = None) -> dict | None:
    """Собирает полный объект метаданных для одного фильма.

    В отличие от kinopoiskapiunofficial.tech, здесь актёры/режиссёры уже
    приходят прямо в ответе /movie/{id} (поле persons) — отдельный запрос
    не нужен. Отдельно нужны только рецензии и (опционально) доп. кадры.
    """
    details = await client.movie_by_id(movie_id)
    if not details:
        return None

    reviews_data = await client.reviews(movie_id, limit=10) or {}
    images_data = await client.images(movie_id, image_type="frame", limit=10) or {}

    genres = [g["name"] for g in (details.get("genres") or []) if g.get("name")]
    countries = [c["name"] for c in (details.get("countries") or []) if c.get("name")]

    # Фото актёров: для российских фильмов — с Кинопоиска (как раньше),
    # для остальных — пробуем найти на TMDB (обычно лучше качеством и
    # актуальнее), с откатом на кинопоисковское фото, если TMDB не нашёл.
    is_russian_production = any("росс" in c.lower() for c in countries)
    photo_overrides: dict[str, str] = {}
    if not is_russian_production and tmdb_client is not None:
        photo_overrides = await tmdb_client.enrich_photos(details.get("persons") or [])
    print(f"[poiskkino_mapper] movie {movie_id}: countries={countries}, "
          f"is_russian_production={is_russian_production}, "
          f"tmdb_photos_found={len(photo_overrides)}")

    rating = details.get("rating") or {}
    votes = details.get("votes") or {}
    ratings = []
    if rating.get("kp"):
        entry = {"image": "kinopoisk://image.rating", "value": float(rating["kp"]), "type": "critic"}
        try:
            if votes.get("kp"):
                entry["count"] = int(votes["kp"])  # неподтверждённое поле, best-effort
        except (TypeError, ValueError):
            pass
        ratings.append(entry)
    if rating.get("imdb"):
        entry = {"image": "imdb://image.rating", "value": float(rating["imdb"]), "type": "audience"}
        if votes.get("imdb"):
            entry["count"] = int(votes["imdb"])
        ratings.append(entry)
    print(f"[poiskkino_mapper] movie {movie_id}: raw rating from source={rating}, "
          f"built Rating array={ratings}")

    people = _split_persons(details.get("persons"), photo_overrides)
    print(f"[poiskkino_mapper] movie {movie_id}: persons total={len(details.get('persons') or [])}, "
          f"Role={len(people['Role'])}, Director={len(people['Director'])}, "
          f"Writer={len(people['Writer'])}, Producer={len(people['Producer'])}")
    # диагностика странного "/ Production" на карточке — смотрим на
    # сырые значения profession/description по первым персонам
    raw_persons_sample = [
        {"name": p.get("name"), "profession": p.get("profession"),
         "enProfession": p.get("enProfession"), "description": p.get("description")}
        for p in (details.get("persons") or [])[:15]
    ]
    print(f"[poiskkino_mapper] movie {movie_id}: raw persons sample={raw_persons_sample}")

    reviews = []
    for r in reviews_data.get("docs", []):
        review_type = (r.get("type") or "").lower()
        sentiment = "fresh" if "позитив" in review_type else "rotten" if "негатив" in review_type else "neutral"
        reviews.append({
            # v0.6.0 убирал "tag" совсем — похоже, это обязательное поле
            # у Plex для Review, без него весь массив браковался целиком
            # и Plex откатывался на резервный источник (RT из Plex Movie).
            # Возвращаю "tag", но коротким — просто "Кинопоиск" вместо
            # длинной подписи.
            "tag": "Кинопоиск",
            "source": r.get("author") or "Кинопоиск",
            "text": _clean_html_text(r.get("review", "")),
            "filter": sentiment,
        })
    print(f"[poiskkino_mapper] movie {movie_id}: reviews_data.total={reviews_data.get('total')}, "
          f"docs_count={len(reviews_data.get('docs', []))}, mapped_reviews={len(reviews)}")

    images = []
    poster = details.get("poster") or {}
    if poster.get("url"):
        images.append({"type": "coverPoster", "url": poster["url"]})
    backdrop = details.get("backdrop") or {}
    if backdrop.get("url"):
        images.append({"type": "background", "url": backdrop["url"]})
    for img in images_data.get("docs", [])[:10]:
        if img.get("url"):
            images.append({"type": "background", "url": img["url"]})

    premiere = details.get("premiere") or {}
    originally_available_at = premiere.get("russia") or premiere.get("world")
    if originally_available_at:
        originally_available_at = str(originally_available_at)[:10]

    content_rating = details.get("ratingMpaa")
    if not content_rating and details.get("ageRating") is not None:
        content_rating = f"{details['ageRating']}+"

    summary = _clean_html_text(details.get("description") or details.get("shortDescription") or "")
    extra_block = _build_extra_summary_block(details, rating, votes)
    if extra_block:
        summary = (summary + "\n\n" + extra_block).strip()

    similar = []
    for m in (details.get("similarMovies") or [])[:10]:
        name = m.get("name") or m.get("enName")
        if name:
            similar.append({"tag": name})

    # Простой способ подтянуть коллекции: lists — это готовые подборки
    # Кинопоиска, в которые входит фильм (например "250 лучших фильмов").
    # Plex сгруппирует фильмы с одинаковым тегом Collection автоматически.
    collections = [{"tag": name} for name in (details.get("lists") or []) if name]

    def _safe_float(v):
        try:
            return float(v) if v is not None else None
        except (TypeError, ValueError):
            return None

    metadata = {
        "type": "movie",
        "ratingKey": str(movie_id),
        "guid": build_guid(movie_id),
        "title": details.get("name") or details.get("enName"),
        "originalTitle": details.get("enName") if details.get("enName") != details.get("name") else None,
        "year": details.get("year"),
        "tagline": details.get("slogan") or "",
        "summary": summary,
        "contentRating": content_rating,
        "originallyAvailableAt": originally_available_at,
        # v0.10.2: возвращаю обратно — оказалось, что и без наших полей
        # рейтинг всё равно не показывался (ни с Кинопоиска, ни от
        # Plex Movie), значит дело не в "блокировке fallback", а в более
        # общем ограничении текущей версии Plex. Раз рабочего фоллбэка
        # всё равно нет, наши данные не помешают — а если Plex когда-то
        # починит отображение для custom-провайдеров, всё заработает само.
        "rating": _safe_float(rating.get("kp")),
        "audienceRating": _safe_float(rating.get("imdb")),
        "Genre": [{"tag": g} for g in genres],
        "Country": [{"tag": c} for c in countries],
        # "Rating": ratings — намеренно не отдаём, см. комментарий выше
        "Role": people["Role"],
        "Director": people["Director"],
        "Writer": people["Writer"],
        "Producer": people["Producer"],
        "Review": reviews,
        "Image": images,
        "Similar": similar,  # неподтверждённое поле, best-effort
        "Collection": collections,  # неподтверждённое поле, best-effort
    }

    external_id = details.get("externalId") or {}
    if external_id.get("imdb"):
        metadata["Guid"] = [{"id": f"imdb://{external_id['imdb']}"}]

    result = {k: v for k, v in metadata.items() if v not in (None, "", [])}
    print(f"[poiskkino_mapper] movie {movie_id}: final Review count in payload = {len(result.get('Review', []))}, "
          f"Similar count = {len(result.get('Similar', []))}, Collection count = {len(result.get('Collection', []))}")
    return result


def map_trailers(movie_id: int, details: dict) -> list[dict]:
    """Best-effort: собирает трейлеры из videos.trailers для /extras —
    ТОЛЬКО с Кинопоиска, YouTube и прочие сторонние сайты исключены
    намеренно (по просьбе пользователя).

    НЕПОДТВЕРЖДЕНО (два независимых момента):
    1. Точная схема ответа /extras для custom-провайдеров нигде не
       задокументирована — в логах видно, что Plex сам её запрашивает
       (значит фича существует), но формат ответа предположен по
       аналогии с обычным Plex Extras (тип "clip").
    2. Неизвестно, отдаёт ли poiskkino.dev вообще трейлеры с самого
       Кинопоиска в этом поле, или только через TMDb (обычно = YouTube).
       Если после фильтра список всегда пустой — значит на этом
       конкретном эндпоинте poiskkino.dev таких данных просто нет, и
       трейлеры с Кинопоиска придётся тянуть из другого источника
       (например, обратно на kinopoiskapiunofficial.tech, у которого
       было отдельное видео с самого Кинопоиска — см. старую версию
       0.1.0 клиента, если понадобится).
    """
    trailers = ((details.get("videos") or {}).get("trailers")) or []
    kinopoisk_trailers = [
        t for t in trailers
        if "kinopoisk" in (t.get("site") or "").lower()
        or "kinopoisk" in (t.get("url") or "").lower()
    ]
    print(f"[poiskkino_mapper] movie {movie_id}: trailers total={len(trailers)}, "
          f"kinopoisk-only={len(kinopoisk_trailers)}, "
          f"sites seen={[t.get('site') for t in trailers]}")

    result = []
    for i, t in enumerate(kinopoisk_trailers[:5]):
        url = t.get("url")
        if not url:
            continue
        result.append({
            "type": "clip",
            "subtype": "trailer",
            "ratingKey": f"{movie_id}-trailer-{i}",
            "guid": f"{PROVIDER_ID}://movie/{movie_id}/trailer/{i}",
            "title": t.get("name") or "Trailer",
            "Media": [{"Part": [{"key": url}]}],
        })
    return result

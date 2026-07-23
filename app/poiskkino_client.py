# -*- coding: utf-8 -*-
"""
Клиент к poiskkino.dev (api.poiskkino.dev).

Схема эндпоинтов и полей взята НЕ по аналогии, а напрямую из настоящего
OpenAPI-спека сервиса (api-1.json, экспортирован пользователем из
Scalar/документации poiskkino.dev) — то есть это не "лучшая догадка",
как было с Plex-схемой, а проверенная структура.

Базовый URL: https://api.poiskkino.dev
Авторизация: заголовок X-API-KEY
"""
import os
import httpx

API_KEY = os.environ.get("POISKKINO_API_KEY", "").strip()
BASE = "https://api.poiskkino.dev"

HEADERS = {
    "X-API-KEY": API_KEY,
    "Accept": "application/json",
}

TIMEOUT = httpx.Timeout(20.0, connect=10.0)


class PoiskKinoClient:
    """Обёртка над api.poiskkino.dev (v1.4/v1.5).

    Бесплатный тариф — 200 запросов/сутки, и demo/free-доступ ограничен
    страницами 1-10 с limit <= 10 (см. код 403 в спеке) — учитывай это
    при пагинации отзывов/картинок.
    """

    def __init__(self, api_key: str | None = None):
        self.headers = dict(HEADERS)
        if api_key:
            self.headers["X-API-KEY"] = api_key

    async def _get(self, path: str, params: dict | None = None) -> dict | None:
        async with httpx.AsyncClient(timeout=TIMEOUT, base_url=BASE) as client:
            try:
                resp = await client.get(path, headers=self.headers, params=params)
                if resp.status_code == 401:
                    raise RuntimeError("Неверный или отсутствующий X-API-KEY (poiskkino.dev)")
                if resp.status_code == 403:
                    raise RuntimeError(
                        "Превышен суточный лимит запросов или лимит demo-пагинации "
                        "(free-тариф: страницы 1-10, limit<=10)"
                    )
                if resp.status_code == 404:
                    return None
                resp.raise_for_status()
                return resp.json()
            except httpx.HTTPError as e:
                print(f"[poiskkino_client] HTTP error for {path}: {e}")
                return None

    async def movie_by_id(self, movie_id: int) -> dict | None:
        """GET /v1.4/movie/{id} -> MovieDtoV1_4 (полная карточка)."""
        return await self._get(f"/v1.4/movie/{movie_id}")

    async def movie_search(self, query: str, page: int = 1, limit: int = 10) -> dict | None:
        """GET /v1.4/movie/search -> {docs: SearchMovieDtoV1_4[], total, page, pages}."""
        return await self._get(
            "/v1.4/movie/search",
            params={"query": query, "page": page, "limit": limit},
        )

    async def reviews(self, movie_id: int, page: int = 1, limit: int = 10) -> dict | None:
        """GET /v1.4/review?movieId=... -> {docs: Review[], ...}."""
        return await self._get(
            "/v1.4/review",
            params={"movieId": str(movie_id), "page": page, "limit": limit},
        )

    async def images(self, movie_id: int, image_type: str = "frame", page: int = 1, limit: int = 10) -> dict | None:
        """GET /v1.4/image?movieId=...&type=... -> {docs: Image[], ...}.

        Типы картинок (по документации poiskkino.dev) обычно включают:
        cover (постер), frame (кадр), backdrop/fanart, screenshot и т.п. —
        конкретный набор допустимых значений type стоит свериться в
        Scalar-документации (поле не задано enum'ом в спеке, значит,
        принимает произвольную строку — источник истины только сам API).
        """
        return await self._get(
            "/v1.4/image",
            params={"movieId": str(movie_id), "type": image_type, "page": page, "limit": limit},
        )

"""Клиент к партнёрскому API РКО Групп.

Документация: https://swagger.rko-group.ru/
База: https://rko-group.ru/api/partner
Авторизация: заголовок X-API-Key (см. https://rko-group.ru/users/my-token/ )
"""

from __future__ import annotations

import json
from typing import Any

import httpx

BASE_URL = "https://rko-group.ru/api/partner"


async def get_rko_request_info(api_key: str, request_id: int) -> tuple[int, Any]:
    """
    GET /rko/request-info/{id}

    Returns (http_status, body): body is dict JSON при успехе, иначе str или dict с ошибкой.
    """
    url = f"{BASE_URL}/rko/request-info/{request_id}"
    async with httpx.AsyncClient(timeout=45.0) as client:
        r = await client.get(url, headers={"X-API-Key": api_key})
    try:
        data = r.json()
    except json.JSONDecodeError:
        return r.status_code, (r.text or "")[:800]
    return r.status_code, data

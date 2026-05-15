import os
from dataclasses import dataclass
from typing import Literal

Mode = Literal["cpa", "ag"]
AgSearchBy = Literal["inn", "surname"]


@dataclass(frozen=True)
class CpaColumns:
    sub1: tuple[str, ...]
    offer: tuple[str, ...]
    status: tuple[str, ...]


@dataclass(frozen=True)
class AgColumns:
    inn: tuple[str, ...]
    fio: tuple[str, ...]
    bank: tuple[str, ...]
    status: tuple[str, ...]


CPA_COLUMNS = CpaColumns(
    sub1=("Sub1", "sub1", "SUB1"),
    offer=("Название оффера",),
    status=("Статус",),
)

AG_COLUMNS = AgColumns(
    inn=("ИНН", "Инн"),
    fio=("ФИО контактного лица",),
    bank=("Банк", "банк", "Bank", "BANK"),
    status=("Статус",),
)


def bot_token() -> str:
    token = os.getenv("BOT_TOKEN", "").strip()
    if not token:
        raise RuntimeError("Задайте переменную окружения BOT_TOKEN")
    return token


def rko_group_api_key() -> str | None:
    """Ключ партнёрского API (https://rko-group.ru/api/partner)."""
    key = os.getenv("RKO_GROUP_API_KEY", "").strip()
    return key or None

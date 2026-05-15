from __future__ import annotations

import io
import re
from typing import Any

import pandas as pd

from bot.config import AG_COLUMNS, AgSearchBy, CPA_COLUMNS


def _norm(s: Any) -> str:
    return str(s).strip().casefold()


def _find_column(columns: list[str], aliases: tuple[str, ...]) -> str | None:
    mapping = {_norm(c): c for c in columns}
    for alias in aliases:
        key = _norm(alias)
        if key in mapping:
            return mapping[key]
    return None


def read_excel_bytes(data: bytes) -> pd.DataFrame:
    return pd.read_excel(io.BytesIO(data), engine="openpyxl")


def _digits(s: str) -> str:
    return "".join(ch for ch in s if ch.isdigit())


def _cell_raw_str(v: Any) -> str:
    if pd.isna(v):
        return ""
    if isinstance(v, bool):
        return str(v)
    if isinstance(v, int):
        return str(v)
    if isinstance(v, float):
        if v.is_integer():
            return str(int(v))
        return str(v).strip()
    s = str(v).strip()
    if re.fullmatch(r"-?\d+\.0+", s):
        s2 = s.split(".", 1)[0]
        if s2.lstrip("-").isdigit():
            return s2
    return s


def filter_cpa_by_sub1(df: pd.DataFrame, sub1_value: str) -> tuple[str | None, list[tuple[str, str]]]:
    """Список (название оффера, статус) по Sub1."""
    df = df.copy()
    df.columns = [str(c).strip() for c in df.columns]
    cols = list(df.columns)
    cfg = CPA_COLUMNS

    c_sub1 = _find_column(cols, cfg.sub1)
    c_offer = _find_column(cols, cfg.offer)
    c_status = _find_column(cols, cfg.status)

    missing: list[str] = []
    if not c_sub1:
        missing.append("Sub1")
    if not c_offer:
        missing.append("Название оффера")
    if not c_status:
        missing.append("Статус")
    if missing:
        return (
            "В файле не нашёл колонки: " + ", ".join(missing) + f". Заголовки: {', '.join(cols)}",
            [],
        )

    needle = sub1_value.strip().casefold()
    if not needle:
        return ("Пустой Sub1.", [])

    series = df[c_sub1].astype(str).str.strip().str.casefold()
    sub = df.loc[series == needle, [c_offer, c_status]]

    rows: list[tuple[str, str]] = []
    for _, r in sub.iterrows():
        offer = _cell_raw_str(r[c_offer]) or "—"
        status = _cell_raw_str(r[c_status]) or "—"
        rows.append((offer, status))

    return (None, rows)


def _fio_first_token_match(fio: str, needle_cf: str) -> bool:
    """Фамилия — первое слово ФИО; допускаем точное совпадение или префикс."""
    parts = fio.split()
    if not parts:
        return False
    fam = parts[0].casefold()
    return fam == needle_cf or fam.startswith(needle_cf)


def filter_ag(df: pd.DataFrame, by: AgSearchBy, query: str) -> tuple[str | None, list[tuple[str, str]]]:
    """Список (банк из колонки «Банк», статус) по ИНН или фамилии в ФИО."""
    df = df.copy()
    df.columns = [str(c).strip() for c in df.columns]
    cols = list(df.columns)
    ag = AG_COLUMNS

    c_inn = _find_column(cols, ag.inn)
    c_fio = _find_column(cols, ag.fio)
    c_bank = _find_column(cols, ag.bank)
    c_status = _find_column(cols, ag.status)

    missing: list[str] = []
    if not c_inn:
        missing.append("ИНН")
    if not c_fio:
        missing.append("ФИО контактного лица")
    if not c_bank:
        missing.append("Банк")
    if not c_status:
        missing.append("Статус")
    if missing:
        return (
            "В файле не нашёл колонки: " + ", ".join(missing) + f". Заголовки: {', '.join(cols)}",
            [],
        )

    q = query.strip()
    if not q:
        return ("Пустой запрос.", [])

    if by == "inn":
        q_digits = _digits(q)
        q_cf = q.casefold()

        def inn_match(v: Any) -> bool:
            raw = _cell_raw_str(v)
            if not raw:
                return False
            if q_digits and _digits(raw) == q_digits:
                return True
            return raw.casefold() == q_cf

        mask = df[c_inn].map(inn_match)
    else:
        needle_cf = q.casefold()

        def surname_match(v: Any) -> bool:
            raw = _cell_raw_str(v)
            if not raw:
                return False
            if _fio_first_token_match(raw, needle_cf):
                return True
            return needle_cf in raw.casefold()

        mask = df[c_fio].map(surname_match)

    sub = df.loc[mask, [c_bank, c_status]]
    rows: list[tuple[str, str]] = []
    for _, r in sub.iterrows():
        bank = _cell_raw_str(r[c_bank]) or "—"
        status = _cell_raw_str(r[c_status]) or "—"
        rows.append((bank, status))

    return (None, rows)

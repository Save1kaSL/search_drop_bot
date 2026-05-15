"""Загрузка списка банков и шаблонов ссылок из banks.json (в корне проекта)."""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

_ROOT = Path(__file__).resolve().parent.parent
BANKS_PATH = _ROOT / "banks.json"


def load_banks() -> list[dict[str, str]]:
    """Каждый элемент: name, url_template (обязательно содержит подстроку {sub1})."""
    if not BANKS_PATH.is_file():
        return []
    try:
        raw = json.loads(BANKS_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as e:
        log.warning("banks.json read error: %s", e)
        return []
    banks = raw.get("banks")
    if not isinstance(banks, list):
        return []
    out: list[dict[str, str]] = []
    for b in banks:
        if not isinstance(b, dict):
            continue
        name = str(b.get("name", "")).strip()
        tpl = str(b.get("url_template", "")).strip()
        if not name or not tpl:
            continue
        if "{sub1}" not in tpl:
            log.warning("bank %r: url_template без {sub1}, пропуск", name)
            continue
        out.append({"name": name, "url_template": tpl})
    return out


def build_link(url_template: str, sub1: str) -> str:
    """Подставляет sub1 в шаблон (остальные {…} не трогаем — только replace {sub1})."""
    from urllib.parse import quote

    safe = quote(sub1.strip(), safe="")
    return url_template.replace("{sub1}", safe)

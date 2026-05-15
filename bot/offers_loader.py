"""Загрузка банков (banks.json) и МФО (mfo.json) с шаблонами ссылок и ставкой."""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any, Literal
from urllib.parse import quote

log = logging.getLogger(__name__)

OfferKind = Literal["bank", "mfo"]

_ROOT = Path(__file__).resolve().parent.parent
BANKS_PATH = _ROOT / "banks.json"
MFO_PATH = _ROOT / "mfo.json"


def _parse_offers(raw_list: Any, *, kind_label: str) -> list[dict[str, str]]:
    if not isinstance(raw_list, list):
        return []
    out: list[dict[str, str]] = []
    for item in raw_list:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name", "")).strip()
        tpl = str(item.get("url_template", "")).strip()
        rate = str(item.get("rate", "")).strip()
        if not name or not tpl:
            continue
        if "{sub1}" not in tpl:
            log.warning("%s %r: url_template без {sub1}, пропуск", kind_label, name)
            continue
        out.append({"name": name, "url_template": tpl, "rate": rate})
    return out


def _load_file(path: Path, key: str, kind_label: str) -> list[dict[str, str]]:
    if not path.is_file():
        return []
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as e:
        log.warning("%s read error: %s", path.name, e)
        return []
    return _parse_offers(raw.get(key), kind_label=kind_label)


def load_banks() -> list[dict[str, str]]:
    return _load_file(BANKS_PATH, "banks", "bank")


def load_mfo() -> list[dict[str, str]]:
    return _load_file(MFO_PATH, "mfo", "mfo")


def load_offers(kind: OfferKind) -> list[dict[str, str]]:
    return load_banks() if kind == "bank" else load_mfo()


def offer_button_label(offer: dict[str, str]) -> str:
    name = offer["name"]
    rate = offer.get("rate", "").strip()
    label = f"{name} — {rate}" if rate else name
    if len(label) > 64:
        return label[:61] + "..."
    return label


def build_link(url_template: str, sub1: str) -> str:
    safe = quote(sub1.strip(), safe="")
    return url_template.replace("{sub1}", safe)

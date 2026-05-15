from __future__ import annotations

import html
import json
import logging
from io import BytesIO
from typing import Any

import httpx
from aiogram import Bot, F, Router
from aiogram.filters import BaseFilter, Command, CommandObject, CommandStart, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    Message,
    ReplyKeyboardMarkup,
    ReplyKeyboardRemove,
)

from bot.banks_loader import build_link, load_banks
from bot.config import AgSearchBy, Mode, rko_group_api_key
from bot.excel_parse import filter_ag, filter_cpa_by_sub1, read_excel_bytes
from bot.rko_partner import get_rko_request_info

log = logging.getLogger(__name__)

router = Router()

CHANGE_MODE_BTN = "Сменить режим"
LINKS_BTN = "Получить ссылку"


class Form(StatesGroup):
    waiting_file = State()
    ag_pick_field = State()
    """AG: ждём инлайн «ИНН / фамилия». CPA: сразу in_session."""
    in_session = State()
    """Ждём Sub1 для сборки ссылки после выбора банка."""
    waiting_sub1_for_link = State()


def mode_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="CPA", callback_data="mode:cpa"),
                InlineKeyboardButton(text="AG", callback_data="mode:ag"),
            ],
        ]
    )


def ag_search_inline() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="По ИНН", callback_data="ag_field:inn"),
                InlineKeyboardButton(text="По фамилии", callback_data="ag_field:surname"),
            ],
        ]
    )


def session_reply_keyboard(*, mode: Mode, ag_by: AgSearchBy | None) -> ReplyKeyboardMarkup:
    if mode == "cpa":
        ph = "Sub1…"
    elif ag_by == "inn":
        ph = "ИНН…"
    elif ag_by == "surname":
        ph = "Фамилия…"
    else:
        ph = "Сначала тип поиска ↑"
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text=CHANGE_MODE_BTN)],
            [KeyboardButton(text=LINKS_BTN)],
        ],
        resize_keyboard=True,
        input_field_placeholder=ph,
    )


def links_only_reply_keyboard() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text=LINKS_BTN)]],
        resize_keyboard=True,
        input_field_placeholder="Потом выберешь банк",
    )


def _mode_title(mode: Mode) -> str:
    return "CPA" if mode == "cpa" else "AG"


def _ag_by_title(by: AgSearchBy) -> str:
    return "ИНН" if by == "inn" else "фамилия (первое слово ФИО)"


def _is_change_mode(text: str) -> bool:
    return text.strip().casefold() == CHANGE_MODE_BTN.casefold()


def _is_links_btn(text: str) -> bool:
    return text.strip().casefold() == LINKS_BTN.casefold()


class LinksButtonFilter(BaseFilter):
    async def __call__(self, message: Message) -> bool:
        return _is_links_btn(message.text or "")


def _bank_choice_markup(banks: list[dict[str, str]]) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    for i, b in enumerate(banks):
        label = b["name"]
        if len(label) > 64:
            label = label[:61] + "..."
        rows.append([InlineKeyboardButton(text=label, callback_data=f"lnk:{i}")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


async def _show_bank_picker(message: Message) -> None:
    banks = load_banks()
    if not banks:
        await message.answer(
            "Список банков пуст. Создай файл <code>banks.json</code> рядом с ботом "
            "(скопируй <code>banks.example.json</code> → <code>banks.json</code> и заполни).",
            parse_mode="HTML",
        )
        return
    await message.answer("Выбери банк:", reply_markup=_bank_choice_markup(banks))


async def show_links_flow(message: Message, state: FSMContext) -> None:
    """Сброс ввода Sub1 для ссылки (если был) и показ инлайн банков."""
    cur = await state.get_state()
    if cur == Form.waiting_sub1_for_link.state:
        data = await state.get_data()
        restore = data.get("_restore_state_after_link")
        await state.update_data(
            link_bank_idx=None,
            link_bank_name=None,
            _restore_state_after_link=None,
        )
        await state.set_state(restore)
    await _show_bank_picker(message)


@router.message(CommandStart())
async def cmd_start(message: Message, state: FSMContext) -> None:
    await state.clear()
    await message.answer(
        "<b>CPA</b> — загрузи таблицу и пиши <b>Sub1</b> подряд.\n"
        "<b>AG</b> — загрузи таблицу, выбери в инлайн поиск <b>по ИНН</b> или <b>по фамилии</b>, "
        "потом вводи значения подряд.\n"
        "Одиночная заявка из ЛК по API: <code>/rko ID</code> (нужен ключ в .env).\n"
        f"Партнёрские ссылки: кнопка «{html.escape(LINKS_BTN)}» или <code>/links</code> "
        "(список банков в <code>banks.json</code>).\n"
        f"Смена режима / новый тип поиска — «{html.escape(CHANGE_MODE_BTN)}» или новый файл.",
        reply_markup=ReplyKeyboardRemove(),
        parse_mode="HTML",
    )
    await message.answer("Выбери режим:", reply_markup=mode_keyboard())
    await message.answer(
        f"Ссылки: кнопка «{html.escape(LINKS_BTN)}» или <code>/links</code>.",
        reply_markup=links_only_reply_keyboard(),
        parse_mode="HTML",
    )


def _format_rko_api_payload(data: dict[str, Any]) -> str:
    """Человекочитаемый вывод JSON ответа API (известные поля сверху)."""
    priority = (
        "id",
        "status",
        "statusName",
        "statusComment",
        "openingDate",
        "activationDate",
        "openAccountAward",
        "activateAccountAward",
        "applicationLink",
        "code",
        "message",
    )
    lines: list[str] = []
    seen: set[str] = set()
    for k in priority:
        if k not in data:
            continue
        v = data[k]
        if v is None or v == "":
            continue
        seen.add(k)
        if isinstance(v, (dict, list)):
            v = json.dumps(v, ensure_ascii=False)[:1200]
        lines.append(f"<b>{html.escape(k)}</b>: {html.escape(str(v), quote=False)}")
    for k in sorted(data.keys()):
        if k in seen:
            continue
        v = data[k]
        if v is None or v == "":
            continue
        if isinstance(v, (dict, list)):
            v = json.dumps(v, ensure_ascii=False)[:800]
        lines.append(f"<b>{html.escape(k)}</b>: {html.escape(str(v), quote=False)}")
    return "\n".join(lines) if lines else "<i>Пустой ответ</i>"


@router.message(Command("rko"))
async def cmd_rko(message: Message, command: CommandObject) -> None:
    key = rko_group_api_key()
    if not key:
        await message.answer(
            "В .env задай <code>RKO_GROUP_API_KEY</code> — ключ из ЛК: "
            '<a href="https://rko-group.ru/users/my-token/">rko-group.ru/users/my-token</a>',
            parse_mode="HTML",
            disable_web_page_preview=True,
        )
        return
    arg = (command.args or "").strip()
    if not arg.isdigit():
        await message.answer(
            "Пример: <code>/rko 436195</code>\n"
            "Запрос к API: <code>GET /api/partner/rko/request-info/{id}</code>\n"
            "Документация: <a href=\"https://swagger.rko-group.ru/\">swagger.rko-group.ru</a>\n\n"
            "<i>В открытой спецификации нет метода «вся выгрузка как в ЛК» — только по ID заявки. "
            "Массовые обновления обычно делают через postback в API.</i>",
            parse_mode="HTML",
            disable_web_page_preview=True,
        )
        return
    rid = int(arg)
    try:
        status_code, body = await get_rko_request_info(key, rid)
    except httpx.RequestError as e:
        log.warning("rko api http error: %s", e)
        await message.answer(f"Ошибка сети при запросе к API: {html.escape(str(e))}")
        return

    if isinstance(body, dict):
        text = _format_rko_api_payload(body)
    else:
        text = html.escape(str(body))

    if status_code == 200 and isinstance(body, dict):
        await message.answer(
            f"<b>Заявка РКО #{rid}</b> (данные API)\n{text}",
            parse_mode="HTML",
        )
        return

    await message.answer(
        f"<b>HTTP {status_code}</b> — заявка #{rid}\n{text}",
        parse_mode="HTML",
    )


@router.message(StateFilter(Form.waiting_sub1_for_link), F.text)
async def on_link_sub1(message: Message, state: FSMContext) -> None:
    raw = message.text or ""
    if _is_change_mode(raw):
        await state.clear()
        await message.answer("Режим сброшен.", reply_markup=ReplyKeyboardRemove())
        await message.answer("Выбери режим:", reply_markup=mode_keyboard())
        await message.answer(
            f"Ссылки: «{html.escape(LINKS_BTN)}» или <code>/links</code>.",
            reply_markup=links_only_reply_keyboard(),
            parse_mode="HTML",
        )
        return
    if _is_links_btn(raw):
        await show_links_flow(message, state)
        return

    data = await state.get_data()
    idx = data.get("link_bank_idx")
    name = data.get("link_bank_name") or "—"
    restore = data.get("_restore_state_after_link")
    banks = load_banks()
    if not isinstance(idx, int) or idx < 0 or idx >= len(banks):
        await state.update_data(
            link_bank_idx=None,
            link_bank_name=None,
            _restore_state_after_link=None,
        )
        await state.set_state(restore)
        await message.answer("Сессия ссылки сброшена. Нажми /links снова.")
        return

    sub1 = raw.strip()
    if not sub1:
        await message.answer("Введи непустой <b>Sub1</b>.", parse_mode="HTML")
        return

    tpl = banks[idx]["url_template"]
    url = build_link(tpl, sub1)
    await state.update_data(
        link_bank_idx=None,
        link_bank_name=None,
        _restore_state_after_link=None,
    )
    await state.set_state(restore)

    esc_url = html.escape(url, quote=True)
    esc_name = html.escape(str(name), quote=False)
    esc_sub = html.escape(sub1, quote=False)

    st_after = await state.get_state()
    sdata2 = await state.get_data()
    mode_after = sdata2.get("mode")
    reply_kb: ReplyKeyboardMarkup | ReplyKeyboardRemove
    if st_after == Form.in_session.state and mode_after in ("cpa", "ag"):
        ag_b = sdata2.get("ag_by") if mode_after == "ag" else None
        reply_kb = session_reply_keyboard(
            mode=mode_after,
            ag_by=ag_b if ag_b in ("inn", "surname") else None,
        )
    elif st_after == Form.ag_pick_field.state and mode_after == "ag":
        reply_kb = session_reply_keyboard(mode="ag", ag_by=None)
    else:
        reply_kb = links_only_reply_keyboard()

    await message.answer(
        f"Банк: <b>{esc_name}</b>\nSub1: <code>{esc_sub}</code>\n\n"
        f'<a href="{esc_url}">Открыть ссылку</a>\n<code>{esc_url}</code>',
        parse_mode="HTML",
        disable_web_page_preview=True,
        reply_markup=reply_kb,
    )


@router.message(LinksButtonFilter())
async def on_links_button(message: Message, state: FSMContext) -> None:
    await show_links_flow(message, state)


@router.message(Command("links"))
async def cmd_links(message: Message, state: FSMContext) -> None:
    await show_links_flow(message, state)


@router.callback_query(F.data.startswith("lnk:"))
async def on_pick_bank_for_link(cb: CallbackQuery, state: FSMContext) -> None:
    if not cb.message:
        await cb.answer()
        return
    part = (cb.data or "").removeprefix("lnk:")
    if not part.isdigit():
        await cb.answer()
        return
    idx = int(part)
    banks = load_banks()
    if idx < 0 or idx >= len(banks):
        await cb.answer("Обнови список банков в banks.json", show_alert=True)
        return
    prev = await state.get_state()
    b = banks[idx]
    await state.update_data(
        _restore_state_after_link=prev,
        link_bank_idx=idx,
        link_bank_name=b["name"],
    )
    await state.set_state(Form.waiting_sub1_for_link)
    await cb.message.answer(
        f"Банк: <b>{html.escape(b['name'], quote=False)}</b>\n"
        "Введи <b>Sub1</b> — подставлю в ссылку (или «Сменить режим» чтобы выйти).",
        parse_mode="HTML",
    )
    await cb.answer()


@router.message(Command("mode"))
async def cmd_mode(message: Message, state: FSMContext) -> None:
    await state.clear()
    await message.answer("Сессия сброшена.", reply_markup=ReplyKeyboardRemove())
    await message.answer("Выбери режим:", reply_markup=mode_keyboard())
    await message.answer(
        f"Ссылки: «{html.escape(LINKS_BTN)}» или <code>/links</code>.",
        reply_markup=links_only_reply_keyboard(),
        parse_mode="HTML",
    )


@router.callback_query(F.data.startswith("mode:"))
async def on_mode(cb: CallbackQuery, state: FSMContext) -> None:
    raw = (cb.data or "").split(":", 1)[-1]
    if raw not in ("cpa", "ag"):
        await cb.answer("Неизвестный режим", show_alert=True)
        return
    mode: Mode = raw  # type: ignore[assignment]
    await state.set_state(Form.waiting_file)
    await state.update_data(mode=mode, ag_by=None)
    await cb.message.answer(
        f"Режим: <b>{_mode_title(mode)}</b>. Пришли Excel (.xlsx).",
        parse_mode="HTML",
    )
    await cb.answer()


def _is_xlsx_document(message: Message) -> bool:
    doc = message.document
    if not doc:
        return False
    name = (doc.file_name or "").lower()
    if name.endswith(".xlsx"):
        return True
    mime = (doc.mime_type or "").lower()
    return mime in (
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )


async def _load_xlsx_bytes(message: Message, bot: Bot) -> bytes | None:
    doc = message.document
    if not doc or not _is_xlsx_document(message):
        return None
    file = await bot.get_file(doc.file_id)
    buf = BytesIO()
    await bot.download_file(file.file_path, destination=buf)
    data = buf.getvalue()
    if not data:
        return None
    return data


@router.message(StateFilter(Form.waiting_file), F.document)
async def on_file(message: Message, state: FSMContext, bot: Bot) -> None:
    data = await _load_xlsx_bytes(message, bot)
    if data is None:
        await message.answer("Нужен файл <b>.xlsx</b>.", parse_mode="HTML")
        return
    sdata = await state.get_data()
    mode = sdata.get("mode")
    if mode not in ("cpa", "ag"):
        await state.clear()
        await message.answer("Сначала выбери режим (/start).", reply_markup=ReplyKeyboardRemove())
        return

    await state.update_data(file_bytes=data, ag_by=None)

    if mode == "cpa":
        await state.set_state(Form.in_session)
        await message.answer(
            "Таблица <b>CPA</b> загружена. Пиши <b>Sub1</b> (можно подряд).\n"
            f"Новый файл — просто пришли .xlsx снова. {html.escape(CHANGE_MODE_BTN)} — другой режим.",
            reply_markup=session_reply_keyboard(mode="cpa", ag_by=None),
            parse_mode="HTML",
        )
        return

    await state.set_state(Form.ag_pick_field)
    await message.answer(
        "Таблица <b>AG</b> загружена.\n"
        "<b>Выбери тип поиска</b> (кнопки ниже), затем вводи ИНН или фамилию.",
        reply_markup=session_reply_keyboard(mode="ag", ag_by=None),
        parse_mode="HTML",
    )
    await message.answer("Тип поиска:", reply_markup=ag_search_inline())


@router.message(StateFilter(Form.waiting_file))
async def on_file_wrong(message: Message) -> None:
    await message.answer("Ожидаю .xlsx документом.")


@router.message(StateFilter(Form.ag_pick_field), F.document)
async def on_replace_file_ag_pick(message: Message, state: FSMContext, bot: Bot) -> None:
    data = await _load_xlsx_bytes(message, bot)
    if data is None:
        await message.answer("Нужен <b>.xlsx</b>.", parse_mode="HTML")
        return
    await state.update_data(file_bytes=data, ag_by=None)
    await message.answer(
        "Файл AG обновлён. Снова выбери тип поиска.",
        reply_markup=session_reply_keyboard(mode="ag", ag_by=None),
        parse_mode="HTML",
    )
    await message.answer("Тип поиска:", reply_markup=ag_search_inline())


@router.message(StateFilter(Form.ag_pick_field), F.text)
async def on_ag_pick_text(message: Message, state: FSMContext) -> None:
    raw = message.text or ""
    if _is_change_mode(raw):
        await state.clear()
        await message.answer("Режим сброшен.", reply_markup=ReplyKeyboardRemove())
        await message.answer("Выбери режим:", reply_markup=mode_keyboard())
        return
    await message.answer(
        "Сначала нажми <b>По ИНН</b> или <b>По фамилии</b> под предыдущим сообщением.",
        parse_mode="HTML",
    )
    await message.answer("Тип поиска:", reply_markup=ag_search_inline())


@router.message(StateFilter(Form.ag_pick_field))
async def on_ag_pick_other(message: Message) -> None:
    await message.answer("Выбери тип поиска инлайн-кнопками или пришли новый .xlsx.")


@router.callback_query(F.data.startswith("ag_field:"), StateFilter(Form.ag_pick_field, Form.in_session))
async def on_ag_field(cb: CallbackQuery, state: FSMContext) -> None:
    raw = (cb.data or "").removeprefix("ag_field:")
    if raw not in ("inn", "surname"):
        await cb.answer()
        return
    by: AgSearchBy = raw  # type: ignore[assignment]

    sdata = await state.get_data()
    mode = sdata.get("mode")

    if mode != "ag":
        await cb.answer("Сначала выбери режим AG и загрузи таблицу.", show_alert=True)
        return

    await state.update_data(ag_by=by)
    await state.set_state(Form.in_session)

    label = "ИНН" if by == "inn" else "фамилию (как в ФИО, по первому слову)"
    await cb.message.answer(
        f"Ищем по <b>{html.escape(_ag_by_title(by))}</b>. Введи {label}.",
        reply_markup=session_reply_keyboard(mode="ag", ag_by=by),
        parse_mode="HTML",
    )
    await cb.answer()


@router.message(StateFilter(Form.in_session), F.document)
async def on_replace_file(message: Message, state: FSMContext, bot: Bot) -> None:
    data = await _load_xlsx_bytes(message, bot)
    if data is None:
        await message.answer("Нужен <b>.xlsx</b> или текст запроса.", parse_mode="HTML")
        return
    sdata = await state.get_data()
    mode = sdata.get("mode")
    await state.update_data(file_bytes=data)
    if mode == "ag":
        await state.update_data(ag_by=None)
        await state.set_state(Form.ag_pick_field)
        await message.answer(
            "Таблица AG обновлена. Выбери тип поиска снова.",
            reply_markup=session_reply_keyboard(mode="ag", ag_by=None),
            parse_mode="HTML",
        )
        await message.answer("Тип поиска:", reply_markup=ag_search_inline())
        return

    await message.answer(
        "Таблица CPA обновлена. Пиши <b>Sub1</b>.",
        reply_markup=session_reply_keyboard(mode="cpa", ag_by=None),
        parse_mode="HTML",
    )


def _format_cpa(sub1: str, rows: list[tuple[str, str]]) -> str:
    sub_safe = html.escape(sub1, quote=False)
    if not rows:
        return (
            f"Режим: <b>CPA</b>\nПо Sub1: <code>{sub_safe}</code>\n<i>Записей нет.</i>"
        )
    lines = [
        f"Режим: <b>CPA</b>\nПо Sub1: <code>{sub_safe}</code>\nНайдено: <b>{len(rows)}</b>",
        "",
    ]
    for i, (offer, status) in enumerate(rows, start=1):
        o = html.escape(offer, quote=False)
        s = html.escape(status, quote=False)
        lines.append(f"{i}. <b>{o}</b> — <i>{s}</i>")
    return "\n".join(lines)


def _format_ag(by: AgSearchBy, query: str, rows: list[tuple[str, str]]) -> str:
    q = html.escape(query.strip(), quote=False)
    by_human = "ИНН" if by == "inn" else "фамилии"
    if not rows:
        return (
            f"Режим: <b>AG</b>\nПо {by_human}: <code>{q}</code>\n<i>Записей нет.</i>"
        )
    lines = [
        f"Режим: <b>AG</b>\nПо {by_human}: <code>{q}</code>\nНайдено: <b>{len(rows)}</b>",
        "",
    ]
    for i, (bank, status) in enumerate(rows, start=1):
        b = html.escape(bank, quote=False)
        s = html.escape(status, quote=False)
        lines.append(f"{i}. <b>{b}</b> — <i>{s}</i>")
    return "\n".join(lines)


def _split_telegram(text: str, limit: int = 3800) -> list[str]:
    if len(text) <= limit:
        return [text]
    chunks: list[str] = []
    cur: list[str] = []
    cur_len = 0
    for line in text.splitlines():
        add = len(line) + 1
        if cur_len + add > limit and cur:
            chunks.append("\n".join(cur))
            cur = [line]
            cur_len = len(line)
        else:
            cur.append(line)
            cur_len += add
    if cur:
        chunks.append("\n".join(cur))
    return chunks


@router.message(StateFilter(Form.in_session), F.text)
async def on_lookup_session(message: Message, state: FSMContext) -> None:
    raw = message.text or ""
    if _is_change_mode(raw):
        await state.clear()
        await message.answer("Режим сброшен.", reply_markup=ReplyKeyboardRemove())
        await message.answer("Выбери режим:", reply_markup=mode_keyboard())
        return

    sdata: dict[str, Any] = await state.get_data()
    mode = sdata.get("mode")
    file_bytes = sdata.get("file_bytes")
    if mode not in ("cpa", "ag") or not isinstance(file_bytes, (bytes, bytearray)):
        await state.clear()
        await message.answer("Сессия сброшена. /start", reply_markup=ReplyKeyboardRemove())
        return

    try:
        df = read_excel_bytes(bytes(file_bytes))
    except Exception as e:  # noqa: BLE001
        log.exception("excel read failed")
        await message.answer(f"Не удалось прочитать Excel: {e}")
        return

    if mode == "cpa":
        err, rows = filter_cpa_by_sub1(df, raw.strip())
        if err:
            await message.answer(err)
            return
        text = _format_cpa(raw.strip(), rows)
    else:
        ag_by = sdata.get("ag_by")
        if ag_by not in ("inn", "surname"):
            await state.set_state(Form.ag_pick_field)
            await message.answer(
                "Сначала выбери тип поиска.",
                reply_markup=session_reply_keyboard(mode="ag", ag_by=None),
            )
            await message.answer("Тип поиска:", reply_markup=ag_search_inline())
            return
        err, rows = filter_ag(df, ag_by, raw.strip())
        if err:
            await message.answer(err)
            return
        text = _format_ag(ag_by, raw.strip(), rows)

    for part in _split_telegram(text):
        await message.answer(part, parse_mode="HTML")

    if mode == "ag":
        await message.answer(
            "Можно ввести ещё запрос или сменить тип:",
            reply_markup=ag_search_inline(),
        )


@router.message(StateFilter(Form.in_session))
async def on_session_other(message: Message, state: FSMContext) -> None:
    sdata = await state.get_data()
    mode = sdata.get("mode")
    if mode == "ag":
        await message.answer(
            "Напиши ИНН или фамилию текстом, пришли новый .xlsx или нажми инлайн "
            "<b>По ИНН / По фамилии</b>.",
            parse_mode="HTML",
        )
        return
    await message.answer(
        "Напиши <b>Sub1</b> или пришли новый .xlsx. "
        f"«{html.escape(CHANGE_MODE_BTN)}» — смена режима.",
        parse_mode="HTML",
    )

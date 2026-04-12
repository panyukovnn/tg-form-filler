import asyncio
import json
import logging
import os
from datetime import datetime, timezone, timedelta

import telegramify_markdown
from telegram import Update
from telegram.constants import ParseMode
from telegram.ext import ApplicationBuilder, ContextTypes, MessageHandler, filters

from tg_form_filler.form_filler import format_result, submit_form
from tg_form_filler.llm_handler import (
    generate_spending_report,
    select_form_and_parse,
    analyze_recent_meals,
    generate_daily_nutrition_report,
    generate_weekly_nutrition_report,
    generate_monthly_nutrition_report,
    compute_auto_default,
    is_drink_row,
)
import tg_form_filler.sheets_editor as sheets_editor
import tg_form_filler.sheets_reader as sheets_reader

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logging.getLogger("httpx").setLevel(logging.WARNING)
logger = logging.getLogger(__name__)

TG_ADMIN_CHAT_ID = int(os.getenv("TG_ADMIN_CHAT_ID", "0"))

MSK = timezone(timedelta(hours=3))

_FOOD_FORM_ID = "food_diary"
_SPENDING_FORM_ID = "spending_diary"
_CHECK_INTERVAL_SEC = 30 * 60   # проверять каждые 30 минут
_FOOD_GAP_HOURS = 8             # напомнить если > 8 часов без записи
_REMINDER_COOLDOWN_SEC = 4 * 3600  # не чаще раза в 4 часа
_DAY_START_HOUR = 8             # с 08:00 МСК
_DAY_END_HOUR = 22              # до 22:00 МСК
_MERGE_WINDOW_SEC = 10 * 60     # предлагать объединение если прошло < 10 минут
_PENDING_MERGE_TTL_SEC = 15 * 60  # подтверждение объединения живёт 15 минут

_last_food_diary_at: datetime = datetime.now(MSK)
_last_food_entry_at: datetime | None = None  # для merge-логики (None до первой записи)
_last_reminder_sent_at: datetime | None = None
_MAX_COMMENT_HISTORY = 10
_meal_comment_history: list[str] = []
_MAX_CHAT_HISTORY = 10
_chat_history: list[dict] = []
_pending_merge: dict | None = None

_AFFIRMATIVE_WORDS = {
    "да", "ага", "угу", "ок", "окей", "yes", "y", "yep", "yeah",
    "давай", "объедини", "объединяй", "объединить", "+", "конечно",
}
_NEGATIVE_WORDS = {
    "нет", "не", "no", "n", "не надо", "не нужно", "отдельно", "отдельный",
}


async def _send_md(bot, chat_id: int, text: str) -> None:
    await bot.send_message(
        chat_id=chat_id,
        text=telegramify_markdown.markdownify(text),
        parse_mode=ParseMode.MARKDOWN_V2,
    )


async def _reply_md(message, text: str):
    return await message.reply_text(
        telegramify_markdown.markdownify(text),
        parse_mode=ParseMode.MARKDOWN_V2,
    )


def _daytime_hours_since(since: datetime, now: datetime) -> float:
    """Считает количество часов в дневном окне [DAY_START, DAY_END) между двумя моментами."""
    total = 0.0
    cursor = since
    while cursor < now:
        day_start = cursor.replace(hour=_DAY_START_HOUR, minute=0, second=0, microsecond=0)
        day_end = cursor.replace(hour=_DAY_END_HOUR, minute=0, second=0, microsecond=0)
        window_start = max(cursor, day_start)
        window_end = min(now, day_end)
        if window_start < window_end:
            total += (window_end - window_start).total_seconds() / 3600
        cursor = (cursor + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
    return total


async def _food_reminder_loop(app) -> None:
    global _last_reminder_sent_at
    while True:
        await asyncio.sleep(_CHECK_INTERVAL_SEC)
        now = datetime.now(MSK)
        if not (_DAY_START_HOUR <= now.hour < _DAY_END_HOUR):
            continue
        if _last_reminder_sent_at and (now - _last_reminder_sent_at).total_seconds() < _REMINDER_COOLDOWN_SEC:
            continue
        daytime_hours = _daytime_hours_since(_last_food_diary_at, now)
        if daytime_hours >= _FOOD_GAP_HOURS:
            logger.info("Sending food reminder (%.1f daytime h since last entry)", daytime_hours)
            await app.bot.send_message(
                chat_id=TG_ADMIN_CHAT_ID,
                text=f"Ты не записывал еду уже {int(daytime_hours)} ч. Не забудь записать приём пищи!",
            )
            _last_reminder_sent_at = now


def _is_drink_entry(config: dict, field_values: dict) -> bool:
    """Определить, помечена ли только что распарсенная запись как напиток."""
    for field in config["fields"]:
        if field.get("sheet_only") and field.get("name") == "Напиток":
            val = (field_values.get(field["entry_id"]) or "").strip().lower()
            return val in {"да", "true", "1", "yes"}
    return False


def _apply_auto_defaults(config: dict, field_values: dict) -> None:
    """Заполняет поля с auto_default, если они не заданы LLM."""
    now = datetime.now(MSK)
    for field in config["fields"]:
        auto = field.get("auto_default")
        if not auto:
            continue
        if field_values.get(field["entry_id"]):
            continue
        default_value = compute_auto_default(auto, now)
        if default_value is not None:
            field_values[field["entry_id"]] = default_value


_TIMESTAMP_DATE_FORMATS = ("%d.%m.%Y", "%Y-%m-%d", "%d/%m/%Y", "%m/%d/%Y")


def _parse_sheet_date(ts: str):
    """Из строки вида '13.04.2026 9:15:00' достать date (первый токен)."""
    if not ts:
        return None
    first = ts.strip().split()[0]
    for fmt in _TIMESTAMP_DATE_FORMATS:
        try:
            return datetime.strptime(first, fmt).date()
        except ValueError:
            continue
    return None


def _parse_price(raw: str) -> float:
    try:
        return float(str(raw).replace(",", ".").replace("\xa0", "").replace(" ", ""))
    except (ValueError, AttributeError):
        return 0.0


def _get_column_name(config: dict, field_name: str) -> str:
    for field in config["fields"]:
        if field["name"] == field_name:
            return field.get("column_name", field_name)
    return field_name


def _fetch_yesterday_spending(config: dict) -> list[dict]:
    """Прочитать из таблицы расходов строки за вчерашний день."""
    spreadsheet_id = config.get("spreadsheet_id", "")
    if not spreadsheet_id:
        logger.warning("spending config has no spreadsheet_id, skipping")
        return []

    sheet_name = config.get("sheet_name", "")
    rows = sheets_editor.get_all_data_rows(spreadsheet_id, sheet_name=sheet_name)

    timestamp_col = config.get("timestamp_column", "Отметка времени")
    category_col = _get_column_name(config, "Категория")
    item_col = _get_column_name(config, "Товар")
    price_col = _get_column_name(config, "Цена")

    yesterday = (datetime.now(MSK) - timedelta(days=1)).date()

    entries = []
    for row in rows:
        row_date = _parse_sheet_date(row.get(timestamp_col, ""))
        if row_date != yesterday:
            continue
        entries.append({
            "category": row.get(category_col, ""),
            "item": row.get(item_col, ""),
            "price": _parse_price(row.get(price_col, "")),
        })
    return entries


async def _daily_spending_report_loop(app) -> None:
    while True:
        now = datetime.now(MSK)
        next_report = now.replace(hour=9, minute=0, second=0, microsecond=0)
        if now >= next_report:
            next_report += timedelta(days=1)
        wait_sec = (next_report - now).total_seconds()
        logger.info("Daily spending report scheduled in %.0f s", wait_sec)
        await asyncio.sleep(wait_sec)

        try:
            spending_config = next(
                (c for c in FORM_CONFIGS if c["form_id"] == _SPENDING_FORM_ID), None
            )
            if spending_config is None:
                logger.warning("Spending form config not loaded, skipping report")
                continue
            entries = _fetch_yesterday_spending(spending_config)
            report = generate_spending_report(entries)
            logger.info("Sending daily spending report (%d entries)", len(entries))
            await _send_md(app.bot, TG_ADMIN_CHAT_ID, report)
        except Exception:
            logger.exception("Error generating daily spending report")


async def _daily_nutrition_report_loop(app) -> None:
    while True:
        now = datetime.now(MSK)
        next_report = now.replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(days=1)
        wait_sec = (next_report - now).total_seconds()
        logger.info("Daily nutrition report scheduled in %.0f s", wait_sec)
        await asyncio.sleep(wait_sec)

        try:
            yesterday = datetime.now(MSK) - timedelta(days=1)
            entries = sheets_reader.get_entries_for_date(yesterday)
            report = generate_daily_nutrition_report(entries)
            logger.info("Sending daily nutrition report (%d entries)", len(entries))
            await _send_md(app.bot, TG_ADMIN_CHAT_ID, report)
        except Exception:
            logger.exception("Error generating daily nutrition report")


async def _weekly_nutrition_report_loop(app) -> None:
    while True:
        now = datetime.now(MSK)
        days_until_monday = (7 - now.weekday()) % 7
        if days_until_monday == 0 and now.hour >= 0:
            days_until_monday = 7
        next_report = (now + timedelta(days=days_until_monday)).replace(
            hour=0, minute=5, second=0, microsecond=0
        )
        wait_sec = (next_report - now).total_seconds()
        logger.info("Weekly nutrition report scheduled in %.0f s", wait_sec)
        await asyncio.sleep(wait_sec)

        try:
            end_date = datetime.now(MSK) - timedelta(days=1)
            start_date = end_date - timedelta(days=6)
            entries = sheets_reader.get_entries_for_range(start_date, end_date)
            report = generate_weekly_nutrition_report(entries)
            logger.info("Sending weekly nutrition report (%d entries)", len(entries))
            await _send_md(app.bot, TG_ADMIN_CHAT_ID, report)
        except Exception:
            logger.exception("Error generating weekly nutrition report")


async def _monthly_nutrition_report_loop(app) -> None:
    while True:
        now = datetime.now(MSK)
        if now.month == 12:
            next_month_first = now.replace(year=now.year + 1, month=1, day=1,
                                           hour=0, minute=10, second=0, microsecond=0)
        else:
            next_month_first = now.replace(month=now.month + 1, day=1,
                                           hour=0, minute=10, second=0, microsecond=0)
        wait_sec = (next_month_first - now).total_seconds()
        logger.info("Monthly nutrition report scheduled in %.0f s", wait_sec)
        await asyncio.sleep(wait_sec)

        try:
            end_date = datetime.now(MSK) - timedelta(days=1)
            start_date = end_date.replace(day=1)
            entries = sheets_reader.get_entries_for_range(start_date, end_date)
            report = generate_monthly_nutrition_report(entries)
            logger.info("Sending monthly nutrition report (%d entries)", len(entries))
            await _send_md(app.bot, TG_ADMIN_CHAT_ID, report)
        except Exception:
            logger.exception("Error generating monthly nutrition report")


async def _post_init(app) -> None:
    asyncio.create_task(_food_reminder_loop(app))
    asyncio.create_task(_daily_spending_report_loop(app))
    asyncio.create_task(_daily_nutrition_report_loop(app))
    asyncio.create_task(_weekly_nutrition_report_loop(app))
    asyncio.create_task(_monthly_nutrition_report_loop(app))

FORM_CONFIGS_DIR = os.getenv("FORM_CONFIGS_DIR", "form_configs")
FORM_CONFIGS = []
for config_file in ("spending_diary_form_config.json", "food_diary_form_config.json"):
    path = os.path.join(FORM_CONFIGS_DIR, config_file)
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            FORM_CONFIGS.append(json.load(f))

if not FORM_CONFIGS:
    raise RuntimeError(
        "No form config files found (spending_diary_form_config.json, food_diary_form_config.json)"
    )

logger.info("Loaded %d form(s): %s", len(FORM_CONFIGS), [c["form_name"] for c in FORM_CONFIGS])


def _first_word(message: str) -> str:
    normalized = message.strip().lower()
    words = normalized.split()
    if not words:
        return ""
    return words[0].strip(".!?,;:()")


def _is_affirmative(message: str) -> bool:
    normalized = message.strip().lower().strip(".!?,;:()")
    return normalized in _AFFIRMATIVE_WORDS or _first_word(message) in _AFFIRMATIVE_WORDS


def _is_negative(message: str) -> bool:
    normalized = message.strip().lower().strip(".!?,;:()")
    return normalized in _NEGATIVE_WORDS or _first_word(message) in _NEGATIVE_WORDS


def _merge_field_value(field: dict, old_val: str, new_val: str) -> str:
    """Объединить старое и новое значение поля при слиянии записей."""
    options = field.get("options") or []
    field_type = field.get("type", "")

    # Числовые dropdown'ы (порции) — суммировать с ограничением сверху
    if field_type == "dropdown" and options and all(o.isdigit() for o in options):
        try:
            total = int(old_val or 0) + int(new_val or 0)
            max_val = max(int(o) for o in options)
            return str(min(total, max_val))
        except ValueError:
            return new_val or old_val

    # Текстовые поля — конкатенация
    if field_type == "text":
        if not old_val:
            return new_val
        if not new_val:
            return old_val
        if new_val.lower() in old_val.lower():
            return old_val
        return f"{old_val}; {new_val}"

    # Radio (категория, признак напитка) — оставляем старое значение
    return old_val or new_val


def _handle_merge(selected_config: dict, new_values: dict) -> str | None:
    """Объединить новый приём пищи с последней записью в Google Sheets.

    Возвращает текст ответа для пользователя или None если последней записи нет.
    """
    spreadsheet_id = selected_config.get("spreadsheet_id", "")
    if not spreadsheet_id:
        return "Ошибка: spreadsheet_id не указан в конфигурации формы."

    sheet_name = selected_config.get("sheet_name", "")
    newest_first = selected_config.get("newest_first", False)
    rows = sheets_editor.get_last_rows(
        spreadsheet_id, n=5, sheet_name=sheet_name, newest_first=newest_first
    )
    if not rows:
        return None

    # Ищем ближайшую по времени НЕ-напиточную запись — напитки не объединяем
    target = None
    for row in reversed(rows):
        row_dict = dict(zip(row["headers"], row["values"]))
        if not is_drink_row(row_dict):
            target = row
            break
    if target is None:
        return None

    headers = target["headers"]
    old_values = target["values"]

    updates: dict[int, str] = {}
    changes = []
    for field in selected_config["fields"]:
        entry_id = field["entry_id"]
        if entry_id not in new_values:
            continue
        new_val = new_values[entry_id]
        if not new_val:
            continue
        field_name = field["name"]
        column_name = field.get("column_name", field_name)
        if column_name not in headers:
            continue
        col_idx = headers.index(column_name)
        old_val = old_values[col_idx] if col_idx < len(old_values) else ""
        merged = _merge_field_value(field, old_val, new_val)
        if merged != old_val:
            updates[col_idx] = merged
            changes.append(f"  {field_name}: {old_val or '—'} → {merged}")

    if not updates:
        return "Нечего объединять — новых данных не оказалось."

    sheets_editor.update_cells(
        spreadsheet_id, target["row_number"], updates, sheet_name=sheet_name
    )
    logger.info("Merged food entry into row %d", target["row_number"])
    return "🔗 Объединил с предыдущей записью!\n\n" + "\n".join(changes)


async def _update_sheet_only_fields(selected_config: dict, field_values: dict) -> None:
    """Дописать в только что созданную строку поля с sheet_only=true (например, чекбокс 'Напиток').

    Google Forms → Sheets пропагация занимает ~1-3 с, поэтому ждём появления строки с
    совпадающим текстовым полем (обычно 'Блюдо'/'Описание').
    """
    sheet_only_fields = [f for f in selected_config["fields"] if f.get("sheet_only")]
    if not sheet_only_fields:
        return
    if not any(field_values.get(f["entry_id"]) for f in sheet_only_fields):
        return

    spreadsheet_id = selected_config.get("spreadsheet_id", "")
    sheet_name = selected_config.get("sheet_name", "")
    newest_first = selected_config.get("newest_first", False)

    match_field = next(
        (f for f in selected_config["fields"]
         if f.get("type") == "text" and not f.get("sheet_only")),
        None,
    )
    expected = (field_values.get(match_field["entry_id"], "").strip()
                if match_field else "")
    match_column = (match_field.get("column_name", match_field["name"])
                    if match_field else None)

    target = None
    for attempt in range(6):
        rows = sheets_editor.get_last_rows(
            spreadsheet_id, n=1, sheet_name=sheet_name, newest_first=newest_first
        )
        if rows:
            row = rows[-1]
            if match_column and match_column in row["headers"]:
                col_idx = row["headers"].index(match_column)
                actual = row["values"][col_idx] if col_idx < len(row["values"]) else ""
                if actual.strip() == expected:
                    target = row
                    break
            else:
                target = row
                break
        await asyncio.sleep(1.5)

    if target is None:
        logger.warning(
            "Could not locate just-submitted row for sheet-only update (expected %r)", expected
        )
        return

    headers = target["headers"]
    updates: dict[int, str] = {}
    for field in sheet_only_fields:
        value = field_values.get(field["entry_id"])
        if not value:
            continue
        column_name = field.get("column_name", field["name"])
        if column_name not in headers:
            logger.warning("Sheet column %r not found for sheet-only field %r",
                           column_name, field["name"])
            continue
        col_idx = headers.index(column_name)
        updates[col_idx] = value

    if updates:
        sheets_editor.update_cells(
            spreadsheet_id, target["row_number"], updates, sheet_name=sheet_name
        )
        logger.info("Updated %d sheet-only field(s) in row %d",
                    len(updates), target["row_number"])


async def _submit_food_entry(selected_config: dict, field_values: dict) -> str:
    """Отправить запись в дневник питания + получить комментарий нутрициолога."""
    global _last_food_diary_at, _last_food_entry_at
    _apply_auto_defaults(selected_config, field_values)
    logger.info("Submitting food entry: %s", selected_config["form_name"])
    result = submit_form(selected_config, field_values)
    reply = f"Форма: {selected_config['form_name']}\n\n" + format_result(result)

    # Напитки не сдвигают окно merge и не сбрасывают таймер напоминания о еде
    if not _is_drink_entry(selected_config, field_values):
        now = datetime.now(MSK)
        _last_food_diary_at = now
        _last_food_entry_at = now

    if not result["success"]:
        return reply

    try:
        await _update_sheet_only_fields(selected_config, field_values)
    except Exception:
        logger.exception("Error updating sheet-only fields")

    try:
        spreadsheet_id = selected_config.get("spreadsheet_id", "")
        sheet_name = selected_config.get("sheet_name", "")
        newest_first = selected_config.get("newest_first", False)
        rows = sheets_editor.get_last_rows(
            spreadsheet_id, n=9, sheet_name=sheet_name, newest_first=newest_first
        )
        recent = [dict(zip(r["headers"], r["values"])) for r in rows]
        # Только что отправленная запись добавляется в конец, т.к. данные формы
        # могут ещё не появиться в таблице
        just_added = {
            field["name"]: field_values.get(field["entry_id"], "")
            for field in selected_config["fields"]
        }
        recent.append(just_added)
        if recent:
            analysis = analyze_recent_meals(recent, _meal_comment_history)
            if analysis:
                reply += f"\n\n{analysis}"
                _meal_comment_history.append(analysis)
                if len(_meal_comment_history) > _MAX_COMMENT_HISTORY:
                    _meal_comment_history.pop(0)
    except Exception:
        logger.exception("Error analyzing recent meals")
    return reply


def _handle_edit(selected_config: dict, entry_offset: int, field_values: dict) -> str:
    """Редактирование существующей записи в Google Sheets."""
    spreadsheet_id = selected_config.get("spreadsheet_id", "")
    if not spreadsheet_id:
        return "Ошибка: spreadsheet_id не указан в конфигурации формы."

    sheet_name = selected_config.get("sheet_name", "")
    newest_first = selected_config.get("newest_first", False)
    rows = sheets_editor.get_last_rows(spreadsheet_id, n=max(entry_offset, 5),
                                       sheet_name=sheet_name, newest_first=newest_first)
    if not rows:
        return "Не найдено записей для редактирования."

    max_editable = 3
    if entry_offset < 1 or entry_offset > max_editable:
        return f"Можно редактировать только последние {max_editable} записи."

    if entry_offset > len(rows):
        return f"Запись #{entry_offset} не найдена. Доступно записей: {len(rows)}."

    target = rows[-entry_offset]
    headers = target["headers"]
    old_values = target["values"]

    # Маппинг entry_id → col_index через конфигурацию формы
    updates = {}
    changes = []
    for field in selected_config["fields"]:
        entry_id = field["entry_id"]
        if entry_id in field_values and field_values[entry_id]:
            new_value = field_values[entry_id]
            field_name = field["name"]
            column_name = field.get("column_name", field_name)
            if column_name in headers:
                col_idx = headers.index(column_name)
                old_value = old_values[col_idx] if col_idx < len(old_values) else ""
                updates[col_idx] = new_value
                changes.append(f"  {field_name}: {old_value or '—'} → {new_value}")

    if not updates:
        return "Не указаны поля для изменения."

    sheets_editor.update_cells(spreadsheet_id, target["row_number"], updates, sheet_name=sheet_name)

    return "✏️ Запись исправлена!\n\n" + "\n".join(changes)


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id

    if user_id != TG_ADMIN_CHAT_ID:
        logger.warning("Blocked message from user %s", user_id)
        return

    user_message = update.message.text
    logger.info("Message from %s: %s", user_id, user_message)

    global _pending_merge, _last_food_diary_at, _last_food_entry_at

    pre_replies: list[str] = []
    pending_handled_fully = False

    # Сначала обрабатываем ожидание подтверждения объединения
    if _pending_merge is not None:
        age = (datetime.now(MSK) - _pending_merge["created_at"]).total_seconds()
        if age > _PENDING_MERGE_TTL_SEC:
            logger.info("Pending merge expired, dropping it")
            _pending_merge = None
        elif _is_affirmative(user_message):
            pending = _pending_merge
            _pending_merge = None
            try:
                merge_reply = _handle_merge(pending["config"], pending["new_values"])
            except Exception:
                logger.exception("Error merging meal")
                merge_reply = None
            if merge_reply is None:
                # fallback: записываем как новый приём
                merge_reply = await _submit_food_entry(pending["config"], pending["new_values"])
            else:
                now_merged = datetime.now(MSK)
                _last_food_diary_at = now_merged
                _last_food_entry_at = now_merged
            pre_replies.append(merge_reply)
            pending_handled_fully = True
        elif _is_negative(user_message):
            # Явный отказ — отправляем отложенную запись как новый приём
            pending = _pending_merge
            _pending_merge = None
            try:
                pre_replies.append(await _submit_food_entry(pending["config"], pending["new_values"]))
            except Exception as e:
                logger.exception("Error submitting deferred food entry")
                pre_replies.append(f"Произошла ошибка: {e}")
            pending_handled_fully = True
        else:
            # Не да и не нет — считаем, что пользователь начал новую тему;
            # отправляем отложенную запись и продолжаем обрабатывать новое сообщение
            pending = _pending_merge
            _pending_merge = None
            try:
                pre_replies.append(await _submit_food_entry(pending["config"], pending["new_values"]))
            except Exception as e:
                logger.exception("Error submitting deferred food entry")
                pre_replies.append(f"Произошла ошибка: {e}")

    # Если ответ на pending уже полностью обработан — отправляем pre_replies и выходим
    if pending_handled_fully:
        _chat_history.append({"role": "user", "content": user_message})
        for pr in pre_replies:
            _chat_history.append({"role": "assistant", "content": pr})
        if len(_chat_history) > _MAX_CHAT_HISTORY * 2:
            _chat_history[:] = _chat_history[-_MAX_CHAT_HISTORY * 2:]
        for pr in pre_replies:
            await _reply_md(update.message, pr)
        return

    # Сначала отправляем pre_replies, потом начинаем обработку нового сообщения
    for pr in pre_replies:
        await _reply_md(update.message, pr)

    processing_msg = await update.message.reply_text("Обрабатываю...")

    try:
        parsed = select_form_and_parse(user_message, FORM_CONFIGS, _chat_history)
        action = parsed[0]

        if action == "edit":
            _, selected_config, entry_offset, field_values = parsed
            logger.info("Editing form: %s, offset: %d", selected_config["form_name"], entry_offset)
            reply = _handle_edit(selected_config, entry_offset, field_values)
        else:
            _, selected_config, field_values = parsed
            logger.info("Selected form: %s", selected_config["form_name"])

            if selected_config["form_id"] == _FOOD_FORM_ID:
                # Проверяем, не пора ли предложить объединение с прошлой записью.
                # Напитки всегда идут отдельной записью — не предлагаем объединять.
                now = datetime.now(MSK)
                if (
                    not _is_drink_entry(selected_config, field_values)
                    and _last_food_entry_at is not None
                    and (now - _last_food_entry_at).total_seconds() < _MERGE_WINDOW_SEC
                ):
                    elapsed_min = max(int((now - _last_food_entry_at).total_seconds() / 60), 1)
                    # Применяем auto_defaults сейчас, чтобы зафиксировать категорию
                    # на момент первого сообщения
                    _apply_auto_defaults(selected_config, field_values)
                    _pending_merge = {
                        "config": selected_config,
                        "new_values": field_values,
                        "created_at": now,
                    }
                    reply = (
                        f"Прошлый приём пищи был ~{elapsed_min} мин назад. "
                        f"Объединить с предыдущей записью? (да / нет)"
                    )
                else:
                    reply = await _submit_food_entry(selected_config, field_values)
            else:
                _apply_auto_defaults(selected_config, field_values)
                result = submit_form(selected_config, field_values)
                reply = f"Форма: {selected_config['form_name']}\n\n" + format_result(result)
    except Exception as e:
        logger.exception("Error processing message")
        reply = f"Произошла ошибка: {e}"

    try:
        await processing_msg.delete()
    except Exception:
        logger.exception("Failed to delete 'Обрабатываю...' message")

    _chat_history.append({"role": "user", "content": user_message})
    _chat_history.append({"role": "assistant", "content": reply})
    if len(_chat_history) > _MAX_CHAT_HISTORY * 2:
        _chat_history[:] = _chat_history[-_MAX_CHAT_HISTORY * 2:]

    await _reply_md(update.message, reply)


def create_app():
    token = os.getenv("TG_BOT_TOKEN")
    app = ApplicationBuilder().token(token).post_init(_post_init).build()
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    return app
import asyncio
import json
import logging
import os
from datetime import datetime, timezone, timedelta

from telegram import Update
from telegram.ext import ApplicationBuilder, ContextTypes, MessageHandler, filters

from tg_form_filler.form_filler import format_result, submit_form
from tg_form_filler.llm_handler import (
    generate_spending_report,
    select_form_and_parse,
    analyze_recent_meals,
    generate_daily_nutrition_report,
    generate_weekly_nutrition_report,
    generate_monthly_nutrition_report,
)
import tg_form_filler.sheets_reader as sheets_reader
import tg_form_filler.stats as stats

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

_last_food_diary_at: datetime = datetime.now(MSK)
_last_reminder_sent_at: datetime | None = None
_MAX_COMMENT_HISTORY = 10
_meal_comment_history: list[str] = []


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


def _get_field_value(config: dict, field_name: str, field_values: dict) -> str:
    for field in config["fields"]:
        if field["name"] == field_name:
            return field_values.get(field["entry_id"], "")
    return ""


async def _daily_spending_report_loop(app) -> None:
    while True:
        now = datetime.now(MSK)
        next_report = now.replace(hour=9, minute=0, second=0, microsecond=0)
        if now >= next_report:
            next_report += timedelta(days=1)
        wait_sec = (next_report - now).total_seconds()
        logger.info("Daily spending report scheduled in %.0f s", wait_sec)
        await asyncio.sleep(wait_sec)

        entries = stats.get_yesterday_entries()
        stats.cleanup_old_entries()
        report = generate_spending_report(entries)
        logger.info("Sending daily spending report (%d entries)", len(entries))
        await app.bot.send_message(chat_id=TG_ADMIN_CHAT_ID, text=report)


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
            await app.bot.send_message(chat_id=TG_ADMIN_CHAT_ID, text=report)
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
            await app.bot.send_message(chat_id=TG_ADMIN_CHAT_ID, text=report)
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
            await app.bot.send_message(chat_id=TG_ADMIN_CHAT_ID, text=report)
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


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id

    if user_id != TG_ADMIN_CHAT_ID:
        logger.warning("Blocked message from user %s", user_id)
        return

    user_message = update.message.text
    logger.info("Message from %s: %s", user_id, user_message)

    await update.message.reply_text("Обрабатываю...")

    try:
        selected_config, field_values = select_form_and_parse(user_message, FORM_CONFIGS)
        logger.info("Selected form: %s", selected_config["form_name"])
        result = submit_form(selected_config, field_values)
        reply = f"Форма: {selected_config['form_name']}\n\n" + format_result(result)
        if selected_config["form_id"] == _FOOD_FORM_ID:
            global _last_food_diary_at
            _last_food_diary_at = datetime.now(MSK)
            if result["success"]:
                try:
                    recent = sheets_reader.get_last_entries(n=10)
                    if recent:
                        analysis = analyze_recent_meals(recent, _meal_comment_history)
                        if analysis:
                            reply += f"\n\n{analysis}"
                            _meal_comment_history.append(analysis)
                            if len(_meal_comment_history) > _MAX_COMMENT_HISTORY:
                                _meal_comment_history.pop(0)
                except Exception:
                    logger.exception("Error analyzing recent meals")
        if selected_config["form_id"] == _SPENDING_FORM_ID and result["success"]:
            stats.add_entry(
                category=_get_field_value(selected_config, "Категория", field_values),
                item=_get_field_value(selected_config, "Товар", field_values),
                price_str=_get_field_value(selected_config, "Цена", field_values),
            )
    except Exception as e:
        logger.exception("Error processing message")
        reply = f"Произошла ошибка: {e}"

    await update.message.reply_text(reply)


def create_app():
    token = os.getenv("TG_BOT_TOKEN")
    app = ApplicationBuilder().token(token).post_init(_post_init).build()
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    return app
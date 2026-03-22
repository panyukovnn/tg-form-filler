import json
import logging
import os

from telegram import Update
from telegram.ext import ApplicationBuilder, ContextTypes, MessageHandler, filters

from form_filler import format_result, submit_form
from llm_handler import select_form_and_parse

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)

TG_ADMIN_CHAT_ID = int(os.getenv("TG_ADMIN_CHAT_ID", "0"))

FORM_CONFIGS = []
for config_file in ("spending_diary_form_config.json", "food_diary_form_config.json"):
    if os.path.exists(config_file):
        with open(config_file, encoding="utf-8") as f:
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
    except Exception as e:
        logger.exception("Error processing message")
        reply = f"Произошла ошибка: {e}"

    await update.message.reply_text(reply)


def create_app():
    token = os.getenv("TG_BOT_TOKEN")
    app = ApplicationBuilder().token(token).build()
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    return app
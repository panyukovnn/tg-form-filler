import json
import logging
import os

from telegram import Update
from telegram.ext import ApplicationBuilder, ContextTypes, MessageHandler, filters

from form_filler import format_result, submit_form
from llm_handler import parse_message_to_form_data

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)

TG_ADMIN_CHAT_ID = int(os.getenv("TG_ADMIN_CHAT_ID", "0"))

with open("form_config.json", encoding="utf-8") as f:
    FORM_CONFIG = json.load(f)


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id

    if user_id != TG_ADMIN_CHAT_ID:
        logger.warning("Blocked message from user %s", user_id)
        return

    user_message = update.message.text
    logger.info("Message from %s: %s", user_id, user_message)

    await update.message.reply_text("Обрабатываю...")

    try:
        field_values = parse_message_to_form_data(user_message, FORM_CONFIG)
        result = submit_form(FORM_CONFIG, field_values)
        reply = format_result(result)
    except Exception as e:
        logger.exception("Error processing message")
        reply = f"Произошла ошибка: {e}"

    await update.message.reply_text(reply)


def create_app():
    token = os.getenv("TG_BOT_TOKEN")
    app = ApplicationBuilder().token(token).build()
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    return app

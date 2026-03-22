import json
import logging
import os
from datetime import datetime, timezone, timedelta
from openai import OpenAI

logger = logging.getLogger(__name__)

client = OpenAI(
    api_key=os.getenv("DEEPSEEK_API_KEY"),
    base_url="https://api.deepseek.com",
)


def parse_message_to_form_data(user_message: str, form_config: dict) -> dict:
    """Use LLM to extract form field values from a user message (single form)."""
    fields_description = "\n".join(
        f'- "{f["name"]}" ({f["entry_id"]}): {f["description"]}'
        for f in form_config["fields"]
    )

    prompt = f"""Ты помощник, который заполняет Google форму на основе сообщения пользователя.

Форма: {form_config["form_name"]}
Поля формы:
{fields_description}

Сообщение пользователя:
{user_message}

Верни JSON объект, где ключи — это entry_id полей, а значения — что нужно вписать в каждое поле.
Если информации для какого-то поля нет в сообщении, используй пустую строку "".
Верни ТОЛЬКО валидный JSON без пояснений."""

    response = client.chat.completions.create(
        model="deepseek-chat",
        messages=[{"role": "user", "content": prompt}],
        response_format={"type": "json_object"},
    )

    return json.loads(response.choices[0].message.content)


def _get_meal_category(hour: int) -> str:
    if 3 <= hour < 6:
        return "Ранний завтрак"
    elif 6 <= hour < 10:
        return "Завтрак"
    elif 10 <= hour < 12:
        return "Ланч"
    elif 12 <= hour < 15:
        return "Обед"
    elif 15 <= hour < 18:
        return "Полдник"
    elif 18 <= hour < 22:
        return "Ужин"
    else:
        return "Поздний ужин"


def select_form_and_parse(user_message: str, form_configs: list) -> tuple:
    """Use LLM tools to select the right form and extract field values."""
    tools = []
    for config in form_configs:
        properties = {}
        required_fields = []
        for field in config["fields"]:
            prop = {
                "type": "string",
                "description": field["description"],
            }
            if field.get("options"):
                prop["enum"] = field["options"]
            properties[field["entry_id"]] = prop
            if field.get("required"):
                required_fields.append(field["entry_id"])

        tools.append({
            "type": "function",
            "function": {
                "name": config["form_id"],
                "description": config["form_description"],
                "parameters": {
                    "type": "object",
                    "properties": properties,
                    "required": required_fields,
                },
            },
        })

    now_msk = datetime.now(timezone(timedelta(hours=3)))
    msk_time = now_msk.strftime("%H:%M")
    meal_category = _get_meal_category(now_msk.hour)
    system_message = f"Текущее время в Москве: {msk_time}. Если категория приёма пищи не указана явно, используй: {meal_category}."

    logger.info(
        "LLM request — message: %r, system: %r, tools: %s",
        user_message,
        system_message,
        json.dumps(tools, ensure_ascii=False),
    )

    response = client.chat.completions.create(
        model="deepseek-chat",
        messages=[
            {"role": "system", "content": system_message},
            {"role": "user", "content": user_message},
        ],
        tools=tools,
        tool_choice="required",
    )

    tool_call = response.choices[0].message.tool_calls[0]
    form_id = tool_call.function.name
    field_values = json.loads(tool_call.function.arguments)

    logger.info("LLM selected form: %r, field values: %s", form_id, json.dumps(field_values, ensure_ascii=False))

    selected_config = next(c for c in form_configs if c["form_id"] == form_id)
    return selected_config, field_values
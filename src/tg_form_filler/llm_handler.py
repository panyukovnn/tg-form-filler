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
    now_msk = datetime.now(timezone(timedelta(hours=3)))

    tools = []
    for config in form_configs:
        properties = {}
        required_fields = []
        for field in config["fields"]:
            description = field["description"]
            if field.get("auto_default") == "meal_category":
                meal_category = _get_meal_category(now_msk.hour)
                description += f" По умолчанию: {meal_category}."
            prop = {
                "type": "string",
                "description": description,
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

    system_message = "Заполняй форму на основе сообщения пользователя."

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
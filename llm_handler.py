import json
import logging
import os
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


def select_form_and_parse(user_message: str, form_configs: list) -> tuple:
    """Use LLM tools to select the right form and extract field values."""
    tools = []
    for config in form_configs:
        properties = {}
        for field in config["fields"]:
            prop = {
                "type": "string",
                "description": field["description"],
            }
            if field.get("options"):
                prop["enum"] = field["options"]
            properties[field["entry_id"]] = prop

        tools.append({
            "type": "function",
            "function": {
                "name": config["form_id"],
                "description": config["form_description"],
                "parameters": {
                    "type": "object",
                    "properties": properties,
                    "required": [],
                },
            },
        })

    logger.info(
        "LLM request — message: %r, tools: %s",
        user_message,
        json.dumps(tools, ensure_ascii=False),
    )

    response = client.chat.completions.create(
        model="deepseek-chat",
        messages=[{"role": "user", "content": user_message}],
        tools=tools,
        tool_choice="required",
    )

    tool_call = response.choices[0].message.tool_calls[0]
    form_id = tool_call.function.name
    field_values = json.loads(tool_call.function.arguments)

    logger.info("LLM selected form: %r, field values: %s", form_id, json.dumps(field_values, ensure_ascii=False))

    selected_config = next(c for c in form_configs if c["form_id"] == form_id)
    return selected_config, field_values
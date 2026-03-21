import json
import os
from openai import OpenAI

client = OpenAI(
    api_key=os.getenv("DEEPSEEK_API_KEY"),
    base_url="https://api.deepseek.com",
)


def parse_message_to_form_data(user_message: str, form_config: dict) -> dict:
    """Use OpenAI to extract form field values from a user message."""
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

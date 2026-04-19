import json
import logging
import os
from datetime import datetime, timezone, timedelta
from pathlib import Path
from openai import OpenAI

logger = logging.getLogger(__name__)

client = OpenAI(
    api_key=os.getenv("DEEPSEEK_API_KEY"),
    base_url="https://api.deepseek.com",
)

_RULES_DIR = os.getenv("NUTRITION_RULES_DIR", "rules")
_nutrition_rules: str | None = None


def _load_nutrition_rules() -> str:
    global _nutrition_rules
    if _nutrition_rules is None:
        rules_path = Path(_RULES_DIR) / "nutrition_rules.md"
        if rules_path.exists():
            _nutrition_rules = rules_path.read_text(encoding="utf-8")
        else:
            logger.warning("Nutrition rules file not found at %s", rules_path)
            _nutrition_rules = ""
    return _nutrition_rules

def get_meal_category(hour: int) -> str:
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


def compute_auto_default(auto_kind: str, now: datetime) -> str | None:
    if auto_kind == "meal_category":
        return get_meal_category(now.hour)
    return None


def select_form_and_parse(user_message: str, form_configs: list, chat_history: list[dict] | None = None) -> tuple:
    """Use LLM tools to select the right form and extract field values.

    Returns:
        ("submit", config, field_values) — для новой записи
        ("edit", config, entry_offset, field_values) — для редактирования
    """
    now_msk = datetime.now(timezone(timedelta(hours=3)))

    tools = []
    for config in form_configs:
        properties = {}
        required_fields = []
        for field in config["fields"]:
            description = field["description"]
            auto_kind = field.get("auto_default")
            if auto_kind:
                default_value = compute_auto_default(auto_kind, now_msk)
                if default_value is not None:
                    description += (
                        f" Если пользователь НЕ указал значение этого поля явно, "
                        f"НЕ заполняй его — приложение автоматически подставит "
                        f"значение по умолчанию: '{default_value}'. "
                        f"Указывай это поле ТОЛЬКО если пользователь явно назвал другое значение."
                    )
            prop = {
                "type": "string",
                "description": description,
            }
            if field.get("options"):
                prop["enum"] = field["options"]
            properties[field["entry_id"]] = prop
            # Поля с auto_default не required — если LLM их не вернёт,
            # приложение подставит значение по умолчанию.
            if field.get("required") and not auto_kind:
                required_fields.append(field["entry_id"])

        # Tool для новой записи
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

        # Tool для редактирования существующей записи
        edit_properties = {
            "entry_offset": {
                "type": "integer",
                "description": "Какую запись редактировать: 1 = последняя, 2 = предпоследняя, и т.д.",
            },
        }
        for field in config["fields"]:
            # При редактировании auto_default поля тоже доступны (можно явно изменить)
            prop = {
                "type": "string",
                "description": field["description"],
            }
            if field.get("options"):
                prop["enum"] = field["options"]
            edit_properties[field["entry_id"]] = prop

        tools.append({
            "type": "function",
            "function": {
                "name": f"edit_{config['form_id']}",
                "description": f"Редактировать существующую запись в форме '{config['form_name']}'. "
                               f"Указывай только те поля, которые нужно изменить.",
                "parameters": {
                    "type": "object",
                    "properties": edit_properties,
                    "required": ["entry_offset"],
                },
            },
        })

    system_message = (
        "Ты помощник для заполнения форм. "
        "Если пользователь упоминает продукт, блюдо или расход — это ВСЕГДА новая запись, используй основную форму. "
        "Используй edit-форму ТОЛЬКО если пользователь ЯВНО просит исправить, изменить или отредактировать "
        "уже записанные данные (например: 'исправь', 'поменяй', 'ошибся', 'измени'). "
        "Редактировать можно только последние 3 записи (entry_offset от 1 до 3)."
    )

    logger.info(
        "LLM request — message: %r, system: %r, tools: %s",
        user_message,
        system_message,
        json.dumps(tools, ensure_ascii=False),
    )

    messages = [{"role": "system", "content": system_message}]
    if chat_history:
        messages.extend(chat_history)
    messages.append({"role": "user", "content": user_message})

    response = client.chat.completions.create(
        model="deepseek-chat",
        messages=messages,
        tools=tools,
        tool_choice="required",
    )

    tool_call = response.choices[0].message.tool_calls[0]
    form_id = tool_call.function.name
    field_values = json.loads(tool_call.function.arguments)

    logger.info("LLM selected form: %r, field values: %s", form_id, json.dumps(field_values, ensure_ascii=False))

    if form_id.startswith("edit_"):
        original_form_id = form_id[len("edit_"):]
        selected_config = next(c for c in form_configs if c["form_id"] == original_form_id)
        entry_offset = field_values.pop("entry_offset", 1)
        return "edit", selected_config, entry_offset, field_values
    else:
        selected_config = next(c for c in form_configs if c["form_id"] == form_id)
        return "submit", selected_config, field_values


def generate_spending_report(entries: list[dict]) -> str:
    """Generate a brief daily spending report using LLM.

    Each entry is a dict with keys: category, item, price (float).
    """
    if not entries:
        return "Вчера расходов не зафиксировано."

    total = sum(e["price"] for e in entries)
    entries_text = "\n".join(
        f"- {e['category']}: {e['item']} — {e['price']:.0f} ₽"
        for e in entries
    )

    prompt = (
        f"Составь краткий дружелюбный отчёт о расходах за вчерашний день.\n\n"
        f"Итого потрачено: {total:.0f} ₽\n"
        f"Список расходов:\n{entries_text}"
    )

    logger.info("Generating spending report for %d entries, total %.0f ₽", len(entries), total)

    response = client.chat.completions.create(
        model="deepseek-chat",
        messages=[
            {"role": "system", "content": "Ты помощник по личным финансам. Пиши кратко и по делу на русском языке."},
            {"role": "user", "content": prompt},
        ],
    )

    report = response.choices[0].message.content
    return f"💰 Расходы за вчера — итого {total:.0f} ₽\n\n{report}"


def _format_entries_text(entries: list[dict]) -> str:
    lines = []
    for e in entries:
        parts = [f"{k}: {v}" for k, v in e.items() if v]
        lines.append("- " + ", ".join(parts))
    return "\n".join(lines)


_DRINK_TRUE_VALUES = {"да", "true", "1", "yes"}


def is_drink_row(entry: dict) -> bool:
    return (entry.get("Напиток") or "").strip().lower() in _DRINK_TRUE_VALUES


def _filter_out_drinks(entries: list[dict]) -> list[dict]:
    """Отфильтровать записи-напитки (колонка 'Напиток' помечена как истинная)."""
    return [e for e in entries if not is_drink_row(e)]


def analyze_recent_meals(entries: list[dict], previous_comments: list[str] | None = None) -> str:
    """Анализ последних приёмов пищи — короткий отзыв (1-2 предложения).

    Для напитков (поле 'Напиток' == 'Да' в последней записи) возвращает пустую строку —
    напитки не комментируются как приёмы пищи.
    """
    rules = _load_nutrition_rules()
    if not entries:
        return ""

    last_entry = entries[-1]
    if is_drink_row(last_entry):
        logger.info("Skipping nutrition comment — last entry is a drink")
        return ""

    # Исключаем напитки из контекста — они не должны учитываться в анализе еды
    food_entries = [e for e in entries if not is_drink_row(e)]
    if not food_entries:
        return ""

    entries_text = _format_entries_text(food_entries)

    system_prompt = (
        f"{rules}\n\n"
        "Ты получаешь список последних приёмов пищи пользователя в хронологическом порядке. "
        "ПОСЛЕДНИЙ элемент списка — это приём пищи, который пользователь только что добавил. "
        "Дай короткий отзыв ТОЛЬКО на этот последний приём пищи (1-2 предложения), "
        "учитывая контекст предыдущих приёмов за день. "
        "Следуй правилам из инструкции выше — формат ответа на каждый приём пищи.\n\n"
        "ВАЖНО: не повторяй советы и формулировки из своих предыдущих комментариев. "
        "Разнообразь ответы — подмечай разные аспекты питания."
    )

    user_content = f"Последние приёмы пищи (последний — только что добавленный):\n{entries_text}"
    if previous_comments:
        comments_text = "\n".join(f"- {c}" for c in previous_comments)
        user_content += f"\n\nТвои предыдущие комментарии (НЕ повторяй их):\n{comments_text}"

    response = client.chat.completions.create(
        model="deepseek-chat",
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_content},
        ],
    )

    return response.choices[0].message.content


def generate_daily_nutrition_report(entries: list[dict]) -> str:
    """Дневной отчёт о питании."""
    rules = _load_nutrition_rules()
    entries = _filter_out_drinks(entries)
    if not entries:
        return "За вчера записей о питании не найдено."

    entries_text = _format_entries_text(entries)

    system_prompt = (
        f"{rules}\n\n"
        "Ты получаешь все приёмы пищи пользователя за день в хронологическом порядке "
        "(от ранних к поздним). Составь дневной отчёт строго по формату из правил: "
        "✅ Что было хорошо / ⚠️ Что стоит улучшить / 💡 Рекомендации на завтра. "
        "Будь конкретен — ссылайся на конкретные приёмы пищи и продукты.\n\n"
        "Важно: если «Поздний ужин» стоит в списке раньше «Завтрака», значит он "
        "состоялся после полуночи и относится к предыдущему дню — исключи такую запись "
        "из анализа. «Поздний ужин», стоящий после остальных приёмов пищи — нормальный, "
        "учитывай его."
    )

    response = client.chat.completions.create(
        model="deepseek-chat",
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": f"Приёмы пищи за день:\n{entries_text}"},
        ],
    )

    return f"📊 Отчёт о питании за день\n\n{response.choices[0].message.content}"


def generate_weekly_nutrition_report(entries: list[dict]) -> str:
    """Недельный отчёт о питании."""
    rules = _load_nutrition_rules()
    entries = _filter_out_drinks(entries)
    if not entries:
        return "За прошедшую неделю записей о питании не найдено."

    entries_text = _format_entries_text(entries)

    system_prompt = (
        f"{rules}\n\n"
        "Ты получаешь все приёмы пищи пользователя за неделю в хронологическом порядке "
        "(от ранних к поздним). "
        "Составь недельный отчёт о питании:\n"
        "- Общие тренды и паттерны за неделю\n"
        "- Повторяющиеся проблемы (если есть)\n"
        "- Прогресс и положительные изменения\n"
        "- 3-5 конкретных рекомендаций на следующую неделю\n"
        "Опирайся на правила оценки из инструкции. "
        "Тон: дружелюбный, конкретный, мотивирующий.\n\n"
        "Важно: если в рамках одного дня «Поздний ужин» стоит в списке раньше "
        "«Завтрака» — он состоялся после полуночи и относится к предыдущему дню; "
        "исключай такие записи из анализа. «Поздний ужин» после других приёмов пищи "
        "того же дня — нормальный."
    )

    response = client.chat.completions.create(
        model="deepseek-chat",
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": f"Приёмы пищи за неделю:\n{entries_text}"},
        ],
    )

    return f"📋 Отчёт о питании за неделю\n\n{response.choices[0].message.content}"


def generate_monthly_nutrition_report(entries: list[dict]) -> str:
    """Месячный отчёт о питании."""
    rules = _load_nutrition_rules()
    entries = _filter_out_drinks(entries)
    if not entries:
        return "За прошедший месяц записей о питании не найдено."

    entries_text = _format_entries_text(entries)

    system_prompt = (
        f"{rules}\n\n"
        "Ты получаешь все приёмы пищи пользователя за месяц в хронологическом порядке "
        "(от ранних к поздним). "
        "Составь месячный отчёт о питании:\n"
        "- Долгосрочные паттерны и привычки\n"
        "- Сравнение начала и конца месяца (есть ли прогресс)\n"
        "- Главные достижения\n"
        "- Главные проблемы, которые повторяются\n"
        "- 3-5 стратегических рекомендаций на следующий месяц\n"
        "Опирайся на правила оценки из инструкции. "
        "Тон: поддерживающий, аналитический, мотивирующий.\n\n"
        "Важно: если в рамках одного дня «Поздний ужин» стоит в списке раньше "
        "«Завтрака» — он состоялся после полуночи и относится к предыдущему дню; "
        "исключай такие записи из анализа. «Поздний ужин» после других приёмов пищи "
        "того же дня — нормальный."
    )

    response = client.chat.completions.create(
        model="deepseek-chat",
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": f"Приёмы пищи за месяц:\n{entries_text}"},
        ],
    )

    return f"📈 Отчёт о питании за месяц\n\n{response.choices[0].message.content}"
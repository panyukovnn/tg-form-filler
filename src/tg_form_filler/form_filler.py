import logging

import requests

logger = logging.getLogger(__name__)


def submit_form(form_config: dict, field_values: dict) -> dict:
    """Submit Google Form with given field values."""
    url = form_config["form_url"]

    sheet_only_ids = {f["entry_id"] for f in form_config["fields"] if f.get("sheet_only")}
    payload = {k: v for k, v in field_values.items() if v and k not in sheet_only_ids}

    logger.info("Submitting form %r to %s with payload: %s", form_config["form_name"], url, payload)

    response = requests.post(url, data=payload, timeout=10)

    logger.info("Form response status: %s", response.status_code)
    if not response.ok:
        logger.error("Form error response body: %s", response.text)

    filled = {
        f["name"]: field_values.get(f["entry_id"], "")
        for f in form_config["fields"]
    }

    return {
        "success": response.status_code in (200, 302),
        "status_code": response.status_code,
        "filled_fields": filled,
    }


def format_result(result: dict) -> str:
    """Format submission result as a human-readable message."""
    if result["success"]:
        lines = ["Форма успешно заполнена!\n\nЗаполненные поля:"]
        for name, value in result["filled_fields"].items():
            v = value if value else "—"
            lines.append(f"  • {name}: {v}")
        return "\n".join(lines)
    else:
        return f"Ошибка при отправке формы (статус {result['status_code']}). Попробуй ещё раз."

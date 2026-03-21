import requests


def submit_form(form_config: dict, field_values: dict) -> dict:
    """Submit Google Form with given field values."""
    url = form_config["form_url"]

    payload = {k: v for k, v in field_values.items() if v}

    response = requests.post(url, data=payload, timeout=10)

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

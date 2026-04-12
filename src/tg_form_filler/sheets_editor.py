import logging
import os
import json

from google.oauth2 import service_account
from googleapiclient.discovery import build

logger = logging.getLogger(__name__)

_SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]

_service = None


def _get_service():
    global _service
    if _service is not None:
        return _service

    sa_path = os.getenv("GOOGLE_SERVICE_ACCOUNT_JSON", "")
    if not sa_path:
        raise RuntimeError("GOOGLE_SERVICE_ACCOUNT_JSON env variable is not set")

    creds = service_account.Credentials.from_service_account_file(sa_path, scopes=_SCOPES)
    _service = build("sheets", "v4", credentials=creds, cache_discovery=False)
    return _service


def get_last_rows(spreadsheet_id: str, n: int = 5, sheet_name: str = "",
                   newest_first: bool = False) -> list[dict]:
    """Получить последние N записей из таблицы.

    Args:
        newest_first: если True, новые записи находятся вверху листа (сразу после заголовка).

    Возвращает список dict с ключами:
      - row_number: номер строки в таблице (1-based)
      - headers: список заголовков
      - values: список значений
    Порядок: от старых к новым (последний элемент — самая свежая запись).
    """
    service = _get_service()
    if newest_first and sheet_name:
        # Читаем только заголовок + первые N строк данных
        range_str = f"'{sheet_name}'!A1:{_col_letter(25)}{n + 1}"
    else:
        range_str = f"'{sheet_name}'!A:Z" if sheet_name else "A:Z"

    result = service.spreadsheets().values().get(
        spreadsheetId=spreadsheet_id,
        range=range_str,
    ).execute()

    all_rows = result.get("values", [])
    if len(all_rows) < 2:
        return []

    headers = all_rows[0]

    if newest_first:
        selected = all_rows[1:n + 1]
        entries = []
        for i, row in enumerate(selected):
            row_number = i + 2  # 1-based, skip header
            padded = row + [""] * (len(headers) - len(row))
            entries.append({
                "row_number": row_number,
                "headers": headers,
                "values": padded[:len(headers)],
            })
        entries.reverse()  # от старых к новым
        return entries
    else:
        data_rows = all_rows[1:]
        last_rows = data_rows[-n:]
        entries = []
        for i, row in enumerate(last_rows):
            row_number = len(all_rows) - len(last_rows) + i + 1
            padded = row + [""] * (len(headers) - len(row))
            entries.append({
                "row_number": row_number,
                "headers": headers,
                "values": padded[:len(headers)],
            })
        return entries


def get_all_data_rows(spreadsheet_id: str, sheet_name: str = "") -> list[dict]:
    """Получить все строки таблицы как список dict {header: value}.

    Заголовки берутся из первой строки. Порядок возвращаемых записей — как в таблице.
    """
    service = _get_service()
    range_str = f"'{sheet_name}'!A:Z" if sheet_name else "A:Z"
    result = service.spreadsheets().values().get(
        spreadsheetId=spreadsheet_id,
        range=range_str,
    ).execute()

    all_rows = result.get("values", [])
    if len(all_rows) < 2:
        return []

    headers = all_rows[0]
    entries = []
    for row in all_rows[1:]:
        padded = row + [""] * (len(headers) - len(row))
        entries.append(dict(zip(headers, padded[:len(headers)])))
    return entries


def _col_letter(idx: int) -> str:
    return chr(ord("A") + idx)


def update_cells(spreadsheet_id: str, row_number: int, updates: dict[int, str], sheet_name: str = "") -> None:
    """Обновить конкретные ячейки в строке.

    Args:
        spreadsheet_id: ID таблицы
        row_number: номер строки (1-based)
        updates: {col_index (0-based) -> new_value}
        sheet_name: имя листа (если пусто — первый лист)
    """
    service = _get_service()
    prefix = f"'{sheet_name}'!" if sheet_name else ""
    data = []
    for col_idx, value in updates.items():
        col_letter = chr(ord("A") + col_idx)
        cell_range = f"{prefix}{col_letter}{row_number}"
        data.append({
            "range": cell_range,
            "values": [[value]],
        })

    # USER_ENTERED — чтобы TRUE/FALSE попадали в чекбокс-колонки как булевы,
    # а не как текст с ведущим апострофом.
    body = {"valueInputOption": "USER_ENTERED", "data": data}
    service.spreadsheets().values().batchUpdate(
        spreadsheetId=spreadsheet_id,
        body=body,
    ).execute()

    logger.info("Updated %d cell(s) in row %d of spreadsheet %s", len(updates), row_number, spreadsheet_id)

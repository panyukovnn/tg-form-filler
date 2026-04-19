import csv
import io
import logging
import os
from datetime import datetime, timezone, timedelta
from urllib.parse import quote

import requests

logger = logging.getLogger(__name__)

MSK = timezone(timedelta(hours=3))

SPREADSHEET_ID = os.getenv("FOOD_DIARY_SPREADSHEET_ID", "")

_GVIZ_BASE = "https://docs.google.com/spreadsheets/d/{sid}/gviz/tq?tqx=out:csv&tq={query}"


def _query(tq: str) -> list[dict]:
    """Выполнить Google Visualization Query и вернуть список словарей."""
    if not SPREADSHEET_ID:
        logger.warning("FOOD_DIARY_SPREADSHEET_ID not set, skipping sheets read")
        return []

    url = _GVIZ_BASE.format(sid=SPREADSHEET_ID, query=quote(tq))
    logger.info("Sheets query: %s", tq)

    response = requests.get(url, timeout=15)
    response.raise_for_status()

    reader = csv.DictReader(io.StringIO(response.text))
    return list(reader)


def get_last_entries(n: int = 10) -> list[dict]:
    """Получить последние N записей из дневника питания."""
    rows = _query(f"SELECT * ORDER BY A DESC LIMIT {n}")
    rows.reverse()
    return rows


def get_entries_for_date(date: datetime) -> list[dict]:
    """Получить все записи за конкретный день в хронологическом порядке."""
    date_str = date.strftime("%Y-%m-%d")
    rows = _query(f"SELECT * WHERE toDate(A) = date '{date_str}'")
    rows.reverse()
    return rows


def get_entries_for_range(start_date: datetime, end_date: datetime) -> list[dict]:
    """Получить записи за диапазон дат в хронологическом порядке."""
    start_str = start_date.strftime("%Y-%m-%d")
    end_str = end_date.strftime("%Y-%m-%d")
    rows = _query(
        f"SELECT * WHERE toDate(A) >= date '{start_str}' AND toDate(A) <= date '{end_str}'"
    )
    rows.reverse()
    return rows

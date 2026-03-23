from dataclasses import dataclass
from datetime import datetime, timezone, timedelta
from typing import List

MSK = timezone(timedelta(hours=3))


@dataclass
class SpendingEntry:
    timestamp: datetime
    category: str
    item: str
    price: float


_entries: List[SpendingEntry] = []


def add_entry(category: str, item: str, price_str: str) -> None:
    try:
        price = float(str(price_str).replace(",", ".").replace("\xa0", "").replace(" ", ""))
    except (ValueError, AttributeError):
        price = 0.0
    _entries.append(SpendingEntry(
        timestamp=datetime.now(MSK),
        category=category or "",
        item=item or "",
        price=price,
    ))


def get_yesterday_entries() -> List[SpendingEntry]:
    yesterday = (datetime.now(MSK) - timedelta(days=1)).date()
    return [e for e in _entries if e.timestamp.astimezone(MSK).date() == yesterday]


def cleanup_old_entries() -> None:
    """Keep only last 7 days to prevent unbounded memory growth."""
    cutoff = datetime.now(MSK) - timedelta(days=7)
    global _entries
    _entries = [e for e in _entries if e.timestamp >= cutoff]

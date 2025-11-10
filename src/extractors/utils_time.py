from datetime import datetime, timezone
from dateutil import parser

def to_iso8601(dt_str: str | None) -> str:
    """
    Convert various Twitter-like date strings to ISO 8601.
    If dt_str is None or parsing fails, return current UTC ISO string.
    """
    if dt_str:
        try:
            dt = parser.parse(dt_str)
            if not dt.tzinfo:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt.astimezone(timezone.utc).isoformat()
        except Exception:
            pass
    return now_utc_iso()

def now_utc_iso() -> str:
    return datetime.now(timezone.utc).isoformat()
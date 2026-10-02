"""Retry delays without logging response headers or signed URLs."""
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
import random


def retry_delay(headers, attempt):
    value = headers.get('Retry-After') if hasattr(headers, 'get') else None
    if isinstance(value, str):
        try:
            delay = float(value)
            if delay >= 0 and delay != float('inf'):
                return delay
        except ValueError:
            try:
                when = parsedate_to_datetime(value)
                if when.tzinfo is None:
                    when = when.replace(tzinfo=timezone.utc)
                return max(0, (when - datetime.now(timezone.utc)).total_seconds())
            except (ValueError, TypeError, OverflowError):
                pass
    return min(30, 2 ** attempt) + random.uniform(0, 0.5)

"""Время банка. Все даты в базе — местное время Казахстана, а не часовой пояс сервера.

Хостинги обычно работают в UTC, и без этого клиент видел бы время на 5 часов раньше настоящего.
Часовой пояс меняется переменной CA_TZ.
"""
import os
from datetime import datetime
from zoneinfo import ZoneInfo

TZ = ZoneInfo(os.environ.get("CA_TZ", "Asia/Almaty"))


def now() -> datetime:
    return datetime.now(TZ).replace(tzinfo=None, microsecond=0)

"""تنبيهات الأسعار — يراقب سعر الجرام وينبّه لما يتجاوز حدّاً معيّناً."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal

logger = logging.getLogger(__name__)

CHECK_INTERVAL_SECONDS = 300  # كل ٥ دقائق
MAX_PER_CHAT = 10

ABOVE = "above"
BELOW = "below"


@dataclass
class Alert:
    """تنبيه واحد: نبّهني لما جرام عيار كذا يطلع فوق / ينزل تحت كذا ريال."""

    chat_id: int
    karat: int
    target: Decimal
    direction: str  # ABOVE أو BELOW
    currency: str = "SAR"  # التنبيه يُرسل بعملة صاحبه
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    def triggered_by(self, price: Decimal) -> bool:
        if self.direction == ABOVE:
            return price >= self.target
        return price <= self.target

    @property
    def arrow(self) -> str:
        return "▲" if self.direction == ABOVE else "▼"

    @property
    def label(self) -> str:
        word = "فوق" if self.direction == ABOVE else "تحت"
        return f"عيار {self.karat} {word} {self.target:,} {self.currency}"


def direction_for(target: Decimal, current: Decimal) -> str:
    """اتجاه التنبيه يُستنتج من موقع الهدف مقارنة بالسعر الحالي."""
    return ABOVE if target > current else BELOW


def add(store: dict, alert: Alert) -> bool:
    """يضيف تنبيهاً. يرجّع False إذا وصل الحد الأقصى للمحادثة."""
    alerts = store.setdefault("alerts", [])
    if sum(1 for a in alerts if a.chat_id == alert.chat_id) >= MAX_PER_CHAT:
        return False
    alerts.append(alert)
    return True


def for_chat(store: dict, chat_id: int) -> list[Alert]:
    return [a for a in store.get("alerts", []) if a.chat_id == chat_id]


def remove_all(store: dict, chat_id: int) -> int:
    alerts = store.setdefault("alerts", [])
    remaining = [a for a in alerts if a.chat_id != chat_id]
    removed = len(alerts) - len(remaining)
    store["alerts"] = remaining
    return removed


def remove(store: dict, chat_id: int, index: int) -> Alert | None:
    """يحذف تنبيهاً برقمه المعروض للمستخدم (يبدأ من ١)."""
    mine = for_chat(store, chat_id)
    if not 1 <= index <= len(mine):
        return None
    target = mine[index - 1]
    store["alerts"] = [a for a in store.get("alerts", []) if a is not target]
    return target


def pop_triggered(store: dict, price_of) -> list[tuple[Alert, Decimal]]:
    """يسحب التنبيهات اللي تحققت ويحذفها من المخزن (تنبيه لمرة واحدة).

    `price_of(alert)` يرجّع السعر بعملة التنبيه، أو None إذا تعذّر تسعيره —
    وقتها نبقيه في المخزن بدل ما نحذفه أو نطلقه على سعر خاطئ.
    """
    alerts = store.setdefault("alerts", [])
    fired, remaining = [], []

    for alert in alerts:
        price = price_of(alert)
        if price is not None and alert.triggered_by(price):
            fired.append((alert, price))
        else:
            remaining.append(alert)

    store["alerts"] = remaining
    return fired

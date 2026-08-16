"""فحص حالة السوق والتنبيهات."""

from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

import alerts as alerts_store
import market
from alerts import ABOVE, BELOW, Alert


def utc(year, month, day, hour):
    return datetime(year, month, day, hour, tzinfo=timezone.utc)


# ---------- حالة السوق ----------


@pytest.mark.parametrize(
    "moment, expected_open",
    [
        (utc(2026, 8, 12, 14), True),   # أربعاء ظهراً
        (utc(2026, 8, 14, 20), True),   # جمعة قبل الإغلاق بساعة
        (utc(2026, 8, 14, 21), False),  # جمعة لحظة الإغلاق
        (utc(2026, 8, 15, 12), False),  # سبت
        (utc(2026, 8, 16, 21), False),  # أحد قبل الفتح
        (utc(2026, 8, 16, 22), True),   # أحد لحظة الفتح
        (utc(2026, 8, 17, 3), True),    # اثنين فجراً
    ],
)
def test_market_hours(moment, expected_open):
    assert market.status(moment).is_open is expected_open


def test_closed_gives_next_open():
    saturday = utc(2026, 8, 15, 12)
    state = market.status(saturday)
    assert state.is_open is False
    assert state.reopens_at == utc(2026, 8, 16, 22)


def test_friday_night_reopens_on_sunday():
    state = market.status(utc(2026, 8, 14, 23))
    assert state.reopens_at == utc(2026, 8, 16, 22)


def test_open_has_no_reopen_time():
    assert market.status(utc(2026, 8, 12, 14)).reopens_at is None


def test_timezone_is_normalized():
    """وقت بتوقيت الرياض لازم يُحوَّل لـ UTC قبل الحكم."""
    riyadh_saturday = datetime(2026, 8, 15, 15, tzinfo=timezone(timedelta(hours=3)))
    assert market.status(riyadh_saturday).is_open is False


def test_schedule_is_the_fallback_basis():
    assert market.status(utc(2026, 8, 12, 14)).basis == "الجدول"


# ---------- الحكم من وقت التسعيرة نفسها ----------


def test_fresh_quote_means_open():
    now = utc(2026, 8, 12, 14)
    state = market.status(now, quoted_at=now - timedelta(seconds=20))
    assert state.is_open is True
    assert state.basis == "التسعيرة"
    assert state.reopens_at is None


def test_stale_quote_means_closed():
    """تسعيرة إغلاق الجمعة يوم السبت — السوق يقول عن نفسه إنه واقف."""
    state = market.status(utc(2026, 8, 15, 18), quoted_at=utc(2026, 8, 14, 21))
    assert state.is_open is False
    assert state.basis == "التسعيرة"
    assert state.reopens_at == utc(2026, 8, 16, 22)


def test_quote_overrides_schedule():
    """لو الجدول يقول مفتوح والتسعيرة واقفة من ساعات، نصدّق التسعيرة.

    يحمي من عطلة رسمية أو انقطاع ما يعرفه جدولنا المبرمج.
    """
    now = utc(2026, 8, 12, 14)  # أربعاء ظهراً — الجدول يقول مفتوح
    assert market.status(now).is_open is True
    assert market.status(now, quoted_at=now - timedelta(hours=5)).is_open is False


def test_quote_threshold_boundary():
    now = utc(2026, 8, 12, 14)
    just_fresh = now - market.QUOTE_STALE_AFTER + timedelta(seconds=1)
    just_stale = now - market.QUOTE_STALE_AFTER - timedelta(seconds=1)
    assert market.status(now, quoted_at=just_fresh).is_open is True
    assert market.status(now, quoted_at=just_stale).is_open is False


def test_quote_in_other_timezone_is_normalized():
    now = utc(2026, 8, 12, 14)
    riyadh_quote = (now - timedelta(minutes=1)).astimezone(timezone(timedelta(hours=3)))
    assert market.status(now, quoted_at=riyadh_quote).is_open is True


def test_humanize():
    assert market.humanize(timedelta(days=1, hours=10)) == "1 يوم و10 ساعة"
    assert market.humanize(timedelta(hours=3)) == "3 ساعة"
    assert market.humanize(timedelta(minutes=45)) == "45 دقيقة"
    assert market.humanize(timedelta(seconds=-5)) == "الآن"


# ---------- التنبيهات ----------


def make_alert(chat_id=1, karat=21, target="460", direction=ABOVE):
    return Alert(chat_id=chat_id, karat=karat, target=Decimal(target), direction=direction)


def test_direction_inferred_from_current_price():
    assert alerts_store.direction_for(Decimal("500"), Decimal("460")) == ABOVE
    assert alerts_store.direction_for(Decimal("400"), Decimal("460")) == BELOW


def test_trigger_conditions():
    up = make_alert(direction=ABOVE, target="460")
    assert up.triggered_by(Decimal("460")) is True   # المساواة تحقق الشرط
    assert up.triggered_by(Decimal("461")) is True
    assert up.triggered_by(Decimal("459")) is False

    down = make_alert(direction=BELOW, target="460")
    assert down.triggered_by(Decimal("460")) is True
    assert down.triggered_by(Decimal("459")) is True
    assert down.triggered_by(Decimal("461")) is False


def test_add_respects_limit():
    store = {}
    for _ in range(alerts_store.MAX_PER_CHAT):
        assert alerts_store.add(store, make_alert()) is True
    assert alerts_store.add(store, make_alert()) is False
    # محادثة ثانية ما تتأثر بحد الأولى
    assert alerts_store.add(store, make_alert(chat_id=2)) is True


def test_for_chat_isolates_chats():
    store = {}
    alerts_store.add(store, make_alert(chat_id=1))
    alerts_store.add(store, make_alert(chat_id=2))
    assert len(alerts_store.for_chat(store, 1)) == 1
    assert len(alerts_store.for_chat(store, 2)) == 1


def test_remove_by_index():
    store = {}
    alerts_store.add(store, make_alert(target="400"))
    alerts_store.add(store, make_alert(target="500"))
    removed = alerts_store.remove(store, 1, 2)
    assert removed.target == Decimal("500")
    assert len(alerts_store.for_chat(store, 1)) == 1
    assert alerts_store.remove(store, 1, 99) is None
    assert alerts_store.remove(store, 1, 0) is None


def test_remove_all_only_touches_one_chat():
    store = {}
    alerts_store.add(store, make_alert(chat_id=1))
    alerts_store.add(store, make_alert(chat_id=1))
    alerts_store.add(store, make_alert(chat_id=2))
    assert alerts_store.remove_all(store, 1) == 2
    assert len(alerts_store.for_chat(store, 2)) == 1


def test_pop_triggered_fires_once_and_removes():
    store = {}
    alerts_store.add(store, make_alert(target="460", direction=ABOVE))
    alerts_store.add(store, make_alert(target="900", direction=ABOVE))

    fired = alerts_store.pop_triggered(store, lambda _a: Decimal("470"))
    assert len(fired) == 1
    assert fired[0][0].target == Decimal("460")
    assert fired[0][1] == Decimal("470")

    # اللي تحقق انحذف، واللي ما تحقق باقي
    assert len(alerts_store.for_chat(store, 1)) == 1
    assert alerts_store.pop_triggered(store, lambda _a: Decimal("470")) == []


def test_pop_triggered_uses_each_alerts_own_karat():
    store = {}
    alerts_store.add(store, make_alert(karat=24, target="500", direction=ABOVE))
    alerts_store.add(store, make_alert(karat=18, target="500", direction=ABOVE))

    prices = {24: Decimal("520"), 18: Decimal("390")}
    fired = alerts_store.pop_triggered(store, lambda a: prices[a.karat])

    assert len(fired) == 1
    assert fired[0][0].karat == 24


def test_alert_carries_its_currency():
    """كل تنبيه يحمل عملة صاحبه — قروب إماراتي ما يستقبل سعراً بالريال."""
    aed = make_alert(target="450")
    object.__setattr__(aed, "currency", "AED") if hasattr(aed, "__setattr__") else None
    aed = Alert(chat_id=1, karat=21, target=Decimal("450"), direction=ABOVE, currency="AED")
    assert "AED" in aed.label
    assert make_alert().currency == "SAR"


def test_untriceable_alert_is_kept_not_dropped():
    """لو تعذّر تسعير عملة التنبيه، نؤجّله — لا نحذفه ولا نطلقه بسعر خاطئ."""
    store = {}
    alerts_store.add(store, make_alert(target="460", direction=ABOVE))

    fired = alerts_store.pop_triggered(store, lambda _a: None)

    assert fired == []
    assert len(alerts_store.for_chat(store, 1)) == 1

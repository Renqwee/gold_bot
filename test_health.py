"""فحص مراقبة صحة المصادر وسجل الإغلاقات."""

from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

import pytest

import rates
from health import Health, Monitor


def utc(*args):
    return datetime(*args, tzinfo=timezone.utc)


def monitor(**kwargs) -> Monitor:
    return Monitor(primary="Swissquote", since=utc(2026, 8, 17, 10), **kwargs)


# ---------- تصنيف الحالة ----------


def test_primary_source_is_healthy():
    m = monitor()
    assert m.observe("Swissquote", now=utc(2026, 8, 17, 11)) is None
    assert m.state is Health.OK


def test_fallback_source_is_degraded():
    m = monitor()
    report = m.observe("gold-api", now=utc(2026, 8, 17, 11))
    assert m.state is Health.DEGRADED
    assert "gold-api" in report
    assert "Swissquote" in report


def test_total_failure_is_down():
    m = monitor()
    report = m.observe(None, now=utc(2026, 8, 17, 11))
    assert m.state is Health.DOWN
    assert "🚨" in report


def test_stale_quote_counts_as_down():
    """قيمة موسومة «قديمة» تعني أن كل المصادر فشلت."""
    m = monitor()
    m.observe("Swissquote", stale=True, now=utc(2026, 8, 17, 11))
    assert m.state is Health.DOWN


# ---------- الإبلاغ عند التغيّر فقط ----------


def test_no_repeat_reports_while_state_unchanged():
    m = monitor()
    assert m.observe("gold-api", now=utc(2026, 8, 17, 11)) is not None
    for minute in range(12, 20):
        assert m.observe("gold-api", now=utc(2026, 8, 17, minute)) is None


def test_recovery_is_always_reported():
    """التدهور قد يُكتم منعاً للإزعاج، لكن التعافي خبر لازم يوصل."""
    m = monitor()
    m.observe("gold-api", now=utc(2026, 8, 17, 11))
    report = m.observe("Swissquote", now=utc(2026, 8, 17, 11, 5))
    assert report is not None
    assert "✅" in report
    assert m.state is Health.OK


def test_flapping_is_muted():
    """تذبذب سريع بين حالتين سيئتين ما يفجّر سيل رسائل."""
    m = monitor()
    assert m.observe("gold-api", now=utc(2026, 8, 17, 11)) is not None
    # تدهور -> توقف بعد دقيقتين: مكتوم داخل فترة الهدوء
    assert m.observe(None, now=utc(2026, 8, 17, 11, 2)) is None
    # لكن الحالة تُحدَّث فعلياً
    assert m.state is Health.DOWN


def test_report_after_quiet_period_passes():
    m = monitor(quiet_for=timedelta(minutes=10))
    m.observe("gold-api", now=utc(2026, 8, 17, 11))
    assert m.observe(None, now=utc(2026, 8, 17, 11, 30)) is not None


def test_report_includes_duration():
    m = monitor()
    report = m.observe("gold-api", now=utc(2026, 8, 17, 10, 45))
    assert "45 دقيقة" in report


# ---------- دمج سجل الإغلاقات ----------


def test_own_closes_override_fetched():
    """سجلّنا من نفس مصادر البوت، فهو أوفق لأرقامه من رقم طرف ثالث."""
    fetched = [(date(2026, 8, 14), Decimal("4375.61"))]
    own = {"2026-08-14": "4376.19"}
    merged = dict(rates.merge_closes(own, fetched))
    assert merged[date(2026, 8, 14)] == Decimal("4376.19")


def test_merge_keeps_days_we_never_recorded():
    fetched = [
        (date(2026, 8, 13), Decimal("4358.91")),
        (date(2026, 8, 12), Decimal("4411.45")),
    ]
    merged = dict(rates.merge_closes({"2026-08-14": "4376.19"}, fetched))
    assert len(merged) == 3
    assert merged[date(2026, 8, 12)] == Decimal("4411.45")


def test_merge_sorted_newest_first():
    merged = rates.merge_closes(
        {"2026-08-10": "4300", "2026-08-14": "4376"},
        [(date(2026, 8, 12), Decimal("4411"))],
    )
    assert [day for day, _ in merged] == [
        date(2026, 8, 14), date(2026, 8, 12), date(2026, 8, 10)
    ]


def test_merge_survives_corrupt_entries():
    """سطر تالف في السجل ما يسقط الميزة كلها."""
    merged = dict(
        rates.merge_closes(
            {"لا-تاريخ": "4300", "2026-08-14": "ليس رقماً", "2026-08-13": "4358.91"},
            [],
        )
    )
    assert merged == {date(2026, 8, 13): Decimal("4358.91")}


def test_merge_handles_empty_inputs():
    assert rates.merge_closes(None, []) == []
    assert rates.merge_closes({}, []) == []


@pytest.mark.asyncio
async def test_change_pct_prefers_own_record(monkeypatch):
    async def fetched():
        return [
            (date(2026, 8, 14), Decimal("4000")),
            (date(2026, 8, 13), Decimal("3000")),
        ]

    monkeypatch.setattr(rates, "daily_closes", fetched)

    # بسجلّنا: مرجع الويكند هو إغلاق ١٣ المسجّل عندنا (٣٥٠٠)
    pct = await rates.change_pct(
        Decimal("4000"), market_open=False, own_closes={"2026-08-13": "3500"}
    )
    assert pct is not None
    assert abs(pct - Decimal("14.2857")) < Decimal("0.001")

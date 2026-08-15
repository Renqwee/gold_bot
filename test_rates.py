"""فحص محلّلات المصادر ومنطق الجلب — بدون إنترنت، ببيانات ثابتة."""

from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

import pytest

import rates
from rates import Quote

SWISSQUOTE = [
    {
        "topo": {"platform": "SwissquoteCapitalMarkets"},
        "spreadProfilePrices": [{"spreadProfile": "premium", "bid": 4375.846, "ask": 4376.524}],
        "ts": 1786741200093,  # 2026-08-14 21:00:00 UTC — إغلاق الجمعة
    }
]


def utc(*args):
    return datetime(*args, tzinfo=timezone.utc)


@pytest.fixture(autouse=True)
def clean():
    rates.clear_cache()
    yield
    rates.clear_cache()


# ---------- المحلّلات ----------


def test_parse_swissquote_uses_midpoint():
    value, quoted_at = rates.parse_swissquote(SWISSQUOTE)
    assert value == Decimal("4376.185")  # منتصف bid/ask بالضبط
    # الطابع يحمل مللي ثانية (…093) فنقارن بالثانية
    assert quoted_at.replace(microsecond=0) == utc(2026, 8, 14, 21, 0, 0)


def test_swissquote_timestamp_is_real_market_time():
    """الطابع لازم يشير لإغلاق الجمعة، مو لوقت السحب."""
    _value, quoted_at = rates.parse_swissquote(SWISSQUOTE)
    assert quoted_at.weekday() == 4  # جمعة
    assert quoted_at.hour == 21


def test_parse_gold_api_reports_no_timestamp():
    """طابع gold-api يقيس وقت سحبه هو، فنرفض ادّعاء الطزاجة."""
    value, quoted_at = rates.parse_gold_api(
        {"price": 4377.600098, "updatedAt": "2026-08-15T18:20:26Z"}
    )
    assert value == Decimal("4377.600098")
    assert quoted_at is None


def test_parse_fxratesapi_inverts_rate():
    value, quoted_at = rates.parse_fxratesapi(
        {"rates": {"XAU": 0.0002285403}, "timestamp": 1786817580}
    )
    assert Decimal("4375") < value < Decimal("4376")
    assert quoted_at is None


def test_parse_fxratesapi_rejects_zero():
    with pytest.raises(ValueError):
        rates.parse_fxratesapi({"rates": {"XAU": 0}})


def test_parse_er_api():
    value, _ = rates.parse_er_api({"result": "success", "rates": {"SAR": 3.75}})
    assert value == Decimal("3.75")


def test_parse_er_api_rejects_failure():
    with pytest.raises(ValueError):
        rates.parse_er_api({"result": "error", "rates": {"SAR": 3.75}})


def test_parse_currency_api():
    value, _ = rates.parse_currency_api({"usd": {"sar": 3.75}})
    assert value == Decimal("3.75")


def test_float_precision_not_inherited():
    assert rates._to_decimal(0.1) == Decimal("0.1")
    assert rates._to_decimal(0.1) != Decimal(0.1)


@pytest.mark.parametrize("bad", [0, -5, 5, 1e9, float("nan")])
def test_gold_range_rejects_nonsense(bad):
    with pytest.raises(ValueError):
        rates.parse_gold_api({"price": bad})


def test_missing_keys_raise():
    for parser, data in (
        (rates.parse_gold_api, {}),
        (rates.parse_swissquote, []),
        (rates.parse_fxratesapi, {"rates": {}}),
        (rates.parse_er_api, {"result": "success", "rates": {}}),
    ):
        with pytest.raises(Exception):
            parser(data)


# ---------- المصادر كلها سوق فوري ----------


def test_no_futures_or_crypto_sources():
    """العقود الآجلة تبعد ١٫٤٪ عن الفوري، والكريبتو يتداول في الويكند — كلاهما مرفوض."""
    urls = " ".join(url for _name, url, _parser in rates.SPOT_SOURCES).lower()
    for banned in ("yahoo", "gc=f", "binance", "paxg", "coingecko"):
        assert banned not in urls, f"مصدر غير فوري: {banned}"


def test_swissquote_is_first():
    """الأول لأنه الوحيد اللي يعطي وقت التسعيرة الحقيقي."""
    assert rates.SPOT_SOURCES[0][0] == "Swissquote"


# ---------- فحص القفزات ----------


def test_jump_guard_rejects_sudden_move():
    rates._last["gold"] = Quote(
        Decimal("4380"), "test", datetime.now(timezone.utc)
    )
    assert rates._plausible("gold", Decimal("4400")) is True   # ‏٠٫٥٪
    assert rates._plausible("gold", Decimal("4500")) is True   # ‏٢٫٧٪
    assert rates._plausible("gold", Decimal("4700")) is False  # ‏٧٪ — مرفوض
    assert rates._plausible("gold", Decimal("3000")) is False  # انهيار وهمي


def test_jump_guard_allows_gap_after_long_silence():
    """بعد نصف ساعة، فجوة الافتتاح مشروعة فما نرفضها."""
    rates._last["gold"] = Quote(
        Decimal("4380"), "test", datetime.now(timezone.utc) - timedelta(hours=2)
    )
    assert rates._plausible("gold", Decimal("4700")) is True


def test_jump_guard_passes_when_no_history():
    assert rates._plausible("gold", Decimal("4380")) is True


# ---------- الجلب بدون تخزين ----------


@pytest.mark.asyncio
async def test_every_call_fetches(monkeypatch):
    """المطلوب: كل رسالة تجيب سعراً جديداً، لا تعيد استخدام قيمة مخزّنة."""
    calls = []

    async def counting(key, sources, timeout):
        calls.append(1)
        return Quote(Decimal("4380"), "test", datetime.now(timezone.utc))

    monkeypatch.setattr(rates, "_fetch", counting)
    await rates.gold_usd_per_ounce()
    await rates.gold_usd_per_ounce()
    await rates.gold_usd_per_ounce()

    assert len(calls) == 3


@pytest.mark.asyncio
async def test_concurrent_calls_share_one_request(monkeypatch):
    """عشر رسائل في نفس اللحظة = طلب واحد، بدون عرض قيمة قديمة."""
    import asyncio

    calls = []

    async def slow(key, sources, timeout):
        calls.append(1)
        await asyncio.sleep(0.05)
        return Quote(Decimal("4380"), "test", datetime.now(timezone.utc))

    monkeypatch.setattr(rates, "_fetch", slow)
    results = await asyncio.gather(*[rates.gold_usd_per_ounce() for _ in range(10)])

    assert len(calls) == 1
    assert all(r.value == Decimal("4380") for r in results)


@pytest.mark.asyncio
async def test_falls_back_to_last_known(monkeypatch):
    rates._last["gold"] = Quote(
        Decimal("4300"), "test", datetime.now(timezone.utc) - timedelta(days=1)
    )

    async def always_fails(key, sources, timeout):
        raise RuntimeError("لا إنترنت")

    monkeypatch.setattr(rates, "_fetch", always_fails)
    quote = await rates.gold_usd_per_ounce()

    assert quote.value == Decimal("4300")
    assert quote.stale is True


@pytest.mark.asyncio
async def test_raises_when_no_history_and_no_network(monkeypatch):
    async def always_fails(key, sources, timeout):
        raise RuntimeError("لا إنترنت")

    monkeypatch.setattr(rates, "_fetch", always_fails)
    with pytest.raises(RuntimeError):
        await rates.gold_usd_per_ounce()


@pytest.mark.asyncio
async def test_fx_is_cached(monkeypatch):
    """سعر الصرف مخزَّن عن قصد — مربوط ويحدَّث يومياً."""
    calls = []

    async def counting(key, sources, timeout):
        calls.append(1)
        return Quote(Decimal("3.75"), "test", datetime.now(timezone.utc))

    monkeypatch.setattr(rates, "_fetch", counting)
    await rates.usd_to_sar()
    await rates.usd_to_sar()

    assert len(calls) == 1


# ---------- الإغلاقات اليومية ----------

CLOSES_PAYLOAD = {
    "rates": {
        "2026-08-13T23:59:00.000Z": {"XAU": 0.000229415},   # ≈ 4358.9
        "2026-08-12T23:59:00.000Z": {"XAU": 0.000226683},   # ≈ 4411.4
        "2026-08-14T23:59:00.000Z": {"XAU": 0.0002285403},  # ≈ 4375.6
    }
}


def test_parse_daily_closes_sorted_newest_first():
    closes = rates.parse_daily_closes(CLOSES_PAYLOAD)
    assert [day for day, _price in closes] == [
        date(2026, 8, 14), date(2026, 8, 13), date(2026, 8, 12)
    ]
    assert Decimal("4375") < closes[0][1] < Decimal("4376")


def test_reference_close_while_market_open():
    """وقت التداول: أحدث إغلاق هو إغلاق أمس — المرجع الصحيح."""
    closes = rates.parse_daily_closes(CLOSES_PAYLOAD)
    assert rates.reference_close(closes, market_open=True) == closes[0][1]


def test_reference_close_while_market_closed():
    """الويكند: أحدث إغلاق هو نفسه السعر المعروض، فنرجع خطوة للي قبله."""
    closes = rates.parse_daily_closes(CLOSES_PAYLOAD)
    assert rates.reference_close(closes, market_open=False) == closes[1][1]


def test_reference_close_handles_empty():
    assert rates.reference_close([], market_open=True) is None
    assert rates.reference_close([(date(2026, 8, 14), Decimal("4375"))], False) is None


@pytest.mark.asyncio
async def test_change_pct_uses_spot_closes(monkeypatch):
    async def fake_closes():
        return rates.parse_daily_closes(CLOSES_PAYLOAD)

    monkeypatch.setattr(rates, "daily_closes", fake_closes)
    pct = await rates.change_pct(Decimal("4375.6"), market_open=False)

    # مقارنة بإغلاق ١٣ أغسطس (≈4358.9) = ارتفاع طفيف
    assert pct is not None
    assert Decimal("0") < pct < Decimal("1")


@pytest.mark.asyncio
async def test_change_pct_none_without_data(monkeypatch):
    async def no_closes():
        return []

    monkeypatch.setattr(rates, "daily_closes", no_closes)
    assert await rates.change_pct(Decimal("4380"), market_open=True) is None


# ---------- Quote ----------


def test_quote_ages():
    quote = Quote(
        value=Decimal("4380"),
        source="test",
        fetched_at=datetime.now(timezone.utc) - timedelta(seconds=30),
        quoted_at=datetime.now(timezone.utc) - timedelta(hours=21),
    )
    assert quote.age > timedelta(seconds=25)
    assert quote.quote_age > timedelta(hours=20)
    assert not quote.stale


def test_quote_age_none_without_timestamp():
    quote = Quote(Decimal("4380"), "test", datetime.now(timezone.utc))
    assert quote.quote_age is None

"""جلب سعر الذهب الفوري وسعر الصرف.

مبادئ هذي الوحدة:

١. **سعر الذهب لا يُخزَّن.** كل طلب يجلب من المصدر. الشيء الوحيد المشترك هو الطلب
   المتزامن: لو وصلت عشر رسائل في نفس اللحظة يطلع طلب واحد ويأخذون نتيجته —
   مشاركة طلب جارٍ، مو عرض قيمة قديمة.

٢. **كل المصادر سوق فوري (spot)** يقفل نهاية الأسبوع. لا عقود آجلة ولا توكنات
   كريبتو تتداول ٢٤/٧ — الفرق بينها وبين الفوري يوصل ١٫٤٪.

٣. **الطوابع الزمنية موثوقة أو معدومة.** Swissquote يعطي وقت التسعيرة الحقيقي؛
   المصادر الأخرى تختم وقت سحبها هي للبيانات، فنرجّع None بدل ما نوهم بطزاجة.

٤. **آخر قيمة معروفة للطوارئ فقط** — إذا فشلت كل المصادر، تُرجَّع موسومة «قديمة».
"""

from __future__ import annotations

import asyncio
import os
import logging
from dataclasses import dataclass, replace
from functools import partial
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from typing import Callable

import httpx

logger = logging.getLogger(__name__)

# مهلة قصيرة لكل مصدر — قِسنا قفزة ٥٫٤ ثانية من أحد المصادر، والانتقال
# للمصدر التالي أسرع من الانتظار
SOURCE_TIMEOUT = 2.5
FX_TIMEOUT = 6.0
USER_AGENT = "GoldBot/2.0"

# أرضية إعادة الجلب: حماية من الإغراق لا تخزين.
# المصدر يحدّث كل بضع دقائق، فثانيتان لا تغيّران الرقم — لكنها تمنع مستخدماً
# واحداً يرسل عشرات الرسائل من إطلاق عشرات الطلبات. صفر = عطّلها تماماً.
MIN_REFETCH_INTERVAL = timedelta(seconds=float(os.environ.get("MIN_REFETCH_SECONDS", "2")))

# سعر الصرف مربوط ويُحدَّث يومياً عند المصدر، فتخزينه لا يؤثر على طزاجة سعر الذهب
FX_TTL = timedelta(hours=6)
CLOSES_TTL = timedelta(hours=1)  # الإغلاقات اليومية رقم يومي، لا يتغيّر خلال الساعة

# حدود منطقية
GOLD_RANGE = (Decimal("100"), Decimal("100000"))  # دولار للأونصة
# وحدات العملة مقابل الدولار: يشمل الدينار الكويتي (٠٫٣٠٨) والجنيه المصري (٥٠)
FX_RANGE = (Decimal("0.1"), Decimal("10000"))

# الذهب لا يقفز ٣٪ خلال نصف ساعة — أي مصدر يقول غير ذلك يُتجاوَز
MAX_JUMP_PCT = Decimal("3")
JUMP_WINDOW = timedelta(minutes=30)


@dataclass(frozen=True)
class Quote:
    """قيمة مجلوبة مع مصدرها ووقتها."""

    value: Decimal
    source: str
    fetched_at: datetime
    quoted_at: datetime | None = None  # وقت التسعيرة الحقيقي، إن وفّره المصدر
    stale: bool = False  # True = فشل الجلب ونرجّع آخر قيمة معروفة

    @property
    def age(self) -> timedelta:
        return datetime.now(timezone.utc) - self.fetched_at

    @property
    def quote_age(self) -> timedelta | None:
        if self.quoted_at is None:
            return None
        return datetime.now(timezone.utc) - self.quoted_at


def _to_decimal(raw: object) -> Decimal:
    """تحويل آمن لـ Decimal — عبر str عشان ما نورّث خطأ float."""
    try:
        return Decimal(str(raw))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise ValueError(f"قيمة غير صالحة: {raw!r}") from exc


def _validate(value: Decimal, bounds: tuple[Decimal, Decimal]) -> Decimal:
    low, high = bounds
    if not value.is_finite() or not (low <= value <= high):
        raise ValueError(f"قيمة خارج المدى المتوقع: {value}")
    return value


# ---------- المحلّلات (دوال نقية، تنختبر بدون إنترنت) ----------

Reading = tuple[Decimal, datetime | None]


def parse_swissquote(data: list) -> Reading:
    """تسعيرة XAU/USD من Swissquote — بنك سويسري، ويعطي وقت التسعيرة الحقيقي."""
    entry = data[0]
    prices = entry["spreadProfilePrices"][0]
    mid = (_to_decimal(prices["bid"]) + _to_decimal(prices["ask"])) / 2
    quoted_at = datetime.fromtimestamp(int(entry["ts"]) / 1000, timezone.utc)
    return _validate(mid, GOLD_RANGE), quoted_at


def parse_gold_api(data: dict) -> Reading:
    """gold-api. طابعه الزمني يقيس متى سحب هو البيانات، مو متى تحرّك السوق."""
    return _validate(_to_decimal(data["price"]), GOLD_RANGE), None


def parse_fxratesapi(data: dict) -> Reading:
    """fxratesapi يعطي كم أونصة في الدولار، فنقلبها. طابعه غير موثوق كذلك."""
    per_usd = _to_decimal(data["rates"]["XAU"])
    if per_usd <= 0:
        raise ValueError("سعر صرف صفري")
    return _validate(1 / per_usd, GOLD_RANGE), None


def parse_er_api(data: dict, code: str = "SAR") -> Reading:
    if data.get("result") != "success":
        raise ValueError("المصدر رجّع حالة فشل")
    rate = data["rates"].get(code)
    if rate is None:
        raise ValueError(f"المصدر ما يعرف العملة {code}")
    return _validate(_to_decimal(rate), FX_RANGE), None


def parse_currency_api(data: dict, code: str = "SAR") -> Reading:
    rate = data["usd"].get(code.lower())
    if rate is None:
        raise ValueError(f"المصدر ما يعرف العملة {code}")
    return _validate(_to_decimal(rate), FX_RANGE), None


# ---------- المصادر بالترتيب ----------

Source = tuple[str, str, Callable[[object], Reading]]

# كلها سوق فوري يقفل نهاية الأسبوع
SPOT_SOURCES: tuple[Source, ...] = (
    (
        "Swissquote",
        "https://forex-data-feed.swissquote.com/public-quotes/bboquotes/instrument/XAU/USD",
        parse_swissquote,
    ),
    ("gold-api", "https://api.gold-api.com/price/XAU", parse_gold_api),
    (
        "fxratesapi",
        "https://api.fxratesapi.com/latest?base=USD&currencies=XAU",
        parse_fxratesapi,
    ),
)

FX_SOURCES: tuple[Source, ...] = (
    ("exchangerate-api", "https://open.er-api.com/v6/latest/USD", parse_er_api),
    (
        "currency-api",
        "https://latest.currency-api.pages.dev/v1/currencies/usd.json",
        parse_currency_api,
    ),
)

DAILY_CLOSES_URL = (
    "https://api.fxratesapi.com/timeseries"
    "?start_date={start}&end_date={end}&base=USD&currencies=XAU"
)


# ---------- الحالة المشتركة ----------

_last: dict[str, Quote] = {}  # آخر قيمة معروفة — للطوارئ وفحص القفزات
_inflight: dict[str, asyncio.Task] = {}  # الطلبات الجارية
_fx_cache: dict[str, Quote] = {}
_closes_cache: tuple[list[tuple[date, Decimal]], datetime] | None = None
_closes_lock: asyncio.Lock | None = None


def _plausible(key: str, value: Decimal) -> bool:
    """يرفض القفزات المفاجئة — تكشف مصدراً بدأ يرجّع بيانات خربانة."""
    previous = _last.get(key)
    if previous is None or previous.age > JUMP_WINDOW:
        return True
    jump = abs(value - previous.value) / previous.value * 100
    if jump > MAX_JUMP_PCT:
        logger.warning("قفزة مرفوضة: %s%% من %s إلى %s", jump, previous.value, value)
        return False
    return True


async def _fetch(key: str, sources: tuple[Source, ...], timeout: float) -> Quote:
    """يجرّب المصادر بالترتيب حتى ينجح واحد."""
    errors = []
    async with httpx.AsyncClient(
        timeout=timeout, headers={"User-Agent": USER_AGENT}
    ) as client:
        for name, url, parser in sources:
            try:
                response = await client.get(url)
                response.raise_for_status()
                value, quoted_at = parser(response.json())
                if not _plausible(key, value):
                    raise ValueError("قفزة غير منطقية عن آخر قيمة معروفة")
            except Exception as exc:  # noqa: BLE001 — أي فشل ننتقل للمصدر التالي
                logger.warning("فشل المصدر %s: %s", name, exc)
                errors.append(f"{name}: {exc}")
                continue

            quote = Quote(
                value=value,
                source=name,
                fetched_at=datetime.now(timezone.utc),
                quoted_at=quoted_at,
            )
            _last[key] = quote
            return quote

    raise RuntimeError("فشلت كل المصادر — " + " | ".join(errors))


async def _shared(key: str, sources: tuple[Source, ...], timeout: float) -> Quote:
    """يشارك الطلب الجاري بدل ما يكرره — هذا ليس تخزيناً لقيمة قديمة."""
    running = _inflight.get(key)
    if running is not None and not running.done():
        return await asyncio.shield(running)

    task = asyncio.create_task(_fetch(key, sources, timeout))
    _inflight[key] = task
    try:
        return await task
    finally:
        if _inflight.get(key) is task:
            del _inflight[key]


async def gold_usd_per_ounce() -> Quote:
    """سعر أونصة الذهب الفوري بالدولار — يُجلب مع كل استدعاء.

    الاستثناء الوحيد أرضية `MIN_REFETCH_INTERVAL` (ثانيتان): حماية من الإغراق
    لا تخزين. المصدر نفسه يحدّث كل بضع دقائق، فسعر عمره ثانيتان هو **نفس**
    الرقم الذي سيرجّعه طلب جديد — بينما مستخدماً واحداً يرسل ٥٠ رسالة متتالية
    كان سيطلق ٥٠ طلباً حقيقياً ويعرّض البوت للحظر.

    يرفع RuntimeError إذا فشلت كل المصادر وما فيه قيمة سابقة.
    """
    previous = _last.get("gold")
    if (
        MIN_REFETCH_INTERVAL
        and previous is not None
        and not previous.stale
        and previous.age < MIN_REFETCH_INTERVAL
    ):
        return previous

    try:
        return await _shared("gold", SPOT_SOURCES, SOURCE_TIMEOUT)
    except RuntimeError:
        if previous is None:
            raise
        logger.warning("كل المصادر فشلت — نرجّع قيمة عمرها %s", previous.age)
        return replace(previous, stale=True)


async def usd_to(code: str = "SAR") -> Quote:
    """سعر صرف الدولار بالعملة المطلوبة.

    مخزَّن ٦ ساعات عن قصد: أغلب عملات المنطقة مربوطة والمصدر يحدّث يومياً،
    فجلبه مع كل رسالة يضاعف الطلبات بلا فائدة. طزاجة سعر الذهب هي المهمة،
    وهي غير مخزَّنة.
    """
    code = code.upper()
    cached = _fx_cache.get(code)
    if cached is not None and cached.age < FX_TTL and not cached.stale:
        return cached

    sources = tuple(
        (name, url, partial(parser, code=code)) for name, url, parser in FX_SOURCES
    )
    try:
        quote = await _shared(f"fx:{code}", sources, FX_TIMEOUT)
    except RuntimeError:
        if cached is None:
            raise
        return replace(cached, stale=True)

    _fx_cache[code] = quote
    return quote


async def usd_to_sar() -> Quote:
    """اختصار متوافق مع الاستخدام القديم."""
    return await usd_to("SAR")


# ---------- الإغلاقات اليومية (للتغيّر اليومي) ----------


def parse_daily_closes(data: dict) -> list[tuple[date, Decimal]]:
    """يحوّل سلسلة fxratesapi الزمنية إلى [(اليوم, سعر الإغلاق), ...] الأحدث أولاً."""
    closes = []
    for stamp, rates in (data.get("rates") or {}).items():
        per_usd = _to_decimal(rates["XAU"])
        if per_usd <= 0:
            continue
        day = datetime.fromisoformat(stamp.replace("Z", "+00:00")).date()
        closes.append((day, _validate(1 / per_usd, GOLD_RANGE)))
    return sorted(closes, key=lambda row: row[0], reverse=True)


async def daily_closes() -> list[tuple[date, Decimal]]:
    """إغلاقات آخر أيام التداول — من نفس السوق الفوري، مو من العقود الآجلة."""
    global _closes_cache, _closes_lock

    if _closes_lock is None:
        _closes_lock = asyncio.Lock()

    async with _closes_lock:
        if _closes_cache is not None:
            closes, fetched_at = _closes_cache
            if datetime.now(timezone.utc) - fetched_at < CLOSES_TTL:
                return closes

        today = datetime.now(timezone.utc).date()
        url = DAILY_CLOSES_URL.format(start=today - timedelta(days=8), end=today)
        try:
            async with httpx.AsyncClient(
                timeout=FX_TIMEOUT, headers={"User-Agent": USER_AGENT}
            ) as client:
                response = await client.get(url)
                response.raise_for_status()
                closes = parse_daily_closes(response.json())
        except Exception as exc:  # noqa: BLE001 — بيانات تحسينية، فشلها لا يوقف شيئاً
            logger.warning("تعذّر جلب الإغلاقات اليومية: %s", exc)
            return _closes_cache[0] if _closes_cache else []

        _closes_cache = (closes, datetime.now(timezone.utc))
        return closes


def merge_closes(
    own: dict[str, str] | None,
    fetched: list[tuple[date, Decimal]],
) -> list[tuple[date, Decimal]]:
    """يدمج سجلّنا الخاص مع إغلاقات المصدر — سجلّنا له الأولوية.

    سجلّنا مأخوذ من نفس المصادر التي يعرضها البوت، فهو أوفق لأرقامه من رقم
    طرف ثالث. والمصدر يغطي ما قبل تشغيل البوت وما فاتنا وقت التوقف.
    """
    merged: dict[date, Decimal] = {day: price for day, price in fetched}
    for iso, value in (own or {}).items():
        try:
            merged[date.fromisoformat(iso)] = Decimal(value)
        except (ValueError, InvalidOperation):
            logger.warning("سطر تالف في سجل الإغلاقات: %r", iso)
    return sorted(merged.items(), key=lambda row: row[0], reverse=True)


def reference_close(
    closes: list[tuple[date, Decimal]], market_open: bool
) -> Decimal | None:
    """الإغلاق اللي نقيس التغيّر عليه.

    وقت التداول: أحدث إغلاق = إغلاق أمس، وهو المرجع الصحيح.
    والسوق مغلق: أحدث إغلاق هو نفسه السعر المعروض، فنرجع خطوة للإغلاق اللي قبله
    عشان تظهر حركة آخر يوم تداول بدل صفر.
    """
    if not closes:
        return None
    if market_open:
        return closes[0][1]
    return closes[1][1] if len(closes) > 1 else None


async def change_pct(
    current: Decimal,
    market_open: bool,
    own_closes: dict[str, str] | None = None,
) -> Decimal | None:
    """نسبة التغيّر عن الإغلاق المرجعي، أو None إذا البيانات مو متوفرة."""
    closes = merge_closes(own_closes, await daily_closes())
    reference = reference_close(closes, market_open)
    if reference is None or reference == 0:
        return None
    return (current - reference) / reference * 100


def clear_cache() -> None:
    global _closes_cache
    _last.clear()
    _inflight.clear()
    _fx_cache.clear()
    _closes_cache = None

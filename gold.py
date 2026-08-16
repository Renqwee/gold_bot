"""حسابات الذهب: تحويل سعر الأونصة بالدولار إلى سعر الجرام بالريال لكل عيار.

كل العمليات بـ Decimal تجنّباً لأخطاء الفاصلة العائمة.
"""
from dataclasses import dataclass
from decimal import Decimal, ROUND_HALF_UP, getcontext

getcontext().prec = 34

# الأونصة التروية = 31.1034768 جرام بالضبط (التعريف الرسمي)
TROY_OUNCE_GRAMS = Decimal("31.1034768")

# سعر ربط الريال السعودي بالدولار
DEFAULT_USD_SAR = Decimal("3.75")

# العيارات: العيار -> النقاء ككسر دقيق (k / 24)
KARATS = (24, 22, 21, 18, 14, 12, 10, 9)


def purity(karat: int) -> Decimal:
    """نقاء العيار ككسر دقيق، مثال: 21 -> 0.875"""
    return Decimal(karat) / Decimal(24)


def gram_price(
    ounce_usd: Decimal,
    karat: int,
    usd_sar: Decimal = DEFAULT_USD_SAR,
) -> Decimal:
    """سعر الجرام بالريال لعيار معيّن، غير مقرّب."""
    return ounce_usd * usd_sar / TROY_OUNCE_GRAMS * purity(karat)


def quantize(value: Decimal, places: int = 2) -> Decimal:
    """تقريب للعرض فقط — التقريب يتم في آخر خطوة، بعد كل الضرب والقسمة."""
    exp = Decimal(1).scaleb(-places)
    return value.quantize(exp, rounding=ROUND_HALF_UP)


def all_prices(
    ounce_usd: Decimal,
    usd_sar: Decimal = DEFAULT_USD_SAR,
) -> list[tuple[int, Decimal]]:
    """[(العيار, سعر الجرام), ...] لكل العيارات."""
    return [(k, gram_price(ounce_usd, k, usd_sar)) for k in KARATS]


# الأوزان الشائعة للسبائك وللعرض السريع (بالجرام)
COMMON_WEIGHTS = (Decimal("50"), Decimal("100"), Decimal("250"), Decimal("1000"))


def weight_label(grams: Decimal) -> str:
    """تسمية الوزن بالعربي — الكيلو يُعرض كيلو مو ١٠٠٠ جرام."""
    if grams >= 1000 and grams % 1000 == 0:
        kilos = grams / 1000
        count = int(kilos)
        return "كيلو" if count == 1 else f"{count} كيلو"
    trimmed = grams.normalize()
    return f"{trimmed:,f} جرام" if trimmed % 1 else f"{int(trimmed):,} جرام"


def weight_table(
    ounce_usd: Decimal,
    karat: int,
    usd_sar: Decimal = DEFAULT_USD_SAR,
    weights: tuple[Decimal, ...] = COMMON_WEIGHTS,
) -> list[tuple[Decimal, Decimal]]:
    """[(الوزن بالجرام, القيمة بالريال), ...]"""
    price = gram_price(ounce_usd, karat, usd_sar)
    return [(w, price * w) for w in weights]


def weight_value(
    ounce_usd: Decimal,
    karat: int,
    grams: Decimal,
    usd_sar: Decimal = DEFAULT_USD_SAR,
) -> Decimal:
    """قيمة وزن معيّن بالريال."""
    return gram_price(ounce_usd, karat, usd_sar) * grams


def ounce_from_gram(
    gram_sar: Decimal,
    karat: int,
    usd_sar: Decimal = DEFAULT_USD_SAR,
) -> Decimal:
    """العملية العكسية: من سعر الجرام بالريال إلى سعر الأونصة بالدولار."""
    return gram_sar * TROY_OUNCE_GRAMS / usd_sar / purity(karat)


# نصاب زكاة الذهب: ٨٥ جراماً من الذهب الخالص (عيار ٢٤)، والمقدار ربع العشر
ZAKAT_NISAB_GRAMS_24K = Decimal("85")
ZAKAT_RATE = Decimal("0.025")


def zakat(
    ounce_usd: Decimal,
    karat: int,
    grams: Decimal,
    usd_sar: Decimal = DEFAULT_USD_SAR,
) -> tuple[bool, Decimal, Decimal, Decimal]:
    """زكاة الذهب.

    يرجّع: (بلغ النصاب؟, ما يعادله ذهباً خالصاً بالجرام, قيمته بالريال, الزكاة بالريال)
    """
    pure_grams = grams * purity(karat)
    reached = pure_grams >= ZAKAT_NISAB_GRAMS_24K
    value = weight_value(ounce_usd, karat, grams, usd_sar)
    due = value * ZAKAT_RATE if reached else Decimal(0)
    return reached, pure_grams, value, due


# ---------- ضريبة القيمة المضافة ----------

# السعودية: ١٥٪ على المشغولات الذهبية، وصفر على الذهب الاستثماري
# (نقاء ٩٩٪ فأعلى وقابل للتداول في سوق السبائك العالمية).
VAT_RATE = Decimal("0.15")
INVESTMENT_GOLD_MIN_PURITY = Decimal("0.99")


def is_investment_purity(karat: int) -> bool:
    """هل نقاء هذا العيار يبلغ حدّ الذهب الاستثماري؟

    عيار ٢٤ وحده يتجاوز ٩٩٪. والإعفاء مشروط بكونه سبيكة قابلة للتداول عالمياً،
    فسوار عيار ٢٤ يبقى مشغولاً وعليه ضريبة.
    """
    return purity(karat) >= INVESTMENT_GOLD_MIN_PURITY


@dataclass(frozen=True)
class ShopBreakdown:
    """تفكيك سعر المحل إلى ذهب وضريبة ومصنعية."""

    shop_price: Decimal  # ما يطلبه المحل للجرام، شامل كل شيء
    market_price: Decimal  # سعر الذهب الخام
    vat: Decimal  # مبلغ الضريبة داخل السعر
    net_price: Decimal  # سعر المحل بعد نزع الضريبة
    making: Decimal  # المصنعية وهامش المحل
    making_pct: Decimal  # نسبتها إلى سعر الذهب
    vat_applies: bool


def shop_breakdown(
    shop_gram_sar: Decimal,
    market_gram_sar: Decimal,
    vat_applies: bool = True,
) -> ShopBreakdown:
    """يفصل سعر المحل: كم منه ذهب، وكم ضريبة، وكم مصنعية فعلية.

    الضريبة مفروضة على **إجمالي** سعر البيع (ذهب + مصنعية) وهي داخلة في السعر
    المعروض، فنستخرجها بالقسمة على ١٫١٥ — لا بضرب سعر الذهب في ٠٫١٥.
    """
    if vat_applies:
        net = shop_gram_sar / (1 + VAT_RATE)
        vat = shop_gram_sar - net
    else:
        net = shop_gram_sar
        vat = Decimal(0)

    making = net - market_gram_sar
    return ShopBreakdown(
        shop_price=shop_gram_sar,
        market_price=market_gram_sar,
        vat=vat,
        net_price=net,
        making=making,
        making_pct=making / market_gram_sar * 100,
        vat_applies=vat_applies,
    )


def premium(shop_gram_sar: Decimal, market_gram_sar: Decimal) -> tuple[Decimal, Decimal]:
    """الفرق بين سعر المحل وسعر السوق: (الفرق بالريال, النسبة المئوية)."""
    diff = shop_gram_sar - market_gram_sar
    pct = diff / market_gram_sar * 100
    return diff, pct


# ---------- تحويل مدخلات المستخدم إلى رقم ----------

# الأرقام العربية-الهندية والفارسية -> أرقام لاتينية
_DIGIT_MAP = {ord(c): str(i) for i, c in enumerate("٠١٢٣٤٥٦٧٨٩")}
_DIGIT_MAP.update({ord(c): str(i) for i, c in enumerate("۰۱۲۳۴۵۶۷۸۹")})
_DIGIT_MAP[ord("٫")] = "."  # الفاصلة العشرية العربية
_DIGIT_MAP[ord("،")] = ""   # فاصلة الآلاف العربية
_DIGIT_MAP[ord(",")] = ""   # فاصلة الآلاف
_DIGIT_MAP[ord("_")] = ""
_DIGIT_MAP[ord(" ")] = ""
_DIGIT_MAP[ord("٬")] = ""  # فاصل الآلاف العربي
_DIGIT_MAP[ord("‏")] = ""  # علامات الاتجاه
_DIGIT_MAP[ord("‎")] = ""


class ParseError(ValueError):
    """رقم غير صالح."""


def parse_number(text: str) -> Decimal:
    """يحوّل نص المستخدم إلى Decimal موجب، ويدعم الأرقام العربية."""
    cleaned = text.strip().translate(_DIGIT_MAP)
    if not cleaned:
        raise ParseError("فاضي")
    try:
        value = Decimal(cleaned)
    except Exception:
        raise ParseError(f"«{text.strip()}» مو رقم")
    if not value.is_finite():
        raise ParseError("رقم غير صالح")
    if value <= 0:
        raise ParseError("لازم الرقم يكون أكبر من صفر")
    return value

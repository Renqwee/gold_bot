"""فهم رسائل المستخدم المكتوبة بلغة عادية.

الهدف: ما أحد يحتاج يحفظ صيغة أمر. أمثلة تُفهم:
    «12 جرام عيار 21»      → قيمة وزن
    «نص كيلو 24»           → قيمة وزن
    «كم سعر عيار 22»       → سعر عيار
    «3400»                 → سعر أونصة بالدولار
    «الكيلو كم»            → قيمة كيلو بالعيار المفضل
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation

from gold import KARATS

# الأرقام العربية والفارسية -> لاتينية
_DIGITS = {ord(c): str(i) for i, c in enumerate("٠١٢٣٤٥٦٧٨٩")}
_DIGITS.update({ord(c): str(i) for i, c in enumerate("۰۱۲۳۴۵۶۷۸۹")})
_DIGITS[ord("٫")] = "."
_DIGITS[ord("،")] = " "
_DIGITS[ord("٬")] = ""
_DIGITS[ord("ـ")] = ""  # تطويل

# كلمات الوزن — الجيم تنطق قاف/غين في اللهجات، فنقبلها كلها
GRAM_WORDS = r"(?:جرام|جرامات|قرام|قرامات|غرام|غرامات|جم|غ|g|gram|grams)"
KILO_WORDS = r"(?:كيلو|كيلوجرام|كيلوغرام|كجم|كغ|kg|kilo)"
KARAT_WORDS = r"(?:عيار|عيارات|قيراط|كارات|karat|k)"

HALF_WORDS = r"(?:نص|نصف)"
QUARTER_WORDS = r"(?:ربع)"

# جمل تسأل عن السعر بدون رقم
PRICE_QUESTION = re.compile(
    r"(?:كم|سعر|بكم|السعر|أسعار|اسعار|price)", re.IGNORECASE
)


@dataclass(frozen=True)
class Query:
    """ما فهمناه من الرسالة."""

    grams: Decimal | None = None
    karat: int | None = None
    ounce_usd: Decimal | None = None
    asks_price: bool = False

    @property
    def is_empty(self) -> bool:
        return (
            self.grams is None
            and self.karat is None
            and self.ounce_usd is None
            and not self.asks_price
        )


def normalize(text: str) -> str:
    """توحيد الأرقام والحروف وإزالة فواصل الآلاف."""
    text = text.translate(_DIGITS)
    text = re.sub(r"[إأآا]", "ا", text)
    text = re.sub(r"(?<=\d),(?=\d{3})", "", text)  # 1,000 -> 1000
    return re.sub(r"\s+", " ", text).strip()


def _decimal(raw: str) -> Decimal | None:
    try:
        value = Decimal(raw)
    except InvalidOperation:
        return None
    return value if value.is_finite() and value > 0 else None


def _find_karat(text: str) -> int | None:
    """يلقط «عيار 21» أو «21 قيراط» — ويتجاهل أي رقم مو عيار معروف."""
    patterns = (
        rf"{KARAT_WORDS}\s*(\d{{1,2}})",
        rf"(\d{{1,2}})\s*{KARAT_WORDS}",
    )
    for pattern in patterns:
        for match in re.finditer(pattern, text, re.IGNORECASE):
            karat = int(match.group(1))
            if karat in KARATS:
                return karat
    return None


def _find_weight(text: str) -> tuple[Decimal, str] | None:
    """يلقط الوزن ويرجّع (الوزن, النص بعد حذف عبارة الوزن).

    حذف عبارة الوزن مهم عشان ما نخلط رقم الوزن برقم العيار بعدين.
    """
    rules = (
        (rf"{HALF_WORDS}\s*{KILO_WORDS}", lambda _m: Decimal("500")),
        (rf"{QUARTER_WORDS}\s*{KILO_WORDS}", lambda _m: Decimal("250")),
        (
            rf"(\d+(?:\.\d+)?)\s*{KILO_WORDS}",
            lambda m: (v * 1000 if (v := _decimal(m.group(1))) else None),
        ),
        (rf"(\d+(?:\.\d+)?)\s*{GRAM_WORDS}", lambda m: _decimal(m.group(1))),
        (KILO_WORDS, lambda _m: Decimal("1000")),  # «كيلو» بدون رقم
    )

    for pattern, extract in rules:
        match = re.search(pattern, text, re.IGNORECASE)
        if match is None:
            continue
        value = extract(match)
        if value is None:
            continue
        residual = text[: match.start()] + " " + text[match.end() :]
        return value, residual

    return None


def _bare_karat(text: str) -> int | None:
    """رقم مجرد يطابق عياراً معروفاً — يُستخدم بعد ما نحذف عبارة الوزن."""
    for match in re.finditer(r"\b(\d{1,2})\b", text):
        karat = int(match.group(1))
        if karat in KARATS:
            return karat
    return None


def _bare_number(text: str) -> Decimal | None:
    """رقم لحاله بدون أي كلمة معه — يُفسَّر كسعر أونصة."""
    match = re.fullmatch(r"\$?\s*(\d+(?:\.\d+)?)\s*\$?", text.strip())
    return _decimal(match.group(1)) if match else None


def understand(text: str) -> Query:
    """يحوّل رسالة المستخدم إلى استفسار مفهوم."""
    text = normalize(text)
    if not text:
        return Query()

    if (ounce := _bare_number(text)) is not None:
        return Query(ounce_usd=ounce)

    found = _find_weight(text)
    grams, residual = found if found else (None, text)

    karat = _find_karat(text)
    if karat is None:
        # «نص كيلو 24» — العيار مذكور كرقم مجرد بعد الوزن
        karat = _bare_karat(residual)

    # أي رقم كبير باقٍ يُفهم كسعر أونصة، مثل «عيار 21 على 4380»
    ounce = None
    if grams is None:
        for match in re.finditer(r"(\d+(?:\.\d+)?)", text):
            value = _decimal(match.group(1))
            if value is None or (karat is not None and value == karat):
                continue
            # الأونصة ما تنزل تحت ٥٠٠ دولار عملياً — أي رقم أصغر مو سعر أونصة
            if value >= 500:
                ounce = value
                break

    return Query(
        grams=grams,
        karat=karat,
        ounce_usd=ounce,
        asks_price=bool(PRICE_QUESTION.search(text)),
    )

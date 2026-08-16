"""العملات المدعومة.

كل الحساب يتم بالدولار ثم يُحوَّل، فإضافة عملة = سطر واحد هنا.

الاحتياطي (`peg`) للعملات المربوطة رسمياً بالدولار فقط — إذا سقط مصدر الصرف
نستخدمه بأمان. العملات العائمة (الجنيه) والمربوطة بسلة (الدينار الكويتي)
تُترك بلا احتياطي: نرفض عرض رقم مخمَّن بدل أن نضلّل.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal


@dataclass(frozen=True)
class Currency:
    code: str  # رمز ISO
    name: str  # الاسم المفرد للعرض
    flag: str
    decimals: int = 2
    peg: Decimal | None = None  # وحدة مقابل الدولار، للمربوطة رسمياً فقط

    @property
    def label(self) -> str:
        return f"{self.flag} {self.name}"


CURRENCIES: dict[str, Currency] = {
    c.code: c
    for c in (
        Currency("SAR", "ريال سعودي", "🇸🇦", peg=Decimal("3.75")),
        Currency("AED", "درهم إماراتي", "🇦🇪", peg=Decimal("3.6725")),
        Currency("QAR", "ريال قطري", "🇶🇦", peg=Decimal("3.64")),
        Currency("BHD", "دينار بحريني", "🇧🇭", decimals=3, peg=Decimal("0.376")),
        Currency("OMR", "ريال عماني", "🇴🇲", decimals=3, peg=Decimal("0.3845")),
        Currency("JOD", "دينار أردني", "🇯🇴", decimals=3, peg=Decimal("0.709")),
        # مربوط بسلة عملات لا بالدولار وحده — لا احتياطي ثابت
        Currency("KWD", "دينار كويتي", "🇰🇼", decimals=3),
        # عائم
        Currency("EGP", "جنيه مصري", "🇪🇬"),
        Currency("USD", "دولار", "🇺🇸", peg=Decimal("1")),
    )
}

DEFAULT_CODE = "SAR"
DEFAULT = CURRENCIES[DEFAULT_CODE]

# أسماء شائعة يكتبها المستخدم
ALIASES = {
    "ريال": "SAR", "سعودي": "SAR", "سعوديه": "SAR", "سعودية": "SAR",
    "درهم": "AED", "اماراتي": "AED", "امارات": "AED",
    "قطري": "QAR", "قطر": "QAR",
    "بحريني": "BHD", "بحرين": "BHD",
    "عماني": "OMR", "عمان": "OMR",
    "اردني": "JOD", "اردن": "JOD",
    "كويتي": "KWD", "كويت": "KWD",
    "جنيه": "EGP", "مصري": "EGP", "مصر": "EGP",
    "دولار": "USD",
}


def resolve(text: str) -> Currency | None:
    """يحوّل ما كتبه المستخدم إلى عملة — رمزاً كان أو اسماً عربياً."""
    cleaned = text.strip().lower()
    if not cleaned:
        return None

    code = CURRENCIES.get(cleaned.upper())
    if code is not None:
        return code

    normalized = cleaned.replace("أ", "ا").replace("إ", "ا").replace("ة", "ه")
    for word, target in ALIASES.items():
        if word.replace("ة", "ه") == normalized:
            return CURRENCIES[target]
    return None

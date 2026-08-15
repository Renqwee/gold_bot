"""فحص أن صفحة الشرح تحسب بنفس أرقام البوت.

الصفحة فيها نسخة JavaScript من المعادلة. لو أحد غيّر ثابتاً في بايثون ونسي الصفحة،
تصير الصفحة تعطي سعراً مختلفاً عن البوت — وهذي الاختبارات تكشفها.
"""

import re
from decimal import Decimal
from pathlib import Path

import pytest

from gold import COMMON_WEIGHTS, KARATS, TROY_OUNCE_GRAMS

PAGE = Path(__file__).with_name("docs") / "index.html"


@pytest.fixture(scope="module")
def html() -> str:
    if not PAGE.exists():
        pytest.skip("صفحة الشرح غير موجودة")
    return PAGE.read_text(encoding="utf-8")


def test_page_exists(html):
    assert "<html" in html.lower()


def test_troy_ounce_matches(html):
    match = re.search(r"var TROY_OUNCE = ([\d.]+);", html)
    assert match, "ما لقيت ثابت الأونصة في الصفحة"
    assert Decimal(match.group(1)) == TROY_OUNCE_GRAMS


def test_karats_match(html):
    match = re.search(r"var KARATS = \[([\d,\s]+)\];", html)
    assert match, "ما لقيت قائمة العيارات في الصفحة"
    page_karats = tuple(int(k) for k in match.group(1).split(","))
    assert page_karats == KARATS


def test_weights_match(html):
    match = re.search(r"var WEIGHTS = \[([\d,\s]+)\];", html)
    assert match, "ما لقيت قائمة الأوزان في الصفحة"
    page_weights = tuple(Decimal(w.strip()) for w in match.group(1).split(","))
    assert page_weights == COMMON_WEIGHTS


def test_formula_matches(html):
    """صيغة الحساب في الصفحة لازم تكون نفس ترتيب عمليات بايثون."""
    assert "ounce * rate / TROY_OUNCE * (karat / 24)" in html


def test_market_hours_match_python(html):
    """مواعيد السوق في الصفحة لازم تطابق market.py"""
    import market

    assert f"hour >= {market.CLOSE_HOUR_UTC}" in html
    assert f"hour < {market.OPEN_HOUR_UTC}" in html


def test_no_external_resources(html):
    """الصفحة لازم تكون مكتفية بنفسها — بدون CDN أو خطوط خارجية."""
    externals = re.findall(r'(?:src|href)="(https?://[^"]+)"', html)
    allowed = ("https://t.me/",)
    unexpected = [u for u in externals if not u.startswith(allowed)]
    assert not unexpected, f"موارد خارجية غير متوقعة: {unexpected}"


def test_has_disclaimer(html):
    """التنبيه القانوني لازم يكون موجوداً."""
    assert "للاسترشاد فقط" in html
    assert "نصيحة استثمارية" in html


def test_rtl_and_lang(html):
    assert 'dir="rtl"' in html
    assert 'lang="ar"' in html


def test_responsive_meta(html):
    assert 'name="viewport"' in html


def test_dark_mode_support(html):
    assert "prefers-color-scheme: dark" in html

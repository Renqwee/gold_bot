"""فحص العملات."""

from decimal import Decimal

import pytest

import currencies
import rates
from currencies import CURRENCIES, resolve


def test_default_is_saudi_riyal():
    assert currencies.DEFAULT.code == "SAR"


@pytest.mark.parametrize(
    "text, code",
    [
        ("AED", "AED"), ("aed", "AED"), ("  KWD  ", "KWD"),
        ("درهم", "AED"), ("إماراتي", "AED"), ("اماراتي", "AED"),
        ("ريال", "SAR"), ("سعودية", "SAR"), ("سعوديه", "SAR"),
        ("جنيه", "EGP"), ("مصري", "EGP"),
        ("كويتي", "KWD"), ("دولار", "USD"),
    ],
)
def test_resolve_accepts_codes_and_arabic(text, code):
    assert resolve(text).code == code


@pytest.mark.parametrize("text", ["", "  ", "XYZ", "يورو", "12"])
def test_resolve_rejects_unknown(text):
    assert resolve(text) is None


def test_gulf_dinars_use_three_decimals():
    """الدينار يُكتب بثلاث خانات عرفاً، والريال بخانتين."""
    for code in ("KWD", "BHD", "OMR", "JOD"):
        assert CURRENCIES[code].decimals == 3
    for code in ("SAR", "AED", "QAR", "EGP", "USD"):
        assert CURRENCIES[code].decimals == 2


def test_only_pegged_currencies_have_fallback():
    """الجنيه عائم والدينار الكويتي مربوط بسلة — رقم ثابت لهما يضلّل."""
    assert CURRENCIES["EGP"].peg is None
    assert CURRENCIES["KWD"].peg is None
    for code in ("SAR", "AED", "QAR", "BHD", "OMR", "JOD", "USD"):
        assert CURRENCIES[code].peg is not None


def test_peg_values_match_official():
    assert CURRENCIES["SAR"].peg == Decimal("3.75")
    assert CURRENCIES["AED"].peg == Decimal("3.6725")
    assert CURRENCIES["QAR"].peg == Decimal("3.64")
    assert CURRENCIES["USD"].peg == Decimal("1")


def test_every_currency_has_flag_and_name():
    for cur in CURRENCIES.values():
        assert cur.flag and cur.name
        assert cur.flag in cur.label and cur.name in cur.label


# ---------- التحليل ----------


def test_fx_parsers_read_requested_currency():
    payload = {"result": "success", "rates": {"SAR": 3.75, "KWD": 0.308452}}
    assert rates.parse_er_api(payload, code="KWD")[0] == Decimal("0.308452")
    assert rates.parse_er_api(payload, code="SAR")[0] == Decimal("3.75")


def test_fx_parsers_reject_unknown_currency():
    with pytest.raises(ValueError):
        rates.parse_er_api({"result": "success", "rates": {"SAR": 3.75}}, code="XYZ")
    with pytest.raises(ValueError):
        rates.parse_currency_api({"usd": {"sar": 3.75}}, code="XYZ")


def test_currency_api_uses_lowercase_keys():
    assert rates.parse_currency_api({"usd": {"kwd": 0.3086}}, code="KWD")[0] > 0


def test_fx_range_covers_dinar_and_pound():
    """المدى كان مضبوطاً على الريال فقط، فكان يرفض الدينار الكويتي (٠٫٣٠٨)."""
    low, high = rates.FX_RANGE
    assert low < Decimal("0.308")   # الدينار الكويتي
    assert high > Decimal("50.3")   # الجنيه المصري


@pytest.mark.parametrize("bad", [0, -1, 999999, float("inf")])
def test_fx_range_still_rejects_nonsense(bad):
    with pytest.raises(ValueError):
        rates.parse_er_api({"result": "success", "rates": {"SAR": bad}})

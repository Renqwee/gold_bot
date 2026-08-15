"""فحص فهم الكلام الطبيعي."""

from decimal import Decimal

import pytest

from gold import COMMON_WEIGHTS, weight_label, weight_table
from understand import understand


# ---------- الوزن ----------


@pytest.mark.parametrize(
    "text, grams",
    [
        ("12 جرام", "12"),
        ("12 قرام", "12"),
        ("12 غرام", "12"),
        ("12.5 جرام", "12.5"),
        ("١٢ جرام", "12"),
        ("١٢٫٥ قرام", "12.5"),
        ("250 قرام", "250"),
        ("50جرام", "50"),
        ("كيلو", "1000"),
        ("2 كيلو", "2000"),
        ("نص كيلو", "500"),
        ("نصف كيلو", "500"),
        ("ربع كيلو", "250"),
        ("1.5 كجم", "1500"),
        ("100 g", "100"),
    ],
)
def test_weight_parsing(text, grams):
    assert understand(text).grams == Decimal(grams)


# ---------- العيار ----------


@pytest.mark.parametrize(
    "text, karat",
    [
        ("عيار 21", 21),
        ("عيار21", 21),
        ("21 قيراط", 21),
        ("عيار ٢٤", 24),
        ("كم سعر عيار 22", 22),
    ],
)
def test_karat_parsing(text, karat):
    assert understand(text).karat == karat


def test_unknown_karat_ignored():
    """عيار ٢٣ مو من عياراتنا — نتجاهله بدل ما نحسب غلط."""
    assert understand("عيار 23").karat is None
    assert understand("عيار 99").karat is None


# ---------- الاثنين معاً ----------


def test_weight_with_karat():
    query = understand("12 جرام عيار 21")
    assert query.grams == Decimal("12")
    assert query.karat == 21


def test_karat_before_weight():
    query = understand("عيار 22 وزن 50 قرام")
    assert query.grams == Decimal("50")
    assert query.karat == 22


def test_half_kilo_with_karat():
    query = understand("نص كيلو 24")
    assert query.grams == Decimal("500")
    assert query.karat == 24


def test_karat_number_not_mistaken_for_ounce():
    """«عيار 21» ما فيه سعر أونصة — الـ21 عيار مو سعر."""
    query = understand("عيار 21")
    assert query.karat == 21
    assert query.ounce_usd is None
    assert query.grams is None


# ---------- سعر الأونصة ----------


@pytest.mark.parametrize("text", ["3400", "4380.5", "٤٣٨٠", "$4380", "4,380"])
def test_bare_number_is_ounce(text):
    assert understand(text).ounce_usd is not None


def test_ounce_with_karat():
    query = understand("عيار 21 على سعر 4380")
    assert query.karat == 21
    assert query.ounce_usd == Decimal("4380")


def test_small_number_not_treated_as_ounce():
    """«12 جرام عيار 21» ما فيه سعر أونصة رغم وجود أرقام."""
    assert understand("12 جرام عيار 21").ounce_usd is None


# ---------- أسئلة عامة ----------


def test_price_question():
    assert understand("كم سعر الذهب").asks_price is True
    assert understand("بكم الجرام").asks_price is True


def test_irrelevant_text_is_empty():
    for text in ("مرحبا", "شكرا", "😀", ""):
        assert understand(text).is_empty


# ---------- تسميات الأوزان ----------


def test_weight_labels():
    assert weight_label(Decimal("50")) == "50 جرام"
    assert weight_label(Decimal("1000")) == "كيلو"
    assert weight_label(Decimal("2000")) == "2 كيلو"
    assert weight_label(Decimal("12.5")) == "12.5 جرام"


def test_weight_table_matches_gram_price():
    from gold import gram_price

    ounce = Decimal("4380")
    table = weight_table(ounce, 21)
    assert len(table) == len(COMMON_WEIGHTS)
    for grams, total in table:
        assert total == gram_price(ounce, 21) * grams


def test_kilo_is_thousand_times_gram():
    from gold import gram_price

    ounce = Decimal("4380")
    table = dict(weight_table(ounce, 24))
    assert table[Decimal("1000")] == gram_price(ounce, 24) * 1000
    assert table[Decimal("1000")] == table[Decimal("100")] * 10

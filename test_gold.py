"""فحص دقة الحسابات."""

from decimal import Decimal

from gold import (
    DEFAULT_USD_SAR,
    KARATS,
    TROY_OUNCE_GRAMS,
    ParseError,
    all_prices,
    gram_price,
    ounce_from_gram,
    parse_number,
    is_investment_purity,
    premium,
    purity,
    shop_breakdown,
    quantize,
    weight_value,
)


def test_troy_ounce_is_exact():
    assert TROY_OUNCE_GRAMS == Decimal("31.1034768")


def test_purity_is_exact_fraction():
    assert purity(24) == 1
    assert purity(18) == Decimal("0.75")
    assert purity(12) == Decimal("0.5")
    # 22/24 كسر دوري — لازم يبقى بدقة عالية مو 0.9167
    assert purity(22) * 24 == 22
    assert purity(21) * 24 == 21


def test_no_float_error():
    """0.1 + 0.2 == 0.3 لازم تكون صحيحة مع Decimal."""
    price = gram_price(Decimal("0.1"), 24) + gram_price(Decimal("0.2"), 24)
    assert price == gram_price(Decimal("0.3"), 24)


def test_known_value():
    # 3400 × 3.75 ÷ 31.1034768 = 409.9220... ريال للجرام عيار 24
    price = gram_price(Decimal("3400"), 24)
    assert quantize(price) == Decimal("409.92")
    # الفرق عن استخدام 31.1 المقرّبة
    rough = Decimal("3400") * Decimal("3.75") / Decimal("31.1")
    assert quantize(rough) == Decimal("409.97")


def test_karat_21_of_24():
    ounce = Decimal("3400")
    assert gram_price(ounce, 21) == gram_price(ounce, 24) * Decimal("0.875")


def test_karats_descend():
    prices = [p for _, p in all_prices(Decimal("3400"))]
    assert prices == sorted(prices, reverse=True)
    assert len(prices) == len(KARATS)


def test_custom_rate():
    ounce = Decimal("3400")
    assert gram_price(ounce, 24, Decimal("3.7502")) > gram_price(ounce, 24)
    assert gram_price(ounce, 24, DEFAULT_USD_SAR) == gram_price(ounce, 24)


def test_rounding_half_up():
    assert quantize(Decimal("2.345")) == Decimal("2.35")
    assert quantize(Decimal("2.344")) == Decimal("2.34")


def test_weight_value():
    ounce = Decimal("3400")
    assert weight_value(ounce, 21, Decimal("10")) == gram_price(ounce, 21) * 10
    assert weight_value(ounce, 21, Decimal("0.5")) == gram_price(ounce, 21) / 2


def test_ounce_from_gram_round_trips():
    """الذهاب والإياب يرجّع نفس الرقم.

    العيارات ذات النقاء الدوري (22، 10، 9…) يبقى فيها فرق حول 10⁻³⁰ من
    دقة الـ 34 خانة — أصغر بكثير من أي أثر على السعر.
    """
    for karat in KARATS:
        gram = gram_price(Decimal("3400"), karat)
        back = ounce_from_gram(gram, karat)
        assert abs(back - Decimal("3400")) < Decimal("1e-25")
        assert quantize(back) == Decimal("3400.00")


def test_ounce_from_gram_custom_rate():
    rate = Decimal("3.7502")
    gram = gram_price(Decimal("3400"), 18, rate)
    assert quantize(ounce_from_gram(gram, 18, rate)) == Decimal("3400.00")


def test_premium():
    market = gram_price(Decimal("3400"), 21)  # ≈ 358.68
    diff, pct = premium(Decimal("400"), market)
    assert diff > 0
    assert quantize(diff) == Decimal("41.32")
    assert Decimal("11") < pct < Decimal("12")


def test_premium_below_market():
    market = gram_price(Decimal("3400"), 21)
    diff, pct = premium(Decimal("300"), market)
    assert diff < 0
    assert pct < 0


def test_parse_arabic_digits():
    assert parse_number("٣٤٠٠") == Decimal("3400")
    assert parse_number("٣٤٠٠٫٥") == Decimal("3400.5")
    assert parse_number("3,400.75") == Decimal("3400.75")
    assert parse_number("  3400  ") == Decimal("3400")


def test_parse_rejects_bad_input():
    for bad in ("", "abc", "-5", "0", "nan", "inf"):
        try:
            parse_number(bad)
        except ParseError:
            continue
        raise AssertionError(f"لازم يرفض: {bad!r}")


# ---------- تفكيك سعر المحل مع الضريبة ----------


def test_vat_extracted_not_added():
    """الضريبة داخلة في سعر المحل المعروض، فتُستخرج بالقسمة لا بالضرب.

    الخطأ الشائع: ضرب سعر الذهب في ٠٫١٥. الصحيح أن الضريبة على إجمالي
    البيع (ذهب + مصنعية)، فتُستخرج من السعر النهائي.
    """
    b = shop_breakdown(Decimal("115"), Decimal("80"))
    assert quantize(b.net_price) == Decimal("100.00")
    assert quantize(b.vat) == Decimal("15.00")
    assert quantize(b.making) == Decimal("20.00")


def test_breakdown_parts_sum_to_shop_price():
    b = shop_breakdown(Decimal("600"), Decimal("461.66"))
    assert quantize(b.market_price + b.making + b.vat) == quantize(b.shop_price)


def test_exempt_bullion_has_no_vat():
    b = shop_breakdown(Decimal("560"), Decimal("527.62"), vat_applies=False)
    assert b.vat == 0
    assert b.net_price == b.shop_price
    assert quantize(b.making) == quantize(Decimal("560") - Decimal("527.62"))


def test_vat_inflates_apparent_making_charge():
    """بدون فصل الضريبة تبدو المصنعية أكبر بكثير — وهذا التضليل الذي نصلحه."""
    shop, market = Decimal("600"), Decimal("461.66")
    naive, _ = premium(shop, market)          # الطريقة القديمة
    real = shop_breakdown(shop, market).making  # بعد نزع الضريبة
    assert naive > real
    assert quantize(naive - real) == quantize(shop_breakdown(shop, market).vat)


def test_only_karat_24_reaches_investment_purity():
    assert is_investment_purity(24) is True
    for karat in KARATS:
        if karat != 24:
            assert is_investment_purity(karat) is False


def test_shop_below_market_gives_negative_making():
    """سعر أقل من الذهب الخام: غالباً سعر شراء لا بيع، أو عيار خاطئ."""
    b = shop_breakdown(Decimal("300"), Decimal("461.66"))
    assert b.making < 0
    assert b.making_pct < 0

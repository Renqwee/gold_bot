"""فحص قراءة الإعدادات — التوكن ورابط الصفحة."""

import pytest

import bot


@pytest.mark.parametrize(
    "token",
    [
        "111111111:TESTtokenNOTrealAAAAAAAAAAAAAAAAAAA",
        "123456789:ABCdefGHIjklMNOpqrsTUVwxyz1234567890",
    ],
)
def test_valid_tokens_accepted(token):
    assert bot.TOKEN_PATTERN.fullmatch(token)


@pytest.mark.parametrize(
    "token, why",
    [
        (
            "111111111:TESTtokenNOTrealAAAAAAAAAAAAAAAAAAASITE_URL=https://x.github.io/",
            "متغيّر ملتصق بنهاية سطر التوكن في .env",
        ),
        ("111111111", "بدون سرّ"),
        ("abc:AAF-ATkzO7BuDCDkVD2S6ZlvdXUU21n2qb4", "معرّف غير رقمي"),
        ("111111111:short", "السرّ قصير جداً"),
        ("111111111:TESTtokenNOTreal AAAAAAAAAAAAAAAAA", "فيه مسافة"),
        ("", "فاضي"),
    ],
)
def test_broken_tokens_rejected(token, why):
    assert not bot.TOKEN_PATTERN.fullmatch(token), f"لازم يُرفض: {why}"


def test_env_example_documents_both_keys():
    """قالب .env لازم يذكر المفتاحين، وكل واحد في سطر مستقل."""
    from pathlib import Path

    example = Path(__file__).with_name(".env.example")
    if not example.exists():
        pytest.skip(".env.example غير موجود")

    lines = [
        l.strip()
        for l in example.read_text().splitlines()
        if l.strip() and not l.strip().startswith("#")
    ]
    keys = [l.split("=", 1)[0] for l in lines]

    assert "BOT_TOKEN" in keys
    assert "SITE_URL" in keys
    assert len(keys) == len(set(keys)), "مفاتيح مكررة"


def test_env_example_ends_with_newline():
    """بدون سطر جديد في النهاية، أي «>> .env» يلصق المتغيّر بالسطر السابق."""
    from pathlib import Path

    example = Path(__file__).with_name(".env.example")
    if not example.exists():
        pytest.skip(".env.example غير موجود")
    assert example.read_text().endswith("\n")


def test_site_url_must_be_absolute():
    """الرابط بدون بروتوكول يرفضه تيليقرام، فنتجاهله بدل ما نرسل زراً مكسوراً."""
    for bad in ("renqwee.github.io/gold_bot/", "ftp://x.com", "", "  "):
        assert not bad.strip().startswith(("http://", "https://"))
    assert "https://renqwee.github.io/gold_bot/docs/".startswith("https://")


# ---------- مسار ملف الحالة ----------


def test_state_file_defaults_next_to_bot(monkeypatch):
    """بدون المتغيّر يبقى السلوك القديم — تشغيل محلي بلا مفاجآت."""
    from pathlib import Path

    monkeypatch.delenv("BOT_STATE_DIR", raising=False)
    path = bot.state_file()
    assert path.name == "bot_state.pickle"
    assert path.parent == Path(bot.__file__).parent


def test_state_file_follows_env(tmp_path, monkeypatch):
    """داخل دوكر يوجَّه لمجلد مربوط بـ volume."""
    target = tmp_path / "data"
    monkeypatch.setenv("BOT_STATE_DIR", str(target))
    path = bot.state_file()
    assert path == target / "bot_state.pickle"
    assert target.is_dir(), "المجلد لازم يُنشأ إذا ما كان موجوداً"


def test_state_dir_ignores_blank_value(monkeypatch):
    """متغيّر فاضي في .env ما يكسر المسار."""
    from pathlib import Path

    monkeypatch.setenv("BOT_STATE_DIR", "   ")
    assert bot.state_file().parent == Path(bot.__file__).parent

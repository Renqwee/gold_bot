"""بوت تيليقرام لأسعار الذهب — أسعار مباشرة + حاسبة لكل العيارات.

التشغيل:  python bot.py   (التوكن يُقرأ من ملف .env)
"""

import logging
import os
import re
from datetime import datetime, time, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from zoneinfo import ZoneInfo

from dotenv import load_dotenv
from telegram import (
    BotCommand,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    InlineQueryResultArticle,
    InputTextMessageContent,
    KeyboardButton,
    MenuButtonCommands,
    ReplyKeyboardMarkup,
    Update,
    WebAppInfo,
)
from telegram.constants import ParseMode
from telegram.error import BadRequest, NetworkError, TelegramError
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    InlineQueryHandler,
    MessageHandler,
    PicklePersistence,
    filters,
)

import alerts as alerts_store
import currencies
import health
import market
import rates
import understand as nlp
from alerts import Alert
from gold import (
    COMMON_WEIGHTS,
    DEFAULT_USD_SAR,
    KARATS,
    TROY_OUNCE_GRAMS,
    ZAKAT_NISAB_GRAMS_24K,
    ParseError,
    all_prices,
    gram_price,
    ounce_from_gram,
    parse_number,
    VAT_RATE,
    is_investment_purity,
    premium,
    purity,
    shop_breakdown,
    quantize,
    weight_label,
    weight_table,
    weight_value,
    zakat,
)

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logging.getLogger("httpx").setLevel(logging.WARNING)
logger = logging.getLogger(__name__)

RIYADH = ZoneInfo("Asia/Riyadh")

# رابط صفحة الشرح — يُضبط في .env، وإذا ما وُجد تختفي أزرار الرابط تلقائياً
SITE_URL = ""

# معرّف صاحب البوت — يستقبل تنبيهات صحة المصادر. اختياري.
OWNER_ID = 0

# صيغة توكن تيليقرام: معرّف رقمي : سرّ
TOKEN_PATTERN = re.compile(r"\d{6,}:[A-Za-z0-9_-]{30,}")

# مفاتيح chat_data
LAST_OUNCE = "last_ounce"
PINNED_RATE = "pinned_rate"  # موجود = المستخدم ثبّت سعر الصرف يدوياً
FAV_KARAT = "fav_karat"
CURRENCY = "currency"

# مفاتيح bot_data
MONITOR = "health_monitor"
OWN_CLOSES = "own_closes"

DEFAULT_KARAT = 21  # الأكثر تداولاً في السوق السعودي

# أزرار اللوحة الثابتة
BTN_NOW = "💰 السعر الآن"
BTN_MY_KARAT = "⭐ عياري"
BTN_WEIGHT = "🧮 احسب وزن"
BTN_BARS = "🧱 السبائك"
BTN_SHOP = "🏪 قارن محل"
BTN_ALERTS = "🔔 تنبيهاتي"
BTN_HELP = "❓ مساعدة"
BTN_APP = "🌐 الحاسبة"


def main_keyboard(private: bool = True) -> ReplyKeyboardMarkup:
    """اللوحة الثابتة.

    زر الحاسبة يفتح الصفحة **داخل تيليقرام** بدل متصفح خارجي — وتيليقرام
    يقبل أزرار الويب في المحادثات الخاصة فقط، فنحذفه في المجموعات.
    """
    rows = [
        [KeyboardButton(BTN_NOW), KeyboardButton(BTN_MY_KARAT)],
        [KeyboardButton(BTN_WEIGHT), KeyboardButton(BTN_BARS)],
        [KeyboardButton(BTN_SHOP), KeyboardButton(BTN_ALERTS)],
    ]

    last = []
    if SITE_URL and private:
        last.append(KeyboardButton(BTN_APP, web_app=WebAppInfo(url=SITE_URL)))
    last.append(KeyboardButton(BTN_HELP))
    rows.append(last)

    return ReplyKeyboardMarkup(rows, resize_keyboard=True)


def app_button(label: str = "🌐 افتح الحاسبة") -> InlineKeyboardMarkup | None:
    """زر يفتح الصفحة كتطبيق داخل تيليقرام."""
    if not SITE_URL:
        return None
    return InlineKeyboardMarkup(
        [[InlineKeyboardButton(label, web_app=WebAppInfo(url=SITE_URL))]]
    )


def site_button(label: str = "📖 دليل الاستخدام") -> InlineKeyboardMarkup | None:
    """زر يفتح صفحة الشرح — يرجّع None إذا الرابط مو مضبوط."""
    if not SITE_URL:
        return None
    return InlineKeyboardMarkup([[InlineKeyboardButton(label, url=SITE_URL)]])


def weights_block(
    ounce: Decimal,
    karat: int,
    usd_sar: Decimal,
    cur: currencies.Currency = currencies.DEFAULT,
    weights=COMMON_WEIGHTS,
) -> str:
    """جدول أسعار الأوزان الشائعة: ٥٠ و١٠٠ و٢٥٠ جرام والكيلو."""
    rows = "\n".join(
        f"{weight_label(grams):>10} │ {money_big(total, cur):>12} {cur.name}"
        for grams, total in weight_table(ounce, karat, usd_sar, weights)
    )
    return f"<b>الأوزان — عيار {karat}</b>\n<pre>{rows}</pre>"


# ---------- أدوات التنسيق ----------


def fmt(value: Decimal, places: int = 2) -> str:
    return f"{quantize(value, places):,}"


def fmt_money(value: Decimal) -> str:
    """المبالغ الكبيرة بدون كسور، والصغيرة بهللاتها — الهللة تفرق في ١٢ جرام مو في كيلو."""
    return fmt(value, 0) if abs(value) >= 10_000 else fmt(value, 2)


def clock(moment: datetime) -> str:
    return moment.astimezone(RIYADH).strftime("%H:%M")


def escape(text: str) -> str:
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def fav_karat(context: ContextTypes.DEFAULT_TYPE) -> int:
    return context.chat_data.get(FAV_KARAT, DEFAULT_KARAT)


def chat_currency(context: ContextTypes.DEFAULT_TYPE) -> currencies.Currency:
    code = context.chat_data.get(CURRENCY, currencies.DEFAULT_CODE)
    return currencies.CURRENCIES.get(code, currencies.DEFAULT)


def money(value: Decimal, cur: currencies.Currency) -> str:
    """مبلغ بخانات عملته — الدينار ثلاث خانات والريال خانتان."""
    return fmt(value, cur.decimals)


def money_big(value: Decimal, cur: currencies.Currency) -> str:
    """المبالغ الكبيرة بلا كسور — الهللة تفرق في ١٢ جرام مو في كيلو."""
    return fmt(value, 0) if abs(value) >= 10_000 else money(value, cur)


STATE_FILENAME = "bot_state.pickle"


def state_file() -> Path:
    """مسار ملف حالة البوت — التنبيهات والعيار المفضل والعملة وسجل الإغلاقات.

    داخل دوكر يُضبط `BOT_STATE_DIR=/data` ويُربط بـ volume، وإلا تضيع الحالة
    مع كل تحديث للصورة. وبدون المتغيّر يبقى الملف جنب `bot.py` كما كان.
    """
    directory = os.environ.get("BOT_STATE_DIR", "").strip()
    if not directory:
        return Path(__file__).with_name(STATE_FILENAME)

    path = Path(directory)
    path.mkdir(parents=True, exist_ok=True)
    return path / STATE_FILENAME


def is_private(update: Update) -> bool:
    chat = update.effective_chat
    return chat is not None and chat.type == "private"


def market_state(quote: rates.Quote | None = None) -> market.MarketStatus:
    """حالة السوق — من وقت التسعيرة إن توفّر، وإلا من الجدول."""
    return market.status(quoted_at=quote.quoted_at if quote else None)


def market_line(state: market.MarketStatus) -> str:
    """سطر حالة السوق — مفتوح أو مغلق مع وقت الفتح."""
    if state.is_open:
        return "🟢 السوق مفتوح"
    remaining = market.humanize(state.until_open)
    return f"🔴 السوق مغلق — سعر إغلاق الجمعة • يفتح بعد {remaining}"


async def change_line(
    current: Decimal,
    market_open: bool,
    context: ContextTypes.DEFAULT_TYPE | None = None,
) -> str:
    """سطر التغيّر عن الإغلاق المرجعي، أو فراغ إذا البيانات مو متوفرة."""
    own = context.bot_data.get(OWN_CLOSES) if context else None
    pct = await rates.change_pct(current, market_open, own_closes=own)
    if pct is None:
        return ""
    arrow = "▲" if pct >= 0 else "▼"
    sign = "+" if pct >= 0 else "−"
    label = "اليوم" if market_open else "آخر يوم تداول"
    return f"\n{arrow} {sign}{fmt(abs(pct))}% {label}"


WEEKDAYS_AR = ("الاثنين", "الثلاثاء", "الأربعاء", "الخميس", "الجمعة", "السبت", "الأحد")


def freshness(quote: rates.Quote) -> str:
    """عمر السعر — من وقت التسعيرة الحقيقي إن توفّر، وإلا وقت الجلب.

    لو التسعيرة قديمة نذكر اليوم كمان: «00:00» لحالها توهم إنها ليلة اليوم
    بينما هي إغلاق الجمعة.
    """
    if quote.quoted_at is None:
        return f"جُلب {clock(quote.fetched_at)}"

    age = quote.quote_age
    if age < timedelta(minutes=2):
        return "تسعيرة لحظية"
    if age < timedelta(hours=6):
        return f"تسعيرة {clock(quote.quoted_at)}"

    local = quote.quoted_at.astimezone(RIYADH)
    return f"تسعيرة {WEEKDAYS_AR[local.weekday()]} {local:%H:%M}"


# ---------- مصدر الأسعار ----------


async def get_rate(context: ContextTypes.DEFAULT_TYPE) -> tuple[Decimal, str]:
    """سعر صرف الدولار بعملة المحادثة، مع مصدره."""
    cur = chat_currency(context)

    pinned = context.chat_data.get(PINNED_RATE)
    if pinned is not None:
        return pinned, f"مثبّت يدوياً ({fmt(pinned, 4)})"

    try:
        quote = await rates.usd_to(cur.code)
    except Exception as exc:  # noqa: BLE001
        logger.warning("تعذّر جلب صرف %s: %s", cur.code, exc)
        if cur.peg is None:
            # عملة عائمة أو مربوطة بسلة: رقم مخمَّن يضلّل أكثر مما يفيد
            raise
        return cur.peg, f"الربط الرسمي ({fmt(cur.peg, 4)})"

    note = "قديم" if quote.stale else clock(quote.fetched_at)
    return quote.value, f"{fmt(quote.value, 4)} • {note}"


async def live_or_last(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> Decimal | None:
    """السعر المباشر، وإذا فشل الإنترنت يستخدم آخر سعر في المحادثة."""
    try:
        quote = await rates.gold_usd_per_ounce()
    except Exception:  # noqa: BLE001
        ounce = context.chat_data.get(LAST_OUNCE)
        if ounce is None:
            await update.message.reply_text(
                "❌ ما قدرت أجيب السعر المباشر. أرسل سعر الأونصة يدوياً أول، "
                "مثل: <code>3400</code>",
                parse_mode=ParseMode.HTML,
            )
            return None
        return ounce

    context.chat_data[LAST_OUNCE] = quote.value
    return quote.value


async def resolve_ounce(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> Decimal | None:
    """سعر الأونصة: من وسيط الأمر إن وُجد، وإلا السعر المباشر."""
    if not context.args:
        return await live_or_last(update, context)

    try:
        ounce = parse_number(" ".join(context.args))
    except ParseError as exc:
        await update.message.reply_text(f"⚠️ {escape(str(exc))}")
        return None
    context.chat_data[LAST_OUNCE] = ounce
    return ounce


# ---------- الأسعار المباشرة ----------


def live_keyboard() -> InlineKeyboardMarkup:
    top = [
        InlineKeyboardButton(f"عيار {k}", callback_data=f"karat:{k}") for k in (24, 22, 21, 18)
    ]
    return InlineKeyboardMarkup(
        [
            top,
            [
                InlineKeyboardButton("🧱 الأوزان", callback_data="weights"),
                InlineKeyboardButton("🔄 تحديث", callback_data="refresh"),
            ],
        ]
    )


async def build_live_message(context: ContextTypes.DEFAULT_TYPE) -> str:
    """نص رسالة الأسعار المباشرة لكل العيارات."""
    quote = await rates.gold_usd_per_ounce()
    usd_sar, rate_label = await get_rate(context)
    cur = chat_currency(context)
    context.chat_data[LAST_OUNCE] = quote.value

    favorite = fav_karat(context)
    state = market_state(quote)
    rows = "\n".join(
        f"{'⭐' if karat == favorite else '  '} عيار {karat:>2} │ {money(price, cur):>10} {cur.name}"
        for karat, price in all_prices(quote.value, usd_sar)
    )
    warning = "\n⚠️ <b>تعذّر الوصول للمصادر — السعر قديم</b>" if quote.stale else ""

    return (
        f"<b>🟡 سعر الذهب</b>\n"
        f"{market_line(state)}\n\n"
        f"الأونصة: <b>${fmt(quote.value)}</b>"
        f"{await change_line(quote.value, state.is_open, context)}\n"
        f"<pre>{rows}</pre>"
        f"<i>المصدر: {escape(quote.source)} • {freshness(quote)}</i>\n"
        f"<i>الدولار: {escape(rate_label)}</i>{warning}\n\n"
        f"<i>سعر الذهب الخام — بدون مصنعية أو ضريبة.</i>"
    )


async def now_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = await update.message.reply_text("⏳ أجيب السعر…")
    try:
        text = await build_live_message(context)
    except Exception as exc:  # noqa: BLE001
        logger.error("فشل جلب السعر المباشر: %s", exc)
        await message.edit_text(
            "❌ ما قدرت أوصل لمصادر الأسعار الحين.\n"
            "جرّب بعد شوي، أو أرسل سعر الأونصة يدوياً مثل: <code>3400</code>",
            parse_mode=ParseMode.HTML,
        )
        return
    await message.edit_text(
        text, parse_mode=ParseMode.HTML, reply_markup=live_keyboard()
    )


async def on_button(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    action, _, payload = query.data.partition(":")

    if action == "refresh":
        await query.answer("يتم التحديث…")
        try:
            text = await build_live_message(context)
        except Exception:  # noqa: BLE001
            await query.answer("تعذّر التحديث، جرّب بعد شوي", show_alert=True)
            return
        try:
            await query.edit_message_text(
                text, parse_mode=ParseMode.HTML, reply_markup=live_keyboard()
            )
        except BadRequest as exc:
            # تيليقرام يرفض التعديل إذا النص ما تغيّر — السعر ثابت، مو خطأ
            if "not modified" not in str(exc).lower():
                raise
            await query.answer("السعر ما تغيّر")
        return

    ounce = context.chat_data.get(LAST_OUNCE)
    if ounce is None:
        await query.answer("أرسل /now أول", show_alert=True)
        return

    if action == "karat":
        karat = int(payload)
        usd_sar, _ = await get_rate(context)
        cur = chat_currency(context)
        price = gram_price(ounce, karat, usd_sar)
        await query.answer(f"عيار {karat}: {money(price, cur)} {cur.name} للجرام", show_alert=True)
        return

    if action == "weights":
        await query.answer()
        karat = fav_karat(context)
        usd_sar, _ = await get_rate(context)
        cur = chat_currency(context)
        await query.message.reply_text(
            f"{weights_block(ounce, karat, usd_sar, cur)}"
            f"<i>الأونصة: ${fmt(ounce)} • لعيار ثاني: /bars 24</i>",
            parse_mode=ParseMode.HTML,
        )


# ---------- أوامر العيارات ----------


async def karat_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """يعالج /k24 ... /k9"""
    karat = int(update.message.text.split()[0].lstrip("/").split("@")[0][1:])

    ounce = await resolve_ounce(update, context)
    if ounce is None:
        return

    usd_sar, rate_label = await get_rate(context)

    cur = chat_currency(context)
    price = gram_price(ounce, karat, usd_sar)

    await update.message.reply_text(
        f"<b>عيار {karat}</b>\n"
        f"سعر الجرام: <b>{money(price, cur)}</b> {cur.name}\n\n"
        f"<i>الأونصة: ${fmt(ounce)} • النقاء: {fmt(purity(karat) * 100)}%</i>\n"
        f"<i>الدولار: {escape(rate_label)}</i>",
        parse_mode=ParseMode.HTML,
    )


async def all_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    ounce = await resolve_ounce(update, context)
    if ounce is None:
        return
    await send_all(update, context, ounce)


async def send_all(
    update: Update, context: ContextTypes.DEFAULT_TYPE, ounce: Decimal
) -> None:
    """سعر الجرام لكل العيارات + جدول الأوزان للعيار المفضل."""
    usd_sar, rate_label = await get_rate(context)
    cur = chat_currency(context)
    favorite = fav_karat(context)

    rows = "\n".join(
        f"{'⭐' if karat == favorite else '  '} عيار {karat:>2} │ {money(price, cur):>10} {cur.name}"
        for karat, price in all_prices(ounce, usd_sar)
    )

    await update.message.reply_text(
        f"<b>💱 الأونصة ${fmt(ounce)} = سعر الجرام بـ{cur.name}</b>\n"
        f"<pre>{rows}</pre>"
        f"{weights_block(ounce, favorite, usd_sar)}"
        f"<i>الدولار: {escape(rate_label)} • بدون مصنعية أو ضريبة</i>\n"
        f"<i>لعيار ثاني: /bars 24 • لتغيير عيارك: /fav</i>",
        parse_mode=ParseMode.HTML,
    )


async def gram_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/gram <سعر الأونصة بالدولار> — التحويل اليدوي الصريح."""
    if not context.args:
        await update.message.reply_text(
            f"أعطني سعر الأونصة بالدولار وأحوّله لسعر الجرام بـ{chat_currency(context).name}.\n\n"
            "مثال: <code>/gram 4380</code>\n"
            "<i>أو أرسل الرقم لحاله بدون أمر.</i>\n"
            "<i>للسعر المباشر بدون ما تعطيني رقم: /now</i>",
            parse_mode=ParseMode.HTML,
        )
        return

    try:
        ounce = parse_number(" ".join(context.args))
    except ParseError as exc:
        await update.message.reply_text(f"⚠️ {escape(str(exc))}")
        return

    context.chat_data[LAST_OUNCE] = ounce
    await send_all(update, context, ounce)


async def bars_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/bars [العيار] — أسعار ٥٠ و١٠٠ و٢٥٠ جرام والكيلو بالسعر المباشر."""
    karat = 24  # السبائك عادةً عيار ٢٤
    if context.args:
        try:
            karat = int(parse_number(context.args[0]))
        except (ParseError, ValueError) as exc:
            await update.message.reply_text(f"⚠️ {escape(str(exc))}")
            return
        if karat not in KARATS:
            await update.message.reply_text(
                f"⚠️ العيار لازم يكون واحد من: {', '.join(map(str, KARATS))}"
            )
            return

    ounce = await live_or_last(update, context)
    if ounce is None:
        return

    usd_sar, _ = await get_rate(context)

    cur = chat_currency(context)
    per_gram = gram_price(ounce, karat, usd_sar)

    await update.message.reply_text(
        f"<b>🧱 أسعار الأوزان</b>\n"
        f"{market_line(market_state())}\n\n"
        f"{weights_block(ounce, karat, usd_sar, cur)}"
        f"<i>سعر الجرام: {money(per_gram, cur)} {cur.name} • الأونصة: ${fmt(ounce)}</i>\n"
        f"<i>لعيار ثاني: /bars 21 • لوزن غير هذي: /w 37.5 21</i>",
        parse_mode=ParseMode.HTML,
    )


# ---------- الحاسبات ----------


async def weight_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/w <الوزن> <العيار> — قيمة وزن معيّن بالسعر المباشر."""
    if len(context.args) != 2:
        await update.message.reply_text(
            "الاستخدام: <code>/w الوزن العيار</code>\n"
            "مثال: <code>/w 12.5 21</code> = قيمة ١٢٫٥ جرام عيار ٢١",
            parse_mode=ParseMode.HTML,
        )
        return

    try:
        grams = parse_number(context.args[0])
        karat = int(parse_number(context.args[1]))
    except (ParseError, ValueError) as exc:
        await update.message.reply_text(f"⚠️ {escape(str(exc))}")
        return

    if karat not in KARATS:
        await update.message.reply_text(
            f"⚠️ العيار لازم يكون واحد من: {', '.join(map(str, KARATS))}"
        )
        return

    ounce = await live_or_last(update, context)
    if ounce is None:
        return

    usd_sar, _ = await get_rate(context)

    cur = chat_currency(context)
    total = weight_value(ounce, karat, grams, usd_sar)
    per_gram = gram_price(ounce, karat, usd_sar)

    await update.message.reply_text(
        f"<b>{fmt(grams, 3)} جرام • عيار {karat}</b>\n"
        f"القيمة: <b>{money_big(total, cur)}</b> {cur.name}\n\n"
        f"<i>سعر الجرام: {money(per_gram, cur)} {cur.name} • الأونصة: ${fmt(ounce)}</i>",
        parse_mode=ParseMode.HTML,
    )


async def shop_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/shop <سعر المحل> <العيار> [سبيكة] — يفصل سعر المحل إلى ذهب وضريبة ومصنعية."""
    if len(context.args) < 2:
        await update.message.reply_text(
            "الاستخدام: <code>/shop سعر_الجرام_في_المحل العيار</code>\n"
            "مثال: <code>/shop 600 21</code>\n\n"
            "أفصل لك السعر: كم منه ذهب، وكم ضريبة، وكم مصنعية فعلية.\n"
            "<i>للسبائك أضف كلمة «سبيكة» — الذهب الاستثماري معفى من الضريبة.</i>",
            parse_mode=ParseMode.HTML,
        )
        return

    try:
        shop_price = parse_number(context.args[0])
        karat = int(parse_number(context.args[1]))
    except (ParseError, ValueError) as exc:
        await update.message.reply_text(f"⚠️ {escape(str(exc))}")
        return

    if karat not in KARATS:
        await update.message.reply_text(
            f"⚠️ العيار لازم يكون واحد من: {', '.join(map(str, KARATS))}"
        )
        return

    # السبيكة معفاة فقط إذا بلغ نقاؤها حدّ الذهب الاستثماري
    asked_bullion = any(
        word in " ".join(context.args[2:]) for word in ("سبيكة", "سبائك", "bullion", "bar")
    )
    exempt = asked_bullion and is_investment_purity(karat)

    ounce = await live_or_last(update, context)
    if ounce is None:
        return

    usd_sar, _ = await get_rate(context)

    cur = chat_currency(context)
    market = gram_price(ounce, karat, usd_sar)
    b = shop_breakdown(shop_price, market, vat_applies=not exempt)

    lines = [f"سعر المحل:        <b>{fmt(b.shop_price)}</b>"]
    if b.vat_applies:
        lines.append(f"− ضريبة {int(VAT_RATE * 100)}%:      {fmt(b.vat)}")
        lines.append(f"= قبل الضريبة:    {fmt(b.net_price)}")
    lines.append(f"− ذهب خام:        {fmt(b.market_price)}")

    if b.making >= 0:
        verdict = (
            f"<b>المصنعية الفعلية: {money(b.making, cur)} {cur.name} للجرام</b>\n"
            f"<i>أي {fmt(b.making_pct)}% فوق سعر الذهب</i>"
        )
    else:
        verdict = (
            f"<b>⚠️ أقل من سعر الذهب بـ {money(-b.making, cur)} {cur.name}</b>\n"
            f"<i>تأكد من العيار — أو يمكن يكون سعر شراء لا بيع.</i>"
        )

    if exempt:
        note = "<i>سبيكة عيار 24: معفاة من الضريبة (ذهب استثماري نقاء ٩٩٪+).</i>"
    elif asked_bullion:
        note = (
            f"<i>عيار {karat} ما يبلغ نقاء ٩٩٪، فما ينطبق عليه إعفاء الذهب "
            f"الاستثماري ولو كان سبيكة.</i>"
        )
    else:
        note = "<i>حسبتها كمشغولات (عليها ضريبة). للسبائك أضف كلمة «سبيكة».</i>"

    body = "\n".join(lines)
    await update.message.reply_text(
        f"<b>🏪 تفكيك سعر عيار {karat}</b>\n"
        f"<pre>{body}</pre>"
        f"{verdict}\n\n"
        f"{note}",
        parse_mode=ParseMode.HTML,
    )


async def ounce_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/ounce <سعر الجرام> <العيار> — العملية العكسية."""
    if len(context.args) != 2:
        await update.message.reply_text(
            "الاستخدام: <code>/ounce سعر_الجرام العيار</code>\n"
            "مثال: <code>/ounce 358.68 21</code>\n"
            "يرجّع سعر الأونصة بالدولار المكافئ لسعر الجرام هذا.",
            parse_mode=ParseMode.HTML,
        )
        return

    try:
        gram_sar = parse_number(context.args[0])
        karat = int(parse_number(context.args[1]))
    except (ParseError, ValueError) as exc:
        await update.message.reply_text(f"⚠️ {escape(str(exc))}")
        return

    if karat not in KARATS:
        await update.message.reply_text(
            f"⚠️ العيار لازم يكون واحد من: {', '.join(map(str, KARATS))}"
        )
        return

    usd_sar, _ = await get_rate(context)

    cur = chat_currency(context)
    ounce = ounce_from_gram(gram_sar, karat, usd_sar)

    await update.message.reply_text(
        f"<b>العملية العكسية</b>\n"
        f"جرام عيار {karat} بـ {money(gram_sar, cur)} {cur.name}\n"
        f"= أونصة بـ <b>${fmt(ounce)}</b>",
        parse_mode=ParseMode.HTML,
    )


async def zakat_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/zakat <الوزن> [العيار] — زكاة الذهب."""
    if not context.args:
        await update.message.reply_text(
            "الاستخدام: <code>/zakat الوزن [العيار]</code>\n"
            "مثال: <code>/zakat 100 21</code>\n\n"
            f"<i>النصاب {ZAKAT_NISAB_GRAMS_24K} جرام ذهب خالص، والمقدار ٢٫٥٪ "
            f"بعد حَوَلان الحول.</i>",
            parse_mode=ParseMode.HTML,
        )
        return

    try:
        grams = parse_number(context.args[0])
        karat = int(parse_number(context.args[1])) if len(context.args) > 1 else fav_karat(context)
    except (ParseError, ValueError) as exc:
        await update.message.reply_text(f"⚠️ {escape(str(exc))}")
        return

    if karat not in KARATS:
        await update.message.reply_text(
            f"⚠️ العيار لازم يكون واحد من: {', '.join(map(str, KARATS))}"
        )
        return

    ounce = await live_or_last(update, context)
    if ounce is None:
        return

    usd_sar, _ = await get_rate(context)

    cur = chat_currency(context)
    reached, pure_grams, value, due = zakat(ounce, karat, grams, usd_sar)

    if reached:
        verdict = (
            f"✅ بلغ النصاب\n"
            f"الزكاة: <b>{money_big(due, cur)}</b> {cur.name}\n"
            f"<i>(٢٫٥٪ من {money_big(value, cur)} {cur.name})</i>"
        )
    else:
        short = ZAKAT_NISAB_GRAMS_24K - pure_grams
        verdict = (
            f"❌ ما بلغ النصاب\n"
            f"<i>ينقصه {fmt(short, 3)} جرام ذهب خالص</i>"
        )

    await update.message.reply_text(
        f"<b>زكاة الذهب</b>\n"
        f"{fmt(grams, 3)} جرام عيار {karat}\n"
        f"= {fmt(pure_grams, 3)} جرام ذهب خالص\n"
        f"القيمة: {money_big(value, cur)} {cur.name}\n\n"
        f"{verdict}\n\n"
        f"<i>النصاب {ZAKAT_NISAB_GRAMS_24K} جرام خالص، والزكاة تجب بعد حَوَلان الحول. "
        f"هذا حساب تقريبي — للفتوى راجع أهل العلم.</i>",
        parse_mode=ParseMode.HTML,
    )


# ---------- التنبيهات ----------


async def alert_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/alert <السعر> [العيار] — نبّهني لما يوصل الجرام لهذا السعر."""
    if not context.args:
        await show_alerts(update, context)
        return

    try:
        target = parse_number(context.args[0])
        karat = int(parse_number(context.args[1])) if len(context.args) > 1 else fav_karat(context)
    except (ParseError, ValueError) as exc:
        await update.message.reply_text(f"⚠️ {escape(str(exc))}")
        return

    if karat not in KARATS:
        await update.message.reply_text(
            f"⚠️ العيار لازم يكون واحد من: {', '.join(map(str, KARATS))}"
        )
        return

    ounce = await live_or_last(update, context)
    if ounce is None:
        return

    usd_sar, _ = await get_rate(context)

    cur = chat_currency(context)
    current = gram_price(ounce, karat, usd_sar)
    direction = alerts_store.direction_for(target, current)

    alert = Alert(
        chat_id=update.effective_chat.id,
        karat=karat,
        target=target,
        direction=direction,
        currency=cur.code,
    )
    if not alerts_store.add(context.bot_data, alert):
        await update.message.reply_text(
            f"⚠️ وصلت الحد الأقصى ({alerts_store.MAX_PER_CHAT} تنبيهات). "
            f"احذف واحداً بـ /alerts أول."
        )
        return

    word = "يطلع فوق" if direction == alerts_store.ABOVE else "ينزل تحت"
    await update.message.reply_text(
        f"🔔 <b>تم</b>\n"
        f"أنبّهك لما جرام عيار {karat} {word} <b>{money(target, cur)}</b> {cur.name}.\n\n"
        f"<i>السعر الآن: {money(current, cur)} {cur.name} • أفحص كل "
        f"{alerts_store.CHECK_INTERVAL_SECONDS // 60} دقائق</i>",
        parse_mode=ParseMode.HTML,
    )


async def show_alerts(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/alerts — عرض التنبيهات."""
    mine = alerts_store.for_chat(context.bot_data, update.effective_chat.id)
    if not mine:
        await update.message.reply_text(
            "ما عندك تنبيهات.\n\n"
            "لإضافة واحد: <code>/alert 460 21</code>\n"
            f"<i>ينبّهك لما جرام عيار ٢١ يوصل ٤٦٠ {chat_currency(context).name} — طالعاً أو نازلاً.</i>",
            parse_mode=ParseMode.HTML,
        )
        return

    rows = "\n".join(
        f"{i}. {a.arrow} {a.label}" for i, a in enumerate(mine, start=1)
    )
    await update.message.reply_text(
        f"🔔 <b>تنبيهاتك</b>\n{rows}\n\n"
        f"<code>/unalert 1</code> — حذف واحد\n"
        f"<code>/unalert all</code> — حذف الكل",
        parse_mode=ParseMode.HTML,
    )


async def unalert_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id
    if not context.args:
        await update.message.reply_text(
            "الاستخدام: <code>/unalert 1</code> أو <code>/unalert all</code>",
            parse_mode=ParseMode.HTML,
        )
        return

    if context.args[0].lower() in ("all", "الكل"):
        count = alerts_store.remove_all(context.bot_data, chat_id)
        await update.message.reply_text(f"🗑 حذفت {count} تنبيه.")
        return

    try:
        index = int(parse_number(context.args[0]))
    except (ParseError, ValueError):
        await update.message.reply_text("⚠️ أعطني رقم التنبيه من /alerts")
        return

    removed = alerts_store.remove(context.bot_data, chat_id, index)
    if removed is None:
        await update.message.reply_text("⚠️ ما فيه تنبيه بهذا الرقم. شف /alerts")
        return
    await update.message.reply_text(f"🗑 حذفت: {removed.label}")


async def check_alerts(context: ContextTypes.DEFAULT_TYPE) -> None:
    """مهمة دورية — تفحص التنبيهات وترسل اللي تحقق."""
    if not context.bot_data.get("alerts"):
        return
    if not market.status().is_open:
        return  # الجدول يقول مغلق — نوفّر الطلب أصلاً

    try:
        quote = await rates.gold_usd_per_ounce()
    except Exception as exc:  # noqa: BLE001
        logger.warning("فحص التنبيهات: تعذّر جلب السعر (%s)", exc)
        return
    if quote.stale:
        return

    # فحص ثانٍ بعد الجلب: التسعيرة نفسها أصدق من الجدول (عطلة رسمية مثلاً).
    # بدونه يمكن ينطلق تنبيه على سعر مجمّد ما تحرّك أصلاً.
    if not market_state(quote).is_open:
        return

    try:
        fx = await rates.usd_to_sar()
        usd_sar = fx.value
    except Exception:  # noqa: BLE001
        usd_sar = DEFAULT_USD_SAR

    # سعر الصرف بالدولار لكل عملة نحتاجها — التنبيه يُرسل بعملة صاحبه
    chat_rates: dict[str, Decimal] = {}
    for pending in list(context.bot_data.get("alerts", [])):
        code = pending.currency or currencies.DEFAULT_CODE
        if code in chat_rates:
            continue
        try:
            chat_rates[code] = (await rates.usd_to(code)).value
        except Exception:  # noqa: BLE001
            fallback = currencies.CURRENCIES.get(code, currencies.DEFAULT).peg
            if fallback is None:
                continue  # عملة عائمة وما قدرنا نجيب سعرها: نأجّل التنبيه
            chat_rates[code] = fallback

    def price_for(alert) -> Decimal | None:
        rate = chat_rates.get(alert.currency or currencies.DEFAULT_CODE)
        if rate is None:
            return None
        return gram_price(quote.value, alert.karat, rate)

    fired = alerts_store.pop_triggered(context.bot_data, price_for)

    for alert, price in fired:
        cur = currencies.CURRENCIES.get(
            alert.currency or currencies.DEFAULT_CODE, currencies.DEFAULT
        )
        try:
            await context.bot.send_message(
                chat_id=alert.chat_id,
                text=(
                    f"🔔 <b>تنبيه سعر</b>\n"
                    f"{alert.arrow} جرام عيار {alert.karat} صار <b>{money(price, cur)}</b> {cur.name}\n"
                    f"<i>هدفك كان {money(alert.target, cur)} {cur.name}</i>\n\n"
                    f"<i>الأونصة: ${fmt(quote.value)}</i>"
                ),
                parse_mode=ParseMode.HTML,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("تعذّر إرسال تنبيه للمحادثة %s: %s", alert.chat_id, exc)


# ---------- المراقبة والسجل ----------


async def health_check(context: ContextTypes.DEFAULT_TYPE) -> None:
    """يراقب أي مصدر يشتغل فعلاً، وينبّه المالك عند تغيّر الحالة."""
    if not OWNER_ID:
        return

    monitor = context.bot_data.get(MONITOR)
    if monitor is None:
        monitor = health.Monitor(primary=rates.SPOT_SOURCES[0][0])
        context.bot_data[MONITOR] = monitor

    try:
        quote = await rates.gold_usd_per_ounce()
        report = monitor.observe(quote.source, stale=quote.stale)
    except Exception:  # noqa: BLE001 — الفشل التام حالة نراقبها لا خطأ نخفيه
        report = monitor.observe(None)

    if report is None:
        return

    try:
        await context.bot.send_message(chat_id=OWNER_ID, text=report)
    except Exception as exc:  # noqa: BLE001
        logger.warning("تعذّر تنبيه المالك: %s", exc)


async def record_close(context: ContextTypes.DEFAULT_TYPE) -> None:
    """يسجّل سعر الإغلاق يومياً من نفس المصادر التي يعرضها البوت.

    يعطينا تاريخاً مستقلاً عن أي طرف ثالث، ويكشف انحراف أي مصدر لاحقاً.
    """
    if not market.status().is_open:
        return  # عطلة نهاية الأسبوع: لا إغلاق جديد

    try:
        quote = await rates.gold_usd_per_ounce()
    except Exception as exc:  # noqa: BLE001
        logger.warning("تعذّر تسجيل الإغلاق: %s", exc)
        return
    if quote.stale:
        return

    closes = context.bot_data.setdefault(OWN_CLOSES, {})
    today = datetime.now(timezone.utc).date().isoformat()
    closes[today] = str(quote.value)

    # نحتفظ بآخر ٤٠٠ يوم — يكفي لأي مقارنة سنوية بلا تضخّم
    for old in sorted(closes)[:-400]:
        del closes[old]

    logger.info("سُجّل إغلاق %s: %s من %s", today, quote.value, quote.source)


# ---------- الإعدادات ----------


async def currency_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/currency [رمز] — عملة العرض لهذي المحادثة."""
    current = chat_currency(context)

    if not context.args:
        rows = "\n".join(
            f"{'✅' if c.code == current.code else '  '} {c.flag} {c.code} — {c.name}"
            for c in currencies.CURRENCIES.values()
        )
        await update.message.reply_text(
            f"<b>عملتك: {current.label}</b>\n\n{rows}\n\n"
            f"للتغيير: <code>/currency AED</code> أو <code>/currency درهم</code>",
            parse_mode=ParseMode.HTML,
        )
        return

    chosen = currencies.resolve(" ".join(context.args))
    if chosen is None:
        codes = " · ".join(currencies.CURRENCIES)
        await update.message.reply_text(
            f"⚠️ ما عرفت هذي العملة.\nالمتاح: <code>{codes}</code>",
            parse_mode=ParseMode.HTML,
        )
        return

    context.chat_data[CURRENCY] = chosen.code
    context.chat_data.pop(PINNED_RATE, None)  # السعر المثبّت كان لعملة ثانية

    try:
        rate, label = await get_rate(context)
    except Exception:  # noqa: BLE001
        await update.message.reply_text(
            f"✅ عملتك صارت {chosen.label}\n"
            f"⚠️ لكن ما قدرت أجيب سعر صرفها الآن — جرّب بعد شوي.",
            parse_mode=ParseMode.HTML,
        )
        return

    await update.message.reply_text(
        f"✅ عملتك صارت <b>{chosen.label}</b>\n"
        f"<i>1 دولار = {fmt(rate, 4)} {chosen.name} • {escape(label)}</i>",
        parse_mode=ParseMode.HTML,
    )


async def fav_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/fav <العيار> — العيار المفضل، يُستخدم كافتراضي في باقي الأوامر."""
    if not context.args:
        await update.message.reply_text(
            f"عيارك المفضل: <b>{fav_karat(context)}</b>\n"
            f"لتغييره: <code>/fav 22</code>",
            parse_mode=ParseMode.HTML,
        )
        return

    try:
        karat = int(parse_number(context.args[0]))
    except (ParseError, ValueError) as exc:
        await update.message.reply_text(f"⚠️ {escape(str(exc))}")
        return

    if karat not in KARATS:
        await update.message.reply_text(
            f"⚠️ العيار لازم يكون واحد من: {', '.join(map(str, KARATS))}"
        )
        return

    context.chat_data[FAV_KARAT] = karat
    await update.message.reply_text(
        f"⭐ عيارك المفضل صار <b>{karat}</b>.\n"
        f"<i>الحين /zakat و/alert يستخدمونه تلقائياً.</i>",
        parse_mode=ParseMode.HTML,
    )


async def my_karat(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """زر «عياري» — سعر العيار المفضل مباشرة."""
    karat = fav_karat(context)
    ounce = await live_or_last(update, context)
    if ounce is None:
        return

    usd_sar, _ = await get_rate(context)

    cur = chat_currency(context)
    price = gram_price(ounce, karat, usd_sar)

    await update.message.reply_text(
        f"⭐ <b>عيار {karat}</b>\n"
        f"<b>{money(price, cur)}</b> {cur.name} للجرام\n"
        f"{market_line(market_state())}\n\n"
        f"<i>الأونصة: ${fmt(ounce)}</i>\n"
        f"<i>لتغيير عيارك: /fav</i>",
        parse_mode=ParseMode.HTML,
    )


# ---------- سعر الصرف ----------


async def rate_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    cur = chat_currency(context)
    if not context.args:
        value, label = await get_rate(context)
        await update.message.reply_text(
            f"سعر الصرف الحالي: <b>{fmt(value, 4)}</b> {cur.name} للدولار\n"
            f"المصدر: {escape(label)}\n\n"
            f"<code>/rate auto</code> — تلقائي من الإنترنت\n"
            f"<code>/rate 3.7502</code> — تثبيت قيمة يدوياً",
            parse_mode=ParseMode.HTML,
        )
        return

    choice = context.args[0].lower()
    if choice in ("auto", "تلقائي", "reset"):
        context.chat_data.pop(PINNED_RATE, None)
        value, label = await get_rate(context)
        await update.message.reply_text(
            f"✅ سعر الصرف صار تلقائي: <b>{fmt(value, 4)}</b>\n<i>{escape(label)}</i>",
            parse_mode=ParseMode.HTML,
        )
        return

    try:
        new_rate = parse_number(context.args[0])
    except ParseError as exc:
        await update.message.reply_text(f"⚠️ {escape(str(exc))}")
        return

    context.chat_data[PINNED_RATE] = new_rate
    await update.message.reply_text(
        f"✅ ثبّتُّ سعر الصرف على <b>{fmt(new_rate, 4)}</b> {cur.name} للدولار\n"
        f"<i>للرجوع للتلقائي: /rate auto</i>",
        parse_mode=ParseMode.HTML,
    )


async def sources_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """حالة مصادر البيانات."""
    lines = []
    gold_quote = None
    try:
        gold_quote = await rates.gold_usd_per_ounce()
        mark = "⚠️ قديم" if gold_quote.stale else "✅"
        lines.append(
            f"{mark} الذهب: ${fmt(gold_quote.value)} — "
            f"{escape(gold_quote.source)} • {freshness(gold_quote)}"
        )
    except Exception as exc:  # noqa: BLE001
        lines.append(f"❌ الذهب: كل المصادر فشلت ({escape(str(exc)[:80])})")

    try:
        fx = await rates.usd_to_sar()
        mark = "⚠️ قديم" if fx.stale else "✅"
        lines.append(
            f"{mark} الدولار: {fmt(fx.value, 4)} — "
            f"{escape(fx.source)} ({clock(fx.fetched_at)})"
        )
    except Exception as exc:  # noqa: BLE001
        lines.append(f"❌ الدولار: كل المصادر فشلت ({escape(str(exc)[:80])})")

    state = market_state(gold_quote)
    if gold_quote is not None and gold_quote.quoted_at is not None:
        lines.append(
            f"🕒 آخر تسعيرة من السوق: {clock(gold_quote.quoted_at)} "
            f"({gold_quote.quoted_at.astimezone(RIYADH).strftime('%Y-%m-%d')})"
        )

    own = context.bot_data.get(OWN_CLOSES, {})
    closes = rates.merge_closes(own, await rates.daily_closes())
    if closes:
        lines.append(
            f"📅 إغلاقات يومية: {len(closes)} "
            f"(منها {len(own)} من تسجيل البوت نفسه)"
        )

    monitor = context.bot_data.get(MONITOR)
    if monitor is not None:
        lines.append(f"💚 صحة المصادر: {monitor.state.value} منذ {clock(monitor.since)}")

    order = " ← ".join(name for name, _url, _parser in rates.SPOT_SOURCES)
    body = "\n".join(lines)
    await update.message.reply_text(
        f"<b>حالة المصادر</b>\n{market_line(state)}\n"
        f"<i>الحالة مبنية على {state.basis}</i>\n\n{body}\n\n"
        f"<i>ترتيب مصادر الذهب: {escape(order)}</i>\n"
        f"<i>كلها سوق فوري، والسعر يُجلب مع كل طلب بدون تخزين.</i>\n"
        f"<i>الوقت بتوقيت الرياض.</i>",
        parse_mode=ParseMode.HTML,
    )


# ---------- رسائل عامة ----------


async def plain_number(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """نص عادي: زر من اللوحة، أو رقم = سعر أونصة يدوي."""
    text = update.message.text.strip()

    if text == BTN_NOW:
        return await now_command(update, context)
    if text == BTN_MY_KARAT:
        return await my_karat(update, context)
    if text == BTN_ALERTS:
        return await show_alerts(update, context)
    if text == BTN_HELP:
        return await help_command(update, context)
    if text == BTN_BARS:
        context.args = []
        return await bars_command(update, context)
    if text == BTN_WEIGHT:
        return await update.message.reply_text(
            "اكتب الوزن والعيار بأي صيغة، مثل:\n"
            "• <code>12 جرام عيار 21</code>\n"
            "• <code>نص كيلو 24</code>\n"
            "• <code>250 قرام</code>\n\n"
            f"<i>إذا ما ذكرت العيار أستخدم عيارك المفضل ({fav_karat(context)}).</i>",
            parse_mode=ParseMode.HTML,
        )
    if text == BTN_SHOP:
        return await update.message.reply_text(
            "أرسل سعر الجرام في المحل والعيار:\n"
            "<code>/shop 480 21</code>\n\n"
            "<i>أقارنه بسعر السوق وأطلع لك المصنعية.</i>",
            parse_mode=ParseMode.HTML,
        )

    await interpret(update, context, text)


async def interpret(
    update: Update, context: ContextTypes.DEFAULT_TYPE, text: str
) -> None:
    """يفهم الكلام الطبيعي: «12 جرام عيار 21»، «نص كيلو»، «كم عيار 22»، «4380»."""
    query = nlp.understand(text)
    if query.is_empty:
        return  # مو موجّه لنا، نتجاهله بصمت

    # رقم لحاله = سعر أونصة يدوي
    if query.ounce_usd is not None and query.grams is None and query.karat is None:
        context.chat_data[LAST_OUNCE] = query.ounce_usd
        return await send_all(update, context, query.ounce_usd)

    ounce = query.ounce_usd
    if ounce is None:
        ounce = await live_or_last(update, context)
        if ounce is None:
            return
    else:
        context.chat_data[LAST_OUNCE] = ounce

    usd_sar, _ = await get_rate(context)

    cur = chat_currency(context)
    karat = query.karat or fav_karat(context)

    # وزن مذكور → قيمته
    if query.grams is not None:
        total = weight_value(ounce, karat, query.grams, usd_sar)
        per_gram = gram_price(ounce, karat, usd_sar)
        guessed = "" if query.karat else f"\n<i>افترضت عيار {karat} — غيّره بـ /fav</i>"
        return await update.message.reply_text(
            f"<b>{weight_label(query.grams)} • عيار {karat}</b>\n"
            f"<b>{money_big(total, cur)}</b> {cur.name}\n\n"
            f"<i>سعر الجرام: {money(per_gram, cur)} {cur.name} • الأونصة: ${fmt(ounce)}</i>{guessed}",
            parse_mode=ParseMode.HTML,
        )

    # عيار مذكور بدون وزن → سعر جرامه + جدول الأوزان
    if query.karat is not None:
        per_gram = gram_price(ounce, karat, usd_sar)
        return await update.message.reply_text(
            f"<b>عيار {karat}</b>\n"
            f"<b>{money(per_gram, cur)}</b> {cur.name} للجرام\n"
            f"{market_line(market_state())}\n\n"
            f"{weights_block(ounce, karat, usd_sar, cur)}"
            f"<i>الأونصة: ${fmt(ounce)}</i>",
            parse_mode=ParseMode.HTML,
        )

    # سأل عن السعر بدون تفاصيل → الجدول الكامل
    if query.asks_price:
        return await now_command(update, context)


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "<b>أهلاً 👋</b>\n"
        f"أعطيك سعر الذهب بـ{chat_currency(context).name} — بكل بساطة.\n\n"
        "<b>اكتب لي عادي، وأنا أفهمك:</b>\n"
        "• <code>12 جرام عيار 21</code> ← قيمة الوزن\n"
        "• <code>نص كيلو</code> ← قيمة نص كيلو\n"
        "• <code>كم سعر عيار 22</code> ← سعر الجرام\n"
        "• <code>4380</code> ← أحوّل سعر الأونصة لسعر الجرام\n\n"
        "أو استخدم الأزرار تحت 👇\n\n"
        f"<i>عيارك الافتراضي {DEFAULT_KARAT} — غيّره بـ /fav</i>\n"
        "<i>/help لكل الميزات</i>",
        parse_mode=ParseMode.HTML,
        reply_markup=main_keyboard(private=is_private(update)),
    )

    if not is_private(update):
        return  # أزرار الويب لا تعمل في المجموعات

    if (markup := app_button()) is not None:
        await update.message.reply_text(
            "🌐 <b>الحاسبة المرئية</b>\n"
            "كل العيارات والأوزان في شاشة وحدة — تفتح داخل تيليقرام بدون ما تطلع منه.",
            parse_mode=ParseMode.HTML,
            reply_markup=markup,
        )


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    karat_rows = "\n".join(
        f"/k{k:<3} عيار {k:>2} — نقاء {fmt(purity(k) * 100)}%" for k in KARATS
    )
    await update.message.reply_text(
        "<b>اكتب عادي بدون أوامر</b>\n"
        "<code>12 جرام عيار 21</code> • <code>نص كيلو 24</code> • "
        "<code>250 قرام</code> • <code>كم سعر عيار 22</code> • <code>4380</code>\n\n"
        "<b>الأسعار</b>\n"
        "/now — كل العيارات بالسعر المباشر، مع زر تحديث\n"
        "<code>/gram 4380</code> — تعطيني سعر الأونصة وأعطيك سعر الجرام\n"
        "<code>/bars</code> — أسعار ٥٠ و١٠٠ و٢٥٠ جرام والكيلو (عيار ٢٤)\n"
        "<code>/bars 21</code> — نفس الأوزان بعيار ثاني\n"
        "/sources — حالة المصادر وحالة السوق\n\n"
        "<b>العيارات</b>\n"
        f"<pre>{karat_rows}</pre>\n"
        "الأمر بدون رقم = السعر المباشر.\n"
        "الأمر مع رقم = <code>/k21 3400</code> يحسب على أونصة بـ$3400.\n\n"
        "<b>الحاسبات</b>\n"
        "<code>/w 12.5 21</code> — قيمة ١٢٫٥ جرام عيار ٢١\n"
        "<code>/shop 420 21</code> — يقارن سعر المحل بالسوق\n"
        "<code>/zakat 100 21</code> — زكاة الذهب (نصاب ٨٥ جرام، ٢٫٥٪)\n"
        "<code>/ounce 358.68 21</code> — من سعر الجرام لسعر الأونصة\n\n"
        "<b>في أي محادثة</b>\n"
        "اكتب <code>@اسم_البوت 12 جرام 21</code> في أي قروب أو خاص، "
        "واختر النتيجة لترسلها — بدون ما تفتح البوت.\n\n"
        "<b>التنبيهات</b>\n"
        "<code>/alert 460 21</code> — نبّهني لما يوصل الجرام ٤٦٠ ريال\n"
        "/alerts — تنبيهاتي • <code>/unalert 1</code> — حذف واحد\n"
        f"<i>يفحص كل {alerts_store.CHECK_INTERVAL_SECONDS // 60} دقائق، "
        f"والتنبيه يُرسل مرة وحدة ثم ينحذف.</i>\n\n"
        "<b>الإعدادات</b>\n"
        "<code>/fav 22</code> — عيارك المفضل (الافتراضي في باقي الأوامر)\n"
        "<code>/currency AED</code> — عملة العرض (٩ عملات)\n"
        "<code>/rate auto</code> — سعر الصرف تلقائي من الإنترنت\n"
        "<code>/rate 3.7502</code> — تثبيته يدوياً\n\n"
        "<b>عن الأسعار</b>\n"
        "• تُجلب من السوق مع كل رسالة — بدون تخزين\n"
        "• سوق الذهب يقفل من الجمعة مساءً للأحد مساءً — والسعر يتجمّد عند الإغلاق\n"
        "• المصدر يحدّث كل بضع دقائق، فالسعر متأخر دقائق عن التداول اللحظي\n\n"
        "<b>الدقة</b>\n"
        f"• الأونصة التروية = <code>{TROY_OUNCE_GRAMS}</code> جرام (التعريف الرسمي)\n"
        "• النقاء كسر دقيق <code>العيار ÷ 24</code>\n"
        "• حساب عشري كامل، والتقريب في آخر خطوة فقط\n"
        "• يقبل الأرقام العربية <code>٣٤٠٠</code>\n\n"
        "<b>تنبيه</b>\n"
        "الأسعار للذهب الخام من مصادر عامة، للاسترشاد فقط — "
        "بدون مصنعية أو ضريبة، وقد تختلف عن سعر السوق المحلي.",
        parse_mode=ParseMode.HTML,
        reply_markup=main_keyboard(private=is_private(update)),
    )


async def about_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/about — نبذة وروابط."""
    link = f"\n\n🌐 صفحة الشرح والحاسبة:\n{SITE_URL}" if SITE_URL else ""
    await update.message.reply_text(
        "<b>🪙 بوت أسعار الذهب</b>\n"
        "سعر جرام الذهب بالريال السعودي لكل العيارات، بأسعار السوق.\n\n"
        "<b>الحساب</b>\n"
        f"<code>الأونصة × سعر الصرف ÷ {TROY_OUNCE_GRAMS} × (العيار ÷ 24)</code>\n"
        "حساب عشري بدقة ٣٤ خانة، والتقريب في آخر خطوة فقط.\n\n"
        "<b>المصادر</b>\n"
        "مصدران لسعر الذهب ومصدران لسعر الصرف، مع تحويل تلقائي عند فشل أحدها.\n\n"
        "<i>الأسعار للذهب الخام من مصادر عامة، للاسترشاد فقط — بدون مصنعية أو ضريبة، "
        "وليست نصيحة استثمارية.</i>"
        f"{link}",
        parse_mode=ParseMode.HTML,
        reply_markup=(
            app_button("🌐 افتح الحاسبة")
            if is_private(update)
            else site_button("🌐 افتح الصفحة")
        ),
        disable_web_page_preview=True,
    )


# ---------- الوضع المضمّن ----------


async def inline_query(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """‏@البوت داخل أي محادثة: يفهم نفس لغة الرسائل العادية.

    الوضع المضمّن لا يملك chat_data، فالعملة والعيار افتراضيان دائماً —
    ولهذا كل نتيجة تذكر عيارها وعملتها صراحةً.
    """
    query = (update.inline_query.query or "").strip()

    try:
        quote = await rates.gold_usd_per_ounce()
        usd_rate = await rates.usd_to(currencies.DEFAULT_CODE)
    except Exception as exc:  # noqa: BLE001
        logger.warning("الوضع المضمّن: تعذّر جلب السعر (%s)", exc)
        return await update.inline_query.answer([], cache_time=10)

    cur = currencies.DEFAULT
    ounce, usd_sar = quote.value, usd_rate.value
    state = market_state(quote)
    footer = market_line(state)

    parsed = nlp.understand(query) if query else nlp.Query()
    karat = parsed.karat or DEFAULT_KARAT
    results = []

    if parsed.grams is not None:
        total = weight_value(ounce, karat, parsed.grams, usd_sar)
        results.append(
            InlineQueryResultArticle(
                id=f"w{karat}",
                title=f"{weight_label(parsed.grams)} عيار {karat}",
                description=f"{money_big(total, cur)} {cur.name}",
                input_message_content=InputTextMessageContent(
                    f"<b>{weight_label(parsed.grams)} • عيار {karat}</b>\n"
                    f"<b>{money_big(total, cur)}</b> {cur.name}\n\n"
                    f"<i>سعر الجرام: {money(gram_price(ounce, karat, usd_sar), cur)} "
                    f"{cur.name} • الأونصة: ${fmt(ounce)}</i>\n<i>{footer}</i>",
                    parse_mode=ParseMode.HTML,
                ),
            )
        )

    # جدول كامل دائماً كخيار أول أو أخير
    rows = "\n".join(
        f"عيار {k:>2} │ {money(p, cur):>10} {cur.name}"
        for k, p in all_prices(ounce, usd_sar)
    )
    results.append(
        InlineQueryResultArticle(
            id="all",
            title="كل العيارات",
            description=f"الأونصة ${fmt(ounce)} • عيار 21: "
            f"{money(gram_price(ounce, 21, usd_sar), cur)}",
            input_message_content=InputTextMessageContent(
                f"<b>🟡 سعر جرام الذهب</b>\n<pre>{rows}</pre>"
                f"<i>الأونصة: ${fmt(ounce)} • {escape(quote.source)}</i>\n"
                f"<i>{footer}</i>",
                parse_mode=ParseMode.HTML,
            ),
        )
    )

    for k in (24, 22, 21, 18):
        price = gram_price(ounce, k, usd_sar)
        results.append(
            InlineQueryResultArticle(
                id=f"k{k}",
                title=f"عيار {k}",
                description=f"{money(price, cur)} {cur.name} للجرام",
                input_message_content=InputTextMessageContent(
                    f"<b>عيار {k}</b>\n<b>{money(price, cur)}</b> {cur.name} للجرام\n\n"
                    f"<i>الأونصة: ${fmt(ounce)}</i>\n<i>{footer}</i>",
                    parse_mode=ParseMode.HTML,
                ),
            )
        )

    # السوق مقفل = السعر مجمّد، فالتخزين أطول بلا ضرر
    await update.inline_query.answer(
        results, cache_time=300 if not state.is_open else 30, is_personal=False
    )


async def on_error(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    """أخطاء الشبكة العابرة تُسجَّل بسطر، والأخطاء الحقيقية بتتبّع كامل."""
    error = context.error

    if isinstance(error, NetworkError):
        # تعذّر الوصول لتيليقرام. صفحة تتبّع كاملة تغرق السجل بلا فائدة،
        # ومحاولة الاعتذار للمستخدم راح تفشل بنفس السبب.
        logger.warning("تعذّر الاتصال بتيليقرام: %s", error)
        return

    logger.error("خطأ أثناء المعالجة:", exc_info=error)
    if isinstance(update, Update) and update.effective_message:
        try:
            await update.effective_message.reply_text(
                "❌ صار خطأ غير متوقع. جرّب مرة ثانية."
            )
        except TelegramError as exc:
            logger.warning("وتعذّر إبلاغ المستخدم كمان: %s", exc)


async def post_init(application: Application) -> None:
    await application.bot.set_my_commands(
        [
            BotCommand("now", "💰 السعر الآن"),
            BotCommand("gram", "💱 من سعر الأونصة لسعر الجرام"),
            BotCommand("bars", "🧱 أسعار ٥٠ و١٠٠ و٢٥٠ جرام والكيلو"),
            BotCommand("w", "🧮 قيمة وزن معيّن"),
            BotCommand("shop", "🏪 قارن سعر المحل بالسوق"),
            BotCommand("alert", "🔔 نبّهني عند سعر معيّن"),
            BotCommand("alerts", "قائمة تنبيهاتي"),
            BotCommand("zakat", "🕌 زكاة الذهب"),
            BotCommand("fav", "⭐ عياري المفضل"),
            BotCommand("currency", "💱 عملة العرض"),
            BotCommand("ounce", "من الجرام إلى الأونصة"),
            BotCommand("rate", "سعر صرف الدولار"),
            BotCommand("sources", "حالة المصادر والسوق"),
            BotCommand("about", "🌐 عن البوت وصفحة الشرح"),
            BotCommand("help", "المساعدة"),
        ]
    )
    # نُبقي زر القائمة على الأوامر عمداً: لو خليناه تطبيق ويب تختفي قائمة
    # الأوامر الـ١٤ من الواجهة، والحاسبة موجودة أصلاً كزر في اللوحة.
    await application.bot.set_chat_menu_button(menu_button=MenuButtonCommands())
    await application.bot.set_my_short_description("سعر جرام الذهب بالريال، لكل العيارات، مباشر.")
    await application.bot.set_my_description(
        "أعطيك سعر جرام الذهب بالريال السعودي لكل العيارات (24، 22، 21، 18، 14، 12، 10، 9) "
        "بأسعار مباشرة من السوق. اضغط /now للبداية."
    )


def main() -> None:
    # يقرأ ملف .env من جنب bot.py (متغيّرات البيئة الموجودة أصلاً لها الأولوية)
    load_dotenv(Path(__file__).with_name(".env"))

    global SITE_URL, OWNER_ID
    SITE_URL = os.environ.get("SITE_URL", "").strip()
    raw_owner = os.environ.get("OWNER_ID", "").strip()
    if raw_owner:
        try:
            OWNER_ID = int(raw_owner)
        except ValueError:
            logger.warning("OWNER_ID لازم يكون رقماً — تجاهلته")
    if SITE_URL and not SITE_URL.startswith(("http://", "https://")):
        logger.warning("SITE_URL لازم يبدأ بـ https:// — تجاهلته")
        SITE_URL = ""

    token = os.environ.get("BOT_TOKEN", "").strip()
    if not token:
        raise SystemExit(
            "ما لقيت التوكن.\n"
            "حطه في ملف .env جنب bot.py بهذا الشكل:\n"
            "  BOT_TOKEN=123:abc\n"
            "أو مرّره مباشرة: BOT_TOKEN=123:abc python bot.py"
        )

    # فحص الصيغة قبل ما نتصل — يكشف التوكن الملتصق بمتغيّر بعده،
    # ويعطي رسالة مفهومة بدل «Invalid token» من تيليقرام
    if not TOKEN_PATTERN.fullmatch(token):
        raise SystemExit(
            "صيغة التوكن غير صحيحة.\n"
            "المتوقع: أرقام ثم نقطتان ثم ٣٥ حرفاً تقريباً، مثل 123456789:AAF-xxxxx\n\n"
            "السبب الشائع: متغيّر ثانٍ التصق بنهاية سطر التوكن في .env — تأكد أن\n"
            "كل متغيّر في سطر مستقل، وأن الملف ينتهي بسطر جديد.\n\n"
            f"طول ما قرأته: {len(token)} حرفاً"
            + (" — يبدو أن SITE_URL ملتصق به." if "SITE_URL" in token else "")
        )

    # الإعدادات والتنبيهات تبقى محفوظة بعد إعادة التشغيل
    persistence = PicklePersistence(filepath=state_file())

    app = (
        Application.builder()
        .token(token)
        .connect_timeout(20)
        .read_timeout(20)
        .write_timeout(20)
        .pool_timeout(10)
        .get_updates_read_timeout(45)
        .persistence(persistence)
        .post_init(post_init)
        .build()
    )

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(CommandHandler("about", about_command))
    app.add_handler(CommandHandler("now", now_command))
    app.add_handler(CommandHandler("gram", gram_command))
    app.add_handler(CommandHandler("bars", bars_command))
    app.add_handler(CommandHandler("all", all_command))
    app.add_handler(CommandHandler("w", weight_command))
    app.add_handler(CommandHandler("shop", shop_command))
    app.add_handler(CommandHandler("zakat", zakat_command))
    app.add_handler(CommandHandler("ounce", ounce_command))
    app.add_handler(CommandHandler("alert", alert_command))
    app.add_handler(CommandHandler("alerts", show_alerts))
    app.add_handler(CommandHandler("unalert", unalert_command))
    app.add_handler(CommandHandler("fav", fav_command))
    app.add_handler(CommandHandler("currency", currency_command))
    app.add_handler(CommandHandler("rate", rate_command))
    app.add_handler(CommandHandler("sources", sources_command))
    for karat in KARATS:
        app.add_handler(CommandHandler(f"k{karat}", karat_command))
    app.add_handler(CallbackQueryHandler(on_button))
    app.add_handler(InlineQueryHandler(inline_query))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, plain_number))
    app.add_error_handler(on_error)

    app.job_queue.run_repeating(
        check_alerts,
        interval=alerts_store.CHECK_INTERVAL_SECONDS,
        first=30,
        name="check_alerts",
    )
    app.job_queue.run_repeating(
        health_check, interval=600, first=60, name="health_check"
    )
    # ٢٠:٥٠ بتوقيت UTC = قبل إغلاق الجمعة بعشر دقائق، وهو آخر سعر لكل يوم تداول
    app.job_queue.run_daily(
        record_close, time=time(20, 50, tzinfo=timezone.utc), name="record_close"
    )

    logger.info("البوت شغّال…")
    # ‏bootstrap_retries=-1: يصبر بلا حد عند الإقلاع بدل ما يموت من أول فشل
    # شبكة. مهم على السيرفرات: دوكر يبدأ قبل ما تكتمل الشبكة أحياناً.
    app.run_polling(allowed_updates=Update.ALL_TYPES, bootstrap_retries=-1)


if __name__ == "__main__":
    main()

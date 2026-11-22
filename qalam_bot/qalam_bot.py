import asyncio, hashlib, html, logging, os, sqlite3
from typing import Any, Dict, List, Optional, Set, Tuple
from aiogram import Bot, Dispatcher, F, types
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError
from aiogram.filters import CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, KeyboardButton, Message, ReplyKeyboardMarkup
from google import genai
from google.genai import types as genai_types

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger("qalam_bot")

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
if not BOT_TOKEN:
    raise ValueError("BOT_TOKEN topilmadi!")

_raw_keys = os.getenv("GEMINI_API_KEYS") or os.getenv("GEMINI_API_KEY") or ""
GEMINI_API_KEYS = [k.strip().strip("\"'") for k in _raw_keys.split(",") if k.strip().strip("\"'")]
if not GEMINI_API_KEYS:
    raise ValueError("GEMINI_API_KEYS topilmadi!")

try:
    ADMIN_ID = int((os.getenv("ADMIN_ID") or "7733016781").strip().strip("\"'"))
except ValueError:
    ADMIN_ID = 0

ADMIN_SECRET_CODE = "20070518"
CLICK_CARD = os.getenv("CLICK_CARD", "9860 0801 1915 0045").strip()
CARD_HOLDER = "QALAM XIZMATI"
PRICE_TEXT = "5,000 UZS"
DEFAULT_LANG = "uz"
SUPPORTED_LANGS = ("uz", "ru", "en")
DB_PATH = os.getenv("DB_PATH", "bot_data.db")

MODELS = ["gemini-3.5-flash-lite", "gemini-3.8-flash", "gemini-3.5-flash", "gemini-2.5-flash"]
_unavailable_models: Set[str] = set()
_key_rr_index = 0
_key_rr_lock = asyncio.Lock()

bot = Bot(token=BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
dp = Dispatcher()
_clients = [genai.Client(api_key=k) for k in GEMINI_API_KEYS]


class AuthStates(StatesGroup):
    waiting_admin_code = State()


async def _get_ordered_clients() -> List[Tuple[int, genai.Client]]:
    global _key_rr_index
    total = len(_clients)
    async with _key_rr_lock:
        start = _key_rr_index % total
        _key_rr_index = (_key_rr_index + 1) % total
    return [((start + i) % total, _clients[(start + i) % total]) for i in range(total)]


UI_TEXTS: Dict[str, Dict[str, str]] = {
    "uz": {
        "pay_btn": "💳 5,000 UZS to'lash",
        "key_btn": "🔑 Aktivatsiya kodini olish",
        "lang_btn": "🌐 Tilni o'zgartirish",
        "lang_chosen": "✅ Til tanlandi: <b>O'zbekcha</b>.\nFaqat bot, to'lov, ilova va ilova asoschisi haqida savol bering.",
        "already_approved": "✅ To'lovingiz tasdiqlangan!\n📱 Aktivatsiya kodingiz: <code>{key}</code>",
        "already_pending": "⏳ To'lovingiz tekshiruvda! Administrator tasdiqlashini kuting.",
        "payment_info": "💳 <b>Click to'lov:</b>\n\n💰 Summa: <b>{price}</b>\n💳 Karta: <code>{click_card}</code>\n👤 Egasi: <b>{holder}</b>\n\nTo'lovdan so'ng <b>«✅ To'lov qildim»</b> tugmasini bosing.",
        "confirm_paid_btn": "✅ To'lov qildim",
        "paid_sent_msg": "⏳ <b>To'lov tekshiruvga yuborildi.</b> Admin tasdiqlashini kuting.",
        "paid_toast": "To'lov tekshiruvga yuborildi.",
        "key_approved": "🔑 <b>Ilova uchun aktivatsiya kodingiz:</b>\n<code>{key}</code>",
        "key_pending": "🔒 <b>Ilova kodi hali yopiq.</b> To'lovingiz tekshiruvda.",
        "key_unpaid": "🔒 <b>Ilova kodi yopiq.</b>\nAvval <b>«💳 5,000 UZS to'lash»</b> orqali <code>{click_card}</code> kartasiga to'lov qiling.",
        "quota_error": "⏳ Bugungi bepul AI so'rovlar limiti tugadi.",
        "ai_busy_error": "😔 AI serverlarida yuklama yuqori. Qayta urinib ko'ring.",
    },
    "ru": {
        "pay_btn": "💳 Оплатить 5,000 UZS",
        "key_btn": "🔑 Получить код активации",
        "lang_btn": "🌐 Сменить язык",
        "lang_chosen": "✅ Язык выбран: <b>Русский</b>.\nЗадавайте вопросы только о боте, оплате, приложении и создателе.",
        "already_approved": "✅ Ваша оплата подтверждена!\n📱 Код активации: <code>{key}</code>",
        "already_pending": "⏳ Ваша оплата на проверке! Ожидайте подтверждения.",
        "payment_info": "💳 <b>Оплата (Click):</b>\n\n💰 Сумма: <b>{price}</b>\n💳 Карта: <code>{click_card}</code>\n👤 Получатель: <b>{holder}</b>\n\nПосле оплаты нажмите <b>«✅ Я оплатил»</b> ниже.",
        "confirm_paid_btn": "✅ Я оплатил",
        "paid_sent_msg": "⏳ <b>Оплата отправлена на проверку.</b> Ожидайте.",
        "paid_toast": "Оплата отправлена на проверку.",
        "key_approved": "🔑 <b>Ваш код активации:</b>\n<code>{key}</code>",
        "key_pending": "🔒 <b>Код закрыт.</b> Оплата на проверке.",
        "key_unpaid": "🔒 <b>Код закрыт.</b>\nСначала оплатите через <b>«💳 Оплатить 5,000 UZS»</b> на карту <code>{click_card}</code>.",
        "quota_error": "⏳ Дневной лимит AI-запросов исчерпан.",
        "ai_busy_error": "😔 Серверы ИИ перегружены. Повторите попытку.",
    },
    "en": {
        "pay_btn": "💳 Pay 5,000 UZS",
        "key_btn": "🔑 Get Activation Code",
        "lang_btn": "🌐 Change Language",
        "lang_chosen": "✅ Language selected: <b>English</b>.\nAsk questions only about the bot, payment, app, and founder.",
        "already_approved": "✅ Your payment is approved!\n📱 Your activation code: <code>{key}</code>",
        "already_pending": "⏳ Your payment is under review! Wait for admin approval.",
        "payment_info": "💳 <b>Click Payment:</b>\n\n💰 Amount: <b>{price}</b>\n💳 Card: <code>{click_card}</code>\n👤 Holder: <b>{holder}</b>\n\nAfter paying, tap <b>«✅ I Have Paid»</b> below.",
        "confirm_paid_btn": "✅ I Have Paid",
        "paid_sent_msg": "⏳ <b>Payment sent for review.</b> Please wait.",
        "paid_toast": "Payment sent for review.",
        "key_approved": "🔑 <b>Your activation code:</b>\n<code>{key}</code>",
        "key_pending": "🔒 <b>App code is locked.</b> Payment is under review.",
        "key_unpaid": "🔒 <b>App code is locked.</b>\nFirst pay via <b>«💳 Pay 5,000 UZS»</b> to card <code>{click_card}</code>.",
        "quota_error": "⏳ Daily free AI limit reached.",
        "ai_busy_error": "😔 AI servers are busy right now. Try again shortly.",
    },
}

SYSTEM_INSTRUCTION = (
    "You are the official assistant for Qalam bot and app. "
    "Answer ONLY questions about the bot, payment (Click card, 5,000 UZS), activation code, the app, and the app founder. "
    "CRITICAL RULE ABOUT FOUNDER/CREATOR: The ONLY founder, creator, author, and owner of Qalam bot and app is "
    "Oktamov Samandar (in Uzbek: O'ktamov Samandar, in Russian: Уктамов Самандар, in English: Oktamov Samandar). "
    "Whenever asked who the founder, creator, author, or owner is, state ONLY Oktamov Samandar and NEVER mention any other name. "
    "Reply in the user's selected language."
)


def init_db() -> None:
    if os.path.dirname(DB_PATH):
        os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute("CREATE TABLE IF NOT EXISTS users (user_id INTEGER PRIMARY KEY, username TEXT, full_name TEXT, lang TEXT DEFAULT 'uz', status TEXT DEFAULT 'unpaid', activation_key TEXT)")
        conn.commit()


def ensure_user(user: types.User) -> Dict[str, Any]:
    with sqlite3.connect(DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute("SELECT * FROM users WHERE user_id = ?", (user.id,)).fetchone()
        name, uname = (user.full_name or "").strip(), user.username or ""
        if not row:
            conn.execute("INSERT INTO users (user_id, username, full_name, lang, status) VALUES (?, ?, ?, ?, 'unpaid')", (user.id, uname, name, DEFAULT_LANG))
            conn.commit()
            row = conn.execute("SELECT * FROM users WHERE user_id = ?", (user.id,)).fetchone()
        else:
            conn.execute("UPDATE users SET username = ?, full_name = ? WHERE user_id = ?", (uname, name, user.id))
            conn.commit()
        return dict(row)


def set_user_lang(user_id: int, lang: str) -> None:
    chosen = lang if lang in SUPPORTED_LANGS else DEFAULT_LANG
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute("UPDATE users SET lang = ? WHERE user_id = ?", (chosen, user_id))
        conn.commit()


def set_user_status(user_id: int, status: str, activation_key: Optional[str] = None) -> None:
    with sqlite3.connect(DB_PATH) as conn:
        if activation_key is not None:
            conn.execute("UPDATE users SET status = ?, activation_key = ? WHERE user_id = ?", (status, activation_key, user_id))
        else:
            conn.execute("UPDATE users SET status = ? WHERE user_id = ?", (status, user_id))
        conn.commit()


def generate_activation_key(user_id: int) -> str:
    d = hashlib.sha256(f"qalam:{user_id}:{ADMIN_SECRET_CODE}".encode()).hexdigest().upper()
    return f"QALAM-{d[:4]}-{d[4:8]}-{d[8:12]}"


def get_txt(lang: str, key: str, **kwargs: Any) -> str:
    pack = UI_TEXTS.get(lang) or UI_TEXTS[DEFAULT_LANG]
    tpl = pack.get(key) or UI_TEXTS[DEFAULT_LANG][key]
    return tpl.format(**kwargs) if kwargs else tpl


def language_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="🇺🇿 O'zbekcha", callback_data="lang:uz"),
        InlineKeyboardButton(text="🇷🇺 Русский", callback_data="lang:ru"),
        InlineKeyboardButton(text="🇬🇧 English", callback_data="lang:en"),
    ]])


def main_menu_keyboard(lang: str) -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(keyboard=[[KeyboardButton(text=get_txt(lang, "pay_btn")), KeyboardButton(text=get_txt(lang, "key_btn"))], [KeyboardButton(text=get_txt(lang, "lang_btn"))]], resize_keyboard=True)


def payment_confirm_keyboard(lang: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text=get_txt(lang, "confirm_paid_btn"), callback_data="confirm_paid")]])


def admin_approval_keyboard(uid: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="✅ Tasdiqlash", callback_data=f"admin_approve:{uid}"), InlineKeyboardButton(text="❌ Rad etish", callback_data=f"admin_reject:{uid}")]])


async def ask_gemini(prompt: str, lang: str) -> str:
    clients = await _get_ordered_clients()
    quota_err = False
    for model in MODELS:
        if model in _unavailable_models:
            continue
        for attempt in range(2):
            for idx, client in clients:
                try:
                    res = await asyncio.wait_for(client.aio.models.generate_content(model=model, contents=f"[Language: {lang}]\n{prompt}", config=genai_types.GenerateContentConfig(system_instruction=SYSTEM_INSTRUCTION, temperature=0.3)), timeout=12.0)
                    if getattr(res, "text", None):
                        return res.text.strip()
                except Exception as exc:
                    err = str(exc).lower()
                    if "404" in err or "not_found" in err or "not found" in err:
                        _unavailable_models.add(model)
                        break
                    if "429" in err or "quota" in err or "resource_exhausted" in err:
                        quota_err = True
            if model in _unavailable_models:
                break
            if attempt == 0:
                await asyncio.sleep(2.0)
    return get_txt(lang, "quota_error" if quota_err else "ai_busy_error")


@dp.message(CommandStart())
async def cmd_start(message: Message, state: FSMContext) -> None:
    if not message.from_user:
        return
    await state.clear()
    lang = ensure_user(message.from_user).get("lang") or DEFAULT_LANG
    await message.answer("👋 Xush kelibsiz! / Добро пожаловать! / Welcome!\nTilni tanlang / Выберите язык / Choose language:", reply_markup=language_keyboard())
    await message.answer(get_txt(lang, "lang_chosen"), reply_markup=main_menu_keyboard(lang))


@dp.callback_query(F.data.startswith("lang:"))
async def cb_choose_lang(callback: CallbackQuery) -> None:
    if not callback.from_user or not callback.data:
        return
    ensure_user(callback.from_user)
    lang = callback.data.split(":", 1)[1].strip()
    lang = lang if lang in SUPPORTED_LANGS else DEFAULT_LANG
    set_user_lang(callback.from_user.id, lang)
    if callback.message:
        try:
            await callback.message.edit_text(get_txt(lang, "lang_chosen"))
        except TelegramBadRequest:
            await callback.message.answer(get_txt(lang, "lang_chosen"))
        await callback.message.answer("👇", reply_markup=main_menu_keyboard(lang))
    await callback.answer()


@dp.callback_query(F.data == "confirm_paid")
async def cb_confirm_paid(callback: CallbackQuery) -> None:
    if not callback.from_user:
        return
    u = ensure_user(callback.from_user)
    lang = u.get("lang") or DEFAULT_LANG
    if u.get("status") == "approved":
        key = u.get("activation_key") or generate_activation_key(callback.from_user.id)
        await callback.answer("OK")
        if callback.message:
            await callback.message.answer(get_txt(lang, "already_approved", key=html.escape(key)))
        return
    set_user_status(callback.from_user.id, "pending")
    await callback.answer(get_txt(lang, "paid_toast"))
    if callback.message:
        await callback.message.answer(get_txt(lang, "paid_sent_msg"))
    if ADMIN_ID > 0:
        uname = f"@{callback.from_user.username}" if callback.from_user.username else "yo'q"
        admin_text = f"🆕 <b>Yangi to'lov (Click):</b>\n\n👤 Ism: <b>{html.escape(callback.from_user.full_name or 'User')}</b>\n🔗 Username: {html.escape(uname)}\n🆔 ID: <code>{callback.from_user.id}</code>\n💰 Summa: <b>{html.escape(PRICE_TEXT)}</b>"
        try:
            await bot.send_message(ADMIN_ID, admin_text, reply_markup=admin_approval_keyboard(callback.from_user.id))
        except (TelegramForbiddenError, TelegramBadRequest):
            pass


@dp.callback_query(F.data.startswith("admin_approve:"))
async def cb_admin_approve(callback: CallbackQuery) -> None:
    if not callback.from_user or not callback.data or callback.from_user.id != ADMIN_ID:
        await callback.answer("Ruxsat yo'q.", show_alert=True)
        return
    target_id = int(callback.data.split(":", 1)[1])
    key = generate_activation_key(target_id)
    set_user_status(target_id, "approved", key)
    with sqlite3.connect(DB_PATH) as conn:
        row = conn.execute("SELECT lang FROM users WHERE user_id = ?", (target_id,)).fetchone()
    t_lang = row[0] if row and row[0] in SUPPORTED_LANGS else DEFAULT_LANG
    try:
        await bot.send_message(target_id, get_txt(t_lang, "key_approved", key=html.escape(key)))
    except (TelegramForbiddenError, TelegramBadRequest):
        pass
    if callback.message:
        try:
            await callback.message.edit_text((callback.message.html_text or "") + f"\n\n✅ <b>Tasdiqlandi:</b> <code>{html.escape(key)}</code>")
        except TelegramBadRequest:
            pass
    await callback.answer("Tasdiqlandi ✅")


@dp.callback_query(F.data.startswith("admin_reject:"))
async def cb_admin_reject(callback: CallbackQuery) -> None:
    if not callback.from_user or not callback.data or callback.from_user.id != ADMIN_ID:
        await callback.answer("Ruxsat yo'q.", show_alert=True)
        return
    target_id = int(callback.data.split(":", 1)[1])
    set_user_status(target_id, "unpaid")
    try:
        await bot.send_message(target_id, "❌ To'lov tasdiqlanmadi. Tekshirib qayta urinib ko'ring.")
    except (TelegramForbiddenError, TelegramBadRequest):
        pass
    if callback.message:
        try:
            await callback.message.edit_text((callback.message.html_text or "") + "\n\n❌ <b>Rad etildi.</b>")
        except TelegramBadRequest:
            pass
    await callback.answer("Rad etildi ❌")


@dp.message(F.text)
async def handle_text(message: Message) -> None:
    if not message.from_user or not message.text:
        return
    text = message.text.strip()
    u = ensure_user(message.from_user)
    lang, status = u.get("lang") or DEFAULT_LANG, u.get("status") or "unpaid"
    key = u.get("activation_key") or generate_activation_key(message.from_user.id)

    if text in {UI_TEXTS[l]["pay_btn"] for l in SUPPORTED_LANGS}:
        if status == "approved":
            await message.answer(get_txt(lang, "already_approved", key=html.escape(key)))
        elif status == "pending":
            await message.answer(get_txt(lang, "already_pending"))
        else:
            await message.answer(get_txt(lang, "payment_info", price=html.escape(PRICE_TEXT), click_card=html.escape(CLICK_CARD), holder=html.escape(CARD_HOLDER)), reply_markup=payment_confirm_keyboard(lang))
        return

    if text in {UI_TEXTS[l]["key_btn"] for l in SUPPORTED_LANGS}:
        if status == "approved":
            set_user_status(message.from_user.id, "approved", key)
            await message.answer(get_txt(lang, "key_approved", key=html.escape(key)))
        elif status == "pending":
            await message.answer(get_txt(lang, "key_pending"))
        else:
            await message.answer(get_txt(lang, "key_unpaid", click_card=html.escape(CLICK_CARD)))
        return

    if text in {UI_TEXTS[l]["lang_btn"] for l in SUPPORTED_LANGS}:
        await message.answer("🌐 Tilni tanlang / Выберите язык / Choose language:", reply_markup=language_keyboard())
        return

    await bot.send_chat_action(message.chat.id, "typing")
    reply = await ask_gemini(text, lang)
    await message.answer(html.escape(reply))


async def main() -> None:
    init_db()
    logger.info("qalam_bot ishga tushmoqda...")
    await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())


if __name__ == "__main__":
    asyncio.run(main())

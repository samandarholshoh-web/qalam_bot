import asyncio
import hashlib
import html
import logging
import os
import sqlite3
from typing import Any, Dict, List, Optional, Set, Tuple

from aiogram import Bot, Dispatcher, F, types
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError
from aiogram.filters import CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    Message,
    ReplyKeyboardMarkup,
)
from google import genai
from google.genai import types as genai_types

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(name)s - %(message)s",
)
logger = logging.getLogger("qalam_bot")

BOT_TOKEN = os.getenv("BOT_TOKEN")
if not BOT_TOKEN:
    raise ValueError("BOT_TOKEN muhit o'zgaruvchisi topilmadi!")

_raw_keys = os.getenv("GEMINI_API_KEYS") or os.getenv("GEMINI_API_KEY") or ""
GEMINI_API_KEYS: List[str] = [
    k.strip().strip("\"'") for k in _raw_keys.split(",") if k.strip().strip("\"'")
]
if not GEMINI_API_KEYS:
    raise ValueError("GEMINI_API_KEYS yoki GEMINI_API_KEY topilmadi!")

_raw_admin_id = (os.getenv("ADMIN_ID") or "7733016781").strip().strip("\"'")
try:
    ADMIN_ID: int = int(_raw_admin_id)
except ValueError:
    ADMIN_ID = 0

ADMIN_SECRET_CODE: str = "20070518"
CLICK_CARD: str = os.getenv("CLICK_CARD", "9860 0801 1915 0045").strip()
CARD_HOLDER: str = "QALAM XIZMATI"
PRICE_TEXT: str = "5,000 UZS"

DEFAULT_LANG: str = "uz"
SUPPORTED_LANGS: Tuple[str, ...] = ("uz", "ru", "en")
DB_PATH = os.getenv("DB_PATH", "bot_data.db")

MODELS: List[str] = [
    "gemini-3.5-flash-lite",
    "gemini-3.8-flash",
    "gemini-3.5-flash",
    "gemini-2.5-flash",
]

MAX_RETRIES_PER_MODEL: int = 2
BACKOFF_DELAYS: List[float] = [2.0, 4.0]
REQUEST_TIMEOUT_SECONDS: float = 12.0

_unavailable_models: Set[str] = set()
_key_rr_index: int = 0
_key_rr_lock = asyncio.Lock()

bot = Bot(
    token=BOT_TOKEN.strip(),
    default=DefaultBotProperties(parse_mode=ParseMode.HTML),
)
dp = Dispatcher()
_clients: List[genai.Client] = [genai.Client(api_key=k) for k in GEMINI_API_KEYS]


class AuthStates(StatesGroup):
    waiting_admin_code = State()


async def _get_ordered_clients() -> List[Tuple[int, genai.Client]]:
    global _key_rr_index
    total = len(_clients)
    async with _key_rr_lock:
        start = _key_rr_index % total
        _key_rr_index = (_key_rr_index + 1) % total
    return [
        ((start + offset) % total, _clients[(start + offset) % total])
        for offset in range(total)
    ]


UI_TEXTS: Dict[str, Dict[str, str]] = {
    "uz": {
        "pay_btn": "💳 5,000 UZS to'lash",
        "key_btn": "🔑 Aktivatsiya kodini olish",
        "lang_btn": "🌐 Tilni o'zgartirish",
        "lang_chosen": (
            "✅ Til tanlandi: <b>O'zbekcha (Uzbek)</b>.\n"
            "Faqat bot, to'lov, ilova va ilova asoschisi haqida savol bering."
        ),
        "already_approved": (
            "✅ Sizning to'lovingiz allaqachon tasdiqlangan!\n"
            "📱 Ilova (app) uchun aktivatsiya kodingiz: <code>{key}</code>"
        ),
        "already_pending": "⏳ To'lovingiz tekshiruvda! Administrator tasdiqlashini kuting.",
        "payment_info": (
            "💳 <b>Click to'lov ma'lumotlari:</b>\n\n"
            "💰 Summa: <b>{price}</b>\n"
            "💳 Click karta: <code>{click_card}</code>\n"
            "👤 Karta egasi: <b>{holder}</b>\n\n"
            "To'lovdan so'ng <b>«✅ To'lov qildim»</b> tugmasini bosing."
        ),
        "confirm_paid_btn": "✅ To'lov qildim",
        "paid_sent_msg": "⏳ <b>To'lov tekshiruvga yuborildi.</b> Admin tasdiqlashini kuting.",
        "paid_toast": "To'lov tekshiruvga yuborildi.",
        "key_approved": "🔑 <b>Ilova uchun aktivatsiya kodingiz:</b>\n<code>{key}</code>",
        "key_pending": "🔒 <b>Ilova kodi hali yopiq.</b> To'lovingiz tekshiruvda.",
        "key_unpaid": (
            "🔒 <b>Ilova kodi yopiq.</b>\n"
            "Avval <b>«💳 5,000 UZS to'lash»</b> orqali Click karta "
            "(<code>{click_card}</code>) ga to'lov qiling."
        ),
        "quota_error": "⏳ Bugungi bepul AI so'rovlar limiti tugadi.",
        "ai_busy_error": "😔 AI serverlarida yuklama yuqori. Qayta urinib ko'ring.",
    },
    "ru": {
        "pay_btn": "💳 Оплатить 5,000 UZS",
        "key_btn": "🔑 Получить код активации",
        "lang_btn": "🌐 Сменить язык",
        "lang_chosen": (
            "✅ Язык выбран: <b>Русский (Russian)</b>.\n"
            "Задавайте вопросы только о боте, оплате, приложении и создателе."
        ),
        "already_approved": (
            "✅ Ваша оплата подтверждена!\n"
            "📱 Код активации: <code>{key}</code>"
        ),
        "already_pending": "⏳ Ваша оплата на проверке! Ожидайте подтверждения.",
        "payment_info": (
            "💳 <b>Реквизиты для оплаты (Click):</b>\n\n"
            "💰 Сумма: <b>{price}</b>\n"
            "💳 Карта Click: <code>{click_card}</code>\n"
            "👤 Получатель: <b>{holder}</b>\n\n"
            "После оплаты нажмите <b>«✅ Я оплатил»</b> ниже."
        ),
        "confirm_paid_btn": "✅ Я оплатил",
        "paid_sent_msg": "⏳ <b>Оплата отправлена на проверку.</b> Ожидайте.",
        "paid_toast": "Оплата отправлена на проверку.",
        "key_approved": "🔑 <b>Ваш код активации:</b>\n<code>{key}</code>",
        "key_pending": "🔒 <b>Код закрыт.</b> Оплата на проверке.",
        "key_unpaid": (
            "🔒 <b>Код закрыт.</b>\n"
            "Сначала оплатите через <b>«💳 Оплатить 5,000 UZS»</b> на карту Click: "
            "<code>{click_card}</code>."
        ),
        "quota_error": "⏳ Дневной лимит AI-запросов исчерпан.",
        "ai_busy_error": "😔 Серверы ИИ перегружены. Повторите попытку.",
    },
    "en": {
        "pay_btn": "💳 Pay 5,000 UZS",
        "key_btn": "🔑 Get Activation Code",
        "lang_btn": "🌐 Change Language",
        "lang_chosen": (
            "✅ Language selected: <b>English</b>.\n"
            "Ask questions only about the bot, payment, app, and founder."
        ),
        "already_approved": (
            "✅ Your payment is already approved!\n"
            "📱 Your activation code: <code>{key}</code>"
        ),
        "already_pending": "⏳ Your payment is under review! Wait for admin approval.",
        "payment_info": (
            "💳 <b>Click Payment Details:</b>\n\n"
            "💰 Amount: <b>{price}</b>\n"
            "💳 Click Card: <code>{click_card}</code>\n"
            "👤 Holder: <b>{holder}</b>\n\n"
            "After paying, tap <b>«✅ I Have Paid»</b> below."
        ),
        "confirm_paid_btn": "✅ I Have Paid",
        "paid_sent_msg": "⏳ <b>Payment sent for review.</b> Please wait.",
        "paid_toast": "Payment sent for review.",
        "key_approved": "🔑 <b>Your activation code:</b>\n<code>{key}</code>",
        "key_pending": "🔒 <b>App code is locked.</b> Payment is under review.",
        "key_unpaid": (
            "🔒 <b>App code is locked.</b>\n"
            "First pay via <b>«💳 Pay 5,000 UZS»</b> to Click card: "
            "<code>{click_card}</code>."
        ),
        "quota_error": "⏳ Daily free AI limit reached.",
        "ai_busy_error": "😔 AI servers are busy right now. Try again shortly.",
    },
}

SYSTEM_INSTRUCTION = (
    "You are the official assistant for Qalam bot and app. "
    "Answer ONLY questions about the bot, payment (Click card, 5,000 UZS), "
    "activation code, the app, and the app founder. "
    "Reply in the user's selected language."
)


def init_db() -> None:
    db_dir = os.path.dirname(DB_PATH)
    if db_dir:
        os.makedirs(db_dir, exist_ok=True)
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS users (
                user_id INTEGER PRIMARY KEY,
                username TEXT,
                full_name TEXT,
                lang TEXT DEFAULT 'uz',
                status TEXT DEFAULT 'unpaid',
                activation_key TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        conn.commit()


def ensure_user(user: types.User) -> Dict[str, Any]:
    with sqlite3.connect(DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT * FROM users WHERE user_id = ?", (user.id,)
        ).fetchone()
        full_name = (user.full_name or "").strip()
        username = user.username or ""
        if not row:
            conn.execute(
                "INSERT INTO users (user_id, username, full_name, lang, status) "
                "VALUES (?, ?, ?, ?, 'unpaid')",
                (user.id, username, full_name, DEFAULT_LANG),
            )
            conn.commit()
            row = conn.execute(
                "SELECT * FROM users WHERE user_id = ?", (user.id,)
            ).fetchone()
        else:
            conn.execute(
                "UPDATE users SET username = ?, full_name = ?, "
                "updated_at = CURRENT_TIMESTAMP WHERE user_id = ?",
                (username, full_name, user.id),
            )
            conn.commit()
        return dict(row)


def set_user_lang(user_id: int, lang: str) -> None:
    chosen = lang if lang in SUPPORTED_LANGS else DEFAULT_LANG
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute(
            "UPDATE users SET lang = ?, updated_at = CURRENT_TIMESTAMP WHERE user_id = ?",
            (chosen, user_id),
        )
        conn.commit()


def set_user_status(
    user_id: int, status: str, activation_key: Optional[str] = None
) -> None:
    with sqlite3.connect(DB_PATH) as conn:
        if activation_key is not None:
            conn.execute(
                "UPDATE users SET status = ?, activation_key = ?, "
                "updated_at = CURRENT_TIMESTAMP WHERE user_id = ?",
                (status, activation_key, user_id),
            )
        else:
            conn.execute(
                "UPDATE users SET status = ?, updated_at = CURRENT_TIMESTAMP WHERE user_id = ?",
                (status, user_id),
            )
        conn.commit()


def generate_activation_key(user_id: int) -> str:
    digest = hashlib.sha256(
        f"qalam:{user_id}:{ADMIN_SECRET_CODE}".encode("utf-8")
    ).hexdigest().upper()
    return f"QALAM-{digest[:4]}-{digest[4:8]}-{digest[8:12]}"


def get_txt(lang: str, key: str, **kwargs: Any) -> str:
    pack = UI_TEXTS.get(lang) or UI_TEXTS[DEFAULT_LANG]
    template = pack.get(key) or UI_TEXTS[DEFAULT_LANG][key]
    return template.format(**kwargs) if kwargs else template


def language_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="🇺🇿 O'zbekcha", callback_data="lang:uz"),
                InlineKeyboardButton(text="🇷🇺 Русский", callback_data="lang:ru"),
                InlineKeyboardButton(text="🇬🇧 English", callback_data="lang:en"),
            ]
        ]
    )


def main_menu_keyboard(lang: str) -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [
                KeyboardButton(text=get_txt(lang, "pay_btn")),
                KeyboardButton(text=get_txt(lang, "key_btn")),
            ],
            [KeyboardButton(text=get_txt(lang, "lang_btn"))],
        ],
        resize_keyboard=True,
    )


def payment_confirm_keyboard(lang: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=get_txt(lang, "confirm_paid_btn"),
                    callback_data="confirm_paid",
                )
            ]
        ]
    )


def admin_approval_keyboard(target_user_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="✅ Tasdiqlash",
                    callback_data=f"admin_approve:{target_user_id}",
                ),
                InlineKeyboardButton(
                    text="❌ Rad etish",
                    callback_data=f"admin_reject:{target_user_id}",
                ),
            ]
        ]
    )


def _is_quota_error(exc: Exception) -> bool:
    t = str(exc).lower()
    return "resource_exhausted" in t or "quota" in t or "429" in t


def _is_not_found_error(exc: Exception) -> bool:
    t = str(exc).lower()
    return "not_found" in t or "404" in t or "not found" in t


async def ask_gemini(prompt: str, lang: str) -> str:
    ordered_clients = await _get_ordered_clients()
    last_was_quota = False

    for model_name in MODELS:
        if model_name in _unavailable_models:
            continue
        for attempt in range(MAX_RETRIES_PER_MODEL):
            for client_idx, client in ordered_clients:
                try:
                    response = await asyncio.wait_for(
                        client.aio.models.generate_content(
                            model=model_name,
                            contents=f"[Language: {lang}]\n{prompt}",
                            config=genai_types.GenerateContentConfig(
                                system_instruction=SYSTEM_INSTRUCTION,
                                temperature=0.3,
                            ),
                        ),
                        timeout=REQUEST_TIMEOUT_SECONDS,
                    )
                    reply_text = (getattr(response, "text", None) or "").strip()
                    if reply_text:
                        return reply_text
                except asyncio.TimeoutError:
                    logger.warning("Timeout: %s client=%s", model_name, client_idx)
                except Exception as exc:
                    if _is_not_found_error(exc):
                        _unavailable_models.add(model_name)
                        break
                    if _is_quota_error(exc):
                        last_was_quota = True
                    logger.warning("Gemini err (%s): %s", model_name, exc)
            if model_name in _unavailable_models:
                break
            if attempt < len(BACKOFF_DELAYS):
                await asyncio.sleep(BACKOFF_DELAYS[attempt])

    return get_txt(lang, "quota_error" if last_was_quota else "ai_busy_error")


@dp.message(CommandStart())
async def cmd_start(message: Message, state: FSMContext) -> None:
    if not message.from_user:
        return
    await state.clear()
    user_data = ensure_user(message.from_user)
    lang = user_data.get("lang") or DEFAULT_LANG
    await message.answer(
        "👋 Xush kelibsiz! / Добро пожаловать! / Welcome!\n"
        "Tilni tanlang / Выберите язык / Choose your language:",
        reply_markup=language_keyboard(),
    )
    await message.answer(
        get_txt(lang, "lang_chosen"),
        reply_markup=main_menu_keyboard(lang),
    )


@dp.callback_query(F.data.startswith("lang:"))
async def cb_choose_lang(callback: CallbackQuery) -> None:
    if not callback.from_user or not callback.data:
        return
    ensure_user(callback.from_user)
    lang = callback.data.split(":", 1)[1].strip()
    if lang not in SUPPORTED_LANGS:
        lang = DEFAULT_LANG
    set_user_lang(callback.from_user.id, lang)
    if callback.message:
        try:
            await callback.message.edit_text(get_txt(lang, "lang_chosen"))
        except TelegramBadRequest:
            await callback.message.answer(get_txt(lang, "lang_chosen"))
        await callback.message.answer("👇", reply_markup=main_menu_keyboard(lang))
    await callback.answer()


async def handle_payment_request(message: Message) -> None:
    if not message.from_user:
        return
    user_data = ensure_user(message.from_user)
    lang = user_data.get("lang") or DEFAULT_LANG
    status = user_data.get("status") or "unpaid"
    act_key = user_data.get("activation_key") or generate_activation_key(message.from_user.id)
    if status == "approved":
        await message.answer(get_txt(lang, "already_approved", key=html.escape(act_key)))
        return
    if status == "pending":
        await message.answer(get_txt(lang, "already_pending"))
        return
    await message.answer(
        get_txt(
            lang,
            "payment_info",
            price=html.escape(PRICE_TEXT),
            click_card=html.escape(CLICK_CARD),
            holder=html.escape(CARD_HOLDER),
        ),
        reply_markup=payment_confirm_keyboard(lang),
    )


async def handle_key_request(message: Message) -> None:
    if not message.from_user:
        return
    user_data = ensure_user(message.from_user)
    lang = user_data.get("lang") or DEFAULT_LANG
    status = user_data.get("status") or "unpaid"
    act_key = user_data.get("activation_key") or generate_activation_key(message.from_user.id)
    if status == "approved":
        set_user_status(message.from_user.id, "approved", act_key)
        await message.answer(get_txt(lang, "key_approved", key=html.escape(act_key)))
    elif status == "pending":
        await message.answer(get_txt(lang, "key_pending"))
    else:
        await message.answer(get_txt(lang, "key_unpaid", click_card=html.escape(CLICK_CARD)))


@dp.callback_query(F.data == "confirm_paid")
async def cb_confirm_paid(callback: CallbackQuery) -> None:
    if not callback.from_user:
        return
    user_data = ensure_user(callback.from_user)
    lang = user_data.get("lang") or DEFAULT_LANG
    status = user_data.get("status") or "unpaid"

    if status == "approved":
        act_key = user_data.get("activation_key") or generate_activation_key(callback.from_user.id)
        await callback.answer("OK")
        if callback.message:
            await callback.message.answer(
                get_txt(lang, "already_approved", key=html.escape(act_key))
            )
        return

    set_user_status(callback.from_user.id, "pending")
    await callback.answer(get_txt(lang, "paid_toast"), show_alert=False)
    if callback.message:
        await callback.message.answer(get_txt(lang, "paid_sent_msg"))

    if ADMIN_ID > 0:
        uname = f"@{callback.from_user.username}" if callback.from_user.username else "yo'q"
        safe_name = html.escape(callback.from_user.full_name or "Foydalanuvchi")
        admin_text = (
            "🆕 <b>Yangi to'lov so'rovi (Click):</b>\n\n"
            f"👤 Ism: <b>{safe_name}</b>\n"
            f"🔗 Username: {html.escape(uname)}\n"
            f"🆔 User ID: <code>{callback.from_user.id}</code>\n"
            f"💰 Summa: <b>{html.escape(PRICE_TEXT)}</b>"
        )
        try:
            await bot.send_message(
                chat_id=ADMIN_ID,
                text=admin_text,
                reply_markup=admin_approval_keyboard(callback.from_user.id),
            )
        except (TelegramForbiddenError, TelegramBadRequest) as exc:
            logger.warning("Adminga xabar yuborib bo'lmadi: %s", exc)


@dp.callback_query(F.data.startswith("admin_approve:"))
async def cb_admin_approve(callback: CallbackQuery) -> None:
    if not callback.from_user or not callback.data:
        return
    if ADMIN_ID <= 0 or callback.from_user.id != ADMIN_ID:
        await callback.answer("Ruxsat yo'q.", show_alert=True)
        return
    try:
        target_id = int(callback.data.split(":", 1)[1])
    except ValueError:
        await callback.answer("Noto'g'ri ID.", show_alert=True)
        return

    act_key = generate_activation_key(target_id)
    set_user_status(target_id, "approved", act_key)

    with sqlite3.connect(DB_PATH) as conn:
        row = conn.execute("SELECT lang FROM users WHERE user_id = ?", (target_id,)).fetchone()
    target_lang = row[0] if row and row[0] in SUPPORTED_LANGS else DEFAULT_LANG

    try:
        await bot.send_message(
            chat_id=target_id,
            text=get_txt(target_lang, "key_approved", key=html.escape(act_key)),
        )
    except (TelegramForbiddenError, TelegramBadRequest) as exc:
        logger.warning("Kod yuborib bo'lmadi (%s): %s", target_id, exc)

    if callback.message:
        try:
            await callback.message.edit_text(
                (callback.message.html_text or "")
                + f"\n\n✅ <b>Tasdiqlandi.</b> Kalit: <code>{html.escape(act_key)}</code>"
            )
        except TelegramBadRequest:
            pass
    await callback.answer("Tasdiqlandi ✅")


@dp.callback_query(F.data.startswith("admin_reject:"))
async def cb_admin_reject(callback: CallbackQuery) -> None:
    if not callback.from_user or not callback.data:
        return
    if ADMIN_ID <= 0 or callback.from_user.id != ADMIN_ID:
        await callback.answer("Ruxsat yo'q.", show_alert=True)
        return
    try:
        target_id = int(callback.data.split(":", 1)[1])
    except ValueError:
        await callback.answer("Noto'g'ri ID.", show_alert=True)
        return

    set_user_status(target_id, "unpaid")
    try:
        await bot.send_message(
            chat_id=target_id,
            text="❌ To'lov tasdiqlanmadi. Ma'lumotlarni tekshirib qayta urinib ko'ring.",
        )
    except (TelegramForbiddenError, TelegramBadRequest) as exc:
        logger.warning("Rad etish xabari ketmadi (%s): %s", target_id, exc)

    if callback.message:
        try:
            await callback.message.edit_text(
                (callback.message.html_text or "") + "\n\n❌ <b>Rad etildi.</b>"
            )
        except TelegramBadRequest:
            pass
    await callback.answer("Rad etildi ❌")


@dp.message(F.text)
async def handle_text_message(message: Message) -> None:
    if not message.from_user or not message.text:
        return
    text = message.text.strip()
    user_data = ensure_user(message.from_user)
    lang = user_data.get("lang") or DEFAULT_LANG

    pay_labels = {UI_TEXTS[l]["pay_btn"] for l in SUPPORTED_LANGS}
    key_labels = {UI_TEXTS[l]["key_btn"] for l in SUPPORTED_LANGS}
    lang_labels = {UI_TEXTS[l]["lang_btn"] for l in SUPPORTED_LANGS}

    if text in pay_labels:
        await handle_payment_request(message)
        return
    if text in key_labels:
        await handle_key_request(message)
        return
    if text in lang_labels:
        await message.answer(
            "🌐 Tilni tanlang / Выберите язык / Choose your language:",
            reply_markup=language_keyboard(),
        )
        return

    await bot.send_chat_action(chat_id=message.chat.id, action="typing")
    ai_reply = await ask_gemini(text, lang)
    await message.answer(html.escape(ai_reply))


async def main() -> None:
    init_db()
    logger.info("qalam_bot ishga tushmoqda...")
    await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        logger.info("qalam_bot to'xtatildi.")

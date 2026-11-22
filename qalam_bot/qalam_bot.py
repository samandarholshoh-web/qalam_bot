import asyncio
import hashlib
import html
import logging
import os
import sqlite3
from typing import Any, Dict, List, Optional, Set, Tuple

from aiogram import Bot, Dispatcher, F, types
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError
from aiogram.filters import CommandStart
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

# 1. Muhit o'zgaruvchilari va sozlamalar
BOT_TOKEN = os.getenv("BOT_TOKEN")
if not BOT_TOKEN:
    raise ValueError("BOT_TOKEN muhit o'zgaruvchisi topilmadi!")

_raw_keys = os.getenv("GEMINI_API_KEYS") or os.getenv("GEMINI_API_KEY") or ""
GEMINI_API_KEYS: List[str] = [
    k.strip().strip("\"'") for k in _raw_keys.split(",") if k.strip().strip("\"'")
]
if not GEMINI_API_KEYS:
    raise ValueError("GEMINI_API_KEYS yoki GEMINI_API_KEY muhit o'zgaruvchisi topilmadi!")

# Quyidagi ADMIN-ID o'rniga o'zingizning raqamli Telegram ID ingizni kiriting (masalan: 123456789)
_raw_admin_id = (os.getenv(7733016781) or "7733016781").strip().strip("\"'")
try:
    ADMIN_ID: int = int(_raw_admin_id)
except ValueError:
    logger.warning(
        "ADMIN_ID hali raqam bilan kiritilmagan yoki noto'g'ri formatda (%s). 0 deb olindi.",
        _raw_admin_id,
    )
    ADMIN_ID = 0

if ADMIN_ID > 0:
    logger.info("ADMIN_ID muvaffaqiyatli yuklandi: %s", ADMIN_ID)
else:
    logger.warning("ADMIN_ID o'rnatilmagan yoki noto'g'ri (hozir %s).", ADMIN_ID)

CLICK_CARD: str = os.getenv("CLICK_CARD", "9860 0801 1915 0045").strip()
CARD_NUMBER: str = os.getenv("CARD_NUMBER", CLICK_CARD).strip() or CLICK_CARD
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

bot = Bot(token=BOT_TOKEN.strip())
dp = Dispatcher()
_clients: List[genai.Client] = [genai.Client(api_key=key) for key in GEMINI_API_KEYS]


async def _get_ordered_clients() -> List[Tuple[int, genai.Client]]:
    global _key_rr_index
    total = len(_clients)
    async with _key_rr_lock:
        start = _key_rr_index % total
        _key_rr_index = (_key_rr_index + 1) % total
    return [((start + offset) % total, _clients[(start + offset) % total]) for offset in range(total)]


# 2. Ko'p tilli matnlar va interfeys lug'ati
UI_TEXTS: Dict[str, Dict[str, str]] = {
    "uz": {
        "pay_btn": "💳 5,000 UZS to'lash",
        "key_btn": "🔑 Aktivatsiya kodini olish",
        "lang_chosen": "✅ Til tanlandi: <b>O'zbekcha (Uzbek)</b>.\nSavollaringizni yuborishingiz mumkin.",
        "already_approved": "✅ Sizning to'lovingiz allaqachon tasdiqlangan!\n🔑 Aktivatsiya kodingiz: <code>{key}</code>",
        "already_pending": "⏳ Sizning to'lovingiz hozirda tekshiruvda!\nAdministrator tasdiqlashini kuting.",
        "payment_info": (
            "💳 <b>Click to'lov ma'lumotlari:</b>\n\n"
            "💰 Summa: <b>{price}</b>\n"
            "💳 Click karta raqami: <code>{click_card}</code>\n"
            "👤 Karta egasi: <b>{holder}</b>\n\n"
            "To'lovni amalga oshirgach, pastdagi <b>«✅ To'lov qildim»</b> tugmasini bosing."
        ),
        "confirm_paid_btn": "✅ To'lov qildim",
        "paid_sent_msg": "⏳ <b>To'lov tekshiruvga yuborildi.</b>\nAdministrator to'lovni tekshirib tasdiqlashini kuting.",
        "paid_toast": "To'lov tekshiruvga yuborildi.",
        "key_approved": "🔑 <b>Sizning aktivatsiya kodingiz:</b>\n<code>{key}</code>",
        "key_pending": "🔒 <b>Aktivatsiya kodi hali yopiq.</b>\nTo'lovingiz tekshiruvda — administrator tasdiqlashini kuting.",
        "key_unpaid": (
            "🔒 <b>Aktivatsiya kodi yopiq.</b>\n"
            "Avval <b>«💳 5,000 UZS to'lash»</b> tugmasi orqali Click karta (<code>{click_card}</code>) ga to'lov qiling."
        ),
        "quota_error": "⏳ Bugungi bepul AI so'rovlar limiti tugadi. Iltimos, birozdan so'ng qayta urinib ko'ring.",
        "ai_busy_error": "😔 Uzr, hozir sun'iy intellekt serverlarida vaqtinchalik yuqori yuklama kuzatilmoqda. Iltimos, bir necha soniyadan so'ng qayta urinib ko'ring.",
    },
    "ru": {
        "pay_btn": "💳 Оплатить 5,000 UZS",
        "key_btn": "🔑 Получить код активации",
        "lang_chosen": "✅ Язык выбран: <b>Русский (Russian)</b>.\nМожете отправлять ваши вопросы.",
        "already_approved": "✅ Ваша оплата уже подтверждена!\n🔑 Ваш код активации: <code>{key}</code>",
        "already_pending": "⏳ Ваша оплата сейчас на проверке!\nОжидайте подтверждения администратора.",
        "payment_info": (
            "💳 <b>Реквизиты для оплаты (Click):</b>\n\n"
            "💰 Сумма: <b>{price}</b>\n"
            "💳 Номер карты Click: <code>{click_card}</code>\n"
            "👤 Получатель: <b>{holder}</b>\n\n"
            "После оплаты нажмите кнопку <b>«✅ Я оплатил»</b> ниже."
        ),
        "confirm_paid_btn": "✅ Я оплатил",
        "paid_sent_msg": "⏳ <b>Оплата отправлена на проверку.</b>\nОжидайте подтверждения администратора.",
        "paid_toast": "Оплата отправлена на проверку.",
        "key_approved": "🔑 <b>Ваш код активации:</b>\n<code>{key}</code>",
        "key_pending": "🔒 <b>Код активации пока закрыт.</b>\nОплата на проверке — дождитесь подтверждения администратора.",
        "key_unpaid": (
            "🔒 <b>Код активации закрыт.</b>\n"
            "Сначала оплатите через кнопку <b>«💳 Оплатить 5,000 UZS»</b> на карту Click: <code>{click_card}</code>."
        ),
        "quota_error": "⏳ Дневной лимит бесплатных AI-запросов исчерпан. Пожалуйста, попробуйте позже.",
        "ai_busy_error": "😔 Извините, сейчас серверы ИИ временно перегружены. Пожалуйста, повторите попытку через несколько секунд.",
    },
    "en": {
        "pay_btn": "💳 Pay 5,000 UZS",
        "key_btn": "🔑 Get Activation Code",
        "lang_chosen": "✅ Language selected: <b>English</b>.\nYou can send your questions now.",
        "already_approved": "✅ Your payment is already approved!\n🔑 Your activation code: <code>{key}</code>",
        "already_pending": "⏳ Your payment is currently under review!\nPlease wait for admin approval.",
        "payment_info": (
            "💳 <b>Click Payment Details:</b>\n\n"
            "💰 Amount: <b>{price}</b>\n"
            "💳 Click Card Number: <code>{click_card}</code>\n"
            "👤 Account Holder: <b>{holder}</b>\n\n"
            "After completing payment, tap <b>«✅ I Paid»</b> below."
        ),
        "confirm_paid_btn": "✅ I Paid",
        "paid_sent_msg": "⏳ <b>Payment sent for verification.</b>\nPlease wait for the administrator to approve it.",
        "paid_toast": "Payment sent for verification.",
        "key_approved": "🔑 <b>Your activation code:</b>\n<code>{key}</code>",
        "key_pending": "🔒 <b>Activation code is still locked.</b>\nYour payment is under review — wait for admin approval.",
        "key_unpaid": (
            "🔒 <b>Activation code is locked.</b>\n"
            "First make a payment via <b>«💳 Pay 5,000 UZS»</b> to Click card <code>{click_card}</code>."
        ),
        "quota_error": "⏳ Today's free AI request quota has been reached. Please try again later.",
        "ai_busy_error": "😔 Sorry, AI servers are currently experiencing high load. Please try again in a few seconds.",
    },
}

ALL_PAY_BUTTON_TEXTS: Set[str] = {UI_TEXTS[l]["pay_btn"] for l in SUPPORTED_LANGS}
ALL_KEY_BUTTON_TEXTS: Set[str] = {UI_TEXTS[l]["key_btn"] for l in SUPPORTED_LANGS}


def _normalize_lang(lang: Optional[str]) -> str:
    if lang in SUPPORTED_LANGS:
        return str(lang)
    return DEFAULT_LANG


def _get_lang_texts(lang: Optional[str]) -> Dict[str, str]:
    return UI_TEXTS[_normalize_lang(lang)]


def build_main_keyboard(lang: Optional[str] = None) -> ReplyKeyboardMarkup:
    texts = _get_lang_texts(lang)
    return ReplyKeyboardMarkup(
        keyboard=[
            [
                KeyboardButton(text=texts["pay_btn"]),
                KeyboardButton(text=texts["key_btn"]),
            ]
        ],
        resize_keyboard=True,
    )


def language_inline_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="🇷🇺 Russian", callback_data="set_lang:ru"),
                InlineKeyboardButton(text="🇺🇿 Uzbek", callback_data="set_lang:uz"),
                InlineKeyboardButton(text="🇬🇧 English", callback_data="set_lang:en"),
            ]
        ]
    )


def payment_inline_keyboard(lang: Optional[str] = None) -> InlineKeyboardMarkup:
    texts = _get_lang_texts(lang)
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=texts["confirm_paid_btn"],
                    callback_data="paid_confirm",
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
                    callback_data=f"approve_pay:{target_user_id}",
                ),
                InlineKeyboardButton(
                    text="❌ Rad etish",
                    callback_data=f"reject_pay:{target_user_id}",
                ),
            ]
        ]
    )


# 3. SQLite ma'lumotlar bazasi (bot_data.db)
def get_db_connection() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, timeout=30.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA synchronous=NORMAL;")
    conn.execute("PRAGMA busy_timeout=30000;")
    return conn


def init_db() -> None:
    with get_db_connection() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS users (
                user_id INTEGER PRIMARY KEY,
                username TEXT,
                full_name TEXT,
                status TEXT DEFAULT 'unpaid',
                activation_key TEXT,
                lang TEXT DEFAULT 'uz'
            )
            """
        )
        columns = [
            row["name"]
            for row in conn.execute("PRAGMA table_info(users)").fetchall()
        ]
        if "lang" not in columns:
            conn.execute("ALTER TABLE users ADD COLUMN lang TEXT DEFAULT 'uz'")
        conn.commit()


def get_or_create_user(user: types.User) -> Dict[str, Any]:
    full_name = user.full_name or ""
    username = user.username
    with get_db_connection() as conn:
        conn.execute(
            """
            INSERT OR IGNORE INTO users (user_id, username, full_name, status, activation_key, lang)
            VALUES (?, ?, ?, 'unpaid', NULL, ?)
            """,
            (user.id, username, full_name, DEFAULT_LANG),
        )
        conn.execute(
            """
            UPDATE users
            SET username = ?, full_name = ?
            WHERE user_id = ?
            """,
            (username, full_name, user.id),
        )
        conn.commit()

        updated = conn.execute(
            "SELECT user_id, username, full_name, status, activation_key, lang FROM users WHERE user_id = ?",
            (user.id,),
        ).fetchone()
        if updated:
            data = dict(updated)
            data["lang"] = _normalize_lang(data.get("lang"))
            return data
        return {
            "user_id": user.id,
            "username": username,
            "full_name": full_name,
            "status": "unpaid",
            "activation_key": None,
            "lang": DEFAULT_LANG,
        }


def get_user_by_id(user_id: int) -> Optional[Dict[str, Any]]:
    with get_db_connection() as conn:
        row = conn.execute(
            "SELECT user_id, username, full_name, status, activation_key, lang FROM users WHERE user_id = ?",
            (user_id,),
        ).fetchone()
        if not row:
            return None
        data = dict(row)
        data["lang"] = _normalize_lang(data.get("lang"))
        return data


def set_user_lang(user_id: int, lang: str) -> str:
    clean_lang = _normalize_lang(lang)
    with get_db_connection() as conn:
        conn.execute(
            """
            INSERT OR IGNORE INTO users (user_id, username, full_name, status, activation_key, lang)
            VALUES (?, NULL, '', 'unpaid', NULL, ?)
            """,
            (user_id, clean_lang),
        )
        conn.execute(
            "UPDATE users SET lang = ? WHERE user_id = ?",
            (clean_lang, user_id),
        )
        conn.commit()
    return clean_lang


def set_user_status(
    user_id: int,
    status: str,
    activation_key: Optional[str] = None,
) -> None:
    with get_db_connection() as conn:
        conn.execute(
            """
            UPDATE users
            SET status = ?, activation_key = ?
            WHERE user_id = ?
            """,
            (status, activation_key, user_id),
        )
        conn.commit()


def generate_key_for_user(user_id: int) -> str:
    token_prefix = (BOT_TOKEN or "")[:10]
    raw_string = f"QALAM-{user_id}-{token_prefix}"
    digest = hashlib.sha256(raw_string.encode("utf-8")).hexdigest().upper()
    return f"QALAM-{digest[:5]}-{digest[5:10]}"


# 4. Xavfsiz Telegram yordamchi funksiyalari
async def safe_answer_callback(
    callback: CallbackQuery,
    text: Optional[str] = None,
    show_alert: bool = False,
) -> None:
    try:
        await callback.answer(text=text, show_alert=show_alert)
    except TelegramBadRequest as e:
        logger.warning("Callback javobi eskirgan yoki yaroqsiz: %s", e)
    except Exception as e:
        logger.warning("Callback javobida kutilmagan xato: %s", repr(e))


async def notify_admin(
    text: str,
    reply_markup: Optional[InlineKeyboardMarkup] = None,
) -> bool:
    if not ADMIN_ID or ADMIN_ID <= 0:
        logger.warning("ADMIN_ID o'rnatilmagan yoki noto'g'ri; admin xabarnomasi yuborilmadi.")
        return False
    try:
        await bot.send_message(
            chat_id=ADMIN_ID,
            text=text,
            parse_mode="HTML",
            reply_markup=reply_markup,
        )
        return True
    except TelegramBadRequest as e:
        logger.warning(
            "Adminga xabar yuborib bo'lmadi (ADMIN_ID=%s, chat topilmadi yoki noto'g'ri): %s",
            ADMIN_ID,
            e,
        )
    except TelegramForbiddenError as e:
        logger.warning(
            "Admin botni bloklagan yoki hali /start bosmagan (ADMIN_ID=%s): %s",
            ADMIN_ID,
            e,
        )
    except Exception as e:
        logger.warning(
            "Admin xabarnomasida kutilmagan xatolik (ADMIN_ID=%s): %s",
            ADMIN_ID,
            repr(e),
        )
    return False


# 5. Gemini AI, Qat'iy Til Nazorati, Multi-model + Multi-key Fallback
def _build_strict_language_instruction(lang: str) -> str:
    clean_lang = _normalize_lang(lang)
    if clean_lang == "uz":
        return (
            "CRITICAL LANGUAGE RULE: You MUST respond STRICTLY and ONLY in Uzbek (lotin yozuvidagi o'zbek tili). "
            "Never switch to English or Russian, even if the user writes words or sentences in English or Russian. "
            "Every single word of your explanation and response must be in natural Uzbek."
        )
    if clean_lang == "ru":
        return (
            "CRITICAL LANGUAGE RULE: Ты ДОЛЖЕН отвечать СТРОГО и ТОЛЬКО на русском языке. "
            "Никогда не переключайся на английский или узбекский язык, даже если пользователь пишет слова на другом языке. "
            "Весь твой ответ должен быть написан исключительно на русском языке

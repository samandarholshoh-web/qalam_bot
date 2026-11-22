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

# Quyidagi "7733016781" o'rniga kerak bo'lsa o'z raqamli Telegram ID ingizni qo'ying
_raw_admin_id = (os.getenv("ADMIN_ID") or "7733016781").strip().strip("\"'")
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

ADMIN_SECRET_CODE: str = "20070518"

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


class AuthStates(StatesGroup):
    waiting_admin_code = State()


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
        "lang_chosen": "✅ Til tanlandi: <b>O'zbekcha (Uzbek)</b>.\nFaqat bot, to'lov, ilova va ilova asoschisi haqida savol berishingiz mumkin.",
        "already_approved": "✅ Sizning to'lovingiz allaqachon tasdiqlangan!\n📱 Ilova (app) uchun aktivatsiya kodingiz: <code>{key}</code>",
        "already_pending": "⏳ Sizning to'lovingiz hozirda tekshiruvda!\nAdministrator tasdiqlashini kuting.",
        "payment_info": (
            "💳 <b>Click to'lov ma'lumotlari:</b>\n\n"
            "💰 Summa: <b>{price}</b>\n"
            "💳 Click karta raqami: <code>{click_card}</code>\n"
            "👤 Karta egasi: <b>{holder}</b>\n\n"
            "To'lovni amalga oshirgach, pastdagi <b>«✅ To'lov qildim»</b> tugmasini bosing. To'lov tasdiqlangach ilova (app) uchun kodingiz ochiladi."
        ),
        "confirm_paid_btn": "✅ To'lov qildim",
        "paid_sent_msg": "⏳ <b>To'lov tekshiruvga yuborildi.</b>\nAdministrator to'lovni tekshirib tasdiqlashini kuting.",
        "paid_toast": "To'lov tekshiruvga yuborildi.",
        "key_approved": "🔑 <b>Ilova (app) uchun aktivatsiya kodingiz:</b>\n<code>{key}</code>",
        "key_pending": "🔒 <b>Ilova kodi hali yopiq.</b>\nTo'lovingiz tekshiruvda — administrator tasdiqlashini kuting.",
        "key_unpaid": (
            "🔒 <b>Ilova (app) kodi yopiq.</b>\n"
            "Faqat to'lov qilganda va administrator tasdiqlaganda ilova uchun kod beriladi.\n"
            "Avval <b>«💳 5,000 UZS to'lash»</b> tugmasi orqali Click karta (<code>{click_card}</code>) ga to'lov qiling."
        ),
        "quota_error": "⏳ Bugungi bepul AI so'rovlar limiti tugadi. Iltimos, birozdan so'ng qayta urinib ko'ring.",
        "ai_busy_error": "😔 Uzr, hozir sun'iy intellekt serverlarida vaqtinchalik yuqori yuklama kuzatilmoqda. Iltimos, bir necha soniyadan so'ng qayta urinib ko'ring.",
        "out_of_scope_reply": "Men faqatgina botga oid savollarga javob beraman.",
    },
    "ru": {
        "pay_btn": "💳 Оплатить 5,000 UZS",
        "key_btn": "🔑 Получить код активации",
        "lang_chosen": "✅ Язык выбран: <b>Русский (Russian)</b>.\nВы можете задавать вопросы только о боте, оплате, приложении и создателе приложения.",
        "already_approved": "✅ Ваша оплата уже подтверждена!\n📱 Ваш код активации для приложения (app): <code>{key}</code>",
        "already_pending": "⏳ Ваша оплата сейчас на проверке!\nОжидайте подтверждения администратора.",
        "payment_info": (
            "💳 <b>Реквизиты для оплаты (Click):</b>\n\n"
            "💰 Сумма: <b>{price}</b>\n"
            "💳 Номер карты Click: <code>{click_card}</code>\n"
            "👤 Получатель: <b>{holder}</b>\n\n"
            "После оплаты нажмите кнопку <b>«✅ Я оплатил»</b> ниже. После подтверждения откроется код для приложения (app)."
        ),
        "confirm_paid_btn": "✅ Я оплатил",
        "paid_sent_msg": "⏳ <b>Оплата отправлена на проверку.</b>\nОжидайте подтверждения администратора.",
        "paid_toast": "Оплата отправлена на проверку.",
        "key_approved": "🔑 <b>Ваш код активации для приложения (app):</b>\n<code>{key}</code>",
        "key_pending": "🔒 <b>Код для приложения пока закрыт.</b>\nОплата на проверке — дождитесь подтверждения администратора.",
        "key_unpaid": (
            "🔒 <b>Код для приложения (app) закрыт.</b>\n"
            "Код выдаётся только после оплаты и подтверждения администратором.\n"
            "Сначала опла

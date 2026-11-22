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
            "💳 <b>Click Payment Details:</b>

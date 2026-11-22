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
    format="%(asctime)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger("qalam_bot")

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
if not BOT_TOKEN:
    raise ValueError("BOT_TOKEN topilmadi!")

_raw_keys = os.getenv("GEMINI_API_KEYS") or os.getenv("GEMINI_API_KEY") or ""
GEMINI_API_KEYS = [
    k.strip().strip("\"'") for k in _raw_keys.split(",") if k.strip().strip("\"'")
]
if not GEMINI_API_KEYS:
    raise ValueError("GEMINI_API_KEYS yoki GEMINI_API_KEY topilmadi!")

try:
    ADMIN_ID = int((os.getenv("ADMIN_ID") or "9271026986").strip().strip("\"'"))
except ValueError:
    ADMIN_ID = 9271026986

ADMIN_SECRET_CODE = "20070518"
CLICK_CARD = os.getenv("CLICK_CARD", "9860 0801 1915 0045").strip()
CARD_HOLDER = "QALAM XIZMATI"
PRICE_TEXT = "5,000 UZS"
DEFAULT_LANG = "uz"
SUPPORTED_LANGS = ("uz", "ru", "en")
DB_PATH = os.getenv("DB_PATH", "bot_data.db")

MODELS = [
    "gemini-3.5-flash-lite",
    "gemini-3.8-flash",
    "gemini-3.5-flash",
    "gemini-2.5-flash",
]
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
        "lang_chosen": (
            "✅ Til tanlandi: <b>O'zbekcha</b>.\n"
            "Faqat bot, to'lov, ilova va ilova asoschisi haqida savol bering."
        ),
        "already_approved": (
            "✅ To'lovingiz tasdiqlangan!\n"
            "📱 Aktivatsiya kodingiz: <code>{key}</code>"
        ),
        "already_pending": "⏳ To'lovingiz tekshiruvda! Administrator tasdiqlashini kuting.",
        "payment_info": (
            "💳 <b>Click to'lov:</b>\n\n"
            "💰 Summa: <b>{price}</b>\n"
            "💳 Karta: <code>{click_card}</code>\n"
            "👤 Egasi: <b>{holder}</b>\n\n"
            "To'lovdan so'ng <b>«✅ To'lov qildim»</b> tugmasini bosing."
        ),
        "confirm_paid_btn": "✅ To'lov qildim",
        "paid_sent_msg": "⏳ <b>To'lov tekshiruvga yuborildi.</b> Admin tasdiqlashini kuting.",
        "paid_toast": "To'lov tekshiruvga yuborildi.",
        "key_approved": "🔑 <b>Ilova uchun aktivatsiya kodingiz:</b>\n<code>{key}</code>",
        "key_pending": "🔒 <b>Ilova kodi hali yopiq.</b> To'lovingiz tekshiruvda.",
        "key_unpaid": (
            "🔒 <b>Ilova kodi yopiq.</b>\n"
            "Avval <b>«💳 5,000 UZS to'lash»</b> orqali <code>{click_card}</code> kartasiga to'lov qiling."
        ),
        "quota_error": "⏳ Bugungi bepul AI so'rovlar limiti tugadi.",
        "ai_busy_error": "😔 AI serverlarida yuklama yuqori. Qayta urinib ko'ring.",
    },
    "ru": {
        "pay_btn": "💳 Оплатить 5,000 UZS",
        "key_btn": "🔑 Получить код активации",
        "lang_btn": "🌐 Сменить язык",
        "lang_chosen": (
            "✅ Язык выбран: <b>Русский</b>.\n"
            "Задавайте вопросы только о боте, оплате, приложении и создателе."
        ),
        "already_approved": (
            "✅ Ваша оплата подтверждена!\n"
            "📱 Код активации: <code>{key}</code>"
        ),
        "already_pending": "⏳ Ваша оплата на проверке! Ожидайте подтверждения.",
        "payment_info": (
            "💳 <b>Оплата (Click):</b>\n\n"
            "💰 Сумма: <b>{price}</b>\n"
            "💳 Карта: <code>{click_card}</code>\n"
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
            "Сначала опла

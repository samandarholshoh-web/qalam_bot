import asyncio
import hashlib
import html
import logging
import os
import re
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

_raw_keys = (
    os.getenv("GEMINI_API_KEYS")
    or os.getenv("GEMINI_API_KEY")
    or os.getenv("GOOGLE_API_KEY")
    or ""
).strip()
GEMINI_API_KEYS: List[str] = [
    k.strip().strip("\"'") for k in _raw_keys.split(",") if k.strip().strip("\"'")
]
if not GEMINI_API_KEYS:
    raise ValueError("GEMINI_API_KEYS yoki GEMINI_API_KEY topilmadi!")

try:
    ADMIN_ID = int((os.getenv("ADMIN_ID") or "9271026986").strip().strip("\"'"))
except ValueError:
    ADMIN_ID = 9271026986

ADMIN_SECRET_CODE = os.getenv("ADMIN_SECRET_CODE", "20070518").strip()
CLICK_CARD = os.getenv("CLICK_CARD", "9860 0801 1915 0045").strip()
CARD_HOLDER = os.getenv("CARD_HOLDER", "QALAM XIZMATI").strip()
PRICE_TEXT = os.getenv("PRICE_TEXT", "5,000 UZS").strip()
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
_clients: List[genai.Client] = [genai.Client(api_key=k) for k in GEMINI_API_KEYS]


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
        "role_prompt": (
            "👋 <b>Qalam botga xush kelibsiz!</b>\n\n"
            "Kerakli bo'limni tanlang:"
        ),
        "user_role_btn": "👤 Foydalanuvchi",
        "admin_role_btn": "🔐 Admin",
        "user_entered": (
            "✅ <b>Foydalanuvchi bo'limi ochildi.</b>\n"
            "Quyidagi tugmalardan foydalaning yoki bot, to'lov, ilova va asoschisi "
            "haqida savol bering."
        ),
        "pay_btn": "💳 5,000 UZS to'lash",
        "key_btn": "🔑 Aktivatsiya kodini olish",
        "lang_btn": "🌐 Tilni o'zgartirish",
        "back_role_btn": "↩️ Bo'lim tanlashga qaytish",
        "lang_chosen": (
            "✅ Til tanlandi: <b>O'zbekcha</b>.\n"
            "Faqat bot, to'lov, ilova va ilova asoschisi haqida savol bering."
        ),
        "already_approved": (
            "✅ To'lovingiz tasdiqlangan!\n"
            "📱 Aktivatsiya kodingiz: <code>{key}</code>"
        ),
        "already_pending": (
            "⏳ To'lovingiz tekshiruvda! Administrator tasdiqlashini kuting."
        ),
        "payment_info": (
            "💳 <b>To'lov ma'lumotlari:</b>\n"
            "ℹ️ <b>Faqat Click orqali to'lov mavjud.</b>\n\n"
            "💰 Summa: <b>{price}</b>\n"
            "💳 Karta raqami: <code>{click_card}</code>\n"
            "👤 Egasi: <b>{holder}</b>\n\n"
            "To'lov qilgach, <b>«✅ To'lov qildim»</b> tugmasini bosing — "
            "ma'lumotlaringiz adminga yuboriladi."
        ),
        "confirm_paid_btn": "✅ To'lov qildim",
        "paid_sent_msg": (
            "⏳ <b>To'lov qilindi va ma'lumotlaringiz adminga yuborildi.</b> "
            "Admin tasdiqlashini kuting."
        ),
        "paid_toast": "To'lov haqida ma'lumot adminga yuborildi.",
        "key_approved": (
            "🔑 <b>Ilova uchun aktivatsiya kodingiz:</b>\n"
            "<code>{key}</code>"
        ),
        "key_pending": "🔒 <b>Ilova kodi hali yopiq.</b> To'lovingiz tekshiruvda.",
        "key_unpaid": (
            "🔒 <b>Ilova kodi yopiq.</b>\n"
            "Faqat Click orqali to'lov mavjud. Avval <b>«💳 5,000 UZS to'lash»</b> "
            "orqali <code>{click_card}</code> kartasiga to'lov qiling."
        ),
        "admin_code_prompt": (
            "🔐 <b>Admin bo'limiga kirish</b>\n\n"
            "Kirish kodini kiriting. To'g'ri kod kiritilmaguncha admin oynasi ochilmaydi:"
        ),
        "admin_code_wrong": (
            "❌ <b>Kod noto'g'ri!</b> Admin bo'limiga kirish rad etildi.\n"
            "Qayta urinib ko'rish uchun <b>«🔐 Admin»</b> tugmasini bosing yoki /start bosing."
        ),
        "admin_stats": (
            "📊 <b>ADMIN PANEL — STATISTIKA</b>\n\n"
            "👥 Botdan foydalanayotganlar: <b>{total_users}</b> ta\n"
            "💰 To'lov qilganlar: <b>{paid_users}</b> ta\n"
            "⏳ Tekshiruvda turganlar: <b>{pending_users}</b> ta\n"
            "💳 To'lov usuli: <b>Faqat Click</b> (<code>{click_card}</code>)"
        ),
        "quota_error": "⏳ Bugungi bepul AI so'rovlar limiti tugadi.",
        "ai_busy_error": "😔 AI serverlarida yuklama yuqori. Qayta urinib ko'ring.",
    },
    "ru": {
        "role_prompt": (
            "👋 <b>Добро пожаловать в Qalam бот!</b>\n\n"
            "Выберите нужный раздел:"
        ),
        "user_role_btn": "👤 Пользователь",
        "admin_role_btn": "🔐 Админ",
        "user_entered": (
            "✅ <b>Открыт раздел пользователя.</b>\n"
            "Воспользуйтесь кнопками ниже или задайте вопрос о боте, оплате, "
            "приложении и создателе."
        ),
        "pay_btn": "💳 Оплатить 5,000 UZS",
        "key_btn": "🔑 Получить код активации",
        "lang_btn": "🌐 Сменить язык",
        "back_role_btn": "↩️ Вернуться к выбору раздела",
        "lang_chosen": (
            "✅ Язык выбран: <b>Русский</b>.\n"
            "Задавайте вопросы только о боте, оплате, приложении и создателе."
        ),
        "already_approved": (
            "✅ Ваша оплата подтверждена!\n"
            "📱 Код активации: <code>{key}</code>"
        ),
        "already_pending": (
            "⏳ Ваша оплата на проверке! Ожидайте подтверждения."
        ),
        "payment_info": (
            "💳 <b>Данные для оплаты:</b>\n"
            "ℹ️ <b>Оплата доступна только через Click.</b>\n\n"
            "💰 Сумма: <b>{price}</b>\n"
            "💳 Номер карты: <code>{click_card}</code>\n"
            "👤 Получатель: <b>{holder}</b>\n\n"
            "После оплаты нажмите <b>«✅ Я оплатил»</b> — "
            "ваши данные будут отправлены администратору."
        ),
        "confirm_paid_btn": "✅ Я оплатил",
        "paid_sent_msg": (
            "⏳ <b>Оплата выполнена, данные отправлены администратору.</b> "
            "Ожидайте подтверждения."
        ),
        "paid_toast": "Данные об оплате отправлены админу.",
        "key_approved": "🔑 <b>Ваш код активации:</b>\n<code>{key}</code>",
        "key_pending": "🔒 <b>Код закрыт.</b> Оплата на проверке.",
        "key_unpaid": (
            "🔒 <b>Код закрыт.</b>\n"
            "Оплата доступна только через Click. Сначала оплатите через "
            "<b>«💳 Оплатить 5,000 UZS»</b> на карту <code>{click_card}</code>."
        ),
        "admin_code_prompt": (
            "🔐 <b>Вход в админ-раздел</b>\n\n"
            "Введите код доступа. Без верного кода доступ закрыт:"
        ),
        "admin_code_wrong": (
            "❌ <b>Неверный код!</b> Доступ к админ-разделу запрещён.\n"
            "Нажмите <b>«🔐 Админ»</b> или /start, чтобы попробовать снова."
        ),
        "admin_stats": (
            "📊 <b>АДМИН-ПАНЕЛЬ — СТАТИСТИКА</b>\n\n"
            "👥 Пользователей бота: <b>{total_users}</b>\n"
            "💰 Оплативших: <b>{paid_users}</b>\n"
            "⏳ На проверке: <b>{pending_users}</b>\n"
            "💳 Способ оплаты: <b>Только Click</b> (<code>{click_card}</code>)"
        ),
        "quota_error": "⏳ Дневной лимит AI-запросов исчерпан.",
        "ai_busy_error": "😔 Серверы ИИ перегружены. Повторите попытку.",
    },
    "en": {
        "role_prompt": (
            "👋 <b>Welcome to Qalam Bot!</b>\n\n"
            "Choose a section:"
        ),
        "user_role_btn": "👤 User",
        "admin_role_btn": "🔐 Admin",
        "user_entered": (
            "✅ <b>User section opened.</b>\n"
            "Use the buttons below or ask questions about the bot, payment, "
            "app, and founder."
        ),
        "pay_btn": "💳 Pay 5,000 UZS",
        "key_btn": "🔑 Get Activation Code",
        "lang_btn": "🌐 Change Language",
        "back_role_btn": "↩️ Back to section selection",
        "lang_chosen": (
            "✅ Language selected: <b>English</b>.\n"
            "Ask questions only about the bot, payment, app, and founder."
        ),
        "already_approved": (
            "✅ Your payment is approved!\n"
            "📱 Your activation code: <code>{key}</code>"
        ),
        "already_pending": (
            "⏳ Your payment is under review! Wait for admin approval."
        ),
        "payment_info": (
            "💳 <b>Payment Details:</b>\n"
            "ℹ️ <b>Payment is available only via Click.</b>\n\n"
            "💰 Amount: <b>{price}</b>\n"
            "💳 Card number: <code>{click_card}</code>\n"
            "👤 Holder: <b>{holder}</b>\n\n"
            "After paying, tap <b>«✅ I Have Paid»</b> — "
            "your details will be sent to the admin."
        ),
        "confirm_paid_btn": "✅ I Have Paid",
        "paid_sent_msg": (
            "⏳ <b>Payment marked as done and sent to admin.</b> "
            "Please wait for confirmation."
        ),
        "paid_toast": "Payment info sent to admin.",
        "key_approved": "🔑 <b>Your activation code:</b>\n<code>{key}</code>",
        "key_pending": "🔒 <b>App code is locked.</b> Payment is under review.",
        "key_unpaid": (
            "🔒 <b>App code is locked.</b>\n"
            "Payment is available only via Click. First pay via "
            "<b>«💳 Pay 5,000 UZS»</b> to card <code>{click_card}</code>."
        ),
        "admin_code_prompt": (
            "🔐 <b>Admin Section Access</b>\n\n"
            "Enter the access code. Admin view stays locked until the right code is entered:"
        ),
        "admin_code_wrong": (
            "❌ <b>Incorrect code!</b> Admin access denied.\n"
            "Tap <b>«🔐 Admin»</b> or /start to try again."
        ),
        "admin_stats": (
            "📊 <b>ADMIN PANEL — STATISTICS</b>\n\n"
            "👥 Active bot users: <b>{total_users}</b>\n"
            "💰 Paid users: <b>{paid_users}</b>\n"
            "⏳ Pending review: <b>{pending_users}</b>\n"
            "💳 Payment method: <b>Click only</b> (<code>{click_card}</code>)"
        ),
        "quota_error": "⏳ Daily free AI limit reached.",
        "ai_busy_error": "😔 AI servers are busy right now. Try again shortly.",
    },
}

SYSTEM_INSTRUCTION = (
    "You are the official assistant for Qalam bot and app. "
    "Answer ONLY questions about the bot, payment (Click card only, 5,000 UZS), activation code, the app, and the app founder. "
    "CRITICAL RULE: The ONLY founder, creator, author, and owner of Qalam bot and app is "
    "Oktamov Samandar (Uzbek: O'ktamov Samandar, Russian: Уктамов Самандар, English: Oktamov Samandar). "
    "Whenever asked who the founder or creator is, state ONLY Oktamov Samandar and NEVER mention any other name. "
    "Reply in the user's selected language."
)


def _get_conn() -> sqlite3.Connection:
    return sqlite3.connect(DB_PATH, timeout=10.0)


def init_db() -> None:
    db_dir = os.path.dirname(DB_PATH)
    if db_dir:
        os.makedirs(db_dir, exist_ok=True)
    with _get_conn() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS users (
                user_id INTEGER PRIMARY KEY,
                username TEXT,
                full_name TEXT,
                lang TEXT DEFAULT 'uz',
                status TEXT DEFAULT 'unpaid',
                activation_key TEXT
            )
            """
        )
        conn.commit()


def ensure_user(user: types.User) -> Dict[str, Any]:
    with _get_conn() as conn:
        conn.row_factory = sqlite3.Row

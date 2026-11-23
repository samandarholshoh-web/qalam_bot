import asyncio
import hashlib
import html
import logging
import os
import re
import sqlite3
import time
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
    WebAppInfo,
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
MINI_APP_URL = (
    os.getenv("MINI_APP_URL")
    or os.getenv("WEBAPP_URL")
    or ""
).strip()

TRIAL_SECONDS = 7 * 24 * 60 * 60  # 1 hafta (7 kun) bepul sinov muddati
DEFAULT_LANG = "uz"
SUPPORTED_LANGS = ("uz", "ru", "en")
SUPPORTED_STUDY_LANGS = ("zh", "ru", "en")
DB_PATH = os.getenv("DB_PATH", "bot_data.db")

MODELS = [
    "gemini-2.5-flash",
    "gemini-3.5-flash-lite",
    "gemini-3.5-flash",
    "gemini-3.8-flash",
]
_unavailable_models: Set[str] = set()
_key_rr_index = 0
_key_rr_lock = asyncio.Lock()

bot = Bot(token=BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
dp = Dispatcher()
_clients: List[genai.Client] = [genai.Client(api_key=k) for k in GEMINI_API_KEYS]


class AuthStates(StatesGroup):
    waiting_admin_code = State()


STUDY_PROGRAMS: Dict[str, Dict[str, Any]] = {
    "zh": {
        "title": "🇨🇳 Xitoy tili (中文)",
        "lesson": (
            "📘 <b>Xitoy tili — 1-Dars (Salomlashish va tanishuv):</b>\n\n"
            "1️⃣ <b>你好</b> (<i>nǐ hǎo</i>) — Salom!\n"
            "2️⃣ <b>你好吗？</b> (<i>nǐ hǎo ma?</i>) — Ishlaringiz qalay?\n"
            "3️⃣ <b>我很好，谢谢！</b> (<i>wǒ hěn hǎo, xièxie!</i>) — Men yaxshiman, rahmat!\n"
            "4️⃣ <b>工作</b> (<i>gōngzuò</i>) — Ish / kasb\n"
            "5️⃣ <b>朋友</b> (<i>péngyou</i>) — Do‘st"
        ),
        "apps": (
            "🧩 <b>Xitoy tili o‘quv dasturlari:</b>\n"
            "• <b>Pinyin va Ohang (Ton) dasturi:</b> 1-dan 4-tongacha mashq\n"
            "• <b>Ieroglif kartochkalari:</b> 5 ta kundalik so‘z va tarjima\n"
            "• <b>AI Suhbat dasturi:</b> Xitoycha muloqot va xatolarni tekshirish"
        ),
        "sample_prompt": "Xitoy tilida salomlashish va kundalik gap tuzishni o‘rgat. Har bir xitoycha gapga pinyin va o‘zbekcha tarjima ber.",
    },
    "ru": {
        "title": "🇷🇺 Rus tili (Русский)",
        "lesson": (
            "📘 <b>Rus tili — 1-Dars (Kundalik muloqot):</b>\n\n"
            "1️⃣ <b>Привет! Как дела?</b> (<i>Privét! Kak delá?</i>) — Salom! Ishlar qalay?\n"
            "2️⃣ <b>Я хорошо понимаю.</b> (<i>Ya khoroshó panimáyu.</i>) — Men yaxshi tushunaman.\n"
            "3️⃣ <b>Работа</b> (<i>rabóta</i>) — Ish\n"
            "4️⃣ <b>Встреча</b> (<i>fstrécha</i>) — Uchrashuv\n"
            "5️⃣ <b>Важный</b> (<i>vázhnyy</i>) — Muhim"
        ),
        "apps": (
            "🧩 <b>Rus tili o‘quv dasturlari:</b>\n"
            "• <b>Talaffuz va urg‘u (ударение) dasturi:</b> To‘g‘ri o‘qish mashqi\n"
            "• <b>Gap tuzish dasturi:</b> Fe’l va otlarni bog‘lash\n"
            "• <b>AI Suhbat dasturi:</b> Ruscha suhbat va grammatik tuzatish"
        ),
        "sample_prompt": "Rus tilida kundalik suhbat va yangi so‘zlar bilan gap tuzishni o‘rgat. Talaffuz va o‘zbekcha tarjima ber.",
    },
    "en": {
        "title": "🇺🇸 Ingliz tili (English)",
        "lesson": (
            "📘 <b>Ingliz tili — 1-Lesson (Everyday English):</b>\n\n"
            "1️⃣ <b>Hello! How are you?</b> (<i>/həˈloʊ haʊ ɑːr juː/</i>) — Salom! Qalaysiz?\n"
            "2️⃣ <b>I improve every day.</b> (<i>/aɪ ɪmˈpruːv ˈevri deɪ/</i>) — Men har kuni rivojlanaman.\n"
            "3️⃣ <b>Work</b> (<i>/wɜːrk/</i>) — Ish\n"
            "4️⃣ <b>Journey</b> (<i>/ˈdʒɜːrni/</i>) — Safar / sayohat\n"
            "5️⃣ <b>Helpful</b> (<i>/ˈhelpfəl/</i>) — Foydali"
        ),
        "apps": (
            "🧩 <b>Ingliz tili o‘quv dasturlari:</b>\n"
            "• <b>Lexical Chunks dasturi:</b> Tayyor iboralar bilan gapirish\n"
            "• <b>Pronunciation (IPA) dasturi:</b> Talaffuzni mashq qilish\n"
            "• <b>AI Suhbat dasturi:</b> Inglizcha muloqot va xatolarni to‘g‘irlash"
        ),
        "sample_prompt": "Ingliz tilida foydali iboralar va kundalik gaplar tuzishni o‘rgat. IPA transkripsiya va o‘zbekcha tarjima ber.",
    },
}


async def _get_ordered_clients() -> List[Tuple[int, genai.Client]]:
    global _key_rr_index
    total = len(_clients)
    async with _key_rr_lock:
        start = _key_rr_index % total
        _key_rr_index = (_key_rr_index + 1) % total
    return [((start + i) % total, _clients[(start + i) % total]) for i in range(total)]


UI_TEXTS: Dict[str, Dict[str, str]] = {
    "uz": {
        "choose_study_prompt": (
            "🎯 <b>Qaysi tilni o‘rganmoqchisiz?</b>\n\n"
            "O‘rganish uchun tilni tanlang — darslar, o‘quv dasturlari va Mini Ilova ochiladi:\n"
            "• 🇨🇳 Xitoy tili\n"
            "• 🇷🇺 Rus tili\n"
            "• 🇺🇸 Ingliz tili\n\n"
            "🎁 <i>Botdan 1 hafta (7 kun) davomida bepul foydalanishingiz mumkin. "
            "1 haftadan keyin davom etish uchun 5,000 UZS to‘lov so‘raladi.</i>"
        ),
        "study_zh_btn": "🇨🇳 Xitoy tili",
        "study_ru_btn": "🇷🇺 Rus tili",
        "study_en_btn": "🇺🇸 Ingliz tili",
        "study_selected": (
            "✅ Tanlangan o‘quv tili: <b>{study_title}</b>\n"
            "⏳ Bepul sinov holati: <b>{trial_info}</b>\n\n"
            "Darsni boshlash, dasturlarni ochish yoki Mini Ilovaga kirish uchun tugmalardan foydalaning:"
        ),
        "lesson_btn": "📖 Darsni boshlash",
        "apps_btn": "🧩 O‘quv dasturlari",
        "change_study_btn": "🎯 Boshqa tilni tanlash",
        "mini_app_btn": "📱 Mini Ilovani ochish",
        "mini_app_missing_url": (
            "ℹ️ <b>Mini Ilova</b> tayyor! Telegram ichida ochish uchun "
            "<code>MINI_APP_URL</code> (HTTPS manzil) sozlang yoki pastdagi "
            "<b>«📖 Darsni boshlash»</b> va <b>«🧩 O‘quv dasturlari»</b> tugmalaridan foydalaning."
        ),
        "trial_active_text": "1 haftalik bepul davr ({days_left} kun qoldi)",
        "trial_expired_msg": (
            "⏳ <b>1 haftalik (7 kunlik) bepul foydalanish muddatingiz yakunlandi.</b>\n\n"
            "Darslar, o‘quv dasturlari va Mini Ilovadan foydalanishni davom ettirish uchun "
            "<b>5,000 UZS</b> to‘lov qiling."
        ),
        "admin_role_btn": "🔐 Admin",
        "pay_btn": "💳 5,000 UZS to'lash",
        "key_btn": "🔑 Aktivatsiya kodini olish",
        "lang_btn": "🌐 Interfeys tili",
        "back_role_btn": "↩️ Til tanlashga qaytish",
        "lang_chosen": "✅ Interfeys tili tanlandi: <b>O'zbekcha</b>.",
        "already_approved": (
            "✅ To'lovingiz tasdiqlangan!\n"
            "📱 Aktivatsiya kodingiz: <code>{key}</code>"
        ),
        "already_pending": (
            "⏳ To'lovingiz tekshiruvda! Administrator tasdiqlashini kuting."
        ),
        "payment_info": (
            "💳 <b>To'lov ma'lumotlari (1 haftalik bepul davrdan keyin):</b>\n"
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
            "1 haftalik bepul sinovdan so‘ng yoki to‘lov orqali faollashadi. "
            "To‘lov uchun <b>«💳 5,000 UZS to'lash»</b> tugmasini bosing (<code>{click_card}</code>)."
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
        "choose_study_prompt": (
            "🎯 <b>Какой язык вы хотите изучать?</b>\n\n"
            "Выберите язык — откроются уроки, программы обучения и Mini App:\n"
            "• 🇨🇳 Китайский язык\n"
            "• 🇷🇺 Русский язык\n"
            "• 🇺🇸 Английский язык\n\n"
            "🎁 <i>Бот доступен бесплатно 1 неделю (7 дней). "
            "Через неделю для продолжения потребуется оплата 5,000 UZS.</i>"
        ),
        "study_zh_btn": "🇨🇳 Китайский язык",
        "study_ru_btn": "🇷🇺 Русский язык",
        "study_en_btn": "🇺🇸 Английский язык",
        "study_selected": (
            "✅ Выбран язык обучения: <b>{study_title}</b>\n"
            "⏳ Бесплатный период: <b>{trial_info}</b>\n\n"
            "Используйте кнопки ниже для уроков, программ и Mini App:"
        ),
        "lesson_btn": "📖 Начать урок",
        "apps_btn": "🧩 Учебные программы",
        "change_study_btn": "🎯 Выбрать другой язык",
        "mini_app_btn": "📱 Открыть Mini App",
        "mini_app_missing_url": (
            "ℹ️ <b>Mini App</b> готов! Укажите HTTPS-ссылку в <code>MINI_APP_URL</code> "
            "или используйте кнопки <b>«📖 Начать урок»</b> и <b>«🧩 Учебные программы»</b>."
        ),
        "trial_active_text": "Бесплатная неделя (осталось {days_left} дн.)",
        "trial_expired_msg": (
            "⏳ <b>Ваш бесплатный период (1 неделя / 7 дней) завершился.</b>\n\n"
            "Чтобы продолжить пользоваться уроками, программами и Mini App, "
            "оплатите <b>5,000 UZS</b>."
        ),
        "admin_role_btn": "🔐 Админ",
        "pay_btn": "💳 Оплатить 5,000 UZS",
        "key_btn": "🔑 Получить код активации",
        "lang_btn": "🌐 Язык интерфейса",
        "back_role_btn": "↩️ К выбору языка",
        "lang_chosen": "✅ Язык интерфейса выбран: <b>Русский</b>.",
        "already_approved": (
            "✅ Ваша оплата подтверждена!\n"
            "📱 Код активации: <code>{key}</code>"
        ),
        "already_pending": (
            "⏳ Ваша оплата на проверке! Ожидайте подтверждения."
        ),
        "payment_info": (
            "💳 <b>Данные для оплаты (после бесплатной недели):</b>\n"
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
            "Оплата доступна только через Click (<b>5,000 UZS</b>) на карту <code>{click_card}</code>."
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
        "choose_study_prompt": (
            "🎯 <b>Which language do you want to learn?</b>\n\n"
            "Choose a language to open lessons, learning apps, and Mini App:\n"
            "• 🇨🇳 Chinese\n"
            "• 🇷🇺 Russian\n"
            "• 🇺🇸 English\n\n"
            "🎁 <i>Free for 1 week (7 days). "
            "After 1 week, a 5,000 UZS payment is required to continue.</i>"
        ),
        "study_zh_btn": "🇨🇳 Chinese",
        "study_ru_btn": "🇷🇺 Russian",
        "study_en_btn": "🇺🇸 English",
        "study_selected": (
            "✅ Selected learning language: <b>{study_title}</b>\n"
            "⏳ Free trial status: <b>{trial_info}</b>\n\n"
            "Use the buttons below to start a lesson, open apps, or launch the Mini App:"
        ),
        "lesson_btn": "📖 Start Lesson",
        "apps_btn": "🧩 Learning Apps",
        "change_study_btn": "🎯 Change Learning Language",
        "mini_app_btn": "📱 Open Mini App",
        "mini_app_missing_url": (
            "ℹ️ <b>Mini App</b> is ready! Set an HTTPS URL in <code>MINI_APP_URL</code> "
            "or use <b>«📖 Start Lesson»</b> and <b>«🧩 Learning Apps»</b> below."
        ),
        "trial_active_text": "1-week free trial ({days_left} days left)",
        "trial_expired_msg": (
            "⏳ <b>Your 1-week (7-day) free trial has ended.</b>\n\n"
            "To continue using lessons, learning apps, and the Mini App, "
            "please pay <b>5,000 UZS</b>."
        ),
        "admin_role_btn": "🔐 Admin",
        "pay_btn": "💳 Pay 5,000 UZS",
        "key_btn": "🔑 Get Activation Code",
        "lang_btn": "🌐 Interface Language",
        "back_role_btn": "↩️ Back to Language Selection",
        "lang_chosen": "✅ Interface language selected: <b>English</b>.",
        "already_approved": (
            "✅ Your payment is approved!\n"
            "📱 Your activation code: <code>{key}</code>"
        ),
        "already_pending": (
            "⏳ Your payment is under review! Wait for admin approval."
        ),
        "payment_info": (
            "💳 <b>Payment Details (after the 1-week free trial):</b>\n"
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
            "Payment is available only via Click (<b>5,000 UZS</b>) to card <code>{click_card}</code>."
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
    "You are the official language tutor and assistant for Qalam bot and app. "
    "Help users learn Chinese (zh), Russian (ru), and English (en) with clear Uzbek explanations and translations when helpful, "
    "and also answer questions about the bot, 1-week free trial, Click payment (5,000 UZS), activation code, and the app. "
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
                study_lang TEXT,
                status TEXT DEFAULT 'unpaid',
                activation_key TEXT,
                first_seen_at INTEGER
            )
            """
        )
        cols = [
            r[1] for r in conn.execute("PRAGMA table_info(users)").fetchall()
        ]
        if "study_lang" not in cols:
            conn.execute("ALTER TABLE users ADD COLUMN study_lang TEXT")
        if "first_seen_at" not in cols:
            conn.execute("ALTER TABLE users ADD COLUMN first_seen_at INTEGER")
        now_ts = int(time.time())
        conn.execute(
            "UPDATE users SET first_seen_at = ? WHERE first_seen_at IS NULL",
            (now_ts,),
        )
        conn.commit()


def ensure_user(user: types.User) -> Dict[str, Any]:
    now_ts = int(time.time())
    with _get_conn() as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT * FROM users WHERE user_id = ?", (user.id,)
        ).fetchone()
        name = (user.full_name or "").strip()
        uname = user.username or ""
        if not row:
            conn.execute(
                """
                INSERT INTO users (user_id, username, full_name, lang, study_lang, status, first_seen_at)
                VALUES (?, ?, ?, ?, NULL, 'unpaid', ?)
                """,
                (user.id, uname, name, DEFAULT_LANG, now_ts),
            )
            conn.commit()
            row = conn.execute(
                "SELECT * FROM users WHERE user_id = ?", (user.id,)
            ).fetchone()
        else:
            first_seen = row["first_seen_at"] or now_ts
            conn.execute(
                "UPDATE users SET username = ?, full_name = ?, first_seen_at = ? WHERE user_id = ?",
                (uname, name, first_seen, user.id),
            )
            conn.commit()
            row = conn.execute(
                "SELECT * FROM users WHERE user_id = ?", (user.id,)
            ).fetchone()
        return dict(row)


def set_user_lang(user_id: int, lang: str) -> None:
    chosen = lang if lang in SUPPORTED_LANGS else DEFAULT_LANG
    with _get_conn() as conn:
        conn.execute(
            "UPDATE users SET lang = ? WHERE user_id = ?", (chosen, user_id)
        )
        conn.commit()


def set_user_study_lang(user_id: int, study_lang: str) -> None:
    chosen = study_lang if study_lang in SUPPORTED_STUDY_LANGS else "en"
    with _get_conn() as conn:
        conn.execute(
            "UPDATE users SET study_lang = ? WHERE user_id = ?",
            (chosen, user_id),
        )
        conn.commit()


def set_user_status(
    user_id: int, status: str, activation_key: Optional[str] = None
) -> None:
    with _get_conn() as conn:
        if activation_key is not None:
            conn.execute(
                "UPDATE users SET status = ?, activation_key = ? WHERE user_id = ?",
                (status, activation_key, user_id),
            )
        else:
            conn.execute(
                "UPDATE users SET status = ? WHERE user_id = ?", (status, user_id)
            )
        conn.commit()


def has_active_access(user_row: Dict[str, Any]) -> Tuple[bool, int]:
    """Returns (has_access, days_left_in_trial)."""
    status = user_row.get("status") or "unpaid"
    if status == "approved":
        return True, 0
    first_seen = int(user_row.get("first_seen_at") or int(time.time()))
    elapsed = max(0, int(time.time()) - first_seen)
    remaining = max(0, TRIAL_SECONDS - elapsed)
    days_left = max(1, (remaining + 86399) // 86400) if remaining > 0 else 0
    return remaining > 0, days_left


def get_bot_stats() -> Tuple[int, int, int]:
    with _get_conn() as conn:
        total_users = conn.execute("SELECT COUNT(*) FROM users").fetchone()[0]
        paid_users = conn.execute(
            "SELECT COUNT(*) FROM users WHERE status = 'approved'"
        ).fetchone()[0]
        pending_users = conn.execute(
            "SELECT COUNT(*) FROM users WHERE status = 'pending'"
        ).fetchone()[0]
    return int(total_users), int(paid_users), int(pending_users)


def generate_activation_key(user_id: int) -> str:
    digest = hashlib.sha256(
        f"qalam:{user_id}:{ADMIN_SECRET_CODE}".encode()
    ).hexdigest().upper()
    return f"QALAM-{digest[:4]}-{digest[4:8]}-{digest[8:12]}"


def get_txt(lang: str, key: str, **kwargs: Any) -> str:
    pack = UI_TEXTS.get(lang) or UI_TEXTS[DEFAULT_LANG]
    tpl = pack.get(key) or UI_TEXTS[DEFAULT_LANG][key]
    return tpl.format(**kwargs) if kwargs else tpl


def format_safe_html(text: str) -> str:
    escaped = html.escape(text)
    return re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", escaped)


def _is_valid_https_url(url: str) -> bool:
    return url.startswith("https://") and len(url) > len("https://")


def build_mini_app_url(study_lang: Optional[str]) -> str:
    if not _is_valid_https_url(MINI_APP_URL):
        return ""
    if study_lang in SUPPORTED_STUDY_LANGS:
        sep = "&" if "?" in MINI_APP_URL else "?"
        return f"{MINI_APP_URL}{sep}lang={study_lang}"
    return MINI_APP_URL


def mini_app_inline_keyboard(
    lang: str, study_lang: Optional[str] = None
) -> Optional[InlineKeyboardMarkup]:
    url = build_mini_app_url(study_lang)
    if not url:
        return None
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=get_txt(lang, "mini_app_btn"),
                    web_app=WebAppInfo(url=url),
                )
            ]
        ]
    )


def study_language_keyboard(lang: str) -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [
                KeyboardButton(text=get_txt(lang, "study_zh_btn")),
                KeyboardButton(text=get_txt(lang, "study_ru_btn")),
                KeyboardButton(text=get_txt(lang, "study_en_btn")),
            ],
            [
                KeyboardButton(text=get_txt(lang, "pay_btn")),
                KeyboardButton(text=get_txt(lang, "key_btn")),
            ],
            [
                KeyboardButton(text=get_txt(lang, "lang_btn")),
                KeyboardButton(text=get_txt(lang, "admin_role_btn")),
            ],
        ],
        resize_keyboard=True,
    )


def study_workspace_keyboard(
    lang: str, study_lang: Optional[str]
) -> ReplyKeyboardMarkup:
    app_url = build_mini_app_url(study_lang)
    rows: List[List[KeyboardButton]] = [
        [
            KeyboardButton(text=get_txt(lang, "lesson_btn")),
            KeyboardButton(text=get_txt(lang, "apps_btn")),
        ]
    ]
    if app_url:
        rows.append(
            [
                KeyboardButton(
                    text=get_txt(lang, "mini_app_btn"),
                    web_app=WebAppInfo(url=app_url),
                )
            ]
        )
    else:
        rows.append([KeyboardButton(text=get_txt(lang, "mini_app_btn"))])

    rows.append(
        [
            KeyboardButton(text=get_txt(lang, "change_study_btn")),
            KeyboardButton(text=get_txt(lang, "pay_btn")),
        ]
    )
    rows.append(
        [
            KeyboardButton(text=get_txt(lang, "key_btn")),
            KeyboardButton(text=get_txt(lang, "lang_btn")),
        ]
    )
    return ReplyKeyboardMarkup(keyboard=rows, resize_keyboard=True)


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


def admin_approval_keyboard(uid: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="✅ Tasdiqlash", callback_data=f"admin_approve:{uid}"
                ),
                InlineKeyboardButton(
                    text="❌ Rad etish", callback_data=f"admin_reject:{uid}"
                ),
            ]
        ]
    )


async def send_payment_prompt(message: Message, lang: str) -> None:
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


async def ask_gemini(
    prompt: str, lang: str, study_lang: Optional[str] = None
) -> str:
    clients = await _get_ordered_clients()
    quota_err = False
    study_ctx = f" Target learning language: {study_lang}." if study_lang else ""
    for model in MODELS:
        if model in _unavailable_models:
            continue
        for attempt in range(2):
            for _idx, client in clients:
                try:
                    res = await asyncio.wait_for(
                        client.aio.models.generate_content(
                            model=model,
                            contents=f"[Interface Language: {lang}]{study_ctx}\n{prompt}",
                            config=genai_types.GenerateContentConfig(
                                system_instruction=SYSTEM_INSTRUCTION,
                                temperature=0.3,
                            ),
                        ),
                        timeout=12.0,
                    )
                    if getattr(res, "text", None):
                        return res.text.strip()
                except Exception as exc:
                    err = str(exc).lower()
                    if "404" in err or "not_found" in err or "not found" in err:
                        _unavailable_models.add(model)
                        break
                    if (
                        "429" in err
                        or "quota" in err
                        or "resource_exhausted" in err
                    ):
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
    ensure_user(message.from_user)
    await message.answer(
        "🌐 Tilni tanlang / Выберите язык / Choose language:",
        reply_markup=language_keyboard(),
    )


@dp.callback_query(F.data.startswith("lang:"))
async def cb_choose_lang(callback: CallbackQuery, state: FSMContext) -> None:
    if not callback.from_user or not callback.data:
        return
    await state.clear()
    u = ensure_user(callback.from_user)
    lang = callback.data.split(":", 1)[1].strip()
    lang = lang if lang in SUPPORTED_LANGS else DEFAULT_LANG
    set_user_lang(callback.from_user.id, lang)
    u["lang"] = lang

    if callback.message:
        try:
            await callback.message.edit_text(get_txt(lang, "lang_chosen"))
        except TelegramBadRequest:
            await callback.message.answer(get_txt(lang, "lang_chosen"))

        allowed, _days_left = has_active_access(u)
        if not allowed:
            await callback.message.answer(
                get_txt(lang, "trial_expired_msg"),
                reply_markup=study_language_keyboard(lang),
            )
            await send_payment_prompt(callback.message, lang)
        else:
            await callback.message.answer(
                get_txt(lang, "choose_study_prompt"),
                reply_markup=study_language_keyboard(lang),
            )
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
            await callback.message.answer(
                get_txt(lang, "already_approved", key=html.escape(key))
            )
        return
    set_user_status(callback.from_user.id, "pending")
    await callback.answer(get_txt(lang, "paid_toast"))
    if callback.message:
        await callback.message.answer(get_txt(lang, "paid_sent_msg"))
    if ADMIN_ID > 0:
        uname = (
            f"@{callback.from_user.username}"
            if callback.from_user.username
            else "yo'q"
        )
        admin_text = (
            "🆕 <b>Yangi to'lov qilindi (1 haftalik davrdan keyin / Click):</b>\n\n"
            f"👤 Ism: <b>{html.escape(callback.from_user.full_name or 'User')}</b>\n"
            f"🔗 Username: {html.escape(uname)}\n"
            f"🆔 ID: <code>{callback.from_user.id}</code>\n"
            f"💳 Karta: <code>{html.escape(CLICK_CARD)}</code>\n"
            f"💰 Summa:

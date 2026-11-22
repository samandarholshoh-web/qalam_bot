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
        row = conn.execute(
            "SELECT * FROM users WHERE user_id = ?", (user.id,)
        ).fetchone()
        name = (user.full_name or "").strip()
        uname = user.username or ""
        if not row:
            conn.execute(
                "INSERT INTO users (user_id, username, full_name, lang, status) VALUES (?, ?, ?, ?, 'unpaid')",
                (user.id, uname, name, DEFAULT_LANG),
            )
            conn.commit()
            row = conn.execute(
                "SELECT * FROM users WHERE user_id = ?", (user.id,)
            ).fetchone()
        else:
            conn.execute(
                "UPDATE users SET username = ?, full_name = ? WHERE user_id = ?",
                (uname, name, user.id),
            )
            conn.commit()
        return dict(row)


def set_user_lang(user_id: int, lang: str) -> None:
    chosen = lang if lang in SUPPORTED_LANGS else DEFAULT_LANG
    with _get_conn() as conn:
        conn.execute(
            "UPDATE users SET lang = ? WHERE user_id = ?", (chosen, user_id)
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


def role_selection_keyboard(lang: str) -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [
                KeyboardButton(text=get_txt(lang, "user_role_btn")),
                KeyboardButton(text=get_txt(lang, "admin_role_btn")),
            ],
            [KeyboardButton(text=get_txt(lang, "lang_btn"))],
        ],
        resize_keyboard=True,
    )


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


def user_menu_keyboard(lang: str) -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [
                KeyboardButton(text=get_txt(lang, "pay_btn")),
                KeyboardButton(text=get_txt(lang, "key_btn")),
            ],
            [
                KeyboardButton(text=get_txt(lang, "lang_btn")),
                KeyboardButton(text=get_txt(lang, "back_role_btn")),
            ],
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


async def ask_gemini(prompt: str, lang: str) -> str:
    clients = await _get_ordered_clients()
    quota_err = False
    for model in MODELS:
        if model in _unavailable_models:
            continue
        for attempt in range(2):
            for _idx, client in clients:
                try:
                    res = await asyncio.wait_for(
                        client.aio.models.generate_content(
                            model=model,
                            contents=f"[Language: {lang}]\n{prompt}",
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
    lang = ensure_user(message.from_user).get("lang") or DEFAULT_LANG
    await message.answer(
        "🌐 Tilni tanlang / Выберите язык / Choose language:",
        reply_markup=language_keyboard(),
    )
    await message.answer(
        get_txt(lang, "role_prompt"),
        reply_markup=role_selection_keyboard(lang),
    )


@dp.callback_query(F.data.startswith("lang:"))
async def cb_choose_lang(callback: CallbackQuery, state: FSMContext) -> None:
    if not callback.from_user or not callback.data:
        return
    await state.clear()
    ensure_user(callback.from_user)
    lang = callback.data.split(":", 1)[1].strip()
    lang = lang if lang in SUPPORTED_LANGS else DEFAULT_LANG
    set_user_lang(callback.from_user.id, lang)
    if callback.message:
        try:
            await callback.message.edit_text(get_txt(lang, "lang_chosen"))
        except TelegramBadRequest:
            await callback.message.answer(get_txt(lang, "lang_chosen"))
        await callback.message.answer(
            get_txt(lang, "role_prompt"),
            reply_markup=role_selection_keyboard(lang),
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
            "🆕 <b>Yangi to'lov qilindi (Faqat Click):</b>\n\n"
            f"👤 Ism: <b>{html.escape(callback.from_user.full_name or 'User')}</b>\n"
            f"🔗 Username: {html.escape(uname)}\n"
            f"🆔 ID: <code>{callback.from_user.id}</code>\n"
            f"💳 Karta: <code>{html.escape(CLICK_CARD)}</code>\n"
            f"💰 Summa: <b>{html.escape(PRICE_TEXT)}</b>"
        )
        try:
            await bot.send_message(
                ADMIN_ID,
                admin_text,
                reply_markup=admin_approval_keyboard(callback.from_user.id),
            )
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
    with _get_conn() as conn:
        row = conn.execute(
            "SELECT lang FROM users WHERE user_id = ?", (target_id,)
        ).fetchone()
    t_lang = row[0] if row and row[0] in SUPPORTED_LANGS else DEFAULT_LANG
    try:
        await bot.send_message(
            target_id, get_txt(t_lang, "key_approved", key=html.escape(key))
        )
    except (TelegramForbiddenError, TelegramBadRequest):
        pass
    if callback.message:
        try:
            await callback.message.edit_text(
                (callback.message.html_text or "")
                + f"\n\n✅ <b>Tasdiqlandi:</b> <code>{html.escape(key)}</code>"
            )
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
        await bot.send_message(
            target_id, "❌ To'lov tasdiqlanmadi. Tekshirib qayta urinib ko'ring."
        )
    except (TelegramForbiddenError, TelegramBadRequest):
        pass
    if callback.message:
        try:
            await callback.message.edit_text(
                (callback.message.html_text or "") + "\n\n❌ <b>Rad etildi.</b>"
            )
        except TelegramBadRequest:
            pass
    await callback.answer("Rad etildi ❌")


@dp.message(AuthStates.waiting_admin_code, F.text)
async def handle_admin_code(message: Message, state: FSMContext) -> None:
    if not message.from_user or not message.text:
        return
    u = ensure_user(message.from_user)
    lang = u.get("lang") or DEFAULT_LANG
    entered_code = message.text.strip()

    if entered_code != ADMIN_SECRET_CODE:
        await state.clear()
        await message.answer(
            get_txt(lang, "admin_code_wrong"),
            reply_markup=role_selection_keyboard(lang),
        )
        return

    await state.clear()
    total_users, paid_users, pending_users = get_bot_stats()
    await message.answer(
        get_txt(
            lang,
            "admin_stats",
            total_users=total_users,
            paid_users=paid_users,
            pending_users=pending_users,
            click_card=html.escape(CLICK_CARD),
        ),
        reply_markup=role_selection_keyboard(lang),
    )


@dp.message(F.text)
async def handle_text(message: Message, state: FSMContext) -> None:
    if not message.from_user or not message.text:
        return
    text = message.text.strip()
    u = ensure_user(message.from_user)
    lang = u.get("lang") or DEFAULT_LANG
    status = u.get("status") or "unpaid"
    key = u.get("activation_key") or generate_activation_key(message.from_user.id)

    if text in {UI_TEXTS[lang_code]["user_role_btn"] for lang_code in SUPPORTED_LANGS}:
        await state.clear()
        await message.answer(
            get_txt(lang, "user_entered"),
            reply_markup=user_menu_keyboard(lang),
        )
        return

    if text in {UI_TEXTS[lang_code]["admin_role_btn"] for lang_code in SUPPORTED_LANGS}:
        await state.set_state(AuthStates.waiting_admin_code)
        await message.answer(
            get_txt(lang, "admin_code_prompt"),
            reply_markup=role_selection_keyboard(lang),
        )
        return

    if text in {UI_TEXTS[lang_code]["back_role_btn"] for lang_code in SUPPORTED_LANGS}:
        await state.clear()
        await message.answer(
            get_txt(lang, "role_prompt"),
            reply_markup=role_selection_keyboard(lang),
        )
        return

    if text in {UI_TEXTS[lang_code]["pay_btn"] for lang_code in SUPPORTED_LANGS}:
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
        return

    if text in {UI_TEXTS[lang_code]["key_btn"] for lang_code in SUPPORTED_LANGS}:
        if status == "approved":
            set_user_status(message.from_user.id, "approved", key)
            await message.answer(get_txt(lang, "key_approved", key=html.escape(key)))
        elif status == "pending":
            await message.answer(get_txt(lang, "key_pending"))
        else:
            await message.answer(
                get_txt(lang, "key_unpaid", click_card=html.escape(CLICK_CARD))
            )
        return

    if text in {UI_TEXTS[lang_code]["lang_btn"] for lang_code in SUPPORTED_LANGS}:
        await state.clear()
        await message.answer(
            "🌐 Tilni tanlang / Выберите язык / Choose language:",
            reply_markup=language_keyboard(),
        )
        return

    await bot.send_chat_action(message.chat.id, "typing")
    reply = await ask_gemini(text, lang)
    await message.answer(format_safe_html(reply))


async def main() -> None:
    init_db()
    logger.info("qalam_bot ishga tushmoqda...")
    try:
        await bot.delete_webhook(drop_pending_updates=False)
        await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())
    finally:
        await bot.session.close()
        for client in _clients:
            try:
                await client.aio.aclose()
            except Exception:
                pass


if __name__ == "__main__":
    asyncio.run(main())

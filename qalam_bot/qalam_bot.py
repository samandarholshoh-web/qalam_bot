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
            "Весь твой ответ должен быть написан исключительно на русском языке."
        )
    return (
        "CRITICAL LANGUAGE RULE: You MUST respond STRICTLY and ONLY in English. "
        "Never switch to Uzbek or Russian, even if the user includes words or phrases in another language. "
        "Your entire response must be written exclusively in clear English."
    )


async def log_available_models() -> None:
    try:
        models = await asyncio.wait_for(
            asyncio.to_thread(_clients[0].models.list),
            timeout=10.0,
        )
        model_names = [getattr(m, "name", str(m)) for m in models]
        logger.info("Mavjud Gemini modellari (%d ta kalit): %s", len(_clients), model_names)
    except Exception as e:
        logger.error("Modellarni olishda xatolik yuz berdi: %s", repr(e))


def _is_503_error(err: Exception) -> bool:
    err_str = str(err).upper()
    return "503" in err_str or "UNAVAILABLE" in err_str


def _is_404_error(err: Exception) -> bool:
    err_str = str(err).upper()
    return "404" in err_str or "NOT_FOUND" in err_str


def _is_429_error(err: Exception) -> bool:
    err_str = str(err).upper()
    return "429" in err_str or "RESOURCE_EXHAUSTED" in err_str or "QUOTA" in err_str


async def generate_ai_reply(prompt: str, lang: str = DEFAULT_LANG) -> str:
    clean_lang = _normalize_lang(lang)
    system_instruction = _build_strict_language_instruction(clean_lang)
    config = genai_types.GenerateContentConfig(
        system_instruction=system_instruction,
    )

    candidate_models = [m for m in MODELS if m not in _unavailable_models]
    if not candidate_models:
        _unavailable_models.clear()
        candidate_models = list(MODELS)

    ordered_clients = await _get_ordered_clients()
    attempt_logs: List[str] = []

    for model_name in candidate_models:
        skip_model = False
        for key_idx, ai_client in ordered_clients:
            for attempt in range(MAX_RETRIES_PER_MODEL):
                try:
                    response = await asyncio.wait_for(
                        ai_client.aio.models.generate_content(
                            model=model_name,
                            contents=prompt,
                            config=config,
                        ),
                        timeout=REQUEST_TIMEOUT_SECONDS,
                    )
                    text = getattr(response, "text", None)
                    if text and text.strip():
                        logger.info(
                            "AI javobi olindi: model=%s, lang=%s, key=%d, urinish=%d/%d",
                            model_name,
                            clean_lang,
                            key_idx,
                            attempt + 1,
                            MAX_RETRIES_PER_MODEL,
                        )
                        return text.strip()
                    raise RuntimeError("Model bo'sh javob qaytardi.")
                except asyncio.TimeoutError:
                    attempt_logs.append(f"{model_name}[k{key_idx}:{attempt + 1}]=TIMEOUT")
                    logger.warning(
                        "Gemini timeout (%ss): model=%s, lang=%s, key=%d, urinish=%d/%d",
                        REQUEST_TIMEOUT_SECONDS,
                        model_name,
                        clean_lang,
                        key_idx,
                        attempt + 1,
                        MAX_RETRIES_PER_MODEL,
                    )
                except Exception as e:
                    if _is_404_error(e):
                        _unavailable_models.add(model_name)
                        attempt_logs.append(f"{model_name}[k{key_idx}]=404_NOT_FOUND")
                        logger.warning(
                            "Model topilmadi (404 NOT_FOUND), keyingi modelga o'tilmoqda: model=%s",
                            model_name,
                        )
                        skip_model = True
                        break

                    if _is_429_error(e):
                        attempt_logs.append(f"{model_name}[k{key_idx}]=429_QUOTA")
                        logger.warning(
                            "Gemini 429 Quota limiti tugadi (key=%d, model=%s) — keyingi kalit/modelga o'tilmoqda.",
                            key_idx,
                            model_name,
                        )
                        break

                    if _is_503_error(e):
                        attempt_logs.append(f"{model_name}[k{key_idx}:{attempt + 1}]=503_UNAVAILABLE")
                        logger.warning(
                            "Gemini 503 UNAVAILABLE xatosi: model=%s, key=%d, urinish=%d/%d",
                            model_name,
                            key_idx,
                            attempt + 1,
                            MAX_RETRIES_PER_MODEL,
                        )
                    else:
                        attempt_logs.append(f"{model_name}[k{key_idx}:{attempt + 1}]={type(e).__name__}")
                        logger.error(
                            "Gemini xatosi: model=%s, key=%d, urinish=%d/%d, tafsilot=%s",
                            model_name,
                            key_idx,
                            attempt + 1,
                            MAX_RETRIES_PER_MODEL,
                            repr(e),
                        )

                if attempt < MAX_RETRIES_PER_MODEL - 1:
                    delay = BACKOFF_DELAYS[min(attempt, len(BACKOFF_DELAYS) - 1)]
                    await asyncio.sleep(delay)

            if skip_model:
                break

    raise RuntimeError(f"Barcha modellar muvaffaqiyatsiz tugadi: {', '.join(attempt_logs)}")


# 6. Tugmalar, til tanlash va to'lov/aktivatsiya oqimi
@dp.message(CommandStart())
async def handle_start(message: Message) -> None:
    if not message.from_user:
        return
    user_data = await asyncio.to_thread(get_or_create_user, message.from_user)
    lang = _normalize_lang(user_data.get("lang"))

    greeting = (
        "Assalomu alaykum! <b>Qalam</b> sun'iy intellekt botiga xush kelibsiz.\n"
        "Здравствуйте! Добро пожаловать в ИИ-бот <b>Qalam</b>.\n"
        "Hello! Welcome to <b>Qalam</b> AI bot.\n\n"
        "Iltimos, javob tilini tanlang / Выберите язык ответа / Please choose your language:"
    )
    await message.answer(
        greeting,
        parse_mode="HTML",
        reply_markup=language_inline_keyboard(),
    )
    await message.answer(
        "👇",
        reply_markup=build_main_keyboard(lang),
    )


@dp.callback_query(F.data.startswith("set_lang:"))
async def handle_set_language(callback: CallbackQuery) -> None:
    if not callback.from_user or not callback.data:
        return
    selected_lang = _normalize_lang(callback.data.split(":", 1)[1])
    await asyncio.to_thread(get_or_create_user, callback.from_user)
    await asyncio.to_thread(set_user_lang, callback.from_user.id, selected_lang)

    texts = _get_lang_texts(selected_lang)
    if callback.message:
        try:
            await callback.message.edit_text(
                texts["lang_chosen"],
                parse_mode="HTML",
            )
        except Exception as e:
            logger.warning("Til xabarini yangilashda xato: %s", repr(e))

    try:
        await bot.send_message(
            chat_id=callback.from_user.id,
            text=texts["lang_chosen"],
            parse_mode="HTML",
            reply_markup=build_main_keyboard(selected_lang),
        )
    except Exception as e:
        logger.warning("Asosiy klaviaturani yuborishda xato: %s", repr(e))

    await safe_answer_callback(callback, "OK")


@dp.message(F.text.in_(ALL_PAY_BUTTON_TEXTS))
async def handle_payment_menu(message: Message) -> None:
    if not message.from_user:
        return
    user_data = await asyncio.to_thread(get_or_create_user, message.from_user)
    lang = _normalize_lang(user_data.get("lang"))
    texts = _get_lang_texts(lang)
    kb = build_main_keyboard(lang)

    if user_data["status"] == "approved" and user_data["activation_key"]:
        safe_key = html.escape(str(user_data["activation_key"]))
        await message.answer(
            texts["already_approved"].format(key=safe_key),
            parse_mode="HTML",
            reply_markup=kb,
        )
        return

    if user_data["status"] == "pending":
        await message.answer(
            texts["already_pending"],
            parse_mode="HTML",
            reply_markup=kb,
        )
        return

    safe_card = html.escape(CLICK_CARD)
    safe_holder = html.escape(CARD_HOLDER)
    safe_price = html.escape(PRICE_TEXT)

    payment_text = texts["payment_info"].format(
        price=safe_price,
        click_card=safe_card,
        holder=safe_holder,
    )
    await message.answer(
        payment_text,
        parse_mode="HTML",
        reply_markup=payment_inline_keyboard(lang),
    )


@dp.callback_query(F.data == "paid_confirm")
async def handle_paid_confirm(callback: CallbackQuery) -> None:
    if not callback.from_user:
        return
    user_data = await asyncio.to_thread(get_or_create_user, callback.from_user)
    user_id = user_data["user_id"]
    lang = _normalize_lang(user_data.get("lang"))
    texts = _get_lang_texts(lang)
    full_name = user_data["full_name"] or "Noma'lum"
    username_display = f"@{user_data['username']}" if user_data["username"] else "yo'q"

    await asyncio.to_thread(set_user_status, user_id, "pending", None)

    if callback.message:
        try:
            await callback.message.edit_text(
                texts["paid_sent_msg"],
                parse_mode="HTML",
            )
        except Exception as e:
            logger.warning("Xabarni yangilashda xato: %s", repr(e))

    admin_text = (
        "📥 <b>Yangi Click to'lov so'rovi!</b>\n\n"
        f"👤 Ism: <b>{html.escape(full_name)}</b>\n"
        f"🔗 Username: {html.escape(username_display)}\n"
        f"🆔 ID: <code>{user_id}</code>\n"
        f"🌐 Til: <code>{html.escape(lang.upper())}</code>\n"
        f"💳 Click karta: <code>{html.escape(CLICK_CARD)}</code>\n"
        f"💰 Summa: <b>{html.escape(PRICE_TEXT)}</b>"
    )
    await notify_admin(
        text=admin_text,
        reply_markup=admin_approval_keyboard(user_id),
    )

    await safe_answer_callback(callback, texts["paid_toast"], show_alert=False)


@dp.callback_query(F.data.startswith("approve_pay:"))
async def handle_approve_payment(callback: CallbackQuery) -> None:
    if not callback.from_user or callback.from_user.id != ADMIN_ID:
        await safe_answer_callback(callback, "Bu amal faqat administrator uchun!", show_alert=True)
        return

    if not callback.data:
        return

    try:
        target_user_id = int(callback.data.split(":", 1)[1])
    except ValueError:
        await safe_answer_callback(callback, "Noto'g'ri foydalanuvchi ID.", show_alert=True)
        return

    target_user = await asyncio.to_thread(get_user_by_id, target_user_id)
    if not target_user:
        await safe_answer_callback(callback, "Foydalanuvchi bazadan topilmadi.", show_alert=True)
        return

    target_lang = _normalize_lang(target_user.get("lang"))
    activation_key = generate_key_for_user(target_user_id)
    await asyncio.to_thread(set_user_status, target_user_id, "approved", activation_key)

    if callback.message:
        try:
            await callback.message.edit_text(
                f"✅ <b>To'lov tasdiqlandi!</b>\n"
                f"🆔 Foydalanuvchi ID: <code>{target_user_id}</code>\n"
                f"🔑 Kalit: <code>{html.escape(activation_key)}</code>",
                parse_mode="HTML",
            )
        except Exception as e:
            logger.warning("Admin xabarini yangilashda xato: %s", repr(e))

    user_approved_messages = {
        "uz": (
            "🎉 <b>Tabriklaymiz! Click to'lovingiz tasdiqlandi.</b>\n\n"
            f"🔑 Sizning aktivatsiya kodingiz:\n"
            f"<code>{html.escape(activation_key)}</code>"
        ),
        "ru": (
            "🎉 <b>Поздравляем! Ваша оплата Click подтверждена.</b>\n\n"
            f"🔑 Ваш код активации:\n"
            f"<code>{html.escape(activation_key)}</code>"
        ),
        "en": (
            "🎉 <b>Congratulations! Your Click payment has been approved.</b>\n\n"
            f"🔑 Your activation code:\n"
            f"<code>{html.escape(activation_key)}</code>"
        ),
    }

    try:
        await bot.send_message(
            chat_id=target_user_id,
            text=user_approved_messages[target_lang],
            parse_mode="HTML",
            reply_markup=build_main_keyboard(target_lang),
        )
    except Exception as e:
        logger.warning("Foydalanuvchiga kalit yuborishda xato (user_id=%s): %s", target_user_id, repr(e))

    await safe_answer_callback(callback, "To'lov tasdiqlandi va kalit yuborildi.")


@dp.callback_query(F.data.startswith("reject_pay:"))
async def handle_reject_payment(callback: CallbackQuery) -> None:
    if not callback.from_user or callback.from_user.id != ADMIN_ID:
        await safe_answer_callback(callback, "Bu amal faqat administrator uchun!", show_alert=True)
        return

    if not callback.data:
        return

    try:
        target_user_id = int(callback.data.split(":", 1)[1])
    except ValueError:
        await safe_answer_callback(callback, "Noto'g'ri foydalanuvchi ID.", show_alert=True)
        return

    target_user = await asyncio.to_thread(get_user_by_id, target_user_id)
    if not target_user:
        await safe_answer_callback(callback, "Foydalanuvchi bazadan topilmadi.", show_alert=True)
        return

    target_lang = _normalize_lang(target_user.get("lang"))
    await asyncio.to_thread(set_user_status, target_user_id, "unpaid", None)

    if callback.message:
        try:
            await callback.message.edit_text(
                f"❌ <b>To'lov rad etildi.</b>\n"
                f"🆔 Foydalanuvchi ID: <code>{target_user_id}</code>",
                parse_mode="HTML",
            )
        except Exception as e:
            logger.warning("Admin rad etish xabarini yangilashda xato: %s", repr(e))

    user_rejected_messages = {
        "uz": "❌ <b>To'lovingiz tasdiqlanmadi.</b>\nIltimos, to'lov ma'lumotlarini tekshiring yoki qayta urinib ko'ring.",
        "ru": "❌ <b>Оплата не подтверждена.</b>\nПожалуйста, проверьте реквизиты оплаты или попробуйте снова.",
        "en": "❌ <b>Payment was not approved.</b>\nPlease check your payment details or try again.",
    }

    try:
        await bot.send_message(
            chat_id=target_user_id,
            text=user_rejected_messages[target_lang],
            parse_mode="HTML",
            reply_markup=build_main_keyboard(target_lang),
        )
    except Exception as e:
        logger.warning("Foydalanuvchiga rad etish xabarini yuborishda xato (user_id=%s): %s", target_user_id, repr(e))

    await safe_answer_callback(callback, "To'lov rad etildi.")


@dp.message(F.text.in_(ALL_KEY_BUTTON_TEXTS))
async def handle_get_activation_key(message: Message) -> None:
    if not message.from_user:
        return
    user_data = await asyncio.to_thread(get_or_create_user, message.from_user)
    lang = _normalize_lang(user_data.get("lang"))
    texts = _get_lang_texts(lang)
    kb = build_main_keyboard(lang)

    status = user_data["status"]
    activation_key = user_data["activation_key"]

    if status == "approved" and activation_key:
        await message.answer(
            texts["key_approved"].format(key=html.escape(str(activation_key))),
            parse_mode="HTML",
            reply_markup=kb,
        )
    elif status == "pending":
        await message.answer(
            texts["key_pending"],
            parse_mode="HTML",
            reply_markup=kb,
        )
    else:
        await message.answer(
            texts["key_unpaid"].format(click_card=html.escape(CLICK_CARD)),
            parse_mode="HTML",
            reply_markup=kb,
        )


@dp.message(F.text)
async def handle_ai_message(message: Message) -> None:
    if not message.from_user or not message.text:
        return
    prompt = message.text.strip()
    if not prompt:
        return

    user_id = message.from_user.id
    user_data = await asyncio.to_thread(get_or_create_user, message.from_user)
    lang = _normalize_lang(user_data.get("lang"))
    texts = _get_lang_texts(lang)
    kb = build_main_keyboard(lang)

    try:
        await bot.send_chat_action(chat_id=message.chat.id, action="typing")
    except Exception as e:
        logger.warning("Typing holatini yuborishda xato (user_id=%s): %s", user_id, repr(e))

    try:
        reply_text = await generate_ai_reply(prompt, lang=lang)
    except Exception as e:
        err_str = str(e)
        logger.error("AI javob qaytarishda xatolik (user_id=%s, lang=%s): %s", user_id, lang, repr(e))
        await notify_admin(
            f"⚠️ <b>AI xizmati xatosi</b>\n"
            f"🆔 User ID: <code>{user_id}</code>\n"
            f"🌐 Til: <code>{html.escape(lang.upper())}</code>\n"
            f"📄 Tafsilot: <code>{html.escape(err_str[:300])}</code>"
        )
        if "429_QUOTA" in err_str:
            user_msg = texts["quota_error"]
        else:
            user_msg = texts["ai_busy_error"]
        await message.answer(user_msg, reply_markup=kb)
        return

    chunk_size = 4000
    for start in range(0, len(reply_text), chunk_size):
        chunk = reply_text[start : start + chunk_size]
        await message.answer(chunk)


# 7. Ishga tushirish
async def main() -> None:
    init_db()
    await log_available_models()
    await bot.delete_webhook(drop_pending_updates=False)
    logger.info("Qalam bot polling rejimida ishga tushmoqda...")
    try:
        await dp.start_polling(
            bot,
            allowed_updates=dp.resolve_used_update_types(),
            handle_signals=True,
        )
    finally:
        await bot.session.close()


if __name__ == "__main__":
    asyncio.run(main())

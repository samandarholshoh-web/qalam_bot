import asyncio
import hashlib
import html
import logging
import os
import sqlite3
import time
from contextlib import contextmanager
from typing import Any, Dict, Iterator, List, Optional, Set, Tuple

from aiogram import Bot, Dispatcher, F, types
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
GEMINI_API_KEYS: List[str] = [k.strip() for k in _raw_keys.split(",") if k.strip()]
if not GEMINI_API_KEYS:
    raise ValueError("GEMINI_API_KEYS yoki GEMINI_API_KEY muhit o'zgaruvchisi topilmadi!")

ADMIN_ID: int = 0
CARD_NUMBER: str = ""
CARD_HOLDER: str = "QALAM XIZMATI"
PRICE_TEXT: str = "5,000 UZS"

PAY_BTN_TEXT: str = "💳 5,000 UZS to'lash"
KEY_BTN_TEXT: str = "🔑 Aktivatsiya kodini olish"

DB_PATH = "bot_data.db"

PRIMARY_MODEL: str = "gemini-3.5-flash-lite"
MODELS: List[str] = [
    PRIMARY_MODEL,
    "gemini-3.8-flash",
    "gemini-3.0-flash",
]

PRIMARY_TIMEOUT_SECONDS: float = 10.0
FALLBACK_TIMEOUT_SECONDS: float = 15.0
MODEL_TIMEOUTS: Dict[str, float] = {
    "gemini-3.5-flash-lite": PRIMARY_TIMEOUT_SECONDS,
    "gemini-3.8-flash": FALLBACK_TIMEOUT_SECONDS,
    "gemini-3.0-flash": FALLBACK_TIMEOUT_SECONDS,
}

FALLBACK_PAUSE_SECONDS: float = 1.0
USER_COOLDOWN_SECONDS: float = 2.5
MAX_CONCURRENT_AI_REQUESTS: int = 40

bot = Bot(token=BOT_TOKEN.strip())
dp = Dispatcher()

_clients: List[genai.Client] = [genai.Client(api_key=key) for key in GEMINI_API_KEYS]
_key_rr_index: int = 0
_key_rr_lock = asyncio.Lock()
_ai_concurrency_semaphore = asyncio.Semaphore(MAX_CONCURRENT_AI_REQUESTS)
_user_last_request_ts: Dict[int, float] = {}
_user_active_locks: Dict[int, asyncio.Lock] = {}
_disabled_models: Set[str] = set()


def _normalize_btn_text(text: Optional[str]) -> str:
    if not text:
        return ""
    return (
        text.strip()
        .replace("’", "'")
        .replace("‘", "'")
        .replace("ʻ", "'")
        .replace("ʼ", "'")
    )


def _is_pay_button(message: Message) -> bool:
    return _normalize_btn_text(message.text) == _normalize_btn_text(PAY_BTN_TEXT)


def _is_key_button(message: Message) -> bool:
    return _normalize_btn_text(message.text) == _normalize_btn_text(KEY_BTN_TEXT)


def _get_user_lock(user_id: int) -> asyncio.Lock:
    lock = _user_active_locks.get(user_id)
    if lock is None:
        lock = asyncio.Lock()
        _user_active_locks[user_id] = lock
    return lock


async def _get_ordered_clients() -> List[Tuple[int, genai.Client]]:
    global _key_rr_index
    total = len(_clients)
    async with _key_rr_lock:
        start = _key_rr_index % total
        _key_rr_index = (_key_rr_index + 1) % total
    return [((start + offset) % total, _clients[(start + offset) % total]) for offset in range(total)]


# 2. Ma'lumotlar bazasi qatlami (SQLite WAL + PostgreSQL'ga oson ko'chiriladigan struktura)
@contextmanager
def db_connection() -> Iterator[sqlite3.Connection]:
    conn = sqlite3.connect(DB_PATH, timeout=30.0)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("PRAGMA journal_mode=WAL;")
        conn.execute("PRAGMA synchronous=NORMAL;")
        conn.execute("PRAGMA busy_timeout=30000;")
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def db_init_schema() -> None:
    with db_connection() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS users (
                user_id INTEGER PRIMARY KEY,
                username TEXT,
                full_name TEXT,
                status TEXT DEFAULT 'unpaid',
                activation_key TEXT
            )
            """
        )
    logger.info("SQLite baza WAL rejimida tayyorlandi: %s", DB_PATH)


def db_upsert_and_get_user(user_id: int, username: Optional[str], full_name: str) -> Dict[str, Any]:
    with db_connection() as conn:
        row = conn.execute(
            "SELECT user_id, username, full_name, status, activation_key FROM users WHERE user_id = ?",
            (user_id,),
        ).fetchone()
        if row is None:
            conn.execute(
                """
                INSERT INTO users (user_id, username, full_name, status, activation_key)
                VALUES (?, ?, ?, 'unpaid', NULL)
                """,
                (user_id, username, full_name),
            )
        else:
            conn.execute(
                """
                UPDATE users
                SET username = ?, full_name = ?
                WHERE user_id = ?
                """,
                (username, full_name, user_id),
            )
        updated = conn.execute(
            "SELECT user_id, username, full_name, status, activation_key FROM users WHERE user_id = ?",
            (user_id,),
        ).fetchone()
        if updated:
            return dict(updated)
        return {
            "user_id": user_id,
            "username": username,
            "full_name": full_name,
            "status": "unpaid",
            "activation_key": None,
        }


def db_get_user(user_id: int) -> Optional[Dict[str, Any]]:
    with db_connection() as conn:
        row = conn.execute(
            "SELECT user_id, username, full_name, status, activation_key FROM users WHERE user_id = ?",
            (user_id,),
        ).fetchone()
        return dict(row) if row else None


def db_set_user_status(
    user_id: int,
    status: str,
    activation_key: Optional[str] = None,
) -> None:
    with db_connection() as conn:
        conn.execute(
            """
            UPDATE users
            SET status = ?, activation_key = ?
            WHERE user_id = ?
            """,
            (status, activation_key, user_id),
        )


def init_db() -> None:
    db_init_schema()


def get_or_create_user(user: types.User) -> Dict[str, Any]:
    return db_upsert_and_get_user(
        user_id=user.id,
        username=user.username,
        full_name=user.full_name or "",
    )


def set_user_status(
    user_id: int,
    status: str,
    activation_key: Optional[str] = None,
) -> None:
    db_set_user_status(user_id=user_id, status=status, activation_key=activation_key)


def generate_key_for_user(user_id: int) -> str:
    token_prefix = (BOT_TOKEN or "")[:10]
    raw_string = f"QALAM-{user_id}-{token_prefix}"
    digest = hashlib.sha256(raw_string.encode("utf-8")).hexdigest().upper()
    part1 = digest[:5]
    part2 = digest[5:10]
    return f"QALAM-{part1}-{part2}"


# 3. Gemini AI, Multi-model Fallback va loglash
async def log_available_models() -> None:
    try:
        primary_client = _clients[0]
        models_iter = await asyncio.wait_for(
            asyncio.to_thread(primary_client.models.list),
            timeout=10.0,
        )
        model_names = [getattr(m, "name", str(m)) for m in models_iter]
        logger.info(
            "Mavjud Gemini modellari (%d ta kalit ulangan): %s",
            len(_clients),
            model_names,
        )
    except Exception as e:
        logger.error("Modellarni olishda xato (kalitlar soni=%d): %s", len(_clients), repr(e))


def _extract_error_code(err: Exception) -> str:
    text = str(err)
    for code in ("404", "503", "429", "500", "504", "403", "400"):
        if code in text:
            return code
    return type(err).__name__


def _get_timeout_for_model(model_name: str) -> float:
    return MODEL_TIMEOUTS.get(model_name, FALLBACK_TIMEOUT_SECONDS)


async def _request_single_model(
    ai_client: genai.Client,
    model_name: str,
    prompt: str,
    timeout_seconds: float,
) -> str:
    response = await asyncio.wait_for(
        ai_client.aio.models.generate_content(
            model=model_name,
            contents=prompt,
        ),
        timeout=timeout_seconds,
    )
    text = getattr(response, "text", None)
    if not text or not text.strip():
        raise ValueError("Model bo'sh javob qaytardi")
    return text.strip()


async def generate_ai_reply(prompt: str) -> str:
    ordered_clients = await _get_ordered_clients()
    failure_summaries: List[str] = []

    active_models = [m for m in MODELS if m not in _disabled_models]
    if not active_models:
        _disabled_models.clear()
        active_models = list(MODELS)

    for model_name in active_models:
        timeout_sec = _get_timeout_for_model(model_name)
        skip_remaining_keys_for_model = False

        for key_idx, ai_client in ordered_clients:
            try:
                reply = await _request_single_model(
                    ai_client=ai_client,
                    model_name=model_name,
                    prompt=prompt,
                    timeout_seconds=timeout_sec,
                )
                logger.info(
                    "AI javob muvaffaqiyatli olindi: model=%s, key_index=%d",
                    model_name,
                    key_idx,
                )
                return reply
            except asyncio.TimeoutError:
                code = "TIMEOUT"
                failure_summaries.append(f"{model_name}[key#{key_idx}]={code}")
                logger.warning(
                    "Gemini timeout: model=%s, key_index=%d, timeout=%ss",
                    model_name,
                    key_idx,
                    timeout_sec,
                )
            except Exception as e:
                code = _extract_error_code(e)
                failure_summaries.append(f"{model_name}[key#{key_idx}]={code}")
                logger.error(
                    "Gemini xatosi: model=%s, key_index=%d, kod=%s, xato=%s",
                    model_name,
                    key_idx,
                    code,
                    repr(e),
                )
                if code == "404" or "NOT_FOUND" in str(e):
                    _disabled_models.add(model_name)
                    skip_remaining_keys_for_model = True
                    logger.warning(
                        "Model %s 404 NOT_FOUND qaytardi — keyingi modelga o'tilmoqda.",
                        model_name,
                    )
                    break

            await asyncio.sleep(FALLBACK_PAUSE_SECONDS)

        if skip_remaining_keys_for_model:
            continue

    summary_text = ", ".join(failure_summaries)
    raise RuntimeError(f"Barcha model va kalitlar urinildi: {summary_text}")


# 4. Tugmalar va to'lov/aktivatsiya oqimi
main_keyboard = ReplyKeyboardMarkup(
    keyboard=[
        [
            KeyboardButton(text=PAY_BTN_TEXT),
            KeyboardButton(text=KEY_BTN_TEXT),
        ]
    ],
    resize_keyboard=True,
)


def payment_inline_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="✅ To'lov qildim",
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


@dp.message(CommandStart())
async def handle_start(message: Message) -> None:
    if not message.from_user:
        return
    try:
        await asyncio.to_thread(get_or_create_user, message.from_user)
        greeting = (
            "Assalomu alaykum! <b>Qalam</b> sun'iy intellekt botiga xush kelibsiz.\n\n"
            "Savollaringizni yuboring — men sizga yordam beraman. "
            "Aktivatsiya kodini olish uchun quyidagi tugmalardan foydalaning."
        )
        await message.answer(
            greeting,
            parse_mode="HTML",
            reply_markup=main_keyboard,
        )
    except Exception as e:
        logger.error("handle_start xatosi (user_id=%s): %s", message.from_user.id, repr(e), exc_info=True)
        await message.answer(
            "Assalomu alaykum! Qalam botiga xush kelibsiz. Quyidagi tugmalardan foydalaning:",
            reply_markup=main_keyboard,
        )


@dp.message(_is_pay_button)
async def handle_payment_menu(message: Message) -> None:
    if not message.from_user:
        return
    user_id = message.from_user.id
    logger.info("To'lov tugmasi bosildi: user_id=%s", user_id)

    try:
        user_data = await asyncio.to_thread(get_or_create_user, message.from_user)
        status = user_data.get("status")
        activation_key = user_data.get("activation_key")

        if status == "approved":
            if not activation_key:
                activation_key = generate_key_for_user(user_id)
                await asyncio.to_thread(
                    set_user_status,
                    user_id,
                    "approved",
                    activation_key,
                )
            safe_key = html.escape(str(activation_key))
            await message.answer(
                f"✅ <b>Sizning to'lovingiz allaqachon tasdiqlangan!</b>\n"
                f"🔑 Aktivatsiya kodingiz: <code>{safe_key}</code>",
                parse_mode="HTML",
                reply_markup=main_keyboard,
            )
            return

        if status == "pending":
            await message.answer(
                "⏳ <b>Sizning to'lovingiz hozirda tekshiruvda!</b>\n"
                "Administrator tasdiqlashini kuting. Tasdiqlangach, aktivatsiya kodingiz avtomatik yuboriladi.",
                parse_mode="HTML",
                reply_markup=main_keyboard,
            )
            return

        safe_card = html.escape(CARD_NUMBER if CARD_NUMBER else "Kiritilmagan")
        safe_holder = html.escape(CARD_HOLDER)
        safe_price = html.escape(PRICE_TEXT)

        payment_text = (
            "💳 <b>To'lov ma'lumotlari:</b>\n\n"
            f"💰 Summa: <b>{safe_price}</b>\n"
            f"🔢 Karta raqami: <code>{safe_card}</code>\n"
            f"👤 Karta egasi: <b>{safe_holder}</b>\n\n"
            "To'lovni amalga oshirgach, pastdagi <b>«✅ To'lov qildim»</b> tugmasini bosing."
        )
        await message.answer(
            payment_text,
            parse_mode="HTML",
            reply_markup=payment_inline_keyboard(),
        )
    except Exception as e:
        logger.error("handle_payment_menu xatosi (user_id=%s): %s", user_id, repr(e), exc_info=True)
        await message.answer(
            "⚠️ To'lov ma'lumotlarini yuklashda vaqtinchalik xatolik yuz berdi. "
            "Iltimos, tugmani qayta bosing.",
            reply_markup=main_keyboard,
        )


@dp.message(_is_key_button)
async def handle_get_activation_key(message: Message) -> None:
    if not message.from_user:
        return
    user_id = message.from_user.id
    logger.info("Aktivatsiya kodi tugmasi bosildi: user_id=%s", user_id)

    try:
        user_data = await asyncio.to_thread(get_or_create_user, message.from_user)
        status = user_data.get("status")
        activation_key = user_data.get("activation_key")

        if status == "approved":
            if not activation_key:
                activation_key = generate_key_for_user(user_id)
                await asyncio.to_thread(
                    set_user_status,
                    user_id,
                    "approved",
                    activation_key,
                )
            await message.answer(
                f"🔑 <b>Sizning aktivatsiya kodingiz:</b>\n"
                f"<code>{html.escape(str(activation_key))}</code>",
                parse_mode="HTML",
                reply_markup=main_keyboard,
            )
        elif status == "pending":
            await message.answer(
                "🔒 <b>Aktivatsiya kodi hali yopiq.</b>\n"
                "To'lovingiz tekshiruvda — administrator tasdiqlashini kuting.",
                parse_mode="HTML",
                reply_markup=main_keyboard,
            )
        else:
            await message.answer(
                "🔒 <b>Aktivatsiya kodi yopiq (to'lov qilinmagan).</b>\n"
                "Avval <b>«💳 5,000 UZS to'lash»</b> tugmasi orqali to'lovni amalga oshiring.",
                parse_mode="HTML",
                reply_markup=main_keyboard,
            )
    except Exception as e:
        logger.error("handle_get_activation_key xatosi (user_id=%s): %s", user_id, repr(e), exc_info=True)
        await message.answer(
            "⚠️ Aktivatsiya kodini tekshirishda vaqtinchalik xatolik yuz berdi. "
            "Iltimos, tugmani qayta bosing.",
            reply_markup=main_keyboard,
        )


@dp.callback_query(F.data == "paid_confirm")
async def handle_paid_confirm(callback: CallbackQuery) -> None:
    if not callback.from_user:
        return
    try:
        user_data = await asyncio.to_thread(get_or_create_user, callback.from_user)
        user_id = user_data["user_id"]
        full_name = user_data["full_name"] or "Noma'lum"
        username_display = f"@{user_data['username']}" if user_data["username"] else "yo'q"

        await asyncio.to_thread(set_user_status, user_id, "pending", None)

        if callback.message:
            try:
                await callback.message.edit_text(
                    "⏳ <b>To'lov tekshiruvga yuborildi.</b>\n"
                    "Administrator to'lovni tekshirib tasdiqlashini kuting.",
                    parse_mode="HTML",
                )
            except Exception as e:
                logger.error("Xabarni yangilashda xato: %s", repr(e))

        admin_text = (
            "📥 <b>Yangi to'lov so'rovi!</b>\n\n"
            f"👤 Ism: <b>{html.escape(full_name)}</b>\n"
            f"🔗 Username: {html.escape(username_display)}\n"
            f"🆔 ID: <code>{user_id}</code>\n"
            f"💰 Summa: <b>{html.escape(PRICE_TEXT)}</b>"
        )
        try:
            await bot.send_message(
                chat_id=ADMIN_ID,
                text=admin_text,
                parse_mode="HTML",
                reply_markup=admin_approval_keyboard(user_id),
            )
        except Exception as e:
            logger.error("Adminga xabar yuborishda xato: %s", repr(e))

        await callback.answer("To'lov tekshiruvga yuborildi.", show_alert=False)
    except Exception as e:
        logger.error("handle_paid_confirm xatosi: %s", repr(e), exc_info=True)
        await callback.answer("Xatolik yuz berdi, qayta urinib ko'ring.", show_alert=True)


@dp.callback_query(F.data.startswith("approve_pay:"))
async def handle_approve_payment(callback: CallbackQuery) -> None:
    if not callback.from_user or callback.from_user.id != ADMIN_ID:
        await callback.answer("Bu amal faqat administrator uchun!", show_alert=True)
        return

    if not callback.data:
        return

    try:
        target_user_id = int(callback.data.split(":", 1)[1])
    except ValueError:
        await callback.answer("Noto'g'ri foydalanuvchi ID.", show_alert=True)
        return

    target_user = await asyncio.to_thread(db_get_user, target_user_id)
    if not target_user:
        await callback.answer("Foydalanuvchi bazadan topilmadi.", show_alert=True)
        return

    activation_key = generate_key_for_user(target_user_id)
    await asyncio.to_thread(set_user_status, target_user_id, "approved", activation_key)

    if callback.message:
        try:
            await callback.message.edit_text(
                f"✅ <b>To'lov tasdiqlandi!</b>\n"
                f"🆔 Foydalanuvchi ID: <code>{target_user_id}

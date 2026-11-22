import asyncio
import hashlib
import html
import logging
import os
import sqlite3
import time
from contextlib import contextmanager
from typing import Any, Dict, Iterator, List, Optional, Tuple

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

DB_PATH = "bot_data.db"
MODELS: List[str] = [
    "gemini-3.8-flash",
    "gemini-3.5-flash-lite",
    "gemini-2.5-flash-lite",
]

REQUEST_TIMEOUT_SECONDS: float = 12.0
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
    for code in ("503", "429", "500", "504", "403", "400"):
        if code in text:
            return code
    return type(err).__name__


async def _request_single_model(
    ai_client: genai.Client,
    model_name: str,
    prompt: str,
) -> str:
    response = await asyncio.wait_for(
        ai_client.aio.models.generate_content(
            model=model_name,
            contents=prompt,
        ),
        timeout=REQUEST_TIMEOUT_SECONDS,
    )
    text = getattr(response, "text", None)
    if not text or not text.strip():
        raise ValueError("Model bo'sh javob qaytardi")
    return text.strip()


async def generate_ai_reply(prompt: str) -> str:
    ordered_clients = await _get_ordered_clients()
    failure_summaries: List[str] = []

    for model_name in MODELS:
        for key_idx, ai_client in ordered_clients:
            try:
                reply = await _request_single_model(ai_client, model_name, prompt)
                logger.info(
                    "AI javob muvaffaqiyatli olindi: model=%s, key_index=%d",
                    model_name,
                    key_idx,
                )
                return reply
            except asyncio.TimeoutError:
                code = "TIMEOUT"
                detail = f"{REQUEST_TIMEOUT_SECONDS}s limit tugadi"
                failure_summaries.append(f"{model_name}[key#{key_idx}]={code}")
                logger.warning(
                    "Gemini timeout: model=%s, key_index=%d, sabab=%s",
                    model_name,
                    key_idx,
                    detail,
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

            await asyncio.sleep(FALLBACK_PAUSE_SECONDS)

    summary_text = ", ".join(failure_summaries)
    raise RuntimeError(f"Barcha model va kalitlar urinildi: {summary_text}")


# 4. Tugmalar va to'lov/aktivatsiya oqimi
main_keyboard = ReplyKeyboardMarkup(
    keyboard=[
        [
            KeyboardButton(text="💳 5,000 UZS to'lash"),
            KeyboardButton(text="🔑 Aktivatsiya kodini olish"),
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
    get_or_create_user(message.from_user)
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


@dp.message(F.text == "💳 5,000 UZS to'lash")
async def handle_payment_menu(message: Message) -> None:
    if not message.from_user:
        return
    user_data = get_or_create_user(message.from_user)
    if user_data["status"] == "approved" and user_data["activation_key"]:
        safe_key = html.escape(str(user_data["activation_key"]))
        await message.answer(
            f"✅ Sizning to'lovingiz allaqachon tasdiqlangan!\n"
            f"🔑 Aktivatsiya kodingiz: <code>{safe_key}</code>",
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


@dp.message(F.text == "🔑 Aktivatsiya kodini olish")
async def handle_get_activation_key(message: Message) -> None:
    if not message.from_user:
        return
    user_data = get_or_create_user(message.from_user)
    status = user_data["status"]
    activation_key = user_data["activation_key"]

    if status == "approved" and activation_key:
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
            "🔒 <b>Aktivatsiya kodi yopiq.</b>\n"
            "Avval <b>«💳 5,000 UZS to'lash»</b> tugmasi orqali to'lovni amalga oshiring.",
            parse_mode="HTML",
            reply_markup=main_keyboard,
        )


@dp.callback_query(F.data == "paid_confirm")
async def handle_paid_confirm(callback: CallbackQuery) -> None:
    if not callback.from_user:
        return
    user_data = get_or_create_user(callback.from_user)
    user_id = user_data["user_id"]
    full_name = user_data["full_name"] or "Noma'lum"
    username_display = f"@{user_data['username']}" if user_data["username"] else "yo'q"

    set_user_status(user_id=user_id, status="pending", activation_key=None)

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

    target_user = db_get_user(target_user_id)
    if not target_user:
        await callback.answer("Foydalanuvchi bazadan topilmadi.", show_alert=True)
        return

    activation_key = generate_key_for_user(target_user_id)
    set_user_status(
        user_id=target_user_id,
        status="approved",
        activation_key=activation_key,
    )

    if callback.message:
        try:
            await callback.message.edit_text(
                f"✅ <b>To'lov tasdiqlandi!</b>\n"
                f"🆔 Foydalanuvchi ID: <code>{target_user_id}</code>\n"
                f"🔑 Kalit: <code>{html.escape(activation_key)}</code>",
                parse_mode="HTML",
            )
        except Exception as e:
            logger.error("Admin xabarini yangilashda xato: %s", repr(e))

    try:
        await bot.send_message(
            chat_id=target_user_id,
            text=(
                "🎉 <b>Tabriklaymiz! To'lovingiz tasdiqlandi.</b>\n\n"
                f"🔑 Sizning aktivatsiya kodingiz:\n"
                f"<code>{html.escape(activation_key)}</code>"
            ),
            parse_mode="HTML",
        )
    except Exception as e:
        logger.error("Foydalanuvchiga kalit yuborishda xato: %s", repr(e))

    await callback.answer("To'lov tasdiqlandi va kalit yuborildi.")


@dp.callback_query(F.data.startswith("reject_pay:"))
async def handle_reject_payment(callback: CallbackQuery) -> None:
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

    target_user = db_get_user(target_user_id)
    if not target_user:
        await callback.answer("Foydalanuvchi bazadan topilmadi.", show_alert=True)
        return

    set_user_status(
        user_id=target_user_id,
        status="unpaid",
        activation_key=None,
    )

    if callback.message:
        try:
            await callback.message.edit_text(
                f"❌ <b>To'lov rad etildi.</b>\n"
                f"🆔 Foydalanuvchi ID: <code>{target_user_id}</code>",
                parse_mode="HTML",
            )
        except Exception as e:
            logger.error("Admin rad etish xabarini yangilashda xato: %s", repr(e))

    try:
        await bot.send_message(
            chat_id=target_user_id,
            text=(
                "❌ <b>To'lovingiz tasdiqlanmadi.</b>\n"
                "Iltimos, to'lov ma'lumotlarini tekshiring yoki qayta urinib ko'ring."
            ),
            parse_mode="HTML",
        )
    except Exception as e:
        logger.error("Foydalanuvchiga rad etish xabarini yuborishda xato: %s", repr(e))

    await callback.answer("To'lov rad etildi.")


@dp.message(F.text)
async def handle_ai_message(message: Message) -> None:
    if not message.from_user or not message.text:
        return
    prompt = message.text.strip()
    if not prompt:
        return

    user_id = message.from_user.id
    user_lock = _get_user_lock(user_id)

    if user_lock.locked():
        await message.answer(
            "⏳ Oldingi so'rovingiz hali bajarilmoqda. Javob kelguncha biroz kuting.",
            reply_markup=main_keyboard,
        )
        return

    now = time.monotonic()
    last_ts = _user_last_request_ts.get(user_id, 0.0)
    if now - last_ts < USER_COOLDOWN_SECONDS:
        remaining = round(USER_COOLDOWN_SECONDS - (now - last_ts), 1)
        await message.answer(
            f"⏱ Iltimos, ketma-ket xabar yuborishda {remaining} soniya kuting.",
            reply_markup=main_keyboard,
        )
        return

    async with user_lock:
        _user_last_request_ts[user_id] = time.monotonic()
        get_or_create_user(message.from_user)

        try:
            await bot.send_chat_action(chat_id=message.chat.id, action="typing")
        except Exception as e:
            logger.warning("Typing holatini yuborishda xato (user_id=%s): %s", user_id, repr(e))

        async with _ai_concurrency_semaphore:
            try:
                reply_text = await generate_ai_reply(prompt)
            except Exception as e:
                logger.error(
                    "Barcha AI urinishlari muvaffaqiyatsiz (user_id=%s): %s",
                    user_id,
                    repr(e),
                )
                await message.answer(
                    "⚠️ Hozir sun'iy intellekt serverlari vaqtincha yuqori yuklama bilan ishlamoqda "
                    "yoki limit tugagan. Iltimos, 10–15 soniyadan so'ng qayta urinib ko'ring.",
                    reply_markup=main_keyboard,
                )
                return

        chunk_size = 4000
        for start in range(0, len(reply_text), chunk_size):
            chunk = reply_text[start : start + chunk_size]
            await message.answer(chunk)


# 5. Ishga tushirish
async def main() -> None:
    init_db()
    await log_available_models()
    logger.info("Qalam bot polling rejimida ishga tushmoqda...")
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())

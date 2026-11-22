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
GEMINI_API_KEYS: List[str] = [k.strip().strip("\"'") for k in _raw_keys.split(",") if k.strip().strip("\"'")]
if not GEMINI_API_KEYS:
    raise ValueError("GEMINI_API_KEYS yoki GEMINI_API_KEY muhit o'zgaruvchisi topilmadi!")

_raw_admin_id = os.getenv("ADMIN_ID", "").strip().strip("\"'")
try:
    ADMIN_ID: int = int(_raw_admin_id) if _raw_admin_id else 0
except ValueError:
    logger.warning("ADMIN_ID noto'g'ri formatda (%s), faqat raqam kiriting. 0 deb olindi.", _raw_admin_id)
    ADMIN_ID = 0

if ADMIN_ID > 0:
    logger.info("ADMIN_ID muvaffaqiyatli yuklandi: %s", ADMIN_ID)
else:
    logger.warning("ADMIN_ID Railway Variables bo'limida o'rnatilmagan (hozir 0).")

CARD_NUMBER: str = os.getenv("CARD_NUMBER", "").strip()
CARD_HOLDER: str = "QALAM XIZMATI"
PRICE_TEXT: str = "5,000 UZS"

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


# 2. SQLite ma'lumotlar bazasi (bot_data.db)
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
                activation_key TEXT
            )
            """
        )
        conn.commit()


def get_or_create_user(user: types.User) -> Dict[str, Any]:
    full_name = user.full_name or ""
    username = user.username
    with get_db_connection() as conn:
        conn.execute(
            """
            INSERT INTO users (user_id, username, full_name, status, activation_key)
            VALUES (?, ?, ?, 'unpaid', NULL)
            ON CONFLICT(user_id) DO UPDATE SET
                username = excluded.username,
                full_name = excluded.full_name
            """,
            (user.id, username, full_name),
        )
        conn.commit()

        updated = conn.execute(
            "SELECT user_id, username, full_name, status, activation_key FROM users WHERE user_id = ?",
            (user.id,),
        ).fetchone()
        return (
            dict(updated)
            if updated
            else {
                "user_id": user.id,
                "username": username,
                "full_name": full_name,
                "status": "unpaid",
                "activation_key": None,
            }
        )


def get_user_by_id(user_id: int) -> Optional[Dict[str, Any]]:
    with get_db_connection() as conn:
        row = conn.execute(
            "SELECT user_id, username, full_name, status, activation_key FROM users WHERE user_id = ?",
            (user_id,),
        ).fetchone()
        return dict(row) if row else None


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


# 3. Xavfsiz Telegram yordamchi funksiyalari
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


# 4. Gemini AI, Multi-model + Multi-key Fallback va loglash
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


async def generate_ai_reply(prompt: str) -> str:
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
                        ),
                        timeout=REQUEST_TIMEOUT_SECONDS,
                    )
                    text = getattr(response, "text", None)
                    if text and text.strip():
                        logger.info(
                            "AI javobi muvaffaqiyatli olindi: model=%s, key=%d, urinish=%d/%d",
                            model_name,
                            key_idx,
                            attempt + 1,
                            MAX_RETRIES_PER_MODEL,
                        )
                        return text.strip()
                    raise RuntimeError("Model bo'sh javob qaytardi.")
                except asyncio.TimeoutError:
                    attempt_logs.append(f"{model_name}[k{key_idx}:{attempt + 1}]=TIMEOUT")
                    logger.warning(
                        "Gemini timeout (%ss): model=%s, key=%d, urinish=%d/%d",
                        REQUEST_TIMEOUT_SECONDS,
                        model_name,
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


# 5. Tugmalar va to'lov/aktivatsiya oqimi
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


@dp.message(F.text == "💳 5,000 UZS to'lash")
async def handle_payment_menu(message: Message) -> None:
    if not message.from_user:
        return
    user_data = await asyncio.to_thread(get_or_create_user, message.from_user)
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


@dp.callback_query(F.data == "paid_confirm")
async def handle_paid_confirm(callback: CallbackQuery) -> None:
    if not callback.from_user:
        return
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
            logger.warning("Xabarni yangilashda xato: %s", repr(e))

    admin_text = (
        "📥 <b>Yangi to'lov so'rovi!</b>\n\n"
        f"👤 Ism: <b>{html.escape(full_name)}</b>\n"
        f"🔗 Username: {html.escape(username_display)}\n"
        f"🆔 ID: <code>{user_id}</code>\n"
        f"💰 Summa: <b>{html.escape(PRICE_TEXT)}</b>"
    )
    await notify_admin(
        text=admin_text,
        reply_markup=admin_approval_keyboard(user_id),
    )

    await safe_answer_callback(callback, "To'lov tekshiruvga yuborildi.", show_alert=False)


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

    try:
        await bot.send_message(
            chat_id=target_user_id,
            text=(
                "🎉 <b>Tabriklaymiz! To'lovingiz tasdiqlandi.</b>\n\n"
                f"🔑 Sizning aktivatsiya kodingiz:\n"
                f"<code>{html.escape(activation_key)}</code>"
            ),
            parse_mode="HTML",
            reply_markup=main_keyboard,
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

    try:
        await bot.send_message(
            chat_id=target_user_id,
            text=(
                "❌ <b>To'lovingiz tasdiqlanmadi.</b>\n"
                "Iltimos, to'lov ma'lumotlarini tekshiring yoki qayta urinib ko'ring."
            ),
            parse_mode="HTML",
            reply_markup=main_keyboard,
        )
    except Exception as e:
        logger.warning("Foydalanuvchiga rad etish xabarini yuborishda xato (user_id=%s): %s", target_user_id, repr(e))

    await safe_answer_callback(callback, "To'lov rad etildi.")


@dp.message(F.text == "🔑 Aktivatsiya kodini olish")
async def handle_get_activation_key(message: Message) -> None:
    if not message.from_user:
        return
    user_data = await asyncio.to_thread(get_or_create_user, message.from_user)
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


@dp.message(F.text)
async def handle_ai_message(message: Message) -> None:
    if not message.from_user or not message.text:
        return
    prompt = message.text.strip()
    if not prompt:
        return

    user_id = message.from_user.id
    await asyncio.to_thread(get_or_create_user, message.from_user)

    try:
        await bot.send_chat_action(chat_id=message.chat.id, action="typing")
    except Exception as e:
        logger.warning("Typing holatini yuborishda xato (user_id=%s): %s", user_id, repr(e))

    try:
        reply_text = await generate_ai_reply(prompt)
    except Exception as e:
        err_str = str(e)
        logger.error("AI javob qaytarishda xatolik (user_id=%s): %s", user_id, repr(e))
        await notify_admin(
            f"⚠️ <b>AI xizmati xatosi</b>\n"
            f"🆔 User ID: <code>{user_id}</code>\n"
            f"📄 Tafsilot: <code>{html.escape(err_str[:300])}</code>"
        )
        if "429_QUOTA" in err_str:
            user_msg = (
                "⏳ Bugungi bepul AI so'rovlar limiti tugadi. "
                "Iltimos, birozdan so'ng qayta urinib ko'ring."
            )
        else:
            user_msg = (
                "😔 Uzr, hozir sun'iy intellekt serverlarida vaqtinchalik yuqori yuklama kuzatilmoqda. "
                "Iltimos, bir necha soniyadan so'ng qayta urinib ko'ring."
            )
        await message.answer(user_msg, reply_markup=main_keyboard)
        return

    chunk_size = 4000
    for start in range(0, len(reply_text), chunk_size):
        chunk = reply_text[start : start + chunk_size]
        await message.answer(chunk)


# 6. Ishga tushirish
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

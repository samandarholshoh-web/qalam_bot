import asyncio
import hashlib
import html
import logging
import os
import sqlite3
from typing import Any, Dict, Optional

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

# 1. Muhit o'zgaruvchilari va sozlamalar
BOT_TOKEN = os.getenv("BOT_TOKEN")
if not BOT_TOKEN:
    raise ValueError("BOT_TOKEN muhit o'zgaruvchisi topilmadi!")

GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
if not GEMINI_API_KEY:
    raise ValueError("GEMINI_API_KEY muhit o'zgaruvchisi topilmadi!")

ADMIN_ID: int = 0
CARD_NUMBER: str = ""
CARD_HOLDER: str = "QALAM XIZMATI"
PRICE_TEXT: str = "5,000 UZS"

DB_PATH = "bot_data.db"
MODELS = ["gemini-3.8-flash"]

bot = Bot(token=BOT_TOKEN.strip())
dp = Dispatcher()
client = genai.Client(api_key=GEMINI_API_KEY.strip())


# 2. SQLite ma'lumotlar bazasi (bot_data.db)
def get_db_connection() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
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
        row = conn.execute(
            "SELECT user_id, username, full_name, status, activation_key FROM users WHERE user_id = ?",
            (user.id,),
        ).fetchone()
        if row is None:
            conn.execute(
                """
                INSERT INTO users (user_id, username, full_name, status, activation_key)
                VALUES (?, ?, ?, 'unpaid', NULL)
                """,
                (user.id, username, full_name),
            )
            conn.commit()
        else:
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
            "SELECT user_id, username, full_name, status, activation_key FROM users WHERE user_id = ?",
            (user.id,),
        ).fetchone()
        return dict(updated) if updated else {
            "user_id": user.id,
            "username": username,
            "full_name": full_name,
            "status": "unpaid",
            "activation_key": None,
        }


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
    raw_string = f"QALAM-{user_id}-{BOT_TOKEN[:10]}"
    digest = hashlib.sha256(raw_string.encode("utf-8")).hexdigest().upper()
    part1 = digest[:5]
    part2 = digest[5:10]
    return f"QALAM-{part1}-{part2}"


# 3. Gemini AI va loglash
async def log_available_models() -> None:
    try:
        models = await asyncio.to_thread(client.models.list)
        model_names = [getattr(m, "name", str(m)) for m in models]
        logging.info("Mavjud Gemini modellari: %s", model_names)
    except Exception as e:
        logging.error("Modellarni olishda xatolik yuz berdi: %s", repr(e))


async def generate_ai_reply(prompt: str) -> str:
    last_error: Optional[Exception] = None
    for model_name in MODELS:
        for attempt in range(4):
            try:
                response = await asyncio.to_thread(
                    client.models.generate_content,
                    model=model_name,
                    contents=prompt,
                )
                text = getattr(response, "text", None)
                if text and text.strip():
                    return text.strip()
                raise RuntimeError("Model bo'sh javob qaytardi.")
            except Exception as e:
                last_error = e
                logging.error(
                    "Gemini xatosi (model=%s, urinish=%d/4): %s",
                    model_name,
                    attempt + 1,
                    repr(e),
                )
                if attempt < 3:
                    await asyncio.sleep(3 * (attempt + 1))
    raise RuntimeError(f"AI javob bera olmadi: {last_error}")


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
            logging.error("Xabarni yangilashda xato: %s", repr(e))

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
        logging.error("Adminga xabar yuborishda xato: %s", repr(e))

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

    target_user = get_user_by_id(target_user_id)
    if not target_user:
        await
    callback.answer("Foydalanuvchi bazada topilmadi.", show_alert=True)
                    return

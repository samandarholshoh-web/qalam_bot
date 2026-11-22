import asyncio
import hashlib
import logging
import os
import sqlite3

from aiogram import Bot, Dispatcher, F, types
from aiogram.filters import CommandStart
from aiogram.types import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    ReplyKeyboardMarkup,
)
from google import genai

logging.basicConfig(level=logging.INFO)

BOT_TOKEN = os.getenv("BOT_TOKEN")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")

ADMIN_ID = 987654321
CARD_NUMBER = "9860 0801 1915 0045"
CARD_HOLDER = "QALAM XIZMATI"
PRICE_TEXT = "5,000 UZS"

if not BOT_TOKEN or not GEMINI_API_KEY:
    raise ValueError("BOT_TOKEN yoki GEMINI_API_KEY topilmadi!")

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()
client = genai.Client(api_key=GEMINI_API_KEY.strip())

MODELS = [
    "gemini-3.8-flash",
]

DB_PATH = "bot_data.db"


def init_db():
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute(
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
    conn.close()


def get_or_create_user(user: types.User) -> dict:
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute(
        "SELECT user_id, username, full_name, status, activation_key FROM users WHERE user_id = ?",
        (user.id,),
    )
    row = cur.fetchone()
    if not row:
        cur.execute(
            "INSERT INTO users (user_id, username, full_name, status, activation_key) VALUES (?, ?, ?, 'unpaid', NULL)",
            (user.id, user.username or "", user.full_name or ""),
        )
        row = (user.id, user.username or "", user.full_name or "", "unpaid", None)
    else:
        cur.execute(
            "UPDATE users SET username = ?, full_name = ? WHERE user_id = ?",
            (user.username or "", user.full_name or "", user.id),
        )
    conn.commit()
    conn.close()
    return {
        "user_id": row[0],
        "username": row[1],
        "full_name": row[2],
        "status": row[3],
        "activation_key": row[4],
    }


def set_user_status(user_id: int, status: str, activation_key: str | None = None):
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute(
        "UPDATE users SET status = ?, activation_key = ? WHERE user_id = ?",
        (status, activation_key, user_id),
    )
    conn.commit()
    conn.close()


def generate_key_for_user(user_id: int) -> str:
    raw = f"QALAM-{user_id}-{BOT_TOKEN[:10]}"
    digest = hashlib.sha256(raw.encode()).hexdigest()[:10].upper()
    return f"QALAM-{digest[:5]}-{digest[5:]}"main_keyboard = ReplyKeyboardMarkup(
    keyboard=[
        [KeyboardButton(text="💳 5,000 UZS to'lash")],
        [KeyboardButton(text="🔑 Aktivatsiya kodini olish")],
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


async def log_available_models():
    try:
        models = await asyncio.to_thread(client.models.list)
        names = [getattr(m, "name", str(m)) for m in models]
        logging.info(f"AVAILABLE MODELS: {names}")
    except Exception as e:
        logging.error(f"MODEL LIST ERROR: {repr(e)}")


async def generate_ai_reply(prompt: str) -> str:
    last_error = None
    for model_name in MODELS:
        for attempt in range(4):
            try:
                res = await asyncio.to_thread(
                    client.models.generate_content,
                    model=model_name,
                    contents=prompt,
                )
                if res and res.text and res.text.strip():
                    return res.text.strip()
            except Exception as e:
                logging.error(
                    f"GEMINI ERROR ({model_name}, attempt {attempt + 1}): {repr(e)}"
                )
                last_error = e
                await asyncio.sleep(3 * (attempt + 1))
    raise last_error or RuntimeError("Model javob qaytarmadi.")@dp.message(F.text == "🔑 Aktivatsiya kodini olish")
async def handle_activation(message: types.Message):
    u = get_or_create_user(message.from_user)
    if u["status"] == "approved" and u["activation_key"]:
        await message.answer(
            f"🔑 <b>Aktivatsiya kodingiz:</b>\n<code>{u['activation_key']}</code>",
            parse_mode="HTML",
        )
    elif u["status"] == "pending":
        await message.answer(
            "🔒 <b>Kalit hali yopiq.</b> Admin to'lovni tasdiqlashini kuting.",
            parse_mode="HTML",
        )
    else:
        await message.answer(
            f"🔒 <b>Kalit yopiq.</b> Avval «💳 5,000 UZS to'lash» orqali <code>{CARD_NUMBER}</code> ga <b>{PRICE_TEXT}</b> to'lang.",
            parse_mode="HTML",
        )


@dp.message(F.text)
async def handle_text(message: types.Message):
    text = message.text.strip()
    if not text:
        return
    try:
        reply = await generate_ai_reply(text)
        for i in range(0, len(reply), 4000):
            await message.answer(reply[i : i + 4000])
    except Exception as e:
        logging.error(f"FINAL GEMINI ERROR: {repr(e)}")
        await message.answer("Xatolik yuz berdi, keyinroq qayta urinib ko'ring.")


async def main():
    init_db()
    await log_available_models()
    await dp.start_polling(bot)


if name == "main":
    asyncio.run(main())

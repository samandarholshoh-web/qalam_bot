import asyncio
import html
import logging
import os
import sqlite3
import time
from typing import Any, Dict, List, Tuple

from aiogram import Bot, Dispatcher, F, types
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    Message,
    ReplyKeyboardMarkup,
    WebAppInfo,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger("qalam_bot")

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
if not BOT_TOKEN:
    raise ValueError("BOT_TOKEN topilmadi!")

try:
    ADMIN_ID = int((os.getenv("ADMIN_ID") or "9271026986").strip().strip("\"'"))
except ValueError:
    ADMIN_ID = 9271026986

ADMIN_SECRET_CODE = os.getenv("ADMIN_SECRET_CODE", "").strip()
CLICK_CARD = os.getenv("CLICK_CARD", "9860 0801 1915 0045").strip()
CARD_HOLDER = os.getenv("CARD_HOLDER", "QALAM XIZMATI").strip()
DB_PATH = os.getenv(
    "DB_PATH", "/data/bot_data.db" if os.path.isdir("/data") else "bot_data.db"
).strip()

MINI_APP_URL = (
    os.getenv("MINI_APP_URL")
    or os.getenv("WEBAPP_URL")
    or ""
).strip()
INSTAGRAM_URL = os.getenv("INSTAGRAM_URL", "").strip()
TELEGRAM_URL = os.getenv("TELEGRAM_URL", "").strip()

bot = Bot(token=BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
dp = Dispatcher(storage=MemoryStorage())


class AuthStates(StatesGroup):
    waiting_admin_code = State()


class PaymentFlow(StatesGroup):
    waiting_amount = State()
    waiting_receipt = State()


# ==========================================================
# DO'KON MAHSULOTLARI (O'ZINGIZ SHU YERGA QO'SHASIZ)
# Namuna ko'rinishi:
# "📘 Kitob nomi — 25 000 so'm": 25000,
# ==========================================================
SHOP_PRODUCTS: Dict[str, int] = {
    # Mahsulotlaringizni shu yerga qo'shing:
}


# ==========================================================
# MA'LUMOTLAR BAZASI (SQLite)
# ==========================================================
def _get_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, timeout=15.0)
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA synchronous=NORMAL;")
    return conn


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
                created_at INTEGER
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS receipts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER,
                reason TEXT,
                amount INTEGER,
                photo_file_id TEXT,
                status TEXT DEFAULT 'pending',
                created_at INTEGER
            )
            """
        )
        conn.commit()


def ensure_user(user: types.User) -> None:
    now_ts = int(time.time())
    name = (user.full_name or "").strip()
    uname = user.username or ""
    with _get_conn() as conn:
        conn.execute(
            """
            INSERT INTO users (user_id, username, full_name, created_at)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(user_id) DO UPDATE SET
                username = excluded.username,
                full_name = excluded.full_name
            """,
            (user.id, uname, name, now_ts),
        )
        conn.commit()


def save_receipt(user_id: int, reason: str, amount: int, photo_file_id: str) -> int:
    with _get_conn() as conn:
        cur = conn.execute(
            """
            INSERT INTO receipts (user_id, reason, amount, photo_file_id, status, created_at)
            VALUES (?, ?, ?, ?, 'pending', ?)
            """,
            (user_id, reason, amount, photo_file_id, int(time.time())),
        )
        conn.commit()
        return int(cur.lastrowid)


def update_receipt_status(receipt_id: int, status: str) -> None:
    with _get_conn() as conn:
        conn.execute("UPDATE receipts SET status = ? WHERE id = ?", (status, receipt_id))
        conn.commit()


def get_user_receipts(user_id: int) -> List[Dict[str, Any]]:
    with _get_conn() as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT * FROM receipts WHERE user_id = ? ORDER BY id DESC LIMIT 15",
            (user_id,),
        ).fetchall()
        return [dict(r) for r in rows]


def get_bot_stats() -> Tuple[int, int, int]:
    with _get_conn() as conn:
        total_users = conn.execute("SELECT COUNT(*) FROM users").fetchone()[0]
        approved_receipts = conn.execute(
            "SELECT COUNT(*) FROM receipts WHERE status = 'approved'"
        ).fetchone()[0]
        pending_receipts = conn.execute(
            "SELECT COUNT(*) FROM receipts WHERE status = 'pending'"
        ).fetchone()[0]
    return int(total_users), int(approved_receipts), int(pending_receipts)


def _is_valid_https_url(url: str) -> bool:
    return url.startswith("https://") and len(url) > len("https://")


# ==========================================================
# KLAVIATURALAR
# ==========================================================
def main_menu_keyboard() -> ReplyKeyboardMarkup:
    first_btn = (
        KeyboardButton(text="📱 Ilovani ochish", web_app=WebAppInfo(url=MINI_APP_URL))
        if _is_valid_https_url(MINI_APP_URL)
        else KeyboardButton(text="📱 Ilovani ochish")
    )
    return ReplyKeyboardMarkup(
        keyboard=[
            [first_btn],
            [KeyboardButton(text="🛒 Do'kon"), KeyboardButton(text="💳 To'lov")],
            [KeyboardButton(text="📸 Instagram"), KeyboardButton(text="✈️ Telegram")],
            [KeyboardButton(text="🔐 Admin")],
        ],
        resize_keyboard=True,
    )


def shop_keyboard() -> ReplyKeyboardMarkup:
    rows = [[KeyboardButton(text=product)] for product in SHOP_PRODUCTS]
    rows.append([KeyboardButton(text="🏠 Bosh menyu")])
    return ReplyKeyboardMarkup(keyboard=rows, resize_keyboard=True)


def payment_menu_keyboard() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="📅 Oylik o'qish to'lovi")],
            [KeyboardButton(text="🧾 Mening cheklarim"), KeyboardButton(text="🏠 Bosh menyu")],
        ],
        resize_keyboard=True,
    )


def cancel_keyboard() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text="❌ Bekor qilish")]],
        resize_keyboard=True,
    )


def social_link_inline(label: str, url: str) -> InlineKeyboardMarkup | None:
    if not _is_valid_https_url(url):
        return None
    return InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text=label, url=url)]]
    )


def admin_receipt_keyboard(uid: int, receipt_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="✅ Tasdiqlash",
                    callback_data=f"admin_approve:{uid}:{receipt_id}",
                ),
                InlineKeyboardButton(
                    text="❌ Rad etish",
                    callback_data=f"admin_reject:{uid}:{receipt_id}",
                ),
            ]
        ]
    )


MAIN_MENU_TEXT = (
    "🏠 <b>Bosh menyu</b>\n\n"
    "📱 Darslar, mashqlar, imtihon va reyting endi <b>ilovada</b> — pastdagi tugmani bosing.\n"
    "🛒 Do'kon va 💳 To'lov — shu botda."
)


def _parse_admin_callback(data: str) -> Tuple[int, int] | None:
    parts = data.split(":")
    if len(parts) < 3:
        return None
    try:
        uid = int(parts[1])
        receipt_id = int(parts[2])
        return uid, receipt_id
    except ValueError:
        return None


# ==========================================================
# HANDLERLAR
# ==========================================================
@dp.message(CommandStart())
async def cmd_start(message: Message, state: FSMContext) -> None:
    if not message.from_user:
        return
    await state.clear()
    ensure_user(message.from_user)
    await message.answer(MAIN_MENU_TEXT, reply_markup=main_menu_keyboard())


@dp.callback_query(F.data.startswith("admin_approve:"))
async def cb_admin_approve(callback: CallbackQuery) -> None:
    if not callback.from_user or not callback.data:
        return
    if callback.from_user.id != ADMIN_ID:
        await callback.answer("Ruxsat yo'q", show_alert=True)
        return
    parsed = _parse_admin_callback(callback.data)
    if not parsed:
        await callback.answer("Noto'g'ri ma'lumot", show_alert=True)
        return
    uid, receipt_id = parsed
    if receipt_id > 0:
        update_receipt_status(receipt_id, "approved")
    await callback.answer("Tasdiqlandi!")
    if callback.message and isinstance(callback.message, Message):
        cap = callback.message.html_text or ""
        try:
            await callback.message.edit_caption(caption=f"{cap}\n\n✅ <b>TASDIQLANDI</b>")
        except TelegramBadRequest:
            pass
    try:
        await bot.send_message(
            uid,
            "✅ To'lovingiz administrator tomonidan tasdiqlandi! Ilovadan foydalanishingiz mumkin.",
        )
    except Exception as exc:
        logger.warning("Foydalanuvchiga tasdiq yuborilmadi (%s): %s", uid, exc)


@dp.callback_query(F.data.startswith("admin_reject:"))
async def cb_admin_reject(callback: CallbackQuery) -> None:
    if not callback.from_user or not callback.data:
        return
    if callback.from_user.id != ADMIN_ID:
        await callback.answer("Ruxsat yo'q", show_alert=True)
        return
    parsed = _parse_admin_callback(callback.data)
    if not parsed:
        await callback.answer("Noto'g'ri ma'lumot", show_alert=True)
        return
    uid, receipt_id = parsed
    if receipt_id > 0:
        update_receipt_status(receipt_id, "rejected")
    await callback.answer("Rad etildi")
    if callback.message and isinstance(callback.message, Message):
        cap = callback.message.html_text or ""
        try:
            await callback.message.edit_caption(caption=f"{cap}\n\n❌ <b>RAD ETILDI</b>")
        except TelegramBadRequest:
            pass
    try:
        await bot.send_message(uid, "❌ Yuborilgan chek rad etildi. Qayta tekshirib yuboring.")
    except Exception as exc:
        logger.warning("Foydalanuvchiga rad javobi yuborilmadi (%s): %s", uid, exc)


@dp.message(PaymentFlow.waiting_amount)
async def process_payment_amount(message: Message, state: FSMContext) -> None:
    if not message.from_user:
        return
    ensure_user(message.from_user)
    text = (message.text or "").strip()

    if text in ("❌ Bekor qilish", "🏠 Bosh menyu"):
        await state.clear()
        await message.answer(MAIN_MENU_TEXT, reply_markup=main_menu_keyboard())
        return

    cleaned = text.replace(" ", "").replace(",", "")
    if not cleaned.isdigit() or int(cleaned) <= 0:
        await message.answer(
            "❗️ Iltimos, summani faqat musbat raqam bilan yozing (masalan: 500000).",
            reply_markup=cancel_keyboard(),
        )
        return

    amount = int(cleaned)
    await state.update_data(amount=amount, reason="📅 Oylik o'qish to'lovi")
    await state.set_state(PaymentFlow.waiting_receipt)
    await message.answer(
        f"💳 Karta raqami: <code>{html.escape(CLICK_CARD)}</code> (<b>{html.escape(CARD_HOLDER)}</b>)\n"
        "📌 Maqsad: <b>📅 Oylik o'qish to'lovi</b>\n"
        f"💰 Summa: <b>{amount:,} so'm</b>\n\n"
        "To'lovni amalga oshiring va <b>chek rasmini</b> shu yerga yuboring:",
        reply_markup=cancel_keyboard(),
    )


@dp.message(PaymentFlow.waiting_receipt, F.photo)
async def process_receipt_photo(message: Message, state: FSMContext) -> None:
    if not message.from_user or not message.photo:
        return
    ensure_user(message.from_user)
    data = await state.get_data()
    amount = int(data.get("amount") or 0)
    reason = str(data.get("reason") or "To'lov")
    if amount <= 0:
        await state.clear()
        await message.answer(
            "❗️ To'lov summasi topilmadi. Iltimos, to'lov jarayonini boshidan boshlang.",
            reply_markup=main_menu_keyboard(),
        )
        return

    photo_id = message.photo[-1].file_id
    receipt_id = save_receipt(message.from_user.id, reason, amount, photo_id)
    await state.clear()
    await message.answer(
        "✅ <b>Chekingiz qabul qilindi va adminga yuborildi!</b>\n"
        "Administrator tasdiqlashini kuting.",
        reply_markup=main_menu_keyboard(),
    )

    if ADMIN_ID > 0:
        uname = f"@{message.from_user.username}" if message.from_user.username else "yo'q"
        caption = (
            f"🧾 <b>Yangi chek (#{receipt_id})</b>\n\n"
            f"👤 Ism: <b>{html.escape(message.from_user.full_name or 'User')}</b>\n"
            f"🔗 Username: {html.escape(uname)}\n"
            f"🆔 ID: <code>{message.from_user.id}</code>\n"
            f"📌 Sabab: <b>{html.escape(reason)}</b>\n"
            f"💰 Summa: <b>{amount:,} so'm</b>"
        )
        try:
            await bot.send_photo(
                chat_id=ADMIN_ID,
                photo=photo_id,
                caption=caption,
                reply_markup=admin_receipt_keyboard(message.from_user.id, receipt_id),
            )
        except Exception as exc:
            logger.warning("Adminga chek yuborilmadi: %s", exc)


@dp.message(PaymentFlow.waiting_receipt)
async def process

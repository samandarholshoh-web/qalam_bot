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
    return "429" in err_str or "RESOURCE_EX

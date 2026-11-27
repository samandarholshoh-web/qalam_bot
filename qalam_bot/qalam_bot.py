import asyncio
from contextlib import closing
import html
import logging
import os
import sqlite3
import time
from typing import Any, Dict, List, Optional, Set, Tuple

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
import pyotp

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

TOTP_SECRET = os.getenv("TOTP_SECRET", "").strip()
CLICK_CARD = os.getenv("CLICK_CARD", "9860 0801 1915 0045").strip()
CARD_HOLDER = os.getenv("CARD_HOLDER", "QALAM XIZMATI").strip()
DB_PATH = os.getenv(
    "DB_PATH", "/data/bot_data.db" if os.path.isdir("/data") else "bot_data.db"
).strip()

MINI_APP_URL = (
    os.getenv("MINI_APP_URL") or os.getenv("WEBAPP_URL") or ""
).strip()
INSTAGRAM_URL = os.getenv("INSTAGRAM_URL", "").strip()
TELEGRAM_URL = os.getenv("TELEGRAM_URL", "").strip()

bot = Bot(token=BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
dp = Dispatcher(storage=MemoryStorage())

_USED_TOTP_CODES: Set[Tuple[str, int]] = set()


class AuthStates(StatesGroup):
  waiting_admin_code = State()


class PaymentFlow(StatesGroup):
  waiting_amount = State()
  waiting_receipt = State()


SHOP_PRODUCTS: Dict[str, int] = {
    "📘 HSK 1 kaligrafiya kitobi — 20 000 so'm": 20000,
    "📘 HSK 1 lug'ati — 15 000 so'm": 15000,
    "🖥️ HSK 1 dars taqdimotlari — 30 000 so'm": 30000,
    "📘 HSK 2 kaligrafiya kitobi — 20 000 so'm": 20000,
    "📘 HSK 2 lug'ati — 15 000 so'm": 15000,
    "🖥️ HSK 2 dars taqdimotlari — 30 000 so'm": 30000,
}


# ==========================================================
# GOOGLE AUTHENTICATOR (TOTP)
# ==========================================================
def _cleanup_used_totp(current_step: int) -> None:
  expired = {item for item in _USED_TOTP_CODES if current_step - item[1] > 3}
  _USED_TOTP_CODES.difference_update(expired)


def verify_totp_code(code:

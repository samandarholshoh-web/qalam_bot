import asyncio
import logging
import os

from aiogram import Bot, Dispatcher, F, types
from aiogram.filters import CommandStart
from aiogram.types import KeyboardButton, ReplyKeyboardMarkup
from google import genai

logging.basicConfig(level=logging.INFO)

BOT_TOKEN = os.getenv("BOT_TOKEN")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")

if not BOT_TOKEN:
    raise ValueError("BOT_TOKEN environment variable topilmadi!")
if not GEMINI_API_KEY:
    raise ValueError("GEMINI_API_KEY environment variable topilmadi!")

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()
client = genai.Client(api_key=GEMINI_API_KEY.strip())

MODELS = [
    "gemini-3.8-flash",
]

main_keyboard = ReplyKeyboardMarkup(
    keyboard=[
        [KeyboardButton(text="💳 5,000 UZS to'lash")],
        [KeyboardButton(text="🔑 Aktivatsiya kodini olish")],
    ],
    resize_keyboard=True,
)


async def generate_ai_reply(prompt: str) -> str:
    last_error = None

    for model_name in MODELS:
        for attempt in range(3):
            try:
                response = await asyncio.to_thread(
                    client.models.generate_content,
                    model=model_name,
                    contents=prompt,
                )
                if response and response.text and response.text.strip():
                    return response.text.strip()
            except Exception as e:
                logging.error(
                    f"GEMINI ERROR ({model_name}, attempt {attempt + 1}): {repr(e)}"
                )
                last_error = e
                await asyncio.sleep(1.5 * (attempt + 1))

    raise last_error or RuntimeError("Model vaqtincha javob qaytarmadi.")


@dp.message(CommandStart())
async def cmd_start(message: types.Message):
    await message.answer(
        "Assalomu alaykum! Qalam sun'iy intellekt botiga xush kelibsiz.\n"
        "Savolingizni yozing, sizga mamnuniyat bilan javob beraman.",
        reply_markup=main_keyboard,
    )


@dp.message(F.text == "💳 5,000 UZS to'lash")
async def handle_payment(message: types.Message):
    await message.answer(
        "5,000 UZS to'lov ma'lumotlari:\n"
        "To'lovni amalga oshirgach, «🔑 Aktivatsiya kodini olish» tugmasidan foydalaning."
    )


@dp.message(F.text == "🔑 Aktivatsiya kodini olish")
async def handle_activation(message: types.Message):
    await message.answer(
        "Aktivatsiya kodi bo'yicha ma'lumot olish uchun administratorga murojaat qiling."
    )


@dp.message(F.text)
async def handle_text(message: types.Message):
    user_text = message.text.strip()
    if not user_text:
        return

    try:
        reply_text = await generate_ai_reply(user_text)

        for i in range(0, len(reply_text), 4000):
            await message.answer(reply_text[i : i + 4000])

    except Exception as e:
        logging.error(f"FINAL GEMINI ERROR: {repr(e)}")
        await message.answer(
            "Xatolik yuz berdi, iltimos keyinroq qayta urinib ko'ring."
        )


async def main():
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())

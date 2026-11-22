import asyncio
import os
import random
import string

from aiogram import Bot, Dispatcher, types
from aiogram.filters import Command
from aiogram.utils.keyboard import InlineKeyboardBuilder
from google import genai

BOT_TOKEN = os.getenv("BOT_TOKEN")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")

client = genai.Client(api_key=GEMINI_API_KEY)
bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()


def generate_activation_token() -> str:
    letters = string.ascii_uppercase + string.digits
    return "".join(random.choice(letters) for _ in range(8))


@dp.message(Command("start"))
async def cmd_start(message: types.Message):
    builder = InlineKeyboardBuilder()
    builder.button(text="💳 5,000 UZS to'lash", callback_data="pay_sub")
    builder.button(text="🔑 Aktivatsiya kodini olish", callback_data="get_token")
    builder.adjust(1)

    await message.answer(
        "Assalomu alaykum! Qalam ilovasining sun'iy intellekt botiga xush kelibsiz.\n\n"
        "Menga istalgan o'quv yoki fanlar bo'yicha savol yozib yuborishingiz mumkin, sun'iy intellekt javob beradi.",
        reply_markup=builder.as_markup(),
        parse_mode="Markdown",
    )


@dp.callback_query(lambda c: c.data == "pay_sub")
async def process_pay(callback: types.CallbackQuery):
    await callback.message.answer(
        "💳 5,000 UZS to'lov ma'lumotlari keyinroq ulanadi.\n"
        "Aktivatsiya kodi olish uchun «🔑 Aktivatsiya kodini olish» tugmasini bosing."
    )
    await callback.answer()


@dp.callback_query(lambda c: c.data == "get_token")
async def process_token(callback: types.CallbackQuery):
    activation_code = generate_activation_token()
    await callback.message.answer(
        f"✅ To'lov tasdiqlandi!\n\nSizning unikal aktivatsiya kodingiz:\n{activation_code}\n\n"
        "Bu kodni Qalam ilovasidagi 'Aktivatsiya kodini kiritish' oynasiga kiriting.",
        parse_mode="Markdown",
    )
    await callback.answer()


@dp.message()
async def handle_ai_query(message: types.Message):
    if not message.text:
        await message.answer("Iltimos, matn ko'rinishida savol yuboring.")
        return

    wait_msg = await message.answer("⏳ Sun'iy intellekt o'ylamoqda...")

    try:
        response = client.models.generate_content(
            model="gemini-3.8-flash",
            contents=message.text,
        )
        ai_reply = response.text or "Javob qaytarilmadi, boshqa savol yozib ko'ring."

        await message.answer(ai_reply, parse_mode="Markdown")
        await bot.delete_message(chat_id=message.chat.id, message_id=wait_msg.message_id)
    except Exception as e:
        print("GEMINI ERROR:", repr(e), flush=True)
        try:
            await bot.delete_message(chat_id=message.chat.id, message_id=wait_msg.message_id)
        except Exception:
            pass
        await message.answer("Xatolik yuz berdi, iltimos keyinroq qayta urinib ko'ring.")


async def main():
    if not BOT_TOKEN:
        raise RuntimeError("BOT_TOKEN topilmadi! Railway Variables ichiga BOT_TOKEN kiriting.")
    if not GEMINI_API_KEY:
        raise RuntimeError("GEMINI_API_KEY topilmadi! Railway Variables ichiga GEMINI_API_KEY kiriting.")

    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())

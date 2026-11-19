import logging
from aiogram import Bot, Dispatcher, types
from aiogram.filters import Command
from aiogram.utils.keyboard import InlineKeyboardBuilder
import random
import string
import google.generativeai as genai

# 1. Bu yerga o'zingizning botingiz tokeni va Gemini kalitingizni yozasiz
BOT_TOKEN = "9147437409:AAEr0f6lAzM9wKytDjFaPvYZiJtEagY5prg"
GEMINI_API_KEY = "curl -fsSL https://dev.meta.ai/install.sh | bash"  # O'zingizning kalitingizni qo'shtirnoq ichiga yozasiz

# Gemini AI ni sozlash
genai.configure(api_key=GEMINI_API_KEY)
model = genai.GenerativeModel("gemini-1.5-flash")

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()

# Unikal aktivatsiya kodini generatsiya qilish funksiyasi
def generate_activation_token():
    letters = string.ascii_uppercase + string.digits
    return ''.join(random.choice(letters) for _ in range(8))

@dp.message(Command("start"))
async def cmd_start(message: types.Message):
    builder = InlineKeyboardBuilder()
    builder.button(text="💳 5,000 UZS to'lash", callback_data="pay_sub")
    builder.button(text="🔑 Aktivatsiya kodini olish", callback_data="get_token")
    builder.adjust(1)
    
    await message.answer(
        "Assalomu alaykum! Qalam ilovasining sun'iy intellekt botiga xush kelibsiz.\n\n"
        "Menga istalgan o'quv yoki fanlar bo'yicha savol yozib yuborishingiz mumkin, sun'iy intellekt javob beradi!",
        reply_markup=builder.as_markup(),
        parse_mode="Markdown"
    )

@dp.callback_query(lambda c: c.data == "get_token")
async def process_token(callback: types.CallbackQuery):
    activation_code = generate_activation_token()
    
    await callback.message.answer(
        f"✅ To'lov tasdiqlandi!\n\nSizning unikal aktivatsiya kodingiz:\n{activation_code}\n\n"
        "Bu kodni Qalam ilovasidagi 'Aktivatsiya kodini kiritish' oynasiga kiriting.",
        parse_mode="Markdown"
    )
    await callback.answer()

# Foydalanuvchi yozgan har qanday savolga AI orqali javob berish
@dp.message()
async def handle_ai_query(message: types.Message):
    wait_msg = await message.answer("⏳ Sun'iy intellekt o'ylamoqda...")
    
    try:
        response = model.generate_content(message.text)
        ai_reply = response.text
        
        await message.answer(ai_reply, parse_mode="Markdown")
        await bot.delete_message(chat_id=message.chat.id, message_id=wait_msg.message_id)
        
    except Exception as e:
        await message.answer("Xatolik yuz berdi, iltimos birozdan so'ng qayta urinib ko'ring.")

async def main():
    logging.basicConfig(level=logging.INFO)
    await dp.start_polling(bot)

if name == "main":
    import asyncio
    asyncio.run(main())

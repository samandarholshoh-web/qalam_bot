@dp.callback_query(F.data.startswith("approve_pay:"))
async def handle_approve_payment(callback: types.CallbackQuery):
    if callback.from_user.id != ADMIN_ID:
        await callback.answer("Faqat administrator tasdiqlay oladi.", show_alert=True)
        return

    target_user_id = int(callback.data.split(":")[1])
    key = generate_key_for_user(target_user_id)
    set_user_status(target_user_id, "approved", key)

    await callback.answer("To'lov tasdiqlandi va kalit ochildi!", show_alert=True)
    await callback.message.edit_text(
        callback.message.text + f"\n\n✅ <b>TASDIQLANDI</b>\n🔑 Kalit: <code>{key}</code>",
        parse_mode="HTML",
    )

    try:
        await bot.send_message(
            chat_id=target_user_id,
            text=(
                f"✅ <b>To'lovingiz tasdiqlandi!</b>\n\n"
                f"🔑 Sizning aktivatsiya kodingiz:\n<code>{key}</code>\n\n"
                f"Endi «🔑 Aktivatsiya kodini olish» tugmasi orqali ham kalitingizni ko'rishingiz mumkin."
            ),
            parse_mode="HTML",
        )
    except Exception as e:
        logging.error(f"USER NOTIFY ERROR: {repr(e)}")


@dp.callback_query(F.data.startswith("reject_pay:"))
async def handle_reject_payment(callback: types.CallbackQuery):
    if callback.from_user.id != ADMIN_ID:
        await callback.answer("Faqat administrator rad eta oladi.", show_alert=True)
        return

    target_user_id = int(callback.data.split(":")[1])
    set_user_status(target_user_id, "unpaid", None)

    await callback.answer("To'lov rad etildi.", show_alert=True)
    await callback.message.edit_text(
        callback.message.text + "\n\n❌ <b>RAD ETILDI</b>",
        parse_mode="HTML",
    )

    try:
        await bot.send_message(
            chat_id=target_user_id,
            text=(
                "❌ <b>To'lov hali tasdiqlanmadi</b>\n\n"
                "Iltimos, to'lovni to'liq bajarganingizni tekshiring va qayta «💳 5,000 UZS to'lash» tugmasidan foydalaning. "
                "To'lov tasdiqlanmaguncha aktivatsiya kodi ochilmaydi."
            ),
            parse_mode="HTML",
        )
    except Exception as e:
        logging.error(f"USER REJECT NOTIFY ERROR: {repr(e)}")


@dp.message(F.text == "🔑 Aktivatsiya kodini olish")
async def handle_activation(message: types.Message):
    user_data = get_or_create_user(message.from_user)

    if user_data["status"] == "approved" and user_data["activation_key"]:
        await message.answer(
            f"🔑 <b>Sizning aktivatsiya kodingiz:</b>\n\n"
            f"<code>{user_data['activation_key']}</code>",
            parse_mode="HTML",
        )
    elif user_data["status"] == "pending":
        await message.answer(
            "🔒 <b>Aktivatsiya kodi hali yopiq</b>\n\n"
            "To'lov so'rovingiz yuborilgan, lekin administrator hali tasdiqlamagani sababli kalit ochilmagan. "
            "Tasdiqlangach shu tugmani qayta bosing."
        )
    else:
        await message.answer(
            f"🔒 <b>Aktivatsiya kodi yopiq</b>\n\n"
            f"Kalitni olish uchun avval <b>«💳 5,000 UZS to'lash»</b> tugmasi orqali karta raqamiga (<code>{CARD_NUMBER}</code>) "
            f"<b>{PRICE_TEXT}</b> to'lov qiling va to'lov tasdiqlanishini kuting.",
            parse_mode="HTML",
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
    init_db()
    await log_available_models()
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())

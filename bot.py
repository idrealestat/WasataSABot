# بعد إرسال الرد
if len(reply) > 500:
    keyboard = [
        [InlineKeyboardButton("✅ نعم", callback_data="feedback_yes")],
        [InlineKeyboardButton("❌ لا", callback_data="feedback_no")]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    await context.bot.send_message(chat_id=user_id, text="هل أفادتك هذه الإجابة؟", reply_markup=reply_markup)
    context_service.update(user_id, "عقد وساطة" or "عقد إيجار", "تقييم الإجابة")

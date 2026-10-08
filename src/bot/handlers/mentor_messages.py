"""Conversational transport helpers; existing access and accounting remain authoritative."""
import asyncio


async def spoken_message(message, state, client):
    from .new_user import webapp_client, _resolve_last_used, _ensure_user, _can_use_professor_mode, LAST_USED_PROFESSOR
    from .ai_helpers import _download_telegram_file_bytes
    from src.ai.helpers import check_blocked, CHAT_NOT_BANNED_FILTER
    from src.bot.texts import user_texts
    from src.bot.keyboards import user_keyboards
    if not await check_blocked(message) or not await CHAT_NOT_BANNED_FILTER(message): return None
    existing = await webapp_client.get_user("tg_id", message.from_user.id)
    mode, _ = _resolve_last_used(existing)
    if mode != LAST_USED_PROFESSOR:
        await message.answer(user_texts.expert_text_only, reply_markup=user_keyboards.upgrade_to_professor)
        return None
    user = await _ensure_user(message, client)
    if not user.tg_phone:
        await message.answer("Для голосовых сообщений сначала подтвердите номер телефона в ИИ-профессоре. Этот шаг можно продолжить текстом.")
        return None
    if not _can_use_professor_mode(user):
        await message.answer(user_texts.premium_limit_0, reply_markup=user_keyboards.only_free)
        return None
    media = message.voice or message.video_note
    if (media.file_size or 0) > 20 * 1024 * 1024:
        await message.answer("Запись слишком большая. Пришлите короткое голосовое сообщение.")
        return None
    try:
        async with asyncio.timeout(60):
            content = await _download_telegram_file_bytes(message, media)
            transcript = await client.transcribe_audio_bytes(filename="mentor-answer.ogg" if message.voice else "mentor-answer.mp4", content=content)
        if not transcript or not transcript.strip(): raise ValueError()
    except Exception:
        await message.answer("Не получилось расслышать запись. Уже собранное осталось; можно повторить или ответить текстом.")
        return None
    copy = message.model_copy(update={"text":transcript.strip(), "voice":None, "video_note":None, "caption":None})
    copy.as_(message.bot)
    return copy

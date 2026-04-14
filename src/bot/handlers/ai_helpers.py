import io
import logging
import mimetypes
import os
import asyncio
import time
import uuid
from typing import Any, Sequence

from aiogram import F
from aiogram.types import Message

from config import AI_REQUEST_TIMEOUT_SECONDS
from src.ai.helpers import _notify_user
from src.ai.webapp_client import webapp_client

try: from aiogram_media_group import MediaGroupFilter, media_group_handler
except ImportError:
    _media_group_logger = logging.getLogger(__name__)
    _media_group_logger.warning("aiogram-media-group is not installed. Falling back to single-message behavior for media groups.")

    def MediaGroupFilter(*args, **kwargs): return F.media_group_id
    def media_group_handler(*dargs, **dkwargs):
        def decorator(func):
            async def wrapper(message: Message, *args, **kwargs): return await func([message], *args, **kwargs)
            return wrapper
        return decorator


_SUPPORTED_CONTEXT_FILE_EXTENSIONS = {
    ".art", ".bat", ".brf", ".c", ".cls", ".css", ".csv", ".diff", ".doc", ".docx", ".dot", ".eml", ".es",
    ".h", ".hs", ".htm", ".html", ".ics", ".ifb", ".java", ".js", ".json", ".keynote", ".ksh", ".ltx", ".mail",
    ".markdown", ".md", ".mht", ".mhtml", ".mjs", ".nws", ".odt", ".pages", ".patch", ".pdf", ".pl", ".pm",
    ".pot", ".ppa", ".pps", ".ppt", ".pptx", ".pwz", ".py", ".rst", ".rtf", ".scala", ".sh", ".shtml", ".srt",
    ".sty", ".svg", ".svgz", ".tex", ".text", ".txt", ".vcf", ".vtt", ".wiz", ".xla", ".xlb", ".xlc", ".xlm",
    ".xls", ".xlsx", ".xlt", ".xlw", ".xml", ".yaml", ".yml",
}
VIDEO_TRANSCRIPTION_NOTICE = "На данный момент модель не может распозновать видео, вместо этого мы транскрибируем аудио с него"
AI_TEMPORARY_ERROR_TEXT = "Временная ошибка сервиса. Попробуйте ещё раз через 1-2 минуты."
AI_TIMEOUT_ERROR_TEXT = "Сервис отвечает дольше обычного. Попробуйте отправить сообщение еще раз через 1-2 минуты."
ai_pipeline_logger = logging.getLogger("ai.pipeline")


def _elapsed_ms(started_at: float) -> int:
    return int((time.monotonic() - started_at) * 1000)


def has_supported_media(message: Message) -> bool: return bool(message.photo or message.video or message.video_note or message.document or message.voice)
async def _download_telegram_file_bytes(message: Message, file_obj: Any) -> bytes:
    payload = io.BytesIO()
    await message.bot.download(file_obj, destination=payload)
    return payload.getvalue()


def _safe_filename(raw_name: str | None, fallback: str, *, mime_type: str | None = None) -> str:
    name = os.path.basename((raw_name or "").strip()) if raw_name else ""
    if name: return name
    if "." in fallback: return fallback
    guessed_ext = mimetypes.guess_extension(mime_type or "") or ".bin"
    return f"{fallback}{guessed_ext}"


def _append_voice_transcripts_to_input(input_text: str, voice_transcripts: list[str]) -> str:
    if not voice_transcripts: return input_text
    title = "Транскрипция голосового сообщения" if len(voice_transcripts) == 1 else "Транскрипция голосовых сообщений"
    transcript_block = "\n".join(voice_transcripts)
    if input_text: return f"{input_text}\n\n{title}:\n{transcript_block}"
    return f"{title}:\n{transcript_block}"


def _append_video_transcripts_to_input(input_text: str, video_transcripts: list[str]) -> str:
    if not video_transcripts: return input_text
    title = "Транскрипция аудио из видео" if len(video_transcripts) == 1 else "Транскрипция аудио из видеофайлов"
    transcript_block = "\n".join(video_transcripts)
    if input_text: return f"{input_text}\n\n{title}:\n{transcript_block}"
    return f"{title}:\n{transcript_block}"


def _append_attachment_notes_to_input(input_text: str, attachment_notes: list[str]) -> str:
    if not attachment_notes: return input_text
    notes_block = "\n".join(attachment_notes)
    if input_text: return f"{input_text}\n\nПримечания по вложениям:\n{notes_block}"
    return f"Примечания по вложениям:\n{notes_block}"


def _is_supported_context_file(filename: str) -> bool:
    _, ext = os.path.splitext((filename or "").strip())
    return ext.lower() in _SUPPORTED_CONTEXT_FILE_EXTENSIONS


async def _collect_media_from_message(message: Message, professor_client=None, *, suffix: str = "", trace_id: str | None = None) -> tuple[list[tuple[str, bytes]], list[tuple[str, bytes]], list[str], list[str], list[str], bool]:
    file_contents: list[tuple[str, bytes]] = []
    image_contents: list[tuple[str, bytes]] = []
    voice_transcripts: list[str] = []
    video_transcripts: list[str] = []
    attachment_notes: list[str] = []
    has_video_media = False

    if message.photo:
        photo = message.photo[-1]
        filename = f"photo_{message.message_id}{suffix}.jpg"
        step_started = time.monotonic()
        ai_pipeline_logger.info("AI step | trace=%s | stage=media.photo.download.start | message_id=%s | filename=%s", trace_id, message.message_id, filename)
        photo_bytes = await _download_telegram_file_bytes(message, photo)
        ai_pipeline_logger.info("AI step | trace=%s | stage=media.photo.download.done | message_id=%s | filename=%s | bytes=%d | elapsed_ms=%d", trace_id, message.message_id, filename, len(photo_bytes), _elapsed_ms(step_started))
        image_contents.append((filename, photo_bytes))

    if message.video:
        has_video_media = True
        filename = _safe_filename(message.video.file_name, f"video_{message.message_id}{suffix}.mp4", mime_type=message.video.mime_type)
        step_started = time.monotonic()
        ai_pipeline_logger.info("AI step | trace=%s | stage=media.video.download.start | message_id=%s | filename=%s", trace_id, message.message_id, filename)
        video_bytes = await _download_telegram_file_bytes(message, message.video)
        ai_pipeline_logger.info("AI step | trace=%s | stage=media.video.download.done | message_id=%s | filename=%s | bytes=%d | elapsed_ms=%d", trace_id, message.message_id, filename, len(video_bytes), _elapsed_ms(step_started))
        if professor_client:
            step_started = time.monotonic()
            ai_pipeline_logger.info("AI step | trace=%s | stage=media.video.transcribe.start | message_id=%s | filename=%s", trace_id, message.message_id, filename)
            transcript = await professor_client.transcribe_audio_bytes(filename=filename, content=video_bytes)
            ai_pipeline_logger.info("AI step | trace=%s | stage=media.video.transcribe.done | message_id=%s | filename=%s | text_len=%d | elapsed_ms=%d", trace_id, message.message_id, filename, len(transcript or ""), _elapsed_ms(step_started))
            if transcript: video_transcripts.append(transcript)
            else: attachment_notes.append("Пользователь прислал видео, но транскрипция аудио не удалась. Попроси кратко пересказать содержание текстом.")
        else: attachment_notes.append("Пользователь прислал видео. Попроси кратко пересказать содержание текстом.")

    if message.video_note:
        has_video_media = True
        filename = f"video_note_{message.message_id}{suffix}.mp4"
        step_started = time.monotonic()
        ai_pipeline_logger.info("AI step | trace=%s | stage=media.video_note.download.start | message_id=%s | filename=%s", trace_id, message.message_id, filename)
        video_bytes = await _download_telegram_file_bytes(message, message.video_note)
        ai_pipeline_logger.info("AI step | trace=%s | stage=media.video_note.download.done | message_id=%s | filename=%s | bytes=%d | elapsed_ms=%d", trace_id, message.message_id, filename, len(video_bytes), _elapsed_ms(step_started))
        if professor_client:
            step_started = time.monotonic()
            ai_pipeline_logger.info("AI step | trace=%s | stage=media.video_note.transcribe.start | message_id=%s | filename=%s", trace_id, message.message_id, filename)
            transcript = await professor_client.transcribe_audio_bytes(filename=filename, content=video_bytes)
            ai_pipeline_logger.info("AI step | trace=%s | stage=media.video_note.transcribe.done | message_id=%s | filename=%s | text_len=%d | elapsed_ms=%d", trace_id, message.message_id, filename, len(transcript or ""), _elapsed_ms(step_started))
            if transcript: video_transcripts.append(transcript)
            else: attachment_notes.append("Пользователь прислал video_note, но транскрипция аудио не удалась. Попроси кратко пересказать содержание текстом.")
        else: attachment_notes.append("Пользователь прислал video_note. Попроси кратко пересказать содержание текстом.")

    if message.document:
        filename = _safe_filename(message.document.file_name, f"document_{message.message_id}{suffix}", mime_type=message.document.mime_type)
        if _is_supported_context_file(filename):
            step_started = time.monotonic()
            ai_pipeline_logger.info("AI step | trace=%s | stage=media.document.download.start | message_id=%s | filename=%s", trace_id, message.message_id, filename)
            document_bytes = await _download_telegram_file_bytes(message, message.document)
            ai_pipeline_logger.info("AI step | trace=%s | stage=media.document.download.done | message_id=%s | filename=%s | bytes=%d | elapsed_ms=%d", trace_id, message.message_id, filename, len(document_bytes), _elapsed_ms(step_started))
            file_contents.append((filename, document_bytes))
        else:
            _, ext = os.path.splitext(filename)
            attachment_notes.append(f"Пользователь прислал документ '{filename}', но формат '{ext or 'без расширения'}' не поддерживается для анализа файла. Попроси PDF/DOCX/TXT или краткий текстовый пересказ.")

    if message.voice:
        filename = _safe_filename(getattr(message.voice, "file_name", None), f"voice_{message.message_id}{suffix}.ogg", mime_type=message.voice.mime_type)
        step_started = time.monotonic()
        ai_pipeline_logger.info("AI step | trace=%s | stage=media.voice.download.start | message_id=%s | filename=%s", trace_id, message.message_id, filename)
        voice_bytes = await _download_telegram_file_bytes(message, message.voice)
        ai_pipeline_logger.info("AI step | trace=%s | stage=media.voice.download.done | message_id=%s | filename=%s | bytes=%d | elapsed_ms=%d", trace_id, message.message_id, filename, len(voice_bytes), _elapsed_ms(step_started))
        if professor_client:
            step_started = time.monotonic()
            ai_pipeline_logger.info("AI step | trace=%s | stage=media.voice.transcribe.start | message_id=%s | filename=%s", trace_id, message.message_id, filename)
            transcript = await professor_client.transcribe_audio_bytes(filename=filename, content=voice_bytes)
            ai_pipeline_logger.info("AI step | trace=%s | stage=media.voice.transcribe.done | message_id=%s | filename=%s | text_len=%d | elapsed_ms=%d", trace_id, message.message_id, filename, len(transcript or ""), _elapsed_ms(step_started))
            if transcript: voice_transcripts.append(transcript)
            else: attachment_notes.append("Пользователь прислал голосовое сообщение, но распознавание не удалось. Попроси повторить голосовое с лучшим качеством или написать текстом.")
        else: attachment_notes.append("Пользователь прислал голосовое сообщение. Распознай смысл по контексту или попроси написать текстом.")

    return file_contents, image_contents, voice_transcripts, video_transcripts, attachment_notes, has_video_media


async def collect_message_payload(message: Message, professor_client=None, *, trace_id: str | None = None) -> tuple[str, list[tuple[str, bytes]], list[tuple[str, bytes]], bool]:
    started_at = time.monotonic()
    ai_pipeline_logger.info("AI step | trace=%s | stage=payload.single.start | message_id=%s", trace_id, message.message_id)
    input_text = (message.text or message.caption or "").strip()
    file_contents, image_contents, voice_transcripts, video_transcripts, attachment_notes, has_video_media = await _collect_media_from_message(message, professor_client=professor_client, trace_id=trace_id)
    input_text = _append_voice_transcripts_to_input(input_text, voice_transcripts)
    input_text = _append_video_transcripts_to_input(input_text, video_transcripts)
    input_text = _append_attachment_notes_to_input(input_text, attachment_notes)
    if not input_text and not file_contents and not image_contents:
        input_text = "Пользователь прислал вложение без текста. Попроси уточнить задачу словами."
    ai_pipeline_logger.info("AI step | trace=%s | stage=payload.single.done | message_id=%s | text_len=%d | files=%d | images=%d | has_video=%s | elapsed_ms=%d", trace_id, message.message_id, len(input_text), len(file_contents), len(image_contents), has_video_media, _elapsed_ms(started_at))
    return input_text, file_contents, image_contents, has_video_media


async def collect_media_group_payload(messages: Sequence[Message], professor_client=None, *, trace_id: str | None = None)-> tuple[str, list[tuple[str, bytes]], list[tuple[str, bytes]], bool]:
    started_at = time.monotonic()
    ai_pipeline_logger.info("AI step | trace=%s | stage=payload.album.start | count=%d", trace_id, len(messages))
    input_text = ""
    file_contents: list[tuple[str, bytes]] = []
    image_contents: list[tuple[str, bytes]] = []
    voice_transcripts: list[str] = []
    video_transcripts: list[str] = []
    attachment_notes: list[str] = []
    has_video_media = False

    for idx, msg in enumerate(messages, start=1):
        msg_text = (msg.text or msg.caption or "").strip()
        if not input_text and msg_text: input_text = msg_text
        msg_files, msg_images, msg_transcripts, msg_video_transcripts, msg_notes, msg_has_video = await _collect_media_from_message(msg, professor_client=professor_client, suffix=f"_{idx}", trace_id=trace_id)
        file_contents.extend(msg_files)
        image_contents.extend(msg_images)
        voice_transcripts.extend(msg_transcripts)
        video_transcripts.extend(msg_video_transcripts)
        attachment_notes.extend(msg_notes)
        has_video_media = has_video_media or msg_has_video

    input_text = _append_voice_transcripts_to_input(input_text, voice_transcripts)
    input_text = _append_video_transcripts_to_input(input_text, video_transcripts)
    input_text = _append_attachment_notes_to_input(input_text, attachment_notes)
    if not input_text and not file_contents and not image_contents:
        input_text = "Пользователь прислал альбом вложений без текста. Попроси уточнить задачу словами."
    ai_pipeline_logger.info("AI step | trace=%s | stage=payload.album.done | count=%d | text_len=%d | files=%d | images=%d | has_video=%s | elapsed_ms=%d", trace_id, len(messages), len(input_text), len(file_contents), len(image_contents), has_video_media, _elapsed_ms(started_at))
    return input_text, file_contents, image_contents, has_video_media


async def ensure_responses_conversation_id(professor_client, user_id: int, conversation_id: str | None, *, trace_id: str | None = None) -> str:
    if conversation_id and conversation_id.startswith("conv_"):
        ai_pipeline_logger.info("AI step | trace=%s | stage=conversation.ensure.cached | user_id=%s | conversation_id=%s", trace_id, user_id, conversation_id)
        return conversation_id

    started_at = time.monotonic()
    ai_pipeline_logger.info("AI step | trace=%s | stage=conversation.ensure.create.start | user_id=%s | old_conversation_id=%s", trace_id, user_id, conversation_id)

    new_conversation_id = await professor_client.create_conversation(user_id=user_id)
    ai_pipeline_logger.info("AI step | trace=%s | stage=conversation.ensure.create.done | user_id=%s | new_conversation_id=%s | elapsed_ms=%d", trace_id, user_id, new_conversation_id, _elapsed_ms(started_at))

    started_at = time.monotonic()
    ai_pipeline_logger.info("AI step | trace=%s | stage=conversation.ensure.persist.start | user_id=%s | conversation_id=%s", trace_id, user_id, new_conversation_id)

    await webapp_client.update_user(user_id, {"conversation_id": new_conversation_id})
    ai_pipeline_logger.info("AI step | trace=%s | stage=conversation.ensure.persist.done | user_id=%s | conversation_id=%s | elapsed_ms=%d", trace_id, user_id, new_conversation_id, _elapsed_ms(started_at))
    return new_conversation_id


async def send_message_v2_from_telegram(message: Message, professor_client, user_id: int, conversation_id: str | None, *, input_text_override: str | None = None) -> dict[str, Any]:
    trace_id = uuid.uuid4().hex[:12]
    started_at = time.monotonic()
    ai_pipeline_logger.info("AI flow start | trace=%s | mode=single | user_id=%s | message_id=%s | conversation_id=%s", trace_id, user_id, message.message_id, conversation_id)

    active_conversation_id = await ensure_responses_conversation_id(professor_client, user_id, conversation_id, trace_id=trace_id)
    input_text, file_contents, image_contents, has_video_media = await collect_message_payload(message, professor_client=professor_client, trace_id=trace_id)
    if has_video_media:
        step_started = time.monotonic()
        ai_pipeline_logger.info("AI step | trace=%s | stage=notify.video_notice.start | user_id=%s | message_id=%s", trace_id, user_id, message.message_id)
        await _notify_user(message, VIDEO_TRANSCRIPTION_NOTICE, 8)
        ai_pipeline_logger.info("AI step | trace=%s | stage=notify.video_notice.done | user_id=%s | message_id=%s | elapsed_ms=%d", trace_id, user_id, message.message_id, _elapsed_ms(step_started))

    if input_text_override is not None: input_text = input_text_override + input_text
    ai_pipeline_logger.info("AI step | trace=%s | stage=openai.request.start | mode=single | user_id=%s | conversation_id=%s | input_len=%d | files=%d | images=%d", trace_id, user_id, active_conversation_id, len(input_text), len(file_contents), len(image_contents))
    response = await professor_client.send_message_v2(input_text=input_text, conversation_id=active_conversation_id, file_contents=file_contents, image_contents=image_contents, user_id=user_id, trace_id=trace_id)
    ai_pipeline_logger.info("AI step | trace=%s | stage=openai.request.done | mode=single | user_id=%s | conversation_id=%s | text_len=%d | files=%d | in_tokens=%s | cached_in_tokens=%s | out_tokens=%s | conversation_reset_reason=%s", trace_id, user_id, active_conversation_id, len(str(response.get("text") or "")), len(response.get("files") or []), response.get("input_tokens"), response.get("cached_input_tokens"), response.get("output_tokens"), response.get("conversation_reset_reason"))

    final_conversation_id = response.get("conversation_id")
    if final_conversation_id and final_conversation_id != active_conversation_id:
        step_started = time.monotonic()
        ai_pipeline_logger.info("AI step | trace=%s | stage=conversation.sync.start | user_id=%s | from=%s | to=%s", trace_id, user_id, active_conversation_id, final_conversation_id)
        await webapp_client.update_user(user_id, {"conversation_id": final_conversation_id})
        ai_pipeline_logger.info("AI step | trace=%s | stage=conversation.sync.done | user_id=%s | to=%s | elapsed_ms=%d", trace_id, user_id, final_conversation_id, _elapsed_ms(step_started))

    ai_pipeline_logger.info("AI flow done | trace=%s | mode=single | user_id=%s | total_elapsed_ms=%d", trace_id, user_id, _elapsed_ms(started_at))
    return response


async def send_message_v2_from_media_group(messages: Sequence[Message], professor_client, user_id: int, conversation_id: str | None, *, input_text_override: str | None = None) -> dict[str, Any]:
    if not messages: raise ValueError("messages must not be empty")

    trace_id = uuid.uuid4().hex[:12]
    started_at = time.monotonic()
    ai_pipeline_logger.info("AI flow start | trace=%s | mode=album | user_id=%s | messages=%d | conversation_id=%s", trace_id, user_id, len(messages), conversation_id)

    active_conversation_id = await ensure_responses_conversation_id(professor_client, user_id, conversation_id, trace_id=trace_id)
    input_text, file_contents, image_contents, has_video_media = await collect_media_group_payload(messages, professor_client=professor_client, trace_id=trace_id)
    if has_video_media:
        step_started = time.monotonic()
        ai_pipeline_logger.info("AI step | trace=%s | stage=notify.video_notice.start | user_id=%s | message_id=%s", trace_id, user_id, messages[0].message_id)
        await _notify_user(messages[0], VIDEO_TRANSCRIPTION_NOTICE, 8)
        ai_pipeline_logger.info("AI step | trace=%s | stage=notify.video_notice.done | user_id=%s | message_id=%s | elapsed_ms=%d", trace_id, user_id, messages[0].message_id, _elapsed_ms(step_started))

    if input_text_override is not None: input_text = input_text_override
    ai_pipeline_logger.info("AI step | trace=%s | stage=openai.request.start | mode=album | user_id=%s | conversation_id=%s | input_len=%d | files=%d | images=%d", trace_id, user_id, active_conversation_id, len(input_text), len(file_contents), len(image_contents))
    response = await professor_client.send_message_v2(input_text=input_text, conversation_id=active_conversation_id, file_contents=file_contents, image_contents=image_contents, user_id=user_id, trace_id=trace_id)
    ai_pipeline_logger.info("AI step | trace=%s | stage=openai.request.done | mode=album | user_id=%s | conversation_id=%s | text_len=%d | files=%d | in_tokens=%s | cached_in_tokens=%s | out_tokens=%s | conversation_reset_reason=%s", trace_id, user_id, active_conversation_id, len(str(response.get("text") or "")), len(response.get("files") or []), response.get("input_tokens"), response.get("cached_input_tokens"), response.get("output_tokens"), response.get("conversation_reset_reason"))

    final_conversation_id = response.get("conversation_id")
    if final_conversation_id and final_conversation_id != active_conversation_id:
        step_started = time.monotonic()
        ai_pipeline_logger.info("AI step | trace=%s | stage=conversation.sync.start | user_id=%s | from=%s | to=%s", trace_id, user_id, active_conversation_id, final_conversation_id)
        await webapp_client.update_user(user_id, {"conversation_id": final_conversation_id})
        ai_pipeline_logger.info("AI step | trace=%s | stage=conversation.sync.done | user_id=%s | to=%s | elapsed_ms=%d", trace_id, user_id, final_conversation_id, _elapsed_ms(step_started))

    ai_pipeline_logger.info("AI flow done | trace=%s | mode=album | user_id=%s | total_elapsed_ms=%d", trace_id, user_id, _elapsed_ms(started_at))
    return response


async def safe_ai_response(message: Message, request_coro, *, error_text: str = AI_TEMPORARY_ERROR_TEXT, timeout_text: str = AI_TIMEOUT_ERROR_TEXT, timeout_seconds: float = float(AI_REQUEST_TIMEOUT_SECONDS)) -> dict[str, Any] | None:
    started_at = time.monotonic()
    user_id = getattr(getattr(message, "from_user", None), "id", None)
    message_id = getattr(message, "message_id", None)
    ai_pipeline_logger.info("AI wrapper start | user_id=%s | message_id=%s", user_id, message_id)

    try:
        result = await asyncio.wait_for(request_coro, timeout=timeout_seconds)
        ai_pipeline_logger.info("AI wrapper done | user_id=%s | message_id=%s | elapsed_ms=%d | ok=%s", user_id, message_id, _elapsed_ms(started_at), result is not None)
        return result

    except asyncio.CancelledError: raise
    except asyncio.TimeoutError:
        ai_pipeline_logger.error("AI wrapper timeout | user_id=%s | message_id=%s | elapsed_ms=%d | timeout_s=%s", user_id, message_id, _elapsed_ms(started_at), timeout_seconds)
        try: await message.answer(timeout_text)
        except Exception: pass
        return None

    except Exception as e:
        ai_pipeline_logger.exception(f"AI wrapper failed {e} | user_id=%s | message_id=%s | elapsed_ms=%d", user_id, message_id, _elapsed_ms(started_at))
        logging.getLogger(__name__).exception(f"AI request failed for user_id=%s: {e}", getattr(getattr(message, "from_user", None), "id", None))
        try: await message.answer(error_text)
        except Exception: pass
        return None


async def safe_webapp_call(coro, *, operation: str, default=None):
    try: return await coro
    except asyncio.CancelledError: raise
    except Exception:
        logging.getLogger(__name__).exception("Webapp call failed: %s", operation)
        return default


def schedule_webapp_call(coro, *, operation: str) -> asyncio.Task:
    task = asyncio.create_task(coro)

    def _on_done(done_task: asyncio.Task):
        try: done_task.result()
        except asyncio.CancelledError: return
        except Exception: logging.getLogger(__name__).exception("Background webapp call crashed: %s", operation)

    task.add_done_callback(_on_done)
    return task


__all__ = [
    "MediaGroupFilter",
    "media_group_handler",
    "has_supported_media",
    "send_message_v2_from_telegram",
    "send_message_v2_from_media_group",
    "safe_ai_response",
    "safe_webapp_call",
    "schedule_webapp_call",
]

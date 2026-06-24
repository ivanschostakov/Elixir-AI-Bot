import asyncio
import copy
import logging
import mimetypes
import re

from datetime import datetime, timedelta
from typing import Any
from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.exceptions import TelegramBadRequest
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import Message, BufferedInputFile, InputMediaPhoto, InputMediaDocument, ReplyKeyboardRemove, InlineKeyboardButton, InlineQueryResultArticle, InputTextMessageContent

from config import (
    EXPERT_BOT_TOKEN,
    DOSE_BOT_TOKEN,
    DOSE_ASSISTANT_ID,
    DOSE_OPENAI_API,
    PROFESSOR_BOT_TOKEN,
    PROFESSOR_ASSISTANT_ID,
    LOGS_DIR,
    BOT_NAMES,
    PROFESSOR_OPENAI_API,
    EXPERT_OPENAI_API,
    EXPERT_ASSISTANT_ID,
)
from src.bot.handlers import *
from src.ai.helpers import split_text, MAX_TG_MSG_LEN
from src.bot.keyboards import user_keyboards
from src.bot.middleware import ContextMiddleware
from src.bot.texts import user_texts
from src.ai.client import ProfessorClient
from src.ai.webapp_client import webapp_client

RESPONSES_CITATION_BLOCK_RE = re.compile(r"\ue200.*?\ue201", re.DOTALL)
RESPONSES_CITATION_TOKEN_RE = re.compile(r"(?:\bfilecite\b|\bturn\d+file\d+\b)")
PRIVATE_USE_CHAR_RE = re.compile(r"[\ue000-\uf8ff]")
SQUARE_CITATION_RE = re.compile(r"【[^】]*】")
MARKDOWN_CODE_BLOCK_RE = re.compile(r"```(?:[\w.+-]+)?\n?(.*?)```", re.DOTALL)
MARKDOWN_INLINE_CODE_RE = re.compile(r"`([^`\n]+)`")
SANDBOX_LINK_RE = re.compile(r"sandbox:/[^\s)\]]+")
IMAGE_FILE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp", ".tif", ".tiff"}
polling_logger = logging.getLogger("aiogram.polling_lifecycle")


def _strip_copyable_markdown(text: str) -> str:
    cleaned = MARKDOWN_CODE_BLOCK_RE.sub(lambda m: (m.group(1) or "").strip("\n"), text or "")
    cleaned = MARKDOWN_INLINE_CODE_RE.sub(r"\1", cleaned)
    cleaned = cleaned.replace("```", "").replace("`", "")
    return cleaned


def _sanitize_response_text(text: str) -> str:
    cleaned = RESPONSES_CITATION_BLOCK_RE.sub("", text or "")
    cleaned = SQUARE_CITATION_RE.sub("", cleaned)
    cleaned = PRIVATE_USE_CHAR_RE.sub("", cleaned)
    cleaned = RESPONSES_CITATION_TOKEN_RE.sub("", cleaned)
    cleaned = SANDBOX_LINK_RE.sub("", cleaned)
    cleaned = re.sub(r"[ \t]+\n", "\n", cleaned)
    cleaned = re.sub(r"\n[ \t]+", "\n", cleaned)
    cleaned = re.sub(r"[ \t]{2,}", " ", cleaned)
    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned)
    cleaned = _strip_copyable_markdown(cleaned)
    return cleaned.strip()


def _is_image_attachment(filename: str, content: bytes) -> bool:
    mime, _ = mimetypes.guess_type(filename)
    if mime and mime.startswith("image/"): return True
    name = (filename or "").lower()
    ext = f".{name.rsplit('.', 1)[-1]}" if "." in name else ""
    if ext in IMAGE_FILE_EXTENSIONS: return True
    head = (content or b"")[:16]
    if head.startswith(b"\x89PNG\r\n\x1a\n"): return True
    if head.startswith(b"\xff\xd8\xff"): return True
    if head.startswith((b"GIF87a", b"GIF89a")): return True
    if head.startswith(b"BM"): return True
    if head.startswith((b"II*\x00", b"MM\x00*")): return True
    if len(head) >= 12 and head[:4] == b"RIFF" and head[8:12] == b"WEBP": return True
    return False


def _normalize_response_files(raw_files: list[Any], *, logger: logging.Logger | None = None) -> list[tuple[str, bytes]]:
    attachments: list[tuple[str, bytes]] = []
    for idx, item in enumerate(raw_files):
        if isinstance(item, dict):
            content = item.get("content")
            if not isinstance(content, (bytes, bytearray)):
                if logger: logger.warning("Skipping file item without bytes content at index=%d", idx)
                continue
            name = str(item.get("filename") or item.get("name") or f"file_{idx}.bin")
            safe_name = name.split("/")[-1].split("\\")[-1] or f"file_{idx}.bin"
            attachments.append((safe_name, bytes(content)))
            continue
        if logger: logger.warning("Skipping unsupported file item type at index=%d type=%s", idx, type(item).__name__)
    return attachments


def _chunk_attachments(items: list[tuple[str, bytes]], chunk_size: int = 10) -> list[list[tuple[str, bytes]]]:
    if chunk_size <= 0: return [items] if items else []
    return [items[i:i + chunk_size] for i in range(0, len(items), chunk_size)]


class ProfessorBot(Bot):
    def __init__(self, api_key: str, bot_name: str):
        super().__init__(api_key, default=DefaultBotProperties(parse_mode="html"))

        self.__logger = logging.getLogger(f"{self.__class__.__name__}::{bot_name}")
        self.__logger.setLevel(logging.INFO)
        self.__bg_tasks: set[asyncio.Task] = set()

        log_file = LOGS_DIR / f"{bot_name}.txt"
        log_file.parent.mkdir(parents=True, exist_ok=True)

        if not any(isinstance(h, logging.FileHandler) and getattr(h, "baseFilename", None) == str(log_file) for h in self.__logger.handlers):
            fh = logging.FileHandler(log_file, encoding="utf-8")
            fh.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s", "%Y-%m-%d %H:%M:%S"))
            self.__logger.addHandler(fh)

    @property
    def log(self): return self.__logger

    async def create_user(self, user_id: int, phone: str, name: str = None, surname: str = None) -> str:
        conversation_id = await expert_client.create_conversation(user_id=user_id)
        await webapp_client.upsert_user({"tg_id": user_id, "tg_phone": phone, "name": name, "surname": surname, "conversation_id": conversation_id})
        self.__logger.info("Created new user: %s, phone=%s", user_id, phone)
        return conversation_id

    async def _reply_text_safe(self, message: Message, text: str, *, reply_markup=None) -> Message:
        try: return await message.reply(text, parse_mode=None, reply_markup=reply_markup)
        except TelegramBadRequest as e:
            self.__logger.warning("Plain text reply failed, retrying plain. err=%s", e)
            return await message.reply(text, parse_mode=None, reply_markup=reply_markup)

    async def _reply_photo_safe(self, message: Message, photo: BufferedInputFile, *, caption: str | None, reply_markup=None):
        try: return await message.reply_photo(photo, caption=caption, parse_mode=None, reply_markup=reply_markup)
        except TelegramBadRequest as e:
            self.__logger.warning("Plain photo caption failed, retrying plain. err=%s", e)
            return await message.reply_photo(photo, caption=caption, parse_mode=None, reply_markup=reply_markup)

    async def _reply_document_safe(self, message: Message, document: BufferedInputFile, *, caption: str | None, reply_markup=None):
        try: return await message.reply_document(document, caption=caption, parse_mode=None, reply_markup=reply_markup)
        except TelegramBadRequest as e:
            self.__logger.warning("Plain document caption failed, retrying plain. err=%s", e)
            return await message.reply_document(document, caption=caption, parse_mode=None, reply_markup=reply_markup)

    async def _reply_media_group_safe(self, message: Message, files: list[tuple[str, bytes]], *, caption: str | None = None):
        media_md = [InputMediaPhoto(media=BufferedInputFile(file=content, filename=name), parse_mode=None) for name, content in files]
        if caption: media_md[0].caption = caption
        try: return await message.reply_media_group(media_md)
        except TelegramBadRequest as e: self.__logger.warning("Plain media group failed, retrying plain. err=%s", e)
        media_plain = [InputMediaPhoto(media=BufferedInputFile(file=content, filename=name), parse_mode=None) for name, content in files]
        if caption: media_plain[0].caption = caption
        return await message.reply_media_group(media_plain)

    async def _reply_document_group_safe(self, message: Message, files: list[tuple[str, bytes]], *, caption: str | None = None):
        media_md = [InputMediaDocument(media=BufferedInputFile(file=content, filename=name), parse_mode=None) for name, content in files]
        if caption: media_md[0].caption = caption
        try: return await message.reply_media_group(media_md)
        except TelegramBadRequest as e: self.__logger.warning("Plain document group failed, retrying plain. err=%s", e)
        media_plain = [InputMediaDocument(media=BufferedInputFile(file=content, filename=name), parse_mode=None) for name, content in files]
        if caption: media_plain[0].caption = caption
        return await message.reply_media_group(media_plain)

    def _schedule_background(self, name: str, coro) -> None:
        task = asyncio.create_task(coro)
        self.__bg_tasks.add(task)

        def _on_done(done_task: asyncio.Task):
            self.__bg_tasks.discard(done_task)
            try:
                done_task.result()
            except asyncio.CancelledError:
                return
            except Exception:
                self.__logger.exception("Background task failed: %s", name)

        task.add_done_callback(_on_done)

    async def _safe_update_block(self, user_id: int, blocked_until: datetime):
        try: await webapp_client.update_user(user_id, {"blocked_until": blocked_until})
        except Exception as exc: self.__logger.warning("Failed to update blocked_until for user_id=%s: %s", user_id, exc)

    async def _safe_update_token_totals(self, user_id: int, input_tokens: int, output_tokens: int):
        try:
            db_user = await webapp_client.get_user("tg_id", user_id)
            prev_input = getattr(db_user, "input_tokens", 0) if db_user else 0
            prev_output = getattr(db_user, "output_tokens", 0) if db_user else 0
            new_input = prev_input + input_tokens
            new_output = prev_output + output_tokens
            await webapp_client.update_user(user_id, {"input_tokens": new_input, "output_tokens": new_output})
            self.__logger.info("Token usage | +in=%d +out=%d | prev=%d/%d | new=%d/%d", input_tokens, output_tokens, prev_input, prev_output, new_input, new_output)
            self.__logger.info("Updated tokens for %s: +%d/%d (total %d/%d)", user_id, input_tokens, output_tokens, new_input, new_output)
        except Exception as exc: self.__logger.warning("Failed to sync token totals for user_id=%s: %s", user_id, exc)

    async def parse_response(self, response: dict, message: Message, back_menu: bool = False, adv: bool = False):
        user_id = message.from_user.id
        self.__logger = self.__logger
        self.__logger.info("INCOMING message | user_id=%s | text=%r",user_id, getattr(message, "text", None))
        files = _normalize_response_files(response.get("files") or [], logger=self.__logger)
        text: str = (response.get("text") or "").strip()
        input_tokens: int = int(response.get("input_tokens") or 0)
        output_tokens: int = int(response.get("output_tokens") or 0)
        self.__logger.info("MODEL RESPONSE (raw) | user_id=%s | files=%d | text_len=%d | input_tokens=%d | output_tokens=%d",user_id, len(files), len(text), input_tokens, output_tokens)
        match = re.search(r"BLOCK_USER_TG_(\d+)", text, re.IGNORECASE)
        if match:
            days = int(match.group(1))
            text = re.sub(r"BLOCK_USER_TG_\d+", "", text, flags=re.IGNORECASE).strip()
            blocked_until = (datetime.now() + timedelta(days=days) if days > 0 else datetime.max)
            self.__logger.warning("Blocking user for %s days (until %s)", days, blocked_until)
            self._schedule_background("update_blocked_until", self._safe_update_block(user_id, blocked_until))

        if input_tokens or output_tokens:
            self._schedule_background("update_user_token_totals", self._safe_update_token_totals(user_id, input_tokens, output_tokens))

        keyboard = copy.deepcopy(user_keyboards.backk)
        if adv: keyboard.inline_keyboard.append([InlineKeyboardButton(text="Ознакомиться с программой", url="https://t.me/obucheniepeptid/32"), InlineKeyboardButton(text="Попасть на обучение", url="https://www.peptidecourse.ru/")])
        reply_markup = keyboard if back_menu else ReplyKeyboardRemove()
        if not files and not text:
            self.__logger.warning("EMPTY response (no files, no text)")
            return await self._reply_text_safe(message, "oshibochka vishla da", reply_markup=reply_markup)

        clean_text = _sanitize_response_text(text).replace("**", "*")
        if not files and not clean_text:
            self.__logger.warning("EMPTY response after citation cleanup")
            return await self._reply_text_safe(message, "oshibochka vishla da", reply_markup=reply_markup)
        out_text = clean_text + (user_texts.blockquote if adv else "")
        sent_message = None

        if out_text:
            text_markup = reply_markup
            if len(out_text) > MAX_TG_MSG_LEN:
                self.__logger.info("OUTGOING long text | len=%d | splitting", len(out_text))
                chunks = await split_text(out_text)
                for idx, chunk in enumerate(chunks[:-1], start=1):
                    self.__logger.info("OUTGOING chunk %d/%d | len=%d", idx, len(chunks), len(chunk))
                    await self._reply_text_safe(message, chunk, reply_markup=ReplyKeyboardRemove())
                sent_message = await self._reply_text_safe(message, chunks[-1], reply_markup=text_markup)
            else:
                self.__logger.info("OUTGOING text | len=%d | preview=%r", len(out_text), out_text)
                sent_message = await self._reply_text_safe(message, out_text, reply_markup=text_markup)

        if not files: return sent_message

        self.__logger.info("OUTGOING response has %d file(s)", len(files))
        image_files = [(name, content) for name, content in files if _is_image_attachment(name, content)]
        document_files = [(name, content) for name, content in files if not _is_image_attachment(name, content)]
        self.__logger.info("OUTGOING attachments split | images=%d | documents=%d", len(image_files), len(document_files))

        for chunk_idx, chunk in enumerate(_chunk_attachments(image_files, 10), start=1):
            if len(chunk) == 1:
                name, content = chunk[0]
                self.__logger.info("OUTGOING single photo | chunk=%d | filename=%s", chunk_idx, name)
                sent_message = await self._reply_photo_safe(message, BufferedInputFile(file=content, filename=name), caption=None, reply_markup=ReplyKeyboardRemove())
                continue
            self.__logger.info("OUTGOING image media group | chunk=%d | count=%d", chunk_idx, len(chunk))
            media_group_messages = await self._reply_media_group_safe(message, chunk, caption=None)
            if media_group_messages: sent_message = media_group_messages[-1]

        for chunk_idx, chunk in enumerate(_chunk_attachments(document_files, 10), start=1):
            if len(chunk) == 1:
                name, content = chunk[0]
                self.__logger.info("OUTGOING single document | chunk=%d | filename=%s", chunk_idx, name)
                sent_message = await self._reply_document_safe(message, BufferedInputFile(file=content, filename=name), caption=None, reply_markup=ReplyKeyboardRemove())
                continue
            self.__logger.info("OUTGOING document media group | chunk=%d | count=%d", chunk_idx, len(chunk))
            media_group_messages = await self._reply_document_group_safe(message, chunk, caption=None)
            if media_group_messages: sent_message = media_group_messages[-1]

        return sent_message

    async def parse_guest_query(self, response: dict, message: Message):
        user_id = message.from_user.id
        self.__logger.info("INCOMING guest_message | user_id=%s | text=%r", user_id, getattr(message, "text", None))

        files = _normalize_response_files(response.get("files") or [], logger=self.__logger)
        text: str = (response.get("text") or "").strip()
        input_tokens: int = int(response.get("input_tokens") or 0)
        output_tokens: int = int(response.get("output_tokens") or 0)

        self.__logger.info(
            "MODEL GUEST RESPONSE (raw) | user_id=%s | files=%d | text_len=%d | input_tokens=%d | output_tokens=%d",
            user_id,
            len(files),
            len(text),
            input_tokens,
            output_tokens,
        )

        match = re.search(r"BLOCK_USER_TG_(\d+)", text, re.IGNORECASE)
        if match:
            days = int(match.group(1))
            text = re.sub(r"BLOCK_USER_TG_\d+", "", text, flags=re.IGNORECASE).strip()
            blocked_until = datetime.now() + timedelta(days=days) if days > 0 else datetime.max
            self.__logger.warning("Blocking guest user for %s days (until %s)", days, blocked_until)
            self._schedule_background("update_blocked_until", self._safe_update_block(user_id, blocked_until))

        if input_tokens or output_tokens:
            self._schedule_background("update_user_token_totals", self._safe_update_token_totals(user_id, input_tokens, output_tokens))

        clean_text = _sanitize_response_text(text).replace("**", "*")

        if not clean_text:
            clean_text = "oshibochka vishla da"

        if files:
            files_hint = f"\n\n(В ответе было {len(files)} вложений. В гостевом режиме отправляется только текст.)"
            if len(clean_text) + len(files_hint) <= MAX_TG_MSG_LEN:
                clean_text += files_hint

        if len(clean_text) > MAX_TG_MSG_LEN:
            self.__logger.info("OUTGOING guest long text | len=%d | trimming", len(clean_text))
            clean_text = clean_text[: MAX_TG_MSG_LEN - 2].rstrip() + "…"

        try:
            return await message.answer_guest_query(
                result=InlineQueryResultArticle(
                    id=f"guest:{message.message_id}",
                    title="Ответ",
                    input_message_content=InputTextMessageContent(
                        message_text=clean_text,
                        parse_mode=None,
                    ),
                )
            )
        except TelegramBadRequest as e:
            self.__logger.warning("Plain guest response failed, retrying plain. err=%s", e)
            return await message.answer_guest_query(
                result=InlineQueryResultArticle(
                    id=f"guest:{message.message_id}",
                    title="Ответ",
                    input_message_content=InputTextMessageContent(
                        message_text=clean_text,
                        parse_mode=None,
                    ),
                ),
            )

expert_bot = ProfessorBot(EXPERT_BOT_TOKEN, BOT_NAMES[EXPERT_BOT_TOKEN])
expert_client = ProfessorClient(EXPERT_OPENAI_API, EXPERT_ASSISTANT_ID, keyword="professor")
expert_dp = Dispatcher(storage=MemoryStorage())
expert_dp.include_routers(expert_admin_router, expert_user_router)
expert_dp.message.middleware(ContextMiddleware(expert_bot, expert_client, role="expert"))
expert_dp.callback_query.middleware(ContextMiddleware(expert_bot, expert_client, role="expert"))

dose_bot = ProfessorBot(DOSE_BOT_TOKEN, BOT_NAMES[DOSE_BOT_TOKEN])
dose_client = ProfessorClient(DOSE_OPENAI_API, DOSE_ASSISTANT_ID, keyword="dose")
dose_dp = Dispatcher(storage=MemoryStorage())
dose_dp.include_routers(dose_admin_router, dose_user_router)
dose_dp.message.middleware(ContextMiddleware(dose_bot, dose_client, role="expert"))
dose_dp.callback_query.middleware(ContextMiddleware(dose_bot, dose_client, role="expert"))

professor_bot = ProfessorBot(PROFESSOR_BOT_TOKEN, BOT_NAMES[PROFESSOR_BOT_TOKEN])
professor_client = ProfessorClient(PROFESSOR_OPENAI_API, PROFESSOR_ASSISTANT_ID, keyword="new")
professor_dp = Dispatcher(storage=MemoryStorage())
professor_dp.include_routers(professor_chat_router, professor_admin_router, professor_user_router, professor_guest_router)
professor_dp.message.middleware(
    ContextMiddleware(
        professor_bot,
        professor_client,
        role="professor",
        expert_bot=expert_bot,
        expert_client=expert_client,
    )
)
professor_dp.callback_query.middleware(
    ContextMiddleware(
        professor_bot,
        professor_client,
        role="professor",
        expert_bot=expert_bot,
        expert_client=expert_client,
    )
)


async def run_expert_bot():
    polling_logger.info("Expert bot polling init: deleting webhook")
    await expert_bot.delete_webhook(drop_pending_updates=False)
    polling_logger.info("Expert bot webhook deleted: starting polling")
    await expert_dp.start_polling(expert_bot)
    polling_logger.warning("Expert bot start_polling returned")


async def run_dose_bot():
    polling_logger.info("Dose bot polling init: deleting webhook")
    await dose_bot.delete_webhook(drop_pending_updates=False)
    polling_logger.info("Dose bot webhook deleted: starting polling")
    await dose_dp.start_polling(dose_bot)
    polling_logger.warning("Dose bot start_polling returned")


async def run_professor_bot():
    polling_logger.info("Professor bot polling init: deleting webhook")
    await professor_bot.delete_webhook(drop_pending_updates=False)
    polling_logger.info("Professor bot webhook deleted: starting polling")
    await professor_dp.start_polling(professor_bot)
    polling_logger.warning("Professor bot start_polling returned")

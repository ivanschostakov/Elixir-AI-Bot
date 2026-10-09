"""One editable navigation card, with bounded text pages for long diaries."""
import secrets
from types import SimpleNamespace

from aiogram.exceptions import TelegramBadRequest
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup, InputMediaPhoto


UI_KEYS = ("mentor_panel", "mentor_cards", "mentor_media")


async def clear_input(state):
    saved = await state.get_data()
    await state.clear()
    await state.update_data(**{key: saved[key] for key in UI_KEYS if key in saved})


async def remember_card(state, message, *, kind="form", **extra):
    mid = getattr(message, "message_id", None)
    chat_id = getattr(getattr(message, "chat", None), "id", None)
    if not isinstance(mid, int) or not isinstance(chat_id, int):
        return
    card = {"message_id": mid, "chat_id": chat_id,
        "photo": bool(getattr(message, "photo", None)), "kind": kind, **extra}
    saved = await state.get_data()
    cards = [c for c in saved.get("mentor_cards", []) if (c["chat_id"], c["message_id"]) != (chat_id, mid)]
    marker = {key: value for key, value in card.items() if key not in {"pages", "token"}}
    await state.update_data(mentor_panel=card, mentor_cards=[*cards, marker][-20:])


def is_main_menu(message):
    markup = getattr(message, "reply_markup", None)
    callbacks = {b.callback_data for row in getattr(markup, "inline_keyboard", []) for b in row}
    return {"mentor:open", "user:ai:start", "user:calculators"} <= callbacks


def is_mentor_menu(message):
    markup = getattr(message, "reply_markup", None)
    callbacks = {b.callback_data for row in getattr(markup, "inline_keyboard", []) for b in row}
    return {"mentor:leave", "mentor:today", "mentor:food", "mentor:settings"} <= callbacks


async def edit_panel(message, text, markup):
    try:
        if getattr(message, "photo", None):
            await message.edit_caption(caption=text, reply_markup=markup, parse_mode=None)
        else:
            await message.edit_text(text, reply_markup=markup, parse_mode=None)
    except TelegramBadRequest as error:
        if "message is not modified" not in str(error).lower():
            raise


class MentorPanel:
    def __init__(self, message, state, target=None, saved_card=None, *, cards=(), action=None):
        self.message = message
        self.target = target if target is not None else message
        self.state = state
        self.saved_card = saved_card
        self.blocks = []
        self.cards = list(cards)
        self.action = action
        self.kind = "navigation"
        self.root = action in {"open", "menu", "start", "leave"}
        self.completed = False

    def __getattr__(self, key):
        return getattr(self.message, key)

    async def answer(self, text, reply_markup=None, **kwargs):
        self.blocks.append((str(text), reply_markup))
        return self.message

    async def edit_reply_markup(self, **kwargs):
        return await self.message.edit_reply_markup(**kwargs)

    async def complete(self, text, reply_markup=None, *, replace=False):
        rows = [[InlineKeyboardButton(text=text.splitlines()[0][:60], callback_data="mentor:receipt")]]
        if reply_markup:
            rows += reply_markup.inline_keyboard
        markup = InlineKeyboardMarkup(inline_keyboard=rows)
        if replace:
            limit = 900 if getattr(self.message, "photo", None) else 3500
            if len(text.encode("utf-16-le")) // 2 > limit:
                pages, chunk, units = [], [], 0
                serialized = markup.model_dump(mode="json")["inline_keyboard"]
                for char in text:
                    size = 2 if ord(char) > 0xffff else 1
                    if units+size > limit:
                        pages.append({"text": "".join(chunk), "rows": serialized})
                        chunk, units = [], 0
                    chunk.append(char)
                    units += size
                pages.append({"text": "".join(chunk), "rows": serialized})
                token = secrets.token_hex(4)
                receipt = self.message
                try:
                    await show_page(receipt, pages, token, 0)
                except TelegramBadRequest as error:
                    if not missing_card(error):
                        raise
                    receipt = await self.message.answer(pages[0]["text"], parse_mode=None)
                    await show_page(receipt, pages, token, 0)
                self.completed = True
                await remember_card(self.state, receipt, kind="receipt", token=token, pages=pages, index=0)
                return
            try:
                await edit_panel(self.message, text, markup)
                receipt = self.message
            except TelegramBadRequest as error:
                if not missing_card(error):
                    raise
                receipt = await self.message.answer(text, reply_markup=markup, parse_mode=None)
            self.completed = True
            await remember_card(self.state, receipt, kind="receipt")
            return
        # Other reviews retain their full explanation and consume only the action buttons.
        saved = self.saved_card or {}
        if saved.get("message_id") == self.message.message_id and len(saved.get("pages", [])) > 1:
            pages = [{"text": p["text"], "rows": markup.model_dump(mode="json")["inline_keyboard"]}
                for p in saved["pages"]]
            token = secrets.token_hex(4)
            index = saved.get("index", 0)
            await show_page(self.message, pages, token, index)
            await remember_card(self.state, self.message, kind="receipt", token=token, pages=pages, index=index)
            self.completed = True
            return
        known = any(c.get("message_id") == self.message.message_id and c.get("chat_id") == self.message.chat.id
            for c in [saved, *self.cards])
        original = (getattr(self.message, "caption", None) if getattr(self.message, "photo", None)
            else getattr(self.message, "text", None)) or ""
        full = original+"\n\n"+text
        limit = 1000 if getattr(self.message, "photo", None) else 4000
        if known and len(full.encode("utf-16-le")) // 2 <= limit:
            await edit_panel(self.message, full, markup)
        else:
            await self.message.edit_reply_markup(reply_markup=markup)
        self.completed = True
        await remember_card(self.state, self.message, kind="receipt")

    async def answer_photo(self, photo, **kwargs):
        return await show_media(self.message, self.state, photo, **kwargs)

    async def flush(self):
        if self.completed or not self.blocks:
            return
        pages = []
        limit = 900 if getattr(self.target, "photo", None) else 3500
        for text, markup in self.blocks:
            rows = markup.model_dump(mode="json")["inline_keyboard"] if markup else []
            for start in range(0, max(1, len(text)), limit):
                chunk = text[start:start+limit]
                if pages and len(pages[-1]["text"]) + len(chunk) + 2 <= limit and len(pages[-1]["rows"])+len(rows) <= 40:
                    pages[-1]["text"] += "\n\n" + chunk
                    pages[-1]["rows"] += rows
                else:
                    pages.append({"text": chunk, "rows": list(rows)})
        saved = self.saved_card if self.saved_card is not None else (await self.state.get_data()).get("mentor_panel", {})
        cards = [saved, *self.cards]
        own = next((c for c in cards if c.get("message_id") == self.target.message_id
            and c.get("chat_id") == self.message.chat.id and c.get("kind", "navigation") != "receipt"), None)
        if not own and not (self.action == "open" and is_main_menu(self.message)) and not is_mentor_menu(self.message):
            # AI prose and receipts remain history; reuse a separate navigation card.
            nav = next((c for c in reversed(self.cards) if c.get("chat_id") == self.message.chat.id
                and c.get("kind") == "navigation"), None)
            if nav and getattr(self.message, "bot", None):
                self.target = StoredCard(self.message.bot, nav)
            else:
                self.target = await self.message.answer("Открываю раздел…", parse_mode=None)
        token = secrets.token_hex(4)
        try:
            await show_page(self.target, pages, token, 0, root=self.root)
        except TelegramBadRequest as error:
            if not missing_card(error):
                raise
            self.target = await self.message.answer("Открываю раздел…", parse_mode=None)
            await show_page(self.target, pages, token, 0, root=self.root)
        await remember_card(self.state, self.target, kind=self.kind, token=token, pages=pages, root=self.root)


class StoredCard:
    def __init__(self, bot, saved):
        self.bot, self.saved = bot, saved
        self.message_id, self.photo = saved["message_id"], saved.get("photo", False)
        self.chat = SimpleNamespace(id=saved["chat_id"])

    async def edit_text(self, text, **kwargs):
        return await self.bot.edit_message_text(text=text, chat_id=self.saved["chat_id"], message_id=self.message_id, **kwargs)

    async def edit_caption(self, **kwargs):
        return await self.bot.edit_message_caption(chat_id=self.saved["chat_id"], message_id=self.message_id, **kwargs)


class ReplyCards:
    def __init__(self, message, state, *, kind="form"):
        self.message, self.state = message, state
        self.kind = kind

    def __getattr__(self, key):
        return getattr(self.message, key)

    async def answer(self, text, **kwargs):
        previous = (await self.state.get_data()).get("mentor_panel", {})
        sent = await self.message.answer(text, **kwargs)
        await remember_card(self.state, sent, kind=self.kind)
        if previous.get("kind") == "form" and previous.get("chat_id") == self.message.chat.id and getattr(self.message, "bot", None):
            try:
                await self.message.bot.edit_message_reply_markup(chat_id=previous["chat_id"],
                    message_id=previous["message_id"], reply_markup=None)
            except TelegramBadRequest as error:
                if not missing_card(error) and "message is not modified" not in str(error).lower():
                    raise
        return sent

    async def flush(self):
        pass


async def reply_panel(message, state):
    # Form responses must appear below the user's input, never above it.
    return ReplyCards(message, state)


async def complete_card(message, text, reply_markup=None, **kwargs):
    if isinstance(message, MentorPanel):
        return await message.complete(text, reply_markup)
    return await message.answer(text, reply_markup=reply_markup, **kwargs)


def missing_card(error):
    value = str(error).lower()
    return "message to edit not found" in value or "message can't be edited" in value


async def show_media(message, state, photo, **kwargs):
    saved = (await state.get_data()).get("mentor_media", {})
    caption = kwargs.get("caption")
    markup = kwargs.get("reply_markup")
    if saved.get("chat_id") == message.chat.id:
        try:
            await message.bot.edit_message_media(chat_id=saved["chat_id"], message_id=saved["message_id"],
                media=InputMediaPhoto(media=photo, caption=caption, parse_mode=kwargs.get("parse_mode")), reply_markup=markup)
            return
        except TelegramBadRequest as error:
            if "message is not modified" in str(error).lower():
                return
            if not missing_card(error):
                raise
    sent = await message.answer_photo(photo, **kwargs)
    if isinstance(getattr(sent, "message_id", None), int):
        await state.update_data(mentor_media={"message_id": sent.message_id, "chat_id": message.chat.id})
    return sent


async def show_page(message, pages, token, index, *, root=False):
    page = pages[index]
    rows, seen, home = [], set(), []
    for row in page["rows"]:
        unique = []
        for b in row:
            key = (b.get("callback_data"), b.get("url"), b["text"])
            if key in seen:
                continue
            seen.add(key)
            if b.get("callback_data") == "mentor:menu":
                home = [b]
            else:
                unique.append(b)
        if unique:
            rows.append(unique)
    paging = []
    if index:
        paging.append(InlineKeyboardButton(text="←", callback_data=f"mentor:page:{token}:{index-1}"))
    if index+1 < len(pages):
        paging.append(InlineKeyboardButton(text="→", callback_data=f"mentor:page:{token}:{index+1}"))
    if paging:
        rows.append(paging)
    if not root:
        rows.append(home or [InlineKeyboardButton(text="← Меню наставника", callback_data="mentor:menu")])
    text = page["text"] + (f"\n\n{index+1} / {len(pages)}" if len(pages) > 1 else "")
    await edit_panel(message, text, InlineKeyboardMarkup(inline_keyboard=rows))


async def navigate_page(query, state):
    saved = (await state.get_data()).get("mentor_panel", {})
    parts = query.data.split(":")
    if len(parts) != 4 or parts[2] != saved.get("token") or saved.get("message_id") != query.message.message_id:
        await query.answer("Откройте меню заново: эта страница устарела.")
        return
    index = int(parts[3]) if parts[3].isdigit() else -1
    if not 0 <= index < len(saved.get("pages", [])):
        await query.answer("Страница не найдена.")
        return
    await query.answer()
    await show_page(query.message, saved["pages"], saved["token"], index, root=saved.get("root", False))
    await state.update_data(mentor_panel={**saved, "index": index})

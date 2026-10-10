"""Edit clicked menu cards; open fresh navigation below conversational replies."""
import asyncio
import secrets
from weakref import WeakValueDictionary

from aiogram.exceptions import TelegramBadRequest
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup, InputMediaPhoto


UI_KEYS = ("mentor_panel", "mentor_cards", "mentor_media", "mentor_latest_message")
_panel_locks = WeakValueDictionary()


def panel_lock(state):
    key = (id(state.storage), state.key) if hasattr(state, "key") else id(state)
    lock = _panel_locks.get(key)
    if lock is None:
        lock = asyncio.Lock()
        _panel_locks[key] = lock
    return lock


def same_card(card, message):
    return (card.get("message_id") == message.message_id
        and card.get("chat_id") == message.chat.id)


def latest_message(saved, chat_id):
    candidates = [saved.get("mentor_latest_message", {}), saved.get("mentor_panel", {}),
        saved.get("mentor_media", {}), *saved.get("mentor_cards", [])]
    return max((card for card in candidates if card.get("chat_id") == chat_id
        and isinstance(card.get("message_id"), int)), key=lambda card: card["message_id"], default={})


async def remember_message(state, message, *, kind="user"):
    """Hook for incoming messages and each successful outgoing delivery, not callbacks.

    Call with kind="answer" for AI replies. This only advances the watermark;
    it never makes conversation text an editable service card.
    """
    mid = getattr(message, "message_id", None)
    chat_id = getattr(getattr(message, "chat", None), "id", None)
    if not isinstance(mid, int) or not isinstance(chat_id, int):
        return
    saved = await state.get_data()
    latest = latest_message(saved, chat_id)
    if mid >= latest.get("message_id", 0):
        await state.update_data(mentor_latest_message={"message_id": mid, "chat_id": chat_id, "kind": kind})


def navigation_mode(message, saved, *, action=None):
    """Only the clicked, latest service card can be edited; never retarget a click."""
    cards = [saved.get("mentor_panel", {}), *saved.get("mentor_cards", [])]
    own = next((card for card in cards if same_card(card, message)), None)
    latest = latest_message(saved, message.chat.id)
    if latest.get("message_id", 0) > message.message_id:
        active = saved.get("mentor_panel", {})
        if (active.get("chat_id") == message.chat.id
            and active.get("message_id") == latest["message_id"]
            and active.get("kind", "navigation") in {"navigation", "form"}):
            return "ignore"
        return "send"
    if own is not None:
        return "edit" if own.get("kind", "navigation") in {"navigation", "form"} else "send"
    if same_card(latest, message) and latest.get("kind") in {"user", "answer", "receipt"}:
        return "send"
    if (action == "open" and is_main_menu(message)) or is_mentor_menu(message):
        return "edit"
    return "send"


async def clear_input(state):
    saved = await state.get_data()
    await state.clear()
    await state.update_data(**{key: saved[key] for key in UI_KEYS if key in saved})


async def remember_card(state, message, *, kind="form", **extra):
    mid = getattr(message, "message_id", None)
    chat_id = getattr(getattr(message, "chat", None), "id", None)
    if not isinstance(mid, int) or not isinstance(chat_id, int):
        return
    await remember_message(state, message, kind=kind)
    card = {"message_id": mid, "chat_id": chat_id,
        "photo": bool(getattr(message, "photo", None)), "kind": kind, **extra}
    saved = await state.get_data()
    cards = [c for c in saved.get("mentor_cards", []) if (c["chat_id"], c["message_id"]) != (chat_id, mid)]
    current = saved.get("mentor_panel", {})
    values = {"mentor_cards": sorted([*cards, card], key=lambda c: c["message_id"])[-20:]}
    # Completing an older draft must not promote it over the active navigation.
    if current.get("chat_id") != chat_id or mid >= current.get("message_id", 0):
        values["mentor_panel"] = card
    await state.update_data(**values)


def text_pages(text, markup, limit):
    rows = markup.model_dump(mode="json")["inline_keyboard"] if markup else []
    pages, chunk, units = [], [], 0
    for char in text:
        size = 2 if ord(char) > 0xffff else 1
        if units + size > limit:
            pages.append({"text": "".join(chunk), "rows": list(rows)})
            chunk, units = [], 0
        chunk.append(char)
        units += size
    pages.append({"text": "".join(chunk), "rows": list(rows)})
    return pages


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
        if self.completed:
            return
        markup = reply_markup or InlineKeyboardMarkup(inline_keyboard=[])
        limit = 900 if getattr(self.message, "photo", None) else 3500
        saved = await self.state.get_data()
        cards = [saved.get("mentor_panel", {}), *saved.get("mentor_cards", []), self.saved_card or {}, *self.cards]
        own = next((card for card in cards if same_card(card, self.message)), {})
        if own.get("kind") == "receipt" and own.get("completion_text") == text:
            self.completed = True
            return
        if not replace and own.get("pages"):
            pages = [{"text": p["text"], "rows": markup.model_dump(mode="json")["inline_keyboard"]}
                for p in own["pages"]]
            tail = pages.pop()["text"] + "\n\n" + text
            pages.extend(text_pages(tail, markup, limit))
        else:
            original = (getattr(self.message, "caption", None) if getattr(self.message, "photo", None)
                else getattr(self.message, "text", None)) or ""
            full = text if replace or not original else original + "\n\n" + text
            pages = text_pages(full, markup, limit)
        token = secrets.token_hex(4)
        index = 0 if replace else len(pages) - 1
        receipt = self.message
        try:
            if len(pages) == 1:
                await edit_panel(receipt, pages[0]["text"], markup)
            else:
                await show_page(receipt, pages, token, index)
        except TelegramBadRequest as error:
            if not missing_card(error):
                raise
            receipt = await show_page(self.message, pages, token, index, send=True)
        self.completed = True
        await remember_card(self.state, receipt, kind="receipt", token=token, pages=pages,
            index=index, completion_text=text)

    async def answer_photo(self, photo, **kwargs):
        return await show_media(self.message, self.state, photo, **kwargs)

    async def flush(self):
        async with panel_lock(self.state):
            await self._flush()

    async def _flush(self):
        if self.completed or not self.blocks:
            return
        saved = dict(await self.state.get_data())
        saved.setdefault("mentor_panel", self.saved_card or {})
        saved["mentor_cards"] = [*saved.get("mentor_cards", []), *self.cards]
        mode = navigation_mode(self.message, saved, action=self.action)
        if mode == "ignore":
            self.blocks.clear()
            return
        self.target = self.message
        pages = []
        limit = 900 if mode == "edit" and getattr(self.target, "photo", None) else 3500
        for text, markup in self.blocks:
            for page in text_pages(text, markup, limit):
                chunk, rows = page["text"], page["rows"]
                if pages and len((pages[-1]["text"] + "\n\n" + chunk).encode("utf-16-le")) // 2 <= limit and len(pages[-1]["rows"])+len(rows) <= 40:
                    pages[-1]["text"] += "\n\n" + chunk
                    pages[-1]["rows"] += rows
                else:
                    pages.append({"text": chunk, "rows": list(rows)})
        token = secrets.token_hex(4)
        try:
            self.target = await show_page(self.target, pages, token, 0, root=self.root, send=mode == "send")
        except TelegramBadRequest as error:
            if not missing_card(error):
                raise
            self.target = await show_page(self.message, pages, token, 0, root=self.root, send=True)
        await remember_card(self.state, self.target, kind=self.kind, token=token, pages=pages, root=self.root)
        self.blocks.clear()


class ReplyCards:
    def __init__(self, message, state, *, kind="form"):
        self.message, self.state = message, state
        self.kind = kind

    def __getattr__(self, key):
        return getattr(self.message, key)

    async def answer(self, text, **kwargs):
        await remember_message(self.state, self.message)
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
        return await message.complete(text, reply_markup, replace=kwargs.pop("replace", False))
    return await message.answer(text, reply_markup=reply_markup, **kwargs)


def missing_card(error):
    value = str(error).lower()
    return "message to edit not found" in value or "message can't be edited" in value


async def show_media(message, state, photo, **kwargs):
    data = await state.get_data()
    saved = data.get("mentor_media", {})
    caption = kwargs.get("caption")
    markup = kwargs.get("reply_markup")
    if (saved.get("chat_id") == message.chat.id
        and saved.get("message_id") == latest_message(data, message.chat.id).get("message_id")):
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
        await remember_message(state, sent, kind="media")
        await state.update_data(mentor_media={"message_id": sent.message_id, "chat_id": message.chat.id})
    return sent


async def show_page(message, pages, token, index, *, root=False, send=False):
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
    markup = InlineKeyboardMarkup(inline_keyboard=rows)
    if send:
        return await message.answer(text, reply_markup=markup, parse_mode=None)
    await edit_panel(message, text, markup)
    return message


async def navigate_page(query, state):
    data = await state.get_data()
    parts = query.data.split(":")
    cards = [data.get("mentor_panel", {}), *data.get("mentor_cards", [])]
    saved = next((card for card in cards if same_card(card, query.message)
        and len(parts) == 4 and parts[2] == card.get("token")), {})
    # Receipt paging inspects the clicked history item; it never opens navigation.
    stale = (latest_message(data, query.message.chat.id).get("message_id", 0) > query.message.message_id
        and saved.get("kind") != "receipt")
    if not saved or stale:
        await query.answer("Откройте меню заново: эта страница устарела.")
        return
    index = int(parts[3]) if parts[3].isdigit() else -1
    if not 0 <= index < len(saved.get("pages", [])):
        await query.answer("Страница не найдена.")
        return
    await query.answer()
    await show_page(query.message, saved["pages"], saved["token"], index, root=saved.get("root", False))
    await remember_card(state, query.message, **{key: value for key, value in {**saved, "index": index}.items()
        if key not in {"message_id", "chat_id", "photo"}})

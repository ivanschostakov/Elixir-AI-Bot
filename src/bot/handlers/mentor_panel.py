"""One editable navigation card, with bounded text pages for long diaries."""
import secrets

from aiogram.exceptions import TelegramBadRequest
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup


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
    def __init__(self, message, state, target=None, saved_card=None):
        self.message = message
        self.target = target if target is not None else message
        self.state = state
        self.saved_card = saved_card
        self.blocks = []

    def __getattr__(self, key):
        return getattr(self.message, key)

    async def answer(self, text, reply_markup=None, **kwargs):
        self.blocks.append((str(text), reply_markup))
        return self.message

    async def edit_reply_markup(self, **kwargs):
        # The complete new keyboard is written atomically with the next screen.
        return self.message

    async def flush(self):
        if not self.blocks:
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
        if saved.get("message_id") != self.target.message_id or saved.get("chat_id") != self.message.chat.id:
            # An AI answer or a confirmation is immutable history, not a menu card.
            self.target = await self.message.answer("Открываю раздел…", parse_mode=None)
        token = secrets.token_hex(4)
        await self.state.update_data(mentor_panel={"token": token, "message_id": self.target.message_id,
            "chat_id": self.message.chat.id, "photo": bool(getattr(self.target, "photo", None)), "pages": pages})
        await show_page(self.target, pages, token, 0)


class StoredCard:
    def __init__(self, bot, saved):
        self.bot, self.saved = bot, saved
        self.message_id, self.photo = saved["message_id"], saved.get("photo", False)

    async def edit_text(self, text, **kwargs):
        return await self.bot.edit_message_text(text=text, chat_id=self.saved["chat_id"], message_id=self.message_id, **kwargs)

    async def edit_caption(self, **kwargs):
        return await self.bot.edit_message_caption(chat_id=self.saved["chat_id"], message_id=self.message_id, **kwargs)


async def reply_panel(message, state):
    # Form responses must appear below the user's input, never above it.
    return None


async def show_page(message, pages, token, index):
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
    await show_page(query.message, saved["pages"], saved["token"], index)

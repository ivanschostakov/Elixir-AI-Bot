"""Immutable mentor answers, safely formatted within Telegram limits."""
from .mentor_format import html_text


async def send_mentor_reply(message, text, reply_markup=None):
    remaining = str(text)
    sent = None
    while remaining:
        size = min(len(remaining), 3500)
        while len(html_text(remaining[:size]).encode("utf-16-le")) // 2 > 3900:
            size = max(1, size // 2)
        chunk, remaining = remaining[:size], remaining[size:]
        sent = await message.answer(html_text(chunk), parse_mode="HTML", protect_content=True,
            reply_markup=reply_markup if not remaining else None)
    return sent

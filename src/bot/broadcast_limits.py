from html.parser import HTMLParser


MAX_BROADCAST_TEXT_LENGTH = 4096
MAX_BROADCAST_CAPTION_LENGTH = 1024
MAX_BROADCAST_PHOTOS = 10
MAX_INLINE_BUTTONS = 100
MAX_INLINE_BUTTON_TEXT_LENGTH = 64
MAX_INLINE_BUTTON_URL_LENGTH = 256


class _VisibleTextParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []

    def handle_data(self, data: str) -> None:
        self.parts.append(data)


def telegram_text_length(value: str) -> int:
    """Return Telegram's UTF-16 text length after removing HTML markup."""
    parser = _VisibleTextParser()
    parser.feed(value)
    parser.close()
    visible_text = "".join(parser.parts)
    return len(visible_text.encode("utf-16-le")) // 2


def broadcast_text_limit(*, has_photos: bool) -> int:
    return MAX_BROADCAST_CAPTION_LENGTH if has_photos else MAX_BROADCAST_TEXT_LENGTH

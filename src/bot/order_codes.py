import re


_ORDER_CODE_RE = re.compile(r"(?<![A-Z0-9])(\d{2}-[A-Z0-9]{5}|\d{5,10})(?![A-Z0-9-])", re.IGNORECASE)


def normalize_order_code_input(value: str | int | None) -> str | None:
    """Extract one shop order code from a code or copied order caption."""
    text = str(value or "").strip()
    if not text:
        return None

    matches = {match.upper() for match in _ORDER_CODE_RE.findall(text)}
    if len(matches) != 1:
        return None
    return matches.pop()

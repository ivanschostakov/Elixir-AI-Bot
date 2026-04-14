import logging
import phonenumbers
import re

from telethon import TelegramClient
from typing import Iterable, Optional

from telethon.errors import UsernameInvalidError, UsernameNotOccupiedError
from telethon.tl.functions.contacts import ImportContactsRequest
from telethon.tl.types import InputPhoneContact

from config import TELETHON_API_ID, TELETHON_API_HASH

logger = logging.getLogger("telethon")
client = TelegramClient(TELETHON_API_ID, TELETHON_API_ID, TELETHON_API_HASH)


def normalize_phone(raw: str, default_regions: Iterable[str] = ("AM", "RU", "UA", "BY", "AZ", "KZ", "KG", "MD", "TJ", "TM", "UZ", "GE")) -> Optional[str]:
    def _clean(x: str) -> str:
        x = x.strip()
        if x.startswith("+"): return "+" + re.sub(r"\D", "", x[1:])
        return re.sub(r"\D", "", x)

    s = _clean(raw)
    if not s: return None

    if s.startswith("+"):
        try:
            n = phonenumbers.parse(s, None)
            return phonenumbers.format_number(n, phonenumbers.PhoneNumberFormat.E164) if phonenumbers.is_valid_number(n) else None
        except phonenumbers.NumberParseException: return None

    for region in default_regions:
        try:
            n = phonenumbers.parse(s, region)
            if phonenumbers.is_valid_number(n): return phonenumbers.format_number(n, phonenumbers.PhoneNumberFormat.E164)
        except phonenumbers.NumberParseException: continue

    return raw

async def get_user_id_by_phone(phone: str):
    normalized = normalize_phone(phone)
    result = await client(ImportContactsRequest([InputPhoneContact(client_id=0, phone=normalized, first_name="x", last_name="")]))
    if result.users: return result.users[0].id
    return None

async def get_user_id_by_username(username: str):
    try:
        entity = await client.get_entity(username)
        return entity.id
    except (UsernameNotOccupiedError, UsernameInvalidError, ValueError): return None

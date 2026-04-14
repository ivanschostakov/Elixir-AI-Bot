from datetime import datetime
from aiogram.fsm.context import FSMContext
from aiogram.types import Message

from config import UFA_TZ
from src.bot.texts import user_texts
from src.bot.keyboards import user_keyboards
from src.bot.states import user_states
from src.ai.webapp_client import webapp_client

PHONE_GATE_BOTS = ("professor", "dose")
DEFAULT_LAST_USED = "professor"
UNVERIFIED_REQUEST_LIMIT = 5
LAST_USED_PROFESSOR = "professor"
LAST_USED_NEW = "new"

async def _request_phone(message: Message, state: FSMContext, full_name: str | None = None):
    await state.set_state(user_states.Registration.phone)
    display_name = full_name or message.from_user.full_name
    return await message.answer(user_texts.verify_phone.replace('*', display_name), reply_markup=user_keyboards.phone)

async def _ensure_user(message: Message, professor_client):
    user_id = message.from_user.id
    user = await webapp_client.get_user("tg_id", user_id)
    if not user:
        conversation_id = await professor_client.create_conversation(user_id=user_id)
        user = await webapp_client.upsert_user(
            {
                "tg_id": user_id,
                "name": message.from_user.first_name,
                "surname": message.from_user.last_name,
                "conversation_id": conversation_id,
                "last_used": DEFAULT_LAST_USED,
            }
        )
        return user
    if not user.conversation_id:
        conversation_id = await professor_client.create_conversation(user_id=user_id)
        user = await webapp_client.update_user(user_id, {"conversation_id": conversation_id})
    return user


async def _get_unverified_requests_count(user_id: int) -> int:
    return await webapp_client.get_user_total_requests(user_id, PHONE_GATE_BOTS)

def _normalize_last_used(last_used: str | None) -> str:
    return LAST_USED_NEW if last_used == LAST_USED_NEW else LAST_USED_PROFESSOR

def _resolve_last_used(user) -> tuple[str, bool]:
    raw_last_used = getattr(user, "last_used", None) if user else None
    normalized = _normalize_last_used(raw_last_used)
    return normalized, raw_last_used not in {LAST_USED_PROFESSOR, LAST_USED_NEW}

def _resolve_mode_client(last_used: str | None, professor_client, expert_client=None):
    if _normalize_last_used(last_used) == LAST_USED_NEW: return professor_client
    return expert_client or professor_client

def _has_active_subscription(user) -> bool:
    premium_until = getattr(user, "premium_until", None)
    if not premium_until: return False
    if premium_until.tzinfo is None: premium_until = premium_until.replace(tzinfo=UFA_TZ)
    else: premium_until = premium_until.astimezone(UFA_TZ)
    return premium_until >= datetime.now(tz=UFA_TZ)

def _has_premium_request_credit(user) -> bool:
    return int(getattr(user, "premium_requests", 0) or 0) > 0

def _can_use_professor_mode(user) -> bool:
    return _has_active_subscription(user) or _has_premium_request_credit(user)

def _should_spend_premium_request(user) -> bool:
    return not _has_active_subscription(user)

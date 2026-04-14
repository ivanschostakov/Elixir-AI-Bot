import os
import logging
import pathlib
from os import getenv

from urllib.parse import quote_plus
from zoneinfo import ZoneInfo
from dotenv import load_dotenv

def env(name: str, default: str | None = None, *, strip: bool = True) -> str | None:
    v = os.getenv(name, default)
    if v is None: return None
    return v.strip() if strip else v

def env_int(name: str, default: int | None = None) -> int | None:
    v = env(name)
    if v is None or v == "": return default
    try: return int(v)
    except ValueError: return default

def env_list_ints(name: str) -> list[int]:
    raw = env(name, "")
    if not raw: return []
    parts = [p.strip() for p in raw.split(",") if p.strip()]
    out: list[int] = []
    for p in parts:
        try: out.append(int(p))
        except ValueError: pass
    return out

def build_sync_dsn(user: str, password: str, host: str, port: int, db: str) -> str: return f"postgresql+psycopg2://{quote_plus(user)}:{quote_plus(password)}@{host}:{port}/{quote_plus(db)}"
load_dotenv()
UFA_TZ = ZoneInfo("Asia/Yekaterinburg")

OWNER_TG_IDS      = env_list_ints("OWNER_TG_IDS")
ADMIN_TG_IDS      = env_list_ints("ADMIN_TG_IDS")
TELETHON_PHONE    = env("TELETHON_PHONE", "")
TELETHON_API_ID   = env("TELETHON_API_ID", "")
TELETHON_API_HASH = env("TELETHON_API_HASH", "")
TELETHON_PASSWORD = env("TELETHON_PASSWORD", None)

ELIXIR_CHAT_ID = env_int("ELIXIR_CHAT_ID", 0)
ADMIN_PANEL_TOKEN = env("ADMIN_PANEL_TOKEN", "")

PROFESSOR_BOT_TOKEN  = env("PROFESSOR_BOT_TOKEN", "")
DOSE_BOT_TOKEN = env("DOSE_BOT_TOKEN", "")
NEW_BOT_TOKEN = env("NEW_BOT_TOKEN", "")

PROFESSOR_ASSISTANT_ID  = env("PROFESSOR_ASSISTANT_ID", "")
DOSE_ASSISTANT_ID = env("DOSE_ASSISTANT_ID", "")
NEW_ASSISTANT_ID = env("NEW_ASSISTANT_ID", "")

OPENAI_VS_IDS = [vs_id for vs_id in env("OPENAI_VS_IDS").split(',')]

PROFESSOR_OPENAI_API  = env("PROFESSOR_OPENAI_API", "")
DOSE_OPENAI_API = env("DOSE_OPENAI_API", "")
NEW_OPENAI_API = env("NEW_OPENAI_API", "")
AI_REQUEST_TIMEOUT_SECONDS = env_int("AI_REQUEST_TIMEOUT_SECONDS", 600)
AI_CONVERSATION_SOFT_INPUT_TOKENS = max(env_int("AI_CONVERSATION_SOFT_INPUT_TOKENS", 240000) or 240000, 1)
AI_CONVERSATION_HARD_INPUT_TOKENS = max(env_int("AI_CONVERSATION_HARD_INPUT_TOKENS", 250000) or 250000, 1)

POSTGRES_DB       = env("POSTGRES_DB", "postgres") or "postgres"
POSTGRES_USER     = env("POSTGRES_USER", "postgres") or "postgres"
POSTGRES_HOST     = env("POSTGRES_HOST", "localhost") or "localhost"
POSTGRES_PORT     = env_int("POSTGRES_PORT", 5432) or 5432
POSTGRES_PASSWORD = env("POSTGRES_PASSWORD", "") or ""

SYNC_DATABASE_URL  = build_sync_dsn(POSTGRES_USER, POSTGRES_PASSWORD, POSTGRES_HOST, POSTGRES_PORT, POSTGRES_DB)

BASE_DIR = pathlib.Path(__file__).resolve().parent
DATA_DIR      = BASE_DIR / "data"
INSTRUCTIONS_DIR = DATA_DIR / "instructions"
LOGS_DIR      = BASE_DIR / "logs"
SPENDS_DIR    = DATA_DIR / "spends"
DOWNLOADS_DIR = DATA_DIR / "downloads"

for d in (DATA_DIR, DOWNLOADS_DIR, SPENDS_DIR): d.mkdir(parents=True, exist_ok=True)

API_PREFIX = "/api/v1"
WEBAPP_BASE_DOMAIN = env("WEBAPP_BASE_DOMAIN", "")
INTERNAL_API_BASE_URL = env("INTERNAL_API_BASE_URL", WEBAPP_BASE_DOMAIN or "")
INTERNAL_API_TOKEN = env("INTERNAL_API_TOKEN", "")

_log = logging.getLogger("config")
if AI_CONVERSATION_HARD_INPUT_TOKENS < AI_CONVERSATION_SOFT_INPUT_TOKENS:
    _log.warning(
        "AI_CONVERSATION_HARD_INPUT_TOKENS=%s is below AI_CONVERSATION_SOFT_INPUT_TOKENS=%s; clamping hard limit to soft limit",
        AI_CONVERSATION_HARD_INPUT_TOKENS,
        AI_CONVERSATION_SOFT_INPUT_TOKENS,
    )
    AI_CONVERSATION_HARD_INPUT_TOKENS = AI_CONVERSATION_SOFT_INPUT_TOKENS
if not OWNER_TG_IDS: _log.warning("ADMIN_TG_IDS is empty or invalid; admin-only filters may not work.")
if not PROFESSOR_BOT_TOKEN: _log.warning("AI_BOT_TOKEN is empty.")

BOT_NAMES = {
    PROFESSOR_BOT_TOKEN: "@ProfessorOfPeptidesbot",
    DOSE_BOT_TOKEN: "@Peptideexpertbot",
    NEW_BOT_TOKEN: "@peptidestestbot",
}

BOT_KEYWORDS = {
    PROFESSOR_ASSISTANT_ID: "professor",
    DOSE_ASSISTANT_ID: "dose",
    NEW_ASSISTANT_ID: "new",
}

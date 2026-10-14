"""Short-lived, user-scoped copies of instruction replies, never diary drafts."""
import hashlib
import json
import logging
import re
import time

import config

log = logging.getLogger(__name__)
TTL = 3600


def cache_key(text, context):
    revision = config.env("TELEGRAM_MENTOR_INSTRUCTION_CACHE_REVISION", "20261014")
    return hashlib.sha256(json.dumps([revision, text, context], ensure_ascii=False, sort_keys=True).encode()).hexdigest()


def connection():
    from src.ai.telegram_mentor import _state_db
    return _state_db()


def prepare(db):
    db.execute("CREATE TABLE IF NOT EXISTS instruction_cache (user_id INTEGER NOT NULL, cache_key TEXT NOT NULL, created REAL NOT NULL, response TEXT NOT NULL, PRIMARY KEY(user_id, cache_key))")


def load(uid, key):
    try:
        with connection() as db:
            prepare(db)
            db.execute("DELETE FROM instruction_cache WHERE created<?", (time.time()-TTL,))
            row = db.execute("SELECT response FROM instruction_cache WHERE user_id=? AND cache_key=?", (uid, key)).fetchone()
        return json.loads(row[0]) if row else None
    except Exception:
        log.warning("instruction_cache_read_failed")
        return None


def store(uid, key, response, mode):
    if (not response.get("mentor") or not response.get("text") or response.get("files")
        or any(response.get(k) for k in ("meal_draft", "food_estimate", "target_draft", "program_draft", "record_drafts"))):
        return
    if re.search(r"временно недоступ|не удалось (?:получить|загрузить)|попробуйте (?:позже|ещё раз)|сервис недоступ|temporary unavailable|service unavailable", response["text"], re.I):
        return
    value = {"mentor": True, "text": response["text"], "files": [], "instruction_mode": mode,
        "instruction_course_id": response.get("instruction_course_id"), "open_shop": response.get("open_shop", False)}
    try:
        with connection() as db:
            prepare(db)
            db.execute("INSERT INTO instruction_cache VALUES (?,?,?,?) ON CONFLICT(user_id,cache_key) DO UPDATE SET created=excluded.created,response=excluded.response",
                (uid, key, time.time(), json.dumps(value, ensure_ascii=False)))
            db.execute("DELETE FROM instruction_cache WHERE user_id=? AND cache_key NOT IN (SELECT cache_key FROM instruction_cache WHERE user_id=? ORDER BY created DESC LIMIT 20)", (uid, uid))
    except Exception:
        log.warning("instruction_cache_write_failed")


def erase(uid):
    with connection() as db:
        prepare(db)
        db.execute("DELETE FROM instruction_cache WHERE user_id=?", (uid,))

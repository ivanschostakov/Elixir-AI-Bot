"""Import the production bot without credentials or a network connection."""
import asyncio
import os
import tempfile

os.environ["TELETHON_API_ID"] = "1"
os.environ["TELETHON_API_HASH"] = "test-only-api-hash"
os.environ["DOTENV_DISABLED"] = "1"
# The legacy package creates its Telethon client eagerly at import time.
os.chdir(tempfile.mkdtemp(prefix="mentor-tests-"))
_loop = asyncio.new_event_loop()
asyncio.set_event_loop(_loop)


def pytest_sessionfinish(session, exitstatus):
    _loop.close()

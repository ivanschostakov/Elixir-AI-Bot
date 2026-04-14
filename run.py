import asyncio
import logging
import signal
import time

from src.bot.main import run_dose_bot, run_new_bot, run_professor_bot
from src.logger import setup_logging
from src.tg_methods import client as tg_client

logger = logging.getLogger("main")
RESTART_DELAY_SECONDS = 3.0
HEARTBEAT_SECONDS = 60.0


async def _run_forever(name: str, runner):
    while True:
        try:
            logger.info("Starting %s polling", name)
            await runner()
            logger.warning("%s polling returned unexpectedly. Restarting in %.1fs", name, RESTART_DELAY_SECONDS)
        except asyncio.CancelledError:
            logger.info("%s polling task cancelled", name)
            raise
        except Exception:
            logger.exception("%s polling crashed. Restarting in %.1fs", name, RESTART_DELAY_SECONDS)
        await asyncio.sleep(RESTART_DELAY_SECONDS)


def _task_state(task: asyncio.Task) -> str:
    if not task.done(): return "alive"
    if task.cancelled(): return "cancelled"
    try: exc = task.exception()
    except asyncio.CancelledError: return "cancelled"
    if exc is None: return "done:ok"
    return f"done:exc={type(exc).__name__}"


async def _heartbeat(stop_event: asyncio.Event, task_map: dict[str, asyncio.Task]):
    next_tick = time.monotonic() + HEARTBEAT_SECONDS
    while not stop_event.is_set():
        snapshot = ", ".join(f"{name}={_task_state(task)}" for name, task in task_map.items())
        active_tasks_count = len(asyncio.all_tasks())
        try: tg_connected = bool(tg_client.is_connected())
        except Exception: tg_connected = False
        loop_lag_ms = max(0, int((time.monotonic() - next_tick) * 1000))
        logger.info(
            "Heartbeat | telethon_connected=%s | active_tasks=%d | loop_lag_ms=%d | tasks=[%s]",
            tg_connected,
            active_tasks_count,
            loop_lag_ms,
            snapshot,
        )
        try: await asyncio.wait_for(stop_event.wait(), timeout=HEARTBEAT_SECONDS)
        except asyncio.TimeoutError: pass
        next_tick += HEARTBEAT_SECONDS


async def main():
    await tg_client.start()
    stop_event = asyncio.Event()
    task_map: dict[str, asyncio.Task] = {
        "new_bot": asyncio.create_task(_run_forever("new_bot", run_new_bot)),
        "dose_bot": asyncio.create_task(_run_forever("dose_bot", run_dose_bot)),
        "professor_bot": asyncio.create_task(_run_forever("professor_bot", run_professor_bot)),
    }
    task_map["heartbeat"] = asyncio.create_task(_heartbeat(stop_event, task_map))
    tasks = list(task_map.values())

    async def shutdown():
        if stop_event.is_set(): return
        stop_event.set()
        logger.warning("Shutting down gracefully...")
        for task in tasks:
            if not task.done(): task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        try: await tg_client.disconnect()
        except Exception: logger.exception("Failed to disconnect Telethon client cleanly")
        logger.info("All background tasks stopped cleanly.")

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM): loop.add_signal_handler(sig, lambda s=sig: asyncio.create_task(shutdown()))

    try: await asyncio.gather(*tasks)
    except asyncio.CancelledError: logger.info("Tasks cancelled; exiting gracefully.")
    except Exception as e:
        logger.exception("Unexpected error: %s", e)
        await shutdown()


if __name__ == "__main__":
    setup_logging()
    try: asyncio.run(main())
    except KeyboardInterrupt: logger.warning("Interrupted manually (Ctrl+C). Exiting.")

import asyncio
import logging

from aiogram import Bot, Dispatcher, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.types import (
    Message,
    InlineQueryResultArticle,
    InputTextMessageContent,
)


TOKEN = "8496287141:AAEDaHGQTjhvpTQ9dMMcOhhV-sdzp_-qNHU"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)

log = logging.getLogger("guest_test_bot")

router = Router()


@router.guest_message()
async def guest_test(message: Message):
    log.info(
        "GUEST MESSAGE | message_id=%s | chat_id=%s | from_user=%s | guest_query_id=%s | text=%r | caption=%r",
        message.message_id,
        message.chat.id if message.chat else None,
        message.from_user.id if message.from_user else None,
        message.guest_query_id,
        message.text,
        message.caption,
    )

    text = message.text or message.caption or "no text"

    try:
        result = await message.answer_guest_query(
            result=InlineQueryResultArticle(
                id=f"guest-{message.message_id}",
                title="Guest test answer",
                input_message_content=InputTextMessageContent(
                    message_text=f"Guest mode works ✅\n\nYou sent: {text}",
                    parse_mode=ParseMode.HTML,
                ),
            )
        )

        log.info("GUEST ANSWER SENT | result=%s", result)
        return result

    except Exception:
        log.exception("GUEST ANSWER FAILED")
        raise


@router.message()
async def normal_message(message: Message):
    log.info(
        "NORMAL MESSAGE | message_id=%s | chat_id=%s | from_user=%s | text=%r",
        message.message_id,
        message.chat.id if message.chat else None,
        message.from_user.id if message.from_user else None,
        message.text,
    )

    await message.answer("Normal bot message works ✅")


async def main():
    log.info("Starting bot")

    bot = Bot(
        TOKEN,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )

    dp = Dispatcher()
    dp.include_router(router)

    log.info("Deleting webhook")
    await bot.delete_webhook(drop_pending_updates=True)

    log.info("Starting polling")
    await dp.start_polling(
        bot,
        allowed_updates=[
            "message",
            "guest_message",
        ],
    )


if __name__ == "__main__":
    asyncio.run(main())
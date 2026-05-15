from __future__ import annotations

import asyncio
import logging

from aiogram import Bot, Dispatcher
from aiogram.fsm.storage.memory import MemoryStorage
from dotenv import load_dotenv

from bot.config import bot_token
from bot.handlers import router

load_dotenv()


async def main() -> None:
    logging.basicConfig(level=logging.INFO)
    bot = Bot(token=bot_token())
    dp = Dispatcher(storage=MemoryStorage())
    dp.include_router(router)
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())

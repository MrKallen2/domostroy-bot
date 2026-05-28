import asyncio
from aiogram import Bot
import requests

TOKEN = "8427605899:AAG-gfSPTCDRtmG9lpMRAg16JAcInWisrP0"


async def reset():
    bot = Bot(token=TOKEN)

    # Сбрасываем вебхук и все pending updates
    await bot.delete_webhook(drop_pending_updates=True)
    print("✅ Вебхук сброшен, ожидающие обновления удалены")

    # Закрываем сессию
    await bot.session.close()
    print("✅ Сессия закрыта")


asyncio.run(reset())
print("✅ Готово! Теперь можно запускать бота заново")
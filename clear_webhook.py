import asyncio
import os
from aiogram import Bot

# Используй тот же токен, что и в твоем боте
API_TOKEN = os.getenv('BOT_TOKEN') 
# Если ты запускаешь это локально, замени os.getenv('BOT_TOKEN') на твой токен в кавычках:
# API_TOKEN = "ТВОЙ_ТОКЕН" 

async def clear_hook():
    if not API_TOKEN:
        print("Ошибка: Токен бота не найден в переменных окружения.")
        return

    bot = Bot(token=API_TOKEN)
    print("Попытка сбросить Webhook...")
    
    # Этот метод удалит любой активный Webhook
    result = await bot.delete_webhook(drop_pending_updates=True)
    
    if result:
        print("✅ Успех! Webhook сброшен. Все ожидающие обновления удалены.")
    else:
        print("❌ Не удалось сбросить Webhook. Проверь токен.")
    
    await bot.session.close() # Закрываем сессию

if __name__ == "__main__":
    asyncio.run(clear_hook())

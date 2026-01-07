import asyncio
import logging
import os
import random
import string
# --- ИМПОРТЫ: asyncpg вместо aiosqlite ---
import asyncpg
from aiohttp import web
from aiogram import Bot, Dispatcher, types, F
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import ReplyKeyboardMarkup, KeyboardButton, InlineKeyboardMarkup, InlineKeyboardButton
from geopy.distance import geodesic

# --- НАСТРОЙКИ ---
API_TOKEN = os.getenv('BOT_TOKEN')
PORT = int(os.getenv("PORT", 8080))
DATABASE_URL = os.getenv('DATABASE_URL') # Переменная для Neon
# !!! ВАЖНО: Убедитесь, что ваш ADMIN_ID указан правильно !!!
ADMIN_ID = 1031055597

# Глобальная переменная для пула подключений к БД (будет установлена в main)
db_pool: asyncpg.Pool = None

logging.basicConfig(level=logging.INFO)
bot = Bot(token=API_TOKEN)
dp = Dispatcher()

class Reg(StatesGroup):
    name = State()
    phone = State()
    location = State()

# --- БАЗА ДАННЫХ (POSTGRESQL) ---
async def init_db_pool():
    # Создаем пул подключений, используя DATABASE_URL
    global db_pool
    if not DATABASE_URL:
        logging.error("DATABASE_URL не установлена. Выход.")
        exit()
        
    db_pool = await asyncpg.create_pool(DATABASE_URL)
    logging.info("PostgreSQL Pool создан.")

    # Создание таблиц (если их нет)
    async with db_pool.acquire() as conn:
        # Таблица users
        await conn.execute('''
            CREATE TABLE IF NOT EXISTS users (
                id BIGINT PRIMARY KEY,
                name TEXT,
                phone TEXT,
                lat REAL,
                lon REAL
            )
        ''')
        # Таблица rests
        await conn.execute('''
            CREATE TABLE IF NOT EXISTS rests (
                id SERIAL PRIMARY KEY,
                name TEXT,
                lat REAL,
                lon REAL,
                boxes INTEGER DEFAULT 5
            )
        ''')
    logging.info("Database tables проверены/созданы.")

# --- HTTP SERVER ДЛЯ RENDER (Health Check) ---
async def handle_hc(request):
    return web.Response(text="Bot is running!")

async def start_http_server():
    app = web.Application()
    app.router.add_get("/", handle_hc)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", PORT)
    await site.start()
    logging.info(f"Health check server started on port {PORT}")

# --- ЛОГИКА БОТА ---
@dp.message(Command("start"))
async def start(message: types.Message, state: FSMContext):
    await message.answer("Xush kelibsiz! «Uvol bo'lmasin»! 😊\nВведите Имя и Фамилию:")
    await state.set_state(Reg.name)

@dp.message(Reg.name)
async def get_name(message: types.Message, state: FSMContext):
    await state.update_data(name=message.text)
    kb = ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text="📱 Отправить номер", request_contact=True)]], 
        resize_keyboard=True, one_time_keyboard=True
    )
    await message.answer("Отправьте номер телефона кнопкой ниже:", reply_markup=kb)
    await state.set_state(Reg.phone)

@dp.message(Reg.phone, F.contact)
async def get_phone(message: types.Message, state: FSMContext):
    await state.update_data(phone=message.contact.phone_number)
    kb = ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text="📍 Отправить локацию", request_location=True)]], 
        resize_keyboard=True, one_time_keyboard=True
    )
    await message.answer("Отправьте геолокацию, чтобы найти еду рядом:", reply_markup=kb)
    await state.set_state(Reg.location)

@dp.message(Reg.location, F.location)
async def get_loc(message: types.Message, state: FSMContext):
    data = await state.get_data()
    lat, lon = message.location.latitude, message.location.longitude
    
    # --- ЛОГИКА БД (asyncpg) ---
    async with db_pool.acquire() as conn:
        await conn.execute(
            'INSERT INTO users (id, name, phone, lat, lon) VALUES ($1, $2, $3, $4, $5) ON CONFLICT (id) DO UPDATE SET name=$2, phone=$3, lat=$4, lon=$5',
            message.from_user.id, data['name'], data['phone'], lat, lon
        )
    # --- КОНЕЦ ЛОГИКИ БД ---
    
    await message.answer("✅ Регистрация завершена!", reply_markup=types.ReplyKeyboardRemove())
    await show_restaurants(message, lat, lon)

async def show_restaurants(message, u_lat, u_lon):
    # --- ЛОГИКА БД (asyncpg) ---
    async with db_pool.acquire() as conn:
        rests = await conn.fetch('SELECT name, lat, lon, boxes, id FROM rests WHERE boxes > 0')
    # --- КОНЕЦ ЛОГИКИ БД ---
    
    if not rests:
        await message.answer("К сожалению, сейчас нет активных предложений рядом с вами. 😔")
        return

    nearby = []
    for r in rests:
        # r[1] = lat, r[2] = lon
        dist = geodesic((u_lat, u_lon), (r[1], r[2])).km
        if dist < 10: # Показываем в радиусе 10км
            # r[0]=name, r[3]=boxes, r[4]=id
            nearby.append((r[0], dist, r[3], r[4]))
    
    nearby.sort(key=lambda x: x[1])
    
    if not nearby:
        await message.answer("Рядом с вами (в радиусе 10км) ничего не найдено.")
        return

    text = "🥡 Доступные наборы (15 000 сум):\n\n"
    buttons = []
    for r in nearby:
        text += f"📍 {r[0]} ({r[1]:.1f} км) — Осталось: {r[2]} шт.\n"
        buttons.append([InlineKeyboardButton(text=f"Забронировать в {r[0]}", callback_data=f"book_{r[3]}")])

    await message.answer(text, reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons))

# --- ОБРАБОТКА БРОНИРОВАНИЯ ---
@dp.callback_query(F.data.startswith("book_"))
async def handle_booking(callback: types.CallbackQuery):
    rest_id = int(callback.data.split("_")[1])
    
    # --- ЛОГИКА БД (asyncpg) ---
    async with db_pool.acquire() as conn:
        # Обновляем и возвращаем имя и новое кол-во порций
        res = await conn.fetchrow(
            'UPDATE rests SET boxes = boxes - 1 WHERE id = $1 AND boxes > 0 RETURNING name, boxes',
            rest_id
        )
    # --- КОНЕЦ ЛОГИКИ БД ---
        
    if res:
        name, new_boxes = res
        code = ''.join(random.choices(string.ascii_uppercase + string.digits, k=6))
        await callback.message.edit_text(
            f"✅ Успешно! Ресторан: **{name}**\n"
            f"Ваш код брони: `{code}`\n"
            f"Покажите его сотруднику для оплаты и получения."
        )
    else:
        await callback.answer("Увы, наборы в этом заведении уже закончились!", show_alert=True)
        await callback.message.delete()

@dp.message(Command("add"))
async def add_rest(message: types.Message):
    if message.from_user.id != ADMIN_ID:
        await message.answer("Access denied.")
        return

    try:
        p = message.text.split(maxsplit=3)
        # /add Name Lat Lon
        # --- ЛОГИКА БД (asyncpg) ---
        async with db_pool.acquire() as conn:
            await conn.execute(
                'INSERT INTO rests (name, lat, lon, boxes) VALUES ($1, $2, $3, $4)',
                p[1], float(p[2]), float(p[3]), 5
            )
        # --- КОНЕЦ ЛОГИКИ БД ---
        await message.answer(f"✅ Ресторан {p[1]} добавлен (5 наборов)!")
    except Exception:
        await message.answer("Ошибка! Формат: /add Название 41.31 69.27")

# --- НОВЫЕ ФУНКЦИИ АДМИНИСТРАТОРА (POSTGRESQL) ---

@dp.message(Command("admin"))
async def admin_panel(message: types.Message):
    if message.from_user.id != ADMIN_ID:
        await message.answer("Access denied.")
        return

    # --- ЛОГИКА БД (asyncpg) ---
    async with db_pool.acquire() as conn:
        rests = await conn.fetch('SELECT id, name, boxes FROM rests ORDER BY id')
    # --- КОНЕЦ ЛОГИКИ БД ---
    
    if not rests:
        await message.answer("Нет добавленных ресторанов в базе данных.")
        return

    text = "⚙️ **Панель Управления Ресторанами** ⚙️\n\n"
    buttons = []
    
    for r in rests:
        rest_id, name, boxes = r
        text += f"📍 **{name}** | Наборов: **{boxes}** | ID: {rest_id}\n"
        
        buttons.append([
            InlineKeyboardButton(text=f"➕ Добавить 5 наборов в {name}", callback_data=f"admin_add_5_{rest_id}")
        ])

    await message.answer(text, reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons))


@dp.callback_query(F.data.startswith("admin_"))
async def handle_admin_action(callback: types.CallbackQuery):
    if callback.from_user.id != ADMIN_ID:
        await callback.answer("У вас нет прав администратора.", show_alert=True)
        return
    
    parts = callback.data.split("_")
    action = parts[1]
    amount = int(parts[2])
    rest_id = int(parts[3])
    
    if action == 'add':
        # --- ЛОГИКА БД (asyncpg) ---
        async with db_pool.acquire() as conn:
            res = await conn.fetchrow(
                'UPDATE rests SET boxes = boxes + $1 WHERE id = $2 RETURNING name, boxes',
                amount, rest_id
            )
        # --- КОНЕЦ ЛОГИКИ БД ---
        
        if res:
            name, new_boxes = res
            
            await callback.message.edit_text(
                f"✅ Наборы обновлены!\n"
                f"📍 Ресторан: **{name}**\n"
                f"Новое кол-во наборов: **{new_boxes}**",
                reply_markup=callback.message.reply_markup
            )
        else:
             await callback.answer("Ошибка: Ресторан не найден.", show_alert=True)
            
    await callback.answer()

# --- ЗАПУСК ---
async def main():
    await init_db_pool() # Изменено на init_db_pool()
    
    # Гарантированный сброс Webhook для стабильного Polling
    await bot.delete_webhook(drop_pending_updates=True) 
    
    # Запускаем бота и веб-сервер одновременно
    await asyncio.gather(
        dp.start_polling(bot),
        start_http_server()
    )

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass

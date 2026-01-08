import asyncio
import logging
import os
import random
import string
from typing import Callable, Awaitable, Dict, Any

import asyncpg
from aiohttp import web
from aiogram import Bot, Dispatcher, types, F, BaseMiddleware
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import ReplyKeyboardMarkup, KeyboardButton, InlineKeyboardMarkup, InlineKeyboardButton, ReplyKeyboardRemove
from geopy.distance import geodesic

# --- ИМПОРТ GOOGLE SHEETS ---
try:
    from sheets import GoogleSheetsManager
except ImportError:
    logging.warning("⚠️ Файл sheets.py не найден!")
    GoogleSheetsManager = None

logging.basicConfig(level=logging.INFO)

# --- 1. КОНФИГУРАЦИЯ ---
class Config:
    API_TOKEN = os.getenv('BOT_TOKEN')
    PORT = int(os.getenv("PORT", 8080))
    DATABASE_URL = os.getenv('DATABASE_URL')
    ADMIN_ID = 1031055597 
    SHEET_LINK = "https://docs.google.com/spreadsheets/d/15WbaWB9Hjq7ypEMeCvJ1_FyX__b0U3MWbt8boWom5B8/edit?usp=sharing"

# --- 2. БАЗА ДАННЫХ ---
class Database:
    def __init__(self):
        self._pool: asyncpg.Pool = None

    async def init_pool(self, url: str):
        if not url:
            logging.error("DATABASE_URL пуст!")
            return
        clean_url = url.strip()
        self._pool = await asyncpg.create_pool(clean_url)
        logging.info("PostgreSQL Pool создан.")
        await self._ensure_tables_exist()

    async def close_pool(self):
        if self._pool:
            await self._pool.close()

    async def _ensure_tables_exist(self):
        async with self._pool.acquire() as conn:
            await conn.execute('''
                CREATE TABLE IF NOT EXISTS users (
                    id BIGINT PRIMARY KEY, name TEXT, phone TEXT, lat REAL, lon REAL
                )
            ''')
            await conn.execute('''
                CREATE TABLE IF NOT EXISTS rests (
                    id SERIAL PRIMARY KEY, name TEXT, lat REAL, lon REAL, boxes INTEGER DEFAULT 5
                )
            ''')

    async def create_or_update_user(self, user_id, name, phone, lat, lon):
        await self._pool.execute(
            'INSERT INTO users (id, name, phone, lat, lon) VALUES ($1, $2, $3, $4, $5) '
            'ON CONFLICT (id) DO UPDATE SET name=$2, phone=$3, lat=$4, lon=$5',
            user_id, name, phone, lat, lon
        )

    async def get_active_rests(self):
        return await self._pool.fetch('SELECT name, lat, lon, boxes, id FROM rests WHERE boxes > 0')

    async def decrement_boxes_atomic(self, rest_id):
        return await self._pool.fetchrow(
            'UPDATE rests SET boxes = boxes - 1 WHERE id = $1 AND boxes > 0 RETURNING name, boxes',
            rest_id
        )

    async def get_all_rests(self):
        return await self._pool.fetch('SELECT id, name, boxes FROM rests ORDER BY name')

    async def get_rest_details(self, rest_id):
        return await self._pool.fetchrow('SELECT name, boxes FROM rests WHERE id = $1', rest_id)

    async def set_boxes_quantity(self, rest_id, quantity):
        return await self._pool.fetchrow(
            'UPDATE rests SET boxes = $1 WHERE id = $2 RETURNING name, boxes',
            quantity, rest_id
        )

    async def increment_boxes(self, rest_id, delta):
        return await self._pool.fetchrow(
            'UPDATE rests SET boxes = boxes + $1 WHERE id = $2 RETURNING name, boxes',
            delta, rest_id
        )

    async def insert_new_rest(self, name, lat, lon, initial_boxes=5):
        await self._pool.execute(
            'INSERT INTO rests (name, lat, lon, boxes) VALUES ($1, $2, $3, $4)',
            name, lat, lon, initial_boxes
        )

    async def delete_rest_by_id(self, rest_id):
        return await self._pool.fetchval('DELETE FROM rests WHERE id = $1 RETURNING name', rest_id)

# --- 3. СОСТОЯНИЯ ---
class Reg(StatesGroup):
    name = State()
    phone = State()
    location = State()

class AdminStates(StatesGroup):
    waiting_for_new_quantity = State()
    adding_rest_name = State() 
    adding_rest_location = State() 
    waiting_for_delete_confirm = State() 

# --- 4. MIDDLEWARE ---
class AdminAccessMiddleware(BaseMiddleware):
    def __init__(self, admin_id: int):
        super().__init__()
        self.admin_id = admin_id

    async def __call__(self, handler, event, data):
        user_id = event.from_user.id
        is_admin_cmd = (isinstance(event, types.Message) and event.text == '/admin')
        is_admin_cb = (isinstance(event, types.CallbackQuery) and str(event.data).startswith('admin_'))

        if user_id == self.admin_id or not (is_admin_cmd or is_admin_cb):
            return await handler(event, data)
        await event.answer("Отказано в доступе.")

# --- 5. ХЕНДЛЕРЫ ---
dp = Dispatcher()

@dp.message(Command("start"))
async def start(message: types.Message, state: FSMContext):
    await state.clear()
    await message.answer("Xush kelibsiz! «Uvol bo'lmasin»! 😊\nIsmingizni kiriting:")
    await state.set_state(Reg.name)

@dp.message(Reg.name, F.text)
async def get_name(message: types.Message, state: FSMContext):
    await state.update_data(name=message.text)
    kb = ReplyKeyboardMarkup(keyboard=[[KeyboardButton(text="📱 Telefon raqan", request_contact=True)]], resize_keyboard=True)
    await message.answer("Telefon raqam yuborish:", reply_markup=kb)
    await state.set_state(Reg.phone)

@dp.message(Reg.phone, F.contact)
async def get_phone(message: types.Message, state: FSMContext):
    await state.update_data(phone=message.contact.phone_number)
    kb = ReplyKeyboardMarkup(keyboard=[[KeyboardButton(text="📍 Joykashuv", request_location=True)]], resize_keyboard=True)
    await message.answer("Joylashuvni yuborish:", reply_markup=kb)
    await state.set_state(Reg.location)

@dp.message(Reg.location, F.location)
async def get_loc(message: types.Message, state: FSMContext, db: Database, gs: Any):
    data = await state.get_data()
    lat, lon = message.location.latitude, message.location.longitude
    
    # Сохранение в БД
    await db.create_or_update_user(message.from_user.id, data['name'], data['phone'], lat, lon)
    
    # Сохранение в Google Таблицы (Исправлено: добавлено имя пользователя)
    if gs:
        username = message.from_user.username or "NoUsername"
        asyncio.create_task(gs.add_user(
            user_id=message.from_user.id,
            username=username,
            name=data['name'], 
            phone=data['phone'], 
            lat=lat, 
            lon=lon
        ))
        
    await message.answer("✅ Готово!", reply_markup=ReplyKeyboardRemove())
    await state.clear()
    await show_restaurants(message, lat, lon, db)

async def show_restaurants(message, u_lat, u_lon, db):
    rests = await db.get_active_rests()
    if not rests:
        return await message.answer("Нет активных предложений.")
    
    nearby = []
    for r in rests:
        dist = geodesic((u_lat, u_lon), (r['lat'], r['lon'])).km
        if dist < 10:
            nearby.append((r['name'], dist, r['boxes'], r['id']))
    
    if not nearby:
        return await message.answer("Рядом ничего не найдено.")

    text = "🥡 Доступные наборы:\n"
    buttons = []
    for r in nearby:
        text += f"📍 {r[0]} ({r[1]:.1f} км) — {r[2]} шт.\n"
        buttons.append([InlineKeyboardButton(text=f"Бронь: {r[0]}", callback_data=f"book_{r[3]}")])
    
    await message.answer(text, reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons))

@dp.callback_query(F.data.startswith("book_"))
async def handle_booking(callback: types.CallbackQuery, db: Database):
    rest_id = int(callback.data.split("_")[1])
    res = await db.decrement_boxes_atomic(rest_id)
    if res:
        code = ''.join(random.choices(string.ascii_uppercase + string.digits, k=6))
        await callback.message.edit_text(f"✅ Забронировано в {res['name']}\nКод: `{code}`", parse_mode="Markdown")
    else:
        await callback.answer("Закончилось!", show_alert=True)

# --- АДМИНКА ---
@dp.message(Command("admin"))
async def admin_main(message: types.Message, db: Database):
    rests = await db.get_all_rests()
    btns = [[InlineKeyboardButton(text=f"{r['name']} ({r['boxes']})", callback_data=f"admin_select_{r['id']}")] for r in rests]
    btns.append([InlineKeyboardButton(text="➕ Добавить ресторан", callback_data="admin_add_new")])
    await message.answer("Админ-панель:", reply_markup=InlineKeyboardMarkup(inline_keyboard=btns))

@dp.callback_query(F.data == "admin_add_new")
async def admin_add_start(callback: types.CallbackQuery, state: FSMContext):
    await callback.message.edit_text("Введите название:")
    await state.set_state(AdminStates.adding_rest_name)

@dp.message(AdminStates.adding_rest_name)
async def admin_add_name(message: types.Message, state: FSMContext):
    await state.update_data(new_name=message.text)
    await message.answer("Отправьте локацию ресторана:")
    await state.set_state(AdminStates.adding_rest_location)

@dp.message(AdminStates.adding_rest_location, F.location)
async def admin_add_loc(message: types.Message, state: FSMContext, db: Database, gs: Any):
    data = await state.get_data()
    lat, lon = message.location.latitude, message.location.longitude
    await db.insert_new_rest(data['new_name'], lat, lon)
    if gs:
        asyncio.create_task(gs.add_restaurant(data['new_name'], lat, lon))
    await message.answer("✅ Ресторан добавлен!")
    await state.clear()

# --- 6. HEALTH CHECK ---
async def handle_hc(request):
    return web.Response(text="Bot is OK")

async def start_http_server():
    app = web.Application()
    app.router.add_get("/", handle_hc)
    runner = web.AppRunner(app)
    await runner.setup()
    # Исправлено: запуск сервера теперь не блокирует основной поток
    site = web.TCPSite(runner, "0.0.0.0", Config.PORT)
    await site.start()

# --- 7. БЛОК ЗАПУСКА ---
async def main():
    bot = Bot(token=Config.API_TOKEN)
    db = Database()
    
    logging.info("Подключение к базе данных...")
    await db.init_pool(Config.DATABASE_URL)
    
    if db._pool is None:
        logging.critical("❌ Ошибка: БД не подключена!")
        return

    gs = None
    if GoogleSheetsManager:
        try:
            gs = GoogleSheetsManager(Config.SHEET_LINK)
            logging.info("✅ Google Sheets подключены.")
        except Exception as e:
            logging.error(f"❌ Ошибка Google Sheets: {e}")

    dp.message.middleware(AdminAccessMiddleware(Config.ADMIN_ID))
    dp.callback_query.middleware(AdminAccessMiddleware(Config.ADMIN_ID))

    try:
        logging.info("🚀 Бот запускается...")
        # Сначала запускаем HTTP сервер
        await start_http_server()
        # Затем запускаем поллинг
        await dp.start_polling(bot, db=db, gs=gs)
    finally:
        await db.close_pool()

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        logging.info("Бот остановлен.")
    except Exception as e:
        logging.critical(f"Ошибка при запуске: {e}")

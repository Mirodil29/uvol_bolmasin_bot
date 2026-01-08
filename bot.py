import asyncio
import logging
import os
import random
import string
from typing import Callable, Awaitable, Dict, Any

# --- ИМПОРТЫ СТОРОННИХ БИБЛИОТЕК ---
import asyncpg
from aiohttp import web
from aiogram import Bot, Dispatcher, types, F, BaseMiddleware
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    ReplyKeyboardMarkup, KeyboardButton, 
    InlineKeyboardMarkup, InlineKeyboardButton, 
    ReplyKeyboardRemove
)
from geopy.distance import geodesic

# --- ИМПОРТ ВАШЕГО МОДУЛЯ GOOGLE SHEETS ---
# Файл sheets.py должен лежать рядом с main.py
# Ставим заглушку принудительно
GoogleSheetsManager = None

# Установка уровня логирования
logging.basicConfig(level=logging.INFO)

# --- 1. КОНФИГУРАЦИЯ ---
class Config:
    API_TOKEN = os.getenv('BOT_TOKEN')
    PORT = int(os.getenv("PORT", 8080))
    DATABASE_URL = os.getenv('DATABASE_URL')
    ADMIN_ID = 1031055597 
    # Ссылка на вашу таблицу
    SHEET_LINK = "https://docs.google.com/spreadsheets/d/15WbaWB9Hjq7ypEMeCvJ1_FyX__b0U3MWbt8boWom5B8/edit?usp=sharing"

# --- 2. БАЗА ДАННЫХ (PostgreSQL) ---
class Database:
    def __init__(self):
        self._pool: asyncpg.Pool = None

    async def init_pool(self, url: str):
        if not url:
            logging.error("DATABASE_URL не установлен!")
            return
        self._pool = await asyncpg.create_pool(url)
        logging.info("PostgreSQL Pool создан.")
        await self._ensure_tables_exist()

    async def close_pool(self):
        if self._pool:
            await self._pool.close()
            logging.info("PostgreSQL Pool закрыт.")

    async def _ensure_tables_exist(self):
        async with self._pool.acquire() as conn:
            # Таблица пользователей
            await conn.execute('''
                CREATE TABLE IF NOT EXISTS users (
                    id BIGINT PRIMARY KEY,
                    name TEXT,
                    phone TEXT,
                    lat REAL,
                    lon REAL
                )
            ''')
            # Таблица ресторанов
            await conn.execute('''
                CREATE TABLE IF NOT EXISTS rests (
                    id SERIAL PRIMARY KEY,
                    name TEXT,
                    lat REAL,
                    lon REAL,
                    boxes INTEGER DEFAULT 5
                )
            ''')
            
            # Миграции (добавление колонок, если их нет)
            async def column_exists(table, col):
                val = await conn.fetchval(
                    "SELECT 1 FROM information_schema.columns WHERE table_name=$1 AND column_name=$2", 
                    table, col
                )
                return val is not None

            if not await column_exists('rests', 'lat'):
                await conn.execute('ALTER TABLE rests ADD COLUMN lat REAL')
            if not await column_exists('rests', 'lon'):
                await conn.execute('ALTER TABLE rests ADD COLUMN lon REAL')
            if not await column_exists('rests', 'boxes'):
                await conn.execute('ALTER TABLE rests ADD COLUMN boxes INTEGER DEFAULT 5')

    # --- SQL ЗАПРОСЫ ---
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

# --- 3. FSM СОСТОЯНИЯ ---
class Reg(StatesGroup):
    name = State()
    phone = State()
    location = State()

class AdminStates(StatesGroup):
    waiting_for_new_quantity = State()
    adding_rest_name = State() 
    adding_rest_location = State() 
    waiting_for_delete_confirm = State() 

# --- 4. MIDDLEWARE (Защита админки) ---
class AdminAccessMiddleware(BaseMiddleware):
    def __init__(self, admin_id: int):
        super().__init__()
        self.admin_id = admin_id

    async def __call__(self, handler, event, data):
        user_id = event.from_user.id
        # Проверяем, пытаются ли войти в админку
        is_admin_action = (
            (isinstance(event, types.Message) and event.text == '/admin') or
            (isinstance(event, types.CallbackQuery) and str(event.data).startswith('admin_'))
        )

        if user_id == self.admin_id or not is_admin_action:
            return await handler(event, data)
        else:
            if isinstance(event, types.Message):
                await event.answer("⛔ Access denied.")
            elif isinstance(event, types.CallbackQuery):
                await event.answer("⛔ Access denied.", show_alert=True)
            return

# --- 5. ЛОГИКА БОТА ---
dp = Dispatcher()

# === ЮЗЕР: РЕГИСТРАЦИЯ ===
@dp.message(Command("start"))
async def start(message: types.Message, state: FSMContext):
    await state.clear()
    await message.answer("Xush kelibsiz! «Uvol bo'lmasin»! 😊\nВведите Имя и Фамилию:")
    await state.set_state(Reg.name)

@dp.message(Reg.name, F.text)
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
async def get_loc(message: types.Message, state: FSMContext, db: Database, gs: Any):
    """Финал регистрации: сохранение в БД и Google Sheets."""
    data = await state.get_data()
    lat, lon = message.location.latitude, message.location.longitude
    
    # 1. Сохраняем в PostgreSQL
    try:
        await db.create_or_update_user(message.from_user.id, data['name'], data['phone'], lat, lon)
    except Exception as e:
        logging.error(f"DB Error: {e}")
        await message.answer("❌ Ошибка базы данных.")
        return

    # 2. Сохраняем в Google Sheets (фоновая задача)
    if gs:
        asyncio.create_task(gs.add_user(
            user_id=message.from_user.id,
            username=message.from_user.username or "NoUsername",
            name=data['name'],
            phone=data['phone'],
            lat=lat,
            lon=lon
        ))

    await message.answer("✅ Регистрация завершена!", reply_markup=ReplyKeyboardRemove())
    await state.clear()
    await show_restaurants(message, lat, lon, db)

# === ЮЗЕР: ПОИСК И БРОНЬ ===
async def show_restaurants(message, u_lat, u_lon, db: Database):
    rests = await db.get_active_rests()
    if not rests:
        await message.answer("К сожалению, сейчас нет активных предложений рядом. 😔")
        return

    nearby = []
    for r in rests:
        dist = geodesic((u_lat, u_lon), (r['lat'], r['lon'])).km
        if dist < 10: # Радиус 10 км
            nearby.append((r['name'], dist, r['boxes'], r['id']))
    
    nearby.sort(key=lambda x: x[1])
    
    if not nearby:
        await message.answer("Рядом с вами (в радиусе 10км) ничего не найдено.")
        return

    text = "🥡 **Доступные наборы (15 000 сум):**\n\n"
    buttons = []
    for r in nearby:
        text += f"📍 {r[0]} ({r[1]:.1f} км) — Осталось: {r[2]} шт.\n"
        buttons.append([InlineKeyboardButton(text=f"Забронировать в {r[0]}", callback_data=f"book_{r[3]}")])

    await message.answer(text, reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons), parse_mode="Markdown")

@dp.callback_query(F.data.startswith("book_"))
async def handle_booking(callback: types.CallbackQuery, db: Database):
    rest_id = int(callback.data.split("_")[1])
    res = await db.decrement_boxes_atomic(rest_id)
        
    if res:
        name = res['name']
        code = ''.join(random.choices(string.ascii_uppercase + string.digits, k=6))
        await callback.message.edit_text(
            f"✅ Успешно! Ресторан: **{name}**\n"
            f"Ваш код брони: `{code}`\n"
            f"Покажите его сотруднику для оплаты.",
            parse_mode="Markdown"
        )
    else:
        await callback.answer("Увы, наборы закончились!", show_alert=True)
        try: await callback.message.delete()
        except: pass

# === АДМИН ПАНЕЛЬ ===
@dp.message(Command("admin"))
async def admin_panel(message: types.Message, state: FSMContext, db: Database):
    await state.clear()
    await send_admin_menu(message, db)

async def send_admin_menu(message: types.Message, db: Database, text=None):
    rests = await db.get_all_rests()
    text = text or "⚙️ **Панель Управления**\nВыберите действие:"
    
    buttons = []
    for r in rests:
        buttons.append([InlineKeyboardButton(text=f"📍 {r['name']} (Ост: {r['boxes']})", callback_data=f"admin_select_{r['id']}")])
    
    buttons.append([InlineKeyboardButton(text="➕ Добавить Ресторан", callback_data="admin_add_new")])
    await message.answer(text, reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons), parse_mode="Markdown")

# --- Добавление ресторана ---
@dp.callback_query(F.data == "admin_add_new")
async def admin_add_start(callback: types.CallbackQuery, state: FSMContext):
    await callback.message.edit_text("Введите **Название** ресторана:", parse_mode="Markdown")
    await state.set_state(AdminStates.adding_rest_name)

@dp.message(AdminStates.adding_rest_name, F.text)
async def admin_add_name(message: types.Message, state: FSMContext):
    await state.update_data(new_rest_name=message.text)
    kb = ReplyKeyboardMarkup(keyboard=[[KeyboardButton(text="📍 Локация", request_location=True)]], resize_keyboard=True)
    await message.answer("Теперь отправьте **Геолокацию**:", reply_markup=kb, parse_mode="Markdown")
    await state.set_state(AdminStates.adding_rest_location)

@dp.message(AdminStates.adding_rest_location, F.location)
async def admin_add_loc(message: types.Message, state: FSMContext, db: Database, gs: Any):
    data = await state.get_data()
    name = data['new_rest_name']
    lat, lon = message.location.latitude, message.location.longitude

    # 1. БД
    await db.insert_new_rest(name, lat, lon)
    
    # 2. Google Sheets
    if gs:
        asyncio.create_task(gs.add_restaurant(rest_name=name, lat=lat, lon=lon))

    await message.answer(f"✅ Ресторан **{name}** добавлен!", reply_markup=ReplyKeyboardRemove(), parse_mode="Markdown")
    await state.clear()
    await send_admin_menu(message, db)

# --- Управление рестораном ---
@dp.callback_query(F.data.startswith("admin_select_"))
async def admin_rest_options(callback: types.CallbackQuery, state: FSMContext, db: Database):
    rest_id = int(callback.data.split("_")[-1])
    rest = await db.get_rest_details(rest_id)
    if not rest:
        return await callback.answer("Ресторан не найден", show_alert=True)
    
    await state.update_data(cur_id=rest_id, cur_name=rest['name'])
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📝 Изм. кол-во", callback_data="admin_set_qty")],
        [InlineKeyboardButton(text="➕ Быстро +5", callback_data=f"admin_add_5_{rest_id}")],
        [InlineKeyboardButton(text="🗑️ Удалить", callback_data="admin_del_ask")],
        [InlineKeyboardButton(text="⬅️ Назад", callback_data="admin_back")]
    ])
    await callback.message.edit_text(f"📍 **{rest['name']}**\nНаборов: {rest['boxes']}", reply_markup=kb, parse_mode="Markdown")

@dp.callback_query(F.data == "admin_back")
async def back_to_main(callback: types.CallbackQuery, state: FSMContext, db: Database):
    await state.clear()
    await callback.message.delete()
    await send_admin_menu(callback.message, db)

@dp.callback_query(F.data.startswith("admin_add_5_"))
async def quick_add(callback: types.CallbackQuery, db: Database):
    rest_id = int(callback.data.split("_")[-1])
    await db.increment_boxes(rest_id, 5)
    await callback.answer("+5 наборов добавлено!")
    # Обновляем меню
    rest = await db.get_rest_details(rest_id)
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📝 Изм. кол-во", callback_data="admin_set_qty")],
        [InlineKeyboardButton(text="➕ Быстро +5", callback_data=f"admin_add_5_{rest_id}")],
        [InlineKeyboardButton(text="🗑️ Удалить", callback_data="admin_del_ask")],
        [InlineKeyboardButton(text="⬅️ Назад", callback_data="admin_back")]
    ])
    try:
        await callback.message.edit_text(f"📍 **{rest['name']}**\nНаборов: {rest['boxes']}", reply_markup=kb, parse_mode="Markdown")
    except: pass

@dp.callback_query(F.data == "admin_set_qty")
async def set_qty_ask(callback: types.CallbackQuery, state: FSMContext):
    await callback.message.edit_text("Введите новое количество (число):")
    await state.set_state(AdminStates.waiting_for_new_quantity)

@dp.message(AdminStates.waiting_for_new_quantity)
async def set_qty_done(message: types.Message, state: FSMContext, db: Database):
    if not message.text.isdigit():
        return await message.answer("Введите число!")
    
    data = await state.get_data()
    await db.set_boxes_quantity(data['cur_id'], int(message.text))
    await message.answer("✅ Количество обновлено.")
    await state.clear()
    await send_admin_menu(message, db)

@dp.callback_query(F.data == "admin_del_ask")
async def del_ask(callback: types.CallbackQuery):
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Да, удалить", callback_data="admin_del_confirm")],
        [InlineKeyboardButton(text="❌ Нет", callback_data="admin_back")]
    ])
    await callback.message.edit_text("Вы уверены? Это действие необратимо.", reply_markup=kb)

@dp.callback_query(F.data == "admin_del_confirm")
async def del_confirm(callback: types.CallbackQuery, state: FSMContext, db: Database):
    data = await state.get_data()
    await db.delete_rest_by_id(data['cur_id'])
    await callback.answer("Ресторан удален.")
    await state.clear()
    await callback.message.delete()
    await send_admin_menu(callback.message, db)

# --- 6. HEALTH CHECK (Для Render) ---
async def handle_hc(request):
    return web.Response(text="Bot is running OK!")

async def start_http_server():
    app = web.Application()
    app.router.add_get("/", handle_hc)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", Config.PORT)
    await site.start()

# --- 7. ЗАПУСК ---
async def main():
    bot = Bot(token=Config.API_TOKEN)
    db = Database()
    
    # Инициализация Google Sheets
    gs = None
    if GoogleSheetsManager:
        try:
            gs = GoogleSheetsManager(Config.https://docs.google.com/spreadsheets/d/15WbaWB9Hjq7ypEMeCvJ1_FyX__b0U3MWbt8boWom5B8/edit?usp=sharing)
            logging.info("✅ Google Sheets подключены.")
        except Exception as e:
            logging.error(f"❌ Ошибка Google Sheets: {e}")

    # Регистрация Middleware
    dp.message.middleware(AdminAccessMiddleware(Config.ADMIN_ID))
    dp.callback_query.middleware(AdminAccessMiddleware(Config.ADMIN_ID))

    # Запуск
    await db.init_pool(Config.DATABASE_URL)
    try:
        logging.info("🚀 Бот запускается...")
        # Передаем db и gs в диспетчер, чтобы они были доступны в хендлерах
        await asyncio.gather(
            dp.start_polling(bot, db=db, gs=gs),
            start_http_server()
        )
    finally:
        await db.close_pool()

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass

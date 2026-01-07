import asyncio
import logging
from aiogram import Bot, Dispatcher, types, F
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import ReplyKeyboardMarkup, KeyboardButton, InlineKeyboardMarkup, InlineKeyboardButton
from geopy.distance import geodesic
import aiosqlite

# --- 1. НАСТРОЙКИ ---
API_TOKEN = '8484796508:AAHiuOTZT1JbrYBb4BpZn2riBT0AtK2TXnc'
ADMIN_ID = 0 

logging.basicConfig(level=logging.INFO)
bot = Bot(token=API_TOKEN)
dp = Dispatcher()

class Reg(StatesGroup):
    name = State()
    phone = State()
    location = State()

async def init_db():
    async with aiosqlite.connect('uvol_bolmasin.db') as db:
        await db.execute('CREATE TABLE IF NOT EXISTS users (id INTEGER PRIMARY KEY, name TEXT, phone TEXT, lat REAL, lon REAL)')
        await db.execute('CREATE TABLE IF NOT EXISTS rests (id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT, lat REAL, lon REAL, boxes INTEGER DEFAULT 5)')
        await db.commit()

@dp.message(Command("start"))
async def start(message: types.Message, state: FSMContext):
    global ADMIN_ID
    if ADMIN_ID == 0: ADMIN_ID = message.from_user.id
    await message.answer("Xush kelibsiz! «Uvol bo'lmasin»! 😊\nВведите Имя и Фамилию:")
    await state.set_state(Reg.name)

@dp.message(Reg.name)
async def get_name(message: types.Message, state: FSMContext):
    await state.update_data(name=message.text)
    kb = ReplyKeyboardMarkup(keyboard=[[KeyboardButton(text="📱 Номер", request_contact=True)]], resize_keyboard=True)
    await message.answer("Отправьте номер телефона:", reply_markup=kb)
    await state.set_state(Reg.phone)

@dp.message(Reg.phone, F.contact)
async def get_phone(message: types.Message, state: FSMContext):
    await state.update_data(phone=message.contact.phone_number)
    kb = ReplyKeyboardMarkup(keyboard=[[KeyboardButton(text="📍 Локация", request_location=True)]], resize_keyboard=True)
    await message.answer("Отправьте геолокацию:", reply_markup=kb)
    await state.set_state(Reg.location)

@dp.message(Reg.location, F.location)
async def get_loc(message: types.Message, state: FSMContext):
    data = await state.get_data()
    lat, lon = message.location.latitude, message.location.longitude
    async with aiosqlite.connect('uvol_bolmasin.db') as db:
        await db.execute('INSERT OR REPLACE INTO users VALUES (?, ?, ?, ?, ?)', (message.from_user.id, data['name'], data['phone'], lat, lon))
        await db.commit()
    await message.answer("✅ Регистрация завершена!")
    await state.clear()
    await show_restaurants(message, lat, lon)

async def show_restaurants(message: types.Message, u_lat, u_lon):
    async with aiosqlite.connect('uvol_bolmasin.db') as db:
        async with db.execute('SELECT name, lat, lon, boxes, id FROM rests') as cursor:
            rests = await cursor.fetchall()
    if not rests:
        await message.answer("Пока нет активных предложений.")
        return
    nearby = []
    for r in rests:
        dist = geodesic((u_lat, u_lon), (r[1], r[2])).km
        nearby.append((r[0], dist, r[3], r[4]))
    nearby.sort(key=lambda x: x[1])
    text = "🥡 Доступные наборы (15 000 сум):\n\n"
    buttons = [[InlineKeyboardButton(text=f"Забронировать в {r[0]}", callback_data=f"book_{r[3]}")] for r in nearby]
    await message.answer(text, reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons))

@dp.message(Command("add"))
async def add_rest(message: types.Message):
    if message.from_user.id != ADMIN_ID: return
    try:
        p = message.text.split()
        async with aiosqlite.connect('uvol_bolmasin.db') as db:
            await db.execute('INSERT INTO rests (name, lat, lon) VALUES (?, ?, ?)', (p[1], float(p[2]), float(p[3])))
            await db.commit()
        await message.answer(f"✅ Ресторан {p[1]} добавлен!")
    except:
        await message.answer("Пример: /add Bon! 41.31 69.27")

async def main():
    await init_db()
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())

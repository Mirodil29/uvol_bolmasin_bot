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
from aiogram.types import ReplyKeyboardMarkup, KeyboardButton, InlineKeyboardMarkup, InlineKeyboardButton, ReplyKeyboardRemove
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
# Диспетчер теперь будет автоматически использовать MemoryStorage для FSM
dp = Dispatcher() 

# --- FSM СОСТОЯНИЯ: ПОЛЬЗОВАТЕЛИ ---
class Reg(StatesGroup):
    name = State()
    phone = State()
    location = State()

# --- FSM СОСТОЯНИЯ: АДМИН ПАНЕЛЬ (НОВЫЕ СОСТОЯНИЯ) ---
class AdminStates(StatesGroup):
    """Состояния для администрирования ресторанов"""
    # Состояние ожидания ввода нового количества для ресторана
    waiting_for_new_quantity = State()
    # Состояние ожидания ввода данных для нового ресторана
    waiting_for_new_rest_data = State() 

# --- БАЗА ДАННЫХ (POSTGRESQL) ---
async def init_db_pool():
    global db_pool
    if not DATABASE_URL:
        logging.error("DATABASE_URL не установлен!")
        return

    db_pool = await asyncpg.create_pool(DATABASE_URL)
    logging.info("PostgreSQL Pool создан.")

    # Создание таблиц (если их нет)
    async with db_pool.acquire() as conn:
        # PostgreSQL использует SERIAL PRIMARY KEY
        await conn.execute('''
            CREATE TABLE IF NOT EXISTS users (
                id BIGINT PRIMARY KEY,
                name TEXT,
                phone TEXT,
                lat REAL,
                lon REAL
            )
        ''')
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

# --- ЛОГИКА БОТА: РЕГИСТРАЦИЯ И БРОНИРОВАНИЕ (ОСТАВЛЕНО БЕЗ ИЗМЕНЕНИЙ) ---

@dp.message(Command("start"))
async def start(message: types.Message, state: FSMContext):
    # Добавляем сброс состояния на случай, если пользователь завис
    await state.clear()
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
    
    await message.answer("✅ Регистрация завершена!", reply_markup=ReplyKeyboardRemove())
    await state.clear()
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
        dist = geodesic((u_lat, u_lon), (r[1], r[2])).km
        if dist < 10: # Показываем в радиусе 10км
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
async def add_rest_old(message: types.Message):
    if message.from_user.id != ADMIN_ID:
        await message.answer("Access denied.")
        return
    # Эта команда устарела, так как мы будем использовать FSM для добавления
    await message.answer("⚠️ Команда /add устарела. Используйте /admin и подменю 'Добавить Ресторан'.")

# --- НОВЫЕ ФУНКЦИИ АДМИНИСТРАТОРА (FSM + POSTGRESQL) ---

def get_admin_main_keyboard(rests):
    """Генерирует клавиатуру с ресторанами для управления"""
    buttons = []
    for r in rests:
        rest_id, name, boxes = r
        # Новый callback для выбора ресторана, например: admin_select:123
        buttons.append([InlineKeyboardButton(text=f"📍 {name} (Наборов: {boxes})", callback_data=f"admin_select_{rest_id}")])
    
    # Кнопка для добавления нового ресторана
    buttons.append([InlineKeyboardButton(text="➕ Добавить Новый Ресторан", callback_data="admin_add_new")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)


async def send_admin_panel(message: types.Message, text: str = None):
    """Отправляет или редактирует главное меню админ-панели"""
    async with db_pool.acquire() as conn:
        rests = await conn.fetch('SELECT id, name, boxes FROM rests ORDER BY name')
    
    text = text if text else "⚙️ **Панель Управления Ресторанами** ⚙️\nВыберите ресторан для управления:"
    
    await message.answer(text, reply_markup=get_admin_main_keyboard(rests), parse_mode="Markdown")


@dp.message(Command("admin"))
async def admin_panel(message: types.Message, state: FSMContext):
    if message.from_user.id != ADMIN_ID:
        await message.answer("Access denied.")
        return
    
    # Сброс любого предыдущего FSM состояния администратора перед показом меню
    await state.clear() 
    await send_admin_panel(message)


@dp.callback_query(F.data.startswith("admin_select_"))
async def admin_select_rest(callback: types.CallbackQuery, state: FSMContext):
    """Показывает подменю управления выбранным рестораном"""
    rest_id = int(callback.data.split("_")[-1])
    
    async with db_pool.acquire() as conn:
        rest = await conn.fetchrow('SELECT name, boxes FROM rests WHERE id = $1', rest_id)
    
    if not rest:
        await callback.answer("Ресторан не найден.", show_alert=True)
        return
    
    name, boxes = rest
    
    # Сохраняем ID ресторана в FSM Context для дальнейших действий
    await state.update_data(current_rest_id=rest_id)
    
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🎯 Установить Количество", callback_data="admin_set_qty")],
        [InlineKeyboardButton(text="➕ Добавить +5 наборов (Быстро)", callback_data=f"admin_add_5_{rest_id}")],
        [InlineKeyboardButton(text="🗑️ Удалить Ресторан", callback_data=f"admin_delete_{rest_id}")],
        [InlineKeyboardButton(text="⬅️ Назад к списку", callback_data="admin_back")]
    ])
    
    await callback.message.edit_text(
        f"🛠️ **Управление: {name}**\n\nТекущий остаток: **{boxes}** наборов.",
        reply_markup=keyboard,
        parse_mode="Markdown"
    )
    await callback.answer()


@dp.callback_query(F.data == "admin_set_qty")
async def admin_start_set_quantity(callback: types.CallbackQuery, state: FSMContext):
    """Начинает процесс установки количества (переводит в FSM)"""
    data = await state.get_data()
    rest_id = data.get('current_rest_id')
    
    async with db_pool.acquire() as conn:
        name = await conn.fetchval('SELECT name FROM rests WHERE id = $1', rest_id)
    
    if not rest_id or not name:
        await callback.answer("Ошибка FSM. Вернитесь в главное меню.", show_alert=True)
        await send_admin_panel(callback.message)
        return
    
    await callback.message.edit_text(
        f"**Введите НОВОЕ общее количество наборов для ресторана {name}.**\n\n(Только целое число, например: **30**)",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="❌ Отмена", callback_data="admin_cancel_fsm")]
        ]),
        parse_mode="Markdown"
    )
    # !!! КЛЮЧЕВОЙ ШАГ: ПЕРЕВОД В СОСТОЯНИЕ FSM !!!
    await state.set_state(AdminStates.waiting_for_new_quantity)
    await callback.answer()


@dp.message(AdminStates.waiting_for_new_quantity)
async def admin_finish_set_quantity(message: types.Message, state: FSMContext):
    """Обрабатывает введенное число и обновляет БД"""
    try:
        new_qty = int(message.text)
        if new_qty < 0:
            raise ValueError
    except ValueError:
        await message.answer("❌ Неверный ввод. Пожалуйста, введите **целое положительное число**.")
        return
        
    data = await state.get_data()
    rest_id = data.get('current_rest_id')
    
    if not rest_id:
        await message.answer("❌ Ошибка: Ресторан не выбран. Начните с /admin.")
        await state.clear()
        return

    # --- ЛОГИКА БД (asyncpg) ---
    async with db_pool.acquire() as conn:
        res = await conn.fetchrow(
            'UPDATE rests SET boxes = $1 WHERE id = $2 RETURNING name, boxes',
            new_qty, rest_id
        )
    # --- КОНЕЦ ЛОГИКИ БД ---
    
    await state.clear() # Сбрасываем FSM состояние!
    
    if res:
        name, boxes = res
        await message.answer(
            f"✅ **Успешно обновлено!**\n\n"
            f"Ресторан: **{name}**\n"
            f"Установлено: **{boxes}** наборов.",
            parse_mode="Markdown"
        )
    else:
        await message.answer("❌ Ошибка обновления. Ресторан не найден.")
    
    # Возвращаем в основное меню админа
    await send_admin_panel(message)


# --- ОБРАБОТЧИКИ НАВИГАЦИИ И ДЕЙСТВИЙ ---

@dp.callback_query(F.data == "admin_back")
async def admin_back_to_menu(callback: types.CallbackQuery, state: FSMContext):
    """Возвращает из подменю в главное меню админа"""
    await state.clear()
    await callback.message.delete()
    await send_admin_panel(callback.message)


@dp.callback_query(F.data == "admin_cancel_fsm")
async def admin_cancel_fsm(callback: types.CallbackQuery, state: FSMContext):
    """Отмена FSM состояния"""
    await state.clear()
    await callback.message.delete()
    await send_admin_panel(callback.message, text="Операция отменена. Возврат в главное меню.")


@dp.callback_query(F.data.startswith("admin_add_5_"))
async def handle_admin_add_5(callback: types.CallbackQuery):
    """Оставлена логика быстрого добавления +5"""
    rest_id = int(callback.data.split("_")[-1])
    
    # --- ЛОГИКА БД (asyncpg) ---
    async with db_pool.acquire() as conn:
        res = await conn.fetchrow(
            'UPDATE rests SET boxes = boxes + 5 WHERE id = $1 RETURNING name, boxes',
            rest_id
        )
    # --- КОНЕЦ ЛОГИКИ БД ---
        
    if res:
        name, new_boxes = res
        await callback.message.edit_text(
            f"✅ Наборы обновлены!\n📍 Ресторан: **{name}**\nНовое кол-во наборов: **{new_boxes}**",
            reply_markup=get_admin_main_keyboard([ (rest_id, name, new_boxes) ]), # Обновляем кнопку для текущего ресторана
            parse_mode="Markdown"
        )
    else:
        await callback.answer("Ошибка: Ресторан не найден.", show_alert=True)
    
    await callback.answer()

# --- ЗАПУСК ---
async def main():
    # 1. Сначала инициализируем пул подключений к Postgres
    await init_db_pool() 
    
    # 2. Гарантированный сброс Webhook для стабильного Polling
    await bot.delete_webhook(drop_pending_updates=True) 
    
    # 3. Запускаем бота и веб-сервер одновременно
    # Используем asyncio.gather для одновременного запуска
    await asyncio.gather(
        dp.start_polling(bot),
        start_http_server()
    )
    
if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass

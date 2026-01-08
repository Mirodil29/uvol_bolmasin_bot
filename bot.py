import asyncio
import logging
import os
import random
import string
from typing import Callable, Awaitable, Dict, Any
# --- ИМПОРТЫ AIOGRAM / POSTGRES ---
import asyncpg
from aiohttp import web
from aiogram import Bot, Dispatcher, types, F, BaseMiddleware
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import ReplyKeyboardMarkup, KeyboardButton, InlineKeyboardMarkup, InlineKeyboardButton, ReplyKeyboardRemove

# Импорт для расчетов дистанции
from geopy.distance import geodesic

# Установка уровня логирования
logging.basicConfig(level=logging.INFO)

# --- 1. АРХИТЕКТУРНЫЙ СЛОЙ: КОНФИГУРАЦИЯ ---
class Config:
    """Class for centralized configuration management."""
    API_TOKEN = os.getenv('BOT_TOKEN')
    PORT = int(os.getenv("PORT", 8080))
    DATABASE_URL = os.getenv('DATABASE_URL')
    # IMPORTANT: Replace with the actual administrator ID
    ADMIN_ID = 1031055597 

# --- 2. АРХИТЕКТУРНЫЙ СЛОЙ: DAO (Data Access Object) ---
class Database:
    """Class for encapsulating all database operations (PostgreSQL)."""
    def __init__(self):
        self._pool: asyncpg.Pool = None

    async def init_pool(self, url: str):
        """Initializes the connection pool and checks tables."""
        if not url:
            raise ValueError("DATABASE_URL is not set!")
        self._pool = await asyncpg.create_pool(url)
        logging.info("PostgreSQL Pool created.")
        await self._ensure_tables_exist()

    async def close_pool(self):
        """Gracefully closes the pool upon shutdown."""
        if self._pool:
            await self._pool.close()
            logging.info("PostgreSQL Pool closed.")

    async def _ensure_tables_exist(self):
        """Creates users and rests tables if they do not exist."""
        async with self._pool.acquire() as conn:
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
        logging.info("Database tables checked/created.")

    # --- CRUD: USERS ---
    async def create_or_update_user(self, user_id, name, phone, lat, lon):
        await self._pool.execute(
            'INSERT INTO users (id, name, phone, lat, lon) VALUES ($1, $2, $3, $4, $5) '
            'ON CONFLICT (id) DO UPDATE SET name=$2, phone=$3, lat=$4, lon=$5',
            user_id, name, phone, lat, lon
        )

    # --- CRUD: RESTAURANTS (for User) ---
    async def get_active_rests(self):
        return await self._pool.fetch('SELECT name, lat, lon, boxes, id FROM rests WHERE boxes > 0')

    async def decrement_boxes_atomic(self, rest_id):
        return await self._pool.fetchrow(
            'UPDATE rests SET boxes = boxes - 1 WHERE id = $1 AND boxes > 0 RETURNING name, boxes',
            rest_id
        )

    # --- CRUD: RESTAURANTS (for Admin) ---
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
        return await self._pool.fetchval(
            'DELETE FROM rests WHERE id = $1 RETURNING name',
            rest_id
        )

# --- 3. FSM STATES ---
class Reg(StatesGroup):
    name = State()
    phone = State()
    location = State()

class AdminStates(StatesGroup):
    waiting_for_new_quantity = State()
    adding_rest_name = State() 
    adding_rest_location = State() 
    waiting_for_delete_confirm = State() 

# --- 4. MIDDLEWARE: ADMIN ACCESS CONTROL ---
class AdminAccessMiddleware(BaseMiddleware):
    """
    Checks if the user is the administrator.
    If not, it stops further processing for admin-protected handlers.
    """
    def __init__(self, admin_id: int):
        super().__init__()
        self.admin_id = admin_id

    async def __call__(
        self,
        handler: Callable[[types.Message, Dict[str, Any]], Awaitable[Any]],
        event: types.Message,
        data: Dict[str, Any]
    ) -> Any:
        # Check if the handler is protected (e.g., /admin command)
        # We assume that all handlers that need this protection are explicitly marked (e.g., using a filter or command)
        
        # Simple check for the /admin command and any subsequent FSM states
        is_admin_command = (isinstance(event, types.Message) and event.text == '/admin')
        is_admin_callback = (isinstance(event, types.CallbackQuery) and event.data.startswith('admin_'))

        user_id = event.from_user.id

        if user_id == self.admin_id or not (is_admin_command or is_admin_callback):
            # If the user is an admin OR if the event is not related to admin functions,
            # we pass control to the next handler.
            return await handler(event, data)
        else:
            # If the event is related to admin functions but the user is not an admin,
            # we block the processing.
            if isinstance(event, types.Message):
                await event.answer("Access denied.")
            elif isinstance(event, types.CallbackQuery):
                await event.answer("Access denied.", show_alert=True)
            return # Stop propagation

# --- 5. INITIALIZATION AND SHUTDOWN HOOKS (Graceful Shutdown) ---
async def on_startup(dispatcher: Dispatcher, db: Database):
    """Executed upon bot launch."""
    try:
        await db.init_pool(Config.DATABASE_URL)
        dispatcher["db"] = db 
        logging.info("System ready. Database connected and passed to context.")
    except Exception as e:
        logging.critical(f"Critical DB initialization error: {e}")
        await dispatcher.stop_polling()

async def on_shutdown(dispatcher: Dispatcher, db: Database):
    """Executed upon bot shutdown."""
    await db.close_pool()
    logging.info("System shut down. Resources released.")

# --- 6. HANDLERS: USER LOGIC ---

dp = Dispatcher()

# --- User Handlers (Simplified and Cleaned) ---

@dp.message(Command("start"))
async def start(message: types.Message, state: FSMContext):
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
async def get_loc(message: types.Message, state: FSMContext, db: Database):
    """(db: Database - DI)"""
    data = await state.get_data()
    lat, lon = message.location.latitude, message.location.longitude
    
    try:
        await db.create_or_update_user(message.from_user.id, data['name'], data['phone'], lat, lon)
    except Exception as e:
        logging.error(f"DB Error (User registration): {e}")
        await message.answer("❌ Произошла ошибка при сохранении данных. Попробуйте снова.")
        return
    
    await message.answer("✅ Регистрация завершена!", reply_markup=ReplyKeyboardRemove())
    await state.clear()
    await show_restaurants(message, lat, lon, db)

async def show_restaurants(message, u_lat, u_lon, db: Database):
    """Search and display nearest active restaurants."""
    try:
        rests = await db.get_active_rests()
    except Exception as e:
        logging.error(f"DB Error (Get rests): {e}")
        await message.answer("❌ Ошибка при загрузке данных о ресторанах.")
        return

    if not rests:
        await message.answer("К сожалению, сейчас нет активных предложений рядом с вами. 😔")
        return

    nearby = []
    for r in rests:
        dist = geodesic((u_lat, u_lon), (r['lat'], r['lon'])).km
        if dist < 10: # Show within a 10km radius
            nearby.append((r['name'], dist, r['boxes'], r['id']))
    
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


@dp.callback_query(F.data.startswith("book_"))
async def handle_booking(callback: types.CallbackQuery, db: Database):
    """Booking handler with atomic box decrement. (db: Database - DI)"""
    rest_id = int(callback.data.split("_")[1])
    
    try:
        res = await db.decrement_boxes_atomic(rest_id)
    except Exception as e:
        logging.error(f"DB Error (Booking): {e}")
        await callback.answer("❌ Произошла ошибка бронирования. Попробуйте позже.", show_alert=True)
        return
        
    if res:
        name, new_boxes = res['name'], res['boxes']
        code = ''.join(random.choices(string.ascii_uppercase + string.digits, k=6))
        await callback.message.edit_text(
            f"✅ Успешно! Ресторан: **{name}**\n"
            f"Ваш код брони: `{code}`\n"
            f"Покажите его сотруднику для оплаты и получения.",
            parse_mode="Markdown"
        )
    else:
        await callback.answer("Увы, наборы в этом заведении уже закончились!", show_alert=True)
        try:
             await callback.message.delete()
        except:
             pass 

# --- 7. HANDLERS: ADMIN LOGIC (CRUD) ---

# NOTE: The admin check 'if message.from_user.id != Config.ADMIN_ID:' IS REMOVED
# because it is handled by the AdminAccessMiddleware now! This is the main architectural benefit.

async def send_admin_panel(message: types.Message, db: Database, text: str = None):
    """Sends or edits the main admin panel menu."""
    try:
        rests = await db.get_all_rests()
    except Exception as e:
        logging.error(f"DB Error (Get all rests): {e}")
        await message.answer("❌ Ошибка при загрузке списка ресторанов.")
        return
    
    text = text if text else "⚙️ **Панель Управления Ресторанами** ⚙️\nВыберите ресторан для управления:"
    
    def get_admin_main_keyboard(rests):
        buttons = []
        for r in rests:
            rest_id, name, boxes = r['id'], r['name'], r['boxes']
            buttons.append([InlineKeyboardButton(text=f"📍 {name} (Наборов: {boxes})", callback_data=f"admin_select_{rest_id}")])
        
        buttons.append([InlineKeyboardButton(text="➕ Добавить Новый Ресторан", callback_data="admin_add_new")])
        return InlineKeyboardMarkup(inline_keyboard=buttons)
    
    await message.answer(text, reply_markup=get_admin_main_keyboard(rests), parse_mode="Markdown")

@dp.message(Command("admin"))
async def admin_panel(message: types.Message, state: FSMContext, db: Database):
    """Main entry point to the admin panel. (db: Database - DI)"""
    # Removed the check: if message.from_user.id != Config.ADMIN_ID:
    
    await state.clear() 
    await send_admin_panel(message, db)

# --- ADDING A RESTAURANT ---

@dp.callback_query(F.data == "admin_add_new")
async def admin_start_add_new(callback: types.CallbackQuery, state: FSMContext):
    await state.clear()
    await callback.message.edit_text(
        "📝 **ДОБАВЛЕНИЕ РЕСТОРАНА**\n\nВведите **Название** нового ресторана:",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="❌ Отмена", callback_data="admin_cancel_fsm")]
        ]),
        parse_mode="Markdown"
    )
    await state.set_state(AdminStates.adding_rest_name)
    await callback.answer()

@dp.message(AdminStates.adding_rest_name)
async def admin_get_rest_name(message: types.Message, state: FSMContext):
    name = message.text.strip()
    if not name or len(name) < 2 or len(name) > 50:
        await message.answer("❌ Название должно быть от 2 до 50 символов. Попробуйте снова.")
        return

    await state.update_data(new_rest_name=name)
    kb = ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text="📍 Отправить локацию", request_location=True)]], 
        resize_keyboard=True, one_time_keyboard=True
    )
    await message.answer(
        f"✅ Название сохранено: **{name}**\n\nТеперь отправьте **Геолокацию** ресторана (используйте кнопку ниже).",
        reply_markup=kb,
        parse_mode="Markdown"
    )
    await state.set_state(AdminStates.adding_rest_location)

@dp.message(AdminStates.adding_rest_location, F.location)
async def admin_get_rest_location(message: types.Message, state: FSMContext, db: Database):
    """Step 3: Gets location, saves to DB, and completes FSM. (db: Database - DI)"""
    data = await state.get_data()
    name = data.get('new_rest_name')
    lat, lon = message.location.latitude, message.location.longitude
    
    if not name:
        await message.answer("❌ Ошибка: Название ресторана потеряно. Начните сначала с /admin.", reply_markup=ReplyKeyboardRemove())
        await state.clear()
        return

    try:
        await db.insert_new_rest(name, lat, lon)
        
        await message.answer(
            f"🎉 **Ресторан успешно добавлен!**\n\n"
            f"Название: **{name}**\n"
            f"Координаты: {lat:.4f}, {lon:.4f}\n"
            f"Начальное количество наборов: 5",
            reply_markup=ReplyKeyboardRemove(),
            parse_mode="Markdown"
        )
    except Exception as e:
        logging.error(f"DB Error (Insert rest): {e}")
        await message.answer("❌ Произошла ошибка при сохранении данных в базу.", reply_markup=ReplyKeyboardRemove())

    await state.clear()
    await send_admin_panel(message, db)
    

# --- QUANTITY MANAGEMENT AND DELETION ---

@dp.callback_query(F.data.startswith("admin_select_"))
async def admin_select_rest(callback: types.CallbackQuery, state: FSMContext, db: Database):
    """Shows the submenu for managing the selected restaurant. (db: Database - DI)"""
    rest_id = int(callback.data.split("_")[-1])
    
    try:
        rest = await db.get_rest_details(rest_id)
    except Exception as e:
        logging.error(f"DB Error (Get rest details): {e}")
        await callback.answer("Ошибка БД. Не удалось загрузить данные.", show_alert=True)
        return

    if not rest:
        await callback.answer("Ресторан не найден.", show_alert=True)
        return
    
    name, boxes = rest['name'], rest['boxes']
    
    await state.update_data(current_rest_id=rest_id, current_rest_name=name)
    
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🎯 Установить Количество", callback_data="admin_set_qty")],
        [InlineKeyboardButton(text="➕ Добавить +5 наборов (Быстро)", callback_data=f"admin_add_5_{rest_id}")],
        [InlineKeyboardButton(text="🗑️ Удалить Ресторан", callback_data="admin_delete_start")], 
        [InlineKeyboardButton(text="⬅️ Назад к списку", callback_data="admin_back")]
    ])
    
    await callback.message.edit_text(
        f"🛠️ **Управление: {name}**\n\nТекущий остаток: **{boxes}** наборов.",
        reply_markup=keyboard,
        parse_mode="Markdown"
    )
    await callback.answer()

# --- DELETION LOGIC ---
@dp.callback_query(F.data == "admin_delete_start")
async def admin_start_delete(callback: types.CallbackQuery, state: FSMContext):
    """Requests confirmation for restaurant deletion."""
    data = await state.get_data()
    rest_id = data.get('current_rest_id')
    name = data.get('current_rest_name')
    
    if not rest_id or not name:
        await callback.answer("Ошибка FSM. Вернитесь в главное меню.", show_alert=True)
        # We need to retrieve the db instance from the context for the admin_panel function call
        db_instance = callback.bot.get_data()["db"] 
        await admin_panel(callback.message, state, db=db_instance)
        return

    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=f"✅ Подтвердить Удаление {name}", callback_data="admin_delete_confirm")],
        [InlineKeyboardButton(text="❌ Отмена (Вернуться)", callback_data="admin_back_to_select")]
    ])
    
    await callback.message.edit_text(
        f"⚠️ **ВНИМАНИЕ! ПОДТВЕРДИТЕ УДАЛЕНИЕ**\n\nВы собираетесь безвозвратно удалить ресторан **{name}** (ID: {rest_id}) из системы.\n\nЭто действие нельзя отменить.",
        reply_markup=keyboard,
        parse_mode="Markdown"
    )
    await state.set_state(AdminStates.waiting_for_delete_confirm)
    await callback.answer()

@dp.callback_query(AdminStates.waiting_for_delete_confirm, F.data == "admin_delete_confirm")
async def admin_finish_delete(callback: types.CallbackQuery, state: FSMContext, db: Database):
    """Performs restaurant deletion from the DB. (db: Database - DI)"""
    data = await state.get_data()
    rest_id = data.get('current_rest_id')
    
    if not rest_id:
        await callback.answer("Ошибка: ID ресторана потерян.", show_alert=True)
        await state.clear()
        await send_admin_panel(callback.message, db)
        return

    try:
        deleted_name = await db.delete_rest_by_id(rest_id)
        
        await callback.message.edit_text(
            f"🗑️ Ресторан **{deleted_name or rest_id}** успешно удален из системы.",
            parse_mode="Markdown"
        )
    except Exception as e:
        logging.error(f"DB Error (Delete rest): {e}")
        await callback.message.edit_text("❌ Произошла ошибка при удалении ресторана.")

    await state.clear()
    await send_admin_panel(callback.message, db)
    await callback.answer()

# --- OTHER ADMIN ACTIONS ---

@dp.callback_query(F.data == "admin_back_to_select")
async def admin_back_to_select(callback: types.CallbackQuery, state: FSMContext, db: Database):
    """Returns from the deletion confirmation menu to the restaurant management menu."""
    await state.set_state(None)
    await admin_select_rest(callback, state, db)

@dp.callback_query(F.data == "admin_set_qty")
async def admin_start_set_quantity(callback: types.CallbackQuery, state: FSMContext):
    data = await state.get_data()
    rest_id = data.get('current_rest_id')
    name = data.get('current_rest_name')
    
    if not rest_id or not name:
        await callback.answer("Ошибка FSM. Вернитесь в главное меню.", show_alert=True)
        db_instance = callback.bot.get_data()["db"] 
        await admin_panel(callback.message, state, db=db_instance)
        return
    
    await callback.message.edit_text(
        f"**Введите НОВОЕ общее количество наборов для ресторана {name}.**\n\n(Только целое число, например: **30**)",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="❌ Отмена", callback_data="admin_cancel_fsm")]
        ]),
        parse_mode="Markdown"
    )
    await state.set_state(AdminStates.waiting_for_new_quantity)
    await callback.answer()


@dp.message(AdminStates.waiting_for_new_quantity)
async def admin_finish_set_quantity(message: types.Message, state: FSMContext, db: Database):
    """Processes the entered number and updates the DB. (db: Database - DI)"""
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
        await message.answer("❌ Ошибка: Ресторан не выбран. Начните с /admin.", reply_markup=ReplyKeyboardRemove())
        await state.clear()
        return

    try:
        res = await db.set_boxes_quantity(rest_id, new_qty)
    except Exception as e:
        logging.error(f"DB Error (Set quantity): {e}")
        await message.answer("❌ Ошибка обновления. Не удалось связаться с базой данных.")
        res = None
    
    await state.clear()
    
    if res:
        name, boxes = res['name'], res['boxes']
        await message.answer(
            f"✅ **Успешно обновлено!**\n\n"
            f"Ресторан: **{name}**\n"
            f"Установлено: **{boxes}** наборов.",
            parse_mode="Markdown"
        )
    else:
        await message.answer("❌ Ошибка обновления. Ресторан не найден.")
    
    await send_admin_panel(message, db)


@dp.callback_query(F.data == "admin_back")
async def admin_back_to_menu(callback: types.CallbackQuery, state: FSMContext, db: Database):
    """Returns from the submenu to the main admin menu. (db: Database - DI)"""
    await state.clear()
    await callback.message.edit_reply_markup(reply_markup=None) 
    await send_admin_panel(callback.message, db)
    await callback.answer()


@dp.callback_query(F.data == "admin_cancel_fsm")
async def admin_cancel_fsm(callback: types.CallbackQuery, state: FSMContext, db: Database):
    """Cancels any FSM admin state. (db: Database - DI)"""
    await state.clear()
    await callback.message.edit_text("Операция отменена. Возврат в главное меню.")
    await send_admin_panel(callback.message, db, text="Операция отменена. Возврат в главное меню.")
    await callback.answer()


@dp.callback_query(F.data.startswith("admin_add_5_"))
async def handle_admin_add_5(callback: types.CallbackQuery, db: Database):
    """Quick addition of +5 sets. (db: Database - DI)"""
    rest_id = int(callback.data.split("_")[-1])
    
    try:
        res = await db.increment_boxes(rest_id, 5)
    except Exception as e:
        logging.error(f"DB Error (Add 5): {e}")
        await callback.answer("❌ Ошибка при добавлении наборов.", show_alert=True)
        res = None
        
    if res:
        name, new_boxes = res['name'], res['boxes']
        await callback.message.edit_text("Обновление данных...")
        await send_admin_panel(callback.message, db, text=f"✅ Наборы для {name} обновлены: **{new_boxes}** шт.")
    else:
        await callback.answer("Ошибка: Ресторан не найден.", show_alert=True)
    
    await callback.answer()

# --- 8. ERROR HANDLER ---

@dp.errors()
async def error_handler(exception, event):
    """General error handler for non-critical failures."""
    logging.error(f"An unhandled error occurred in handler {event.update.event_type.name}: {exception}")
    if event.update.callback_query:
        await event.update.callback_query.answer("Произошла внутренняя ошибка. Попробуйте снова.")
    elif event.update.message:
        await event.update.message.answer("Произошла внутренняя ошибка. Попробуйте начать заново с /start.")
    return True

# --- 9. HTTP SERVER FOR RENDER (Health Check) ---
async def handle_hc(request):
    """Health check for hosting (Render)."""
    return web.Response(text="Bot is running!")

async def start_http_server():
    """Starts a small HTTP server for health check."""
    app = web.Application()
    app.router.add_get("/", handle_hc)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", Config.PORT)
    await site.start()
    logging.info(f"Health check server started on port {Config.PORT}")

# --- 10. SYSTEM LAUNCH (Main Entry Point) ---
async def main():
    bot = Bot(token=Config.API_TOKEN)
    db_instance = Database() 

    # 1. Register Admin Middleware
    # It checks the user ID for /admin command and 'admin_' callbacks
    dp.message.middleware(AdminAccessMiddleware(Config.ADMIN_ID))
    dp.callback_query.middleware(AdminAccessMiddleware(Config.ADMIN_ID))

    # 2. Register Hooks for Graceful Shutdown
    dp.startup.register(lambda: on_startup(dp, db_instance))
    dp.shutdown.register(lambda: on_shutdown(dp, db_instance))

    # 3. Start the bot and web server concurrently
    await asyncio.gather(
        dp.start_polling(bot, db=db_instance),
        start_http_server()
    )
    
if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
    except Exception as e:
        logging.critical(f"Critical error in main(): {e}")

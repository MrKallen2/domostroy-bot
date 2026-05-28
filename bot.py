import asyncio
import logging
import os
import aiohttp
import re
import random
from email_sender import send_order_to_email
import config
from aiogram.client.default import DefaultBotProperties
from config import YOOKASSA_SECRET_KEY
from aiogram import Bot, Dispatcher, types
from aiogram.fsm.storage.redis import RedisStorage
from aiogram.fsm.storage.base import DefaultKeyBuilder
from aiogram.filters import Command
from aiogram.fsm.storage.base import StorageKey
from aiogram.types import Message, ReplyKeyboardMarkup, KeyboardButton, FSInputFile, InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.fsm.context import FSMContext
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from aiogram.types import ReplyKeyboardRemove
from aiogram.exceptions import TelegramNetworkError, TelegramRetryAfter
from apscheduler.triggers.date import DateTrigger
from datetime import datetime, timedelta
import pytz
from redis import Redis
from aiogram.client.session.aiohttp import AiohttpSession
from aiohttp import ClientTimeout
from utils import get_material_props
import json
import time
from yookassa import Configuration, Payment
from dotenv import load_dotenv
import uuid

# После инициализации бота
scheduler = AsyncIOScheduler(timezone='Asia/Novosibirsk')  # твой часовой пояс

# Глобальные переменные для времени бурояма
DRILL_TIMES = {}  # {district: {"small_20": 5, "small_30": 6, "small_35": 7, "big_35": 5, "big_45": 6}}
MANUAL_PRICE_PER_PILE = 2500  # Стоимость ручного монтажа за 1 сваю
DRILL_PRICE_PER_HOUR = 3500   # Стоимость бурояма за час

# Твой ID счетчика из настроек Метрики (цифры)
METRICA_ID = "108670488"

async def track_event(user_id, event_name):
    url = f"https://mc.yandex.ru/watch/{METRICA_ID}"
    params = {
        "wmode": 7,
        "page-url": f"https://t.me/svainsk_bot/{event_name}",
        "browser-info": f"uid:{user_id}:v:1",
        "charset": "utf-8"
    }
    async with aiohttp.ClientSession() as session:
        try:
            async with session.get(url, params=params, timeout=5) as resp:
                if resp.status == 200:
                    logging.info(f"Метрика: событие {event_name} отправлено")
        except Exception as e:
            logging.error(f"Ошибка Метрики: {e}")

# Используем данные из твоего .env
Configuration.account_id = os.getenv("YOOKASSA_SHOP_ID")
Configuration.secret_key = os.getenv("YOOKASSA_SECRET_KEY")

# Ключи для Redis
CALC_LOCK_KEY = "calc_lock"      # блокировка расчета
CALC_QUEUE_KEY = "calc_queue"    # очередь пользователей
CALC_CHAT_KEY = "calc_chat:"     # префикс для chat_id пользователя

# Хранилище ID последних сообщений (чтобы удалять)
last_question_messages = {}

from config import (
    GROUP_ID, THREAD_DOMOSTROY, THREAD_MONTAGNIKI,
    THREAD_POSTAVSHCHIKI, THREAD_DOSTAVKA,
    REDIS_HOST, REDIS_PORT, REDIS_DB,
    SPREADSHEET_URL, CALC_SPREADSHEET_URL,
    WAREHOUSE_ADDRESS
)
# BOT_TOKEN и YOOKASSA_SECRET_KEY уже загружены в config.py
from states import OrderStates
from sheets import sheets

global DISTRICTS
DISTRICTS = []

# Настройка логирования
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)

# Инициализация Redis для очереди (отдельная БД)
redis_queue = Redis(host=REDIS_HOST, port=REDIS_PORT, db=2, decode_responses=True)

# Инициализация Redis для хранения состояний
# Создаем key_builder с поддержкой destiny
key_builder = DefaultKeyBuilder(with_destiny=True)

redis_storage = RedisStorage.from_url(
    f"redis://{REDIS_HOST}:{REDIS_PORT}/{REDIS_DB}",
    key_builder=key_builder
)

# Инициализация бота и диспетчера
dp = Dispatcher(storage=redis_storage)

# ID чатов (для удобства)
CHATS = {
    "domostroy": {"chat_id": GROUP_ID, "thread_id": THREAD_DOMOSTROY},
    "montagniki": {"chat_id": GROUP_ID, "thread_id": THREAD_MONTAGNIKI},
    "postavshchiki": {"chat_id": GROUP_ID, "thread_id": THREAD_POSTAVSHCHIKI},
    "dostavka": {"chat_id": GROUP_ID, "thread_id": THREAD_DOSTAVKA},
}

# ПОЛНЫЙ СПИСОК РАЙОНОВ НОВОСИБИРСКОЙ ОБЛАСТИ
# Список районов будет загружен из таблицы
DISTRICTS = []  # временно пусто

async def create_payment(amount, description):
    idempotency_key = str(uuid.uuid4())
    payment = Payment.create({
        "amount": {
            "value": f"{amount}.00",
            "currency": "RUB"
        },
        "confirmation": {
            "type": "redirect",
            "return_url": "https://t.me/svainsk_bot" # Замени на юзернейм своего бота
        },
        "capture": True,
        "description": description
    }, idempotency_key)

    return payment.confirmation.confirmation_url


async def delayed_queue_check():
    """Отложенная проверка очереди"""
    await asyncio.sleep(2)
    await process_next_in_queue()

async def auto_delete_message(chat_id: int, message_id: int, delay: int = 4):
    """Автоматически удаляет сообщение через указанное количество секунд"""
    await asyncio.sleep(delay)
    try:
        await bot.delete_message(chat_id, message_id)
    except:
        pass  # Игнорируем ошибки, если сообщение уже удалено

# ========== ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ ДЛЯ ИНЛАЙН-КЛАВИАТУР ==========

def get_material_keyboard():
    """Клавиатура для выбора материала"""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="Брус", callback_data="material_Брус")],
            [InlineKeyboardButton(text="Доска", callback_data="material_Дерево")],
            [InlineKeyboardButton(text="СИП-панели", callback_data="material_Сип-панели")],
            [InlineKeyboardButton(text="Кирпич", callback_data="material_Кирпич")],
            [InlineKeyboardButton(text="Газобетон", callback_data="material_Газобетон")],
            [InlineKeyboardButton(text="Каркас", callback_data="material_Каркас")]
        ]
    )


def get_floors_keyboard():
    """Клавиатура для выбора этажности"""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="1", callback_data="floors_1"),
                InlineKeyboardButton(text="2", callback_data="floors_2"),
                InlineKeyboardButton(text="3", callback_data="floors_3")
            ]
        ]
    )


def get_district_keyboard(districts):
    """Клавиатура для выбора района (обычная клавиатура снизу)"""
    print(f"🔥 get_district_keyboard вызвана с {len(districts)} районами")

    if not districts:
        print("⚠️ ВНИМАНИЕ: districts пустой в get_district_keyboard!")
        return ReplyKeyboardMarkup(
            keyboard=[[KeyboardButton(text="⚠️ Районы не загружены")]],
            resize_keyboard=True
        )

    keyboard = []
    row = []
    for i, district in enumerate(districts):
        row.append(KeyboardButton(text=district))
        if len(row) == 2 or i == len(districts) - 1:
            keyboard.append(row)
            row = []

    # Кнопка "Назад"
    # keyboard.append([KeyboardButton(text="⬅️ Назад")])

    print(f"✅ Создана клавиатура с {len(districts)} районами, {len(keyboard)} строк")
    return ReplyKeyboardMarkup(keyboard=keyboard, resize_keyboard=True)



async def get_dates_keyboard_inline():
    """Клавиатура для выбора даты"""
    from datetime import datetime, timedelta

    dates = []
    for i in range(7):
        date = (datetime.now() + timedelta(days=i)).strftime("%d.%m.%Y")
        dates.append(date)

    keyboard = []
    row = []
    for i, date in enumerate(dates):
        row.append(InlineKeyboardButton(text=date, callback_data=f"date_{date}"))
        if len(row) == 2 or i == len(dates) - 1:
            keyboard.append(row)
            row = []
    # Кнопка "Другая дата" и "Назад"
    keyboard.append([
        InlineKeyboardButton(text="📅 Другая дата", callback_data="date_other"),
        InlineKeyboardButton(text="⬅️ Назад", callback_data="back_to_district")
    ])
    return InlineKeyboardMarkup(inline_keyboard=keyboard)


def get_time_keyboard_inline(slots):
    """Клавиатура для выбора времени"""
    keyboard = []
    row = []
    for i, slot in enumerate(slots):
        row.append(InlineKeyboardButton(text=slot, callback_data=f"time_{slot}"))
        if len(row) == 2 or i == len(slots) - 1:
            keyboard.append(row)
            row = []
    # Кнопка "Назад"
    keyboard.append([InlineKeyboardButton(text="⬅️ Назад к дате", callback_data="back_to_date")])
    return InlineKeyboardMarkup(inline_keyboard=keyboard)


def get_yes_no_keyboard(callback_prefix):
    """Универсальная клавиатура Да/Нет"""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="✅ Да", callback_data=f"{callback_prefix}_yes"),
                InlineKeyboardButton(text="❌ Нет", callback_data=f"{callback_prefix}_no")
            ]
        ]
    )


def get_equipment_keyboard():
    """Клавиатура для выбора техники"""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="✅ Да, заезд свободный", callback_data="equipment_big_yes")],
            [InlineKeyboardButton(text="❌ Нет, есть ограничения", callback_data="equipment_big_no")]
        ]
    )


def get_small_equipment_keyboard():
    """Клавиатура для выбора маленькой техники"""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="✅ Да, маленький заедет", callback_data="equipment_small_yes")],
            [InlineKeyboardButton(text="🔧 Только ручной инструмент", callback_data="equipment_manual")]
        ]
    )


class CalcQueue:
    """Очередь расчетов через Redis - полная версия"""

    @staticmethod
    async def try_acquire_lock(user_id: int) -> bool:
        """Пытается захватить блокировку для расчета"""
        lock_acquired = redis_queue.set(CALC_LOCK_KEY, user_id, ex=120, nx=True)
        return lock_acquired is True

    @staticmethod
    async def release_lock():
        """Освобождает блокировку"""
        redis_queue.delete(CALC_LOCK_KEY)

    @staticmethod
    async def get_lock_holder() -> int:
        """Кто сейчас держит блокировку"""
        holder = redis_queue.get(CALC_LOCK_KEY)
        return int(holder) if holder else 0

    @staticmethod
    async def add_to_queue(user_id: int, chat_id: int) -> int:
        """Добавляет пользователя в очередь, возвращает позицию"""
        # Сохраняем chat_id
        redis_queue.setex(f"{CALC_CHAT_KEY}{user_id}", 300, chat_id)

        # Добавляем в очередь
        redis_queue.rpush(CALC_QUEUE_KEY, user_id)
        position = redis_queue.llen(CALC_QUEUE_KEY)
        redis_queue.expire(CALC_QUEUE_KEY, 300)

        return position

    @staticmethod
    async def get_queue_position(user_id: int) -> int:
        """Получает позицию пользователя в очереди"""
        queue = redis_queue.lrange(CALC_QUEUE_KEY, 0, -1)
        try:
            return queue.index(str(user_id)) + 1
        except ValueError:
            return 0

    @staticmethod
    async def remove_from_queue(user_id: int):
        """Удаляет пользователя из очереди"""
        redis_queue.lrem(CALC_QUEUE_KEY, 0, user_id)
        redis_queue.delete(f"{CALC_CHAT_KEY}{user_id}")

    @staticmethod
    async def get_next_from_queue() -> tuple:
        """Получает следующего из очереди (user_id, chat_id)"""
        next_user = redis_queue.lpop(CALC_QUEUE_KEY)
        if next_user:
            next_user = int(next_user)
            chat_id = redis_queue.get(f"{CALC_CHAT_KEY}{next_user}")
            redis_queue.delete(f"{CALC_CHAT_KEY}{next_user}")
            return next_user, int(chat_id) if chat_id else None
        return 0, None

    @staticmethod
    async def get_queue_length() -> int:
        """Получает длину очереди"""
        return redis_queue.llen(CALC_QUEUE_KEY)

# Хранилище для отслеживания сообщений очереди
queue_messages = {}  # {user_id: message_id}


@dp.message(Command("test_email"))
async def test_email_command(message: Message, state: FSMContext):
    """Тестовая отправка письма (только для разработчика)"""
    # Проверяем, что это разработчик
    if message.from_user.id != 7626450915:  # твой ID
        await message.answer("❌ Доступ запрещён")
        return

    # Создаём тестовые данные
    test_order = {
        'order_number': 'TEST_' + str(random.randint(1000, 9999)),
        'length': 6,
        'width': 8,
        'material': 'Кирпич',
        'floors': 2,
        'pile_count': 16,
        'pile_type': 'Свая 108×3000',
        'prepayment': 25000,
        'fio': 'Тестов Тест Тестович',
        'phone': '+7 999 123-45-67',
        'address': 'г. Новосибирск, ул. Тестовая, д. 1',
        'district': 'Центральный',
        'selected_date': '15.05.2026',
        'selected_time': '10:00',
        'equipment': 'big',
        'electricity': True,
        'geology': False,
        'service': 'Сваи с монтажом',
        'delivery_type': 'delivery',
    }

    await message.answer("🔄 Отправляю тестовое письмо...")

    try:
        result = await send_order_to_email(test_order)
        if result:
            await message.answer("✅ Тестовое письмо отправлено! Проверьте почту.")
        else:
            await message.answer("❌ Ошибка отправки. Смотри логи.")
    except Exception as e:
        await message.answer(f"❌ Ошибка: {e}")

class MediaManager:
    """Кеширует file_id картинок, чтобы не загружать повторно"""

    def __init__(self):
        self.cache = {}  # Временный кеш в памяти

    async def get_file_id(self, url: str, bot: Bot, chat_id: int) -> str:
        # 1. Проверяем в памяти
        if url in self.cache:
            return self.cache[url]

        # 2. Проверяем в Google Sheets
        try:
            file_id = sheets.get_file_id(url)
            if file_id:
                print(f"✅ Найдено в Google Sheets: {url}")
                self.cache[url] = file_id
                return file_id
        except Exception as e:
            print(f"❌ Ошибка при получении из Google Sheets: {e}")

        # 3. Если нет file_id — загружаем по URL (но с защитой)
        print(f"🔄 Загружаем новое фото по URL: {url}")
        try:
            from aiogram.types import URLInputFile
            file = URLInputFile(url)

            msg = await asyncio.wait_for(
                bot.send_photo(
                    chat_id=chat_id,
                    photo=file,
                    disable_notification=True
                ),
                timeout=15.0
            )

            file_id = msg.photo[-1].file_id
            self.cache[url] = file_id

            # Сохраняем в Google Sheets
            try:
                sheets.save_file_id(url, file_id)
                print(f"✅ Сохранено в Google Sheets: {url}")
            except Exception as e:
                print(f"❌ Не удалось сохранить file_id: {e}")

            await msg.delete()
            return file_id

        except asyncio.TimeoutError:
            print(f"⏳ Таймаут загрузки нового фото по URL")
            raise
        except Exception as e:
            print(f"❌ Ошибка загрузки фото по URL: {e}")
            raise

media_manager = MediaManager()


async def update_queue_position(user_id: int, chat_id: int, message_id: int):
    """Фоновая задача для обновления позиции в очереди"""
    last_position = 0

    while True:
        await asyncio.sleep(3)  # Проверяем каждые 3 секунды

        position = await CalcQueue.get_queue_position(user_id)

        if position == 0:
            # Пользователь больше не в очереди
            break

        if position != last_position:
            last_position = position
            queue_length = await CalcQueue.get_queue_length()

            try:
                await bot.edit_message_text(
                    chat_id=chat_id,
                    message_id=message_id,
                    text=(
                        f"<b>⏳ Вы в очереди на расчет!</b>\n\n"
                        f"<b>Ваша позиция:</b> <code>{position}</code>\n"
                        f"<b>Всего в очереди:</b> <code>{queue_length}</code>\n\n"
                        f"<blockquote>🔧 Впереди еще {position - 1} расчетов.\n"
                        f"Примерное ожидание: ~{(position - 1) * 25} секунд.\n\n"
                        f"Когда подойдет ваша очередь, это сообщение изменится.</blockquote>"
                    ),
                    parse_mode="HTML"
                )
            except Exception as e:
                print(f"Ошибка обновления позиции: {e}")

    # Удаляем из словаря
    queue_messages.pop(user_id, None)


async def send_cached_photo(chat_id: int, url: str, caption: str = "", reply_markup=None, max_retries=2):
    """Отправляет фото максимально быстро через file_id"""
    for attempt in range(max_retries):
        try:
            # Пытаемся получить file_id (из кеша или Google Sheets)
            file_id = await media_manager.get_file_id(url, bot, chat_id)

            # Отправляем с таймаутом
            await asyncio.wait_for(
                bot.send_photo(
                    chat_id=chat_id,
                    photo=file_id,
                    caption=caption,
                    reply_markup=reply_markup,
                    parse_mode="HTML"
                ),
                timeout=12.0
            )
            print(f"✅ Фото отправлено через file_id для {chat_id}")
            return True

        except asyncio.TimeoutError:
            print(f"⏳ Таймаут при отправке фото (попытка {attempt+1})")
        except Exception as e:
            print(f"❌ Ошибка отправки фото (попытка {attempt+1}): {e}")

        if attempt < max_retries - 1:
            await asyncio.sleep(1.5)

    # Если file_id не сработал — последний fallback (только текст)
    try:
        await bot.send_message(
            chat_id=chat_id,
            text=caption,
            parse_mode="HTML",
            reply_markup=reply_markup
        )
        print(f"⚠️ Отправлен только текст (fallback) для {chat_id}")
    except Exception as e:
        print(f"❌ Критическая ошибка fallback: {e}")

    return False

async def send_temp_message(chat_id: int, text: str, parse_mode: str = "HTML"):
    """Отправляет временное сообщение и возвращает его для последующего удаления"""
    return await bot.send_message(chat_id, text, parse_mode=parse_mode)


async def update_question_message(chat_id: int, text: str, reply_markup, state: FSMContext):
    """Удаляет предыдущее сообщение с вопросом и временное подтверждение, затем отправляет новое"""

    data = await state.get_data()
    last_msg_id = data.get('last_question_msg_id')
    last_temp_id = data.get('last_temp_msg_id')

    print(f"DEBUG: last_msg_id={last_msg_id}, last_temp_id={last_temp_id}")  # ДОБАВЬ ЭТО

    # Удаляем предыдущий вопрос
    if last_msg_id:
        try:
            await bot.delete_message(chat_id, last_msg_id)
            print(f"DEBUG: Удалил сообщение {last_msg_id}")
        except Exception as e:
            print(f"DEBUG: Ошибка удаления {last_msg_id}: {e}")

    # ✅ Удаляем временное подтверждение
    if last_temp_id:
        try:
            await bot.delete_message(chat_id, last_temp_id)
            print(f"DEBUG: Удалил временное {last_temp_id}")
        except Exception as e:
            print(f"DEBUG: Ошибка удаления временного {last_temp_id}: {e}")
        await state.update_data(last_temp_msg_id=None)

    # Отправляем новый вопрос
    msg = await bot.send_message(
        chat_id=chat_id,
        text=text,
        reply_markup=reply_markup,
        parse_mode="HTML"
    )
    print(f"DEBUG: Отправил новое сообщение {msg.message_id}")

    await state.update_data(last_question_msg_id=msg.message_id)

# Функция для отправки сообщений в конкретный топик
async def send_to_topic(chat_name: str, text: str, keyboard=None):
    """Отправляет сообщение в указанный чат/топик"""
    chat = CHATS.get(chat_name)
    if not chat:
        return

    await bot.send_message(
        chat_id=chat["chat_id"],
        text=text,
        message_thread_id=chat["thread_id"],
        reply_markup=keyboard
    )



@dp.errors()
async def handle_network_errors(event: types.ErrorEvent):
    exception = event.exception
    print(f"🌐 Сетевая ошибка: {type(exception).__name__} — {exception}")
    if isinstance(exception, TelegramNetworkError):
        await asyncio.sleep(3)
        return True
    return False


last_start = {}


@dp.message(Command("start"))
async def start(message: Message, state: FSMContext):
    user_id = message.from_user.id
    now = time.time()

    if user_id in last_start and now - last_start[user_id] < 2.0:
        print(f"⚠️ Игнорируем повторный /start от {user_id}")
        return

    last_start[user_id] = now

    # --- МАЯЧОК: Запуск бота ---
    await track_event(user_id, "start_bot")

    print(f"[{datetime.now()}] Получен /start от {user_id}")
    if message.chat.type != "private":
        return

    print(f"🚀 Начинаем обработку /start для пользователя {user_id}")

    await state.clear()

    text = (
        "<b>✨ Привет! Я — Богдан, ваш персональный помощник от компании ДОМОСТРОЙ.</b>\n\n"
        "<b>Помогу подобрать идеальные винтовые сваи для вашего фундамента, рассчитать нагрузку и оформить монтаж — без участия менеджера и лишних звонков.</b>\n\n"
        "<blockquote><b>Как я работаю❓</b>\n"
        "— Вы вводите параметры, я по строгому алгоритму вычисляю несущую способность на одну сваю.\n"
        "— Учитываю регион и вес конструкции — ошибки исключены.\n"
        "— Оплата проходит в один клик прямо через меня.</blockquote>\n\n"
        "<b>👇 Чтобы продолжить, выберите интересующий вас вариант:</b>"
    )

    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="🔩 Сваи с монтажом", callback_data="service_mount")],
            [InlineKeyboardButton(text="📦 Сваи без монтажа", callback_data="service_no_mount")]
        ]
    )

    # Быстрая отправка через send_cached_photo (использует file_id)
    await send_cached_photo(
        chat_id=message.chat.id,
        url="https://i.ibb.co/tTpW2SwC/photo-2026-03-05-13-42-50.jpg",   # можешь поменять на свою
        caption=text,
        reply_markup=kb
    )

    await state.set_state(OrderStates.choosing_service)
    print(f"✅ /start полностью завершён для пользователя {user_id}")

def get_terms_keyboard(service_type: str):
    """Клавиатура с пользовательским соглашением"""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="📔 Ознакомиться", url="https://drive.google.com/file/d/1MHWBz63prb5bl9yXlyNUPL01MrNQh40N/view")],
            [InlineKeyboardButton(text="✅ Принимаю", callback_data=f"accept_terms_{service_type}")]
        ]
    )

async def restore_scheduled_jobs():
    """Восстанавливает запланированные задачи из таблицы при запуске"""
    try:
        # Получаем все активные заказы из графика
        # Тебе нужно будет добавить функцию в sheets.py
        active_orders = sheets.get_active_orders_with_dates()

        for order in active_orders:
            # Здесь та же логика планирования, что и выше
            # Восстанавливаем задачи для каждого заказа
            pass

        print(f"✅ Восстановлено задач: {len(active_orders)}")
    except Exception as e:
        print(f"❌ Ошибка восстановления задач: {e}")


async def notify_chats_about_order(order_data: dict, state: FSMContext):
    """Отправляет уведомление в ДОМОСТРОЙ о новом заказе"""

    # Определяем тип заказа для красивого отображения
    service_type = order_data.get('service', 'Сваи с монтажом')
    delivery_type = order_data.get('delivery_type', 'delivery')

    # Формируем строку типа заказа
    if service_type == "Сваи с монтажом":
        order_type_emoji = "🔨"
        order_type_text = "С МОНТАЖОМ"
        date_label = "📅 Дата монтажа"
    else:
        order_type_emoji = "📦"
        if delivery_type == "delivery":
            order_type_text = "БЕЗ МОНТАЖА (ДОСТАВКА)"
            date_label = "📅 Дата доставки"
        else:
            order_type_text = "БЕЗ МОНТАЖА (САМОВЫВОЗ)"
            date_label = "📅 Дата самовывоза"

    # Уведомление в ДОМОСТРОЙ (топик 2) - общая информация
    domostroy_msg = (
        f"🔔 **НОВЫЙ ЗАКАЗ #{order_data['order_number']}** {order_type_emoji}\n\n"
        f"📌 **Тип:** {order_type_text}\n"
        f"🏠 **Дом:** {order_data['length']}×{order_data['width']}м, {order_data['material']}, {order_data['floors']} эт.\n"
        f"🔩 **Сваи:** {order_data['pile_count']} шт ({order_data['pile_type']})\n"
        f"💸 **Предоплата:** {order_data['prepayment']} ₽\n"
        f"👤 **Клиент:** {order_data['fio']}\n"
        f"📞 **Телефон:** {order_data['phone']}\n"
    )

    if order_data.get('address'):
        domostroy_msg += f"📍 **Адрес:** {order_data['address']}\n"
    if order_data.get('district'):
        domostroy_msg += f"📍 **Район:** {order_data['district']}\n"
    if order_data.get('selected_date'):
        domostroy_msg += f"{date_label}: {order_data['selected_date']}\n"
    if order_data.get('selected_time'):
        domostroy_msg += f"⏰ **Время:** {order_data['selected_time']}\n"

    await send_to_topic("domostroy", domostroy_msg)

    print(f"✅ Уведомление отправлено в ДОМОСТРОЙ по заказу #{order_data['order_number']}")

    # 2. Уведомление монтажникам (топик 3) - заявка с ДВУМЯ кнопками
    # montagniki_msg = (
    #     f"📦 **НОВАЯ ЗАЯВКА #{order_data['order_number']}**\n\n"
    #     f"🏠 Дом: {order_data['length']}×{order_data['width']}м, {order_data['material']}, {order_data['floors']} эт.\n"
    #     f"🔩 Сваи: {order_data['pile_count']} шт ({order_data['pile_type']})\n"
    #     f"📍 Адрес: {order_data['address']}\n"
    #     f"📅 Дата: {order_data['selected_date']}\n"
    #     f"🚜 Техника: {order_data['equipment_name']}\n"
    #     f"⚡ Электричество: {'нет' if not order_data.get('electricity', True) else 'есть'}\n"
    #     f"📋 Геология: {'есть' if order_data.get('geology') else 'нет'}\n\n"
    #     f"👤 Клиент: {order_data['fio']}\n"
    #     f"📞 Телефон: {order_data['phone']}"
    # )
    #
    # # ДВЕ кнопки: ВЗЯТЬ ЗАЯВКУ и МОНТАЖ ЗАВЕРШЁН
    # kb_montagniki = InlineKeyboardMarkup(
    #     inline_keyboard=[
    #         [InlineKeyboardButton(text="✅ ВЗЯТЬ ЗАЯВКУ", callback_data=f"take_order_{order_data['order_number']}")],
    #         [InlineKeyboardButton(text="🏁 МОНТАЖ ЗАВЕРШЁН", callback_data=f"finish_mount_{order_data['order_number']}")]
    #     ]
    # )
    #
    # await send_to_topic("montagniki", montagniki_msg, kb_montagniki)

    # 3. Уведомление поставщикам (топик 4)
    # postavshchiki_msg = (
    #     f"🏭 **ЗАКАЗ НА ПРОИЗВОДСТВО #{order_data['order_number']}**\n\n"
    #     f"🔩 Сваи: {order_data['pile_count']} шт ({order_data['pile_type']})\n"
    #     f"📌 Оголовки: {order_data['pile_count']} шт\n"
    #     f"📅 Дата готовности: {order_data['ready_date']}\n"
    # )
    #
    # # Кнопки для поставщиков
    # kb_postavshchiki = InlineKeyboardMarkup(
    #     inline_keyboard=[
    #         [
    #             InlineKeyboardButton(text="✅ ЕСТЬ В НАЛИЧИИ", callback_data=f"in_stock_{order_data['order_number']}"),
    #             InlineKeyboardButton(text="🏭 ИЗГОТОВИМ", callback_data=f"manufacture_{order_data['order_number']}")
    #         ]
    #     ]
    # )
    #
    # await send_to_topic("postavshchiki", postavshchiki_msg, kb_postavshchiki)

    # 4. Уведомление доставке (топик 5) - пока не отправляем, ждем подтверждения поставщиков
    # await state.update_data(need_delivery_notification=True)

# ============================================
# ОТЛОЖЕННЫЕ УВЕДОМЛЕНИЯ (НОВАЯ ВЕРСИЯ ПО ТВОИМ СКРИНАМ)
# ============================================

# async def notify_day_before(order_data: dict):
#     """За день до монтажа: отправляет клиенту вопрос 'Всё ли в силе?'"""
#     try:
#         order_number = order_data['order_number']
#         client_chat_id = order_data['client_chat_id']
#         time = order_data.get('selected_time', 'время не указано')
#         date = order_data.get('selected_date', 'дата не указана')
#
#         # Инлайн-кнопки
#         kb = InlineKeyboardMarkup(
#             inline_keyboard=[
#                 [
#                     InlineKeyboardButton(text="✅ ВСЁ В СИЛЕ", callback_data=f"confirm_{order_number}"),
#                     InlineKeyboardButton(text="🔄 ПЕРЕНЕСТИ", callback_data=f"reschedule_{order_number}")
#                 ]
#             ]
#         )
#
#         text = (
#             f"🔔 **НАПОМИНАНИЕ**\n\n"
#             f"Завтра в {time} у вас назначен монтаж. Заказ №{order_number}.\n\n"
#             f"Всё ли в силе?"
#         )
#
#         await bot.send_message(
#             chat_id=client_chat_id,
#             text=text,
#             parse_mode="Markdown",
#             reply_markup=kb
#         )
#
#         print(f"✅ Уведомление за день отправлено клиенту #{order_number}")
#
#     except Exception as e:
#         print(f"❌ Ошибка уведомления за день #{order_number}: {e}")
#

# @dp.callback_query(lambda c: c.data and c.data.startswith('confirm_'))
# async def process_confirm_order(callback: types.CallbackQuery):
#     """Клиент подтвердил, что всё в силе"""
#     order_number = callback.data.split('_')[1]
#
#     await callback.message.edit_text(
#         callback.message.text + "\n\n✅ Отлично!.",
#         parse_mode="Markdown"
#     )
#
#     # Уведомление монтажникам
#     await send_to_topic(
#         "montagniki",
#         f"👷 Клиент подтвердил заявку #{order_number}"
#     )
#
#     await callback.answer()
#
def get_diameter_keyboard():
    """Клавиатура для выбора диаметра"""
    diameters = [57, 76, 89, 108, 133]
    keyboard = []
    row = []
    for i, d in enumerate(diameters):
        row.append(InlineKeyboardButton(text=f"{d} мм", callback_data=f"diameter_{d}"))
        if len(row) == 2 or i == len(diameters) - 1:
            keyboard.append(row)
            row = []
    return InlineKeyboardMarkup(inline_keyboard=keyboard)


@dp.callback_query(lambda c: c.data == "back_to_recommendations")
async def back_to_recommendations_from_district(callback: types.CallbackQuery, state: FSMContext):
    """Возврат к выбору свай из выбора района"""
    await callback.answer()

    # --- МАЯЧОК: Возврат к расчету ---
    await track_event(callback.from_user.id, "back_to_calc")

    # Удаляем сообщение с районами
    await callback.message.delete()

    # Показываем снова выбор свай
    data = await state.get_data()
    piles_info = data.get('piles_info', {})

    # Показываем рекомендации снова
    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="✅ Взять рекомендованные", callback_data="action_take")],
            [InlineKeyboardButton(text="✏️ Выбрать самому", callback_data="action_choose")]
        ]
    )

    await bot.send_message(
        chat_id=callback.message.chat.id,
        text=(
            f"<b>🔍 Вернулись к выбору свай</b>\n\n"
            f"Ранее я рекомендовал:\n"
            f"<b>Свая {piles_info.get('diameter')}/3000/{piles_info.get('blade_width')} — {piles_info.get('count')} шт</b>\n\n"
            f"<b>👇 Выберите действие:</b>"
        ),
        parse_mode="HTML",
        reply_markup=kb
    )
    await state.set_state(OrderStates.showing_recommendations)


@dp.callback_query(lambda c: c.data and c.data.startswith('reschedule_'))
async def process_reschedule_order(callback: types.CallbackQuery, state: FSMContext):
    """Клиент хочет перенести дату"""
    order_number = callback.data.split('_')[1]

    await callback.message.answer(
        "📅 Выберите новую дату монтажа:",
        reply_markup=await get_dates_keyboard()
    )

    # Сохраняем номер заказа в состоянии
    await state.update_data(reschedule_order=order_number)
    await state.set_state(OrderStates.reschedule_date)

    await callback.answer()


@dp.message(OrderStates.reschedule_date)
async def process_reschedule_date(message: Message, state: FSMContext):
    """Обработка новой даты при переносе"""
    new_date = message.text.strip()
    data = await state.get_data()
    order_number = data.get('reschedule_order')

    # Обновляем дату в таблице (нужна функция)
    sheets.update_order_date(order_number, new_date)

    # Уведомление монтажникам
    await send_to_topic(
        "montagniki",
        f"🔄 Клиент перенёс заявку #{order_number}, новая дата: {new_date}"
    )

    await message.answer(
        f"✅ Дата изменена на {new_date}. Ожидайте подтверждения."
    )
    await state.clear()


async def notify_morning_check(order_data: dict):
    """Утром в день монтажа: проверка связи с бригадой"""
    try:
        order_number = order_data['order_number']
        client_chat_id = order_data['client_chat_id']

        kb = InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(text="✅ ДА", callback_data=f"morning_yes_{order_number}"),
                    InlineKeyboardButton(text="❌ НЕТ", callback_data=f"morning_no_{order_number}")
                ]
            ]
        )

        text = (
            f"🌅 **ДОБРОЕ УТРО!**\n\n"
            f"С вами уже связалась монтажная бригада?"
        )

        await bot.send_message(
            chat_id=client_chat_id,
            text=text,
            parse_mode="Markdown",
            reply_markup=kb
        )

        print(f"✅ Утренняя проверка отправлена клиенту #{order_number}")

    except Exception as e:
        print(f"❌ Ошибка утренней проверки #{order_number}: {e}")


@dp.callback_query(lambda c: c.data and c.data.startswith('morning_yes_'))
async def process_morning_yes(callback: types.CallbackQuery):
    """Клиент ответил ДА - бригада связалась"""
    order_number = callback.data.split('_')[2]

    await callback.message.edit_text(
        callback.message.text + "\n\n✅ Отлично! После завершения монтажа вы сможете оплатить остаток.",
        parse_mode="Markdown"
    )
    await callback.answer()


@dp.callback_query(lambda c: c.data and c.data.startswith('morning_no_'))
async def process_morning_no(callback: types.CallbackQuery):
    """Клиент ответил НЕТ - бригада не связалась"""
    order_number = callback.data.split('_')[2]

    await callback.message.edit_text(
        callback.message.text + "\n\n⚠️ Мы оповестим бригаду о необходимости связаться с вами.",
        parse_mode="Markdown"
    )

    # Срочное уведомление монтажникам с кнопкой
    kb_urgent = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="✅ СВЯЗАЛСЯ С КЛИЕНТОМ", callback_data=f"contacted_{order_number}")]
        ]
    )

    await send_to_topic(
        "montagniki",
        f"🚨 **СРОЧНО!** Клиент по заказу #{order_number} не дождался звонка бригады.\n\n"
        f"Нажмите кнопку после того, как свяжетесь с клиентом.",
        kb_urgent
    )

    # Уведомление закреплённому менеджеру
    await send_to_topic(
        "domostroy",
        f"🚨 Проблема с заказом #{order_number}: клиент не дождался звонка бригады."
    )



    await callback.answer()


@dp.callback_query(lambda c: c.data and c.data.startswith('contacted_'))
async def process_contacted(callback: types.CallbackQuery):
    """Бригада сообщила, что связалась с клиентом"""
    order_number = callback.data.split('_')[1]

    await callback.message.edit_text(
        callback.message.text + f"\n\n✅ Бригада связалась с клиентом",
        parse_mode="Markdown"
    )

    # Уведомление в ДОМОСТРОЙ
    await send_to_topic(
        "domostroy",
        f"📞 Бригада связалась с клиентом по заказу #{order_number}"
    )

    # Отправляем уведомление клиенту
    try:
        # Получаем chat_id клиента (нужно где-то хранить)
        # Пока заглушка, позже нужно брать из таблицы
        client_chat_id = 123456789  # TODO: получать из таблицы по order_number

        await bot.send_message(
            chat_id=client_chat_id,
            text="✅ **ХОРОШИЕ НОВОСТИ!**\n\n"
                 "Монтажная бригада связалась с нами и уже скоро вам позвонит.\n"
                 "Приносим извинения за задержку.",
            parse_mode="Markdown"
        )
        print(f"✅ Уведомление клиенту о звонке отправлено #{order_number}")

    except Exception as e:
        print(f"❌ Ошибка уведомления клиента #{order_number}: {e}")

    await callback.answer()

@dp.callback_query(lambda c: c.data and c.data.startswith('tnps_'))
async def process_tnps(callback: types.CallbackQuery):
    data_parts = callback.data.split('_')
    score = data_parts[1]
    order_number = data_parts[2]

    # Сохраняем оценку в таблицу
    sheets.save_tnps_score(order_number, score)

    await callback.message.edit_text(
        f"Спасибо за оценку {score}. Вы нам помогли! ❤️",
        parse_mode="HTML"
    )
    await callback.answer()


async def get_modify_estimate_keyboard(state: FSMContext) -> InlineKeyboardMarkup:
    """Создаёт умную inline-клавиатуру — только с теми пунктами, которые можно убрать"""
    data = await state.get_data()
    modified = data.get('modified_estimate', {})

    service_type = data.get('service')
    delivery_type = data.get('delivery_type')
    has_generator = not data.get('electricity', True) and data.get('generator_days', 0) > 0
    has_montage = service_type == "Сваи с монтажом" and delivery_type == "delivery"
    has_delivery = delivery_type == "delivery"

    inline_kb = []

    if not modified.get('remove_heads'):
        inline_kb.append([InlineKeyboardButton(text="🚫 Убрать оголовки", callback_data="mod_remove_heads")])

    if has_montage and not modified.get('remove_montage'):
        inline_kb.append([InlineKeyboardButton(text="🚫 Убрать монтаж", callback_data="mod_remove_montage")])

    if has_delivery and not modified.get('remove_delivery'):
        inline_kb.append([InlineKeyboardButton(text="🚫 Убрать доставку", callback_data="mod_remove_delivery")])

    if has_generator and not modified.get('remove_generator'):
        inline_kb.append([InlineKeyboardButton(text="🚫 Убрать генератор", callback_data="mod_remove_generator")])

    inline_kb.append([InlineKeyboardButton(text="🔄 Составить заново", callback_data="mod_restart")])
    inline_kb.append([InlineKeyboardButton(text="✅ Завершить изменения", callback_data="mod_finish")])

    return InlineKeyboardMarkup(inline_keyboard=inline_kb)


@dp.callback_query(lambda c: c.data.startswith('mod_'))
async def process_modify_callback(callback: types.CallbackQuery, state: FSMContext):
    """Обрабатывает нажатия на inline-кнопки изменения сметы"""
    action = callback.data
    data = await state.get_data()

    if 'modified_estimate' not in data:
        await state.update_data(modified_estimate={
            'remove_heads': False,
            'remove_montage': False,
            'remove_delivery': False,
            'remove_generator': False
        })

    modified = (await state.get_data())['modified_estimate']

    response_text = ""

    if action == "mod_remove_heads":
        modified['remove_heads'] = True
        response_text = "✅ Оголовки убраны"
    elif action == "mod_remove_montage":
        modified['remove_montage'] = True
        response_text = "✅ Монтаж убран"
    elif action == "mod_remove_delivery":
        modified['remove_delivery'] = True
        response_text = "✅ Доставка убрана"
    elif action == "mod_remove_generator":
        modified['remove_generator'] = True
        response_text = "✅ Генератор убран"
    elif action == "mod_restart":
        await state.clear()
        await callback.message.edit_text("🔄 Начинаем расчёт заново...")
        await start(callback.message, state)
        await callback.answer()
        return
    elif action == "mod_finish":
        await callback.message.edit_text("✅ Изменения сохранены")
        await show_modified_estimate(callback.message, state)
        await callback.answer()
        return

    # Сохраняем изменения
    await state.update_data(modified_estimate=modified)

    # Обновляем текущее сообщение с новой клавиатурой
    await callback.message.edit_text(
        f"{response_text}\n\n🔧 Что ещё убрать из сметы?",
        reply_markup=await get_modify_estimate_keyboard(state)
    )
    await callback.answer()

@dp.message(OrderStates.changing_estimate)
async def process_estimate_changes(message: Message, state: FSMContext):
    # Если вдруг /start во время изменений
    if message.text == "/start":
        await state.clear()
        await start(message, state)
        return

    await message.answer(
        "🔧 Выберите, что убрать из сметы:",
        reply_markup=await get_modify_estimate_keyboard(state)
    )


@dp.callback_query(lambda c: c.data and c.data.startswith('brigade_contact_no_'))
async def process_brigade_contact_no(callback: types.CallbackQuery):
    order_number = callback.data.split('_')[3]

    await callback.message.edit_text(
        callback.message.text + "\n\n⚠️ Мы оповестим бригаду о необходимости связаться с вами.",
        parse_mode="Markdown"
    )

    # Отправляем срочное уведомление монтажникам
    await send_to_topic(
        "montagniki",
        f"🚨 **СРОЧНО!** Клиент по заказу #{order_number} сообщил, что бригада не связалась с ним утром в день монтажа."
    )

    # Отправляем менеджеру
    await send_to_topic(
        "domostroy",
        f"🚨 Проблема с заказом #{order_number}: клиент не дождался звонка бригады."
    )

    await callback.answer()


async def show_order_summary(message: Message, state: FSMContext):
    """Показывает сводку по заказу"""
    data = await state.get_data()
    piles_info = data.get('piles_info', {})

    # Формируем список строк БЕЗ Markdown-разметки (чтобы избежать ошибок)
    summary = [
        "📋 ПРЕДВАРИТЕЛЬНЫЙ ЗАКАЗ",
        "=" * 30,
        f"🏠 Дом: {data.get('length')}×{data.get('width')}м",
        f"🧱 Материал: {data.get('material')}",
        f"📏 Этажей: {data.get('floors')}",
        f"🔩 Сваи: {piles_info.get('count', '?')} шт ({piles_info.get('type', '?')})",
    ]

    # Тип получения
    if data.get('delivery_type') == "samovyvoz":
        summary.append("🚗 Самовывоз")
        # Адрес самовывоза
        pickup_address = "г. Новосибирск, СПК Сибирский Авиатор 15А"
        summary.append(f"📍 Адрес самовывоза: {pickup_address}")
    else:
        summary.append("🚚 Доставка с монтажом")
        if data.get('district'):
            summary.append(f"📍 Район: {data.get('district')}")
        if data.get('selected_date'):
            summary.append(f"📅 Дата: {data.get('selected_date')}")

    # Техника (если есть)
    equipment = data.get('equipment')
    if equipment == "big":
        summary.append("🚜 Техника: большой буроям")
    elif equipment == "small":
        summary.append("🚜 Техника: маленький буроям")
    elif equipment == "manual":
        summary.append("👷 Техника: ручной монтаж")

    # Электричество (если есть генератор)
    if not data.get('electricity', True):
        days = data.get('generator_days', 0)
        if days > 0:
            summary.append(f"⚡ Генератор: {days} сут")

    # Геология
    if data.get('geology'):
        summary.append("📋 Геология: есть" + (" (гарантия)" if data.get('guarantee') else ""))
    else:
        summary.append("📋 Геология: нет")

    # Контактные данные
    if data.get('fio'):
        summary.append(f"👤 Клиент: {data.get('fio')}")
    if data.get('phone'):
        summary.append(f"📞 Телефон: {data.get('phone')}")

    summary.append("=" * 30)
    summary.append("📌 СЛЕДУЮЩИЙ ШАГ: ФОРМИРОВАНИЕ СМЕТЫ")

    # Кнопка для перехода к смете
    kb = ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text="✅ Перейти к смете")]],
        resize_keyboard=True
    )

    # Отправляем БЕЗ parse_mode (чтобы избежать ошибок с Markdown)
    await message.answer(
        "\n".join(summary),
        reply_markup=kb
    )


def get_pile_weight(diameter, length):
    """Возвращает вес одной сваи в кг"""
    # Данные из таблиц [citation:1][citation:4]
    weight_table = {
        57: {2500: 14, 3000: 16, 3500: 18, 4000: 20, 4500: 22, 5000: 24, 5500: 26, 6000: 28},
        76: {2500: 17, 3000: 19, 3500: 22, 4000: 25, 4500: 27, 5000: 30, 5500: 33, 6000: 36},
        89: {2500: 20, 3000: 23, 3500: 26, 4000: 29, 4500: 32, 5000: 36, 5500: 39, 6000: 42},
        108: {2500: 30, 3000: 35, 3500: 40, 4000: 45, 4500: 50, 5000: 55, 5500: 60, 6000: 65},
        133: {2500: 38, 3000: 44, 3500: 51, 4000: 57, 4500: 63, 5000: 70, 5500: 76, 6000: 84},
    }

    # Округляем длину до ближайшего значения из таблицы
    available_lengths = sorted(weight_table.get(diameter, {}).keys())
    if not available_lengths:
        return 30  # запасной вариант

    closest_length = min(available_lengths, key=lambda x: abs(x - length))
    return weight_table[diameter].get(closest_length, 30)


def calculate_piles(material, floors, length, width):
    """
    Расчет винтовых свай по строительным нормам
    """
    # Коэффициенты
    material_load = {
        "Дерево": 0.8,
        "Каркас": 0.9,
        "Газобетон": 1.3,
        "Кирпич": 1.8
    }

    floor_factor = {
        1: 1.0,
        2: 1.8,
        3: 2.5
    }

    # Площадь дома
    area = length * width

    # Базовая нагрузка
    base_load = material_load.get(material, 1.0) * floor_factor.get(floors, 1.0)
    total_load = area * base_load * 1.3  # тонны (+30% снеговая)

    print(f"📊 calculate_piles: площадь={area}м², нагрузка={total_load:.2f}т")

    # Шаг (как на сайте)
    max_side = max(length, width)
    if max_side < 8:
        step = 2.0
    elif max_side <= 10:
        step = 2.5
    else:
        step = 3.0

    # Расчет количества свай (как на сайте)
    edge_offset = 0.5
    effective_length = length - (edge_offset * 2)
    effective_width = width - (edge_offset * 2)

    import math
    gaps_col = max(1, math.ceil(effective_length / step))
    gaps_row = max(1, math.ceil(effective_width / step))

    cols = gaps_col + 1
    rows = gaps_row + 1
    count_by_grid = cols * rows

    # Выбор диаметра
    pile_capacity = {57: 1.5, 76: 2.21, 89: 3.12, 108: 4.25, 133: 5.6}
    selected_diameter = 108
    required_capacity = total_load / count_by_grid

    for diam in [57, 76, 89, 108, 133]:
        if pile_capacity[diam] >= required_capacity:
            selected_diameter = diam
            break

    if selected_diameter < 89:
        selected_diameter = 89

    # Проверка по нагрузке
    min_needed = math.ceil(total_load / pile_capacity[selected_diameter])
    final_count = max(count_by_grid, min_needed)

    print(f"📏 Максимальная сторона: {max_side}м → шаг: {step}м")
    print(f"📊 Свай по длине: {cols}, по ширине: {rows}")
    print(f"📌 Итого: {final_count} свай")

    # Таблица ширины лопасти
    blade_width_table = {
        57: 200,
        76: 200,
        89: 250,
        108: 300,
        133: 350
    }

    return {
        'type': f"Свая {selected_diameter}×3000",
        'diameter': selected_diameter,
        'length': 3000,
        'count': final_count,
        'step': step,
        'blade_width': blade_width_table.get(selected_diameter, 250),
        'cols': cols,
        'rows': rows
    }


@dp.message(Command("clear_cache"))
async def clear_cache(message: Message):
    """Очищает кэш медиа"""
    if message.chat.type != "private":
        return

    try:
        # Очищаем кэш в памяти
        media_manager.cache.clear()

        # Очищаем кэш в Google Sheets
        media_sheet = sheets.spreadsheet.worksheet("Медиа")
        # Удаляем все строки кроме заголовка
        rows = media_sheet.get_all_values()
        if len(rows) > 1:
            media_sheet.delete_rows(2, len(rows))

        await message.answer("✅ Кэш медиа очищен! Теперь фото загрузится заново.")
    except Exception as e:
        await message.answer(f"❌ Ошибка: {e}")





# Обработчик выбора услуги
# @dp.message(OrderStates.choosing_service)
# async def process_service_choice(message: Message, state: FSMContext):
#     choice = message.text
#     await state.update_data(service=choice)
#
#     # Сохраняем chat_id
#     await state.update_data(chat_id=message.chat.id)
#
#     # Убираем клавиатуру и показываем следующий шаг
#     await update_question_message(
#         chat_id=message.chat.id,
#         text=(
#             "<b>✅ Отлично, вы выбрали «Винтовые сваи с монтажом» — надёжное решение для вашего фундамента.</b>\n\n"
#             "<blockquote><b>🛠️ О наших бригадах</b>\n"
#             "Наши монтажные бригады оснащены техникой на любой случай: от мощных буроямов до компактных ручных сваекрутов. Каждый специалист — профессионал своего дела, поэтому работы пройдут безупречно.</blockquote>\n\n"
#             "<b>📐 Чтобы подобрать сваи максимально точно, укажите габариты постройки в формате <code>5×5</code> (длина × ширина в метрах).</b>\n\n"
#             "<blockquote><b>📊 Я рассчитаю:</b>\n"
#             "— точное количество свай с шагом 2 метра;\n"
#             "— подберу диаметр винтовых свай исходя из нагрузки и территориальной принадлежности</blockquote>\n\n"
#             "<b>➡️ Введите размеры, и мы продолжим.</b>"
#         ),
#         reply_markup=ReplyKeyboardMarkup(keyboard=[[]], resize_keyboard=True),
#         state=state
#     )
#     await state.set_state(OrderStates.entering_dimensions)


# Обработчик ввода габаритов
@dp.message(OrderStates.entering_dimensions)
async def process_dimensions(message: Message, state: FSMContext):
    text = message.text
    numbers = re.findall(r"\d+\.?\d*", text)

    if len(numbers) >= 2:
        length = float(numbers[0])
        width = float(numbers[1])
        # 🔥 СОРТИРУЕМ: длина всегда больше или равна ширине
        length, width = max(length, width), min(length, width)
        await state.update_data(length=length, width=width)

        # Обновляем сообщение с инлайн-кнопками для материала
        await update_question_message(
            chat_id=message.chat.id,
            text=(
                "<b>✨ Отлично, движемся дальше!</b>\n\n"
                "<b>Теперь выберите материал строения.</b>\n"
                "<blockquote><b>Это обязательный шаг — именно от материала зависит точная нагрузка на фундамент.</b></blockquote>\n\n"
                "<b>👇 Пожалуйста, выберите материал дома:</b>"
            ),
            reply_markup=get_material_keyboard(),
            state=state
        )
        # ✅ ВАЖНО: меняем состояние
        await state.set_state(OrderStates.choosing_material)
    else:
        # Ошибка — тоже обновляем то же сообщение
        await update_question_message(
            chat_id=message.chat.id,
            text="❌ Не могу распознать размеры. Введите в формате: длина × ширина\nНапример: 6×8",
            reply_markup=ReplyKeyboardMarkup(keyboard=[[]], resize_keyboard=True),
            state=state
        )


# Обработчик выбора материала
@dp.message(OrderStates.choosing_material)
async def process_material(message: Message, state: FSMContext):
    material = message.text

    if material in ["Дерево", "Кирпич", "Газобетон", "Каркас"]:
        await state.update_data(material=material)

        # ✅ СОЗДАЁМ ИНЛАЙН-КНОПКИ для этажей
        kb = InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(text="1", callback_data="floors_1"),
                    InlineKeyboardButton(text="2", callback_data="floors_2"),
                    InlineKeyboardButton(text="3", callback_data="floors_3")
                ]
            ]
        )

        # Отправляем сообщение с инлайн-кнопками
        await update_question_message(
            chat_id=message.chat.id,
            text="<b>👇 Пожалуйста, выберите этажность дома:</b>",
            reply_markup=kb,
            state=state
        )
        # НЕ ставим state - выбор этажа будет через callback
    else:
        # Ошибка — показываем клавиатуру снова
        kb = ReplyKeyboardMarkup(
            keyboard=[
                [KeyboardButton(text="Дерево")],
                [KeyboardButton(text="Кирпич")],
                [KeyboardButton(text="Газобетон")],
                [KeyboardButton(text="Каркас")]
            ],
            resize_keyboard=True
        )
        await update_question_message(
            chat_id=message.chat.id,
            text="❌ Выберите материал из списка: Дерево, Кирпич, Газобетон, Каркас",
            reply_markup=kb,
            state=state
        )


# КАЛБЕК МАТЕРИАЛ
@dp.callback_query(lambda c: c.data and c.data.startswith('material_'))
async def process_material_callback(callback: types.CallbackQuery, state: FSMContext):
    await callback.answer()

    material = callback.data.replace('material_', '')
    await state.update_data(material=material)

    # ✅ Удаляем сообщение с кнопками материала
    await callback.message.delete()

    # ✅ Отправляем НОВОЕ сообщение через bot.send_message (НЕ через callback.message)
    await bot.send_message(
        chat_id=callback.message.chat.id,
        text="<b>⬇️ Выберите этажность:</b>",
        parse_mode="HTML",
        reply_markup=get_floors_keyboard()
    )


# КАЛБЕК ВЗЯТЬ РЕКОМЕНДОВАННЫЕ
@dp.callback_query(lambda c: c.data.startswith('action_'))
async def process_action_callback(callback: types.CallbackQuery, state: FSMContext):
    await callback.answer()

    user_id = callback.from_user.id

    # ←←← ПРИНУДИТЕЛЬНОЕ ПОВТОРНОЕ ВОССТАНОВЛЕНИЕ ПЕРЕД ДЕЙСТВИЕМ
    full_data_key = f"calc_user_data_full:{user_id}"
    user_data_str = redis_queue.get(full_data_key)
    if user_data_str:
        try:
            import ast
            full_data = ast.literal_eval(user_data_str)
            await state.set_data(full_data)
            print(f"✅ Принудительно восстановлено состояние перед action_take для {user_id}")
        except Exception as e:
            print(f"❌ Ошибка принудительного восстановления: {e}")

    # Дополнительно восстанавливаем piles_info из отдельного ключа
    piles_key = f"calc_piles_info:{user_id}"
    piles_str = redis_queue.get(piles_key)
    if piles_str:
        try:
            import ast
            piles_info = ast.literal_eval(piles_str)
            current = await state.get_data()
            current['piles_info'] = piles_info
            await state.set_data(current)
            print(f"✅ Принудительно восстановлен piles_info перед action_take для {user_id}")
        except Exception as e:
            print(f"❌ Ошибка восстановления piles_info: {e}")

    data = await state.get_data()
    service_type = data.get('service')
    piles_info = data.get('piles_info')

    print(f"DEBUG action_take: user={user_id} | service='{service_type}' | piles_info={piles_info}")

    if not service_type:
        await callback.message.answer("❌ Тип услуги потерян. Начните заново — /start")
        await state.clear()
        return

    if callback.data == "action_take":
        if not piles_info or piles_info.get('count', 0) == 0:
            print(f"❌ КРИТИЧНО: piles_info всё ещё пустой у {user_id} при action_take")
            await callback.message.answer("❌ Данные о сваях потеряны. Начните заново — /start")
            await state.clear()
            return

        await state.update_data(selected_piles=piles_info, pile_source='recommended')

        if service_type == "Сваи без монтажа":
            kb = InlineKeyboardMarkup(
                inline_keyboard=[
                    [InlineKeyboardButton(text="🚚 Доставка", callback_data="delivery_type_delivery")],
                    [InlineKeyboardButton(text="🚗 Самовывоз", callback_data="delivery_type_pickup")]
                ]
            )

            await send_cached_photo(
                chat_id=callback.message.chat.id,
                url="https://i.ibb.co/GGHdGct/6-Ukh-GJL0-IBv7t9sw-Klcvrd20fzgtl-Az2q-CLu9vo6-Tct-Hma-y-Pm-Wuw-Ylw-Rv-I-j-Sg-E90-ONDk-Aj-PFHl-Zk-Yi-FOf.jpg",
                caption=(
                    "<b>👌 Отлично, свая выбрана!</b>\n\n"
                    "<blockquote><b>📦 Как хотите получить сваи?</b></blockquote>\n\n"
                    "<b>🚚 Доставка</b> — привезём на ваш участок\n"
                    "<i>• Стоимость зависит от района</i>\n"
                    "<i>• Точная дата и время доставки</i>\n\n"
                    "<b>🚗 Самовывоз</b> — заберёте самостоятельно\n"
                    "<i>• Адрес склада: г. Новосибирск, СПК Сибирский Авиатор 15А</i>\n"
                    "<i>• Выберите удобное время</i>\n\n"
                    "👇 <b>Выберите вариант получения:</b>"
                ),
                reply_markup=kb
            )
            await state.set_state(OrderStates.choosing_pickup_delivery)

        else:  # Сваи с монтажом
            kb = get_district_keyboard(DISTRICTS)
            await bot.send_message(
                chat_id=callback.message.chat.id,
                text=(
                    "<b>👌 Отлично, рад, что вы доверяете моей рекомендации!</b>\n\n"
                    "<b>Теперь выберите ваш район в списке ниже.</b>\n"
                    "<blockquote><b>📍 От вашего выбора зависит:</b>\n"
                    "— 🚜 время работы бурояма на объекте;\n"
                    "— 💸 стоимость доставки.</blockquote>"
                ),
                parse_mode="HTML",
                reply_markup=kb
            )
            await state.set_state(OrderStates.entering_district)

    else:  # action_choose
        kb = InlineKeyboardMarkup(
            inline_keyboard=[
                [InlineKeyboardButton(text=f"{d} мм", callback_data=f"diameter_{d}")
                 for d in [57, 76, 89, 108, 133]]
            ]
        )
        await bot.send_message(
            chat_id=callback.message.chat.id,
            text="<b>🔩 ВЫБОР СВАЙ</b>\n\nВыберите диаметр сваи:",
            parse_mode="HTML",
            reply_markup=kb
        )
        await state.set_state(OrderStates.choosing_pile_diameter)


@dp.callback_query(lambda c: c.data and c.data.startswith('delivery_type_'))
async def process_delivery_type_callback(callback: types.CallbackQuery, state: FSMContext):
    await callback.answer()

    if callback.data == "delivery_type_delivery":
        await state.update_data(delivery_type="delivery")
        await callback.message.delete()

        kb = get_district_keyboard(DISTRICTS)
        await send_cached_photo(
            chat_id=callback.message.chat.id,
            url="https://i.ibb.co/dwtmbNYY/h0du-LZh4j-DXRq-Zh1-YOi-JV4-PSCx-Qu-Zs4u-T5-DZ-IIl-EVby-SX8k-VZEg68-y-Prnb394-EU02-Mfd-Ww3-Ld-D7-Hv7-RAHINM3.jpg",
            caption="<b>📍 Выберите район доставки</b>\n\n<blockquote>От этого зависит стоимость доставки и время прибытия</blockquote>",
            reply_markup=kb
        )
        await state.set_state(OrderStates.entering_district)

    elif callback.data == "delivery_type_pickup":
        await state.update_data(delivery_type="samovyvoz")
        await callback.message.delete()

        await send_cached_photo(
            chat_id=callback.message.chat.id,
            url="https://i.ibb.co/hFy5mQfM/photo-2026-03-11-10-02-53.jpg",
            caption=(
                "<b>👤 Отлично, самовывоз!</b>\n\n"
                "<blockquote>📍 Адрес склада: <b>г. Новосибирск, СПК Сибирский Авиатор 15А</b></blockquote>\n\n"
                "<b>Введите ваше ФИО:</b>\n"
                "<i>Это нужно для оформления документов</i>"
            ),
            reply_markup=None
        )
        await state.set_state(OrderStates.entering_fio)

# КАЛБЕК ДЛЯ ВЫБОРА ДИАМЕТРА
@dp.callback_query(lambda c: c.data and c.data.startswith('diameter_'))
async def process_diameter_callback(callback: types.CallbackQuery, state: FSMContext):
    await callback.answer()

    diameter = int(callback.data.replace('diameter_', ''))
    await state.update_data(selected_diameter=diameter)

    # Получаем доступные длины из таблицы
    available_lengths = [1500, 2000, 2500, 3000, 3500, 4000, 4500, 5000, 5500, 6000]
    valid_lengths = []
    for length in available_lengths:
        price = sheets.get_pile_price(diameter, length)
        if price:
            valid_lengths.append(length)

    if not valid_lengths:
        await callback.message.edit_text(
            f"❌ Для диаметра {diameter} мм нет доступных длин.\nПопробуйте другой диаметр:",
            reply_markup=get_diameter_keyboard()  # функция для клавиатуры диаметров
        )
        return

    # Создаем клавиатуру с длинами
    keyboard = []
    row = []
    for i, length in enumerate(valid_lengths):
        length_m = length / 1000
        row.append(InlineKeyboardButton(text=f"{length_m} м", callback_data=f"length_{length}"))
        if len(row) == 2 or i == len(valid_lengths) - 1:
            keyboard.append(row)
            row = []

    # Кнопка "Назад"
    keyboard.append([InlineKeyboardButton(text="⬅️ Назад к выбору диаметра", callback_data="back_to_diameters")])

    kb = InlineKeyboardMarkup(inline_keyboard=keyboard)

    # Проверяем, изменилось ли что-то
    new_text = f"🔩 Диаметр: {diameter} мм\n\nТеперь выберите длину сваи:"

    if callback.message.text != new_text or callback.message.reply_markup != kb:
        await callback.message.edit_text(
            new_text,
            parse_mode="HTML",
            reply_markup=kb
        )
    await state.set_state(OrderStates.choosing_pile_length)


# КАЛБЕК ДЛЯ ВЫБОРА ДЛИНЫ
@dp.callback_query(lambda c: c.data and c.data.startswith('length_'))
async def process_length_callback(callback: types.CallbackQuery, state: FSMContext):
    await callback.answer()

    length = int(callback.data.replace('length_', ''))
    data = await state.get_data()
    diameter = data.get('selected_diameter')

    # Определяем тип сваи
    if diameter <= 57:
        pile_type = "Свая 57"
    elif diameter <= 76:
        pile_type = "Свая 76"
    elif diameter <= 89:
        pile_type = "Свая 89"
    elif diameter <= 108:
        pile_type = "Свая 108"
    else:
        pile_type = "Свая 133"

    pile_info = {
        'type': f"{pile_type}×{length}",
        'diameter': diameter,
        'length': length,
        'price_per_pile': sheets.get_pile_price(diameter, length)
    }
    await state.update_data(selected_pile_info=pile_info)

    # ✅ Удаляем сообщение с выбором длины
    await callback.message.delete()

    # ✅ СРАЗУ спрашиваем количество
    await callback.message.answer(
        f"<b>✅ Выбрана свая:</b> {pile_info['type']}\n\n"
        f"<b>🔢 Введите нужное количество свай:</b>\n"
        f"(целое число, например: 16)",
        parse_mode="HTML"
    )

    # ✅ Переходим к состоянию ввода количества
    await state.set_state(OrderStates.choosing_pile_count)



@dp.message(OrderStates.choosing_pile_count)
async def process_pile_count(message: Message, state: FSMContext):
    user_id = message.from_user.id
    try:
        count = int(message.text.strip())

        if count < 4:
            await message.answer("❌ Минимальное количество свай - 4. Введите больше:")
            return
        if count > 100:
            await message.answer("❌ Максимальное количество свай - 100. Введите меньше:")
            return

        data = await state.get_data()
        selected_pile = data.get('selected_pile_info')   # ← должно быть selected_pile_info

        if not selected_pile:
            await message.answer("❌ Ошибка: данные о свае потеряны. Начните заново — /start")
            await state.clear()
            return

        # Формируем piles_info
        piles_info = {
            'type': selected_pile['type'],
            'diameter': selected_pile['diameter'],
            'length': selected_pile['length'],
            'count': count,
            'blade_width': sheets.get_blade_width(selected_pile['diameter']),
            'price_per_pile': selected_pile.get('price_per_pile', 2500)
        }

        service_type = data.get('service', 'Сваи с монтажом')

        await state.update_data(
            piles_info=piles_info,
            pile_source='manual',
            selected_piles=piles_info,
            delivery_type="delivery" if service_type == "Сваи с монтажом" else data.get('delivery_type', 'delivery')
        )

        # Сразу удаляем сообщение пользователя (чтобы не было дублей)
        try:
            await message.delete()
        except:
            pass

        print(f"✅ Пользователь {user_id} выбрал {count} свай диаметром {piles_info['diameter']}мм")

        # Дальше — в зависимости от типа услуги
        if service_type == "Сваи без монтажа":
            kb = InlineKeyboardMarkup(
                inline_keyboard=[
                    [InlineKeyboardButton(text="🚚 Доставка", callback_data="delivery_type_delivery")],
                    [InlineKeyboardButton(text="🚗 Самовывоз", callback_data="delivery_type_pickup")]
                ]
            )

            await send_cached_photo(
                chat_id=message.chat.id,
                url="https://i.ibb.co/GGHdGct/6-Ukh-GJL0-IBv7t9sw-Klcvrd20fzgtl-Az2q-CLu9vo6-Tct-Hma-y-Pm-Wuw-Ylw-Rv-I-j-Sg-E90-ONDk-Aj-PFHl-Zk-Yi-FOf.jpg",
                caption=(
                    "<b>👌 Отлично, свая выбрана!</b>\n\n"
                    "<blockquote><b>📦 Как хотите получить сваи?</b></blockquote>\n\n"
                    "<b>🚚 Доставка</b> — привезём на ваш участок\n"
                    "<b>🚗 Самовывоз</b> — заберёте самостоятельно\n\n"
                    "👇 <b>Выберите вариант получения:</b>"
                ),
                reply_markup=kb
            )
            await state.set_state(OrderStates.choosing_pickup_delivery)

        else:
            # Сваи с монтажом — сразу район
            global DISTRICTS
            if not DISTRICTS:
                DISTRICTS = sheets.get_all_districts()

            kb = get_district_keyboard(DISTRICTS)

            await bot.send_message(
                chat_id=message.chat.id,
                text=(
                    "<b>👌 Отлично, свая выбрана!</b>\n\n"
                    "<b>Теперь выберите ваш район:</b>\n"
                    "<blockquote>От этого зависит стоимость доставки и время работы техники.</blockquote>"
                ),
                parse_mode="HTML",
                reply_markup=kb
            )
            await state.set_state(OrderStates.entering_district)

    except ValueError:
        await message.answer("❌ Пожалуйста, введите целое число (например: 16)")
    except Exception as e:
        logging.error(f"Критическая ошибка в process_pile_count: {e}")
        await message.answer("❌ Произошла ошибка. Начните заново — /start")
        await state.clear()


# КАЛБЕК ДЛЯ ВОЗВРАТА К ВЫБОРУ ДИАМЕТРА
@dp.callback_query(lambda c: c.data == "back_to_diameters")
async def back_to_diameters_callback(callback: types.CallbackQuery, state: FSMContext):
    await callback.answer()

    diameters = [57, 76, 89, 108, 133]
    keyboard = []
    row = []
    for i, d in enumerate(diameters):
        row.append(InlineKeyboardButton(text=f"{d} мм", callback_data=f"diameter_{d}"))
        if len(row) == 2 or i == len(diameters) - 1:
            keyboard.append(row)
            row = []

    kb = InlineKeyboardMarkup(inline_keyboard=keyboard)

    await callback.message.edit_text(
        "<b>🔩 ВЫБОР СВАЙ</b>\n\nВыберите диаметр сваи:",
        parse_mode="HTML",
        reply_markup=kb
    )
    await state.set_state(OrderStates.choosing_pile_diameter)

# КАЛБЕК ВЫБОР УСЛУГИ (С ДОБАВЛЕННЫМ ПОЛЬЗОВАТЕЛЬСКИМ СОГЛАШЕНИЕМ)
@dp.callback_query(lambda c: c.data.startswith('service_'))
async def process_service_callback(callback: types.CallbackQuery, state: FSMContext):
    try:
        await callback.answer()
    except Exception as e:
        print(f"⚠️ Не удалось ответить на callback: {e}")

    service = "Сваи с монтажом" if callback.data == "service_mount" else "Сваи без монтажа"
    await state.update_data(service=service)
    await state.update_data(chat_id=callback.message.chat.id)

    # Удаляем предыдущее сообщение (выбор услуги)
    await callback.message.delete()

    # МИНИМАЛЬНЫЙ ТЕКСТ - ФОРМАЛЬНОСТЬ
    text = (
        "<b>⚠️ Бот работает в тестовом режиме</b>\n\n"
        "Нажмите <b>«Принимаю»</b> для продолжения."
    )

    await send_cached_photo(
        chat_id=callback.message.chat.id,
        url="https://i.ibb.co/xSprDLkt/Chat-GPT-Image-16-2026-13-47-00.png",
        caption=text,
        reply_markup=get_terms_keyboard(service)
    )
    # НЕ меняем состояние - ждём нажатия кнопки соглашения


# ДОБАВИТЬ ЭТУ ФУНКЦИЮ (если ещё нет)
def get_terms_keyboard(service_type: str):
    """Клавиатура с пользовательским соглашением"""
    # Преобразуем service_type для callback (убираем пробелы)
    service_key = "mount" if "монтажом" in service_type else "nomount"

    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="📔 Ознакомиться",
                                  url="https://drive.google.com/file/d/1MHWBz63prb5bl9yXlyNUPL01MrNQh40N/view")],
            [InlineKeyboardButton(text="✅ Принимаю", callback_data=f"accept_terms_{service_key}")]
        ]
    )


# ОБРАБОТЧИК ПРИНЯТИЯ СОГЛАШЕНИЯ
@dp.callback_query(lambda c: c.data and c.data.startswith('accept_terms_'))
async def process_accept_terms(callback: types.CallbackQuery, state: FSMContext):
    """Пользователь принял пользовательское соглашение"""
    await callback.answer()

    await state.update_data(terms_accepted=True)

    # Удаляем сообщение с соглашением
    await callback.message.delete()

    # Отправляем краткое подтверждение
    temp_msg = await callback.message.answer(
        "✅ Спасибо за принятие пользовательского соглашения!",
        parse_mode="HTML"
    )
    asyncio.create_task(auto_delete_message(callback.message.chat.id, temp_msg.message_id, 2))

    # ТЕПЕРЬ ПОКАЗЫВАЕМ СООБЩЕНИЕ С ПРОСЬБОЙ ВВЕСТИ РАЗМЕРЫ
    data = await state.get_data()
    service = data.get('service')

    if service == "Сваи с монтажом":
        prompt = (
            "<b>✅ Отлично, вы выбрали «Винтовые сваи с монтажом» — надёжное решение для вашего фундамента.</b>\n\n"
            "<blockquote><b>🛠️ О наших бригадах</b>\n"
            "Наши монтажные бригады оснащены техникой на любой случай: от мощных буроямов до компактных ручных сваекрутов. Каждый специалист — профессионал своего дела, поэтому работы пройдут безупречно.</blockquote>\n\n"
            "<b>📐 Чтобы подобрать сваи максимально точно, укажите габариты постройки в формате <code>5×5</code> (длина × ширина в метрах).</b>\n"
            "<b>💡 Если у вас не целое число, используйте точку, например: <code>5.5×6</code></b>\n\n"
            "<blockquote><b>📊 Я рассчитаю:</b>\n"
            "— точное количество свай с шагом 2 метра;\n"
            "— подберу диаметр винтовых свай исходя из нагрузки и территориальной принадлежности</blockquote>\n\n"
            "<b>➡️ Введите размеры, и мы продолжим.</b>"
        )
        photo_url = "https://i.ibb.co/tTpW2SwC/photo-2026-03-05-13-42-50.jpg"
    else:
        prompt = (
            "<b>✅ Отлично, вы выбрали «Винтовые сваи без монтажа» — вы сможете установить их самостоятельно или с любой удобной бригадой.</b>\n\n"
            "<b>📐 Чтобы подобрать сваи максимально точно, укажите габариты постройки в формате <code>5×5</code> (длина × ширина в метрах).</b>\n"
            "<b>💡 Если у вас не целое число, используйте точку, например: <code>5.5×6</code></b>\n\n"
            "<blockquote><b>📊 Я рассчитаю:</b>\n"
            "— точное количество свай с шагом 2 метра;\n"
            "— подберу диаметр винтовых свай исходя из нагрузки;\n"
            "— рассчитаю стоимость и подготовлю смету.</blockquote>\n\n"
            "<b>➡️ Введите размеры, и мы продолжим.</b>"
        )
        photo_url = "https://i.ibb.co/HDRVWYGz/photo-2026-03-05-13-44-17.jpg"

    await send_cached_photo(
        chat_id=callback.message.chat.id,
        url=photo_url,
        caption=prompt,
        reply_markup=None
    )

    # Переходим к состоянию ввода размеров
    await state.set_state(OrderStates.entering_dimensions)


#КАЛБЕК РАЙОНЫ (теперь это обычный message handler)
@dp.message(OrderStates.entering_district)
async def process_district_message(message: Message, state: FSMContext):
    district = message.text.strip()
    global DISTRICTS
    if district not in DISTRICTS:
        await message.answer(
            "❌ Пожалуйста, выберите район из списка ниже:",
            reply_markup=get_district_keyboard(DISTRICTS)
        )
        return

    await state.update_data(district=district)

    # Удаляем сообщение с районами
    data = await state.get_data()
    last_district_msg_id = data.get('last_district_msg_id')
    if last_district_msg_id:
        try:
            await bot.delete_message(message.chat.id, last_district_msg_id)
        except Exception as e:
            print(f"Ошибка удаления сообщения с районами: {e}")
        await state.update_data(last_district_msg_id=None)

    # Отправляем временное подтверждение
    temp_msg = await message.answer(
        f"✅ Выбран район: {district}",
        reply_markup=ReplyKeyboardRemove()
    )
    asyncio.create_task(auto_delete_message(message.chat.id, temp_msg.message_id, 1))

    service_type = data.get('service')
    # Для свай без монтажа мы попали сюда только при выборе доставки
    if service_type == "Сваи без монтажа":
        await message.answer(
            "<b>📅 Выберите удобную дату доставки:</b>\n\n"
            "Мы привезём сваи в этот день",
            parse_mode="HTML",
            reply_markup=await get_dates_keyboard_inline()
        )
    else:
        # Сваи с монтажом - явно устанавливаем delivery_type
        await state.update_data(delivery_type="delivery")  # ← ДОБАВЬ ЭТУ СТРОКУ

        await message.answer(
            "<b>📅 Осталось пару вопросов — выбираем дату!</b>\n\n"
            "<b>Давайте подберём удобный день для монтажа.</b>\n"
            "Все даты в календаре реально свободны — вы никого не поджимаете.\n\n"
            "<blockquote><b>⚡ Как только вы выберете дату и проведёте оплату, я мгновенно:</b>\n"
            "— внесу вас в график монтажа;\n"
            "— оповещу производство и бригаду.</blockquote>\n\n"
            "<b>Менеджер свяжется для подтверждения заявки 🚀</b>\n\n"
            "Если хотите запланировать позже предложенного — нажмите <b>«Другое»</b>, и введите удобную для вас дату.",
            parse_mode="HTML",
            reply_markup=await get_dates_keyboard_inline()
        )

# КАЛБЕК ДАТА
@dp.callback_query(lambda c: c.data and c.data.startswith('date_'))
async def process_date_callback(callback: types.CallbackQuery, state: FSMContext):
    await callback.answer()

    if callback.data == "date_other":
        await callback.message.delete()
        await callback.message.answer(
            "Введите дату в формате <b>ДД.ММ.ГГГГ</b>\nНапример: 15.06.2026",
            parse_mode="HTML"
        )
        await state.set_state(OrderStates.custom_date)
        return

    selected_date = callback.data.replace('date_', '')
    await state.update_data(selected_date=selected_date)

    # ✅ Просто удаляем и отправляем новое
    await callback.message.delete()

    free_slots = sheets.get_free_slots(selected_date)
    await callback.message.answer(
        "<b>⏰ Выберите время:</b>",
        parse_mode="HTML",
        reply_markup=get_time_keyboard_inline(free_slots)
    )


# КАЛБЕК ВРЕМЯ
@dp.callback_query(lambda c: c.data and c.data.startswith('time_'))
async def process_time_callback(callback: types.CallbackQuery, state: FSMContext):
    await callback.answer()

    selected_time = callback.data.replace('time_', '')
    await state.update_data(selected_time=selected_time)

    await callback.message.delete()

    data = await state.get_data()
    service_type = data.get('service')

    if service_type == "Сваи без монтажа":
        text= (
            "<b>Чтобы я мог обращаться к вам лично, а все документы были оформлены правильно, напишите, ваше ФИО.</b>\n\n"
            "<blockquote>Это займёт всего секунду, зато потом я буду знать, с кем имею удовольствие общаться 🤝</blockquote>"
        )
    else:
        text= (
            "<b>Чтобы я мог обращаться к вам лично, а все документы были оформлены правильно, напишите, ваше ФИО.</b>\n\n"
            "<blockquote>Это займёт всего секунду, зато потом я буду знать, с кем имею удовольствие общаться 🤝</blockquote>"
        )

    await bot.send_message(
        chat_id=callback.message.chat.id,
        text=text,
        parse_mode="HTML"
    )
    await state.set_state(OrderStates.entering_fio)

@dp.callback_query(lambda c: c.data and c.data.startswith('equipment_'))
async def process_equipment_callback(callback: types.CallbackQuery, state: FSMContext):
    await callback.answer()

    data = await state.get_data()
    name = data.get('fio', '')

    if callback.data == "equipment_big_yes":
        await state.update_data(equipment="big")
        await callback.message.edit_text(
            f"<b>Отлично, {name}!</b>\n\n"
            f"<blockquote><b>⚡ ВАЖНЫЙ МОМЕНТ ПРО ЭЛЕКТРИЧЕСТВО</b></blockquote>\n\n"
            f"Для завершающего этапа монтажа нам нужно приварить оголовки к сваям. Это ответственный процесс, от которого зависит надёжность всего фундамента.\n\n"
            f"<b>Что для этого нужно?</b>\n"
            f"— Сварочный аппарат (мы привезём свой)\n"
            f"— Электричество 220В на участке\n"
            f"— Примерно 1-2 часа работы\n\n"
            f"<blockquote><b>🔌 Если электричества нет — не страшно!</b> Мы можем организовать генератор. В следующем шаге вы сможете выбрать количество дней аренды.</blockquote>\n\n"
            f"<b>👇 Подскажите, есть ли на участке электричество?</b>",
            parse_mode="HTML",
            reply_markup=get_yes_no_keyboard("electricity")
        )

    elif callback.data == "equipment_big_no":
        await callback.message.edit_text(
            f"<b>{name}, спасибо, что уточнили!</b> Значит, большой буроям не пройдёт — не страшно, у нас есть техника поменьше.\n\n"
            f"‼️<b>Подскажите, сможет ли на участок заехать маленький гусеничный буроям?</b> Его габариты — 2 × 1,5 метра, он пройдёт даже в узкие проёмы.\n"
            f"Если и он не проедет, мы привезём ручной сваекрут — монтаж займёт чуть больше времени, но качество останется таким же безупречным.\n\n"
            f"<b>👇 Выберите вариант:</b>",
            parse_mode="HTML",
            reply_markup=get_small_equipment_keyboard()
        )

    elif callback.data == "equipment_small_yes":
        await state.update_data(equipment="small")
        await callback.message.edit_text(
            f"<b>Отлично, {name}!</b>\n\n"
            f"<blockquote><b>⚡ ВАЖНЫЙ МОМЕНТ ПРО ЭЛЕКТРИЧЕСТВО</b></blockquote>\n\n"
            f"Для завершающего этапа монтажа нам нужно приварить оголовки к сваям. Это ответственный процесс, от которого зависит надёжность всего фундамента.\n\n"
            f"<b>Что для этого нужно?</b>\n"
            f"— Сварочный аппарат (мы привезём свой)\n"
            f"— Электричество 220В на участке\n"
            f"— Примерно 1-2 часа работы\n\n"
            f"<blockquote><b>🔌 Если электричества нет — не страшно!</b> Мы можем организовать генератор. В следующем шаге вы сможете выбрать количество дней аренды.</blockquote>\n\n"
            f"<b>👇 Подскажите, есть ли на участке электричество?</b>",
            parse_mode="HTML",
            reply_markup=get_yes_no_keyboard("electricity")
        )
    elif callback.data == "equipment_manual":
        # Получаем информацию о районе и диаметре
        district = data.get('district')
        diameter = data.get('diameter') or data.get('piles_info', {}).get('diameter', 0)
        pile_count = data.get('pile_count', 0)

        # 🔥 ПРОВЕРКА ДИАМЕТРА
        if diameter >= 108:
            kb = InlineKeyboardMarkup(
                inline_keyboard=[
                    [InlineKeyboardButton(text="⬅️ Назад к выбору техники", callback_data="back_to_equipment")],
                    [InlineKeyboardButton(text="📞 Связаться с менеджером", url="https://t.me/mskallen")]
                ]
            )

            await callback.message.edit_text(
                f"<b>❌ Ручной монтаж невозможен для свай диаметром {diameter} мм</b>\n\n"
                "<blockquote>Сваи диаметром 108 мм и выше можно установить только с помощью техники — ручной сваекрут не справится.</blockquote>\n\n"
                "<b>Что можно сделать?</b>\n"
                "• Вернуться и выбрать другой тип техники\n"
                "• Связаться с менеджером для уточнения деталей",
                parse_mode="HTML",
                reply_markup=kb
            )
            return

        # Проверяем, разрешён ли ручной монтаж в этом районе
        district_info = sheets.get_district_info(district) if district else None
        manual_allowed = True
        if district_info and district_info.get('mounting'):
            manual_allowed = district_info['mounting'].get('manual_available', True)
            print(f"🔥 Ручной монтаж в районе {district}: {'разрешён' if manual_allowed else 'ЗАПРЕЩЁН'}")

        if not manual_allowed:
            kb = InlineKeyboardMarkup(
                inline_keyboard=[
                    [InlineKeyboardButton(text="⬅️ Назад к выбору техники", callback_data="back_to_equipment")],
                    [InlineKeyboardButton(text="📞 Связаться с менеджером", url="https://t.me/mskallen")]
                ]
            )

            await callback.message.edit_text(
                f"<b>❌ В районе {district} ручной монтаж невозможен</b>\n\n"
                "<blockquote>По техническим причинам мы не можем выполнять монтаж в данном районе. К сожалению там в 80% встречается супесь и песок, которые не позволяют работать ручным инструментом. Выберите вариант с буроямом.</blockquote>\n\n"
                "<b>Что можно сделать?</b>\n"
                "• Вернуться и выбрать другой тип техники\n"
                "• Связаться с менеджером для уточнения деталей",
                parse_mode="HTML",
                reply_markup=kb
            )
            return

        if pile_count <= 16:
            await state.update_data(equipment="manual")
            await callback.message.edit_text(
                f"<b>{name}, не беда!</b> Раз большая техника не проходит — используем ручной сваекрут. Он компактный, пройдёт где угодно, а качество монтажа останется безупречным 🤝\n\n"
                f"<blockquote><b>Теперь важный момент:</b> для завершающего этапа — приварки оголовков к сваям — нам понадобится сварочный аппарат.</blockquote>\n"
                f"Подскажите, есть ли на участке электричество? Это нужно, чтобы зафиксировать оголовки надёжно и навсегда.\n\n"
                f"<b>👇 Выберите вариант:</b>",
                parse_mode="HTML",
                reply_markup=get_yes_no_keyboard("electricity")
            )
        else:
            kb = InlineKeyboardMarkup(
                inline_keyboard=[
                    [InlineKeyboardButton(text="⬅️ Назад к выбору техники", callback_data="back_to_equipment")],
                    [InlineKeyboardButton(text="📞 Связаться с менеджером", url="https://t.me/mskallen")]
                ]
            )

            await callback.message.edit_text(
                "<b>❌ ЗАКАЗ НЕВОЗМОЖЕН</b>\n\n"
                f"<blockquote>К сожалению, техника не может заехать на участок, "
                f"а для ручного монтажа {pile_count} свай потребуется слишком много времени.</blockquote>\n\n"
                "<b>Что можно сделать?</b>\n"
                "• Вернуться и выбрать другой тип техники\n"
                "• Связаться с менеджером для поиска решения",
                parse_mode="HTML",
                reply_markup=kb
            )
            return


@dp.callback_query(lambda c: c.data == "back_to_equipment")
async def back_to_equipment_callback(callback: types.CallbackQuery, state: FSMContext):
    """Возврат к выбору техники"""
    await callback.answer()

    data = await state.get_data()
    name = data.get('fio', '')

    # Показываем снова выбор техники
    await callback.message.edit_text(
        f"<b>Отлично, {name}! Мы уже у финиша 🏁</b>\n\n"
        "<b>‼️ Уточните важный технический момент:</b>\n"
        "сможет ли на ваш участок свободно заехать техника с габаритами <b>4,5 × 2,5 метра</b> (большой буроям)?\n\n"
        "Это нужно, чтобы бригада сразу взяла нужное оборудование и не столкнулась с сюрпризами на месте.\n\n"
        "<b>👇 Выберите вариант:</b>",
        parse_mode="HTML",
        reply_markup=get_equipment_keyboard()
    )

# КАЛБЕК ЭЛЕКТРИЧЕСТВО
@dp.callback_query(lambda c: c.data and (c.data.startswith('electricity_') or
                                         c.data.startswith('geology_') or
                                         c.data.startswith('guarantee_')))
async def process_yes_no_callback(callback: types.CallbackQuery, state: FSMContext):
    await callback.answer()

    data = await state.get_data()
    name = data.get('fio', '')

    if callback.data.startswith('electricity_'):
        if callback.data == "electricity_yes":
            await state.update_data(electricity=True, generator_days=0)
            await callback.message.edit_text(
                f"<b>✨ {name}, финальный вопрос!</b>\n\n"
                f"<b>📄 У вас есть геологическое заключение по грунтам?</b>\n"
                f"Это позволит нам предоставить вам расширенную гарантию на фундамент — с учётом всех особенностей вашего участка.\n\n"
                f"<blockquote><b>🔍 Если заключения нет — не проблема!</b>\n"
                f"Мы всё равно даём гарантию на сами сваи (качество изготовления) <b>до 50 лет</b> — мы уверены в своей продукции на 100%.</blockquote>\n\n"
                f"<b>👇 Выберите вариант:</b>",
                parse_mode="HTML",
                reply_markup=get_yes_no_keyboard("geology")
            )
        else:  # electricity_no
            # Кнопки с вариантами дней
            kb = InlineKeyboardMarkup(
                inline_keyboard=[
                    [
                        InlineKeyboardButton(text="1 день", callback_data="generator_1"),
                        InlineKeyboardButton(text="2 дня", callback_data="generator_2"),
                        InlineKeyboardButton(text="3 дня", callback_data="generator_3")
                    ]
                ]
            )
            await callback.message.edit_text(
                f"<b>{name}, понял!</b> Раз электричества нет — возьмём генератор напрокат.\n\n"
                f"<blockquote><b>⚡ Стоимость аренды генератора — 3 000 ₽/сутки.</b> Этого достаточно для работы сварочного аппарата и всего необходимого оборудования.</blockquote>\n\n"
                f"<b>📅 На сколько дней потребуется генератор?</b>\n"
                f"Обычно достаточно 1-3 дней, но вы можете выбрать нужное количество.\n\n"
                f"<b>👇 Выберите количество дней:</b>",
                parse_mode="HTML",
                reply_markup=kb
            )

    elif callback.data.startswith('geology_'):
        if callback.data == "geology_yes":
            await state.update_data(geology=True)
            google_drive_link = "https://drive.google.com/file/d/10BpqRiankSN7oNaejZFU5SV0_CYL89P_/view"
            await callback.message.edit_text(
                "<b>📄 ДОПОЛНИТЕЛЬНАЯ ГАРАНТИЯ</b>\n\n"
                "<blockquote>На основе вашей геологии мы можем предоставить "
                "<b><u>расширенную гарантию</u></b> на фундамент.</blockquote>\n\n"
                f"Посмотреть: <a href='{google_drive_link}'>📎 ДОКУМЕНТ</a>\n\n"
                "<b>❓ Нужна ли она вам?</b>",
                parse_mode="HTML",
                reply_markup=get_yes_no_keyboard("guarantee")
            )
        else:  # geology_no
            await state.update_data(geology=False, guarantee=False)
            await callback.message.delete()
            await go_to_estimate(callback.message, state)

    elif callback.data.startswith('guarantee_'):
        if callback.data == "guarantee_yes":
            await state.update_data(guarantee=True)
        else:  # guarantee_no
            await state.update_data(guarantee=False)

        await callback.message.delete()
        await go_to_estimate(callback.message, state)



# Найдите этот callback и добавьте склонение в подтверждение:

@dp.callback_query(lambda c: c.data and c.data.startswith('generator_'))
async def process_generator_callback(callback: types.CallbackQuery, state: FSMContext):
    await callback.answer()

    days = int(callback.data.split('_')[1])
    await state.update_data(electricity=False, generator_days=days)

    # Правильное склонение для сообщения
    if days == 1:
        days_word = "день"
    elif days < 5:
        days_word = "дня"
    else:
        days_word = "дней"

    # Переходим к геологии
    data = await state.get_data()
    name = data.get('fio', '')

    await callback.message.edit_text(
        f"<b>✨ {name}, финальный вопрос!</b>\n\n"
        f"<b>📄 У вас есть геологическое заключение по грунтам?</b>\n"
        f"Это позволит нам предоставить вам расширенную гарантию на фундамент — с учётом всех особенностей вашего участка.\n\n"
        f"<blockquote><b>🔍 Если заключения нет — не проблема!</b>\n"
        f"Мы всё равно даём гарантию на сами сваи (качество изготовления) <b>до 50 лет</b> — мы уверены в своей продукции на 100%.</blockquote>\n\n"
        f"<b>👇 Выберите вариант:</b>",
        parse_mode="HTML",
        reply_markup=get_yes_no_keyboard("geology")
    )


@dp.callback_query(lambda c: c.data and c.data.startswith('estimate_'))
async def process_estimate_actions(callback: types.CallbackQuery, state: FSMContext):
    await callback.answer()

    if callback.data == "estimate_pay":
        data = await state.get_data()
        amount = float(data.get('prepayment', 0))

        if amount <= 0:
            await callback.message.answer("❌ Сумма оплаты не может быть 0.")
            return

        # === САМОЕ ВАЖНОЕ ИСПРАВЛЕНИЕ ===
        service_type = data.get('service')

        # Дополнительная защита — проверяем по другим местам, если service потерялся
        if not service_type:
            # Проверяем по выбранной услуге в самом начале
            if callback.message.chat.id:  # просто чтобы не было ошибки
                print(f"⚠️ service_type потерян! data.keys(): {list(data.keys())}")
            service_type = "Сваи с монтажом"  # fallback

        delivery_type = data.get('delivery_type', 'delivery')

        print(f"DEBUG: service_type = '{service_type}'")   # ← добавь для отладки

        # === Выбор договора ===
        if service_type == "Сваи с монтажом":
            contract_text = "📜 Договор поставки с монтажом"
            contract_url = "https://drive.google.com/file/d/10BpqRiankSN7oNaejZFU5SV0_CYL89P_/view"
        else:
            contract_text = "📜 Договор поставки без монтажа"
            contract_url = "https://drive.google.com/file/d/1EpbW9xu0uEuxrudtQOpKmYYVlYF3377w/view"

        # Описание для ЮKassa
        piles_info = data.get('piles_info', {})
        pile_count = piles_info.get('count', 0)
        pile_diameter = piles_info.get('diameter', 108)

        if service_type == "Сваи с монтажом":
            description = f"Сваи {pile_diameter}мм — {pile_count} шт. Монтаж на участке"
        elif delivery_type == "delivery":
            description = f"Сваи {pile_diameter}мм — {pile_count} шт. Доставка до адреса"
        else:
            description = f"Сваи {pile_diameter}мм — {pile_count} шт. Самовывоз со склада"

        try:
            Configuration.account_id = config.YOOKASSA_SHOP_ID
            Configuration.secret_key = config.YOOKASSA_SECRET_KEY

            idempotency_key = str(uuid.uuid4())

            payment = Payment.create({
                "amount": {"value": f"{amount:.2f}", "currency": "RUB"},
                "confirmation": {
                    "type": "redirect",
                    "return_url": "https://t.me/svainsk_bot"
                },
                "capture": True,
                "description": description[:128],
                "receipt": {
                    "customer": {
                        "full_name": data.get('fio', 'Клиент'),
                        "email": data.get('email', 'noemail@example.com'),
                        "phone": data.get('phone', '')
                    },
                    "items": [
                        {
                            "description": description[:128],
                            "quantity": "1.00",
                            "amount": {"value": f"{amount:.2f}", "currency": "RUB"},
                            "vat_code": 1,
                            "payment_mode": "full_payment",
                            "payment_subject": "commodity"
                        }
                    ]
                }
            }, idempotency_key)

            payment_url = payment.confirmation.confirmation_url
            await state.update_data(yookassa_payment_id=payment.id)

            await callback.message.answer(
                "<b>✅ ССЫЛКА НА ОПЛАТУ ГОТОВА!</b>\n\n"
                "<blockquote>"
                f"Сумма к оплате: <b>{amount} ₽</b>\n\n"
                "Оплата проходит через ЮKassa"
                "</blockquote>\n\n"
                "<b>Оплата проходит через:</b>\n"
                "— Банковские карты (Visa, Mastercard, Мир)\n"
                "— СБП\n"
                "— СберПэй\n"
                "— ЮMoney\n"
                "— Другие способы",
                parse_mode="HTML",
                reply_markup=InlineKeyboardMarkup(
                    inline_keyboard=[
                        [InlineKeyboardButton(text="💳 ОПЛАТИТЬ ЧЕРЕЗ ЮKASSA", url=payment_url)],
                        [InlineKeyboardButton(text="🔧 ИЗМЕНИТЬ СМЕТУ", callback_data="estimate_change")]
                    ]
                )
            )
            # ХУЙ

        except Exception as e:
            logging.error(f"ЮKassa error: {e}", exc_info=True)
            await callback.message.answer(f"❌ Ошибка ЮKassa: {str(e)[:400]}")

    elif callback.data == "estimate_change":
        kb = ReplyKeyboardMarkup(
            keyboard=[
                [KeyboardButton(text="🚫 Убрать оголовки")],
                [KeyboardButton(text="🚫 Убрать монтаж")],
                [KeyboardButton(text="🚫 Убрать генератор")],
                [KeyboardButton(text="🔄 Составить заново")],
                [KeyboardButton(text="✅ Завершить изменения")]
            ],
            resize_keyboard=True
        )
        await callback.message.answer(
            "<b>🔧 ИЗМЕНЕНИЕ СМЕТЫ</b>\n\n"
            "<b>Выберите, что убрать из сметы:</b>",
            parse_mode="HTML",
            reply_markup=await get_modify_estimate_keyboard(state)  # ← ЭТО ИНЛАЙН
        )
        await state.set_state(OrderStates.changing_estimate)

@dp.callback_query(lambda c: c.data and c.data.startswith('back_'))
async def process_back_callback(callback: types.CallbackQuery, state: FSMContext):
    await callback.answer()

    if callback.data == "back_to_service":
        # Удаляем текущее сообщение
        await callback.message.delete()
        # Отправляем новое с обычной клавиатурой
        kb = ReplyKeyboardMarkup(
            keyboard=[
                [KeyboardButton(text="Сваи с монтажом")],
                [KeyboardButton(text="Сваи без монтажа")]
            ],
            resize_keyboard=True
        )
        await bot.send_message(
            chat_id=callback.message.chat.id,
            text="<b>👇 Выберите услугу:</b>",
            parse_mode="HTML",
            reply_markup=kb
        )
        await state.set_state(OrderStates.choosing_service)

    elif callback.data == "back_to_district":
        # Удаляем текущее сообщение с датой
        await callback.message.delete()

        # ✅ Отправляем новое сообщение с обычной клавиатурой районов
        msg = await bot.send_message(
            chat_id=callback.message.chat.id,
            text="<b>📍 Выберите район повторно:</b>",
            parse_mode="HTML",
            reply_markup=get_district_keyboard(DISTRICTS)
        )

        # ✅ СОХРАНЯЕМ ID ЭТОГО СООБЩЕНИЯ
        await state.update_data(last_district_msg_id=msg.message_id)
        await state.set_state(OrderStates.entering_district)

    elif callback.data == "back_to_date":
        # Для даты можно оставить edit_text, так как там инлайн-клавиатура
        await callback.message.edit_text(
            "<b>📅 Осталось пару вопросов — выбираем дату!</b>\n\n"
            "<b>Давайте подберём удобный день для монтажа.</b>\n"
            "Все даты в календаре реально свободны — вы никого не поджимаете.\n\n"
            "<blockquote><b>⚡ Как только вы выберете дату и проведёте оплату, я мгновенно:</b>\n"
            "— внесу вас в график монтажа;\n"
            "— оповещу производство и бригаду.</blockquote>\n\n"
            "<b>Менеджер свяжется для подтверждения заявки 🚀</b>\n\n"
            "Если хотите запланировать позже предложенного — нажмите <b>«Другое»</b>, и введите удобную для вас дату.",
            parse_mode="HTML",
            reply_markup=await get_dates_keyboard_inline()
        )


async def run_calculation_with_queue(callback_query: types.CallbackQuery, state: FSMContext):
    """Запускает расчет с очередью"""
    user_id = callback_query.from_user.id
    chat_id = callback_query.message.chat.id

    data = await state.get_data()
    floors = data.get('floors')
    length = data.get('length')
    width = data.get('width')
    material = data.get('material')

    print(f"🔥 run_calculation_with_queue: floors={floors}, length={length}, width={width}, material={material} для user={user_id}")

    await state.update_data(
        user_chat_id=chat_id,
        is_calculating=False,
        floors=floors,
        length=length,
        width=width,
        material=material
    )

    # Проверки очереди и блокировки (оставляем как у тебя)
    queue_position = await CalcQueue.get_queue_position(user_id)
    if queue_position > 0:
        await callback_query.message.answer(f"<b>⏳ Вы уже в очереди!</b>\nВаша позиция: <code>{queue_position}</code>", parse_mode="HTML")
        return

    lock_holder = await CalcQueue.get_lock_holder()
    if lock_holder == user_id:
        await callback_query.message.answer("⏳ Ваш расчет уже выполняется...", parse_mode="HTML")
        return

    lock_acquired = await CalcQueue.try_acquire_lock(user_id)

    if lock_acquired:
        print(f"✅ Захвачена блокировка для {user_id}")
        await do_calculation(callback_query, state)
    else:
        holder = await CalcQueue.get_lock_holder()
        if holder == user_id:
            await callback_query.message.answer("⏳ Ваш расчет уже выполняется...", parse_mode="HTML")
            return

        # === ИСПРАВЛЕННОЕ СОХРАНЕНИЕ ПОЛНЫХ ДАННЫХ ===
        full_data_key = f"calc_user_data_full:{user_id}"
        current_data = await state.get_data()
        redis_queue.setex(full_data_key, 600, str(current_data))   # ← ВСЁ состояние

        position = await CalcQueue.add_to_queue(user_id, chat_id)
        queue_length = await CalcQueue.get_queue_length()

        msg = await callback_query.message.answer(
            f"<b>⏳ Ваш запрос добавлен в очередь!</b>\n\n"
            f"Позиция: <code>{position}</code> из {queue_length}\n\n"
            f"Когда подойдёт очередь — расчёт начнётся автоматически.",
            parse_mode="HTML"
        )

        queue_messages[user_id] = msg.message_id
        asyncio.create_task(update_queue_position(user_id, chat_id, msg.message_id))


async def check_queue_and_run(user_id: int, chat_id: int, state: FSMContext):
    """Проверяет очередь и запускает расчет - FIFO очередь"""
    max_attempts = 60
    attempt = 0

    while attempt < max_attempts:
        # Получаем первого в очереди
        next_user = await CalcQueue.get_next_from_queue()

        # Если мы первые
        if next_user == user_id:
            # Пытаемся захватить блокировку
            lock_acquired = await CalcQueue.try_acquire_lock(user_id)
            if lock_acquired:
                # Удаляем сообщение очереди
                data = await state.get_data()
                queue_msg_id = data.get('queue_msg_id')
                if queue_msg_id:
                    try:
                        await bot.delete_message(chat_id, queue_msg_id)
                        await state.update_data(queue_msg_id=None)
                    except:
                        pass

                await start_calculation_for_user(user_id, chat_id)  # ← передаем оба параметра
                return
        else:
            # Проверяем, не устарела ли позиция
            position = await CalcQueue.get_queue_position(user_id)
            if position > 0:
                # Обновляем сообщение каждые 5 секунд
                if attempt % 5 == 0:
                    data = await state.get_data()
                    queue_msg_id = data.get('queue_msg_id')
                    if queue_msg_id:
                        try:
                            await bot.edit_message_text(
                                chat_id=chat_id,
                                message_id=queue_msg_id,
                                text=f"<b>⏳ Расчет в очереди...</b>\n\n"
                                     f"<b>Ваша позиция:</b> <code>{position}</code>\n\n"
                                     f"<blockquote>🔧 Впереди еще {position - 1} расчетов.\n"
                                     f"Примерное ожидание: ~{(position - 1) * 25} секунд.</blockquote>",
                                parse_mode="HTML"
                            )
                        except:
                            pass

        await asyncio.sleep(1)
        attempt += 1

    # Таймаут
    await CalcQueue.remove_from_queue(user_id)
    await bot.send_message(
        chat_id=chat_id,
        text=f"<b>❌ Время ожидания истекло!</b>\n\n"
             f"Извините, но расчет занял слишком много времени.\n"
             f"<b>Пожалуйста, начните заново командой /start</b>",
        parse_mode="HTML"
    )


async def start_calculation_for_user(user_id: int, chat_id: int):
    print(f"🔥 start_calculation_for_user: user={user_id}, chat={chat_id}")

    storage_key = StorageKey(
        bot_id=str(bot.id),
        chat_id=str(user_id),
        user_id=str(user_id),
        destiny=str(user_id)
    )

    user_state = FSMContext(storage=dp.storage, key=storage_key)

    # 1. Основное восстановление полного состояния
    full_data_key = f"calc_user_data_full:{user_id}"
    user_data_str = redis_queue.get(full_data_key)

    if user_data_str:
        try:
            import ast
            full_data = ast.literal_eval(user_data_str)
            await user_state.set_data(full_data)
            print(f"✅ Полностью восстановлены данные из Redis для {user_id}")
        except Exception as e:
            print(f"❌ Ошибка восстановления полных данных: {e}")

    # 2. Дополнительное восстановление только piles_info (страховка)
    data = await user_state.get_data()
    if not data.get('piles_info'):
        piles_key = f"calc_piles_info:{user_id}"
        piles_str = redis_queue.get(piles_key)
        if piles_str:
            try:
                import ast
                piles_info = ast.literal_eval(piles_str)
                current = await user_state.get_data()
                current['piles_info'] = piles_info
                await user_state.set_data(current)
                print(f"✅ Восстановлен piles_info из отдельного ключа для {user_id}")
            except Exception as e:
                print(f"❌ Не удалось восстановить piles_info отдельно: {e}")

    # Финальная проверка
    data = await user_state.get_data()
    if not data.get('piles_info'):
        print(f"⚠️ piles_info ВСЁ ЕЩЁ отсутствует после всех попыток у {user_id} — будет пересчитан")
    else:
        print(f"✅ piles_info успешно присутствует после восстановления у {user_id}")

    fake_callback = type('obj', (object,), {
        'from_user': type('obj', (object,), {'id': user_id}),
        'message': type('obj', (object,), {'chat': type('obj', (object,), {'id': chat_id})})
    })()

    await do_calculation(fake_callback, user_state)


async def do_calculation(callback_query: types.CallbackQuery, state: FSMContext):
    """Сам расчет"""

    user_id = callback_query.from_user.id
    data = await state.get_data()

    if not data.get('piles_info'):
        print(f"⚠️ Нет piles_info для {user_id} — пересчитываем заново")

    # Проверяем, не выполняется ли уже расчёт
    if data.get('is_calculating'):
        print(f"⚠️ Расчет уже выполняется для {user_id}, пропускаем")
        return

    # Получаем chat_id
    if hasattr(callback_query, 'message') and callback_query.message:
        chat_id = callback_query.message.chat.id
    else:
        chat_id = data.get('user_chat_id', user_id)

    if not chat_id:
        print(f"❌ Нет chat_id для пользователя {user_id}")
        return

    print(f"🔥 do_calculation: user={user_id}, chat={chat_id}")

    await state.update_data(is_calculating=True)

    floors = data.get('floors')
    print(f"🔥 do_calculation: floors = {floors}")

    if not floors:
        floors = 1
        await state.update_data(floors=1)

    # Удаляем сообщение очереди
    if user_id in queue_messages:
        try:
            await bot.delete_message(chat_id, queue_messages[user_id])
        except:
            pass
        del queue_messages[user_id]

    # Сообщение "Выполняю расчет..."
    try:
        wait_msg = await bot.send_message(
            chat_id=chat_id,
            text=f"<b>Выполняю расчет для {str(floors)} этаж(а)...</b>\n\n"
                 f"<blockquote>⏱ Расчет займет примерно 5-10 секунд.\n"
                 f"Пожалуйста, подождите...</blockquote>",
            parse_mode="HTML"
        )
    except Exception as e:
        print(f"❌ Ошибка отправки сообщения: {e}")
        wait_msg = await bot.send_message(
            chat_id=user_id,
            text=f"<b>Выполняю расчет для {str(floors)} этаж(а)...</b>\n\n"
                 f"<blockquote>⏱ Расчет займет примерно 5-10 секунд.\n"
                 f"Пожалуйста, подождите...</blockquote>",
            parse_mode="HTML"
        )
        chat_id = user_id

    try:
        data = await state.get_data()
        floors = data.get('floors')

        length = data.get('length')
        width = data.get('width')
        if length and width:
            length, width = max(length, width), min(length, width)
            await state.update_data(length=length, width=width)

        print(f"🔥 ПЕРЕД save_to_source_sheet: data = {data}")

        sheets.save_to_source_sheet(data)
        await asyncio.sleep(3)

        length = data.get('length')
        width = data.get('width')
        max_side = max(length, width)

        if max_side < 8:
            step = 2.0
        elif max_side <= 10:
            step = 2.5
        else:
            step = 3.0

        result = sheets.get_pile_result(
            length=data.get('length'),
            width=data.get('width'),
            step=step
        )

        pile_capacity = {57: 1.5, 76: 2.21, 89: 3.12, 108: 4.25, 133: 5.6, 159: 10.2}

        if result:
            piles_info = {
                'diameter': result['diameter'],
                'count': result['count'],
                'type': f"Свая {result['diameter']}×3000",
                'length': 3000,
                'blade_width': sheets.get_blade_width(result['diameter']),
                'grid_count': result.get('grid_count', result['count']),
                'min_needed': result.get('min_needed', result['count'])
            }
            total_weight = result.get('total_weight', 0)
            sheets.clear_source_sheet()
            print(f"✅ Используем расчёт из таблицы: {result}")
        else:
            piles_info = calculate_piles(
                material=data.get('material'),
                floors=floors,
                length=data.get('length'),
                width=data.get('width')
            )
            area = data.get('length') * data.get('width')
            material_load = {"Дерево": 0.8, "Каркас": 0.9, "Газобетон": 1.3, "Кирпич": 1.8}
            floor_factor = {1: 1.0, 2: 1.8, 3: 2.5}
            total_weight = area * material_load.get(data.get('material'), 1.0) * floor_factor.get(floors, 1.0) * 1.3
            print(f"⚠️ Используем расчёт по умолчанию")

        # ====================== ВАЖНЫЙ БЛОК СОХРАНЕНИЯ ======================
        # Сохраняем piles_info
        await state.update_data(piles_info=piles_info)

        # Защита service_type (чтобы не терялся у второго пользователя)
        service_type = data.get('service')
        if service_type:
            await state.update_data(service=service_type)

        # Принудительное сохранение всего состояния в Redis
        full_data_key = f"calc_user_data_full:{user_id}"
        current_data = await state.get_data()
        redis_queue.setex(full_data_key, 600, str(current_data))
        print(f"✅ Принудительно сохранено актуальное состояние (с piles_info) для {user_id}")

        # Дополнительно сохраняем piles_info отдельно (страховка)
        redis_queue.setex(f"calc_piles_info:{user_id}", 600, str(piles_info))
        # =====================================================================

        capacity = pile_capacity.get(piles_info['diameter'], 5.6)

        kb = InlineKeyboardMarkup(
            inline_keyboard=[
                [InlineKeyboardButton(text="✅ Взять рекомендованные", callback_data="action_take")],
                [InlineKeyboardButton(text="✏️ Выбрать самому", callback_data="action_choose")]
            ]
        )

        await send_cached_photo(
            chat_id=chat_id,
            url="https://i.ibb.co/F48gZCMJ/photo-2026-03-05-13-49-20.jpg",
            caption=(
                f"<b>🔍 Отлично, расчёт завершён!</b>\n\n"
                f"<blockquote><b>📋 Исходные данные:</b>\n"
                f"{data.get('length')}×{data.get('width')} | {data.get('material')} | {str(floors)} эт.</blockquote>\n\n"
                f"<b>⚖️ Расчёт нагрузки</b>\n"
                f"Вес дома: <b>{total_weight:.2f} т (включая +30% снеговой нагрузки)</b>\n\n"
                f"<b>🔩 Рекомендация по свае</b>\n"
                f"<blockquote>Диаметр: <b>{piles_info['diameter']} мм</b> (несущая способность {capacity} т/сваю)\n"
                f"Длина: <b>от 3000 мм</b>\n"
                f"Лопасть: <b>{piles_info['blade_width']} мм</b>\n"
                f"Стенка: <b>3,5 мм</b></blockquote>\n\n"
                f"<b>📦 Комплектация</b>\n"
                f"<blockquote>Сваи: <b>{piles_info['count']} шт</b>\n"
                f"Оголовки 200×200: <b>{piles_info['count']} шт</b></blockquote>\n\n"
                f"<b>🎯 Моё предложение</b>\n"
                f"<blockquote><b>Свая винтовая {piles_info['diameter']}/3000/{piles_info['blade_width']} — {piles_info['count']} шт</b></blockquote>\n\n"
                f"<b>📍 Учтены требования Новосибирской области</b>\n"
                f"— глубина промерзания 2,2–2,5 м\n"
                f"— запас по длине 3000 мм для надёжности\n\n"
                f"<b>👇 Выберите шаг</b>\n"
                f"— Выбрать самому (скорректировать)\n"
                f"— Взять рекомендованные (оформить монтаж)"
            ),
            reply_markup=kb,
        )
        await state.set_state(OrderStates.showing_recommendations)

        await wait_msg.delete()

    except Exception as e:
        print(f"❌ Ошибка в do_calculation: {e}")
        await wait_msg.delete()
        await bot.send_message(chat_id=chat_id, text=f"❌ Произошла ошибка при расчёте: {str(e)}", parse_mode="HTML")
    finally:
        await state.update_data(is_calculating=False)
        await CalcQueue.release_lock()
        await process_next_in_queue()


async def process_next_in_queue():
    """Запускает следующего пользователя из очереди АВТОМАТИЧЕСКИ"""

    # ✅ Сначала проверяем, свободна ли блокировка
    lock_holder = await CalcQueue.get_lock_holder()
    if lock_holder != 0:
        print(f"⚠️ Блокировка все еще занята пользователем {lock_holder}, ждем...")
        asyncio.create_task(delayed_queue_check())
        return

    next_user_id, next_chat_id = await CalcQueue.get_next_from_queue()

    if next_user_id and next_chat_id:
        print(f"🔄 АВТОМАТИЧЕСКИ запускаем пользователя {next_user_id}")

        # ✅ Захватываем блокировку для следующего пользователя
        lock_acquired = await CalcQueue.try_acquire_lock(next_user_id)
        if not lock_acquired:
            print(f"⚠️ Не удалось захватить блокировку для {next_user_id}")
            await CalcQueue.add_to_queue(next_user_id, next_chat_id)
            return

        # ✅ Уведомляем пользователя (отправляем в его личный чат)
        try:
            await bot.send_message(
                chat_id=next_user_id,  # ← отправляем пользователю в личку
                text=(
                    f"<b>✅ Ваша очередь!</b>\n\n"
                    f"Предыдущий расчет завершен.\n"
                    f"<b>Автоматически начинаю расчет...</b>"
                ),
                parse_mode="HTML"
            )
        except Exception as e:
            print(f"❌ Ошибка отправки уведомления: {e}")

        # ✅ Запускаем расчет
        await start_calculation_for_user(next_user_id, next_user_id)
    else:
        print("📭 Очередь пуста")


async def delayed_queue_check():
    """Отложенная проверка очереди"""
    await asyncio.sleep(2)
    await process_next_in_queue()


@dp.callback_query(lambda c: c.data and c.data.startswith('floors_'))
async def process_floors_callback(callback: types.CallbackQuery, state: FSMContext):
    try:
        await callback.answer()
    except:
        pass

    data = await state.get_data()

    # Защита от повторных нажатий
    if data.get('is_calculating'):
        await callback.message.answer("⏳ Ваш расчет уже выполняется. Пожалуйста, подождите...", parse_mode="HTML")
        return

    queue_position = await CalcQueue.get_queue_position(callback.from_user.id)
    if queue_position > 0:
        await callback.message.answer(f"<b>⏳ Вы уже в очереди!</b>\nВаша позиция: <code>{queue_position}</code>", parse_mode="HTML")
        return

    try:
        floors = int(callback.data.split('_')[1])

        await state.update_data(
            floors=floors,
            user_id=callback.from_user.id,
            user_chat_id=callback.message.chat.id,
            is_calculating=False
        )

        print(f"✅ Сохранены этажи: {floors} для пользователя {callback.from_user.id}")

        # УДАЛЯЕМ сообщение с кнопками этажей
        try:
            await callback.message.delete()
        except:
            pass

        # ←←← ВАЖНО: сразу сохраняем текущее состояние в Redis
        full_data_key = f"calc_user_data_full:{callback.from_user.id}"
        current_data = await state.get_data()
        redis_queue.setex(full_data_key, 600, str(current_data))
        print(f"✅ Сохранено состояние после выбора этажей для {callback.from_user.id}")

        await run_calculation_with_queue(callback, state)

    except Exception as e:
        print(f"❌ Ошибка в process_floors_callback: {e}")
        await state.update_data(is_calculating=False)


async def delete_temp_message(chat_id: int, state: FSMContext):
    """Удаляет временное сообщение, если оно есть, и сбрасывает ID"""
    data = await state.get_data()
    temp_id = data.get('last_temp_msg_id')
    if temp_id:
        try:
            await bot.delete_message(chat_id, temp_id)
        except:
            pass
        await state.update_data(last_temp_msg_id=None)

@dp.callback_query(lambda c: c.data and c.data.startswith('finish_mount_'))
async def process_finish_mount(callback: types.CallbackQuery):
    """Бригада нажала кнопку МОНТАЖ ЗАВЕРШЁН"""
    order_number = callback.data.split('_')[2]

    await callback.message.edit_text(
        callback.message.text + f"\n\n✅ Монтаж завершён",
        parse_mode="Markdown"
    )

    # Получаем данные заказа (нужно будет брать из таблицы)
    # Пока остаток 0 для теста
    remaining = 0  # позже брать из таблицы
    client_chat_id = 123456789  # позже брать из таблицы

    # Кнопки для клиента
    kb_client = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="💳 ОПЛАТИТЬ", callback_data=f"pay_remaining_{order_number}"),
                InlineKeyboardButton(text="💰 НАЛИЧНЫМИ", callback_data=f"cash_{order_number}")
            ]
        ]
    )

    await bot.send_message(
        chat_id=client_chat_id,
        text=f"✅ **МОНТАЖ ВЫПОЛНЕН!**\n\nОстаток к оплате: {remaining} ₽",
        parse_mode="Markdown",
        reply_markup=kb_client
    )

    # Уведомление в общий чат
    await send_to_topic(
        "domostroy",
        f"✅ Монтаж заказа #{order_number} завершён"
    )

    await callback.answer()


# Обработчик выбора этажей
# @dp.message(OrderStates.choosing_floors)
# async def process_floors(message: Message, state: FSMContext):
#     text = message.text.strip()
#
#     if text in ["1", "2", "3"]:
#         floors = int(text)
#     else:
#         try:
#             floors = int(text)
#         except ValueError:
#             kb = ReplyKeyboardMarkup(
#                 keyboard=[
#                     [KeyboardButton(text="1"), KeyboardButton(text="2")],
#                     [KeyboardButton(text="3")]
#                 ],
#                 resize_keyboard=True
#             )
#             await update_question_message(
#                 chat_id=message.chat.id,
#                 text="❌ Введите число: 1, 2 или 3",
#                 reply_markup=kb,
#                 state=state
#             )
#             return
#
#     if floors not in [1, 2, 3]:
#         kb = ReplyKeyboardMarkup(
#             keyboard=[
#                 [KeyboardButton(text="1"), KeyboardButton(text="2")],
#                 [KeyboardButton(text="3")]
#             ],
#             resize_keyboard=True
#         )
#         await update_question_message(
#             chat_id=message.chat.id,
#             text="❌ Этажность может быть 1, 2 или 3",
#             reply_markup=kb,
#             state=state
#         )
#         return
#
#     await state.update_data(floors=floors)
#     data = await state.get_data()
#
#     # Сохраняем данные в таблицу
#     sheets.save_to_source_sheet(data)
#
#     # Даём таблице время на расчёт (2 секунды)
#     await asyncio.sleep(2)
#
#     # Забираем результат из таблицы
#     result = sheets.get_pile_result(
#         length=data.get('length'),
#         width=data.get('width')
#     )
#
#     # Таблица несущей способности свай
#     pile_capacity = {
#         57: 1.5, 76: 2.21, 89: 3.12,
#         108: 4.25, 133: 5.6, 159: 10.2
#     }
#
#     if result:
#         # Если получили результат - используем его
#         piles_info = {
#             'diameter': result['diameter'],
#             'count': result['count'],
#             'type': f"Свая {result['diameter']}×3000",
#             'length': 3000,
#             'blade_width': sheets.get_blade_width(result['diameter']),
#             'grid_count': result.get('grid_count', result['count']),
#             'min_needed': result.get('min_needed', result['count'])
#         }
#
#         # ✅ Берём вес ИЗ ТАБЛИЦЫ (он уже есть в result)
#         total_weight = result.get('total_weight', 0)
#
#         # 🗑️ Очищаем исходные данные, чтобы не мешали следующим
#         sheets.clear_source_sheet()
#
#         print(f"✅ Используем расчёт из таблицы: {result}")
#     else:
#         # Если таблица не ответила - используем старый расчёт (запасной вариант)
#         piles_info = calculate_piles(
#             material=data.get('material'),
#             floors=floors,
#             length=data.get('length'),
#             width=data.get('width')
#         )
#
#         # Рассчитываем вес для запасного варианта
#         area = data.get('length') * data.get('width')
#         material_load = {"Дерево": 0.8, "Каркас": 0.9, "Газобетон": 1.3, "Кирпич": 1.8}
#         floor_factor = {1: 1.0, 2: 1.8, 3: 2.5}
#         total_weight = area * material_load.get(data.get('material'), 1.0) * floor_factor.get(floors, 1.0) * 1.3
#
#         print(f"⚠️ Используем расчёт по умолчанию")
#
#     await state.update_data(piles_info=piles_info)
#
#     # ✅ Удаляем предыдущее сообщение с вопросом
#     question_data = await state.get_data()
#     last_question = question_data.get('last_question_msg_id')
#     if last_question:
#         try:
#             await bot.delete_message(message.chat.id, last_question)
#         except:
#             pass
#         await state.update_data(last_question_msg_id=None)
#
#     # ✅ Удаляем временное сообщение, если было
#     last_temp = question_data.get('last_temp_msg_id')
#     if last_temp:
#         try:
#             await bot.delete_message(message.chat.id, last_temp)
#         except:
#             pass
#         await state.update_data(last_temp_msg_id=None)
#
#     # Получаем несущую способность для выбранного диаметра
#     capacity = pile_capacity.get(piles_info['diameter'], 5.6)
#
#     # Показываем рекомендации
#     kb = ReplyKeyboardMarkup(
#         keyboard=[
#             [KeyboardButton(text="Взять рекомендованные")],
#             [KeyboardButton(text="Выбрать самому")]
#         ],
#         resize_keyboard=True
#     )
#
#     await message.answer(
#         f"<b>🔍 Отлично, расчёт завершён!</b>\n\n"
#
#         f"<blockquote><b>📋 Исходные данные:</b>\n"
#         f"{data.get('length')}×{data.get('width')} | {data.get('material')} | {floors} эт.</blockquote>\n\n"
#
#         f"<b>⚖️ Расчёт нагрузки</b>\n"
#         f"Вес дома: <b>{total_weight:.2f} т</b> (увеличен на 30% с учётом снеговой нагрузки)\n\n"
#
#         f"<b>🔩 Рекомендация по свае</b>\n"
#         f"<blockquote>Диаметр: <b>{piles_info['diameter']} мм</b> (несущая способность {capacity} т/сваю)\n"
#         f"Длина: <b>от 3000 мм</b>\n"
#         f"Лопасть: <b>{piles_info['blade_width']} мм</b>\n"
#         f"Стенка: <b>3,5 мм</b></blockquote>\n\n"
#
#         f"<b>📦 Комплектация</b>\n"
#         f"<blockquote>Сваи: <b>{piles_info['count']} шт</b>\n"
#         f"Оголовки 200×200: <b>{piles_info['count']} шт</b></blockquote>\n\n"
#
#         f"<b>🎯 Моё предложение</b>\n"
#         f"<blockquote><b>Свая винтовая {piles_info['diameter']}/3000/{piles_info['blade_width']} — {piles_info['count']} шт</b></blockquote>\n\n"
#
#         f"<b>📍 Учтены требования Новосибирской области</b>\n"
#         f"— глубина промерзания 2,2–2,5 м\n"
#         f"— запас по длине 3000 мм для надёжности\n\n"
#
#         f"<b>👇 Выберите шаг</b>\n"
#         f"— Выбрать самому (скорректировать)\n"
#         f"— Взять рекомендованные (оформить монтаж)",
#
#         reply_markup=kb,
#         parse_mode="HTML"
#     )
#     await state.set_state(OrderStates.showing_recommendations)


# Обработчик выбора после рекомендаций
@dp.message(OrderStates.showing_recommendations)
async def process_recommendations_choice(message: Message, state: FSMContext):
    choice = message.text
    data = await state.get_data()
    service_type = data.get('service')  # "Сваи с монтажом" или "Сваи без монтажа"
    user_id = message.from_user.id

    if choice == "Взять рекомендованные":
        # --- МАЯЧОК: Пользователь согласился с алгоритмом ---
        await track_event(user_id, "choice_recommended")

        piles_info = data.get('piles_info')
        await state.update_data(selected_piles=piles_info, pile_source='recommended')

        if service_type == "Сваи без монтажа":
            # Клавиатура для выбора доставка/самовывоз
            kb = ReplyKeyboardMarkup(
                keyboard=[
                    [KeyboardButton(text="🚚 Доставка")],
                    [KeyboardButton(text="🚗 Самовывоз")]
                ],
                resize_keyboard=True
            )
            await message.answer(
                "<b>📦 Как хотите получить сваи?</b>\n\n"
                "<b>🚚 Доставка</b> — привезём на ваш участок\n"
                "<b>🚗 Самовывоз</b> — заберёте самостоятельно с нашего склада",
                parse_mode="HTML",
                reply_markup=kb
            )
            await state.set_state(OrderStates.choosing_pickup_delivery)
        else:
            # Для свай с монтажом - выбор района
            kb = get_district_keyboard(DISTRICTS)
            # Отправляем сообщение и сохраняем его ID
            msg = await bot.send_message(
                chat_id=message.chat.id,
                text=(
                    "<b>👌 Отлично, рад, что вы доверяете моей рекомендации!</b>\n\n"
                    "<b>Теперь выберите ваш район в списке ниже.</b>\n"
                    "Все адреса и СНТ мы объединили по районам — это упрощает расчёт и ускоряет оформление.\n\n"
                    "<blockquote><b>📍 От вашего выбора зависит:</b>\n"
                    "— 🚜 время работы бурояма на объекте;\n"
                    "— 💸 стоимость доставки.</blockquote>"
                ),
                parse_mode="HTML",
                reply_markup=kb
            )
            await state.update_data(last_district_msg_id=msg.message_id)
            await state.set_state(OrderStates.entering_district)

    elif choice == "Выбрать самому":
        # --- МАЯЧОК: Пользователь пошел в ручной выбор ---
        await track_event(user_id, "choice_manual")

        diameters = [57, 76, 89, 108, 133]
        keyboard = []
        row = []
        for i, d in enumerate(diameters):
            row.append(KeyboardButton(text=f"{d} мм"))
            if len(row) == 2 or i == len(diameters) - 1:
                keyboard.append(row)
                row = []
        kb = ReplyKeyboardMarkup(keyboard=keyboard, resize_keyboard=True, one_time_keyboard=True)
        await message.answer(
            "<b>🔩 ВЫБОР СВАЙ</b>\n\nВыберите диаметр сваи:",
            reply_markup=kb,
            parse_mode="HTML"
        )
        await state.set_state(OrderStates.choosing_pile_diameter)
    else:
        await message.answer("Пожалуйста, выберите действие из меню")


@dp.message(OrderStates.choosing_pickup_delivery)
async def process_pickup_delivery(message: Message, state: FSMContext):
    choice = message.text.strip()
    if choice == "🚚 Доставка":
        await state.update_data(delivery_type="delivery")

        # ✅ ФОТО ДЛЯ ДОСТАВКИ ЧЕРЕЗ send_cached_photo
        kb = get_district_keyboard(DISTRICTS)
        await send_cached_photo(
            chat_id=message.chat.id,
            url="https://i.ibb.co/5W8jSyMs/photo-2026-03-11-09-59-52.jpg",  # фото доставки
            caption="<b>📍 Выберите район доставки:</b>",
            reply_markup=kb
        )
        await state.set_state(OrderStates.entering_district)

    elif choice == "🚗 Самовывоз":
        await state.update_data(delivery_type="samovyvoz")

        # ✅ ФОТО ДЛЯ САМОВЫВОЗА ЧЕРЕЗ send_cached_photo
        await send_cached_photo(
            chat_id=message.chat.id,
            url="https://i.ibb.co/hFy5mQfM/photo-2026-03-11-10-02-53.jpg",  # фото самовывоза
            caption=(
                "<b>👤 Введите ваше ФИО:</b>\n\n"
                "<blockquote>📍 Адрес склада: <b>г. Новосибирск, СПК Сибирский Авиатор 15А</b></blockquote>\n\n"
                "Это нужно для оформления документов"
            ),
            reply_markup=ReplyKeyboardRemove()
        )
        await state.set_state(OrderStates.entering_fio)
    else:
        await message.answer("Пожалуйста, выберите вариант из меню")


# @dp.message(OrderStates.choosing_pile_diameter)
# async def process_pile_diameter(message: Message, state: FSMContext):
#     # ✅ ЕСЛИ ЭТО /start - ПЕРЕДАЕМ В ОСНОВНОЙ ОБРАБОТЧИК
#     if message.text == "/start":
#         await start(message, state)
#         return
#
#     text = message.text.replace(' мм', '')
#     try:
#         diameter = int(text)
#         if diameter not in [57, 76, 89, 108, 133]:
#             raise ValueError
#     except ValueError:
#         await message.answer("❌ Пожалуйста, выберите диаметр из списка")
#         return
#     # ... остальной код
#
#     await state.update_data(selected_diameter=diameter)
#     available_lengths = [1500, 2000, 2500, 3000, 3500, 4000, 4500, 5000, 5500, 6000]
#     valid_lengths = []
#     for length in available_lengths:
#         price = sheets.get_pile_price(diameter, length)
#         if price:
#             valid_lengths.append(length)
#
#     if not valid_lengths:
#         await message.answer("❌ Для выбранного диаметра нет доступных длин.\nПопробуйте другой диаметр:",
#             reply_markup=ReplyKeyboardMarkup(
#                 keyboard=[[KeyboardButton(text="57 мм"), KeyboardButton(text="76 мм")],
#                           [KeyboardButton(text="89 мм"), KeyboardButton(text="108 мм")],
#                           [KeyboardButton(text="133 мм")]],
#                 resize_keyboard=True
#             )
#         )
#         await state.set_state(OrderStates.choosing_pile_diameter)
#         return
#
#     keyboard = []
#     row = []
#     for i, length in enumerate(valid_lengths):
#         length_m = length / 1000
#         row.append(KeyboardButton(text=f"{length_m} м"))
#         if len(row) == 2 or i == len(valid_lengths) - 1:
#             keyboard.append(row)
#             row = []
#     kb = ReplyKeyboardMarkup(keyboard=keyboard, resize_keyboard=True, one_time_keyboard=True)
#
#     await update_question_message(
#         chat_id=message.chat.id,
#         text=f"🔩 Диаметр: {diameter} мм\n\nТеперь выберите длину сваи:",
#         reply_markup=kb,
#         state=state
#     )
#     await state.set_state(OrderStates.choosing_pile_length)


@dp.message(OrderStates.choosing_pile_length)
async def process_pile_length(message: Message, state: FSMContext):
    text = message.text.replace(' м', '')
    try:
        length_m = float(text)
        length = int(length_m * 1000)
    except ValueError:
        await message.answer("❌ Пожалуйста, выберите длину из списка")
        return

    data = await state.get_data()
    diameter = data.get('selected_diameter')
    price = sheets.get_pile_price(diameter, length)

    if not price:
        await message.answer("❌ Для выбранных параметров нет цены.\nПопробуйте другую длину.")
        return

    if diameter <= 57:
        pile_type = "Свая 57"
    elif diameter <= 76:
        pile_type = "Свая 76"
    elif diameter <= 89:
        pile_type = "Свая 89"
    elif diameter <= 108:
        pile_type = "Свая 108"
    else:
        pile_type = "Свая 133"

    pile_info = {
        'type': f"{pile_type}×{length}",
        'diameter': diameter,
        'length': length,
        'price_per_pile': price
    }
    await state.update_data(selected_pile_info=pile_info)

    kb = ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="✅ Подтвердить выбор")],
            [KeyboardButton(text="🔄 Выбрать другой диаметр")]
        ],
        resize_keyboard=True, one_time_keyboard=True
    )

    await update_question_message(
        chat_id=message.chat.id,
        text=f"<b>✅ ВЫБРАНА СВАЯ</b>\n\n"
             f"📏 Диаметр: <b>{diameter} мм</b>\n"
             f"📐 Длина: <b>{length / 1000} м</b>\n\n"
             f"Подтверждаете выбор?",
        reply_markup=kb,
        state=state
    )
    await state.set_state(OrderStates.pile_confirmation)


# @dp.message(OrderStates.pile_confirmation)
# async def process_pile_confirmation(message: Message, state: FSMContext):
#     if message.text == "✅ Подтвердить выбор":
#         data = await state.get_data()
#         selected_pile = data.get('selected_pile_info')
#         piles_info = calculate_piles(
#             material=data.get('material'),
#             floors=data.get('floors'),
#             length=data.get('length'),
#             width=data.get('width')
#         )
#         piles_info['type'] = selected_pile['type']
#         piles_info['diameter'] = selected_pile['diameter']
#         piles_info['length'] = selected_pile['length']
#
#         await state.update_data(
#             piles_info=piles_info,
#             pile_source='manual',
#             delivery_type="delivery"
#         )
#
#         keyboard = []
#         row = []
#         for i, district in enumerate(DISTRICTS):
#             row.append(KeyboardButton(text=district))
#             if len(row) == 2 or i == len(DISTRICTS) - 1:
#                 keyboard.append(row)
#                 row = []
#         kb = InlineKeyboardMarkup(inline_keyboard=keyboard)
#
#         await update_question_message(
#             chat_id=message.chat.id,
#             text="👌 Отлично, свая выбрана!\n\n"
#                  "Теперь выберите ваш район в списке ниже.\n"
#                  "Все адреса и СНТ мы объединили по районам — это упрощает расчёт и ускоряет оформление.\n\n"
#                  "📍 От вашего выбора зависит:\n"
#                  "— 🚜 время работы бурояма на объекте;\n"
#                  "— 💰 стоимость доставки.",
#             reply_markup=kb,
#             state=state
#         )
#         await state.set_state(OrderStates.entering_district)
#
#     elif message.text == "🔄 Выбрать другой диаметр":
#         diameters = [57, 76, 89, 108, 133]
#         keyboard = []
#         row = []
#         for i, d in enumerate(diameters):
#             row.append(KeyboardButton(text=f"{d} мм"))
#             if len(row) == 2 or i == len(diameters) - 1:
#                 keyboard.append(row)
#                 row = []
#         kb = ReplyKeyboardMarkup(keyboard=keyboard, resize_keyboard=True)
#
#         await update_question_message(
#             chat_id=message.chat.id,
#             text="🔩 Выберите диаметр сваи:",
#             reply_markup=kb,
#             state=state
#         )
#         await state.set_state(OrderStates.choosing_pile_diameter)
#     else:
#         await message.answer("Пожалуйста, выберите действие из меню")




# # Обработчик выбора района (НОВЫЙ, С КНОПКАМИ)
# @dp.message(OrderStates.entering_district)
# async def process_district_selection(message: Message, state: FSMContext):
#     selected_district = message.text.strip()
#
#     # Удаляем предыдущее временное сообщение
#     data = await state.get_data()
#     last_temp = data.get('last_temp_msg_id')
#     if last_temp:
#         try:
#             await bot.delete_message(message.chat.id, last_temp)
#         except:
#             pass
#         await state.update_data(last_temp_msg_id=None)
#
#     if selected_district == "⬅️ Назад к выбору":
#         kb_back = ReplyKeyboardMarkup(
#             keyboard=[
#                 [KeyboardButton(text="🚗 Самовывоз")],
#                 [KeyboardButton(text="🚚 Доставка с монтажом")]
#             ],
#             resize_keyboard=True
#         )
#         await update_question_message(
#             chat_id=message.chat.id,
#             text="Как будем получать?",
#             reply_markup=kb_back,
#             state=state
#         )
#         await state.set_state(OrderStates.choosing_pickup_delivery)
#         return
#
#     if selected_district in DISTRICTS:
#         await state.update_data(district=selected_district)
#
#         # Отправляем новое временное
#         temp_msg = await send_temp_message(message.chat.id, f"✅ Выбран район: {selected_district}")
#         await state.update_data(last_temp_msg_id=temp_msg.message_id)
#
#         kb_dates = await get_dates_keyboard()
#         await update_question_message(
#             chat_id=message.chat.id,
#             text=(
#                 "<b>📅 Осталось пару вопросов — выбираем дату!</b>\n\n"
#
#                 "<b>Давайте подберём удобный день для монтажа.</b>\n"
#                 "Все даты в календаре реально свободны — вы никого не поджимаете.\n\n"
#
#                 "<blockquote><b>⚡ Как только вы выберете дату и проведёте оплату, я мгновенно:</b>\n"
#                 "— внесу вас в график монтажа;\n"
#                 "— оповещу производство и бригаду.</blockquote>\n\n"
#
#                 "<b>Менеджер свяжется для подтверждения заявки 🚀</b>\n\n"
#
#                 "Если хотите запланировать позже предложенного — нажмите <b>«Другое»</b>, и введите удобную для вас дату."
#             ),
#             reply_markup=kb_dates,
#             state=state
#         )
#         await state.set_state(OrderStates.choosing_date)
#     else:
#         keyboard = []
#         row = []
#         for i, district in enumerate(DISTRICTS):
#             row.append(KeyboardButton(text=district))
#             if len(row) == 2 or i == len(DISTRICTS) - 1:
#                 keyboard.append(row)
#                 row = []
#         keyboard.append([KeyboardButton(text="⬅️ Назад к выбору")])
#         kb = ReplyKeyboardMarkup(keyboard=keyboard, resize_keyboard=True)
#
#         await update_question_message(
#             chat_id=message.chat.id,
#             text="📍 **ВЫБЕРИТЕ РАЙОН**\n\nВыберите ваш район из списка:",
#             reply_markup=kb,
#             state=state
#         )


# Обработчик выбора даты
# @dp.message(OrderStates.choosing_date)
# async def process_date(message: Message, state: FSMContext):
#     selected_date = message.text.strip()
#     free_slots = sheets.get_free_slots(selected_date)
#
#     # Удаляем предыдущее временное сообщение (если есть)
#     data = await state.get_data()
#     last_temp = data.get('last_temp_msg_id')
#     if last_temp:
#         try:
#             await bot.delete_message(message.chat.id, last_temp)
#         except:
#             pass
#         await state.update_data(last_temp_msg_id=None)
#
#     if not free_slots:
#         await update_question_message(
#             chat_id=message.chat.id,
#             text="❌ На эту дату нет свободного времени.\nВыберите другую дату:",
#             reply_markup=await get_dates_keyboard(),
#             state=state
#         )
#         return
#
#     if selected_date == "📅 Другая дата":
#         await update_question_message(
#             chat_id=message.chat.id,
#             text="Введите дату в формате ДД.ММ.ГГГГ\nНапример: 15.06.2026",
#             reply_markup=ReplyKeyboardMarkup(keyboard=[[]], resize_keyboard=True),
#             state=state
#         )
#         await state.set_state(OrderStates.custom_date)
#         return
#
#     # Сохраняем выбранную дату
#     await state.update_data(selected_date=selected_date)
#
#     # ✅ Отправляем временное сообщение-подтверждение
#     temp_msg = await bot.send_message(
#         message.chat.id,
#         f"✅ Выбрана дата: {selected_date}",
#         parse_mode="HTML"
#     )
#     await state.update_data(last_temp_msg_id=temp_msg.message_id)
#
#     # Создаем клавиатуру с временем
#     kb_times = ReplyKeyboardMarkup(
#         keyboard=[[KeyboardButton(text=slot) for slot in free_slots[i:i + 2]]
#                   for i in range(0, len(free_slots), 2)],
#         resize_keyboard=True
#     )
#
#     # Отправляем вопрос про время (заменит предыдущий вопрос)
#     await update_question_message(
#         chat_id=message.chat.id,
#         text=(
#             f"<b>✅ Выбрана дата: {selected_date}</b>\n\n"
#
#             "<b>⏰ Отлично, переходим к выбору времени!</b>\n\n"
#
#             "<blockquote>Наши бригады работают в связке с производством — отгрузка материалов начинается не ранее 9:00. Поэтому монтаж возможен с учётом этого времени.</blockquote>\n\n"
#
#             "<b>Ниже — реально свободные слоты на выбранную вами дату.</b> Выберите удобный, и я сразу закреплю его за вами:\n\n"
#
#             "<b>👇 Какое время вам подходит?</b>"
#         ),
#         reply_markup=kb_times,
#         state=state
#     )
#     await state.set_state(OrderStates.entering_time)


# @dp.message(OrderStates.entering_time)
# async def process_time(message: Message, state: FSMContext):
#     selected_time = message.text.strip()
#     await state.update_data(selected_time=selected_time)
#
#     # ✅ Удаляем предыдущее временное сообщение (подтверждение даты)
#     data = await state.get_data()
#     last_temp = data.get('last_temp_msg_id')
#     if last_temp:
#         try:
#             await bot.delete_message(message.chat.id, last_temp)
#         except:
#             pass
#         await state.update_data(last_temp_msg_id=None)
#
#     # Отправляем новое временное сообщение (подтверждение времени)
#     temp_msg = await send_temp_message(message.chat.id, f"✅ Выбрано время: {selected_time}")
#     await state.update_data(last_temp_msg_id=temp_msg.message_id)
#
#     await update_question_message(
#         chat_id=message.chat.id,
#         text=(
#             "<b>Чтобы я мог обращаться к вам лично, а все документы были оформлены правильно, напишите, как вас зовут.</b>\n\n"
#
#             "<blockquote>Это займёт всего секунду, зато потом я буду знать, с кем имею удовольствие общаться 🤝</blockquote>"
#         ),
#         reply_markup=ReplyKeyboardMarkup(keyboard=[[]], resize_keyboard=True),
#         state=state
#     )
#     await state.set_state(OrderStates.entering_fio)


# async def ask_big_equipment(message: Message, state: FSMContext):
#     data = await state.get_data()
#     name = data.get('fio', '')
#     kb = ReplyKeyboardMarkup(
#         keyboard=[
#             [KeyboardButton(text="✅ Да, заезд свободный")],
#             [KeyboardButton(text="❌ Нет, есть ограничения")]
#         ],
#         resize_keyboard=True
#     )
#
#     await update_question_message(
#         chat_id=message.chat.id,
#         text=(
#             f"<b>Отлично, {name}! Мы уже у финиша 🏁</b>\n\n"
#
#             "<b><blockquote>‼️ Уточните важный технический момент:</blockquote></b>\n"
#             "сможет ли на ваш участок свободно заехать техника с габаритами <b>4,5 × 2,5 метра</b> (большой буроям)?\n\n"
#
#             "Это нужно, чтобы бригада сразу взяла нужное оборудование и не столкнулась с сюрпризами на месте.\n\n"
#
#             "<b>👇 Выберите вариант:</b>"
#         ),
#         reply_markup=kb,
#         state=state
#     )
#     await state.set_state(OrderStates.checking_big_equipment)

# Обработчик для большой техники (ИСПРАВЛЕН)
# @dp.message(OrderStates.checking_big_equipment)
# async def check_big_equipment(message: Message, state: FSMContext):
#     answer = message.text
#     data = await state.get_data()
#     name = data.get('fio', '')
#
#     if answer == "✅ Да, заезд свободный":
#         await state.update_data(equipment="big", generator_needed=False)
#         await ask_electricity(message, state)
#     elif answer == "❌ Нет, есть ограничения":
#         kb = ReplyKeyboardMarkup(
#             keyboard=[
#                 [KeyboardButton(text="✅ Да, маленький заедет")],
#                 [KeyboardButton(text="🔧 Нет, только ручной инструмент")]
#             ],
#             resize_keyboard=True
#         )
#         await update_question_message(
#             chat_id=message.chat.id,
#             text=f"{name}, спасибо, что уточнили! Значит, большой буроям не пройдёт — не страшно, у нас есть техника поменьше.\n\n"
#                  f"‼️Подскажите, сможет ли на участок заехать маленький гусеничный буроям? Его габариты — 2 × 1,5 метра, он пройдёт даже в узкие проёмы.\n\n"
#                  f"Если и он не проедет, мы привезём ручной сваекрут — монтаж займёт чуть больше времени, но качество останется таким же безупречным.\n\n"
#                  f"👇 Выберите вариант:",
#             reply_markup=kb,
#             state=state
#         )
#         await state.set_state(OrderStates.checking_small_equipment)
#     else:
#         await message.answer("Пожалуйста, выберите вариант из меню используя кнопки.")
#
# # Обработчик для маленькой техники (ИСПРАВЛЕН)
# @dp.message(OrderStates.checking_small_equipment)
# async def check_small_equipment(message: Message, state: FSMContext):
#     data = await state.get_data()
#     name = data.get('fio', '')
#     answer = message.text
#     pile_count = data.get('pile_count', 0)
#
#     if answer == "✅ Да, маленький заедет":
#         await state.update_data(equipment="small", generator_needed=False)
#         await ask_electricity(message, state)
#     elif answer == "🔧 Нет, только ручной инструмент":
#         if pile_count <= 16:
#             await state.update_data(equipment="manual", generator_needed=False)
#             await ask_electricity(message, state)
#         else:
#             await update_question_message(
#                 chat_id=message.chat.id,
#                 text=(
#                     "<b>❌ ЗАКАЗ НЕВОЗМОЖЕН</b>\n\n"
#
#                     "<blockquote>К сожалению, техника не может заехать на участок, "
#                     "а условия для ручного монтажа не соблюдены.</blockquote>\n\n"
#
#                     "<b>Свяжитесь с менеджером для поиска решения.</b>"
#                 ),
#                 reply_markup=ReplyKeyboardMarkup(
#                     keyboard=[[KeyboardButton(text="/start")]],
#                     resize_keyboard=True
#                 ),
#                 state=state
#             )
#             await state.clear()
#     else:
#         await message.answer("Пожалуйста, выберите вариант из меню используя кнопки.")
#
#
# # Вспомогательная функция для вопроса про электричество
# async def ask_electricity(message: Message, state: FSMContext):
#     data = await state.get_data()
#     name = data.get('fio', '')
#     kb = ReplyKeyboardMarkup(
#         keyboard=[
#             [KeyboardButton(text="⚡️ Да, электричество есть")],
#             [KeyboardButton(text="🔋 Нет")]
#         ],
#         resize_keyboard=True
#     )
#
#     await update_question_message(
#         chat_id=message.chat.id,
#         text=(
#             "<b>⚡ Теперь важный момент:</b> для завершающего этапа — приварки оголовков к сваям — нам понадобится сварочный аппарат.\n\n"
#
#             "<blockquote><b>Подскажите, есть ли на участке электричество?</b> Это нужно, чтобы зафиксировать оголовки надёжно и навсегда.</blockquote>\n\n"
#
#             "<b>👇 Выберите вариант:</b>"
#         ),
#         reply_markup=kb,
#         state=state
#     )
#     await state.set_state(OrderStates.checking_electricity)
#
#
# # Обработчик для электричества
# @dp.message(OrderStates.checking_electricity)
# async def check_electricity(message: Message, state: FSMContext):
#     answer = message.text
#
#     if answer == "⚡️ Да, электричество есть":
#         await state.update_data(electricity=True, generator_days=0)
#         await ask_geology(message, state)
#
#     elif answer == "🔋 Нет":
#         # Нужно узнать количество дней работы генератора
#         await message.answer(
#             "<b>⚡ ГЕНЕРАТОР</b>\n\n"
#
#             "<b>На сколько дней потребуется генератор?</b>\n"
#             "Введите число (обычно 1-3 дня):\n"
#             f"<pre>Например: 2</pre>",
#             reply_markup=ReplyKeyboardMarkup(
#                 keyboard=[[]],
#                 resize_keyboard=True
#             )
#         )
#         await state.set_state(OrderStates.generator_days_input)
#     else:
#         await message.answer("Пожалуйста, выберите вариант из меню")
#
#
# Обработчик ввода количества дней для генератора
@dp.message(OrderStates.generator_days_input)
async def process_generator_days(message: Message, state: FSMContext):
    try:
        days = int(message.text.strip())
        if days < 1:
            days = 1
        if days > 10:
            days = 10

        await state.update_data(electricity=False, generator_days=days)

        # Склонение слова "день"
        if days == 1:
            days_word = "день"
        elif days < 5:
            days_word = "дня"
        else:
            days_word = "дней"

        await message.answer(
            f"✅ Принято: генератор на {days} {days_word}"
        )
        await ask_geology(message, state)
    except ValueError:
        await message.answer("❌ Введите число (например: 2)")


# Вопрос про геологию
async def ask_geology(message: Message, state: FSMContext):
    data = await state.get_data()
    name = data.get('fio', '')
    kb = ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="📄 Да, есть заключение")],
            [KeyboardButton(text="🔍 Нет")]
        ],
        resize_keyboard=True
    )

    await update_question_message(
        chat_id=message.chat.id,
        text=(
            f"<b>✨ {name}, финальный вопрос!</b>\n\n"
            f"<b>📄 У вас есть геологическое заключение по грунтам?</b>\n"
            f"Это позволит нам предоставить вам расширенную гарантию на фундамент — с учётом всех особенностей вашего участка.\n\n"
            f"<blockquote><b>🔍 Если заключения нет — не проблема!</b>\n"
            f"Мы всё равно даём гарантию на сами сваи (качество изготовления) <b>до 50 лет</b> — мы уверены в своей продукции на 100%.</blockquote>\n\n"
            f"<b>👇 Выберите вариант:</b>"
        ),
        reply_markup=kb,
        state=state
    )
    await state.set_state(OrderStates.checking_geology)


async def load_drill_times():
    global DRILL_TIMES
    try:
        # Пытаемся открыть лист "Время бурояма"
        drill_sheet = sheets.spreadsheet.worksheet("Время бурояма")
        all_rows = drill_sheet.get_all_values()

        if len(all_rows) < 2:
            print("⚠️ Лист 'Время бурояма' пуст")
            return

        # Пропускаем заголовок
        for i, row in enumerate(all_rows[1:], start=2):
            if len(row) >= 6 and row[0].strip():
                district = row[0].strip()
                DRILL_TIMES[district] = {
                    "small_20": int(row[1]) if row[1] and row[1].isdigit() else 5,
                    "small_30": int(row[2]) if row[2] and row[2].isdigit() else 6,
                    "small_35": int(row[3]) if row[3] and row[3].isdigit() else 7,
                    "big_35": int(row[4]) if row[4] and row[4].isdigit() else 5,
                    "big_45": int(row[5]) if row[5] and row[5].isdigit() else 6,
                }
                print(f"  ✅ Загружен район: {district}")

        print(f"📊 Загружено время бурояма для {len(DRILL_TIMES)} районов")
    except Exception as e:
        print(f"❌ Не удалось загрузить время бурояма: {e}")
        # Если листа нет - создаём заглушку
        print("⚠️ Используются значения по умолчанию")


def get_drill_time(district: str, equipment: str, pile_count: int) -> int:
    """Возвращает время работы бурояма в часах"""
    if not district or district not in DRILL_TIMES:
        # Значения по умолчанию
        if equipment == "big":
            return 5 if pile_count <= 35 else 6
        else:  # small
            if pile_count <= 20:
                return 5
            elif pile_count <= 30:
                return 6
            else:
                return 7

    times = DRILL_TIMES[district]

    if equipment == "big":
        return times.get("big_35", 5) if pile_count <= 35 else times.get("big_45", 6)
    else:  # small
        if pile_count <= 20:
            return times.get("small_20", 5)
        elif pile_count <= 30:
            return times.get("small_30", 6)
        else:
            return times.get("small_35", 7)

# Обработчик для геологии (ИСПРАВЛЕН)
@dp.message(OrderStates.checking_geology)
async def check_geology(message: Message, state: FSMContext):
    answer = message.text
    if answer == "📄 Да, есть заключение":
        await state.update_data(geology=True)
        await message.answer("✅ Отлично! Сейчас проверим...")

        kb = ReplyKeyboardMarkup(
            keyboard=[
                [KeyboardButton(text="✅ Да, нужна")],
                [KeyboardButton(text="❌ Нет, спасибо")]
            ],
            resize_keyboard=True
        )
        #ХУЙ
        google_drive_link = "https://drive.google.com/file/d/10BpqRiankSN7oNaejZFU5SV0_CYL89P_/view"

        await update_question_message(
            chat_id=message.chat.id,
            text=(
                "<b>📄 ДОПОЛНИТЕЛЬНАЯ ГАРАНТИЯ</b>\n\n"
                "<blockquote>На основе вашей геологии мы можем предоставить "
                "<b><u>расширенную гарантию</u></b> на фундамент.</blockquote>\n\n"
                f"Посмотреть: <a href='{google_drive_link}'>📎 ДОКУМЕНТ</a>\n\n"
                "<b>❓ Нужна ли она вам?</b>"
            ),
            reply_markup=kb,
            state=state
        )
        await state.set_state(OrderStates.offering_guarantee)

    elif answer == "🔍 Нет":
        await state.update_data(geology=False, guarantee=False)
        await go_to_estimate(message, state)
    else:
        await message.answer("Пожалуйста, выберите вариант из меню используя кнопки.")


# Обработчик для дополнительной гарантии (ИСПРАВЛЕН)
@dp.message(OrderStates.offering_guarantee)
async def process_guarantee(message: Message, state: FSMContext):
    answer = message.text

    if answer == "✅ Да, нужна":
        await state.update_data(guarantee=True)
        await go_to_estimate(message, state)
    elif answer == "❌ Нет, спасибо":
        await state.update_data(guarantee=False)
        await go_to_estimate(message, state)
    else:
        await message.answer("Пожалуйста, выберите вариант из меню используя кнопки.")


@dp.message(OrderStates.data_confirmation)
async def process_data_confirmation(message: Message, state: FSMContext):
    if message.text == "✅ Да, всё верно":
        await go_to_estimate(message, state)
    elif message.text == "✏️ Нет, исправить":
        await message.answer(
            "Что именно хотите исправить?\n"
            "1. Имя\n"
            "2. Телефон\n"
            "3. Адрес\n\n"
            "Напишите, что меняем:"
        )
        # Здесь можно добавить логику редактирования
        await state.set_state(OrderStates.editing_data)
    else:
        await message.answer("Пожалуйста, выберите действие из меню")


# Переход к смете
# Переход к смете
async def go_to_estimate(message: Message, state: FSMContext):
    """Переход к формированию сметы"""

    # ✅ 1. ПОЛУЧАЕМ ДАННЫЕ И ПРОВЕРЯЕМ ТИП УСЛУГИ
    data = await state.get_data()
    service_type = data.get('service')

    # ✅ 2. ЕСЛИ ЭТО "СВАИ БЕЗ МОНТАЖА" - ОЧИЩАЕМ ВСЕ ДАННЫЕ ПО МОНТАЖУ
    if service_type == "Сваи без монтажа":
        print("🧹 Очищаем данные монтажа для заказа 'без монтажа'")
        await state.update_data(
            equipment=None,          # Тип техники
            electricity=True,        # Электричество (по умолчанию есть)
            generator_days=0,        # Дни генератора
            geology=False,           # Наличие геологии
            guarantee=False          # Наличие гарантии
        )
        # Важно: обновляем переменную data для использования в show_estimate
        data['equipment'] = None
        data['electricity'] = True
        data['generator_days'] = 0
        data['geology'] = False
        data['guarantee'] = False

    # Отправляем сообщение "Ожидайте..." (это уже было)
    waiting_msg = await message.answer(
        "<b>⏳ Ожидайте, считаем смету...</b>",
        parse_mode="HTML"
    )

    # Вызываем функцию формирования сметы, передавая обновленные data
    await show_estimate(message, state, data)

    # Удаляем сообщение "Ожидайте"
    await waiting_msg.delete()


# Обработчик перехода к смете
@dp.message(lambda message: message.text == "✅ Перейти к смете")
async def process_go_to_estimate(message: Message, state: FSMContext):
    # Получаем все данные
    data = await state.get_data()

    # Вызываем функцию формирования сметы
    await show_estimate(message, state, data)


async def show_estimate(message: Message, state: FSMContext, data: dict = None):
    if data is None:
        data = await state.get_data()

    piles_info = data.get('piles_info', {})
    if not piles_info or piles_info.get('count', 0) == 0:
        print(f"⚠️ Критично: piles_info пустой или count=0 у пользователя {message.from_user.id if hasattr(message, 'from_user') else 'unknown'}")
        await message.answer("❌ Ошибка расчёта. Пожалуйста, начните заново — /start")
        await state.clear()
        return

    piles_info = data.get('piles_info', {})
    fio = data.get('fio', 'Клиент')

    pile_count = piles_info.get('count', 0)
    diameter = piles_info.get('diameter', 108)
    length = piles_info.get('length', 2500)
    blade_width = sheets.get_blade_width(diameter)

    service_type = data.get('service')
    delivery_type = data.get('delivery_type')
    district = data.get('district')
    equipment = data.get('equipment')

    # === ТВОЯ ОРИГИНАЛЬНАЯ ЛОГИКА (НИЧЕГО НЕ УДАЛЯЛ) ===
    pile_price = sheets.get_pile_price(diameter, length) or 2500
    head_price = sheets.get_head_price(diameter) or 300

    pile_cost = pile_count * pile_price
    head_cost = pile_count * head_price

    # Монтаж
    montage_cost = 0
    montage_text = ""
    if service_type == "Сваи с монтажом" and delivery_type == "delivery":
        if equipment in ["big", "small"]:
            # ИСПОЛЬЗУЕМ НОВЫЕ ФУНКЦИИ С ТАБЛИЦЕЙ
            drill_hours = get_drill_time(district, equipment, pile_count)
            hour_price = DRILL_PRICE_PER_HOUR  # 3500 ₽/час
            montage_cost = drill_hours * hour_price
            tech_name = "Большой буроям" if equipment == "big" else "Маленький буроям"
            montage_text = f"— {tech_name}: {drill_hours} ч × {hour_price}₽/ч = {montage_cost} ₽"
        elif equipment == "manual":
            manual_price = MANUAL_PRICE_PER_PILE  # 2500 ₽/шт
            montage_cost = pile_count * manual_price
            montage_text = f"— Ручной монтаж: {pile_count} шт × {manual_price}₽/шт = {montage_cost} ₽"

    # Доставка
    delivery_cost = 0
    delivery_text = ""
    if delivery_type == "delivery" and district:
        total_weight = pile_count * get_pile_weight(diameter, length)
        total_weight_tons = total_weight / 1000
        length_m = length / 1000

        district_info = sheets.get_district_info(district)

        truck_capacity = {'gazelle': 1.5, 'board_4m_1_5t': 1.5, 'board_4m_3t': 3, 'board_6m_5t': 5, 'self_loader_5t': 5}
        truck_length = {'gazelle': 3.0, 'board_4m_1_5t': 4.0, 'board_4m_3t': 4.0, 'board_6m_5t': 6.0, 'self_loader_5t': 6.0}

        if district_info and district_info.get('delivery'):
            trucks = ['gazelle', 'board_4m_1_5t', 'board_4m_3t', 'board_6m_5t', 'self_loader_5t']
            for truck in trucks:
                if total_weight_tons <= truck_capacity[truck] and length_m <= truck_length[truck]:
                    delivery_cost = district_info['delivery'].get(truck, 5000)
                    break

        if delivery_cost > 0:
            delivery_text = f"• Доставка: {district} — {delivery_cost} ₽"
        else:
            delivery_text = f"• Доставка: {district} — нет подходящей машины"

    # Генератор
    generator_cost = 0
    generator_text = ""
    if not data.get('electricity', True):
        days = data.get('generator_days', 0)
        generator_cost = days * 3000
        if days == 1:
            days_word = "день"
        elif days < 5:
            days_word = "дня"
        else:
            days_word = "дней"
        generator_text = f"— Генератор: {days} {days_word} — {generator_cost} ₽"

    subtotal = pile_cost + head_cost + delivery_cost + montage_cost + generator_cost

    if service_type == "Сваи без монтажа":
        prepayment = subtotal
        prepayment_text = f"<b>К оплате (100%): {prepayment} ₽</b>"
    else:
        prepayment = int(subtotal * 0.7)
        prepayment_text = f"<b>Предоплата (70%): {prepayment} ₽</b>"

    await state.update_data(prepayment=prepayment, total_amount=subtotal)

    # === ЕДИНЫЙ КРАСИВЫЙ ТЕКСТ ===
    estimate_text = f"""
<b>📋 {fio}, вот ваша итоговая смета:</b>

<blockquote>Состав заказа:</blockquote>
• Сваи {diameter}/{length}/{blade_width} — {pile_count} шт — <b>{pile_cost} ₽</b>
• Оголовки {diameter}/200х200 — {pile_count} шт — <b>{head_cost} ₽</b>
"""

    if montage_text:
        estimate_text += f"{montage_text}\n"
    if delivery_text:
        estimate_text += f"{delivery_text}\n"
    if generator_text:
        estimate_text += f"{generator_text}\n"

    estimate_text += f"""
<blockquote>
<b>ИТОГО: {subtotal} ₽</b>
{prepayment_text}
</blockquote>

<b>Оплата проходит через ЮKassa:</b>
• Банковской картой (Visa, Mastercard, Мир)
• СБП (мгновенный перевод по номеру телефона)

<b>Если всё верно — нажмите «ОПЛАТИТЬ»</b>
"""

    # ===== ПРАВИЛЬНЫЙ ВЫБОР ДОГОВОРА =====
    if service_type == "Сваи с монтажом":
        contract_text = "📜 Договор поставки с монтажом"
        contract_url = "https://drive.google.com/file/d/10BpqRiankSN7oNaejZFU5SV0_CYL89P_/view"
    else:
        contract_text = "📜 Договор поставки без монтажа"
        contract_url = "https://drive.google.com/file/d/1EpbW9xu0uEuxrudtQOpKmYYVlYF3377w/view"

    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=contract_text, url=contract_url)],  # ← ДОГОВОР ОСТАЕТСЯ
            [InlineKeyboardButton(text="💳 ОПЛАТИТЬ", callback_data="estimate_pay")],
            [InlineKeyboardButton(text="🔧 ИЗМЕНИТЬ СМЕТУ", callback_data="estimate_change")]
        ]
    )

    await send_cached_photo(
        chat_id=message.chat.id,
        url="https://i.ibb.co/k2d9rTWL/photo-2026-03-14-11-10-57.jpg",
        caption=estimate_text.strip(),
        reply_markup=kb
    )

    await state.set_state(OrderStates.showing_estimate)


@dp.message(lambda message: message.successful_payment is not None)
async def successful_payment(message: Message, state: FSMContext):
    payment = message.successful_payment
    data = await state.get_data()

    order_number = data.get('order_number', random.randint(1000, 9999))

    # ===== 1. ОТПРАВЛЯЕМ TNPS =====
    buttons = []
    row = []
    for i in range(1, 11):
        row.append(InlineKeyboardButton(text=str(i), callback_data=f"tnps_{i}_{order_number}"))
        if i % 5 == 0:
            buttons.append(row)
            row = []

    tnps_kb = InlineKeyboardMarkup(inline_keyboard=buttons)

    await message.answer(
        "Мне очень важно знать ваше мнение — это помогает становиться лучше.\n"
        "Оцените, пожалуйста, мою работу по шкале от 0 до 10:\n\n"
        "<blockquote><b>😍 10</b> — «Лучшее решение, мне всё понравилось, очень удобно»\n"
        "<b>😐 5</b> — «В целом неплохо, но с менеджером как‑то спокойнее»\n"
        "<b>😕 1</b> — «Не вызвал доверия, хотелось бы живого общения»</blockquote>\n\n"
        "Всего один клик — и я буду знать, в правильном ли направлении развиваюсь 🚀",
        reply_markup=tnps_kb,
        parse_mode="HTML"
    )

    # ===== 2. ПОЛУЧАЕМ АДРЕС =====
    address = data.get('address', 'не указан')
    if data.get('delivery_type') == "samovyvoz":
        address = "г. Новосибирск, СПК Сибирский Авиатор 15А (самовывоз)"

    # ===== 3. ЗАПИСЫВАЕМ В ГРАФИК =====
    sheets.add_order_to_schedule(
        date=data.get('selected_date'),
        time=data.get('selected_time'),
        order_number=order_number,
        client=data.get('fio', 'не указан'),
        address=address,
        brigade="",
        driver="",
        status="оплачен",
        chat_id=message.chat.id,
        service_type=data.get('service'),
        delivery_type=data.get('delivery_type', 'delivery')
    )

    # ===== 4. ФОРМИРУЕМ ДАННЫЕ ДЛЯ УВЕДОМЛЕНИЙ =====
    order_data = {
        'order_number': order_number,
        'length': data.get('length'),
        'width': data.get('width'),
        'material': data.get('material'),
        'floors': data.get('floors'),
        'pile_count': data.get('piles_info', {}).get('count'),
        'pile_type': data.get('piles_info', {}).get('type'),
        'prepayment': data.get('prepayment', 0),
        'fio': data.get('fio', 'не указан'),
        'phone': data.get('phone', 'не указан'),
        'address': address,
        'district': data.get('district'),
        'selected_date': data.get('selected_date'),
        'selected_time': data.get('selected_time'),
        'equipment': data.get('equipment'),
        'electricity': data.get('electricity', True),
        'geology': data.get('geology', False),
        'service': data.get('service'),
        'delivery_type': data.get('delivery_type', 'delivery'),
    }

    # ===== 5. ОТПРАВЛЯЕМ УВЕДОМЛЕНИЯ =====
    await notify_chats_about_order(order_data, state)

    # ===== 6. ФИНАЛЬНОЕ СООБЩЕНИЕ С ДОГОВОРОМ =====
    service_type = data.get('service')
    delivery_type = data.get('delivery_type', 'delivery')

    # ===== ПРАВИЛЬНЫЙ ВЫБОР ДОГОВОРА ДЛЯ ФИНАЛЬНОГО СООБЩЕНИЯ =====
    if service_type == "Сваи с монтажом":
        contract_url = "https://drive.google.com/file/d/10BpqRiankSN7oNaejZFU5SV0_CYL89P_/view"
    else:
        contract_url = "https://drive.google.com/file/d/1EpbW9xu0uEuxrudtQOpKmYYVlYF3377w/view"

    inline_kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="📄 Образец договора", url=contract_url),
                InlineKeyboardButton(text="👨‍💼 Связаться с менеджером", url="https://t.me/vitaly1191")
            ]
        ]
    )

    if service_type == "Сваи с монтажом":
        schedule = "Я уже внёс вас в график монтажей — выбранная вами дата теперь официально закреплена за вами."
        details = f"📅 Дата монтажа: {data.get('selected_date')}\n⏰ Время: {data.get('selected_time')}\n📍 Адрес: {address}"
    elif service_type == "Сваи без монтажа" and delivery_type == "delivery":
        schedule = "Ваш заказ принят в обработку — сваи будут доставлены в выбранный вами день."
        details = f"📅 Дата доставки: {data.get('selected_date')}\n⏰ Время: {data.get('selected_time')}\n📍 Адрес доставки: {address}"
    else:
        schedule = "Ваш заказ готов к самовывозу. Мы сообщим Вам, когда вы сможете подъехать."
        details = f"📍 Адрес склада: г. Новосибирск, СПК Сибирский Авиатор 15А\n🚗 При въезде скажите номер заказа #{order_number}"

    final_text = (
        f"<b>✨ {data.get('fio', 'Уважаемый клиент')}, отличные новости!</b>\n\n"
        f"<b>✅ Оплата прошла успешно! Ваш номер заказа: #{order_number}</b>\n"
        f"{schedule}\n\n"
        f"<blockquote>{details}</blockquote>\n\n"
        f"<b>📄 Направляю образец договора</b> — вы можете ознакомиться с ним по ссылке ниже.\n\n"
        f"<b>💎 Благодарим за доверие!</b>"
    )

    await send_cached_photo(
        chat_id=message.chat.id,
        url="https://i.ibb.co/bRdhmMYL/photo-2026-03-05-13-53-28.jpg",
        caption=final_text,
        reply_markup=inline_kb
    )

    # Очищаем состояние
    await state.clear()


# Инициализация бота
bot = Bot(
    token=config.BOT_TOKEN,
    default=DefaultBotProperties(parse_mode="HTML")
)

@dp.pre_checkout_query()
async def pre_checkout_handler(pre_checkout_query: types.PreCheckoutQuery):
    await bot.answer_pre_checkout_query(pre_checkout_query.id, ok=True)

async def show_modified_estimate(message: Message, state: FSMContext):
    """Показывает изменённую смету красиво и корректно для обоих типов услуги"""
    data = await state.get_data()
    modified = data.get('modified_estimate', {})
    piles_info = data.get('piles_info', {})
    fio = data.get('fio', 'Клиент')

    pile_count = piles_info.get('count', 0)
    diameter = piles_info.get('diameter', 108)
    length = piles_info.get('length', 3000)
    blade_width = sheets.get_blade_width(diameter)

    service_type = data.get('service')
    delivery_type = data.get('delivery_type', 'delivery')
    district = data.get('district')
    equipment = data.get('equipment')

    # === Цены ===
    pile_price = sheets.get_pile_price(diameter, length) or 2500
    head_price = sheets.get_head_price(diameter) or 300

    pile_cost = pile_count * pile_price
    head_cost = 0 if modified.get('remove_heads') else pile_count * head_price

    # === Монтаж (только если услуга "Сваи с монтажом") ===
    montage_cost = 0
    montage_text = ""
    if service_type == "Сваи с монтажом" and delivery_type == "delivery":
        if not modified.get('remove_montage'):
            if equipment in ["big", "small"]:
                drill_hours = get_drill_time(district, equipment, pile_count)
                hour_price = DRILL_PRICE_PER_HOUR
                montage_cost = drill_hours * hour_price
                tech_name = "Большой буроям" if equipment == "big" else "Маленький буроям"
                montage_text = f"— {tech_name}: {drill_hours} ч × {hour_price}₽/ч = {montage_cost} ₽"
            elif equipment == "manual":
                manual_price = MANUAL_PRICE_PER_PILE
                montage_cost = pile_count * manual_price
                montage_text = f"— Ручной монтаж: {pile_count} шт × {manual_price}₽/шт = {montage_cost} ₽"

    # === Доставка ===
    delivery_cost = 0
    if delivery_type == "delivery" and not modified.get('remove_delivery') and district:
        district_info = sheets.get_district_info(district)
        if district_info and district_info.get('delivery'):
            delivery_cost = district_info['delivery'].get('gazelle', 5000)

    # === Генератор ===
    generator_cost = 0
    if not data.get('electricity', True) and not modified.get('remove_generator'):
        generator_cost = data.get('generator_days', 0) * 3000

    # Итог
    total = pile_cost + head_cost + montage_cost + delivery_cost + generator_cost

    if service_type == "Сваи без монтажа":
        prepayment = total
        prepayment_text = f"<b>К оплате: {prepayment} ₽</b>"
    else:
        prepayment = int(total * 0.7)
        prepayment_text = f"<b>Предоплата 70%: {prepayment} ₽</b>"

    await state.update_data(prepayment=prepayment, total_amount=total)

    # === Формируем красивый текст ===
    text = f"""
<b>📋 {fio}, вот ваша изменённая смета:</b>

<blockquote>Состав заказа:</blockquote>
• Сваи {diameter}×{length}/{blade_width} — {pile_count} шт — <b>{pile_cost} ₽</b>
"""

    if head_cost > 0:
        text += f"• Оголовки — {pile_count} шт — <b>{head_cost} ₽</b>\n"
    else:
        text += "• Оголовки — <b>убраны</b>\n"

    # Монтаж показываем ТОЛЬКО если есть текст
    if montage_text:
        text += f"{montage_text}\n"

    # Доставка / Самовывоз
    if delivery_type == "delivery":
        if delivery_cost > 0:
            text += f"• Доставка — <b>{delivery_cost} ₽</b>\n"
        else:
            text += "• Доставка — <b>убрана</b>\n"
    else:
        text += "• Самовывоз\n"

    # Генератор
    if not data.get('electricity', True):
        if generator_cost > 0:
            text += f"• Генератор — <b>{generator_cost} ₽</b>\n"
        else:
            text += "• Генератор — <b>убран</b>\n"

    text += f"""
<blockquote>
<b>ИТОГО: {total} ₽</b>
{prepayment_text}
</blockquote>

<b>Оплата проходит через ЮKassa:</b>
• Банковской картой (Visa, Mastercard, Мир)
• СБП (мгновенный перевод по номеру телефона)
"""

    # ===== ПРАВИЛЬНЫЙ ВЫБОР ДОГОВОРА =====
    if service_type == "Сваи с монтажом":
        contract_text = "📜 Договор поставки с монтажом"
        contract_url = "https://drive.google.com/file/d/10BpqRiankSN7oNaejZFU5SV0_CYL89P_/view"
    else:
        contract_text = "📜 Договор поставки без монтажа"
        contract_url = "https://drive.google.com/file/d/1EpbW9xu0uEuxrudtQOpKmYYVlYF3377w/view"

    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=contract_text, url=contract_url)],
            [InlineKeyboardButton(text="💳 ОПЛАТИТЬ", callback_data="estimate_pay")],
            [InlineKeyboardButton(text="🔧 ИЗМЕНИТЬ СМЕТУ", callback_data="estimate_change")]
        ]
    )

    await send_cached_photo(
        chat_id=message.chat.id,
        url="https://i.ibb.co/k2d9rTWL/photo-2026-03-14-11-10-57.jpg",
        caption=text.strip(),
        reply_markup=kb
    )

    await state.set_state(OrderStates.showing_estimate)


        # ===== ПЛАНИРОВАНИЕ ОТЛОЖЕННЫХ УВЕДОМЛЕНИЙ =====
        # try:
        #     mount_date_str = data.get('selected_date')
        #     mount_time_str = data.get('selected_time', '10:00')
        #
        #     # Парсим дату и время
        #     mount_datetime = datetime.strptime(f"{mount_date_str} {mount_time_str}", "%d.%m.%Y %H:%M")
        #
        #     # Устанавливаем часовой пояс (Новосибирск)
        #     local_tz = pytz.timezone('Asia/Novosibirsk')
        #     mount_datetime = local_tz.localize(mount_datetime)
        #
        #     now = datetime.now(local_tz)
        #
        #     # 1. За день до монтажа (через 1 минуту)
        #     day_before = datetime.now(local_tz) + timedelta(minutes=1)
        #
        #     if day_before > now:
        #         scheduler.add_job(
        #             notify_day_before,
        #             trigger=DateTrigger(run_date=day_before),
        #             args=[order_data],
        #             id=f"day_before_{order_number}",
        #             replace_existing=True
        #         )
        #         print(f"✅ Запланировано уведомление за день для #{order_number} на {day_before}")
        #
        #     # 2. Утро дня монтажа (через 2 минуты)
        #     morning = datetime.now(local_tz) + timedelta(minutes=2)
        #
        #     if morning > now:
        #         scheduler.add_job(
        #             notify_morning_check,
        #             trigger=DateTrigger(run_date=morning),
        #             args=[order_data],
        #             id=f"morning_{order_number}",
        #             replace_existing=True
        #         )
        #         print(f"✅ Запланировано утреннее уведомление для #{order_number} на {morning}")
        #
        # except Exception as e:
        #     print(f"❌ Ошибка при планировании уведомлений: {e}")
        # ===== КОНЕЦ ПЛАНИРОВАНИЯ =====

        # Отправляем уведомления во все чаты (кроме клиента)

@dp.callback_query(lambda c: c.data == "back_to_start")
async def back_to_start_callback(callback: types.CallbackQuery, state: FSMContext):
    await callback.answer()
    await state.clear()
    await start(callback.message, state)

@dp.callback_query(lambda c: c.data and c.data.startswith('contacted_'))
async def process_contacted(callback: types.CallbackQuery):
    """Бригада сообщила, что связалась с клиентом"""
    order_number = callback.data.split('_')[1]

    await callback.message.edit_text(
        callback.message.text + f"\n\n✅ Бригада связалась с клиентом",
        parse_mode="Markdown"
    )

    # Уведомление в ДОМОСТРОЙ
    await send_to_topic(
        "domostroy",
        f"📞 Бригада связалась с клиентом по заказу #{order_number}"
    )

    # Получаем chat_id клиента из таблицы
    client_chat_id = sheets.get_client_chat_id(order_number)

    if client_chat_id:
        try:
            await bot.send_message(
                chat_id=client_chat_id,
                text="✅ **ХОРОШИЕ НОВОСТИ!**\n\n"
                     "Монтажная бригада связалась с нами и уже скоро вам позвонит.\n"
                     "Приносим извинения за задержку.",
                parse_mode="Markdown"
            )
            print(f"✅ Уведомление клиенту о звонке отправлено #{order_number}")
        except Exception as e:
            print(f"❌ Ошибка уведомления клиента #{order_number}: {e}")
    else:
        print(f"❌ Не найден chat_id для заказа #{order_number}")

    await callback.answer()


# Обработчик для кнопки "ВЗЯТЬ ЗАЯВКУ" (монтажники)
@dp.callback_query(lambda c: c.data and c.data.startswith('take_order_'))
async def process_take_order(callback: types.CallbackQuery):
    order_number = callback.data.split('_')[2]

    # Получаем имя бригадира
    brigade_name = callback.from_user.full_name

    await callback.message.edit_text(
        callback.message.text + f"\n\n✅ Заявка взята бригадой: {brigade_name}",
        parse_mode="Markdown"
    )
    # Обновляем бригаду в таблице
    sheets.update_order_brigade(order_number, brigade_name)

    # Отправляем уведомление в ДОМОСТРОЙ (общий чат)
    await send_to_topic(
        "domostroy",
        f"👷 Заявка #{order_number} взята бригадой {brigade_name}"
    )

    await callback.answer(f"Вы взяли заявку #{order_number}")


async def ask_district(message: Message, state: FSMContext):
    """Запрашивает район для доставки"""
    # Создаем клавиатуру с районами
    keyboard = []
    row = []
    for i, district in enumerate(DISTRICTS):
        row.append(KeyboardButton(text=district))
        if len(row) == 2 or i == len(DISTRICTS) - 1:
            keyboard.append(row)
            row = []

    keyboard.append([KeyboardButton(text="⬅️ Назад к выбору")])

    kb = ReplyKeyboardMarkup(
        keyboard=keyboard,
        resize_keyboard=True
    )

    await message.answer(
        "📍 Выберите ваш район из списка:",
        reply_markup=kb
    )
    await state.set_state(OrderStates.entering_district)


async def get_dates_keyboard():
    """Создает клавиатуру с доступными датами (следующие 7 дней)"""
    from datetime import datetime, timedelta

    dates = []
    for i in range(7):
        date = (datetime.now() + timedelta(days=i)).strftime("%d.%m.%Y")
        dates.append(date)

    # Разбиваем по 2 в ряд
    keyboard = []
    row = []
    for i, date in enumerate(dates):
        row.append(KeyboardButton(text=date))
        if len(row) == 2 or i == len(dates) - 1:
            keyboard.append(row)
            row = []
    # Добавляем кнопку "Другая дата"
    keyboard.append([KeyboardButton(text="📅 Другая дата")])

    return ReplyKeyboardMarkup(keyboard=keyboard, resize_keyboard=True)

# Обработчик для кнопки "ЕСТЬ В НАЛИЧИИ" (поставщики)
# Обработчик для кнопки "ЕСТЬ В НАЛИЧИИ" (поставщики)
@dp.callback_query(lambda c: c.data and c.data.startswith('in_stock_'))
async def process_in_stock(callback: types.CallbackQuery):
    order_number = callback.data.split('_')[2]

    await callback.message.edit_text(
        callback.message.text + f"\n\n✅ Подтверждено: есть в наличии",
        parse_mode="Markdown"
    )

    # Добавляем кнопку "ГОТОВО" прямо под сообщением
    kb_ready = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="✅ ГОТОВО", callback_data=f"ready_{order_number}")]
        ]
    )

    await callback.message.edit_reply_markup(reply_markup=kb_ready)

    # Отправляем уведомление в доставку (предварительное)
    await send_to_topic(
        "dostavka",
        f"🚚 Заказ #{order_number} скоро будет готов к отгрузке (есть в наличии)"
    )

    # Отправляем уведомление в ДОМОСТРОЙ (общий чат)
    await send_to_topic(
        "domostroy",
        f"📦 Заказ #{order_number} — есть в наличии, ожидает подтверждения готовности"
    )

    await callback.answer("Статус обновлен")


# Обработчик для кнопки "ИЗГОТОВИМ" (поставщики)
@dp.callback_query(lambda c: c.data and c.data.startswith('manufacture_'))
async def process_manufacture(callback: types.CallbackQuery, state: FSMContext):
    order_number = callback.data.split('_')[1]

    # Обновляем сообщение
    await callback.message.edit_text(
        callback.message.text + f"\n\n🏭 Заказ взят в производство",
        parse_mode="Markdown"
    )

    # Обновляем статус в таблице
    sheets.update_order_status(order_number, "в производстве")

    # Сохраняем номер заказа в состоянии для дальнейшего отслеживания
    await state.update_data(
        manufacturing_order=order_number,
        manufacturing_chat_id=callback.message.chat.id,
        manufacturing_message_id=callback.message.message_id
    )

    # Отправляем уведомление в ДОМОСТРОЙ
    await send_to_topic(
        "domostroy",
        f"🏭 Заказ #{order_number} взят в производство"
    )

    # Добавляем кнопку "ГОТОВО" под сообщением
    kb_ready = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="✅ ГОТОВО", callback_data=f"ready_{order_number}")]
        ]
    )

    await callback.message.edit_reply_markup(reply_markup=kb_ready)
    await callback.answer("Заказ в работе")


# Обработчик для кнопки "ГОТОВО" (когда изготовили)
@dp.callback_query(lambda c: c.data and c.data.startswith('ready_'))
async def process_ready(callback: types.CallbackQuery):
    order_number = callback.data.split('_')[1]

    # Получаем данные заказа из таблицы или state
    # Пока используем то, что есть в сообщении
    await callback.message.edit_text(
        callback.message.text + f"\n\n✅ Заказ изготовлен",
        parse_mode="Markdown"
    )

    # Обновляем статус в таблице
    sheets.update_order_status(order_number, "готов к отгрузке")

    # Кнопка для водителя
    kb_driver = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="🚚 ВЗЯТЬ ДОСТАВКУ", callback_data=f"take_delivery_{order_number}")]
        ]
    )

    # Получаем адрес объекта из таблицы (нужно добавить функцию)
    # Пока заглушка
    client_address = sheets.get_order_address(order_number) or "адрес не указан"

    # Отправляем уведомление в доставку с ПОЛНЫМИ адресами
    await send_to_topic(
        "dostavka",
        f"🚚 **ЗАДАНИЕ НА ДОСТАВКУ #{order_number}**\n\n"
        f"📦 **ЗАБРАТЬ:**\n{WAREHOUSE_ADDRESS}\n\n"
        f"📍 **ДОСТАВИТЬ:**\n{client_address}\n\n"
        f"Нажмите кнопку, чтобы назначить водителя:",
        kb_driver
    )

    # Отправляем уведомление в ДОМОСТРОЙ
    await send_to_topic(
        "domostroy",
        f"📦 Заказ #{order_number} изготовлен, готов к отгрузке"
    )

    await callback.answer("Статус обновлен")


# Обработчик для кнопки "ВЗЯТЬ ДОСТАВКУ" (водители)
@dp.callback_query(lambda c: c.data and c.data.startswith('take_delivery_'))
async def process_take_delivery(callback: types.CallbackQuery):
    order_number = callback.data.split('_')[2]

    # Получаем имя водителя
    driver_name = callback.from_user.full_name

    # Обновляем сообщение
    await callback.message.edit_text(
        callback.message.text + f"\n\n✅ Доставку взял водитель: {driver_name}",
        parse_mode="Markdown"
    )

    # Записываем водителя в таблицу
    sheets.update_order_driver(order_number, driver_name)

    # Обновляем статус
    sheets.update_order_status(order_number, "водитель назначен")

    # Отправляем уведомление в ДОМОСТРОЙ
    await send_to_topic(
        "domostroy",
        f"🚚 Доставку заказа #{order_number} взял водитель {driver_name}"
    )

    # Добавляем кнопку "ВЫПОЛНЕНО" для водителя
    kb_done = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="✅ ВЫПОЛНЕНО", callback_data=f"delivery_done_{order_number}")]
        ]
    )

    await callback.message.edit_reply_markup(reply_markup=kb_done)
    await callback.answer(f"Вы взяли доставку #{order_number}")


# Обработчик для кнопки "ВЫПОЛНЕНО" (доставка выполнена)
@dp.callback_query(lambda c: c.data and c.data.startswith('delivery_done_'))
async def process_delivery_done(callback: types.CallbackQuery):
    order_number = callback.data.split('_')[2]

    await callback.message.edit_text(
        callback.message.text + f"\n\n✅ Доставка выполнена",
        parse_mode="Markdown"
    )

    # Обновляем статус в таблице
    sheets.update_order_status(order_number, "доставлено")

    # Отправляем уведомление в ДОМОСТРОЙ
    await send_to_topic(
        "domostroy",
        f"✅ Доставка заказа #{order_number} выполнена"
    )

    # Отправляем уведомление монтажникам (что материал на месте)
    await send_to_topic(
        "montagniki",
        f"📦 Материалы для заказа #{order_number} доставлены на объект"
    )

    await callback.answer("Доставка завершена")


# Обработчик для кнопки "ИЗГОТОВИМ" (поставщики)
@dp.callback_query(lambda c: c.data and c.data.startswith('manufacture_'))
async def process_manufacture(callback: types.CallbackQuery, state: FSMContext):
    order_number = callback.data.split('_')[1]

    # Обновляем сообщение
    await callback.message.edit_text(
        callback.message.text + f"\n\n🏭 Заказ взят в производство",
        parse_mode="Markdown"
    )

    # Обновляем статус в таблице
    sheets.update_order_status(order_number, "в производстве")

    # Сохраняем номер заказа в состоянии для дальнейшего отслеживания
    await state.update_data(
        manufacturing_order=order_number,
        manufacturing_chat_id=callback.message.chat.id,
        manufacturing_message_id=callback.message.message_id
    )

    # Отправляем уведомление в ДОМОСТРОЙ
    await send_to_topic(
        "domostroy",
        f"🏭 Заказ #{order_number} взят в производство"
    )

    # Добавляем кнопку "ГОТОВО" под сообщением
    kb_ready = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="✅ ГОТОВО", callback_data=f"ready_{order_number}")]
        ]
    )

    await callback.message.edit_reply_markup(reply_markup=kb_ready)
    await callback.answer("Заказ в работе")

# Обработчик для кнопки "ГОТОВО" (когда изготовили)
@dp.callback_query(lambda c: c.data and c.data.startswith('ready_'))
async def process_ready(callback: types.CallbackQuery):
    order_number = callback.data.split('_')[1]

    await callback.message.edit_text(
        callback.message.text + f"\n\n✅ Заказ изготовлен",
        parse_mode="Markdown"
    )

    # Обновляем статус в таблице
    sheets.update_order_status(order_number, "готов к отгрузке")

    # Отправляем уведомление в доставку
    await send_to_topic(
        "dostavka",
        f"🚚 Заказ #{order_number} готов к отгрузке (изготовлен)"
    )

    # Отправляем уведомление в ДОМОСТРОЙ
    await send_to_topic(
        "domostroy",
        f"📦 Заказ #{order_number} изготовлен, готов к отгрузке"
    )

    await callback.answer("Статус обновлен")

# Обработчик ввода ФИО
@dp.message(OrderStates.entering_fio)
async def process_fio(message: Message, state: FSMContext):
    fio = message.text.strip()

    if len(fio.split()) < 2:
        await message.answer("❌ Введите ФИО полностью (минимум имя и фамилия)")
        return

    await state.update_data(fio=fio)

    # ✅ Удаляем временное сообщение (например "✅ Выбрано время")
    await delete_temp_message(message.chat.id, state)

    await message.answer(
        f"<b>✨ Спасибо, {fio}! Очень приятно познакомиться.</b>\n\n"

        "<b>Укажите, пожалуйста, ваш номер телефона.</b> Он нужен, чтобы:\n"
        "— менеджер мог уточнить детали (если потребуется);\n"
        "— бригада или водитель связались с вами в день монтажа.\n\n"

        "<blockquote><b>🔒 Важно:</b> ваши данные никому не передаются на этом этапе. Они попадут только в заказ, если вы решите его оформить.</blockquote>\n"

        "<b>👇 Введите номер, и я сразу подготовлю итоговый расчёт</b>",
        parse_mode="HTML"  # ← ДОБАВЬ ЭТУ СТРОКУ
    )
    await state.set_state(OrderStates.entering_phone)


@dp.message(OrderStates.entering_address)
async def process_address(message: Message, state: FSMContext):
    # ✅ Удаляем временное сообщение (если было)
    await delete_temp_message(message.chat.id, state)

    address = message.text.strip()
    if len(address) < 5:
        await message.answer("❌ Введите корректный адрес (минимум 5 символов)")
        return

    await state.update_data(address=address)

    data = await state.get_data()
    service_type = data.get('service')

    if service_type == "Сваи без монтажа":
        # Доставка: сразу смета
        await go_to_estimate(message, state)
    else:
        # Монтаж: вопросы про технику
        name = data.get('fio', '')
        await update_question_message(
            chat_id=message.chat.id,
            text=(
                f"<b>Отлично, {name}! Мы уже у финиша 🏁</b>\n\n"
                "<b>‼️ Уточните важный технический момент:</b>\n"
                "сможет ли на ваш участок свободно заехать техника с габаритами <b>4,5 × 2,5 метра</b> (большой буроям)?\n\n"
                "Это нужно, чтобы бригада сразу взяла нужное оборудование и не столкнулась с сюрпризами на месте.\n\n"
                "<b>👇 Выберите вариант:</b>"
            ),
            reply_markup=get_equipment_keyboard(),
            state=state
        )
        # Не меняем state - ответ будет через callback


@dp.message(OrderStates.entering_phone)
async def process_phone(message: Message, state: FSMContext):
    # ✅ Удаляем временное сообщение (если было)
    await delete_temp_message(message.chat.id, state)

    phone = message.text.strip()
    if not any(c.isdigit() for c in phone):
        await message.answer("Введите корректный номер телефона")
        return

    await state.update_data(phone=phone)
    data = await state.get_data()
    service_type = data.get('service')
    delivery_type = data.get('delivery_type', 'delivery')
    fio = data.get('fio', '')

    if service_type == "Сваи без монтажа" and delivery_type == "samovyvoz":
        # Самовывоз: адрес не спрашиваем, сразу смета
        await go_to_estimate(message, state)
    else:
        # Для доставки или монтажа запрашиваем адрес
        if service_type == "Сваи без монтажа":
            # Текст для ДОСТАВКИ (без монтажа)
            prompt = (
                f"<b>Отлично, {fio}, спасибо! Осталось несколько штрихов.</b>\n\n"
                "<b>Укажите, пожалуйста, точный адрес доставки.</b>\n"
                "Он понадобится водителю, чтобы привезти сваи именно к вам без поисков и задержек.\n\n"
                "<blockquote><b>📍 Можете быть уверены:</b> как только заказ будет оформлен, все данные (адрес, имя и телефон) увидят только те, кто непосредственно везёт сваи.</blockquote>\n\n"
                "<b>👇 Введите адрес доставки (г. Новосибирск, ул. Пушкина, д. 17)</b>"
            )
        else:
            # Текст для МОНТАЖА (сваи с монтажом)
            prompt = (
                f"<b>Отлично, {fio}, спасибо! Осталось несколько штрихов.</b>\n\n"
                "<b>Укажите, пожалуйста, точный адрес объекта.</b>\n"
                "Он понадобится бригаде и водителю доставки, чтобы они приехали именно к вам без поисков и задержек.\n\n"
                "<blockquote><b>📍 Можете быть уверены:</b> как только заказ будет оформлен, все данные (адрес, имя и телефон) увидят только те, кто непосредственно везёт и монтирует конструкцию.</blockquote>\n\n"
                "<b>👇 Введите адрес объекта (г. Новосибирск, ул. Пушкина, д. 17)</b>"
            )

        await message.answer(
            prompt,
            parse_mode="HTML"
        )
        await state.set_state(OrderStates.entering_address)


# Тестовый обработчик для проверки отправки в топики
@dp.message(Command("test_chats"))
async def test_chats(message: Message):
    if message.chat.type != "private":
        return

    await send_to_topic("domostroy", "🔔 Тестовое уведомление в ДОМОСТРОЙ")
    await send_to_topic("montagniki", "👷 Тестовое уведомление монтажникам")
    await send_to_topic("postavshchiki", "🏭 Тестовое уведомление поставщикам")
    await send_to_topic("dostavka", "🚚 Тестовое уведомление доставке")

    await message.answer("✅ Тестовые уведомления отправлены во все чаты!")


async def init_sheets_background():
    """Фоновая загрузка таблиц с повышенной устойчивостью к сетевым проблемам"""
    await asyncio.sleep(3)  # даём боту спокойно стартовать


    print("📊 Начинаю фоновую загрузку Google Sheets...")

    loop = asyncio.get_event_loop()

    for attempt in range(3):  # 3 попытки
        try:
            print(f"🔄 Попытка подключения к Google Sheets #{attempt+1}")

            await loop.run_in_executor(None, lambda: sheets.set_spreadsheet(SPREADSHEET_URL))
            await asyncio.sleep(1)

            await loop.run_in_executor(None, lambda: sheets.set_calc_spreadsheet(CALC_SPREADSHEET_URL))

            await load_drill_times()

            global DISTRICTS
            DISTRICTS = await loop.run_in_executor(None, sheets.get_all_districts)

            print(f"✅ Google Sheets успешно загружены. Районов: {len(DISTRICTS)}")
            return


        except Exception as e:
            print(f"❌ Попытка {attempt+1} не удалась: {e}")
            if attempt < 2:
                await asyncio.sleep(5)  # пауза перед следующей попыткой
            else:
                print("⚠️ Не удалось загрузить Google Sheets после 3 попыток. Бот продолжит работу, но расчёты могут быть недоступны.")


async def init_sheets_background():
    # Увеличиваем задержку до 10 секунд.
    # Это КРИТИЧЕСКИ важно для Amnezia/Warp, чтобы сначала стабилизировался канал с Telegram
    await asyncio.sleep(10)
    print("📊 Канал с ТГ стабилен. Начинаю фоновую загрузку Google Sheets...")

    loop = asyncio.get_event_loop()
    try:
        # Открываем таблицы медленно, по очереди
        await loop.run_in_executor(None, lambda: sheets.set_spreadsheet(SPREADSHEET_URL))
        await asyncio.sleep(1)  # Пауза между запросами к разным API
        await loop.run_in_executor(None, lambda: sheets.set_calc_spreadsheet(CALC_SPREADSHEET_URL))

        global DISTRICTS
        data = await loop.run_in_executor(None, sheets.get_all_districts)
        DISTRICTS = data if data else []
        print(f"✅ Данные загружены. Районов: {len(DISTRICTS)}")

    except Exception as e:
        print(f"❌ Ошибка сети через прокси: {e}")


async def main():
    print("🚀 Запуск бота...")

    print(f"✅ BOT_TOKEN успешно загружен ({len(config.BOT_TOKEN)} символов)")
    if not YOOKASSA_SECRET_KEY:
        print("⚠️ Внимание: YOOKASSA_SECRET_KEY не задан!")

    # Очищаем очередь обновлений при старте (очень важно!)
    await bot.delete_webhook(drop_pending_updates=True)

    try:
        redis_queue.flushdb()
        print("✅ Redis очередь очищена")
    except Exception as e:
        print(f"⚠️ Не удалось очистить Redis: {e}")

    # Фоновая загрузка таблиц (не блокирует запуск)
    asyncio.create_task(init_sheets_background())

    print("🤖 Бот запущен и ожидает сообщений...")

    try:
        await dp.start_polling(
            bot,
            skip_updates=True,           # обязательно
            allowed_updates=["message", "callback_query"]
        )
    except Exception as e:
        print(f"❌ Критическая ошибка поллинга: {e}")
    finally:
        await bot.session.close()


if __name__ == "__main__":
    asyncio.run(main())
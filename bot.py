import asyncio
import sqlite3
from datetime import datetime, timedelta
import logging
from difflib import SequenceMatcher
import random
import os
import uuid
import aiohttp

from aiogram import Bot, Dispatcher, F, Router
from aiogram.filters import Command
from aiogram.types import Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton, FSInputFile, ReplyKeyboardMarkup, KeyboardButton
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup

# ==================== CONFIG ====================
from config import BOT_TOKEN, BOT_USERNAME, YOOKASSA_SHOP_ID, YOOKASSA_SECRET_KEY, DB_PATH, FREE_LIMIT

# ==================== LOGGING ====================
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# faster_whisper будет установлен позже
try:
    from faster_whisper import WhisperModel
    WHISPER_AVAILABLE = True
except ImportError:
    WHISPER_AVAILABLE = False
    logger.warning("Faster-Whisper not installed. Voice transcription disabled.")

# ==================== BOT INITIALIZATION ====================
bot = Bot(token=BOT_TOKEN)

# Whisper model (faster-whisper, локально)
WHISPER_MODEL = None  # Инициализируется при старте

# ==================== DATABASE ====================
def init_db():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    
    # Users table
    c.execute('''CREATE TABLE IF NOT EXISTS users (
        user_id INTEGER PRIMARY KEY,
        freeze_mode INTEGER DEFAULT 7,
        is_premium INTEGER DEFAULT 0,
        premium_until TIMESTAMP,
        ideas_count INTEGER DEFAULT 0,
        city TEXT,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )''')
    
    # Ideas table
    c.execute('''CREATE TABLE IF NOT EXISTS ideas (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER,
        content TEXT,
        idea_type TEXT,
        file_id TEXT,
        file_path TEXT,
        source TEXT,
        frozen_until TIMESTAMP,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        opened_count INTEGER DEFAULT 0,
        is_valuable INTEGER DEFAULT 0,
        day_of_week TEXT,
        time_of_day TEXT,
        weather TEXT,
        FOREIGN KEY (user_id) REFERENCES users (user_id)
    )''')
    
    # Deleted ideas stats
    c.execute('''CREATE TABLE IF NOT EXISTS deleted_ideas (
        user_id INTEGER,
        deleted_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        FOREIGN KEY (user_id) REFERENCES users (user_id)
    )''')
    
    # Payments table
    c.execute('''CREATE TABLE IF NOT EXISTS payments (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER,
        payment_id TEXT UNIQUE,
        amount REAL,
        plan_type TEXT,
        status TEXT DEFAULT 'pending',
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        paid_at TIMESTAMP,
        FOREIGN KEY (user_id) REFERENCES users (user_id)
    )''')
    
    conn.commit()
    conn.close()

def get_user(user_id):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT * FROM users WHERE user_id = ?", (user_id,))
    user = c.fetchone()
    if not user:
        c.execute("INSERT INTO users (user_id) VALUES (?)", (user_id,))
        conn.commit()
        c.execute("SELECT * FROM users WHERE user_id = ?", (user_id,))
        user = c.fetchone()
    
    # Check if premium expired
    if user[2] == 1 and user[3]:
        if datetime.fromisoformat(user[3]) < datetime.now():
            c.execute("UPDATE users SET is_premium = 0 WHERE user_id = ?", (user_id,))
            conn.commit()
            c.execute("SELECT * FROM users WHERE user_id = ?", (user_id,))
            user = c.fetchone()
    
    conn.close()
    return user

def is_premium(user_id):
    user = get_user(user_id)
    return user[2] == 1

def save_idea(user_id, content, idea_type, file_id=None, file_path=None, source="direct", weather=None):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    
    user = get_user(user_id)
    freeze_days = user[1]
    
    frozen_until = datetime.now() + timedelta(days=freeze_days)
    now = datetime.now()
    
    c.execute('''INSERT INTO ideas 
        (user_id, content, idea_type, file_id, file_path, source, frozen_until, day_of_week, time_of_day, weather)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)''',
        (user_id, content, idea_type, file_id, file_path, source, frozen_until,
         now.strftime("%A"), now.strftime("%H:%M"), weather))
    
    c.execute("UPDATE users SET ideas_count = ideas_count + 1 WHERE user_id = ?", (user_id,))
    
    conn.commit()
    conn.close()

def check_similarity(user_id, new_content):
    """Check if similar idea exists"""
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT id, content, created_at FROM ideas WHERE user_id = ? ORDER BY created_at DESC LIMIT 50", 
              (user_id,))
    ideas = c.fetchall()
    conn.close()
    
    for idea_id, old_content, created_at in ideas:
        if old_content and new_content:
            similarity = SequenceMatcher(None, new_content.lower(), old_content.lower()).ratio()
            if similarity > 0.7:
                return (idea_id, old_content, created_at)
    return None

def get_thawed_ideas(user_id):
    """Get ideas that are ready to be viewed"""
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute('''SELECT id, content, idea_type, file_id, created_at, opened_count, day_of_week, time_of_day, weather 
                 FROM ideas 
                 WHERE user_id = ? AND frozen_until <= datetime('now')
                 ORDER BY created_at DESC''', (user_id,))
    ideas = c.fetchall()
    conn.close()
    return ideas

def get_old_ideas(user_id, days=30):
    """Get ideas older than N days for dump"""
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    cutoff = datetime.now() - timedelta(days=days)
    c.execute('''SELECT id, content, idea_type, created_at, opened_count 
                 FROM ideas 
                 WHERE user_id = ? AND created_at <= ?
                 ORDER BY created_at ASC''', (user_id, cutoff))
    ideas = c.fetchall()
    conn.close()
    return ideas

def get_stats(user_id):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    
    c.execute("SELECT ideas_count FROM users WHERE user_id = ?", (user_id,))
    total = c.fetchone()[0]
    
    c.execute("SELECT COUNT(*) FROM ideas WHERE user_id = ?", (user_id,))
    alive = c.fetchone()[0]
    
    c.execute("SELECT COUNT(*) FROM deleted_ideas WHERE user_id = ?", (user_id,))
    deleted = c.fetchone()[0]
    
    c.execute("SELECT COUNT(*) FROM ideas WHERE user_id = ? AND is_valuable = 1", (user_id,))
    valuable = c.fetchone()[0]
    
    conn.close()
    
    return {
        'total': total,
        'alive': alive,
        'deleted': deleted,
        'valuable': valuable
    }

def get_random_old_idea(user_id):
    """Get random old idea for echo feature"""
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    cutoff = datetime.now() - timedelta(days=30)
    c.execute('''SELECT id, content, idea_type, file_id, created_at, day_of_week, time_of_day
                 FROM ideas 
                 WHERE user_id = ? AND created_at <= ?
                 ORDER BY RANDOM() LIMIT 1''', (user_id, cutoff))
    idea = c.fetchone()
    conn.close()
    return idea

def get_idea_temperature(opened_count):
    if opened_count >= 3:
        return "🔥 Горячая"
    elif opened_count >= 1:
        return "🌡️ Тёплая"
    else:
        return "❄️ Холодная"

def get_all_ideas_for_export(user_id):
    """Get all ideas for export"""
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute('''SELECT content, idea_type, created_at, is_valuable, day_of_week, time_of_day
                 FROM ideas 
                 WHERE user_id = ?
                 ORDER BY created_at DESC''', (user_id,))
    ideas = c.fetchall()
    conn.close()
    return ideas

def get_valuable_ideas_for_export(user_id):
    """Get only valuable ideas for export"""
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute('''SELECT content, idea_type, created_at, day_of_week, time_of_day
                 FROM ideas 
                 WHERE user_id = ? AND is_valuable = 1
                 ORDER BY created_at DESC''', (user_id,))
    ideas = c.fetchall()
    conn.close()
    return ideas

# ==================== STATES ====================
class SearchStates(StatesGroup):
    waiting_for_query = State()

class FreezeStates(StatesGroup):
    waiting_for_custom_days = State()

class ProfileStates(StatesGroup):
    waiting_for_city = State()

# ==================== KEYBOARD ====================
def get_main_keyboard():
    """Главная клавиатура с командами"""
    keyboard = ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="🔓 Разморозить"), KeyboardButton(text="🔍 Поиск")],
            [KeyboardButton(text="🗑️ Чистка"), KeyboardButton(text="❄️ Заморозка")],
            [KeyboardButton(text="📊 Статистика"), KeyboardButton(text="🔮 Эхо")],
            [KeyboardButton(text="📦 Экспорт"), KeyboardButton(text="💎 Premium")],
            [KeyboardButton(text="👤 Профиль")]
        ],
        resize_keyboard=True
    )
    return keyboard

# ==================== WEATHER ====================
async def get_weather(city):
    """Получить погоду через wttr.in (бесплатно)"""
    if not city:
        return None
    
    try:
        async with aiohttp.ClientSession() as session:
            url = f"https://wttr.in/{city}?format=%C+%t"
            async with session.get(url) as response:
                if response.status == 200:
                    weather = await response.text()
                    return weather.strip()
    except Exception as e:
        logger.error(f"Weather error: {e}")
    
    return None

# ==================== WHISPER TRANSCRIPTION ====================
async def transcribe_audio(file_path):
    """Транскрибация через Faster-Whisper (бесплатно, локально)"""
    global WHISPER_MODEL
    
    if not WHISPER_AVAILABLE:
        logger.warning("Whisper not available, skipping transcription")
        return None
    
    if WHISPER_MODEL is None:
        logger.info("Loading Whisper model...")
        WHISPER_MODEL = WhisperModel("base", device="cpu", compute_type="int8")
        logger.info("Whisper model loaded")
    
    try:
        # Faster-Whisper работает синхронно, так что запускаем в отдельном потоке
        def transcribe_sync():
            segments, info = WHISPER_MODEL.transcribe(file_path, language="ru")
            return " ".join([segment.text for segment in segments]).strip()
        
        loop = asyncio.get_event_loop()
        text = await loop.run_in_executor(None, transcribe_sync)
        
        return text if text else None
    except Exception as e:
        logger.error(f"Transcription error: {e}")
        return None

# ==================== EXPORT ====================
def export_to_markdown(ideas, title="IceBox Export"):
    """Export ideas to markdown format"""
    md = f"# {title}\n\n"
    md += f"*Экспортировано: {datetime.now().strftime('%d.%m.%Y %H:%M')}*\n\n"
    md += "---\n\n"
    
    for content, idea_type, created_at, *rest in ideas:
        is_valuable = rest[0] if len(rest) > 0 else 0
        dow = rest[1] if len(rest) > 1 else ""
        tod = rest[2] if len(rest) > 2 else ""
        
        date_str = datetime.fromisoformat(created_at).strftime("%d.%m.%Y")
        valuable_mark = "⭐ " if is_valuable else ""
        context = f"{dow}, {tod}" if dow else ""
        
        if idea_type == "voice":
            md += f"## {valuable_mark}[Голосовая заметка]\n"
        else:
            md += f"## {valuable_mark}{date_str}\n"
        
        if context:
            md += f"*{context}*\n\n"
        
        if content and idea_type != "voice":
            md += f"{content}\n\n"
        
        md += "---\n\n"
    
    return md

# ==================== YOOKASSA PAYMENT ====================
async def create_payment(user_id, amount, plan_type, description):
    """Create YooKassa payment"""
    payment_id = str(uuid.uuid4())
    
    logger.info(f"Creating payment for user {user_id}: {amount}₽, plan={plan_type}")
    
    async with aiohttp.ClientSession() as session:
        url = "https://api.yookassa.ru/v3/payments"
        
        auth = aiohttp.BasicAuth(YOOKASSA_SHOP_ID, YOOKASSA_SECRET_KEY)
        
        headers = {
            'Idempotence-Key': payment_id,
            'Content-Type': 'application/json'
        }
        
        payload = {
            "amount": {
                "value": f"{amount:.2f}",
                "currency": "RUB"
            },
            "confirmation": {
                "type": "redirect",
                "return_url": f"https://t.me/{BOT_USERNAME}"
            },
            "capture": True,
            "description": description,
            "metadata": {
                "user_id": str(user_id),
                "plan_type": plan_type
            }
        }
        
        logger.info(f"Payment payload: {payload}")
        
        try:
            async with session.post(url, json=payload, headers=headers, auth=auth) as response:
                response_text = await response.text()
                logger.info(f"YooKassa response status: {response.status}")
                logger.info(f"YooKassa response body: {response_text}")
                
                if response.status == 200:
                    result = await response.json()
                    
                    # Save to DB
                    conn = sqlite3.connect(DB_PATH)
                    c = conn.cursor()
                    c.execute('''INSERT INTO payments (user_id, payment_id, amount, plan_type, status)
                                 VALUES (?, ?, ?, ?, 'pending')''',
                              (user_id, result['id'], amount, plan_type))
                    conn.commit()
                    conn.close()
                    
                    logger.info(f"Payment saved to DB: {result['id']}")
                    
                    return result['confirmation']['confirmation_url'], result['id']
                else:
                    logger.error(f"YooKassa error: status={response.status}, body={response_text}")
                    return None, None
        except Exception as e:
            logger.error(f"Payment creation exception: {e}", exc_info=True)
            return None, None

async def check_payment(payment_id):
    """Check payment status"""
    async with aiohttp.ClientSession() as session:
        url = f"https://api.yookassa.ru/v3/payments/{payment_id}"
        auth = aiohttp.BasicAuth(YOOKASSA_SHOP_ID, YOOKASSA_SECRET_KEY)
        
        try:
            async with session.get(url, auth=auth) as response:
                if response.status == 200:
                    result = await response.json()
                    return result.get('status')
                return None
        except Exception as e:
            logger.error(f"Payment check error: {e}")
            return None

def activate_premium(user_id, plan_type):
    """Activate premium for user"""
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    
    if plan_type == "month":
        premium_until = datetime.now() + timedelta(days=30)
    elif plan_type == "year":
        premium_until = datetime.now() + timedelta(days=365)
    else:  # lifetime
        premium_until = datetime.now() + timedelta(days=36500)  # 100 years
    
    c.execute('''UPDATE users 
                 SET is_premium = 1, premium_until = ?
                 WHERE user_id = ?''', (premium_until, user_id))
    conn.commit()
    conn.close()

# ==================== BOT ====================
bot = Bot(token=BOT_TOKEN)
storage = MemoryStorage()
dp = Dispatcher(storage=storage)
router = Router()

@router.message(Command("start"))
async def cmd_start(message: Message):
    get_user(message.from_user.id)
    await message.answer(
        "🧊 <b>IceBox</b> — холодильник для твоих идей\n\n"
        "Просто отправь идею — текст, голос, фото.\n"
        "Она заморозится и вернётся к тебе позже.\n\n"
        "<b>Используй кнопки ниже или команды:</b>\n"
        "/freeze — настроить заморозку\n"
        "/thaw — разморозить идеи\n"
        "/dump — массовая чистка старых идей\n"
        "/find — поиск по словам\n"
        "/stats — статистика\n"
        "/echo — случайная идея из прошлого\n"
        "/export — экспорт в Markdown\n"
        "/premium — подписка\n"
        "/profile — твой профиль\n"
        "/givepremium — активировать Premium",
        parse_mode="HTML",
        reply_markup=get_main_keyboard()
    )

@router.message(Command("premium"))
async def cmd_premium(message: Message):
    user = get_user(message.from_user.id)
    
    if user[2] == 1:
        premium_until = datetime.fromisoformat(user[3]).strftime("%d.%m.%Y")
        
        # Сколько дней осталось
        days_left = (datetime.fromisoformat(user[3]) - datetime.now()).days
        
        await message.answer(
            f"✅ <b>У тебя активна подписка</b>\n\n"
            f"📅 Действует до: <b>{premium_until}</b>\n"
            f"⏰ Осталось: <b>{days_left} дней</b>\n\n"
            f"🎁 <b>Доступно:</b>\n"
            f"• ∞ Безлимит идей\n"
            f"• 🎤 Транскрибация голосовых\n"
            f"• 📦 Экспорт в Markdown\n"
            f"• ❄️ Долгие заморозки (до 365 дней)\n"
            f"• ⚙️ Кастомная заморозка\n\n"
            f"Спасибо за поддержку! 💙",
            parse_mode="HTML"
        )
        return
    
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📅 30 дней — 99₽", callback_data="buy_month")],
        [InlineKeyboardButton(text="🗓️ 1 год — 999₽ 🔥", callback_data="buy_year")],
        [InlineKeyboardButton(text="♾️ Навсегда — 1999₽ ⭐", callback_data="buy_lifetime")]
    ])
    
    await message.answer(
        "💎 <b>IceBox Premium</b>\n\n"
        "🎁 <b>Что получаешь:</b>\n"
        "• ∞ Безлимит идей (сейчас лимит 50)\n"
        "• 🎤 Автоматическая транскрибация голоса\n"
        "• 📦 Экспорт всех идей в Markdown\n"
        "• ❄️ Долгие заморозки (90 дней и навсегда)\n"
        "• ⚙️ Кастомная заморозка (от 1 до 365 дней)\n\n"
        "💳 <b>Способы оплаты:</b>\n"
        "Карты РФ, СБП, ЮMoney, Qiwi\n\n"
        "🔒 Безопасная оплата через ЮKassa\n\n"
        "Выбери план:",
        reply_markup=kb,
        parse_mode="HTML"
    )

@router.callback_query(F.data.startswith("buy_"))
async def process_payment(callback: CallbackQuery):
    plan = callback.data.split("_")[1]
    
    logger.info(f"User {callback.from_user.id} selected plan: {plan}")
    
    plans = {
        "month": (99, "30 дней", "month"),
        "year": (999, "1 год", "year"),
        "lifetime": (1999, "навсегда", "lifetime")
    }
    
    amount, period, plan_type = plans[plan]
    
    logger.info(f"Creating payment: amount={amount}, plan={plan_type}")
    
    # Создаём платёж
    try:
        payment_url, payment_id = await create_payment(
            callback.from_user.id,
            amount,
            plan_type,
            f"IceBox Premium — {period}"
        )
        
        logger.info(f"Payment created: url={payment_url}, id={payment_id}")
        
        if payment_url:
            # Показываем детали платежа
            kb = InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="💳 Оплатить", url=payment_url)],
                [InlineKeyboardButton(text="✅ Я оплатил", callback_data=f"paid_{payment_id}")]
            ])
            
            await callback.message.edit_text(
                f"💎 <b>Оформление Premium</b>\n\n"
                f"📦 План: <b>{period}</b>\n"
                f"💰 Сумма: <b>{amount}₽</b>\n\n"
                f"1️⃣ Нажми <b>«💳 Оплатить»</b>\n"
                f"2️⃣ Оплати любым способом (карта, СБП, ЮMoney)\n"
                f"3️⃣ Вернись сюда и нажми <b>«✅ Я оплатил»</b>\n\n"
                f"⏰ Ссылка действительна 1 час\n\n"
                f"<i>ID платежа: <code>{payment_id}</code></i>",
                reply_markup=kb,
                parse_mode="HTML"
            )
            await callback.answer()
        else:
            logger.error("Payment URL is None")
            await callback.answer("❌ Ошибка создания платежа. Попробуй позже", show_alert=True)
    
    except Exception as e:
        logger.error(f"Payment error: {e}", exc_info=True)
        await callback.answer(f"❌ Ошибка: {str(e)}", show_alert=True)

@router.callback_query(F.data.startswith("paid_"))
async def check_payment_status(callback: CallbackQuery):
    payment_id = callback.data.split("_", 1)[1]
    
    await callback.answer("⏳ Проверяю платёж...", show_alert=False)
    
    status = await check_payment(payment_id)
    
    if status == "succeeded":
        # Get payment info
        conn = sqlite3.connect(DB_PATH)
        c = conn.cursor()
        c.execute("SELECT user_id, plan_type FROM payments WHERE payment_id = ?", (payment_id,))
        result = c.fetchone()
        
        if result:
            user_id, plan_type = result
            activate_premium(user_id, plan_type)
            
            c.execute("UPDATE payments SET status = 'paid', paid_at = datetime('now') WHERE payment_id = ?",
                     (payment_id,))
            conn.commit()
            
            await callback.message.edit_text(
                "✅ <b>Оплата успешна!</b>\n\n"
                "🎉 Premium активирован!\n\n"
                "Теперь тебе доступны:\n"
                "• Безлимит идей\n"
                "• Транскрибация голосовых\n"
                "• Экспорт в Markdown\n"
                "• Долгие заморозки\n\n"
                "Спасибо за поддержку! 💙",
                parse_mode="HTML"
            )
        
        conn.close()
    elif status == "pending" or status == "waiting_for_capture":
        await callback.answer(
            "⏳ Платёж ещё обрабатывается\n\n"
            "Подожди 1-2 минуты и нажми снова",
            show_alert=True
        )
    elif status == "canceled":
        await callback.message.edit_text(
            "❌ <b>Платёж отменён</b>\n\n"
            "Попробуй оформить подписку заново:\n"
            "/premium",
            parse_mode="HTML"
        )
    else:
        await callback.answer(
            "❌ Платёж не найден или отклонён\n\n"
            "Если оплатил, подожди пару минут",
            show_alert=True
        )

@router.message(Command("export"))
async def cmd_export(message: Message):
    if not is_premium(message.from_user.id):
        await message.answer("⭐ Экспорт доступен только для Premium\n\n/premium — оформить подписку")
        return
    
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📦 Все идеи", callback_data="export_all")],
        [InlineKeyboardButton(text="⭐ Только ценные", callback_data="export_valuable")]
    ])
    
    await message.answer("Что экспортировать?", reply_markup=kb)

@router.callback_query(F.data.startswith("export_"))
async def process_export(callback: CallbackQuery):
    export_type = callback.data.split("_")[1]
    
    if export_type == "all":
        ideas = get_all_ideas_for_export(callback.from_user.id)
        title = "IceBox — Все идеи"
    else:
        ideas = get_valuable_ideas_for_export(callback.from_user.id)
        title = "IceBox — Ценные идеи"
    
    if not ideas:
        await callback.answer("Нет идей для экспорта", show_alert=True)
        return
    
    md_content = export_to_markdown(ideas, title)
    
    # Save to file
    filename = f"icebox_export_{callback.from_user.id}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.md"
    with open(filename, 'w', encoding='utf-8') as f:
        f.write(md_content)
    
    # Send file
    await callback.message.answer_document(
        FSInputFile(filename),
        caption=f"📦 Экспорт завершён\n\nИдей: {len(ideas)}"
    )
    
    # Remove temp file
    os.remove(filename)
    await callback.answer()

@router.message(Command("freeze"))
async def cmd_freeze(message: Message):
    is_prem = is_premium(message.from_user.id)
    
    buttons = [
        [InlineKeyboardButton(text="❄️ 1 день", callback_data="freeze_1")],
        [InlineKeyboardButton(text="❄️ 7 дней", callback_data="freeze_7")],
        [InlineKeyboardButton(text="❄️ 14 дней", callback_data="freeze_14")],
        [InlineKeyboardButton(text="❄️ 21 день", callback_data="freeze_21")],
        [InlineKeyboardButton(text="❄️ 30 дней", callback_data="freeze_30")]
    ]
    
    if is_prem:
        buttons.extend([
            [InlineKeyboardButton(text="❄️ 90 дней", callback_data="freeze_90")],
            [InlineKeyboardButton(text="❄️ Навсегда", callback_data="freeze_999")],
            [InlineKeyboardButton(text="⚙️ Своё количество дней", callback_data="freeze_custom")]
        ])
    else:
        buttons.append([InlineKeyboardButton(text="🔒 90 дней / Навсегда / Кастом (Premium)", callback_data="need_premium")])
    
    kb = InlineKeyboardMarkup(inline_keyboard=buttons)
    await message.answer("Выбери срок заморозки для новых идей:", reply_markup=kb)

@router.callback_query(F.data == "need_premium")
async def need_premium(callback: CallbackQuery):
    await callback.answer("Для долгих заморозок нужен Premium", show_alert=True)
    await cmd_premium(callback.message)

@router.callback_query(F.data == "freeze_custom")
async def freeze_custom(callback: CallbackQuery, state: FSMContext):
    await state.set_state(FreezeStates.waiting_for_custom_days)
    await callback.message.edit_text(
        "⚙️ Введи количество дней для заморозки (от 1 до 365):\n\n"
        "Например: <code>45</code> или <code>180</code>",
        parse_mode="HTML"
    )
    await callback.answer()

@router.message(FreezeStates.waiting_for_custom_days)
async def process_custom_freeze(message: Message, state: FSMContext):
    await state.clear()
    
    try:
        days = int(message.text.strip())
        
        if days < 1 or days > 365:
            await message.answer("⚠️ Укажи количество дней от 1 до 365")
            return
        
        conn = sqlite3.connect(DB_PATH)
        c = conn.cursor()
        c.execute("UPDATE users SET freeze_mode = ? WHERE user_id = ?", (days, message.from_user.id))
        conn.commit()
        conn.close()
        
        await message.answer(f"✅ Новые идеи будут замораживаться на {days} дней")
    except ValueError:
        await message.answer("⚠️ Введи число, например: 45")

@router.callback_query(F.data.startswith("freeze_"))
async def process_freeze(callback: CallbackQuery):
    freeze_data = callback.data.split("_")[1]
    
    if freeze_data == "custom":
        return  # Обрабатывается отдельно выше
    
    days = int(freeze_data)
    
    if days > 30 and not is_premium(callback.from_user.id):
        await callback.answer("Нужен Premium для долгих заморозок", show_alert=True)
        return
    
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("UPDATE users SET freeze_mode = ? WHERE user_id = ?", (days, callback.from_user.id))
    conn.commit()
    conn.close()
    
    if days == 1:
        period = "1 день"
    elif days < 999:
        period = f"{days} дней"
    else:
        period = "навсегда"
    
    await callback.message.edit_text(f"✅ Новые идеи будут замораживаться на {period}")
    await callback.answer()

@router.message(Command("thaw"))
async def cmd_thaw(message: Message):
    ideas = get_thawed_ideas(message.from_user.id)
    
    if not ideas:
        await message.answer("❄️ Пока нет размороженных идей")
        return
    
    await message.answer(f"🔓 Доступно идей: {len(ideas)}\n\nВыбери, чтобы открыть:")
    
    for idea in ideas[:10]:
        idea_id, content, idea_type, file_id, created_at, opened_count, dow, tod, weather = idea
        
        preview = content[:50] + "..." if content and len(content) > 50 else content or "[голос/медиа]"
        temp = get_idea_temperature(opened_count)
        date_str = datetime.fromisoformat(created_at).strftime("%d.%m.%Y")
        
        weather_emoji = ""
        if weather:
            weather_emoji = f" {weather}"
        
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [
                InlineKeyboardButton(text="👁️ Открыть", callback_data=f"open_{idea_id}"),
                InlineKeyboardButton(text="🗑️ Удалить", callback_data=f"delete_{idea_id}")
            ],
            [InlineKeyboardButton(text="🧊 +30 дней", callback_data=f"refreeze_{idea_id}")]
        ])
        
        await message.answer(
            f"{temp}\n"
            f"📅 {date_str} ({dow}, {tod}){weather_emoji}\n"
            f"📝 {preview}",
            reply_markup=kb
        )

@router.callback_query(F.data.startswith("open_"))
async def open_idea(callback: CallbackQuery):
    idea_id = int(callback.data.split("_")[1])
    
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT content, idea_type, file_id, created_at, day_of_week, time_of_day, weather FROM ideas WHERE id = ?", (idea_id,))
    idea = c.fetchone()
    
    if not idea:
        await callback.answer("Идея не найдена")
        return
    
    content, idea_type, file_id, created_at, dow, tod, weather = idea
    
    c.execute("UPDATE ideas SET opened_count = opened_count + 1 WHERE id = ?", (idea_id,))
    conn.commit()
    conn.close()
    
    date_str = datetime.fromisoformat(created_at).strftime("%d.%m.%Y %H:%M")
    
    weather_text = f"\n🌤️ {weather}" if weather else ""
    
    context = f"📅 {date_str}\n🗓️ {dow}, {tod}{weather_text}\n\n"
    
    if idea_type == "voice" and file_id:
        await callback.message.answer_voice(file_id, caption=context)
    elif idea_type == "photo" and file_id:
        await callback.message.answer_photo(file_id, caption=context + (content or ""))
    else:
        await callback.message.answer(context + content)
    
    await callback.answer("✅ Открыто")

@router.callback_query(F.data.startswith("delete_"))
async def delete_idea(callback: CallbackQuery):
    idea_id = int(callback.data.split("_")[1])
    
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("INSERT INTO deleted_ideas (user_id) VALUES (?)", (callback.from_user.id,))
    c.execute("DELETE FROM ideas WHERE id = ?", (idea_id,))
    conn.commit()
    conn.close()
    
    await callback.message.edit_text("🗑️ Удалено")
    await callback.answer()

@router.callback_query(F.data.startswith("refreeze_"))
async def refreeze_idea(callback: CallbackQuery):
    idea_id = int(callback.data.split("_")[1])
    
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    new_frozen = datetime.now() + timedelta(days=30)
    c.execute("UPDATE ideas SET frozen_until = ? WHERE id = ?", (new_frozen, idea_id))
    conn.commit()
    conn.close()
    
    await callback.message.edit_text("🧊 Заморожено ещё на 30 дней")
    await callback.answer()

@router.message(Command("dump"))
async def cmd_dump(message: Message):
    ideas = get_old_ideas(message.from_user.id, days=30)
    
    if not ideas:
        await message.answer("Нет старых идей для чистки")
        return
    
    await message.answer(f"🗑️ Найдено старых идей: {len(ideas)}\n\nВыбери действие для каждой:")
    
    for idea in ideas[:15]:
        idea_id, content, idea_type, created_at, opened_count = idea
        
        preview = content[:60] + "..." if content and len(content) > 60 else content or "[голос/медиа]"
        temp = get_idea_temperature(opened_count)
        date_str = datetime.fromisoformat(created_at).strftime("%d.%m")
        
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [
                InlineKeyboardButton(text="❌ Удалить", callback_data=f"dump_del_{idea_id}"),
                InlineKeyboardButton(text="⭐ Ценное", callback_data=f"dump_val_{idea_id}")
            ],
            [InlineKeyboardButton(text="🧊 +90 дней", callback_data=f"dump_freeze_{idea_id}")]
        ])
        
        await message.answer(f"{temp} | {date_str}\n{preview}", reply_markup=kb)

@router.callback_query(F.data.startswith("dump_del_"))
async def dump_delete(callback: CallbackQuery):
    idea_id = int(callback.data.split("_")[2])
    
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("INSERT INTO deleted_ideas (user_id) VALUES (?)", (callback.from_user.id,))
    c.execute("DELETE FROM ideas WHERE id = ?", (idea_id,))
    conn.commit()
    conn.close()
    
    await callback.message.edit_text("❌ Удалено")
    await callback.answer()

@router.callback_query(F.data.startswith("dump_val_"))
async def dump_valuable(callback: CallbackQuery):
    idea_id = int(callback.data.split("_")[2])
    
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("UPDATE ideas SET is_valuable = 1 WHERE id = ?", (idea_id,))
    conn.commit()
    conn.close()
    
    await callback.message.edit_text("⭐ Отмечено как ценное")
    await callback.answer()

@router.callback_query(F.data.startswith("dump_freeze_"))
async def dump_freeze(callback: CallbackQuery):
    idea_id = int(callback.data.split("_")[2])
    
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    new_frozen = datetime.now() + timedelta(days=90)
    c.execute("UPDATE ideas SET frozen_until = ? WHERE id = ?", (new_frozen, idea_id))
    conn.commit()
    conn.close()
    
    await callback.message.edit_text("🧊 Заморожено ещё на 90 дней")
    await callback.answer()

@router.message(Command("stats"))
async def cmd_stats(message: Message):
    stats = get_stats(message.from_user.id)
    user = get_user(message.from_user.id)
    
    premium_status = ""
    if user[2] == 1:
        premium_until = datetime.fromisoformat(user[3]).strftime("%d.%m.%Y")
        premium_status = f"\n💎 Premium до: {premium_until}"
    
    await message.answer(
        "📊 <b>Статистика IceBox</b>\n\n"
        f"💾 Всего сохранено: {stats['total']}\n"
        f"✅ Живых идей: {stats['alive']}\n"
        f"🗑️ Удалено: {stats['deleted']}\n"
        f"⭐ Ценных: {stats['valuable']}\n\n"
        f"📉 Процент выживаемости: {int(stats['alive']/max(stats['total'],1)*100)}%"
        f"{premium_status}",
        parse_mode="HTML"
    )

@router.message(Command("echo"))
async def cmd_echo(message: Message):
    idea = get_random_old_idea(message.from_user.id)
    
    if not idea:
        await message.answer("❄️ Пока нет старых идей для эха")
        return
    
    idea_id, content, idea_type, file_id, created_at, dow, tod = idea
    
    days_ago = (datetime.now() - datetime.fromisoformat(created_at)).days
    context = f"🔮 <b>Эхо из прошлого</b>\n\nТы записал это {days_ago} дней назад\n📅 {dow}, {tod}\n\n"
    
    if idea_type == "voice" and file_id:
        await message.answer_voice(file_id, caption=context, parse_mode="HTML")
    elif idea_type == "photo" and file_id:
        await message.answer_photo(file_id, caption=context + (content or ""), parse_mode="HTML")
    else:
        await message.answer(context + content, parse_mode="HTML")

@router.message(F.text == "🔓 Разморозить")
async def btn_thaw(message: Message):
    await cmd_thaw(message)

@router.message(F.text == "🔍 Поиск")
async def btn_find(message: Message, state: FSMContext):
    await state.set_state(SearchStates.waiting_for_query)
    await message.answer(
        "🔍 Введи слово или фразу для поиска:\n\n"
        "Например: <code>концепция</code> или <code>идея приложения</code>",
        parse_mode="HTML"
    )

@router.message(F.text == "🗑️ Чистка")
async def btn_dump(message: Message):
    await cmd_dump(message)

@router.message(F.text == "❄️ Заморозка")
async def btn_freeze(message: Message):
    await cmd_freeze(message)

@router.message(F.text == "📊 Статистика")
async def btn_stats(message: Message):
    await cmd_stats(message)

@router.message(F.text == "🔮 Эхо")
async def btn_echo(message: Message):
    await cmd_echo(message)

@router.message(F.text == "📦 Экспорт")
async def btn_export(message: Message):
    await cmd_export(message)

@router.message(F.text == "💎 Premium")
async def btn_premium(message: Message):
    await cmd_premium(message)

@router.message(Command("find"))
async def cmd_find(message: Message):
    query = message.text[6:].strip()
    
    if not query:
        await message.answer(
            "🔍 Введи слово или фразу для поиска:\n\n"
            "Например: <code>/find концепция</code>",
            parse_mode="HTML"
        )
        return
    
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute('''SELECT id, content, created_at, is_valuable 
                 FROM ideas 
                 WHERE user_id = ? AND content LIKE ?
                 ORDER BY created_at DESC LIMIT 20''',
              (message.from_user.id, f"%{query}%"))
    results = c.fetchall()
    conn.close()
    
    if not results:
        await message.answer(f"❌ Ничего не найдено по запросу: <b>{query}</b>", parse_mode="HTML")
        return
    
    await message.answer(f"🔍 Найдено: <b>{len(results)}</b>\n", parse_mode="HTML")
    
    for idea_id, content, created_at, is_valuable in results:
        date_str = datetime.fromisoformat(created_at).strftime("%d.%m.%Y")
        valuable_mark = "⭐ " if is_valuable else ""
        
        # Найти позицию запроса в тексте
        query_lower = query.lower()
        content_lower = content.lower()
        pos = content_lower.find(query_lower)
        
        if pos != -1:
            # Показать контекст: 40 символов до и после
            context_start = max(0, pos - 40)
            context_end = min(len(content), pos + len(query) + 40)
            
            before = content[context_start:pos]
            match = content[pos:pos + len(query)]
            after = content[context_end - (pos + len(query)):context_end]
            
            # Добавить многоточие если текст обрезан
            if context_start > 0:
                before = "..." + before
            if context_end < len(content):
                after = after + "..."
            
            preview = f"{before}<b>{match}</b>{after}"
            
            # Остальной текст под спойлер
            full_text = ""
            if len(content) > 100:
                full_text = f"\n\n<tg-spoiler>{content}</tg-spoiler>"
        else:
            preview = content[:80] + ("..." if len(content) > 80 else "")
            full_text = f"\n\n<tg-spoiler>{content}</tg-spoiler>" if len(content) > 80 else ""
        
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="👁️ Открыть", callback_data=f"open_{idea_id}")]
        ])
        
        await message.answer(
            f"{valuable_mark}📅 {date_str}\n{preview}{full_text}",
            reply_markup=kb,
            parse_mode="HTML"
        )

@router.message(F.voice)
async def handle_voice(message: Message):
    user = get_user(message.from_user.id)
    
    if user[2] == 0 and user[4] >= FREE_LIMIT:
        await message.answer("⚠️ Достигнут лимит бесплатных идей (50)\n\n/premium — оформить подписку")
        return
    
    # Get weather
    city = user[5]  # city field
    weather = await get_weather(city) if city else None
    
    # Download voice file
    file = await bot.get_file(message.voice.file_id)
    file_path = f"voice_{message.from_user.id}_{datetime.now().timestamp()}.ogg"
    await bot.download_file(file.file_path, file_path)
    
    content = "[Голосовая заметка]"
    
    # Transcribe if premium
    if user[2] == 1:
        transcription = await transcribe_audio(file_path)
        if transcription:
            content = transcription
            
            # Check for duplicates
            similar = check_similarity(message.from_user.id, transcription)
            if similar:
                idea_id, old_content, old_date = similar
                date_str = datetime.fromisoformat(old_date).strftime("%d.%m.%Y")
                
                kb = InlineKeyboardMarkup(inline_keyboard=[
                    [
                        InlineKeyboardButton(text="💾 Сохранить отдельно", callback_data=f"save_voice_{message.voice.file_id}"),
                        InlineKeyboardButton(text="👁️ Показать старую", callback_data=f"open_{idea_id}")
                    ]
                ])
                
                # Save temp data
                conn = sqlite3.connect(DB_PATH)
                c = conn.cursor()
                c.execute("CREATE TABLE IF NOT EXISTS temp_voice (user_id INTEGER, file_id TEXT, content TEXT, file_path TEXT, weather TEXT, timestamp TIMESTAMP)")
                c.execute("INSERT INTO temp_voice VALUES (?, ?, ?, ?, ?, datetime('now'))", 
                         (message.from_user.id, message.voice.file_id, content, file_path, weather))
                conn.commit()
                conn.close()
                
                await message.answer(
                    f"🔁 Похоже на идею от {date_str}:\n\n{old_content[:150]}...\n\nЭто та же идея?",
                    reply_markup=kb
                )
                return
    
    save_idea(message.from_user.id, content, "voice", message.voice.file_id, file_path, "direct", weather)
    
    # Clean up file if not premium or no transcription
    if user[2] == 0 or not transcription:
        try:
            os.remove(file_path)
        except:
            pass
    
    await message.answer("🧊")

@router.callback_query(F.data.startswith("save_voice_"))
async def save_voice_duplicate(callback: CallbackQuery):
    file_id = callback.data.split("_", 2)[2]
    
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT file_id, content, file_path, weather FROM temp_voice WHERE user_id = ? ORDER BY timestamp DESC LIMIT 1", 
              (callback.from_user.id,))
    result = c.fetchone()
    
    if result:
        file_id, content, file_path, weather = result
        save_idea(callback.from_user.id, content, "voice", file_id, file_path, "direct", weather)
        c.execute("DELETE FROM temp_voice WHERE user_id = ?", (callback.from_user.id,))
        conn.commit()
        await callback.message.edit_text("🧊 Сохранено как новая идея")
    
    conn.close()
    await callback.answer()

@router.message(F.photo)
async def handle_photo(message: Message):
    user = get_user(message.from_user.id)
    
    if user[2] == 0 and user[4] >= FREE_LIMIT:
        await message.answer("⚠️ Достигнут лимит бесплатных идей (50)\n\n/premium — оформить подписку")
        return
    
    # Get weather
    city = user[5]
    weather = await get_weather(city) if city else None
    
    caption = message.caption or "[Фото без описания]"
    
    similar = check_similarity(message.from_user.id, caption)
    if similar:
        idea_id, old_content, old_date = similar
        date_str = datetime.fromisoformat(old_date).strftime("%d.%m.%Y")
        
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [
                InlineKeyboardButton(text="💾 Сохранить отдельно", callback_data=f"save_new_photo"),
                InlineKeyboardButton(text="👁️ Показать старую", callback_data=f"open_{idea_id}")
            ]
        ])
        
        # Save temp
        conn = sqlite3.connect(DB_PATH)
        c = conn.cursor()
        c.execute("CREATE TABLE IF NOT EXISTS temp_photos (user_id INTEGER, file_id TEXT, caption TEXT, weather TEXT, timestamp TIMESTAMP)")
        c.execute("INSERT INTO temp_photos VALUES (?, ?, ?, ?, datetime('now'))", 
                 (message.from_user.id, message.photo[-1].file_id, caption, weather))
        conn.commit()
        conn.close()
        
        await message.answer(
            f"🔁 Похоже на идею от {date_str}:\n\n{old_content[:100]}...\n\nЭто та же идея?",
            reply_markup=kb
        )
        return
    
    save_idea(message.from_user.id, caption, "photo", message.photo[-1].file_id, None, "direct", weather)
    await message.answer("🧊")

@router.callback_query(F.data == "save_new_photo")
async def save_new_photo(callback: CallbackQuery):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT file_id, caption, weather FROM temp_photos WHERE user_id = ? ORDER BY timestamp DESC LIMIT 1", 
              (callback.from_user.id,))
    result = c.fetchone()
    
    if result:
        file_id, caption, weather = result
        save_idea(callback.from_user.id, caption, "photo", file_id, None, "direct", weather)
        c.execute("DELETE FROM temp_photos WHERE user_id = ?", (callback.from_user.id,))
        conn.commit()
        await callback.message.edit_text("🧊 Сохранено как новая идея")
    
    conn.close()
    await callback.answer()

@router.message(Command("givepremium"))
async def cmd_give_premium(message: Message):
    """Команда для выдачи себе премиума (или другому пользователю)"""
    
    parts = message.text.split()
    
    # Если без параметров - выдаём себе навсегда
    if len(parts) == 1:
        target_user_id = message.from_user.id
        days = 36500
        period_text = "навсегда"
    # Если с параметрами - выдаём другому
    elif len(parts) >= 2:
        try:
            target_user_id = int(parts[1])
            
            if len(parts) >= 3 and parts[2] == "lifetime":
                days = 36500
                period_text = "навсегда"
            elif len(parts) >= 3:
                days = int(parts[2])
                period_text = f"{days} дней"
            else:
                days = 30
                period_text = "30 дней"
        except ValueError:
            await message.answer("⚠️ Неверный формат. Используй: /givepremium [USER_ID] [days]")
            return
    else:
        await message.answer(
            "🔧 <b>Использование:</b>\n\n"
            "<code>/givepremium</code> - выдать себе навсегда\n"
            "<code>/givepremium USER_ID days</code> - выдать другому\n\n"
            "Твой ID: <code>{}</code>".format(message.from_user.id),
            parse_mode="HTML"
        )
        return
    
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    
    premium_until = datetime.now() + timedelta(days=days)
    
    c.execute('''UPDATE users 
                 SET is_premium = 1, premium_until = ?
                 WHERE user_id = ?''', (premium_until, target_user_id))
    
    if c.rowcount == 0:
        # Пользователь не найден, создаём
        c.execute("INSERT INTO users (user_id, is_premium, premium_until) VALUES (?, 1, ?)",
                 (target_user_id, premium_until))
    
    conn.commit()
    conn.close()
    
    if target_user_id == message.from_user.id:
        await message.answer(
            f"✅ Premium активирован!\n\n"
            f"⏰ Срок: {period_text}\n"
            f"📅 До: {premium_until.strftime('%d.%m.%Y')}",
            parse_mode="HTML"
        )
    else:
        await message.answer(
            f"✅ Premium выдан!\n\n"
            f"👤 User ID: <code>{target_user_id}</code>\n"
            f"⏰ Срок: {period_text}\n"
            f"📅 До: {premium_until.strftime('%d.%m.%Y')}",
            parse_mode="HTML"
        )

@router.message(Command("profile"))
async def cmd_profile(message: Message):
    """Показать профиль пользователя"""
    user = get_user(message.from_user.id)
    
    user_id, freeze_mode, is_prem, premium_until, ideas_count, city, created_at = user
    
    # Статус премиума
    if is_prem and premium_until:
        premium_date = datetime.fromisoformat(premium_until).strftime("%d.%m.%Y")
        premium_status = f"✅ Активен до {premium_date}"
    else:
        premium_status = "❌ Не активен"
    
    # Город
    city_text = city if city else "Не указан"
    
    # Дата регистрации
    reg_date = datetime.fromisoformat(created_at).strftime("%d.%m.%Y")
    
    # Режим заморозки
    if freeze_mode == 1:
        freeze_text = "1 день"
    elif freeze_mode < 999:
        freeze_text = f"{freeze_mode} дней"
    else:
        freeze_text = "Навсегда"
    
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🌍 Изменить город", callback_data="set_city")]
    ])
    
    await message.answer(
        f"👤 <b>Твой профиль</b>\n\n"
        f"🆔 ID: <code>{user_id}</code>\n"
        f"📅 Регистрация: {reg_date}\n\n"
        f"💎 Premium: {premium_status}\n"
        f"❄️ Режим заморозки: {freeze_text}\n"
        f"📝 Всего идей: {ideas_count}\n"
        f"🌍 Город: {city_text}",
        parse_mode="HTML",
        reply_markup=kb
    )

@router.message(F.text == "👤 Профиль")
async def btn_profile(message: Message):
    await cmd_profile(message)

@router.callback_query(F.data == "set_city")
async def set_city_callback(callback: CallbackQuery, state: FSMContext):
    await state.set_state(ProfileStates.waiting_for_city)
    await callback.message.edit_text(
        "🌍 Введи название своего города:\n\n"
        "Например: <code>Москва</code> или <code>Moscow</code>\n\n"
        "Это нужно чтобы сохранять погоду при создании идей",
        parse_mode="HTML"
    )
    await callback.answer()

@router.message(ProfileStates.waiting_for_city)
async def process_city(message: Message, state: FSMContext):
    await state.clear()
    
    city = message.text.strip()
    
    # Проверяем что город существует
    weather = await get_weather(city)
    
    if weather:
        conn = sqlite3.connect(DB_PATH)
        c = conn.cursor()
        c.execute("UPDATE users SET city = ? WHERE user_id = ?", (city, message.from_user.id))
        conn.commit()
        conn.close()
        
        await message.answer(
            f"✅ Город установлен: <b>{city}</b>\n\n"
            f"🌤️ Текущая погода: {weather}\n\n"
            f"Теперь при сохранении идей будет записываться погода!",
            parse_mode="HTML"
        )
    else:
        await message.answer(
            "⚠️ Не удалось найти этот город.\n"
            "Попробуй написать по-английски или проверь правильность названия.\n\n"
            "Используй /profile чтобы попробовать снова"
        )

@router.message(SearchStates.waiting_for_query)
async def process_search_query(message: Message, state: FSMContext):
    """Обработка поискового запроса"""
    await state.clear()
    
    query = message.text.strip()
    
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute('''SELECT id, content, created_at, is_valuable 
                 FROM ideas 
                 WHERE user_id = ? AND content LIKE ?
                 ORDER BY created_at DESC LIMIT 20''',
              (message.from_user.id, f"%{query}%"))
    results = c.fetchall()
    conn.close()
    
    if not results:
        await message.answer(f"❌ Ничего не найдено по запросу: <b>{query}</b>", parse_mode="HTML")
        return
    
    await message.answer(f"🔍 Найдено: <b>{len(results)}</b>\n", parse_mode="HTML")
    
    for idea_id, content, created_at, is_valuable in results:
        date_str = datetime.fromisoformat(created_at).strftime("%d.%m.%Y")
        valuable_mark = "⭐ " if is_valuable else ""
        
        # Найти позицию запроса в тексте
        query_lower = query.lower()
        content_lower = content.lower()
        pos = content_lower.find(query_lower)
        
        if pos != -1:
            # Показать контекст: 40 символов до и после
            context_start = max(0, pos - 40)
            context_end = min(len(content), pos + len(query) + 40)
            
            before = content[context_start:pos]
            match = content[pos:pos + len(query)]
            after = content[pos + len(query):context_end]
            
            # Добавить многоточие если текст обрезан
            if context_start > 0:
                before = "..." + before
            if context_end < len(content):
                after = after + "..."
            
            preview = f"{before}<b>{match}</b>{after}"
            
            # Остальной текст под спойлер
            full_text = ""
            if len(content) > 100:
                full_text = f"\n\n<tg-spoiler>{content}</tg-spoiler>"
        else:
            preview = content[:80] + ("..." if len(content) > 80 else "")
            full_text = f"\n\n<tg-spoiler>{content}</tg-spoiler>" if len(content) > 80 else ""
        
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="👁️ Открыть", callback_data=f"open_{idea_id}")]
        ])
        
        await message.answer(
            f"{valuable_mark}📅 {date_str}\n{preview}{full_text}",
            reply_markup=kb,
            parse_mode="HTML"
        )

@router.message(F.text)
async def handle_text(message: Message, state: FSMContext):
    # Проверяем, не в режиме ли ожидания ввода
    current_state = await state.get_state()
    if current_state:
        return  # Пропускаем, если ждём ввода для search/freeze
    
    # Skip commands and button presses
    if message.text.startswith("/") or message.text in [
        "🔓 Разморозить", "🔍 Поиск", "🗑️ Чистка", "❄️ Заморозка",
        "📊 Статистика", "🔮 Эхо", "📦 Экспорт", "💎 Premium", "👤 Профиль"
    ]:
        return
    
    user = get_user(message.from_user.id)
    
    if user[2] == 0 and user[4] >= FREE_LIMIT:
        await message.answer("⚠️ Достигнут лимит бесплатных идей (50)\n\n/premium — оформить подписку")
        return
    
    # Get weather
    city = user[5]
    weather = await get_weather(city) if city else None
    
    similar = check_similarity(message.from_user.id, message.text)
    if similar:
        idea_id, old_content, old_date = similar
        date_str = datetime.fromisoformat(old_date).strftime("%d.%m.%Y")
        
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [
                InlineKeyboardButton(text="💾 Сохранить отдельно", callback_data=f"save_new_text"),
                InlineKeyboardButton(text="👁️ Показать старую", callback_data=f"open_{idea_id}")
            ]
        ])
        
        conn = sqlite3.connect(DB_PATH)
        c = conn.cursor()
        c.execute("CREATE TABLE IF NOT EXISTS temp_ideas (user_id INTEGER, content TEXT, weather TEXT, timestamp TIMESTAMP)")
        c.execute("INSERT INTO temp_ideas VALUES (?, ?, ?, datetime('now'))", (message.from_user.id, message.text, weather))
        conn.commit()
        conn.close()
        
        await message.answer(
            f"🔁 Похоже на идею от {date_str}:\n\n{old_content[:150]}...\n\nЭто та же идея?",
            reply_markup=kb
        )
        return
    
    save_idea(message.from_user.id, message.text, "text", None, None, "direct", weather)
    await message.answer("🧊")

@router.callback_query(F.data == "save_new_text")
async def save_new_text(callback: CallbackQuery):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT content, weather FROM temp_ideas WHERE user_id = ? ORDER BY timestamp DESC LIMIT 1", 
              (callback.from_user.id,))
    result = c.fetchone()
    
    if result:
        content, weather = result
        save_idea(callback.from_user.id, content, "text", None, None, "direct", weather)
        c.execute("DELETE FROM temp_ideas WHERE user_id = ?", (callback.from_user.id,))
        conn.commit()
        await callback.message.edit_text("🧊 Сохранено как новая идея")
    
    conn.close()
    await callback.answer()

# ==================== MAIN ====================
async def main():
    init_db()
    dp.include_router(router)
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())

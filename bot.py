import os
import asyncio
import logging
import sqlite3
import re
import random
from datetime import datetime, date
from typing import Optional

from aiohttp import web  # Добавлено для Web Service на Render

from aiogram import Bot, Dispatcher, F, types
from aiogram.filters import CommandStart, Command, CommandObject
from aiogram.types import (
    ReplyKeyboardMarkup, KeyboardButton,
    InlineKeyboardMarkup, InlineKeyboardButton,
    FSInputFile, ErrorEvent
)
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.exceptions import TelegramAPIError

# ---------------------------------------------------------------------------
# Конфигурация
# ---------------------------------------------------------------------------
BOT_TOKEN = os.getenv("BOT_TOKEN", "8785368370:AAFvohvQqqRTrk-lrgGPNefS_-opR9URCOg")
BOT_USERNAME = os.getenv("BOT_USERNAME", "WintezCasino_bot")
PORT = int(os.getenv("PORT", 8080))  # Рендер передает системный PORT

ADMIN_IDS = [5532884382, 7653753080]

PAYMENT_REQUISITES = "💳 Карта: `4441111018371769` Monobank 🏦\n👤 Получатель: Вікторія.Н"
TON_REQUISITES = "💎 Tonkeeper: `8785368370:AAFvohvQqqRTrk-lrgGPNefS_-opR9URCOg`"

MIN_DEPOSIT = 50.0
MIN_WITHDRAW_UAH = 200.0
UAH_TO_USD_RATE = 44.0  # Курс: 1$ = 44 грн

THROW_TIMEOUT = 30
STARTING_BALANCE = 0.0
MAX_BET = 10000.0
MIN_BET = 5.0

logging.basicConfig(level=logging.INFO)
bot = Bot(token=BOT_TOKEN)
dp = Dispatcher(storage=MemoryStorage())

conn = sqlite3.connect("game.db", check_same_thread=False)
cursor = conn.cursor()

# ---------------------------------------------------------------------------
# База данных
# ---------------------------------------------------------------------------
cursor.execute("""
CREATE TABLE IF NOT EXISTS users (
    user_id INTEGER PRIMARY KEY,
    username TEXT,
    balance REAL DEFAULT 0.0,
    turnover REAL DEFAULT 0.0,
    games_count INTEGER DEFAULT 0,
    referrer_id INTEGER DEFAULT NULL,
    ref_rewarded INTEGER DEFAULT 0,
    last_bonus_date TEXT DEFAULT NULL
)
""")

cursor.execute("""
CREATE TABLE IF NOT EXISTS games (
    game_id INTEGER PRIMARY KEY AUTOINCREMENT,
    creator_id INTEGER,
    game_type TEXT,
    bet REAL,
    rounds INTEGER DEFAULT 1,
    status TEXT DEFAULT 'waiting'
)
""")

cursor.execute("""
CREATE TABLE IF NOT EXISTS requests (
    req_id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER,
    req_type TEXT,
    amount REAL,
    details TEXT,
    photo_id TEXT,
    status TEXT DEFAULT 'pending'
)
""")

cursor.execute("""
CREATE TABLE IF NOT EXISTS admin_stats (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    total_fee REAL DEFAULT 0.0,
    games_finished INTEGER DEFAULT 0
)
""")
cursor.execute("INSERT OR IGNORE INTO admin_stats (id, total_fee, games_finished) VALUES (1, 0.0, 0)")
conn.commit()

# Проверка и создание отсутствующих колонок в старой БД
for table, col, col_type in [
    ("users", "turnover", "REAL DEFAULT 0.0"),
    ("users", "games_count", "INTEGER DEFAULT 0"),
    ("users", "referrer_id", "INTEGER DEFAULT NULL"),
    ("users", "ref_rewarded", "INTEGER DEFAULT 0"),
    ("users", "last_bonus_date", "TEXT DEFAULT NULL"),
    ("games", "rounds", "INTEGER DEFAULT 1")
]:
    try:
        cursor.execute(f"ALTER TABLE {table} ADD COLUMN {col} {col_type}")
        conn.commit()
    except sqlite3.OperationalError:
        pass

# Полная очистка всех балансов, оборота и статистики при запуске (чистый старт)
cursor.execute("UPDATE users SET balance = 0.0, turnover = 0.0, games_count = 0")
cursor.execute("UPDATE admin_stats SET total_fee = 0.0, games_finished = 0 WHERE id = 1")
conn.commit()

pending_clicks = {}
BET_RE = re.compile(r"^\d+(\.\d{1,2})?$")

EMOJI_MAP = {
    "dice": "🎲",
    "basketball": "🏀",
    "bowling": "🎳",
    "darts": "🎯"
}

NAMES_MAP = {
    "dice": "🎲 Кубик",
    "basketball": "🏀 Баскетбол",
    "bowling": "🎳 Боулинг",
    "darts": "🎯 Дартс"
}


# ---------------------------------------------------------------------------
# Вспомогательные функции
# ---------------------------------------------------------------------------
def calculate_fee(total_bank: float) -> float:
    if total_bank <= 100.0:
        return total_bank * 0.15
    return total_bank * 0.10


def get_user(user_id: int, username: str = None, referrer_id: int = None):
    cursor.execute(
        "SELECT user_id, username, balance, turnover, games_count, referrer_id, ref_rewarded, last_bonus_date FROM users WHERE user_id = ?",
        (user_id,)
    )
    res = cursor.fetchone()
    if not res:
        cursor.execute(
            "INSERT INTO users (user_id, username, balance, turnover, games_count, referrer_id, ref_rewarded) VALUES (?, ?, ?, 0.0, 0, ?, 0)",
            (user_id, username, STARTING_BALANCE, referrer_id)
        )
        conn.commit()
        return get_user(user_id, username)

    if username and res[1] != username:
        cursor.execute("UPDATE users SET username = ? WHERE user_id = ?", (username, user_id))
        conn.commit()

    return {
        "user_id": res[0],
        "username": res[1],
        "balance": res[2],
        "turnover": res[3],
        "games_count": res[4],
        "referrer_id": res[5],
        "ref_rewarded": res[6],
        "last_bonus_date": res[7]
    }

def try_deduct_balance(user_id: int, amount: float) -> bool:
    cursor.execute(
        "UPDATE users SET balance = balance - ? WHERE user_id = ? AND balance >= ?",
        (amount, user_id, amount)
    )
    conn.commit()
    return cursor.rowcount > 0

def update_balance(user_id: int, amount: float):
    cursor.execute("UPDATE users SET balance = balance + ? WHERE user_id = ?", (amount, user_id))
    conn.commit()

def update_stats(user_id: int, bet: float):
    cursor.execute(
        "UPDATE users SET turnover = turnover + ?, games_count = games_count + 1 WHERE user_id = ?",
        (bet, user_id)
    )
    conn.commit()

def add_admin_income(fee_amount: float):
    cursor.execute(
        "UPDATE admin_stats SET total_fee = total_fee + ?, games_finished = games_finished + 1 WHERE id = 1",
        (fee_amount,)
    )
    conn.commit()

def try_claim_game(game_id: int) -> Optional[tuple]:
    cursor.execute("SELECT creator_id, game_type, bet, rounds, status FROM games WHERE game_id = ?", (game_id,))
    game = cursor.fetchone()
    if not game or game[4] != 'waiting':
        return None

    cursor.execute(
        "UPDATE games SET status = 'playing' WHERE game_id = ? AND status = 'waiting'",
        (game_id,)
    )
    conn.commit()
    if cursor.rowcount == 0:
        return None
    return game

async def safe_send(chat_id: int, text: str, **kwargs):
    try:
        return await bot.send_message(chat_id, text, **kwargs)
    except TelegramAPIError as e:
        logging.warning(f"Не удалось отправить сообщение {chat_id}: {e}")
        return None

async def notify_admins(text: str, photo_id: str = None, reply_markup=None):
    for admin_id in ADMIN_IDS:
        try:
            if photo_id:
                await bot.send_photo(admin_id, photo=photo_id, caption=text, reply_markup=reply_markup, parse_mode="Markdown")
            else:
                await bot.send_message(admin_id, text, reply_markup=reply_markup, parse_mode="Markdown")
        except Exception as e:
            logging.error(f"Ошибка отправки админу {admin_id}: {e}")


# ---------------------------------------------------------------------------
# Состояния FSM
# ---------------------------------------------------------------------------
class CreateGame(StatesGroup):
    waiting_for_bet = State()
    waiting_for_rounds = State()

class DepositState(StatesGroup):
    waiting_for_amount_card = State()
    waiting_for_check_card = State()
    waiting_for_amount_crypto = State()
    waiting_for_check_crypto = State()

class WithdrawState(StatesGroup):
    waiting_for_card_amount = State()
    waiting_for_card_details = State()
    waiting_for_ton_amount = State()
    waiting_for_ton_address = State()

class AdminState(StatesGroup):
    waiting_for_give_id = State()
    waiting_for_give_amount = State()
    waiting_for_broadcast = State()
    waiting_for_search_user = State()


# ---------------------------------------------------------------------------
# Клавиатуры
# ---------------------------------------------------------------------------
def main_keyboard():
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="🎰 Играть в игры")],
            [KeyboardButton(text="👤 Личный кабинет"), KeyboardButton(text="🏆 Топ игроков")],
            [KeyboardButton(text="👥 Реферальная программа"), KeyboardButton(text="ℹ️ Помощь и правила")]
        ],
        resize_keyboard=True
    )

def games_menu_keyboard():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="➕ Создать комнату", callback_data="create_game")],
        [InlineKeyboardButton(text="📜 Активные комнаты", callback_data="list_games")]
    ])

def select_type_keyboard():
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="🎲 Кубик", callback_data="type_dice"),
            InlineKeyboardButton(text="🏀 Баскетбол", callback_data="type_basketball")
        ],
        [
            InlineKeyboardButton(text="🎳 Боулинг", callback_data="type_bowling"),
            InlineKeyboardButton(text="🎯 Дартс", callback_data="type_darts")
        ]
    ])

def select_rounds_keyboard():
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="1 раунд", callback_data="rounds_1"),
            InlineKeyboardButton(text="2 раунда", callback_data="rounds_2"),
            InlineKeyboardButton(text="3 раунда", callback_data="rounds_3")
        ]
    ])

def profile_inline_keyboard():
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="💳 Пополнить UA", callback_data="dep_card"),
            InlineKeyboardButton(text="💎 Пополнить Tonkeeper", callback_data="dep_crypto")
        ],
        [
            InlineKeyboardButton(text="💸 Вывод 💸", callback_data="withdraw_menu")
        ],
        [
            InlineKeyboardButton(text="🎁 Ежедневный бонус", callback_data="daily_bonus")
        ]
    ])

def withdraw_methods_keyboard():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="💳 Вывод на карту", callback_data="withdraw_card")],
        [InlineKeyboardButton(text="💎 Вывод на Tonkeeper", callback_data="withdraw_ton")]
    ])

def deposit_methods_keyboard():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="💳 Пополнить UA", callback_data="dep_card")],
        [InlineKeyboardButton(text="💎 Пополнить Tonkeeper", callback_data="dep_crypto")]
    ])

def admin_keyboard():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📊 Статистика", callback_data="admin_stats"), InlineKeyboardButton(text="💰 Доход", callback_data="admin_income")],
        [InlineKeyboardButton(text="🔍 Найти игрока", callback_data="admin_find_user"), InlineKeyboardButton(text="🧹 Сбросить топы", callback_data="admin_clear_tops")],
        [InlineKeyboardButton(text="🧹 Обнулить балансы", callback_data="admin_reset_all_balances")],
        [InlineKeyboardButton(text="💵 Выдать баланс", callback_data="admin_give")],
        [InlineKeyboardButton(text="📢 Рассылка", callback_data="admin_broadcast")]
    ])


# ---------------------------------------------------------------------------
# Базовые хендлеры
# ---------------------------------------------------------------------------
@dp.message(CommandStart())
async def cmd_start(message: types.Message, command: CommandObject, state: FSMContext):
    await state.clear()
    
    referrer_id = None
    if command.args and command.args.isdigit():
        ref_candidate = int(command.args)
        if ref_candidate != message.from_user.id:
            referrer_id = ref_candidate

    user = get_user(
        user_id=message.from_user.id,
        username=message.from_user.username or message.from_user.first_name,
        referrer_id=referrer_id
    )

    welcome_text = (
        "Добро пожаловать в Wintez Casino!\n\n"
        "Мы рады видеть вас в нашем игровом сообществе. Здесь вас ждут увлекательные развлечения, комфортная атмосфера и возможность приятно провести время.\n\n"
        "🎲 Разнообразные игры на любой вкус\n"
        "🎯 Простое и удобное управление\n"
        "🏀 Регулярные акции и специальные предложения\n"
        "🎳 Круглосуточная поддержка игроков\n\n"
        "> Желаем вам ярких впечатлений, удачных игровых сессий и отличного настроения. Спасибо, что выбрали Wintez Casino!\n"
        "> Играйте ответственно и получайте удовольствие от процесса.\n\n"
        f"💰 Ваш баланс: `{user['balance']:.2f} ₴`"
    )

    photo_path = "CasinoDobroKy.jpg"
    if os.path.exists(photo_path):
        photo = FSInputFile(photo_path)
        await message.answer_photo(
            photo=photo,
            caption=welcome_text,
            reply_markup=main_keyboard(),
            parse_mode="Markdown"
        )
    else:
        await message.answer(
            welcome_text,
            reply_markup=main_keyboard(),
            parse_mode="Markdown"
        )

@dp.message(F.text.contains("Играть") | F.text.contains("Дуэли") | F.text.contains("игры"))
async def open_game_menu(message: types.Message, state: FSMContext):
    await state.clear()
    text = "⚔️Онлайн pvp битва ⚔️\nВыберите действие:"
    photo_path = "dobroplis.png"
    if os.path.exists(photo_path):
        photo = FSInputFile(photo_path)
        await message.answer_photo(
            photo=photo,
            caption=text,
            reply_markup=games_menu_keyboard(),
            parse_mode="Markdown"
        )
    else:
        await message.answer(text, reply_markup=games_menu_keyboard(), parse_mode="Markdown")

@dp.message(F.text.contains("Личный кабинет"))
async def profile(message: types.Message):
    user = get_user(message.from_user.id)
    text = (
        f"👤 **Личный кабинет**\n\n"
        f"💰 Баланс: `{user['balance']:.2f} ₴`\n"
        f"🔄 Оборот: `{user['turnover']:.2f} ₴`\n"
        f"🎮 Всего игр: `{user['games_count']}`"
    )

    photo_path = "Prosile.jpg"
    if os.path.exists(photo_path):
        photo = FSInputFile(photo_path)
        await message.answer_photo(
            photo=photo,
            caption=text,
            reply_markup=profile_inline_keyboard(),
            parse_mode="Markdown"
        )
    else:
        await message.answer(text, reply_markup=profile_inline_keyboard(), parse_mode="Markdown")

@dp.message(F.text.contains("Топ игроков"))
async def top_players(message: types.Message):
    cursor.execute("SELECT user_id, username, turnover FROM users WHERE turnover > 0 ORDER BY turnover DESC LIMIT 10")
    rows = cursor.fetchall()
    
    if not rows:
        await message.answer("🏆 **Топ игроков по обороту:**\n\nСписок пока пуст. Сыграйте первую игру!", parse_mode="Markdown")
        return

    text = "🏆 **Топ игроков по обороту:**\n\n"
    for idx, row in enumerate(rows, 1):
        u_id, u_name, turnover = row
        display_name = f"@{u_name}" if u_name else f"ID: {u_id}"
        text += f"{idx}. **{display_name}** — `{turnover:.2f} ₴`\n"

    await message.answer(text, parse_mode="Markdown")

@dp.message(F.text.contains("Реферальная программа"))
async def referral_program(message: types.Message):
    user = get_user(message.from_user.id)
    user_id = message.from_user.id
    ref_link = f"https://t.me/{BOT_USERNAME}?start={user_id}"
    
    cursor.execute("SELECT COUNT(*) FROM users WHERE referrer_id = ?", (user_id,))
    ref_count = cursor.fetchone()[0]

    text = (
        f"🔗 **Ваша реферальная ссылка:**\n"
        f"`{ref_link}`\n\n"
        f"👥 Приглашено рефералов: `{ref_count}`\n"
        f"💰 Баланс: `{user['balance']:.2f} ₴`\n\n"
        f"🎁 **Приглашайте друзей и получайте по 5 грн за каждого!**\n\n"
        f"💸 Награда начисляется на ваш баланс за каждого приглашённого пользователя, который пополнит баланс на минимальную сумму.\n\n"
        f"🚀 Чем больше приглашаете — тем больше зарабатываете!"
    )
    await message.answer(text, disable_web_page_preview=True, parse_mode="Markdown")

@dp.message(F.text.contains("Помощь и правила"))
async def help_and_rules(message: types.Message):
    text = (
        "🎲 **Кубик** — проверьте свою удачу и сделайте ставку на победный результат.\n\n"
        "🎯 **Дартс** — продемонстрируйте меткость и попадите точно в цель.\n\n"
        "🏀 **Баскетбол** — забрасывайте мячи в корзину и набирайте максимальное количество очков.\n\n"
        "🎳 **Боулинг** — сбивайте кегли, устанавливайте рекорды и наслаждайтесь каждой победой.\n\n"
        "Желаем удачи, ярких эмоций и отличного настроения! Играйте ответственно и получайте удовольствие от процесса.\n\n"
        "💬 **Поддержка:** @w2ntz"
    )
    await message.answer(text, parse_mode="Markdown")

# ---------------------------------------------------------------------------
# Ежедневный бонус
# ---------------------------------------------------------------------------
@dp.callback_query(F.data == "daily_bonus")
async def cb_daily_bonus(callback: types.CallbackQuery):
    user_id = callback.from_user.id
    user = get_user(user_id)
    today_str = date.today().isoformat()

    if user["last_bonus_date"] == today_str:
        await callback.answer("🎁 Вы уже забирали бонус сегодня! Возвращайтесь завтра.", show_alert=True)
        return

    bonus_amount = random.randint(1, 5)
    update_balance(user_id, float(bonus_amount))
    
    cursor.execute("UPDATE users SET last_bonus_date = ? WHERE user_id = ?", (today_str, user_id))
    conn.commit()

    await callback.answer(f"🎉 Вы получили ежедневный бонус: {bonus_amount} ₴!", show_alert=True)
    await safe_send(user_id, f"🎁 **Ваш ежедневный бонус в размере `{bonus_amount} ₴` зачислен на баланс!**", parse_mode="Markdown")


# ---------------------------------------------------------------------------
# Пополнение средств
# ---------------------------------------------------------------------------
@dp.callback_query(F.data == "deposit")
async def cb_deposit(callback: types.CallbackQuery):
    await callback.message.answer("💳 **Выберите способ пополнения:**", reply_markup=deposit_methods_keyboard(), parse_mode="Markdown")
    await callback.answer()

@dp.callback_query(F.data == "dep_card")
async def cb_dep_card(callback: types.CallbackQuery, state: FSMContext):
    await state.set_state(DepositState.waiting_for_amount_card)
    await callback.message.answer(f"💳 **Пополнение UA**\n\nВведите сумму в ₴ (минимум {MIN_DEPOSIT:.0f} ₴):", parse_mode="Markdown")
    await callback.answer()

@dp.message(DepositState.waiting_for_amount_card)
async def process_deposit_amount_card(message: types.Message, state: FSMContext):
    txt = (message.text or "").strip().replace(",", ".")
    if not BET_RE.match(txt):
        await message.answer("⚠️ Введите корректную сумму числом!")
        return

    amount = float(txt)
    if amount < MIN_DEPOSIT:
        await message.answer(f"⚠️ Минимальная сумма пополнения: {MIN_DEPOSIT:.0f} ₴")
        return

    await state.update_data(dep_amount=amount)
    await state.set_state(DepositState.waiting_for_check_card)

    text = (
        f"💸 **Оплата заказа на сумму `{amount:.2f} ₴`**\n\n"
        f"Реквизиты для оплаты:\n{PAYMENT_REQUISITES}\n\n"
        f"⚠️ **Инструкция:**\n"
        f"1. Переведите точно **{amount:.2f} ₴** по реквизитам.\n"
        f"2. Отправьте **фото или скриншот чека** в этот чат."
    )
    
    photo_path = "Popolnini.jpg"
    if os.path.exists(photo_path):
        photo = FSInputFile(photo_path)
        await message.answer_photo(photo=photo, caption=text, parse_mode="Markdown")
    else:
        await message.answer(text, parse_mode="Markdown")

@dp.message(DepositState.waiting_for_check_card, F.photo)
async def process_deposit_check_card(message: types.Message, state: FSMContext):
    data = await state.get_data()
    amount = data.get("dep_amount")
    photo_id = message.photo[-1].file_id
    user_id = message.from_user.id
    username = message.from_user.username or message.from_user.first_name

    cursor.execute(
        "INSERT INTO requests (user_id, req_type, amount, photo_id) VALUES (?, 'deposit', ?, ?)",
        (user_id, amount, photo_id)
    )
    conn.commit()
    req_id = cursor.lastrowid
    await state.clear()

    await message.answer("✅ **Чек отправлен на проверку администраторам!**\nОжидайте зачисления баланса.", parse_mode="Markdown")

    admin_kb = InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="✅ Одобрить", callback_data=f"app_dep_{req_id}"),
            InlineKeyboardButton(text="❌ Отклонить", callback_data=f"rej_dep_{req_id}")
        ]
    ])
    admin_text = (
        f"📥 **Новая заявка на пополнение UAH #{req_id}**\n\n"
        f"👤 Пользователь: @{username} (ID: `{user_id}`)\n"
        f"💰 Сумма: `{amount:.2f} ₴`"
    )
    await notify_admins(admin_text, photo_id=photo_id, reply_markup=admin_kb)

@dp.callback_query(F.data == "dep_crypto")
async def cb_dep_crypto(callback: types.CallbackQuery, state: FSMContext):
    await state.set_state(DepositState.waiting_for_amount_crypto)
    
    text = "💎 **Пополнение Tonkeeper**\n\nВведите сумму пополнения в **USD ($)** (минимум 1.0 $):"
    photo_path = "Popolnini.jpg"
    
    if os.path.exists(photo_path):
        photo = FSInputFile(photo_path)
        await callback.message.answer_photo(photo=photo, caption=text, parse_mode="Markdown")
    else:
        await callback.message.answer(text, parse_mode="Markdown")
        
    await callback.answer()

@dp.message(DepositState.waiting_for_amount_crypto)
async def process_deposit_amount_crypto(message: types.Message, state: FSMContext):
    txt = (message.text or "").strip().replace(",", ".")
    if not BET_RE.match(txt):
        await message.answer("⚠️ Введите корректную сумму числом!")
        return

    amount_usd = float(txt)
    if amount_usd < 1.0:
        await message.answer("⚠️ Минимальная сумма пополнения: 1.0 $")
        return

    await state.update_data(dep_amount_usd=amount_usd)
    await state.set_state(DepositState.waiting_for_check_crypto)

    text = (
        f"💸 **Оплата на пополнение: {amount_usd:.0f}$**\n\n"
        f"Реквизиты для оплаты:\n{TON_REQUISITES}\n\n"
        f"После оплаты предоставьте скриншот чека либо квитанции"
    )
    
    photo_path = "Popolnini.jpg"
    if os.path.exists(photo_path):
        photo = FSInputFile(photo_path)
        await message.answer_photo(photo=photo, caption=text, parse_mode="Markdown")
    else:
        await message.answer(text, parse_mode="Markdown")

@dp.message(DepositState.waiting_for_check_crypto, F.photo)
async def process_deposit_check_crypto(message: types.Message, state: FSMContext):
    data = await state.get_data()
    amount_usd = data.get("dep_amount_usd")
    amount_uah = amount_usd * 41.0
    photo_id = message.photo[-1].file_id
    user_id = message.from_user.id
    username = message.from_user.username or message.from_user.first_name

    cursor.execute(
        "INSERT INTO requests (user_id, req_type, amount, photo_id) VALUES (?, 'deposit_crypto', ?, ?)",
        (user_id, amount_uah, photo_id)
    )
    conn.commit()
    req_id = cursor.lastrowid
    await state.clear()

    await message.answer("✅ **Скриншот оплаты отправлен администраторам!**\nОжидайте подтверждения.", parse_mode="Markdown")

    admin_kb = InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="✅ Одобрить", callback_data=f"app_dep_{req_id}"),
            InlineKeyboardButton(text="❌ Отклонить", callback_data=f"rej_dep_{req_id}")
        ]
    ])
    admin_text = (
        f"💎 **Новая заявка на пополнение Crypto #{req_id}**\n\n"
        f"👤 Пользователь: @{username} (ID: `{user_id}`)\n"
        f"💰 Сумма: `${amount_usd:.2f}` (`{amount_uah:.2f} ₴`)"
    )
    await notify_admins(admin_text, photo_id=photo_id, reply_markup=admin_kb)


# ---------------------------------------------------------------------------
# Вывод средств
# ---------------------------------------------------------------------------
@dp.callback_query(F.data == "withdraw_menu")
async def cb_withdraw_menu(callback: types.CallbackQuery):
    await callback.message.answer("Выберите действие ⬇️", reply_markup=withdraw_methods_keyboard())
    await callback.answer()

@dp.callback_query(F.data == "withdraw_card")
async def cb_withdraw_card(callback: types.CallbackQuery, state: FSMContext):
    user = get_user(callback.from_user.id)
    if user["balance"] < MIN_WITHDRAW_UAH:
        await callback.answer(f"⚠️ Минимальная сумма вывода: {MIN_WITHDRAW_UAH:.0f} ₴", show_alert=True)
        return

    await state.set_state(WithdrawState.waiting_for_card_amount)
    
    text = (
        f"💸 **Вывод средств (UA)**\n"
        f"Доступно: `{user['balance']:.2f} ₴`\n\n"
        f"Введите сумму для вывода в ₴ (от {MIN_WITHDRAW_UAH:.0f} ₴):"
    )

    photo_path = "Vivod.jpg"
    if os.path.exists(photo_path):
        photo = FSInputFile(photo_path)
        await callback.message.answer_photo(photo=photo, caption=text, parse_mode="Markdown")
    else:
        await callback.message.answer(text, parse_mode="Markdown")
        
    await callback.answer()

@dp.message(WithdrawState.waiting_for_card_amount)
async def process_withdraw_card_amount(message: types.Message, state: FSMContext):
    txt = (message.text or "").strip().replace(",", ".")
    if not BET_RE.match(txt):
        await message.answer("⚠️ Введите корректную сумму числом!")
        return

    val = float(txt)
    user = get_user(message.from_user.id)

    if val < MIN_WITHDRAW_UAH:
        await message.answer(f"⚠️ Минимальная сумма вывода: {MIN_WITHDRAW_UAH:.0f} ₴")
        return

    if val > user["balance"]:
        await message.answer("⚠️ Недостаточно средств на балансе!")
        return

    await state.update_data(wit_amount=val)
    await state.set_state(WithdrawState.waiting_for_card_details)
    await message.answer("💳 Введите номер вашей карты:")

@dp.message(WithdrawState.waiting_for_card_details)
async def process_withdraw_card_details(message: types.Message, state: FSMContext):
    details = (message.text or "").strip()
    data = await state.get_data()
    amount_uah = data.get("wit_amount")
    user_id = message.from_user.id
    username = message.from_user.username or message.from_user.first_name

    if not try_deduct_balance(user_id, amount_uah):
        await message.answer("⚠️ Ошибка списания баланса! Попробуйте снова.")
        await state.clear()
        return

    cursor.execute(
        "INSERT INTO requests (user_id, req_type, amount, details) VALUES (?, 'withdraw_card', ?, ?)",
        (user_id, amount_uah, details)
    )
    conn.commit()
    req_id = cursor.lastrowid
    await state.clear()

    await message.answer(f"✅ **Заявка на вывод #{req_id} создана!**\nСумма `{amount_uah:.2f} ₴` отправлена на обработку.", parse_mode="Markdown")

    admin_kb = InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="✅ Выплачено", callback_data=f"app_wit_{req_id}"),
            InlineKeyboardButton(text="❌ Отклонить и вернуть", callback_data=f"rej_wit_{req_id}")
        ]
    ])
    
    admin_text = (
        f"📤 **Заявка на вывод на карту #{req_id}**\n\n"
        f"👤 Пользователь: @{username} (ID: `{user_id}`)\n"
        f"💰 Сумма: `{amount_uah:.2f} ₴`\n"
        f"💳 Карта: `{details}`"
    )
    await notify_admins(admin_text, reply_markup=admin_kb)


# --- Вывод на Tonkeeper ---
@dp.callback_query(F.data == "withdraw_ton")
async def cb_withdraw_ton(callback: types.CallbackQuery, state: FSMContext):
    user = get_user(callback.from_user.id)
    if user["balance"] < MIN_WITHDRAW_UAH:
        await callback.answer(f"⚠️ Минимальная сумма вывода: {MIN_WITHDRAW_UAH:.0f} ₴", show_alert=True)
        return

    await state.set_state(WithdrawState.waiting_for_ton_amount)
    
    text = (
        f"💸 **Вывод средств Tonkeeper💎**\n"
        f"Доступно: `{user['balance']:.2f} ₴`\n\n"
        f"Введите сумму для вывода в ₴ (от {MIN_WITHDRAW_UAH:.0f} ₴):"
    )
    await callback.message.answer(text, parse_mode="Markdown")
    await callback.answer()

@dp.message(WithdrawState.waiting_for_ton_amount)
async def process_withdraw_ton_amount(message: types.Message, state: FSMContext):
    txt = (message.text or "").strip().replace(",", ".")
    if not BET_RE.match(txt):
        await message.answer("⚠️ Введите корректную сумму числом!")
        return

    val = float(txt)
    user = get_user(message.from_user.id)

    if val < MIN_WITHDRAW_UAH:
        await message.answer(f"⚠️ Минимальная сумма вывода: {MIN_WITHDRAW_UAH:.0f} ₴")
        return

    if val > user["balance"]:
        await message.answer("⚠️ Недостаточно средств на балансе!")
        return

    amount_usd = round(val / UAH_TO_USD_RATE, 2)
    await state.update_data(wit_amount=val, wit_usd=amount_usd)
    await state.set_state(WithdrawState.waiting_for_ton_address)

    text = (
        f"💸 **Вывод средств: {amount_usd:.2f}$**\n\n"
        f"💎 Tonkeeper: укажите адрес кошелька⬇️"
    )
    await message.answer(text, parse_mode="Markdown")

@dp.message(WithdrawState.waiting_for_ton_address)
async def process_withdraw_ton_address(message: types.Message, state: FSMContext):
    wallet_address = (message.text or "").strip()
    data = await state.get_data()
    amount_uah = data.get("wit_amount")
    amount_usd = data.get("wit_usd")
    user_id = message.from_user.id
    username = message.from_user.username or message.from_user.first_name

    if not try_deduct_balance(user_id, amount_uah):
        await message.answer("⚠️ Ошибка списания баланса! Попробуйте снова.")
        await state.clear()
        return

    cursor.execute(
        "INSERT INTO requests (user_id, req_type, amount, details) VALUES (?, 'withdraw_ton', ?, ?)",
        (user_id, amount_uah, f"{wallet_address} | ${amount_usd:.2f}")
    )
    conn.commit()
    req_id = cursor.lastrowid
    await state.clear()

    await message.answer(f"✅ **Заявка на вывод #{req_id} создана!**\nСумма `${amount_usd:.2f}` (`{amount_uah:.2f} ₴`) отправлена на обработку.", parse_mode="Markdown")

    admin_kb = InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="✅ Отправлено", callback_data=f"app_ton_{req_id}"),
            InlineKeyboardButton(text="❌ Отклонить и вернуть", callback_data=f"rej_wit_{req_id}")
        ]
    ])
    
    admin_text = (
        f"💎 **Заявка на вывод Tonkeeper #{req_id}**\n\n"
        f"👤 Пользователь: @{username} (ID: `{user_id}`)\n"
        f"💰 Сумма: `${amount_usd:.2f}` (`{amount_uah:.2f} ₴`)\n"
        f"🌐 Кошелек: `{wallet_address}`"
    )
    await notify_admins(admin_text, reply_markup=admin_kb)


# ---------------------------------------------------------------------------
# Обработка заявок Администраторами
# ---------------------------------------------------------------------------
@dp.callback_query(F.data.startswith("app_dep_"))
async def approve_deposit(callback: types.CallbackQuery):
    if callback.from_user.id not in ADMIN_IDS:
        return
    req_id = int(callback.data.split("_")[2])
    cursor.execute("SELECT user_id, amount, status FROM requests WHERE req_id = ?", (req_id,))
    req = cursor.fetchone()
    if not req or req[2] != 'pending':
        await callback.answer("⚠️ Заявка уже обработана!", show_alert=True)
        return

    user_id, amount, _ = req
    cursor.execute("UPDATE requests SET status = 'approved' WHERE req_id = ?", (req_id,))
    update_balance(user_id, amount)
    conn.commit()

    user = get_user(user_id)
    if user["referrer_id"] and user["ref_rewarded"] == 0 and amount >= MIN_DEPOSIT:
        ref_id = user["referrer_id"]
        update_balance(ref_id, 5.0)
        cursor.execute("UPDATE users SET ref_rewarded = 1 WHERE user_id = ?", (user_id,))
        conn.commit()

        await safe_send(
            ref_id,
            f"🎉 **Ваш реферал пополнил баланс!**\n Вам начислено **5.00 ₴** на баланс.",
            parse_mode="Markdown"
        )

    if callback.message.caption:
        await callback.message.edit_caption(caption=callback.message.caption + "\n\n✅ **ОДОБРЕНО**")
    else:
        await callback.message.edit_text(text=callback.message.text + "\n\n✅ **ОДОБРЕНО**")

    await safe_send(user_id, f"🎉 **Ваш баланс пополнен на `{amount:.2f} ₴`!**", parse_mode="Markdown")

@dp.callback_query(F.data.startswith("rej_dep_"))
async def reject_deposit(callback: types.CallbackQuery):
    if callback.from_user.id not in ADMIN_IDS:
        return
    req_id = int(callback.data.split("_")[2])
    cursor.execute("SELECT user_id, amount, status FROM requests WHERE req_id = ?", (req_id,))
    req = cursor.fetchone()
    if not req or req[2] != 'pending':
        await callback.answer("⚠️ Заявка уже обработана!", show_alert=True)
        return

    user_id, amount, _ = req
    cursor.execute("UPDATE requests SET status = 'rejected' WHERE req_id = ?", (req_id,))
    conn.commit()

    if callback.message.caption:
        await callback.message.edit_caption(caption=callback.message.caption + "\n\n❌ **ОТКЛОНЕНО**")
    else:
        await callback.message.edit_text(text=callback.message.text + "\n\n❌ **ОТКЛОНЕНО**")

    await safe_send(user_id, f"❌ **Ваш чек на пополнение ({amount:.2f} ₴) был отклонён.**", parse_mode="Markdown")

@dp.callback_query(F.data.startswith("app_wit_"))
async def approve_withdraw(callback: types.CallbackQuery):
    if callback.from_user.id not in ADMIN_IDS:
        return
    req_id = int(callback.data.split("_")[2])
    cursor.execute("SELECT user_id, amount, status FROM requests WHERE req_id = ?", (req_id,))
    req = cursor.fetchone()
    if not req or req[2] != 'pending':
        await callback.answer("⚠️ Заявка уже обработана!", show_alert=True)
        return

    user_id, amount, _ = req
    cursor.execute("UPDATE requests SET status = 'approved' WHERE req_id = ?", (req_id,))
    conn.commit()

    await callback.message.edit_text(text=callback.message.text + "\n\n✅ **ВЫПЛАЧЕНО**")
    await safe_send(user_id, f"🎉 **Ваша заявка на вывод (`{amount:.2f} ₴`) успешно выплачена!**", parse_mode="Markdown")

@dp.callback_query(F.data.startswith("app_ton_"))
async def approve_ton_withdraw(callback: types.CallbackQuery):
    if callback.from_user.id not in ADMIN_IDS:
        return
    req_id = int(callback.data.split("_")[2])
    cursor.execute("SELECT user_id, amount, details, status FROM requests WHERE req_id = ?", (req_id,))
    req = cursor.fetchone()
    if not req or req[3] != 'pending':
        await callback.answer("⚠️ Заявка уже обработана!", show_alert=True)
        return

    user_id, amount, details, _ = req
    cursor.execute("UPDATE requests SET status = 'approved' WHERE req_id = ?", (req_id,))
    conn.commit()

    wallet_address = details.split(" | ")[0] if " | " in details else details

    await callback.message.edit_text(text=callback.message.text + "\n\n✅ **ОТПРАВЛЕНО**")
    await safe_send(user_id, f"💎 **На ваш адрес `{wallet_address}` были успешно переведены средства!**", parse_mode="Markdown")

@dp.callback_query(F.data.startswith("rej_wit_"))
async def reject_withdraw(callback: types.CallbackQuery):
    if callback.from_user.id not in ADMIN_IDS:
        return
    req_id = int(callback.data.split("_")[2])
    cursor.execute("SELECT user_id, amount, status FROM requests WHERE req_id = ?", (req_id,))
    req = cursor.fetchone()
    if not req or req[2] != 'pending':
        await callback.answer("⚠️ Заявка уже обработана!", show_alert=True)
        return

    user_id, amount, _ = req
    cursor.execute("UPDATE requests SET status = 'rejected' WHERE req_id = ?", (req_id,))
    update_balance(user_id, amount)
    conn.commit()

    await callback.message.edit_text(text=callback.message.text + "\n\n❌ **ОТМЕНЕНО И ВОЗВРАЩЕНО**")
    await safe_send(user_id, f"❌ **Вывод средств (`{amount:.2f} ₴`) отменён.** Деньги возвращены на ваш баланс.", parse_mode="Markdown")


# ---------------------------------------------------------------------------
# Панель администратора (/admin)
# ---------------------------------------------------------------------------
@dp.message(Command("admin"))
async def cmd_admin(message: types.Message):
    if message.from_user.id not in ADMIN_IDS:
        return
    await message.answer("⚙️ **Админ-панель управления**", reply_markup=admin_keyboard(), parse_mode="Markdown")

@dp.callback_query(F.data == "admin_stats")
async def admin_stats(callback: types.CallbackQuery):
    if callback.from_user.id not in ADMIN_IDS:
        return
    cursor.execute("SELECT COUNT(*), SUM(balance), SUM(turnover) FROM users")
    users_count, total_balance, total_turnover = cursor.fetchone()
    total_balance = total_balance or 0.0
    total_turnover = total_turnover or 0.0

    cursor.execute("SELECT COUNT(*) FROM requests WHERE status = 'pending'")
    pending_reqs = cursor.fetchone()[0]

    text = (
        f"📊 **Статистика бота:**\n\n"
        f"👥 Всего пользователей: `{users_count}`\n"
        f"💰 Общий баланс игроков: `{total_balance:.2f} ₴`\n"
        f"🔄 Общий оборот игр: `{total_turnover:.2f} ₴`\n"
        f"⏳ Ожидают проверки: `{pending_reqs}` заявок"
    )
    await callback.message.answer(text, parse_mode="Markdown")
    await callback.answer()

@dp.callback_query(F.data == "admin_income")
async def admin_income(callback: types.CallbackQuery):
    if callback.from_user.id not in ADMIN_IDS:
        return
    
    cursor.execute("SELECT total_fee, games_finished FROM admin_stats WHERE id = 1")
    row = cursor.fetchone()
    total_fee = row[0] if row else 0.0
    games_finished = row[1] if row else 0

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔄 Сбросить счёт", callback_data="admin_reset_income")]
    ])

    text = (
        f"💰 **Финансовый доход бота:**\n\n"
        f"📈 Заработано с комиссий: `{total_fee:.2f} ₴`\n"
        f"🎮 Сыграно завершенных игр: `{games_finished}`"
    )
    await callback.message.answer(text, reply_markup=kb, parse_mode="Markdown")
    await callback.answer()

@dp.callback_query(F.data == "admin_reset_income")
async def admin_reset_income(callback: types.CallbackQuery):
    if callback.from_user.id not in ADMIN_IDS:
        return

    cursor.execute("UPDATE admin_stats SET total_fee = 0.0, games_finished = 0 WHERE id = 1")
    conn.commit()

    await callback.answer("✅ Счётчик доходов успешно сброшен!", show_alert=True)
    
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔄 Сбросить счёт", callback_data="admin_reset_income")]
    ])
    text = (
        f"💰 **Финансовый доход бота:**\n\n"
        f"📈 Заработано с комиссий: `0.00 ₴`\n"
        f"🎮 Сыграно завершенных игр: `0`"
    )
    try:
        await callback.message.edit_text(text, reply_markup=kb, parse_mode="Markdown")
    except TelegramAPIError:
        pass

@dp.callback_query(F.data == "admin_find_user")
async def admin_find_user_start(callback: types.CallbackQuery, state: FSMContext):
    if callback.from_user.id not in ADMIN_IDS:
        return
    await state.set_state(AdminState.waiting_for_search_user)
    await callback.message.answer("🔍 Введите **юзернейм** (можно с `@` или без) либо **Telegram ID** игрока:", parse_mode="Markdown")
    await callback.answer()

@dp.message(AdminState.waiting_for_search_user)
async def admin_find_user_process(message: types.Message, state: FSMContext):
    if message.from_user.id not in ADMIN_IDS:
        return
    
    query = message.text.strip()
    await state.clear()

    user_row = None
    if query.isdigit():
        cursor.execute("SELECT user_id, username, balance, turnover, games_count FROM users WHERE user_id = ?", (int(query),))
        user_row = cursor.fetchone()
    else:
        clean_username = query.lstrip('@')
        cursor.execute("SELECT user_id, username, balance, turnover, games_count FROM users WHERE username LIKE ?", (clean_username,))
        user_row = cursor.fetchone()

    if not user_row:
        await message.answer(f"❌ Пользователь `{query}` не найден в базе данных.", parse_mode="Markdown")
        return

    u_id, u_name, u_balance, u_turnover, u_games = user_row
    display_name = f"@{u_name}" if u_name else "Без юзернейма"

    text = (
        f"👤 **Информация об игроке:**\n\n"
        f"💬 Ник юз: `{display_name}`\n"
        f"🆔 Айди человека: `{u_id}`\n"
        f"💰 Баланс: `{u_balance:.2f} ₴`\n"
        f"🔄 Оборот: `{u_turnover:.2f} ₴`\n"
        f"🎮 Сыграно игр: `{u_games}`"
    )
    await message.answer(text, parse_mode="Markdown")

@dp.callback_query(F.data == "admin_clear_tops")
async def admin_clear_tops(callback: types.CallbackQuery):
    if callback.from_user.id not in ADMIN_IDS:
        return
    
    cursor.execute("UPDATE users SET turnover = 0.0, games_count = 0")
    conn.commit()

    await callback.answer("🧹 Топы игроков успешно очищены!", show_alert=True)
    try:
        await callback.message.answer("✅ **Топы игроков и история оборота обнулены!**", parse_mode="Markdown")
    except TelegramAPIError:
        pass

@dp.callback_query(F.data == "admin_reset_all_balances")
async def admin_reset_all_balances(callback: types.CallbackQuery):
    if callback.from_user.id not in ADMIN_IDS:
        return
    
    cursor.execute("UPDATE users SET balance = 0.0, turnover = 0.0, games_count = 0")
    conn.commit()

    await callback.answer("🧹 Все балансы и статистика полностью обнулены!", show_alert=True)
    try:
        await callback.message.answer("✅ **Все балансы и статистика всех пользователей сброшены в 0!**", parse_mode="Markdown")
    except TelegramAPIError:
        pass

@dp.callback_query(F.data == "admin_give")
async def admin_give(callback: types.CallbackQuery, state: FSMContext):
    if callback.from_user.id not in ADMIN_IDS:
        return
    await state.set_state(AdminState.waiting_for_give_id)
    await callback.message.answer("Введите **ID пользователя**, которому хотите выдать баланс:", parse_mode="Markdown")
    await callback.answer()

@dp.message(AdminState.waiting_for_give_id)
async def admin_give_id(message: types.Message, state: FSMContext):
    if message.from_user.id not in ADMIN_IDS:
        return
    if not message.text.isdigit():
        await message.answer("⚠️ Введите корректный числовой Telegram ID!")
        return

    await state.update_data(give_user_id=int(message.text))
    await state.set_state(AdminState.waiting_for_give_amount)
    await message.answer("Введите сумму (для списания введите со знаком минус, например `-50`):")

@dp.message(AdminState.waiting_for_give_amount)
async def admin_give_amount(message: types.Message, state: FSMContext):
    if message.from_user.id not in ADMIN_IDS:
        return
    txt = (message.text or "").strip().replace(",", ".")
    try:
        amount = float(txt)
    except ValueError:
        await message.answer("⚠️ Введите числовую сумму!")
        return

    data = await state.get_data()
    user_id = data.get("give_user_id")
    await state.clear()

    update_balance(user_id, amount)
    await message.answer(f"✅ Баланс пользователя `{user_id}` изменён на `{amount:.2f} ₴`!", parse_mode="Markdown")
    await safe_send(user_id, f"💳 Ваш баланс был изменён администратором на `{amount:.2f} ₴`!", parse_mode="Markdown")

@dp.callback_query(F.data == "admin_broadcast")
async def admin_broadcast(callback: types.CallbackQuery, state: FSMContext):
    if callback.from_user.id not in ADMIN_IDS:
        return
    await state.set_state(AdminState.waiting_for_broadcast)
    await callback.message.answer("Введите текст для рассылки всем пользователям:")
    await callback.answer()

@dp.message(AdminState.waiting_for_broadcast)
async def admin_broadcast_send(message: types.Message, state: FSMContext):
    if message.from_user.id not in ADMIN_IDS:
        return
    text = message.text
    await state.clear()

    cursor.execute("SELECT user_id FROM users")
    users = cursor.fetchall()
    count = 0

    for u in users:
        if await safe_send(u[0], f"📢 **Сообщение от администрации:**\n\n{text}", parse_mode="Markdown"):
            count += 1
        await asyncio.sleep(0.05)

    await message.answer(f"✅ Рассылка завершена! Успешно доставлено `{count}` пользователям.", parse_mode="Markdown")


# ---------------------------------------------------------------------------
# Игровая логика Дуэлей
# ---------------------------------------------------------------------------
@dp.callback_query(F.data == "create_game")
async def choose_game_type(callback: types.CallbackQuery):
    await callback.message.answer("Выбери игру ⬇️", reply_markup=select_type_keyboard())
    await callback.answer()

@dp.callback_query(F.data.startswith("type_"))
async def set_game_type(callback: types.CallbackQuery, state: FSMContext):
    gtype = callback.data.split("_")[1]
    await state.update_data(gtype=gtype)
    await state.set_state(CreateGame.waiting_for_bet)
    await callback.message.answer(f"Введите сумму ставки от {MIN_BET:.0f} до {MAX_BET:.0f}:")
    await callback.answer()

@dp.message(CreateGame.waiting_for_bet)
async def process_bet(message: types.Message, state: FSMContext):
    txt = (message.text or "").strip().replace(",", ".")
    if not BET_RE.match(txt):
        await message.answer("⚠️ Введите корректную сумму ставки, например: 5 или 20.5")
        return

    bet = float(txt)
    if bet < MIN_BET or bet > MAX_BET:
        await message.answer(f"⚠️ Ставка должна быть от {MIN_BET:.0f} до {MAX_BET:.0f} ₴!")
        return

    await state.update_data(bet=bet)
    await state.set_state(CreateGame.waiting_for_rounds)
    
    await message.answer(
        "✅ **Выберите сколько раундов играть:**",
        reply_markup=select_rounds_keyboard(),
        parse_mode="Markdown"
    )

@dp.callback_query(CreateGame.waiting_for_rounds, F.data.startswith("rounds_"))
async def process_rounds_selection(callback: types.CallbackQuery, state: FSMContext):
    rounds = int(callback.data.split("_")[1])
    data = await state.get_data()
    gtype = data.get("gtype")
    bet = data.get("bet")
    user_id = callback.from_user.id

    if not try_deduct_balance(user_id, bet):
        await callback.answer("⚠️ Недостаточно средств на балансе!", show_alert=True)
        await state.clear()
        return

    await state.clear()
    cursor.execute(
        "INSERT INTO games (creator_id, game_type, bet, rounds) VALUES (?, ?, ?, ?)",
        (user_id, gtype, bet, rounds)
    )
    conn.commit()

    await callback.message.edit_text(
        f"✅ **Комната создана!**\n\n"
        f"📌 Дисциплина: **{NAMES_MAP.get(gtype)}**\n"
        f"🔢 Раундов: **{rounds}**\n"
        f"💰 Ставка: `{bet:.2f} ₴`\n\n"
        f"Ожидание соперника...",
        parse_mode="Markdown"
    )
    await callback.answer()

@dp.callback_query(F.data == "list_games")
async def list_games(callback: types.CallbackQuery):
    cursor.execute("SELECT game_id, creator_id, game_type, bet, rounds FROM games WHERE status = 'waiting' ORDER BY game_id DESC LIMIT 10")
    games_list = cursor.fetchall()

    if not games_list:
        await callback.message.answer("😴 Нет активных комнат. Создайте новую!")
        await callback.answer()
        return

    kb = InlineKeyboardMarkup(inline_keyboard=[])
    current_user_id = callback.from_user.id

    for g in games_list:
        g_id, c_id, gtype, bet, rounds = g
        creator = get_user(c_id)
        c_name = creator["username"] or f"ID: {c_id}"
        btn_text = f"{NAMES_MAP.get(gtype, '🎲')} ({rounds}р) | @{c_name} | {bet:.0f} ₴"
        
        row_buttons = [InlineKeyboardButton(text=btn_text, callback_data=f"join_{g_id}")]
        
        if c_id == current_user_id:
            row_buttons.append(InlineKeyboardButton(text="❌ Удалить", callback_data=f"del_room_{g_id}"))
            
        kb.inline_keyboard.append(row_buttons)

    await callback.message.answer("⚔️ **Выберите комнату для игры или удалите свою:**", reply_markup=kb, parse_mode="Markdown")
    await callback.answer()

@dp.callback_query(F.data.startswith("del_room_"))
async def delete_room(callback: types.CallbackQuery):
    game_id = int(callback.data.split("_")[2])
    user_id = callback.from_user.id

    cursor.execute("SELECT creator_id, bet, status FROM games WHERE game_id = ?", (game_id,))
    row = cursor.fetchone()

    if not row:
        await callback.answer("⚠️ Комната уже не существует!", show_alert=True)
        return

    creator_id, bet, status = row

    if creator_id != user_id:
        await callback.answer("⚠️ Вы не являетесь владельцем этой комнаты!", show_alert=True)
        return

    if status != 'waiting':
        await callback.answer("⚠️ Эту комнату уже нельзя удалить!", show_alert=True)
        return

    update_balance(user_id, bet)
    cursor.execute("UPDATE games SET status = 'cancelled' WHERE game_id = ?", (game_id,))
    conn.commit()

    await callback.answer("✅ Комната успешно удалена, ставка возвращена на баланс!", show_alert=True)
    try:
        await callback.message.edit_text("❌ **Комната была удалена владельцем.**", reply_markup=None)
    except TelegramAPIError:
        pass

@dp.callback_query(F.data == "do_throw")
async def on_throw_button(callback: types.CallbackQuery):
    user_id = callback.from_user.id
    future = pending_clicks.get(user_id)
    if future and not future.done():
        await callback.answer("🎯 Делаем ход...")
        future.set_result(True)
    else:
        await callback.answer("⚠️ Сейчас не ваш черед или время истекло!", show_alert=True)

async def ask_and_throw(user_id: int, emoji: str, opponent_id: int = None):
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=f"🎯 Сделать бросок ({emoji})", callback_data="do_throw")]
    ])
    msg = await safe_send(user_id, f"👇 Нажмите кнопку ниже, чтобы сделать бросок {emoji}:", reply_markup=kb)
    if msg is None:
        return None

    loop = asyncio.get_running_loop()
    future = loop.create_future()
    pending_clicks[user_id] = future

    try:
        await asyncio.wait_for(future, timeout=THROW_TIMEOUT)
        try:
            dice_msg = await bot.send_dice(chat_id=user_id, emoji=emoji)
        except TelegramAPIError as e:
            logging.warning(f"Ошибка отправки dice: {e}")
            return None

        if opponent_id and opponent_id != user_id:
            try:
                await bot.forward_message(chat_id=opponent_id, from_chat_id=user_id, message_id=dice_msg.message_id)
            except TelegramAPIError as e:
                logging.warning(f"Ошибка пересылки: {e}")

        return dice_msg.dice.value
    except asyncio.TimeoutError:
        return None
    finally:
        pending_clicks.pop(user_id, None)
        try:
            await msg.delete()
        except TelegramAPIError:
            pass

@dp.callback_query(F.data.startswith("join_"))
async def join_game(callback: types.CallbackQuery):
    game_id = int(callback.data.split("_")[1])
    joiner_id = callback.from_user.id

    cursor.execute("SELECT creator_id FROM games WHERE game_id = ?", (game_id,))
    row = cursor.fetchone()
    if not row:
        await callback.answer("⚠️ Эта комната больше не существует!", show_alert=True)
        return

    if row[0] == joiner_id:
        await callback.answer("⚠️ Нельзя присоединиться к своей же комнате!", show_alert=True)
        return

    joiner = get_user(joiner_id, callback.from_user.username or callback.from_user.first_name)
    game_peek = try_claim_game(game_id)
    if game_peek is None:
        await callback.answer("⚠️ Эта комната больше не активна!", show_alert=True)
        return

    creator_id, gtype, bet, rounds, _ = game_peek

    if not try_deduct_balance(joiner_id, bet):
        cursor.execute("UPDATE games SET status = 'waiting' WHERE game_id = ?", (game_id,))
        conn.commit()
        await callback.answer("⚠️ У вас недостаточно денег для этой ставки!", show_alert=True)
        return

    update_stats(creator_id, bet)
    update_stats(joiner_id, bet)

    emo = EMOJI_MAP.get(gtype, "🎲")
    creator = get_user(creator_id)
    joiner_name = joiner['username'] or f"ID {joiner_id}"
    creator_name = creator['username'] or f"ID {creator_id}"

    await safe_send(creator_id, f"⚔️ Игрок **@{joiner_name}** вошел в вашу комнату!", parse_mode="Markdown")
    await callback.message.answer(f"⚔️ Дуэль началась! **@{creator_name}** против **@{joiner_name}**", parse_mode="Markdown")
    await callback.answer()

    total_bank = bet * 2
    fee_amount = calculate_fee(total_bank)
    win_money = total_bank - fee_amount

    if gtype in ["dice", "darts"]:
        p1_rolls = []
        p2_rolls = []

        for round_idx in range(1, rounds + 1):
            await safe_send(creator_id, f"{emo} **Раунд {round_idx} из {rounds}**", parse_mode="Markdown")
            await callback.message.answer(f"{emo} **Раунд {round_idx} из {rounds}**", parse_mode="Markdown")

            v1, v2 = await asyncio.gather(
                ask_and_throw(creator_id, emo, opponent_id=joiner_id),
                ask_and_throw(joiner_id, emo, opponent_id=creator_id),
            )

            if v1 is None and v2 is None:
                refund_text = "⏱ **Оба игрока не сделали бросок вовремя.** Игра отменена, ставки возвращены."
                update_balance(creator_id, bet)
                update_balance(joiner_id, bet)
                cursor.execute("UPDATE games SET status = 'cancelled' WHERE game_id = ?", (game_id,))
                conn.commit()
                await safe_send(creator_id, refund_text, parse_mode="Markdown")
                await callback.message.answer(refund_text, parse_mode="Markdown")
                return

            if v1 is None or v2 is None:
                winner_id = joiner_id if v1 is None else creator_id
                loser_name = creator_name if v1 is None else joiner_name
                winner_name = joiner_name if v1 is None else creator_name

                cursor.execute("UPDATE games SET status = 'finished' WHERE game_id = ?", (game_id,))
                conn.commit()
                update_balance(winner_id, win_money)
                add_admin_income(fee_amount)
                
                text_timeout = (
                    f"⏱ @{loser_name} не нажал кнопку за {THROW_TIMEOUT} сек.\n"
                    f"🎉 Победа присуждается **@{winner_name}**, который забрал `{win_money:.2f} ₴`!"
                )
                await safe_send(creator_id, text_timeout, parse_mode="Markdown")
                await callback.message.answer(text_timeout, parse_mode="Markdown")
                return

            p1_rolls.append(v1)
            p2_rolls.append(v2)
            await asyncio.sleep(1)

        await asyncio.sleep(1)

        p1_total = sum(p1_rolls)
        p2_total = sum(p2_rolls)

        res = (
            f"📊 Результаты игры:\n"
            f"👤 @{creator_name}: {p1_total} (броски: {p1_rolls})\n"
            f"👤 @{joiner_name}: {p2_total} (броски: {p2_rolls})\n\n"
        )

        if p1_total > p2_total:
            update_balance(creator_id, win_money)
            add_admin_income(fee_amount)
            res += f"🎉 Победил @{creator_name} и забрал `{win_money:.2f} ₴`!"
        elif p2_total > p1_total:
            update_balance(joiner_id, win_money)
            add_admin_income(fee_amount)
            res += f"🎉 Победил @{joiner_name} и забрал `{win_money:.2f} ₴`!"
        else:
            update_balance(creator_id, bet)
            update_balance(joiner_id, bet)
            res += f"🤝 Ничья по очкам! Ставки возвращены."

        cursor.execute("UPDATE games SET status = 'finished' WHERE game_id = ?", (game_id,))
        conn.commit()

        await safe_send(creator_id, res, parse_mode="Markdown")
        await callback.message.answer(res, parse_mode="Markdown")

    else:
        p1_wins = 0
        p2_wins = 0

        for round_idx in range(1, rounds + 1):
            if rounds > 1:
                await safe_send(creator_id, f"{emo} **Раунд {round_idx} из {rounds}**", parse_mode="Markdown")
                await callback.message.answer(f"{emo} **Раунд {round_idx} из {rounds}**", parse_mode="Markdown")

            val1, val2 = await asyncio.gather(
                ask_and_throw(creator_id, emo, opponent_id=joiner_id),
                ask_and_throw(joiner_id, emo, opponent_id=creator_id),
            )

            if val1 is None and val2 is None:
                refund_text = "⏱ **Оба игрока не сделали бросок вовремя.** Игра отменена, ставки возвращены."
                update_balance(creator_id, bet)
                update_balance(joiner_id, bet)
                cursor.execute("UPDATE games SET status = 'cancelled' WHERE game_id = ?", (game_id,))
                conn.commit()
                await safe_send(creator_id, refund_text, parse_mode="Markdown")
                await callback.message.answer(refund_text, parse_mode="Markdown")
                return

            if val1 is None or val2 is None:
                winner_id = joiner_id if val1 is None else creator_id
                loser_name = creator_name if val1 is None else joiner_name
                winner_name = joiner_name if val1 is None else creator_name

                cursor.execute("UPDATE games SET status = 'finished' WHERE game_id = ?", (game_id,))
                conn.commit()
                update_balance(winner_id, win_money)
                add_admin_income(fee_amount)

                text_timeout = (
                    f"⏱ @{loser_name} не нажал кнопку за {THROW_TIMEOUT} сек.\n"
                    f"🎉 Победа присуждается **@{winner_name}**, который забрал `{win_money:.2f} ₴`!"
                )
                await safe_send(creator_id, text_timeout, parse_mode="Markdown")
                await callback.message.answer(text_timeout, parse_mode="Markdown")
                return

            if emo == "🏀":
                p1_scored = 1 if val1 >= 4 else 0
                p2_scored = 1 if val2 >= 4 else 0
                if p1_scored > p2_scored:
                    p1_wins += 1
                elif p2_scored > p1_scored:
                    p2_wins += 1
            else:
                if val1 > val2:
                    p1_wins += 1
                elif val2 > val1:
                    p2_wins += 1

            await asyncio.sleep(1)

        await asyncio.sleep(1)

        if p1_wins == p2_wins:
            update_balance(creator_id, bet)
            update_balance(joiner_id, bet)
            res = (
                f"📊 Результаты игры:\n"
                f"👤 @{creator_name}: {p1_wins} побед в раундах\n"
                f"👤 @{joiner_name}: {p2_wins} побед в раундах\n\n"
                f"🤝 Ничья по раундам! Ставки возвращены."
            )
        else:
            if p1_wins > p2_wins:
                winner_id = creator_id
                winner_name = creator_name
            else:
                winner_id = joiner_id
                winner_name = joiner_name

            update_balance(winner_id, win_money)
            add_admin_income(fee_amount)
            res = (
                f"📊 Результаты игры:\n"
                f"👤 @{creator_name}: {p1_wins} побед в раундах\n"
                f"👤 @{joiner_name}: {p2_wins} побед в раундах\n\n"
                f"🎉 Победил @{winner_name} и забрал `{win_money:.2f} ₴`!"
            )

        cursor.execute("UPDATE games SET status = 'finished' WHERE game_id = ?", (game_id,))
        conn.commit()

        await safe_send(creator_id, res, parse_mode="Markdown")
        await callback.message.answer(res, parse_mode="Markdown")


# ---------------------------------------------------------------------------
# Веб-сервер для UptimeRobot / Render Web Service
# ---------------------------------------------------------------------------
async def handle_ping(request):
    return web.Response(text="Bot is alive!", status=200)

async def start_web_server():
    app = web.Application()
    app.router.add_get('/', handle_ping)
    app.router.add_get('/healthcheck', handle_ping)
    
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, '0.0.0.0', PORT)
    await site.start()
    logging.info(f"HTTP сервер успешно запущен на порту {PORT}")


# ---------------------------------------------------------------------------
# Глобальный обработчик ошибок и запуск
# ---------------------------------------------------------------------------
@dp.errors()
async def global_error_handler(event: ErrorEvent):
    logging.exception(f"Ошибка при обработке апдейта: {event.exception}")
    return True


async def main():
    await bot.delete_webhook(drop_pending_updates=True)
    
    # Запускаем локальный веб-сервер для прохождения Render Health Check & UptimeRobot
    await start_web_server()
    
    # Запускаем лонг-поллинг Telegram бота
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())

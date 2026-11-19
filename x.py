# =============================================================
# MINT Bot — Часть 1/4: Инфраструктура + базовые команды
# =============================================================
import asyncio
import logging
import random
import os
import re
import json
from datetime import datetime, timedelta
from contextlib import contextmanager
from aiohttp import web
import psycopg2
from psycopg2 import pool

from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.filters import Command
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
    ReplyKeyboardMarkup,
    KeyboardButton,
    FSInputFile,
    BufferedInputFile
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")

TOKEN = os.getenv("BOT_TOKEN")
DATABASE_URL = os.getenv("DATABASE_URL")
ADMIN_ID = 8778627374
MIN_BET = 10
BOT_USERNAME = ""
CURRENCY = "COINS"
BOT_NAME = "MINT"

bot = Bot(token=TOKEN, default=DefaultBotProperties(parse_mode="HTML"))
dp = Dispatcher()
router = Router()

CACHED_ANIMATION = None
chat_roulette_bets = {}
chat_last_bet_time = {}
admin_states = {}  # user_id -> {"action": str, "target": int|None, "data": dict}
active_prompts = {}  # для admin: {user_id: {"chat_id": int, "message_id": int}}


def get_cached_animation():
    global CACHED_ANIMATION
    if CACHED_ANIMATION is None and os.path.exists("red-1.mp4"):
        CACHED_ANIMATION = FSInputFile("red-1.mp4")
    return CACHED_ANIMATION


DB_POOL = None


def init_db_pool():
    global DB_POOL
    DB_POOL = pool.ThreadedConnectionPool(minconn=1, maxconn=20, dsn=DATABASE_URL)
    logging.info("Connection Pool PostgreSQL создан")


@contextmanager
def get_db():
    conn = DB_POOL.getconn()
    try:
        yield conn
    finally:
        DB_POOL.putconn(conn)


def init_db():
    init_db_pool()
    with get_db() as conn:
        with conn.cursor() as cursor:
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS users (
                    user_id BIGINT PRIMARY KEY,
                    first_name TEXT, last_name TEXT, username TEXT,
                    balance BIGINT DEFAULT 4000, last_bonus TEXT,
                    custom_nick TEXT,
                    is_banned BOOLEAN DEFAULT FALSE,
                    ban_until TIMESTAMP,
                    games_played INT DEFAULT 0,
                    games_won INT DEFAULT 0,
                    referrer_id BIGINT,
                    referred_count INT DEFAULT 0,
                    referal_bonus_claimed INT DEFAULT 0,
                    last_daily TEXT,
                    last_robbery TIMESTAMP,
                    vip_until TIMESTAMP,
                    btc NUMERIC DEFAULT 0,
                    clan_id INT
                )""")
            for col, col_type in [
                ("is_banned", "BOOLEAN DEFAULT FALSE"),
                ("ban_until", "TIMESTAMP"),
                ("games_played", "INT DEFAULT 0"),
                ("games_won", "INT DEFAULT 0"),
                ("referrer_id", "BIGINT"),
                ("referred_count", "INT DEFAULT 0"),
                ("referal_bonus_claimed", "INT DEFAULT 0"),
                ("last_daily", "TEXT"),
                ("custom_nick", "TEXT"),
                ("last_robbery", "TIMESTAMP"),
                ("vip_until", "TIMESTAMP"),
                ("btc", "NUMERIC DEFAULT 0"),
                ("clan_id", "INT")
            ]:
                try:
                    cursor.execute(f"ALTER TABLE users ADD COLUMN IF NOT EXISTS {col} {col_type}")
                    conn.commit()
                except psycopg2.Error:
                    conn.rollback()

            cursor.execute("CREATE TABLE IF NOT EXISTS admins (user_id BIGINT PRIMARY KEY, rank TEXT DEFAULT 'moder')")
            cursor.execute("CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT)")
            cursor.execute("CREATE TABLE IF NOT EXISTS disabled_games (game_name TEXT PRIMARY KEY)")
            cursor.execute("CREATE TABLE IF NOT EXISTS roulette_log (id SERIAL PRIMARY KEY, roll INT, color TEXT, user_id BIGINT, bet_amount BIGINT, target TEXT, win_amount BIGINT, timestamp TEXT)")
            cursor.execute("CREATE TABLE IF NOT EXISTS user_last_bets (user_id BIGINT PRIMARY KEY, bets_json TEXT)")
            cursor.execute("CREATE TABLE IF NOT EXISTS promocodes (code TEXT PRIMARY KEY, amount BIGINT NOT NULL, uses INT NOT NULL DEFAULT 1)")
            cursor.execute("CREATE TABLE IF NOT EXISTS used_promocodes (user_id BIGINT, code TEXT, PRIMARY KEY (user_id, code))")
            cursor.execute("CREATE TABLE IF NOT EXISTS secret_powers (user_id BIGINT, command_name TEXT, PRIMARY KEY (user_id, command_name))")
            cursor.execute("CREATE TABLE IF NOT EXISTS admin_log (id SERIAL PRIMARY KEY, admin_id BIGINT, action TEXT, target_id BIGINT, amount BIGINT, timestamp TEXT)")
            cursor.execute("CREATE TABLE IF NOT EXISTS balance_checkpoints (user_id BIGINT, balance BIGINT, checkpoint_time TEXT)")
            cursor.execute("CREATE TABLE IF NOT EXISTS daily_tasks (user_id BIGINT, date TEXT, task_type TEXT, progress INT DEFAULT 0, completed BOOLEAN DEFAULT FALSE, PRIMARY KEY (user_id, date, task_type))")
            cursor.execute("CREATE TABLE IF NOT EXISTS referrals (user_id BIGINT PRIMARY KEY, referrer_id BIGINT, bonus_claimed BOOLEAN DEFAULT FALSE)")
            cursor.execute("CREATE TABLE IF NOT EXISTS deposits (user_id BIGINT PRIMARY KEY, amount BIGINT, start_time TIMESTAMP)")
            cursor.execute("CREATE TABLE IF NOT EXISTS clans (id SERIAL PRIMARY KEY, name TEXT UNIQUE, owner_id BIGINT, balance BIGINT DEFAULT 0)")
            cursor.execute("CREATE TABLE IF NOT EXISTS clan_members (clan_id INT, user_id BIGINT, PRIMARY KEY (clan_id, user_id))")
            cursor.execute("CREATE TABLE IF NOT EXISTS cases_inventory (user_id BIGINT, case_type TEXT, count INT DEFAULT 1, PRIMARY KEY (user_id, case_type))")
            cursor.execute("CREATE TABLE IF NOT EXISTS miners (user_id BIGINT, card_type TEXT, count INT, PRIMARY KEY (user_id, card_type))")
            cursor.execute("CREATE TABLE IF NOT EXISTS mining_stats (user_id BIGINT PRIMARY KEY, accumulated_btc NUMERIC DEFAULT 0, last_update TIMESTAMP)")
            cursor.execute("CREATE TABLE IF NOT EXISTS snos_log (user_id BIGINT PRIMARY KEY, previous_rank TEXT, previous_balance BIGINT, timestamp TEXT)")
            conn.commit()

            cursor.execute("SELECT user_id FROM admins WHERE user_id = %s", (ADMIN_ID,))
            if not cursor.fetchone():
                cursor.execute("INSERT INTO admins (user_id, rank) VALUES (%s, 'owner')", (ADMIN_ID,))

            for key, value in [
                ('bonus_amount', '3000'), ('bonus_cooldown', '8'),
                ('referral_bonus', '75000'), ('referral_limit', '25'),
                ('btc_rate', '1000000000'), ('currency', CURRENCY), ('bot_name', BOT_NAME)
            ]:
                cursor.execute("INSERT INTO settings (key, value) VALUES (%s,%s) ON CONFLICT (key) DO NOTHING", (key, value))
            conn.commit()
    logging.info("БД инициализирована")


init_db()


# ============================================================
# ХЕЛПЕРЫ
# ============================================================
def get_user(user_id: int, first_name: str = "", last_name: str = "", username: str = "") -> dict:
    safe_name = first_name if first_name else "Игрок"
    with get_db() as conn:
        with conn.cursor() as cursor:
            cursor.execute(
                "SELECT user_id, first_name, last_name, username, balance, last_bonus, games_played, games_won, referrer_id, referred_count, referal_bonus_claimed, last_daily, custom_nick, last_robbery, is_banned, ban_until, vip_until, btc, clan_id FROM users WHERE user_id = %s",
                (user_id,))
            row = cursor.fetchone()
            if not row:
                cursor.execute(
                    "INSERT INTO users (user_id, first_name, last_name, username, balance, last_bonus, games_played, games_won, referal_bonus_claimed) VALUES (%s,%s,%s,%s,%s,%s,0,0,0)",
                    (user_id, safe_name, last_name or "", username or "", 4000, None))
                conn.commit()
                return {"user_id": user_id, "first_name": safe_name, "last_name": last_name or "",
                        "username": username or "", "balance": 4000, "last_bonus": None,
                        "games_played": 0, "games_won": 0, "referrer_id": None,
                        "referred_count": 0, "referal_bonus_claimed": 0, "last_daily": None,
                        "custom_nick": None, "last_robbery": None, "is_banned": False,
                        "ban_until": None, "vip_until": None, "btc": 0, "clan_id": None}
            return {"user_id": row[0], "first_name": row[1], "last_name": row[2], "username": row[3],
                    "balance": row[4], "last_bonus": row[5], "games_played": row[6],
                    "games_won": row[7], "referrer_id": row[8], "referred_count": row[9],
                    "referal_bonus_claimed": row[10], "last_daily": row[11], "custom_nick": row[12],
                    "last_robbery": row[13], "is_banned": row[14], "ban_until": row[15],
                    "vip_until": row[16], "btc": float(row[17]) if row[17] else 0, "clan_id": row[18]}


def update_balance(user_id: int, amount: int):
    with get_db() as conn:
        with conn.cursor() as cursor:
            cursor.execute("UPDATE users SET balance = balance + %s WHERE user_id = %s", (amount, user_id))
            conn.commit()


def find_user_by_identifier(identifier: str):
    clean_id = str(identifier).replace("@", "").strip()
    with get_db() as conn:
        with conn.cursor() as cursor:
            if clean_id.isdigit():
                cursor.execute(
                    "SELECT user_id, first_name, last_name, username, balance, last_bonus, games_played, games_won, referrer_id, referred_count, referal_bonus_claimed, last_daily, custom_nick, last_robbery, is_banned, ban_until, vip_until, btc, clan_id FROM users WHERE user_id = %s",
                    (int(clean_id),))
            else:
                cursor.execute(
                    "SELECT user_id, first_name, last_name, username, balance, last_bonus, games_played, games_won, referrer_id, referred_count, referal_bonus_claimed, last_daily, custom_nick, last_robbery, is_banned, ban_until, vip_until, btc, clan_id FROM users WHERE LOWER(username) = LOWER(%s)",
                    (clean_id,))
            row = cursor.fetchone()
            if row:
                return {"user_id": row[0], "first_name": row[1], "last_name": row[2], "username": row[3],
                        "balance": row[4], "last_bonus": row[5], "games_played": row[6],
                        "games_won": row[7], "referrer_id": row[8], "referred_count": row[9],
                        "referal_bonus_claimed": row[10], "last_daily": row[11], "custom_nick": row[12],
                        "last_robbery": row[13], "is_banned": row[14], "ban_until": row[15],
                        "vip_until": row[16], "btc": float(row[17]) if row[17] else 0, "clan_id": row[18]}
    return None


def get_rank(user_id: int) -> str:
    if user_id == ADMIN_ID:
        return "owner"
    with get_db() as conn:
        with conn.cursor() as cursor:
            cursor.execute("SELECT rank FROM admins WHERE user_id = %s", (user_id,))
            row = cursor.fetchone()
            return row[0] if row else "user"


def get_rank_emoji(rank: str) -> str:
    return {"owner": "💎 Владелец", "head": "👑 Гл. админ",
            "admin": "🔰 Админ", "spadmin": "👑 Special Admin",
            "moder": "⭐ Модератор"}.get(rank, "")


def is_admin(user_id: int) -> bool:
    return get_rank(user_id) in ("owner", "head", "admin", "moder", "spadmin")


def is_moder_or_above(user_id: int) -> bool:
    return get_rank(user_id) in ("owner", "head", "admin", "moder", "spadmin")


def is_admin_or_above(user_id: int) -> bool:
    return get_rank(user_id) in ("owner", "head", "admin")


def is_head_or_above(user_id: int) -> bool:
    return get_rank(user_id) in ("owner", "head")


def is_owner(user_id: int) -> bool:
    return user_id == ADMIN_ID


def has_secret_power(user_id: int, command: str) -> bool:
    if user_id == ADMIN_ID:
        return True
    with get_db() as conn:
        with conn.cursor() as cursor:
            cursor.execute("SELECT 1 FROM secret_powers WHERE user_id = %s AND command_name = %s", (user_id, command))
            return cursor.fetchone() is not None


def is_game_disabled(game_name: str) -> bool:
    with get_db() as conn:
        with conn.cursor() as cursor:
            cursor.execute("SELECT 1 FROM disabled_games WHERE game_name = %s", (game_name,))
            return cursor.fetchone() is not None


def get_setting(key: str, default: str) -> str:
    with get_db() as conn:
        with conn.cursor() as cursor:
            cursor.execute("SELECT value FROM settings WHERE key = %s", (key,))
            row = cursor.fetchone()
            return row[0] if row else default


def set_setting(key: str, value: str):
    with get_db() as conn:
        with conn.cursor() as cursor:
            cursor.execute("INSERT INTO settings (key, value) VALUES (%s,%s) ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value", (key, value))
            conn.commit()


def is_cursed(user_id: int) -> bool:
    return get_setting(f"cursed_{user_id}", "0") == "1"


def add_roulette_log(roll, color, user_id=None, bet_amount=None, target=None, win_amount=None):
    with get_db() as conn:
        with conn.cursor() as cursor:
            cursor.execute("INSERT INTO roulette_log (roll, color, user_id, bet_amount, target, win_amount, timestamp) VALUES (%s,%s,%s,%s,%s,%s,%s)",
                (roll, color, user_id, bet_amount, target, win_amount, datetime.now().isoformat()))
            cursor.execute("DELETE FROM roulette_log WHERE id NOT IN (SELECT id FROM roulette_log WHERE roll IS NOT NULL ORDER BY id DESC LIMIT 50)")
            conn.commit()


def get_roulette_history():
    with get_db() as conn:
        with conn.cursor() as cursor:
            cursor.execute("SELECT roll, color FROM roulette_log WHERE roll IS NOT NULL ORDER BY id DESC LIMIT 10")
            return [{"roll": r[0], "color": r[1]} for r in cursor.fetchall()]


def save_last_bets(user_id: int, bets: list):
    with get_db() as conn:
        with conn.cursor() as cursor:
            cursor.execute("INSERT INTO user_last_bets (user_id, bets_json) VALUES (%s,%s) ON CONFLICT (user_id) DO UPDATE SET bets_json = EXCLUDED.bets_json",
                           (user_id, json.dumps(bets)))
            conn.commit()


def get_last_bets(user_id: int) -> list:
    with get_db() as conn:
        with conn.cursor() as cursor:
            cursor.execute("SELECT bets_json FROM user_last_bets WHERE user_id = %s", (user_id,))
            row = cursor.fetchone()
            if row and row[0]:
                try:
                    return json.loads(row[0])
                except:
                    return []
    return []


def get_promo(code: str):
    with get_db() as conn:
        with conn.cursor() as cursor:
            cursor.execute("SELECT code, amount, uses FROM promocodes WHERE code = %s", (code,))
            row = cursor.fetchone()
            if row:
                return {"code": row[0], "amount": row[1], "uses": row[2]}
    return None


def use_promo(user_id: int, code: str, amount: int):
    with get_db() as conn:
        with conn.cursor() as cursor:
            cursor.execute("SELECT 1 FROM used_promocodes WHERE user_id=%s AND code=%s", (user_id, code))
            if cursor.fetchone():
                return False
            cursor.execute("UPDATE promocodes SET uses = uses - 1 WHERE code = %s", (code,))
            cursor.execute("INSERT INTO used_promocodes (user_id, code) VALUES (%s,%s)", (user_id, code))
            cursor.execute("UPDATE users SET balance = balance + %s WHERE user_id = %s", (amount, user_id))
            conn.commit()
            return True


def delete_promo(code: str):
    with get_db() as conn:
        with conn.cursor() as cursor:
            cursor.execute("DELETE FROM promocodes WHERE code = %s", (code,))
            conn.commit()


def log_admin_action(admin_id: int, action: str, target_id: int = 0, amount: int = 0):
    with get_db() as conn:
        with conn.cursor() as cursor:
            cursor.execute("INSERT INTO admin_log (admin_id, action, target_id, amount, timestamp) VALUES (%s,%s,%s,%s,%s)",
                           (admin_id, action, target_id, amount, datetime.now().isoformat()))
            conn.commit()


def get_mention(user_id: int, first_name: str) -> str:
    safe_name = first_name if first_name else "Игрок"
    return f'<a href="tg://user?id={user_id}">{safe_name}</a>'


def in_group(message: Message) -> bool:
    return message.chat.type in ["group", "supergroup"]


def check_group_only(message: Message, game_name: str) -> bool:
    if message.from_user:
        get_user(message.from_user.id, message.from_user.first_name or "",
                 message.from_user.last_name or "", message.from_user.username or "")
    if not in_group(message):
        asyncio.create_task(message.answer("🎮 Игры доступны только в групповых чатах."))
        return False
    if is_game_disabled(game_name):
        asyncio.create_task(message.answer(f"❌ Игра {game_name} отключена администратором."))
        return False
    return True


def format_balance(amount) -> str:
    try:
        return f"{int(amount):,}".replace(",", " ") + f" {CURRENCY}"
    except:
        return f"{amount} {CURRENCY}"


def clean_first_name(first_name: str, user_id: int) -> str:
    if not first_name:
        return f"Пользователь - {user_id}"
    invisible = {"ㅤ", "ᅠ"}
    for ch in first_name:
        if ch.strip() and ch not in invisible:
            return first_name
    return f"Пользователь - {user_id}"


# ---------- VIP ----------
def get_vip_price(hours: int) -> int:
    return {24: 10000000, 72: 25000000, 168: 50000000, 336: 90000000, 720: 150000000}.get(hours, 0)


def is_vip(user_id: int) -> bool:
    user = get_user(user_id)
    if user.get('vip_until') and user['vip_until'] > datetime.now():
        return True
    return False


# ---------- Кланы ----------
def get_clan_by_name_or_id(identifier):
    with get_db() as conn:
        with conn.cursor() as cursor:
            if isinstance(identifier, int) or (isinstance(identifier, str) and identifier.isdigit()):
                cursor.execute("SELECT id, name, owner_id, balance FROM clans WHERE id = %s", (int(identifier),))
            else:
                cursor.execute("SELECT id, name, owner_id, balance FROM clans WHERE lower(name) = lower(%s)", (identifier,))
            row = cursor.fetchone()
            if row:
                return {"id": row[0], "name": row[1], "owner_id": row[2], "balance": row[3]}
    return None


def get_user_clan(user_id):
    with get_db() as conn:
        with conn.cursor() as cursor:
            cursor.execute("SELECT clan_id FROM users WHERE user_id = %s", (user_id,))
            row = cursor.fetchone()
            if row and row[0]:
                return get_clan_by_name_or_id(row[0])
    return None


# ---------- Кейсы ----------
def get_case_price(case_type: str) -> int:
    return {"деревянный": 10000000, "железный": 100000000, "золотой": 1000000000,
            "алмазный": 10000000000, "легендарный": 100000000000,
            "мифический": 1000000000000, "космический": 100000000000000}.get(case_type.lower(), 0)


def get_case_reward(case_type: str):
    r = random.random()
    if case_type == "деревянный":
        if r < 0.55: return ("nothing", 0)
        elif r < 0.80: return ("money", 4000000)
        elif r < 0.93: return ("money", 12000000)
        elif r < 0.985: return ("money", 40000000)
        elif r < 0.998: return ("money", 100000000)
        else: return ("money", 800000000)
    elif case_type == "железный":
        if r < 0.50: return ("nothing", 0)
        elif r < 0.75: return ("money", 25000000)
        elif r < 0.88: return ("money", 60000000)
        elif r < 0.96: return ("money", 150000000)
        elif r < 0.995: return ("money", 400000000)
        else: return ("vip", 6)
    elif case_type == "золотой":
        if r < 0.45: return ("nothing", 0)
        elif r < 0.70: return ("money", 80000000)
        elif r < 0.85: return ("money", 250000000)
        elif r < 0.95: return ("money", 700000000)
        elif r < 0.995: return ("money", 1800000000)
        else: return ("vip", 24)
    elif case_type == "алмазный":
        if r < 0.40: return ("nothing", 0)
        elif r < 0.65: return ("money", 400000000)
        elif r < 0.83: return ("money", 1500000000)
        elif r < 0.94: return ("money", 4000000000)
        elif r < 0.995: return ("money", 9000000000)
        else: return ("vip", 72)
    elif case_type == "легендарный":
        if r < 0.35: return ("nothing", 0)
        elif r < 0.60: return ("money", 1500000000)
        elif r < 0.80: return ("money", 8000000000)
        elif r < 0.92: return ("money", 40000000000)
        elif r < 0.995: return ("money", 90000000000)
        else: return ("vip", 168)
    elif case_type == "мифический":
        if r < 0.30: return ("nothing", 0)
        elif r < 0.55: return ("money", 8000000000)
        elif r < 0.75: return ("money", 80000000000)
        elif r < 0.87: return ("money", 400000000000)
        elif r < 0.995: return ("money", 900000000000)
        else: return ("vip", 336)
    elif case_type == "космический":
        if r < 0.25: return ("nothing", 0)
        elif r < 0.50: return ("money", 80000000000)
        elif r < 0.72: return ("money", 800000000000)
        elif r < 0.85: return ("money", 8000000000000)
        elif r < 0.995: return ("money", 80000000000000)
        else: return ("vip", 720)
    return ("nothing", 0)


# ---------- BTC / Майнинг ----------
def btc_to_coins(btc_amount: float) -> int:
    return int(btc_amount * int(get_setting("btc_rate", "1000000000")))


def coins_to_btc(coins: int) -> float:
    return coins / int(get_setting("btc_rate", "1000000000"))


def get_mining_hash(user_id: int) -> float:
    total = 0.0
    with get_db() as conn:
        with conn.cursor() as cursor:
            cursor.execute("SELECT card_type, count FROM miners WHERE user_id = %s", (user_id,))
            rows = cursor.fetchall()
    hash_rates = {"gt710": 0.00001, "rx580": 0.00005, "rtx3060": 0.0002, "rtx3080": 0.0005, "rtx3090": 0.002}
    for card_type, cnt in rows:
        total += hash_rates.get(card_type, 0) * cnt
    return total


def update_mining_accumulated(user_id: int):
    with get_db() as conn:
        with conn.cursor() as cursor:
            cursor.execute("SELECT accumulated_btc, last_update FROM mining_stats WHERE user_id = %s", (user_id,))
            row = cursor.fetchone()
            if not row:
                cursor.execute("INSERT INTO mining_stats (user_id, accumulated_btc, last_update) VALUES (%s, 0, %s) ON CONFLICT DO NOTHING", (user_id, datetime.now()))
                conn.commit()
                return 0.0
            accumulated, last_update = row
            if last_update is None:
                cursor.execute("UPDATE mining_stats SET last_update = %s WHERE user_id = %s", (datetime.now(), user_id))
                conn.commit()
                return float(accumulated or 0)
            delta_hours = (datetime.now() - last_update).total_seconds() / 3600
            mined = get_mining_hash(user_id) * delta_hours
            new_acc = float(accumulated or 0) + mined
            cursor.execute("UPDATE mining_stats SET accumulated_btc = %s, last_update = %s WHERE user_id = %s",
                           (new_acc, datetime.now(), user_id))
            conn.commit()
            return new_acc


# ---------- Депозит / Ограбление ----------
def deposit_interest(start_time: datetime, amount: int) -> int:
    hours = max(0, (datetime.now() - start_time).total_seconds() // 3600)
    return int(amount * (0.0005 * hours))


def can_rob(user_id: int) -> bool:
    user = get_user(user_id)
    if not user.get("last_robbery"):
        return True
    return (datetime.now() - user["last_robbery"]).total_seconds() >= 15 * 60


def perform_rob(user_id: int) -> dict:
    user = get_user(user_id)
    if user["balance"] < 500000:
        return {"success": False, "reason": "need_balance"}
    if not can_rob(user_id):
        return {"success": False, "reason": "cooldown"}
    roll = random.random()
    if roll < 0.40:
        win = random.choice([100000, 150000, 200000, 250000, 300000, 400000, 500000])
        update_balance(user_id, win)
        with get_db() as conn:
            with conn.cursor() as cursor:
                cursor.execute("UPDATE users SET last_robbery = %s WHERE user_id = %s", (datetime.now(), user_id))
                conn.commit()
        return {"success": True, "win_amount": win}
    else:
        new_bal = max(0, user["balance"] - 200000)
        with get_db() as conn:
            with conn.cursor() as cursor:
                cursor.execute("UPDATE users SET balance = %s, last_robbery = %s WHERE user_id = %s",
                               (new_bal, datetime.now(), user_id))
                conn.commit()
        return {"success": False, "win_amount": 0, "penalty": 200000}


# ============================================================
# КЛАВИАТУРЫ
# ============================================================
def get_main_menu(user_id=None):
    rows = [
        [KeyboardButton(text="👤 Профиль"), KeyboardButton(text="🎮 Мини-игры")],
        [KeyboardButton(text="🏆 Топ"), KeyboardButton(text="👥 Кланы")],
        [KeyboardButton(text="📋 Команды"), KeyboardButton(text="👛 Сейф")],
        [KeyboardButton(text="📢 Новости"), KeyboardButton(text="💬 Чат")],
        [KeyboardButton(text="🛒 Донат")]
    ]
    if user_id and is_admin(user_id):
        rows.insert(0, [KeyboardButton(text="🛡 Админ-панель")])
    return ReplyKeyboardMarkup(keyboard=rows, resize_keyboard=True)


def get_balance_keyboard(user_data: dict, is_in_group: bool):
    b_cool = int(get_setting("bonus_cooldown", "8"))
    bonus_available = True
    if user_data.get("last_bonus"):
        try:
            last_time = datetime.fromisoformat(user_data["last_bonus"])
            if datetime.now() < last_time + timedelta(hours=b_cool):
                bonus_available = False
        except:
            pass
    if bonus_available:
        if is_in_group:
            url = f"https://t.me/{BOT_USERNAME}?start=bonus"
            return InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text=f"Бонус 🎁", url=url)]])
        else:
            return InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="Бонус 🎁", callback_data="get_bonus_lc")]])
    return None


async def process_bonus_logic(user_id: int, first_name: str, is_group_context: bool):
    if is_group_context:
        return "❌ Ежедневный бонус можно получить только в личных сообщениях с ботом!"
    user = get_user(user_id, first_name)
    b_amt = int(get_setting("bonus_amount", "3000"))
    if is_vip(user_id):
        b_amt *= 2
    b_cool = int(get_setting("bonus_cooldown", "8"))
    if user.get("last_bonus"):
        try:
            last_time = datetime.fromisoformat(user["last_bonus"])
            cooldown_period = timedelta(hours=b_cool)
            if datetime.now() < last_time + cooldown_period:
                diff = (last_time + cooldown_period) - datetime.now()
                total_sec = int(diff.total_seconds())
                h, rem = divmod(total_sec, 3600)
                m, s = divmod(rem, 60)
                return f"⏳ Бонус уже получен.\n\nСледующий через\n<b>{h}:{m:02d}:{s:02d}</b>"
        except:
            pass
    update_balance(user_id, b_amt)
    with get_db() as conn:
        with conn.cursor() as cursor:
            cursor.execute("UPDATE users SET last_bonus = %s WHERE user_id = %s", (datetime.now().isoformat(), user_id))
            conn.commit()
    mention = get_mention(user_id, user['first_name'])
    updated = get_user(user_id)
    return f"🎁 {mention} получил бонус <b>{format_balance(b_amt)}</b>!\n\n💰 Баланс: {format_balance(updated['balance'])}"


# ============================================================
# БАЗОВЫЕ КОМАНДЫ
# ============================================================
@router.message(Command("start"))
async def cmd_start(message: Message):
    user_id = message.from_user.id
    fname = message.from_user.first_name or ""
    get_user(user_id, fname, message.from_user.last_name or "", message.from_user.username or "")
    args = message.text.split()
    if len(args) > 1 and args[1] == "bonus":
        if in_group(message):
            await message.answer("❌ Получить бонус можно только в личных сообщениях.")
            return
        await message.answer(await process_bonus_logic(user_id, fname, False))
        return
    if len(args) > 1 and args[1].startswith("ref"):
        try:
            ref_id = int(args[1].replace("ref", ""))
            if ref_id != user_id:
                with get_db() as conn:
                    with conn.cursor() as cursor:
                        cursor.execute("SELECT 1 FROM referrals WHERE user_id = %s", (user_id,))
                        if not cursor.fetchone():
                            cursor.execute("INSERT INTO referrals (user_id, referrer_id) VALUES (%s, %s)", (user_id, ref_id))
                            conn.commit()
                            ref_user = get_user(ref_id)
                            if ref_user and ref_user.get('referal_bonus_claimed', 0) < int(get_setting('referral_limit', '25')):
                                bonus = int(get_setting('referral_bonus', '75000'))
                                update_balance(ref_id, bonus)
                                cursor.execute("UPDATE users SET referred_count = referred_count + 1, referal_bonus_claimed = referal_bonus_claimed + 1 WHERE user_id = %s", (ref_id,))
                                conn.commit()
        except:
            pass
    if in_group(message):
        await message.answer(f"Привет! Я бот {BOT_NAME}. Напиши мне в личные сообщения для главного меню.")
        return
    welcome = (
        f"👋 <b>Добро пожаловать в {BOT_NAME}!</b>\n\n"
        f"🎰 Игры: Рулетка, Джокер, Мины, Дуэли, Coinflip\n"
        f"💎 Валюта: {CURRENCY}\n"
        f"💰 Начальный баланс: 4 000 {CURRENCY}\n\n"
        f"👨‍💻 Связь с разработчиком: @se7ze\n\n"
        f"Используй кнопки ниже для навигации."
    )
    await message.answer(welcome, reply_markup=get_main_menu(user_id))


# ---------- ССЫЛКИ ----------
CHANNEL_URL = "https://t.me/mintnews_ru"
CHAT_URL = "https://t.me/communitypublicchat"


@router.message(F.text == "📢 Новости")
async def cmd_news_btn(message: Message):
    kb = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="📢 Перейти в канал", url=CHANNEL_URL)
    ]])
    await message.answer(
        f"📢 <b>Новостной канал {BOT_NAME}</b>\n\n"
        f"Подпишись, чтобы первым узнавать:\n"
        f"• 🎁 Акции и промокоды\n"
        f"• 🎰 Новые игры и обновления\n"
        f"• 🏆 Розыгрыши и конкурсы\n"
        f"• 📢 Важные анонсы\n\n"
        f"👉 <a href='{CHANNEL_URL}'>Подписаться на канал</a>",
        reply_markup=kb
    )


@router.message(F.text == "💬 Чат")
async def cmd_chat_btn(message: Message):
    kb = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="💬 Перейти в чат", url=CHAT_URL)
    ]])
    await message.answer(
        f"💬 <b>Общий чат {BOT_NAME}</b>\n\n"
        f"В чате можно:\n"
        f"• 🗣 Общаться с другими игроками\n"
        f"• 🎰 Играть в игры прямо в чате\n"
        f"• ⚔️ Вызывать на дуэли\n"
        f"• 🤝 Искать клан\n"
        f"• 📸 Делиться скринами выигрышей\n\n"
        f"👉 <a href='{CHAT_URL}'>Зайти в чат</a>",
        reply_markup=kb
    )

@router.message(F.text == "🛒 Донат")
async def cmd_donate(message: Message):
    await message.answer("💝 Для поддержки проекта пиши @se7ze.")


@router.message(Command("help"))
async def cmd_help(message: Message):
    text = (
        f"📋 <b>Справка {BOT_NAME}</b>\n\n"
        f"🎰 <b>Игры:</b>\n"
        f"• <code>100 red</code> / <code>100 5</code> — Рулетка\n"
        f"• лог — История рулетки\n"
        f"• ставки — Активные ставки\n"
        f"• <code>джокер 100</code> — Джокер\n"
        f"• <code>мины 100</code> — Мины\n"
        f"• <code>coinflip 100</code> — Орёл/Решка\n"
        f"• <code>дуэль 100</code> (реплай) — Дуэль КНБ\n\n"
        f"💰 <b>Экономика:</b>\n"
        f"• п [сумма] [@user] [коммент] — Перевод\n"
        f"• б / баланс — Профиль\n"
        f"• /name [ник] — Сменить игровой ник\n"
        f"• /vip / /vip_buy [часы] — VIP\n"
        f"• банк — Депозит\n"
        f"• депозит [сумма] — Открыть (мин 5 млн)\n"
        f"• снять депозит — Закрыть\n"
        f"• ограбить — Ограбить казино (500к)\n\n"
        f"🎁 <b>Прочее:</b>\n"
        f"• сейф — Кейсы и BTC\n"
        f"• кейс купить/открыть [тип]\n"
        f"• майнинг / майнинг магазин / забрать / продать\n"
        f"• клан — Кланы\n"
        f"• /top — Топ игроков\n"
        f"• /promo [код] — Промокод\n"
        f"• /daily — Ежедневное задание\n"
        f"• /referral — Реферальная ссылка"
    )
    await message.answer(text)


@router.message(F.text == "🎮 Мини-игры")
async def cmd_minigames(message: Message):
    await message.answer(
        f"🎮 <b>Мини-игры</b>\n\n"
        f"🎰 <b>Рулетка</b>: <code>100 red</code> или <code>100 5 10 25</code>\n"
        f"🃏 <b>Джокер</b>: <code>джокер 100</code>\n"
        f"💣 <b>Мины</b>: <code>мины 100</code>\n"
        f"⚔️ <b>Дуэли</b>: <code>дуэль 100</code> (реплай)\n"
        f"🪙 <b>Coinflip</b>: <code>coinflip 100</code>"
    )


@router.message(F.text == "📋 Команды")
async def cmd_commands_btn(message: Message):
    await cmd_help(message)


@router.message(Command("balance"))
async def cmd_balance(message: Message):
    user_id = message.from_user.id
    user = get_user(user_id, message.from_user.first_name or "",
                    message.from_user.last_name or "", message.from_user.username or "")
    rank = get_rank(user_id)
    rank_str = get_rank_emoji(rank)
    mention = get_mention(user_id, user['first_name'])
    text = f"👤 Профиль {mention}\n"
    if rank_str:
        text += f"{rank_str}\n"
    if user.get('custom_nick'):
        text += f"🏷️ {user['custom_nick']}\n"
    if is_vip(user_id):
        text += "⚜️ VIP\n"
    text += f"💰 Баланс: <b>{format_balance(user['balance'])}</b>\n"
    if user.get('btc') and user['btc'] > 0:
        text += f"💎 BTC: {user['btc']:.4f}\n"
    text += f"🎮 Игр: {user['games_played']} | Побед: {user['games_won']}\n"
    if user.get('referrer_id'):
        ref_user = get_user(user['referrer_id'])
        text += f"👥 Пригласил: {get_mention(user['referrer_id'], ref_user['first_name'])}\n"
    await message.answer(text, reply_markup=get_balance_keyboard(user, in_group(message)))


@router.message(F.text.lower().in_(["б", "баланс", "👤 профиль"]))
async def cmd_balance_text(message: Message):
    await cmd_balance(message)


@router.callback_query(F.data == "get_bonus_lc")
async def callback_bonus_lc(callback: CallbackQuery):
    await callback.answer()
    result = await process_bonus_logic(callback.from_user.id, callback.from_user.first_name or "", False)
    await callback.message.answer(result)


@router.message(Command("top"))
async def cmd_top(message: Message):
    limit = 10
    args = message.text.split()
    if len(args) > 1 and args[1].isdigit():
        limit = int(args[1])
    with get_db() as conn:
        with conn.cursor() as cursor:
            cursor.execute("SELECT user_id, first_name, balance FROM users ORDER BY balance DESC LIMIT %s", (limit,))
            rows = cursor.fetchall()
    if not rows:
        await message.answer("🏆 Топ пуст.")
        return
    medals = {1: "🥇", 2: "🥈", 3: "🥉"}
    text = f"🏆 <b>Топ {len(rows)} игроков:</b>\n\n"
    for i, row in enumerate(rows, 1):
        uid, fname, bal = row
        display = clean_first_name(fname, uid)
        text += f"{medals.get(i, '')} {i}. <a href='tg://user?id={uid}'>{display}</a> — <b>{format_balance(bal)}</b>\n"
    await message.answer(text)


@router.message(F.text == "🏆 Топ")
async def cmd_top_btn(message: Message):
    await cmd_top(message)


@router.message(Command("bonus"))
async def cmd_bonus(message: Message):
    res = await process_bonus_logic(message.from_user.id, message.from_user.first_name or "", in_group(message))
    await message.answer(res)


@router.message(F.text.lower() == "бонус")
async def cmd_bonus_text(message: Message):
    await cmd_bonus(message)


@router.message(Command("daily"))
async def cmd_daily(message: Message):
    if in_group(message):
        return
    user_id = message.from_user.id
    user = get_user(user_id)
    today = datetime.now().strftime("%Y-%m-%d")
    if user.get('last_daily') == today:
        await message.answer("✅ Вы уже получили ежедневное задание сегодня.")
        return
    tasks = [
        {"type": "roulette_play", "desc": "Сыграйте в рулетку 3 раза", "target": 3, "reward": 5000},
        {"type": "joker_play", "desc": "Сыграйте в Джокера 2 раза", "target": 2, "reward": 4000},
        {"type": "win_game", "desc": "Выиграйте 1 раз в любую игру", "target": 1, "reward": 6000}
    ]
    task = random.choice(tasks)
    with get_db() as conn:
        with conn.cursor() as cursor:
            cursor.execute("INSERT INTO daily_tasks (user_id, date, task_type) VALUES (%s,%s,%s) ON CONFLICT DO NOTHING",
                           (user_id, today, task['type']))
            cursor.execute("UPDATE users SET last_daily = %s WHERE user_id = %s", (today, user_id))
            conn.commit()
    await message.answer(
        f"📋 <b>Ежедневное задание:</b>\n\n{task['desc']}\n\n"
        f"🎁 Награда: <b>{format_balance(task['reward'])}</b>\n\n"
        f"Прогресс: 0/{task['target']}"
    )


@router.message(Command("referral"))
async def cmd_referral(message: Message):
    user_id = message.from_user.id
    user = get_user(user_id)
    ref_link = f"https://t.me/{BOT_USERNAME}?start=ref{user_id}"
    bonus = int(get_setting('referral_bonus', '75000'))
    limit = int(get_setting('referral_limit', '25'))
    await message.answer(
        f"👥 <b>Реферальная система</b>\n\n"
        f"Ваша ссылка:\n<code>{ref_link}</code>\n\n"
        f"Приглашено: <b>{user['referred_count']}</b>\n"
        f"Бонус за каждого: <b>{format_balance(bonus)}</b>\n"
        f"Лимит бонусов: <b>{user.get('referal_bonus_claimed', 0)}/{limit}</b>"
    )


@router.message(Command("promo"))
async def cmd_promo(message: Message):
    args = message.text.split()
    if len(args) < 2:
        await message.answer("❌ Использование: <code>/promo [код]</code>")
        return
    code = args[1]
    promo = get_promo(code)
    if not promo:
        await message.answer("❌ Промокод не найден.")
        return
    if promo['uses'] <= 0:
        await message.answer("❌ Лимит использований исчерпан.")
        return
    if use_promo(message.from_user.id, code, promo['amount']):
        await message.answer(f"✅ Промокод активирован! Начислено <b>{format_balance(promo['amount'])}</b>.")
    else:
        await message.answer("❌ Вы уже активировали этот промокод.")


@router.message(F.text.lower().startswith("п "))
async def cmd_transfer(message: Message):
    parts = message.text.split()
    if len(parts) < 2 or not parts[1].isdigit():
        return
    amount = int(parts[1])
    if amount <= 0:
        await message.answer("❌ Сумма должна быть больше 0.")
        return
    user = get_user(message.from_user.id)
    if user['balance'] < amount:
        await message.answer("❌ Недостаточно средств.")
        return
    target = None
    comment = ""
    if message.reply_to_message and message.reply_to_message.from_user:
        target = get_user(message.reply_to_message.from_user.id)
        if len(parts) > 2:
            comment = " ".join(parts[2:])
    elif len(parts) > 2:
        target = find_user_by_identifier(parts[2])
        if len(parts) > 3:
            comment = " ".join(parts[3:])
    if not target:
        await message.answer("❌ Получатель не найден.")
        return
    if target['user_id'] == message.from_user.id:
        await message.answer("❌ Нельзя перевести самому себе.")
        return
    update_balance(user['user_id'], -amount)
    update_balance(target['user_id'], amount)
    msg = f"💸 {get_mention(user['user_id'], user['first_name'])} перевел {get_mention(target['user_id'], target['first_name'])} <b>{format_balance(amount)}</b>."
    if comment:
        msg += f"\n💬 {comment}"
    await message.answer(msg)


@router.message(Command("name"))
async def cmd_change_name(message: Message):
    args = message.text.split(maxsplit=1)
    if len(args) < 2:
        await message.answer("Использование: <code>/name [новое_имя]</code>")
        return
    new_nick = args[1].strip()[:32]
    if not new_nick:
        await message.answer("❌ Ник не может быть пустым.")
        return
    with get_db() as conn:
        with conn.cursor() as cursor:
            cursor.execute("UPDATE users SET custom_nick = %s WHERE user_id = %s", (new_nick, message.from_user.id))
            conn.commit()
    await message.answer(f"✅ Ваш игровой ник: 🏷️ {new_nick}")


@router.message(Command("vip"))
async def cmd_vip(message: Message):
    user = get_user(message.from_user.id)
    if is_vip(message.from_user.id):
        until = user['vip_until'].strftime('%d.%m.%Y %H:%M')
        await message.answer(f"👑 <b>VIP-статус</b>\n\n⚜️ Ты VIP\n📅 До {until}\n🎁 Бонус x2 действует")
    else:
        await message.answer(
            f"👑 <b>VIP-статус</b>\n\nУ тебя нет VIP.\n\n"
            f"<b>Купить:</b> <code>vip_buy [часы]</code>\n"
            f"• 24 ч — {format_balance(10000000)}\n"
            f"• 72 ч — {format_balance(25000000)}\n"
            f"• 168 ч — {format_balance(50000000)}\n"
            f"• 336 ч — {format_balance(90000000)}\n"
            f"• 720 ч — {format_balance(150000000)}"
        )


@router.message(Command("vip_buy"))
async def cmd_vip_buy(message: Message):
    args = message.text.split()
    if len(args) < 2 or not args[1].isdigit():
        await message.answer("Использование: <code>/vip_buy [часы]</code>")
        return
    hours = int(args[1])
    price = get_vip_price(hours)
    if price == 0:
        await message.answer("❌ Неверный срок.")
        return
    user = get_user(message.from_user.id)
    if user['balance'] < price:
        await message.answer("❌ Недостаточно средств.")
        return
    update_balance(message.from_user.id, -price)
    current_until = user.get('vip_until')
    new_until = (current_until + timedelta(hours=hours)) if (current_until and current_until > datetime.now()) \
        else (datetime.now() + timedelta(hours=hours))
    with get_db() as conn:
        with conn.cursor() as cursor:
            cursor.execute("UPDATE users SET vip_until = %s WHERE user_id = %s", (new_until, message.from_user.id))
            conn.commit()
    await message.answer(f"✅ VIP активирован до {new_until.strftime('%d.%m.%Y %H:%M')}")


@router.message(F.text.lower() == "vip")
async def cmd_vip_text(message: Message):
    await cmd_vip(message)


@router.message(F.text.lower().startswith("vip_buy"))
async def cmd_vip_buy_text(message: Message):
    await cmd_vip_buy(message)

# =============================================================
# MINT Bot — Часть 2/4: Экономика
# =============================================================

# ============================================================
# КЛАНЫ
# ============================================================
CLAN_HELP = (
    f"🏰 <b>Кланы {BOT_NAME}</b>\n\n"
    f"• <code>клан создать &lt;название&gt;</code> — {format_balance(100000000)}\n"
    f"• <code>клан вступить &lt;название/id&gt;</code>\n"
    f"• <code>клан выйти</code>\n"
    f"• <code>клан инфо</code>\n"
    f"• <code>клан казна &lt;сумма&gt;</code>\n"
    f"• <code>клан снять &lt;сумма&gt;</code> (владелец)\n"
    f"• <code>клан топ</code>"
)


@router.message(F.text == "👥 Кланы")
async def cmd_clan_btn(message: Message):
    await message.answer(CLAN_HELP)


@router.message(F.text.lower().startswith(("clan", "клан")))
async def clan_cmd(message: Message):
    if message.text.lower() in ("клан", "clan"):
        await message.answer(CLAN_HELP)
        return
    args = message.text.split()
    if len(args) < 2:
        await message.answer(CLAN_HELP)
        return
    sub = args[1].lower()

    if sub == "создать":
        if len(args) < 3:
            await message.answer("Укажи название (англ. буквы, 3-20 символов).")
            return
        name = args[2]
        if not re.match("^[a-zA-Z0-9_]{3,20}$", name):
            await message.answer("❌ Название: 3-20 символов, англ. буквы/цифры/_")
            return
        if get_user_clan(message.from_user.id):
            await message.answer("❌ Ты уже в клане.")
            return
        price = 100000000
        user = get_user(message.from_user.id)
        if user['balance'] < price:
            await message.answer(f"❌ Нужно {format_balance(price)} для создания.")
            return
        with get_db() as conn:
            with conn.cursor() as cursor:
                cursor.execute("SELECT id FROM clans WHERE lower(name) = lower(%s)", (name,))
                if cursor.fetchone():
                    await message.answer("❌ Клан с таким названием уже существует.")
                    return
                update_balance(message.from_user.id, -price)
                cursor.execute("INSERT INTO clans (name, owner_id, balance) VALUES (%s, %s, 0) RETURNING id",
                               (name, message.from_user.id))
                clan_id = cursor.fetchone()[0]
                cursor.execute("INSERT INTO clan_members (clan_id, user_id) VALUES (%s, %s)", (clan_id, message.from_user.id))
                cursor.execute("UPDATE users SET clan_id = %s WHERE user_id = %s", (clan_id, message.from_user.id))
                conn.commit()
        await message.answer(f"🏰 Клан '<b>{name}</b>' создан! ID: {clan_id}")

    elif sub == "вступить":
        if len(args) < 3:
            await message.answer("Укажи название или ID клана.")
            return
        clan = get_clan_by_name_or_id(args[2])
        if not clan:
            await message.answer("❌ Клан не найден.")
            return
        if get_user_clan(message.from_user.id):
            await message.answer("❌ Ты уже в клане.")
            return
        with get_db() as conn:
            with conn.cursor() as cursor:
                cursor.execute("INSERT INTO clan_members (clan_id, user_id) VALUES (%s, %s)", (clan['id'], message.from_user.id))
                cursor.execute("UPDATE users SET clan_id = %s WHERE user_id = %s", (clan['id'], message.from_user.id))
                conn.commit()
        await message.answer(f"✅ Ты вступил в клан '<b>{clan['name']}</b>'.")

    elif sub == "выйти":
        clan = get_user_clan(message.from_user.id)
        if not clan:
            await message.answer("Ты не в клане.")
            return
        with get_db() as conn:
            with conn.cursor() as cursor:
                cursor.execute("DELETE FROM clan_members WHERE clan_id = %s AND user_id = %s", (clan['id'], message.from_user.id))
                cursor.execute("UPDATE users SET clan_id = NULL WHERE user_id = %s", (message.from_user.id,))
                conn.commit()
        await message.answer("✅ Ты вышел из клана.")

    elif sub == "инфо":
        clan = get_user_clan(message.from_user.id)
        if not clan:
            await message.answer("Ты не в клане.")
            return
        with get_db() as conn:
            with conn.cursor() as cursor:
                cursor.execute("SELECT COUNT(*) FROM clan_members WHERE clan_id = %s", (clan['id'],))
                cnt = cursor.fetchone()[0]
        owner = get_user(clan['owner_id'])
        await message.answer(
            f"🏰 <b>{clan['name']}</b> (ID: {clan['id']})\n"
            f"👑 Владелец: {get_mention(clan['owner_id'], owner['first_name'])}\n"
            f"👥 Участников: {cnt}\n"
            f"💰 Казна: {format_balance(clan['balance'])}"
        )

    elif sub == "казна":
        if len(args) < 3 or not args[2].isdigit():
            await message.answer("Укажи сумму: <code>клан казна 1000000</code>")
            return
        amount = int(args[2])
        clan = get_user_clan(message.from_user.id)
        if not clan:
            await message.answer("Ты не в клане.")
            return
        user = get_user(message.from_user.id)
        if user['balance'] < amount:
            await message.answer("❌ Недостаточно средств.")
            return
        update_balance(message.from_user.id, -amount)
        with get_db() as conn:
            with conn.cursor() as cursor:
                cursor.execute("UPDATE clans SET balance = balance + %s WHERE id = %s", (amount, clan['id']))
                conn.commit()
        await message.answer(f"💰 Казна пополнена на {format_balance(amount)}.")

    elif sub == "снять":
        if len(args) < 3 or not args[2].isdigit():
            await message.answer("Укажи сумму.")
            return
        amount = int(args[2])
        clan = get_user_clan(message.from_user.id)
        if not clan or clan['owner_id'] != message.from_user.id:
            await message.answer("❌ Только владелец может снимать.")
            return
        if clan['balance'] < amount:
            await message.answer("❌ Недостаточно в казне.")
            return
        with get_db() as conn:
            with conn.cursor() as cursor:
                cursor.execute("UPDATE clans SET balance = balance - %s WHERE id = %s", (amount, clan['id']))
                cursor.execute("UPDATE users SET balance = balance + %s WHERE user_id = %s", (amount, message.from_user.id))
                conn.commit()
        await message.answer(f"✅ Снято {format_balance(amount)}.")

    elif sub == "топ":
        with get_db() as conn:
            with conn.cursor() as cursor:
                cursor.execute("SELECT id, name, balance FROM clans ORDER BY balance DESC LIMIT 10")
                rows = cursor.fetchall()
        if not rows:
            await message.answer("Кланов пока нет.")
            return
        text = "🏆 <b>Топ кланов:</b>\n\n"
        for i, r in enumerate(rows, 1):
            text += f"{i}. <b>{r[1]}</b> — {format_balance(r[2])}\n"
        await message.answer(text)

    else:
        await message.answer(CLAN_HELP)


# ============================================================
# СЕЙФ / КЕЙСЫ
# ============================================================
@router.message(F.text.lower().in_(["сейф", "safe"]))
async def safe_cmd(message: Message):
    user = get_user(message.from_user.id)
    with get_db() as conn:
        with conn.cursor() as cursor:
            cursor.execute("SELECT case_type, count FROM cases_inventory WHERE user_id = %s", (message.from_user.id,))
            cases = cursor.fetchall()
    text = "👛 <b>Твой сейф</b>\n\n"
    if cases:
        for c in cases:
            text += f"📦 {c[0]} ×{c[1]}\n"
    else:
        text += "📦 Кейсов нет.\n"
    if user.get('btc', 0) > 0:
        text += f"\n💎 BTC: {user['btc']:.8f}"
    await message.answer(text)


@router.message(F.text == "👛 Сейф")
async def cmd_safe_btn(message: Message):
    await safe_cmd(message)


CASE_HELP = (
    f"📦 <b>Кейсы {BOT_NAME}</b>\n\n"
    f"<b>Купить:</b> <code>кейс купить &lt;тип&gt;</code>\n"
    f"<b>Открыть:</b> <code>кейс открыть &lt;тип&gt;</code>\n\n"
    f"<b>Список:</b>\n"
    f"• деревянный — {format_balance(10000000)}\n"
    f"• железный — {format_balance(100000000)}\n"
    f"• золотой — {format_balance(1000000000)}\n"
    f"• алмазный — {format_balance(10000000000)}\n"
    f"• легендарный — {format_balance(100000000000)}\n"
    f"• мифический — {format_balance(1000000000000)}\n"
    f"• космический — {format_balance(100000000000000)}"
)


@router.message(F.text.lower().startswith(("кейс", "case")))
async def case_cmd(message: Message):
    args = message.text.split()
    if len(args) < 3:
        await message.answer(CASE_HELP)
        return
    sub = args[1].lower()
    case_type = args[2].lower()

    if sub == "купить":
        price = get_case_price(case_type)
        if price == 0:
            await message.answer("❌ Неизвестный тип кейса.")
            return
        user = get_user(message.from_user.id)
        if user['balance'] < price:
            await message.answer(f"❌ Нужно {format_balance(price)}.")
            return
        update_balance(message.from_user.id, -price)
        with get_db() as conn:
            with conn.cursor() as cursor:
                cursor.execute(
                    "INSERT INTO cases_inventory (user_id, case_type, count) VALUES (%s, %s, 1) "
                    "ON CONFLICT (user_id, case_type) DO UPDATE SET count = cases_inventory.count + 1",
                    (message.from_user.id, case_type))
                conn.commit()
        await message.answer(f"📦 Кейс '<b>{case_type}</b>' куплен за {format_balance(price)}.")

    elif sub == "открыть":
        with get_db() as conn:
            with conn.cursor() as cursor:
                cursor.execute("SELECT count FROM cases_inventory WHERE user_id = %s AND case_type = %s",
                               (message.from_user.id, case_type))
                row = cursor.fetchone()
                if not row or row[0] <= 0:
                    await message.answer("❌ У тебя нет такого кейса.")
                    return
                if row[0] == 1:
                    cursor.execute("DELETE FROM cases_inventory WHERE user_id = %s AND case_type = %s",
                                   (message.from_user.id, case_type))
                else:
                    cursor.execute("UPDATE cases_inventory SET count = count - 1 WHERE user_id = %s AND case_type = %s",
                                   (message.from_user.id, case_type))
                conn.commit()

        rtype, rval = get_case_reward(case_type)
        if rtype == "nothing":
            await message.answer("📦 Открыл кейс... <b>пусто</b> 😢")
        elif rtype == "money":
            update_balance(message.from_user.id, rval)
            await message.answer(f"🎉 Из кейса выпало <b>{format_balance(rval)}</b>!")
        elif rtype == "vip":
            user = get_user(message.from_user.id)
            cu = user.get('vip_until')
            new_until = (cu + timedelta(hours=rval)) if (cu and cu > datetime.now()) else (datetime.now() + timedelta(hours=rval))
            with get_db() as conn:
                with conn.cursor() as cursor:
                    cursor.execute("UPDATE users SET vip_until = %s WHERE user_id = %s", (new_until, message.from_user.id))
                    conn.commit()
            await message.answer(f"👑 Из кейса выпал <b>VIP на {rval} часов</b>!")
    else:
        await message.answer(CASE_HELP)


# ============================================================
# BTC
# ============================================================
@router.message(F.text.lower().in_(["btc", "биткоин"]))
async def btc_cmd(message: Message):
    user = get_user(message.from_user.id)
    rate = int(get_setting("btc_rate", "1000000000"))
    await message.answer(
        f"💎 <b>Биткоин</b>\n\n"
        f"📈 Курс: 1 BTC = <b>{format_balance(rate)}</b>\n"
        f"💰 Твой BTC: <b>{user['btc']:.8f}</b>\n"
        f"💵 В COINS: ~{format_balance(btc_to_coins(user['btc']))}\n\n"
        f"<code>продать &lt;кол-во&gt;</code> — продать BTC\n"
        f"<code>продать всё</code> — продать всё"
    )


@router.message(F.text.lower().startswith(("продать", "sell")))
async def sell_btc(message: Message):
    args = message.text.split()
    user = get_user(message.from_user.id)
    if len(args) > 1 and args[1].lower() in ["всё", "все", "all"]:
        btc_amt = user['btc']
        if btc_amt <= 0:
            await message.answer("❌ У тебя нет BTC.")
            return
    else:
        if len(args) < 2:
            await message.answer("Использование: <code>продать &lt;кол-во&gt;</code> или <code>продать всё</code>")
            return
        try:
            btc_amt = float(args[1])
        except:
            await message.answer("❌ Введи число.")
            return
    if user['btc'] < btc_amt:
        await message.answer("❌ Недостаточно BTC.")
        return
    rate = int(get_setting("btc_rate", "1000000000"))
    coins_amt = int(btc_amt * rate)
    commission = int(coins_amt * 0.02)
    receive = coins_amt - commission
    with get_db() as conn:
        with conn.cursor() as cursor:
            cursor.execute("UPDATE users SET btc = btc - %s, balance = balance + %s WHERE user_id = %s",
                           (btc_amt, receive, message.from_user.id))
            conn.commit()
    await message.answer(
        f"✅ Продано {btc_amt:.8f} BTC за <b>{format_balance(receive)}</b>\n"
        f"💸 Комиссия (2%): {format_balance(commission)}"
    )


# ============================================================
# МАЙНИНГ
# ============================================================
MINING_PRICES = {"gt710": 1000000, "rx580": 10000000, "rtx3060": 100000000,
                 "rtx3080": 500000000, "rtx3090": 2000000000}
MINING_RATES = {"gt710": 0.00001, "rx580": 0.00005, "rtx3060": 0.0002,
                "rtx3080": 0.0005, "rtx3090": 0.002}


@router.message(F.text.lower().startswith(("майнинг магазин", "магазин майнинг")))
async def mining_shop(message: Message):
    text = f"🛒 <b>Магазин видеокарт</b>\n\n"
    for card, price in MINING_PRICES.items():
        text += f"• <b>{card}</b> — {format_balance(price)} ({MINING_RATES[card]} BTC/ч)\n"
    text += f"\n<b>Купить:</b> <code>майнинг купить &lt;тип&gt; &lt;кол-во&gt;</code>\n"
    text += f"⚠️ Лимит: 10 карт суммарно"
    await message.answer(text)


@router.message(F.text.lower().startswith(("майнинг", "mining")))
async def mining_cmd(message: Message):
    args = message.text.split()

    if len(args) >= 4 and args[1].lower() in ["купить", "buy"]:
        card_type = args[2].lower()
        try:
            count = int(args[3])
        except ValueError:
            await message.answer("❌ Количество — число.")
            return
        if card_type not in MINING_PRICES:
            await message.answer("❌ Неизвестный тип. Доступно: gt710, rx580, rtx3060, rtx3080, rtx3090")
            return
        if count < 1:
            await message.answer("❌ Количество > 0.")
            return
        with get_db() as conn:
            with conn.cursor() as cursor:
                cursor.execute("SELECT COALESCE(SUM(count),0) FROM miners WHERE user_id = %s", (message.from_user.id,))
                current = cursor.fetchone()[0]
        if current + count > 10:
            await message.answer(f"❌ Максимум 10 карт. У тебя: {current}.")
            return
        total_cost = MINING_PRICES[card_type] * count
        user = get_user(message.from_user.id)
        if user['balance'] < total_cost:
            await message.answer(f"❌ Нужно {format_balance(total_cost)}.")
            return
        update_balance(message.from_user.id, -total_cost)
        with get_db() as conn:
            with conn.cursor() as cursor:
                cursor.execute(
                    "INSERT INTO miners (user_id, card_type, count) VALUES (%s, %s, %s) "
                    "ON CONFLICT (user_id, card_type) DO UPDATE SET count = miners.count + EXCLUDED.count",
                    (message.from_user.id, card_type, count))
                conn.commit()
        await message.answer(f"✅ Куплено {count} шт. <b>{card_type}</b>\n💸 Списано: {format_balance(total_cost)}")
        return

    if len(args) < 2:
        accumulated = update_mining_accumulated(message.from_user.id)
        with get_db() as conn:
            with conn.cursor() as cursor:
                cursor.execute("SELECT card_type, count FROM miners WHERE user_id = %s", (message.from_user.id,))
                rows = cursor.fetchall()
        if not rows:
            await message.answer(
                f"⛏ <b>Майнинг-ферма</b>\n\n"
                f"У тебя нет видеокарт.\n\n"
                f"<code>майнинг магазин</code> — список карт\n"
                f"<code>майнинг купить &lt;тип&gt; &lt;кол-во&gt;</code>"
            )
            return
        total = get_mining_hash(message.from_user.id)
        text = f"⛏ <b>Твоя майнинг-ферма</b>\n\n"
        for card_type, cnt in rows:
            text += f"• {card_type} ×{cnt}: {MINING_RATES.get(card_type, 0) * cnt:.8f} BTC/ч\n"
        text += f"\n📊 Всего: <b>{total:.8f} BTC/ч</b>\n"
        text += f"💰 Накоплено: <b>{accumulated:.8f} BTC</b>\n"
        text += f"💵 ~{format_balance(btc_to_coins(accumulated))}\n\n"
        text += f"<code>забрать</code> — забрать накопленное\n"
        text += f"<code>майнинг магазин</code> — магазин"
        await message.answer(text)
        return

    await message.answer("❌ Неизвестная команда.")


@router.message(F.text.lower().startswith(("забрать", "collect")))
async def collect_mining(message: Message):
    accumulated = update_mining_accumulated(message.from_user.id)
    if accumulated <= 0:
        await message.answer("❌ Нет накопленного BTC.")
        return
    btc_amt = float(accumulated)
    with get_db() as conn:
        with conn.cursor() as cursor:
            cursor.execute("UPDATE mining_stats SET accumulated_btc = 0 WHERE user_id = %s", (message.from_user.id,))
            cursor.execute("UPDATE users SET btc = btc + %s WHERE user_id = %s", (btc_amt, message.from_user.id))
            conn.commit()
    await message.answer(f"✅ Забрано {btc_amt:.8f} BTC (~{format_balance(btc_to_coins(btc_amt))}).")


# ============================================================
# БАНК / ДЕПОЗИТ
# ============================================================
@router.message(F.text.lower() == "банк")
async def bank_info(message: Message):
    with get_db() as conn:
        with conn.cursor() as cursor:
            cursor.execute("SELECT amount, start_time FROM deposits WHERE user_id = %s", (message.from_user.id,))
            row = cursor.fetchone()
    if not row:
        await message.answer(
            f"🏦 <b>Банк</b>\n\n"
            f"У вас нет депозита.\n\n"
            f"<code>депозит &lt;сумма&gt;</code> — открыть (мин {format_balance(5000000)})\n"
            f"📈 0.05% в час"
        )
        return
    amount, start_time = row
    interest = deposit_interest(start_time, amount)
    total = amount + interest
    await message.answer(
        f"🏦 <b>Ваш депозит</b>\n\n"
        f"💰 Сумма: {format_balance(amount)}\n"
        f"📈 Проценты: {format_balance(interest)}\n"
        f"💎 Итого: {format_balance(total)}\n"
        f"💸 Комиссия снятия: 2%\n\n"
        f"<code>снять депозит</code> — закрыть"
    )


@router.message(F.text.lower().startswith("депозит "))
async def deposit_open(message: Message):
    parts = message.text.split()
    if len(parts) < 2 or not parts[1].isdigit():
        await message.answer(f"Использование: <code>депозит &lt;сумма&gt;</code> (мин {format_balance(5000000)})")
        return
    amount = int(parts[1])
    if amount < 5000000:
        await message.answer(f"❌ Минимум {format_balance(5000000)}.")
        return
    user = get_user(message.from_user.id)
    if user['balance'] < amount:
        await message.answer("❌ Недостаточно средств.")
        return
    with get_db() as conn:
        with conn.cursor() as cursor:
            cursor.execute("SELECT 1 FROM deposits WHERE user_id = %s", (message.from_user.id,))
            if cursor.fetchone():
                await message.answer("❌ Депозит уже открыт. Сначала снимите.")
                return
            update_balance(message.from_user.id, -amount)
            cursor.execute("INSERT INTO deposits (user_id, amount, start_time) VALUES (%s, %s, %s)",
                           (message.from_user.id, amount, datetime.now()))
            conn.commit()
    await message.answer(f"✅ Депозит открыт на {format_balance(amount)}.\n📈 0.05% в час.")


@router.message(F.text.lower() == "снять депозит")
async def deposit_withdraw(message: Message):
    with get_db() as conn:
        with conn.cursor() as cursor:
            cursor.execute("SELECT amount, start_time FROM deposits WHERE user_id = %s", (message.from_user.id,))
            row = cursor.fetchone()
            if not row:
                await message.answer("❌ У вас нет открытого депозита.")
                return
            amount, start_time = row
            interest = deposit_interest(start_time, amount)
            total = amount + interest
            commission = int(total * 0.02)
            to_bal = total - commission
            cursor.execute("DELETE FROM deposits WHERE user_id = %s", (message.from_user.id,))
            cursor.execute("UPDATE users SET balance = balance + %s WHERE user_id = %s", (to_bal, message.from_user.id))
            conn.commit()
    await message.answer(
        f"✅ Депозит закрыт.\n\n"
        f"💰 Сумма: {format_balance(amount)}\n"
        f"📈 Проценты: {format_balance(interest)}\n"
        f"💸 Комиссия (2%): {format_balance(commission)}\n"
        f"💎 Зачислено: <b>{format_balance(to_bal)}</b>"
    )


# ============================================================
# ОГРАБЛЕНИЕ
# ============================================================
@router.message(F.text.lower() == "ограбить")
async def rob_cmd(message: Message):
    if not check_group_only(message, "ограбление"):
        return
    result = perform_rob(message.from_user.id)
    if not result["success"]:
        if result.get("reason") == "need_balance":
            await message.answer(f"❌ Нужно минимум {format_balance(500000)} для ограбления.")
        elif result.get("reason") == "cooldown":
            await message.answer("❌ Ограбление раз в 15 минут.")
        else:
            await message.answer(f"❌ Неудача. Штраф: {format_balance(result.get('penalty', 0))}")
    else:
        await message.answer(f"🎉 Ограбление успешно! Украл {format_balance(result['win_amount'])}.")

# =============================================================
# MINT Bot — Часть 3/4: Игры
# =============================================================

# ============================================================
# ДЖОКЕР
# ============================================================
joker_sessions = {}
JOKER_MULTIS = [1.0, 1.5, 2.0, 2.5, 3.0, 4.0, 5.0, 7.0, 10.0]


def get_joker_kb(session_id, finished=False):
    sess = joker_sessions.get(session_id)
    kb = []
    if sess and "history" in sess:
        for row in sess["history"]:
            kb.append(row)
    if not finished:
        row = [
            InlineKeyboardButton(text="🎴", callback_data=f"jk_{session_id}_0"),
            InlineKeyboardButton(text="🎴", callback_data=f"jk_{session_id}_1"),
            InlineKeyboardButton(text="🎴", callback_data=f"jk_{session_id}_2")
        ]
        kb.append(row)
        btn_text = "🟢 💰 Забрать" if sess and sess["level"] > 0 else "💰 Забрать"
        kb.append([InlineKeyboardButton(text=btn_text, callback_data=f"jk_cash_{session_id}")])
    return InlineKeyboardMarkup(inline_keyboard=kb)


@router.message(F.text.lower().startswith("джокер"))
async def game_joker(message: Message):
    if not check_group_only(message, "джокер"):
        return
    args = message.text.split()
    if len(args) < 2 or not args[1].isdigit():
        await message.answer("❌ Использование: <code>джокер [ставка]</code>")
        return
    bet = int(args[1])
    if bet < MIN_BET:
        await message.answer(f"Минимальная ставка {MIN_BET} {CURRENCY}")
        return
    user = get_user(message.from_user.id, message.from_user.first_name or "",
                    message.from_user.last_name or "", message.from_user.username or "")
    if user["balance"] < bet:
        await message.answer("❌ Недостаточно средств")
        return
    update_balance(user["user_id"], -bet)
    session_id = f"{message.from_user.id}_{message.message_id}"
    skull_pos = random.randint(0, 2)
    joker_sessions[session_id] = {
        "user_id": user["user_id"], "user_name": user["first_name"],
        "bet": bet, "level": 0, "skull_pos": skull_pos, "history": []
    }
    mention = get_mention(user["user_id"], user["first_name"])
    await message.answer(
        f"🃏 {mention}, вы начали Джокер!\n"
        f"💰 Ставка: {format_balance(bet)}\n"
        f"💵 Выигрыш: x{JOKER_MULTIS[0]} = {format_balance(bet)}\n"
        f"💀 Избегай скелета!",
        reply_markup=get_joker_kb(session_id, finished=False)
    )


@router.callback_query(F.data.startswith("jk_"))
async def joker_callback(callback: CallbackQuery):
    parts = callback.data.split("_")

    if parts[1] == "noop":
        await callback.answer("Этаж пройден.")
        return

    if parts[1] == "cash":
        session_id = f"{parts[2]}_{parts[3]}"
        if session_id not in joker_sessions:
            await callback.answer("Игра завершена.")
            return
        sess = joker_sessions[session_id]
        if sess["user_id"] != callback.from_user.id:
            await callback.answer("Чужая игра!")
            return
        lvl = sess["level"]
        if lvl == 0:
            update_balance(sess["user_id"], sess["bet"])
            del joker_sessions[session_id]
            await callback.message.edit_text("Вы забрали ставку обратно.")
            return
        win = int(sess["bet"] * JOKER_MULTIS[lvl])
        update_balance(sess["user_id"], win)
        with get_db() as conn:
            with conn.cursor() as cursor:
                cursor.execute("UPDATE users SET games_played = games_played + 1, games_won = games_won + 1 WHERE user_id = %s", (sess["user_id"],))
                conn.commit()
        del joker_sessions[session_id]
        mention = get_mention(sess["user_id"], sess["user_name"])
        await callback.message.edit_text(f"💰 {mention}, вы забрали выигрыш <b>{format_balance(win)}</b>!")
        return

    session_id = f"{parts[1]}_{parts[2]}"
    choice = int(parts[3])
    if session_id not in joker_sessions:
        await callback.answer("Игра завершена.")
        return
    sess = joker_sessions[session_id]
    if sess["user_id"] != callback.from_user.id:
        await callback.answer("Чужая игра!")
        return

    skull_pos = sess["skull_pos"]
    if is_cursed(callback.from_user.id) and random.random() < 0.25:
        choice = skull_pos
    mention = get_mention(sess["user_id"], sess["user_name"])

    if choice == skull_pos:
        row = []
        for i in range(3):
            row.append(InlineKeyboardButton(text="💀" if i == skull_pos else "🃏", callback_data="jk_noop"))
        sess["history"].append(row)
        bet = sess["bet"]
        del joker_sessions[session_id]
        with get_db() as conn:
            with conn.cursor() as cursor:
                cursor.execute("UPDATE users SET games_played = games_played + 1 WHERE user_id = %s", (sess["user_id"],))
                conn.commit()
        await callback.message.edit_text(
            f"💀 {mention}, вы проиграли! Проиграно {format_balance(bet)}.",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=sess["history"])
        )
        return

    row = []
    for i in range(3):
        row.append(InlineKeyboardButton(text="🃏", callback_data="jk_noop"))
    sess["history"].append(row)
    sess["level"] += 1
    lvl = sess["level"]
    sess["skull_pos"] = random.randint(0, 2)

    if lvl >= len(JOKER_MULTIS) - 1:
        win = int(sess["bet"] * JOKER_MULTIS[-1])
        update_balance(sess["user_id"], win)
        with get_db() as conn:
            with conn.cursor() as cursor:
                cursor.execute("UPDATE users SET games_played = games_played + 1, games_won = games_won + 1 WHERE user_id = %s", (sess["user_id"],))
                conn.commit()
        del joker_sessions[session_id]
        await callback.message.edit_text(
            f"🎉 {mention}, максимальный множитель! Выигрыш <b>{format_balance(win)}</b>",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=sess["history"])
        )
    else:
        cur_win = int(sess["bet"] * JOKER_MULTIS[lvl])
        await callback.message.edit_text(
            f"🃏 {mention}, продолжаем!\n"
            f"💰 Ставка: {format_balance(sess['bet'])}\n"
            f"💵 Выигрыш: x{JOKER_MULTIS[lvl]} = {format_balance(cur_win)}",
            reply_markup=get_joker_kb(session_id, finished=False)
        )


# ============================================================
# МИНЫ
# ============================================================
mines_sessions = {}
MINES_MULTIS = [1.0, 1.2, 1.5, 1.9, 2.4, 3.0, 3.8, 4.8, 6.0, 7.6, 9.5,
                12.0, 15.0, 19.0, 24.0, 30.0, 38.0, 48.0, 60.0]
MINES_BOMBS = 7


def get_mines_kb(session_id, revealed, exploded=False):
    sess = mines_sessions.get(session_id)
    bombs = sess["bomb_positions"] if sess else set()
    kb = []
    for r in range(5):
        row = []
        for c in range(5):
            idx = r * 5 + c
            if exploded:
                if idx in bombs:
                    row.append(InlineKeyboardButton(text="💣", callback_data="mn_noop"))
                elif idx in revealed:
                    row.append(InlineKeyboardButton(text="💎", callback_data="mn_noop"))
                else:
                    row.append(InlineKeyboardButton(text="⬛", callback_data="mn_noop"))
            elif idx in revealed:
                row.append(InlineKeyboardButton(text="💎", callback_data="mn_noop"))
            else:
                row.append(InlineKeyboardButton(text="⬛", callback_data=f"mn_{session_id}_{idx}"))
        kb.append(row)
    if sess and not exploded and len(revealed) > 0:
        win_amount = int(sess["bet"] * MINES_MULTIS[len(revealed)])
        kb.append([InlineKeyboardButton(
            text=f"💰 Забрать {format_balance(win_amount)}",
            callback_data=f"mn_cash_{session_id}"
        )])
    return InlineKeyboardMarkup(inline_keyboard=kb)


@router.message(F.text.lower().startswith("мины"))
async def game_mines(message: Message):
    if not check_group_only(message, "мины"):
        return
    args = message.text.split()
    if len(args) < 2 or not args[1].isdigit():
        await message.answer("❌ Использование: <code>мины [ставка]</code>")
        return
    bet = int(args[1])
    if bet < MIN_BET:
        await message.answer(f"Минимальная ставка {MIN_BET} {CURRENCY}")
        return
    user = get_user(message.from_user.id, message.from_user.first_name or "",
                    message.from_user.last_name or "", message.from_user.username or "")
    if user["balance"] < bet:
        await message.answer("❌ Недостаточно средств")
        return
    update_balance(user["user_id"], -bet)
    session_id = f"{message.from_user.id}_{message.message_id}"
    bombs = set(random.sample(range(25), MINES_BOMBS))
    mines_sessions[session_id] = {
        "user_id": user["user_id"], "user_name": user["first_name"],
        "bet": bet, "bomb_positions": bombs, "revealed": set()
    }
    mention = get_mention(user["user_id"], user["first_name"])
    await message.answer(
        f"💣 {mention}, начнём Мины!\n"
        f"💰 Ставка: {format_balance(bet)}\n"
        f"💣 Бомб: {MINES_BOMBS} из 25\n"
        f"💵 Текущий выигрыш: {format_balance(bet)}",
        reply_markup=get_mines_kb(session_id, set())
    )


@router.callback_query(F.data.startswith("mn_"))
async def mines_callback(callback: CallbackQuery):
    parts = callback.data.split("_")

    if parts[1] == "noop":
        await callback.answer()
        return

    if parts[1] == "cash":
        session_id = f"{parts[2]}_{parts[3]}"
        if session_id not in mines_sessions:
            await callback.answer("Игра завершена.")
            return
        sess = mines_sessions[session_id]
        if sess["user_id"] != callback.from_user.id:
            await callback.answer("Чужая игра!")
            return
        lvl = len(sess["revealed"])
        if lvl == 0:
            update_balance(sess["user_id"], sess["bet"])
            del mines_sessions[session_id]
            await callback.message.edit_text("Вы забрали ставку обратно.")
            return
        win = int(sess["bet"] * MINES_MULTIS[lvl])
        update_balance(sess["user_id"], win)
        with get_db() as conn:
            with conn.cursor() as cursor:
                cursor.execute("UPDATE users SET games_played = games_played + 1, games_won = games_won + 1 WHERE user_id = %s", (sess["user_id"],))
                conn.commit()
        del mines_sessions[session_id]
        mention = get_mention(sess["user_id"], sess["user_name"])
        await callback.message.edit_text(f"💰 {mention} забрал <b>{format_balance(win)}</b>!")
        return

    session_id = f"{parts[1]}_{parts[2]}"
    idx = int(parts[3])
    if session_id not in mines_sessions:
        await callback.answer("Игра завершена.")
        return
    sess = mines_sessions[session_id]
    if sess["user_id"] != callback.from_user.id:
        await callback.answer("Чужая игра!")
        return
    if idx in sess["revealed"]:
        await callback.answer("Клетка уже открыта!")
        return
    mention = get_mention(sess["user_id"], sess["user_name"])

    if is_cursed(callback.from_user.id) and random.random() < 0.25:
        if idx not in sess["bomb_positions"]:
            idx = random.choice(list(sess["bomb_positions"]))

    if idx in sess["bomb_positions"]:
        sess["revealed"].add(idx)
        kb = get_mines_kb(session_id, sess["revealed"], exploded=True)
        bet = sess["bet"]
        del mines_sessions[session_id]
        with get_db() as conn:
            with conn.cursor() as cursor:
                cursor.execute("UPDATE users SET games_played = games_played + 1 WHERE user_id = %s", (sess["user_id"],))
                conn.commit()
        await callback.message.edit_text(
            f"💥 {mention}, вы подорвались!\nПроиграно: <b>{format_balance(bet)}</b>",
            reply_markup=kb
        )
        return

    sess["revealed"].add(idx)
    lvl = len(sess["revealed"])
    if lvl >= 18:
        win = int(sess["bet"] * MINES_MULTIS[18])
        update_balance(sess["user_id"], win)
        kb = get_mines_kb(session_id, sess["revealed"], exploded=True)
        del mines_sessions[session_id]
        with get_db() as conn:
            with conn.cursor() as cursor:
                cursor.execute("UPDATE users SET games_played = games_played + 1, games_won = games_won + 1 WHERE user_id = %s", (sess["user_id"],))
                conn.commit()
        await callback.message.edit_text(
            f"🎉 {mention}, вы открыли всё!\nВыигрыш: <b>{format_balance(win)}</b>",
            reply_markup=kb
        )
        return

    cur_win = int(sess["bet"] * MINES_MULTIS[lvl])
    await callback.message.edit_text(
        f"💣 {mention}, продолжаем!\n"
        f"💰 Ставка: {format_balance(sess['bet'])}\n"
        f"🔍 Открыто: {lvl}/18\n"
        f"💵 Текущий выигрыш: {format_balance(cur_win)}",
        reply_markup=get_mines_kb(session_id, sess["revealed"])
    )


# ============================================================
# РУЛЕТКА
# ============================================================
REDS = [1, 3, 5, 7, 9, 12, 14, 16, 18, 19, 21, 23, 25, 27, 30, 32, 34, 36]


def parse_roulette_target(target_str: str):
    t = target_str.lower()
    if t in ["к", "red", "красное"]:
        return "к", "RED"
    elif t in ["ч", "black", "черное"]:
        return "ч", "BLACK"
    elif t in ["even", "чет"]:
        return "even", "EVEN"
    elif t in ["odd", "нечет"]:
        return "odd", "ODD"
    elif t in ["1-12", "13-24", "25-36"]:
        return t, t
    elif "-" in t:
        try:
            s, e = map(int, t.split("-"))
            if 0 <= s <= 36 and 0 <= e <= 36 and s <= e and (e - s + 1) <= 6:
                return t, f"{s}-{e}"
        except:
            pass
    elif t.isdigit():
        val = int(t)
        if 0 <= val <= 36:
            return str(val), str(val)
    return None, None


def choice_emoji(choice):
    return {"RED": "🔴", "BLACK": "⚫", "EVEN": "🔵", "ODD": "🟠", "0": "🟢"}.get(choice, "")


@router.message(F.text.lower() == "ставки")
async def roulette_bets_list(message: Message):
    if not check_group_only(message, "рулетка"):
        return
    bets = chat_roulette_bets.get(message.chat.id, [])
    if not bets:
        await message.answer("📋 Нет активных ставок.")
        return
    text = "📋 <b>Активные ставки</b>\n\n"
    for b in bets:
        emoji = choice_emoji(b["choice_display"])
        text += f"• {get_mention(b['user_id'], b['user_name'])} — {b['bet']} {CURRENCY} на {emoji} {b['choice_display']}\n"
    await message.answer(text)


@router.message(F.text.lower() == "лог")
async def cmd_roulette_log(message: Message):
    if not check_group_only(message, "рулетка"):
        return
    history = get_roulette_history()
    if not history:
        await message.answer("📜 История пуста.")
        return
    lines = [f"{item['roll']}{item['color']}" for item in history]
    await message.answer("\n".join(lines))


@router.message(F.text.lower().in_(["го", "старт"]))
async def roulette_go(message: Message):
    if not check_group_only(message, "рулетка"):
        return
    chat_id = message.chat.id
    user_id = message.from_user.id
    if chat_id not in chat_roulette_bets or not any(b["user_id"] == user_id for b in chat_roulette_bets[chat_id]):
        return
    last_time = chat_last_bet_time.get((chat_id, user_id))
    if last_time and (datetime.now() - last_time).total_seconds() < 3:
        await message.answer("⏳ Подождите 3 секунды.")
        return

    bets_to_play = chat_roulette_bets[chat_id]
    chat_roulette_bets[chat_id] = []
    if not bets_to_play:
        return

    try:
        anim = get_cached_animation()
        if anim:
            sent_msg = await message.answer_animation(animation=anim)
        else:
            sent_msg = await message.answer("🎰 Крутим рулетку...")
        await asyncio.sleep(5)
        try:
            await bot.delete_message(chat_id, sent_msg.message_id)
        except:
            pass
    except:
        await asyncio.sleep(5)

    roll = random.randint(0, 36)
    color = "🔴" if roll in REDS else ("🟢" if roll == 0 else "⚫")
    add_roulette_log(roll, color)

    res_lines = [f"🎰 <b>Рулетка: {roll}{color}</b>\n"]
    unique_ids = set(b["user_id"] for b in bets_to_play)
    cursed_users = {uid: is_cursed(uid) for uid in unique_ids}

    wins, losses = [], []
    for b in bets_to_play:
        choice = b["choice"]
        bet = b["bet"]
        is_win, multi = False, 0
        if choice in ["к", "red"]:
            is_win, multi = roll in REDS, 2
        elif choice in ["ч", "black"]:
            is_win, multi = (roll not in REDS and roll != 0), 2
        elif choice in ["even", "чет"]:
            is_win, multi = (roll != 0 and roll % 2 == 0), 2
        elif choice in ["odd", "нечет"]:
            is_win, multi = (roll != 0 and roll % 2 != 0), 2
        elif choice in ["1-12", "13-24", "25-36"]:
            s, e = map(int, choice.split("-"))
            is_win, multi = (s <= roll <= e), 3
        elif "-" in choice:
            s, e = map(int, choice.split("-"))
            is_win = (s <= roll <= e)
            n = e - s + 1
            multi = max(2, int(36 / n))
        else:
            is_win = (roll == int(choice))
            multi = 36

        if is_win and cursed_users.get(b["user_id"], False) and random.random() < 0.3:
            is_win = False

        if is_win:
            wins.append((b["user_id"], bet * multi, bet, b["choice_display"]))
        else:
            losses.append(b["user_id"])

    with get_db() as conn:
        with conn.cursor() as cursor:
            for uid, wa, _, _ in wins:
                cursor.execute("UPDATE users SET balance = balance + %s, games_played = games_played + 1, games_won = games_won + 1 WHERE user_id = %s", (wa, uid))
            for uid in losses:
                cursor.execute("UPDATE users SET games_played = games_played + 1 WHERE user_id = %s", (uid,))
            conn.commit()

    for uid, wa, bet, display in wins:
        user = get_user(uid)
        emoji = choice_emoji(display)
        res_lines.append(f"✅ {get_mention(uid, user['first_name'])} — {bet} на {emoji} {display} → <b>+{format_balance(wa)}</b>")

    if losses and not wins:
        res_lines.append("❌ Никто не выиграл.")

    kb = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="🔁 Повторить", callback_data=f"rl_rep_{user_id}"),
        InlineKeyboardButton(text="✖️ Удвоить", callback_data=f"rl_dbl_{user_id}")
    ]])
    await message.answer("\n".join(res_lines), reply_markup=kb)


@router.callback_query(F.data.startswith("rl_rep_") | F.data.startswith("rl_dbl_"))
async def roulette_action_callback(callback: CallbackQuery):
    parts = callback.data.split("_")
    action = parts[1]
    target_uid = int(parts[2])
    if callback.from_user.id != target_uid:
        await callback.answer("❌ Не ваша ставка!", show_alert=True)
        return
    user = get_user(target_uid, callback.from_user.first_name or "",
                    callback.from_user.last_name or "", callback.from_user.username or "")
    last_bets = get_last_bets(target_uid)
    if not last_bets:
        await callback.answer("❌ Нет сохранённой ставки!", show_alert=True)
        return
    mult = 2 if action == "dbl" else 1
    total_cost = sum(b["bet"] * mult for b in last_bets)
    if user["balance"] < total_cost:
        await callback.answer(f"❌ Нужно {format_balance(total_cost)}", show_alert=True)
        return
    update_balance(target_uid, -total_cost)
    chat_id = callback.message.chat.id
    if chat_id not in chat_roulette_bets:
        chat_roulette_bets[chat_id] = []
    updated, displays = [], []
    for b in last_bets:
        new_amt = b["bet"] * mult
        chat_roulette_bets[chat_id].append({
            "user_id": target_uid, "user_name": user["first_name"],
            "bet": new_amt, "choice": b["choice"], "choice_display": b["choice_display"]
        })
        updated.append({"bet": new_amt, "choice": b["choice"], "choice_display": b["choice_display"]})
        displays.append(f"{new_amt} {CURRENCY} на {b['choice_display']}")
    chat_last_bet_time[(chat_id, target_uid)] = datetime.now()
    save_last_bets(target_uid, updated)
    await callback.answer("✅ Ставка сделана!")
    await callback.message.answer(
        f"🎰 Ставка принята: {get_mention(target_uid, user['first_name'])} — {format_balance(total_cost)}\n\n" + "\n".join(displays)
    )


@router.message(F.text.lower() == "отмена")
async def roulette_cancel(message: Message):
    if not check_group_only(message, "рулетка"):
        return
    chat_id = message.chat.id
    user_id = message.from_user.id
    bets = chat_roulette_bets.get(chat_id, [])
    user_bets = [b for b in bets if b["user_id"] != user_id]
    removed = len(bets) - len(user_bets)
    if removed == 0:
        await message.answer("У вас нет активных ставок.")
        return
    for b in bets:
        if b["user_id"] == user_id:
            update_balance(user_id, b["bet"])
    if user_bets:
        chat_roulette_bets[chat_id] = user_bets
    else:
        chat_roulette_bets.pop(chat_id, None)
    chat_last_bet_time.pop((chat_id, user_id), None)
    await message.answer(f"✅ Отменено ставок: {removed}. Средства возвращены.")


@router.message(F.text.regexp(r"^\d+\s+\S"), lambda m: m.from_user.id not in admin_states)
async def roulette_generic(message: Message):
    text = message.text.strip()
    parts = text.split()
    if not parts or not parts[0].isdigit():
        return
    bet = int(parts[0])
    targets = parts[1:]
    if not targets:
        return

    valid = []
    for tgt in targets:
        code, display = parse_roulette_target(tgt)
        if code is not None:
            valid.append((code, display))
    if not valid:
        return
    if len(valid) > 75:
        valid = valid[:75]

    if not check_group_only(message, "рулетка"):
        return
    if bet < MIN_BET:
        await message.answer(f"Минимальная ставка на объект: {MIN_BET} {CURRENCY}")
        return

    user = get_user(message.from_user.id, message.from_user.first_name or "",
                    message.from_user.last_name or "", message.from_user.username or "")
    total = bet * len(valid)
    if user["balance"] < total:
        await message.answer(f"❌ Нужно {format_balance(total)} для {len(valid)} ставок.")
        return

    update_balance(user["user_id"], -total)
    chat_id = message.chat.id
    if chat_id not in chat_roulette_bets:
        chat_roulette_bets[chat_id] = []
    new_bets, displays = [], []
    for code, display in valid:
        chat_roulette_bets[chat_id].append({
            "user_id": user["user_id"], "user_name": user["first_name"],
            "bet": bet, "choice": code, "choice_display": display
        })
        new_bets.append({"bet": bet, "choice": code, "choice_display": display})
        displays.append(f"{bet} {CURRENCY} на {display}")
    chat_last_bet_time[(chat_id, user["user_id"])] = datetime.now()
    save_last_bets(user["user_id"], new_bets)
    mention = get_mention(user["user_id"], user["first_name"])
    await message.answer(f"🎰 Ставка принята: {mention} — {format_balance(total)}\n\n" + "\n".join(displays))


# ============================================================
# COINFLIP
# ============================================================
coinflip_sessions = {}


@router.message(F.text.lower().startswith("coinflip"))
async def game_coinflip(message: Message):
    if not check_group_only(message, "coinflip"):
        return
    args = message.text.split()
    if len(args) < 2 or not args[1].isdigit():
        await message.answer("❌ Использование: <code>coinflip [ставка]</code>")
        return
    bet = int(args[1])
    if bet < MIN_BET:
        await message.answer(f"Минимальная ставка {MIN_BET} {CURRENCY}")
        return
    user = get_user(message.from_user.id, message.from_user.first_name or "",
                    message.from_user.last_name or "", message.from_user.username or "")
    if user["balance"] < bet:
        await message.answer("❌ Недостаточно средств")
        return
    update_balance(user["user_id"], -bet)
    session_id = f"{message.from_user.id}_{message.message_id}"
    coinflip_sessions[session_id] = {"user_id": user["user_id"], "bet": bet}
    kb = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="🦅 Орёл", callback_data=f"cf_{session_id}_heads"),
        InlineKeyboardButton(text="🪙 Решка", callback_data=f"cf_{session_id}_tails")
    ]])
    mention = get_mention(user["user_id"], user["first_name"])
    await message.answer(
        f"🪙 {mention} подбрасывает монетку!\n"
        f"💰 Ставка: <b>{format_balance(bet)}</b>\n"
        f"Выберите сторону:",
        reply_markup=kb
    )


@router.callback_query(F.data.startswith("cf_"))
async def coinflip_callback(callback: CallbackQuery):
    parts = callback.data.split("_")
    session_id = f"{parts[1]}_{parts[2]}"
    choice = parts[3]
    if session_id not in coinflip_sessions:
        await callback.answer("Игра завершена.")
        return
    sess = coinflip_sessions[session_id]
    if callback.from_user.id != sess["user_id"]:
        await callback.answer("Чужая игра!")
        return
    del coinflip_sessions[session_id]
    result = random.choice(["heads", "tails"])
    emoji = "🦅" if result == "heads" else "🪙"
    if choice == result:
        win = sess["bet"] * 2
        update_balance(sess["user_id"], win)
        with get_db() as conn:
            with conn.cursor() as cursor:
                cursor.execute("UPDATE users SET games_played = games_played + 1, games_won = games_won + 1 WHERE user_id = %s", (sess["user_id"],))
                conn.commit()
        await callback.message.edit_text(f"🪙 Выпало: {emoji}\n🎉 Вы выиграли <b>{format_balance(win)}</b>!")
    else:
        with get_db() as conn:
            with conn.cursor() as cursor:
                cursor.execute("UPDATE users SET games_played = games_played + 1 WHERE user_id = %s", (sess["user_id"],))
                conn.commit()
        await callback.message.edit_text(f"🪙 Выпало: {emoji}\n❌ Вы проиграли <b>{format_balance(sess['bet'])}</b>.")
    await callback.answer()


# ============================================================
# ДУЭЛИ (КНБ)
# ============================================================
duels = {}


@router.message(F.text.lower().startswith("дуэль"))
async def game_duel(message: Message):
    if not check_group_only(message, "дуэль"):
        return
    if not message.reply_to_message:
        await message.answer("❌ Ответьте на сообщение соперника.")
        return
    target = message.reply_to_message.from_user
    if target.id == message.from_user.id or target.is_bot:
        return
    args = message.text.split()
    if len(args) < 2 or not args[1].isdigit():
        await message.answer("❌ Использование: <code>дуэль [ставка]</code> в ответ.")
        return
    bet = int(args[1])
    if bet < MIN_BET:
        await message.answer(f"Минимальная ставка {MIN_BET} {CURRENCY}")
        return
    p1 = get_user(message.from_user.id, message.from_user.first_name or "",
                  message.from_user.last_name or "", message.from_user.username or "")
    p2 = get_user(target.id, target.first_name or "", target.last_name or "", target.username or "")
    if p1["balance"] < bet or p2["balance"] < bet:
        await message.answer("❌ У одного из участников недостаточно средств.")
        return
    duel_id = f"{message.chat.id}_{message.message_id}"
    duels[duel_id] = {
        "p1_id": p1["user_id"], "p1_name": p1["first_name"], "p1_choice": None,
        "p2_id": p2["user_id"], "p2_name": p2["first_name"], "p2_choice": None,
        "bet": bet, "chat_id": message.chat.id
    }
    kb = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="⚔️ Сражаться", callback_data=f"duel_acc_{duel_id}"),
        InlineKeyboardButton(text="❌ Отказаться", callback_data=f"duel_den_{duel_id}")
    ]])
    msg = await message.answer(
        f"⚔️ {get_mention(p1['user_id'], p1['first_name'])} вызывает {get_mention(p2['user_id'], p2['first_name'])} на дуэль!\n"
        f"💰 Ставка: {format_balance(bet)}",
        reply_markup=kb
    )
    asyncio.create_task(duel_accept_timeout(duel_id, msg))


async def duel_accept_timeout(duel_id, msg: Message):
    await asyncio.sleep(60)
    if duel_id in duels and not duels[duel_id].get("accepted"):
        del duels[duel_id]
        try:
            await msg.edit_text("⏱ Время вышло! Дуэль отменена.")
        except:
            pass


@router.callback_query(F.data.startswith("duel_"))
async def duel_init_callback(callback: CallbackQuery):
    parts = callback.data.split("_")
    action = parts[1]
    duel_id = f"{parts[2]}_{parts[3]}"
    if duel_id not in duels:
        await callback.answer("Дуэль устарела.")
        return
    duel = duels[duel_id]
    uid = callback.from_user.id

    if action == "den":
        if uid not in (duel["p1_id"], duel["p2_id"]):
            await callback.answer("Вы не участник!")
            return
        del duels[duel_id]
        await callback.message.edit_text("❌ Дуэль отклонена.")
        return

    if action == "acc":
        if uid != duel["p2_id"]:
            await callback.answer("Сражаться может только вызываемый!")
            return
        u1 = get_user(duel["p1_id"])
        u2 = get_user(duel["p2_id"])
        if u1["balance"] < duel["bet"] or u2["balance"] < duel["bet"]:
            await callback.message.edit_text("❌ Недостаточно средств.")
            del duels[duel_id]
            return
        duel["accepted"] = True
        update_balance(duel["p1_id"], -duel["bet"])
        update_balance(duel["p2_id"], -duel["bet"])
        kb = InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="🪨", callback_data=f"rps_{duel_id}_rock"),
            InlineKeyboardButton(text="📄", callback_data=f"rps_{duel_id}_paper"),
            InlineKeyboardButton(text="✂️", callback_data=f"rps_{duel_id}_scissors")
        ]])
        try:
            await bot.send_message(duel["p1_id"],
                f"⚔️ Дуэль против {get_mention(duel['p2_id'], duel['p2_name'])}! Ставка: {format_balance(duel['bet'])}.",
                reply_markup=kb)
            await bot.send_message(duel["p2_id"],
                f"⚔️ Дуэль против {get_mention(duel['p1_id'], duel['p1_name'])}! Ставка: {format_balance(duel['bet'])}.",
                reply_markup=kb)
            await callback.message.edit_text("⏳ Игроки делают выбор...")
            asyncio.create_task(duel_choice_timeout(duel_id, callback.message))
        except:
            update_balance(duel["p1_id"], duel["bet"])
            update_balance(duel["p2_id"], duel["bet"])
            del duels[duel_id]
            await callback.message.edit_text("❌ Ошибка дуэли.")


async def duel_choice_timeout(duel_id, msg: Message):
    await asyncio.sleep(60)
    if duel_id in duels:
        d = duels[duel_id]
        update_balance(d["p1_id"], d["bet"])
        update_balance(d["p2_id"], d["bet"])
        del duels[duel_id]
        try:
            await bot.edit_message_text("⏱ Время вышло! Ставки возвращены.", chat_id=msg.chat.id, message_id=msg.message_id)
        except:
            pass


@router.callback_query(F.data.startswith("rps_"))
async def rps_callback(callback: CallbackQuery):
    parts = callback.data.split("_")
    duel_id = f"{parts[1]}_{parts[2]}"
    choice = parts[3]
    if duel_id not in duels:
        await callback.answer("Дуэль устарела.")
        return
    duel = duels[duel_id]
    uid = callback.from_user.id
    if uid == duel["p1_id"]:
        duel["p1_choice"] = choice
    elif uid == duel["p2_id"]:
        duel["p2_choice"] = choice
    else:
        return
    await callback.message.edit_text("✅ Выбор сделан. Ожидаем соперника...")
    if duel["p1_choice"] and duel["p2_choice"]:
        c1, c2 = duel["p1_choice"], duel["p2_choice"]
        bank = duel["bet"] * 2
        if c1 == c2:
            update_balance(duel["p1_id"], duel["bet"])
            update_balance(duel["p2_id"], duel["bet"])
            res = "🤝 Ничья! Ставки возвращены."
        else:
            rules = {"rock": "scissors", "scissors": "paper", "paper": "rock"}
            if rules[c1] == c2:
                win_id, win_name = duel["p1_id"], duel["p1_name"]
            else:
                win_id, win_name = duel["p2_id"], duel["p2_name"]
            update_balance(win_id, bank)
            with get_db() as conn:
                with conn.cursor() as cursor:
                    cursor.execute("UPDATE users SET games_played = games_played + 1, games_won = games_won + 1 WHERE user_id = %s", (win_id,))
                    loser = duel["p2_id"] if win_id == duel["p1_id"] else duel["p1_id"]
                    cursor.execute("UPDATE users SET games_played = games_played + 1 WHERE user_id = %s", (loser,))
                    conn.commit()
            res = f"🏆 Победитель: {get_mention(win_id, win_name)} забирает {format_balance(bank)}!"
        try:
            await bot.send_message(duel["chat_id"], f"⚔️ <b>Результат дуэли</b>\n{res}")
        except:
            pass
        del duels[duel_id]

# =============================================================
# MINT Bot — Часть 4/4: Админ-панель + Секретки + Запуск
# =============================================================

# ============================================================
# /admh — СПРАВКА ПО ВСЕМ АДМИН-КОМАНДАМ
# ============================================================
ADMIN_HELP_TEXT = (
    "🛡 <b>АДМИН-КОМАНДЫ MINT</b>\n"
    "━━━━━━━━━━━━━━━━━━━━━━━━\n\n"

    "⭐ <b>MODER+ (модератор и выше):</b>\n"
    "• <code>/admin</code> — инлайн-панель\n"
    "• <code>/info @user</code> — инфо о юзере\n"
    "• <code>/stats</code> — статистика бота\n"
    "• <code>/setbal @user сумма</code> — установить баланс\n"
    "• <code>/take @user сумма</code> — списать\n"
    "• <code>/resetbal @user</code> — сброс до 4000\n"
    "• <code>/history @user</code> — история игр\n"
    "• <code>/admh</code> или <code>.admh</code> — эта справка\n\n"

    "🔰 <b>ADMIN+ (администратор и выше):</b>\n"
    "• <code>/addpromo код сумма uses</code> — создать промо\n"
    "• <code>/delpromo код</code> — удалить промо\n"
    "• <code>/makeadmin @user</code> — назначить модером\n"
    "• <code>/unadmin @user</code> — снять ранг\n\n"

    "👑 <b>HEAD+ (гл. админ и выше):</b>\n"
    "• <code>/add_admin @user ранг</code> — назначить (moder/admin/head)\n"
    "• <code>/remove_admin @user</code> — снять\n"
    "• <code>/broadcast текст</code> — рассылка\n"
    "• <code>/setbonus сумма</code> — размер бонуса\n"
    "• <code>/setcooldown часы</code> — кулдаун бонуса\n"
    "• <code>/freeze @user</code> — заморозить на 24ч\n"
    "• <code>/unfreeze @user</code> — разморозить\n"
    "• <code>/snos @user</code> — снести с должности\n"
    "• <code>/unsnos @user</code> — восстановить\n"
    "• <code>/globalbonus сумма</code> — всем +сумма\n"
    "• <code>/adminlog [N]</code> — лог действий\n"
    "• <code>/list_admins</code> — список админов\n"
    "• <code>/wipe</code> — очистить ставки рулетки\n"
    "• <code>/checkpoint @user</code> — сохранить баланс\n"
    "• <code>/restore_checkpoint @user</code> — восстановить\n\n"

    "💎 <b>OWNER (только владелец):</b>\n"
    "• <code>/zero @user</code> — обнулить баланс\n"
    "• <code>/double @user</code> — удвоить баланс\n"
    "• <code>/randomize @user мин макс</code> — случайный\n"
    "• <code>/setallbal сумма</code> — всем установить\n"
    "• <code>/resetallbal</code> — всем сброс до 4000\n"
    "• <code>/curse @user</code> — проклясть (25% луз)\n"
    "• <code>/bless @user</code> — снять проклятие\n"
    "• <code>/lottery сумма</code> — случайному юзеру\n"
    "• <code>/setname @user имя</code> — сменить имя\n"
    "• <code>/nick @user ник</code> — сменить ник\n"
    "• <code>/deleteuser @user</code> — ПОЛНОЕ удаление\n"
    "• <code>/givepower @user cmd</code> — выдать секретку\n"
    "• <code>/takepower @user cmd</code> — забрать\n"
    "• <code>/listpowers [@user]</code> — список сил\n"
    "• <code>/mypowers</code> — мои секретки\n"
    "• <code>/clearlog</code> — очистить admin_log\n"
    "• <code>/backup</code> — дамп БД в JSON\n"
    "• <code>/sql_execute SQL</code> — прямой SQL ⚠️\n"
    "• <code>/emergency_stop</code> — остановить бота 🚨\n\n"

    "📌 <b>Ранги:</b>\n"
    "💎 owner → 👑 head → 🔰 admin → ⭐ moder"
)


@router.message(Command("admh"))
async def cmd_admh(message: Message):
    if not is_admin(message.from_user.id):
        return
    await message.answer(ADMIN_HELP_TEXT)


@router.message(F.text == ".admh")
async def cmd_admh_dot(message: Message):
    if not is_admin(message.from_user.id):
        return
    await message.answer(ADMIN_HELP_TEXT)


# ============================================================
# 🛡 АДМИН-ПАНЕЛЬ
# ============================================================
def _admin_main_text(uid):
    with get_db() as conn:
        with conn.cursor() as cursor:
            cursor.execute("SELECT COUNT(*), COALESCE(SUM(balance),0), COALESCE(AVG(balance),0) FROM users")
            users_cnt, total_bal, avg_bal = cursor.fetchone()
            cursor.execute("SELECT COUNT(*) FROM users WHERE is_banned = TRUE")
            banned = cursor.fetchone()[0]
            cursor.execute("SELECT COUNT(*) FROM users WHERE vip_until > NOW()")
            vips = cursor.fetchone()[0]
            cursor.execute("SELECT COUNT(*) FROM clans")
            clans_cnt = cursor.fetchone()[0]
            cursor.execute("SELECT COUNT(*) FROM promocodes WHERE uses > 0")
            promos_cnt = cursor.fetchone()[0]
            cursor.execute("SELECT COUNT(*) FROM disabled_games")
            disabled_cnt = cursor.fetchone()[0]
            cursor.execute("SELECT COUNT(*) FROM deposits")
            deps_cnt = cursor.fetchone()[0]
            cursor.execute("SELECT COALESCE(SUM(games_played),0) FROM users")
            total_games = cursor.fetchone()[0]

    status = "🔴 ТЕХ.РАБОТЫ" if disabled_cnt > 0 else "🟢 РАБОТАЕТ"

    return (
        f"🛡 <b>АДМИН-ПАНЕЛЬ {BOT_NAME}</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"👤 <b>{get_rank_emoji(get_rank(uid))}</b>\n"
        f"📡 Статус: <b>{status}</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"👥 Юзеров: <b>{users_cnt}</b>\n"
        f"💰 Баланс: <b>{format_balance(total_bal)}</b>\n"
        f"📊 Средний: <b>{format_balance(int(avg_bal))}</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"⚜️ VIP: <b>{vips}</b> | 🚫 Бан: <b>{banned}</b>\n"
        f"🏰 Кланов: <b>{clans_cnt}</b> | 🎁 Промо: <b>{promos_cnt}</b>\n"
        f"🏦 Депозитов: <b>{deps_cnt}</b> | 🎮 Игр: <b>{total_games}</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"<i>Выбери раздел ↓</i>"
    )


def _admin_main_kb(uid):
    rows = [
        [InlineKeyboardButton(text="👥 Пользователи", callback_data="adm:users"),
         InlineKeyboardButton(text="🔍 Поиск по нику", callback_data="adm:search")],
        [InlineKeyboardButton(text="💰 Экономика", callback_data="adm:econ"),
         InlineKeyboardButton(text="📊 Статистика", callback_data="adm:stats")],
        [InlineKeyboardButton(text="📈 Аналитика", callback_data="adm:analytics"),
         InlineKeyboardButton(text="🏆 Лидерборд", callback_data="adm:leaderboard")],
        [InlineKeyboardButton(text="🎁 Промокоды", callback_data="adm:promos"),
         InlineKeyboardButton(text="📦 Выдать предмет", callback_data="adm:giveitem")],
        [InlineKeyboardButton(text="🎁 Массовая раздача", callback_data="adm:massgift"),
         InlineKeyboardButton(text="💎 VIP-управление", callback_data="adm:vipmgr")],
        [InlineKeyboardButton(text="🎮 Игры", callback_data="adm:games"),
         InlineKeyboardButton(text="⚙️ Настройки", callback_data="adm:settings")],
        [InlineKeyboardButton(text="🏰 Кланы", callback_data="adm:clans"),
         InlineKeyboardButton(text="⛏️ Майнинг", callback_data="adm:mining")],
        [InlineKeyboardButton(text="🏦 Депозиты", callback_data="adm:deposits"),
         InlineKeyboardButton(text="📢 Рассылка", callback_data="adm:broadcast")],
        [InlineKeyboardButton(text="📤 В канал", callback_data="adm:tochannel"),
         InlineKeyboardButton(text="⏸ Тех.работы", callback_data="adm:maintenance")],
        [InlineKeyboardButton(text="🚫 Баны", callback_data="adm:bans"),
         InlineKeyboardButton(text="🔄 Сбросы", callback_data="adm:resets")],
        [InlineKeyboardButton(text="📜 Логи", callback_data="adm:logs"),
         InlineKeyboardButton(text="💾 Бэкап", callback_data="adm:backup")],
        [InlineKeyboardButton(text="🛡 Админы", callback_data="adm:admins"),
         InlineKeyboardButton(text="📋 Справка", callback_data="adm:help")],
    ]
    return InlineKeyboardMarkup(inline_keyboard=rows)


@router.message(Command("admin"))
async def cmd_admin_panel(message: Message):
    if not is_admin(message.from_user.id):
        return
    admin_states.pop(message.from_user.id, None)
    await message.answer(_admin_main_text(message.from_user.id),
                         reply_markup=_admin_main_kb(message.from_user.id))


@router.message(F.text == "🛡 Админ-панель")
async def cmd_admin_panel_btn(message: Message):
    if not is_admin(message.from_user.id):
        return
    admin_states.pop(message.from_user.id, None)
    await message.answer(_admin_main_text(message.from_user.id),
                         reply_markup=_admin_main_kb(message.from_user.id))


@router.callback_query(F.data == "adm:main")
async def adm_main(callback: CallbackQuery):
    if not _check_admin(callback):
        await callback.answer("Нет доступа", show_alert=True)
        return
    admin_states.pop(callback.from_user.id, None)
    try:
        await callback.message.edit_text(_admin_main_text(callback.from_user.id),
                                          reply_markup=_admin_main_kb(callback.from_user.id))
    except:
        await callback.message.answer(_admin_main_text(callback.from_user.id),
                                       reply_markup=_admin_main_kb(callback.from_user.id))
    await callback.answer()


@router.callback_query(F.data == "adm:help")
async def adm_help(callback: CallbackQuery):
    if not _check_admin(callback):
        return
    await callback.message.edit_text(ADMIN_HELP_TEXT, reply_markup=_back_kb())
    await callback.answer()


# ---------- 👥 ПОЛЬЗОВАТЕЛИ ----------
@router.callback_query(F.data == "adm:users")
async def adm_users(callback: CallbackQuery):
    if not _check_admin(callback):
        await callback.answer("Нет доступа", show_alert=True)
        return
    admin_states[callback.from_user.id] = {"action": "find_user"}
    await callback.message.edit_text(
        "👥 <b>Поиск пользователя</b>\n\n"
        "Отправь:\n"
        "• <b>@username</b>\n"
        "• <b>ID</b> (цифры)\n"
        "• или <b>ответь</b> (reply) на сообщение юзера\n\n"
        "Для отмены: /cancel",
        reply_markup=_back_kb()
    )
    await callback.answer()


def _user_profile_text(u):
    rank = get_rank(u['user_id'])
    banned = "🚫 ЗАБАНЕН" if u.get('is_banned') else "✅ Активен"
    vip = "⚜️ VIP" if (u.get('vip_until') and u['vip_until'] > datetime.now()) else ""
    nick = f"\n🏷️ {u['custom_nick']}" if u.get('custom_nick') else ""
    return (
        f"👤 <b>{clean_first_name(u['first_name'], u['user_id'])}</b>{nick}\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"🆔 <code>{u['user_id']}</code>\n"
        f"📛 @{u['username'] or '—'}\n"
        f"📊 {get_rank_emoji(rank) or '👤 Игрок'}\n"
        f"{vip}\n"
        f"💰 Баланс: <b>{format_balance(u['balance'])}</b>\n"
        f"💎 BTC: {u.get('btc', 0):.8f}\n"
        f"🎮 Игр: {u['games_played']} | 🏆 Побед: {u['games_won']}\n"
        f"👥 Рефералов: {u.get('referred_count', 0)}\n"
        f"🔒 {banned}"
    )


def _user_actions_kb(uid):
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="💰 Выдать", callback_data=f"adm:give:{uid}"),
         InlineKeyboardButton(text="💸 Списать", callback_data=f"adm:take:{uid}")],
        [InlineKeyboardButton(text="✏️ Установить баланс", callback_data=f"adm:setbal:{uid}")],
        [InlineKeyboardButton(text="🎁 VIP", callback_data=f"adm:givevip:{uid}"),
         InlineKeyboardButton(text="🚫 Бан/Разбан", callback_data=f"adm:ban:{uid}")],
        [InlineKeyboardButton(text="🏷️ Сменить ник", callback_data=f"adm:nick:{uid}")],
        [InlineKeyboardButton(text="⭐ Модер", callback_data=f"adm:rank:{uid}:moder"),
         InlineKeyboardButton(text="🔰 Админ", callback_data=f"adm:rank:{uid}:admin")],
        [InlineKeyboardButton(text="👑 Head", callback_data=f"adm:rank:{uid}:head")],
        [InlineKeyboardButton(text="◀️ К поиску", callback_data="adm:users")]
    ])


@router.callback_query(F.data.startswith("adm:user:"))
async def adm_user_profile(callback: CallbackQuery):
    if not _check_admin(callback):
        return
    uid = int(callback.data.split(":")[2])
    u = get_user(uid)
    admin_states.pop(callback.from_user.id, None)
    await callback.message.edit_text(_user_profile_text(u), reply_markup=_user_actions_kb(uid))
    await callback.answer()


@router.callback_query(F.data.startswith("adm:give:"))
async def adm_give_start(callback: CallbackQuery):
    if not _check_admin(callback):
        return
    uid = int(callback.data.split(":")[2])
    admin_states[callback.from_user.id] = {"action": "await_amount", "target": uid, "kind": "give"}
    await callback.message.edit_text(
        f"💰 <b>Выдача баланса</b>\n\nID: <code>{uid}</code>\n\nВведи сумму:",
        reply_markup=_back_kb(back_to=f"adm:user:{uid}")
    )
    await callback.answer()


@router.callback_query(F.data.startswith("adm:take:"))
async def adm_take_start(callback: CallbackQuery):
    if not _check_admin(callback):
        return
    uid = int(callback.data.split(":")[2])
    admin_states[callback.from_user.id] = {"action": "await_amount", "target": uid, "kind": "take"}
    await callback.message.edit_text(
        f"💸 <b>Списание баланса</b>\n\nID: <code>{uid}</code>\n\nВведи сумму:",
        reply_markup=_back_kb(back_to=f"adm:user:{uid}")
    )
    await callback.answer()


@router.callback_query(F.data.startswith("adm:setbal:"))
async def adm_setbal_start(callback: CallbackQuery):
    if not _check_admin(callback):
        return
    uid = int(callback.data.split(":")[2])
    admin_states[callback.from_user.id] = {"action": "await_amount", "target": uid, "kind": "setbal"}
    await callback.message.edit_text(
        f"✏️ <b>Установка баланса</b>\n\nID: <code>{uid}</code>\n\nВведи сумму:",
        reply_markup=_back_kb(back_to=f"adm:user:{uid}")
    )
    await callback.answer()


@router.callback_query(F.data.startswith("adm:givevip:"))
async def adm_givevip_start(callback: CallbackQuery):
    if not _check_admin(callback):
        return
    uid = int(callback.data.split(":")[2])
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="24 ч", callback_data=f"adm:do_vip:{uid}:24"),
         InlineKeyboardButton(text="72 ч", callback_data=f"adm:do_vip:{uid}:72")],
        [InlineKeyboardButton(text="7 дней", callback_data=f"adm:do_vip:{uid}:168"),
         InlineKeyboardButton(text="30 дней", callback_data=f"adm:do_vip:{uid}:720")],
        [InlineKeyboardButton(text="◀️ Назад", callback_data=f"adm:user:{uid}")]
    ])
    await callback.message.edit_text(
        f"🎁 <b>Выдать VIP</b>\n\nID: <code>{uid}</code>\n\nСрок:",
        reply_markup=kb
    )
    await callback.answer()


@router.callback_query(F.data.startswith("adm:do_vip:"))
async def adm_do_vip(callback: CallbackQuery):
    if not _check_admin(callback):
        return
    _, _, uid, hours = callback.data.split(":")
    uid, hours = int(uid), int(hours)
    u = get_user(uid)
    cu = u.get('vip_until')
    new_until = (cu + timedelta(hours=hours)) if (cu and cu > datetime.now()) else (datetime.now() + timedelta(hours=hours))
    with get_db() as conn:
        with conn.cursor() as cursor:
            cursor.execute("UPDATE users SET vip_until = %s WHERE user_id = %s", (new_until, uid))
            conn.commit()
    log_admin_action(callback.from_user.id, f"givevip {uid} {hours}h")
    await callback.message.edit_text(
        f"✅ {get_mention(uid, u['first_name'])} — VIP на {hours} ч\n"
        f"📅 До: {new_until.strftime('%d.%m.%Y %H:%M')}",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="◀️ Профиль", callback_data=f"adm:user:{uid}")
        ]])
    )
    await callback.answer("VIP выдан")


@router.callback_query(F.data.startswith("adm:ban:"))
async def adm_ban_toggle(callback: CallbackQuery):
    if not _check_admin(callback):
        return
    uid = int(callback.data.split(":")[2])
    u = get_user(uid)
    if uid == ADMIN_ID:
        await callback.answer("Нельзя банить владельца!", show_alert=True)
        return
    if u.get('is_banned'):
        with get_db() as conn:
            with conn.cursor() as cursor:
                cursor.execute("UPDATE users SET is_banned = FALSE, ban_until = NULL WHERE user_id = %s", (uid,))
                conn.commit()
        log_admin_action(callback.from_user.id, f"unban {uid}")
        txt = f"✅ {get_mention(uid, u['first_name'])} разбанен."
    else:
        with get_db() as conn:
            with conn.cursor() as cursor:
                cursor.execute("UPDATE users SET is_banned = TRUE, ban_until = NULL WHERE user_id = %s", (uid,))
                conn.commit()
        log_admin_action(callback.from_user.id, f"ban {uid}")
        txt = f"🚫 {get_mention(uid, u['first_name'])} забанен."
    await callback.message.edit_text(txt, reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="◀️ Профиль", callback_data=f"adm:user:{uid}")
    ]]))
    await callback.answer()


@router.callback_query(F.data.startswith("adm:nick:"))
async def adm_nick_start(callback: CallbackQuery):
    if not _check_admin(callback):
        return
    uid = int(callback.data.split(":")[2])
    admin_states[callback.from_user.id] = {"action": "await_nick", "target": uid}
    await callback.message.edit_text(
        f"🏷️ <b>Смена игрового ника</b>\n\nID: <code>{uid}</code>\n\nВведи ник:",
        reply_markup=_back_kb(back_to=f"adm:user:{uid}")
    )
    await callback.answer()


@router.callback_query(F.data.startswith("adm:rank:"))
async def adm_set_rank(callback: CallbackQuery):
    if not _check_admin(callback):
        return
    if not is_head_or_above(callback.from_user.id):
        await callback.answer("Только Head+", show_alert=True)
        return
    parts = callback.data.split(":")
    uid, rank = int(parts[2]), parts[3]
    if uid == ADMIN_ID:
        await callback.answer("Нельзя менять владельца", show_alert=True)
        return
    with get_db() as conn:
        with conn.cursor() as cursor:
            cursor.execute("INSERT INTO admins (user_id, rank) VALUES (%s,%s) ON CONFLICT (user_id) DO UPDATE SET rank = EXCLUDED.rank",
                           (uid, rank))
            conn.commit()
    log_admin_action(callback.from_user.id, f"setrank {uid} {rank}")
    u = get_user(uid)
    await callback.message.edit_text(
        f"✅ {get_mention(uid, u['first_name'])} теперь {get_rank_emoji(rank)}",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="◀️ Профиль", callback_data=f"adm:user:{uid}")
        ]])
    )
    await callback.answer("Ранг установлен")


# ---------- 💰 ЭКОНОМИКА ----------
@router.callback_query(F.data == "adm:econ")
async def adm_econ(callback: CallbackQuery):
    if not _check_admin(callback):
        return
    with get_db() as conn:
        with conn.cursor() as cursor:
            cursor.execute("SELECT COUNT(*), COALESCE(SUM(balance),0), COALESCE(AVG(balance),0), COALESCE(MAX(balance),0), COALESCE(MIN(balance),0) FROM users")
            cnt, total, avg, mx, mn = cursor.fetchone()
            cursor.execute("SELECT user_id, first_name, balance FROM users ORDER BY balance DESC LIMIT 5")
            top = cursor.fetchall()
    text = (
        f"💰 <b>Экономика</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"👥 Игроков: <b>{cnt}</b>\n"
        f"💵 Сумма: <b>{format_balance(total)}</b>\n"
        f"📊 Средний: <b>{format_balance(int(avg))}</b>\n"
        f"🏆 Максимум: <b>{format_balance(mx)}</b>\n"
        f"📉 Минимум: <b>{format_balance(mn)}</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"<b>ТОП-5 богачей:</b>\n"
    )
    for i, (uid, fname, bal) in enumerate(top, 1):
        text += f"{i}. {clean_first_name(fname, uid)} — {format_balance(bal)}\n"
    buttons = []
    if is_head_or_above(callback.from_user.id):
        buttons.append([InlineKeyboardButton(text="📈 Массовая выдача", callback_data="adm:massgive")])
    buttons.append([InlineKeyboardButton(text="◀️ Назад", callback_data="adm:main")])
    await callback.message.edit_text(text, reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons))
    await callback.answer()


@router.callback_query(F.data == "adm:massgive")
async def adm_massgive(callback: CallbackQuery):
    if not is_head_or_above(callback.from_user.id):
        await callback.answer("Только Head+", show_alert=True)
        return
    admin_states[callback.from_user.id] = {"action": "mass_give"}
    await callback.message.edit_text(
        "📈 <b>Массовая выдача</b>\n\nВведи сумму — начислится <b>всем</b> юзерам.\n\nДля отмены: /cancel",
        reply_markup=_back_kb(back_to="adm:econ")
    )
    await callback.answer()


# ---------- 🎁 ПРОМО ----------
@router.callback_query(F.data == "adm:promos")
async def adm_promos(callback: CallbackQuery):
    if not _check_admin(callback):
        return
    with get_db() as conn:
        with conn.cursor() as cursor:
            cursor.execute("SELECT code, amount, uses FROM promocodes ORDER BY code LIMIT 20")
            rows = cursor.fetchall()
    text = "🎁 <b>Промокоды</b>\n━━━━━━━━━━━━━━━━━━━━\n"
    buttons = [[InlineKeyboardButton(text="➕ Создать промокод", callback_data="adm:promo_new")]]
    if rows:
        for code, amount, uses in rows:
            text += f"<code>{code}</code> — {format_balance(amount)} × {uses}\n"
            buttons.append([InlineKeyboardButton(text=f"❌ Удалить {code}", callback_data=f"adm:promo_del:{code}")])
    else:
        text += "Промокодов нет."
    buttons.append([InlineKeyboardButton(text="◀️ Назад", callback_data="adm:main")])
    await callback.message.edit_text(text, reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons))
    await callback.answer()


@router.callback_query(F.data == "adm:promo_new")
async def adm_promo_new(callback: CallbackQuery):
    if not _check_admin(callback):
        return
    admin_states[callback.from_user.id] = {"action": "promo_code"}
    await callback.message.edit_text(
        "🎁 <b>Новый промокод</b>\n\nШаг 1/3 — Введи код (3-20 символов, латиница/цифры):",
        reply_markup=_back_kb(back_to="adm:promos")
    )
    await callback.answer()


@router.callback_query(F.data.startswith("adm:promo_del:"))
async def adm_promo_del(callback: CallbackQuery):
    if not _check_admin(callback):
        return
    code = callback.data.split(":", 2)[2]
    delete_promo(code)
    log_admin_action(callback.from_user.id, f"delpromo {code}")
    await callback.answer(f"Удалён {code}")
    await adm_promos(callback)


# ---------- 🎮 ИГРЫ ----------
@router.callback_query(F.data == "adm:games")
async def adm_games(callback: CallbackQuery):
    if not _check_admin(callback):
        return
    games = ["рулетка", "джокер", "мины", "дуэль", "coinflip", "ограбление"]
    buttons = []
    for g in games:
        status = "🚫 ВЫКЛ" if is_game_disabled(g) else "✅ ВКЛ"
        buttons.append([InlineKeyboardButton(text=f"{g}: {status}", callback_data=f"adm:game_toggle:{g}")])
    buttons.append([InlineKeyboardButton(text="◀️ Назад", callback_data="adm:main")])
    await callback.message.edit_text(
        "🎮 <b>Управление играми</b>\n\nНажми на игру → переключение вкл/выкл:",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons)
    )
    await callback.answer()


@router.callback_query(F.data.startswith("adm:game_toggle:"))
async def adm_game_toggle(callback: CallbackQuery):
    if not _check_admin(callback):
        return
    game = callback.data.split(":", 2)[2]
    with get_db() as conn:
        with conn.cursor() as cursor:
            cursor.execute("SELECT 1 FROM disabled_games WHERE game_name = %s", (game,))
            if cursor.fetchone():
                cursor.execute("DELETE FROM disabled_games WHERE game_name = %s", (game,))
                state = "включена ✅"
            else:
                cursor.execute("INSERT INTO disabled_games (game_name) VALUES (%s) ON CONFLICT DO NOTHING", (game,))
                state = "отключена 🚫"
            conn.commit()
    log_admin_action(callback.from_user.id, f"toggle_game {game}")
    await callback.answer(f"Игра {game} {state}")
    await adm_games(callback)


# ---------- 📊 СТАТИСТИКА ----------
@router.callback_query(F.data == "adm:stats")
async def adm_stats(callback: CallbackQuery):
    if not _check_admin(callback):
        return
    with get_db() as conn:
        with conn.cursor() as cursor:
            cursor.execute("SELECT COUNT(*), COALESCE(SUM(balance),0) FROM users")
            cnt, total = cursor.fetchone()
            cursor.execute("SELECT COUNT(*) FROM users WHERE is_banned = TRUE")
            bans = cursor.fetchone()[0]
            cursor.execute("SELECT COUNT(*) FROM admins")
            adms = cursor.fetchone()[0]
            cursor.execute("SELECT COUNT(*) FROM promocodes WHERE uses > 0")
            promos = cursor.fetchone()[0]
            cursor.execute("SELECT COUNT(*) FROM clans")
            clans = cursor.fetchone()[0]
            cursor.execute("SELECT COALESCE(SUM(games_played),0), COALESCE(SUM(games_won),0) FROM users")
            gp, gw = cursor.fetchone()
            cursor.execute("SELECT COUNT(*) FROM deposits")
            deps = cursor.fetchone()[0]
    text = (
        f"📊 <b>Статистика {BOT_NAME}</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"👥 Пользователей: <b>{cnt}</b>\n"
        f"💰 Общий баланс: <b>{format_balance(total)}</b>\n"
        f"🚫 Забанено: <b>{bans}</b>\n"
        f"🛡 Админов: <b>{adms}</b>\n"
        f"🎁 Активных промо: <b>{promos}</b>\n"
        f"🏰 Кланов: <b>{clans}</b>\n"
        f"🏦 Депозитов: <b>{deps}</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"🎮 Всего игр: <b>{gp}</b>\n"
        f"🏆 Побед: <b>{gw}</b>"
    )
    await callback.message.edit_text(text, reply_markup=_back_kb())
    await callback.answer()


# ---------- 📢 РАССЫЛКА ----------
@router.callback_query(F.data == "adm:broadcast")
async def adm_broadcast(callback: CallbackQuery):
    if not _check_admin(callback):
        return
    if not is_head_or_above(callback.from_user.id):
        await callback.answer("Только Head+", show_alert=True)
        return
    admin_states[callback.from_user.id] = {"action": "broadcast"}
    await callback.message.edit_text(
        "📢 <b>Рассылка</b>\n\nОтправь текст (HTML поддерживается).\n\nДля отмены: /cancel",
        reply_markup=_back_kb()
    )
    await callback.answer()


# ---------- ⚙️ НАСТРОЙКИ ----------
@router.callback_query(F.data == "adm:settings")
async def adm_settings(callback: CallbackQuery):
    if not _check_admin(callback):
        return
    bonus = get_setting("bonus_amount", "3000")
    cd = get_setting("bonus_cooldown", "8")
    ref_bonus = get_setting("referral_bonus", "75000")
    ref_limit = get_setting("referral_limit", "25")
    btc_rate = get_setting("btc_rate", "1000000000")
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=f"🎁 Бонус: {format_balance(int(bonus))}", callback_data="adm:set:bonus")],
        [InlineKeyboardButton(text=f"⏰ Кулдаун: {cd} ч", callback_data="adm:set:cd")],
        [InlineKeyboardButton(text=f"👥 Реф.бонус: {format_balance(int(ref_bonus))}", callback_data="adm:set:refbonus")],
        [InlineKeyboardButton(text=f"🎯 Реф.лимит: {ref_limit}", callback_data="adm:set:reflimit")],
        [InlineKeyboardButton(text=f"💎 Курс BTC: {format_balance(int(btc_rate))}", callback_data="adm:set:rate")],
        [InlineKeyboardButton(text="◀️ Назад", callback_data="adm:main")]
    ])
    await callback.message.edit_text(
        "⚙️ <b>Настройки</b>\n\nНажми для изменения:",
        reply_markup=kb
    )
    await callback.answer()


@router.callback_query(F.data.startswith("adm:set:"))
async def adm_set_start(callback: CallbackQuery):
    if not _check_admin(callback):
        return
    if not is_head_or_above(callback.from_user.id):
        await callback.answer("Только Head+", show_alert=True)
        return
    key = callback.data.split(":")[2]
    prompts = {
        "bonus": "🎁 Введи новый бонус (число):",
        "cd": "⏰ Введи новый кулдаун в часах:",
        "refbonus": "👥 Введи новый реф.бонус:",
        "reflimit": "🎯 Введи новый реф.лимит (число бонусов):",
        "rate": "💎 Введи новый курс BTC (в COINS за 1 BTC):"
    }
    admin_states[callback.from_user.id] = {"action": "set_setting", "key": key}
    await callback.message.edit_text(prompts[key], reply_markup=_back_kb(back_to="adm:settings"))
    await callback.answer()


# ---------- 🚫 БАНЫ ----------
@router.callback_query(F.data == "adm:bans")
async def adm_bans(callback: CallbackQuery):
    if not _check_admin(callback):
        return
    with get_db() as conn:
        with conn.cursor() as cursor:
            cursor.execute("SELECT user_id, first_name, username FROM users WHERE is_banned = TRUE LIMIT 20")
            rows = cursor.fetchall()
    if not rows:
        await callback.message.edit_text("✅ Забаненных нет.", reply_markup=_back_kb())
        await callback.answer()
        return
    text = "🚫 <b>Забаненные</b>\n\n"
    buttons = []
    for uid, fname, uname in rows:
        text += f"• {clean_first_name(fname, uid)} (@{uname or '—'})\n"
        buttons.append([InlineKeyboardButton(text=f"✅ Разбан {fname or uid}", callback_data=f"adm:ban:{uid}")])
    buttons.append([InlineKeyboardButton(text="◀️ Назад", callback_data="adm:main")])
    await callback.message.edit_text(text, reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons))
    await callback.answer()


# ---------- 📜 ЛОГИ ----------
@router.callback_query(F.data == "adm:logs")
async def adm_logs(callback: CallbackQuery):
    if not _check_admin(callback):
        return
    with get_db() as conn:
        with conn.cursor() as cursor:
            cursor.execute("SELECT admin_id, action, target_id, amount, timestamp FROM admin_log ORDER BY id DESC LIMIT 20")
            rows = cursor.fetchall()
    if not rows:
        await callback.message.edit_text("📜 Лог пуст.", reply_markup=_back_kb())
        await callback.answer()
        return
    text = "📜 <b>Последние действия</b>\n\n"
    for admin_id, action, target_id, amount, ts in rows:
        u = get_user(admin_id)
        line = f"🕒 {ts[:16] if ts else '-'} | {u['first_name']}: {action}"
        if target_id:
            line += f" | → {target_id}"
        if amount:
            line += f" | {format_balance(amount)}"
        text += line + "\n"
    await callback.message.edit_text(text, reply_markup=_back_kb())
    await callback.answer()


# ---------- 💾 БЭКАП ----------
@router.callback_query(F.data == "adm:backup")
async def adm_backup(callback: CallbackQuery):
    if not _check_admin(callback):
        return
    if not is_owner(callback.from_user.id):
        await callback.answer("Только владелец", show_alert=True)
        return
    with get_db() as conn:
        with conn.cursor() as cursor:
            cursor.execute("SELECT * FROM users")
            users = [dict(zip([c[0] for c in cursor.description], row)) for row in cursor.fetchall()]
    backup = json.dumps(users, default=str, ensure_ascii=False, indent=2)
    await callback.message.answer_document(BufferedInputFile(backup.encode(), "backup.json"))
    await callback.answer("Бэкап отправлен")


# ---------- 🛡 АДМИНЫ ----------
@router.callback_query(F.data == "adm:admins")
async def adm_admins(callback: CallbackQuery):
    if not _check_admin(callback):
        return
    with get_db() as conn:
        with conn.cursor() as cursor:
            cursor.execute("SELECT user_id, rank FROM admins ORDER BY CASE rank WHEN 'owner' THEN 0 WHEN 'head' THEN 1 WHEN 'spadmin' THEN 2 WHEN 'admin' THEN 3 WHEN 'moder' THEN 4 END")
            rows = cursor.fetchall()
    text = "🛡 <b>Администраторы</b>\n\n"
    buttons = []
    for uid, rank in rows:
        u = get_user(uid)
        text += f"{get_rank_emoji(rank)} — {clean_first_name(u['first_name'], uid)}\n"
        if rank != "owner" and is_head_or_above(callback.from_user.id):
            buttons.append([InlineKeyboardButton(text=f"❌ Снять {u['first_name']}", callback_data=f"adm:unadmin:{uid}")])
    buttons.append([InlineKeyboardButton(text="◀️ Назад", callback_data="adm:main")])
    await callback.message.edit_text(text, reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons))
    await callback.answer()


@router.callback_query(F.data.startswith("adm:unadmin:"))
async def adm_unadmin(callback: CallbackQuery):
    if not _check_admin(callback):
        return
    if not is_head_or_above(callback.from_user.id):
        await callback.answer("Только Head+", show_alert=True)
        return
    uid = int(callback.data.split(":")[2])
    if uid == ADMIN_ID:
        await callback.answer("Нельзя снять владельца", show_alert=True)
        return
    with get_db() as conn:
        with conn.cursor() as cursor:
            cursor.execute("DELETE FROM admins WHERE user_id = %s", (uid,))
            conn.commit()
    log_admin_action(callback.from_user.id, f"unadmin {uid}")
    await callback.answer("Снят")
    await adm_admins(callback)

# ============================================================
# 🆕 НОВЫЕ РАЗДЕЛЫ АДМИН-ПАНЕЛИ
# ============================================================

# ---------- 🔍 ПОИСК ПО НИКУ ----------
@router.callback_query(F.data == "adm:search")
async def adm_search(callback: CallbackQuery):
    if not _check_admin(callback):
        return
    admin_states[callback.from_user.id] = {"action": "search_nick"}
    await callback.message.edit_text(
        "🔍 <b>Поиск по нику</b>\n\n"
        "Введи часть имени или ника игрока (мин 2 символа):\n\n"
        "<i>Для отмены: /cancel</i>",
        reply_markup=_back_kb()
    )
    await callback.answer()


# ---------- 📈 АНАЛИТИКА ----------
@router.callback_query(F.data == "adm:analytics")
async def adm_analytics(callback: CallbackQuery):
    if not _check_admin(callback):
        return
    with get_db() as conn:
        with conn.cursor() as cursor:
            cursor.execute("SELECT COALESCE(SUM(games_played),0), COALESCE(SUM(games_won),0) FROM users")
            gp, gw = cursor.fetchone()
            today = datetime.now().strftime("%Y-%m-%d")
            cursor.execute("SELECT COUNT(*) FROM daily_tasks WHERE date = %s", (today,))
            active_today = cursor.fetchone()[0]
            cursor.execute("SELECT COUNT(*) FROM roulette_log WHERE roll IS NOT NULL")
            total_rolls = cursor.fetchone()[0]
            cursor.execute("SELECT user_id, SUM(win_amount) FROM roulette_log WHERE win_amount > 0 GROUP BY user_id ORDER BY SUM(win_amount) DESC LIMIT 5")
            top_roulette = cursor.fetchall()
            cursor.execute("SELECT user_id, win_amount FROM roulette_log WHERE win_amount > 0 ORDER BY win_amount DESC LIMIT 1")
            big_win = cursor.fetchone()
            cursor.execute("SELECT COUNT(*) FROM users WHERE last_daily = %s", (today,))
            daily_today = cursor.fetchone()[0]
    win_rate = round(gw / gp * 100, 1) if gp > 0 else 0

    text = (
        f"📈 <b>Аналитика {BOT_NAME}</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"🎮 Всего игр: <b>{gp}</b>\n"
        f"🏆 Побед: <b>{gw}</b>\n"
        f"📊 Винрейт: <b>{win_rate}%</b>\n"
        f"👥 Активных сегодня: <b>{daily_today}</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"🎰 <b>Рулетка:</b>\n"
        f"🎲 Всего прокрутов: <b>{total_rolls}</b>\n"
    )
    if big_win:
        bw_user = get_user(big_win[0])
        text += f"🎯 Крупнейший выигрыш: <b>{format_balance(big_win[1])}</b> ({clean_first_name(bw_user['first_name'], big_win[0])})\n"

    if top_roulette:
        text += f"\n💰 <b>ТОП-5 по выигрышам в рулетке:</b>\n"
        for i, (uid, wins) in enumerate(top_roulette, 1):
            u = get_user(uid)
            text += f"{i}. {clean_first_name(u['first_name'], uid)} — <b>{format_balance(wins)}</b>\n"

    await callback.message.edit_text(text, reply_markup=_back_kb())
    await callback.answer()


# ---------- 🎁 МАССОВАЯ РАЗДАЧА ----------
@router.callback_query(F.data == "adm:massgift")
async def adm_massgift(callback: CallbackQuery):
    if not _check_admin(callback):
        return
    if not is_head_or_above(callback.from_user.id):
        await callback.answer("Только Head+", show_alert=True)
        return
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📦 Кейсы всем", callback_data="adm:mg:case"),
         InlineKeyboardButton(text="👑 VIP всем", callback_data="adm:mg:vip")],
        [InlineKeyboardButton(text="💎 BTC всем", callback_data="adm:mg:btc")],
        [InlineKeyboardButton(text="◀️ Назад", callback_data="adm:main")]
    ])
    await callback.message.edit_text(
        "🎁 <b>Массовая раздача</b>\n\n"
        "Выбери что раздать <b>ВСЕМ</b> игрокам:\n\n"
        "⚠️ <i>Действие необратимо</i>",
        reply_markup=kb
    )
    await callback.answer()


@router.callback_query(F.data == "adm:mg:case")
async def adm_mg_case(callback: CallbackQuery):
    if not _check_admin(callback):
        return
    cases = ["деревянный", "железный", "золотой", "алмазный", "легендарный", "мифический", "космический"]
    kb = [[InlineKeyboardButton(text=c, callback_data=f"adm:mg_do_case:{c}")] for c in cases]
    kb.append([InlineKeyboardButton(text="◀️ Назад", callback_data="adm:massgift")])
    await callback.message.edit_text(
        "📦 <b>Кейсы всем</b>\n\nВыбери тип кейса:",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=kb)
    )
    await callback.answer()


@router.callback_query(F.data.startswith("adm:mg_do_case:"))
async def adm_mg_do_case(callback: CallbackQuery):
    if not is_head_or_above(callback.from_user.id):
        await callback.answer("Только Head+", show_alert=True)
        return
    case_type = callback.data.split(":")[2]
    with get_db() as conn:
        with conn.cursor() as cursor:
            cursor.execute("SELECT user_id FROM users")
            users = [r[0] for r in cursor.fetchall()]
            for uid in users:
                cursor.execute(
                    "INSERT INTO cases_inventory (user_id, case_type, count) VALUES (%s, %s, 1) "
                    "ON CONFLICT (user_id, case_type) DO UPDATE SET count = cases_inventory.count + 1",
                    (uid, case_type))
            conn.commit()
    log_admin_action(callback.from_user.id, f"massgift case {case_type} to {len(users)}")
    await callback.answer(f"✅ {case_type} выдан {len(users)} юзерам", show_alert=True)
    await adm_massgift(callback)


@router.callback_query(F.data == "adm:mg:vip")
async def adm_mg_vip(callback: CallbackQuery):
    if not _check_admin(callback):
        return
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="24 часа", callback_data="adm:mg_do_vip:24"),
         InlineKeyboardButton(text="72 часа", callback_data="adm:mg_do_vip:72")],
        [InlineKeyboardButton(text="7 дней", callback_data="adm:mg_do_vip:168"),
         InlineKeyboardButton(text="30 дней", callback_data="adm:mg_do_vip:720")],
        [InlineKeyboardButton(text="◀️ Назад", callback_data="adm:massgift")]
    ])
    await callback.message.edit_text(
        "👑 <b>VIP всем</b>\n\nВыбери срок:",
        reply_markup=kb
    )
    await callback.answer()


@router.callback_query(F.data.startswith("adm:mg_do_vip:"))
async def adm_mg_do_vip(callback: CallbackQuery):
    if not is_head_or_above(callback.from_user.id):
        await callback.answer("Только Head+", show_alert=True)
        return
    hours = int(callback.data.split(":")[2])
    new_until = datetime.now() + timedelta(hours=hours)
    with get_db() as conn:
        with conn.cursor() as cursor:
            cursor.execute("UPDATE users SET vip_until = GREATEST(COALESCE(vip_until, NOW()), %s)", (new_until,))
            conn.commit()
    log_admin_action(callback.from_user.id, f"massgift vip {hours}h")
    await callback.answer(f"✅ VIP на {hours}ч всем выдан", show_alert=True)
    await adm_massgift(callback)


@router.callback_query(F.data == "adm:mg:btc")
async def adm_mg_btc(callback: CallbackQuery):
    if not _check_admin(callback):
        return
    admin_states[callback.from_user.id] = {"action": "massgift_btc"}
    await callback.message.edit_text(
        "💎 <b>BTC всем</b>\n\n"
        "Введи количество BTC для каждого юзера (например 0.001):\n\n"
        "<i>Для отмены: /cancel</i>",
        reply_markup=_back_kb(back_to="adm:massgift")
    )
    await callback.answer()


# ---------- 💎 VIP-УПРАВЛЕНИЕ ----------
@router.callback_query(F.data == "adm:vipmgr")
async def adm_vipmgr(callback: CallbackQuery):
    if not _check_admin(callback):
        return
    with get_db() as conn:
        with conn.cursor() as cursor:
            cursor.execute("SELECT user_id, first_name, vip_until FROM users WHERE vip_until > NOW() ORDER BY vip_until LIMIT 30")
            rows = cursor.fetchall()
    text = f"💎 <b>VIP-управление</b>\n━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
    text += f"👑 Активных VIP: <b>{len(rows)}</b>\n\n"
    buttons = []
    for uid, fname, until in rows:
        until_str = until.strftime("%d.%m.%Y %H:%M") if until else "—"
        text += f"• {clean_first_name(fname, uid)} — до {until_str}\n"
        buttons.append([InlineKeyboardButton(text=f"❌ Снять {fname}", callback_data=f"adm:viprm:{uid}")])
    if rows:
        buttons.append([InlineKeyboardButton(text="❌ Снять ВСЕХ ⚠️", callback_data="adm:viprmall")])
    buttons.append([InlineKeyboardButton(text="◀️ Назад", callback_data="adm:main")])
    await callback.message.edit_text(text, reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons))
    await callback.answer()


@router.callback_query(F.data.startswith("adm:viprm:"))
async def adm_viprm(callback: CallbackQuery):
    if not _check_admin(callback):
        return
    if not is_head_or_above(callback.from_user.id):
        await callback.answer("Только Head+", show_alert=True)
        return
    uid = int(callback.data.split(":")[2])
    with get_db() as conn:
        with conn.cursor() as cursor:
            cursor.execute("UPDATE users SET vip_until = NULL WHERE user_id = %s", (uid,))
            conn.commit()
    log_admin_action(callback.from_user.id, f"viprm {uid}")
    await callback.answer("✅ VIP снят", show_alert=True)
    await adm_vipmgr(callback)


@router.callback_query(F.data == "adm:viprmall")
async def adm_viprmall(callback: CallbackQuery):
    if not is_head_or_above(callback.from_user.id):
        await callback.answer("Только Head+", show_alert=True)
        return
    with get_db() as conn:
        with conn.cursor() as cursor:
            cursor.execute("UPDATE users SET vip_until = NULL WHERE vip_until IS NOT NULL")
            conn.commit()
    log_admin_action(callback.from_user.id, "viprmall")
    await callback.answer("✅ Все VIP сняты", show_alert=True)
    await adm_vipmgr(callback)


# ---------- ⛏️ МАЙНИНГ ----------
@router.callback_query(F.data == "adm:mining")
async def adm_mining(callback: CallbackQuery):
    if not _check_admin(callback):
        return
    with get_db() as conn:
        with conn.cursor() as cursor:
            cursor.execute("SELECT COUNT(DISTINCT user_id) FROM miners")
            miners_cnt = cursor.fetchone()[0]
            cursor.execute("SELECT COUNT(*) FROM miners")
            total_cards = cursor.fetchone()[0]
            cursor.execute("SELECT card_type, SUM(count) FROM miners GROUP BY card_type ORDER BY SUM(count) DESC")
            by_type = cursor.fetchall()
            cursor.execute("SELECT user_id, SUM(count) as cnt FROM miners GROUP BY user_id ORDER BY cnt DESC LIMIT 5")
            top = cursor.fetchall()
    text = (
        f"⛏️ <b>Майнинг</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"👥 Майнеров: <b>{miners_cnt}</b>\n"
        f"🎮 Всего карт: <b>{total_cards}</b>\n\n"
        f"<b>По типам:</b>\n"
    )
    for card, cnt in by_type:
        text += f"• {card}: <b>{cnt}</b>\n"
    if top:
        text += f"\n🏆 <b>ТОП-5 майнеров:</b>\n"
        for uid, cnt in top:
            u = get_user(uid)
            text += f"• {clean_first_name(u['first_name'], uid)} — <b>{cnt}</b> карт\n"
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔄 Сбросить ВСЕ фермы ⚠️", callback_data="adm:mining_reset")],
        [InlineKeyboardButton(text="◀️ Назад", callback_data="adm:main")]
    ])
    await callback.message.edit_text(text, reply_markup=kb)
    await callback.answer()


@router.callback_query(F.data == "adm:mining_reset")
async def adm_mining_reset(callback: CallbackQuery):
    if not is_head_or_above(callback.from_user.id):
        await callback.answer("Только Head+", show_alert=True)
        return
    with get_db() as conn:
        with conn.cursor() as cursor:
            cursor.execute("DELETE FROM miners")
            cursor.execute("UPDATE mining_stats SET accumulated_btc = 0")
            conn.commit()
    log_admin_action(callback.from_user.id, "mining_reset_all")
    await callback.answer("✅ Все фермы сброшены", show_alert=True)
    await adm_mining(callback)


# ---------- 🏦 ДЕПОЗИТЫ ----------
@router.callback_query(F.data == "adm:deposits")
async def adm_deposits(callback: CallbackQuery):
    if not _check_admin(callback):
        return
    with get_db() as conn:
        with conn.cursor() as cursor:
            cursor.execute("SELECT user_id, amount, start_time FROM deposits ORDER BY amount DESC LIMIT 30")
            rows = cursor.fetchall()
    if not rows:
        await callback.message.edit_text("🏦 <b>Активных депозитов нет.</b>", reply_markup=_back_kb())
        await callback.answer()
        return
    total = sum(r[1] for r in rows)
    text = f"🏦 <b>Депозиты</b>\n━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
    text += f"📊 Всего: <b>{len(rows)}</b> | Сумма: <b>{format_balance(total)}</b>\n\n"
    for uid, amt, start in rows[:15]:
        u = get_user(uid)
        interest = deposit_interest(start, amt)
        text += f"• {clean_first_name(u['first_name'], uid)} — {format_balance(amt)} (+{format_balance(interest)})\n"
    await callback.message.edit_text(text, reply_markup=_back_kb())
    await callback.answer()


# ---------- 📤 В КАНАЛ ----------
@router.callback_query(F.data == "adm:tochannel")
async def adm_tochannel(callback: CallbackQuery):
    if not _check_admin(callback):
        return
    if not is_head_or_above(callback.from_user.id):
        await callback.answer("Только Head+", show_alert=True)
        return
    admin_states[callback.from_user.id] = {"action": "tochannel_text"}
    await callback.message.edit_text(
        f"📤 <b>Пост в канал</b>\n\n"
        f"Канал: <a href='{CHANNEL_URL}'>{CHANNEL_URL}</a>\n\n"
        f"Отправь текст поста (HTML поддерживается):\n\n"
        f"<i>Для отмены: /cancel</i>",
        reply_markup=_back_kb()
    )
    await callback.answer()


# ---------- 🔄 СБРОСЫ ----------
@router.callback_query(F.data == "adm:resets")
async def adm_resets(callback: CallbackQuery):
    if not _check_admin(callback):
        return
    if not is_head_or_above(callback.from_user.id):
        await callback.answer("Только Head+", show_alert=True)
        return
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔄 Сброс статистики (игр/побед)", callback_data="adm:reset_stats"),
         InlineKeyboardButton(text="📅 Сброс ежедневок", callback_data="adm:reset_daily")],
        [InlineKeyboardButton(text="📦 Сброс кейсов", callback_data="adm:reset_cases"),
         InlineKeyboardButton(text="⛏️ Сброс майнеров", callback_data="adm:reset_mining")],
        [InlineKeyboardButton(text="💰 Сброс ВСЕХ балансов ⚠️", callback_data="adm:reset_balances")],
        [InlineKeyboardButton(text="◀️ Назад", callback_data="adm:main")]
    ])
    await callback.message.edit_text(
        "🔄 <b>Сбросы</b>\n\n"
        "Выбери что сбросить <b>у всех</b> игроков:\n\n"
        "⚠️ <i>Действия необратимы</i>",
        reply_markup=kb
    )
    await callback.answer()


@router.callback_query(F.data == "adm:reset_stats")
async def adm_reset_stats(callback: CallbackQuery):
    if not is_head_or_above(callback.from_user.id):
        await callback.answer("Только Head+", show_alert=True)
        return
    with get_db() as conn:
        with conn.cursor() as cursor:
            cursor.execute("UPDATE users SET games_played = 0, games_won = 0")
            conn.commit()
    log_admin_action(callback.from_user.id, "reset_stats_all")
    await callback.answer("✅ Статистика сброшена", show_alert=True)


@router.callback_query(F.data == "adm:reset_daily")
async def adm_reset_daily(callback: CallbackQuery):
    if not is_head_or_above(callback.from_user.id):
        await callback.answer("Только Head+", show_alert=True)
        return
    with get_db() as conn:
        with conn.cursor() as cursor:
            cursor.execute("UPDATE users SET last_daily = NULL")
            cursor.execute("DELETE FROM daily_tasks WHERE date < %s", (datetime.now().strftime("%Y-%m-%d"),))
            conn.commit()
    log_admin_action(callback.from_user.id, "reset_daily_all")
    await callback.answer("✅ Ежедневки сброшены", show_alert=True)


@router.callback_query(F.data == "adm:reset_cases")
async def adm_reset_cases(callback: CallbackQuery):
    if not is_head_or_above(callback.from_user.id):
        await callback.answer("Только Head+", show_alert=True)
        return
    with get_db() as conn:
        with conn.cursor() as cursor:
            cursor.execute("DELETE FROM cases_inventory")
            conn.commit()
    log_admin_action(callback.from_user.id, "reset_cases_all")
    await callback.answer("✅ Все кейсы удалены", show_alert=True)


@router.callback_query(F.data == "adm:reset_mining")
async def adm_reset_mining(callback: CallbackQuery):
    if not is_head_or_above(callback.from_user.id):
        await callback.answer("Только Head+", show_alert=True)
        return
    with get_db() as conn:
        with conn.cursor() as cursor:
            cursor.execute("DELETE FROM miners")
            cursor.execute("UPDATE mining_stats SET accumulated_btc = 0")
            conn.commit()
    log_admin_action(callback.from_user.id, "reset_mining_all")
    await callback.answer("✅ Все фермы сброшены", show_alert=True)


@router.callback_query(F.data == "adm:reset_balances")
async def adm_reset_balances_confirm(callback: CallbackQuery):
    if not is_owner(callback.from_user.id):
        await callback.answer("Только владелец!", show_alert=True)
        return
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ ДА, сбросить всем 4000", callback_data="adm:reset_bal_yes")],
        [InlineKeyboardButton(text="❌ Отмена", callback_data="adm:resets")]
    ])
    await callback.message.edit_text(
        "⚠️ <b>ВНИМАНИЕ!</b>\n\n"
        "Сбросить балансы <b>ВСЕХ</b> игроков до <b>4000 COINS</b>?\n\n"
        "Это <b>НЕОБРАТИМО</b>!",
        reply_markup=kb
    )
    await callback.answer()


@router.callback_query(F.data == "adm:reset_bal_yes")
async def adm_reset_bal_yes(callback: CallbackQuery):
    if not is_owner(callback.from_user.id):
        return
    with get_db() as conn:
        with conn.cursor() as cursor:
            cursor.execute("UPDATE users SET balance = 4000")
            conn.commit()
    log_admin_action(callback.from_user.id, "reset_balances_all")
    await callback.answer("✅ Все балансы сброшены до 4000", show_alert=True)
    await adm_main(callback)

# ============================================================
# STATE-ОБРАБОТЧИК (ввод от админа)
# ============================================================
@router.message(F.text, lambda m: m.from_user.id in admin_states)
async def admin_state_handler(message: Message):
    uid = message.from_user.id
    if not is_admin(uid):
        admin_states.pop(uid, None)
        return
    state = admin_states.get(uid, {})
    action = state.get("action")
    text = (message.text or "").strip()

    if text == "/cancel":
        admin_states.pop(uid, None)
        await message.answer("❌ Отменено.", reply_markup=_admin_main_kb(uid))
        return

    try:
        if action == "find_user":
            target = None
            if message.reply_to_message and message.reply_to_message.from_user:
                target = get_user(message.reply_to_message.from_user.id)
            else:
                target = find_user_by_identifier(text)
            if not target:
                await message.answer("❌ Не найден. Попробуй снова или /cancel.")
                return
            admin_states.pop(uid, None)
            await message.answer(_user_profile_text(target),
                                  reply_markup=_user_actions_kb(target["user_id"]))

        elif action == "await_amount":
            try:
                amount = int(text.replace(" ", "").replace("_", ""))
            except:
                await message.answer("❌ Введи число.")
                return
            target_uid = state["target"]
            kind = state["kind"]
            if kind == "give":
                update_balance(target_uid, amount)
                log_admin_action(uid, f"give {target_uid} {amount}")
                await message.answer(f"✅ Выдано {format_balance(amount)} → ID <code>{target_uid}</code>",
                                      reply_markup=_admin_main_kb(uid))
            elif kind == "take":
                update_balance(target_uid, -amount)
                log_admin_action(uid, f"take {target_uid} {amount}")
                await message.answer(f"✅ Списано {format_balance(amount)} у ID <code>{target_uid}</code>",
                                      reply_markup=_admin_main_kb(uid))
            elif kind == "setbal":
                with get_db() as conn:
                    with conn.cursor() as cursor:
                        cursor.execute("UPDATE users SET balance = %s WHERE user_id = %s", (amount, target_uid))
                        conn.commit()
                log_admin_action(uid, f"setbal {target_uid} {amount}")
                await message.answer(f"✅ Баланс установлен: {format_balance(amount)}",
                                      reply_markup=_admin_main_kb(uid))
            admin_states.pop(uid, None)

        elif action == "await_nick":
            target_uid = state["target"]
            nick = text[:32]
            with get_db() as conn:
                with conn.cursor() as cursor:
                    cursor.execute("UPDATE users SET custom_nick = %s WHERE user_id = %s", (nick, target_uid))
                    conn.commit()
            log_admin_action(uid, f"nick {target_uid} {nick}")
            admin_states.pop(uid, None)
            await message.answer(f"✅ Ник изменён: 🏷️ {nick}", reply_markup=_admin_main_kb(uid))

        elif action == "promo_code":
            if not re.match(r"^[A-Za-z0-9_]{3,20}$", text):
                await message.answer("❌ 3-20 символов, латиница/цифры/_")
                return
            admin_states[uid] = {"action": "promo_amount", "code": text}
            await message.answer(f"Код: <code>{text}</code>\n\nШаг 2/3 — Введи сумму награды:")

        elif action == "promo_amount":
            try:
                amount = int(text.replace(" ", ""))
            except:
                await message.answer("❌ Число.")
                return
            admin_states[uid] = {"action": "promo_uses", "code": state["code"], "amount": amount}
            await message.answer(f"Сумма: {format_balance(amount)}\n\nШаг 3/3 — Введи кол-во использований:")

        elif action == "promo_uses":
            try:
                uses = int(text)
            except:
                await message.answer("❌ Число.")
                return
            code = state["code"]
            amount = state["amount"]
            with get_db() as conn:
                with conn.cursor() as cursor:
                    cursor.execute("INSERT INTO promocodes (code, amount, uses) VALUES (%s,%s,%s) ON CONFLICT (code) DO UPDATE SET amount=EXCLUDED.amount, uses=EXCLUDED.uses",
                                   (code, amount, uses))
                    conn.commit()
            log_admin_action(uid, f"addpromo {code} {amount} {uses}")
            admin_states.pop(uid, None)
            await message.answer(
                f"✅ Промокод создан!\n\n<code>{code}</code> — {format_balance(amount)} × {uses}",
                reply_markup=_admin_main_kb(uid)
            )

        elif action == "set_setting":
            key = state["key"]
            try:
                val = int(text.replace(" ", ""))
            except:
                await message.answer("❌ Число.")
                return
            mapping = {"bonus": "bonus_amount", "cd": "bonus_cooldown",
                       "refbonus": "referral_bonus", "reflimit": "referral_limit", "rate": "btc_rate"}
            set_setting(mapping[key], str(val))
            log_admin_action(uid, f"set_{mapping[key]} {val}")
            admin_states.pop(uid, None)
            await message.answer(f"✅ Настройка изменена: <b>{val}</b>", reply_markup=_admin_main_kb(uid))

        elif action == "broadcast":
            with get_db() as conn:
                with conn.cursor() as cursor:
                    cursor.execute("SELECT user_id FROM users")
                    users = [r[0] for r in cursor.fetchall()]
            status = await message.answer(f"📢 Начинаю рассылку ({len(users)})...")
            ok = fail = 0
            for u_id in users:
                try:
                    await bot.send_message(int(u_id), f"📢 <b>Объявление</b>\n\n{text}")
                    ok += 1
                    await asyncio.sleep(0.05)
                except:
                    fail += 1
            admin_states.pop(uid, None)
            await status.edit_text(f"✅ Рассылка завершена!\n📤 Доставлено: {ok}\n❌ Ошибок: {fail}")

        elif action == "mass_give":
            try:
                amount = int(text.replace(" ", ""))
            except:
                await message.answer("❌ Число.")
                return
            with get_db() as conn:
                with conn.cursor() as cursor:
                    cursor.execute("UPDATE users SET balance = balance + %s", (amount,))
                    conn.commit()
            log_admin_action(uid, f"mass_give {amount}")
            admin_states.pop(uid, None)
            await message.answer(f"✅ Всем начислено {format_balance(amount)}", reply_markup=_admin_main_kb(uid))

        elif action == "search_nick":
            if len(text) < 2:
                await message.answer("❌ Минимум 2 символа. Или /cancel.")
                return
            with get_db() as conn:
                with conn.cursor() as cursor:
                    cursor.execute("""
                        SELECT user_id, first_name, username, balance FROM users
                        WHERE LOWER(first_name) LIKE LOWER(%s)
                           OR LOWER(custom_nick) LIKE LOWER(%s)
                           OR LOWER(username) LIKE LOWER(%s)
                        ORDER BY balance DESC LIMIT 15
                    """, (f"%{text}%", f"%{text}%", f"%{text}%"))
                    matches = cursor.fetchall()
            if not matches:
                await message.answer(f"❌ По запросу «{text}» никого не найдено. Попробуй снова или /cancel.")
                return
            admin_states.pop(uid, None)
            text_out = f"🔍 <b>Результаты: «{text}»</b>\n\n"
            buttons = []
            for m_uid, fname, uname, bal in matches:
                text_out += f"• {clean_first_name(fname, m_uid)} (@{uname or '—'}) — {format_balance(bal)}\n"
                buttons.append([InlineKeyboardButton(
                    text=f"{fname or m_uid} — {format_balance(bal)}",
                    callback_data=f"adm:user:{m_uid}"
                )])
            buttons.append([InlineKeyboardButton(text="◀️ Назад", callback_data="adm:main")])
            await message.answer(text_out, reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons))

        elif action == "tochannel_text":
            try:
                await bot.send_message(CHANNEL_URL.replace("https://t.me/", "@"), text)
                log_admin_action(uid, "tochannel")
                admin_states.pop(uid, None)
                await message.answer(
                    f"✅ <b>Пост отправлен в канал!</b>\n\n"
                    f"🔗 {CHANNEL_URL}",
                    reply_markup=_admin_main_kb(uid)
                )
            except Exception as e:
                await message.answer(f"❌ Ошибка отправки: {e}\n\nПроверь, что бот админ канала.")

        elif action == "massgift_btc":
            try:
                btc_amt = float(text.replace(",", "."))
            except:
                await message.answer("❌ Введи число (например 0.001).")
                return
            if btc_amt <= 0:
                await message.answer("❌ Должно быть > 0.")
                return
            with get_db() as conn:
                with conn.cursor() as cursor:
                    cursor.execute("UPDATE users SET btc = btc + %s", (btc_amt,))
                    conn.commit()
            log_admin_action(uid, f"massgift_btc {btc_amt}")
            admin_states.pop(uid, None)
            await message.answer(
                f"✅ Всем выдано по <b>{btc_amt:.8f} BTC</b>\n"
                f"~{format_balance(btc_to_coins(btc_amt))} каждому",
                reply_markup=_admin_main_kb(uid)
            )

        else:
            admin_states.pop(uid, None)

    except Exception as e:
        logging.error(f"Admin state error: {e}")
        admin_states.pop(uid, None)
        await message.answer(f"❌ Ошибка: {e}", reply_markup=_admin_main_kb(uid))


# ============================================================
# 📋 КОМАНДЫ-АЛИАСЫ (обычные /command)
# ============================================================
@router.message(Command("info"))
async def cmd_info(message: Message):
    if not is_moder_or_above(message.from_user.id):
        return
    target = None
    if message.reply_to_message and message.reply_to_message.from_user:
        target = get_user(message.reply_to_message.from_user.id)
    else:
        args = message.text.split()
        if len(args) < 2:
            await message.answer("Использование: <code>/info @user</code> или реплай")
            return
        target = find_user_by_identifier(args[1])
    if not target:
        await message.answer("❌ Пользователь не найден.")
        return
    await message.answer(_user_profile_text(target), reply_markup=_user_actions_kb(target["user_id"]))


@router.message(Command("stats"))
async def cmd_stats(message: Message):
    if not is_moder_or_above(message.from_user.id):
        return
    with get_db() as conn:
        with conn.cursor() as cursor:
            cursor.execute("SELECT COUNT(*), COALESCE(SUM(balance),0) FROM users")
            cnt, total = cursor.fetchone()
    await message.answer(f"📊 Пользователей: <b>{cnt}</b>\n💰 Баланс: <b>{format_balance(total)}</b>")


@router.message(Command("setbal"))
async def cmd_setbal(message: Message):
    if not is_moder_or_above(message.from_user.id):
        return
    args = message.text.split()
    if len(args) < 3 or not args[2].replace("-", "").isdigit():
        await message.answer("Использование: <code>/setbal @user сумма</code>")
        return
    target = find_user_by_identifier(args[1])
    if not target:
        await message.answer("❌ Не найден.")
        return
    new_bal = int(args[2])
    with get_db() as conn:
        with conn.cursor() as cursor:
            cursor.execute("UPDATE users SET balance = %s WHERE user_id = %s", (new_bal, target["user_id"]))
            conn.commit()
    log_admin_action(message.from_user.id, f"setbal {target['user_id']} {new_bal}")
    await message.answer(f"✅ Баланс {get_mention(target['user_id'], target['first_name'])}: <b>{format_balance(new_bal)}</b>")


@router.message(Command("take"))
async def cmd_take(message: Message):
    if not is_moder_or_above(message.from_user.id):
        return
    args = message.text.split()
    if len(args) < 3 or not args[2].isdigit():
        await message.answer("Использование: <code>/take @user сумма</code>")
        return
    target = find_user_by_identifier(args[1])
    if not target:
        await message.answer("❌ Не найден.")
        return
    amount = int(args[2])
    update_balance(target["user_id"], -amount)
    log_admin_action(message.from_user.id, f"take {target['user_id']} {amount}")
    await message.answer(f"✅ Списано {format_balance(amount)} у {get_mention(target['user_id'], target['first_name'])}")


@router.message(Command("resetbal"))
async def cmd_resetbal(message: Message):
    if not is_moder_or_above(message.from_user.id):
        return
    args = message.text.split()
    if len(args) < 2:
        await message.answer("Использование: <code>/resetbal @user</code>")
        return
    target = find_user_by_identifier(args[1])
    if not target:
        await message.answer("❌ Не найден.")
        return
    with get_db() as conn:
        with conn.cursor() as cursor:
            cursor.execute("UPDATE users SET balance = 4000 WHERE user_id = %s", (target["user_id"],))
            conn.commit()
    log_admin_action(message.from_user.id, f"resetbal {target['user_id']}")
    await message.answer(f"✅ Баланс {get_mention(target['user_id'], target['first_name'])} → 4000")


@router.message(Command("history"))
async def cmd_history(message: Message):
    if not is_moder_or_above(message.from_user.id):
        return
    args = message.text.split()
    if len(args) < 2:
        await message.answer("Использование: <code>/history @user</code>")
        return
    target = find_user_by_identifier(args[1])
    if not target:
        await message.answer("❌ Не найден.")
        return
    with get_db() as conn:
        with conn.cursor() as cursor:
            cursor.execute("SELECT bet_amount, target, win_amount, timestamp FROM roulette_log WHERE user_id=%s ORDER BY id DESC LIMIT 10",
                           (target["user_id"],))
            rows = cursor.fetchall()
    if not rows:
        await message.answer("История пуста.")
        return
    text = f"📜 10 игр {get_mention(target['user_id'], target['first_name'])}:\n\n"
    for bet, tgt, win, ts in rows:
        text += f"🕒 {ts[:19] if ts else '-'} | {bet} на {tgt} | +{win}\n"
    await message.answer(text)


@router.message(Command("addpromo"))
async def cmd_addpromo(message: Message):
    if not is_admin_or_above(message.from_user.id):
        return
    args = message.text.split()
    if len(args) < 4 or not args[2].isdigit() or not args[3].isdigit():
        await message.answer("Использование: <code>/addpromo код сумма uses</code>")
        return
    code, amount, uses = args[1].strip(), int(args[2]), int(args[3])
    with get_db() as conn:
        with conn.cursor() as cursor:
            cursor.execute("INSERT INTO promocodes (code, amount, uses) VALUES (%s,%s,%s) ON CONFLICT (code) DO UPDATE SET amount=EXCLUDED.amount, uses=EXCLUDED.uses",
                           (code, amount, uses))
            conn.commit()
    log_admin_action(message.from_user.id, f"addpromo {code} {amount} {uses}")
    await message.answer(f"✅ Промокод <code>{code}</code>: {format_balance(amount)} × {uses}")


@router.message(Command("delpromo"))
async def cmd_delpromo(message: Message):
    if not is_admin_or_above(message.from_user.id):
        return
    args = message.text.split()
    if len(args) < 2:
        await message.answer("Использование: <code>/delpromo код</code>")
        return
    delete_promo(args[1].strip())
    log_admin_action(message.from_user.id, f"delpromo {args[1]}")
    await message.answer(f"✅ Промокод <code>{args[1]}</code> удалён.")


@router.message(Command("makeadmin"))
async def cmd_makeadmin(message: Message):
    if not is_admin_or_above(message.from_user.id):
        return
    args = message.text.split()
    if len(args) < 2:
        await message.answer("Использование: <code>/makeadmin @user</code>")
        return
    target = find_user_by_identifier(args[1])
    if not target:
        await message.answer("❌ Не найден.")
        return
    with get_db() as conn:
        with conn.cursor() as cursor:
            cursor.execute("INSERT INTO admins (user_id, rank) VALUES (%s, 'moder') ON CONFLICT (user_id) DO UPDATE SET rank='moder'",
                           (target["user_id"],))
            conn.commit()
    log_admin_action(message.from_user.id, f"makeadmin {target['user_id']}")
    await message.answer(f"⭐ {get_mention(target['user_id'], target['first_name'])} — модер.")


@router.message(Command("unadmin"))
async def cmd_unadmin(message: Message):
    if not is_admin_or_above(message.from_user.id):
        return
    args = message.text.split()
    if len(args) < 2:
        await message.answer("Использование: <code>/unadmin @user</code>")
        return
    target = find_user_by_identifier(args[1])
    if not target:
        await message.answer("❌ Не найден.")
        return
    if target["user_id"] == ADMIN_ID:
        await message.answer("❌ Нельзя снять владельца.")
        return
    with get_db() as conn:
        with conn.cursor() as cursor:
            cursor.execute("DELETE FROM admins WHERE user_id = %s", (target["user_id"],))
            conn.commit()
    log_admin_action(message.from_user.id, f"unadmin {target['user_id']}")
    await message.answer(f"✅ {get_mention(target['user_id'], target['first_name'])} разжалован.")


@router.message(Command("add_admin"))
async def cmd_add_admin(message: Message):
    if not is_head_or_above(message.from_user.id):
        return
    args = message.text.split()
    if len(args) < 2:
        await message.answer("Использование: <code>/add_admin @user [moder/admin/head/spadmin]</code>")
        return
    target = find_user_by_identifier(args[1])
    if not target:
        await message.answer("❌ Не найден.")
        return
    allowed = ("moder", "admin", "head", "spadmin")
    rank = args[2] if len(args) > 2 and args[2] in allowed else "moder"
    if rank == "spadmin" and not is_owner(message.from_user.id):
        await message.answer("❌ Только владелец может назначать spadmin.")
        return
    with get_db() as conn:
        with conn.cursor() as cursor:
            cursor.execute("INSERT INTO admins (user_id, rank) VALUES (%s,%s) ON CONFLICT (user_id) DO UPDATE SET rank = EXCLUDED.rank",
                           (target["user_id"], rank))
            conn.commit()
    log_admin_action(message.from_user.id, f"add_admin {target['user_id']} {rank}")
    await message.answer(f"✅ {get_mention(target['user_id'], target['first_name'])} → {get_rank_emoji(rank)}")


@router.message(Command("remove_admin"))
async def cmd_remove_admin(message: Message):
    if not is_head_or_above(message.from_user.id):
        return
    args = message.text.split()
    if len(args) < 2:
        await message.answer("Использование: <code>/remove_admin @user</code>")
        return
    target = find_user_by_identifier(args[1])
    if not target:
        await message.answer("❌ Не найден.")
        return
    if target["user_id"] == ADMIN_ID:
        await message.answer("❌ Нельзя снять владельца.")
        return
    with get_db() as conn:
        with conn.cursor() as cursor:
            cursor.execute("DELETE FROM admins WHERE user_id = %s", (target["user_id"],))
            conn.commit()
    log_admin_action(message.from_user.id, f"remove_admin {target['user_id']}")
    await message.answer(f"✅ {get_mention(target['user_id'], target['first_name'])} снят.")


@router.message(Command("broadcast"))
async def cmd_broadcast(message: Message):
    if not is_head_or_above(message.from_user.id):
        return
    text = message.text.replace("/broadcast", "").strip()
    if not text:
        await message.answer("Использование: <code>/broadcast текст</code>")
        return
    with get_db() as conn:
        with conn.cursor() as cursor:
            cursor.execute("SELECT user_id FROM users")
            users = [r[0] for r in cursor.fetchall()]
    status = await message.answer(f"📢 Рассылка ({len(users)})...")
    ok = fail = 0
    for u_id in users:
        try:
            await bot.send_message(int(u_id), f"📢 <b>Объявление</b>\n\n{text}")
            ok += 1
            await asyncio.sleep(0.05)
        except:
            fail += 1
    await status.edit_text(f"✅ Готово!\n📤 {ok} | ❌ {fail}")


@router.message(Command("setbonus"))
async def cmd_setbonus(message: Message):
    if not is_head_or_above(message.from_user.id):
        return
    args = message.text.split()
    if len(args) < 2 or not args[1].isdigit():
        await message.answer(f"🎁 Текущий: {format_balance(int(get_setting('bonus_amount', '3000')))}")
        return
    set_setting("bonus_amount", args[1])
    log_admin_action(message.from_user.id, f"setbonus {args[1]}")
    await message.answer(f"✅ Бонус: {format_balance(int(args[1]))}")


@router.message(Command("setcooldown"))
async def cmd_setcooldown(message: Message):
    if not is_head_or_above(message.from_user.id):
        return
    args = message.text.split()
    if len(args) < 2 or not args[1].isdigit():
        await message.answer("Использование: <code>/setcooldown 8</code>")
        return
    set_setting("bonus_cooldown", args[1])
    log_admin_action(message.from_user.id, f"setcooldown {args[1]}")
    await message.answer(f"✅ Кулдаун: {args[1]} ч.")


@router.message(Command("freeze"))
async def cmd_freeze(message: Message):
    if not is_head_or_above(message.from_user.id):
        return
    target = None
    if message.reply_to_message and message.reply_to_message.from_user:
        target = get_user(message.reply_to_message.from_user.id)
    else:
        args = message.text.split()
        if len(args) < 2:
            await message.answer("Использование: <code>/freeze @user</code>")
            return
        target = find_user_by_identifier(args[1])
    if not target:
        await message.answer("❌ Не найден.")
        return
    if target["user_id"] == ADMIN_ID:
        await message.answer("❌ Нельзя.")
        return
    ban_until = datetime.now() + timedelta(hours=24)
    with get_db() as conn:
        with conn.cursor() as cursor:
            cursor.execute("UPDATE users SET is_banned = TRUE, ban_until = %s WHERE user_id = %s",
                           (ban_until, target["user_id"]))
            conn.commit()
    log_admin_action(message.from_user.id, f"freeze {target['user_id']}")
    await message.answer(f"🥶 {get_mention(target['user_id'], target['first_name'])} заморожен на 24ч.")


@router.message(Command("unfreeze"))
async def cmd_unfreeze(message: Message):
    if not is_head_or_above(message.from_user.id):
        return
    target = None
    if message.reply_to_message and message.reply_to_message.from_user:
        target = get_user(message.reply_to_message.from_user.id)
    else:
        args = message.text.split()
        if len(args) < 2:
            await message.answer("Использование: <code>/unfreeze @user</code>")
            return
        target = find_user_by_identifier(args[1])
    if not target:
        await message.answer("❌ Не найден.")
        return
    with get_db() as conn:
        with conn.cursor() as cursor:
            cursor.execute("UPDATE users SET is_banned = FALSE, ban_until = NULL WHERE user_id = %s", (target["user_id"],))
            conn.commit()
    log_admin_action(message.from_user.id, f"unfreeze {target['user_id']}")
    await message.answer(f"✅ {get_mention(target['user_id'], target['first_name'])} разморожен.")


@router.message(Command("list_admins"))
async def cmd_list_admins(message: Message):
    if not is_moder_or_above(message.from_user.id):
        return
    with get_db() as conn:
        with conn.cursor() as cursor:
            cursor.execute("SELECT user_id, rank FROM admins ORDER BY CASE rank WHEN 'owner' THEN 0 WHEN 'head' THEN 1 WHEN 'spadmin' THEN 2 WHEN 'admin' THEN 3 WHEN 'moder' THEN 4 END")
            rows = cursor.fetchall()
    text = "🛡 <b>Администраторы:</b>\n\n"
    for uid, rank in rows:
        u = get_user(uid)
        text += f"{get_rank_emoji(rank)} {get_mention(uid, u['first_name'])}\n"
    await message.answer(text)


# ============================================================
# 💎 СЕКРЕТНЫЕ КОМАНДЫ (owner / head / spadmin)
# ============================================================
def _can_secret(uid, cmd):
    return is_head_or_above(uid) or has_secret_power(uid, cmd)


@router.message(Command("zero"))
async def cmd_zero(message: Message):
    if not _can_secret(message.from_user.id, "zero"):
        return
    args = message.text.split()
    if len(args) < 2:
        await message.answer("Использование: <code>/zero @user</code>")
        return
    target = find_user_by_identifier(args[1])
    if not target:
        await message.answer("❌ Не найден.")
        return
    with get_db() as conn:
        with conn.cursor() as cursor:
            cursor.execute("UPDATE users SET balance=0 WHERE user_id=%s", (target["user_id"],))
            conn.commit()
    log_admin_action(message.from_user.id, f"zero {target['user_id']}")
    await message.answer(f"✅ Баланс {get_mention(target['user_id'], target['first_name'])} → 0")


@router.message(Command("double"))
async def cmd_double(message: Message):
    if not _can_secret(message.from_user.id, "double"):
        return
    args = message.text.split()
    if len(args) < 2:
        await message.answer("Использование: <code>/double @user</code>")
        return
    target = find_user_by_identifier(args[1])
    if not target:
        await message.answer("❌ Не найден.")
        return
    new_bal = target["balance"] * 2
    with get_db() as conn:
        with conn.cursor() as cursor:
            cursor.execute("UPDATE users SET balance=%s WHERE user_id=%s", (new_bal, target["user_id"]))
            conn.commit()
    log_admin_action(message.from_user.id, f"double {target['user_id']} -> {new_bal}")
    await message.answer(f"✅ Баланс удвоен: {format_balance(new_bal)}")


@router.message(Command("randomize"))
async def cmd_randomize(message: Message):
    if not _can_secret(message.from_user.id, "randomize"):
        return
    args = message.text.split()
    if len(args) < 4:
        await message.answer("Использование: <code>/randomize @user мин макс</code>")
        return
    target = find_user_by_identifier(args[1])
    if not target:
        await message.answer("❌ Не найден.")
        return
    try:
        lo, hi = int(args[2]), int(args[3])
        bal = random.randint(lo, hi)
        with get_db() as conn:
            with conn.cursor() as cursor:
                cursor.execute("UPDATE users SET balance=%s WHERE user_id=%s", (bal, target["user_id"]))
                conn.commit()
        log_admin_action(message.from_user.id, f"randomize {target['user_id']} {lo}-{hi} -> {bal}")
        await message.answer(f"✅ Случайный баланс: {format_balance(bal)}")
    except ValueError:
        await message.answer("❌ Числа.")


@router.message(Command("transfer"))
async def cmd_transfer_admin(message: Message):
    if not _can_secret(message.from_user.id, "transfer"):
        return
    args = message.text.split()
    if len(args) < 3:
        await message.answer("Использование: <code>/transfer сумма @user</code> или <code>/transfer @user сумма</code>")
        return
    amount, target = None, None
    if args[1].replace("-", "").isdigit():
        amount = int(args[1])
        target = find_user_by_identifier(args[2])
    else:
        target = find_user_by_identifier(args[1])
        if len(args) > 2 and args[2].replace("-", "").isdigit():
            amount = int(args[2])
    if not target:
        await message.answer("❌ Не найден.")
        return
    if amount is None:
        await message.answer("❌ Число.")
        return
    update_balance(target["user_id"], amount)
    log_admin_action(message.from_user.id, f"transfer {target['user_id']} {amount}")
    await message.answer(f"✅ {format_balance(amount)} → {get_mention(target['user_id'], target['first_name'])}")


@router.message(Command("nick"))
async def cmd_nick(message: Message):
    if not _can_secret(message.from_user.id, "nick"):
        return
    args = message.text.split()
    if len(args) < 3:
        await message.answer("Использование: <code>/nick @user ник</code>")
        return
    target = find_user_by_identifier(args[1])
    if not target:
        await message.answer("❌ Не найден.")
        return
    new_nick = " ".join(args[2:])[:32]
    with get_db() as conn:
        with conn.cursor() as cursor:
            cursor.execute("UPDATE users SET custom_nick=%s WHERE user_id=%s", (new_nick, target["user_id"]))
            conn.commit()
    log_admin_action(message.from_user.id, f"nick {target['user_id']} {new_nick}")
    await message.answer(f"✅ Ник: 🏷️ {new_nick}")


@router.message(Command("curse"))
async def cmd_curse(message: Message):
    if not is_owner(message.from_user.id) and not has_secret_power(message.from_user.id, "curse"):
        return
    args = message.text.split()
    if len(args) < 2:
        await message.answer("Использование: <code>/curse @user</code>")
        return
    target = find_user_by_identifier(args[1])
    if not target:
        await message.answer("❌ Не найден.")
        return
    set_setting(f"cursed_{target['user_id']}", "1")
    await message.answer(f"😈 Проклятие наложено на {get_mention(target['user_id'], target['first_name'])}")


@router.message(Command("bless"))
async def cmd_bless(message: Message):
    if not is_owner(message.from_user.id) and not has_secret_power(message.from_user.id, "bless"):
        return
    args = message.text.split()
    if len(args) < 2:
        await message.answer("Использование: <code>/bless @user</code>")
        return
    target = find_user_by_identifier(args[1])
    if not target:
        await message.answer("❌ Не найден.")
        return
    with get_db() as conn:
        with conn.cursor() as cursor:
            cursor.execute("DELETE FROM settings WHERE key=%s", (f"cursed_{target['user_id']}",))
            conn.commit()
    await message.answer("✨ Проклятие снято.")


@router.message(Command("lottery"))
async def cmd_lottery(message: Message):
    if not _can_secret(message.from_user.id, "lottery"):
        return
    args = message.text.split()
    if len(args) < 2 or not args[1].isdigit():
        await message.answer("Использование: <code>/lottery сумма</code>")
        return
    prize = int(args[1])
    with get_db() as conn:
        with conn.cursor() as cursor:
            cursor.execute("SELECT user_id FROM users ORDER BY RANDOM() LIMIT 1")
            row = cursor.fetchone()
    if row:
        update_balance(row[0], prize)
        u = get_user(row[0])
        await message.answer(f"🎉 {get_mention(row[0], u['first_name'])} выиграл {format_balance(prize)}!")


@router.message(Command("setname"))
async def cmd_setname(message: Message):
    if not _can_secret(message.from_user.id, "setname"):
        return
    args = message.text.split()
    if len(args) < 3:
        await message.answer("Использование: <code>/setname @user имя</code>")
        return
    target = find_user_by_identifier(args[1])
    if not target:
        await message.answer("❌ Не найден.")
        return
    new_name = " ".join(args[2:])[:64]
    with get_db() as conn:
        with conn.cursor() as cursor:
            cursor.execute("UPDATE users SET first_name=%s WHERE user_id=%s", (new_name, target["user_id"]))
            conn.commit()
    await message.answer(f"✅ Имя: {new_name}")


@router.message(Command("globalbonus"))
async def cmd_globalbonus(message: Message):
    if not _can_secret(message.from_user.id, "globalbonus"):
        return
    args = message.text.split()
    if len(args) < 2 or not args[1].isdigit():
        await message.answer("Использование: <code>/globalbonus сумма</code>")
        return
    amt = int(args[1])
    with get_db() as conn:
        with conn.cursor() as cursor:
            cursor.execute("UPDATE users SET balance = balance + %s", (amt,))
            conn.commit()
    log_admin_action(message.from_user.id, f"globalbonus {amt}")
    await message.answer(f"✅ Всем +{format_balance(amt)}")


@router.message(Command("setallbal"))
async def cmd_setallbal(message: Message):
    if not is_owner(message.from_user.id):
        return
    args = message.text.split()
    if len(args) < 2 or not args[1].isdigit():
        await message.answer("Использование: <code>/setallbal сумма</code>")
        return
    nb = int(args[1])
    with get_db() as conn:
        with conn.cursor() as cursor:
            cursor.execute("UPDATE users SET balance = %s", (nb,))
            conn.commit()
    log_admin_action(message.from_user.id, f"setallbal {nb}")
    await message.answer(f"✅ Всем баланс = {format_balance(nb)}")


@router.message(Command("resetallbal"))
async def cmd_resetallbal(message: Message):
    if not is_owner(message.from_user.id):
        return
    with get_db() as conn:
        with conn.cursor() as cursor:
            cursor.execute("UPDATE users SET balance = 4000")
            conn.commit()
    log_admin_action(message.from_user.id, "resetallbal")
    await message.answer("✅ Всем баланс → 4000")


@router.message(Command("clearlog"))
async def cmd_clearlog(message: Message):
    if not _can_secret(message.from_user.id, "clearlog"):
        return
    with get_db() as conn:
        with conn.cursor() as cursor:
            cursor.execute("TRUNCATE TABLE admin_log")
            conn.commit()
    await message.answer("✅ Лог очищен.")


@router.message(Command("wipe"))
async def cmd_wipe(message: Message):
    if not _can_secret(message.from_user.id, "wipe"):
        return
    chat_roulette_bets.clear()
    chat_last_bet_time.clear()
    await message.answer("✅ Ставки очищены.")


@router.message(Command("checkpoint"))
async def cmd_checkpoint(message: Message):
    if not _can_secret(message.from_user.id, "checkpoint"):
        return
    args = message.text.split()
    if len(args) < 2:
        await message.answer("Использование: <code>/checkpoint @user</code>")
        return
    target = find_user_by_identifier(args[1])
    if not target:
        await message.answer("❌ Не найден.")
        return
    with get_db() as conn:
        with conn.cursor() as cursor:
            cursor.execute("INSERT INTO balance_checkpoints (user_id, balance, checkpoint_time) VALUES (%s,%s,%s)",
                           (target["user_id"], target["balance"], datetime.now().isoformat()))
            conn.commit()
    await message.answer(f"✅ Чекпоинт: {format_balance(target['balance'])}")


@router.message(Command("restore_checkpoint"))
async def cmd_restore_checkpoint(message: Message):
    if not _can_secret(message.from_user.id, "restore_checkpoint"):
        return
    args = message.text.split()
    if len(args) < 2:
        await message.answer("Использование: <code>/restore_checkpoint @user</code>")
        return
    target = find_user_by_identifier(args[1])
    if not target:
        await message.answer("❌ Не найден.")
        return
    with get_db() as conn:
        with conn.cursor() as cursor:
            cursor.execute("SELECT balance FROM balance_checkpoints WHERE user_id=%s ORDER BY checkpoint_time DESC LIMIT 1",
                           (target["user_id"],))
            row = cursor.fetchone()
            if row:
                cursor.execute("UPDATE users SET balance=%s WHERE user_id=%s", (row[0], target["user_id"]))
                conn.commit()
                await message.answer(f"✅ Баланс восстановлен: {format_balance(row[0])}")
            else:
                await message.answer("❌ Чекпоинт не найден.")


@router.message(Command("adminlog"))
async def cmd_adminlog(message: Message):
    if not _can_secret(message.from_user.id, "adminlog"):
        return
    args = message.text.split()
    limit = 10
    if len(args) > 1 and args[1].isdigit():
        limit = min(int(args[1]), 50)
    with get_db() as conn:
        with conn.cursor() as cursor:
            cursor.execute("SELECT admin_id, action, target_id, amount, timestamp FROM admin_log ORDER BY id DESC LIMIT %s", (limit,))
            rows = cursor.fetchall()
    if not rows:
        await message.answer("📜 Лог пуст.")
        return
    text = "📜 <b>Лог админов:</b>\n\n"
    for admin_id, action, target_id, amount, ts in rows:
        u = get_user(admin_id)
        line = f"🕒 {ts[:16] if ts else '-'} | {u['first_name']}: {action}"
        if target_id:
            line += f" → {target_id}"
        if amount:
            line += f" | {format_balance(amount)}"
        text += line + "\n"
    await message.answer(text)


@router.message(Command("snos"))
async def cmd_snos(message: Message):
    if not is_head_or_above(message.from_user.id):
        return
    args = message.text.split()
    if len(args) < 2:
        await message.answer("Использование: <code>/snos @user</code>")
        return
    target = find_user_by_identifier(args[1])
    if not target:
        await message.answer("❌ Не найден.")
        return
    if target["user_id"] == ADMIN_ID:
        await message.answer("❌ Нельзя снести владельца.")
        return
    rank = get_rank(target["user_id"])
    if rank == "user":
        await message.answer("❌ Пользователь не админ.")
        return
    with get_db() as conn:
        with conn.cursor() as cursor:
            cursor.execute("""INSERT INTO snos_log (user_id, previous_rank, previous_balance, timestamp)
                VALUES (%s, %s, %s, %s)
                ON CONFLICT (user_id) DO UPDATE SET
                    previous_rank = EXCLUDED.previous_rank,
                    previous_balance = EXCLUDED.previous_balance,
                    timestamp = EXCLUDED.timestamp""",
                (target["user_id"], rank, target["balance"], datetime.now().isoformat()))
            cursor.execute("DELETE FROM admins WHERE user_id = %s", (target["user_id"],))
            cursor.execute("UPDATE users SET balance = 0 WHERE user_id = %s", (target["user_id"],))
            conn.commit()
    log_admin_action(message.from_user.id, f"snos {target['user_id']}")
    await message.answer(f"💥 {get_mention(target['user_id'], target['first_name'])} снесён! Ранг и баланс обнулены.")


@router.message(Command("unsnos"))
async def cmd_unsnos(message: Message):
    if not is_head_or_above(message.from_user.id):
        return
    args = message.text.split()
    if len(args) < 2:
        await message.answer("Использование: <code>/unsnos @user</code>")
        return
    target = find_user_by_identifier(args[1])
    if not target:
        await message.answer("❌ Не найден.")
        return
    with get_db() as conn:
        with conn.cursor() as cursor:
            cursor.execute("SELECT previous_rank, previous_balance FROM snos_log WHERE user_id = %s", (target["user_id"],))
            row = cursor.fetchone()
            if not row:
                await message.answer("❌ Запись не найдена.")
                return
            prev_rank, prev_bal = row
            cursor.execute("INSERT INTO admins (user_id, rank) VALUES (%s, %s) ON CONFLICT (user_id) DO UPDATE SET rank = EXCLUDED.rank",
                           (target["user_id"], prev_rank))
            cursor.execute("UPDATE users SET balance = %s WHERE user_id = %s", (prev_bal, target["user_id"]))
            cursor.execute("DELETE FROM snos_log WHERE user_id = %s", (target["user_id"],))
            conn.commit()
    await message.answer(f"✅ {get_mention(target['user_id'], target['first_name'])} восстановлен: {get_rank_emoji(prev_rank)}, {format_balance(prev_bal)}")


@router.message(Command("givepower"))
async def cmd_givepower(message: Message):
    if not is_owner(message.from_user.id):
        return
    args = message.text.split()
    if len(args) < 3:
        await message.answer("Использование: <code>/givepower @user команда</code>")
        return
    target = find_user_by_identifier(args[1])
    if not target:
        await message.answer("❌ Не найден.")
        return
    cmd = args[2].lower()
    with get_db() as conn:
        with conn.cursor() as cursor:
            cursor.execute("INSERT INTO secret_powers (user_id, command_name) VALUES (%s,%s) ON CONFLICT DO NOTHING",
                           (target["user_id"], cmd))
            conn.commit()
    await message.answer(f"✅ {get_mention(target['user_id'], target['first_name'])} → /{cmd}")


@router.message(Command("takepower"))
async def cmd_takepower(message: Message):
    if not is_owner(message.from_user.id):
        return
    args = message.text.split()
    if len(args) < 3:
        await message.answer("Использование: <code>/takepower @user команда</code>")
        return
    target = find_user_by_identifier(args[1])
    if not target:
        await message.answer("❌ Не найден.")
        return
    with get_db() as conn:
        with conn.cursor() as cursor:
            cursor.execute("DELETE FROM secret_powers WHERE user_id=%s AND command_name=%s",
                           (target["user_id"], args[2].lower()))
            conn.commit()
    await message.answer(f"❌ Доступ /{args[2]} отозван.")


@router.message(Command("listpowers"))
async def cmd_listpowers(message: Message):
    if not _can_secret(message.from_user.id, "listpowers"):
        return
    args = message.text.split()
    uid = message.from_user.id
    name = message.from_user.first_name or "Игрок"
    if len(args) >= 2:
        target = find_user_by_identifier(args[1])
        if not target:
            await message.answer("❌ Не найден.")
            return
        uid, name = target["user_id"], target["first_name"]
    with get_db() as conn:
        with conn.cursor() as cursor:
            cursor.execute("SELECT command_name FROM secret_powers WHERE user_id=%s", (uid,))
            cmds = [r[0] for r in cursor.fetchall()]
    await message.answer(f"🔑 {get_mention(uid, name)}: {', '.join(cmds) if cmds else 'нет'}")


@router.message(Command("mypowers"))
async def cmd_mypowers(message: Message):
    uid = message.from_user.id
    if uid == ADMIN_ID:
        await message.answer("🔑 У вас все секретные команды (owner).")
        return
    with get_db() as conn:
        with conn.cursor() as cursor:
            cursor.execute("SELECT command_name FROM secret_powers WHERE user_id=%s", (uid,))
            cmds = [r[0] for r in cursor.fetchall()]
    await message.answer(f"🔑 Ваши секретки: {', '.join(cmds) if cmds else 'нет'}")


@router.message(Command("deleteuser"))
async def cmd_deleteuser(message: Message):
    if not is_owner(message.from_user.id):
        return
    args = message.text.split()
    if len(args) < 2:
        await message.answer("Использование: <code>/deleteuser @user</code>")
        return
    target = find_user_by_identifier(args[1])
    if not target:
        await message.answer("❌ Не найден.")
        return
    if target["user_id"] == ADMIN_ID:
        await message.answer("❌ Нельзя удалить владельца.")
        return
    uid = target["user_id"]
    with get_db() as conn:
        with conn.cursor() as cursor:
            for tbl in ["users", "admins", "roulette_log", "user_last_bets", "daily_tasks",
                        "referrals", "used_promocodes", "balance_checkpoints", "secret_powers",
                        "miners", "mining_stats", "cases_inventory", "deposits"]:
                try:
                    cursor.execute(f"DELETE FROM {tbl} WHERE user_id=%s", (uid,))
                except:
                    conn.rollback()
            conn.commit()
    await message.answer(f"✅ Пользователь {uid} полностью удалён.")


@router.message(Command("sql_execute"))
async def cmd_sql_execute(message: Message):
    if not is_owner(message.from_user.id):
        return
    query = message.text.replace("/sql_execute", "").strip()
    if not query:
        await message.answer("Использование: <code>/sql_execute SELECT ...</code>")
        return
    try:
        with get_db() as conn:
            with conn.cursor() as cursor:
                cursor.execute(query)
                if cursor.description:
                    rows = cursor.fetchall()[:15]
                    text = "\n".join(str(r) for r in rows)
                else:
                    conn.commit()
                    text = "OK."
        await message.answer(f"<code>{text}</code>")
    except Exception as e:
        await message.answer(f"❌ {e}")


@router.message(Command("backup"))
async def cmd_backup(message: Message):
    if not is_owner(message.from_user.id):
        return
    with get_db() as conn:
        with conn.cursor() as cursor:
            cursor.execute("SELECT * FROM users")
            users = [dict(zip([c[0] for c in cursor.description], row)) for row in cursor.fetchall()]
    data = json.dumps(users, default=str, ensure_ascii=False, indent=2)
    await message.answer_document(BufferedInputFile(data.encode(), "backup.json"))


@router.message(Command("emergency_stop"))
async def cmd_emergency_stop(message: Message):
    if not is_owner(message.from_user.id):
        return
    await message.answer("🛑 Бот остановлен.")
    await bot.session.close()
    exit(0)


# ============================================================
# 🌐 ВЕБ-СЕРВЕР ДЛЯ RENDER
# ============================================================
async def handle_ping(request):
    return web.Response(text="MINT bot is running!")


async def web_server():
    app = web.Application()
    app.router.add_get("/", handle_ping)
    runner = web.AppRunner(app)
    await runner.setup()
    port = int(os.getenv("PORT", 10000))
    site = web.TCPSite(runner, "0.0.0.0", port)
    await site.start()
    logging.info(f"Web server started on port {port}")


# ============================================================
# 🔧 MIDDLEWARE + ЗАПУСК
# ============================================================
@dp.message.middleware()
async def check_bans(handler, event, data):
    if event.from_user:
        user = get_user(event.from_user.id)
        if user.get('is_banned'):
            if user.get('ban_until'):
                if user['ban_until'] > datetime.now():
                    return
                else:
                    with get_db() as conn:
                        with conn.cursor() as cursor:
                            cursor.execute("UPDATE users SET is_banned = FALSE, ban_until = NULL WHERE user_id = %s",
                                           (event.from_user.id,))
                            conn.commit()
            else:
                return
    return await handler(event, data)


@dp.message.middleware()
async def remove_bot_mention(handler, event, data):
    if event.text and event.text.startswith("/"):
        parts = event.text.split()
        if parts:
            cmd_part = parts[0]
            if "@" in cmd_part:
                cmd, _, mention = cmd_part.partition("@")
                if BOT_USERNAME and mention.lower() == BOT_USERNAME.lower():
                    new_text = cmd
                    if len(parts) > 1:
                        new_text += " " + " ".join(parts[1:])
                    event.text = new_text
    return await handler(event, data)


async def main():
    global BOT_USERNAME
    bot_info = await bot.get_me()
    if bot_info.username:
        BOT_USERNAME = bot_info.username
    logging.info(f"Bot: @{BOT_USERNAME} | Owner: {ADMIN_ID}")

    dp.include_router(router)
    await bot.delete_webhook(drop_pending_updates=True)
    await web_server()
    logging.info(f"🚀 {BOT_NAME} Bot запущен!")
    await dp.start_polling(bot)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        logging.info("Бот остановлен.")

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
                ("clan_id", "INT"),
		("robberies_won", "INT DEFAULT 0")
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
            cursor.execute("CREATE TABLE IF NOT EXISTS rob_inventory (user_id BIGINT, item_type TEXT, count INT DEFAULT 0, PRIMARY KEY (user_id, item_type))")
            cursor.execute("CREATE TABLE IF NOT EXISTS rob_cooldown (user_id BIGINT, target TEXT, last_time TIMESTAMP, PRIMARY KEY (user_id, target))")
            cursor.execute("CREATE TABLE IF NOT EXISTS deposit_topups (id SERIAL PRIMARY KEY, user_id BIGINT, amount BIGINT, date TEXT, timestamp TIMESTAMP)")
            cursor.execute("""CREATE TABLE IF NOT EXISTS transfers_log (
                id SERIAL PRIMARY KEY,
                sender_id BIGINT,
                receiver_id BIGINT,
                amount BIGINT,
                comment TEXT,
                timestamp TEXT
            )""")
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
    with get_db() as conn:
        with conn.cursor() as cursor:
            cursor.execute(
                "SELECT user_id, first_name, last_name, username, balance, last_bonus, games_played, games_won, "
                "referrer_id, referred_count, referal_bonus_claimed, last_daily, custom_nick, last_robbery, "
                "is_banned, ban_until, vip_until, btc, clan_id FROM users WHERE user_id = %s",
                (user_id,))
            row = cursor.fetchone()
            if not row:
                cursor.execute(
                    "INSERT INTO users (user_id, first_name, last_name, username, balance, last_bonus, "
                    "games_played, games_won, referal_bonus_claimed) VALUES (%s,%s,%s,%s,4000,NULL,0,0,0)",
                    (user_id, first_name or None, last_name or None, username or None))
                conn.commit()
                return {"user_id": user_id, "first_name": first_name or None,
                        "last_name": last_name or None, "username": username or None,
                        "balance": 4000, "last_bonus": None, "games_played": 0, "games_won": 0,
                        "referrer_id": None, "referred_count": 0, "referal_bonus_claimed": 0,
                        "last_daily": None, "custom_nick": None, "last_robbery": None,
                        "is_banned": False, "ban_until": None, "vip_until": None,
                        "btc": 0, "clan_id": None}

            db_fname, db_uname = row[1], row[3]
            need_update = False
            if first_name and (not db_fname or db_fname == "Игрок"):
                db_fname = first_name
                need_update = True
            if username and not db_uname:
                db_uname = username
                need_update = True
            if need_update:
                cursor.execute(
                    "UPDATE users SET first_name = %s, username = %s WHERE user_id = %s",
                    (db_fname, db_uname, user_id))
                conn.commit()

            return {"user_id": row[0], "first_name": db_fname, "last_name": row[2],
                    "username": db_uname, "balance": row[4], "last_bonus": row[5],
                    "games_played": row[6], "games_won": row[7], "referrer_id": row[8],
                    "referred_count": row[9], "referal_bonus_claimed": row[10],
                    "last_daily": row[11], "custom_nick": row[12], "last_robbery": row[13],
                    "is_banned": row[14], "ban_until": row[15], "vip_until": row[16],
                    "btc": float(row[17]) if row[17] else 0, "clan_id": row[18]}

def get_user_title(user: dict) -> str:
    games = user.get('games_played', 0)
    bal = user.get('balance', 0)
    robs = user.get('robberies_won', 0)
    btc_val = user.get('btc', 0)
    
    titles = []
    if bal >= 10_000_000_000: titles.append("👑 Император")
    elif bal >= 1_000_000_000: titles.append("💎 Олигарх")
    elif bal >= 100_000_000: titles.append("💰 Богач")
        
    if games >= 500: titles.append("🎰 Казино-маньяк")
    elif games >= 100: titles.append("🎲 Азартный")
    elif games >= 10: titles.append("🥉 Новичок")
        
    if robs >= 50: titles.append("🏴‍☠️ Легенда улиц")
    elif robs >= 10: titles.append("🩸 Кровопийца")

    if btc_val >= 1.0: titles.append("⛏ Магнат")
    if user.get('vip_until') and user['vip_until'] > datetime.now(): titles.append("⚜️ VIP")
        
    if not titles: return "👤 Игрок"
    return " | ".join(titles)

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


RANK_LEVELS = {"user": 0, "moder": 1, "spadmin": 2, "admin": 3, "head": 4, "owner": 5}

RANK_NAMES = {
    "user": "👤 Игрок",
    "moder": "⭐ Модератор",
    "spadmin": "✨ Спец. админ",
    "admin": "🔰 Админ",
    "head": "👑 Гл. админ",
    "owner": "💎 Владелец",
}


def get_rank(user_id: int) -> str:
    if user_id == ADMIN_ID:
        return "owner"
    with get_db() as conn:
        with conn.cursor() as cursor:
            cursor.execute("SELECT rank FROM admins WHERE user_id = %s", (user_id,))
            row = cursor.fetchone()
            return row[0] if row else "user"


def get_rank_level(user_id: int) -> int:
    return RANK_LEVELS.get(get_rank(user_id), 0)


def has_rank(user_id: int, min_rank: str) -> bool:
    return get_rank_level(user_id) >= RANK_LEVELS.get(min_rank, 999)


def get_rank_emoji(rank: str) -> str:
    return RANK_NAMES.get(rank, "")


def is_admin(user_id: int) -> bool:
    return has_rank(user_id, "moder")


def is_moder_or_above(user_id: int) -> bool:
    return has_rank(user_id, "moder")


def is_spadmin_or_above(user_id: int) -> bool:
    return has_rank(user_id, "spadmin")


def is_admin_or_above(user_id: int) -> bool:
    return has_rank(user_id, "admin")


def is_head_or_above(user_id: int) -> bool:
    return has_rank(user_id, "head")


def is_owner(user_id: int) -> bool:
    return user_id == ADMIN_ID or get_rank(user_id) == "owner"


def can_manage_admin(actor_id: int, target_id: int) -> bool:
    if actor_id == target_id:
        return False
    if get_rank(target_id) == "owner":
        return False
    return get_rank_level(actor_id) > get_rank_level(target_id)

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

def log_transfer(sender_id: int, receiver_id: int, amount: int, comment: str = ""):
    with get_db() as conn:
        with conn.cursor() as cursor:
            cursor.execute(
                "INSERT INTO transfers_log (sender_id, receiver_id, amount, comment, timestamp) "
                "VALUES (%s,%s,%s,%s,%s)",
                (sender_id, receiver_id, amount, comment, datetime.now().isoformat()))
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

def get_display_name(user_id: int, first_name=None, username=None, custom_nick=None) -> str:
    if custom_nick and str(custom_nick).strip():
        return str(custom_nick).strip()
    if first_name and str(first_name).strip() and first_name != "Игрок":
        return str(first_name).strip()
    if username and str(username).strip():
        return f"@{username}"
    return f"id{user_id}"


def get_user_link(user_id: int, first_name=None, username=None,
                  custom_nick=None, show_id: bool = False) -> str:
    name = get_display_name(user_id, first_name, username, custom_nick)
    safe = name.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    link = f'<a href="tg://user?id={user_id}">{safe}</a>'
    if show_id and not name.startswith("id"):
        link += f" <code>({user_id})</code>"
    return link

def clean_first_name(first_name: str, user_id: int) -> str:
    return get_display_name(user_id, first_name)


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


# ============================================================
# 📦 КЕЙСЫ V2
# ============================================================
CASE_TYPES = {
    "деревянный": {
        "emoji": "📦", "price": 10_000_000,
        "color": "🟫",
        "drops": [
            ("nothing", 0, 0.50),
            ("money", 4_000_000, 0.25),
            ("money", 12_000_000, 0.13),
            ("money", 40_000_000, 0.055),
            ("money", 100_000_000, 0.013),
            ("money", 800_000_000, 0.002),
        ]
    },
    "железный": {
        "emoji": "🗃", "price": 100_000_000,
        "color": "⬜",
        "drops": [
            ("nothing", 0, 0.50),
            ("money", 25_000_000, 0.25),
            ("money", 60_000_000, 0.13),
            ("money", 150_000_000, 0.08),
            ("money", 400_000_000, 0.035),
            ("vip", 6, 0.005),
        ]
    },
    "золотой": {
        "emoji": "🎁", "price": 1_000_000_000,
        "color": "🟨",
        "drops": [
            ("nothing", 0, 0.45),
            ("money", 80_000_000, 0.25),
            ("money", 250_000_000, 0.15),
            ("money", 700_000_000, 0.10),
            ("money", 1_800_000_000, 0.045),
            ("vip", 24, 0.005),
        ]
    },
    "алмазный": {
        "emoji": "💎", "price": 10_000_000_000,
        "color": "🔷",
        "drops": [
            ("nothing", 0, 0.40),
            ("money", 400_000_000, 0.25),
            ("money", 1_500_000_000, 0.18),
            ("money", 4_000_000_000, 0.11),
            ("money", 9_000_000_000, 0.055),
            ("vip", 72, 0.005),
        ]
    },
    "легендарный": {
        "emoji": "🏆", "price": 100_000_000_000,
        "color": "🟪",
        "drops": [
            ("nothing", 0, 0.35),
            ("money", 1_500_000_000, 0.25),
            ("money", 8_000_000_000, 0.20),
            ("money", 40_000_000_000, 0.12),
            ("money", 90_000_000_000, 0.075),
            ("vip", 168, 0.005),
        ]
    },
    "мифический": {
        "emoji": "🔮", "price": 1_000_000_000_000,
        "color": "🟥",
        "drops": [
            ("nothing", 0, 0.30),
            ("money", 8_000_000_000, 0.25),
            ("money", 80_000_000_000, 0.20),
            ("money", 400_000_000_000, 0.12),
            ("money", 900_000_000_000, 0.125),
            ("vip", 336, 0.005),
        ]
    },
    "космический": {
        "emoji": "🌌", "price": 100_000_000_000_000,
        "color": "🟦",
        "drops": [
            ("nothing", 0, 0.25),
            ("money", 80_000_000_000, 0.25),
            ("money", 800_000_000_000, 0.22),
            ("money", 8_000_000_000_000, 0.13),
            ("money", 80_000_000_000_000, 0.145),
            ("vip", 720, 0.005),
        ]
    },
}


def roll_case(case_type: str):
    """Возвращает (тип, значение) по шансам."""
    data = CASE_TYPES.get(case_type)
    if not data:
        return ("nothing", 0)
    r = random.random()
    cumulative = 0.0
    for dtype, dval, chance in data["drops"]:
        cumulative += chance
        if r < cumulative:
            return (dtype, dval)
    return data["drops"][-1][:2]


def case_win_text(case_type: str, results: list) -> str:
    """Форматирует текст результата открытия."""
    emoji = CASE_TYPES[case_type]["emoji"]
    total_money = 0
    total_vip = 0
    details = []

    for dtype, dval in results:
        if dtype == "money":
            total_money += dval
            details.append(f"💰 +{format_balance(dval)}")
        elif dtype == "vip":
            total_vip += dval
            details.append(f"👑 VIP ×{dval}ч")
        else:
            details.append("❌ пусто")

    summary = f"{emoji} <b>Открыто: {len(results)}× {case_type}</b>\n━━━━━━━━━━━━━━━━━━━\n"
    for i, d in enumerate(details, 1):
        summary += f"{i}. {d}\n"
    summary += "━━━━━━━━━━━━━━━━━━━\n"
    if total_money > 0:
        summary += f"💰 Всего денег: <b>{format_balance(total_money)}</b>\n"
    if total_vip > 0:
        summary += f"👑 Всего VIP: <b>{total_vip} ч</b>\n"
    return summary, total_money, total_vip


def apply_case_rewards(uid: int, total_money: int, total_vip: int):
    if total_money > 0:
        update_balance(uid, total_money)
    if total_vip > 0:
        user = get_user(uid)
        cu = user.get('vip_until')
        new_until = (cu + timedelta(hours=total_vip)) if (cu and cu > datetime.now()) else (datetime.now() + timedelta(hours=total_vip))
        with get_db() as conn:
            with conn.cursor() as cursor:
                cursor.execute("UPDATE users SET vip_until = %s WHERE user_id = %s", (new_until, uid))
                conn.commit()


def case_main_menu():
    text = "📦 <b>КЕЙСЫ</b>\n━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
    for name, data in CASE_TYPES.items():
        drops = data["drops"]
        max_money = max((d[1] for d in drops if d[0] == "money"), default=0)
        max_vip = max((d[1] for d in drops if d[0] == "vip"), default=0)
        max_str = f"до {format_balance(max_money)}" if max_money > 0 else ""
        if max_vip > 0:
            max_str += f" | VIP ×{max_vip}ч"
        text += f"{data['emoji']} <b>{name}</b> — {format_balance(data['price'])}\n"
        text += f"   <i>{max_str}</i>\n\n"
    text += "👇 <i>Нажми на кейс чтобы купить или открыть:</i>"
    rows = []
    for name, data in CASE_TYPES.items():
        rows.append([InlineKeyboardButton(
            text=f"{data['emoji']} {name} — {format_balance(data['price'])}",
            callback_data=f"case:info:{name}"
        )])
    return text, InlineKeyboardMarkup(inline_keyboard=rows)


@router.message(F.text.lower().in_(["кейс", "кейсы", "case"]))
async def case_main(message: Message):
    text, kb = case_main_menu()
    await message.answer(text, reply_markup=kb)


@router.message(F.text == "👛 Сейф")
async def cmd_safe_btn(message: Message):
    await safe_cmd(message)


@router.callback_query(F.data.startswith("case:info:"))
async def case_info(cb: CallbackQuery):
    name = cb.data.split(":")[2]
    if name not in CASE_TYPES:
        await cb.answer("Неизвестный кейс", show_alert=True)
        return
    data = CASE_TYPES[name]
    uid = cb.from_user.id

    with get_db() as conn:
        with conn.cursor() as cursor:
            cursor.execute("SELECT count FROM cases_inventory WHERE user_id = %s AND case_type = %s", (uid, name))
            row = cursor.fetchone()
            have = row[0] if row else 0

    text = f"{data['emoji']} <b>{name.upper()}</b>\n━━━━━━━━━━━━━━━━━━━\n"
    text += f"💰 Цена: <b>{format_balance(data['price'])}</b>\n"
    text += f"📦 У тебя: <b>{have}</b>\n\n"
    text += "🎲 <b>Шансы выпадения:</b>\n"
    for dtype, dval, chance in data["drops"]:
        pct = chance * 100
        if dtype == "money":
            text += f"  💰 {format_balance(dval)} — <b>{pct:.1f}%</b>\n"
        elif dtype == "vip":
            text += f"  👑 VIP ×{dval}ч — <b>{pct:.1f}%</b>\n"
        else:
            text += f"  ❌ пусто — <b>{pct:.1f}%</b>\n"

    rows = []
    if have > 0:
        rows.append([InlineKeyboardButton(text="📦 Открыть 1", callback_data=f"case:open1:{name}")])
        if have >= 5:
            rows.append([InlineKeyboardButton(text="📦×5 Открыть 5", callback_data=f"case:open5:{name}")])
        if have >= 10:
            rows.append([InlineKeyboardButton(text="📦×10 Открыть 10", callback_data=f"case:open10:{name}")])
    rows.append([InlineKeyboardButton(text=f"🛒 Купить за {format_balance(data['price'])}", callback_data=f"case:buy:{name}")])
    rows.append([InlineKeyboardButton(text="◀️ К кейсам", callback_data="case:main")])
    await cb.message.edit_text(text, reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))
    await cb.answer()


@router.callback_query(F.data == "case:main")
async def case_back(cb: CallbackQuery):
    text, kb = case_main_menu()
    await cb.message.edit_text(text, reply_markup=kb)
    await cb.answer()


@router.callback_query(F.data.startswith("case:buy:"))
async def case_buy(cb: CallbackQuery):
    name = cb.data.split(":")[2]
    if name not in CASE_TYPES:
        await cb.answer("Неизвестный кейс", show_alert=True)
        return
    price = CASE_TYPES[name]["price"]
    user = get_user(cb.from_user.id)
    if user["balance"] < price:
        await cb.answer(f"❌ Нужно {format_balance(price)}\nУ тебя: {format_balance(user['balance'])}", show_alert=True)
        return
    update_balance(cb.from_user.id, -price)
    with get_db() as conn:
        with conn.cursor() as cursor:
            cursor.execute(
                "INSERT INTO cases_inventory (user_id, case_type, count) VALUES (%s, %s, 1) "
                "ON CONFLICT (user_id, case_type) DO UPDATE SET count = cases_inventory.count + 1",
                (cb.from_user.id, name))
            conn.commit()
    await cb.answer(f"✅ Куплено: {CASE_TYPES[name]['emoji']} {name}", show_alert=True)
    await case_info(cb)


@router.callback_query(F.data.startswith("case:open"))
async def case_open(cb: CallbackQuery):
    parts = cb.data.split(":")
    action = parts[1]  # open1/open5/open10
    name = parts[2]
    if name not in CASE_TYPES:
        await cb.answer("Неизвестный кейс", show_alert=True)
        return
    count = {"open1": 1, "open5": 5, "open10": 10}.get(action, 1)
    uid = cb.from_user.id

    with get_db() as conn:
        with conn.cursor() as cursor:
            cursor.execute("SELECT count FROM cases_inventory WHERE user_id = %s AND case_type = %s", (uid, name))
            row = cursor.fetchone()
            have = row[0] if row else 0
            if have < count:
                await cb.answer(f"❌ Нужно {count}, у тебя {have}", show_alert=True)
                return
            new_count = have - count
            if new_count <= 0:
                cursor.execute("DELETE FROM cases_inventory WHERE user_id = %s AND case_type = %s", (uid, name))
            else:
                cursor.execute("UPDATE cases_inventory SET count = %s WHERE user_id = %s AND case_type = %s", (new_count, uid, name))
            conn.commit()

    # Анимация
    emoji = CASE_TYPES[name]["emoji"]
    await cb.message.edit_text(
        f"{emoji} <b>Открываем...</b>\n\n"
        f"🎰 {'🎁 🎰 📦 💎 🏆 🔮 🌌'}\n\n"
        f"<i>Удачной охоты!</i>",
        reply_markup=None
    )
    await cb.answer()
    await asyncio.sleep(1.5)

    results = [roll_case(name) for _ in range(count)]
    summary, total_money, total_vip = case_win_text(name, results)
    apply_case_rewards(uid, total_money, total_vip)

    await cb.message.edit_text(
        summary,
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="📦 Ещё раз", callback_data=f"case:info:{name}")],
            [InlineKeyboardButton(text="📦 Все кейсы", callback_data="case:main")]
        ])
    )


# Старая команда через текст (совместимость)
@router.message(F.text.lower().startswith(("кейс ", "case ")))
async def case_cmd(message: Message):
    args = message.text.split()
    if len(args) < 3:
        text, kb = case_main_menu()
        await message.answer(text, reply_markup=kb)
        return
    sub = args[1].lower()
    case_type = args[2].lower()
    if case_type not in CASE_TYPES:
        await message.answer("❌ Неизвестный тип. Доступно: " + ", ".join(CASE_TYPES.keys()))
        return
    if sub == "купить":
        price = CASE_TYPES[case_type]["price"]
        user = get_user(message.from_user.id)
        if user["balance"] < price:
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
        await message.answer(f"📦 Кейс '<b>{case_type}</b>' куплен!")
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
        results = [roll_case(case_type)]
        summary, total_money, total_vip = case_win_text(case_type, results)
        apply_case_rewards(message.from_user.id, total_money, total_vip)
        await message.answer(summary)
    else:
        await message.answer("Использование: <code>кейс купить/открыть [тип]</code>")


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
        f"👨‍💻 Связь с разработчиком: @d0xse\n\n"
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
    await message.answer("💝 Для поддержки проекта пиши @d0xse.")


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
@router.message(F.text.lower().in_(["б", "баланс", "👤 профиль"])) # <--- Добавили, чтобы работали кнопки
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
        
    # 👇 ВОТ ЭТА СТРОЧКА ДОБАВЛЯЕТ ТИТУЛ
    text += f"🎖 Статус: <b>{get_user_title(user)}</b>\n"
    
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
@router.message(F.text == "🏆 Топ")
async def cmd_top(message: Message):
    limit = 10
    args = message.text.split()
    if len(args) > 1 and args[1].isdigit():
        limit = int(args[1])

    with get_db() as conn:
        with conn.cursor() as cursor:
            cursor.execute("SELECT user_id FROM users ORDER BY balance DESC LIMIT %s", (limit,))
            rows = cursor.fetchall()

    if not rows:
        return await message.answer("🏆 Топ пуст.")

    medals = {1: "🥇", 2: "🥈", 3: "🥉"}
    text = f"🏆 <b>Топ {len(rows)} игроков:</b>\n\n"

    for i, r in enumerate(rows, 1):
        uid = r[0]
        u = get_user(uid)
        link = get_user_link(uid, u['first_name'], u.get('username'), u.get('custom_nick'))
        text += f"{medals.get(i, f'{i}.')} {link} — <b>{format_balance(u['balance'])}</b>\n"
        text += f"   <i>{get_user_title(u)}</i>\n" # <--- ТИТУЛ

    await message.answer(text)

@router.message(Command("resolve_names"))
async def cmd_resolve_names(message: Message):
    if message.from_user.id != ADMIN_ID:
        await message.answer("❌ Только владелец.")
        return

    await message.answer("🔍 Ищу юзеров без имён...")

    with get_db() as conn:
        with conn.cursor() as cursor:
            cursor.execute(
                "SELECT user_id FROM users WHERE first_name IS NULL OR first_name = 'Игрок' OR first_name = ''"
            )
            uids = [r[0] for r in cursor.fetchall()]

    if not uids:
        await message.answer("✅ Все имена уже в порядке.")
        return

    updated = 0
    failed = 0
    for uid in uids:
        try:
            chat = await bot.get_chat(uid)
            new_fname = getattr(chat, "first_name", "") or ""
            new_uname = getattr(chat, "username", "") or ""
            if new_fname or new_uname:
                with get_db() as conn:
                    with conn.cursor() as cursor:
                        cursor.execute(
                            "UPDATE users SET first_name = %s, username = %s WHERE user_id = %s",
                            (new_fname or None, new_uname or None, uid))
                        conn.commit()
                updated += 1
            else:
                failed += 1
        except Exception as e:
            logging.warning(f"get_chat failed for {uid}: {e}")
            failed += 1
        await asyncio.sleep(0.05)  # антирейт-лимит

    await message.answer(
        f"✅ <b>Готово!</b>\n\n"
        f"👤 Обработано: <b>{len(uids)}</b>\n"
        f"✅ Обновлено: <b>{updated}</b>\n"
        f"❌ Не удалось: <b>{failed}</b>\n\n"
        f"<i>Теперь напиши /top — имена должны отобразиться.</i>"
    )


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
    log_transfer(user['user_id'], target['user_id'], amount, comment)
    msg = f"💸 {get_user_link(user['user_id'], user['first_name'], user.get('username'), user.get('custom_nick'))} перевел {get_user_link(target['user_id'], target['first_name'], target.get('username'), target.get('custom_nick'))} <b>{format_balance(amount)}</b>."
    if comment:
        msg += f"\n💬 {comment}"
    await message.answer(msg)

@router.message(Command("transfers"))
async def cmd_transfer_logs(message: Message):
    if not is_admin(message.from_user.id):
        await message.answer("❌ Нет доступа.")
        return

    args = message.text.split()
    limit = int(args[1]) if len(args) > 1 and args[1].isdigit() else 30

    with get_db() as conn:
        with conn.cursor() as cursor:
            cursor.execute("""
                SELECT t.id, t.sender_id, u1.first_name, u1.username, u1.custom_nick,
                       t.receiver_id, u2.first_name, u2.username, u2.custom_nick,
                       t.amount, t.comment, t.timestamp
                FROM transfers_log t
                LEFT JOIN users u1 ON u1.user_id = t.sender_id
                LEFT JOIN users u2 ON u2.user_id = t.receiver_id
                ORDER BY t.id DESC LIMIT %s
            """, (limit,))
            rows = cursor.fetchall()

    if not rows:
        await message.answer("📭 Логов переводов пока нет.")
        return

    text = f"💸 <b>Последние {len(rows)} переводов</b>\n━━━━━━━━━━━━━━━━━\n"
    for (tid, s_id, s_fn, s_un, s_nk,
         r_id, r_fn, r_un, r_nk,
         amount, comment, ts) in rows:
        s_link = get_user_link(s_id, s_fn, s_un, s_nk, show_id=True)
        r_link = get_user_link(r_id, r_fn, r_un, r_nk, show_id=True)
        dt = ts[:16].replace("T", " ") if ts else "—"
        text += f"<b>#{tid}</b> · {dt}\n"
        text += f"  {s_link} ➡️ {r_link}\n"
        text += f"  💰 {format_balance(amount)}"
        if comment:
            text += f" · 💬 {comment[:40]}"
        text += "\n\n"

    await message.answer(text)


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


# ============================================================
# 👑 VIP V2
# ============================================================
VIP_PACKAGES = {
    24:  {"price": 10_000_000,  "label": "24 часа",   "emoji": "🕐"},
    72:  {"price": 25_000_000,  "label": "3 дня",     "emoji": "📅"},
    168: {"price": 50_000_000,  "label": "7 дней",    "emoji": "📆"},
    336: {"price": 90_000_000,  "label": "14 дней",   "emoji": "🗓"},
    720: {"price": 150_000_000, "label": "30 дней",   "emoji": "📊"},
}

VIP_BENEFITS = [
    ("🎁", "Бонус ×2", "Ежедневный бонус удваивается"),
    ("⚡", "Приоритет в топе", "VIP-игроки показываются выше"),
    ("💎", "VIP-бейдж", "Особый статус в профиле и чате"),
    ("🎰", "Эксклюзивные кейсы", "Скоро: VIP-кейсы с лучшими шансами"),
    ("🚀", "Без кулдаунов", "Скоро: мгновенное ограбление без ожидания"),
]


def vip_menu_text(uid) -> str:
    user = get_user(uid)
    is_vip_user = False
    until_str = ""
    hours_left = 0
    if user.get('vip_until') and user['vip_until'] > datetime.now():
        is_vip_user = True
        until_str = user['vip_until'].strftime('%d.%m.%Y %H:%M')
        hours_left = int((user['vip_until'] - datetime.now()).total_seconds() // 3600)

    text = f"👑 <b>VIP-СТАТУС {BOT_NAME}</b>\n"
    text += "━━━━━━━━━━━━━━━━━━━━━━━\n\n"

    if is_vip_user:
        text += f"✅ <b>У тебя активен VIP!</b>\n"
        text += f"📅 До: <b>{until_str}</b>\n"
        text += f"⏱ Осталось: <b>{hours_left} ч</b>\n\n"
    else:
        text += "❌ <b>У тебя нет VIP</b>\n\n"

    text += "🎁 <b>Что даёт VIP:</b>\n"
    for emoji, name, desc in VIP_BENEFITS:
        text += f"{emoji} <b>{name}</b> — {desc}\n"

    text += "\n💰 <b>Пакеты:</b>"
    return text


def vip_menu_kb(uid):
    user = get_user(uid)
    is_vip_user = user.get('vip_until') and user['vip_until'] > datetime.now()
    rows = []
    for hours, pkg in VIP_PACKAGES.items():
        rows.append([InlineKeyboardButton(
            text=f"{pkg['emoji']} {pkg['label']} — {format_balance(pkg['price'])}",
            callback_data=f"vip:buy:{hours}"
        )])
    if is_vip_user:
        rows.append([InlineKeyboardButton(text="🔄 Мой VIP", callback_data="vip:status")])
    rows.append([InlineKeyboardButton(text="📖 Подробнее", callback_data="vip:info")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def vip_info_text() -> str:
    return (
        "📖 <b>ЧТО ТАКОЕ VIP</b>\n"
        "━━━━━━━━━━━━━━━━━━━━━━━\n\n"
        "<b>1️⃣ Бонус ×2</b>\n"
        "Ежедневный бонус удваивается:\n"
        f"• Обычный: 3 000 COINS\n"
        f"• VIP: 6 000 COINS\n"
        f"📈 Экономия: 6 000+ COINS в день\n\n"

        "<b>2️⃣ Вип-бейдж</b>\n"
        "👑 Рядом с твоим именем в профиле\n"
        "🏆 В топе игроков показываешься выше\n\n"

        "<b>3️⃣ Скидки и бонусы</b>\n"
        "Скоро: VIP-цены в магазинах\n"
        "Скоро: эксклюзивные кейсы\n\n"

        "<b>💡 Стоит ли брать?</b>\n"
        "Если играешь каждый день → окупается за 3-5 дней\n"
        "Если редко → лучше не бери\n\n"

        "<b>⏰ Кулдаун бонуса:</b> 8 часов\n"
        "VIP не убирает кулдаун, но удваивает награду"
    )


@router.message(Command("vip"))
async def cmd_vip(message: Message):
    await message.answer(vip_menu_text(message.from_user.id),
                          reply_markup=vip_menu_kb(message.from_user.id))


@router.message(F.text.lower() == "vip")
async def cmd_vip_text(message: Message):
    await cmd_vip(message)


@router.callback_query(F.data == "vip:status")
async def vip_status(cb: CallbackQuery):
    await cb.message.edit_text(vip_menu_text(cb.from_user.id),
                                reply_markup=vip_menu_kb(cb.from_user.id))
    await cb.answer()


@router.callback_query(F.data == "vip:info")
async def vip_info(cb: CallbackQuery):
    await cb.message.edit_text(vip_info_text(), reply_markup=InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="◀️ Назад", callback_data="vip:status")]
    ]))
    await cb.answer()


@router.callback_query(F.data.startswith("vip:buy:"))
async def vip_buy_cb(cb: CallbackQuery):
    try:
        hours = int(cb.data.split(":")[2])
    except:
        await cb.answer("Ошибка", show_alert=True)
        return
    if hours not in VIP_PACKAGES:
        await cb.answer("Неизвестный пакет", show_alert=True)
        return
    pkg = VIP_PACKAGES[hours]
    price = pkg["price"]
    user = get_user(cb.from_user.id)
    if user["balance"] < price:
        await cb.answer(
            f"❌ Нужно {format_balance(price)}\nУ тебя: {format_balance(user['balance'])}",
            show_alert=True
        )
        return

    update_balance(cb.from_user.id, -price)
    current_until = user.get('vip_until')
    new_until = (current_until + timedelta(hours=hours)) if (current_until and current_until > datetime.now()) \
        else (datetime.now() + timedelta(hours=hours))
    with get_db() as conn:
        with conn.cursor() as cursor:
            cursor.execute("UPDATE users SET vip_until = %s WHERE user_id = %s", (new_until, cb.from_user.id))
            conn.commit()
    log_admin_action(cb.from_user.id, f"vip_buy {hours}h -{price}")

    await cb.answer(f"✅ VIP на {pkg['label']} активирован!", show_alert=True)
    await cb.message.edit_text(
        f"👑 <b>VIP АКТИВИРОВАН!</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"📅 Пакет: <b>{pkg['label']}</b>\n"
        f"💰 Оплачено: {format_balance(price)}\n"
        f"⏰ Действует до: <b>{new_until.strftime('%d.%m.%Y %H:%M')}</b>\n\n"
        f"🎁 Теперь твой бонус удваивается!\n"
        f"⚡ Не забудь забрать его завтра!",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🎁 Забрать бонус", callback_data="get_bonus_lc")],
            [InlineKeyboardButton(text="🔄 Мой VIP", callback_data="vip:status")]
        ])
    )


# Старая команда /vip_buy для совместимости
@router.message(Command("vip_buy"))
async def cmd_vip_buy(message: Message):
    args = message.text.split()
    if len(args) < 2 or not args[1].isdigit():
        await message.answer("Использование: <code>/vip_buy [часы]</code>\nДоступно: 24, 72, 168, 336, 720")
        return
    hours = int(args[1])
    if hours not in VIP_PACKAGES:
        await message.answer(f"❌ Неверный срок. Доступно: {', '.join(map(str, VIP_PACKAGES.keys()))}")
        return
    # Показываем меню с подтверждением
    pkg = VIP_PACKAGES[hours]
    await message.answer(
        f"👑 <b>Покупка VIP</b>\n\n"
        f"📅 {pkg['label']}\n"
        f"💰 Цена: {format_balance(pkg['price'])}\n\n"
        f"Подтвердить?",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="✅ Купить", callback_data=f"vip:buy:{hours}")],
            [InlineKeyboardButton(text="❌ Отмена", callback_data="vip:status")]
        ])
    )

# =============================================================
# MINT Bot — Часть 2/4: Экономика
# =============================================================

# ============================================================
# КЛАНЫ V2 — проще + туториал
# ============================================================
CLAN_CREATE_PRICE = 10000000  # 10 млн
clan_states = {}  # user_id -> {"action": ...}


def _clan_main_kb():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="➕ Создать клан", callback_data="clan:create"),
         InlineKeyboardButton(text="🔍 Список кланов", callback_data="clan:list")],
        [InlineKeyboardButton(text="👤 Мой клан", callback_data="clan:mine"),
         InlineKeyboardButton(text="❓ Как это работает", callback_data="clan:tutorial")],
        [InlineKeyboardButton(text="🏆 Топ кланов", callback_data="clan:top")]
    ])


def _clan_main_text():
    return (
        f"🏰 <b>КЛАНЫ {BOT_NAME}</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━━\n"
        f"Объединяйся с друзьями, качай общую казну,\n"
        f"соревнуйся с другими кланами за первое место!\n\n"
        f"💰 <b>Создание:</b> {format_balance(CLAN_CREATE_PRICE)}\n"
        f"👥 <b>Участники:</b> неограниченно\n"
        f"🏦 <b>Казна:</b> общая, снимает владелец\n\n"
        f"<i>👇 Выбери действие:</i>"
    )


@router.message(F.text == "👥 Кланы")
async def cmd_clan_btn(message: Message):
    await message.answer(_clan_main_text(), reply_markup=_clan_main_kb())


@router.message(F.text.lower() == "клан")
async def cmd_clan_text(message: Message):
    await message.answer(_clan_main_text(), reply_markup=_clan_main_kb())


@router.callback_query(F.data == "clan:main")
async def clan_main_cb(callback: CallbackQuery):
    await callback.message.edit_text(_clan_main_text(), reply_markup=_clan_main_kb())
    await callback.answer()


@router.callback_query(F.data == "clan:tutorial")
async def clan_tutorial(callback: CallbackQuery):
    await callback.message.edit_text(
        f"📚 <b>КАК РАБОТАЮТ КЛАНЫ</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━━\n\n"
        f"<b>1️⃣ Создай клан</b>\n"
        f"💰 Нужно {format_balance(CLAN_CREATE_PRICE)}\n"
        f"📝 Название: 3-20 символов, англ. буквы\n"
        f"🎯 Ты автоматически становишься владельцем\n\n"
        f"<b>2️⃣ Приглашай друзей</b>\n"
        f"Друзья: <code>клан вступить Название</code>\n"
        f"Или через кнопку <b>🔍 Список кланов</b>\n\n"
        f"<b>3️⃣ Пополняйте казну</b>\n"
        f"<code>клан казна 100000</code> — любой участник\n"
        f"💰 Казна хранится в клане, не в твоём балансе\n\n"
        f"<b>4️⃣ Снимай казну</b> (только владелец)\n"
        f"<code>клан снять 50000</code> — идёт в твой баланс\n\n"
        f"<b>5️⃣ Соревнуйся за топ</b>\n"
        f"🏆 Самый богатый клан — в топе\n\n"
        f"<i>Нажми «◀️ Назад» чтобы вернуться</i>",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="◀️ Назад", callback_data="clan:main")]
        ])
    )
    await callback.answer()


@router.callback_query(F.data == "clan:create")
async def clan_create_cb(callback: CallbackQuery):
    if get_user_clan(callback.from_user.id):
        await callback.answer("Ты уже в клане!", show_alert=True)
        return
    clan_states[callback.from_user.id] = {"action": "clan_create_name"}
    await callback.message.edit_text(
        f"➕ <b>СОЗДАНИЕ КЛАНА</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━━\n"
        f"💰 Цена: <b>{format_balance(CLAN_CREATE_PRICE)}</b>\n\n"
        f"📝 Отправь <b>название клана</b> в чат:\n"
        f"• 3-20 символов\n"
        f"• Английские буквы/цифры/_\n"
        f"• Без пробелов\n\n"
        f"<i>Пример: <code>Dragons</code></i>\n\n"
        f"<i>Для отмены: /cancel</i>",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="❌ Отмена", callback_data="clan:main")]
        ])
    )
    await callback.answer()


@router.callback_query(F.data == "clan:list")
async def clan_list_cb(callback: CallbackQuery):
    with get_db() as conn:
        with conn.cursor() as cursor:
            cursor.execute("""
                SELECT c.id, c.name, c.balance,
                       (SELECT COUNT(*) FROM clan_members WHERE clan_id = c.id) as members
                FROM clans c ORDER BY c.balance DESC LIMIT 15
            """)
            rows = cursor.fetchall()
    if not rows:
        await callback.message.edit_text(
            f"🔍 <b>Список кланов</b>\n\n"
            f"Кланов пока нет. Стань первым!\n\n"
            f"💰 Создание: {format_balance(CLAN_CREATE_PRICE)}",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="➕ Создать клан", callback_data="clan:create")],
                [InlineKeyboardButton(text="◀️ Назад", callback_data="clan:main")]
            ])
        )
        await callback.answer()
        return
    text = f"🔍 <b>СПИСОК КЛАНОВ</b>\n━━━━━━━━━━━━━━━━━━━━━\n\n"
    buttons = []
    in_clan = get_user_clan(callback.from_user.id)
    for cid, name, bal, members in rows:
        text += f"🏰 <b>{name}</b> — 👥 {members} | 💰 {format_balance(bal)}\n"
        if not in_clan:
            buttons.append([InlineKeyboardButton(
                text=f"✅ Вступить в {name}",
                callback_data=f"clan:join:{cid}"
            )])
    buttons.append([InlineKeyboardButton(text="◀️ Назад", callback_data="clan:main")])
    await callback.message.edit_text(text, reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons))
    await callback.answer()


@router.callback_query(F.data.startswith("clan:join:"))
async def clan_join_cb(callback: CallbackQuery):
    cid = int(callback.data.split(":")[2])
    if get_user_clan(callback.from_user.id):
        await callback.answer("Ты уже в клане!", show_alert=True)
        return
    clan = get_clan_by_name_or_id(cid)
    if not clan:
        await callback.answer("Клан не найден", show_alert=True)
        return
    with get_db() as conn:
        with conn.cursor() as cursor:
            cursor.execute("INSERT INTO clan_members (clan_id, user_id) VALUES (%s, %s)",
                           (clan['id'], callback.from_user.id))
            cursor.execute("UPDATE users SET clan_id = %s WHERE user_id = %s",
                           (clan['id'], callback.from_user.id))
            conn.commit()
    await callback.message.edit_text(
        f"✅ <b>Ты вступил в клан!</b>\n\n"
        f"🏰 Клан: <b>{clan['name']}</b>\n"
        f"👥 Участников: +1\n"
        f"💰 Казна: {format_balance(clan['balance'])}\n\n"
        f"📋 <i>Полезно знать:</i>\n"
        f"• <code>клан инфо</code> — инфо о клане\n"
        f"• <code>клан казна 100000</code> — пополнить\n"
        f"• <code>клан выйти</code> — покинуть",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="👤 Мой клан", callback_data="clan:mine")],
            [InlineKeyboardButton(text="◀️ Назад", callback_data="clan:main")]
        ])
    )
    await callback.answer("Вступил!")


@router.callback_query(F.data == "clan:mine")
async def clan_mine_cb(callback: CallbackQuery):
    clan = get_user_clan(callback.from_user.id)
    if not clan:
        await callback.message.edit_text(
            f"👤 <b>Мой клан</b>\n\n"
            f"Ты не в клане.\n\n"
            f"Вступи в существующий через <b>🔍 Список кланов</b>\n"
            f"или создай свой за {format_balance(CLAN_CREATE_PRICE)}",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="➕ Создать клан", callback_data="clan:create")],
                [InlineKeyboardButton(text="🔍 Список кланов", callback_data="clan:list")],
                [InlineKeyboardButton(text="◀️ Назад", callback_data="clan:main")]
            ])
        )
        await callback.answer()
        return
    with get_db() as conn:
        with conn.cursor() as cursor:
            cursor.execute("SELECT COUNT(*) FROM clan_members WHERE clan_id = %s", (clan['id'],))
            cnt = cursor.fetchone()[0]
    owner = get_user(clan['owner_id'])
    is_owner_user = clan['owner_id'] == callback.from_user.id
    owner_tag = " (ты)" if is_owner_user else ""
    await callback.message.edit_text(
        f"👤 <b>Мой клан</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━━\n"
        f"🏰 <b>{clan['name']}</b> (ID: {clan['id']})\n"
        f"👑 Владелец: {get_mention(clan['owner_id'], owner['first_name'])}{owner_tag}\n"
        f"👥 Участников: <b>{cnt}</b>\n"
        f"💰 Казна: <b>{format_balance(clan['balance'])}</b>\n\n"
        f"<b>Команды:</b>\n"
        f"• <code>клан казна 100000</code> — пополнить\n"
        f"{'• <code>клан снять 50000</code> — снять в баланс' + chr(10) if is_owner_user else ''}"
        f"• <code>клан выйти</code> — покинуть",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🔄 Обновить", callback_data="clan:mine")],
            [InlineKeyboardButton(text="◀️ Назад", callback_data="clan:main")]
        ])
    )
    await callback.answer()


@router.callback_query(F.data == "clan:top")
async def clan_top_cb(callback: CallbackQuery):
    with get_db() as conn:
        with conn.cursor() as cursor:
            cursor.execute("SELECT id, name, balance FROM clans ORDER BY balance DESC LIMIT 10")
            rows = cursor.fetchall()
    if not rows:
        await callback.message.edit_text(
            f"🏆 Топ кланов пуст.\n\nСтань первым!",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="➕ Создать клан", callback_data="clan:create")],
                [InlineKeyboardButton(text="◀️ Назад", callback_data="clan:main")]
            ])
        )
        await callback.answer()
        return
    medals = {1: "🥇", 2: "🥈", 3: "🥉"}
    text = f"🏆 <b>ТОП-10 КЛАНОВ</b>\n━━━━━━━━━━━━━━━━━━━━━\n\n"
    for i, (cid, name, bal) in enumerate(rows, 1):
        text += f"{medals.get(i, f'{i}.')} <b>{name}</b> — {format_balance(bal)}\n"
    await callback.message.edit_text(
        text,
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="◀️ Назад", callback_data="clan:main")]
        ])
    )
    await callback.answer()


# ---------- ТЕКСТОВЫЕ КОМАНДЫ (для совместимости) ----------
@router.message(F.text.lower().startswith(("clan ", "клан ")))
async def clan_cmd(message: Message):
    args = message.text.split()
    if len(args) < 2:
        await message.answer(_clan_main_text(), reply_markup=_clan_main_kb())
        return
    sub = args[1].lower()

    if sub == "создать":
        if get_user_clan(message.from_user.id):
            await message.answer("❌ Ты уже в клане.")
            return
        clan_states[message.from_user.id] = {"action": "clan_create_name"}
        await message.answer(
            f"➕ <b>Создание клана</b>\n\n"
            f"💰 Цена: {format_balance(CLAN_CREATE_PRICE)}\n\n"
            f"Отправь название клана в чат:\n"
            f"• 3-20 символов, англ. буквы/цифры\n"
            f"• Пример: <code>Dragons</code>\n\n"
            f"<i>Для отмены: /cancel</i>"
        )
        return

    elif sub == "вступить":
        if len(args) < 3:
            await message.answer(
                f"Укажи название или ID клана:\n"
                f"<code>клан вступить Dragons</code>\n\n"
                f"Или посмотри список: кнопка <b>👥 Кланы</b>"
            )
            return
        if get_user_clan(message.from_user.id):
            await message.answer("❌ Ты уже в клане.")
            return
        clan = get_clan_by_name_or_id(args[2])
        if not clan:
            await message.answer("❌ Клан не найден. Проверь название.")
            return
        with get_db() as conn:
            with conn.cursor() as cursor:
                cursor.execute("INSERT INTO clan_members (clan_id, user_id) VALUES (%s, %s)",
                               (clan['id'], message.from_user.id))
                cursor.execute("UPDATE users SET clan_id = %s WHERE user_id = %s",
                               (clan['id'], message.from_user.id))
                conn.commit()
        await message.answer(f"✅ Ты вступил в клан '<b>{clan['name']}</b>'!")

    elif sub == "выйти":
        clan = get_user_clan(message.from_user.id)
        if not clan:
            await message.answer("Ты не в клане.")
            return
        with get_db() as conn:
            with conn.cursor() as cursor:
                cursor.execute("DELETE FROM clan_members WHERE clan_id = %s AND user_id = %s",
                               (clan['id'], message.from_user.id))
                cursor.execute("UPDATE users SET clan_id = NULL WHERE user_id = %s", (message.from_user.id,))
                conn.commit()
        await message.answer("✅ Ты вышел из клана.")

    elif sub == "инфо":
        clan = get_user_clan(message.from_user.id)
        if not clan:
            await message.answer("Ты не в клане. Открой <b>👥 Кланы</b> чтобы вступить.")
            return
        with get_db() as conn:
            with conn.cursor() as cursor:
                cursor.execute("SELECT COUNT(*) FROM clan_members WHERE clan_id = %s", (clan['id'],))
                cnt = cursor.fetchone()[0]
        owner = get_user(clan['owner_id'])
        await message.answer(
            f"🏰 <b>{clan['name']}</b>\n"
            f"👑 Владелец: {get_mention(clan['owner_id'], owner['first_name'])}\n"
            f"👥 Участников: {cnt}\n"
            f"💰 Казна: {format_balance(clan['balance'])}"
        )

    elif sub == "казна":
        if len(args) < 3 or not args[2].isdigit():
            await message.answer("Укажи сумму: <code>клан казна 100000</code>")
            return
        amount = int(args[2])
        clan = get_user_clan(message.from_user.id)
        if not clan:
            await message.answer("Ты не в клане.")
            return
        user = get_user(message.from_user.id)
        if user['balance'] < amount:
            await message.answer(f"❌ Недостаточно. У тебя: {format_balance(user['balance'])}")
            return
        update_balance(message.from_user.id, -amount)
        with get_db() as conn:
            with conn.cursor() as cursor:
                cursor.execute("UPDATE clans SET balance = balance + %s WHERE id = %s", (amount, clan['id']))
                conn.commit()
        await message.answer(f"💰 Казна пополнена на {format_balance(amount)}.")

    elif sub == "снять":
        if len(args) < 3 or not args[2].isdigit():
            await message.answer("Укажи сумму: <code>клан снять 50000</code>")
            return
        amount = int(args[2])
        clan = get_user_clan(message.from_user.id)
        if not clan or clan['owner_id'] != message.from_user.id:
            await message.answer("❌ Только владелец может снимать.")
            return
        if clan['balance'] < amount:
            await message.answer(f"❌ В казне только {format_balance(clan['balance'])}.")
            return
        with get_db() as conn:
            with conn.cursor() as cursor:
                cursor.execute("UPDATE clans SET balance = balance - %s WHERE id = %s", (amount, clan['id']))
                cursor.execute("UPDATE users SET balance = balance + %s WHERE user_id = %s",
                               (amount, message.from_user.id))
                conn.commit()
        await message.answer(f"✅ Снято {format_balance(amount)} в твой баланс.")

    elif sub == "топ":
        with get_db() as conn:
            with conn.cursor() as cursor:
                cursor.execute("SELECT id, name, balance FROM clans ORDER BY balance DESC LIMIT 10")
                rows = cursor.fetchall()
        if not rows:
            await message.answer("Кланов пока нет.")
            return
        medals = {1: "🥇", 2: "🥈", 3: "🥉"}
        text = "🏆 <b>Топ кланов:</b>\n\n"
        for i, r in enumerate(rows, 1):
            text += f"{medals.get(i, f'{i}.')} <b>{r[1]}</b> — {format_balance(r[2])}\n"
        await message.answer(text)

    else:
        await message.answer(_clan_main_text(), reply_markup=_clan_main_kb())


# ---------- STATE ДЛЯ СОЗДАНИЯ КЛАНА ----------
@router.message(F.text, lambda m: m.from_user.id in clan_states)
async def clan_state_handler(message: Message):
    uid = message.from_user.id
    state = clan_states.get(uid, {})
    action = state.get("action")
    text = (message.text or "").strip()

    if text == "/cancel":
        clan_states.pop(uid, None)
        await message.answer("❌ Отменено.", reply_markup=_clan_main_kb())
        return

    if action == "clan_create_name":
        if not re.match("^[a-zA-Z0-9_]{3,20}$", text):
            await message.answer("❌ Название: 3-20 символов, англ. буквы/цифры/_.\nПопробуй снова или /cancel")
            return
        if get_user_clan(uid):
            clan_states.pop(uid, None)
            await message.answer("❌ Ты уже в клане.")
            return
        user = get_user(uid)
        if user['balance'] < CLAN_CREATE_PRICE:
            clan_states.pop(uid, None)
            await message.answer(
                f"❌ Недостаточно средств.\n"
                f"Нужно: {format_balance(CLAN_CREATE_PRICE)}\n"
                f"У тебя: {format_balance(user['balance'])}"
            )
            return
        with get_db() as conn:
            with conn.cursor() as cursor:
                cursor.execute("SELECT id FROM clans WHERE lower(name) = lower(%s)", (text,))
                if cursor.fetchone():
                    await message.answer("❌ Клан с таким названием уже есть. Попробуй другое или /cancel")
                    return
                update_balance(uid, -CLAN_CREATE_PRICE)
                cursor.execute("INSERT INTO clans (name, owner_id, balance) VALUES (%s, %s, 0) RETURNING id",
                               (text, uid))
                clan_id = cursor.fetchone()[0]
                cursor.execute("INSERT INTO clan_members (clan_id, user_id) VALUES (%s, %s)", (clan_id, uid))
                cursor.execute("UPDATE users SET clan_id = %s WHERE user_id = %s", (clan_id, uid))
                conn.commit()
        clan_states.pop(uid, None)
        await message.answer(
            f"🏰 <b>КЛАН СОЗДАН!</b>\n"
            f"━━━━━━━━━━━━━━━━━━━━━\n"
            f"📛 Название: <b>{text}</b>\n"
            f"🆔 ID: {clan_id}\n"
            f"👑 Ты — владелец\n\n"
            f"<b>Что делать дальше:</b>\n"
            f"• Приглашай друзей: <code>клан вступить {text}</code>\n"
            f"• Пополняй казну: <code>клан казна 100000</code>\n"
            f"• Смотри инфо: кнопка <b>👤 Мой клан</b>",
            reply_markup=_clan_main_kb()
        )

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
# 🏦 БАНК / ДЕПОЗИТ V2
# ============================================================
DEPOSIT_MIN = 5000
DEPOSIT_MAX_TOPUPS_PER_DAY = 10
DEPOSIT_INTEREST_PER_HOUR = 0.0005


def deposit_interest_v2(start_time: datetime, amount: int) -> int:
    hours = max(0, (datetime.now() - start_time).total_seconds() // 3600)
    return int(amount * (DEPOSIT_INTEREST_PER_HOUR * hours))


def get_today_topups(user_id: int) -> int:
    today = datetime.now().strftime("%Y-%m-%d")
    with get_db() as conn:
        with conn.cursor() as cursor:
            cursor.execute("SELECT COUNT(*) FROM deposit_topups WHERE user_id = %s AND date = %s",
                           (user_id, today))
            return cursor.fetchone()[0]


def get_deposit(user_id: int):
    with get_db() as conn:
        with conn.cursor() as cursor:
            cursor.execute("SELECT amount, start_time FROM deposits WHERE user_id = %s", (user_id,))
            row = cursor.fetchone()
            if row:
                return {"amount": row[0], "start_time": row[1]}
    return None


def bank_main_text(user_id: int) -> str:
    dep = get_deposit(user_id)
    user = get_user(user_id)
    today_topups = get_today_topups(user_id)
    topups_left = DEPOSIT_MAX_TOPUPS_PER_DAY - today_topups

    text = f"🏦 <b>БАНК {BOT_NAME}</b>\n"
    text += f"━━━━━━━━━━━━━━━━━━━━━\n"
    text += f"💰 Баланс: <b>{format_balance(user['balance'])}</b>\n\n"

    if not dep:
        text += (
            f"❌ <b>Депозит не открыт</b>\n\n"
            f"📈 <b>Условия:</b>\n"
            f"• Минимум: {format_balance(DEPOSIT_MIN)}\n"
            f"• Процент: 0.05% в час (~1.2% в день)\n"
            f"• Комиссия снятия: 2%\n\n"
            f"<i>Открой депозит и получай пассивный доход!</i>"
        )
    else:
        interest = deposit_interest_v2(dep["start_time"], dep["amount"])
        total = dep["amount"] + interest
        hours_elapsed = int((datetime.now() - dep["start_time"]).total_seconds() // 3600)
        text += (
            f"✅ <b>Депозит активен</b>\n"
            f"━━━━━━━━━━━━━━━━━━━━━\n"
            f"💰 Вложено: <b>{format_balance(dep['amount'])}</b>\n"
            f"📈 Начислено: <b>{format_balance(interest)}</b>\n"
            f"💎 Итого: <b>{format_balance(total)}</b>\n"
            f"⏱ В банке: <b>{hours_elapsed} ч</b>\n"
            f"━━━━━━━━━━━━━━━━━━━━━\n"
            f"📊 Пополнений сегодня: <b>{today_topups}/{DEPOSIT_MAX_TOPUPS_PER_DAY}</b>\n"
            f"   Осталось: <b>{topups_left}</b>\n\n"
            f"💸 При снятии комиссия 2% = {format_balance(int(total * 0.02))}"
        )
    return text


def bank_main_kb(user_id: int):
    dep = get_deposit(user_id)
    rows = []
    if not dep:
        rows.append([InlineKeyboardButton(text="➕ Открыть депозит", callback_data="bank:open")])
    else:
        rows.append([InlineKeyboardButton(text="💰 Пополнить", callback_data="bank:topup")])
        rows.append([InlineKeyboardButton(text="💸 Снять всё", callback_data="bank:withdraw")])
    rows.append([InlineKeyboardButton(text="📖 Как работает", callback_data="bank:guide")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


@router.message(F.text.lower() == "банк")
async def bank_info(message: Message):
    uid = message.from_user.id
    await message.answer(bank_main_text(uid), reply_markup=bank_main_kb(uid))


@router.callback_query(F.data == "bank:main")
async def bank_main_cb(callback: CallbackQuery):
    try:
        await callback.message.edit_text(bank_main_text(callback.from_user.id),
                                          reply_markup=bank_main_kb(callback.from_user.id))
    except Exception:
        pass
    await callback.answer()


@router.callback_query(F.data == "bank:guide")
async def bank_guide(callback: CallbackQuery):
    text = (
        f"📖 <b>КАК РАБОТАЕТ БАНК</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━━\n\n"
        f"<b>1️⃣ Открытие</b>\n"
        f"• Минимум {format_balance(DEPOSIT_MIN)}\n"
        f"• Создаётся один депозит\n\n"
        f"<b>2️⃣ Проценты</b>\n"
        f"• 0.05% в час (≈1.2% в день, ≈36% в месяц)\n"
        f"• Начисляются автоматически\n\n"
        f"<b>3️⃣ Пополнение</b>\n"
        f"• До {DEPOSIT_MAX_TOPUPS_PER_DAY} раз в день\n"
        f"• Любая сумма от {format_balance(DEPOSIT_MIN)}\n"
        f"• Проценты на общую сумму\n\n"
        f"<b>4️⃣ Снятие</b>\n"
        f"• Доступно в любой момент\n"
        f"• Комиссия 2% от суммы+процентов\n\n"
        f"<b>5️⃣ После снятия</b>\n"
        f"• Можно открыть новый депозит сразу же"
    )
    await callback.message.edit_text(text, reply_markup=InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="◀️ Назад", callback_data="bank:main")]
    ]))
    await callback.answer()


@router.callback_query(F.data == "bank:open")
async def bank_open_start(callback: CallbackQuery):
    admin_states[callback.from_user.id] = {"action": "bank_open_amount"}
    await callback.message.edit_text(
        f"➕ <b>Открытие депозита</b>\n\n"
        f"Минимум: <b>{format_balance(DEPOSIT_MIN)}</b>\n\n"
        f"Введи сумму в чат:\n"
        f"<i>Для отмены: /cancel</i>",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="◀️ Назад", callback_data="bank:main")]
        ])
    )
    await callback.answer()


@router.callback_query(F.data == "bank:topup")
async def bank_topup_start(callback: CallbackQuery):
    uid = callback.from_user.id
    today = get_today_topups(uid)
    if today >= DEPOSIT_MAX_TOPUPS_PER_DAY:
        await callback.answer(
            f"❌ Лимит {DEPOSIT_MAX_TOPUPS_PER_DAY}/день исчерпан.\nПопробуй завтра.",
            show_alert=True
        )
        return
    admin_states[uid] = {"action": "bank_topup_amount"}
    await callback.message.edit_text(
        f"💰 <b>Пополнение депозита</b>\n\n"
        f"Минимум: <b>{format_balance(DEPOSIT_MIN)}</b>\n"
        f"📊 Осталось пополнений сегодня: <b>{DEPOSIT_MAX_TOPUPS_PER_DAY - today}</b>\n\n"
        f"Введи сумму в чат:\n"
        f"<i>Для отмены: /cancel</i>",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="◀️ Назад", callback_data="bank:main")]
        ])
    )
    await callback.answer()


@router.callback_query(F.data == "bank:withdraw")
async def bank_withdraw_cb(callback: CallbackQuery):
    uid = callback.from_user.id
    dep = get_deposit(uid)
    if not dep:
        await callback.answer("❌ Нет депозита", show_alert=True)
        return
    interest = deposit_interest_v2(dep["start_time"], dep["amount"])
    total = dep["amount"] + interest
    commission = int(total * 0.02)
    to_balance = total - commission
    with get_db() as conn:
        with conn.cursor() as cursor:
            cursor.execute("DELETE FROM deposits WHERE user_id = %s", (uid,))
            cursor.execute("UPDATE users SET balance = balance + %s WHERE user_id = %s", (to_balance, uid))
            conn.commit()
    await callback.message.edit_text(
        f"✅ <b>Депозит закрыт</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━━\n"
        f"💰 Вложено: {format_balance(dep['amount'])}\n"
        f"📈 Проценты: {format_balance(interest)}\n"
        f"💵 Итого: {format_balance(total)}\n"
        f"💸 Комиссия (2%): {format_balance(commission)}\n"
        f"━━━━━━━━━━━━━━━━━━━━━\n"
        f"💎 Зачислено: <b>{format_balance(to_balance)}</b>",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="➕ Открыть заново", callback_data="bank:open")],
            [InlineKeyboardButton(text="🏦 В банк", callback_data="bank:main")]
        ])
    )
    await callback.answer("✅ Деньги зачислены")


@router.message(F.text.lower().startswith("депозит "))
async def deposit_open_text(message: Message):
    parts = message.text.split()
    if len(parts) < 2 or not parts[1].isdigit():
        await message.answer(f"Использование: <code>депозит [сумма]</code> (мин {format_balance(DEPOSIT_MIN)})\nИли нажми <b>банк</b>")
        return
    amount = int(parts[1])
    if amount < DEPOSIT_MIN:
        await message.answer(f"❌ Минимум {format_balance(DEPOSIT_MIN)}.")
        return
    user = get_user(message.from_user.id)
    if user["balance"] < amount:
        await message.answer("❌ Недостаточно средств.")
        return
    dep = get_deposit(message.from_user.id)
    if dep:
        await message.answer("❌ Депозит уже открыт. Используй <b>банк</b> для пополнения.")
        return
    update_balance(message.from_user.id, -amount)
    with get_db() as conn:
        with conn.cursor() as cursor:
            cursor.execute("INSERT INTO deposits (user_id, amount, start_time) VALUES (%s, %s, %s)",
                           (message.from_user.id, amount, datetime.now()))
            conn.commit()
    await message.answer(f"✅ Депозит открыт на {format_balance(amount)}.\n📈 0.05% в час.")


@router.message(F.text.lower() == "снять депозит")
async def deposit_withdraw_text(message: Message):
    uid = message.from_user.id
    dep = get_deposit(uid)
    if not dep:
        await message.answer("❌ Нет депозита.")
        return
    interest = deposit_interest_v2(dep["start_time"], dep["amount"])
    total = dep["amount"] + interest
    commission = int(total * 0.02)
    to_balance = total - commission
    with get_db() as conn:
        with conn.cursor() as cursor:
            cursor.execute("DELETE FROM deposits WHERE user_id = %s", (uid,))
            cursor.execute("UPDATE users SET balance = balance + %s WHERE user_id = %s", (to_balance, uid))
            conn.commit()
    await message.answer(
        f"✅ Депозит закрыт.\n"
        f"💰 Зачислено: <b>{format_balance(to_balance)}</b>\n"
        f"💸 Комиссия: {format_balance(commission)}"
    )

# ============================================================
# 🏴‍☠️ ОГРАБЛЕНИЕ V2
# ============================================================

ROB_TOOLS = {
    "mask":   {"name": "Маска",       "emoji": "🎭", "price": 250_000,  "bonus": 3,  "wear": 0.6, "desc": "Скрывает лицо"},
    "bag":    {"name": "Сумка",       "emoji": "💼", "price": 500_000,  "bonus": 5,  "wear": 0.6, "desc": "Больше денег"},
    "suit":   {"name": "Спец одежда", "emoji": "🥷", "price": 1_000_000, "bonus": 7,  "wear": 0.5, "desc": "Свобода движений"},
    "car":    {"name": "Машина",      "emoji": "🚗", "price": 2_000_000, "bonus": 10, "wear": 0.3, "desc": "Быстрый побег"},
    "weapon": {"name": "Оружие",      "emoji": "🔫", "price": 4_000_000, "bonus": 15, "wear": 0.3, "desc": "Отпугивает охрану"},
}

ROB_TARGETS = {
    "atm":    {"emoji": "🏧", "name": "Банкомат", "base": 60, "guard": 10,
               "min": 300_000, "max": 500_000, "cooldown": 30, "cap": 90},
    "casino": {"emoji": "🎰", "name": "Казино",   "base": 45, "guard": 40,
               "min": 500_000, "max": 1_500_000, "cooldown": 60, "cap": 85},
    "bank":   {"emoji": "🏦", "name": "Банк",     "base": 30, "guard": 70,
               "min": 1_500_000, "max": 2_000_000, "cooldown": 120, "cap": 80},
}


def rob_get_inventory(user_id: int) -> dict:
    inv = {}
    with get_db() as conn:
        with conn.cursor() as cursor:
            cursor.execute("SELECT item_type, count FROM rob_inventory WHERE user_id = %s AND count > 0", (user_id,))
            for item, cnt in cursor.fetchall():
                inv[item] = cnt
    return inv


def rob_add_item(user_id: int, item: str, count: int = 1):
    with get_db() as conn:
        with conn.cursor() as cursor:
            cursor.execute(
                "INSERT INTO rob_inventory (user_id, item_type, count) VALUES (%s, %s, %s) "
                "ON CONFLICT (user_id, item_type) DO UPDATE SET count = rob_inventory.count + EXCLUDED.count",
                (user_id, item, count))
            conn.commit()


def rob_remove_item(user_id: int, item: str, count: int = 1):
    with get_db() as conn:
        with conn.cursor() as cursor:
            cursor.execute(
                "UPDATE rob_inventory SET count = GREATEST(count - %s, 0) WHERE user_id = %s AND item_type = %s",
                (count, user_id, item))
            conn.commit()


def rob_get_cooldown(user_id: int, target: str) -> int:
    with get_db() as conn:
        with conn.cursor() as cursor:
            cursor.execute("SELECT last_time FROM rob_cooldown WHERE user_id = %s AND target = %s", (user_id, target))
            row = cursor.fetchone()
    if not row or not row[0]:
        return 0
    cooldown = ROB_TARGETS[target]["cooldown"] * 60
    elapsed = (datetime.now() - row[0]).total_seconds()
    return max(0, int(cooldown - elapsed))


def rob_set_cooldown(user_id: int, target: str):
    with get_db() as conn:
        with conn.cursor() as cursor:
            cursor.execute(
                "INSERT INTO rob_cooldown (user_id, target, last_time) VALUES (%s, %s, %s) "
                "ON CONFLICT (user_id, target) DO UPDATE SET last_time = EXCLUDED.last_time",
                (user_id, target, datetime.now()))
            conn.commit()


def rob_calc_chance(user_id: int, target: str):
    t = ROB_TARGETS[target]
    inv = rob_get_inventory(user_id)
    bonus = 0
    used = []
    for item_key, item_data in ROB_TOOLS.items():
        if inv.get(item_key, 0) > 0:
            bonus += item_data["bonus"]
            used.append(item_key)
    chance = min(t["base"] + bonus, t["cap"])
    return chance, used


def rob_main_menu_text(user_id: int):
    inv = rob_get_inventory(user_id)
    balance = get_user(user_id)["balance"]
    text = f"🏴‍☠️ <b>ОГРАБЛЕНИЯ</b>\n━━━━━━━━━━━━━━━━━━━━━━━━\n"
    text += f"💰 Баланс: <b>{format_balance(balance)}</b>\n\n"
    text += "🎒 <b>Инструменты:</b>\n"
    if inv:
        for item_key, cnt in inv.items():
            if item_key in ROB_TOOLS:
                d = ROB_TOOLS[item_key]
                text += f"  {d['emoji']} {d['name']} ×{cnt}\n"
    else:
        text += "  <i>нет</i>\n"
    text += "\n🎯 <b>Цели:</b>\n"
    for key, t in ROB_TARGETS.items():
        cd = rob_get_cooldown(user_id, key)
        chance, _ = rob_calc_chance(user_id, key)
        if cd > 0:
            m, s = cd // 60, cd % 60
            status = f"⏳ {m}м {s}с" if m else f"⏳ {s}с"
        else:
            status = "✅ Готово"
        text += f"  {t['emoji']} <b>{t['name']}</b> — {status}\n"
        text += f"     💰 {format_balance(t['min'])}–{format_balance(t['max'])} | 🎲 {chance}% | 🛡 {t['guard']}%\n"
    text += "\n<i>👇 Выбери цель:</i>"
    return text


def rob_main_menu_kb(user_id: int):
    rows = []
    for key, t in ROB_TARGETS.items():
        cd = rob_get_cooldown(user_id, key)
        label = f"{t['emoji']} {t['name']}"
        if cd > 0:
            label += " ⏳"
        rows.append([InlineKeyboardButton(text=label, callback_data=f"rob:do:{key}")])
    rows.append([InlineKeyboardButton(text="🎒 Инвентарь", callback_data="rob:inventory"),
                 InlineKeyboardButton(text="🛒 Магазин", callback_data="rob:shop")])
    rows.append([InlineKeyboardButton(text="📖 Гайд", callback_data="rob:guide")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


@router.message(F.text.lower() == "ограбить")
async def rob_cmd(message: Message):
    if not check_group_only(message, "ограбление"):
        return
    uid = message.from_user.id
    await message.answer(rob_main_menu_text(uid), reply_markup=rob_main_menu_kb(uid))


@router.callback_query(F.data == "rob:main")
async def rob_main_cb(callback: CallbackQuery):
    uid = callback.from_user.id
    try:
        await callback.message.edit_text(rob_main_menu_text(uid), reply_markup=rob_main_menu_kb(uid))
    except Exception:
        pass
    await callback.answer()


@router.callback_query(F.data == "rob:guide")
async def rob_guide(callback: CallbackQuery):
    text = (
        "📖 <b>КАК ОГРАБЛЯТЬ</b>\n━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
        "<b>1️⃣ Купи инструменты</b>\n"
        "🎭 Маска +3% | 💼 Сумка +5% | 🥷 Костюм +7%\n"
        "🚗 Машина +10% | 🔫 Оружие +15%\n\n"
        "<b>2️⃣ Выбери цель</b>\n"
        "🏧 Банкомат — безопасно\n"
        "🎰 Казино — средне\n"
        "🏦 Банк — опасно, но джекпот\n\n"
        "<b>3️⃣ Охрана</b>\n"
        "🛡 Если сработает — шанс −25% (с 🔫 только −10%)\n\n"
        "<b>4️⃣ Провал</b>\n"
        "💸 Штраф 20-30% | 🎒 Могут отобрать инструмент"
    )
    await callback.message.edit_text(text, reply_markup=InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="◀️ Назад", callback_data="rob:main")]
    ]))
    await callback.answer()


@router.callback_query(F.data == "rob:inventory")
async def rob_inventory(callback: CallbackQuery):
    uid = callback.from_user.id
    inv = rob_get_inventory(uid)
    if not inv:
        text = "🎒 <b>Инвентарь пуст</b>\n\nКупи в магазине!"
    else:
        text = "🎒 <b>Твой инвентарь</b>\n━━━━━━━━━━━━━━━━━━━━━━━━\n"
        for item_key, cnt in inv.items():
            if item_key in ROB_TOOLS:
                d = ROB_TOOLS[item_key]
                text += f"{d['emoji']} <b>{d['name']}</b> ×{cnt} (+{d['bonus']}%)\n"
    await callback.message.edit_text(text, reply_markup=InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🛒 Магазин", callback_data="rob:shop")],
        [InlineKeyboardButton(text="◀️ Назад", callback_data="rob:main")]
    ]))
    await callback.answer()


@router.callback_query(F.data == "rob:shop")
async def rob_shop(callback: CallbackQuery):
    text = "🛒 <b>МАГАЗИН</b>\n━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
    for key, d in ROB_TOOLS.items():
        text += f"{d['emoji']} <b>{d['name']}</b> — {format_balance(d['price'])}\n   +{d['bonus']}% | износ {int(d['wear']*100)}%\n\n"
    rows = []
    for key, d in ROB_TOOLS.items():
        rows.append([InlineKeyboardButton(
            text=f"{d['emoji']} {d['name']} — {format_balance(d['price'])}",
            callback_data=f"rob:buy:{key}")])
    rows.append([InlineKeyboardButton(text="◀️ Назад", callback_data="rob:main")])
    await callback.message.edit_text(text, reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))
    await callback.answer()


@router.callback_query(F.data.startswith("rob:buy:"))
async def rob_buy(callback: CallbackQuery):
    uid = callback.from_user.id
    item_key = callback.data.split(":")[2]
    if item_key not in ROB_TOOLS:
        await callback.answer("Неизвестно", show_alert=True)
        return
    d = ROB_TOOLS[item_key]
    user = get_user(uid)
    if user["balance"] < d["price"]:
        await callback.answer(f"❌ Нужно {format_balance(d['price'])}", show_alert=True)
        return
    update_balance(uid, -d["price"])
    rob_add_item(uid, item_key, 1)
    await callback.answer(f"✅ Куплено: {d['name']}", show_alert=True)
    await rob_shop(callback)


@router.callback_query(F.data.startswith("rob:do:"))
async def rob_do(callback: CallbackQuery):
    uid = callback.from_user.id
    target = callback.data.split(":")[2]
    if target not in ROB_TARGETS:
        await callback.answer("Неизвестно", show_alert=True)
        return
    cd = rob_get_cooldown(uid, target)
    if cd > 0:
        m, s = cd // 60, cd % 60
        await callback.answer(f"⏳ Кулдаун: {m}м {s}с", show_alert=True)
        return
    user = get_user(uid)
    if user["balance"] < 500_000:
        await callback.answer(f"❌ Нужно минимум {format_balance(500_000)}", show_alert=True)
        return
    t = ROB_TARGETS[target]
    chance, used_tools = rob_calc_chance(uid, target)
    has_guard = random.random() * 100 < t["guard"]
    guard_penalty = 0
    if has_guard:
        has_weapon = "weapon" in used_tools
        guard_penalty = 10 if has_weapon else 25
        chance = max(5, chance - guard_penalty)
    success = random.random() * 100 < chance
    tools_str = " ".join(ROB_TOOLS[t_]["emoji"] for t_ in used_tools) if used_tools else "❌ без инструментов"
    await callback.message.edit_text(
        f"🎬 <b>Ограбление {t['name'].lower()}...</b>\n\n"
        f"🎒 {tools_str}\n🎲 Шанс: {chance}%\n"
        + (f"🛡 <b>ОХРАНА!</b> Шанс −{guard_penalty}%\n" if has_guard else "")
        + "\n<i>Проникаем...</i>",
        reply_markup=None)
    await callback.answer()
    await asyncio.sleep(2.5)
    worn_items = []
    for t_key in used_tools:
        if random.random() < ROB_TOOLS[t_key]["wear"]:
            rob_remove_item(uid, t_key, 1)
            worn_items.append(t_key)
    rob_set_cooldown(uid, target)
    if success:
        win_amount = random.randint(t["min"], t["max"])
        update_balance(uid, win_amount)
        with get_db() as conn:
            with conn.cursor() as cursor:
                cursor.execute("UPDATE users SET robberies_won = COALESCE(robberies_won, 0) + 1 WHERE user_id = %s", (uid,))
                conn.commit()
        log_admin_action(uid, f"rob_success {target} {win_amount}")
        result_text = (
            f"🎉🎉🎉 <b>УСПЕХ!</b>\n━━━━━━━━━━━━━━━━━━━━━━━━\n"
            f"{t['emoji']} <b>{t['name']}</b> ограблен!\n\n"
            f"💰 Добыча: <b>{format_balance(win_amount)}</b>\n🎲 Шанс был: {chance}%\n"
        )
        if worn_items:
            result_text += f"\n🔧 Износились: " + ", ".join(f"{ROB_TOOLS[k]['emoji']}" for k in worn_items)
    else:
        penalty_pct = 0.30 if has_guard else 0.20
        penalty = min(int(t["max"] * penalty_pct), user["balance"])
        update_balance(uid, -penalty)
        stolen = None
        if has_guard and used_tools:
            stolen = random.choice(used_tools)
            rob_remove_item(uid, stolen, 1)
        log_admin_action(uid, f"rob_fail {target} -{penalty}")
        result_text = f"🚨 <b>ПРОВАЛ!</b>\n━━━━━━━━━━━━━━━━━━━━━━━━\n{t['emoji']} <b>{t['name']}</b> — не вышло!\n\n"
        if has_guard:
            result_text += "🛡 <b>Охрана поймала!</b>\n"
        result_text += f"💸 Штраф: <b>{format_balance(penalty)}</b>\n"
        if stolen:
            result_text += f"🎒 Отобрали: {ROB_TOOLS[stolen]['emoji']} {ROB_TOOLS[stolen]['name']}\n"
    await callback.message.edit_text(result_text, reply_markup=InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔄 Ещё раз", callback_data="rob:main")],
        [InlineKeyboardButton(text="🛒 Магазин", callback_data="rob:shop")]
    ]))

def admin_panel_text(uid):
    user = get_user(uid)
    rank_name = RANK_NAMES.get(get_rank(uid), "Игрок")
    with get_db() as conn:
        with conn.cursor() as cursor:
            cursor.execute("SELECT COUNT(*) FROM users"); total_users = cursor.fetchone()[0]
            cursor.execute("SELECT COALESCE(SUM(balance),0) FROM users"); total_bal = cursor.fetchone()[0] or 0
            cursor.execute("SELECT COUNT(*) FROM users WHERE is_banned = TRUE"); bans = cursor.fetchone()[0]
    return (f"🛡 <b>АДМИН-ПАНЕЛЬ {BOT_NAME}</b>\n━━━━━━━━━━━━━━━━━━━━━━━━\n"
            f"👤 <b>Вы:</b> {rank_name} <code>{uid}</code>\n"
            f"━━━━━━━━━━━━━━━━━━━━━━━━\n"
            f"👥 Юзеров: <b>{total_users}</b>\n"
            f"💰 Баланс: <b>{format_balance(total_bal)}</b>\n"
            f"📊 Средний: <b>{format_balance(total_bal // total_users if total_users > 0 else 0)}</b>\n"
            f"🚫 Бан: {bans}\n"
            f"━━━━━━━━━━━━━━━━━━━━━━━━\n<i>Выбери раздел 👇</i>")

def admin_panel_kb(uid):
    lvl = get_rank_level(uid)
    rows = [[InlineKeyboardButton(text="📊 Статистика", callback_data="adm:stats")]]
    if lvl >= 1:
        rows.append([InlineKeyboardButton(text="💰 Выдать деньги", callback_data="adm:give_money"), InlineKeyboardButton(text="🚫 Баны", callback_data="adm:ban")])
    if lvl >= 2:
        rows.append([InlineKeyboardButton(text="📦 Выдать предмет", callback_data="adm:give_item")])
    if lvl >= 3:
        rows.append([InlineKeyboardButton(text="🌧 Сделать дождь", callback_data="adm:coin_rain")])
    rows.append([InlineKeyboardButton(text="❌ Закрыть", callback_data="adm:close")])
    return InlineKeyboardMarkup(inline_keyboard=rows)

@router.message(Command("admin"))
@router.message(F.text == "🛡 Админ-панель")
async def cmd_admin(message: Message):
    if not is_admin(message.from_user.id): return await message.answer("❌ Нет доступа.")
    await message.answer(admin_panel_text(message.from_user.id), reply_markup=admin_panel_kb(message.from_user.id))

@router.callback_query(F.data == "adm:main")
async def adm_main_cb(cb: CallbackQuery):
    if not is_admin(cb.from_user.id): return await cb.answer("❌", show_alert=True)
    try: await cb.message.edit_text(admin_panel_text(cb.from_user.id), reply_markup=admin_panel_kb(cb.from_user.id))
    except: pass
    await cb.answer()

@router.callback_query(F.data == "adm:close")
async def adm_close(cb: CallbackQuery):
    try: await cb.message.delete()
    except: pass
    await cb.answer()

@router.callback_query(F.data == "adm:coin_rain")
async def adm_coin_rain_start(cb: CallbackQuery):
    if not is_admin_or_above(cb.from_user.id): return await cb.answer("❌ Нет доступа", show_alert=True)
    admin_states[cb.from_user.id] = {"action": "adm_coin_rain"}
    await cb.message.edit_text("🌧 <b>ДОЖДЬ ИЗ МОНЕТ</b>\n\nВведи сумму, которую хочешь выдать <b>каждому</b> игроку:\n<i>Для отмены: /cancel</i>", reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="◀️ Назад", callback_data="adm:main")]]))
    await cb.answer()

@router.callback_query(F.data == "adm:give_item")
async def adm_give_item_start(cb: CallbackQuery):
    if not is_spadmin_or_above(cb.from_user.id): return await cb.answer("❌ Доступно только SPADMIN+", show_alert=True)
    admin_states[cb.from_user.id] = {"action": "adm_give_item_target"}
    await cb.message.edit_text("📦 <b>ВЫДАТЬ ПРЕДМЕТ</b>\n\nОтправь ID или @username игрока:\n<i>Для отмены: /cancel</i>", reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="◀️ Назад", callback_data="adm:main")]]))
    await cb.answer()

@router.callback_query(F.data.startswith("give:"))
async def adm_give_item_process(cb: CallbackQuery):
    uid = cb.from_user.id
    state = admin_states.get(uid)
    if not state or state.get("action") != "adm_give_item_choose": return await cb.answer("Сессия истекла.", show_alert=True)
    target_id = state["target_id"]
    parts = cb.data.split(":"); item_category, item_name = parts[1], parts[2]
    try:
        with get_db() as conn:
            with conn.cursor() as cursor:
                if item_category == "case":
                    cursor.execute("INSERT INTO cases_inventory (user_id, case_type, count) VALUES (%s, %s, 1) ON CONFLICT (user_id, case_type) DO UPDATE SET count = cases_inventory.count + 1", (target_id, item_name))
                    item_text = f"Кейс «{item_name}»"
                elif item_category == "card":
                    cursor.execute("INSERT INTO miners (user_id, card_type, count) VALUES (%s, %s, 1) ON CONFLICT (user_id, card_type) DO UPDATE SET count = miners.count + 1", (target_id, item_name))
                    item_text = f"Карта «{item_name}»"
                elif item_category == "tool":
                    cursor.execute("INSERT INTO rob_inventory (user_id, item_type, count) VALUES (%s, %s, 1) ON CONFLICT (user_id, item_type) DO UPDATE SET count = rob_inventory.count + 1", (target_id, item_name))
                    item_text = f"Инструмент «{ROB_TOOLS[item_name]['name']}»"
                conn.commit()
        log_admin_action(uid, f"give_{item_category}", target_id=target_id)
        admin_states.pop(uid, None)
        await cb.message.edit_text(f"✅ <b>Успешно выдано!</b>\n\n👤 Игрок: <code>{target_id}</code>\n🎁 Предмет: {item_text}", reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="◀️ В меню", callback_data="adm:main")]]))
        await cb.answer("Выдано!")
    except Exception as e:
        logging.error(f"Ошибка: {e}")
        await cb.answer("❌ Ошибка БД", show_alert=True)

@router.message(F.text, lambda m: m.from_user.id in admin_states)
async def admin_text_handler(message: Message):
    uid = message.from_user.id
    state = admin_states.get(uid, {})
    action = state.get("action")
    text = message.text.strip()
    if text == "/cancel":
        admin_states.pop(uid, None)
        return await message.answer("❌ Отменено.", reply_markup=admin_panel_kb(uid))
    if action == "adm_coin_rain":
        if not text.isdigit(): return await message.answer("❌ Введи число. Или /cancel")
        amount = int(text)
        admin_states.pop(uid, None)
        with get_db() as conn:
            with conn.cursor() as cursor:
                cursor.execute("UPDATE users SET balance = balance + %s", (amount,)); conn.commit()
                cursor.execute("SELECT user_id FROM users"); users = cursor.fetchall()
        await message.answer(f"✅ Начислено по {format_balance(amount)} всем!", reply_markup=admin_panel_kb(uid))
        for u in users:
            try:
                await bot.send_message(u[0], f"🌧 <b>ДОЖДЬ ИЗ МОНЕТ!</b>\n\nВладелец выдал каждому по <b>{format_balance(amount)}</b>! 🎉")
                await asyncio.sleep(0.05)
            except: pass
        return
    if action == "adm_give_item_target":
        target = find_user_by_identifier(text)
        if not target: return await message.answer("❌ Игрок не найден. Попробуй ещё раз или /cancel")
        admin_states[uid] = {"action": "adm_give_item_choose", "target_id": target["user_id"]}
        kb = [[InlineKeyboardButton(text=f"📦 Кейс: {case}", callback_data=f"give:case:{case}")] for case in CASE_TYPES.keys()]
        kb += [[InlineKeyboardButton(text=f"💻 Карта: {card}", callback_data=f"give:card:{card}")] for card in MINING_PRICES.keys()]
        kb += [[InlineKeyboardButton(text=f"{tool_data['emoji']} {tool_data['name']}", callback_data=f"give:tool:{tool_key}")] for tool_key, tool_data in ROB_TOOLS.items()]
        kb.append([InlineKeyboardButton(text="❌ Отмена", callback_data="adm:main")])
        await message.answer(f"📦 Выдаём: <b>{target['first_name']}</b> (<code>{target['user_id']}</code>)\n\nВыбери предмет:", reply_markup=InlineKeyboardMarkup(inline_keyboard=kb))
        return

@router.callback_query(F.data.in_(["adm:stats", "adm:give_money", "adm:ban"]))
async def adm_wip_cb(cb: CallbackQuery):
    await cb.answer("🚧 В разработке!", show_alert=True)

async def main():
    dp.include_router(router)
    await bot.delete_webhook(drop_pending_updates=True)
    print("Бот запущен!")
    await dp.start_polling(bot)

if __name__ == "__main__":
    try: asyncio.run(main())
    except KeyboardInterrupt: print("Бот выключен.")

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
# МИНЫ V3 — СБАЛАНСИРОВАННАЯ
# ============================================================
mines_sessions = {}
MINES_BOMB_OPTIONS = [3, 5, 7, 10, 15, 20, 24]
MINES_HOUSE_EDGE = 0.04  # 4% в пользу казино (справедливо)


def mines_multiplier(bombs: int, opened: int) -> float:
    """Честный множитель с house edge."""
    if opened <= 0:
        return 1.0
    safe = 25 - bombs
    if opened > safe:
        opened = safe
    prob = 1.0
    for i in range(opened):
        prob *= (safe - i) / (25 - i)
    if prob <= 0:
        return 0
    return round((1 / prob) * (1 - MINES_HOUSE_EDGE), 2)


def mines_max_multiplier(bombs: int) -> float:
    return mines_multiplier(bombs, 25 - bombs)


def mines_risk_emoji(bombs: int) -> str:
    if bombs <= 3: return "🟢"
    if bombs <= 7: return "🟡"
    if bombs <= 15: return "🟠"
    return "🔴"


def mines_risk_text(bombs: int) -> str:
    if bombs <= 3: return "Низкий"
    if bombs <= 7: return "Средний"
    if bombs <= 15: return "Высокий"
    return "ЭКСТРИМ"


def get_mines_choice_kb(session_id):
    sess = mines_sessions.get(session_id)
    if not sess:
        return None
    kb = []
    for b in MINES_BOMB_OPTIONS:
        m1 = mines_multiplier(b, 1)
        mmax = mines_max_multiplier(b)
        risk = mines_risk_emoji(b)
        kb.append([InlineKeyboardButton(
            text=f"{risk} 💣{b} — x{m1} за клетку (макс x{mmax})",
            callback_data=f"mn_bomb_{session_id}_{b}"
        )])
    kb.append([InlineKeyboardButton(
        text=f"❌ Отменить и вернуть {format_balance(sess['bet'])}",
        callback_data=f"mn_cancel_{session_id}"
    )])
    return InlineKeyboardMarkup(inline_keyboard=kb)


def get_mines_grid_kb(session_id, revealed, exploded=False):
    sess = mines_sessions.get(session_id)
    if not sess:
        return None
    bombs = sess.get("bomb_positions", set())
    bomb_count = sess.get("bombs", 0)
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
        mult = mines_multiplier(bomb_count, len(revealed))
        win = int(sess["bet"] * mult)
        kb.append([InlineKeyboardButton(
            text=f"💰 ЗАБРАТЬ {format_balance(win)} (x{mult})",
            callback_data=f"mn_cash_{session_id}"
        )])
    return InlineKeyboardMarkup(inline_keyboard=kb)


def mines_status_text(sess, mention):
    bombs = sess["bombs"]
    lvl = len(sess["revealed"])
    safe = 25 - bombs
    mult = mines_multiplier(bombs, lvl)
    cur_win = int(sess["bet"] * mult)
    next_mult = mines_multiplier(bombs, lvl + 1) if lvl < safe else mult
    next_win = int(sess["bet"] * next_mult)

    filled = int(lvl / safe * 10) if safe > 0 else 0
    bar = "▰" * filled + "▱" * (10 - filled)

    text = (
        f"💣 {mention}, игра идёт!\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"💰 Ставка: {format_balance(sess['bet'])}\n"
        f"💣 Бомб: <b>{bombs}</b> ({mines_risk_emoji(bombs)} {mines_risk_text(bombs)})\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"💎 Открыто: <b>{lvl}/{safe}</b> {bar}\n"
        f"📊 Множитель: <b>x{mult}</b>\n"
        f"💵 Сейчас: <b>{format_balance(cur_win)}</b>\n"
    )
    if lvl < safe:
        text += f"➡️ Следующая: x{next_mult} = {format_balance(next_win)}\n"
    text += f"━━━━━━━━━━━━━━━━━━━━\n"
    text += f"<i>Открывай клетки или забери выигрыш!</i>"
    return text


@router.message(F.text.lower().startswith("мины"))
async def game_mines(message: Message):
    if not check_group_only(message, "мины"):
        return
    args = message.text.split()
    if len(args) < 2 or not args[1].isdigit():
        await message.answer(
            f"💣 <b>Мины</b> — сапёр с множителями\n\n"
            f"📋 <b>Использование:</b> <code>мины [ставка]</code>\n"
            f"📝 <b>Пример:</b> <code>мины 1000</code>\n\n"
            f"🎯 <b>Как играть:</b>\n"
            f"1️⃣ Ставишь ставку\n"
            f"2️⃣ Выбираешь кол-во бомб (3-24)\n"
            f"3️⃣ Открываешь клетки — множитель растёт\n"
            f"4️⃣ Забираешь выигрыш или подрываешься\n\n"
            f"⚠️ Мин. ставка: <b>{MIN_BET} {CURRENCY}</b>"
        )
        return
    bet = int(args[1])
    if bet < MIN_BET:
        await message.answer(f"❌ Минимальная ставка: <b>{MIN_BET} {CURRENCY}</b>")
        return
    user = get_user(message.from_user.id, message.from_user.first_name or "",
                    message.from_user.last_name or "", message.from_user.username or "")
    if user["balance"] < bet:
        await message.answer(f"❌ Недостаточно средств.\n💰 Баланс: {format_balance(user['balance'])}")
        return
    update_balance(user["user_id"], -bet)
    session_id = f"{message.from_user.id}_{message.message_id}"
    mines_sessions[session_id] = {
        "user_id": user["user_id"], "user_name": user["first_name"],
        "bet": bet, "bombs": 0, "bomb_positions": set(), "revealed": set(),
        "state": "choosing"
    }
    mention = get_mention(user["user_id"], user["first_name"])
    await message.answer(
        f"💣 {mention}, выбирай сложность!\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"💰 Ставка: <b>{format_balance(bet)}</b>\n"
        f"📊 Больше бомб = выше множитель\n"
        f"🎯 Меньше бомб = безопаснее\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"🟢 Низкий | 🟡 Средний | 🟠 Высокий | 🔴 Экстрим\n\n"
        f"<i>Выбери количество бомб:</i>",
        reply_markup=get_mines_choice_kb(session_id)
    )


@router.callback_query(F.data.startswith("mn_bomb_"))
async def mines_bomb_chosen(callback: CallbackQuery):
    parts = callback.data.split("_")
    session_id = f"{parts[2]}_{parts[3]}"
    bombs = int(parts[4])
    if session_id not in mines_sessions:
        await callback.answer("Игра завершена.", show_alert=True)
        return
    sess = mines_sessions[session_id]
    if sess["user_id"] != callback.from_user.id:
        await callback.answer("❌ Чужая игра!", show_alert=True)
        return
    if sess.get("state") != "choosing":
        await callback.answer("Уже выбрано!", show_alert=True)
        return
    bomb_set = set(random.sample(range(25), bombs))
    sess["bombs"] = bombs
    sess["bomb_positions"] = bomb_set
    sess["state"] = "playing"
    mention = get_mention(sess["user_id"], sess["user_name"])
    safe = 25 - bombs
    m1 = mines_multiplier(bombs, 1)
    mmax = mines_max_multiplier(bombs)
    await callback.message.edit_text(
        f"💣 {mention}, игра началась!\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"💰 Ставка: {format_balance(sess['bet'])}\n"
        f"💣 Бомб: <b>{bombs}</b> ({mines_risk_emoji(bombs)} {mines_risk_text(bombs)})\n"
        f"💎 Безопасных: <b>{safe}</b>\n"
        f"📊 Множитель: от x{m1} до x{mmax}\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"<i>Открывай клетки — множитель растёт!</i>",
        reply_markup=get_mines_grid_kb(session_id, set())
    )
    await callback.answer()


@router.callback_query(F.data.startswith("mn_cancel_"))
async def mines_cancel(callback: CallbackQuery):
    parts = callback.data.split("_")
    session_id = f"{parts[2]}_{parts[3]}"
    if session_id not in mines_sessions:
        await callback.answer("Игра завершена.", show_alert=True)
        return
    sess = mines_sessions[session_id]
    if sess["user_id"] != callback.from_user.id:
        await callback.answer("❌ Чужая игра!", show_alert=True)
        return
    update_balance(sess["user_id"], sess["bet"])
    del mines_sessions[session_id]
    await callback.message.edit_text(
        f"❌ <b>Игра отменена</b>\n\n"
        f"Возвращено: <b>{format_balance(sess['bet'])}</b>"
    )
    await callback.answer("Отменено")


@router.callback_query(F.data.startswith("mn_"))
async def mines_callback(callback: CallbackQuery):
    parts = callback.data.split("_")

    if parts[1] == "noop":
        await callback.answer()
        return

    # --- ЗАБРАТЬ ВЫИГРЫШ ---
    if parts[1] == "cash":
        session_id = f"{parts[2]}_{parts[3]}"
        if session_id not in mines_sessions:
            await callback.answer("Игра завершена.", show_alert=True)
            return
        sess = mines_sessions[session_id]
        if sess["user_id"] != callback.from_user.id:
            await callback.answer("❌ Чужая игра!", show_alert=True)
            return
        lvl = len(sess["revealed"])
        if lvl == 0:
            update_balance(sess["user_id"], sess["bet"])
            del mines_sessions[session_id]
            await callback.message.edit_text(f"↩️ Вернул ставку: {format_balance(sess['bet'])}")
            return
        mult = mines_multiplier(sess["bombs"], lvl)
        win = int(sess["bet"] * mult)
        profit = win - sess["bet"]
        update_balance(sess["user_id"], win)
        with get_db() as conn:
            with conn.cursor() as cursor:
                cursor.execute("UPDATE users SET games_played = games_played + 1, games_won = games_won + 1 WHERE user_id = %s", (sess["user_id"],))
                conn.commit()
        del mines_sessions[session_id]
        mention = get_mention(sess["user_id"], sess["user_name"])
        await callback.message.edit_text(
            f"💰 <b>{mention} забрал выигрыш!</b>\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"💣 Бомб: {sess['bombs']}\n"
            f"💎 Открыто: {lvl}/{25-sess['bombs']}\n"
            f"📊 Множитель: <b>x{mult}</b>\n"
            f"💵 Ставка: {format_balance(sess['bet'])}\n"
            f"💰 Выигрыш: <b>{format_balance(win)}</b>\n"
            f"📈 Профит: <b>+{format_balance(profit)}</b>"
        )
        return

    # --- ОТКРЫТЬ КЛЕТКУ ---
    session_id = f"{parts[1]}_{parts[2]}"
    idx = int(parts[3])
    if session_id not in mines_sessions:
        await callback.answer("Игра завершена.", show_alert=True)
        return
    sess = mines_sessions[session_id]
    if sess["user_id"] != callback.from_user.id:
        await callback.answer("❌ Чужая игра!", show_alert=True)
        return
    if idx in sess["revealed"]:
        await callback.answer("Клетка уже открыта!", show_alert=True)
        return
    mention = get_mention(sess["user_id"], sess["user_name"])

    # Проклятие (25% шанс подмены безопасной на бомбу)
    if is_cursed(callback.from_user.id) and random.random() < 0.25:
        if idx not in sess["bomb_positions"]:
            idx = random.choice(list(sess["bomb_positions"]))

    # --- ПОДРЫВ ---
    if idx in sess["bomb_positions"]:
        sess["revealed"].add(idx)
        kb = get_mines_grid_kb(session_id, sess["revealed"], exploded=True)
        bet = sess["bet"]
        bombs = sess["bombs"]
        lvl = len(sess["revealed"]) - 1
        del mines_sessions[session_id]
        with get_db() as conn:
            with conn.cursor() as cursor:
                cursor.execute("UPDATE users SET games_played = games_played + 1 WHERE user_id = %s", (sess["user_id"],))
                conn.commit()
        await callback.message.edit_text(
            f"💥 <b>{mention}, подорвался!</b>\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"💣 Бомб: {bombs}\n"
            f"💎 Открыто: {lvl} клеток\n"
            f"💸 Проиграно: <b>{format_balance(bet)}</b>\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"<i>Повезёт в следующий раз!</i>",
            reply_markup=kb
        )
        return

    # --- ОТКРЫЛИ БЕЗОПАСНУЮ ---
    sess["revealed"].add(idx)
    lvl = len(sess["revealed"])
    bombs = sess["bombs"]
    safe = 25 - bombs

    # --- ДЖЕКПОТ ---
    if lvl >= safe:
        mult = mines_multiplier(bombs, lvl)
        win = int(sess["bet"] * mult)
        update_balance(sess["user_id"], win)
        kb = get_mines_grid_kb(session_id, sess["revealed"], exploded=True)
        del mines_sessions[session_id]
        with get_db() as conn:
            with conn.cursor() as cursor:
                cursor.execute("UPDATE users SET games_played = games_played + 1, games_won = games_won + 1 WHERE user_id = %s", (sess["user_id"],))
                conn.commit()
        await callback.message.edit_text(
            f"🎉🎉🎉 <b>ДЖЕКПОТ!!!</b> 🎉🎉🎉\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"🏆 {mention} открыл ВСЁ поле!\n"
            f"💣 Бомб: {bombs}\n"
            f"💎 Открыто: {lvl}/{safe} — 100%!\n"
            f"📊 Множитель: <b>x{mult}</b>\n"
            f"💰 Выигрыш: <b>{format_balance(win)}</b>\n"
            f"━━━━━━━━━━━━━━━━━━━━",
            reply_markup=kb
        )
        return

    # --- ПРОДОЛЖЕНИЕ ---
    await callback.message.edit_text(
        mines_status_text(sess, mention),
        reply_markup=get_mines_grid_kb(session_id, sess["revealed"])
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
    "• <code>/history @user</code> — история игр\n"
    "• <code>/admh</code> или <code>.admh</code> — эта справка\n\n"

    "✨ <b>SPADMIN+ (спец. админ и выше):</b>\n"
    "• <code>/setbal @user сумма</code> — установить баланс\n"
    "• <code>/take @user сумма</code> — списать средства\n"
    "• <code>/resetbal @user</code> — сброс баланса до 4000\n\n"

    "🔰 <b>ADMIN+ (администратор и выше):</b>\n"
    "• <code>/addpromo код сумма uses</code> — создать промо\n"
    "• <code>/delpromo код</code> — удалить промо\n"
    "• <code>/makeadmin @user</code> — назначить модером\n"
    "• <code>/unadmin @user</code> — снять ранг\n\n"

    "👑 <b>HEAD+ (гл. админ и выше):</b>\n"
    "• <code>/add_admin @user ранг</code> — назначить (moder/spadmin/admin/head)\n"
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
    "• <code>/checkpoint @user</code> — сохранить баланс\n"
    "• <code>/restore_checkpoint @user</code> — восстановить\n\n"

    "📌 <b>Иерархия рангов:</b>\n"
    "💎 owner → 👑 head → 🔰 admin → ✨ spadmin → ⭐ moder"
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
# /vldh — СПРАВКА ТОЛЬКО ДЛЯ ВЛАДЕЛЬЦА
# ============================================================

OWNER_HELP_TEXT = (
    "💎 <b>КОМАНДЫ ВЛАДЕЛЬЦА MINT</b>\n"
    "━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
    "• <code>/zero @user</code> — обнулить баланс\n"
    "• <code>/double @user</code> — удвоить баланс\n"
    "• <code>/randomize @user мин макс</code> — случайный баланс\n"
    "• <code>/setallbal сумма</code> — всем установить баланс\n"
    "• <code>/resetallbal</code> — всем сброс до 4000\n"
    "• <code>/curse @user</code> — проклясть\n"
    "• <code>/bless @user</code> — снять проклятие\n"
    "• <code>/lottery сумма</code> — случайному юзеру\n"
    "• <code>/setname @user имя</code> — сменить имя\n"
    "• <code>/nick @user ник</code> — сменить ник\n"
    "• <code>/deleteuser @user</code> — удалить пользователя\n"
    "• <code>/givepower @user cmd</code> — выдать секретную команду\n"
    "• <code>/takepower @user cmd</code> — забрать команду\n"
    "• <code>/listpowers [@user]</code> — список команд\n"
    "• <code>/mypowers</code> — мои команды\n"
    "• <code>/clearlog</code> — очистить журнал\n"
    "• <code>/backup</code> — резервная копия БД\n"
    "• <code>/sql_execute SQL</code> — SQL-команда\n"
    "• <code>/emergency_stop</code> — остановить бота\n"
    "\n"
    "━━━━━━━━━━━━━━━━━━━━━━━━\n"
    "Дополнительные команды владельца также доступны здесь."
)


@router.message(Command("vldh"))
async def cmd_vldh(message: Message):
    if not is_owner(message.from_user.id):
        return
    await message.answer(OWNER_HELP_TEXT)


@router.message(F.text == ".vldh")
async def cmd_vldh_dot(message: Message):
    if not is_owner(message.from_user.id):
        return
    await message.answer(OWNER_HELP_TEXT)

# ============================================================
# 🛡 АДМИН-ПАНЕЛЬ
# ============================================================

def _check_admin(event) -> bool:
    """Проверка прав администратора для callback/message event."""
    if hasattr(event, "from_user") and event.from_user:
        return is_admin(event.from_user.id)
    return False


def _back_kb(extra_rows=None, back_to="adm:main"):
    """Кнопка 'Назад' + опциональные дополнительные ряды."""
    rows = list(extra_rows) if extra_rows else []
    rows.append([InlineKeyboardButton(text="◀️ Назад", callback_data=back_to)])
    return InlineKeyboardMarkup(inline_keyboard=rows)


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
    except Exception:
        try:
            await callback.message.answer(_admin_main_text(callback.from_user.id),
                                           reply_markup=_admin_main_kb(callback.from_user.id))
        except Exception as e:
            logging.error(f"adm_main error: {e}")
    await callback.answer()


@router.callback_query(F.data == "adm:help")
async def adm_help(callback: CallbackQuery):
    if not _check_admin(callback):
        await callback.answer("Нет доступа", show_alert=True)
        return
    try:
        await callback.message.edit_text(ADMIN_HELP_TEXT, reply_markup=_back_kb())
    except Exception:
        await callback.message.answer(ADMIN_HELP_TEXT, reply_markup=_back_kb())
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
        elif action == "bank_open_amount":
            try:
                amount = int(text.replace(" ", "").replace("_", ""))
            except:
                await message.answer("❌ Введи число.")
                return
            if amount < DEPOSIT_MIN:
                await message.answer(f"❌ Минимум {format_balance(DEPOSIT_MIN)}.")
                return
            user = get_user(uid)
            if user["balance"] < amount:
                await message.answer(f"❌ Недостаточно. У тебя: {format_balance(user['balance'])}")
                return
            dep = get_deposit(uid)
            if dep:
                admin_states.pop(uid, None)
                await message.answer("❌ Депозит уже открыт.")
                return
            update_balance(uid, -amount)
            with get_db() as conn:
                with conn.cursor() as cursor:
                    cursor.execute("INSERT INTO deposits (user_id, amount, start_time) VALUES (%s, %s, %s)",
                                   (uid, amount, datetime.now()))
                    conn.commit()
            admin_states.pop(uid, None)
            await message.answer(f"✅ Депозит открыт на <b>{format_balance(amount)}</b>!\n📈 0.05% в час.")

        elif action == "bank_topup_amount":
            try:
                amount = int(text.replace(" ", "").replace("_", ""))
            except:
                await message.answer("❌ Введи число.")
                return
            if amount < DEPOSIT_MIN:
                await message.answer(f"❌ Минимум {format_balance(DEPOSIT_MIN)}.")
                return
            dep = get_deposit(uid)
            if not dep:
                admin_states.pop(uid, None)
                await message.answer("❌ Депозита нет. Сначала открой.")
                return
            today = get_today_topups(uid)
            if today >= DEPOSIT_MAX_TOPUPS_PER_DAY:
                admin_states.pop(uid, None)
                await message.answer(f"❌ Лимит {DEPOSIT_MAX_TOPUPS_PER_DAY}/день.")
                return
            user = get_user(uid)
            if user["balance"] < amount:
                await message.answer(f"❌ Недостаточно. У тебя: {format_balance(user['balance'])}")
                return
            update_balance(uid, -amount)
            with get_db() as conn:
                with conn.cursor() as cursor:
                    cursor.execute("UPDATE deposits SET amount = amount + %s WHERE user_id = %s", (amount, uid))
                    cursor.execute("INSERT INTO deposit_topups (user_id, amount, date, timestamp) VALUES (%s, %s, %s, %s)",
                                   (uid, amount, datetime.now().strftime("%Y-%m-%d"), datetime.now()))
                    conn.commit()
            admin_states.pop(uid, None)
            today_after = today + 1
            await message.answer(
                f"✅ Пополнено на <b>{format_balance(amount)}</b>\n"
                f"📊 Осталось пополнений: {DEPOSIT_MAX_TOPUPS_PER_DAY - today_after}/{DEPOSIT_MAX_TOPUPS_PER_DAY}"
            )

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
# 🛡 АДМИН-ПАНЕЛЬ V2 (ПОЛНОСТЬЮ РАБОЧАЯ)
# ============================================================

def admin_panel_text(uid):
    user = get_user(uid)
    rank = get_rank(uid)
    rank_name = RANK_NAMES.get(rank, rank)
    
    # Собираем статистику для шапки
    with get_db() as conn:
        with conn.cursor() as cursor:
            cursor.execute("SELECT COUNT(*) FROM users")
            total_users = cursor.fetchone()[0]
            cursor.execute("SELECT COALESCE(SUM(balance),0) FROM users")
            total_bal = cursor.fetchone()[0] or 0
            cursor.execute("SELECT COUNT(*) FROM users WHERE is_banned = TRUE")
            bans = cursor.fetchone()[0]
            cursor.execute("SELECT COUNT(*) FROM clans")
            clans = cursor.fetchone()[0]
            cursor.execute("SELECT COUNT(*) FROM promocodes")
            promos = cursor.fetchone()[0]
            cursor.execute("SELECT COUNT(*) FROM deposits")
            deposits = cursor.fetchone()[0]
            
    # Проверка тех. работ (если добавишь)
    tech_status = "🔴 ВКЛ" if get_setting("maintenance", "0") == "1" else "🟢 ВЫКЛ"

    text = (
        f"🛡 <b>АДМИН-ПАНЕЛЬ {BOT_NAME}</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"👤 <b>Вы:</b> {rank_name} <code>{uid}</code>\n"
        f"⚙️ <b>Статус:</b> {tech_status}\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"👥 Юзеров: <b>{total_users}</b>\n"
        f"💰 Баланс: <b>{format_balance(total_bal)}</b>\n"
        f"📊 Средний: <b>{format_balance(total_bal // total_users if total_users > 0 else 0)}</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"👑 VIP: 0 | 🚫 Бан: {bans}\n"
        f"🏰 Кланов: {clans} | 🎁 Промо: {promos}\n"
        f"🏦 Депозитов: {deposits} | 🎮 Игр: 0\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"<i>Выбери раздел 👇</i>"
    )
    return text


def admin_panel_kb(uid):
    lvl = get_rank_level(uid)
    rows = [
        [InlineKeyboardButton(text="👤 Профиль игрока", callback_data="adm:user_info"),
         InlineKeyboardButton(text="📊 Статистика", callback_data="adm:stats")],
    ]
    if lvl >= 1:
        rows.append([
            InlineKeyboardButton(text="💰 Выдать деньги", callback_data="adm:give_money"),
            InlineKeyboardButton(text="💸 Списать деньги", callback_data="adm:take_money"),
        ])
        rows.append([
            InlineKeyboardButton(text="🔨 Забанить", callback_data="adm:ban"),
            InlineKeyboardButton(text="✅ Разбанить", callback_data="adm:unban"),
        ])
        rows.append([
            InlineKeyboardButton(text="💸 Логи переводов", callback_data="adm:transfers"),
            InlineKeyboardButton(text="📋 Логи админов", callback_data="adm:adminlog"),
        ])
    if lvl >= 2:
        rows.append([
            InlineKeyboardButton(text="🎁 Промокоды", callback_data="adm:promo"),
            InlineKeyboardButton(text="📦 Выдать кейс", callback_data="adm:give_case"),
        ])
        rows.append([
            InlineKeyboardButton(text="👑 Выдать VIP", callback_data="adm:give_vip"),
            InlineKeyboardButton(text="💎 Выдать BTC", callback_data="adm:give_btc"),
        ])
    if lvl >= 3:
        rows.append([
            InlineKeyboardButton(text="🎮 Игры вкл/выкл", callback_data="adm:games"),
            InlineKeyboardButton(text="📢 Рассылка", callback_data="adm:broadcast"),
        ])
        # 👇 ВОТ ОНА, КНОПКА ДОЖДЯ (добавлена сюда)
        rows.append([
            InlineKeyboardButton(text="🌧 Сделать дождь", callback_data="adm:coin_rain"),
        ])
    if lvl >= 4:
        rows.append([
            InlineKeyboardButton(text="🛡 Управление админами", callback_data="adm:manage_admins"),
        ])
    if lvl >= 5:
        rows.append([
            InlineKeyboardButton(text="⚙️ Настройки", callback_data="adm:settings"),
            InlineKeyboardButton(text="🔐 Секретки", callback_data="adm:secret"),
        ])
    rows.append([InlineKeyboardButton(text="❌ Закрыть", callback_data="adm:close")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


@router.message(Command("admin"))
async def cmd_admin(message: Message):
    if not is_admin(message.from_user.id):
        await message.answer("❌ Нет доступа.")
        return
    await message.answer(admin_panel_text(message.from_user.id),
                         reply_markup=admin_panel_kb(message.from_user.id))


@router.message(F.text == "🛡 Админ-панель")
async def cmd_admin_btn(message: Message):
    await cmd_admin(message)


@router.callback_query(F.data == "adm:main")
async def adm_main_cb(cb: CallbackQuery):
    if not is_admin(cb.from_user.id):
        return await cb.answer("❌", show_alert=True)
    try:
        await cb.message.edit_text(admin_panel_text(cb.from_user.id),
                                   reply_markup=admin_panel_kb(cb.from_user.id))
    except:
        pass
    await cb.answer()


@router.callback_query(F.data == "adm:close")
async def adm_close(cb: CallbackQuery):
    try:
        await cb.message.delete()
    except:
        pass
    await cb.answer()


# ============================================================
# 📊 СТАТИСТИКА
# ============================================================

@router.callback_query(F.data == "adm:stats")
async def adm_stats_cb(cb: CallbackQuery):
    if not is_admin(cb.from_user.id):
        return await cb.answer("❌", show_alert=True)
    with get_db() as conn:
        with conn.cursor() as cursor:
            cursor.execute("SELECT COUNT(*) FROM users")
            total = cursor.fetchone()[0]
            cursor.execute("SELECT COALESCE(SUM(balance),0) FROM users")
            total_bal = cursor.fetchone()[0] or 0
            cursor.execute("SELECT COUNT(*) FROM deposits")
            dep_count = cursor.fetchone()[0]
            cursor.execute("SELECT COALESCE(SUM(amount),0) FROM deposits")
            dep_sum = cursor.fetchone()[0] or 0
            cursor.execute("SELECT COUNT(*) FROM transfers_log")
            trans = cursor.fetchone()[0]

    text = (
        f"📊 <b>СТАТИСТИКА {BOT_NAME}</b>\n━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
        f"👥 Всего юзеров: <b>{total}</b>\n"
        f"💰 Всего COINS: <b>{format_balance(total_bal)}</b>\n"
        f"🏦 Депозитов: <b>{dep_count}</b> на {format_balance(dep_sum)}\n"
        f"💸 Переводов: <b>{trans}</b>\n"
    )
    await cb.message.edit_text(text, reply_markup=InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="◀️ Назад", callback_data="adm:main")]
    ]))
    await cb.answer()


# ============================================================
# 📦 ВЫДАТЬ ПРЕДМЕТ (Полный цикл)
# ============================================================

@router.callback_query(F.data == "adm:give_item")
async def adm_give_item_start(cb: CallbackQuery):
    if not is_spadmin_or_above(cb.from_user.id):
        return await cb.answer("❌ Доступно только SPADMIN+", show_alert=True)
    
    admin_states[cb.from_user.id] = {"action": "adm_give_item_target"}
    await cb.message.edit_text(
        "📦 <b>ВЫДАТЬ ПРЕДМЕТ</b>\n\n"
        "Шаг 1/2 — отправь ID или @username игрока:\n"
        "<i>Для отмены: /cancel</i>",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="◀️ Назад", callback_data="adm:main")]
        ])
    )
    await cb.answer()


# ============================================================
# 🎁 ПРОМОКОДЫ
# ============================================================

@router.callback_query(F.data == "adm:promo")
async def adm_promo_cb(cb: CallbackQuery):
    if not is_spadmin_or_above(cb.from_user.id):
        return await cb.answer("❌", show_alert=True)
    with get_db() as conn:
        with conn.cursor() as cursor:
            cursor.execute("SELECT code, amount, uses FROM promocodes ORDER BY code LIMIT 10")
            rows = cursor.fetchall()
    text = f"🎁 <b>ПРОМОКОДЫ</b>\n━━━━━━━━━━━━━━━━━━━━━━━━\n"
    if not rows:
        text += "\n<i>Пока нет промокодов</i>\n"
    else:
        for code, amt, uses in rows:
            text += f"<code>{code}</code> — {format_balance(amt)} × {uses}\n"
    
    await cb.message.edit_text(text, reply_markup=InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="➕ Создать", callback_data="adm:promo_create")],
        [InlineKeyboardButton(text="◀️ Назад", callback_data="adm:main")]
    ]))
    await cb.answer()

@router.callback_query(F.data == "adm:promo_create")
async def adm_promo_create_cb(cb: CallbackQuery):
    if not is_spadmin_or_above(cb.from_user.id):
        return await cb.answer("❌", show_alert=True)
    admin_states[cb.from_user.id] = {"action": "adm_promo_create"}
    await cb.message.edit_text(
        "🎁 <b>СОЗДАНИЕ ПРОМОКОДА</b>\n\n"
        "Формат: <code>КОД СУММА КОЛИЧЕСТВО</code>\n"
        "Пример: <code>PROMO2025 1000000 100</code>\n\n"
        "<i>Для отмены: /cancel</i>",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="◀️ Назад", callback_data="adm:promo")]
        ])
    )
    await cb.answer()

# ============================================================
# 📢 РАССЫЛКА
# ============================================================

@router.callback_query(F.data == "adm:broadcast")
async def adm_broadcast_start(cb: CallbackQuery):
    if not is_admin_or_above(cb.from_user.id):
        return await cb.answer("❌", show_alert=True)
    admin_states[cb.from_user.id] = {"action": "adm_broadcast"}
    await cb.message.edit_text(
        "📢 <b>РАССЫЛКА</b>\n\n"
        "Отправь текст сообщения:\n"
        "<i>Для отмены: /cancel</i>",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="◀️ Назад", callback_data="adm:main")]
        ])
    )
    await cb.answer()

# ============================================================
# 🎮 ИГРЫ ВКЛ/ВЫКЛ
# ============================================================

@router.callback_query(F.data == "adm:games")
async def adm_games_cb(cb: CallbackQuery):
    if not is_admin_or_above(cb.from_user.id):
        return await cb.answer("❌", show_alert=True)
    games_list = ["рулетка", "джокер", "мины", "coinflip", "дуэль", "ограбление", "кейсы"]
    with get_db() as conn:
        with conn.cursor() as cursor:
            cursor.execute("SELECT game_name FROM disabled_games")
            disabled = {r[0] for r in cursor.fetchall()}
    rows = []
    for g in games_list:
        status = "🔴 OFF" if g in disabled else "🟢 ON"
        rows.append([InlineKeyboardButton(
            text=f"{status} {g}",
            callback_data=f"adm:toggle_game:{g}"
        )])
    rows.append([InlineKeyboardButton(text="◀️ Назад", callback_data="adm:main")])
    await cb.message.edit_text(
        f"🎮 <b>УПРАВЛЕНИЕ ИГРАМИ</b>",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=rows)
    )
    await cb.answer()

@router.callback_query(F.data.startswith("adm:toggle_game:"))
async def adm_toggle_game(cb: CallbackQuery):
    if not is_admin_or_above(cb.from_user.id):
        return await cb.answer("❌", show_alert=True)
    game = cb.data.split(":", 2)[2]
    with get_db() as conn:
        with conn.cursor() as cursor:
            cursor.execute("SELECT 1 FROM disabled_games WHERE game_name = %s", (game,))
            if cursor.fetchone():
                cursor.execute("DELETE FROM disabled_games WHERE game_name = %s", (game,))
                action = "включена"
            else:
                cursor.execute("INSERT INTO disabled_games (game_name) VALUES (%s)", (game,))
                action = "выключена"
            conn.commit()
    await cb.answer(f"✅ {game}: {action}")
    await adm_games_cb(cb)


# ============================================================
# 📜 ЛОГИ
# ============================================================

@router.callback_query(F.data == "adm:adminlog")
async def adm_adminlog_cb(cb: CallbackQuery):
    if not is_admin(cb.from_user.id):
        return await cb.answer("❌", show_alert=True)
    with get_db() as conn:
        with conn.cursor() as cursor:
            cursor.execute("""
                SELECT l.id, l.admin_id, u.first_name, l.action, l.target_id, l.timestamp
                FROM admin_log l
                LEFT JOIN users u ON u.user_id = l.admin_id
                ORDER BY l.id DESC LIMIT 15
            """)
            rows = cursor.fetchall()
    if not rows:
        text = "📭 Логов действий нет."
    else:
        text = f"📋 <b>Последние действия</b>\n━━━━━━━━━━━━━━━━━━━━━━━━\n"
        for lid, aid, afn, act, tid, ts in rows:
            admin = get_user_link(aid, afn, show_id=False)
            dt = ts[:16].replace("T", " ") if ts else "—"
            text += f"<b>#{lid}</b> {dt}\n  {admin}\n  ⚡ {act}"
            if tid:
                text += f" → <code>{tid}</code>"
            text += "\n"
    await cb.message.edit_text(text, reply_markup=InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="◀️ Назад", callback_data="adm:main")]
    ]))
    await cb.answer()

# ============================================================
# ⏸ ТЕХ.РАБОТЫ (Заглушка)
# ============================================================

@router.callback_query(F.data == "adm:maintenance")
async def adm_maintenance(cb: CallbackQuery):
    if not is_owner(cb.from_user.id):
        return await cb.answer("❌ Только Владелец", show_alert=True)
    
    status = get_setting("maintenance", "0")
    new_status = "0" if status == "1" else "1"
    set_setting("maintenance", new_status)
    
    await cb.answer(f"Тех.работы: {'ВКЛ' if new_status == '1' else 'ВЫКЛ'}", show_alert=True)
    await adm_main_cb(cb)


# ============================================================
# 💡 ЗАГЛУШКИ ДЛЯ НЕДОПИСАННЫХ КНОПОК
# ============================================================

@router.callback_query(F.data.in_([
    "adm:users", "adm:search", "adm:economy", "adm:analytics", 
    "adm:leaderboard", "adm:mass_give", "adm:give_vip", "adm:settings", 
    "adm:clans", "adm:mining", "adm:deposits", "adm:post_channel", 
    "adm:ban", "adm:resets", "adm:backup", "adm:manage_admins", "adm:help"
]))
async def adm_wip_cb(cb: CallbackQuery):
    """Универсальный обработчик для кнопок, которые в разработке"""
    await cb.answer("🚧 Этот раздел находится в разработке!", show_alert=True)


# ============================================================
# 🧠 ОБРАБОТЧИК ТЕКСТА (САМАЯ ВАЖНАЯ ЧАСТЬ)
# ============================================================

@router.message(F.text, lambda m: m.from_user.id in admin_states)
async def admin_text_handler(message: Message):
    uid = message.from_user.id
    state = admin_states.get(uid, {})
    action = state.get("action")
    text = message.text.strip()

    if text == "/cancel":
        admin_states.pop(uid, None)
        await message.answer("❌ Отменено.", reply_markup=admin_panel_kb(uid))
        return

    # --- ВЫДАЧА ПРЕДМЕТА ---
    if action == "adm_give_item_target":
        target = find_user_by_identifier(text)
        if not target:
            await message.answer("❌ Игрок не найден. Попробуй ещё раз или /cancel")
            return
        
        admin_states[uid] = {
            "action": "adm_give_item_choose",
            "target_id": target["user_id"]
        }
        
        kb = []
        for case in CASE_TYPES.keys():
            kb.append([InlineKeyboardButton(text=f"📦 Кейс: {case}", callback_data=f"give:case:{case}")])
        for card in MINING_PRICES.keys():
            kb.append([InlineKeyboardButton(text=f"💻 Карта: {card}", callback_data=f"give:card:{card}")])
        for tool_key, tool_data in ROB_TOOLS.items():
            kb.append([InlineKeyboardButton(text=f"{tool_data['emoji']} {tool_data['name']}", callback_data=f"give:tool:{tool_key}")])
        
        kb.append([InlineKeyboardButton(text="❌ Отмена", callback_data="adm:main")])
        
        await message.answer(
            f"📦 Выдаём игроку: <b>{target['first_name']}</b> (<code>{target['user_id']}</code>)\n\n"
            f"Шаг 2/2: Выбери предмет:",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=kb)
        )
        return

    # --- СОЗДАНИЕ ПРОМОКОДА ---
    elif action == "adm_promo_create":
        parts = text.split()
        if len(parts) != 3 or not parts[1].isdigit() or not parts[2].isdigit():
            await message.answer("❌ Неверный формат. Нужно: <code>КОД СУММА КОЛИЧЕСТВО</code>\nПопробуй снова или /cancel")
            return
        
        code = parts[0].upper()
        amount = int(parts[1])
        uses = int(parts[2])
        
        try:
            with get_db() as conn:
                with conn.cursor() as cursor:
                    cursor.execute("INSERT INTO promocodes (code, amount, uses) VALUES (%s, %s, %s)", (code, amount, uses))
                    conn.commit()
            admin_states.pop(uid, None)
            await message.answer(f"✅ Промокод <code>{code}</code> на {format_balance(amount)} (х{uses}) создан!", reply_markup=admin_panel_kb(uid))
        except psycopg2.Error:
            await message.answer("❌ Ошибка: такой промокод уже существует или база недоступна.")
        return

    # --- РАССЫЛКА ---
    elif action == "adm_broadcast":
        admin_states.pop(uid, None)
        with get_db() as conn:
            with conn.cursor() as cursor:
                cursor.execute("SELECT user_id FROM users")
                users = cursor.fetchall()
        
        await message.answer("⏳ Рассылка запущена...", reply_markup=admin_panel_kb(uid))
        success = 0
        for u in users:
            try:
                await bot.send_message(u[0], text)
                success += 1
                await asyncio.sleep(0.05)
            except:
                pass
        await message.answer(f"✅ Рассылка завершена. Доставлено: {success}/{len(users)}")
        return


# ============================================================
# 🎒 ВЫДАЧА ПРЕДМЕТА (ФИНАЛЬНЫЙ ШАГ)
# ============================================================

@router.callback_query(F.data.startswith("give:"))
async def adm_give_item_process(cb: CallbackQuery):
    uid = cb.from_user.id
    state = admin_states.get(uid)
    
    if not state or state.get("action") != "adm_give_item_choose":
        await cb.answer("Сессия истекла, начните заново.", show_alert=True)
        return
        
    target_id = state["target_id"]
    parts = cb.data.split(":")
    item_category = parts[1]
    item_name = parts[2]
    
    try:
        with get_db() as conn:
            with conn.cursor() as cursor:
                if item_category == "case":
                    cursor.execute(
                        "INSERT INTO cases_inventory (user_id, case_type, count) VALUES (%s, %s, 1) "
                        "ON CONFLICT (user_id, case_type) DO UPDATE SET count = cases_inventory.count + 1",
                        (target_id, item_name))
                    item_text = f"Кейс «{item_name}»"
                elif item_category == "card":
                    cursor.execute(
                        "INSERT INTO miners (user_id, card_type, count) VALUES (%s, %s, 1) "
                        "ON CONFLICT (user_id, card_type) DO UPDATE SET count = miners.count + 1",
                        (target_id, item_name))
                    item_text = f"Видеокарта «{item_name}»"
                elif item_category == "tool":
                    cursor.execute(
                        "INSERT INTO rob_inventory (user_id, item_type, count) VALUES (%s, %s, 1) "
                        "ON CONFLICT (user_id, item_type) DO UPDATE SET count = rob_inventory.count + 1",
                        (target_id, item_name))
                    item_text = f"Инструмент «{ROB_TOOLS[item_name]['name']}»"
                conn.commit()
                
        log_admin_action(uid, f"give_item_{item_category}", target_id=target_id)
        admin_states.pop(uid, None)
        
        await cb.message.edit_text(
            f"✅ <b>Успешно выдано!</b>\n\n"
            f"👤 Игрок: <code>{target_id}</code>\n"
            f"🎁 Предмет: {item_text}",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="◀️ В админ-меню", callback_data="adm:main")]
            ])
        )
        await cb.answer("Выдано!")
        
    except Exception as e:
        logging.error(f"Ошибка выдачи предмета: {e}")
        await cb.answer("❌ Ошибка базы данных.", show_alert=True)

# ============================================================
# 🔐 СЕКРЕТНЫЕ КОМАНДЫ (только owner)
# ============================================================

@router.callback_query(F.data == "adm:secret")
async def adm_secret_cb(cb: CallbackQuery):
    if not is_owner(cb.from_user.id):
        return await cb.answer("❌ Только владелец", show_alert=True)
    text = (
        "🔐 <b>СЕКРЕТНЫЕ КОМАНДЫ (OWNER)</b>\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
        "⚡ <b>Экономика:</b>\n"
        "<code>/wipe @user</code> — обнулить баланс\n"
        "<code>/mass_pay СУММА</code> — выдать всем\n"
        "<code>/rate ЧИСЛО</code> — курс BTC (за 1 BTC)\n"
        "<code>/bonus_set СУММА</code> — сумма бонуса\n"
        "<code>/bonus_cd ЧАСЫ</code> — кулдаун бонуса\n"
        "<code>/ref_bonus СУММА</code> — реф. бонус\n\n"
        "⚡ <b>Админы:</b>\n"
        "<code>/setrank @user РАНГ</code> — быстрая выдача\n"
        "<code>/force_admin @user</code> — мгновенно moder\n\n"
        "⚡ <b>Кулдауны:</b>\n"
        "<code>/reset_cd @user</code> — сброс всех кулдаунов\n"
        "<code>/reset_cd all</code> — всем\n\n"
        "⚡ <b>Прочее:</b>\n"
        "<code>/bot_name НОВОЕ_ИМЯ</code> — имя бота\n"
        "<code>/currency СИМВОЛ</code> — валюта\n"
        "<code>/hard_reset @user</code> — полный сброс юзера\n"
        "<code>/silent on|off</code> — невидимый режим\n"
        "<code>/audit</code> — все действия админов (100)\n"
        "<code>/online</code> — статистика сейчас"
    )
    try:
        await cb.message.edit_text(text, reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="◀️ Назад", callback_data="adm:main")]
        ]))
    except:
        pass
    await cb.answer()


# ============================================================
# ⚙️ НАСТРОЙКИ (owner)
# ============================================================

@router.callback_query(F.data == "adm:settings")
async def adm_settings_cb(cb: CallbackQuery):
    if not is_owner(cb.from_user.id):
        return await cb.answer("❌ Только владелец", show_alert=True)
    bonus = get_setting("bonus_amount", "3000")
    cd = get_setting("bonus_cooldown", "8")
    ref = get_setting("referral_bonus", "75000")
    ref_limit = get_setting("referral_limit", "25")
    rate = get_setting("btc_rate", "1000000000")
    text = (
        f"⚙️ <b>НАСТРОЙКИ БОТА</b>\n━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
        f"🎁 Бонус: <b>{format_balance(bonus)}</b>\n"
        f"⏰ Кулдаун бонуса: <b>{cd} ч</b>\n"
        f"👥 Реф. бонус: <b>{format_balance(ref)}</b>\n"
        f"📊 Реф. лимит: <b>{ref_limit}</b>\n"
        f"💎 Курс BTC: <b>{format_balance(rate)}</b> за 1 BTC\n\n"
        f"<i>Меняй через /secret команды</i>"
    )
    try:
        await cb.message.edit_text(text, reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="◀️ Назад", callback_data="adm:main")]
        ]))
    except:
        pass
    await cb.answer()


# ============================================================
# ⚡ СЕКРЕТНЫЕ КОМАНДЫ OWNER
# ============================================================

@router.message(Command("wipe"))
async def cmd_wipe(message: Message):
    if not is_owner(message.from_user.id):
        return
    args = message.text.split()
    if len(args) < 2:
        return await message.answer("Использование: /wipe @user")
    target = find_user_by_identifier(args[1])
    if not target:
        return await message.answer("❌ Не найден")
    with get_db() as conn:
        with conn.cursor() as cursor:
            cursor.execute("UPDATE users SET balance = 0 WHERE user_id = %s", (target["user_id"],))
            conn.commit()
    await message.answer(f"✅ Баланс {get_user_link(target['user_id'], target['first_name'])} обнулён.")


@router.message(Command("mass_pay"))
async def cmd_mass_pay(message: Message):
    if not is_owner(message.from_user.id):
        return
    args = message.text.split()
    if len(args) < 2 or not args[1].isdigit():
        return await message.answer("Использование: /mass_pay СУММА")
    amount = int(args[1])
    if amount <= 0 or amount > 100_000_000:
        return await message.answer("❌ Сумма от 1 до 100 000 000")
    with get_db() as conn:
        with conn.cursor() as cursor:
            cursor.execute("SELECT COUNT(*) FROM users")
            total = cursor.fetchone()[0]
            cursor.execute("UPDATE users SET balance = balance + %s", (amount,))
            conn.commit()
    await message.answer(f"✅ Выдано {format_balance(amount)} × {total} юзеров")


@router.message(Command("rate"))
async def cmd_set_rate(message: Message):
    if not is_owner(message.from_user.id):
        return
    args = message.text.split()
    if len(args) < 2 or not args[1].isdigit():
        return await message.answer("Использование: /rate ЧИСЛО (цена 1 BTC в COINS)")
    set_setting("btc_rate", args[1])
    await message.answer(f"✅ Курс BTC: {format_balance(args[1])}")


@router.message(Command("bonus_set"))
async def cmd_bonus_set(message: Message):
    if not is_owner(message.from_user.id):
        return
    args = message.text.split()
    if len(args) < 2 or not args[1].isdigit():
        return await message.answer("Использование: /bonus_set СУММА")
    set_setting("bonus_amount", args[1])
    await message.answer(f"✅ Бонус: {format_balance(args[1])}")


@router.message(Command("bonus_cd"))
async def cmd_bonus_cd(message: Message):
    if not is_owner(message.from_user.id):
        return
    args = message.text.split()
    if len(args) < 2 or not args[1].isdigit():
        return await message.answer("Использование: /bonus_cd ЧАСЫ")
    set_setting("bonus_cooldown", args[1])
    await message.answer(f"✅ Кулдаун: {args[1]} ч")


@router.message(Command("ref_bonus"))
async def cmd_ref_bonus(message: Message):
    if not is_owner(message.from_user.id):
        return
    args = message.text.split()
    if len(args) < 2 or not args[1].isdigit():
        return await message.answer("Использование: /ref_bonus СУММА")
    set_setting("referral_bonus", args[1])
    await message.answer(f"✅ Реф. бонус: {format_balance(args[1])}")


@router.message(Command("setrank"))
async def cmd_setrank(message: Message):
    if not is_owner(message.from_user.id):
        return
    args = message.text.split()
    if len(args) < 3:
        return await message.answer("Использование: /setrank @user moder|spadmin|admin|head|owner")
    target = find_user_by_identifier(args[1])
    if not target:
        return await message.answer("❌ Не найден")
    new_rank = args[2].lower()
    if new_rank not in RANK_LEVELS:
        return await message.answer(f"❌ Ранг должен быть: {', '.join(RANK_LEVELS.keys())}")
    if target["user_id"] == ADMIN_ID and new_rank != "owner":
        return await message.answer("❌ Нельзя понизить себя")
    with get_db() as conn:
        with conn.cursor() as cursor:
            if new_rank == "user":
                cursor.execute("DELETE FROM admins WHERE user_id = %s", (target["user_id"],))
            else:
                cursor.execute(
                    "INSERT INTO admins (user_id, rank) VALUES (%s, %s) ON CONFLICT (user_id) DO UPDATE SET rank = EXCLUDED.rank",
                    (target["user_id"], new_rank))
            conn.commit()
    log_admin_action(message.from_user.id, f"setrank {new_rank}", target["user_id"])
    await message.answer(
        f"✅ {get_user_link(target['user_id'], target['first_name'], target.get('username'))} → {RANK_NAMES.get(new_rank, new_rank)}")


@router.message(Command("force_admin"))
async def cmd_force_admin(message: Message):
    if not is_owner(message.from_user.id):
        return
    args = message.text.split()
    if len(args) < 2:
        return await message.answer("Использование: /force_admin @user")
    target = find_user_by_identifier(args[1])
    if not target:
        return await message.answer("❌ Не найден")
    with get_db() as conn:
        with conn.cursor() as cursor:
            cursor.execute(
                "INSERT INTO admins (user_id, rank) VALUES (%s, 'moder') ON CONFLICT (user_id) DO NOTHING",
                (target["user_id"],))
            conn.commit()
    await message.answer(f"✅ {get_user_link(target['user_id'], target['first_name'])} теперь ⭐ Модератор")


@router.message(Command("reset_cd"))
async def cmd_reset_cd(message: Message):
    if not is_owner(message.from_user.id):
        return
    args = message.text.split()
    if len(args) < 2:
        return await message.answer("Использование: /reset_cd @user | all")
    if args[1].lower() == "all":
        with get_db() as conn:
            with conn.cursor() as cursor:
                cursor.execute("UPDATE users SET last_bonus = NULL, last_robbery = NULL, last_daily = NULL")
                cursor.execute("DELETE FROM rob_cooldown")
                conn.commit()
        await message.answer("✅ Все кулдауны сброшены у всех юзеров")
    else:
        target = find_user_by_identifier(args[1])
        if not target:
            return await message.answer("❌ Не найден")
        with get_db() as conn:
            with conn.cursor() as cursor:
                cursor.execute("UPDATE users SET last_bonus = NULL, last_robbery = NULL, last_daily = NULL WHERE user_id = %s",
                               (target["user_id"],))
                cursor.execute("DELETE FROM rob_cooldown WHERE user_id = %s", (target["user_id"],))
                conn.commit()
        await message.answer(f"✅ Кулдауны сброшены у {get_user_link(target['user_id'], target['first_name'])}")


@router.message(Command("bot_name"))
async def cmd_bot_name(message: Message):
    if not is_owner(message.from_user.id):
        return
    global BOT_NAME
    args = message.text.split(maxsplit=1)
    if len(args) < 2:
        return await message.answer("Использование: /bot_name НОВОЕ_ИМЯ")
    BOT_NAME = args[1].strip()[:32]
    set_setting("bot_name", BOT_NAME)
    await message.answer(f"✅ Имя бота: <b>{BOT_NAME}</b>")


@router.message(Command("currency"))
async def cmd_currency(message: Message):
    if not is_owner(message.from_user.id):
        return
    global CURRENCY
    args = message.text.split()
    if len(args) < 2:
        return await message.answer("Использование: /currency СИМВОЛ")
    CURRENCY = args[1].strip()[:10]
    set_setting("currency", CURRENCY)
    await message.answer(f"✅ Валюта: <b>{CURRENCY}</b>")


@router.message(Command("hard_reset"))
async def cmd_hard_reset(message: Message):
    if not is_owner(message.from_user.id):
        return
    args = message.text.split()
    if len(args) < 2:
        return await message.answer("Использование: /hard_reset @user")
    target = find_user_by_identifier(args[1])
    if not target:
        return await message.answer("❌ Не найден")
    uid = target["user_id"]
    with get_db() as conn:
        with conn.cursor() as cursor:
            cursor.execute("DELETE FROM cases_inventory WHERE user_id = %s", (uid,))
            cursor.execute("DELETE FROM miners WHERE user_id = %s", (uid,))
            cursor.execute("DELETE FROM mining_stats WHERE user_id = %s", (uid,))
            cursor.execute("DELETE FROM deposits WHERE user_id = %s", (uid,))
            cursor.execute("DELETE FROM clan_members WHERE user_id = %s", (uid,))
            cursor.execute("DELETE FROM rob_inventory WHERE user_id = %s", (uid,))
            cursor.execute("DELETE FROM rob_cooldown WHERE user_id = %s", (uid,))
            cursor.execute("""
                UPDATE users SET balance = 4000, btc = 0, vip_until = NULL,
                is_banned = FALSE, ban_until = NULL, games_played = 0, games_won = 0,
                referrer_id = NULL, referred_count = 0, referal_bonus_claimed = 0,
                last_bonus = NULL, last_daily = NULL, last_robbery = NULL,
                custom_nick = NULL, clan_id = NULL WHERE user_id = %s
            """, (uid,))
            conn.commit()
    await message.answer(f"✅ {get_user_link(uid, target['first_name'])} полностью сброшен.")


@router.message(Command("silent"))
async def cmd_silent(message: Message):
    if not is_owner(message.from_user.id):
        return
    args = message.text.split()
    if len(args) < 2 or args[1].lower() not in ["on", "off"]:
        return await message.answer("Использование: /silent on|off")
    set_setting(f"silent_{message.from_user.id}", "1" if args[1].lower() == "on" else "0")
    await message.answer(f"✅ Silent mode: <b>{args[1].lower()}</b>")


@router.message(Command("audit"))
async def cmd_audit(message: Message):
    if not is_owner(message.from_user.id):
        return
    with get_db() as conn:
        with conn.cursor() as cursor:
            cursor.execute("""
                SELECT l.id, l.admin_id, u.first_name, u.username,
                       l.action, l.target_id, l.amount, l.timestamp
                FROM admin_log l
                LEFT JOIN users u ON u.user_id = l.admin_id
                ORDER BY l.id DESC LIMIT 100
            """)
            rows = cursor.fetchall()
    if not rows:
        return await message.answer("📭 Логов нет.")
    text = f"📋 <b>ПОЛНЫЙ АУДИТ (100 последних)</b>\n━━━━━━━━━━━━━━━━━━━━━━━━\n"
    for lid, aid, afn, aun, act, tid, amt, ts in rows:
        admin = get_user_link(aid, afn, aun, show_id=True)
        dt = ts[:16].replace("T", " ") if ts else "—"
        text += f"<b>#{lid}</b> {dt} · {admin}\n  ⚡ {act}"
        if tid:
            text += f" → <code>{tid}</code>"
        if amt:
            text += f" ({format_balance(amt)})"
        text += "\n"
    # Разбиваем на 2 части если длинно
    if len(text) > 4000:
        text1 = text[:3900]
        text2 = text[3900:]
        await message.answer(text1)
        await message.answer(text2)
    else:
        await message.answer(text)


@router.message(Command("online"))
async def cmd_online(message: Message):
    if not is_owner(message.from_user.id):
        return
    with get_db() as conn:
        with conn.cursor() as cursor:
            cursor.execute("SELECT COUNT(*) FROM users WHERE last_bonus IS NOT NULL OR games_played > 0")
            active = cursor.fetchone()[0]
            cursor.execute("SELECT COUNT(*) FROM users")
            total = cursor.fetchone()[0]
    await message.answer(
        f"📊 <b>Активность</b>\n\n"
        f"👥 Всего: <b>{total}</b>\n"
        f"🔥 Активных (играли/бонус): <b>{active}</b>"
    )


# ============================================================
# 🔧 ОБРАБОТЧИК СОСТОЯНИЙ (ввод от админа)
# ============================================================

@router.message(F.text, lambda m: m.from_user.id in admin_states)
async def admin_state_handler(message: Message):
    uid = message.from_user.id
    state = admin_states.get(uid)
    if not state:
        return
    action = state.get("action", "")
    text = (message.text or "").strip()

    # Пропускаем bank_* и clan_* (их обрабатывают другие хендлеры)
    if action.startswith("bank_") or action.startswith("clan_"):
        return

    if text == "/cancel":
        admin_states.pop(uid, None)
        await message.answer("❌ Отменено.", reply_markup=admin_panel_kb(uid))
        return

    # --- profile ---
    if action == "adm_user_info":
        target = find_user_by_identifier(text)
        admin_states.pop(uid, None)
        if not target:
            return await message.answer("❌ Не найден", reply_markup=admin_panel_kb(uid))
        await message.answer(_render_user_profile(target), reply_markup=admin_panel_kb(uid))
        return

    # --- give_money ---
    if action == "adm_give_money_target":
        target = find_user_by_identifier(text)
        if not target:
            return await message.answer("❌ Не найден. Попробуй /cancel")
        state["action"] = "adm_give_money_amount"
        state["target_id"] = target["user_id"]
        state["target_name"] = target["first_name"]
        admin_states[uid] = state
        await message.answer(
            f"👤 Получатель: {get_user_link(target['user_id'], target['first_name'], target.get('username'))}\n\n"
            f"Шаг 2/2 — введи сумму (или /cancel):"
        )
        return

    if action == "adm_give_money_amount":
        if not text.lstrip("-").isdigit():
            return await message.answer("❌ Введи число или /cancel")
        amount = int(text)
        lvl = get_rank_level(uid)
        if lvl < 3 and amount > 10_000_000_000:
            return await message.answer("❌ Макс 10 млрд для твоего ранга.")
        target_id = state["target_id"]
        update_balance(target_id, amount)
        log_admin_action(uid, f"give_money +{amount}", target_id, amount)
        admin_states.pop(uid, None)
        await message.answer(
            f"✅ Выдано <b>{format_balance(amount)}</b> → {get_user_link(target_id, state.get('target_name'))}",
            reply_markup=admin_panel_kb(uid))
        return

    # --- take_money ---
    if action == "adm_take_money_target":
        target = find_user_by_identifier(text)
        if not target:
            return await message.answer("❌ Не найден. /cancel")
        state["action"] = "adm_take_money_amount"
        state["target_id"] = target["user_id"]
        state["target_name"] = target["first_name"]
        state["target_balance"] = target["balance"]
        admin_states[uid] = state
        await message.answer(
            f"👤 {get_user_link(target['user_id'], target['first_name'])}\n"
            f"💰 Баланс: {format_balance(target['balance'])}\n\n"
            f"Введи сумму для списания (/cancel):"
        )
        return

    if action == "adm_take_money_amount":
        if not text.isdigit():
            return await message.answer("❌ Введи число или /cancel")
        amount = int(text)
        if amount <= 0:
            return await message.answer("❌ Сумма > 0")
        target_id = state["target_id"]
        cur = state.get("target_balance", 0)
        take = min(amount, cur)
        update_balance(target_id, -take)
        log_admin_action(uid, f"take_money -{take}", target_id, take)
        admin_states.pop(uid, None)
        await message.answer(
            f"✅ Списано <b>{format_balance(take)}</b> у {get_user_link(target_id, state.get('target_name'))}",
            reply_markup=admin_panel_kb(uid))
        return

    # --- ban ---
    if action == "adm_ban_target":
        target = find_user_by_identifier(text)
        if not target:
            return await message.answer("❌ Не найден. /cancel")
        if get_rank_level(target["user_id"]) >= get_rank_level(uid):
            return await message.answer("❌ Нельзя банить равного/старшего")
        state["action"] = "adm_ban_hours"
        state["target_id"] = target["user_id"]
        state["target_name"] = target["first_name"]
        admin_states[uid] = state
        await message.answer(
            f"👤 {get_user_link(target['user_id'], target['first_name'])}\n\n"
            f"Шаг 2/3 — введи часы бана (0 = навсегда, /cancel):"
        )
        return

    if action == "adm_ban_hours":
        if not text.isdigit():
            return await message.answer("❌ Введи число (часы) или /cancel")
        hours = int(text)
        state["action"] = "adm_ban_reason"
        state["hours"] = hours
        admin_states[uid] = state
        await message.answer("Шаг 3/3 — причина бана (или /skip):")
        return

    if action == "adm_ban_reason":
        reason = "" if text == "/skip" else text
        target_id = state["target_id"]
        hours = state.get("hours", 0)
        ban_until = None if hours == 0 else (datetime.now() + timedelta(hours=hours))
        with get_db() as conn:
            with conn.cursor() as cursor:
                cursor.execute("UPDATE users SET is_banned = TRUE, ban_until = %s WHERE user_id = %s",
                               (ban_until, target_id))
                conn.commit()
        log_admin_action(uid, f"ban {hours}h: {reason}", target_id)
        admin_states.pop(uid, None)
        until_str = "навсегда" if hours == 0 else f"до {ban_until.strftime('%d.%m.%Y %H:%M')}"
        await message.answer(
            f"🔨 {get_user_link(target_id, state.get('target_name'))} забанен {until_str}\n💬 {reason}",
            reply_markup=admin_panel_kb(uid))
        return

    # --- unban ---
    if action == "adm_unban_target":
        target = find_user_by_identifier(text)
        if not target:
            return await message.answer("❌ Не найден. /cancel")
        with get_db() as conn:
            with conn.cursor() as cursor:
                cursor.execute("UPDATE users SET is_banned = FALSE, ban_until = NULL WHERE user_id = %s",
                               (target["user_id"],))
                conn.commit()
        log_admin_action(uid, "unban", target["user_id"])
        admin_states.pop(uid, None)
        await message.answer(
            f"✅ {get_user_link(target['user_id'], target['first_name'])} разбанен",
            reply_markup=admin_panel_kb(uid))
        return

    # --- promo create ---
    if action == "adm_promo_create":
        parts = text.split()
        if len(parts) < 3 or not parts[1].isdigit() or not parts[2].isdigit():
            return await message.answer("❌ Формат: КОД СУММА КОЛИЧЕСТВО\nПример: PROMO 1000000 100")
        code, amt, uses = parts[0].upper(), int(parts[1]), int(parts[2])
        if get_promo(code):
            return await message.answer("❌ Такой код уже есть")
        with get_db() as conn:
            with conn.cursor() as cursor:
                cursor.execute("INSERT INTO promocodes (code, amount, uses) VALUES (%s, %s, %s)",
                               (code, amt, uses))
                conn.commit()
        log_admin_action(uid, f"promo_create {code}", 0, amt)
        admin_states.pop(uid, None)
        await message.answer(
            f"✅ Промокод создан:\n<code>{code}</code> — {format_balance(amt)} × {uses}",
            reply_markup=admin_panel_kb(uid))
        return

    if action == "adm_promo_delete":
        if not get_promo(text.upper()):
            return await message.answer("❌ Промокод не найден")
        delete_promo(text.upper())
        log_admin_action(uid, f"promo_delete {text.upper()}")
        admin_states.pop(uid, None)
        await message.answer(f"✅ Промокод <code>{text.upper()}</code> удалён",
                             reply_markup=admin_panel_kb(uid))
        return

    # --- give case ---
    if action == "adm_give_case_target":
        target = find_user_by_identifier(text)
        if not target:
            return await message.answer("❌ Не найден. /cancel")
        state["action"] = "adm_give_case_data"
        state["target_id"] = target["user_id"]
        state["target_name"] = target["first_name"]
        admin_states[uid] = state
        types = ", ".join(CASE_TYPES.keys())
        await message.answer(
            f"👤 {get_user_link(target['user_id'], target['first_name'])}\n\n"
            f"Формат: <code>ТИП КОЛИЧЕСТВО</code>\n"
            f"Доступно: {types}\n"
            f"Пример: <code>золотой 5</code>"
        )
        return

    if action == "adm_give_case_data":
        parts = text.split()
        if len(parts) < 2 or not parts[1].isdigit():
            return await message.answer("❌ Формат: тип количество")
        ctype = parts[0].lower()
        count = int(parts[1])
        if ctype not in CASE_TYPES or count <= 0:
            return await message.answer("❌ Неверный тип/количество")
        target_id = state["target_id"]
        with get_db() as conn:
            with conn.cursor() as cursor:
                cursor.execute(
                    "INSERT INTO cases_inventory (user_id, case_type, count) VALUES (%s, %s, %s) "
                    "ON CONFLICT (user_id, case_type) DO UPDATE SET count = cases_inventory.count + EXCLUDED.count",
                    (target_id, ctype, count))
                conn.commit()
        log_admin_action(uid, f"give_case {ctype} x{count}", target_id)
        admin_states.pop(uid, None)
        await message.answer(
            f"✅ Выдано {CASE_TYPES[ctype]['emoji']} <b>{ctype}</b> ×{count} → {get_user_link(target_id, state.get('target_name'))}",
            reply_markup=admin_panel_kb(uid))
        return

    # --- give vip ---
    if action == "adm_give_vip_target":
        target = find_user_by_identifier(text)
        if not target:
            return await message.answer("❌ Не найден. /cancel")
        state["action"] = "adm_give_vip_hours"
        state["target_id"] = target["user_id"]
        state["target_name"] = target["first_name"]
        admin_states[uid] = state
        await message.answer(
            f"👤 {get_user_link(target['user_id'], target['first_name'])}\n\n"
            f"Введи количество часов VIP:"
        )
        return

    if action == "adm_give_vip_hours":
        if not text.isdigit():
            return await message.answer("❌ Введи число")
        hours = int(text)
        if hours <= 0:
            return await message.answer("❌ Часы > 0")
        target_id = state["target_id"]
        user = get_user(target_id)
        cur = user.get("vip_until")
        new_until = (cur + timedelta(hours=hours)) if (cur and cur > datetime.now()) else (datetime.now() + timedelta(hours=hours))
        with get_db() as conn:
            with conn.cursor() as cursor:
                cursor.execute("UPDATE users SET vip_until = %s WHERE user_id = %s", (new_until, target_id))
                conn.commit()
        log_admin_action(uid, f"give_vip {hours}h", target_id)
        admin_states.pop(uid, None)
        await message.answer(
            f"✅ VIP +{hours}ч → {get_user_link(target_id, state.get('target_name'))}\n"
            f"📅 До {new_until.strftime('%d.%m.%Y %H:%M')}",
            reply_markup=admin_panel_kb(uid))
        return

    # --- give btc ---
    if action == "adm_give_btc_target":
        target = find_user_by_identifier(text)
        if not target:
            return await message.answer("❌ Не найден. /cancel")
        state["action"] = "adm_give_btc_amount"
        state["target_id"] = target["user_id"]
        state["target_name"] = target["first_name"]
        admin_states[uid] = state
        await message.answer(f"👤 {get_user_link(target['user_id'], target['first_name'])}\n\nВведи кол-во BTC:")
        return

    if action == "adm_give_btc_amount":
        try:
            amount = float(text)
        except:
            return await message.answer("❌ Введи число")
        if amount <= 0:
            return await message.answer("❌ > 0")
        target_id = state["target_id"]
        with get_db() as conn:
            with conn.cursor() as cursor:
                cursor.execute("UPDATE users SET btc = btc + %s WHERE user_id = %s", (amount, target_id))
                conn.commit()
        log_admin_action(uid, f"give_btc {amount}")
        admin_states.pop(uid, None)
        await message.answer(
            f"✅ Выдано <b>{amount:.8f} BTC</b> → {get_user_link(target_id, state.get('target_name'))}",
            reply_markup=admin_panel_kb(uid))
        return

    # --- add admin ---
    if action == "adm_add_admin_target":
        target = find_user_by_identifier(text)
        if not target:
            return await message.answer("❌ Не найден. /cancel")
        state["action"] = "adm_add_admin_rank"
        state["target_id"] = target["user_id"]
        state["target_name"] = target["first_name"]
        admin_states[uid] = state
        await message.answer(
            f"👤 {get_user_link(target['user_id'], target['first_name'])}\n\n"
            f"Введи ранг: moder / spadmin / admin"
        )
        return

    if action == "adm_add_admin_rank":
        rank = text.lower()
        if rank not in ["moder", "spadmin", "admin"]:
            return await message.answer("❌ Ранг: moder / spadmin / admin")
        my_lvl = get_rank_level(uid)
        new_lvl = RANK_LEVELS.get(rank, 0)
        if new_lvl >= my_lvl:
            return await message.answer("❌ Нельзя назначить равного/выше себя")
        target_id = state["target_id"]
        with get_db() as conn:
            with conn.cursor() as cursor:
                cursor.execute(
                    "INSERT INTO admins (user_id, rank) VALUES (%s, %s) ON CONFLICT (user_id) DO UPDATE SET rank = EXCLUDED.rank",
                    (target_id, rank))
                conn.commit()
        log_admin_action(uid, f"add_admin {rank}", target_id)
        admin_states.pop(uid, None)
        await message.answer(
            f"✅ {get_user_link(target_id, state.get('target_name'))} → {RANK_NAMES.get(rank, rank)}",
            reply_markup=admin_panel_kb(uid))
        return

    # --- remove admin ---
    if action == "adm_remove_admin_target":
        target = find_user_by_identifier(text)
        if not target:
            return await message.answer("❌ Не найден. /cancel")
        if not can_manage_admin(uid, target["user_id"]):
            return await message.answer("❌ Нельзя снять этого админа (равен/выше/owner)")
        with get_db() as conn:
            with conn.cursor() as cursor:
                cursor.execute("DELETE FROM admins WHERE user_id = %s", (target["user_id"],))
                conn.commit()
        log_admin_action(uid, "remove_admin", target["user_id"])
        admin_states.pop(uid, None)
        await message.answer(
            f"✅ {get_user_link(target['user_id'], target['first_name'])} снят с админов",
            reply_markup=admin_panel_kb(uid))
        return

    # --- broadcast ---
    if action == "adm_broadcast":
        broadcast_text = text
        admin_states.pop(uid, None)
        with get_db() as conn:
            with conn.cursor() as cursor:
                cursor.execute("SELECT user_id FROM users")
                uids = [r[0] for r in cursor.fetchall()]
        await message.answer(f"📢 Рассылка на {len(uids)} юзеров началась...")
        sent = 0
        failed = 0
        for u in uids:
            try:
                await bot.send_message(u, broadcast_text)
                sent += 1
            except Exception:
                failed += 1
            await asyncio.sleep(0.05)
        log_admin_action(uid, f"broadcast sent={sent}")
        await message.answer(
            f"✅ <b>Рассылка завершена</b>\n\n"
            f"📤 Доставлено: <b>{sent}</b>\n"
            f"❌ Ошибок: <b>{failed}</b>",
            reply_markup=admin_panel_kb(uid))
        return


# ============================================================
# 🚫 ПРОВЕРКА БАНА (добавить в начало других хендлеров)
# ============================================================

# Если хочешь блокировать забаненных юзеров полностью, раскомментируй и адаптируй
# @router.message(Command("start"))
# async def cmd_start_banned_check(message: Message):
#     ...


# ============================================================
# 🌐 ВЕБ-СЕРВЕР ДЛЯ RENDER (обязательно для Web Service)
# ============================================================
from aiohttp import web

async def handle_ping(request):
    return web.Response(text="MINT bot is running!")

async def main():
    # 1. Запускаем веб-сервер, чтобы Render видел открытый порт
    app = web.Application()
    app.router.add_get("/", handle_ping)
    runner = web.AppRunner(app)
    await runner.setup()
    
    # Render передает порт через переменную PORT
    port = int(os.getenv("PORT", 10000))
    site = web.TCPSite(runner, "0.0.0.0", port)
    await site.start()
    print(f"Веб-сервер запущен на порту {port}")

    # 2. Запускаем самого бота
    dp.include_router(router)
    await bot.delete_webhook(drop_pending_updates=True)
    print("Бот запущен!")
    await dp.start_polling(bot)

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        print("Бот остановлен.")

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

import os
from contextlib import asynccontextmanager

import aiosqlite
from dotenv import load_dotenv

load_dotenv()
DB_PATH = os.getenv("DB_PATH", "gifts.db")

SCHEMA = """
CREATE TABLE IF NOT EXISTS gifts (
    name TEXT PRIMARY KEY,
    gift_id TEXT,
    released_at INTEGER,
    upgradable INTEGER
);
CREATE TABLE IF NOT EXISTS models (
    gift TEXT NOT NULL,
    name TEXT NOT NULL,
    rarity_permille REAL,
    PRIMARY KEY (gift, name)
);
-- floor цена модели на маркете (TON), история снимков
CREATE TABLE IF NOT EXISTS floors (
    ts INTEGER NOT NULL,
    gift TEXT NOT NULL,
    model TEXT NOT NULL,       -- '' = floor по всей коллекции
    market TEXT NOT NULL,
    price_ton REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_floors ON floors (gift, model, ts);
CREATE UNIQUE INDEX IF NOT EXISTS ux_floors ON floors (gift, model, market, ts);
-- тираж подарков (availability_total из открытого датасета)
CREATE TABLE IF NOT EXISTS supply (
    gift_id TEXT PRIMARY KEY,
    total INTEGER NOT NULL
);
-- реальные продажи (публичный канал уведомлений о продажах)
CREATE TABLE IF NOT EXISTS sales (
    id INTEGER PRIMARY KEY,    -- номер поста в канале
    ts INTEGER NOT NULL,
    gift TEXT NOT NULL,
    num INTEGER,
    model TEXT,
    symbol TEXT,
    backdrop TEXT,
    price_ton REAL NOT NULL,
    source TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_sales_model ON sales (gift, model, ts);
CREATE INDEX IF NOT EXISTS ix_sales_ts ON sales (ts);
-- общая капитализация (снимки)
CREATE TABLE IF NOT EXISTS marketcap (
    ts INTEGER PRIMARY KEY,
    usd REAL NOT NULL,
    ton REAL NOT NULL
);
-- живые точки капитализации (раз в 15 минут), считаются по тем же ценам, что видит пользователь
CREATE TABLE IF NOT EXISTS cap_live (
    ts INTEGER PRIMARY KEY,
    ton REAL NOT NULL,
    usd REAL NOT NULL
);
-- цены подарка на разных маркетах (из Gifts_Details датасета), последняя версия
CREATE TABLE IF NOT EXISTS market_prices (
    gift TEXT PRIMARY KEY,
    portals REAL,
    mrkt REAL,
    getgems REAL,
    ts INTEGER
);
-- сводка Peek.tg (открытый запрос, редко): цена, изменение за сутки, число улучшенных (issued) и тираж (total)
CREATE TABLE IF NOT EXISTS peek (
    gift TEXT PRIMARY KEY,
    price REAL,
    change REAL,
    issued INTEGER,
    total INTEGER,
    ts INTEGER
);
-- данные пользователей мини-приложения (id из подписанного Telegram initData)
CREATE TABLE IF NOT EXISTS users (
    tg_id INTEGER PRIMARY KEY,
    first_ts INTEGER NOT NULL,
    last_ts INTEGER NOT NULL
);
-- избранное: kind = gift | model | set (для set в gift лежит id сета)
CREATE TABLE IF NOT EXISTS favs (
    tg_id INTEGER NOT NULL,
    kind TEXT NOT NULL,
    gift TEXT NOT NULL,
    model TEXT NOT NULL DEFAULT '',
    ts INTEGER NOT NULL,
    PRIMARY KEY (tg_id, kind, gift, model)
);
-- портфель: сколько штук подарка (или конкретной модели) у пользователя
CREATE TABLE IF NOT EXISTS portfolio (
    tg_id INTEGER NOT NULL,
    gift TEXT NOT NULL,
    model TEXT NOT NULL DEFAULT '',
    qty REAL NOT NULL,
    PRIMARY KEY (tg_id, gift, model)
);
-- уведомления о цене: cond = below | above | change (изменение за сутки в % по модулю)
CREATE TABLE IF NOT EXISTS alerts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    tg_id INTEGER NOT NULL,
    gift TEXT NOT NULL,
    model TEXT NOT NULL DEFAULT '',
    cond TEXT NOT NULL,
    value REAL NOT NULL,
    active INTEGER NOT NULL DEFAULT 1,
    last_fire INTEGER NOT NULL DEFAULT 0
);
-- точное число улучшенных подарков коллекции: наибольший существующий номер на публичных страницах t.me/nft/<Имя>-<номер>
CREATE TABLE IF NOT EXISTS issued_exact (
    gift TEXT PRIMARY KEY,
    issued INTEGER NOT NULL,
    ts INTEGER NOT NULL
);
-- фоны подарков (changes.tg): цвета и средняя редкость по всем коллекциям, где фон встречается
CREATE TABLE IF NOT EXISTS backdrops (
    name TEXT PRIMARY KEY,
    center TEXT, edge TEXT, pattern TEXT, text TEXT,
    rarity_avg REAL,
    gifts INTEGER
);
-- цена с учётом фона: самый дешёвый лот той же модели и фона (peek.tg); хранится, чтобы не теряться при перезапуске сервера
CREATE TABLE IF NOT EXISTS bd_prices (
    gift TEXT NOT NULL, model TEXT NOT NULL, backdrop TEXT NOT NULL,
    price REAL, ts INTEGER NOT NULL,
    PRIMARY KEY (gift, model, backdrop)
);
-- история донатов: Stars (из бота) и переводы GRAM/USDT на кошелёк (из tonapi)
CREATE TABLE IF NOT EXISTS donations (
    id TEXT PRIMARY KEY,
    ts INTEGER NOT NULL,
    kind TEXT NOT NULL,          -- stars | ton | usdt
    who TEXT NOT NULL,           -- @username / имя / короткий адрес кошелька
    amount REAL NOT NULL
);
-- курс TON/GRAM к USD
CREATE TABLE IF NOT EXISTS rates (
    ts INTEGER PRIMARY KEY,
    ton_usd REAL NOT NULL
);
"""


@asynccontextmanager
async def connect():
    async with aiosqlite.connect(DB_PATH, timeout=30) as conn:
        conn.row_factory = aiosqlite.Row
        # WAL: читатели не блокируют писателя; busy_timeout: при занятой базе ждём, а не падаем «database is locked»
        await conn.execute("PRAGMA busy_timeout=30000")
        await conn.execute("PRAGMA journal_mode=WAL")
        await conn.execute("PRAGMA synchronous=NORMAL")
        yield conn


async def init() -> None:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.executescript(SCHEMA)
        try:  # короткое сообщение к донату (добавлено позже)
            await db.execute("ALTER TABLE donations ADD COLUMN msg TEXT")
        except Exception:
            pass
        try:  # язык пользователя для уведомлений (добавлено позже)
            await db.execute("ALTER TABLE users ADD COLUMN lang TEXT")
        except Exception:
            pass
        await db.commit()

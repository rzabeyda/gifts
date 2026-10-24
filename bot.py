import asyncio
import httpx
import os
import re
import sqlite3
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

from aiogram import Bot, Dispatcher
from aiogram.filters import Command, CommandObject
from aiogram import F
from aiogram.types import (BotCommand, BotCommandScopeChat, FSInputFile, InlineKeyboardButton, InlineKeyboardMarkup,
                           MenuButtonWebApp, Message, PreCheckoutQuery, ReplyKeyboardRemove, WebAppInfo)
from dotenv import load_dotenv

load_dotenv()
WEBAPP_URL = os.getenv("WEBAPP_URL", "https://gifts.zabeyda.lol")
OWNER_ID = int(os.getenv("OWNER_ID", "7308147004"))
DB_PATH = os.getenv("DB_PATH", "gifts.db")
WELCOME = Path(__file__).parent / "welcome.jpg"  # картинка из pics/gift.jpg
TZ = timezone(timedelta(hours=3))  # дни считаем по московскому времени
SHARE_URL = "https://t.me/share/url?url=https%3A//t.me/gift20bot&text=" + "%D0%92%D1%81%D1%91%20%D0%BF%D1%80%D0%BE%20Telegram%20Gifts%20%D0%B2%20%D0%BE%D0%B4%D0%BD%D0%BE%D0%B9%20%D0%B0%D0%BF%D0%BA%D0%B5"
SHARE_URL_EN = "https://t.me/share/url?url=https%3A//t.me/gift20bot&text=Everything%20about%20Telegram%20Gifts%20in%20one%20app"
dp = Dispatcher()


def record_user(uid: int) -> bool:
    """Записывает пользователя в общую базу; True, если он новый."""
    try:
        con = sqlite3.connect(DB_PATH, timeout=10)
        cur = con.execute("INSERT OR IGNORE INTO users (tg_id, first_ts, last_ts) VALUES (?,?,?)", (uid, int(time.time()), int(time.time())))
        con.commit()
        new = cur.rowcount > 0
        con.close()
        return new
    except Exception:
        return False


def admin_stats() -> str:
    """Статистика для владельца: всего пользователей и новые по дням (по первому визиту)."""
    con = sqlite3.connect(DB_PATH, timeout=10)
    total = con.execute("SELECT COUNT(*) FROM users").fetchone()[0]
    today0 = datetime.now(TZ).replace(hour=0, minute=0, second=0, microsecond=0)
    t0 = int(today0.timestamp())

    def between(a: int, b: int) -> int:
        return con.execute("SELECT COUNT(*) FROM users WHERE first_ts >= ? AND first_ts < ?", (a, b)).fetchone()[0]

    today, yday, before = between(t0, t0 + 86400), between(t0 - 86400, t0), between(t0 - 2 * 86400, t0 - 86400)
    con.close()
    return f"Юзеры: {total}\n\nСегодня: {today}\nВчера: {yday}\nПозавчера: {before}"


@dp.message(Command("start"))
async def start(m: Message, command: CommandObject):
    # ссылка «Поделиться»: /start g_plush_pepe открывает приложение сразу на нужном экране
    arg = (command.args or "").strip()
    url = WEBAPP_URL + ("?s=" + arg if arg and re.fullmatch(r"[A-Za-z0-9_\-]{1,64}", arg) else "")
    kb = InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text="Open", web_app=WebAppInfo(url=url)),
                          InlineKeyboardButton(text="Share", url=SHARE_URL_EN)]]
    )
    caption = "Telegram NFT Gifts overview"
    if WELCOME.exists():
        await m.answer_photo(FSInputFile(WELCOME), caption=caption, reply_markup=kb)
    else:
        await m.answer(caption, reply_markup=kb)
    if m.from_user.id == OWNER_ID:
        await m.answer("/admin", reply_markup=ReplyKeyboardRemove())  # убираем прежнюю кнопку Admin у владельца
    elif record_user(m.from_user.id):  # новый пользователь: сообщение владельцу
        who = f"@{m.from_user.username}" if m.from_user.username else (m.from_user.full_name or str(m.from_user.id))
        try:
            await m.bot.send_message(OWNER_ID, f"Новый юзер {who}")
        except Exception:
            pass


@dp.message(Command("digest"))
async def digest(m: Message):
    """Включает или выключает ежедневную сводку по избранному (18:00 МСК)."""
    con = sqlite3.connect(DB_PATH, timeout=10)
    con.execute("INSERT OR IGNORE INTO users (tg_id, first_ts, last_ts) VALUES (?,?,?)", (m.from_user.id, int(time.time()), int(time.time())))
    row = con.execute("SELECT digest FROM users WHERE tg_id = ?", (m.from_user.id,)).fetchone()
    on = not (row and row[0])
    con.execute("UPDATE users SET digest = ? WHERE tg_id = ?", (1 if on else 0, m.from_user.id))
    con.commit(); con.close()
    await m.answer("Сводка по избранному (18:00 МСК) и сообщения о новых подарках включены" if on else "Сводка выключена. Включить снова: /digest")


@dp.message(Command("admin"))
async def admin(m: Message):
    if m.from_user.id != OWNER_ID:
        return
    await m.answer(admin_stats(), reply_markup=ReplyKeyboardRemove())


@dp.pre_checkout_query()
async def pre_checkout(q: PreCheckoutQuery):
    await q.answer(ok=True)  # доната принимаем всегда


@dp.message(F.successful_payment)
async def paid(m: Message):
    sp = m.successful_payment
    try:  # записываем донат в историю
        who = f"@{m.from_user.username}" if m.from_user.username else (m.from_user.full_name or "аноним")
        con = sqlite3.connect(DB_PATH, timeout=10)
        msg = (sp.invoice_payload[2:] if (sp.invoice_payload or "").startswith("d:") else "")[:50] or None
        con.execute("INSERT OR IGNORE INTO donations (id, ts, kind, who, amount, msg) VALUES (?,?,?,?,?,?)", (f"stars:{sp.telegram_payment_charge_id}", int(time.time()), "stars", who, float(sp.total_amount), msg))
        con.commit(); con.close()
        await m.bot.send_message(OWNER_ID, f"Донат {sp.total_amount} Stars от {who}" + (chr(10) + msg if msg else ""))
    except Exception:
        pass
    await m.answer("Спасибо за поддержку! / Thank you for your support!")


_last_reply: dict[int, float] = {}


def _norm(s: str) -> str:
    return re.sub(r"[^a-z0-9]", "", s.lower())


async def find_gifts(query: str) -> list[dict]:
    """Коллекции, подходящие под запрос: точное имя, потом начало, потом вхождение (в порядке капитализации)."""
    key = os.getenv("DEV_API_KEY", "")
    async with httpx.AsyncClient(timeout=15) as c:
        r = await c.get("http://127.0.0.1:8030/api/gifts", headers={"X-Dev-Key": key})
    rows = sorted((g for g in r.json() if g.get("floor_ton")), key=lambda g: -(g.get("cap_ton") or 0))
    qn = _norm(query)
    if not qn:
        return []
    exact = [g for g in rows if _norm(g["name"]) == qn]
    start = [g for g in rows if _norm(g["name"]).startswith(qn) and g not in exact]
    word = [g for g in rows if g not in exact and g not in start and any(_norm(w).startswith(qn) for w in g["name"].split())]
    part = [g for g in rows if g not in exact and g not in start and g not in word and qn in _norm(g["name"])]
    return exact + start + word + part


def _pct(v) -> str:
    return "—" if v is None else f"{v:+.1f}%".replace("-", "−")


@dp.message(Command("gram"))
async def gram_rate(m: Message):
    """Курс GRAM (TON) в долларах одной строкой."""
    try:
        async with httpx.AsyncClient(timeout=15) as c:
            r = await c.get("http://127.0.0.1:8030/api/marketcap?days=1", headers={"X-Dev-Key": os.getenv("DEV_API_KEY", "")})
        rate = r.json().get("ton_usd")
    except Exception:
        return
    if rate:
        await m.answer(f"GRAM: ${rate:.2f}")


@dp.message(F.text.startswith("/"))
async def gift_by_name(m: Message):
    """/pepe или /snoop dogg — цена коллекции прямо в чате."""
    text = re.sub(r"@\w+", "", m.text[1:], count=1).strip() if "@" in m.text.split()[0] else m.text[1:].strip()
    if not text:
        return
    group = m.chat.type != "private"
    if group:  # в группах чужие команды (/ban, /warn ...) не трогаем и не чаще раза в 15 секунд на чат
        if time.time() - _last_reply.get(m.chat.id, 0) < 15:
            return
    try:
        found = await find_gifts(text)
    except Exception:
        return
    con = sqlite3.connect(DB_PATH, timeout=10)
    row = con.execute("SELECT lang FROM users WHERE tg_id = ?", (m.from_user.id,)).fetchone()
    con.close()
    ru = (row[0] if row and row[0] else "ru") == "ru"
    if not found and group:
        return
    if group:
        _last_reply[m.chat.id] = time.time()
    if not found:
        await m.answer("Не нашёл такой подарок. Попробуйте, например: /pepe" if ru else "Gift not found. Try e.g. /pepe")
        return
    g, nl = found[0], chr(10)
    ch = g.get("changes") or {}
    price = g["floor_ton"]
    p = f"{price:,.0f}".replace(",", " ") if price >= 100 else f"{price:g}"
    lines = [f"{g['name']} (/{_norm(text)})", ("Цена: " if ru else "Price: ") + p + " GRAM",
             ("Сутки: " if ru else "24h: ") + _pct(ch.get("1d")), ("Неделя: " if ru else "7d: ") + _pct(ch.get("1w"))]
    url = WEBAPP_URL + "?s=g_" + re.sub(r"[^a-z ]", "", g["name"].lower()).strip().replace(" ", "_")
    if group:  # в группах без кнопок: текст и адрес бота внизу
        await m.answer(nl.join(lines + [nl + "@gift20bot"]))
        return
    kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="Open", web_app=WebAppInfo(url=url)),
                                                InlineKeyboardButton(text="Поделиться" if ru else "Share", url=SHARE_URL)]])
    await m.answer(nl.join(lines), reply_markup=kb)


async def daily_report(bot: Bot):
    """Каждый день в 19:00 (МСК) присылает владельцу статистику, как команда /admin."""
    while True:
        now = datetime.now(TZ)
        nxt = now.replace(hour=19, minute=0, second=0, microsecond=0)
        if nxt <= now:
            nxt += timedelta(days=1)
        await asyncio.sleep((nxt - now).total_seconds())
        try:
            await bot.send_message(OWNER_ID, admin_stats())
        except Exception:
            pass


async def main():
    bot = Bot(os.environ["BOT_TOKEN"])
    asyncio.create_task(daily_report(bot))
    await bot.set_chat_menu_button(menu_button=MenuButtonWebApp(text="Open", web_app=WebAppInfo(url=WEBAPP_URL)))
    try:  # команда /admin в меню только у владельца
        await bot.set_my_commands([BotCommand(command="start", description="Open"), BotCommand(command="admin", description="Admin")],
                                  scope=BotCommandScopeChat(chat_id=OWNER_ID))
    except Exception:
        pass
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())

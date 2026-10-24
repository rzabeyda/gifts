import asyncio
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
        inline_keyboard=[[InlineKeyboardButton(text="Open", web_app=WebAppInfo(url=url))]]
    )
    caption = "Обзор Telegram NFT Подарков"
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

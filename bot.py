import asyncio
import httpx
import os
import re
import sqlite3
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

from aiogram import Bot, Dispatcher, BaseMiddleware
import modchat
from aiogram.filters import Command, CommandObject
from aiogram import F
from aiogram.types import (ChatPermissions, BotCommand, BotCommandScopeChat, FSInputFile, InlineKeyboardButton, InlineKeyboardMarkup,
                           KeyboardButton, MenuButtonWebApp, Message, PreCheckoutQuery, ReplyKeyboardMarkup, ReplyKeyboardRemove, WebAppInfo)
from dotenv import load_dotenv

load_dotenv()
WEBAPP_URL = os.getenv("WEBAPP_URL", "https://gifts.zabeyda.lol")
OWNER_ID = int(os.getenv("OWNER_ID", "7308147004"))
DB_PATH = os.getenv("DB_PATH", "gifts.db")
WELCOME = Path(__file__).parent / "welcome.jpg"  # картинка из pics/gift.jpg
TZ = timezone(timedelta(hours=3))  # дни считаем по московскому времени
SHARE_URL = "https://t.me/share/url?url=https%3A//t.me/gift20bot&text=" + "%D0%92%D1%81%D1%91%20%D0%BF%D1%80%D0%BE%20Telegram%20Gifts%20%D0%B2%20%D0%BE%D0%B4%D0%BD%D0%BE%D0%B9%20%D0%B0%D0%BF%D0%BA%D0%B5"
ADMIN_CACHE: dict = {}  # (чат, пользователь) -> (время, это админ)


async def is_chat_admin(bot, chat_id: int, uid: int) -> bool:
    hit = ADMIN_CACHE.get((chat_id, uid))
    if hit and time.time() - hit[0] < 300:
        return hit[1]
    try:
        st = (await bot.get_chat_member(chat_id, uid)).status
        ok = st in ("administrator", "creator")
    except Exception:
        ok = False
    ADMIN_CACHE[(chat_id, uid)] = (time.time(), ok)
    return ok


class ChatModeration(BaseMiddleware):
    """В группах: мат -> сообщение удаляется и автор получает мут на 1 минуту; чужие ссылки -> сообщение удаляется.
    Админы чата, владелец бота и ссылки на @gift20bot не трогаются; команды (/pepe ...) проходят дальше."""

    async def __call__(self, handler, event, data):
        m = event
        if not getattr(m, "chat", None) or m.chat.type not in ("group", "supergroup") or not m.from_user or m.sender_chat:
            return await handler(event, data)
        text = m.text or m.caption or ""
        ents = (m.entities or []) + (m.caption_entities or [])
        if not text or m.from_user.id in (OWNER_ID, 1087968824) or m.from_user.is_bot:
            return await handler(event, data)
        swear, link = modchat.has_profanity(text), modchat.has_foreign_link(text, ents)
        if not (swear or link):
            return await handler(event, data)
        bot = data["bot"]
        if await is_chat_admin(bot, m.chat.id, m.from_user.id):
            return await handler(event, data)
        try:
            await m.delete()
        except Exception:
            return await handler(event, data)  # нет прав удалять: ничего не делаем
        if swear:
            try:
                await bot.restrict_chat_member(m.chat.id, m.from_user.id, permissions=ChatPermissions(can_send_messages=False),
                                               until_date=datetime.now(timezone.utc) + timedelta(seconds=65))
                who = f"@{m.from_user.username}" if m.from_user.username else m.from_user.full_name
                note = await bot.send_message(m.chat.id, f"{who}, мат запрещён: мут на 1 минуту")
                asyncio.create_task(_del_later(note, 10))
            except Exception:
                pass
        return None


async def _del_later(msg, sec: int):
    await asyncio.sleep(sec)
    try:
        await msg.delete()
    except Exception:
        pass


CHAT_URL = "https://t.me/peeppo_chat"
SHARE_URL_EN = "https://t.me/share/url?url=https%3A//t.me/gift20bot&text=Everything%20about%20Telegram%20Gifts%20in%20one%20app"
dp = Dispatcher()
dp.message.outer_middleware(ChatModeration())
dp.edited_message.outer_middleware(ChatModeration())


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
    ref = None
    if arg.startswith("ref_") and arg[4:].isdigit():
        ref, arg = int(arg[4:]), ""
    elif arg[:2] in ("d_", "r_") and arg[2:].isdigit():
        ref = int(arg[2:])
    elif "-r" in arg and arg.rsplit("-r", 1)[1].isdigit():
        arg, r = arg.rsplit("-r", 1)
        ref = int(r)
    url = WEBAPP_URL + ("?s=" + arg if arg and re.fullmatch(r"[A-Za-z0-9_\-]{1,64}", arg) else "")
    kb = InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text="Open", web_app=WebAppInfo(url=url)), InlineKeyboardButton(text="Chat", url=CHAT_URL)],
                         [InlineKeyboardButton(text="Share", url=SHARE_URL_EN)]]
    )
    caption = "Telegram NFT Gifts overview"
    if WELCOME.exists():
        await m.answer_photo(FSInputFile(WELCOME), caption=caption, reply_markup=kb)
    else:
        await m.answer(caption, reply_markup=kb)
    if m.from_user.id != OWNER_ID and record_user(m.from_user.id):
        inv = None  # кто пригласил (для сообщения владельцу)
        if ref and ref != m.from_user.id:  # пришёл по чужой ссылке: засчитываем реферала
            try:
                con = sqlite3.connect(DB_PATH, timeout=10)
                now_t = int(time.time())
                row = con.execute("SELECT pf_user, pf_name FROM users WHERE tg_id = ?", (ref,)).fetchone()
                inv = ("@" + row[0]) if row and row[0] else ((row[1] if row and row[1] else None) or f"id {ref}")
                recent = con.execute("SELECT COUNT(*) FROM referrals WHERE inviter_id = ? AND ts > ?", (ref, now_t - 60)).fetchone()[0]
                if recent >= 5:  # антибот: пачка приглашений за минуту не засчитывается
                    con.execute("INSERT OR IGNORE INTO referrals_removed (invited_id, inviter_id, ts, premium, in_chat, reason, removed_ts) VALUES (?,?,?,?,0,'burst',?)", (m.from_user.id, ref, now_t, 1 if m.from_user.is_premium else 0, now_t))
                    con.commit(); con.close()
                    if recent == 5:
                        try:
                            await m.bot.send_message(OWNER_ID, f"Антибот: у {inv} пачка приглашений за минуту, новые не засчитываются")
                        except Exception:
                            pass
                    return
                con.execute("INSERT OR IGNORE INTO referrals (invited_id, inviter_id, ts, premium) VALUES (?,?,?,?)", (m.from_user.id, ref, now_t, 1 if m.from_user.is_premium else 0))
                con.commit()
                con.close()
                if m.from_user.is_premium:  # друг с Premium засчитывается сразу: +1 бот +3 Premium
                    try:
                        await m.bot.send_message(ref, "Друг с Premium засчитан: +3 билета в розыгрыше. Статистика: /ref")
                    except Exception:
                        pass
            except Exception:
                pass
        who = f"@{m.from_user.username}" if m.from_user.username else (m.from_user.full_name or str(m.from_user.id))
        try:
            await m.bot.send_message(OWNER_ID, f"{inv} привёл {who}" if inv else f"Новый юзер {who}")
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


REF_STARS, REF_MIN = 25, 100


def _ref_name(pf_user, pf_name, uid) -> str:
    if pf_user:
        return "@" + pf_user
    return (pf_name or "").strip() or f"id{uid}"


def ref_balance(con, uid: int) -> tuple[int, int, int]:
    """-> (засчитано друзей, заработано TGS, доступно к выводу)"""
    n, earned = con.execute("SELECT COUNT(*), COALESCE(SUM(stars), 0) FROM ref_credits WHERE inviter_id = ?", (uid,)).fetchone()
    taken = con.execute("SELECT COALESCE(SUM(stars), 0) FROM ref_payouts WHERE tg_id = ? AND status != 'rejected'", (uid,)).fetchone()[0]
    return n, earned, earned - taken


def _tix_word(n: int, ru: bool) -> str:
    """«1 билет», «2 билета», «5 билетов»."""
    if not ru:
        return f"{n} ticket" + ("" if n == 1 else "s")
    k = n % 100
    w = "билет" if n % 10 == 1 and k != 11 else "билета" if 2 <= n % 10 <= 4 and not 12 <= k <= 14 else "билетов"
    return f"{n} {w}"


@dp.message(Command("ref"))
async def ref_cmd(m: Message):
    """Статистика рефералов: только в личке с ботом; в топе считаются друзья с NFT в профиле."""
    if m.chat.type != "private":
        return
    con = sqlite3.connect(DB_PATH, timeout=10)
    row = con.execute("SELECT lang FROM users WHERE tg_id = ?", (m.from_user.id,)).fetchone()
    ru = (row[0] if row and row[0] else "ru") == "ru"
    stats = con.execute("""SELECT r.inviter_id, COUNT(*) AS n, SUM(r.in_chat) AS chat, SUM(r.premium) AS prm,
        SUM(CASE WHEN EXISTS (SELECT 1 FROM ref_credits c WHERE c.invited_id = r.invited_id) THEN 1 ELSE 0 END) AS nft, u.pf_user, u.pf_name
 FROM referrals r LEFT JOIN users u ON u.tg_id = r.inviter_id
 WHERE r.inviter_id != ? AND r.ts >= 0 GROUP BY r.inviter_id""", (OWNER_ID,)).fetchall()
    def _tix(r):  # (всего, части)
        parts = [("NFT", r[4] * 6), ("PRM", r[3] * 3), ("CHAT", r[2] * 2), ("BOT", r[1] * 0)]
        return sum(v for _, v in parts), parts
    top = sorted([(r[0], _tix(r)[0], _tix(r)[1], r[5], r[6]) for r in stats], key=lambda x: -x[1])[:7]
    n, earned, avail = ref_balance(con, m.from_user.id)
    invited = con.execute("SELECT COUNT(*) FROM referrals WHERE inviter_id = ?", (m.from_user.id,)).fetchone()[0]
    con.close()
    nl = chr(10)
    lines = ["Розыгрыш 4 NFT @gift20bot" if ru else "Raffle of 4 NFTs @gift20bot", ""]
    lines += [f"{i}. {_ref_name(r[3], r[4], r[0])} — {_tix_word(r[1], ru)}" for i, r in enumerate(top, 1)] or ["Пока никого" if ru else "Nobody yet"]
    lines += ["", "Итоги 1 ноября в 16:00 МСК" if ru else "Results on November 1 at 16:00 MSK"]
    await m.answer(nl.join(lines))


RAFFLE_END = datetime(2026, 11, 1, 16, 0, tzinfo=TZ)


RAFFLE_PRIZES = 4


def raffle_pick(con):
    """4 разных победителя: случайно, шанс пропорционален билетам; результат хранится один раз. -> список (id, билетов у него, всего билетов)"""
    rows = con.execute("SELECT winner_id, tickets, total FROM ref_draw ORDER BY id").fetchall()
    if rows:
        return rows
    stats = con.execute("""SELECT r.inviter_id, COUNT(*) AS n, SUM(r.in_chat) AS chat, SUM(r.premium) AS prm,
        SUM(CASE WHEN EXISTS (SELECT 1 FROM ref_credits c WHERE c.invited_id = r.invited_id) THEN 1 ELSE 0 END) AS nft, u.pf_user, u.pf_name
 FROM referrals r LEFT JOIN users u ON u.tg_id = r.inviter_id
 WHERE r.inviter_id != ? AND r.ts >= 0 GROUP BY r.inviter_id""", (OWNER_ID,)).fetchall()
    pool = {r[0]: r[1] * 0 + r[2] * 2 + r[3] * 3 + r[4] * 6 for r in stats}
    pool = {u: t for u, t in pool.items() if t > 0}
    total = sum(pool.values())
    if not pool:
        return []
    import random
    left, out = dict(pool), []
    for _ in range(min(RAFFLE_PRIZES, len(pool))):
        uid = random.choices(list(left), weights=list(left.values()))[0]
        out.append((uid, left.pop(uid), total))
    for uid, t, tot in out:
        con.execute("INSERT INTO ref_draw (winner_id, tickets, total, ts) VALUES (?,?,?,?)", (uid, t, tot, int(time.time())))
    con.commit()
    return out


def raffle_text(con, rows) -> str:
    lines = ["Итоги розыгрыша:"]
    for i, r in enumerate(rows, 1):
        u = con.execute("SELECT pf_user, pf_name FROM users WHERE tg_id = ?", (r[0],)).fetchone()
        lines.append(f"{i}. {_ref_name(u[0] if u else None, u[1] if u else None, r[0])} (id {r[0]}) — билетов {r[1]} из {r[2]}")
    return chr(10).join(lines)


@dp.message(Command("draw"))
async def draw_cmd(m: Message):
    """Только владелец: итог розыгрыша после срока."""
    if m.from_user.id != OWNER_ID:
        return
    if datetime.now(TZ) < RAFFLE_END:
        await m.answer("Розыгрыш ещё идёт: итоги 1 ноября в 16:00 МСК")
        return
    con = sqlite3.connect(DB_PATH, timeout=10)
    row = raffle_pick(con)
    await m.answer(raffle_text(con, row) if row else "Нет засчитанных друзей")
    con.close()


async def raffle_announcer(bot: Bot):
    """В срок присылает владельцу итог розыгрыша."""
    wait = (RAFFLE_END - datetime.now(TZ)).total_seconds()
    if wait > 0:
        await asyncio.sleep(wait + 5)
    try:
        con = sqlite3.connect(DB_PATH, timeout=10)
        row = raffle_pick(con)
        await bot.send_message(OWNER_ID, raffle_text(con, row) if row else "Розыгрыш завершён: засчитанных друзей нет")
        con.close()
    except Exception:
        pass


@dp.message(Command("withdraw"))
async def withdraw_cmd(m: Message):
    if m.chat.type != "private":
        return
    con = sqlite3.connect(DB_PATH, timeout=10)
    row = con.execute("SELECT lang FROM users WHERE tg_id = ?", (m.from_user.id,)).fetchone()
    ru = (row[0] if row and row[0] else "ru") == "ru"
    _, _, avail = ref_balance(con, m.from_user.id)
    if avail < REF_MIN or con.execute("SELECT 1 FROM ref_payouts WHERE tg_id = ? AND status = 'pending'", (m.from_user.id,)).fetchone():
        con.close()
        await m.answer((f"Вывод доступен от {REF_MIN} TGS, у тебя {avail}. Если заявка уже отправлена, жди выплаты." if ru else f"Withdrawal from {REF_MIN} TGS; you have {avail}. If a request is already sent, please wait."))
        return
    pid = con.execute("INSERT INTO ref_payouts (tg_id, stars, status, ts) VALUES (?,?,'pending',?)", (m.from_user.id, avail, int(time.time()))).lastrowid
    con.commit(); con.close()
    who = f"@{m.from_user.username}" if m.from_user.username else (m.from_user.full_name or str(m.from_user.id))
    try:
        await m.bot.send_message(OWNER_ID, f"Заявка #{pid}: {who} просит вывести {avail} TGS. Выплатил: /paid {pid}")
    except Exception:
        pass
    await m.answer("Заявка отправлена, выплата вручную." if ru else "Request sent, payout is manual.")


@dp.message(Command("paid"))
async def paid_cmd(m: Message, command: CommandObject):
    if m.from_user.id != OWNER_ID:
        return
    arg = (command.args or "").strip()
    if not arg.isdigit():
        return
    con = sqlite3.connect(DB_PATH, timeout=10)
    row = con.execute("SELECT tg_id, stars FROM ref_payouts WHERE id = ? AND status = 'pending'", (int(arg),)).fetchone()
    if row:
        con.execute("UPDATE ref_payouts SET status = 'paid' WHERE id = ?", (int(arg),)); con.commit()
    con.close()
    if row:
        await m.answer(f"Заявка #{arg} отмечена выплаченной")
        try:
            await m.bot.send_message(row[0], f"Выплата {row[1]} TGS отправлена. Спасибо!")
        except Exception:
            pass


@dp.message(Command("admin"))
async def admin(m: Message):
    if m.from_user.id != OWNER_ID:
        return
    await m.answer(admin_stats(), reply_markup=OWNER_KB)


OWNER_KB = ReplyKeyboardMarkup(keyboard=[[KeyboardButton(text="admin"), KeyboardButton(text="ref")]], resize_keyboard=True, is_persistent=True)


@dp.message(F.text.in_({"admin", "ref"}) & (F.from_user.id == OWNER_ID))
async def owner_buttons(m: Message):
    """Кнопки внизу чата только у владельца: слева admin, справа ref."""
    if m.text == "admin":
        await m.answer(admin_stats(), reply_markup=OWNER_KB)
    else:
        await ref_cmd(m)


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
        if msg and modchat.has_any_link(msg):
            msg = None
        con.execute("INSERT OR IGNORE INTO donations (id, ts, kind, who, amount, msg) VALUES (?,?,?,?,?,?)", (f"stars:{sp.telegram_payment_charge_id}", int(time.time()), "stars", who, float(sp.total_amount), msg))
        con.commit(); con.close()
        await m.bot.send_message(OWNER_ID, f"Донат {sp.total_amount} Stars от {who}" + (chr(10) + msg if msg else ""))
    except Exception:
        pass
    con = sqlite3.connect(DB_PATH, timeout=10)
    row = con.execute("SELECT lang FROM users WHERE tg_id = ?", (m.from_user.id,)).fetchone()
    con.close()
    ru = (row[0] if row and row[0] else "ru") == "ru"
    kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="Open", web_app=WebAppInfo(url=WEBAPP_URL)), InlineKeyboardButton(text="Chat", url=CHAT_URL)]])
    await m.answer("Спасибо за поддержку! Это помогает развивать Gifts." if ru else "Thank you for your support! It helps Gifts grow.", reply_markup=kb)


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
             ("Сутки: " if ru else "24h: ") + _pct(ch.get("1d")), ("Месяц: " if ru else "30d: ") + _pct(ch.get("1m"))]
    url = WEBAPP_URL + "?s=g_" + re.sub(r"[^a-z ]", "", g["name"].lower()).strip().replace(" ", "_")
    if group:  # в группах без кнопок: текст и адрес бота внизу
        await m.answer(nl.join(lines + [nl + "@gift20bot"]))
        return
    kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="Open", web_app=WebAppInfo(url=url)), InlineKeyboardButton(text="Chat", url=CHAT_URL)],
                                               [InlineKeyboardButton(text="Поделиться" if ru else "Share", url=SHARE_URL)]])
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
    asyncio.create_task(raffle_announcer(bot))
    await bot.set_chat_menu_button(menu_button=MenuButtonWebApp(text="Open", web_app=WebAppInfo(url=WEBAPP_URL)))
    try:  # команда /admin в меню только у владельца
        await bot.set_my_commands([BotCommand(command="start", description="Open"), BotCommand(command="admin", description="Admin")],
                                  scope=BotCommandScopeChat(chat_id=OWNER_ID))
    except Exception:
        pass
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())

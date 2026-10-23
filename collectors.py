"""Сборщики данных. Каждый источник — отдельная функция, чтобы поломка одного не валила остальные."""
import asyncio
import html
import logging
import os
import re
import time
from datetime import datetime
from urllib.parse import quote

import httpx

import db

log = logging.getLogger("collectors")
CHANGES = "https://api.changes.tg"  # каталог подарков/моделей (credit: @GiftChanges)


def now() -> int:
    return int(time.time())


async def collect_catalog(client: httpx.AsyncClient) -> None:
    """Каталог: все улучшаемые подарки и их модели (api.changes.tg)."""
    names = (await client.get(f"{CHANGES}/gifts")).json()
    sem = asyncio.Semaphore(8)

    async def one(name: str):
        async with sem:
            r = await client.get(f"{CHANGES}/gift/{quote(name)}")
            r.raise_for_status()
            return r.json()

    results = await asyncio.gather(*(one(n) for n in names), return_exceptions=True)
    async with db.connect() as conn:
        for name, data in zip(names, results):
            if isinstance(data, Exception):
                log.warning("catalog %s: %s", name, data)
                continue
            g = data["gift"]
            await conn.execute(
                "INSERT OR REPLACE INTO gifts VALUES (?,?,?,?)",
                (g["name"], g["id"], g.get("releasedAt"), int(g.get("upgradable", False))),
            )
            for m in data.get("models", []):
                await conn.execute(
                    "INSERT OR REPLACE INTO models VALUES (?,?,?)",
                    (g["name"], m["name"], m.get("rarityPermille")),
                )
        bd: dict[str, dict] = {}  # фоны: цвета (из первого упоминания) и редкость во всех коллекциях
        for data in results:
            if isinstance(data, Exception):
                continue
            for b in data.get("backdrops", []):
                x = bd.setdefault(b["name"], {"hex": b.get("hex") or {}, "r": []})
                if b.get("rarityPermille") is not None:
                    x["r"].append(b["rarityPermille"])
        for name, x in bd.items():
            h = x["hex"]
            await conn.execute("INSERT OR REPLACE INTO backdrops VALUES (?,?,?,?,?,?,?)",
                               (name, h.get("centerColor"), h.get("edgeColor"), h.get("patternColor"), h.get("textColor"),
                                sum(x["r"]) / len(x["r"]) if x["r"] else None, len(x["r"])))
        await conn.commit()
    log.info("catalog: %d gifts, %d backdrops", len(names), len(bd))


async def collect_rates(client: httpx.AsyncClient) -> None:
    """Курс TON (GRAM) к USD."""
    r = await client.get("https://tonapi.io/v2/rates", params={"tokens": "ton", "currencies": "usd"})
    price = r.json()["rates"]["TON"]["prices"]["USD"]
    async with db.connect() as conn:
        await conn.execute("INSERT OR REPLACE INTO rates VALUES (?,?)", (now(), price))
        await conn.commit()


async def collect_marketcap(client: httpx.AsyncClient) -> None:
    """Общая капитализация из Dune (запрос 5286696). Нужен DUNE_API_KEY."""
    key = os.getenv("DUNE_API_KEY")
    if not key:
        return
    r = await client.get(
        "https://api.dune.com/api/v1/query/5286696/results", headers={"X-Dune-API-Key": key}
    )
    r.raise_for_status()
    rows = r.json()["result"]["rows"]
    # TODO: уточнить имя колонки по реальному ответу; берём первое числовое значение.
    usd = next(v for v in rows[0].values() if isinstance(v, (int, float)))
    async with db.connect() as conn:
        cur = await conn.execute("SELECT ton_usd FROM rates ORDER BY ts DESC LIMIT 1")
        row = await cur.fetchone()
        ton_usd = row["ton_usd"] if row else 0
        await conn.execute(
            "INSERT OR REPLACE INTO marketcap VALUES (?,?,?)",
            (now(), usd, usd / ton_usd if ton_usd else 0),
        )
        await conn.commit()


DATASET = "https://raw.githubusercontent.com/ssamy2/TelegramGiftsAssests/main"
MARKET = "community"  # открытый датасет, обновляется автором раз в несколько дней


_issued_first = [True]


async def collect_issued(client: httpx.AsyncClient) -> None:
    """Точное число улучшенных подарков: наибольший номер, для которого есть страница t.me/nft/<Имя>-<номер>.

    Номера выдаются по порядку улучшения, поэтому максимальный существующий номер = число улучшенных. Страницы публичные,
    вход в Telegram не нужен. Ищем от известной оценки (галоп + бинарный поиск, ~10 запросов на коллекцию), не чаще одного запроса
    в 0,8 с. Запуск раз в 12 часов; при любом сбое сети коллекция пропускается и остаётся прежнее значение.
    """
    if _issued_first[0]:  # после старта сервера даём время основным сборщикам
        _issued_first[0] = False
        await asyncio.sleep(180)
    headers = {}

    async def exists(slug: str, n: int) -> bool:
        # номер мог выбыть, поэтому «нет» только если нет и следующих двух
        for k in range(3):
            await asyncio.sleep(0.8)
            r = await client.get(f"https://t.me/nft/{slug}-{n + k}", headers=headers, follow_redirects=True)
            if r.status_code != 200:
                raise RuntimeError(f"t.me status {r.status_code}")
            if f'og:title" content="' in r.text and f"#{n + k}\"" in r.text:
                return True
        # «нет» могло быть и ограничением запросов (t.me тогда отдаёт общую страницу): проверяем контрольный номер 1
        await asyncio.sleep(0.8)
        r = await client.get(f"https://t.me/nft/{slug}-1", follow_redirects=True)
        if r.status_code != 200 or "#1\"" not in r.text:
            await asyncio.sleep(60)
            raise RuntimeError("t.me throttled")
        return False

    async with db.connect() as conn:
        names = [r["name"] for r in await (await conn.execute("SELECT name FROM gifts")).fetchall()]
        prev = {r["gift"]: r["issued"] for r in await (await conn.execute("SELECT gift, issued FROM issued_exact")).fetchall()}
        peek = {r["gift"]: r["issued"] for r in await (await conn.execute("SELECT gift, issued FROM peek WHERE issued > 0")).fetchall()}
        est = await upgraded_map(conn)
    done = 0
    for name in names:
        slug = re.sub(r"[^A-Za-z]", "", name)
        hint = prev.get(name) or peek.get(name) or est.get(name) or 1000
        try:
            if await exists(slug, hint):
                lo, step = hint, max(8, hint // 200)
                hi = lo + step
                while await exists(slug, hi):
                    lo, step = hi, step * 2
                    hi = lo + step
            else:
                hi, lo = hint, max(1, hint // 2)
                while lo > 1 and not await exists(slug, lo):
                    hi, lo = lo, max(1, lo // 2)
            while hi - lo > 1:
                mid = (lo + hi) // 2
                if await exists(slug, mid):
                    lo = mid
                else:
                    hi = mid
        except Exception as e:  # сеть/ограничение: оставляем прежнее значение
            log.warning("issued %s: %s", name, e)
            continue
        ref = prev.get(name) or peek.get(name) or est.get(name)
        if ref and not (0.5 <= lo / ref <= 2):  # расходится с другими источниками в разы: скорее всего, ограничение запросов, а не данные
            log.warning("issued %s: %s vs %s, skipped", name, lo, ref)
            continue
        if lo >= 1:
            async with db.connect() as conn:
                await conn.execute("INSERT OR REPLACE INTO issued_exact VALUES (?,?,?)", (name, lo, now()))
                await conn.commit()
            done += 1
    log.info("issued_exact updated: %d/%d", done, len(names))


_pm_state = {"first": True, "cool": 0.0}


async def collect_peek_models(client: httpx.AsyncClient) -> None:
    """Цены моделей, которых нет в датасете (Resistance Dog, Durov's Cap и др.): самый дешёвый лот модели по открытому поиску peek.tg.

    Раз в 3 часа, по одной модели с паузой 2,5 с (около 20 минут на проход, ~14 запросов в минуту при лимите 35).
    Записываем как обычный снимок цены (market='community'), поэтому графики, цены и пузыри работают у этих моделей так же, как у остальных.
    При 403/429 прекращаем проход и ждём 15 минут.
    """
    if _pm_state["first"]:
        _pm_state["first"] = False
        await asyncio.sleep(240)
    if time.time() < _pm_state["cool"]:
        return
    async with db.connect() as conn:
        cur = await conn.execute(
            """SELECT m.gift, m.name FROM models m
               WHERE NOT EXISTS (SELECT 1 FROM floors f WHERE f.gift = m.gift AND f.model = m.name AND f.market = 'community'
                                 AND f.ts > ?)
               ORDER BY m.gift, m.name""", (now() - 3 * 86400,))
        todo = [(r["gift"], r["name"]) for r in await cur.fetchall()]
        # у подарка датасетные цены свежие у большинства моделей — отдельные пропуски (редкие лоты) пропускаем, догоняем только целиком «слепые» подарки
        cur = await conn.execute("SELECT gift, COUNT(DISTINCT model) n FROM floors WHERE market = 'community' AND model != '' AND ts > ? GROUP BY gift", (now() - 3 * 86400,))
        have_n = {r["gift"]: r["n"] for r in await cur.fetchall()}
    cur_tot = {}
    for g, _ in todo:
        cur_tot[g] = cur_tot.get(g, 0) + 1
    todo = [(g, m) for g, m in todo if have_n.get(g, 0) < 5 or cur_tot[g] >= 5]  # слепые подарки целиком; одиночные пропуски других подарков не трогаем
    headers = {"Origin": "https://peek.tg", "Referer": "https://peek.tg/", "User-Agent": "Mozilla/5.0"}
    done = 0
    for gift, model in todo:
        try:
            r = await client.get("https://server.peek.tg/api/nft/gifts/search", headers=headers,
                                 params={"name": re.sub(r"[^A-Za-z]", "", gift), "models": model, "marketOnly": "true",
                                         "sortBy": "price", "sortOrder": "asc", "page": 1, "limit": 5})
        except Exception as e:
            log.warning("peek models %s/%s: %s", gift, model, e)
            await asyncio.sleep(2.5)
            continue
        if r.status_code in (403, 429) or r.status_code >= 500:
            _pm_state["cool"] = time.time() + 900
            log.warning("peek models: status %s, pause 15 min", r.status_code)
            break
        price = None
        for x in r.json().get("results", []):
            mp = x.get("market") or {}
            if mp.get("asset") in (None, "TON") and isinstance(mp.get("price"), (int, float)):
                price = float(mp["price"]) if price is None else min(price, float(mp["price"]))
        if price:
            async with db.connect() as conn:
                await conn.execute("INSERT OR REPLACE INTO floors (ts, gift, model, market, price_ton) VALUES (?,?,?,?,?)",
                                   (now(), gift, model, "community", price))
                await conn.commit()
            done += 1
        await asyncio.sleep(2.5)
    log.info("peek models: %d/%d priced", done, len(todo))


async def collect_peek(client: httpx.AsyncClient) -> None:
    """Сводка с peek.tg: один открытый запрос (без входа в Telegram), раз в 15 минут. Нужна ради issued — числа улучшенных."""
    r = await client.get("https://server.peek.tg/api/nft/stats/gift/all", params={"period": "24h", "sort": "change", "order": "desc", "limit": 500},
                         headers={"Origin": "https://peek.tg", "Referer": "https://peek.tg/", "User-Agent": "Mozilla/5.0"})
    r.raise_for_status()
    rows = r.json()
    if not isinstance(rows, list) or len(rows) < 20:
        return
    async with db.connect() as conn:
        cur = await conn.execute("SELECT name FROM gifts")
        ours = {re.sub(r"[^a-z]", "", n["name"].lower()): n["name"] for n in await cur.fetchall()}
        ts = now()
        for p in rows:
            name = ours.get(re.sub(r"[^a-z]", "", str(p.get("name", "")).lower()))
            if name:
                await conn.execute("INSERT OR REPLACE INTO peek VALUES (?,?,?,?,?,?)",
                                   (name, p.get("price"), p.get("change"), p.get("issued"), p.get("total"), ts))
        await conn.commit()


async def collect_floors(client: httpx.AsyncClient) -> None:
    """Цены по моделям из открытого датасета (models/<short_name>/prices.json, цены в TON).

    Прямых данных с Portals/Tonnel/MRKT нет (нужен вход в Telegram), поэтому берём общий датасет.
    """
    details = (await client.get(f"{DATASET}/Gifts_Details.json")).json()
    short = {g["full_name"]: g["short_name"] for g in details.get("upgraded", [])}
    # цена подарка целиком (минимум по Portals/Getgems/MRKT, как в Gift Charts) — отдельное поле в Gifts_Details
    gift_floor = {g["full_name"]: float(g["floor_price_ton"]) for g in details.get("upgraded", []) if g.get("floor_price_ton")}
    details_ts = int(details.get("last_updated") or now())
    async with db.connect() as conn:  # цены подарка по маркетам (для окна «Купить»), перезаписываем каждый раз
        for g in details.get("upgraded", []):
            await conn.execute(
                "INSERT OR REPLACE INTO market_prices VALUES (?,?,?,?,?)",
                (g["full_name"], g.get("portal_price_ton"), g.get("tgmrkt_price_ton"), g.get("getgems_price_ton"), details_ts),
            )
        await conn.commit()
    async with db.connect() as conn:  # новых данных нет — ничего не скачиваем (проверка дёшево, поэтому можно часто)
        cur = await conn.execute("SELECT MAX(ts) AS t FROM floors WHERE model = '' AND market = ?", (MARKET,))
        if details_ts <= ((await cur.fetchone())["t"] or 0):
            return
    sem = asyncio.Semaphore(8)

    async def one(name: str, sn: str):
        async with sem:
            r = await client.get(f"{DATASET}/models/{sn}/prices.json")
            r.raise_for_status()
            return r.json()

    items = list(short.items())
    results = await asyncio.gather(*(one(n, s) for n, s in items), return_exceptions=True)
    rows = 0
    async with db.connect() as conn:
        for (name, _), data in zip(items, results):
            if isinstance(data, Exception):
                log.warning("floors %s: %s", name, data)
                continue
            ts = int(data.get("last_updated") or now())
            cur = await conn.execute("SELECT name FROM models WHERE gift = ?", (name,))
            catalog = {r["name"] for r in await cur.fetchall()}
            models = data.get("models") or {}
            # берём только модели из официального каталога: в датасете бывают чужие названия
            prices = {m: float(p) for m, p in models.items() if p and (not catalog or m in catalog)}
            # floor подарка: поле floor_price_ton из Gifts_Details; если его нет — минимум по моделям
            coll = gift_floor.get(name) or (min(prices.values()) if prices else None)
            if coll:
                await conn.execute(
                    "INSERT OR REPLACE INTO floors (ts, gift, model, market, price_ton) VALUES (?,?,?,?,?)",
                    (details_ts if name in gift_floor else ts, name, "", MARKET, coll),
                )
            await conn.execute(
                "DELETE FROM floors WHERE gift = ? AND market = ? AND model != '' "
                "AND model NOT IN (SELECT name FROM models WHERE gift = ?)",
                (name, MARKET, name),
            )
            for model, price in prices.items():
                await conn.execute(
                    "INSERT OR REPLACE INTO floors (ts, gift, model, market, price_ton) VALUES (?,?,?,?,?)",
                    (ts, name, model, MARKET, price),
                )
                rows += 1
        await conn.commit()
    log.info("floors: %d rows", rows)


async def collect_supply(client: httpx.AsyncClient) -> None:
    """Тираж подарков (сколько штук выпущено) из открытого датасета."""
    d = (await client.get(f"{DATASET}/gifts_api_response.json")).json()
    async with db.connect() as conn:
        for g in d["star_gifts_full"]["gifts"]:
            if g.get("availability_total"):
                await conn.execute(
                    "INSERT OR REPLACE INTO supply VALUES (?,?)", (str(g["id"]), int(g["availability_total"]))
                )
        await conn.commit()


async def upgraded_map(conn, min_n: int = 10) -> dict[str, int]:
    """Оценка числа улучшенных подарков по серийным номерам продаж (только подарки с >= min_n продаж)."""
    import caps

    cur = await conn.execute("SELECT g.name AS gift, p.total FROM gifts g JOIN supply p ON p.gift_id = g.gift_id")
    total = {r["gift"]: r["total"] for r in await cur.fetchall()}
    cur = await conn.execute(
        "SELECT gift, COUNT(*) AS n, MAX(num) AS mx FROM sales WHERE num IS NOT NULL GROUP BY gift"
    )
    out: dict[str, int] = {}
    for r in await cur.fetchall():
        if r["n"] >= min_n:
            est = caps.estimate_upgraded(r["n"], r["mx"], total.get(r["gift"]))
            if est:
                out[r["gift"]] = est
    # число улучшенных, которое отдаёт peek.tg, точнее нашей оценки по продажам
    cur = await conn.execute("SELECT gift, issued FROM peek WHERE issued > 0 AND ts > ?", (now() - 86400,))
    for r in await cur.fetchall():
        out[r["gift"]] = int(r["issued"])
    out.update(caps.UPGRADED_OVERRIDES)  # сверено с эталоном
    # точный счёт по публичным страницам t.me/nft — приоритетнее всего (если не расходится с остальными в разы: значит, сбой)
    cur = await conn.execute("SELECT gift, issued FROM issued_exact WHERE ts > ?", (now() - 3 * 86400,))
    for r in await cur.fetchall():
        old = out.get(r["gift"])
        if not old or 0.5 <= r["issued"] / old <= 2:
            out[r["gift"]] = int(r["issued"])
    return out


async def current_cap(conn):
    """Текущая оценка капитализации: сумма по подаркам (тираж × цена подарка), см. caps.py.

    Цена подарка — самая дешёвая из цен его моделей. Возвращает (GRAM, USD) или None.
    """
    import caps

    cur = await conn.execute("SELECT g.name AS gift, s.total FROM gifts g JOIN supply s ON s.gift_id = g.gift_id")
    supply = {r["gift"]: r["total"] for r in await cur.fetchall()}
    upg = await upgraded_map(conn)
    supply = {g: upg.get(g) or t for g, t in supply.items()}  # где есть оценка улучшенных — берём её, иначе макс. тираж
    for g, n in upg.items():  # подарки без записи о тираже (кубки), но с известным числом улучшенных
        supply.setdefault(g, n)
    cur = await conn.execute(
        """SELECT f.gift, f.model, f.price_ton FROM floors f
           WHERE f.market = 'community' AND f.model != ''
             AND f.ts = (SELECT MAX(ts) FROM floors x WHERE x.gift = f.gift AND x.model = f.model)"""
    )
    prices: dict[str, dict] = {}
    for r in await cur.fetchall():
        prices.setdefault(r["gift"], {})[r["model"]] = r["price_ton"]
    cur = await conn.execute(  # цена подарка из датасета (floor_price_ton), как в списке
        """SELECT f.gift, f.price_ton FROM floors f WHERE f.model = '' AND f.market = 'community'
           AND f.ts = (SELECT MAX(ts) FROM floors x WHERE x.gift = f.gift AND x.model = '')"""
    )
    gfl = {r["gift"]: r["price_ton"] for r in await cur.fetchall()}
    cap = 0.0
    for gift, total in supply.items():
        floor = gfl.get(gift) or caps.collection_floor(prices.get(gift, {}))
        if floor:
            cap += floor * total
    cur = await conn.execute("SELECT ton_usd FROM rates ORDER BY ts DESC LIMIT 1")
    rate = await cur.fetchone()
    return (cap, cap * rate["ton_usd"]) if cap and rate else None


async def collect_marketcap_estimate(client: httpx.AsyncClient) -> None:
    """Сохраняет снимок оценки капитализации для графика (раз в час или при изменении)."""
    async with db.connect() as conn:
        cur_cap = await current_cap(conn)
        if not cur_cap:
            return
        cap, usd = cur_cap
        # точка привязана ко времени последнего обновления цен в источнике: так история не забивается копиями
        cur = await conn.execute("SELECT MAX(ts) AS t FROM floors WHERE model != '' AND market = 'community'")
        data_ts = (await cur.fetchone())["t"] or now()
        await conn.execute("INSERT OR REPLACE INTO marketcap VALUES (?,?,?)", (data_ts, usd, cap))
        await conn.commit()
    log.info("marketcap estimate: %.0f GRAM", cap)


SALES_URL ="https://t.me/s/mrktnotification"  # публичная веб-версия канала, вход не нужен
SALES_SOURCE = "mrkt-channel"
_POST = re.compile(r'data-post="[^/]+/(\d+)"(.*?)(?=data-post="|\Z)', re.S)
_TEXT = re.compile(r'tgme_widget_message_text[^>]*>(.*?)</div>', re.S)
_TIME = re.compile(r'<time[^>]*datetime="([^"]+)"')


def parse_sales(page: str) -> list[dict]:
    out = []
    for pid, chunk in _POST.findall(page):
        m, t = _TEXT.search(chunk), _TIME.search(chunk)
        if not m or not t:
            continue
        text = html.unescape(re.sub(r"<[^>]+>", "", re.sub(r"<br\s*/?>", "\n", m.group(1))))
        if "Gift Sold" not in text:
            continue
        head = re.search(r"^(.+?) #(\d+)\s*$", text, re.M)
        model = re.search(r"Model:\s*(.+?)\s*\(", text)
        symbol = re.search(r"Symbol:\s*(.+?)\s*\(", text)
        backdrop = re.search(r"Backdrop:\s*(.+?)\s*\(", text)
        price = re.search(r"Price:\s*([\d.,]+)", text)
        if not (head and price):
            continue
        out.append({
            "id": int(pid),
            "ts": int(datetime.fromisoformat(t.group(1)).timestamp()),
            "gift": head.group(1).strip(),
            "num": int(head.group(2)),
            "model": model.group(1) if model else None,
            "symbol": symbol.group(1) if symbol else None,
            "backdrop": backdrop.group(1) if backdrop else None,
            "price_ton": float(price.group(1).replace(",", "")),
        })
    return out


def _norm(name: str) -> str:
    """Ключ для сравнения названий: канал и каталог пишут апострофы и пробелы по-разному (’ и ', двойной пробел)."""
    return " ".join(name.replace("’", "'").replace("‘", "'").lower().split())


async def _catalog_maps(conn):
    cur = await conn.execute("SELECT name FROM gifts")
    gifts = {_norm(r["name"]): r["name"] for r in await cur.fetchall()}
    cur = await conn.execute("SELECT gift, name FROM models")
    models: dict[str, dict[str, str]] = {}
    for r in await cur.fetchall():
        models.setdefault(r["gift"], {})[_norm(r["name"])] = r["name"]
    return gifts, models


def _resolve(s: dict, gifts: dict, models: dict) -> None:
    """Приводит названия подарка и модели из канала к названиям каталога (если они там есть)."""
    s["gift"] = gifts.get(_norm(s["gift"]), s["gift"])
    if s["model"]:
        s["model"] = models.get(s["gift"], {}).get(_norm(s["model"]), s["model"])


_sales_fixed = False


async def collect_sales(client: httpx.AsyncClient) -> None:
    """Реальные продажи из публичного канала: свежая страница + подгрузка пропущенного."""
    global _sales_fixed
    async with db.connect() as conn:
        gifts_map, models_map = await _catalog_maps(conn)
        if not _sales_fixed:  # разово чиним уже сохранённые продажи с «чужим» написанием названий
            cur = await conn.execute("SELECT DISTINCT gift, model FROM sales")
            for r in await cur.fetchall():
                s = {"gift": r["gift"], "model": r["model"]}
                _resolve(s, gifts_map, models_map)
                if (s["gift"], s["model"]) != (r["gift"], r["model"]):
                    await conn.execute("UPDATE sales SET gift = ?, model = ? WHERE gift = ? AND model IS ?",
                                       (s["gift"], s["model"], r["gift"], r["model"]))
            await conn.commit()
            _sales_fixed = True
        cur = await conn.execute("SELECT MAX(id) AS m FROM sales")
        known = (await cur.fetchone())["m"] or 0
    pages_limit = 30 if known == 0 else 10  # первый запуск — собираем историю глубже
    before, new = None, 0
    for _ in range(pages_limit):
        r = await client.get(SALES_URL, params={"before": before} if before else None)
        r.raise_for_status()
        sales = parse_sales(r.text)
        ids = [int(x) for x in re.findall(r'data-post="[^/]+/(\d+)"', r.text)]
        async with db.connect() as conn:
            for s in sales:
                _resolve(s, gifts_map, models_map)
                cur = await conn.execute(
                    "INSERT OR IGNORE INTO sales VALUES (?,?,?,?,?,?,?,?,?)",
                    (s["id"], s["ts"], s["gift"], s["num"], s["model"], s["symbol"], s["backdrop"],
                     s["price_ton"], SALES_SOURCE),
                )
                new += cur.rowcount
            await conn.commit()
        if not ids or min(ids) <= known:
            break
        before = min(ids)
        await asyncio.sleep(1)
    if new:
        log.info("sales: +%d", new)


async def prewarm_images(client: httpx.AsyncClient) -> None:
    """Заранее скачивает и сжимает все картинки подарков и моделей, чтобы приложение отдавало их мгновенно."""
    import images

    async with db.connect() as conn:
        cur = await conn.execute("SELECT name FROM gifts WHERE upgradable = 1")
        gifts = [r["name"] for r in await cur.fetchall()]
        cur = await conn.execute("SELECT gift, name FROM models")
        models = [(r["gift"], r["name"]) for r in await cur.fetchall()]
    sem = asyncio.Semaphore(40)
    done = 0

    async def one(kind: str, a: str, b: str, names=None):
        nonlocal done
        if not a or (kind == "m" and not images.ok_part(b)):
            return
        async with sem:
            if await images.ensure(client, kind, a, b, False, miss_ttl=7 * 86400, names=names):
                done += 1

    await asyncio.gather(*(one("g", images.short(g), "") for g in gifts))  # сначала подарки
    await asyncio.gather(*(one("m", images.short(g), images.mfile(m), (g, m)) for g, m in models))
    log.info("images ready: %d", done)


async def run_forever(coro_fn, interval: int, name: str) -> None:
    async with httpx.AsyncClient(timeout=30) as client:
        while True:
            try:
                await coro_fn(client)
            except Exception as e:  # один источник не должен валить остальные
                log.error("%s failed: %s", name, e)
            await asyncio.sleep(interval)

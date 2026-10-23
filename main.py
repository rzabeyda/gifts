import asyncio
import io
import logging
import re
import statistics
import time
from contextlib import asynccontextmanager
from pathlib import Path

import httpx
from fastapi import FastAPI, HTTPException
from PIL import Image
from fastapi.responses import FileResponse, Response
from fastapi.staticfiles import StaticFiles

import caps
import collectors
import db
import images
import themes

logging.basicConfig(level=logging.INFO)
logging.getLogger("httpx").setLevel(logging.WARNING)  # httpx пишет полный адрес запроса, а в адресе Bot API лежит токен бота: в журнал это не попадает
WEB = Path(__file__).parent / "web"


async def keep_warm():
    """Кэши живут 30–60 с; обновляем их в фоне, чтобы пользователь не ждал пересчёта (он занимал ~4 с)."""
    while True:
        try:
            await gifts()
            await api_themes()
            await all_models()
            # тяжёлый расчёт «изменений моделей» обновляем заранее, пока кэш ещё не истёк, чтобы пользователь его не ждал
            if time.time() - _mc_cache["t"] > 420:
                async with _mc_lock:
                    if time.time() - _mc_cache["t"] > 420:
                        await _model_changes_compute()
        except Exception as e:
            logging.warning("warm: %s", e)
        await asyncio.sleep(10)


@asynccontextmanager
async def lifespan(app: FastAPI):
    await db.init()
    tasks = [
        asyncio.create_task(collectors.run_forever(collectors.collect_catalog, 6 * 3600, "catalog")),
        asyncio.create_task(collectors.run_forever(collectors.collect_rates, 60, "rates")),
        asyncio.create_task(collectors.run_forever(collectors.collect_marketcap, 600, "marketcap")),
        asyncio.create_task(collectors.run_forever(collectors.collect_supply, 6 * 3600, "supply")),
        asyncio.create_task(collectors.run_forever(collectors.collect_marketcap_estimate, 60, "marketcap-est")),
        asyncio.create_task(collectors.run_forever(collectors.collect_floors, 300, "floors")),
        asyncio.create_task(collectors.run_forever(collectors.collect_peek, 900, "peek")),
        asyncio.create_task(collectors.run_forever(collectors.collect_peek_models, 3 * 3600, "peek-models")),
        asyncio.create_task(collectors.run_forever(collectors.collect_issued, 12 * 3600, "issued")),
        asyncio.create_task(collectors.run_forever(collectors.collect_sales, 30, "sales")),
        asyncio.create_task(collectors.run_forever(collectors.prewarm_images, 900, "images")),
        asyncio.create_task(cap_snapshots()),
        asyncio.create_task(keep_warm()),
        asyncio.create_task(alerts_loop()),
    ]
    yield
    for t in tasks:
        t.cancel()


app = FastAPI(lifespan=lifespan)


@app.middleware("http")
async def api_auth(request, call_next):
    """API отдаётся только из мини-приложения: нужна подписанная Telegram строка initData (её проверяет HMAC токеном бота).
    Служебный ключ DEV_API_KEY из окружения сервера нужен для проверок и нигде не публикуется."""
    if request.url.path.startswith("/api/") and request.method != "OPTIONS":
        dev = os.getenv("DEV_API_KEY", "")
        if verify_init_data(request.headers.get("x-init-data", "")) is None and not (dev and hmac.compare_digest(request.headers.get("x-dev-key", ""), dev)):
            from fastapi.responses import JSONResponse
            return JSONResponse({"detail": "telegram auth required"}, status_code=401)
    return await call_next(request)


@app.middleware("http")
async def no_cache_page(request, call_next):
    """Страница приложения не кэшируется (иначе Telegram может долго показывать старую версию)."""
    resp = await call_next(request)
    if request.url.path in ("/", "/index.html"):
        resp.headers["Cache-Control"] = "no-store"
    return resp


async def q(sql: str, args: tuple = ()):
    async with db.connect() as conn:
        cur = await conn.execute(sql, args)
        return [dict(r) for r in await cur.fetchall()]


_eff_cache: dict = {"t": 0.0, "v": {}}
FRESH_SALE = 3 * 3600  # свежие продажи (последние 3 часа) задают актуальный уровень цен вместо устаревшего датасета
sale_floor: dict[str, float] = {}  # цена подарка по свежим продажам (заполняет effective_floors)
FRESH_SNAPSHOT = 6 * 3600  # снимок цен моложе 6 часов считаем свежим: калибровка по продажам не нужна
RECENT = 3600  # окно «быстрого уровня» цены
ASK_PREMIUM = 1.0  # сверка с Gift Charts (21.10): поправка +3.5% завышала цены ходовых подарков на 2–5%, убрана
OUTLIER = 0.85        # продажа дешевле 85% от «нижнего кластера» продаж подарка — выброс (ошибочный лот), не учитываем


async def effective_floors() -> dict:
    """Цена модели = минимум из датасета и самых дешёвых свежих продаж (без выбросов).

    Датасет обновляется редко, и цена там может быть выше реальной; раз модель только что продали
    дешевле, значит, лот по такой цене существовал. Но одиночная продажа сильно ниже остальных (случайно
    выставленный лот) не должна тянуть вниз цену всего подарка, поэтому такие продажи отбрасываются.
    Результат кэшируется на минуту.
    """
    if time.time() - _eff_cache["t"] < 60:
        return _eff_cache["v"]
    base = await q(
        """SELECT f.gift, f.model, f.price_ton, f.ts FROM floors f
           WHERE f.market = 'community' AND f.model != ''
             AND f.ts = (SELECT MAX(ts) FROM floors x WHERE x.gift = f.gift AND x.model = f.model)"""
    )
    sold = await q(
        "SELECT gift, model, price_ton, ts FROM sales WHERE ts > strftime('%s','now') - ?", (FRESH_SALE,)
    )
    ds = {(r["gift"], r["model"]): r["price_ton"] for r in base}
    # Возраст снимка цен по каждому подарку: пока он свежий, цены датасета верны как есть (сверка с Gift Charts 22.10
    # показала совпадение до сотых), калибровка по продажам нужна только если датасет устарел.
    snap_ts: dict[str, int] = {}
    for r in base:
        snap_ts[r["gift"]] = max(r["ts"], snap_ts.get(r["gift"], 0))
    by_gift: dict[str, list] = {}
    for r in sold:
        by_gift.setdefault(r["gift"], []).append(r)
    clean: list[dict] = []  # свежие продажи без выбросов
    for gift, ss in by_gift.items():
        low = sorted(r["price_ton"] for r in ss)[:5]  # нижний кластер цен подарка
        ref = statistics.median(low) if len(low) >= 3 else None
        clean += [r for r in ss if not (ref and r["price_ton"] < OUTLIER * ref)]
    # Цена подарка по продажам — для подарков, которых нет в датасете (Resistance Dog и др.): медиана трёх самых
    # дешёвых свежих продаж (>= 5 продаж за окно)
    day = await q("SELECT gift, price_ton, ts FROM sales WHERE ts > strftime('%s','now') - 86400")
    by_day: dict[str, list[tuple[int, float]]] = {}
    for r in day:
        by_day.setdefault(r["gift"], []).append((r["ts"], r["price_ton"]))
    sale_floor.clear()
    cutoff = time.time() - FRESH_SALE
    for gft, lst in by_day.items():
        recent = [p for t, p in lst if t > cutoff]
        ps = recent if len(recent) >= 5 else [p for _, p in lst]  # мало свежих продаж — берём окно 24 часа
        if len(ps) >= 5:
            low = sorted(ps)[:5]
            ref = statistics.median(low)
            ps = sorted(p for p in ps if p >= OUTLIER * ref)  # без выбросов
            sale_floor[gft] = statistics.median(ps[:3])
    # Калибровка уровня: датасет задаёт соотношение цен между моделями, а свежие продажи — общий уровень цен
    # подарка. Для каждого подарка берём медиану (цена продажи / цена датасета той же модели); при малом числе
    # продаж подтягиваем к общей по рынку.
    pairs: dict[str, list[float]] = {}
    for r in clean:
        d = ds.get((r["gift"], r["model"]))
        if d and 0.5 <= r["price_ton"] / d <= 2:
            pairs.setdefault(r["gift"], []).append(r["price_ton"] / d)
    allr = [x for v in pairs.values() for x in v]
    glob = statistics.median(allr) if len(allr) >= 20 else 1.0
    scale: dict[str, float] = {}
    for gift, v in pairs.items():
        n = len(v)
        scale[gift] = min(2.0, max(0.6, (n * statistics.median(v) + 5 * glob) / (n + 5)))
    # Быстрый уровень: у ходовых подарков (>= 6 продаж за последний час) цена подгоняется по трём самым дешёвым
    # свежим продажам — рынок движется быстрее, чем успевает медиана за 3 часа.
    now_ts = time.time()
    recent: dict[str, list[float]] = {}
    for r in clean:
        if now_ts - r["ts"] <= RECENT:
            recent.setdefault(r["gift"], []).append(r["price_ton"])
    ds_min: dict[str, float] = {}
    for (gift, _m), p in ds.items():
        ds_min[gift] = min(p, ds_min.get(gift, p))
    for gift, ps in recent.items():
        if len(ps) >= 6 and ds_min.get(gift):
            scale[gift] = min(2.0, max(0.6, statistics.median(sorted(ps)[:3]) / ds_min[gift]))
    # Продажи идут по самым дешёвым лотам, а цена на маркете — это цена самого дешёвого лота из тех, что висят
    # сейчас (она чуть выше). Поправка подобрана по ценам маркета, которые сообщил пользователь (Brownie 4.18,
    # минимум по рынку 3.75): без неё цены у нас ниже на 3–5%.
    out: dict = {}
    for (gift, model), p in ds.items():
        fresh = now_ts - snap_ts.get(gift, 0) < FRESH_SNAPSHOT
        out.setdefault(gift, {})[model] = (p if fresh else p * scale.get(gift, glob) * ASK_PREMIUM, "dataset")
    for r in clean:  # модели, которых нет в датасете: берём цену свежей продажи
        if (r["gift"], r["model"]) not in ds:
            cur = out.setdefault(r["gift"], {}).get(r["model"])
            if cur is None or r["price_ton"] < cur[0]:
                out[r["gift"]][r["model"]] = (r["price_ton"], "sale")
    _eff_cache.update(t=time.time(), v=out)
    return out


@app.get("/api/gifts")
async def gifts():
    """Коллекции с ценой, тиражом, капитализацией и изменениями (кэш 30 секунд: расчёт тяжёлый, ~1 с)."""
    if _gifts_cache["v"] is not None and time.time() - _gifts_cache["t"] < 30:
        return _gifts_cache["v"]
    async with _gifts_cache["lock"]:  # одновременные запросы ждут один пересчёт, а не считают каждый свой
        if _gifts_cache["v"] is not None and time.time() - _gifts_cache["t"] < 30:
            return _gifts_cache["v"]
        rows = await _gifts_compute()
        _gifts_cache.update(t=time.time(), v=rows)
        return rows


_gifts_cache: dict = {"t": 0.0, "v": None, "lock": asyncio.Lock()}


async def _gifts_compute():
    rows = await q(
        """
        SELECT g.name, g.gift_id, g.released_at,
               (SELECT total FROM supply s WHERE s.gift_id = g.gift_id) AS supply,
               (SELECT COUNT(*) FROM models m WHERE m.gift = g.name) AS models,
               (SELECT MAX(f.ts) FROM floors f WHERE f.gift = g.name) AS updated
        FROM gifts g WHERE g.upgradable = 1 ORDER BY g.released_at
        """
    )
    eff = await effective_floors()
    async with db.connect() as conn:
        upg = await collectors.upgraded_map(conn)  # оценка числа улучшенных по серийным номерам продаж
    # история floor коллекции (снимки источника): по ней считаем изменение цены за разные периоды
    hist: dict[str, list] = {}
    for h in await q(
        "SELECT gift, ts, price_ton FROM floors WHERE model = '' AND market = 'community' ORDER BY gift, ts"
    ):
        hist.setdefault(h["gift"], []).append((h["ts"], h["price_ton"]))
    mkt = {x["gift"]: x for x in await q("SELECT gift, portals, mrkt, getgems FROM market_prices")}
    gfl = {x["gift"]: x for x in await q(  # цена подарка из датасета (floor_price_ton), как в Gift Charts
        """SELECT f.gift, f.price_ton, f.ts FROM floors f WHERE f.model = '' AND f.market = 'community'
           AND f.ts = (SELECT MAX(ts) FROM floors x WHERE x.gift = f.gift AND x.model = '')""")}
    now_ts = time.time()
    for r in rows:  # цена подарка; капитализация = число улучшенных × эта цена
        g0 = gfl.get(r["name"])
        if g0 and now_ts - g0["ts"] < FRESH_SNAPSHOT:
            r["floor_ton"] = g0["price_ton"]
        elif g0 is None and r["name"] in sale_floor:  # подарка нет в датасете — цена по свежим продажам
            r["floor_ton"] = sale_floor[r["name"]]
        else:  # снимок устарел — оцениваем по ценам моделей, подогнанным под свежие продажи
            r["floor_ton"] = caps.collection_floor({m: p for m, (p, _) in eff.get(r["name"], {}).items()})
        r["upgraded"] = upg.get(r["name"])
        count = r["upgraded"] or r["supply"]
        r["cap_ton"] = r["floor_ton"] * count if r["floor_ton"] and count else None
        r["changes"] = price_changes(hist.get(r["name"], []))
        mp = mkt.get(r["name"])
        r["markets"] = {"portals": mp["portals"], "mrkt": mp["mrkt"], "getgems": mp["getgems"]} if mp else {}
    return rows


PERIOD_DAYS = {"1h": 1 / 24, "1d": 1, "1w": 7, "1m": 30, "all": None}


def price_changes(series: list[tuple[int, float]]) -> dict:
    """Изменение цены в % за периоды: к снимку не позже чем «период» назад от последнего.

    Если истории короче периода, сравниваем с самым ранним снимком (то есть «с начала данных»).
    """
    out = {k: None for k in PERIOD_DAYS}
    if len(series) < 2:
        return out
    last_ts, last_p = series[-1]
    for key, days in PERIOD_DAYS.items():
        older = [p for t, p in series[:-1] if days is not None and t <= last_ts - days * 86400]
        if not older and days is not None and days < 1:  # за час сравнивать не с чем: нужен снимок старше часа
            continue
        base = older[-1] if older else series[0][1]
        if base:
            out[key] = round((last_p / base - 1) * 100, 2)
    return out


# --- картинки: см. images.py (кэш + сжатие), здесь только отдача ---
_img_sem = asyncio.Semaphore(24)
_img_client: httpx.AsyncClient | None = None


async def serve_image(kind: str, a: str, b: str, full: bool, names: tuple[str, str] | None = None):
    if not images.SAFE.match(a) or (b and not images.ok_part(b)):
        raise HTTPException(404)
    headers = {"Cache-Control": "public, max-age=2592000, immutable"}
    path = images.path_for(kind, a, b, full)
    if not path.exists():  # не из предзагрузки: скачиваем при первом запросе
        global _img_client
        _img_client = _img_client or httpx.AsyncClient(timeout=30, follow_redirects=True)
        async with _img_sem:
            path = await images.ensure(_img_client, kind, a, b, full, names=names)
        if path is None:
            raise HTTPException(404)
    return FileResponse(path, media_type="image/webp", headers=headers)


@app.get("/img/g/{a}.webp")
async def img_gift(a: str, full: int = 0):
    return await serve_image("g", a, "", bool(full))


@app.get("/img/m/{a}/{b}.webp")
async def img_model(a: str, b: str, full: int = 0, g: str = "", m: str = ""):
    names = (g, m) if g and m and len(g) <= 80 and len(m) <= 120 else None  # настоящие названия для запасного источника
    return await serve_image("m", a, b, bool(full), names)


@app.get("/api/gifts/{gift}/models")
async def models(gift: str):
    rows = await q(
        """
        SELECT m.name, m.rarity_permille,
               (SELECT f.price_ton FROM floors f WHERE f.gift = m.gift AND f.model = m.name
                 ORDER BY f.ts DESC, f.price_ton LIMIT 1) AS floor_ton,
               (SELECT f.ts FROM floors f WHERE f.gift = m.gift AND f.model = m.name
                 ORDER BY f.ts DESC LIMIT 1) AS updated,
               (SELECT s.price_ton FROM sales s WHERE s.gift = m.gift AND s.model = m.name
                 ORDER BY s.ts DESC LIMIT 1) AS last_sale_ton,
               (SELECT s.ts FROM sales s WHERE s.gift = m.gift AND s.model = m.name
                 ORDER BY s.ts DESC LIMIT 1) AS last_sale_ts,
               (SELECT COUNT(*) FROM sales s WHERE s.gift = m.gift AND s.model = m.name) AS sales_count
        FROM models m WHERE m.gift = ?
        """,
        (gift,),
    )
    if not rows:
        raise HTTPException(404, "unknown gift")
    eff = (await effective_floors()).get(gift, {})
    for r in rows:
        r["floor_ton"], r["price_src"] = eff.get(r["name"], (None, None))
    rows.sort(key=lambda r: (r["floor_ton"] is None, r["floor_ton"] or 0, r["name"]))
    return rows


@app.get("/api/gifts/{gift}/models/{model}/history")
async def history(gift: str, model: str, days: int = 7):
    """История цены модели (снимки датасета) и точки реальных продаж за период."""
    floors = await q(
        """SELECT ts, MIN(price_ton) AS price_ton FROM floors
           WHERE gift = ? AND model = ? AND ts > strftime('%s','now') - ? * 86400
           GROUP BY ts ORDER BY ts""",
        (gift, model, days),
    )
    sales = await q(
        """SELECT ts, price_ton FROM sales
           WHERE gift = ? AND model = ? AND ts > strftime('%s','now') - ? * 86400 ORDER BY ts""",
        (gift, model, days),
    )
    return {"floors": floors, "sales": sales}


@app.get("/api/gifts/{gift}/history")
async def gift_history(gift: str, days: int = 30):
    """История цены подарка (floor коллекции по снимкам источника + сегодняшняя цена) и реальные продажи."""
    floors = await q(
        """SELECT ts, price_ton FROM floors
           WHERE gift = ? AND model = '' AND market = 'community' AND ts > strftime('%s','now') - ? * 86400
           ORDER BY ts""",
        (gift, days),
    )
    sales = await q(
        """SELECT ts, price_ton FROM (SELECT ts, price_ton FROM sales
              WHERE gift = ? AND ts > strftime('%s','now') - ? * 86400 ORDER BY ts DESC LIMIT 400) ORDER BY ts""",
        (gift, days),
    )
    price = caps.collection_floor({m: p for m, (p, _) in (await effective_floors()).get(gift, {}).items()})
    # сегодняшняя цена (как в списке подарков) отдаётся отдельно: она откалибрована по свежим продажам и не
    # должна выглядеть как скачок на линии из снимков источника
    return {"floors": floors, "sales": sales, "now": {"ts": int(time.time()), "price_ton": price} if price else None}


_theme_cache: dict = {"t": 0.0, "v": {}}


async def theme_members() -> dict:
    """Модели по темам: {id: [{gift, name, rarity_permille}, ...]} (кэш на 5 минут)."""
    if time.time() - _theme_cache["t"] < 300 and _theme_cache["v"]:
        return _theme_cache["v"]
    idx: dict = {tid: [] for tid in themes.TITLES}
    for r in await q("SELECT gift, name, rarity_permille FROM models"):
        for tid in themes.match(r["gift"], r["name"]):
            idx[tid].append(r)
    _theme_cache.update(t=time.time(), v=idx)
    return idx


@app.get("/api/themes")
async def api_themes():
    idx, eff = await theme_members(), await effective_floors()
    out = []
    for tid, title, _, _ in themes.THEMES:
        ms = idx.get(tid, [])
        if len(ms) < 6:
            continue
        prices = [eff[m["gift"]][m["name"]][0] for m in ms if m["name"] in eff.get(m["gift"], {})]
        rare = sorted(ms, key=lambda m: (m["rarity_permille"] is None, m["rarity_permille"] or 0))[:3]
        out.append({"id": tid, "group": themes.GROUP_OF.get(tid, "other"), "title": title, "title_en": themes.TITLES_EN.get(tid, title), "count": len(ms),
                    "floor_ton": min(prices) if prices else None,
                    "samples": [{"gift": m["gift"], "model": m["name"]} for m in rare]})
    return out


@app.get("/api/themes/{tid}/models")
async def api_theme_models(tid: str):
    if tid not in themes.TITLES:
        raise HTTPException(404, "unknown theme")
    ms, eff = (await theme_members())[tid], await effective_floors()
    last = {(r["gift"], r["model"]): r for r in await q(
        "SELECT gift, model, price_ton, MAX(ts) AS ts FROM sales GROUP BY gift, model")}
    out = []
    for m in ms:
        price, src = eff.get(m["gift"], {}).get(m["name"], (None, None))
        ls = last.get((m["gift"], m["name"]))
        out.append({"gift": m["gift"], "name": m["name"], "rarity_permille": m["rarity_permille"],
                    "floor_ton": price, "price_src": src,
                    "last_sale_ton": ls["price_ton"] if ls else None, "last_sale_ts": ls["ts"] if ls else None})
    out.sort(key=lambda r: (r["floor_ton"] is None, r["floor_ton"] or 0, r["name"]))
    return {"id": tid, "title": themes.TITLES[tid], "title_en": themes.TITLES_EN.get(tid, themes.TITLES[tid]), "models": out}


# ---- живые лоты с peek.tg (открытый поиск без входа в Telegram): редко, с кэшем и общим ограничителем частоты ----
_live_cache: dict = {}
_live_state = {"last": 0.0, "lock": asyncio.Lock()}
LIVE_TTL, LIVE_GAP = 300, 3.0  # кэш на ключ 5 минут; не чаще одного запроса к peek.tg раз в 3 секунды


@app.get("/api/live")
async def api_live(gift: str, model: str = ""):
    """Самые дешёвые лоты подарка/модели по маркетам: {markets: {market: мин. цена}, items: [...], total}. Без данных о владельцах."""
    if len(gift) > 80 or len(model) > 120:
        raise HTTPException(400, "bad request")
    key = (gift, model)
    hit = _live_cache.get(key)
    if hit and time.time() - hit["t"] < LIVE_TTL:
        return hit["v"]
    async with _live_state["lock"]:
        hit = _live_cache.get(key)
        if hit and time.time() - hit["t"] < LIVE_TTL:
            return hit["v"]
        wait = _live_state["last"] + LIVE_GAP - time.time()
        if wait > 0:
            await asyncio.sleep(wait)
        _live_state["last"] = time.time()
        name = re.sub(r"[^A-Za-z]", "", gift)
        params = {"name": name, "marketOnly": "true", "sortBy": "price", "sortOrder": "asc", "page": 1, "limit": 40}
        if model:
            params["models"] = model
        try:
            async with httpx.AsyncClient(timeout=20) as c:
                r = await c.get("https://server.peek.tg/api/nft/gifts/search", params=params,
                                headers={"Origin": "https://peek.tg", "Referer": "https://peek.tg/", "User-Agent": "Mozilla/5.0"})
            r.raise_for_status()
            data = r.json()
        except Exception as e:
            logging.warning("live %s/%s: %s", gift, model, e)
            v = hit["v"] if hit else {"markets": {}, "items": [], "total": 0, "ok": False}
            _live_cache[key] = {"t": time.time() - LIVE_TTL + 900, "v": v}  # при отказе не повторяем запрос 15 минут
            return v
        mk, items = {}, []
        for x in data.get("results", []):
            mp = x.get("market") or {}
            if mp.get("asset") not in (None, "TON") or not isinstance(mp.get("price"), (int, float)):
                continue
            mk[mp["market"]] = min(mk.get(mp["market"], 1e18), float(mp["price"]))
            items.append({"market": mp["market"], "price": float(mp["price"]), "number": x.get("giftNumber"),
                          "model": x.get("model"), "pattern": x.get("pattern"), "backdrop": x.get("backdrop"),
                          "url": f"https://t.me/nft/{name}-{x.get('giftNumber')}"})
        v = {"markets": mk, "items": items[:8], "total": (data.get("pagination") or {}).get("totalItems", len(items)), "ok": True}
        if len(_live_cache) > 500:
            _live_cache.clear()
        _live_cache[key] = {"t": time.time(), "v": v}
        return v


# ---- пользователи: вход по подписанному Telegram initData (проверка HMAC токеном бота) ----
import hashlib
import hmac
import json as _json
import os
from urllib.parse import parse_qsl

from fastapi import Request
from pydantic import BaseModel


def verify_init_data(raw: str) -> int | None:
    """id пользователя из initData, если подпись верна и данные не старше 3 суток."""
    token = os.getenv("BOT_TOKEN", "")
    if not raw or not token:
        return None
    try:
        data = dict(parse_qsl(raw, keep_blank_values=True))
        got = data.pop("hash", "")
        check = chr(10).join(f"{k}={v}" for k, v in sorted(data.items()))
        secret = hmac.new(b"WebAppData", token.encode(), hashlib.sha256).digest()
        if not hmac.compare_digest(hmac.new(secret, check.encode(), hashlib.sha256).hexdigest(), got):
            return None
        if time.time() - int(data.get("auth_date", 0)) > 3 * 86400:
            return None
        return int(_json.loads(data["user"])["id"])
    except Exception:
        return None


OWNER_ID = int(os.getenv("OWNER_ID", "7308147004"))


async def notify_owner(uid: int, username: str | None, name: str, src: str):
    """Сообщение владельцу бота о новом пользователе (в чат с ботом)."""
    if uid == OWNER_ID:
        return
    who = f"@{username}" if username else (name or str(uid))
    text = f"Новый юзер {who}"
    try:
        async with httpx.AsyncClient(timeout=15) as c:
            await c.post(f"https://api.telegram.org/bot{os.getenv('BOT_TOKEN', '')}/sendMessage", json={"chat_id": OWNER_ID, "text": text})
    except Exception as e:
        logging.warning("notify_owner: %s", e)


async def current_user(request: Request) -> int:
    uid = verify_init_data(request.headers.get("x-init-data", ""))
    if uid is None:
        raise HTTPException(401, "telegram auth required")
    lang = "ru"
    try:
        lang = "ru" if str(_json.loads(dict(parse_qsl(request.headers["x-init-data"])).get("user", "{}")).get("language_code", "")).startswith(("ru", "uk", "be", "kk")) else "en"
    except Exception:
        pass
    async with db.connect() as conn:
        is_new = await (await conn.execute("SELECT 1 FROM users WHERE tg_id = ?", (uid,))).fetchone() is None
        await conn.execute("INSERT INTO users (tg_id, first_ts, last_ts, lang) VALUES (?,?,?,?) ON CONFLICT(tg_id) DO UPDATE SET last_ts=excluded.last_ts, lang=excluded.lang",
                           (uid, int(time.time()), int(time.time()), lang))
        await conn.commit()
    if is_new:  # новый пользователь приложения: сообщение владельцу в чат бота
        try:
            u = _json.loads(dict(parse_qsl(request.headers["x-init-data"])).get("user", "{}"))
        except Exception:
            u = {}
        asyncio.create_task(notify_owner(uid, u.get("username"), " ".join(x for x in (u.get("first_name"), u.get("last_name")) if x), "app"))
    return uid


class FavIn(BaseModel):
    kind: str
    gift: str
    model: str = ""


class HoldIn(BaseModel):
    gift: str
    model: str = ""
    qty: float


class AlertIn(BaseModel):
    gift: str
    model: str = ""
    cond: str
    value: float


LIMITS = {"favs": 300, "portfolio": 100, "alerts": 30}


_mine_cache: dict = {}
# Цена с учётом фона: самый дешёвый лот той же модели и с тем же фоном (открытый поиск peek.tg; редко, с общей очередью и паузами)
_bd_price: dict = {}          # (подарок, модель, фон) -> (время, цена или None, если лотов нет)
_bd_state = {"running": False, "cool": 0.0}
BD_TTL = 3 * 3600


async def fill_bd_prices(keys: list):
    if _bd_state["running"] or time.time() < _bd_state["cool"]:
        return
    _bd_state["running"] = True
    try:
        async with httpx.AsyncClient(timeout=20) as c:
            for gift, model, backdrop in keys:
                params = {"name": re.sub(r"[^A-Za-z]", "", gift), "models": model, "backdrops": backdrop,
                          "marketOnly": "true", "sortBy": "price", "sortOrder": "asc", "page": 1, "limit": 10}
                r = await c.get("https://server.peek.tg/api/nft/gifts/search", params=params,
                                headers={"Origin": "https://peek.tg", "Referer": "https://peek.tg/", "User-Agent": "Mozilla/5.0"})
                if r.status_code in (403, 429) or r.status_code >= 500:
                    _bd_state["cool"] = time.time() + 900  # закрыли или лимит: не трогаем 15 минут
                    return
                price, prices = None, []
                for x in r.json().get("results", []):
                    mp = x.get("market") or {}
                    if mp.get("asset") in (None, "TON") and isinstance(mp.get("price"), (int, float)):
                        prices.append(float(mp["price"]))
                if prices:
                    price = min(prices)
                    floor = (await effective_floors()).get(gift, {}).get(model, (None, None))[0]
                    # одиночный завышенный лот («1000» на модель, которая стоит 4) — не цена; такой лот не берём
                    if floor and price > 3 * floor and len(prices) < 2:
                        price = None
                _bd_price[(gift, model, backdrop)] = (time.time(), price)
                async with db.connect() as conn:
                    await conn.execute("INSERT OR REPLACE INTO bd_prices VALUES (?,?,?,?,?)", (gift, model, backdrop, price, int(time.time())))
                    await conn.commit()
                await asyncio.sleep(2.2)  # лимит peek — 35 запросов в минуту, держим вдвое меньше
    except Exception as e:
        logging.warning("bd prices: %s", e)
        _bd_state["cool"] = time.time() + 300
    finally:
        _bd_state["running"] = False


@app.get("/api/my-gifts")
async def api_my_gifts(request: Request):
    """Подарки из профиля самого пользователя: официальный метод Bot API getUserGifts (без чужих аккаунтов).
    Работает для тех, кто открыл бота; видны только подарки, которые показаны в профиле. Кэш 5 минут."""
    uid = await current_user(request)
    hit = _mine_cache.get(uid)
    if hit and time.time() - hit["t"] < (20 if hit["v"].get("pending") else 300):
        return hit["v"]
    token = os.getenv("BOT_TOKEN", "")
    items, total, offset = [], 0, ""
    async with httpx.AsyncClient(timeout=25) as c:
        for _ in range(6):  # до 600 подарков
            r = (await c.post(f"https://api.telegram.org/bot{token}/getUserGifts",
                              json={"user_id": uid, "limit": 100, "offset": offset, "exclude_unique": False})).json()
            if not r.get("ok"):
                logging.warning("getUserGifts: %s", r.get("description"))
                break
            res = r["result"]
            total = res.get("total_count", total)
            for x in res.get("gifts", []):
                if x.get("type") != "unique":
                    continue
                g = x["gift"]
                items.append({"gift": g.get("base_name"), "number": g.get("number"), "slug": g.get("name"),
                              "model": (g.get("model") or {}).get("name"), "model_r": (g.get("model") or {}).get("rarity_per_mille"),
                              "backdrop": (g.get("backdrop") or {}).get("name"), "backdrop_r": (g.get("backdrop") or {}).get("rarity_per_mille"),
                              "symbol": (g.get("symbol") or {}).get("name")})
            offset = res.get("next_offset") or ""
            if not offset:
                break
            await asyncio.sleep(0.2)
    col = {r["name"]: r for r in await q("SELECT name, center, edge FROM backdrops")}
    for it in items:  # цвета фона подарка, чтобы показать его так же, как в Telegram
        c = col.get(it["backdrop"])
        it["c1"], it["c2"] = (c["center"], c["edge"]) if c else (None, None)
    now_t = time.time()
    if not _bd_state.get("loaded"):  # после перезапуска сервера поднимаем сохранённые цены, а не считаем заново
        _bd_state["loaded"] = True
        for r in await q("SELECT gift, model, backdrop, price, ts FROM bd_prices WHERE ts > ?", (int(now_t - BD_TTL),)):
            _bd_price.setdefault((r["gift"], r["model"], r["backdrop"]), (r["ts"], r["price"]))
    need = []
    for it in items:
        k = (it["gift"], it["model"], it["backdrop"])
        hit = _bd_price.get(k)
        it["price_exact"] = hit[1] if hit and now_t - hit[0] < BD_TTL else None
        if (not hit or now_t - hit[0] >= BD_TTL) and it["model"] and it["backdrop"] and k not in need:
            need.append(k)
    if need:
        asyncio.create_task(fill_bd_prices(need))
    v = {"total": total, "items": items, "pending": len(need)}
    _mine_cache[uid] = {"t": time.time(), "v": v}
    return v


# ---- «Поделиться»: карточка-картинка и готовое сообщение (Bot API savePreparedInlineMessage + Mini App shareMessage) ----
_FONT_B = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"
_FONT_R = "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"


def _font(path: str, size: int):
    from PIL import ImageFont
    try:
        return ImageFont.truetype(path, size)
    except OSError:
        try:
            return ImageFont.truetype("arial.ttf", size)
        except OSError:
            return ImageFont.load_default()


def _fit(draw, text: str, path: str, size: int, width: int):
    while size > 22:
        f = _font(path, size)
        if draw.textlength(text, font=f) <= width:
            return f
        size -= 2
    return _font(path, 22)


def _load_img(kind: str, a: str, b: str = ""):
    p = images.path_for(kind, a, b, False)
    if p.exists():
        try:
            return Image.open(p).convert("RGBA")
        except Exception:
            return None
    return None


def render_card(title: str, sub: str, imgs: list) -> bytes:
    """Картинка 1280x720: чёрный фон, розовое свечение, картинки слева, название и цена справа."""
    from PIL import ImageDraw, ImageFilter
    W, H = 1280, 720
    bg = Image.new("RGB", (W, H), (5, 5, 8))
    glow = Image.new("RGB", (W, H), (0, 0, 0))
    gd = ImageDraw.Draw(glow)
    gd.ellipse((-200, -250, 700, 500), fill=(120, 20, 95))
    gd.ellipse((700, 300, 1500, 1000), fill=(70, 25, 120))
    bg = Image.blend(bg, glow.filter(ImageFilter.GaussianBlur(160)), 0.55)
    d = ImageDraw.Draw(bg)
    d.rounded_rectangle((28, 28, W - 28, H - 28), radius=44, outline=(255, 255, 255), width=3)
    imgs = [i for i in imgs if i is not None][:3]
    if imgs:
        if len(imgs) == 1:
            im = imgs[0].resize((420, 420))
            bg.paste(im, (90, 150), im)
        else:
            for k, im in enumerate(imgs):
                im = im.resize((300, 300))
                bg.paste(im, (60 + k * 150, 120 + (k % 2) * 140), im)
    x0 = 560 if len(imgs) == 1 else 620
    tw = W - x0 - 70
    d.text((x0, 230), title, font=_fit(d, title, _FONT_B, 76, tw), fill=(255, 255, 255))
    d.text((x0, 345), sub, font=_fit(d, sub, _FONT_R, 46, tw), fill=(255, 170, 230))
    d.text((x0, 600), "Gift Hub  \u00b7  @gift20bot", font=_font(_FONT_B, 34), fill=(190, 190, 200))
    buf = io.BytesIO()
    bg.save(buf, "JPEG", quality=88)
    return buf.getvalue()


def _card_data(kind: str, key: str, lang: str, gifts_list: list, themes_out: list | None):
    """-> (заголовок, подпись, список картинок, deep-link параметр)"""
    ru = lang == "ru"
    if kind == "g":
        g = next((x for x in gifts_list if images.short(x["name"]) == key), None)
        if not g:
            raise HTTPException(404)
        price = f"{g['floor_ton']:,.0f}".replace(",", " ") if (g.get("floor_ton") or 0) >= 100 else f"{(g.get('floor_ton') or 0):g}"
        sub = f"{price} GRAM" + (f"  \u00b7  {int(g['upgraded']):,}".replace(",", " ") if g.get("upgraded") else "")
        return g["name"], sub, [_load_img("g", images.short(g["name"]))], "g_" + key
    if kind == "s":
        t = next((x for x in (themes_out or []) if x["id"] == key), None)
        if not t:
            raise HTTPException(404)
        title = t["title"] if ru else t.get("title_en") or t["title"]
        sub = f"{t['count']} " + ("моделей" if ru else "models") + (f"  \u00b7  {t['floor_ton']:g} GRAM" if t.get("floor_ton") else "")
        ims = [_load_img("m", images.short(s["gift"]), images.mfile(s["model"])) for s in t["samples"]]
        return title, sub, ims, "t_" + key
    raise HTTPException(404)


@app.get("/card/{kind}/{key}.jpg")
async def share_card(kind: str, key: str, lang: str = "ru"):
    """Публичная картинка-карточка (её забирает сам Telegram)."""
    if not re.fullmatch(r"[a-z0-9_\-]{1,64}", key) or kind not in ("g", "s"):
        raise HTTPException(404)
    gl = await gifts() if kind == "g" else []
    th = (await api_themes()) if kind == "s" else None
    title, sub, ims, _ = _card_data(kind, key, "en" if lang == "en" else "ru", gl, th)
    return Response(render_card(title, sub, ims), media_type="image/jpeg", headers={"Cache-Control": "public, max-age=600"})


class ShareIn(BaseModel):
    kind: str
    key: str
    lang: str = "ru"


@app.post("/api/share-prepare")
async def api_share_prepare(body: ShareIn, request: Request):
    """Готовое сообщение для окна «поделиться» внутри Telegram: картинка, название, цена и кнопка «Открыть»."""
    uid = await current_user(request)
    if body.kind not in ("g", "s") or not re.fullmatch(r"[a-z0-9_\-]{1,64}", body.key):
        raise HTTPException(400, "bad request")
    token = os.getenv("BOT_TOKEN", "")
    base = os.getenv("WEBAPP_URL", "https://gifts.zabeyda.lol").rstrip("/")
    lang = "en" if body.lang == "en" else "ru"
    gl = await gifts() if body.kind == "g" else []
    th = (await api_themes()) if body.kind == "s" else None
    title, sub, _, start = _card_data(body.kind, body.key, lang, gl, th)
    result = {"type": "photo", "id": hashlib.sha1(f"{body.kind}{body.key}{lang}".encode()).hexdigest()[:24],
              "photo_url": f"{base}/card/{body.kind}/{body.key}.jpg?lang={lang}", "thumbnail_url": f"{base}/card/{body.kind}/{body.key}.jpg?lang={lang}",
              "photo_width": 1280, "photo_height": 720, "title": title, "description": sub,
              "caption": f"{title}\n{sub}",
              "reply_markup": {"inline_keyboard": [[{"text": "Open", "url": f"https://t.me/gift20bot?start={start}"}]]}}
    async with httpx.AsyncClient(timeout=20) as c:
        r = await c.post(f"https://api.telegram.org/bot{token}/savePreparedInlineMessage",
                         json={"user_id": uid, "result": result, "allow_user_chats": True, "allow_bot_chats": False,
                               "allow_group_chats": True, "allow_channel_chats": True})
    j = r.json()
    if not j.get("ok"):
        logging.warning("savePreparedInlineMessage: %s", j.get("description"))
        raise HTTPException(502, "telegram error")
    return {"id": j["result"]["id"]}


@app.get("/api/me")
async def api_me(request: Request):
    uid = await current_user(request)
    return {
        "favs": await q("SELECT kind, gift, model FROM favs WHERE tg_id = ? ORDER BY ts DESC", (uid,)),
        "portfolio": await q("SELECT gift, model, qty FROM portfolio WHERE tg_id = ?", (uid,)),
        "alerts": await q("SELECT id, gift, model, cond, value, active FROM alerts WHERE tg_id = ? ORDER BY id DESC", (uid,)),
    }


@app.post("/api/fav")
async def api_fav(body: FavIn, request: Request):
    uid = await current_user(request)
    if body.kind not in ("gift", "model", "set") or len(body.gift) > 80 or len(body.model) > 120:
        raise HTTPException(400, "bad request")
    async with db.connect() as conn:
        cur = await conn.execute("DELETE FROM favs WHERE tg_id=? AND kind=? AND gift=? AND model=?", (uid, body.kind, body.gift, body.model))
        on = cur.rowcount == 0
        if on:
            n = (await (await conn.execute("SELECT COUNT(*) FROM favs WHERE tg_id=?", (uid,))).fetchone())[0]
            if n >= LIMITS["favs"]:
                raise HTTPException(400, "limit")
            await conn.execute("INSERT INTO favs VALUES (?,?,?,?,?)", (uid, body.kind, body.gift, body.model, int(time.time())))
        await conn.commit()
    return {"on": on}


@app.post("/api/portfolio")
async def api_portfolio(body: HoldIn, request: Request):
    uid = await current_user(request)
    if len(body.gift) > 80 or len(body.model) > 120 or not (0 <= body.qty <= 1e7):
        raise HTTPException(400, "bad request")
    async with db.connect() as conn:
        if body.qty == 0:
            await conn.execute("DELETE FROM portfolio WHERE tg_id=? AND gift=? AND model=?", (uid, body.gift, body.model))
        else:
            n = (await (await conn.execute("SELECT COUNT(*) FROM portfolio WHERE tg_id=?", (uid,))).fetchone())[0]
            if n >= LIMITS["portfolio"]:
                raise HTTPException(400, "limit")
            await conn.execute("INSERT INTO portfolio VALUES (?,?,?,?) ON CONFLICT(tg_id,gift,model) DO UPDATE SET qty=excluded.qty",
                               (uid, body.gift, body.model, body.qty))
        await conn.commit()
    return {"ok": True}


@app.post("/api/alerts")
async def api_alert_add(body: AlertIn, request: Request):
    uid = await current_user(request)
    if body.cond not in ("below", "above", "change") or len(body.gift) > 80 or len(body.model) > 120 or not (0 < body.value < 1e9):
        raise HTTPException(400, "bad request")
    async with db.connect() as conn:
        n = (await (await conn.execute("SELECT COUNT(*) FROM alerts WHERE tg_id=?", (uid,))).fetchone())[0]
        if n >= LIMITS["alerts"]:
            raise HTTPException(400, "limit")
        cur = await conn.execute("INSERT INTO alerts (tg_id, gift, model, cond, value) VALUES (?,?,?,?,?)", (uid, body.gift, body.model, body.cond, body.value))
        await conn.commit()
    return {"id": cur.lastrowid}


@app.delete("/api/alerts/{aid}")
async def api_alert_del(aid: int, request: Request):
    uid = await current_user(request)
    async with db.connect() as conn:
        await conn.execute("DELETE FROM alerts WHERE id=? AND tg_id=?", (aid, uid))
        await conn.commit()
    return {"ok": True}


def alert_hit(cond: str, value: float, price: float | None, change: float | None) -> bool:
    if cond == "below":
        return price is not None and price < value
    if cond == "above":
        return price is not None and price > value
    return change is not None and abs(change) >= value


async def alerts_loop():
    """Раз в 2 минуты проверяет уведомления и отправляет сообщения через Bot API. below/above срабатывают один раз, change — не чаще раза в сутки."""
    await asyncio.sleep(60)
    while True:
        try:
            token = os.getenv("BOT_TOKEN", "")
            rows = await q("SELECT a.*, COALESCE(u.lang, 'ru') AS lang FROM alerts a LEFT JOIN users u ON u.tg_id = a.tg_id WHERE a.active = 1")
            if rows and token:
                g_by = {g["name"]: g for g in await gifts()}
                eff, mch = await effective_floors(), await model_changes()
                async with httpx.AsyncClient(timeout=20) as c:
                    for a in rows:
                        if a["cond"] == "change" and time.time() - a["last_fire"] < 86400:
                            continue
                        g = g_by.get(a["gift"])
                        if a["model"]:
                            price = eff.get(a["gift"], {}).get(a["model"], (None, None))[0]
                            change = (mch.get((a["gift"], a["model"])) or {}).get("1d")
                        else:
                            price, change = (g or {}).get("floor_ton"), ((g or {}).get("changes") or {}).get("1d")
                        if not alert_hit(a["cond"], a["value"], price, change):
                            continue
                        name = f"{a['model']} · {a['gift']}" if a["model"] else a["gift"]
                        ru = a["lang"] == "ru"
                        if a["cond"] == "change":
                            text = (f"{name}: изменение за сутки {change:+.2f}% (порог {a['value']:g}%). Цена {price:g} GRAM" if ru else f"{name}: 24h change {change:+.2f}% (threshold {a['value']:g}%). Price {price:g} GRAM")
                        else:
                            sign = ("ниже" if a["cond"] == "below" else "выше") if ru else ("below" if a["cond"] == "below" else "above")
                            text = f"{name}: {price:g} GRAM — {sign} {a['value']:g}" if ru else f"{name}: {price:g} GRAM — {sign} {a['value']:g}"
                        url = os.getenv("WEBAPP_URL", "https://gifts.zabeyda.lol") + ("?s=g_" + re.sub(r"[^a-z ]", "", a["gift"].lower()).strip().replace(" ", "_"))
                        r = await c.post(f"https://api.telegram.org/bot{token}/sendMessage", json={
                            "chat_id": a["tg_id"], "text": text,
                            "reply_markup": {"inline_keyboard": [[{"text": "Open", "web_app": {"url": url}}]]}})
                        async with db.connect() as conn:  # при любом исходе не повторяем: заблокировавшему бота не пишем снова
                            await conn.execute("UPDATE alerts SET last_fire = ?, active = ? WHERE id = ?", (int(time.time()), 1 if a["cond"] == "change" and r.status_code == 200 else 0, a["id"]))
                            await conn.commit()
                        await asyncio.sleep(0.1)
        except Exception as e:
            logging.warning("alerts: %s", e)
        await asyncio.sleep(120)


class StarsIn(BaseModel):
    amount: int


_donate_state = {"last": 0.0}


@app.post("/api/donate/stars")
async def api_donate_stars(body: StarsIn):
    """Ссылка на счёт в Telegram Stars для доната (любая сумма 1–10000)."""
    token = os.getenv("BOT_TOKEN", "")
    if not token or not (1 <= body.amount <= 10000):
        raise HTTPException(400, "bad amount")
    if time.time() - _donate_state["last"] < 0.3:  # не чаще ~3 в секунду на всех
        raise HTTPException(429, "slow down")
    _donate_state["last"] = time.time()
    async with httpx.AsyncClient(timeout=15) as c:
        r = await c.post(f"https://api.telegram.org/bot{token}/createInvoiceLink", json={
            "title": "Gift Hub", "description": "Support the project", "payload": "donate",
            "currency": "XTR", "prices": [{"label": "Donate", "amount": body.amount}]})
    data = r.json()
    if not data.get("ok"):
        logging.warning("invoice: %s", data)
        raise HTTPException(502, "telegram error")
    return {"link": data["result"]}


_all_models_cache: dict = {"t": 0.0, "v": None}


async def all_models() -> list:
    if _all_models_cache["v"] is not None and time.time() - _all_models_cache["t"] < 60:
        return _all_models_cache["v"]
    eff = await effective_floors()
    out = []
    for r in await q("SELECT gift, name, rarity_permille FROM models"):
        price, src = eff.get(r["gift"], {}).get(r["name"], (None, None))
        out.append({"gift": r["gift"], "name": r["name"], "rarity_permille": r["rarity_permille"], "floor_ton": price, "price_src": src})
    out.sort(key=lambda x: (x["floor_ton"] is None, x["floor_ton"] or 0, x["name"]))
    _all_models_cache.update(t=time.time(), v=out)
    return out


@app.get("/api/models/all")
async def api_models_all():
    """Все модели всех подарков, кэш 60 с."""
    return await all_models()


@app.get("/api/models/price")
async def api_models_price(dir: str = "desc", limit: int = 100, q: str = ""):
    """Самые дорогие (dir=desc) или самые дешёвые (dir=asc) модели; не больше limit штук, чтобы грузилось быстро."""
    limit = max(1, min(limit, 200))
    words = [w for w in q.lower().split() if w][:5]
    rows = [r for r in await all_models() if r["floor_ton"] is not None and all(w in (r["name"] + " " + r["gift"]).lower() for w in words)]
    rows.sort(key=lambda r: r["floor_ton"], reverse=(dir != "asc"))
    return rows[:limit]


@app.get("/api/backdrops")
async def api_backdrops():
    """Фоны подарков: цвета и средняя редкость (в промилле); самые редкие первыми."""
    return await q("SELECT name, center, edge, pattern, text, rarity_avg, gifts FROM backdrops ORDER BY rarity_avg, name")


_mc_cache: dict = {"t": 0.0, "v": None}
_mc_lock = asyncio.Lock()
MC_PERIODS = {"1h": 1 / 24, "1d": 1, "1w": 7, "1m": 30, "all": None}


async def model_changes() -> dict:
    """Изменение цены каждой модели за периоды по снимкам источника (кэш 10 минут).

    База сравнения — последний снимок не позже чем «период» назад; если такого снимка нет или он слишком старый
    (источник долго не обновлялся) — период для модели недоступен. «Всё» — самый ранний снимок.
    Строки читаются потоком и по одной модели: в памяти держится только текущая модель, а не вся таблица.
    """
    if time.time() - _mc_cache["t"] < 600 and _mc_cache["v"] is not None:
        return _mc_cache["v"]
    async with _mc_lock:  # пересчёт тяжёлый (читает всю историю цен): одновременные запросы ждут один расчёт, а не запускают свои
        if time.time() - _mc_cache["t"] < 600 and _mc_cache["v"] is not None:
            return _mc_cache["v"]
        return await _model_changes_compute()


async def _model_changes_compute() -> dict:
    out: dict[tuple, dict] = {}

    def finish(key, s, latest):
        ts1, p1 = s[-1]
        if latest - ts1 > 6 * 3600 or p1 < 1:  # модели без свежей цены пропускаем
            return
        res = {}
        for per, days in MC_PERIODS.items():
            if days is None:
                base = s[0][1] if len(s) > 1 else None
            else:
                tgt = ts1 - days * 86400
                older = [(t, p) for t, p in s if t <= tgt]
                base = older[-1][1] if older and tgt - older[-1][0] <= max(days * 86400 * 0.6, 6 * 3600) else None
            res[per] = round((p1 / base - 1) * 100, 2) if base and base >= 1 else None
        out[key] = {"price": p1, **res}

    async with db.connect() as conn:
        cur = await conn.execute("SELECT MAX(ts) AS t FROM floors WHERE market = 'community' AND model != ''")
        latest = (await cur.fetchone())["t"] or 0
        cur = await conn.execute(
            "SELECT gift, model, ts, price_ton FROM floors WHERE market = 'community' AND model != '' ORDER BY gift, model, ts"
        )
        key, cur_s = None, []
        while True:
            rows = await cur.fetchmany(20000)
            if not rows:
                break
            for r in rows:
                k = (r[0], r[1])
                if k != key:
                    if key is not None and cur_s:
                        finish(key, cur_s, latest)
                    key, cur_s = k, []
                cur_s.append((r[2], r[3]))
        if key is not None and cur_s:
            finish(key, cur_s, latest)
    _mc_cache.update(t=time.time(), v=out)
    return out


@app.get("/api/models/top")
async def models_top(period: str = "1d", dir: str = "up", limit: int = 60, by: str = "pct", fill: int = 0):
    """Модели с самым большим ростом (dir=up) или падением (dir=down) цены за период."""
    if period not in MC_PERIODS:
        raise HTTPException(400, "bad period")
    ch = await model_changes()
    rar = {(r["gift"], r["name"]): r["rarity_permille"] for r in await q("SELECT gift, name, rarity_permille FROM models")}
    # у редких моделей цена = единственный лот, и когда дешёвый лот купили, «цена» прыгает в разы — это не рост рынка.
    # Отсекаем такие скачки (до +300% за короткие периоды, до +1000% за год и всё время)
    limit_pct = 1000 if period == "all" else 50 if period == "1h" else 300
    async with db.connect() as conn:  # сколько штук каждой модели существует: улучшенные × доля модели
        upg = await collectors.upgraded_map(conn)
        cur = await conn.execute("SELECT g.name AS gift, s.total FROM gifts g JOIN supply s ON s.gift_id = g.gift_id")
        total = {r["gift"]: r["total"] for r in await cur.fetchall()}
    out, rest = [], []
    for k, v in ch.items():
        pct = v.get(period)
        if pct is None or k not in rar or not rar[k] or abs(pct) > limit_pct:
            continue
        units = (upg.get(k[0]) or total.get(k[0]) or 0) * rar[k] / 1000
        if units <= 0:
            continue
        price = v["price"]
        profit = (price - price / (1 + pct / 100)) * units  # прирост (убыток) капитализации модели за период
        item = {"gift": k[0], "name": k[1], "floor_ton": price, "rarity_permille": rar[k], "change": pct,
                "units": round(units), "profit_ton": profit}
        if (profit > 0) != (dir != "down"):
            rest.append(item)  # не того знака: пригодится, только если нужно добрать до нужного числа
            continue
        out.append(item)
    if by == "pct":  # по проценту; модели, которых меньше 20 штук, отбрасываем — у них цена прыгает из-за одного лота
        out = [x for x in out if x["units"] >= 20]
        out.sort(key=lambda x: x["change"], reverse=(dir != "down"))
    else:
        out.sort(key=lambda x: x["profit_ton"], reverse=(dir != "down"))  # сверху — модели, заработавшие больше всех
    if fill and len(out) < limit:  # добираем ближайшими к нужному знаку, чтобы пузырей всегда было нужное число
        rest = [x for x in rest if x["units"] >= 20] if by == "pct" else rest
        rest.sort(key=lambda x: x["change"] if by == "pct" else x["profit_ton"], reverse=(dir != "down"))
        out += rest[: limit - len(out)]
    return out[: min(limit, 200)]


@app.get("/api/sales")
async def sales(limit: int = 50):
    """Последние реальные продажи."""
    return await q(
        "SELECT ts, gift, num, model, symbol, backdrop, price_ton FROM sales ORDER BY ts DESC LIMIT ?",
        (min(limit, 200),),
    )


async def cap_now() -> tuple[float, float | None]:
    """Текущая капитализация (GRAM, USD): сумма тираж × цена по списку подарков — те же цены, что видит пользователь."""
    total = sum(g["cap_ton"] for g in await gifts() if g["cap_ton"])
    rate = await q("SELECT ton_usd FROM rates ORDER BY ts DESC LIMIT 1")
    return total, (total * rate[0]["ton_usd"] if rate else None)


async def cap_snapshots() -> None:
    """Раз в 15 минут сохраняет живую точку капитализации: так график дальше строится по настоящим данным."""
    await asyncio.sleep(90)  # даём прогреться ценам и курсу
    while True:
        try:
            ton, usd = await cap_now()
            if ton and usd:
                async with db.connect() as conn:
                    await conn.execute("INSERT OR REPLACE INTO cap_live VALUES (?,?,?)", (int(time.time()), ton, usd))
                    await conn.commit()
        except Exception as e:  # один сбой не должен останавливать сбор
            logging.getLogger("cap").error("snapshot failed: %s", e)
        await asyncio.sleep(900)


def drop_spikes(pts: list[dict], win: int = 3, tol: float = 0.015) -> list[dict]:
    """Убирает одиночные выбросы: точка, отличающаяся больше чем на tol от медианы соседей (win с каждой стороны).
    Капитализация меняется плавно, поэтому такие скачки — сбой замера, а не рынок."""
    out = []
    for i, p in enumerate(pts):
        nb = sorted(q["usd"] for j, q in enumerate(pts[max(0, i - win): i + win + 1]) if q is not p)
        if nb and abs(p["usd"] / nb[len(nb) // 2] - 1) > tol:
            continue
        out.append(p)
    return out


@app.get("/api/marketcap")
async def marketcap(days: int = 36500):
    """График капитализации: история по снимкам источника + живые точки + текущее значение.

    У подарков, которых нет в истории источника (Resistance Dog, Durov's Cap, ...), цен в прошлом нет, поэтому к
    историческим точкам добавляется их сегодняшняя капитализация (константа), чтобы кривая не занижалась.
    """
    rate = await q("SELECT ton_usd FROM rates ORDER BY ts DESC LIMIT 1")
    ton_usd = rate[0]["ton_usd"] if rate else None
    glist = await gifts()
    total = sum(g["cap_ton"] for g in glist if g["cap_ton"])
    have = {r["gift"] for r in await q("SELECT DISTINCT gift FROM floors WHERE model = ''")}
    extra = sum(g["cap_ton"] for g in glist if g["cap_ton"] and g["name"] not in have)
    pts = [{"ts": r["ts"], "ton": r["ton"] + extra,
            "usd": r["usd"] + extra * (r["usd"] / r["ton"] if r["ton"] else (ton_usd or 0))}
           for r in await q("SELECT ts, usd, ton FROM marketcap ORDER BY ts")]
    live = await q("SELECT ts, ton, usd FROM cap_live ORDER BY ts")
    if live:  # там, где есть частые живые точки, редкие исторические не нужны: две кривые с разным уровнем давали зигзаг
        pts = [p for p in pts if p["ts"] < live[0]["ts"]] + live
    now_ts = int(time.time())
    pts.append({"ts": now_ts, "ton": total, "usd": total * ton_usd if ton_usd else None})
    pts = sorted((p for p in pts if p["usd"] is not None and p["ts"] > now_ts - days * 86400), key=lambda p: p["ts"])
    pts = drop_spikes(pts)
    return {
        "ton_usd": ton_usd,
        "current": {"ton": total, "usd": total * ton_usd if ton_usd else None} if total else None,
        "points": pts,
    }


@app.get("/api/image/{gift}")
async def image(gift: str):
    """Картинки берутся с api.changes.tg прямо в браузере; эндпоинт-заглушка."""
    raise HTTPException(404)


app.mount("/", StaticFiles(directory=WEB, html=True), name="web")


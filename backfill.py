"""Разовая загрузка истории капитализации из git-истории датасета цен.

Для каждого снимка (раз в BUCKET секунд) берём prices.json всех подарков на тот коммит,
считаем floor коллекции (минимум по моделям из каталога) и сумму floor × тираж.
Курс TON к USD на момент снимка берём с CoinGecko (дневные точки).
Запуск: python backfill.py
"""
import asyncio
import bisect
import json
import logging
from datetime import datetime

import httpx

import db

log = logging.getLogger("backfill")
logging.basicConfig(level=logging.INFO)
REPO = "ssamy2/TelegramGiftsAssests"
API = f"https://api.github.com/repos/{REPO}"
RAW = f"https://raw.githubusercontent.com/{REPO}"
BUCKET = 12 * 3600  # один снимок в 12 часов
PROBE = "models/plush_pepe/prices.json"  # этот файл обновляется в каждом коммите цен


def ts_of(iso: str) -> int:
    return int(datetime.fromisoformat(iso.replace("Z", "+00:00")).timestamp())


async def commits(client: httpx.AsyncClient) -> list[tuple[int, str]]:
    out, page = [], 1
    while page <= 20:
        r = await client.get(f"{API}/commits", params={"path": PROBE, "per_page": 100, "page": page})
        r.raise_for_status()
        batch = r.json()
        if not batch:
            break
        out += [(ts_of(x["commit"]["committer"]["date"]), x["sha"]) for x in batch]
        page += 1
    return sorted(out)


def pick(commits_: list[tuple[int, str]]) -> list[tuple[int, str]]:
    """Последний коммит в каждом интервале BUCKET."""
    by_bucket: dict[int, tuple[int, str]] = {}
    for ts, sha in commits_:
        by_bucket[ts // BUCKET] = (ts, sha)
    return sorted(by_bucket.values())


async def ton_rates(client: httpx.AsyncClient) -> tuple[list[int], list[float]]:
    r = await client.get(
        "https://api.coingecko.com/api/v3/coins/the-open-network/market_chart",
        params={"vs_currency": "usd", "days": "90"},
    )
    r.raise_for_status()
    pts = r.json()["prices"]
    return [int(t / 1000) for t, _ in pts], [p for _, p in pts]


async def main() -> None:
    await db.init()
    async with httpx.AsyncClient(timeout=60, headers={"User-Agent": "gifts-backfill"}, follow_redirects=True) as c:
        snaps = pick(await commits(c))
        log.info("snapshots: %d (%s .. %s)", len(snaps), snaps[0][0], snaps[-1][0])
        rate_ts, rate_px = await ton_rates(c)
        details = (await c.get(f"{RAW}/main/Gifts_Details.json")).json()
        short = {g["full_name"]: g["short_name"] for g in details.get("upgraded", [])}

        async with db.connect() as conn:
            cur = await conn.execute("SELECT g.name, s.total FROM gifts g JOIN supply s ON s.gift_id = g.gift_id")
            supply = {r["name"]: r["total"] for r in await cur.fetchall()}
            cur = await conn.execute("SELECT gift, name FROM models")
            catalog: dict[str, set[str]] = {}
            for r in await cur.fetchall():
                catalog.setdefault(r["gift"], set()).add(r["name"])

        sem = asyncio.Semaphore(12)

        async def prices_at(sha: str, gift: str) -> dict[str, float]:
            async with sem:
                r = await c.get(f"{RAW}/{sha}/models/{short[gift]}/prices.json")
            if r.status_code != 200:
                return {}
            return {m: float(p) for m, p in (json.loads(r.text).get("models") or {}).items()
                    if p and m in catalog.get(gift, ())}

        names = [g for g in supply if g in short]
        for ts, sha in snaps:
            per_gift = await asyncio.gather(*(prices_at(sha, g) for g in names))
            floors = [min(p.values()) if p else None for p in per_gift]
            cap = sum(f * supply[g] for g, f in zip(names, floors) if f)
            if not cap:
                continue
            i = max(0, bisect.bisect_left(rate_ts, ts) - 1)
            usd = cap * rate_px[min(i, len(rate_px) - 1)]
            async with db.connect() as conn:
                await conn.execute("INSERT OR REPLACE INTO marketcap VALUES (?,?,?)", (ts, usd, cap))
                for g, f, prices in zip(names, floors, per_gift):
                    if f:
                        prices = {"": f, **prices}  # '' — floor коллекции, остальное — по моделям
                        await conn.executemany(
                            "INSERT OR IGNORE INTO floors (ts, gift, model, market, price_ton) VALUES (?,?,?,?,?)",
                            [(ts, g, m, "community", p) for m, p in prices.items()],
                        )
                await conn.commit()
            log.info("%s cap=%.0f GRAM (%d gifts priced)", datetime.fromtimestamp(ts), cap, sum(1 for f in floors if f))


if __name__ == "__main__":
    asyncio.run(main())

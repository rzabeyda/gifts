"""Перестраивает историю цен подарков по полю floor_price_ton из git-истории Gifts_Details.json.

Тот же показатель, что используется сейчас, поэтому история и сегодняшняя цена согласованы (раньше история была
«минимум по моделям», а сегодняшняя цена — другая, и графики выглядели как нонсенс).
Запуск: python backfill_details.py
"""
import asyncio
import logging
from datetime import datetime

import httpx

import db

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("backfill_details")
REPO = "ssamy2/TelegramGiftsAssests"
BUCKET = 3 * 3600  # один снимок в 3 часа


def ts_of(iso: str) -> int:
    return int(datetime.fromisoformat(iso.replace("Z", "+00:00")).timestamp())


async def main() -> None:
    await db.init()
    async with httpx.AsyncClient(timeout=60, headers={"User-Agent": "gifts-backfill"}, follow_redirects=True) as c:
        commits, page = [], 1
        while page <= 60:
            r = await c.get(f"https://api.github.com/repos/{REPO}/commits",
                            params={"path": "Gifts_Details.json", "per_page": 100, "page": page})
            if r.status_code != 200:
                log.warning("github %s", r.status_code)
                break
            batch = r.json()
            if not batch:
                break
            commits += [(ts_of(x["commit"]["committer"]["date"]), x["sha"]) for x in batch]
            page += 1
        by_bucket: dict[int, tuple[int, str]] = {}
        for ts, sha in sorted(commits):
            by_bucket[ts // BUCKET] = (ts, sha)
        snaps = sorted(by_bucket.values())
        log.info("коммитов %d, снимков %d", len(commits), len(snaps))
        sem = asyncio.Semaphore(10)

        async def get(ts: int, sha: str):
            async with sem:
                try:
                    r = await c.get(f"https://raw.githubusercontent.com/{REPO}/{sha}/Gifts_Details.json")
                    d = r.json()
                    t = int(d.get("last_updated") or ts)
                    return t, {g["full_name"]: float(g["floor_price_ton"]) for g in d.get("upgraded", []) if g.get("floor_price_ton")}
                except Exception as e:  # отдельный битый снимок пропускаем
                    log.warning("snapshot %s: %s", sha[:8], e)
                    return None

        results = [x for x in await asyncio.gather(*(get(ts, sha) for ts, sha in snaps)) if x and x[1]]
        log.info("загружено снимков: %d", len(results))
        async with db.connect() as conn:
            # старые «цены подарка» (минимум по моделям) другого определения — заменяем целиком
            await conn.execute("DELETE FROM floors WHERE model = '' AND market = 'community'")
            rows = [(t, g, "", "community", p) for t, fl in results for g, p in fl.items()]
            await conn.executemany("INSERT OR REPLACE INTO floors (ts, gift, model, market, price_ton) VALUES (?,?,?,?,?)", rows)
            await conn.commit()
        log.info("записано цен подарков: %d", len(rows))


if __name__ == "__main__":
    asyncio.run(main())

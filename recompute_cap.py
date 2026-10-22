"""Пересчёт истории капитализации по новой формуле (типичная цена модели × тираж) из снимков цен в таблице floors.

Курс USD берём у уже сохранённых точек (отношение usd/ton ближайшей ранней точки).
Запуск: python recompute_cap.py
"""
import asyncio
import bisect
import logging
from collections import defaultdict

import caps
import db

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("recompute")


async def main() -> None:
    await db.init()
    async with db.connect() as conn:
        cur = await conn.execute("SELECT g.name AS gift, s.total FROM gifts g JOIN supply s ON s.gift_id = g.gift_id")
        supply = {r["gift"]: r["total"] for r in await cur.fetchall()}
        import collectors
        upg = await collectors.upgraded_map(conn)  # число улучшенных (оценка) вместо максимального тиража
        supply = {g: upg.get(g) or t for g, t in supply.items()}
        for g, n in upg.items():
            supply.setdefault(g, n)
        cur = await conn.execute("SELECT ts, usd, ton FROM marketcap WHERE ton > 0 ORDER BY ts")
        old = [(r["ts"], r["usd"] / r["ton"]) for r in await cur.fetchall()]
        if not old:
            cur = await conn.execute("SELECT ton_usd FROM rates ORDER BY ts DESC LIMIT 1")
            old = [(0, (await cur.fetchone())["ton_usd"])]
        old_ts = [t for t, _ in old]
        # цена подарка в каждом снимке — то же поле floor_price_ton, что и сегодняшняя цена
        cur = await conn.execute("SELECT ts, gift, price_ton FROM floors WHERE market = 'community' AND model = ''")
        snaps: dict[int, dict[str, float]] = defaultdict(dict)
        for r in await cur.fetchall():
            snaps[r["ts"]][r["gift"]] = r["price_ton"]
        log.info("снимков: %d", len(snaps))
        rows = []
        # неполные снимки (когда в источнике обновилась только часть подарков) искажают сумму — пропускаем
        coverage = {ts: sum(1 for g in snaps[ts] if g in supply) for ts in snaps}
        need = 0.9 * max(coverage.values())
        for ts in sorted(snaps):
            if coverage[ts] < need:
                continue
            cap = 0.0
            for gift, total in supply.items():
                floor = snaps[ts].get(gift)
                if floor:
                    cap += floor * total
            if not cap:
                continue
            i = max(0, bisect.bisect_right(old_ts, ts) - 1)
            rows.append((ts, cap * old[i][1], cap))
        if not rows:
            log.warning("нечего записывать")
            return
        await conn.execute("DELETE FROM marketcap")
        await conn.executemany("INSERT OR REPLACE INTO marketcap VALUES (?,?,?)", rows)
        await conn.commit()
        log.info("записано точек: %d; первая %s, последняя %s GRAM", len(rows), round(rows[0][2]), round(rows[-1][2]))


if __name__ == "__main__":
    asyncio.run(main())

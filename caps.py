"""Капитализация подарка = число улучшенных подарков × цена подарка (floor коллекции — цена самой дешёвой модели).

Точное число улучшенных знает только сам Telegram. Мы оцениваем его по серийным номерам продаж MRKT: номера выдаются
по порядку при улучшении, поэтому максимальный увиденный номер — нижняя граница, а по числу продаж можно поправить
(задача об оценке максимума по выборке).
"""


# Число улучшенных подарков, сверенное пользователем по приложению Gift Charts (21.10.2026). У этих подарков продаж
# слишком мало для оценки по серийным номерам, либо оценка расходится с эталоном (часть подарков сожжена при крафте).
UPGRADED_OVERRIDES = {
    "Plush Pepe": 2830, "Heart Locket": 1950, "Resistance Dog": 14280,
    "Algorithm Cup": 165, "Intelligence Cup": 134,
    # точное current_supply с дашборда Dune (howard_peng/telegram-gifts): оценка по серийным номерам здесь ошибается на +30%
    "Desk Calendar": 266960,
}


def collection_floor(prices: dict[str, float]) -> float | None:
    """Цена подарка = самая дешёвая из цен его моделей."""
    vals = [p for p in prices.values() if p]
    return min(vals) if vals else None


def estimate_upgraded(n: int, max_serial: int, total: int | None) -> int | None:
    """Оценка числа улучшенных: N ≈ max·(1 + 1/n) − 1, не меньше max и не больше максимального тиража."""
    if not n or not max_serial:
        return None
    est = max(max_serial * (1 + 1 / n) - 1, max_serial)
    if total:
        est = min(est, total)
    return round(est)

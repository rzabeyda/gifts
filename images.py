"""Кэш картинок: скачиваем из открытых источников один раз, сжимаем и отдаём с нашего сервера."""
import asyncio
import hashlib
import io
import re
import time
from pathlib import Path
from urllib.parse import quote

import httpx
from PIL import Image

CACHE = Path(__file__).parent / "imgcache"
CACHE.mkdir(exist_ok=True)
SAFE = re.compile(r"^[a-z_]{1,80}$")  # имя подарка в файлах датасета: plush_pepe
SRC = {
    "g": ["https://cdn.jsdelivr.net/gh/ssamy2/TG_Photos@main/webp/by_name/{a}.webp",
          "https://raw.githubusercontent.com/ssamy2/TG_Photos/main/webp/by_name/{a}.webp"],
    # файлы моделей в датасете: имя модели в нижнем регистре, пробелы -> «_», остальные символы сохранены
    "m": ["https://cdn.jsdelivr.net/gh/ssamy2/TelegramGiftsAssests@main/models/{a}/{b}.webp",
          "https://raw.githubusercontent.com/ssamy2/TelegramGiftsAssests/main/models/{a}/{b}.webp"],
}
CHANGES_PNG = "https://api.changes.tg/model/{g}/{m}.png"  # запасной источник: нет в датасете (credit: @GiftChanges)


def short(name: str) -> str:
    """Имя папки подарка в датасете: только латиница, нижний регистр, слова через подчёркивание."""
    return "_".join(re.sub(r"[^a-z ]", "", name.lower()).split())


def mfile(name: str) -> str:
    """Имя файла модели в датасете: нижний регистр, пробелы -> «_»; апострофы, дефисы и т. п. сохраняются."""
    return name.lower().replace(" ", "_")


def ok_part(b: str) -> bool:
    """Имя файла модели безопасно для URL и пути: без разделителей, «..» и управляющих символов."""
    return 0 < len(b) <= 120 and not any(c in b for c in "/\\?#%") and ".." not in b and b.isprintable()


def path_for(kind: str, a: str, b: str, full: bool) -> Path:
    key = b if SAFE.match(b or "x") else hashlib.sha1(b.encode()).hexdigest()[:16]  # странные имена -> хэш
    return CACHE / f"{kind}_{a}_{key}_{512 if full else 112}.webp"


def _resize(data: bytes, path: Path, full: bool) -> None:
    im = Image.open(io.BytesIO(data)).convert("RGBA")
    im.thumbnail((512 if full else 112,) * 2)
    tmp = path.with_suffix(".tmp")  # пишем во временный файл и подменяем целиком: недописанный файл никто не получит
    im.save(tmp, "WEBP", quality=82 if full else 80)
    tmp.replace(path)


async def _get(client: httpx.AsyncClient, url: str) -> bytes | None:
    try:
        r = await client.get(url)
        return r.content if r.status_code == 200 and r.content else None
    except httpx.HTTPError:
        return None


async def ensure(client: httpx.AsyncClient, kind: str, a: str, b: str, full: bool = False,
                 miss_ttl: float = 600, names: tuple[str, str] | None = None) -> Path | None:
    """Путь к готовой картинке; скачивает и сжимает при отсутствии. None — нигде нет.

    names=(подарок, модель) с настоящими названиями включает запасной источник GiftChanges.
    """
    path = path_for(kind, a, b, full)
    if path.exists():
        return path
    miss = path.with_suffix(".miss")  # метка «нет в источнике», чтобы не долбить его повторно
    if miss.exists() and time.time() - miss.stat().st_mtime < miss_ttl:
        return None
    data = None
    for tpl in SRC[kind]:
        data = await _get(client, tpl.format(a=a, b=quote(b, safe="")))
        if data:
            break
    if not data and kind == "m" and names:
        data = await _get(client, CHANGES_PNG.format(g=quote(names[0], safe=""), m=quote(names[1], safe="")))
    try:
        if not data:
            raise ValueError("no source")
        await asyncio.to_thread(_resize, data, path, full)  # CPU-работа не блокирует сервер
        miss.unlink(missing_ok=True)
        return path
    except Exception:
        miss.touch()
        return None

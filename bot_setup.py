"""Разовая настройка бота: аватарка и описание. Запуск: python bot_setup.py"""
import asyncio
import os

from aiogram import Bot
from aiogram.types import FSInputFile, InputProfilePhotoStatic
from dotenv import load_dotenv
from PIL import Image, ImageDraw

load_dotenv()
AVATAR = "avatar.png"
ACC = (62, 166, 255)


def make_avatar(path: str = AVATAR, size: int = 640) -> None:
    """Чёрный фон, подарок с лентой и растущий график."""
    S = 4  # рисуем в 4x и уменьшаем для сглаживания
    im = Image.new("RGB", (size * S, size * S), (0, 0, 0))
    d = ImageDraw.Draw(im)
    u = size * S / 100
    box = lambda x0, y0, x1, y1: (x0 * u, y0 * u, x1 * u, y1 * u)
    d.rounded_rectangle(box(24, 46, 76, 80), radius=4 * u, fill=(24, 24, 28), outline=ACC, width=int(1.6 * u))
    d.rounded_rectangle(box(20, 36, 80, 50), radius=3 * u, fill=(30, 30, 36), outline=ACC, width=int(1.6 * u))
    d.rectangle(box(47, 36, 53, 80), fill=ACC)
    for sx in (-1, 1):  # бант
        d.ellipse(box(50 + sx * 14 - 9, 22, 50 + sx * 14 + 9, 36), outline=ACC, width=int(1.6 * u))
    pts = [(14, 20), (26, 14), (36, 17), (50, 8), (64, 11), (86, 4)]
    d.line([(x * u, y * u) for x, y in pts], fill=ACC, width=int(1.4 * u), joint="curve")
    im.resize((size, size), Image.LANCZOS).save(path)


TEXT = "Мониторинг цен на Telegram-подарки"


def make_banner(path: str = "welcome.png", w: int = 1280, h: int = 720) -> None:
    """Приветственная картинка 16:9. Шрифты Windows; на сервере файл не пересоздаётся."""
    from PIL import ImageFont

    make_avatar("_icon.png", 640)
    im = Image.new("RGB", (w, h), (0, 0, 0))
    icon = Image.open("_icon.png").resize((460, 460), Image.LANCZOS)
    im.paste(icon, (50, 130))
    d = ImageDraw.Draw(im)
    x, maxw = 550, w - 550 - 50

    def fit(text: str, font_file: str, size: int) -> "ImageFont.FreeTypeFont":
        while size > 20:
            f = ImageFont.truetype(font_file, size)
            if d.textlength(text, font=f) <= maxw:
                return f
            size -= 2
        return f

    bold, reg = r"C:\Windows\Fonts\arialbd.ttf", r"C:\Windows\Fonts\arial.ttf"
    l1, l2, l3 = "Мониторинг цен", "на Telegram-подарки", "все модели · цены · капитализация"
    f = fit(l2, bold, 72)  # одинаковый размер для обеих строк заголовка
    d.text((x, 245), l1, font=f, fill=(242, 242, 243))
    d.text((x, 335), l2, font=f, fill=ACC)
    d.text((x, 460), l3, font=fit(l3, reg, 40), fill=(138, 138, 144))
    im.save(path)
    os.remove("_icon.png")


async def main():
    bot = Bot(os.environ["BOT_TOKEN"])
    try:
        await bot.set_my_short_description(short_description=TEXT)
        await bot.set_my_description(description=TEXT)
        print("ok")
    finally:
        await bot.session.close()


if __name__ == "__main__":
    asyncio.run(main())

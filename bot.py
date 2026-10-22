import asyncio
import os
import re
from pathlib import Path

from aiogram import Bot, Dispatcher
from aiogram.filters import Command, CommandObject
from aiogram import F
from aiogram.types import PreCheckoutQuery, FSInputFile, InlineKeyboardButton, InlineKeyboardMarkup, MenuButtonWebApp, Message, WebAppInfo
from dotenv import load_dotenv

load_dotenv()
WEBAPP_URL = os.getenv("WEBAPP_URL", "https://gifts.zabeyda.lol")
WELCOME = Path(__file__).parent / "welcome.jpg"  # картинка из pics/gift.jpg
dp = Dispatcher()


@dp.message(Command("start"))
async def start(m: Message, command: CommandObject):
    # ссылка «Поделиться»: /start g_plush_pepe открывает приложение сразу на нужном экране
    arg = (command.args or "").strip()
    url = WEBAPP_URL + ("?s=" + arg if arg and re.fullmatch(r"[A-Za-z0-9_\-]{1,64}", arg) else "")
    kb = InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text="Open", web_app=WebAppInfo(url=url))]]
    )
    caption = "Мониторинг цен на Telegram-подарки"
    if WELCOME.exists():
        await m.answer_photo(FSInputFile(WELCOME), caption=caption, reply_markup=kb)
    else:
        await m.answer(caption, reply_markup=kb)


@dp.pre_checkout_query()
async def pre_checkout(q: PreCheckoutQuery):
    await q.answer(ok=True)  # доната принимаем всегда


@dp.message(F.successful_payment)
async def paid(m: Message):
    await m.answer("Спасибо за поддержку! / Thank you for your support!")


async def main():
    bot = Bot(os.environ["BOT_TOKEN"])
    await bot.set_chat_menu_button(menu_button=MenuButtonWebApp(text="Open", web_app=WebAppInfo(url=WEBAPP_URL)))
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())

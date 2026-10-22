# Gift Hub

Telegram Mini App + bot that tracks prices of Telegram collectible gifts: every gift and every model, charts, market cap,
themed sets, bubbles view, favorites, portfolio and price alerts. FastAPI + SQLite backend, vanilla JS frontend (`web/index.html`).

## Data sources (no Telegram user account is used)
- GitHub dataset `ssamy2/TelegramGiftsAssests` (prices, hourly)
- `api.changes.tg` (catalog, rarity, images)
- public channel `t.me/s/mrktnotification` (sales)
- Dune dashboard (supply), tonapi (rate), open `peek.tg` summary (number of upgraded gifts)

## Run
```
python -m venv .venv && .venv/bin/pip install -r requirements.txt
cp .env.example .env   # fill BOT_TOKEN
.venv/bin/uvicorn main:app --host 127.0.0.1 --port 8030   # API + web
.venv/bin/python bot.py                                   # bot
```
Put nginx (HTTPS) in front of the API; the Mini App needs an HTTPS domain set in BotFather.

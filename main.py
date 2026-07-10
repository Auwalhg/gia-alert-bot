import asyncio
import html
import logging
import os
import sqlite3
from datetime import datetime, timezone
from typing import Iterable

from dotenv import load_dotenv
from google import genai
from telegram import Update
from telegram.constants import ParseMode
from telegram.ext import Application, CommandHandler, ContextTypes
from telethon import TelegramClient, events
from telethon.sessions import StringSession

load_dotenv()
logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"), format="%(asctime)s | %(levelname)s | %(name)s | %(message)s")
logger = logging.getLogger("gia-alert-bot")

def env_required(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        raise RuntimeError(f"Missing required environment variable: {name}")
    return value

BOT_TOKEN = env_required("BOT_TOKEN")
TG_API_ID = int(env_required("TG_API_ID"))
TG_API_HASH = env_required("TG_API_HASH")
TG_SESSION = env_required("TG_SESSION")
GEMINI_API_KEY = env_required("GEMINI_API_KEY")
CHANNELS = [x.strip().lstrip("@") for x in os.getenv("CHANNELS", "").split(",") if x.strip()]
if not CHANNELS:
    raise RuntimeError("CHANNELS is empty.")

DB_PATH = os.getenv("DB_PATH", "gia_alert.db")
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-2.5-flash")
ADMIN_CHAT_ID = os.getenv("ADMIN_CHAT_ID", "").strip()

db = sqlite3.connect(DB_PATH, check_same_thread=False)
db.execute("CREATE TABLE IF NOT EXISTS subscribers (chat_id INTEGER PRIMARY KEY, created_at TEXT NOT NULL)")
db.execute("CREATE TABLE IF NOT EXISTS processed_posts (channel_key TEXT NOT NULL, message_id INTEGER NOT NULL, processed_at TEXT NOT NULL, PRIMARY KEY(channel_key, message_id))")
db.commit()

ai = genai.Client(api_key=GEMINI_API_KEY)
bot_app = Application.builder().token(BOT_TOKEN).build()
reader = TelegramClient(StringSession(TG_SESSION), TG_API_ID, TG_API_HASH)

def now_utc() -> str:
    return datetime.now(timezone.utc).isoformat()

def add_subscriber(chat_id: int) -> None:
    db.execute("INSERT OR IGNORE INTO subscribers(chat_id, created_at) VALUES (?, ?)", (chat_id, now_utc()))
    db.commit()

def remove_subscriber(chat_id: int) -> None:
    db.execute("DELETE FROM subscribers WHERE chat_id = ?", (chat_id,))
    db.commit()

def subscribers() -> list[int]:
    ids = [int(r[0]) for r in db.execute("SELECT chat_id FROM subscribers")]
    if ADMIN_CHAT_ID:
        try:
            admin = int(ADMIN_CHAT_ID)
            if admin not in ids:
                ids.append(admin)
        except ValueError:
            logger.warning("ADMIN_CHAT_ID is not valid")
    return ids

def was_processed(channel_key: str, message_id: int) -> bool:
    return db.execute("SELECT 1 FROM processed_posts WHERE channel_key=? AND message_id=?", (channel_key, message_id)).fetchone() is not None

def mark_processed(channel_key: str, message_id: int) -> None:
    db.execute("INSERT OR IGNORE INTO processed_posts(channel_key, message_id, processed_at) VALUES (?, ?, ?)", (channel_key, message_id, now_utc()))
    db.commit()

def split_message(text: str, limit: int = 3900) -> Iterable[str]:
    while len(text) > limit:
        cut = text.rfind("\n", 0, limit)
        if cut < 500:
            cut = limit
        yield text[:cut]
        text = text[cut:].lstrip()
    if text:
        yield text

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.effective_chat or not update.message:
        return
    add_subscriber(update.effective_chat.id)
    await update.message.reply_text("✅ An kunna GIA Alert Bot.\n\nZan turo maka sabbin updates tare da fassarar Hausa da GIA risk note.\n\nYi amfani da /status domin duba channels.")

async def cmd_stop(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.effective_chat or not update.message:
        return
    remove_subscriber(update.effective_chat.id)
    await update.message.reply_text("⛔ An dakatar da alerts zuwa wannan chat.")

async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message:
        return
    body = "\n".join(f"• @{name}" for name in CHANNELS)
    await update.message.reply_text(f"🟢 GIA Alert Bot yana aiki.\n\n📡 Jimillar channels: {len(CHANNELS)}\n\n{body}")

def analyze_post(source_name: str, text: str, post_link: str) -> str:
    prompt = f"""Kai ne GIA Crypto Intelligence Translator da Risk Screener.

Ka amsa da HAUSA kawai.
Ka fassara post din zuwa Hausa mai saukin fahimta.
Kada ka kirkiri bayanin da babu shi.
Kada ka ce project verified sai an nuna hujjar official source.
Idan hujja ba ta isa ba, rubuta 'Ba a tabbatar ba'.
Idan akwai gift code, ka fito da code din da reward dinsa a sarari.
Idan akwai airdrop, task, mining, listing, security alert ko deadline, ka bayyana shi.

Ka yi amfani da wannan tsari:

📌 Nau'in Update:
📝 Takaitaccen Bayani:
✅ Abin da ake Bukatar Yi:
🎁 Lada/Fa'ida:
⏰ Wa'adi:
🛡️ Matsayin Tabbaci:
⚠️ Hadari ko Abin Lura:

Source: {source_name}
Post link: {post_link}

Original post:
{text}"""
    response = ai.models.generate_content(model=GEMINI_MODEL, contents=prompt)
    return (response.text or "Ba a samu fassara ba.").strip()

async def broadcast(message: str) -> None:
    targets = subscribers()
    if not targets:
        logger.warning("Babu subscriber. A bude bot a tura /start.")
        return
    for chat_id in targets:
        for part in split_message(message):
            try:
                await bot_app.bot.send_message(chat_id=chat_id, text=part, parse_mode=ParseMode.HTML, disable_web_page_preview=True)
            except Exception:
                logger.exception("Failed to send alert to %s", chat_id)

@reader.on(events.NewMessage(chats=CHANNELS))
async def on_new_post(event) -> None:
    try:
        chat = await event.get_chat()
        username = getattr(chat, "username", None)
        title = getattr(chat, "title", None) or username or "Telegram Channel"
        channel_key = username or str(event.chat_id)
        message_id = int(event.message.id)
        if was_processed(channel_key, message_id):
            return
        original_text = (event.raw_text or "").strip() or "[Post din media ne ba tare da rubutu ba. A bude original post domin ganin hoton ko bidiyon.]"
        post_link = f"https://t.me/{username}/{message_id}" if username else "Babu public post link"
        analysis = await asyncio.to_thread(analyze_post, title, original_text, post_link)
        alert = (
            "🚨 <b>GIA INTELLIGENCE ALERT</b>\n\n"
            f"📡 <b>Source:</b> {html.escape(title)}\n"
            f"🔗 <b>Original Post:</b> {html.escape(post_link)}\n\n"
            f"{html.escape(analysis)}\n\n"
            "━━━━━━━━━━━━━━\n"
            "🧭 <b>Evidence Before Emotion — Bincike Kafin Shawara.</b>"
        )
        await broadcast(alert)
        mark_processed(channel_key, message_id)
        logger.info("Processed %s/%s", channel_key, message_id)
    except Exception:
        logger.exception("Error while processing a Telegram post")

async def main() -> None:
    bot_app.add_handler(CommandHandler("start", cmd_start))
    bot_app.add_handler(CommandHandler("stop", cmd_stop))
    bot_app.add_handler(CommandHandler("status", cmd_status))
    await bot_app.initialize()
    await bot_app.start()
    if bot_app.updater is None:
        raise RuntimeError("Telegram updater is unavailable")
    await bot_app.updater.start_polling(drop_pending_updates=True)
    await reader.start()
    logger.info("GIA Alert Bot started with %s channels", len(CHANNELS))
    try:
        await reader.run_until_disconnected()
    finally:
        if bot_app.updater:
            await bot_app.updater.stop()
        await bot_app.stop()
        await bot_app.shutdown()
        db.close()

if __name__ == "__main__":
    asyncio.run(main())

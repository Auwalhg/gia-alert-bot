import asyncio
import hashlib
import html
import logging
import os
import re
import sqlite3
from datetime import datetime, timezone

import aiohttp
from bs4 import BeautifulSoup
from dotenv import load_dotenv
from google import genai
from telegram import Update
from telegram.constants import ParseMode
from telegram.ext import Application, CommandHandler, ContextTypes

load_dotenv()
logging.basicConfig(level=os.getenv('LOG_LEVEL', 'INFO'), format='%(asctime)s | %(levelname)s | %(message)s')
log = logging.getLogger('gia-v2')

BOT_TOKEN = os.getenv('BOT_TOKEN', '').strip()
GEMINI_API_KEY = os.getenv('GEMINI_API_KEY', '').strip()
CHANNELS = [x.strip().lstrip('@') for x in os.getenv('CHANNELS', '').split(',') if x.strip()]
OFFICIAL = {x.strip().lstrip('@').lower() for x in os.getenv('OFFICIAL_CHANNELS', '').split(',') if x.strip()}
CHECK_INTERVAL = max(60, int(os.getenv('CHECK_INTERVAL', '300')))
REQUEST_DELAY = max(1, int(os.getenv('REQUEST_DELAY', '2')))
ALERT_MODE = os.getenv('ALERT_MODE', 'priority').lower()
MIN_SCORE = max(0, min(100, int(os.getenv('MIN_PRIORITY_SCORE', '55'))))
FIRST_RUN_SILENT = os.getenv('FIRST_RUN_SILENT', 'true').lower() == 'true'
DB_PATH = os.getenv('DB_PATH', 'gia_alert_v2.db')
MODEL = os.getenv('GEMINI_MODEL', 'gemini-2.5-flash')
ADMIN_CHAT_ID = os.getenv('ADMIN_CHAT_ID', '').strip()

if not BOT_TOKEN:
    raise RuntimeError('BOT_TOKEN is missing')
if not CHANNELS:
    raise RuntimeError('CHANNELS is empty')

db = sqlite3.connect(DB_PATH, check_same_thread=False)
db.execute('CREATE TABLE IF NOT EXISTS subscribers(chat_id INTEGER PRIMARY KEY, created_at TEXT NOT NULL)')
db.execute('CREATE TABLE IF NOT EXISTS seen(channel TEXT NOT NULL, post_id INTEGER NOT NULL, seen_at TEXT NOT NULL, PRIMARY KEY(channel,post_id))')
db.execute('CREATE TABLE IF NOT EXISTS alerts(fingerprint TEXT PRIMARY KEY, category TEXT, first_seen TEXT, source_count INTEGER DEFAULT 1)')
db.execute('CREATE TABLE IF NOT EXISTS health(channel TEXT PRIMARY KEY,last_ok TEXT,last_error TEXT)')
db.commit()

ai = genai.Client(api_key=GEMINI_API_KEY) if GEMINI_API_KEY else None
app = Application.builder().token(BOT_TOKEN).build()


def now():
    return datetime.now(timezone.utc).isoformat()


def add_subscriber(chat_id):
    db.execute('INSERT OR IGNORE INTO subscribers VALUES (?,?)', (chat_id, now()))
    db.commit()


def remove_subscriber(chat_id):
    db.execute('DELETE FROM subscribers WHERE chat_id=?', (chat_id,))
    db.commit()


def subscribers():
    ids = [int(r[0]) for r in db.execute('SELECT chat_id FROM subscribers')]
    if ADMIN_CHAT_ID:
        try:
            admin = int(ADMIN_CHAT_ID)
            if admin not in ids:
                ids.append(admin)
        except ValueError:
            pass
    return ids


def is_seen(channel, post_id):
    return db.execute('SELECT 1 FROM seen WHERE channel=? AND post_id=?', (channel, post_id)).fetchone() is not None


def mark_seen(channel, post_id):
    db.execute('INSERT OR IGNORE INTO seen VALUES (?,?,?)', (channel, post_id, now()))
    db.commit()


def seen_count(channel):
    return db.execute('SELECT COUNT(*) FROM seen WHERE channel=?', (channel,)).fetchone()[0]


def set_health(channel, ok, message=''):
    if ok:
        db.execute('INSERT INTO health(channel,last_ok,last_error) VALUES (?,?,NULL) ON CONFLICT(channel) DO UPDATE SET last_ok=excluded.last_ok,last_error=NULL', (channel, now()))
    else:
        db.execute('INSERT INTO health(channel,last_ok,last_error) VALUES (?,NULL,?) ON CONFLICT(channel) DO UPDATE SET last_error=excluded.last_error', (channel, message[:300]))
    db.commit()


def normalize(text):
    text = re.sub(r'https?://\S+', ' ', text.lower())
    return re.sub(r'\s+', ' ', re.sub(r'[^a-z0-9]+', ' ', text)).strip()


def extract_code(text):
    patterns = [
        r'(?:gift|promo|redeem|secret node)\s*code\s*[:\-]\s*([A-Za-z0-9_-]{4,40})',
        r'\bcode\s*[:\-]\s*([A-Za-z0-9_-]{4,40})',
    ]
    for pattern in patterns:
        match = re.search(pattern, text, re.I)
        if match:
            return match.group(1)
    return ''


def extract_reward(text):
    patterns = [
        r'(\+\s*\d+(?:\.\d+)?\s*(?:base rate|rate|points?|tokens?)\s*(?:for|/)\s*\d+\s*(?:hours?|hrs?|h))',
        r'(reward\s*[:\-]\s*[^\n]{2,80})',
    ]
    for pattern in patterns:
        match = re.search(pattern, text, re.I)
        if match:
            return match.group(1).strip()
    return ''


def detect(text, channel):
    lowered = text.lower()
    category, score = 'Other', 25
    rules = [
        ('Security Alert', 95, ['hack', 'phishing', 'exploit', 'drainer', 'security alert']),
        ('Gift Code', 90, ['gift code', 'secret node', 'promo code', 'redeem code']),
        ('Listing', 85, ['listing', 'listed on', 'trading pair', 'spot trading']),
        ('Airdrop', 80, ['airdrop', 'claim', 'snapshot', 'eligibility']),
        ('TGE/Mainnet', 78, ['tge', 'token generation event', 'mainnet']),
        ('Task/Campaign', 68, ['task', 'campaign', 'reward pool']),
        ('Mining', 62, ['mining', 'hashrate', 'base rate']),
        ('News', 50, ['announcement', 'partnership', 'roadmap', 'update']),
    ]
    for name, value, words in rules:
        if any(word in lowered for word in words):
            category, score = name, value
            break

    code = extract_code(text)
    reward = extract_reward(text)
    if code:
        category, score = 'Gift Code', max(score, 92)

    score += 6 if channel.lower() in OFFICIAL else -4
    if any(x in lowered for x in ['seed phrase', 'private key', 'pay activation fee', 'wallet drainer']):
        score -= 35
    score = max(0, min(100, score))

    key = code.lower() if code else normalize(text)[:300]
    fingerprint = hashlib.sha256(f'{category}|{key}'.encode()).hexdigest()
    return category, score, code, reward, fingerprint


def is_duplicate(fingerprint, category):
    row = db.execute('SELECT source_count FROM alerts WHERE fingerprint=?', (fingerprint,)).fetchone()
    if row:
        db.execute('UPDATE alerts SET source_count=source_count+1 WHERE fingerprint=?', (fingerprint,))
        db.commit()
        return True
    db.execute('INSERT INTO alerts VALUES (?,?,?,1)', (fingerprint, category, now()))
    db.commit()
    return False


def source_status(channel):
    return 'Official source' if channel.lower() in OFFICIAL else 'Discovery source — sai an tabbatar daga official channel'


def fallback(channel, category, score, code, reward):
    return (
        f"📌 Nau'in Update: {category}\n"
        f"📝 Takaitaccen Bayani: An samu sabon bayani daga @{channel}.\n"
        "✅ Abin da ake Buƙatar Yi: Buɗe original post domin cikakken bayani.\n"
        f"🎁 Gift Code: {code or 'Ba a samu ba'}\n"
        f"🎁 Lada/Fa'ida: {reward or 'Ba a tantance ba'}\n"
        f"🛡️ Matsayin Tabbaci: {source_status(channel)}\n"
        f"📊 GAIN Priority Score: {score}/100\n"
        "⚠️ Haɗari ko Abin Lura: Kada a biya kuɗi ko haɗa wallet sai an tabbatar."
    )


def analyze(channel, text, link, category, score, code, reward):
    if not ai:
        return fallback(channel, category, score, code, reward)
    prompt = f"""Ka amsa da Hausa kawai. Kada ka ƙirƙiri bayanin da babu shi.
Ka fito da gift code da reward a sarari idan suna cikin rubutun.

Tsari:
📌 Nau'in Update:
📝 Takaitaccen Bayani:
✅ Abin da ake Buƙatar Yi:
🎁 Gift Code:
🎁 Lada/Fa'ida:
⏰ Wa'adi:
🛡️ Matsayin Tabbaci:
📊 GAIN Priority Score: {score}/100
⚠️ Haɗari ko Abin Lura:

Source: @{channel}
Status: {source_status(channel)}
Link: {link}
Detected code: {code or 'none'}
Detected reward: {reward or 'none'}
Original:
{text}"""
    try:
        result = ai.models.generate_content(model=MODEL, contents=prompt)
        return (result.text or fallback(channel, category, score, code, reward)).strip()
    except Exception:
        log.exception('Gemini failed')
        return fallback(channel, category, score, code, reward)


async def fetch_channel(session, channel):
    async with session.get(
        f'https://t.me/s/{channel}',
        headers={'User-Agent': 'Mozilla/5.0'},
        timeout=aiohttp.ClientTimeout(total=30),
    ) as response:
        if response.status != 200:
            raise RuntimeError(f'HTTP {response.status}')
        page = await response.text()

    soup = BeautifulSoup(page, 'html.parser')
    posts = []
    for wrapper in soup.select('.tgme_widget_message_wrap'):
        message = wrapper.select_one('.tgme_widget_message')
        if not message:
            continue
        match = re.search(r'/(\d+)$', message.get('data-post', ''))
        if not match:
            continue
        post_id = int(match.group(1))
        text_element = wrapper.select_one('.tgme_widget_message_text')
        text = text_element.get_text('\n', strip=True) if text_element else '[Media post ba tare da rubutu ba]'
        link_element = wrapper.select_one('a.tgme_widget_message_date')
        link = link_element.get('href') if link_element and link_element.get('href') else f'https://t.me/{channel}/{post_id}'
        posts.append((post_id, text, link))
    return posts


async def send_alert(message):
    for chat_id in subscribers():
        try:
            await app.bot.send_message(chat_id=chat_id, text=message, parse_mode=ParseMode.HTML, disable_web_page_preview=True)
        except Exception:
            log.exception('send failed')


async def process_post(channel, post_id, text, link):
    category, score, code, reward, fingerprint = detect(text, channel)
    if ALERT_MODE == 'priority' and score < MIN_SCORE:
        mark_seen(channel, post_id)
        return
    if is_duplicate(fingerprint, category):
        mark_seen(channel, post_id)
        return

    body = await asyncio.to_thread(analyze, channel, text, link, category, score, code, reward)
    priority = 'HIGH' if score >= 80 else 'MEDIUM' if score >= 55 else 'LOW'
    message = (
        "🚨 <b>GIA INTELLIGENCE ALERT v2</b>\n\n"
        f"📡 <b>Source:</b> @{html.escape(channel)}\n"
        f"🎯 <b>Priority:</b> {priority}\n"
        f"🔗 <b>Original:</b> {html.escape(link)}\n\n"
        f"{html.escape(body)}\n\n"
        "🧭 <b>Evidence Before Emotion — Bincike Kafin Shawara.</b>"
    )
    await send_alert(message)
    mark_seen(channel, post_id)


async def monitor_loop():
    await asyncio.sleep(5)
    async with aiohttp.ClientSession() as session:
        while True:
            for channel in CHANNELS:
                try:
                    posts = await fetch_channel(session, channel)
                    posts.sort(key=lambda item: item[0])
                    latest = posts[-8:]
                    if FIRST_RUN_SILENT and seen_count(channel) == 0:
                        for post_id, _, _ in latest:
                            mark_seen(channel, post_id)
                        set_health(channel, True)
                        await asyncio.sleep(REQUEST_DELAY)
                        continue
                    for post_id, text, link in latest:
                        if not is_seen(channel, post_id):
                            await process_post(channel, post_id, text, link)
                    set_health(channel, True)
                except Exception as exc:
                    set_health(channel, False, str(exc))
                    log.exception('channel failed %s', channel)
                await asyncio.sleep(REQUEST_DELAY)
            await asyncio.sleep(CHECK_INTERVAL)


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat and update.message:
        add_subscriber(update.effective_chat.id)
        await update.message.reply_text('✅ An kunna GIA Alert Bot v2.')


async def cmd_stop(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat and update.message:
        remove_subscriber(update.effective_chat.id)
        await update.message.reply_text('⛔ An dakatar da alerts.')


async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.message:
        alerts = db.execute('SELECT COUNT(*) FROM alerts').fetchone()[0]
        await update.message.reply_text(
            f'🟢 GIA Alert Bot v2 yana aiki.\n📡 Channels: {len(CHANNELS)}\n🎯 Mode: {ALERT_MODE}\n📊 Minimum score: {MIN_SCORE}\n🗃️ Unique alerts: {alerts}'
        )


async def cmd_channels(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.message:
        await update.message.reply_text('📡 Channels:\n\n' + '\n'.join(f'• @{channel}' for channel in CHANNELS))


async def cmd_health(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.message:
        rows = db.execute('SELECT channel,last_ok,last_error FROM health ORDER BY channel').fetchall()
        lines = [f"{'✅' if ok and not err else '⚠️'} @{channel}" for channel, ok, err in rows[:50]]
        await update.message.reply_text('🩺 Channel Health:\n\n' + '\n'.join(lines))


async def cmd_stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.message:
        alerts = db.execute('SELECT COUNT(*) FROM alerts').fetchone()[0]
        posts = db.execute('SELECT COUNT(*) FROM seen').fetchone()[0]
        subs_count = db.execute('SELECT COUNT(*) FROM subscribers').fetchone()[0]
        await update.message.reply_text(
            f'📊 GIA BOT STATS\n\n👥 Subscribers: {subs_count}\n📰 Posts processed: {posts}\n🚨 Unique alerts: {alerts}'
        )


async def main():
    for command, function in [
        ('start', cmd_start),
        ('stop', cmd_stop),
        ('status', cmd_status),
        ('channels', cmd_channels),
        ('health', cmd_health),
        ('stats', cmd_stats),
    ]:
        app.add_handler(CommandHandler(command, function))

    await app.initialize()
    await app.start()
    await app.updater.start_polling(drop_pending_updates=True)
    monitor_task = asyncio.create_task(monitor_loop())
    try:
        await monitor_task
    finally:
        monitor_task.cancel()
        await app.updater.stop()
        await app.stop()
        await app.shutdown()
        db.close()


if __name__ == '__main__':
    asyncio.run(main())

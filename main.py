import asyncio, html, logging, os, re, sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone

import aiohttp
from bs4 import BeautifulSoup
from dotenv import load_dotenv
from google import genai
from telegram import Update
from telegram.constants import ParseMode
from telegram.ext import Application, CommandHandler, ContextTypes

load_dotenv()
logging.basicConfig(level=os.getenv("LOG_LEVEL","INFO"),
                    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s")
log = logging.getLogger("gia-production")

BOT_TOKEN = os.getenv("BOT_TOKEN","").strip()
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY","").strip()
CHANNELS = [x.strip().lstrip("@") for x in os.getenv("CHANNELS","").split(",") if x.strip()]
CHECK_INTERVAL = max(60, int(os.getenv("CHECK_INTERVAL","300")))
REQUEST_DELAY = max(1, int(os.getenv("REQUEST_DELAY","2")))
FIRST_RUN_SILENT = os.getenv("FIRST_RUN_SILENT","true").lower() == "true"
ALERT_MODE = os.getenv("ALERT_MODE","all").lower()
MIN_PRIORITY_SCORE = int(os.getenv("MIN_PRIORITY_SCORE","50"))
DB_PATH = os.getenv("DB_PATH","gia_alert.db")
GEMINI_MODEL = os.getenv("GEMINI_MODEL","gemini-2.5-flash")
ADMIN_CHAT_ID = os.getenv("ADMIN_CHAT_ID","").strip()

if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN is missing")
if not CHANNELS:
    raise RuntimeError("CHANNELS is empty")

db = sqlite3.connect(DB_PATH, check_same_thread=False)
db.execute("CREATE TABLE IF NOT EXISTS subscribers(chat_id INTEGER PRIMARY KEY, created_at TEXT NOT NULL)")
db.execute("CREATE TABLE IF NOT EXISTS seen_posts(channel TEXT NOT NULL, post_id INTEGER NOT NULL, seen_at TEXT NOT NULL, PRIMARY KEY(channel,post_id))")
db.execute("CREATE TABLE IF NOT EXISTS channel_health(channel TEXT PRIMARY KEY, last_ok TEXT, last_error TEXT)")
db.commit()

ai = genai.Client(api_key=GEMINI_API_KEY) if GEMINI_API_KEY else None
app = Application.builder().token(BOT_TOKEN).build()

@dataclass
class Post:
    channel: str
    post_id: int
    text: str
    link: str

def now():
    return datetime.now(timezone.utc).isoformat()

def add_subscriber(chat_id):
    db.execute("INSERT OR IGNORE INTO subscribers(chat_id,created_at) VALUES (?,?)",(chat_id,now()))
    db.commit()

def remove_subscriber(chat_id):
    db.execute("DELETE FROM subscribers WHERE chat_id=?",(chat_id,))
    db.commit()

def get_subscribers():
    ids=[int(r[0]) for r in db.execute("SELECT chat_id FROM subscribers")]
    if ADMIN_CHAT_ID:
        try:
            x=int(ADMIN_CHAT_ID)
            if x not in ids: ids.append(x)
        except ValueError:
            pass
    return ids

def seen(channel, post_id):
    return db.execute("SELECT 1 FROM seen_posts WHERE channel=? AND post_id=?",(channel,post_id)).fetchone() is not None

def mark_seen(channel, post_id):
    db.execute("INSERT OR IGNORE INTO seen_posts(channel,post_id,seen_at) VALUES (?,?,?)",(channel,post_id,now()))
    db.commit()

def seen_count(channel):
    return db.execute("SELECT COUNT(*) FROM seen_posts WHERE channel=?",(channel,)).fetchone()[0]

def set_health(channel, ok, message=""):
    if ok:
        db.execute("INSERT INTO channel_health(channel,last_ok,last_error) VALUES (?,?,NULL) ON CONFLICT(channel) DO UPDATE SET last_ok=excluded.last_ok,last_error=NULL",(channel,now()))
    else:
        db.execute("INSERT INTO channel_health(channel,last_ok,last_error) VALUES (?,NULL,?) ON CONFLICT(channel) DO UPDATE SET last_error=excluded.last_error",(channel,message[:300]))
    db.commit()

def classify(text):
    t=text.lower()
    checks=[
        ("Security Alert",90,["hack","scam","phishing","exploit","security alert"]),
        ("Gift Code",85,["gift code","secret node","promo code","redeem code","code:"]),
        ("Listing",80,["listing","listed on","trading pair","tge"]),
        ("Airdrop",75,["airdrop","claim","eligibility","snapshot"]),
        ("Task",65,["task","campaign","complete","reward"]),
        ("Mining",60,["mining","mine","hashrate","base rate"]),
        ("News",45,["announcement","update","partnership","roadmap"]),
    ]
    for name,score,words in checks:
        if any(w in t for w in words):
            return name,score
    return "Other",20

def fallback(post, category, score):
    return (
        f"📌 Nau'in Update: {category}\n"
        "📝 Takaitaccen Bayani: An samu sabon post daga channel ɗin da ake bibiya.\n"
        "✅ Abin da ake Buƙatar Yi: Buɗe original post domin cikakken bayani.\n"
        "🎁 Lada/Fa'ida: Ba a tantance ba.\n"
        "⏰ Wa'adi: Ba a tantance ba.\n"
        "🛡️ Matsayin Tabbaci: Discovery source — sai an tabbatar daga official channel.\n"
        f"📊 GIA Priority Score: {score}/100\n"
        "⚠️ Haɗari ko Abin Lura: Kada a biya kuɗi ko haɗa wallet sai an tabbatar."
    )

def analyze(post, category, score):
    if not ai:
        return fallback(post,category,score)
    prompt=f'''
Ka amsa da Hausa kawai.
Ka fassara wannan Telegram post zuwa Hausa mai sauƙi.
Kada ka ƙirƙiri bayanin da babu shi.
Idan akwai gift code, ka fito da code da reward a sarari.
Idan akwai task, airdrop, listing, mining, deadline ko security alert, ka bayyana.
Ka rubuta cewa sai an tabbatar daga official channel idan source ɗin discovery ne.

Tsari:
📌 Nau'in Update:
📝 Takaitaccen Bayani:
✅ Abin da ake Buƙatar Yi:
🎁 Lada/Fa'ida:
⏰ Wa'adi:
🛡️ Matsayin Tabbaci:
📊 GIA Priority Score: {score}/100
⚠️ Haɗari ko Abin Lura:

Source: @{post.channel}
Link: {post.link}
Original:
{post.text}
'''
    res=ai.models.generate_content(model=GEMINI_MODEL,contents=prompt)
    return (res.text or fallback(post,category,score)).strip()

async def fetch_channel(session, channel):
    url=f"https://t.me/s/{channel}"
    headers={"User-Agent":"Mozilla/5.0"}
    async with session.get(url,headers=headers,timeout=aiohttp.ClientTimeout(total=30)) as r:
        if r.status != 200:
            raise RuntimeError(f"HTTP {r.status}")
        page=await r.text()
    soup=BeautifulSoup(page,"html.parser")
    out=[]
    for wrap in soup.select(".tgme_widget_message_wrap"):
        msg=wrap.select_one(".tgme_widget_message")
        if not msg: continue
        data=msg.get("data-post","")
        m=re.search(r"/(\d+)$",data)
        if not m: continue
        pid=int(m.group(1))
        text_el=wrap.select_one(".tgme_widget_message_text")
        text=text_el.get_text("\n",strip=True) if text_el else "[Media post ba tare da rubutu ba]"
        link_el=wrap.select_one("a.tgme_widget_message_date")
        link=link_el.get("href") if link_el and link_el.get("href") else f"https://t.me/{channel}/{pid}"
        out.append(Post(channel,pid,text,link))
    return out

async def send_alert(text):
    for chat_id in get_subscribers():
        try:
            await app.bot.send_message(chat_id=chat_id,text=text,parse_mode=ParseMode.HTML,disable_web_page_preview=True)
        except Exception:
            log.exception("Send failed to %s",chat_id)

async def process(post):
    category,score=classify(post.text)
    if ALERT_MODE=="priority" and score < MIN_PRIORITY_SCORE:
        mark_seen(post.channel,post.post_id)
        return
    hausa=await asyncio.to_thread(analyze,post,category,score)
    msg=(
        "🚨 <b>GIA INTELLIGENCE ALERT</b>\n\n"
        f"📡 <b>Source:</b> @{html.escape(post.channel)}\n"
        f"🔗 <b>Original:</b> {html.escape(post.link)}\n\n"
        f"{html.escape(hausa)}\n\n"
        "🧭 <b>Evidence Before Emotion — Bincike Kafin Shawara.</b>"
    )
    await send_alert(msg)
    mark_seen(post.channel,post.post_id)

async def monitor_loop():
    await asyncio.sleep(5)
    async with aiohttp.ClientSession() as session:
        while True:
            for channel in CHANNELS:
                try:
                    posts=await fetch_channel(session,channel)
                    posts.sort(key=lambda p:p.post_id)
                    latest=posts[-8:]
                    if FIRST_RUN_SILENT and seen_count(channel)==0:
                        for p in latest: mark_seen(channel,p.post_id)
                        set_health(channel,True)
                        await asyncio.sleep(REQUEST_DELAY)
                        continue
                    for p in latest:
                        if not seen(channel,p.post_id):
                            await process(p)
                    set_health(channel,True)
                except Exception as e:
                    set_health(channel,False,str(e))
                    log.exception("Channel failed: %s",channel)
                await asyncio.sleep(REQUEST_DELAY)
            await asyncio.sleep(CHECK_INTERVAL)

async def start_cmd(update:Update, context:ContextTypes.DEFAULT_TYPE):
    if update.effective_chat and update.message:
        add_subscriber(update.effective_chat.id)
        await update.message.reply_text("✅ An kunna GIA Alert Bot.")

async def stop_cmd(update:Update, context:ContextTypes.DEFAULT_TYPE):
    if update.effective_chat and update.message:
        remove_subscriber(update.effective_chat.id)
        await update.message.reply_text("⛔ An dakatar da alerts.")

async def status_cmd(update:Update, context:ContextTypes.DEFAULT_TYPE):
    if update.message:
        await update.message.reply_text(f"🟢 Bot yana aiki.\n📡 Channels: {len(CHANNELS)}\n⏱️ Interval: {CHECK_INTERVAL}s\n🎯 Mode: {ALERT_MODE}")

async def channels_cmd(update:Update, context:ContextTypes.DEFAULT_TYPE):
    if update.message:
        await update.message.reply_text("📡 Channels:\n\n" + "\n".join(f"• @{c}" for c in CHANNELS))

async def health_cmd(update:Update, context:ContextTypes.DEFAULT_TYPE):
    if update.message:
        rows=db.execute("SELECT channel,last_ok,last_error FROM channel_health ORDER BY channel").fetchall()
        if not rows:
            await update.message.reply_text("Babu health data tukuna.")
            return
        lines=[f"{'✅' if ok and not err else '⚠️'} @{ch}" for ch,ok,err in rows[:40]]
        await update.message.reply_text("🩺 Channel Health:\n\n" + "\n".join(lines))

async def main():
    app.add_handler(CommandHandler("start",start_cmd))
    app.add_handler(CommandHandler("stop",stop_cmd))
    app.add_handler(CommandHandler("status",status_cmd))
    app.add_handler(CommandHandler("channels",channels_cmd))
    app.add_handler(CommandHandler("health",health_cmd))
    await app.initialize()
    await app.start()
    if app.updater is None: raise RuntimeError("Updater unavailable")
    await app.updater.start_polling(drop_pending_updates=True)
    task=asyncio.create_task(monitor_loop())
    try:
        await task
    finally:
        task.cancel()
        await app.updater.stop()
        await app.stop()
        await app.shutdown()
        db.close()

if __name__=="__main__":
    asyncio.run(main())

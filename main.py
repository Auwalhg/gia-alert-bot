import asyncio
import hashlib
import html
import json
import logging
import os
import re
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

import aiohttp
from bs4 import BeautifulSoup
from dotenv import load_dotenv
from google import genai
from telegram import Update
from telegram.constants import ParseMode
from telegram.ext import Application, CommandHandler, ContextTypes

load_dotenv()

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO"),
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)
log = logging.getLogger("gia-v3")


def env(name: str, default: str = "") -> str:
    return os.getenv(name, default).strip()


BOT_TOKEN = env("BOT_TOKEN")
GEMINI_API_KEY = env("GEMINI_API_KEY")
CHANNELS = [x.strip().lstrip("@") for x in env("CHANNELS").split(",") if x.strip()]
OFFICIAL_CHANNELS = {
    x.strip().lstrip("@").lower()
    for x in env("OFFICIAL_CHANNELS").split(",")
    if x.strip()
}

if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN is missing")
if not CHANNELS:
    raise RuntimeError("CHANNELS is empty")

CHECK_INTERVAL = max(60, int(env("CHECK_INTERVAL", "300")))
REQUEST_DELAY = max(1, int(env("REQUEST_DELAY", "2")))
LATEST_POSTS_PER_CHANNEL = max(1, int(env("LATEST_POSTS_PER_CHANNEL", "8")))
FIRST_RUN_SILENT = env("FIRST_RUN_SILENT", "true").lower() == "true"
ALERT_MODE = env("ALERT_MODE", "priority").lower()
MIN_PRIORITY_SCORE = max(0, min(100, int(env("MIN_PRIORITY_SCORE", "55"))))
DB_PATH = env("DB_PATH", "gia_alert_v3.db")
GEMINI_MODEL = env("GEMINI_MODEL", "gemini-2.5-flash")
ADMIN_CHAT_ID = env("ADMIN_CHAT_ID")
DAILY_REPORT_HOUR_UTC = max(0, min(23, int(env("DAILY_REPORT_HOUR_UTC", "20"))))
WEEKLY_REPORT_DAY = max(0, min(6, int(env("WEEKLY_REPORT_DAY", "6"))))  # Monday=0
GAR_THRESHOLD = max(0, min(100, int(env("GAR_THRESHOLD", "85"))))
BILINGUAL_ALERTS = env("BILINGUAL_ALERTS", "true").lower() == "true"

db = sqlite3.connect(DB_PATH, check_same_thread=False)
db.row_factory = sqlite3.Row

db.executescript("""
CREATE TABLE IF NOT EXISTS subscribers(
    chat_id INTEGER PRIMARY KEY,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS seen_posts(
    channel TEXT NOT NULL,
    post_id INTEGER NOT NULL,
    seen_at TEXT NOT NULL,
    PRIMARY KEY(channel, post_id)
);

CREATE TABLE IF NOT EXISTS alerts(
    fingerprint TEXT PRIMARY KEY,
    category TEXT NOT NULL,
    title TEXT NOT NULL,
    priority_score INTEGER NOT NULL,
    priority_level TEXT NOT NULL,
    gift_code TEXT,
    reward TEXT,
    source_channel TEXT NOT NULL,
    source_link TEXT NOT NULL,
    official INTEGER NOT NULL DEFAULT 0,
    scam_score INTEGER NOT NULL DEFAULT 0,
    gar_candidate INTEGER NOT NULL DEFAULT 0,
    hausa_summary TEXT,
    english_summary TEXT,
    whatsapp_post TEXT,
    evidence_json TEXT,
    first_seen TEXT NOT NULL,
    last_seen TEXT NOT NULL,
    source_count INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE IF NOT EXISTS channel_health(
    channel TEXT PRIMARY KEY,
    last_ok TEXT,
    last_error TEXT
);

CREATE TABLE IF NOT EXISTS report_state(
    report_key TEXT PRIMARY KEY,
    last_sent TEXT NOT NULL
);
""")
db.commit()

ai = genai.Client(api_key=GEMINI_API_KEY) if GEMINI_API_KEY else None
app = Application.builder().token(BOT_TOKEN).build()


@dataclass
class Post:
    channel: str
    post_id: int
    text: str
    link: str


@dataclass
class Analysis:
    category: str
    title: str
    priority_score: int
    priority_level: str
    gift_code: str = ""
    reward: str = ""
    deadline: str = ""
    scam_score: int = 0
    risk_note: str = ""
    official: bool = False
    gar_candidate: bool = False
    hausa_summary: str = ""
    english_summary: str = ""
    whatsapp_post: str = ""
    evidence: dict[str, Any] | None = None
    fingerprint: str = ""


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def now_iso() -> str:
    return now_utc().isoformat()


def add_subscriber(chat_id: int) -> None:
    db.execute(
        "INSERT OR IGNORE INTO subscribers(chat_id, created_at) VALUES (?, ?)",
        (chat_id, now_iso()),
    )
    db.commit()


def remove_subscriber(chat_id: int) -> None:
    db.execute("DELETE FROM subscribers WHERE chat_id=?", (chat_id,))
    db.commit()


def get_subscribers() -> list[int]:
    ids = [int(r["chat_id"]) for r in db.execute("SELECT chat_id FROM subscribers")]
    if ADMIN_CHAT_ID:
        try:
            admin = int(ADMIN_CHAT_ID)
            if admin not in ids:
                ids.append(admin)
        except ValueError:
            log.warning("ADMIN_CHAT_ID is invalid")
    return ids


def is_seen(channel: str, post_id: int) -> bool:
    return db.execute(
        "SELECT 1 FROM seen_posts WHERE channel=? AND post_id=?",
        (channel, post_id),
    ).fetchone() is not None


def mark_seen(channel: str, post_id: int) -> None:
    db.execute(
        "INSERT OR IGNORE INTO seen_posts(channel, post_id, seen_at) VALUES (?, ?, ?)",
        (channel, post_id, now_iso()),
    )
    db.commit()


def seen_count(channel: str) -> int:
    return int(db.execute(
        "SELECT COUNT(*) AS c FROM seen_posts WHERE channel=?",
        (channel,),
    ).fetchone()["c"])


def set_health(channel: str, ok: bool, message: str = "") -> None:
    if ok:
        db.execute(
            """INSERT INTO channel_health(channel,last_ok,last_error)
               VALUES (?, ?, NULL)
               ON CONFLICT(channel) DO UPDATE SET
               last_ok=excluded.last_ok,last_error=NULL""",
            (channel, now_iso()),
        )
    else:
        db.execute(
            """INSERT INTO channel_health(channel,last_ok,last_error)
               VALUES (?, NULL, ?)
               ON CONFLICT(channel) DO UPDATE SET
               last_error=excluded.last_error""",
            (channel, message[:500]),
        )
    db.commit()


def source_is_official(channel: str) -> bool:
    return channel.lower() in OFFICIAL_CHANNELS


def normalize(text: str) -> str:
    text = re.sub(r"https?://\S+", " ", text.lower())
    text = re.sub(r"[^a-z0-9\u0600-\u06ff]+", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def extract_code(text: str) -> str:
    patterns = [
        r"(?:gift|promo|redeem|bonus|voucher|secret node|claim)\s*code\s*[:\-]\s*([A-Za-z0-9_-]{4,40})",
        r"\bcode\s*[:\-]\s*([A-Za-z0-9_-]{4,40})",
        r"\bpassword\s*[:\-]\s*([A-Za-z0-9_-]{4,40})",
    ]
    for pattern in patterns:
        m = re.search(pattern, text, re.I)
        if m:
            return m.group(1)
    return ""


def extract_reward(text: str) -> str:
    patterns = [
        r"(\+\s*\d+(?:\.\d+)?\s*(?:base rate|rate|uvx|points?|xp|tokens?)\s*(?:for|/)?\s*\d*\s*(?:hours?|hrs?|h)?)",
        r"(reward\s*[:\-]\s*[^\n]{2,100})",
        r"(\d+(?:\.\d+)?\s*(?:usdt|usd|points?|tokens?|hours?|hrs?)\b)",
    ]
    for pattern in patterns:
        m = re.search(pattern, text, re.I)
        if m:
            return m.group(1).strip()
    return ""


def heuristic_analysis(post: Post) -> Analysis:
    t = post.text.lower()
    category = "Other"
    score = 25
    title = "Sabon Telegram Update"

    rules = [
        ("Security Alert", 96, ["hack", "phishing", "exploit", "drainer", "stolen", "security alert"]),
        ("Gift Code", 92, ["gift code", "secret node", "promo code", "redeem code", "voucher code"]),
        ("Listing", 88, ["listing", "listed on", "trading pair", "spot trading", "futures"]),
        ("Airdrop", 82, ["airdrop", "claim", "snapshot", "eligibility"]),
        ("TGE/Mainnet", 80, ["tge", "token generation event", "mainnet", "main net"]),
        ("Task/Campaign", 70, ["task", "campaign", "reward pool", "complete to earn"]),
        ("Mining", 65, ["mining", "mine", "hashrate", "base rate"]),
        ("Partnership/News", 52, ["partnership", "roadmap", "announcement", "update"]),
    ]

    for name, base, words in rules:
        if any(w in t for w in words):
            category = name
            score = base
            title = name
            break

    gift_code = extract_code(post.text)
    reward = extract_reward(post.text)

    if gift_code:
        category = "Gift Code"
        title = f"Gift Code: {gift_code}"
        score = max(score, 94)

    official = source_is_official(post.channel)
    score += 5 if official else -3

    scam_score = 0
    risk_terms = {
        "seed phrase": 100,
        "private key": 100,
        "wallet drainer": 100,
        "send usdt": 75,
        "pay activation fee": 70,
        "connect wallet": 25,
        "dm admin": 15,
    }
    matched = []
    for term, value in risk_terms.items():
        if term in t:
            scam_score = max(scam_score, value)
            matched.append(term)

    if scam_score >= 70:
        score -= 35

    score = max(0, min(100, score))
    priority = "CRITICAL" if score >= 95 else "HIGH" if score >= 80 else "MEDIUM" if score >= 55 else "LOW"
    gar_candidate = score >= GAR_THRESHOLD and scam_score < 40

    key = gift_code.lower() if gift_code else normalize(post.text)[:400]
    fingerprint = hashlib.sha256(f"{category}|{key}".encode()).hexdigest()

    risk_note = (
        "Babban haɗari: " + ", ".join(matched)
        if matched else
        "Kada a biya kuɗi ko haɗa wallet sai an tabbatar daga official source."
    )

    hausa = (
        f"An samu sabon {category} daga @{post.channel}. "
        f"{'An gano code: ' + gift_code + '. ' if gift_code else ''}"
        f"{'Lada: ' + reward + '. ' if reward else ''}"
        f"Matsayin tabbaci: {'Official source' if official else 'Discovery source'}."
    )
    english = (
        f"New {category} detected from @{post.channel}. "
        f"{'Code: ' + gift_code + '. ' if gift_code else ''}"
        f"{'Reward: ' + reward + '. ' if reward else ''}"
        f"Source status: {'Official' if official else 'Discovery'}."
    )
    whatsapp = (
        f"🚨 *GIA ALERT*\n\n"
        f"📌 *Type:* {category}\n"
        f"📡 *Source:* @{post.channel}\n"
        f"🎯 *Priority:* {priority} ({score}/100)\n"
        f"🎁 *Code:* {gift_code or 'Ba a samu ba'}\n"
        f"🏆 *Reward:* {reward or 'Ba a tantance ba'}\n"
        f"⚠️ *Risk:* {risk_note}\n\n"
        f"🔗 {post.link}\n\n"
        f"_Evidence Before Emotion — Bincike Kafin Shawara._"
    )

    return Analysis(
        category=category,
        title=title,
        priority_score=score,
        priority_level=priority,
        gift_code=gift_code,
        reward=reward,
        scam_score=scam_score,
        risk_note=risk_note,
        official=official,
        gar_candidate=gar_candidate,
        hausa_summary=hausa,
        english_summary=english,
        whatsapp_post=whatsapp,
        evidence={
            "channel": post.channel,
            "post_id": post.post_id,
            "link": post.link,
            "official": official,
            "matched_risk_terms": matched,
        },
        fingerprint=fingerprint,
    )


def safe_json(text: str) -> dict[str, Any]:
    text = text.strip()
    text = re.sub(r"^```json\s*", "", text, flags=re.I)
    text = re.sub(r"\s*```$", "", text)
    start = text.find("{")
    end = text.rfind("}")
    if start == -1 or end == -1:
        return {}
    try:
        return json.loads(text[start:end + 1])
    except json.JSONDecodeError:
        return {}


def ai_enrich(post: Post, base: Analysis) -> Analysis:
    if not ai:
        return base

    prompt = f"""
Return valid JSON only.

Analyze this crypto Telegram post using GIA intelligence standards.
Do not invent facts. Keep missing values as empty strings.

JSON keys:
category, title, priority_score, gift_code, reward, deadline,
scam_score, risk_note, hausa_summary, english_summary, whatsapp_post,
gar_candidate, evidence_points

Rules:
- priority_score: 0-100
- scam_score: 0-100
- gar_candidate: boolean
- Hausa summary must be clear Hausa.
- English summary must be concise.
- WhatsApp post must be ready to paste.
- Discovery sources must not be called verified.
- Official status supplied below must be respected.

Source: @{post.channel}
Official: {base.official}
Link: {post.link}
Heuristic category: {base.category}
Heuristic score: {base.priority_score}
Detected code: {base.gift_code}
Detected reward: {base.reward}

Post:
{post.text}
"""
    try:
        response = ai.models.generate_content(model=GEMINI_MODEL, contents=prompt)
        data = safe_json(response.text or "")
        if not data:
            return base

        score = max(0, min(100, int(data.get("priority_score", base.priority_score))))
        scam = max(0, min(100, int(data.get("scam_score", base.scam_score))))
        priority = "CRITICAL" if score >= 95 else "HIGH" if score >= 80 else "MEDIUM" if score >= 55 else "LOW"

        base.category = str(data.get("category") or base.category)
        base.title = str(data.get("title") or base.title)
        base.priority_score = score
        base.priority_level = priority
        base.gift_code = str(data.get("gift_code") or base.gift_code)
        base.reward = str(data.get("reward") or base.reward)
        base.deadline = str(data.get("deadline") or "")
        base.scam_score = scam
        base.risk_note = str(data.get("risk_note") or base.risk_note)
        base.hausa_summary = str(data.get("hausa_summary") or base.hausa_summary)
        base.english_summary = str(data.get("english_summary") or base.english_summary)
        base.whatsapp_post = str(data.get("whatsapp_post") or base.whatsapp_post)
        base.gar_candidate = bool(data.get("gar_candidate", base.gar_candidate)) and scam < 40
        points = data.get("evidence_points")
        if isinstance(points, list):
            base.evidence = {**(base.evidence or {}), "ai_evidence_points": points}

        key = base.gift_code.lower() if base.gift_code else normalize(post.text)[:400]
        base.fingerprint = hashlib.sha256(f"{base.category}|{key}".encode()).hexdigest()
        return base
    except Exception:
        log.exception("AI enrichment failed")
        return base


def register_alert(a: Analysis, post: Post) -> bool:
    row = db.execute(
        "SELECT source_count FROM alerts WHERE fingerprint=?",
        (a.fingerprint,),
    ).fetchone()

    if row:
        db.execute(
            "UPDATE alerts SET last_seen=?, source_count=source_count+1 WHERE fingerprint=?",
            (now_iso(), a.fingerprint),
        )
        db.commit()
        return False

    db.execute(
        """INSERT INTO alerts(
            fingerprint,category,title,priority_score,priority_level,
            gift_code,reward,source_channel,source_link,official,scam_score,
            gar_candidate,hausa_summary,english_summary,whatsapp_post,
            evidence_json,first_seen,last_seen,source_count
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,1)""",
        (
            a.fingerprint, a.category, a.title, a.priority_score, a.priority_level,
            a.gift_code, a.reward, post.channel, post.link, int(a.official),
            a.scam_score, int(a.gar_candidate), a.hausa_summary, a.english_summary,
            a.whatsapp_post, json.dumps(a.evidence or {}, ensure_ascii=False),
            now_iso(), now_iso(),
        ),
    )
    db.commit()
    return True


async def fetch_channel(session: aiohttp.ClientSession, channel: str) -> list[Post]:
    url = f"https://t.me/s/{channel}"
    async with session.get(
        url,
        headers={"User-Agent": "Mozilla/5.0"},
        timeout=aiohttp.ClientTimeout(total=30),
    ) as resp:
        if resp.status != 200:
            raise RuntimeError(f"HTTP {resp.status}")
        page = await resp.text()

    soup = BeautifulSoup(page, "html.parser")
    posts: list[Post] = []

    for wrap in soup.select(".tgme_widget_message_wrap"):
        msg = wrap.select_one(".tgme_widget_message")
        if not msg:
            continue

        data_post = msg.get("data-post", "")
        m = re.search(r"/(\d+)$", data_post)
        if not m:
            continue

        post_id = int(m.group(1))
        text_el = wrap.select_one(".tgme_widget_message_text")
        text = text_el.get_text("\n", strip=True) if text_el else "[Media post ba tare da rubutu ba]"
        link_el = wrap.select_one("a.tgme_widget_message_date")
        link = link_el.get("href") if link_el and link_el.get("href") else f"https://t.me/{channel}/{post_id}"
        posts.append(Post(channel, post_id, text, link))

    return posts


async def broadcast(message: str) -> None:
    for chat_id in get_subscribers():
        try:
            await app.bot.send_message(
                chat_id=chat_id,
                text=message,
                parse_mode=ParseMode.HTML,
                disable_web_page_preview=True,
            )
        except Exception:
            log.exception("Failed to send to %s", chat_id)


def alert_message(a: Analysis, post: Post) -> str:
    official = "✅ Official" if a.official else "🔎 Discovery"
    gar = "\n👑 <b>GAR Candidate:</b> YES" if a.gar_candidate else ""
    scam = f"\n🛡️ <b>Scam Score:</b> {a.scam_score}/100"

    body = (
        "🚨 <b>GIA INTELLIGENCE ALERT v3</b>\n\n"
        f"📌 <b>Type:</b> {html.escape(a.category)}\n"
        f"📡 <b>Source:</b> @{html.escape(post.channel)} ({official})\n"
        f"🎯 <b>Priority:</b> {a.priority_level} ({a.priority_score}/100)"
        f"{scam}{gar}\n"
        f"🎁 <b>Code:</b> {html.escape(a.gift_code or 'Ba a samu ba')}\n"
        f"🏆 <b>Reward:</b> {html.escape(a.reward or 'Ba a tantance ba')}\n"
        f"⏰ <b>Deadline:</b> {html.escape(a.deadline or 'Ba a bayyana ba')}\n\n"
        f"🇳🇬 <b>Hausa:</b>\n{html.escape(a.hausa_summary)}\n"
    )
    if BILINGUAL_ALERTS:
        body += f"\n🇬🇧 <b>English:</b>\n{html.escape(a.english_summary)}\n"

    body += (
        f"\n⚠️ <b>Risk:</b> {html.escape(a.risk_note)}\n"
        f"🔗 <b>Evidence:</b> {html.escape(post.link)}\n\n"
        "🧭 <b>Evidence Before Emotion — Bincike Kafin Shawara.</b>"
    )
    return body


async def process_post(post: Post) -> None:
    analysis = heuristic_analysis(post)
    analysis = await asyncio.to_thread(ai_enrich, post, analysis)

    if ALERT_MODE == "priority" and analysis.priority_score < MIN_PRIORITY_SCORE:
        mark_seen(post.channel, post.post_id)
        return

    if not register_alert(analysis, post):
        mark_seen(post.channel, post.post_id)
        return

    await broadcast(alert_message(analysis, post))
    mark_seen(post.channel, post.post_id)


async def monitor_loop() -> None:
    await asyncio.sleep(5)
    async with aiohttp.ClientSession() as session:
        while True:
            for channel in CHANNELS:
                try:
                    posts = await fetch_channel(session, channel)
                    posts.sort(key=lambda p: p.post_id)
                    latest = posts[-LATEST_POSTS_PER_CHANNEL:]

                    if FIRST_RUN_SILENT and seen_count(channel) == 0:
                        for post in latest:
                            mark_seen(channel, post.post_id)
                        set_health(channel, True)
                        await asyncio.sleep(REQUEST_DELAY)
                        continue

                    for post in latest:
                        if not is_seen(channel, post.post_id):
                            await process_post(post)

                    set_health(channel, True)

                except Exception as exc:
                    set_health(channel, False, str(exc))
                    log.exception("Channel failed: %s", channel)

                await asyncio.sleep(REQUEST_DELAY)

            await asyncio.sleep(CHECK_INTERVAL)


def build_report(since: datetime, title: str) -> str:
    rows = db.execute(
        """SELECT * FROM alerts
           WHERE first_seen >= ?
           ORDER BY priority_score DESC, first_seen DESC
           LIMIT 25""",
        (since.isoformat(),),
    ).fetchall()

    if not rows:
        return f"📊 <b>{title}</b>\n\nBabu sabon unique alert a wannan lokacin."

    counts: dict[str, int] = {}
    high = 0
    gar = 0
    lines = []

    for row in rows:
        counts[row["category"]] = counts.get(row["category"], 0) + 1
        if row["priority_score"] >= 80:
            high += 1
        if row["gar_candidate"]:
            gar += 1

        lines.append(
            f"• <b>{html.escape(row['category'])}</b> — "
            f"{row['priority_level']} {row['priority_score']}/100 — "
            f"@{html.escape(row['source_channel'])}"
        )

    category_text = ", ".join(f"{k}: {v}" for k, v in sorted(counts.items()))
    return (
        f"📊 <b>{title}</b>\n\n"
        f"🚨 Unique alerts: {len(rows)}\n"
        f"🔥 High/Critical: {high}\n"
        f"👑 GAR candidates: {gar}\n"
        f"📂 Categories: {html.escape(category_text)}\n\n"
        + "\n".join(lines[:15])
    )


def report_sent_today(key: str) -> bool:
    row = db.execute(
        "SELECT last_sent FROM report_state WHERE report_key=?",
        (key,),
    ).fetchone()
    if not row:
        return False
    try:
        last = datetime.fromisoformat(row["last_sent"])
        return last.date() == now_utc().date()
    except ValueError:
        return False


def mark_report_sent(key: str) -> None:
    db.execute(
        """INSERT INTO report_state(report_key,last_sent) VALUES (?,?)
           ON CONFLICT(report_key) DO UPDATE SET last_sent=excluded.last_sent""",
        (key, now_iso()),
    )
    db.commit()


async def report_loop() -> None:
    while True:
        current = now_utc()

        if current.hour == DAILY_REPORT_HOUR_UTC and not report_sent_today("daily"):
            await broadcast(build_report(current - timedelta(days=1), "GIA DAILY INTELLIGENCE REPORT"))
            mark_report_sent("daily")

        if (
            current.weekday() == WEEKLY_REPORT_DAY
            and current.hour == DAILY_REPORT_HOUR_UTC
            and not report_sent_today("weekly")
        ):
            await broadcast(build_report(current - timedelta(days=7), "GIA WEEKLY INTELLIGENCE REPORT"))
            mark_report_sent("weekly")

        await asyncio.sleep(300)


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_chat and update.message:
        add_subscriber(update.effective_chat.id)
        await update.message.reply_text("✅ An kunna GIA Alert Bot v3.0.")


async def cmd_stop(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_chat and update.message:
        remove_subscriber(update.effective_chat.id)
        await update.message.reply_text("⛔ An dakatar da alerts.")


async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message:
        return

    total = db.execute("SELECT COUNT(*) AS c FROM alerts").fetchone()["c"]
    high = db.execute(
        "SELECT COUNT(*) AS c FROM alerts WHERE priority_score>=80"
    ).fetchone()["c"]
    gar = db.execute(
        "SELECT COUNT(*) AS c FROM alerts WHERE gar_candidate=1"
    ).fetchone()["c"]

    await update.message.reply_text(
        "🟢 GIA Alert Bot v3.0 yana aiki.\n"
        f"📡 Channels: {len(CHANNELS)}\n"
        f"🎯 Alert mode: {ALERT_MODE}\n"
        f"📊 Minimum score: {MIN_PRIORITY_SCORE}\n"
        f"🚨 Unique alerts: {total}\n"
        f"🔥 High/Critical: {high}\n"
        f"👑 GAR candidates: {gar}"
    )


async def cmd_channels(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.message:
        await update.message.reply_text(
            "📡 Channels:\n\n" + "\n".join(f"• @{c}" for c in CHANNELS)
        )


async def cmd_health(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message:
        return

    rows = db.execute(
        "SELECT channel,last_ok,last_error FROM channel_health ORDER BY channel"
    ).fetchall()
    if not rows:
        await update.message.reply_text("Babu health data tukuna.")
        return

    lines = [f"{'✅' if r['last_ok'] and not r['last_error'] else '⚠️'} @{r['channel']}" for r in rows]
    await update.message.reply_text("🩺 Channel Health:\n\n" + "\n".join(lines[:50]))


async def cmd_stats(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message:
        return

    stats = {
        "subscribers": db.execute("SELECT COUNT(*) AS c FROM subscribers").fetchone()["c"],
        "posts": db.execute("SELECT COUNT(*) AS c FROM seen_posts").fetchone()["c"],
        "alerts": db.execute("SELECT COUNT(*) AS c FROM alerts").fetchone()["c"],
        "gar": db.execute("SELECT COUNT(*) AS c FROM alerts WHERE gar_candidate=1").fetchone()["c"],
    }

    await update.message.reply_text(
        "📊 GIA BOT STATS v3\n\n"
        f"👥 Subscribers: {stats['subscribers']}\n"
        f"📰 Posts processed: {stats['posts']}\n"
        f"🚨 Unique alerts: {stats['alerts']}\n"
        f"👑 GAR candidates: {stats['gar']}"
    )


async def cmd_daily(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.message:
        await update.message.reply_text(
            build_report(now_utc() - timedelta(days=1), "GIA DAILY INTELLIGENCE REPORT"),
            parse_mode=ParseMode.HTML,
            disable_web_page_preview=True,
        )


async def cmd_weekly(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.message:
        await update.message.reply_text(
            build_report(now_utc() - timedelta(days=7), "GIA WEEKLY INTELLIGENCE REPORT"),
            parse_mode=ParseMode.HTML,
            disable_web_page_preview=True,
        )


async def cmd_gar(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message:
        return

    rows = db.execute(
        """SELECT title,priority_score,source_channel,source_link
           FROM alerts WHERE gar_candidate=1
           ORDER BY priority_score DESC LIMIT 15"""
    ).fetchall()

    if not rows:
        await update.message.reply_text("👑 Babu GAR candidate tukuna.")
        return

    text = "👑 GAR CANDIDATES\n\n" + "\n".join(
        f"• {r['title']} — {r['priority_score']}/100 — @{r['source_channel']}"
        for r in rows
    )
    await update.message.reply_text(text)


async def cmd_whatsapp(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message:
        return

    row = db.execute(
        "SELECT whatsapp_post FROM alerts ORDER BY first_seen DESC LIMIT 1"
    ).fetchone()

    if not row:
        await update.message.reply_text("Babu alert tukuna.")
        return

    await update.message.reply_text(row["whatsapp_post"])


async def main() -> None:
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("stop", cmd_stop))
    app.add_handler(CommandHandler("status", cmd_status))
    app.add_handler(CommandHandler("channels", cmd_channels))
    app.add_handler(CommandHandler("health", cmd_health))
    app.add_handler(CommandHandler("stats", cmd_stats))
    app.add_handler(CommandHandler("daily", cmd_daily))
    app.add_handler(CommandHandler("weekly", cmd_weekly))
    app.add_handler(CommandHandler("gar", cmd_gar))
    app.add_handler(CommandHandler("whatsapp", cmd_whatsapp))

    await app.initialize()
    await app.start()

    if app.updater is None:
        raise RuntimeError("Updater unavailable")

    await app.updater.start_polling(drop_pending_updates=True)
    monitor_task = asyncio.create_task(monitor_loop())
    report_task = asyncio.create_task(report_loop())

    log.info("GIA Alert Bot v3 started with %d channels", len(CHANNELS))

    try:
        await asyncio.gather(monitor_task, report_task)
    finally:
        monitor_task.cancel()
        report_task.cancel()
        await app.updater.stop()
        await app.stop()
        await app.shutdown()
        db.close()


if __name__ == "__main__":
    asyncio.run(main())

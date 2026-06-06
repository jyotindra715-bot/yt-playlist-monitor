import os
import asyncio
import logging
import re
from datetime import datetime, timezone

import httpx
from playwright.async_api import async_playwright
from telegram import Update
from telegram.ext import ApplicationBuilder, CommandHandler, MessageHandler, filters, ContextTypes

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

BOT_TOKEN = os.environ["BOT_TOKEN"]
CHAT_ID = int(os.environ["CHAT_ID"])
YT_API_KEY = os.environ["YT_API_KEY"]

PLAYLISTS = {
    "BABYMONSTER":  "PLuWI86ItS2gEykz3Xy1MyaQxFOcnYbUr9",
    "BLACKPINK":    "PLuWI86ItS2gH_xUTgC8xgiq03StltmpMk",
    "TREASURE":     "PLuWI86ItS2gGxsh4hl2ZHeoyAWIm471HK",
    "CHOOM":        "PLD-A2t1CuHHCa_GyD-2pXVuZLqQIvKLUs",
    "YG STUDIO":    "PLuWI86ItS2gGnlX-NDajZGDugWie_ZZlp",
    "NEXT MONSTER":        "PLuWI86ItS2gFnopUHOZyCtFYxpwsjNRNm",
    "SUGAR HONEY ICE TEA": "PLD-A2t1CuHHB0Tz9lNEahFd8BHnFEoy6C",
}

# API method state
last_count: dict[str, int | None] = {name: None for name in PLAYLISTS}
update_detected_at: dict[str, datetime | None] = {name: None for name in PLAYLISTS}

# Scrape method state — last seen "Updated X" text per playlist
last_update_text: dict[str, str | None] = {name: None for name in PLAYLISTS}


def reset_memory():
    global last_count, update_detected_at, last_update_text
    last_count = {name: None for name in PLAYLISTS}
    update_detected_at = {name: None for name in PLAYLISTS}
    last_update_text = {name: None for name in PLAYLISTS}
    logger.info("Memory cleared.")


# ─── API METHOD ───────────────────────────────────────────────────────────────

async def fetch_playlist_info(playlist_id: str) -> dict | None:
    url = "https://www.googleapis.com/youtube/v3/playlists"
    params = {
        "part": "snippet,contentDetails",
        "id": playlist_id,
        "key": YT_API_KEY,
    }
    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.get(url, params=params)
        resp.raise_for_status()
        data = resp.json()
    items = data.get("items", [])
    if not items:
        return None
    return items[0]


async def check_playlists_api(app) -> None:
    global last_count, update_detected_at

    for name, playlist_id in PLAYLISTS.items():
        try:
            info = await fetch_playlist_info(playlist_id)
        except Exception as e:
            logger.error(f"[API] Error fetching {name}: {e}")
            continue

        if info is None:
            continue

        item_count = info["contentDetails"]["itemCount"]
        prev_count = last_count[name]

        if prev_count is None:
            last_count[name] = item_count
            logger.info(f"[API] {name}: baseline count = {item_count}")
            continue

        if item_count > prev_count:
            added = item_count - prev_count
            last_count[name] = item_count
            update_detected_at[name] = datetime.now(timezone.utc)

            playlist_url = f"https://www.youtube.com/playlist?list={playlist_id}"
            msg = (
                f"🔔 *{name}* playlist was just updated! _(via API)_\n"
                f"📦 {added} new video{'s' if added != 1 else ''} added "
                f"(possibly private/unlisted — new release incoming? 👀)\n"
                f"Total videos: {item_count}\n"
                f"[Open Playlist]({playlist_url})"
            )
            await app.bot.send_message(chat_id=CHAT_ID, text=msg, parse_mode="Markdown")
            logger.info(f"[API] Alert sent for {name}: {prev_count} → {item_count}")

        elif item_count < prev_count:
            removed = prev_count - item_count
            last_count[name] = item_count

            playlist_url = f"https://www.youtube.com/playlist?list={playlist_id}"
            msg = (
                f"🗑️ *{name}* playlist: {removed} video{'s' if removed != 1 else ''} removed!\n"
                f"Total videos: {item_count}\n"
                f"[Open Playlist]({playlist_url})"
            )
            await app.bot.send_message(chat_id=CHAT_ID, text=msg, parse_mode="Markdown")
            logger.info(f"[API] Deletion alert sent for {name}")


# ─── SCRAPE METHOD ────────────────────────────────────────────────────────────

async def scrape_playlist_update_text(playlist_id: str) -> str | None:
    url = f"https://www.youtube.com/playlist?list={playlist_id}"
    try:
        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=True)
            page = await browser.new_page()
            await page.goto(url, wait_until="networkidle", timeout=30000)
            content = await page.content()
            await browser.close()

        match = re.search(r'Updated (today|yesterday|\d+ \w+ ago)', content, re.IGNORECASE)
        if match:
            return match.group()
        return None
    except Exception as e:
        logger.error(f"[SCRAPE] Error scraping {playlist_id}: {e}")
        return None


async def check_playlists_scrape(app) -> None:
    global last_update_text, update_detected_at

    for name, playlist_id in PLAYLISTS.items():
        text = await scrape_playlist_update_text(playlist_id)
        logger.info(f"[SCRAPE] {name}: '{text}'")

        if text is None:
            continue

        prev_text = last_update_text[name]

        # Alert if:
        # 1. First time seeing "Updated today"
        # 2. Text changed (e.g. went from "3 days ago" to "today")
        if text.lower() == "updated today" and prev_text != text:
            last_update_text[name] = text
            update_detected_at[name] = datetime.now(timezone.utc)

            playlist_url = f"https://www.youtube.com/playlist?list={playlist_id}"
            msg = (
                f"🔔 *{name}* playlist shows *Updated Today*! _(via Scrape)_\n"
                f"New release incoming? 👀\n"
                f"[Open Playlist]({playlist_url})"
            )
            await app.bot.send_message(chat_id=CHAT_ID, text=msg, parse_mode="Markdown")
            logger.info(f"[SCRAPE] Alert sent for {name}")
        else:
            last_update_text[name] = text


# ─── STATUS ───────────────────────────────────────────────────────────────────

def human_time_ago(dt: datetime | None) -> str:
    if dt is None:
        return "No update detected"
    now = datetime.now(timezone.utc)
    diff = now - dt
    seconds = int(diff.total_seconds())
    if seconds < 60:
        return "Just Now"
    elif seconds < 3600:
        mins = seconds // 60
        return f"{mins} min{'s' if mins != 1 else ''} ago"
    elif seconds < 86400:
        hours = seconds // 3600
        return f"{hours} hour{'s' if hours != 1 else ''} ago"
    else:
        days = seconds // 86400
        return f"{days} day{'s' if days != 1 else ''} ago"


async def get_status_message() -> str:
    lines = ["📋 *Playlist Status*\n"]
    for name, playlist_id in PLAYLISTS.items():
        try:
            info = await fetch_playlist_info(playlist_id)
        except Exception:
            lines.append(f"• *{name}*: ❌ Error fetching")
            continue

        if info is None:
            lines.append(f"• *{name}*: ❌ Not found")
            continue

        item_count = info["contentDetails"]["itemCount"]
        detected = update_detected_at.get(name)
        scrape_text = last_update_text.get(name)

        if detected:
            time_str = human_time_ago(detected)
            emoji = "🟢" if time_str == "Just Now" else "🟡"
            lines.append(f"• *{name}*: {emoji} Updated {time_str} ({item_count} videos)")
        elif scrape_text:
            lines.append(f"• *{name}*: 🟡 {scrape_text} ({item_count} videos)")
        else:
            lines.append(f"• *{name}*: ⚪ No Update ({item_count} videos)")

    return "\n".join(lines)


# ─── HANDLERS ─────────────────────────────────────────────────────────────────

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (update.message.text or "").strip().lower()
    if text == "update":
        await update.message.reply_text("🔄 Fetching live status...")
        msg = await get_status_message()
        await update.message.reply_text(msg, parse_mode="Markdown")
    elif text == "reset":
        reset_memory()
        await update.message.reply_text(
            "🔄 Memory cleared! Baselines will re-record on next check (within 5 mins)."
        )


async def handle_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "👋 *YG Playlist Monitor* is active!\n\n"
        "Monitoring via *API* (itemCount) + *Scraping* (Updated today text) — 7 playlists\n\n"
        "Commands:\n"
        "• *update* — check current status\n"
        "• *reset* — clear memory & restart tracking",
        parse_mode="Markdown",
    )


# ─── LOOPS ────────────────────────────────────────────────────────────────────

async def periodic_api_check(app):
    while True:
        await check_playlists_api(app)
        await asyncio.sleep(300)  # every 5 minutes


async def periodic_scrape_check(app):
    while True:
        await check_playlists_scrape(app)
        await asyncio.sleep(300)  # every 5 minutes


async def post_init(app):
    await app.bot.send_message(
        chat_id=CHAT_ID,
        text="🟢 *Bot started!* Monitoring 7 playlists via API + Scraping.\nSend *update* to check status.",
        parse_mode="Markdown",
    )
    asyncio.create_task(periodic_api_check(app))
    asyncio.create_task(periodic_scrape_check(app))


def main():
    app = (
        ApplicationBuilder()
        .token(BOT_TOKEN)
        .post_init(post_init)
        .build()
    )

    app.add_handler(CommandHandler("start", handle_start))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    logger.info("Bot started.")
    app.run_polling()


if __name__ == "__main__":
    main()

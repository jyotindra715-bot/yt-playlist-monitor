import os
import asyncio
import logging
from datetime import datetime, timezone

import httpx
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
    "NEXT MONSTER": "PLuWI86ItS2gFnopUHOZyCtFYxpwsjNRNm",
}

last_count: dict[str, int | None] = {name: None for name in PLAYLISTS}
update_detected_at: dict[str, datetime | None] = {name: None for name in PLAYLISTS}


def reset_memory():
    global last_count, update_detected_at
    last_count = {name: None for name in PLAYLISTS}
    update_detected_at = {name: None for name in PLAYLISTS}
    logger.info("Memory cleared.")


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


async def check_playlists(app) -> None:
    global last_count, update_detected_at

    for name, playlist_id in PLAYLISTS.items():
        try:
            info = await fetch_playlist_info(playlist_id)
        except Exception as e:
            logger.error(f"Error fetching {name}: {e}")
            continue

        if info is None:
            continue

        item_count = info["contentDetails"]["itemCount"]
        prev_count = last_count[name]

        if prev_count is None:
            last_count[name] = item_count
            logger.info(f"{name}: baseline count = {item_count}")
            continue

        if item_count > prev_count:
            added = item_count - prev_count
            last_count[name] = item_count
            update_detected_at[name] = datetime.now(timezone.utc)

            playlist_url = f"https://www.youtube.com/playlist?list={playlist_id}"
            msg = (
                f"🔔 *{name}* playlist was just updated!\n"
                f"📦 {added} new video{'s' if added != 1 else ''} added "
                f"(possibly private/unlisted — new release incoming? 👀)\n"
                f"Total videos: {item_count}\n"
                f"[Open Playlist]({playlist_url})"
            )
            await app.bot.send_message(chat_id=CHAT_ID, text=msg, parse_mode="Markdown")
            logger.info(f"Alert sent for {name}: count {prev_count} → {item_count}")

        elif item_count < prev_count:
            removed = prev_count - item_count
            last_count[name] = item_count
            logger.info(f"{name}: {removed} video(s) removed, count now {item_count}")

            playlist_url = f"https://www.youtube.com/playlist?list={playlist_id}"
            msg = (
                f"🗑️ *{name}* playlist: {removed} video{'s' if removed != 1 else ''} removed!\n"
                f"Total videos: {item_count}\n"
                f"[Open Playlist]({playlist_url})"
            )
            await app.bot.send_message(chat_id=CHAT_ID, text=msg, parse_mode="Markdown")
            logger.info(f"Deletion alert sent for {name}")


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

        if detected:
            time_str = human_time_ago(detected)
            emoji = "🟢" if time_str == "Just Now" else "🟡"
            lines.append(f"• *{name}*: {emoji} Updated {time_str} ({item_count} videos)")
        else:
            lines.append(f"• *{name}*: ⚪ No Update ({item_count} videos)")

    return "\n".join(lines)


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
        "I watch for changes in playlist video counts — "
        "this catches *private & unlisted videos* too 👀\n\n"
        "Commands:\n"
        "• *update* — check current status\n"
        "• *reset* — clear memory & restart tracking",
        parse_mode="Markdown",
    )


async def periodic_check(app):
    while True:
        await check_playlists(app)
        await asyncio.sleep(300)  # every 5 minutes


async def post_init(app):
    await app.bot.send_message(
        chat_id=CHAT_ID,
        text="🟢 *Bot started!* Monitoring 6 playlists.\nSend *update* to check status.",
        parse_mode="Markdown",
    )
    asyncio.create_task(periodic_check(app))


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

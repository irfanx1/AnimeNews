import aiohttp
import asyncio
import os
from pyrogram import Client, filters
from pyrogram.types import InlineKeyboardButton, InlineKeyboardMarkup
import threading
import pymongo
import feedparser
from config import *
from webhook import start_webhook

from module.rss.rss import (
    news_feed_loop, format_rss_entry,
    find_youtube_url, download_video
)


mongo_client = pymongo.MongoClient(MONGO_URI)
db = mongo_client["AnimeNewsBot"]
user_settings_collection = db["user_settings"]
global_settings_collection = db["global_settings"]

app = Client("AnimeNewsBot", api_id=API_ID, api_hash=API_HASH, bot_token=BOT_TOKEN)

webhook_thread = threading.Thread(target=start_webhook, daemon=True)
webhook_thread.start()


@app.on_message(filters.command("start"))
async def start(client, message):
    chat_id = message.chat.id
    buttons = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("ᴍᴀɪɴ ʜᴜʙ", url="https://t.me/+pGtd3B1igQcxNTZl"),
            InlineKeyboardButton("ɴᴇᴛᴡᴏʀᴋ", url="https://t.me/Zenkai_Network"),
        ],
        [
            InlineKeyboardButton("ᴅᴇᴠᴇʟᴏᴩᴇʀ", url="https://t.me/SubaruXnatsuki"),
        ],
    ])
    caption = (
        f"<b><blockquote>ʙᴀᴋᴋᴀᴀᴀ {message.from_user.username}!!!\n\n"
        f"ɪ ᴀᴍ ᴀɴ ᴀɴɪᴍᴇ ɴᴇᴡs ʙᴏᴛ.\n"
        f"ɪ ᴛᴀᴋᴇ ᴀɴɪᴍᴇ ɴᴇᴡs ᴄᴏᴍɪɴɢ ғʀᴏᴍ ʀss ꜰᴇᴇᴅs ᴀɴᴅ ᴀᴜᴛᴏᴍᴀᴛɪᴄᴀʟʟʏ ᴜᴘʟᴏᴀᴅ ɪᴛ ᴛᴏ ᴍʏ ᴍᴀsᴛᴇʀ's ᴀɴɪᴍᴇ ɴᴇᴡs ᴄʜᴀɴɴᴇʟ.</b></blockquote>"
    )
    if START_PIC:
        await app.send_photo(chat_id, START_PIC, caption=caption, reply_markup=buttons)
    else:
        await app.send_message(chat_id, caption, reply_markup=buttons)


@app.on_message(filters.command("news"))
async def connect_news(client, message):
    chat_id = message.chat.id
    if message.from_user.id not in ADMINS:
        await app.send_message(chat_id, "<b><blockquote>You do not have permission to use this command.</blockquote></b>")
        return
    if len(message.text.split()) == 1:
        await app.send_message(chat_id, "<b><blockquote>Please provide a channel ID or username.</blockquote></b>")
        return

    channel_input = " ".join(message.text.split()[1:]).strip()
    if channel_input.startswith("-100"):
        channel = int(channel_input)
        display = str(channel)
    else:
        channel = channel_input if channel_input.startswith("@") else f"@{channel_input}"
        display = channel

    global_settings_collection.update_one(
        {"_id": "config"},
        {"$set": {"news_channel": channel}},
        upsert=True
    )
    await app.send_message(chat_id, f"<b><blockquote>News channel set to: {display}</blockquote></b>")


@app.on_message(filters.command("sendnews"))
async def sendnews_cmd(client, message):
    chat_id = message.chat.id

    if message.from_user.id not in ADMINS:
        await app.send_message(chat_id, "<b><blockquote>You do not have permission to use this command.</blockquote></b>")
        return

    args = message.text.split(maxsplit=2)
    if len(args) < 3:
        await app.send_message(chat_id, "<b><blockquote>Usage: /sendnews {rss link} {task position}</blockquote></b>")
        return

    rss_link = args[1]
    try:
        position = int(args[2]) - 1
        if position < 0:
            raise ValueError
    except ValueError:
        await app.send_message(chat_id, "<b><blockquote>Task position must be a positive integer.</blockquote></b>")
        return

    config = global_settings_collection.find_one({"_id": "config"})
    if not config or "news_channel" not in config:
        await app.send_message(chat_id, "<b><blockquote>No news channel configured. Use /news to set one.</blockquote></b>")
        return

    news_channel = config["news_channel"]
    try:
        news_channel = int(news_channel)
    except Exception:
        pass

    feed = feedparser.parse(rss_link)
    if not feed.entries or position >= len(feed.entries):
        await app.send_message(chat_id, "<b><blockquote>No news found at that position.</blockquote></b>")
        return

    entry = feed.entries[position]
    entry_id = entry.get('id', entry.get('link', str(position)))
    msg, thumbnail_url, link = await format_rss_entry(entry)
    video_path = None

    try:
        # Try to find a YouTube video in the article
        yt_url = await find_youtube_url(link)

        if yt_url:
            safe_id = "".join(c for c in str(entry_id) if c.isalnum())
            video_path = await download_video(yt_url, safe_id)

        if video_path:
            # Single post: video + caption
            await app.send_video(chat_id=news_channel, video=video_path, caption=msg)
            await app.send_message(chat_id, "<b><blockquote>✅ News sent with video!</blockquote></b>")
        elif thumbnail_url:
            # Single post: photo + caption
            await app.send_photo(chat_id=news_channel, photo=thumbnail_url, caption=msg)
            await app.send_message(chat_id, "<b><blockquote>✅ News sent with thumbnail (no video found).</blockquote></b>")
        else:
            # Text only
            await app.send_message(chat_id=news_channel, text=msg, disable_web_page_preview=True)
            await app.send_message(chat_id, "<b><blockquote>✅ News sent (no thumbnail or video found).</blockquote></b>")

    except Exception as e:
        await app.send_message(chat_id, f"<b><blockquote>❌ Error sending news: {e}</blockquote></b>")
    finally:
        if video_path and os.path.exists(video_path):
            try:
                os.remove(video_path)
            except Exception as e:
                print(f"[Video] Error deleting temp file: {e}")


async def main():
    await app.start()
    print("Bot is running...")
    asyncio.create_task(news_feed_loop(app, db, global_settings_collection, [URL_A]))
    await asyncio.Event().wait()


if __name__ == '__main__':
    loop = asyncio.get_event_loop()
    loop.run_until_complete(main())

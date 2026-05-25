from urllib.parse import urlparse, parse_qs
import asyncio
import os
import feedparser
from pyrogram import Client
import aiohttp
from bs4 import BeautifulSoup
import yt_dlp


def extract_youtube_watch_url(yt_url):
    if "youtube.com/embed/" in yt_url:
        video_id = yt_url.split("youtube.com/embed/")[-1].split("?")[0].split("/")[0]
        return f"https://www.youtube.com/watch?v={video_id}"
    elif "youtube.com/watch" in yt_url:
        parsed = urlparse(yt_url)
        v = parse_qs(parsed.query).get("v", [""])[0]
        if v:
            return f"https://www.youtube.com/watch?v={v}"
    return yt_url


def get_actual_video_path(ydl, info):
    raw_path = ydl.prepare_filename(info)
    base = os.path.splitext(raw_path)[0]
    for ext in ["mp4", "mkv", "webm", "mov", "avi"]:
        candidate = f"{base}.{ext}"
        if os.path.exists(candidate):
            return candidate
    return raw_path


async def find_youtube_url(link):
    """Scrape the article page and return a YouTube watch URL if an embed is found, else None."""
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(link, timeout=aiohttp.ClientTimeout(total=30)) as resp:
                html = await resp.text()
    except Exception as e:
        print(f"[Video] Error fetching article page {link}: {e}")
        return None

    soup = BeautifulSoup(html, "html.parser")

    main_selectors = [".news-body", ".entry-content", "article", "main", "#content", ".content"]
    yt_iframe = None
    for sel in main_selectors:
        main_block = soup.select_one(sel)
        if main_block:
            yt_iframe = main_block.find(
                "iframe",
                src=lambda s: s and ("youtube.com" in s or "youtube-nocookie.com" in s)
            )
            if yt_iframe:
                break
    if not yt_iframe:
        yt_iframe = soup.find(
            "iframe",
            src=lambda s: s and ("youtube.com" in s or "youtube-nocookie.com" in s)
        )

    if not yt_iframe:
        return None

    yt_url = yt_iframe["src"]
    if yt_url.startswith("//"):
        yt_url = "https:" + yt_url
    elif yt_url.startswith("/"):
        yt_url = "https://www.youtube.com" + yt_url

    return extract_youtube_watch_url(yt_url)


async def download_video(yt_url, safe_id):
    """Download a YouTube video and return the local file path, or None on failure."""
    ydl_opts = {
        'format': 'bestvideo[height<=720][ext=mp4]+bestaudio[ext=m4a]/best[height<=720][ext=mp4]/best[height<=720]/best',
        'outtmpl': f'/tmp/ytvideo_{safe_id}.%(ext)s',
        'quiet': True,
        'merge_output_format': 'mp4',
        **({"cookiefile": "./cookies.txt"} if os.path.exists("./cookies.txt") else {}),
    }
    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(yt_url, download=True)
            video_path = get_actual_video_path(ydl, info)

        if not os.path.exists(video_path):
            print(f"[Video] File not found after download: {video_path}")
            return None

        file_size_mb = os.path.getsize(video_path) / (1024 * 1024)
        if file_size_mb > 49:
            print(f"[Video] File too large ({file_size_mb:.1f} MB), skipping.")
            os.remove(video_path)
            return None

        return video_path

    except Exception as e:
        print(f"[Video] yt-dlp error for {yt_url}: {e}")
        return None


async def fetch_and_send_news(app: Client, db, global_settings_collection, urls):
    config = global_settings_collection.find_one({"_id": "config"})
    if not config or "news_channel" not in config:
        return

    news_channel = config["news_channel"]
    try:
        news_channel = int(news_channel)
    except Exception:
        pass

    for url in urls:
        feed = await asyncio.to_thread(feedparser.parse, url)
        if not feed.entries:
            continue

        entry = feed.entries[0]
        entry_id = entry.get('id', entry.get('link'))

        if not db.sent_news.find_one({"entry_id": entry_id}):
            msg, thumbnail_url, link = await format_rss_entry(entry)
            video_path = None

            try:
                # Try to find a YouTube video in the article first
                yt_url = await find_youtube_url(link)

                if yt_url:
                    safe_id = "".join(c for c in str(entry_id) if c.isalnum())
                    video_path = await download_video(yt_url, safe_id)

                if video_path:
                    # Send as single video post with caption
                    await app.send_video(chat_id=news_channel, video=video_path, caption=msg)
                    print(f"[News] Sent with video: {entry.title if 'title' in entry else ''}")
                elif thumbnail_url:
                    # No video — send as photo post
                    await app.send_photo(chat_id=news_channel, photo=thumbnail_url, caption=msg)
                    print(f"[News] Sent with photo: {entry.title if 'title' in entry else ''}")
                else:
                    # No video, no thumbnail — send text only
                    await app.send_message(chat_id=news_channel, text=msg, disable_web_page_preview=True)
                    print(f"[News] Sent text only: {entry.title if 'title' in entry else ''}")

                db.sent_news.insert_one({
                    "entry_id": entry_id,
                    "title": entry.title if 'title' in entry else '',
                    "link": link
                })

            except Exception as e:
                print(f"Error sending news: {e}")
            finally:
                if video_path and os.path.exists(video_path):
                    try:
                        os.remove(video_path)
                    except Exception as e:
                        print(f"[Video] Error deleting temp file: {e}")


async def news_feed_loop(app: Client, db, global_settings_collection, urls):
    while True:
        await fetch_and_send_news(app, db, global_settings_collection, urls)
        await asyncio.sleep(10)


async def get_ann_image(guid_url):
    def is_valid_img(src):
        return src and src.startswith("http") and "spacer.gif" not in src

    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(guid_url, timeout=aiohttp.ClientTimeout(total=15)) as resp:
                html = await resp.text()
        soup = BeautifulSoup(html, "html.parser")
        figure = soup.find("figure")
        if figure:
            img_tag = figure.find("img")
            if img_tag:
                src = img_tag.get("data-src") or img_tag.get("src")
                if is_valid_img(src):
                    return src
        img = soup.find("meta", property="og:image")
        if img and is_valid_img(img.get("content")):
            return img["content"]
        img_tag = soup.find("img")
        if img_tag:
            src = img_tag.get("data-src") or img_tag.get("src")
            if is_valid_img(src):
                return src
    except Exception as e:
        print(f"Error fetching ANN image: {e}")
    return None


async def format_rss_entry(entry):
    title = entry.title if 'title' in entry else 'No Title'
    summary = entry.summary if 'summary' in entry else ''
    link = entry.link if 'link' in entry else ''
    thumbnail_url = entry.media_thumbnail[0]['url'] if 'media_thumbnail' in entry else None

    if "animenewsnetwork.com" in link and not thumbnail_url:
        guid_url = entry.get('guid', link)
        thumbnail_url = await get_ann_image(guid_url)

    msg = (
        f"<b><blockquote>{title}</blockquote></b>\n"
        f"<blockquote expandable><i>{summary}</i></blockquote>\n"
        f"<b><blockquote><a href='{link}'>Read Full News</a></blockquote></b>"
    )
    return msg, thumbnail_url, link

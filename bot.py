"""
Telegram Instagram Downloader Bot (v2 - Fixed for Python 3.14 + python-telegram-bot 21+)
==========================================================================================
Compatible with: Python 3.14, python-telegram-bot >= 21.0
Run: python bot.py or double-click setup.bat
"""

import os
import re
import json
import time
import asyncio
import logging
import shutil
import tempfile
import subprocess
from datetime import datetime
from pathlib import Path
from typing import Optional

from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    BotCommand,
)
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ContextTypes,
    filters,
)
from telegram.constants import ParseMode

import instaloader
import yt_dlp
import requests

# ============================================================
# CONFIGURATION — Edit these two lines
# ============================================================
TOKEN = "8832328333:AAEPt_nrE-s6kzPf2oc882wsB_whXn9gPbc"   # Get from @BotFather
ADMIN_ID =   5779614630                   # Your Telegram user ID

# ============================================================
# CONSTANTS
# ============================================================
MAX_DAILY = 20
MAX_SIZE_MB = 50
COOLDOWN_SEC = 2

BASE_DIR = Path(__file__).parent
DATA_DIR = BASE_DIR / "data"
USERS_FILE = DATA_DIR / "users.json"
DL_DIR = DATA_DIR / "downloads"

# ============================================================
# LOGGING
# ============================================================
logging.basicConfig(
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger("igbot")

# ============================================================
# GLOBAL STATE
# ============================================================
user_last_req: dict[int, float] = {}

# Instaloader instance (quiet mode)
IL = instaloader.Instaloader(
    download_videos=True,
    download_video_thumbnails=False,
    download_geotags=False,
    download_comments=False,
    save_metadata=False,
    compress_json=False,
    quiet=True,
)


# ============================================================
# USER TRACKING (users.json)
# ============================================================
def _ensure_dirs():
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    DL_DIR.mkdir(parents=True, exist_ok=True)


def _load_users() -> dict:
    _ensure_dirs()
    if USERS_FILE.exists():
        try:
            return json.loads(USERS_FILE.read_text("utf-8"))
        except Exception:
            return {}
    return {}


def _save_users(data: dict):
    _ensure_dirs()
    USERS_FILE.write_text(json.dumps(data, ensure_ascii=False, indent=2), "utf-8")


def _user_record(uid: int) -> dict:
    users = _load_users()
    key = str(uid)
    today = datetime.now().strftime("%Y-%m-%d")
    rec = users.get(key)
    if not rec or rec.get("date") != today:
        rec = {"downloads_today": 0, "date": today, "total": 0}
        users[key] = rec
        _save_users(users)
    return rec


def _inc_download(uid: int) -> dict:
    users = _load_users()
    key = str(uid)
    today = datetime.now().strftime("%Y-%m-%d")
    rec = users.get(key, {})
    if rec.get("date") != today:
        rec = {"downloads_today": 0, "date": today, "total": 0}
    rec["downloads_today"] = rec.get("downloads_today", 0) + 1
    rec["total"] = rec.get("total", 0) + 1
    users[key] = rec
    _save_users(users)
    return rec


def _check_cooldown(uid: int) -> Optional[float]:
    now = time.time()
    last = user_last_req.get(uid, 0)
    diff = now - last
    if diff < COOLDOWN_SEC:
        return COOLDOWN_SEC - diff
    return None


# ============================================================
# URL / CONTENT HELPERS
# ============================================================
def is_ig_url(url: str) -> bool:
    return bool(re.match(r"https?://(?:www\.)?(?:instagram\.com|instagr\.am)/", url.strip()))


def extract_shortcode(url: str) -> Optional[str]:
    m = re.search(r"instagram\.com/(?:p|reel|tv|reels)/([A-Za-z0-9_-]+)", url)
    return m.group(1) if m else None


def detect_type(url: str) -> str:
    if "/reel" in url:
        return "reel"
    if "/tv/" in url:
        return "igtv"
    if "/stories/" in url:
        return "story"
    if "/highlights/" in url:
        return "highlight"
    if "/p/" in url:
        return "post"
    return "unknown"


def fmt_size(b: int) -> str:
    if b < 1024:
        return f"{b} B"
    if b < 1048576:
        return f"{b/1024:.1f} KB"
    return f"{b/1048576:.1f} MB"


def _cleanup_dir(d: Path, max_age: int = 300):
    if not d.exists():
        return
    now = time.time()
    for f in d.iterdir():
        if f.is_file() and (now - f.stat().st_mtime) > max_age:
            try:
                f.unlink()
            except OSError:
                pass


def _compress_video(src: str) -> str:
    """Compress video with ffmpeg. Returns original if ffmpeg missing or fails."""
    dst = src.rsplit(".", 1)[0] + "_cmp.mp4"
    try:
        r = subprocess.run(
            ["ffmpeg", "-y", "-i", src,
             "-vcodec", "libx264", "-crf", "28", "-preset", "fast",
             "-acodec", "aac", "-b:a", "128k", dst],
            capture_output=True, timeout=120,
        )
        if r.returncode == 0 and os.path.exists(dst):
            if os.path.getsize(dst) < os.path.getsize(src):
                return dst
    except Exception:
        pass
    return src


# ============================================================
# DOWNLOAD FUNCTIONS
# ============================================================
async def dl_post(url: str, hq: bool = False) -> list[dict]:
    """Download Instagram post (photo / video / album)."""
    results: list[dict] = []
    sc = extract_shortcode(url)
    if not sc:
        return results

    tmp = DL_DIR / f"p_{int(time.time())}_{sc}"
    tmp.mkdir(parents=True, exist_ok=True)

    try:
        post = instaloader.Post.from_shortcode(IL.context, sc)

        if post.is_video:
            vurl = post.video_url
            if vurl:
                out = tmp / f"{sc}.mp4"
                opts = {
                    "outtmpl": str(out),
                    "quiet": True,
                    "no_warnings": True,
                    "format": "best" if hq else "best[height<=720]",
                }
                with yt_dlp.YoutubeDL(opts) as ydl:
                    ydl.download([vurl])
                for f in tmp.iterdir():
                    if f.suffix in (".mp4", ".webm", ".mkv") and f.is_file():
                        results.append({"path": str(f), "type": "video", "size": f.stat().st_size})
                        break

        elif post.typename == "GraphSidecar":
            for i, node in enumerate(post.get_sidecar_nodes()):
                if node.is_video and node.video_url:
                    out = tmp / f"{sc}_{i}.mp4"
                    opts = {"outtmpl": str(out), "quiet": True, "no_warnings": True, "format": "best"}
                    with yt_dlp.YoutubeDL(opts) as ydl:
                        ydl.download([node.video_url])
                    for f in tmp.iterdir():
                        if f.name.startswith(f"{sc}_{i}") and f.is_file():
                            results.append({"path": str(f), "type": "video", "size": f.stat().st_size})
                            break
                else:
                    img = node.display_url
                    if img:
                        out = tmp / f"{sc}_{i}.jpg"
                        resp = requests.get(img, timeout=30)
                        if resp.ok:
                            out.write_bytes(resp.content)
                            results.append({"path": str(out), "type": "photo", "size": len(resp.content)})
        else:
            img = post.url
            if img:
                out = tmp / f"{sc}.jpg"
                resp = requests.get(img, timeout=30)
                if resp.ok:
                    out.write_bytes(resp.content)
                    results.append({"path": str(out), "type": "photo", "size": len(resp.content)})

    except Exception as e:
        logger.error(f"dl_post error: {e}")

    return results


async def dl_reel(url: str, hq: bool = False) -> list[dict]:
    """Download Instagram Reel."""
    results: list[dict] = []
    sc = extract_shortcode(url)
    if not sc:
        return results

    tmp = DL_DIR / f"r_{int(time.time())}_{sc}"
    tmp.mkdir(parents=True, exist_ok=True)

    try:
        post = instaloader.Post.from_shortcode(IL.context, sc)
        if post.is_video and post.video_url:
            out = tmp / f"{sc}_reel.mp4"
            opts = {
                "outtmpl": str(out),
                "quiet": True,
                "no_warnings": True,
                "format": "best" if hq else "best[height<=720]",
            }
            with yt_dlp.YoutubeDL(opts) as ydl:
                ydl.download([post.video_url])
            for f in tmp.iterdir():
                if f.suffix in (".mp4", ".webm", ".mkv") and f.is_file():
                    results.append({"path": str(f), "type": "video", "size": f.stat().st_size})
                    break
    except Exception as e:
        logger.error(f"dl_reel error: {e}")

    return results


async def dl_igtv(url: str) -> list[dict]:
    """Download IGTV."""
    results: list[dict] = []
    sc = extract_shortcode(url)
    if not sc:
        return results

    tmp = DL_DIR / f"tv_{int(time.time())}_{sc}"
    tmp.mkdir(parents=True, exist_ok=True)

    try:
        post = instaloader.Post.from_shortcode(IL.context, sc)
        if post.is_video and post.video_url:
            out = tmp / f"{sc}_igtv.mp4"
            opts = {"outtmpl": str(out), "quiet": True, "no_warnings": True, "format": "best"}
            with yt_dlp.YoutubeDL(opts) as ydl:
                ydl.download([post.video_url])
            for f in tmp.iterdir():
                if f.suffix in (".mp4", ".webm", ".mkv") and f.is_file():
                    results.append({"path": str(f), "type": "video", "size": f.stat().st_size})
                    break
    except Exception as e:
        logger.error(f"dl_igtv error: {e}")

    return results


async def dl_stories(username: str) -> list[dict]:
    """Download active stories from a public profile."""
    results: list[dict] = []
    tmp = DL_DIR / f"st_{username}_{int(time.time())}"
    tmp.mkdir(parents=True, exist_ok=True)

    try:
        profile = instaloader.Profile.from_username(IL.context, username)
        if profile.is_private:
            return results

        for story_item in IL.get_stories(userids=[profile.userid]):
            for story in story_item.get_items():
                ts = story.date_utc.timestamp()
                if story.is_video and story.video_url:
                    out = tmp / f"story_{ts:.0f}.mp4"
                    opts = {"outtmpl": str(out), "quiet": True, "no_warnings": True, "format": "best"}
                    with yt_dlp.YoutubeDL(opts) as ydl:
                        ydl.download([story.video_url])
                    for f in tmp.iterdir():
                        if f.suffix == ".mp4" and f.is_file():
                            results.append({"path": str(f), "type": "video", "size": f.stat().st_size})
                            break
                else:
                    img = story.url
                    if img:
                        out = tmp / f"story_{ts:.0f}.jpg"
                        resp = requests.get(img, timeout=30)
                        if resp.ok:
                            out.write_bytes(resp.content)
                            results.append({"path": str(out), "type": "photo", "size": len(resp.content)})
    except Exception as e:
        logger.error(f"dl_stories error: {e}")

    return results


async def get_highlights_list(username: str) -> list[dict]:
    """Get list of highlight albums."""
    out: list[dict] = []
    try:
        profile = instaloader.Profile.from_username(IL.context, username)
        if profile.is_private:
            return out
        for hl in IL.get_highlights(profile):
            out.append({"id": hl.unique_id, "title": hl.title})
    except Exception as e:
        logger.error(f"get_highlights_list error: {e}")
    return out


async def dl_highlight(username: str, hl_id: str) -> list[dict]:
    """Download a specific highlight album."""
    results: list[dict] = []
    tmp = DL_DIR / f"hl_{hl_id}_{int(time.time())}"
    tmp.mkdir(parents=True, exist_ok=True)

    try:
        profile = instaloader.Profile.from_username(IL.context, username)
        for hl in IL.get_highlights(profile):
            if hl.unique_id != hl_id:
                continue
            for item in hl.get_items():
                ts = item.date_utc.timestamp()
                if item.is_video and item.video_url:
                    out = tmp / f"hl_{ts:.0f}.mp4"
                    opts = {"outtmpl": str(out), "quiet": True, "no_warnings": True, "format": "best"}
                    with yt_dlp.YoutubeDL(opts) as ydl:
                        ydl.download([item.video_url])
                    for f in tmp.iterdir():
                        if f.suffix == ".mp4" and f.is_file():
                            results.append({"path": str(f), "type": "video", "size": f.stat().st_size})
                            break
                else:
                    img = item.url
                    if img:
                        out = tmp / f"hl_{ts:.0f}.jpg"
                        resp = requests.get(img, timeout=30)
                        if resp.ok:
                            out.write_bytes(resp.content)
                            results.append({"path": str(out), "type": "photo", "size": len(resp.content)})
            break
    except Exception as e:
        logger.error(f"dl_highlight error: {e}")

    return results


async def extract_audio(video_path: str) -> Optional[str]:
    """Extract audio from video. Returns mp3 path or None."""
    mp3 = video_path.rsplit(".", 1)[0] + ".mp3"
    try:
        cmd = [
            "ffmpeg", "-y", "-i", video_path,
            "-vn", "-acodec", "libmp3lame", "-q:a", "2", mp3,
        ]
        r = subprocess.run(cmd, capture_output=True, timeout=120)
        if r.returncode == 0 and os.path.exists(mp3):
            return mp3
    except Exception as e:
        logger.error(f"extract_audio error: {e}")
    return None


# ============================================================
# BOT HANDLERS
# ============================================================
async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    name = user.first_name or "کاربر"
    _user_record(user.id)

    txt = (
        f"سلام {name}! 👋\n"
        f"به ربات دانلود اینستاگرام خوش اومدی! 📸\n\n"
        f"🔹 <b>قابلیت‌ها:</b>\n\n"
        f"• ارسال لینک پست → دانلود عکس/ویدیو 📷\n"
        f"• ارسال لینک ریلز → دانلود ویدیو 🎥\n"
        f"• ارسال لینک IGTV → دانلود ویدیو 📺\n"
        f"• <code>/story username</code> → دانلود استوری 📖\n"
        f"• <code>/highlights username</code> → دانلود هایلایت ⭐\n\n"
        f"🔹 <b>محدودیت‌ها:</b>\n"
        f"• حداکثر ۲۰ دانلود در روز\n"
        f"• ۲ ثانیه فاصله بین درخواست‌ها\n\n"
        f"فقط لینک اینستاگرام رو بفرست! ⏳"
    )
    kb = InlineKeyboardMarkup([[
        InlineKeyboardButton("راهنما 📖", callback_data="help"),
        InlineKeyboardButton("سازنده 👤", callback_data="creator"),
    ]])
    await update.message.reply_text(txt, parse_mode=ParseMode.HTML, reply_markup=kb)


async def cmd_help(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    txt = (
        "📖 <b>راهنمای استفاده</b>\n\n"
        "🔹 <b>دانلود پست:</b>\n"
        "<code>https://www.instagram.com/p/ABC123/</code>\n\n"
        "🔹 <b>دانلود ریلز:</b>\n"
        "<code>https://www.instagram.com/reel/ABC123/</code>\n\n"
        "🔹 <b>دانلود IGTV:</b>\n"
        "<code>https://www.instagram.com/tv/ABC123/</code>\n\n"
        "🔹 <b>دانلود استوری:</b>\n"
        "<code>/story instagram</code>\n\n"
        "🔹 <b>دانلود هایلایت:</b>\n"
        "<code>/highlights instagram</code>\n\n"
        "⚠️ پیج باید عمومی باشد."
    )
    await update.message.reply_text(txt, parse_mode=ParseMode.HTML)


async def cb_handler(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    data = q.data

    if data == "help":
        await q.edit_message_text(
            "📖 <b>راهنما</b>\n\n"
            "لینک اینستاگرام رو بفرستید تا دانلود شود.\n"
            "برای استوری: <code>/story username</code>\n"
            "برای هایلایت: <code>/highlights username</code>",
            parse_mode=ParseMode.HTML,
        )

    elif data == "creator":
        await q.edit_message_text(
            "👤 <b>سازنده ربات</b>\n\nبرای ارتباط از /start استفاده کنید.",
            parse_mode=ParseMode.HTML,
        )

    elif data.startswith("story_dl_"):
        username = data.replace("story_dl_", "")
        await q.edit_message_text(
            f"⏳ <b>در حال دانلود استوری‌های @{username}...</b>",
            parse_mode=ParseMode.HTML,
        )
        await _do_story_dl(q, ctx, username)

    elif data.startswith("hl_dl_"):
        parts = data.split("_", 3)
        if len(parts) >= 4:
            username = parts[2]
            hl_id = parts[3]
            await q.edit_message_text("⏳ <b>در حال دانلود هایلایت...</b>", parse_mode=ParseMode.HTML)
            await _do_hl_dl(q, ctx, username, hl_id)

    elif data.startswith("audio_"):
        fpath = data.replace("audio_", "")
        if os.path.exists(fpath):
            await q.edit_message_text("🎵 <b>در حال استخراج صوت...</b>", parse_mode=ParseMode.HTML)
            mp3 = await extract_audio(fpath)
            if mp3:
                try:
                    with open(mp3, "rb") as af:
                        await ctx.bot.send_audio(chat_id=q.message.chat_id, audio=af, title="Instagram Audio")
                    await q.edit_message_text("✅ <b>صوت با موفقیت استخراج شد!</b>", parse_mode=ParseMode.HTML)
                except Exception:
                    await q.edit_message_text("❌ خطا در ارسال صوت.", parse_mode=ParseMode.HTML)
                finally:
                    try:
                        os.remove(mp3)
                    except OSError:
                        pass
            else:
                await q.edit_message_text(
                    "❌ خطا در استخراج صوت.\nممکن است ffmpeg نصب نباشد.",
                    parse_mode=ParseMode.HTML,
                )
        else:
            await q.edit_message_text("❌ فایل یافت نشد.", parse_mode=ParseMode.HTML)


async def _do_story_dl(q, ctx, username):
    stories = await dl_stories(username)
    if not stories:
        await q.edit_message_text(f"📭 <b>استوری فعالی برای @{username} یافت نشد.</b>", parse_mode=ParseMode.HTML)
        return
    n = 0
    for item in stories:
        try:
            if item["type"] == "video":
                fp = item["path"]
                if os.path.getsize(fp) > MAX_SIZE_MB * 1048576:
                    fp = _compress_video(fp)
                with open(fp, "rb") as vf:
                    await ctx.bot.send_video(q.message.chat_id, vf, caption=f"📸 استوری @{username}")
                n += 1
            else:
                with open(item["path"], "rb") as pf:
                    await ctx.bot.send_photo(q.message.chat_id, pf, caption=f"📸 استوری @{username}")
                n += 1
        except Exception as e:
            logger.error(f"send story: {e}")
    await ctx.bot.send_message(q.message.chat_id, f"✅ <b>{n} استوری دانلود شد.</b>", parse_mode=ParseMode.HTML)
    d = Path(stories[0]["path"]).parent
    shutil.rmtree(d, ignore_errors=True)


async def _do_hl_dl(q, ctx, username, hl_id):
    items = await dl_highlight(username, hl_id)
    if not items:
        await q.edit_message_text("📭 <b>محتوایی یافت نشد.</b>", parse_mode=ParseMode.HTML)
        return
    n = 0
    for item in items:
        try:
            if item["type"] == "video":
                fp = item["path"]
                if os.path.getsize(fp) > MAX_SIZE_MB * 1048576:
                    fp = _compress_video(fp)
                with open(fp, "rb") as vf:
                    await ctx.bot.send_video(q.message.chat_id, vf, caption=f"⭐ هایلایت @{username}")
                n += 1
            else:
                with open(item["path"], "rb") as pf:
                    await ctx.bot.send_photo(q.message.chat_id, pf, caption=f"⭐ هایلایت @{username}")
                n += 1
        except Exception as e:
            logger.error(f"send highlight: {e}")
    await ctx.bot.send_message(q.message.chat_id, f"✅ <b>{n} آیتم هایلایت دانلود شد.</b>", parse_mode=ParseMode.HTML)
    d = Path(items[0]["path"]).parent
    shutil.rmtree(d, ignore_errors=True)


async def cmd_story(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not ctx.args:
        await update.message.reply_text(
            "⚠️ <b>لطفاً username رو وارد کنید.</b>\n\nمثال: <code>/story instagram</code>",
            parse_mode=ParseMode.HTML,
        )
        return

    username = ctx.args[0].lstrip("@")
    uid = update.effective_user.id

    wait = _check_cooldown(uid)
    if wait:
        await update.message.reply_text(f"⏳ <b>لطفاً {wait:.0f} ثانیه صبر کنید.</b>", parse_mode=ParseMode.HTML)
        return

    rec = _user_record(uid)
    if rec.get("downloads_today", 0) >= MAX_DAILY:
        await update.message.reply_text("🚫 <b>محدودیت روزانه تمام شد!</b>\nفردا دوباره تلاش کنید.", parse_mode=ParseMode.HTML)
        return

    user_last_req[uid] = time.time()
    msg = await update.message.reply_text(f"⏳ <b>بررسی استوری‌های @{username}...</b>", parse_mode=ParseMode.HTML)

    try:
        profile = instaloader.Profile.from_username(IL.context, username)
        if profile.is_private:
            await msg.edit_text("🔒 <b>این پیج خصوصی است.</b>", parse_mode=ParseMode.HTML)
            return
    except instaloader.exceptions.ProfileNotExistsException:
        await msg.edit_text("❌ <b>پیج یافت نشد!</b>", parse_mode=ParseMode.HTML)
        return
    except Exception:
        await msg.edit_text("🌐 <b>مشکل در اتصال. دوباره تلاش کنید.</b>", parse_mode=ParseMode.HTML)
        return

    kb = InlineKeyboardMarkup([[InlineKeyboardButton("📥 دانلود استوری‌ها", callback_data=f"story_dl_{username}")]])
    await msg.edit_text(
        f"📸 <b>@{username}</b>\n\nاستوری فعال موجود است.\nروی دکمه زیر کلیک کنید:",
        parse_mode=ParseMode.HTML,
        reply_markup=kb,
    )


async def cmd_highlights(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not ctx.args:
        await update.message.reply_text(
            "⚠️ <b>لطفاً username رو وارد کنید.</b>\n\nمثال: <code>/highlights instagram</code>",
            parse_mode=ParseMode.HTML,
        )
        return

    username = ctx.args[0].lstrip("@")
    uid = update.effective_user.id

    wait = _check_cooldown(uid)
    if wait:
        await update.message.reply_text(f"⏳ <b>لطفاً {wait:.0f} ثانیه صبر کنید.</b>", parse_mode=ParseMode.HTML)
        return

    rec = _user_record(uid)
    if rec.get("downloads_today", 0) >= MAX_DAILY:
        await update.message.reply_text("🚫 <b>محدودیت روزانه تمام شد!</b>\nفردا دوباره تلاش کنید.", parse_mode=ParseMode.HTML)
        return

    user_last_req[uid] = time.time()
    msg = await update.message.reply_text(f"⏳ <b>دریافت هایلایت‌های @{username}...</b>", parse_mode=ParseMode.HTML)

    try:
        profile = instaloader.Profile.from_username(IL.context, username)
        if profile.is_private:
            await msg.edit_text("🔒 <b>این پیج خصوصی است.</b>", parse_mode=ParseMode.HTML)
            return
    except instaloader.exceptions.ProfileNotExistsException:
        await msg.edit_text("❌ <b>پیج یافت نشد!</b>", parse_mode=ParseMode.HTML)
        return
    except Exception:
        await msg.edit_text("🌐 <b>مشکل در اتصال. دوباره تلاش کنید.</b>", parse_mode=ParseMode.HTML)
        return

    hls = await get_highlights_list(username)
    if not hls:
        await msg.edit_text(f"📭 <b>هایلایتی برای @{username} یافت نشد.</b>", parse_mode=ParseMode.HTML)
        return

    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton(f"⭐ {h['title']}", callback_data=f"hl_dl_{username}_{h['id']}")]
        for h in hls[:10]
    ])
    await msg.edit_text(
        f"⭐ <b>هایلایت‌های @{username}:</b>\n\nیکی را انتخاب کنید:",
        parse_mode=ParseMode.HTML,
        reply_markup=kb,
    )


async def handle_text(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Handle plain text — detect Instagram links."""
    if not update.message or not update.message.text:
        return
    text = update.message.text.strip()
    if not is_ig_url(text):
        return

    uid = update.effective_user.id

    wait = _check_cooldown(uid)
    if wait:
        await update.message.reply_text(f"⏳ <b>لطفاً {wait:.0f} ثانیه صبر کنید.</b>", parse_mode=ParseMode.HTML)
        return

    rec = _user_record(uid)
    if rec.get("downloads_today", 0) >= MAX_DAILY:
        await update.message.reply_text("🚫 <b>محدودیت روزانه تمام شد!</b>\nفردا دوباره تلاش کنید.", parse_mode=ParseMode.HTML)
        return

    user_last_req[uid] = time.time()
    _inc_download(uid)

    ct = detect_type(text)
    status = await update.message.reply_text("⏳ <b>در حال دانلود... لطفاً صبر کنید</b>", parse_mode=ParseMode.HTML)

    try:
        if ct in ("story", "highlight"):
            await status.edit_text(
                "⚠️ <b>برای استوری و هایلایت، username رو بفرست.</b>\n\n"
                "مثال:\n<code>/story instagram</code>\n<code>/highlights instagram</code>",
                parse_mode=ParseMode.HTML,
            )
            return

        if ct == "reel":
            files = await dl_reel(text)
        elif ct == "igtv":
            files = await dl_igtv(text)
        else:
            files = await dl_post(text)

        if not files:
            await status.edit_text(
                "📭 <b>محتوایی یافت نشد.</b>\nلینک رو بررسی کنید یا پیج عمومی باشد.",
                parse_mode=ParseMode.HTML,
            )
            return

        sent = 0
        last_vid = None
        chat_id = update.effective_chat.id

        for item in files:
            fp = item["path"]
            sz = item["size"]

            if item["type"] == "video" and sz > MAX_SIZE_MB * 1048576:
                await status.edit_text("📦 <b>فایل بزرگ است، فشرده‌سازی...</b>", parse_mode=ParseMode.HTML)
                fp = _compress_video(fp)

            try:
                if item["type"] == "video":
                    with open(fp, "rb") as vf:
                        await ctx.bot.send_video(chat_id, vf, caption=f"🎥 دانلود شده\n📁 {fmt_size(sz)}")
                    last_vid = fp
                else:
                    with open(fp, "rb") as pf:
                        await ctx.bot.send_photo(chat_id, pf, caption=f"📸 دانلود شده\n📁 {fmt_size(sz)}")
                sent += 1
            except Exception as e:
                logger.error(f"send error: {e}")

        remaining = MAX_DAILY - _user_record(uid).get("downloads_today", 0)
        await status.edit_text(
            f"✅ <b>{sent} فایل دانلود شد!</b>\n\n📊 باقی‌مانده امروز: {remaining} دانلود",
            parse_mode=ParseMode.HTML,
        )

        if last_vid and ct in ("reel", "igtv"):
            kb = InlineKeyboardMarkup([[InlineKeyboardButton("دانلود صوت 🎵", callback_data=f"audio_{last_vid}")]])
            await update.message.reply_text("🎵 <b>صوت هم می‌خوای؟</b>", parse_mode=ParseMode.HTML, reply_markup=kb)

        # Cleanup
        for item in files:
            try:
                shutil.rmtree(Path(item["path"]).parent, ignore_errors=True)
                break
            except Exception:
                pass

    except Exception as e:
        logger.error(f"handle_text error: {e}")
        await status.edit_text("❌ <b>خطایی رخ داد!</b>\nدوباره تلاش کنید.", parse_mode=ParseMode.HTML)
        try:
            if ADMIN_ID:
                await ctx.bot.send_message(ADMIN_ID, f"⚠️ خطا:\n{str(e)[:300]}")
        except Exception:
            pass


async def error_handler(update: object, ctx: ContextTypes.DEFAULT_TYPE):
    logger.error("Unhandled exception: %s", ctx.error, exc_info=ctx.error)
    if ADMIN_ID:
        try:
            await ctx.bot.send_message(ADMIN_ID, f"⚠️ Bot error:\n{str(ctx.error)[:300]}")
        except Exception:
            pass


# ============================================================
# MAIN
# ============================================================
def main():
    _ensure_dirs()
    _cleanup_dir(DL_DIR)

    print("=" * 50)
    print("  Telegram Instagram Downloader Bot")
    print("=" * 50)
    print()
    print("✅ ربات فعال شد! برای خاموش کردن، این پنجره رو ببند.")
    print()

    if TOKEN == "YOUR_BOT_TOKEN_HERE":
        print("⚠️  لطفاً توکن ربات رو در فایل bot.py تنظیم کنید!")
        print("    از @BotFather در تلگرام توکن بگیرید.")
        input("\nPress Enter to exit...")
        return

    app = Application.builder().token(TOKEN).build()

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CommandHandler("story", cmd_story))
    app.add_handler(CommandHandler("highlights", cmd_highlights))
    app.add_handler(CallbackQueryHandler(cb_handler))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))
    app.add_error_handler(error_handler)

    print("Polling started. Press Ctrl+C to stop.")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()

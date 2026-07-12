# bot.py — Serena Unzip Bot v2 (Complete)
import asyncio
import datetime
import os
import random
import re
import shutil
import time
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from pyrogram import Client, enums, filters, idle
from pyrogram.errors import FloodWait, MessageNotModified
from pyrogram.types import (
    CallbackQuery, Chat, InlineKeyboardButton, InlineKeyboardMarkup, Message,
)

# ── Safe colored button helper (works with pyrofork; graceful fallback for plain pyrogram) ──
def _btn(text: str, callback_data: str, style: str = None) -> InlineKeyboardButton:
    """InlineKeyboardButton wrapper.
    NOTE: style= is a python-telegram-bot (PTB) feature, NOT Pyrogram.
    Pyrogram does not support button colors via style= parameter.
    Kept as API-compatible wrapper for future migration."""
    return InlineKeyboardButton(text, callback_data=callback_data)


async def _safe_edit(msg, text: str, **kwargs):
    """Edit silently — handles MessageNotModified + FloodWait."""
    try:
        await msg.edit_text(text, **kwargs)
    except MessageNotModified:
        pass
    except FloodWait as e:
        await asyncio.sleep(min(e.value, 15))
        try: await msg.edit_text(text, **kwargs)
        except Exception: pass
    except Exception:
        pass

async def _safe_reply(msg, text: str, **kwargs):
    """Reply with FloodWait retry."""
    try:
        return await msg.reply_text(text, **kwargs)
    except FloodWait as e:
        await asyncio.sleep(min(e.value, 15))
        try: return await msg.reply_text(text, **kwargs)
        except Exception: pass
    except Exception:
        pass
    return None


from config import Config
from database import (
    count_users, get_all_users, get_or_create_user, get_premium_until,
    get_referral_count, get_user_settings, has_been_referred, is_banned,
    register_referral, register_temp_path, save_user_settings, set_ban,
    set_premium, update_user_stats,
)
from utils.cleanup import cleanup_worker
from utils.cloud_upload import smart_upload, upload_to_gofile, upload_to_catbox
from utils.extractors import detect_encrypted, extract_archive
from utils.file_splitter import split_file, human_size
from utils.gdrive import get_gdrive_direct_link
from utils.http_downloader import download_file
from utils.link_parser import classify_link, extract_links_from_folder, find_links_in_text
from utils.m3u8_tools import download_m3u8_stream, get_m3u8_variants
from utils.media_tools import (
    add_watermark, compress_video, extract_audio, extract_subtitles,
    generate_thumbnail, get_media_info, merge_videos, take_screenshot,
)
from utils.password_list import COMMON_PASSWORDS
from utils.pdf_tools import (
    extract_text_from_pdf, get_pdf_info, merge_pdfs,
    parse_page_ranges, split_pdf, split_pdf_by_range,
)
from utils.progress import human_bytes, human_time, make_progress_message, progress_for_pyrogram
from utils.ytdl_tools import download_video as ytdl_download, get_formats, is_supported_url, _download_instagram_photos, _is_instagram_url
from utils.zip_creator import create_archive

# ── Client ──────────────────────────────────────────────────────────────────
app = Client(
    "serena_unzip_bot",
    api_id=Config.API_ID,
    api_hash=Config.API_HASH,
    bot_token=Config.BOT_TOKEN,
    in_memory=True,
)

VIDEO_EXT_SET = {".mp4", ".mkv", ".mov", ".avi", ".webm", ".ts"}
IMAGE_EXT_SET = {".jpg", ".jpeg", ".png", ".webp", ".gif"}
AUDIO_EXT_SET = {".mp3", ".m4a", ".aac", ".ogg", ".opus", ".flac", ".wav"}
PDF_EXT_SET   = {".pdf"}
ARCHIVE_EXTS  = (".zip",".rar",".7z",".tar",".gz",".tgz",".tar.gz",
                 ".tar.bz2",".tbz2",".bz2",".xz",".tar.xz")
EMOJI_LIST    = ["🚀","📦","🎬","🧩","📄","🔗","🧪","⚡","💾","🧰"]

# ── State dicts ─────────────────────────────────────────────────────────────
# Custom filter — non-command text (avoids TypeError from ~filters.command)
_non_cmd = filters.create(
    lambda _, __, m: not (
        (m.text    and m.text.strip().startswith("/")) or
        (m.caption and m.caption.strip().startswith("/"))
    )
)

user_locks:     Dict[int,asyncio.Lock]    = {}
tasks:          Dict[str,Dict[str,Any]]   = {}
M3U8_TASKS:     Dict[str,Dict[str,Any]]   = {}
YTDL_TASKS:     Dict[str,Dict[str,Any]]   = {}
COMPRESS_TASKS: Dict[str,Dict[str,Any]]   = {}
SPLIT_TASKS:    Dict[str,Dict[str,Any]]   = {}
SUB_TASKS:      Dict[str,Dict[str,Any]]   = {}
PDF_TASKS:      Dict[str,Dict[str,Any]]   = {}
BIG_FILE_TASKS: Dict[str,Dict[str,Any]]   = {}
pending_password: Dict[int,Dict[str,Any]] = {}
pending_state:    Dict[int,Dict[str,Any]] = {}
user_cancelled:   Dict[int,bool]          = {}
zip_sessions:     Dict[int,Dict[str,Any]] = {}
merge_sessions:   Dict[int,Dict[str,Any]] = {}
LINK_SESSIONS: Dict[Tuple[int,int],Dict[str,Any]] = {}
ZIP_QUEUE_SESSIONS: Dict[int,Dict[str,Any]] = {}  # uid → queue session
USER_TASKS: Dict[int,asyncio.Task] = {}           # uid → running asyncio Task
log_chat_info: Optional[Chat] = None
log_is_forum:  bool           = False
user_log_topics: Dict[int,int] = {}

# ════════════════════════════════════════════════════════════════════════════
# TINY HELPERS
# ════════════════════════════════════════════════════════════════════════════
def get_lock(uid):
    if uid not in user_locks: user_locks[uid] = asyncio.Lock()
    return user_locks[uid]

def is_owner(uid): return uid in Config.OWNER_IDS
def random_emoji(): return random.choice(EMOJI_LIST)

def _ext(name): return Path(name).suffix.lower()
def is_archive_file(n): return any(n.lower().endswith(e) for e in ARCHIVE_EXTS)
def is_video_file(n):   return _ext(n) in VIDEO_EXT_SET
def is_audio_file(n):   return _ext(n) in AUDIO_EXT_SET
def is_pdf_file(n):     return _ext(n) in PDF_EXT_SET
def is_image_file(n):   return _ext(n) in IMAGE_EXT_SET
def is_video_path(p):   return _ext(p) in VIDEO_EXT_SET

def _fmt_dur(s):
    s=int(s); h,r=divmod(s,3600); m,s=divmod(r,60)
    return f"{h:02d}:{m:02d}:{s:02d}"


async def _get_video_duration(path: str) -> int:
    """Get video duration in seconds for send_video calls (fixes 0:00 display)."""
    try:
        proc = await asyncio.create_subprocess_exec(
            "ffprobe", "-v", "quiet",
            "-show_entries", "format=duration",
            "-of", "default=noprint_wrappers=1:nokey=1",
            path,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        out, _ = await proc.communicate()
        val = out.decode().strip()
        return int(float(val)) if val else 0
    except Exception:
        return 0

def _secs_to_midnight():
    now=datetime.datetime.utcnow()
    mid=(now+datetime.timedelta(days=1)).replace(hour=0,minute=0,second=0,microsecond=0)
    return int((mid-now).total_seconds())

def _ht(s):
    if s<0: s=0
    h,r=divmod(s,3600); m,s=divmod(r,60)
    return f"{h}h {m}m" if h else f"{m}m {s}s"

async def is_premium_user(uid):
    t=await get_premium_until(uid)
    if not t: return False
    if t<time.time():
        await set_premium(uid,None); return False
    return True

async def check_rate_limit(uid, msg):
    if is_owner(uid) or await is_premium_user(uid): return True
    user=await get_or_create_user(uid); stats=user.get("stats",{})
    if stats.get("daily_tasks",0)>=Config.FREE_DAILY_TASK_LIMIT:
        await msg.reply_text(
            f"⚠️ <b>Daily limit reached!</b>\n📦 Tasks: <b>{stats['daily_tasks']}/{Config.FREE_DAILY_TASK_LIMIT}</b>\n"
            f"⏰ Resets in: <b>{_ht(_secs_to_midnight())}</b>\n\nGet ⭐ Premium for unlimited!",
            reply_markup=InlineKeyboardMarkup([[_btn("⭐ Get Premium", "premium_info", "primary")]]))
        return False
    if float(stats.get("daily_size_mb",0))>=Config.FREE_DAILY_SIZE_MB:
        await msg.reply_text("⚠️ Daily data limit reached. Get ⭐ Premium for no limits!"); return False
    last=stats.get("last_task_ts")
    if last:
        elapsed=(datetime.datetime.utcnow()-last).total_seconds() if isinstance(last,datetime.datetime) else time.time()-float(last)
        if elapsed<Config.FREE_MIN_WAIT_SEC:
            await msg.reply_text(f"⏳ Cooldown: <b>{int(Config.FREE_MIN_WAIT_SEC-elapsed)}s</b> baaki. Premium = 10s wait!"); return False
    return True

async def _get_thumb_mode(uid):
    cfg=await get_user_settings(uid); return (cfg or {}).get("thumb_mode","random")

async def choose_thumbnail(uid, vpath):
    mode=await _get_thumb_mode(uid); pos="00:00:00.200" if mode=="original" else "00:00:02"
    tp=vpath+".jpg"
    try: await generate_thumbnail(vpath,tp,time_pos=pos); return tp
    except: return None

async def build_caption(uid, default):
    cfg=await get_user_settings(uid)
    if not cfg: return default
    cap=default; base=cfg.get("caption_base")
    if base:
        n=cfg.get("caption_counter",0)+1; cfg["caption_counter"]=n
        cap=f"{n:03d} {base}"; await save_user_settings(uid,cfg)
    rf=cfg.get("replace_from"); rt=cfg.get("replace_to")
    if rf and rt is not None: cap=cap.replace(rf,rt)
    return cap

# ════════════════════════════════════════════════════════════════════════════
# LOGGING
# ════════════════════════════════════════════════════════════════════════════
async def _log_target(client, user):
    global log_chat_info, log_is_forum
    if not Config.LOG_CHANNEL_ID: return None,None
    if log_chat_info is None:
        try:
            c=await client.get_chat(Config.LOG_CHANNEL_ID)
            log_chat_info=c; log_is_forum=bool(getattr(c,"is_forum",False))
        except: return None,None
    cid=log_chat_info.id
    if not log_is_forum: return cid,None
    if user.id in user_log_topics: return cid,user_log_topics[user.id]
    root=None
    if hasattr(client,"create_forum_topic"):
        try:
            t=await client.create_forum_topic(cid,name=f"{user.first_name or 'User'} | {user.id}")
            root=getattr(t,"id",None) or getattr(t,"message_id",None)
        except: pass
    if root:
        user_log_topics[user.id]=root
        try: await client.send_message(cid,f"👤 {user.first_name} | {user.id}",reply_to_message_id=root)
        except: pass
    return cid,root

async def log_input(client, message, ctx):
    if not Config.LOG_CHANNEL_ID or not message.from_user: return
    user=message.from_user; cid,root=await _log_target(client,user)
    if not cid: return
    cap=f"🔹 INPUT\n• {user.first_name} (@{user.username or 'N/A'})\n• ID: {user.id}\n• {ctx}"
    kw={"reply_to_message_id":root} if root else {}
    try: await client.send_message(cid,cap,**kw)
    except: pass

async def log_output(client, user, msg, ctx):
    if not Config.LOG_CHANNEL_ID or not user or not msg: return
    cid,root=await _log_target(client,user)
    if not cid: return
    cap=f"✅ OUTPUT\n• {user.first_name} (@{user.username or 'N/A'})\n• ID: {user.id}\n• {ctx}"
    kw={"reply_to_message_id":root} if root else {}
    try: await client.send_message(cid,cap,**kw)
    except: pass

# ════════════════════════════════════════════════════════════════════════════
# KEYBOARDS
# ════════════════════════════════════════════════════════════════════════════
def _ob(): return InlineKeyboardButton("👤 Owner",url=f"https://t.me/{Config.OWNER_USERNAME}")
def _cb(): return InlineKeyboardButton("📢 Channel",url=f"https://t.me/{Config.FORCE_SUB_CHANNEL}")

def main_keyboard():
    return InlineKeyboardMarkup([
        [_cb()],
        [_btn("📦 Unzip File", "help:unzip", "success"),
         _btn("⬇️ Download Link", "help:link", "success")],
        [_btn("⚙️ Settings", "open_settings", "primary"),
         _btn("📊 My Stats", "show_mystats", "primary")],
        [_btn("💬 Help", "show_help", "primary"),
         _btn("⭐ Premium", "premium_info", "primary")],
        [_ob()],
    ])

def settings_keyboard():
    return InlineKeyboardMarkup([
        [_btn("📝 Caption", "settings:caption", "primary"),
         _btn("🔤 Replace Words", "settings:replace", "primary")],
        [_btn("📸 Original Thumb", "settings:thumb:original", "primary"),
         _btn("🎲 Random Thumb", "settings:thumb:random", "primary")],
        [_btn("⚙️ Reset All", "settings:reset", "primary")],
        [_ob()],
    ])

def file_action_keyboard(msg, fname, fsize_mb=0):
    cid,mid=msg.chat.id,msg.id; rows=[]
    if is_archive_file(fname):
        rows.append([_btn("📦 Unzip", f"unzip|{cid}|{mid}|nopass", "danger"),
                     _btn("🔐 With Password", f"unzip|{cid}|{mid}|askpass", "success")])
        if Config.ENABLE_AUTO_PASSWORD:
            rows.append([_btn("🔑 Auto-Try Passwords", f"unzip|{cid}|{mid}|autopass", "success")])
    elif is_video_file(fname):
        rows.append([_btn("🎵 Extract Audio", f"audio|{cid}|{mid}", "success"),
                     _btn("📦 Compress", f"compress|{cid}|{mid}", "primary")])
        rows.append([_btn("✂️ Split File", f"split|{cid}|{mid}", "primary"),
                     _btn("ℹ️ File Info", f"finfo|{cid}|{mid}", "primary")])
        if _ext(fname) in {".mkv",".mp4",".mov",".avi"}:
            rows.append([_btn("🔤 Extract Subs", f"subs|{cid}|{mid}", "success"),
                         _btn("📸 Screenshot", f"screenshot|{cid}|{mid}", "primary")])
        rows.append([_btn("💧 Watermark", f"watermark|{cid}|{mid}", "primary"),
                     _btn("✏️ Rename", f"rename|{cid}|{mid}", "primary")])
    elif is_audio_file(fname):
        rows.append([_btn("ℹ️ File Info", f"finfo|{cid}|{mid}", "primary"),
                     _btn("✏️ Rename", f"rename|{cid}|{mid}", "primary")])
    elif is_pdf_file(fname):
        rows.append([_btn("📄 PDF Tools", f"pdf|{cid}|{mid}", "primary"),
                     _btn("ℹ️ File Info", f"finfo|{cid}|{mid}", "primary")])
        rows.append([_btn("✏️ Rename", f"rename|{cid}|{mid}", "primary")])
    else:
        # BUG FIX #1 — unsupported file
        rows.append([_btn("📤 Send As-Is", f"sendas|{cid}|{mid}", "success"),
                     _btn("✏️ Rename", f"rename|{cid}|{mid}", "primary")])
        rows.append([_btn("🗜 Add to ZIP", f"addtozip|{cid}|{mid}", "primary")])
    if fsize_mb>Config.AUTO_SPLIT_MB:
        rows.append([_btn("✂️ Split Parts", f"split|{cid}|{mid}", "primary"),
                     _btn("☁️ Cloud Upload", f"cloudopt|{cid}|{mid}", "success")])
    rows.append([_ob()]); return InlineKeyboardMarkup(rows)

# ════════════════════════════════════════════════════════════════════════════
# FORCE SUB
# ════════════════════════════════════════════════════════════════════════════
async def check_force_sub(client, message):
    if not message.from_user or not Config.FORCE_SUB_CHANNEL: return True
    # Groups/channels mein force_sub check nahi hoga — only DM
    if hasattr(message, "chat") and message.chat.type != enums.ChatType.PRIVATE:
        return True
    try:
        m = await client.get_chat_member(Config.FORCE_SUB_CHANNEL, message.from_user.id)
        if m.status not in (
            enums.ChatMemberStatus.OWNER,
            enums.ChatMemberStatus.ADMINISTRATOR,
            enums.ChatMemberStatus.MEMBER,
        ):
            raise ValueError
        return True
    except Exception:
        try:
            await message.reply_text(
                "⚠️ Pehle channel join karo!",
                reply_markup=InlineKeyboardMarkup([
                    [_cb()],
                    [_btn("✅ Joined! Try Again", "retry_force_sub", "success")],
                ]),
            )
        except Exception:
            pass
        return False

# ════════════════════════════════════════════════════════════════════════════
# COMMANDS
# ════════════════════════════════════════════════════════════════════════════
@app.on_message(filters.command("start"))
async def start_cmd(client, message):
    if not message.from_user: return
    uid=message.from_user.id
    if await is_banned(uid): return
    if not await check_force_sub(client,message): return
    await get_or_create_user(uid)
    args=message.command[1:]
    if args and args[0].startswith("ref_"):
        try:
            rid=int(args[0][4:])
            if rid!=uid and not await has_been_referred(uid):
                await register_referral(uid,rid)
                rc=await get_referral_count(rid)
                if rc>=Config.REFERRAL_REQUIRED:
                    await set_premium(rid,time.time()+Config.REFERRAL_REWARD_DAYS*86400)
                    try: await client.send_message(rid,f"🎉 {Config.REFERRAL_REQUIRED} referrals! <b>{Config.REFERRAL_REWARD_DAYS} days Premium</b> free mila!")
                    except: pass
        except: pass
    name=message.from_user.first_name or "there"
    prem="⭐ Premium" if await is_premium_user(uid) else "Free"
    cap=(f"Hey <b>{name}</b>! 👋\n\nWelcome to <b>{Config.BOT_NAME}</b> [{prem}]\n\n"
         f"{random_emoji()} Unzip 20+ formats (ZIP/RAR/7Z/TAR + passwords)\n"
         f"{random_emoji()} Instagram (Photos/Reels/Stories), Twitter, Facebook, TikTok downloader\n"
         f"{random_emoji()} Video Compress, Merge, Split, Watermark\n"
         f"{random_emoji()} Extract Audio, Subtitles, Screenshots\n"
         f"{random_emoji()} ZIP Creator, File Renamer, PDF Tools\n"
         f"{random_emoji()} Auto-process TXT/M3U8/GDrive links\n\nUse /help for full guide.")
    if Config.START_PIC: await message.reply_photo(Config.START_PIC,caption=cap,reply_markup=main_keyboard())
    else: await message.reply_text(cap,reply_markup=main_keyboard())


@app.on_message(filters.command("help"))
async def help_cmd(client, message):
    text=(
        "✨ <b>Serena Unzip v2 — Help</b>\n\n"
        "📦 <b>Archives</b>\n"
        "• Send archive → Unzip / Password / Auto-Try\n"
        "• ZIP, RAR, 7Z, TAR, GZ, BZ2, XZ supported\n\n"
        "🎬 <b>Video Tools</b>\n"
        "• Extract Audio  • Compress (360p–1080p)\n"
        "• Split File  • Extract Subtitles\n"
        "• Screenshot  • Watermark  • Rename\n\n"
        "⬇️ <b>Downloaders</b>\n"
        "• <code>/ytdl &lt;link&gt;</code> — Instagram, Twitter, TikTok, Facebook, Vimeo…\n"
        "• TXT / links → auto-download (direct, m3u8, GDrive)\n\n"
        "🗜 <b>ZIP Creator</b>: <code>/zip</code> → files bhejo → ✅ Done\n"
        "📦 <b>ZIP Queue</b>: <code>/zipqueue</code> → multiple ZIPs → process all in order\n"
        "🔗 <b>Merge Videos</b>: <code>/merge</code> → videos bhejo → ✅ Merge\n"
        "✏️ <b>Rename</b>: Reply to file → <code>/rename</code>\n"
        "ℹ️ <b>Info</b>: Reply to file → <code>/info</code>\n"
        "📄 <b>PDF</b>: Tap PDF Tools button\n"
        "📊 <b>Stats</b>: <code>/mystats</code>\n"
        "🎁 <b>Referral</b>: <code>/refer</code> — 5 friends = 7 days Premium FREE!\n\n"
        "👥 <b>Groups — Reply to file + command:</b>\n"
        "<code>/unzip</code>  <code>/audio</code>  <code>/compress</code>  <code>/info</code>\n"
        "<code>/subs</code>  <code>/rename</code>  <code>/split</code>  <code>/screenshot</code>\n"
        "<code>/pdf</code>  <code>/watermark</code>  <code>/ytdl</code>\n\n"
        "🛠 <b>Admin</b>: <code>/admin</code>  <code>/status</code>  <code>/broadcast</code>\n"
        "<code>/premium &lt;id&gt; [days]</code>  <code>/ban</code>  <code>/unban</code>"
    )
    await message.reply_text(text)


@app.on_message(filters.command("settings"))
async def settings_cmd(client, message):
    if not message.from_user: return
    uid=message.from_user.id; cfg=await get_user_settings(uid) or {}
    text=(f"{random_emoji()} <b>Your Settings</b>\n\n"
          f"📝 Caption base: <code>{cfg.get('caption_base') or 'None'}</code>\n"
          f"🔤 Replace: <code>{cfg.get('replace_from') or 'None'}</code> → <code>{cfg.get('replace_to') or 'None'}</code>\n"
          f"🖼 Thumbnail: <code>{cfg.get('thumb_mode','random')}</code>\n\n"
          "Use buttons below:")
    await message.reply_text(text, reply_markup=settings_keyboard())


@app.on_message(filters.command("cancel"))
async def cancel_cmd(client, message):
    if not message.from_user: return
    uid=message.from_user.id
    user_cancelled[uid]=True
    # Kill any running asyncio task
    task=USER_TASKS.pop(uid,None)
    if task and not task.done(): task.cancel()
    # Clear all sessions
    zip_sessions.pop(uid,None); merge_sessions.pop(uid,None)
    pending_state.pop(uid,None); pending_password.pop(uid,None)
    # Also clear ZIP queue if active
    q=ZIP_QUEUE_SESSIONS.get(uid)
    if q: q["cancelled"]=True; ZIP_QUEUE_SESSIONS.pop(uid,None)
    await message.reply_text(
        "🛑 <b>Everything Cancelled!</b>\n\n"
        "✅ Running task killed\n✅ All sessions cleared\n✅ ZIP queue removed"
    )


@app.on_message(filters.command("mystats"))
async def mystats_cmd(client, message):
    if not message.from_user: return
    uid=message.from_user.id; user=await get_or_create_user(uid)
    stats=user.get("stats",{}); cfg=await get_user_settings(uid) or {}
    ip=await is_premium_user(uid); pu=await get_premium_until(uid); rc=await get_referral_count(uid)
    ps="✅ Active"
    if ip and pu: ps+=f" (expires {datetime.datetime.utcfromtimestamp(pu).strftime('%d %b %Y')})"
    elif not ip: ps="❌ Free"
    await message.reply_text(
        f"📊 <b>Your Stats</b>\n\n👤 {message.from_user.first_name}\n🆔 <code>{uid}</code>\n"
        f"⭐ Premium: {ps}\n\n📦 Tasks today: <b>{stats.get('daily_tasks',0)}/{Config.FREE_DAILY_TASK_LIMIT}</b>\n"
        f"💾 Data today: <b>{stats.get('daily_size_mb',0):.1f}/{Config.FREE_DAILY_SIZE_MB} MB</b>\n"
        f"🏆 Total tasks: <b>{stats.get('total_tasks',0)}</b>\n🎁 Referrals: <b>{rc}</b>\n\n"
        f"📝 Caption: <code>{cfg.get('caption_base') or 'None'}</code>\n"
        f"🖼 Thumb: <code>{cfg.get('thumb_mode','random')}</code>")


@app.on_message(filters.command("refer"))
async def refer_cmd(client, message):
    if not message.from_user: return
    uid=message.from_user.id; me=await client.get_me()
    link=f"https://t.me/{me.username}?start=ref_{uid}"
    rc=await get_referral_count(uid); need=max(0,Config.REFERRAL_REQUIRED-rc)
    await message.reply_text(
        f"🎁 <b>Referral Program</b>\n\nYour link:\n<code>{link}</code>\n\n"
        f"✅ Referred: <b>{rc}/{Config.REFERRAL_REQUIRED}</b>  |  Aur {need} chahiye\n"
        f"🏆 Reward: <b>{Config.REFERRAL_REWARD_DAYS} days Premium FREE!</b>",
        reply_markup=InlineKeyboardMarkup([[
            InlineKeyboardButton("📤 Share",url=f"https://t.me/share/url?url={link}&text=Join+Serena+Bot!")
        ]]))


@app.on_message(filters.command("ytdl"))
async def ytdl_cmd(client, message):
    if not message.from_user: return
    uid=message.from_user.id
    if await is_banned(uid): return
    if not await check_force_sub(client,message): return
    if not await check_rate_limit(uid,message): return
    args=message.command[1:]
    if not args:
        await message.reply_text(
            "📥 Usage: <code>/ytdl &lt;URL&gt;</code>\n\n"
            "Supported: Instagram, Twitter/X, Facebook, TikTok,\nVimeo, Dailymotion, Reddit + 1000 more\n\n"
            "<i>Note: YouTube & paid education platforms supported nahi hain.</i>"); return
    url=args[0].strip()
    if not is_supported_url(url):
        await message.reply_text("❌ Ye URL supported nahi hai."); return
    status=await message.reply_text("🔍 Fetching info…")
    try: formats=await get_formats(url)
    except Exception as e:
        await status.edit_text(f"❌ Info fetch failed:\n<code>{e}</code>"); return
    task_id=uuid.uuid4().hex
    temp_root=Path(Config.TEMP_DIR)/str(uid)/task_id
    temp_root.mkdir(parents=True,exist_ok=True)
    await register_temp_path(uid,str(temp_root),Config.AUTO_DELETE_DEFAULT_MIN)
    YTDL_TASKS[task_id]={"user_id":uid,"url":url,"formats":formats,"temp_root":str(temp_root),"chat_id":message.chat.id,"reply_to":message.id}
    buttons=[]
    for i,f in enumerate(formats):
        sz=f" (~{human_bytes(f['size_approx'])})" if f.get("size_approx") else ""
        buttons.append([_btn(f"{f['label']}{sz}", f"ytdlq|{task_id}|{i}", "primary")])
    buttons.append([_btn("❌ Cancel", f"ytdlcancel|{task_id}", "danger")])
    try: await status.edit_text(f"🎬 Quality choose karo:\n<code>{url}</code>",reply_markup=InlineKeyboardMarkup(buttons))
    except: pass


@app.on_message(filters.command("zip"))
async def zip_cmd(client, message):
    if not message.from_user: return
    uid=message.from_user.id
    if await is_banned(uid): return
    if uid in zip_sessions:
        await message.reply_text("Ek ZIP session already chal raha hai. /cancel karo pehle."); return
    temp_root=Path(Config.TEMP_DIR)/str(uid)/uuid.uuid4().hex
    temp_root.mkdir(parents=True,exist_ok=True)
    await register_temp_path(uid,str(temp_root),Config.AUTO_DELETE_DEFAULT_MIN)
    zip_sessions[uid]={"files":[],"temp_root":str(temp_root),"chat_id":message.chat.id,"reply_to":message.id,"password":None}
    await message.reply_text(
        "📦 <b>ZIP Creator Mode ON!</b>\n\nAb files bhejo ek ek karke.\nDone hone pe ✅ dabao.",
        reply_markup=InlineKeyboardMarkup([
            [_btn("✅ Done — Create ZIP", f"zipfile|done|{uid}", "success"),
             _btn("🔐 Add Password", f"zipfile|askpass|{uid}", "primary")],
            [_btn("❌ Cancel", f"zipfile|cancel|{uid}", "danger")]]))


@app.on_message(filters.command("merge"))
async def merge_cmd(client, message):
    if not message.from_user: return
    uid=message.from_user.id
    if await is_banned(uid): return
    if uid in merge_sessions:
        await message.reply_text("Ek merge session chal raha hai. /cancel karo."); return
    temp_root=Path(Config.TEMP_DIR)/str(uid)/uuid.uuid4().hex
    temp_root.mkdir(parents=True,exist_ok=True)
    await register_temp_path(uid,str(temp_root),Config.AUTO_DELETE_DEFAULT_MIN)
    merge_sessions[uid]={"files":[],"temp_root":str(temp_root),"chat_id":message.chat.id,"reply_to":message.id}
    await message.reply_text(
        "🔗 <b>Merge Mode ON!</b>\n\nVideos bhejo (same format). Order same rahega.",
        reply_markup=InlineKeyboardMarkup([
            [_btn("✅ Merge Now", f"mergefile|done|{uid}", "success"),
             _btn("❌ Cancel", f"mergefile|cancel|{uid}", "danger")]]))


@app.on_message(filters.command(["rename","info","subs","screenshot","pdf","compress","split","audio","unzip","watermark"]))
async def file_command_handler(client, message):
    if not message.from_user: return
    uid=message.from_user.id
    if await is_banned(uid): return
    cmd=message.command[0].lower()
    r=message.reply_to_message
    media=None
    if r: media=r.document or r.video or r.audio
    if not r or not media:
        hints={"rename":"Kisi file pe reply karke /rename use karo.",
               "info":"Kisi file pe reply karke /info use karo.",
               "subs":"Kisi MKV/MP4 pe reply karke /subs use karo.",
               "screenshot":"Kisi video pe reply karke /screenshot HH:MM:SS use karo.",
               "pdf":"Kisi PDF pe reply karke /pdf use karo.",
               "compress":"Kisi video pe reply karke /compress use karo.",
               "split":"Kisi file pe reply karke /split use karo.",
               "audio":"Kisi video pe reply karke /audio use karo.",
               "unzip":"Kisi archive pe reply karke /unzip use karo.",
               "watermark":"Kisi video pe reply karke /watermark [text] use karo."}
        await message.reply_text(hints.get(cmd,"Kisi file pe reply karke command use karo.")); return
    cid,mid=r.chat.id,r.id
    fname=media.file_name or "file"
    if cmd=="rename":
        pending_state[uid]={"action":"rename","chat_id":cid,"msg_id":mid,"fname":fname}
        await message.reply_text(f"✏️ <code>{fname}</code> ka naya naam bhejo:")
    elif cmd=="info": await _handle_file_info(client,message,r)
    elif cmd=="subs": await _trigger_subs(client,message,r)
    elif cmd=="screenshot":
        args=message.command[1:]
        if args: await _handle_screenshot_with_time(client,message,r,args[0])
        else:
            pending_state[uid]={"action":"screenshot","chat_id":cid,"msg_id":mid}
            await message.reply_text("📸 Time bhejo: <code>MM:SS</code> ya <code>HH:MM:SS</code>")
    elif cmd=="pdf": await _trigger_pdf_tools(client,message,r,fname)
    elif cmd=="compress":
        if not await check_rate_limit(uid,message): return
        await _trigger_compress(client,message,r,cid,mid)
    elif cmd=="split": await _trigger_split(client,message,r,cid,mid,fname)
    elif cmd=="audio": await handle_extract_audio(client,None,r,reply_to_msg=message)
    elif cmd=="unzip":
        if not is_archive_file(fname): await message.reply_text("Ye archive nahi hai."); return
        if not await check_rate_limit(uid,message): return
        await run_unzip_task(client,r,password=None,reply_msg=message)
    elif cmd=="watermark":
        args_list=message.command[1:]
        if args_list: await _handle_watermark(client,message,r," ".join(args_list))
        else:
            pending_state[uid]={"action":"watermark","chat_id":cid,"msg_id":mid}
            await message.reply_text("💧 Watermark text bhejo:")


@app.on_message(filters.command(["status","users"]) & filters.private)
async def status_cmd(client, message):
    if not message.from_user or not is_owner(message.from_user.id): return
    total,premium,banned=await count_users(); disk=shutil.disk_usage("/")
    await message.reply_text(
        f"📊 <b>Bot Status</b>\n\n👥 Users: <b>{total}</b>\n⭐ Premium: <b>{premium}</b>\n"
        f"🚫 Banned: <b>{banned}</b>\n\n💾 Total: <code>{human_bytes(disk.total)}</code>\n"
        f"💾 Used: <code>{human_bytes(disk.used)}</code>\n💾 Free: <code>{human_bytes(disk.free)}</code>")


@app.on_message(filters.command("admin") & filters.private)
async def admin_cmd(client, message):
    if not message.from_user or not is_owner(message.from_user.id): return
    total,premium,banned=await count_users(); disk=shutil.disk_usage("/")
    await message.reply_text(
        f"🛡 <b>Admin Panel</b>\n\n👥 {total}  ⭐ {premium}  🚫 {banned}\n"
        f"💾 Free: {human_bytes(disk.free)}",
        reply_markup=InlineKeyboardMarkup([
            [_btn("📊 Full Stats", "admin:status", "primary"),
             _btn("📢 Broadcast hint", "admin:broadcast", "primary")],
            [_btn("🧹 Clean Storage", "admin:clean", "primary")]]))


@app.on_message(filters.command("broadcast") & filters.private)
async def broadcast_cmd(client, message):
    if not message.from_user or not is_owner(message.from_user.id): return
    if not message.reply_to_message: await message.reply_text("Reply to a message first."); return
    users=await get_all_users(); sent=failed=0
    for u in users:
        try: await message.reply_to_message.copy(chat_id=u); sent+=1; await asyncio.sleep(0.05)
        except: failed+=1
    await message.reply_text(f"📢 Done. Sent: {sent}  Failed: {failed}")


@app.on_message(filters.command("premium"))
async def premium_cmd(client, message):
    if not message.from_user or not is_owner(message.from_user.id): return
    parts=(message.text or "").split()
    if len(parts)<2: await message.reply_text("Usage: /premium <id> [days]"); return
    try: target=int(parts[1])
    except: await message.reply_text("ID must be integer."); return
    days=int(parts[2]) if len(parts)>=3 else 30
    await set_premium(target,time.time()+days*86400)
    await message.reply_text(f"⭐ User {target} = Premium for {days} days.")


@app.on_message(filters.command(["ban","unban"]) & filters.private)
async def ban_cmd(client, message):
    if not message.from_user or not is_owner(message.from_user.id): return
    cmd=message.command[0].lower(); target=None
    if message.reply_to_message: target=message.reply_to_message.from_user.id
    elif len(message.command)>1:
        try: target=int(message.command[1])
        except: pass
    if not target: await message.reply_text("User id ya reply use karo."); return
    await set_ban(target,cmd=="ban")
    await message.reply_text(f"User {target} {'banned' if cmd=='ban' else 'unbanned'}.")



# ════════════════════════════════════════════════════════════════════════════
# ZIP QUEUE — /zipqueue command + auto batch processing
# ════════════════════════════════════════════════════════════════════════════
@app.on_message(filters.command(["zipqueue","zqueue","zq"]))
async def zipqueue_cmd(client, message):
    """Advanced ZIP Queue: batch extract multiple archives with full ETA, progress & stats."""
    if not message.from_user: return
    uid = message.from_user.id
    if await is_banned(uid): return
    if not await check_force_sub(client, message): return
    await get_or_create_user(uid)
    in_group = message.chat.type != enums.ChatType.PRIVATE

    if uid in ZIP_QUEUE_SESSIONS and not ZIP_QUEUE_SESSIONS[uid].get("processing"):
        sess = ZIP_QUEUE_SESSIONS[uid]
        n = len(sess["files"])
        total_mb = sum(f["size"] for f in sess["files"]) / 1048576
        await message.reply_text(
            f"📦 <b>Queue Already Active!</b>\n\n"
            f"📋 ZIPs queued : <b>{n}</b>\n"
            f"💾 Total size  : <b>{total_mb:.1f} MB</b>\n\n"
            "Aur ZIPs bhejo ya neeche process karo ⬇️",
            reply_markup=InlineKeyboardMarkup([
                [_btn(f"▶️ Process All ({n} ZIPs)", f"zq_start|{uid}", "success")],
                [_btn("📋 List Queue", f"zq_list|{uid}", "primary"),
                 _btn("🗑 Clear", f"zq_cancel|{uid}", "danger")],
            ])
        ); return

    ZIP_QUEUE_SESSIONS[uid] = {
        "files": [],
        "chat_id":         message.chat.id,
        "reply_to":        message.id,
        "thread_id":       message.message_thread_id,   # ← Topic/Thread ID fix
        "cancelled":       False,
        "processing":      False,
        "default_password": None,
        "created_at":      time.time(),
    }
    group_note = (
        "\n\n📌 <b>Group Note:</b> Bot ko group mein files milti hain tabhi jab "
        "Bot ka Privacy Mode OFF ho (BotFather mein set karo)."
        if in_group else ""
    )
    await message.reply_text(
        "📦 <b>ZIP Queue Mode ON!</b>\n\n"
        "📌 <b>How to use:</b>\n"
        "  1️⃣  ZIP files bhejo (ek ek ya jaldi jaldi)\n"
        "  2️⃣  <b>▶️ Process</b> dabao — sab extract honge\n"
        "  3️⃣  Har ZIP ke baad cache turant delete hoga ✅\n\n"
        "🔐 <b>Password:</b> /zqpass [password] se saari ZIPs ka common password set karo\n"
        "📋 <b>List:</b> Queue mein kaun kaun si ZIPs hain dekhne ke liye button dabao\n"
        "🛑 <b>Cancel:</b> /cancelqueue ya button se cancel karo"
        + group_note,
        reply_markup=InlineKeyboardMarkup([
            [_btn("▶️ Process Queue (0 ZIPs)", f"zq_start|{uid}", "success")],
            [_btn("📋 List Queue", f"zq_list|{uid}", "primary"),
             _btn("🗑 Cancel Queue", f"zq_cancel|{uid}", "danger")],
        ])
    )


@app.on_message(filters.command(["zqpass"]))
async def zqpass_cmd(client, message):
    """Set a common password for all ZIPs in the queue."""
    if not message.from_user: return
    uid = message.from_user.id
    sess = ZIP_QUEUE_SESSIONS.get(uid)
    if not sess:
        await message.reply_text("❌ Koi active queue nahi. Pehle /zipqueue chalao."); return
    args = message.command[1:]
    if not args:
        await message.reply_text("❌ Password bhejo: <code>/zqpass yourpassword</code>"); return
    password = " ".join(args)
    sess["default_password"] = password
    await message.reply_text(f"🔐 Queue password set: <tg-spoiler>{password}</tg-spoiler>\nSaari ZIPs is password se extract hongi.")


@app.on_message(filters.command(["cancelqueue","qcancel"]))
async def cancelqueue_cmd(client, message):
    if not message.from_user: return
    uid = message.from_user.id
    sess = ZIP_QUEUE_SESSIONS.get(uid)
    if sess: sess["cancelled"] = True
    task = USER_TASKS.pop(uid, None)
    if task and not task.done(): task.cancel()
    ZIP_QUEUE_SESSIONS.pop(uid, None)
    user_cancelled[uid] = True
    await message.reply_text(
        "🛑 <b>ZIP Queue cancel ho gaya!</b>\n"
        "Saari pending ZIPs hata di gayi hain.",
        reply_markup=InlineKeyboardMarkup([[
            _btn("📦 Naya Queue Start", "cmd_zipqueue", "success")
        ]])
    )


async def _process_zip_queue(client, uid: int, chat_id: int, reply_to: int, thread_id: int = None):
    """Process all queued ZIPs sequentially. Cache delete after each ZIP."""
    sess = ZIP_QUEUE_SESSIONS.get(uid)
    if not sess or not sess["files"]: return
    USER_TASKS[uid] = asyncio.current_task()
    # ── Sort by message ID → guarantees exact order user sent files ──
    sorted_files = sorted(sess["files"], key=lambda f: f.get("msg_id", 0))
    total = len(sorted_files)
    ok = fail = 0
    header = await client.send_message(
        chat_id,
        f"🚀 <b>ZIP Queue Processing Start!</b>\n📦 Total: <b>{total}</b> ZIPs\n"
        f"📋 Sequence: sorted by send order ✅",
        reply_to_message_id=reply_to,
        message_thread_id=thread_id,
    )
    # ── Delete old queue notification message (reply_to) to keep chat clean ──
    try:
        await client.delete_messages(chat_id, [reply_to])
    except Exception:
        pass
    for i, finfo in enumerate(sorted_files, 1):
        if sess.get("cancelled"): break
        fname = finfo["file_name"]
        st = await client.send_message(
            chat_id,
            f"📥 <b>[{i}/{total}]</b> Downloading: <code>{fname}</code>…",
            reply_to_message_id=header.id
        )
        item_root = Path(Config.TEMP_DIR) / str(uid) / uuid.uuid4().hex
        item_root.mkdir(parents=True, exist_ok=True)
        try:
            start_dl = time.time()
            await _safe_edit(st, f"📥 <b>[{i}/{total}]</b> Downloading: <code>{fname}</code>…")
            dl = await client.download_media(
                finfo["file_id"], file_name=str(item_root),
                progress=progress_for_pyrogram,
                progress_args=(st, start_dl, fname, "Downloading")
            )
            if not dl: raise RuntimeError("Download failed")
            if sess.get("cancelled"): break
            await _safe_edit(st, f"🔓 <b>[{i}/{total}]</b> Extracting: <code>{fname}</code>…")
            extract_dir = item_root / "extracted"
            result = extract_archive(dl, str(extract_dir), password=finfo.get("password"))
            rel_files = sorted(result["files"], key=str.lower)
            if sess.get("cancelled"): break
            await st.edit_text(
                f"📤 <b>[{i}/{total}]</b> Sending <b>{len(rel_files)}</b> files…\n"
                f"📄 from: <code>{fname}</code>"
            )
            sent_n = 0
            for rel in rel_files:
                if sess.get("cancelled"): break
                full = extract_dir / rel
                if not full.is_file(): continue
                try:
                    if is_video_path(rel):
                        dur = await _get_video_duration(str(full))
                        thumb = await choose_thumbnail(uid, str(full))
                        cap = await build_caption(uid, Path(rel).name)
                        start_up = time.time()
                        up_sz = full.stat().st_size
                        up_st = await make_progress_message(
                            client, chat_id, reply_to,
                            f"📤 <b>[{i}/{total}]</b> Uploading: <code>{Path(rel).name}</code>…",
                            thread_id=thread_id,
                        )
                        async def _up_v_prog(cur, tot,
                                             _m=up_st, _sd=start_up,
                                             _fn=Path(rel).name, _ksz=up_sz):
                            await progress_for_pyrogram(cur, tot, _m, _sd, _fn,
                                direction="Uploading", known_total=_ksz)
                        await client.send_video(chat_id, str(full), caption=cap,
                            thumb=thumb, duration=dur,
                            progress=_up_v_prog,
                            reply_to_message_id=reply_to,
                            message_thread_id=thread_id)
                        if up_st:
                            try: await up_st.delete()
                            except: pass
                    elif is_image_file(rel):
                        await client.send_photo(chat_id, str(full),
                            caption=Path(rel).name, reply_to_message_id=reply_to,
                            message_thread_id=thread_id)
                    elif is_audio_file(rel):
                        await client.send_audio(chat_id, str(full),
                            caption=Path(rel).name, reply_to_message_id=reply_to,
                            message_thread_id=thread_id)
                    else:
                        start_up = time.time()
                        await st.edit_text(f"📤 <b>[{i}/{total}]</b> Uploading: <code>{Path(rel).name}</code>…")
                        await client.send_document(chat_id, str(full),
                            caption=rel,
                            progress=progress_for_pyrogram,
                            progress_args=(st, start_up, Path(rel).name, "Uploading"),
                            reply_to_message_id=header.id)
                    sent_n += 1
                except Exception as e:
                    await client.send_message(chat_id,
                        f"⚠️ <code>{Path(rel).name}</code>: {str(e)[:150]}",
                        reply_to_message_id=reply_to,
                        message_thread_id=thread_id)
                await asyncio.sleep(0.8)  # 0.8s gap prevents FloodWait
            try:
                await st.edit_text(
                    f"✅ <b>[{i}/{total}]</b> Done: <code>{fname}</code>\n"
                    f"📁 {sent_n}/{len(rel_files)} files sent.")
                await asyncio.sleep(1.5)
                try: await st.delete()   # ← delete status message after done
                except: pass
            except: pass
            ok += 1
            # ── Send GIF after EVERY successfully extracted ZIP ──
            if Config.QUEUE_END_GIF:
                try:
                    gif = Config.QUEUE_END_GIF.strip()
                    if gif.startswith("http"):
                        await client.send_animation(chat_id, gif,
                            reply_to_message_id=reply_to)
                    else:
                        try:
                            await client.send_sticker(chat_id, gif,
                                reply_to_message_id=reply_to)
                        except Exception:
                            await client.send_animation(chat_id, gif,
                                reply_to_message_id=reply_to)
                except Exception:
                    pass
            # Delete per-ZIP status message to keep chat clean
            try:
                await st.delete()
            except Exception:
                pass
        except asyncio.CancelledError:
            raise
        except Exception as e:
            try: await st.edit_text(
                f"❌ <b>[{i}/{total}]</b> Failed: <code>{fname}</code>\n"
                f"<code>{str(e)[:250]}</code>")
            except: pass
            fail += 1
        finally:
            # ✅ Cache delete immediately after each ZIP
            import shutil as _sh
            _sh.rmtree(str(item_root), ignore_errors=True)
        await asyncio.sleep(0.5)
    USER_TASKS.pop(uid, None)
    cancelled = sess.get("cancelled", False)
    ZIP_QUEUE_SESSIONS.pop(uid, None)
    try:
        if cancelled:
            await header.edit_text(
                f"🛑 <b>Queue Cancelled!</b>\n"
                f"✅ Processed: {ok} | ❌ Failed: {fail} | ⏭ Skipped: {total-ok-fail}")
        else:
            await header.edit_text(
                f"🎉 <b>Queue Complete!</b>\n\n"
                f"📦 Total: <b>{total}</b>  ✅ OK: <b>{ok}</b>  ❌ Failed: <b>{fail}</b>\n\n"
                f"🗑 Cache cleared after each ZIP!")
            # ── End animation (QUEUE_END_GIF from Render env) ──
            if Config.QUEUE_END_GIF:
                try:
                    gif_url = Config.QUEUE_END_GIF.strip()
                    if gif_url.startswith("http"):
                        # Giphy / web URL → send as animation
                        await client.send_animation(
                            chat_id,
                            gif_url,
                            reply_to_message_id=reply_to,
                        )
                    else:
                        # Telegram file_id or sticker_id
                        try:
                            await client.send_sticker(chat_id, gif_url, reply_to_message_id=reply_to)
                        except Exception:
                            await client.send_animation(chat_id, gif_url, reply_to_message_id=reply_to)
                except Exception:
                    pass
    except: pass

# ════════════════════════════════════════════════════════════════════════════
# FILE HANDLER
# ════════════════════════════════════════════════════════════════════════════
@app.on_message((filters.document|filters.video|filters.photo|filters.audio) )
async def on_file(client, message):
    if not message.from_user: return
    uid=message.from_user.id

    # ── GROUP FILTER: groups mein sirf ZIP queue wali files process hongi ──
    # Baaki sab (photos, videos, random docs) ignore — kachra nahi badhega
    # Private DM mein normal kaam karta hai
    is_group = message.chat and message.chat.type in (
        enums.ChatType.GROUP, enums.ChatType.SUPERGROUP
    )
    if is_group:
        # Only allow if ZIP queue is active for this user AND file is a ZIP
        media_check = message.document or message.video
        fname_check = (media_check.file_name or "") if media_check else ""
        in_queue = uid in ZIP_QUEUE_SESSIONS and not ZIP_QUEUE_SESSIONS[uid].get("processing")
        if not (in_queue and is_archive_file(fname_check)):
            return  # ← group mein command ke bina kuch nahi hoga

    if await is_banned(uid): return
    if not await check_force_sub(client,message): return
    await get_or_create_user(uid)
    media=message.document or message.video
    fname=""
    if media: fname=media.file_name or "file"
    elif message.audio: fname=message.audio.file_name or "audio.mp3"
    # ZIP session collect
    if uid in zip_sessions and media:
        sess=zip_sessions[uid]
        sess["files"].append({"file_id":media.file_id,"file_name":fname,"size":getattr(media,"file_size",0) or 0})
        cnt=len(sess["files"])
        await message.reply_text(f"📎 File {cnt}: <code>{fname}</code>\nAur bhejo ya ✅ Done.",
            reply_markup=InlineKeyboardMarkup([
                [_btn("✅ Done", f"zipfile|done|{uid}", "success"),
                 _btn("🔐 Password", f"zipfile|askpass|{uid}", "primary")],
                [_btn("❌ Cancel", f"zipfile|cancel|{uid}", "danger")]]))
        return
    # Merge session collect
    if uid in merge_sessions and message.video:
        sess=merge_sessions[uid]
        sess["files"].append({"file_id":message.video.file_id,"file_name":message.video.file_name or "video.mp4"})
        cnt=len(sess["files"])
        await message.reply_text(f"🎬 Video {cnt}: <code>{message.video.file_name or 'video'}</code>",
            reply_markup=InlineKeyboardMarkup([
                [_btn("✅ Merge Now", f"mergefile|done|{uid}", "success"),
                 _btn("❌ Cancel", f"mergefile|cancel|{uid}", "danger")]]))
        return
    # ── AUTO ZIP QUEUE: ZIPs add hoti rehti hai, ek saath process hoti hain ──
    if uid in ZIP_QUEUE_SESSIONS and media and is_archive_file(fname):
        sess = ZIP_QUEUE_SESSIONS[uid]
        MAX_QUEUE = 100
        if not sess.get("processing", False):
            n_now = len(sess["files"])
            if n_now >= MAX_QUEUE:
                await message.reply_text(
                    f"⚠️ Queue full hai! Maximum <b>{MAX_QUEUE} ZIPs</b> allowed.\n"
                    f"Pehle process karo ya /cancelqueue se clear karo."
                )
                return
            # ── Store message.id for correct sequence sorting ──
            sess["files"].append({
                "file_id":    media.file_id,
                "file_name":  fname,
                "size":       getattr(media, "file_size", 0) or 0,
                "password":   sess.get("default_password"),
                "msg_id":     message.id,   # ← sequence key
            })
            n = len(sess["files"])
            total_mb = sum(f["size"] for f in sess["files"]) / 1048576
            remaining = MAX_QUEUE - n
            await message.reply_text(
                f"✅ <b>ZIP #{n} added!</b>  (Slot {n}/{MAX_QUEUE})\n"
                f"📄 <code>{fname}</code>\n"
                f"💾 Total: {total_mb:.1f} MB  |  🆓 Remaining slots: {remaining}\n\n"
                f"Aur ZIPs bhejo ya process dabao ⬇️",
                reply_markup=InlineKeyboardMarkup([
                    [_btn(f"▶️ Process All {n} ZIPs", f"zq_start|{uid}", "success")],
                    [InlineKeyboardButton("📋 List", callback_data=f"zq_list|{uid}"),
                     _btn("🗑 Cancel", f"zq_cancel|{uid}", "danger")],
                ])
            )
            return
    # TXT link source
    if message.chat.type==enums.ChatType.PRIVATE and message.document and fname.lower().endswith(".txt"):
        temp_root=Path(Config.TEMP_DIR)/str(uid)/uuid.uuid4().hex
        temp_root.mkdir(parents=True,exist_ok=True)
        await register_temp_path(uid,str(temp_root),Config.AUTO_DELETE_DEFAULT_MIN)
        st=await message.reply_text("📥 TXT downloading…")
        try:
            p=await client.download_media(message.document,file_name=str(temp_root))
            content=Path(p).read_text(encoding="utf-8",errors="ignore")
        except Exception as e: await st.edit_text(f"❌ TXT download failed:\n<code>{e}</code>"); return
        try: await st.delete()
        except: pass
        await process_links_message(client,message,content); return
    if not media: return
    fsize_mb=(getattr(media,"file_size",0) or 0)/(1024*1024)
    try: await log_input(client,message,f"file: {fname}")
    except: pass
    await message.reply_text(
        f"📂 <code>{fname}</code>  (<b>{human_bytes(int(fsize_mb*1024*1024))}</b>)\nChoose action 👇",
        reply_markup=file_action_keyboard(message,fname,fsize_mb))

# ════════════════════════════════════════════════════════════════════════════
# TEXT / LINKS HANDLER
# ════════════════════════════════════════════════════════════════════════════
async def process_links_message(client, message, content):
    if not message.from_user: return
    links=find_links_in_text(content or "")
    if not links: await _safe_reply(message, "No valid URLs found."); return
    LINK_SESSIONS[(message.chat.id,message.id)]={"links":links,"content":content or ""}
    cats={}
    for u in links:
        k=classify_link(u); cats[k]=cats.get(k,0)+1
    em={"gdrive":"🗂","m3u8":"📺","direct":"💾","telegram":"✈️","unknown":"🔗"}
    lines=[f"{em.get(k,'🔗')} {k}: <b>{v}</b>" for k,v in cats.items()]
    await message.reply_text(
        f"🔗 <b>{len(links)} links found</b>\n\n"+"\n".join(lines)+"\n\nChoose action:",
        reply_markup=InlineKeyboardMarkup([
            [_btn("⬇️ Download All", f"links|download_all|{message.chat.id}|{message.id}", "success")],
            [_btn("🧹 Cleaned TXT", f"links|clean_txt|{message.chat.id}|{message.id}", "primary"),
             _btn("⏭ Skip", f"links|skip|{message.chat.id}|{message.id}", "primary")]]))


@app.on_message((filters.text|filters.caption) & _non_cmd )
async def on_text(client, message):
    if not message.from_user: return
    uid=message.from_user.id
    txt=(message.text or message.caption or "").strip()
    if txt.startswith("/"): return
    if await is_banned(uid): return
    if uid in pending_password:
        info=pending_password.pop(uid)
        await handle_unzip_from_password(client,message,info,txt); return
    if uid in pending_state:
        state=pending_state.pop(uid); action=state.get("action")
        if action=="settings_caption":
            if not txt: await message.reply_text("Caption empty nahi ho sakta."); return
            cfg=await get_user_settings(uid) or {}
            cfg["caption_base"]=txt; cfg["caption_counter"]=0
            await save_user_settings(uid,cfg)
            await message.reply_text(f"✅ Caption set! e.g. <code>001 {txt}</code>")
        elif action=="settings_replace":
            parts=[p.strip() for p in re.split(r"->|=>",txt,maxsplit=1)]
            if len(parts)!=2 or not parts[0]: await message.reply_text("Format: <code>old -> new</code>"); return
            cfg=await get_user_settings(uid) or {}
            cfg["replace_from"]=parts[0]; cfg["replace_to"]=parts[1]
            await save_user_settings(uid,cfg)
            await message.reply_text(f"✅ Replace: <code>{parts[0]}</code> → <code>{parts[1]}</code>")
        elif action=="rename":
            orig=await client.get_messages(state["chat_id"],state["msg_id"])
            await _do_rename(client,message,orig,txt)
        elif action=="screenshot":
            orig=await client.get_messages(state["chat_id"],state["msg_id"])
            await _handle_screenshot_with_time(client,message,orig,txt)
        elif action=="watermark":
            orig=await client.get_messages(state["chat_id"],state["msg_id"])
            await _handle_watermark(client,message,orig,txt)
        elif action=="zip_password":
            sess=zip_sessions.get(uid)
            if sess:
                sess["password"]=txt
                await message.reply_text(f"🔐 Password set: <code>{txt}</code>. ✅ Done dabao.",
                    reply_markup=InlineKeyboardMarkup([[
                        _btn("✅ Done", f"zipfile|done|{uid}", "success"),
                        _btn("❌ Cancel", f"zipfile|cancel|{uid}", "danger")]]))
        elif action=="pdf_split_range":
            tid=state.get("task_id"); pi=PDF_TASKS.get(tid)
            if pi:
                ranges=parse_page_ranges(txt,pi.get("total_pages",999))
                if not ranges: await message.reply_text("❌ Invalid range. Format: <code>1-5,7,10-15</code>")
                else: await _do_pdf_split_range(client,message,tid,ranges)
        return
    if not await check_force_sub(client,message): return
    await get_or_create_user(uid)
    await process_links_message(client,message,txt)


@app.on_message((filters.text|filters.caption) & _non_cmd & (filters.group|filters.channel))
async def group_text_handler(client, message):
    if not message.from_user: return
    txt=(message.text or message.caption or "") or ""
    if txt.lstrip().startswith("/"): return
    me=await client.get_me(); mentioned=False
    if me.username and ("@"+me.username.lower()) in txt.lower(): mentioned=True
    if (message.reply_to_message and message.reply_to_message.from_user and
            message.reply_to_message.from_user.id==me.id): mentioned=True
    if not mentioned: return
    uid=message.from_user.id
    if await is_banned(uid): return
    if not await check_force_sub(client,message): return
    await get_or_create_user(uid)
    await process_links_message(client,message,txt.strip())


async def _send_mystats(client, user, dest):
    uid   = user.id
    u     = await get_or_create_user(uid)
    stats = u.get("stats", {})
    cfg   = await get_user_settings(uid) or {}
    is_prem   = await is_premium_user(uid)
    prem_ts   = await get_premium_until(uid)
    ref_count = await get_referral_count(uid)
    prem_str  = "⭐ Active"
    if is_prem and prem_ts:
        exp = datetime.datetime.utcfromtimestamp(prem_ts).strftime("%d %b %Y")
        prem_str += f" (expires {exp})"
    elif not is_prem:
        prem_str = "❌ Free"
    text = (
        f"📊 <b>Your Stats</b>\n\n"
        f"👤 Name: <b>{user.first_name}</b>\n"
        f"🆔 ID: <code>{uid}</code>\n"
        f"⭐ Premium: {prem_str}\n\n"
        f"📦 Tasks today: <b>{stats.get('daily_tasks',0)}/{Config.FREE_DAILY_TASK_LIMIT}</b>\n"
        f"💾 Data today: <b>{stats.get('daily_size_mb',0):.1f} MB/{Config.FREE_DAILY_SIZE_MB} MB</b>\n"
        f"🏆 Total tasks: <b>{stats.get('total_tasks',0)}</b>\n"
        f"🎁 Referrals: <b>{ref_count}</b>\n\n"
        f"📝 Caption: <code>{cfg.get('caption_base') or 'None'}</code>\n"
        f"🖼 Thumbnail: <code>{cfg.get('thumb_mode','random')}</code>"
    )
    await dest.reply_text(text)


async def _send_settings(client, user, dest):
    uid = user.id
    cfg = await get_user_settings(uid) or {}
    text = (
        f"{random_emoji()} <b>Your Settings</b>\n\n"
        f"📝 Caption base: <code>{cfg.get('caption_base') or 'None'}</code>\n"
        f"🔤 Replace: <code>{cfg.get('replace_from') or 'None'}</code> → "
        f"<code>{cfg.get('replace_to') or 'None'}</code>\n"
        f"🖼 Thumbnail: <code>{cfg.get('thumb_mode','random')}</code>\n\n"
        "Use buttons below:"
    )
    await dest.reply_text(text, reply_markup=settings_keyboard())


# ════════════════════════════════════════════════════════════════════════════
# CALLBACKS
# ════════════════════════════════════════════════════════════════════════════
@app.on_callback_query()
async def callbacks(client, cq: CallbackQuery):
    data=cq.data or ""
    if data=="retry_force_sub": await cq.message.delete(); return
    if data in ("show_help","help:unzip","help:link"):
        await cq.answer()
        await help_cmd(client, cq.message); return
    if data=="show_mystats":
        await cq.answer()
        await _send_mystats(client, cq.from_user, cq.message); return
    if data=="open_settings":
        await cq.answer()
        await _send_settings(client, cq.from_user, cq.message); return
    if data=="premium_info":
        await cq.answer()
        await cq.message.reply_text(
            "⭐ <b>Premium</b>\n\n<b>Free:</b> 30 tasks/day, 4GB/day, 5min cooldown\n"
            f"<b>Premium:</b> Unlimited, no limits, 10s cooldown\n\n"
            f"Buy: @{Config.OWNER_USERNAME}\nEarn free: /refer"); return
    if data.startswith("settings:"):
        uid=cq.from_user.id; action=data.split(":",1)[1]
        if action=="reset":
            await save_user_settings(uid,{})
            try: await cq.message.edit_text("✅ Settings reset.",reply_markup=settings_keyboard())
            except: pass
            await cq.answer("Reset!"); return
        if action=="caption":
            pending_state[uid]={"action":"settings_caption"}
            await cq.message.reply_text("📝 Base caption bhejo:\nExample: <code>My Pack</code>")
            await cq.answer(); return
        if action=="replace":
            pending_state[uid]={"action":"settings_replace"}
            await cq.message.reply_text("🔤 Replace rule:\n<code>old -> new</code>")
            await cq.answer(); return
        if action.startswith("thumb:"):
            mode=action.split(":",1)[1]; cfg=await get_user_settings(uid) or {}
            cfg["thumb_mode"]=mode; await save_user_settings(uid,cfg)
            await cq.message.reply_text(f"✅ Thumb mode: <b>{mode}</b>"); await cq.answer(); return
    if data.startswith("unzip|"):
        try: _,cid,mid,mode=data.split("|",3); orig=await client.get_messages(int(cid),int(mid))
        except: await cq.answer("File nahi mila.",show_alert=True); return
        await handle_unzip_button(client,cq,orig,mode); return
    if data.startswith("ucancel|"):
        _,tid=data.split("|",1); tasks.pop(tid,None)
        try: await cq.message.edit_text("❌ Unzip cancelled.")
        except: pass
        await cq.answer(); return
    if data.startswith("audio|"):
        try: _,cid,mid=data.split("|",2); orig=await client.get_messages(int(cid),int(mid))
        except: await cq.answer("Video nahi mila.",show_alert=True); return
        await handle_extract_audio(client,cq,orig); return
    if data.startswith("sendall|"):
        _,tid=data.split("|",1); await handle_send_all(client,cq,tid); return
    if data.startswith("sendone|"):
        _,tid,idx=data.split("|",2); await handle_send_one(client,cq,tid,int(idx)); return
    if data.startswith("sendas|"):
        _,cid,mid=data.split("|",2)
        try: orig=await client.get_messages(int(cid),int(mid))
        except: await cq.answer("File nahi mila.",show_alert=True); return
        await cq.answer()
        try:
            if orig.document: await client.send_document(cq.message.chat.id,orig.document.file_id,reply_to_message_id=cq.message.id)
            elif orig.video: await client.send_video(cq.message.chat.id,orig.video.file_id,reply_to_message_id=cq.message.id)
        except Exception as e: await cq.message.reply_text(f"❌ <code>{e}</code>")
        return
    if data.startswith("rename|"):
        _,cid,mid=data.split("|",2)
        try: orig=await client.get_messages(int(cid),int(mid))
        except: await cq.answer("File nahi mila.",show_alert=True); return
        media=orig.document or orig.video or orig.audio
        fname=(media.file_name if media else None) or "file"
        pending_state[cq.from_user.id]={"action":"rename","chat_id":int(cid),"msg_id":int(mid),"fname":fname}
        await cq.message.reply_text(f"✏️ Naya naam bhejo (current: <code>{fname}</code>):")
        await cq.answer(); return
    if data.startswith("finfo|"):
        _,cid,mid=data.split("|",2)
        try: orig=await client.get_messages(int(cid),int(mid))
        except: await cq.answer("File nahi mila.",show_alert=True); return
        await cq.answer(); await _handle_file_info(client,cq.message,orig); return
    if data.startswith("compress|"):
        _,cid,mid=data.split("|",2)
        try: orig=await client.get_messages(int(cid),int(mid))
        except: await cq.answer("File nahi mila.",show_alert=True); return
        await cq.answer(); await _trigger_compress(client,cq.message,orig,int(cid),int(mid)); return
    if data.startswith("comprq|"):
        _,tid,res=data.split("|",2); await cq.answer(); await _do_compress(client,cq,tid,res); return
    if data.startswith("split|"):
        _,cid,mid=data.split("|",2)
        try: orig=await client.get_messages(int(cid),int(mid))
        except: await cq.answer("File nahi mila.",show_alert=True); return
        await cq.answer()
        media=orig.document or orig.video; fname=(media.file_name if media else None) or "file"
        await _trigger_split(client,cq.message,orig,int(cid),int(mid),fname); return
    if data.startswith("splitq|"):
        _,tid,smb=data.split("|",2); await cq.answer(); await _do_split(client,cq,tid,int(smb)); return
    if data.startswith("subs|"):
        _,cid,mid=data.split("|",2)
        try: orig=await client.get_messages(int(cid),int(mid))
        except: await cq.answer("File nahi mila.",show_alert=True); return
        await cq.answer(); await _trigger_subs(client,cq.message,orig); return
    if data.startswith("subsq|"):
        _,tid,sidx=data.split("|",2); await cq.answer()
        val=sidx if sidx=="all" else int(sidx)
        await _do_extract_sub(client,cq,tid,val); return
    if data.startswith("screenshot|"):
        _,cid,mid=data.split("|",2)
        pending_state[cq.from_user.id]={"action":"screenshot","chat_id":int(cid),"msg_id":int(mid)}
        await cq.message.reply_text("📸 Time bhejo: <code>MM:SS</code> ya <code>HH:MM:SS</code>")
        await cq.answer(); return
    if data.startswith("watermark|"):
        _,cid,mid=data.split("|",2)
        pending_state[cq.from_user.id]={"action":"watermark","chat_id":int(cid),"msg_id":int(mid)}
        await cq.message.reply_text("💧 Watermark text bhejo:"); await cq.answer(); return
    if data.startswith("pdf|"):
        _,cid,mid=data.split("|",2)
        try: orig=await client.get_messages(int(cid),int(mid))
        except: await cq.answer("File nahi mila.",show_alert=True); return
        await cq.answer()
        media=orig.document; fname=(media.file_name if media else None) or "file.pdf"
        await _trigger_pdf_tools(client,cq.message,orig,fname); return
    if data.startswith("pdfq|"):
        _,tid,action=data.split("|",2); await cq.answer(); await _do_pdf_action(client,cq,tid,action); return
    if data.startswith("zipfile|"):
        _,action,uid_str=data.split("|",2); uid=int(uid_str)
        if cq.from_user.id!=uid: await cq.answer("Ye tumhara session nahi.",show_alert=True); return
        await cq.answer()
        if action=="cancel":
            zip_sessions.pop(uid,None)
            try: await cq.message.edit_text("❌ ZIP session cancelled.")
            except: pass
        elif action=="askpass":
            pending_state[uid]={"action":"zip_password"}
            await cq.message.reply_text("🔐 Password bhejo (ya /cancel):")
        elif action=="done": await _do_create_zip(client,cq,uid)
        return
    if data.startswith("mergefile|"):
        _,action,uid_str=data.split("|",2); uid=int(uid_str)
        if cq.from_user.id!=uid: await cq.answer("Ye tumhara session nahi.",show_alert=True); return
        await cq.answer()
        if action=="cancel":
            merge_sessions.pop(uid,None)
            try: await cq.message.edit_text("❌ Merge cancelled.")
            except: pass
        elif action=="done": await _do_merge_videos(client,cq,uid)
        return
    if data.startswith("ytdlq|"):
        _,tid,idx=data.split("|",2); await cq.answer(); await _do_ytdl_download(client,cq,tid,int(idx)); return
    if data.startswith("ytdlcancel|"):
        _,tid=data.split("|",1); YTDL_TASKS.pop(tid,None)
        try: await cq.message.edit_text("❌ Download cancelled.")
        except: pass
        await cq.answer(); return
    if data.startswith("m3q|"):
        try: _,tid,idx=data.split("|",2)
        except: await cq.answer(); return
        await handle_m3u8_quality_choice(client,cq,tid,int(idx)); return
    if data.startswith("links|"):
        parts=data.split("|",3)
        if len(parts)<4: await cq.answer(); return
        _,action,cid,mid=parts
        try: orig=await client.get_messages(int(cid),int(mid))
        except: await cq.answer("Message nahi mila.",show_alert=True); return
        key=(orig.chat.id,orig.id); sess=LINK_SESSIONS.get(key)
        content=sess["content"] if sess else (orig.text or orig.caption or "")
        links=sess["links"] if sess else find_links_in_text(content)
        if action=="clean_txt":
            await cq.answer(); txt="\n".join(sorted(set(links))) or "No URLs."
            await cq.message.edit_text("🧹 <b>Cleaned URLs:</b>\n\n"+txt[:4000])
        elif action=="download_all": await cq.answer(); await handle_links_download_all(client,cq,orig)
        else:
            await cq.answer()
            try: await cq.message.edit_text("⏭ Skipped.")
            except: pass
        return
    if data.startswith("cloudopt|"):
        _,cid,mid=data.split("|",2); await cq.answer()
        await cq.message.reply_text("☁️ Cloud platform choose karo:",
            reply_markup=InlineKeyboardMarkup([
                [_btn("🌐 GoFile (No limit)", f"cloudgo|{cid}|{mid}", "primary"),
                 _btn("📦 Catbox (≤200MB)", f"cloudcat|{cid}|{mid}", "primary")],
                [_btn("❌ Cancel", "noop", "danger")]])); return
    if data.startswith("cloudgo|") or data.startswith("cloudcat|"):
        platform="gofile" if data.startswith("cloudgo|") else "catbox"
        _,cid,mid=data.split("|",2)
        try: orig=await client.get_messages(int(cid),int(mid))
        except: await cq.answer("File nahi mila.",show_alert=True); return
        await cq.answer(); await _do_cloud_upload(client,cq,orig,platform); return
    if data.startswith("admin:"):
        if not is_owner(cq.from_user.id): await cq.answer("Owner only.",show_alert=True); return
        action=data.split(":",1)[1]
        if action=="status":
            total,premium,banned=await count_users(); disk=shutil.disk_usage("/")
            await cq.message.reply_text(
                f"📊 Users: {total}  Premium: {premium}  Banned: {banned}\n"
                f"💾 Free: {human_bytes(disk.free)}")
        elif action=="broadcast":
            await cq.message.reply_text("📢 Kisi message ko reply karke /broadcast bhejo.")
        elif action=="clean":
            await cq.message.reply_text("🧹 Cleanup triggered (background worker chal raha hai).")
        await cq.answer(); return
    if data.startswith("zq_list|"):
        uid = int(data.split("|",1)[1])
        if cq.from_user.id != uid: await cq.answer("Ye tumhara queue nahi!", show_alert=True); return
        sess = ZIP_QUEUE_SESSIONS.get(uid)
        if not sess: await cq.answer("Queue nahi mili.", show_alert=True); return
        files = sess["files"]
        if not files: await cq.answer("Queue empty hai!", show_alert=True); return
        lines = [f"📦 <b>Queue ({len(files)} ZIPs):</b>"]
        for i, f in enumerate(files, 1):
            mb = f["size"]/1048576
            pw = "🔐" if f.get("password") or sess.get("default_password") else ""
            lines.append(f"  {i}. <code>{f['file_name']}</code> ({mb:.1f} MB) {pw}")
        total_mb = sum(f["size"] for f in files)/1048576
        lines.append(f"\n💾 <b>Total: {total_mb:.1f} MB</b>")
        try: await cq.message.reply_text("\n".join(lines))
        except: pass
        await cq.answer()
        return
    if data=="cmd_zipqueue":
        await cq.answer()
        ZIP_QUEUE_SESSIONS[cq.from_user.id] = {
            "files": [], "chat_id": cq.message.chat.id,
            "reply_to": cq.message.id, "cancelled": False, "processing": False,
        }
        try: await cq.message.edit_text(
            "📦 <b>ZIP Queue Mode ON!</b>\n\nZIP files bhejo — sab queue mein add ho jaayenge!\n"
            "Phir ▶️ Process dabao.",
            reply_markup=InlineKeyboardMarkup([
                [_btn("▶️ Process Queue", f"zq_start|{cq.from_user.id}", "success")],
                [_btn("🗑 Cancel Queue", f"zq_cancel|{cq.from_user.id}", "danger")],
            ]))
        except: pass
        return
    if data.startswith("zq_start|"):
        uid = int(data.split("|",1)[1])
        if cq.from_user.id != uid:
            await cq.answer("Ye tumhara queue nahi hai!", show_alert=True); return
        sess = ZIP_QUEUE_SESSIONS.get(uid)
        if not sess:
            await cq.answer("Queue expire ho gayi! /zipqueue se dobara shuru karo.", show_alert=True); return
        if not sess["files"]:
            await cq.answer("Queue empty hai! Pehle ZIPs bhejo.", show_alert=True); return
        if sess.get("processing"):
            await cq.answer("Queue already chal rahi hai!", show_alert=True); return
        sess["processing"] = True
        try: await cq.message.edit_text(
            f"▶️ <b>Queue Processing Shuru!</b> {len(sess['files'])} ZIP(s)…"
        )
        except: pass
        await cq.answer("Queue start ho gayi!")
        sess_thread = sess.get("thread_id")
        task = asyncio.create_task(
            _process_zip_queue(client, uid, cq.message.chat.id, cq.message.id, thread_id=sess_thread)
        )
        USER_TASKS[uid] = task
        return
    if data.startswith("zq_cancel|"):
        uid = int(data.split("|",1)[1])
        if cq.from_user.id != uid:
            await cq.answer("Ye tumhara queue nahi!", show_alert=True); return
        sess = ZIP_QUEUE_SESSIONS.get(uid)
        if sess: sess["cancelled"] = True
        task = USER_TASKS.pop(uid, None)
        if task and not task.done(): task.cancel()
        ZIP_QUEUE_SESSIONS.pop(uid, None)
        try: await cq.message.edit_text("🗑 <b>Queue Cancel ho gayi!</b>")
        except: pass
        await cq.answer("Queue cancel!")
        return
    if data=="noop": await cq.answer(); return
    await cq.answer()



# ════════════════════════════════════════════════════════════════════════════
    await message.reply_text("🛑 <b>Queue cancelled!</b> All pending ZIPs removed.")



# ════════════════════════════════════════════════════════════════════════════
# UNZIP
# ════════════════════════════════════════════════════════════════════════════
async def handle_unzip_button(client, cq, orig, mode):
    if not cq.from_user: await cq.answer(); return
    uid=cq.from_user.id
    if await is_banned(uid): await cq.answer("Banned.",show_alert=True); return
    if not await check_force_sub(client,orig): await cq.answer(); return
    doc=orig.document
    if not doc: await cq.answer("No document.",show_alert=True); return
    fname=doc.file_name or "archive"
    if not is_archive_file(fname): await cq.answer("Archive nahi hai.",show_alert=True); return
    if not await check_rate_limit(uid,cq.message): await cq.answer(); return
    if mode=="askpass":
        pending_password[uid]={"chat_id":orig.chat.id,"msg_id":orig.id,"file_name":fname}
        await cq.message.reply_text(f"🔐 Password bhejo for <code>{fname}</code>:")
        await cq.answer(); return
    if mode=="autopass":
        await cq.answer(); await _auto_try_passwords(client,cq.message,orig); return
    await cq.answer(); await run_unzip_task(client,orig,password=None,reply_msg=cq.message)

async def _auto_try_passwords(client, reply_msg, orig):
    if not orig.from_user: return
    uid=orig.from_user.id
    status=await reply_msg.reply_text("🔑 Auto-trying common passwords…")
    doc=orig.document
    if not doc: await status.edit_text("No document."); return
    temp_root=Path(Config.TEMP_DIR)/str(uid)/uuid.uuid4().hex
    temp_root.mkdir(parents=True,exist_ok=True)
    await register_temp_path(uid,str(temp_root),Config.AUTO_DELETE_DEFAULT_MIN)
    try: dl=await client.download_media(doc,file_name=str(temp_root))
    except Exception as e: await status.edit_text(f"❌ Download failed:\n<code>{e}</code>"); return
    found=None
    for pw in COMMON_PASSWORDS:
        try:
            td=str(temp_root/"test_pw")
            extract_archive(dl,td,password=pw)
            found=pw; shutil.rmtree(td,ignore_errors=True); break
        except: shutil.rmtree(str(temp_root/"test_pw"),ignore_errors=True)
    if found:
        await status.edit_text(f"✅ Password found: <code>{found}</code>\nExtracting…")
        await run_unzip_task(client,orig,password=found,reply_msg=reply_msg)
    else:
        await status.edit_text("❌ Common passwords mein se koi kaam nahi aaya.\n🔐 Manually enter karo.")

async def handle_unzip_from_password(client, msg, info, password):
    orig=await client.get_messages(info["chat_id"],info["msg_id"])
    await msg.reply_text("✅ Password mila, extracting…")
    await run_unzip_task(client,orig,password=password,reply_msg=msg)

async def run_unzip_task(client, msg, password, reply_msg=None):
    if not msg.from_user: return
    uid=msg.from_user.id; lock=get_lock(uid); dest=reply_msg or msg
    if lock.locked(): await dest.reply_text("⏳ Ek task chal raha hai."); return
    async with lock:
        user_cancelled[uid]=False
        doc=msg.document; fname=doc.file_name or "archive"
        size_mb=(doc.file_size or 0)/(1024*1024)
        await get_or_create_user(uid)
        temp_root=Path(Config.TEMP_DIR)/str(uid)/uuid.uuid4().hex
        temp_root.mkdir(parents=True,exist_ok=True)
        await register_temp_path(uid,str(temp_root),Config.AUTO_DELETE_DEFAULT_MIN)
        status=await dest.reply_text("📥 Downloading archive…"); start=time.time()
        try:
            dl=await client.download_media(doc,file_name=str(temp_root),progress=progress_for_pyrogram,
                progress_args=(status,start,fname,"to server"))
        except Exception as e: await status.edit_text(f"❌ Download failed:\n<code>{e}</code>"); return
        if not dl: await status.edit_text("❌ Download failed."); return
        if user_cancelled.get(uid): await status.edit_text("❌ Cancelled."); return
        if not password and detect_encrypted(dl):
            await status.edit_text("🔐 Archive encrypted hai!\nUse <b>With Password</b> ya <b>Auto-Try</b>."); return
        await status.edit_text("📦 Extracting…")
        extract_dir=temp_root/"extracted"
        try: result=extract_archive(dl,str(extract_dir),password=password)
        except Exception as e: await status.edit_text(f"❌ Extract error:\n<code>{e}</code>"); return
        if user_cancelled.get(uid): await status.edit_text("❌ Cancelled mid-way."); return
        stats=result["stats"]; files=sorted(result["files"],key=lambda p:p.lower())
        links_map=extract_links_from_folder(str(extract_dir))
        tid=uuid.uuid4().hex
        tasks[tid]={"type":"unzip","user_id":uid,"base_dir":str(extract_dir),"files":files,"archive_name":os.path.basename(dl)}
        summary=(f"✅ <b>Extraction Done!</b>\n\n📁 <code>{os.path.basename(dl)}</code>\n"
                 f"📊 Files: <b>{stats['total_files']}</b>  Folders: <b>{stats['folders']}</b>\n"
                 f"🎬 Videos: <b>{stats['videos']}</b>  📄 PDFs: <b>{stats['pdf']}</b>  "
                 f"📱 APKs: <b>{stats['apk']}</b>\n"
                 f"📝 TXT: <b>{stats['txt']}</b>  📺 M3U8: <b>{stats['m3u']}</b>  "
                 f"Others: <b>{stats['others']}</b>\n")
        tl=sum(len(v) for v in links_map.values())
        if tl: summary+=f"\n🔗 Links found: <b>{tl}</b> (Direct:{len(links_map.get('direct',[]))} M3U8:{len(links_map.get('m3u8',[]))} GDrive:{len(links_map.get('gdrive',[]))})\n"
        rows=[[_btn("❌ Cancel", f"ucancel|{tid}", "danger")],
              [_btn("🚀 Send ALL", f"sendall|{tid}", "success")]]
        for idx,rel in enumerate(files[:25]):
            short=rel if len(rel)<=42 else "…"+rel[-39:]
            rows.append([_btn(short, f"sendone|{tid}|{idx}", "success")])
        if tl:
            all_links=[l for v in links_map.values() for l in v]
            LINK_SESSIONS[(status.chat.id,status.id)]={"links":all_links,"content":"\n".join(all_links)}
            rows.append([_btn(f"⬇️ Download {tl} links", f"links|download_all|{status.chat.id}|{status.id}", "success")])
        await status.edit_text(summary,reply_markup=InlineKeyboardMarkup(rows))
        await update_user_stats(uid,size_mb)

async def handle_send_all(client, cq, tid):
    info=tasks.get(tid)
    if not info: await cq.answer("Task expired.",show_alert=True); return
    user=cq.from_user
    if user.id!=info["user_id"]: await cq.answer("Ye tumhara task nahi.",show_alert=True); return
    await cq.answer(); await cq.message.edit_text("📤 Sending all files…")
    base=Path(info["base_dir"]); files=info["files"]
    chat_id=cq.message.chat.id; reply_to=cq.message.id
    is_priv=cq.message.chat.type==enums.ChatType.PRIVATE; pinned=False
    if is_priv:
        try: await client.pin_chat_message(chat_id,reply_to); pinned=True
        except: pass
    for rel in files:
        if user_cancelled.get(user.id): break
        full=base/rel
        if not full.is_file(): continue
        try:
            if is_video_path(rel):
                name=Path(rel).name; cap=await build_caption(user.id,name)
                thumb=await choose_thumbnail(user.id,str(full))
                st=await client.send_message(chat_id,f"📤 {name}",reply_to_message_id=reply_to)
                start_u=time.time()
                dur=await _get_video_duration(str(full))
                sent=await client.send_video(chat_id,str(full),caption=cap,thumb=thumb,duration=dur,
                    progress=progress_for_pyrogram,progress_args=(st,start_u,name,"to Telegram"),
                    reply_to_message_id=reply_to,message_thread_id=thread_id)
                try: await st.delete()
                except: pass
            else:
                st=await client.send_message(chat_id,f"📤 {rel}",reply_to_message_id=reply_to,message_thread_id=thread_id)
                start_u=time.time()
                sent=await client.send_document(chat_id,str(full),caption=rel,
                    progress=progress_for_pyrogram,progress_args=(st,start_u,rel,"to Telegram"),
                    reply_to_message_id=reply_to,message_thread_id=thread_id)
                try: await st.delete()
                except: pass
            try: await log_output(client,user,sent,f"send_all {info.get('archive_name','?')}")
            except: pass
        except: pass
        await asyncio.sleep(0.4)
    if is_priv and pinned:
        try: await client.unpin_chat_message(chat_id,reply_to)
        except: pass
    await client.send_message(chat_id,"✅ All files sent!",reply_to_message_id=reply_to)

async def handle_send_one(client, cq, tid, index):
    info=tasks.get(tid)
    if not info: await cq.answer("Task expired.",show_alert=True); return
    user=cq.from_user
    if user.id!=info["user_id"]: await cq.answer("Ye tumhara task nahi.",show_alert=True); return
    files=info["files"]
    if index<0 or index>=len(files): await cq.answer("Invalid.",show_alert=True); return
    await cq.answer()
    base=Path(info["base_dir"]); rel=files[index]; full=base/rel
    if not full.is_file(): await cq.message.reply_text("File missing."); return
    chat_id=cq.message.chat.id; reply_to=cq.message.id
    try:
        if is_video_path(rel):
            name=Path(rel).name; cap=await build_caption(user.id,name)
            thumb=await choose_thumbnail(user.id,str(full))
            st=await client.send_message(chat_id,f"📤 {name}",reply_to_message_id=reply_to)
            start_u=time.time()
            dur=await _get_video_duration(str(full))
            sent=await client.send_video(chat_id,str(full),caption=cap,thumb=thumb,duration=dur,
                progress=progress_for_pyrogram,progress_args=(st,start_u,name,"to Telegram"),reply_to_message_id=reply_to)
            try: await st.delete()
            except: pass
        else:
            st=await client.send_message(chat_id,f"📤 {rel}",reply_to_message_id=reply_to)
            start_u=time.time()
            sent=await client.send_document(chat_id,str(full),caption=rel,
                progress=progress_for_pyrogram,progress_args=(st,start_u,rel,"to Telegram"),reply_to_message_id=reply_to)
            try: await st.delete()
            except: pass
        try: await log_output(client,user,sent,f"send_one {info.get('archive_name','?')}")
        except: pass
    except: pass

# ════════════════════════════════════════════════════════════════════════════
# AUDIO EXTRACT
# ════════════════════════════════════════════════════════════════════════════
async def handle_extract_audio(client, cq, msg, reply_to_msg=None):
    user=(cq.from_user if cq else None) or msg.from_user
    if not user: return
    uid=user.id
    if await is_banned(uid):
        if cq: await cq.answer("Banned.",show_alert=True); return
    video=msg.video
    if not video:
        if cq: await cq.answer("Video nahi hai.",show_alert=True); return
    lock=get_lock(uid)
    if lock.locked():
        if cq: await cq.answer("Ek task chal raha hai.",show_alert=True); return
    if cq: await cq.answer()
    dest_msg=(cq.message if cq else None) or reply_to_msg or msg
    if not await check_rate_limit(uid,dest_msg): return
    async with lock:
        fname=video.file_name or "video.mp4"; base=os.path.splitext(fname)[0]
        temp_root=Path(Config.TEMP_DIR)/str(uid)/uuid.uuid4().hex
        temp_root.mkdir(parents=True,exist_ok=True)
        await register_temp_path(uid,str(temp_root),Config.AUTO_DELETE_DEFAULT_MIN)
        status=await dest_msg.reply_text("📥 Downloading video for audio extract…"); start=time.time()
        try:
            dl=await client.download_media(video,file_name=str(temp_root),
                progress=progress_for_pyrogram,progress_args=(status,start,fname,"to server"))
        except Exception as e: await status.edit_text(f"❌ Download failed:\n<code>{e}</code>"); return
        audio_path=str(temp_root/f"{base}.m4a")
        try: await extract_audio(dl,audio_path)
        except Exception as e: await status.edit_text(f"❌ ffmpeg error:\n<code>{e}</code>"); return
        await status.edit_text("📤 Uploading audio…"); start_u=time.time()
        try:
            # BUG FIX #4 — send_audio not send_document
            sent=await client.send_audio(dest_msg.chat.id,audio_path,
                caption=f"🎵 Extracted from: <b>{fname}</b>",title=base,performer="Serena Bot",
                progress=progress_for_pyrogram,progress_args=(status,start_u,f"{base}.m4a","to Telegram"),
                reply_to_message_id=dest_msg.id)
            try: await status.delete()
            except: pass
            try: await log_output(client,user,sent,"audio extracted")
            except: pass
        except Exception as e: await status.edit_text(f"❌ Upload failed:\n<code>{e}</code>")

# ════════════════════════════════════════════════════════════════════════════
# NEW FEATURES
# ════════════════════════════════════════════════════════════════════════════

# ── File Info ────────────────────────────────────────────────────────────────
async def _handle_file_info(client, dest, orig):
    media=orig.document or orig.video or orig.audio
    if not media: await dest.reply_text("Koi media nahi mili."); return
    uid=dest.from_user.id if dest.from_user else 0
    fname=media.file_name or "file"; size=getattr(media,"file_size",0) or 0
    status=await dest.reply_text("ℹ️ Fetching info…")
    temp_root=Path(Config.TEMP_DIR)/str(uid)/uuid.uuid4().hex
    temp_root.mkdir(parents=True,exist_ok=True)
    await register_temp_path(uid,str(temp_root),Config.AUTO_DELETE_DEFAULT_MIN)
    try: dl=await client.download_media(media,file_name=str(temp_root))
    except Exception as e: await status.edit_text(f"❌ Download failed:\n<code>{e}</code>"); return
    info=await get_media_info(dl)
    if not info:
        await status.edit_text(f"ℹ️ <b>File Info</b>\n\n📄 <code>{fname}</code>\n💾 {human_bytes(size)}"); return
    txt=f"ℹ️ <b>File Info</b>\n\n📄 <code>{fname}</code>\n💾 {human_bytes(info.get('size_bytes',size))}\n"
    if info.get("duration"): txt+=f"⏱ {_fmt_dur(info['duration'])}\n"
    if info.get("resolution"): txt+=f"🖥 {info['resolution']}\n"
    if info.get("video_codec"):
        txt+=f"🎬 {info['video_codec']}"
        if info.get("fps"): txt+=f" @ {info['fps']:.1f}fps"
        txt+="\n"
    if info.get("audio_codec"): txt+=f"🔊 {info['audio_codec']} {info.get('audio_channels','')}ch {info.get('audio_lang','')}\n"
    if info.get("subtitle_count"): txt+=f"🔤 {info['subtitle_count']} subtitle tracks\n"
    await status.edit_text(txt)

# ── Compress ─────────────────────────────────────────────────────────────────
async def _trigger_compress(client, dest, orig, cid, mid):
    media=orig.video or orig.document
    if not media or not is_video_file(media.file_name or ""): await dest.reply_text("Ye video nahi hai."); return
    uid=dest.from_user.id if dest.from_user else 0
    tid=uuid.uuid4().hex
    temp_root=Path(Config.TEMP_DIR)/str(uid)/tid; temp_root.mkdir(parents=True,exist_ok=True)
    await register_temp_path(uid,str(temp_root),Config.AUTO_DELETE_DEFAULT_MIN)
    COMPRESS_TASKS[tid]={"user_id":uid,"chat_id":cid,"msg_id":mid,"temp_root":str(temp_root),"fname":media.file_name or "video.mp4"}
    await dest.reply_text(f"📦 Compress: <code>{media.file_name or 'video'}</code>\nResolution:",
        reply_markup=InlineKeyboardMarkup([
            [_btn("📱 360p", f"comprq|{tid}|360", "primary"),_btn("📺 480p", f"comprq|{tid}|480", "primary")],
            [_btn("💻 720p", f"comprq|{tid}|720", "primary"),_btn("🖥 1080p", f"comprq|{tid}|1080", "primary")],
            [_btn("❌ Cancel", "noop", "danger")]]))

async def _do_compress(client, cq, tid, res):
    info=COMPRESS_TASKS.get(tid)
    if not info: await cq.message.reply_text("Task expired."); return
    uid=cq.from_user.id
    if uid!=info["user_id"]: return
    if not await check_rate_limit(uid,cq.message): return
    orig=await client.get_messages(info["chat_id"],info["msg_id"])
    media=orig.video or orig.document
    if not media: return
    lock=get_lock(uid)
    if lock.locked(): await cq.message.reply_text("Ek task chal raha hai."); return
    async with lock:
        temp_root=Path(info["temp_root"])
        status=await cq.message.reply_text(f"📥 Downloading for {res}p compression…"); start=time.time()
        try:
            dl=await client.download_media(media,file_name=str(temp_root),
                progress=progress_for_pyrogram,progress_args=(status,start,info["fname"],"to server"))
        except Exception as e: await status.edit_text(f"❌ Download failed:\n<code>{e}</code>"); return
        out=str(temp_root/f"compressed_{res}p.mp4")
        await status.edit_text(f"⚙️ Compressing to {res}p…")
        try: await compress_video(dl,out,resolution=res)
        except Exception as e: await status.edit_text(f"❌ Compression failed:\n<code>{e}</code>"); return
        thumb=await choose_thumbnail(uid,out); cap=await build_caption(uid,f"{Path(info['fname']).stem}_{res}p.mp4")
        dur=await _get_video_duration(out)
        await status.edit_text("📤 Uploading…"); start_u=time.time()
        try:
            sent=await client.send_video(cq.message.chat.id,out,caption=cap,thumb=thumb,duration=dur,
                progress=progress_for_pyrogram,progress_args=(status,start_u,f"compressed_{res}p.mp4","to Telegram"),
                reply_to_message_id=cq.message.id)
            try: await status.delete()
            except: pass
            await log_output(client,cq.from_user,sent,f"compressed {res}p")
        except Exception as e: await status.edit_text(f"❌ Upload failed:\n<code>{e}</code>")
        await update_user_stats(uid,os.path.getsize(dl)/(1024*1024))
    COMPRESS_TASKS.pop(tid,None)

# ── Split ────────────────────────────────────────────────────────────────────
async def _trigger_split(client, dest, orig, cid, mid, fname):
    uid=dest.from_user.id if dest.from_user else 0
    tid=uuid.uuid4().hex
    temp_root=Path(Config.TEMP_DIR)/str(uid)/tid; temp_root.mkdir(parents=True,exist_ok=True)
    await register_temp_path(uid,str(temp_root),Config.AUTO_DELETE_DEFAULT_MIN)
    SPLIT_TASKS[tid]={"user_id":uid,"chat_id":cid,"msg_id":mid,"temp_root":str(temp_root),"fname":fname}
    await dest.reply_text(f"✂️ Split: <code>{fname}</code>\nPart size:",
        reply_markup=InlineKeyboardMarkup([
            [_btn("📦 500 MB", f"splitq|{tid}|500", "primary"),_btn("📦 1 GB", f"splitq|{tid}|1024", "primary")],
            [_btn("📦 1.5 GB", f"splitq|{tid}|1536", "primary"),_btn("📦 1.9 GB", f"splitq|{tid}|1900", "primary")],
            [_btn("❌ Cancel", "noop", "danger")]]))

async def _do_split(client, cq, tid, size_mb):
    info=SPLIT_TASKS.get(tid)
    if not info: await cq.message.reply_text("Task expired."); return
    uid=cq.from_user.id
    if uid!=info["user_id"]: return
    if not await check_rate_limit(uid,cq.message): return
    orig=await client.get_messages(info["chat_id"],info["msg_id"])
    media=orig.document or orig.video
    if not media: return
    lock=get_lock(uid)
    if lock.locked(): await cq.message.reply_text("Ek task chal raha hai."); return
    async with lock:
        temp_root=Path(info["temp_root"])
        status=await cq.message.reply_text("📥 Downloading to split…"); start=time.time()
        try:
            dl=await client.download_media(media,file_name=str(temp_root),
                progress=progress_for_pyrogram,progress_args=(status,start,info["fname"],"to server"))
        except Exception as e: await status.edit_text(f"❌ Download failed:\n<code>{e}</code>"); return
        await status.edit_text(f"✂️ Splitting into {size_mb}MB parts…")
        try: parts=await split_file(dl,part_size_mb=size_mb)
        except Exception as e: await status.edit_text(f"❌ Split failed:\n<code>{e}</code>"); return
        await status.edit_text(f"📤 Sending {len(parts)} parts…")
        for i,pp in enumerate(parts,1):
            pname=os.path.basename(pp)
            st=await client.send_message(cq.message.chat.id,f"📤 Part {i}/{len(parts)}",reply_to_message_id=cq.message.id)
            start_u=time.time()
            try:
                await client.send_document(cq.message.chat.id,pp,caption=pname,
                    progress=progress_for_pyrogram,progress_args=(st,start_u,pname,"to Telegram"),reply_to_message_id=cq.message.id)
                try: await st.delete()
                except: pass
            except Exception as e: await st.edit_text(f"❌ Part {i} failed: <code>{e}</code>")
        try: await status.delete()
        except: pass
        await cq.message.reply_text(f"✅ Split done! {len(parts)} parts sent.")
        await update_user_stats(uid,os.path.getsize(dl)/(1024*1024))
    SPLIT_TASKS.pop(tid,None)

# ── Subtitles ─────────────────────────────────────────────────────────────────
async def _trigger_subs(client, dest, orig):
    media=orig.video or orig.document
    if not media: await dest.reply_text("Koi media nahi mili."); return
    uid=dest.from_user.id if dest.from_user else 0
    fname=media.file_name or "video"; tid=uuid.uuid4().hex
    temp_root=Path(Config.TEMP_DIR)/str(uid)/tid; temp_root.mkdir(parents=True,exist_ok=True)
    await register_temp_path(uid,str(temp_root),Config.AUTO_DELETE_DEFAULT_MIN)
    status=await dest.reply_text("📥 Downloading for subs…"); start=time.time()
    try:
        dl=await client.download_media(media,file_name=str(temp_root),
            progress=progress_for_pyrogram,progress_args=(status,start,fname,"to server"))
    except Exception as e: await status.edit_text(f"❌ Download failed:\n<code>{e}</code>"); return
    await status.edit_text("🔍 Finding subtitle tracks…")
    subs=await extract_subtitles(dl,str(temp_root/"subs"))
    if not subs: await status.edit_text("❌ Koi subtitle track nahi mila."); return
    SUB_TASKS[tid]={"user_id":uid,"subs":subs,"chat_id":dest.chat.id,"reply_to":status.id}
    buttons=[[_btn(s["label"], f"subsq|{tid}|{s['stream_index']}", "primary")] for s in subs]
    buttons.append([_btn("📥 Download ALL", f"subsq|{tid}|all", "success")])
    await status.edit_text(f"🔤 <b>{len(subs)} subtitle tracks found!</b>",reply_markup=InlineKeyboardMarkup(buttons))

async def _do_extract_sub(client, cq, tid, sidx):
    info=SUB_TASKS.get(tid)
    if not info: await cq.message.reply_text("Task expired."); return
    subs=info["subs"]; chat_id=cq.message.chat.id; reply_to=cq.message.id
    targets=subs if sidx=="all" else [s for s in subs if s["stream_index"]==sidx]
    for sub in targets:
        try: await client.send_document(chat_id,sub["output_path"],caption=f"🔤 {sub['label']}",reply_to_message_id=reply_to)
        except Exception as e: await cq.message.reply_text(f"❌ {sub['label']}: <code>{e}</code>")
    txt=f"✅ All {len(subs)} subs sent!" if sidx=="all" else f"✅ {targets[0]['label'] if targets else 'N/A'} sent!"
    try: await cq.message.edit_text(txt)
    except: pass
    SUB_TASKS.pop(tid,None)

# ── Screenshot ───────────────────────────────────────────────────────────────
async def _handle_screenshot_with_time(client, dest, orig, time_str):
    media=orig.video or orig.document
    if not media: await dest.reply_text("Koi video nahi mili."); return
    uid=dest.from_user.id if dest.from_user else 0
    fname=media.file_name or "video.mp4"
    temp_root=Path(Config.TEMP_DIR)/str(uid)/uuid.uuid4().hex; temp_root.mkdir(parents=True,exist_ok=True)
    await register_temp_path(uid,str(temp_root),Config.AUTO_DELETE_DEFAULT_MIN)
    status=await dest.reply_text(f"📥 Downloading for screenshot at {time_str}…"); start=time.time()
    try:
        dl=await client.download_media(media,file_name=str(temp_root),
            progress=progress_for_pyrogram,progress_args=(status,start,fname,"to server"))
    except Exception as e: await status.edit_text(f"❌ Download failed:\n<code>{e}</code>"); return
    ss=str(temp_root/"screenshot.jpg")
    try: await take_screenshot(dl,ss,time_str)
    except Exception as e: await status.edit_text(f"❌ Screenshot failed:\n<code>{e}</code>"); return
    try:
        await client.send_photo(dest.chat.id,ss,caption=f"📸 at <code>{time_str}</code> from <b>{fname}</b>",reply_to_message_id=dest.id)
        try: await status.delete()
        except: pass
    except Exception as e: await status.edit_text(f"❌ Send failed:\n<code>{e}</code>")

# ── Watermark ─────────────────────────────────────────────────────────────────
async def _handle_watermark(client, dest, orig, wtext):
    media=orig.video or orig.document
    if not media or not is_video_file(media.file_name or ""): await dest.reply_text("Ye video nahi hai."); return
    uid=dest.from_user.id if dest.from_user else 0
    fname=media.file_name or "video.mp4"
    if not await check_rate_limit(uid,dest): return
    temp_root=Path(Config.TEMP_DIR)/str(uid)/uuid.uuid4().hex; temp_root.mkdir(parents=True,exist_ok=True)
    await register_temp_path(uid,str(temp_root),Config.AUTO_DELETE_DEFAULT_MIN)
    status=await dest.reply_text("📥 Downloading for watermark…"); start=time.time()
    try:
        dl=await client.download_media(media,file_name=str(temp_root),
            progress=progress_for_pyrogram,progress_args=(status,start,fname,"to server"))
    except Exception as e: await status.edit_text(f"❌ Download failed:\n<code>{e}</code>"); return
    out=str(temp_root/f"wm_{Path(fname).stem}.mp4")
    await status.edit_text("💧 Adding watermark…")
    try: await add_watermark(dl,out,wtext)
    except Exception as e: await status.edit_text(f"❌ Watermark failed:\n<code>{e}</code>"); return
    thumb=await choose_thumbnail(uid,out); cap=await build_caption(uid,f"💧 {fname}")
    dur=await _get_video_duration(out)
    await status.edit_text("📤 Uploading…"); start_u=time.time()
    try:
        sent=await client.send_video(dest.chat.id,out,caption=cap,thumb=thumb,duration=dur,
            progress=progress_for_pyrogram,progress_args=(status,start_u,fname,"to Telegram"),reply_to_message_id=dest.id)
        try: await status.delete()
        except: pass
        await log_output(client,dest.from_user,sent,"watermark added")
    except Exception as e: await status.edit_text(f"❌ Upload failed:\n<code>{e}</code>")

# ── Rename ────────────────────────────────────────────────────────────────────
async def _do_rename(client, dest, orig, new_name):
    if not new_name: await dest.reply_text("Name empty nahi ho sakta."); return
    uid=dest.from_user.id if dest.from_user else 0
    media=orig.document or orig.video or orig.audio
    if not media: await dest.reply_text("Koi file nahi mili."); return
    fname=media.file_name or "file"
    if not Path(new_name).suffix: new_name+=Path(fname).suffix
    temp_root=Path(Config.TEMP_DIR)/str(uid)/uuid.uuid4().hex; temp_root.mkdir(parents=True,exist_ok=True)
    await register_temp_path(uid,str(temp_root),Config.AUTO_DELETE_DEFAULT_MIN)
    status=await dest.reply_text("📥 Downloading to rename…"); start=time.time()
    try:
        dl=await client.download_media(media,file_name=str(temp_root),
            progress=progress_for_pyrogram,progress_args=(status,start,fname,"to server"))
    except Exception as e: await status.edit_text(f"❌ Download failed:\n<code>{e}</code>"); return
    new_path=str(temp_root/new_name)
    try: os.rename(dl,new_path)
    except Exception as e: await status.edit_text(f"❌ Rename failed:\n<code>{e}</code>"); return
    await status.edit_text(f"📤 Uploading as <code>{new_name}</code>…"); start_u=time.time()
    try:
        if is_video_path(new_name):
            thumb=await choose_thumbnail(uid,new_path); cap=await build_caption(uid,new_name)
            dur=await _get_video_duration(new_path)
            sent=await client.send_video(dest.chat.id,new_path,caption=cap,thumb=thumb,duration=dur,
                progress=progress_for_pyrogram,progress_args=(status,start_u,new_name,"to Telegram"),reply_to_message_id=dest.id)
        else:
            sent=await client.send_document(dest.chat.id,new_path,caption=new_name,
                progress=progress_for_pyrogram,progress_args=(status,start_u,new_name,"to Telegram"),reply_to_message_id=dest.id)
        try: await status.delete()
        except: pass
        await log_output(client,dest.from_user,sent,f"renamed {fname}→{new_name}")
    except Exception as e: await status.edit_text(f"❌ Upload failed:\n<code>{e}</code>")

# ── PDF Tools ────────────────────────────────────────────────────────────────
async def _trigger_pdf_tools(client, dest, orig, fname):
    if not is_pdf_file(fname): await dest.reply_text("Ye PDF nahi hai."); return
    uid=dest.from_user.id if dest.from_user else 0
    media=orig.document
    if not media: await dest.reply_text("Document nahi mila."); return
    tid=uuid.uuid4().hex; temp_root=Path(Config.TEMP_DIR)/str(uid)/tid
    temp_root.mkdir(parents=True,exist_ok=True)
    await register_temp_path(uid,str(temp_root),Config.AUTO_DELETE_DEFAULT_MIN)
    status=await dest.reply_text("📥 Downloading PDF…"); start=time.time()
    try:
        dl=await client.download_media(media,file_name=str(temp_root),
            progress=progress_for_pyrogram,progress_args=(status,start,fname,"to server"))
    except Exception as e: await status.edit_text(f"❌ Download failed:\n<code>{e}</code>"); return
    try: pi=get_pdf_info(dl)
    except: pi={"pages":"?","size_mb":0}
    PDF_TASKS[tid]={"user_id":uid,"pdf_path":dl,"temp_root":str(temp_root),"fname":fname,"total_pages":pi.get("pages",0),"chat_id":dest.chat.id,"reply_to":status.id}
    await status.edit_text(
        f"📄 <b>{fname}</b>\nPages: <b>{pi['pages']}</b>  Size: <b>{pi['size_mb']} MB</b>\n\nWhat to do?",
        reply_markup=InlineKeyboardMarkup([
            [_btn("✂️ Split by Pages", f"pdfq|{tid}|split", "primary"),
             _btn("📝 Extract Text", f"pdfq|{tid}|text", "success")],
            [_btn("❌ Cancel", "noop", "danger")]]))

async def _do_pdf_action(client, cq, tid, action):
    info=PDF_TASKS.get(tid)
    if not info: await cq.message.reply_text("Task expired."); return
    uid=cq.from_user.id
    if uid!=info["user_id"]: return
    dl=info["pdf_path"]; temp_root=Path(info["temp_root"]); fname=info["fname"]; base=Path(fname).stem
    if action=="text":
        out=str(temp_root/f"{base}_text.txt")
        try:
            extract_text_from_pdf(dl,out)
            await client.send_document(cq.message.chat.id,out,caption=f"📝 Text from: <b>{fname}</b>",reply_to_message_id=cq.message.id)
            try: await cq.message.edit_text("✅ Text extracted!")
            except: pass
        except Exception as e: await cq.message.reply_text(f"❌ <code>{e}</code>")
    elif action=="split":
        total=info.get("total_pages",0)
        pending_state[uid]={"action":"pdf_split_range","task_id":tid}
        await cq.message.reply_text(
            f"✂️ Page range bhejo (total: <b>{total}</b> pages)\n\n"
            "Format: <code>1-5</code> ya <code>1-5,7,10-15</code>")
    PDF_TASKS.pop(tid,None)

async def _do_pdf_split_range(client, dest, tid, ranges):
    info=PDF_TASKS.get(tid)
    if not info: return
    dl=info["pdf_path"]; temp_root=Path(info["temp_root"]); fname=info["fname"]
    status=await dest.reply_text("✂️ Splitting PDF…")
    try:
        parts=split_pdf_by_range(dl,str(temp_root/"split"),ranges)
        for p in parts:
            await client.send_document(dest.chat.id,p,caption=os.path.basename(p),reply_to_message_id=dest.id)
        try: await status.edit_text(f"✅ {len(parts)} PDF parts sent!")
        except: pass
    except Exception as e: await status.edit_text(f"❌ PDF split failed:\n<code>{e}</code>")
    PDF_TASKS.pop(tid,None)

# ── ZIP Creator ───────────────────────────────────────────────────────────────
async def _do_create_zip(client, cq, uid):
    sess=zip_sessions.get(uid)
    if not sess: await cq.message.reply_text("Session expired. /zip se dobara shuru karo."); return
    if not sess["files"]: await cq.message.reply_text("Koi file nahi hai! Pehle files bhejo."); return
    status=await cq.message.reply_text(f"📥 Downloading {len(sess['files'])} files…")
    temp_root=Path(sess["temp_root"]); dl_paths=[]
    for i,f in enumerate(sess["files"],1):
        try:
            await status.edit_text(f"📥 Downloading file {i}/{len(sess['files'])}: <code>{f['file_name']}</code>")
            p=await client.download_file(f["file_id"],file_name=str(temp_root/f["file_name"]))
            if p: dl_paths.append(str(temp_root/f["file_name"]))
        except:
            # fallback
            try:
                import tempfile
                tmp=str(temp_root/f["file_name"])
                await client.download_media(f["file_id"],file_name=str(temp_root))
                dl_paths.append(tmp)
            except: pass
    if not dl_paths: await status.edit_text("❌ Files download nahi ho payi."); zip_sessions.pop(uid,None); return
    await status.edit_text("🗜 Creating archive…")
    try:
        arc=create_archive(dl_paths,str(temp_root),"serena_archive",password=sess.get("password"))
    except Exception as e: await status.edit_text(f"❌ Archive creation failed:\n<code>{e}</code>"); zip_sessions.pop(uid,None); return
    arc_name=os.path.basename(arc)
    await status.edit_text("📤 Uploading archive…"); start_u=time.time()
    try:
        sent=await client.send_document(cq.message.chat.id,arc,caption=f"🗜 <code>{arc_name}</code>",
            progress=progress_for_pyrogram,progress_args=(status,start_u,arc_name,"to Telegram"),reply_to_message_id=cq.message.id)
        try: await status.delete()
        except: pass
        if sess.get("password"): await cq.message.reply_text(f"🔐 Archive password: <code>{sess['password']}</code>")
        await log_output(client,cq.from_user,sent,"zip created")
    except Exception as e: await status.edit_text(f"❌ Upload failed:\n<code>{e}</code>")
    zip_sessions.pop(uid,None)

# ── Merge Videos ──────────────────────────────────────────────────────────────
async def _do_merge_videos(client, cq, uid):
    sess=merge_sessions.get(uid)
    if not sess: await cq.message.reply_text("Session expired."); return
    if len(sess["files"])<2: await cq.message.reply_text("Kam se kam 2 videos chahiye."); return
    if not await check_rate_limit(uid,cq.message): return
    status=await cq.message.reply_text(f"📥 Downloading {len(sess['files'])} videos…")
    temp_root=Path(sess["temp_root"]); paths=[]
    for i,f in enumerate(sess["files"],1):
        try:
            await status.edit_text(f"📥 Video {i}/{len(sess['files'])}: <code>{f['file_name']}</code>")
            dest_path=str(temp_root/f["file_name"])
            await client.download_media(f["file_id"],file_name=str(temp_root))
            paths.append(dest_path)
        except: pass
    if len(paths)<2: await status.edit_text("❌ Videos download nahi ho payi."); merge_sessions.pop(uid,None); return
    out=str(temp_root/"merged_output.mp4")
    await status.edit_text("🔗 Merging videos…")
    try: await merge_videos(paths,out)
    except Exception as e: await status.edit_text(f"❌ Merge failed:\n<code>{e}</code>"); merge_sessions.pop(uid,None); return
    thumb=await choose_thumbnail(uid,out); cap=await build_caption(uid,"merged_output.mp4")
    dur=await _get_video_duration(out)
    await status.edit_text("📤 Uploading merged video…"); start_u=time.time()
    try:
        sent=await client.send_video(cq.message.chat.id,out,caption=cap,thumb=thumb,duration=dur,
            progress=progress_for_pyrogram,progress_args=(status,start_u,"merged_output.mp4","to Telegram"),reply_to_message_id=cq.message.id)
        try: await status.delete()
        except: pass
        await log_output(client,cq.from_user,sent,"videos merged")
    except Exception as e: await status.edit_text(f"❌ Upload failed:\n<code>{e}</code>")
    merge_sessions.pop(uid,None)
    await update_user_stats(uid,os.path.getsize(out)/(1024*1024))

# ── yt-dlp download ───────────────────────────────────────────────────────────
async def _do_ytdl_download(client, cq, tid, idx):
    info=YTDL_TASKS.get(tid)
    if not info: await cq.message.reply_text("Task expired."); return
    uid=cq.from_user.id
    if uid!=info["user_id"]: return
    if not await check_rate_limit(uid,cq.message): return
    formats=info["formats"]
    if idx<0 or idx>=len(formats): await cq.message.reply_text("Invalid choice."); return
    fmt=formats[idx]; url=info["url"]; temp_root=Path(info["temp_root"])
    try: await cq.message.edit_text(f"⬇️ Downloading <b>{fmt['label']}</b>…\n<code>{url}</code>")
    except: pass
    # ── Instagram handling ──
    # "insta_photo" format_id = photo/carousel post → use _download_instagram_photos
    # Normal format_ids → use download_video (with audio-safe format strings)
    try:
        if _is_instagram_url(url) and fmt.get("format_id") == "insta_photo":
            # Direct photo/carousel download — no quality selection needed
            try: await cq.message.edit_text("📸 Downloading Instagram post…")
            except: pass
            photo_files = await _download_instagram_photos(url, str(temp_root))
            if photo_files:
                cap = await build_caption(uid, "📸 Instagram")
                for i, fpath in enumerate(photo_files):
                    ext_i = Path(fpath).suffix.lower()
                    fname_i = Path(fpath).name
                    try:
                        if ext_i in IMAGE_EXT_SET:
                            await client.send_photo(info["chat_id"], fpath,
                                caption=cap if i==0 else fname_i,
                                reply_to_message_id=info["reply_to"])
                        elif is_video_path(fname_i):
                            dur_i = await _get_video_duration(fpath)
                            thumb_i = await choose_thumbnail(uid, fpath)
                            await client.send_video(info["chat_id"], fpath,
                                caption=cap if i==0 else fname_i,
                                thumb=thumb_i, duration=dur_i,
                                reply_to_message_id=info["reply_to"])
                    except Exception as ei:
                        await client.send_message(info["chat_id"],
                            f"⚠️ File {i+1}: <code>{str(ei)[:150]}</code>",
                            reply_to_message_id=info["reply_to"])
                    await asyncio.sleep(0.3)
                try: await cq.message.delete()
                except: pass
                await update_user_stats(uid, sum(Path(f).stat().st_size for f in photo_files if Path(f).exists())/1048576)
            YTDL_TASKS.pop(tid,None); return
        dl_path = await ytdl_download(url, str(temp_root), format_id=fmt["format_id"], height=fmt.get("height",0))
    except Exception as e:
        err_str = str(e)
        # Instagram photo post? Redirect to photo downloader
        if _is_instagram_url(url) and any(k in err_str.lower() for k in
                ("no video", "there is no video", "no video in this post",
                 "no video formats")):
            try: await cq.message.edit_text("📸 Photo post detected — switching to image downloader…")
            except: pass
            try:
                photo_files = await _download_instagram_photos(url, str(temp_root))
                if photo_files:
                    cap = await build_caption(uid, "📸 Instagram")
                    for i, fpath in enumerate(photo_files):
                        ext_i = Path(fpath).suffix.lower()
                        fname_i = Path(fpath).name
                        try:
                            if ext_i in IMAGE_EXT_SET:
                                await client.send_photo(info["chat_id"], fpath,
                                    caption=cap if i==0 else fname_i,
                                    reply_to_message_id=info["reply_to"])
                            elif is_video_path(fname_i):
                                dur_i = await _get_video_duration(fpath)
                                th_i = await choose_thumbnail(uid, fpath)
                                await client.send_video(info["chat_id"], fpath,
                                    caption=cap if i==0 else fname_i,
                                    thumb=th_i, duration=dur_i,
                                    reply_to_message_id=info["reply_to"])
                        except Exception: pass
                        await asyncio.sleep(0.3)
                    try: await cq.message.delete()
                    except: pass
                    await update_user_stats(uid, sum(Path(f).stat().st_size for f in photo_files if Path(f).exists())/1048576)
                    YTDL_TASKS.pop(tid,None); return
            except Exception as pe:
                err_str = str(pe)
        try: await cq.message.edit_text(f"❌ Download failed:\n<code>{err_str[:500]}</code>")
        except: pass
        YTDL_TASKS.pop(tid,None); return
    basename=os.path.basename(dl_path)
    status=cq.message; start_u=time.time()
    try: await status.edit_text(f"📤 Uploading: <code>{basename}</code>")
    except: pass
    try:
        # Check if multiple files were downloaded (e.g. Instagram carousel)
        all_files = sorted(Path(info["temp_root"]).rglob("*"), key=lambda p: p.stat().st_mtime)
        media_files = [p for p in all_files if p.is_file() and p.suffix.lower() not in (".json",".part",".ytdl")]
        # Use the latest file as primary
        if not media_files:
            await status.edit_text("❌ No file downloaded."); YTDL_TASKS.pop(tid,None); return
        # Send each file (carousel support)
        for i, fpath in enumerate(media_files):
            fname_i = fpath.name
            ext_i = fpath.suffix.lower()
            try:
                cap_i = await build_caption(uid, fname_i) if i==0 else fname_i
                if ext_i in IMAGE_EXT_SET:
                    # Instagram photo / story image
                    sent = await client.send_photo(info["chat_id"], str(fpath),
                        caption=cap_i, reply_to_message_id=info["reply_to"])
                elif is_video_path(fname_i):
                    thumb_i = await choose_thumbnail(uid, str(fpath))
                    dur_i = await _get_video_duration(str(fpath))
                    sent = await client.send_video(info["chat_id"], str(fpath),
                        caption=cap_i, thumb=thumb_i, duration=dur_i,
                        progress=progress_for_pyrogram,
                        progress_args=(status, start_u, fname_i, "to Telegram"),
                        reply_to_message_id=info["reply_to"])
                elif is_audio_file(fname_i):
                    sent = await client.send_audio(info["chat_id"], str(fpath),
                        caption=cap_i, reply_to_message_id=info["reply_to"])
                else:
                    sent = await client.send_document(info["chat_id"], str(fpath),
                        caption=cap_i, reply_to_message_id=info["reply_to"])
                if i == 0:
                    try: await status.delete()
                    except: pass
                    await log_output(client, cq.from_user, sent, f"ytdl: {url}")
                    await update_user_stats(uid, sum(f.stat().st_size for f in media_files)/(1024*1024))
            except Exception as e:
                await client.send_message(info["chat_id"], f"❌ File {i+1} upload failed: <code>{e}</code>",
                    reply_to_message_id=info["reply_to"])
    except Exception as e:
        try: await status.edit_text(f"❌ Upload failed:\n<code>{e}</code>")
        except: pass
    YTDL_TASKS.pop(tid,None)

# ── Cloud Upload ──────────────────────────────────────────────────────────────
async def _do_cloud_upload(client, cq, orig, platform):
    media=orig.document or orig.video
    if not media: await cq.message.reply_text("File nahi mili."); return
    uid=cq.from_user.id
    temp_root=Path(Config.TEMP_DIR)/str(uid)/uuid.uuid4().hex; temp_root.mkdir(parents=True,exist_ok=True)
    await register_temp_path(uid,str(temp_root),Config.AUTO_DELETE_DEFAULT_MIN)
    fname=media.file_name or "file"
    status=await cq.message.reply_text(f"📥 Downloading {fname}…"); start=time.time()
    try:
        dl=await client.download_media(media,file_name=str(temp_root),
            progress=progress_for_pyrogram,progress_args=(status,start,fname,"to server"))
    except Exception as e: await status.edit_text(f"❌ Download failed:\n<code>{e}</code>"); return
    await status.edit_text(f"☁️ Uploading to {platform}…")
    try:
        if platform=="gofile": url=await upload_to_gofile(dl)
        elif platform=="catbox": url=await upload_to_catbox(dl)
        else: url=await smart_upload(dl)
        await status.edit_text(
            f"✅ <b>Uploaded to {platform}!</b>\n\n🔗 <a href='{url}'>Download Link</a>\n<code>{url}</code>")
    except Exception as e: await status.edit_text(f"❌ Cloud upload failed:\n<code>{e}</code>")

# ════════════════════════════════════════════════════════════════════════════
# M3U8 & LINKS
# ════════════════════════════════════════════════════════════════════════════
async def offer_m3u8_menu(client, cq, uid, url, temp_root):
    chat_id=cq.message.chat.id; reply_to=cq.message.id
    try: variants=await get_m3u8_variants(url)
    except Exception as e:
        await client.send_message(chat_id,f"❌ m3u8 parse failed:\n<code>{e}</code>",reply_to_message_id=reply_to); return
    base=url.split("?",1)[0].split("#",1)[0].rsplit("/",1)[-1] or "stream"
    if base.endswith(".m3u8"): base=base[:-5]
    if not variants: variants=[{"name":"Auto","url":url}]
    tid=uuid.uuid4().hex
    M3U8_TASKS[tid]={"user_id":uid,"url":url,"variants":variants,"temp_root":str(temp_root),"base_name":base}
    buttons=[[_btn(v["name"], f"m3q|{tid}|{i}", "primary")] for i,v in enumerate(variants)]
    await client.send_message(chat_id,f"📺 m3u8:\n<code>{url}</code>\n\nQuality:",
        reply_markup=InlineKeyboardMarkup(buttons),reply_to_message_id=reply_to)

async def handle_m3u8_quality_choice(client, cq, tid, idx):
    info=M3U8_TASKS.get(tid)
    if not info: await cq.answer("Task expired.",show_alert=True); return
    if not cq.from_user or cq.from_user.id!=info["user_id"]: await cq.answer("Ye tumhara task nahi.",show_alert=True); return
    variants=info["variants"]
    if idx<0 or idx>=len(variants): await cq.answer("Invalid.",show_alert=True); return
    v=variants[idx]; url=v["url"]; name=v["name"]
    temp_root=Path(info["temp_root"]); base=info["base_name"]
    chat_id=cq.message.chat.id; uid=cq.from_user.id; reply_to=cq.message.id
    await cq.answer()
    try: await cq.message.edit_text(f"📥 Downloading {name} stream…")
    except: pass
    dest=str(temp_root/f"{base}_{name}.mp4")
    try: await download_m3u8_stream(url,dest)
    except Exception as e:
        try: await cq.message.edit_text(f"❌ m3u8 download failed:\n<code>{e}</code>")
        except: pass
        M3U8_TASKS.pop(tid,None); return
    cap=await build_caption(uid,f"{base} [{name}]"); thumb=await choose_thumbnail(uid,dest)
    dur=await _get_video_duration(dest)
    try: await cq.message.edit_text("📤 Uploading m3u8 video…")
    except: pass
    start_u=time.time()
    try:
        sent=await client.send_video(chat_id,dest,caption=cap,thumb=thumb,duration=dur,
            progress=progress_for_pyrogram,progress_args=(cq.message,start_u,base,"to Telegram"),reply_to_message_id=reply_to)
        try: await cq.message.delete()
        except: pass
        await log_output(client,cq.from_user,sent,f"m3u8: {url}")
    except Exception as e:
        try: await cq.message.edit_text(f"❌ Upload failed:\n<code>{e}</code>")
        except: pass
    M3U8_TASKS.pop(tid,None)

async def handle_links_download_all(client, cq, original_msg):
    key=(original_msg.chat.id,original_msg.id); sess=LINK_SESSIONS.get(key)
    content=sess["content"] if sess else (original_msg.text or original_msg.caption or "") or ""
    all_links=sess["links"] if sess else find_links_in_text(content)
    if not all_links: await cq.message.edit_text("Koi URL nahi mila."); return
    cats: Dict[str,list]={"direct":[],"m3u8":[],"gdrive":[],"telegram":[],"unknown":[]}
    for u in all_links:
        k=classify_link(u); cats.setdefault(k,[]); cats[k].append(u)
    direct=cats.get("direct",[]); m3u8s=cats.get("m3u8",[]); gdrives=cats.get("gdrive",[]); unknowns=cats.get("unknown",[])
    candidates=direct+unknowns
    if not candidates and not m3u8s and not gdrives:
        await cq.message.edit_text("Supported links (direct/m3u8/gdrive) nahi mile."); return
    if not cq.from_user: return
    user=cq.from_user; uid=user.id
    temp_root=Path(Config.TEMP_DIR)/str(uid)/uuid.uuid4().hex
    temp_root.mkdir(parents=True,exist_ok=True)
    await register_temp_path(uid,str(temp_root),Config.AUTO_DELETE_DEFAULT_MIN)
    try: await cq.message.edit_text(f"⬇️ Direct: {len(candidates)} | GDrive: {len(gdrives)} | m3u8: {len(m3u8s)}\nDownloading…")
    except: pass
    ok=fail=0; chat_id=cq.message.chat.id; reply_to=cq.message.id
    is_priv=cq.message.chat.type==enums.ChatType.PRIVATE; pinned=False
    if is_priv:
        try: await client.pin_chat_message(chat_id,reply_to); pinned=True
        except: pass
    for url in candidates:
        if user_cancelled.get(uid): break
        base=url.split("?",1)[0].split("#",1)[0].rsplit("/",1)[-1] or f"file_{uuid.uuid4().hex[:8]}"
        dest=str(temp_root/base)
        try:
            st=await client.send_message(chat_id,f"⬇️ {url[:60]}…",reply_to_message_id=reply_to)
            fp=await download_file(url,dest,status_message=st,file_name=base,direction="to server")
            bn=os.path.basename(fp); await st.edit_text(f"📤 Uploading: {bn}")
            start_u=time.time()
            if is_video_path(bn):
                cap=await build_caption(uid,bn); thumb=await choose_thumbnail(uid,fp)
                sent=await client.send_video(chat_id,fp,caption=cap,thumb=thumb,
                    progress=progress_for_pyrogram,progress_args=(st,start_u,bn,"to Telegram"),reply_to_message_id=reply_to)
            else:
                sent=await client.send_document(chat_id,fp,caption=bn,
                    progress=progress_for_pyrogram,progress_args=(st,start_u,bn,"to Telegram"),reply_to_message_id=reply_to)
            try: await st.delete()
            except: pass
            ok+=1; await log_output(client,user,sent,f"direct link: {url}")
        except: fail+=1
        await asyncio.sleep(0.4)
    for url in gdrives:
        if user_cancelled.get(uid): break
        du=get_gdrive_direct_link(url)
        if not du: fail+=1; continue
        base=f"gdrive_{uuid.uuid4().hex[:8]}"; dest=str(temp_root/base)
        try:
            st=await client.send_message(chat_id,f"☁️ GDrive: {url[:50]}…",reply_to_message_id=reply_to)
            fp=await download_file(du,dest,status_message=st,file_name=base,direction="to server")
            bn=os.path.basename(fp); await st.edit_text(f"📤 Uploading: {bn}")
            start_u=time.time()
            if is_video_path(bn):
                cap=await build_caption(uid,bn); thumb=await choose_thumbnail(uid,fp)
                sent=await client.send_video(chat_id,fp,caption=cap,thumb=thumb,
                    progress=progress_for_pyrogram,progress_args=(st,start_u,bn,"to Telegram"),reply_to_message_id=reply_to)
            else:
                sent=await client.send_document(chat_id,fp,caption=bn,
                    progress=progress_for_pyrogram,progress_args=(st,start_u,bn,"to Telegram"),reply_to_message_id=reply_to)
            try: await st.delete()
            except: pass
            ok+=1; await log_output(client,user,sent,f"gdrive: {url}")
        except: fail+=1
        await asyncio.sleep(0.4)
    for url in m3u8s:
        if user_cancelled.get(uid): break
        await offer_m3u8_menu(client,cq,uid,url,temp_root)
    try: await cq.message.edit_text(f"✅ Direct/GDrive done.\nSuccess: {ok}  Failed: {fail}\nm3u8: {len(m3u8s)} (quality buttons above)")
    except: pass
    if is_priv and pinned:
        try: await client.unpin_chat_message(chat_id,reply_to)
        except: pass
        await client.send_message(chat_id,"✅ All link downloads finished!",reply_to_message_id=reply_to)

# ════════════════════════════════════════════════════════════════════════════
# MAIN
# ════════════════════════════════════════════════════════════════════════════
async def main():
    asyncio.create_task(cleanup_worker())
    await app.start()
    print("✅ Serena Unzip Bot v2 started.")
    await idle()
    await app.stop()

if __name__=="__main__":
    asyncio.run(main())

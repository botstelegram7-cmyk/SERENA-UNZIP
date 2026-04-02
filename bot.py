# bot.py — Serena Unzip Bot v3 (Upgraded: English · Bandwidth-Safe · Semaphore)
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
from pyrogram.errors import MessageNotModified, UserIsBlocked, FloodWait, PeerIdInvalid
from pyrogram.types import (
    CallbackQuery, Chat, InlineKeyboardButton, InlineKeyboardMarkup, Message,
)

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
    add_watermark, compress_video,
    compress_only, resize_only, compress_and_resize,
    extract_audio, extract_subtitles,
    generate_thumbnail, get_media_info, merge_videos, take_screenshot,
    _fmt_eta,
)
from utils.password_list import COMMON_PASSWORDS
from utils.pdf_tools import (
    extract_text_from_pdf, get_pdf_info, merge_pdfs,
    parse_page_ranges, split_pdf, split_pdf_by_range,
)
from utils.progress import human_bytes, progress_for_pyrogram
from utils.ytdl_tools import download_video as ytdl_download, get_formats, is_supported_url
from utils.zip_creator import create_archive

# ── Client ──────────────────────────────────────────────────────────────────
app = Client(
    "serena_unzip_bot",
    api_id=Config.API_ID,
    api_hash=Config.API_HASH,
    bot_token=Config.BOT_TOKEN,
    in_memory=True,
)

async def _safe_reply(message, text, **kwargs):
    """Send reply, silently ignore if user blocked bot or peer not found."""
    try:
        return await message.reply_text(text, **kwargs)
    except (UserIsBlocked, PeerIdInvalid, MessageNotModified):
        pass
    except Exception:
        pass
    return None

VIDEO_EXT_SET = {".mp4", ".mkv", ".mov", ".avi", ".webm", ".ts"}
AUDIO_EXT_SET = {".mp3", ".m4a", ".aac", ".ogg", ".opus", ".flac", ".wav"}
PDF_EXT_SET   = {".pdf"}
ARCHIVE_EXTS  = (".zip",".rar",".7z",".tar",".gz",".tgz",".tar.gz",
                 ".tar.bz2",".tbz2",".bz2",".xz",".tar.xz")
EMOJI_LIST    = ["🚀","📦","🎬","🧩","📄","🔗","🧪","⚡","💾","🧰"]

# ── Bandwidth / Resource Guards ──────────────────────────────────────────────
# Max 3 concurrent heavy I/O operations — prevents RAM overflow on 512MB Render
GLOBAL_SEMAPHORE = asyncio.Semaphore(3)

# ── State dicts ─────────────────────────────────────────────────────────────
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
def is_video_path(p):   return _ext(p) in VIDEO_EXT_SET

def _fmt_dur(s):
    s=int(s); h,r=divmod(s,3600); m,s=divmod(r,60)
    return f"{h:02d}:{m:02d}:{s:02d}"

def _secs_to_midnight():
    now=datetime.datetime.utcnow()
    mid=(now+datetime.timedelta(days=1)).replace(hour=0,minute=0,second=0,microsecond=0)
    return int((mid-now).total_seconds())

def _ht(s):
    if s<0: s=0
    h,r=divmod(s,3600); m,s=divmod(r,60)
    return f"{h}h {m}m" if h else f"{m}m {s}s"

def _safe_cleanup(*paths):
    """Immediately delete files/dirs after use to free disk & RAM."""
    for p in paths:
        if not p: continue
        try:
            if os.path.isdir(str(p)):
                shutil.rmtree(str(p), ignore_errors=True)
            elif os.path.isfile(str(p)):
                os.remove(str(p))
        except Exception:
            pass

def _disk_free_mb() -> float:
    return shutil.disk_usage("/").free / (1024 * 1024)

async def _check_disk_space_ok(dest_msg, min_mb: int = 300) -> bool:
    """Return False and send message if disk space is critically low."""
    free = _disk_free_mb()
    if free < min_mb:
        await dest_msg.reply_text(
            f"⚠️ <b>Server disk space is low!</b>\n"
            f"💾 Free: <b>{free:.0f} MB</b> (minimum needed: {min_mb} MB)\n\n"
            f"Please try again in a few minutes or contact the owner."
        )
        return False
    return True

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
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⭐ Get Premium",callback_data="premium_info")]]))
        return False
    if float(stats.get("daily_size_mb",0))>=Config.FREE_DAILY_SIZE_MB:
        await msg.reply_text("⚠️ Daily data limit reached. Get ⭐ Premium for no limits!"); return False
    last=stats.get("last_task_ts")
    if last:
        elapsed=(datetime.datetime.utcnow()-last).total_seconds() if isinstance(last,datetime.datetime) else time.time()-float(last)
        if elapsed<Config.FREE_MIN_WAIT_SEC:
            await msg.reply_text(
                f"⏳ Please wait <b>{int(Config.FREE_MIN_WAIT_SEC-elapsed)}s</b> before next task.\n"
                f"⭐ Premium users only wait 10s!"
            )
            return False
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
        [InlineKeyboardButton("📦 Unzip File",callback_data="help:unzip"),
         InlineKeyboardButton("⬇️ Download Link",callback_data="help:link")],
        [InlineKeyboardButton("⚙️ Settings",callback_data="open_settings"),
         InlineKeyboardButton("📊 My Stats",callback_data="show_mystats")],
        [InlineKeyboardButton("💬 Help",callback_data="show_help"),
         InlineKeyboardButton("⭐ Premium",callback_data="premium_info")],
        [InlineKeyboardButton("🚨 Report Bug",url="https://t.me/Technical_serenabot")],
        [_ob()],
    ])

def settings_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📝 Caption",callback_data="settings:caption"),
         InlineKeyboardButton("🔤 Replace Words",callback_data="settings:replace")],
        [InlineKeyboardButton("📸 Original Thumb",callback_data="settings:thumb:original"),
         InlineKeyboardButton("🎲 Random Thumb",callback_data="settings:thumb:random")],
        [InlineKeyboardButton("⚙️ Reset All",callback_data="settings:reset")],
        [_ob()],
    ])

def file_action_keyboard(msg, fname, fsize_mb=0):
    cid,mid=msg.chat.id,msg.id; rows=[]
    if is_archive_file(fname):
        rows.append([InlineKeyboardButton("📦 Unzip",callback_data=f"unzip|{cid}|{mid}|nopass"),
                     InlineKeyboardButton("🔐 With Password",callback_data=f"unzip|{cid}|{mid}|askpass")])
        if Config.ENABLE_AUTO_PASSWORD:
            rows.append([InlineKeyboardButton("🔑 Auto-Try Passwords",callback_data=f"unzip|{cid}|{mid}|autopass")])
    elif is_video_file(fname):
        rows.append([InlineKeyboardButton("🎵 Extract Audio",callback_data=f"audio|{cid}|{mid}"),
                     InlineKeyboardButton("📦 Compress",callback_data=f"compress|{cid}|{mid}")])
        rows.append([InlineKeyboardButton("✂️ Split File",callback_data=f"split|{cid}|{mid}"),
                     InlineKeyboardButton("ℹ️ File Info",callback_data=f"finfo|{cid}|{mid}")])
        if _ext(fname) in {".mkv",".mp4",".mov",".avi"}:
            rows.append([InlineKeyboardButton("🔤 Extract Subs",callback_data=f"subs|{cid}|{mid}"),
                         InlineKeyboardButton("📸 Screenshot",callback_data=f"screenshot|{cid}|{mid}")])
        rows.append([InlineKeyboardButton("💧 Watermark",callback_data=f"watermark|{cid}|{mid}"),
                     InlineKeyboardButton("✏️ Rename",callback_data=f"rename|{cid}|{mid}")])
    elif is_audio_file(fname):
        rows.append([InlineKeyboardButton("ℹ️ File Info",callback_data=f"finfo|{cid}|{mid}"),
                     InlineKeyboardButton("✏️ Rename",callback_data=f"rename|{cid}|{mid}")])
    elif is_pdf_file(fname):
        rows.append([InlineKeyboardButton("📄 PDF Tools",callback_data=f"pdf|{cid}|{mid}"),
                     InlineKeyboardButton("ℹ️ File Info",callback_data=f"finfo|{cid}|{mid}")])
        rows.append([InlineKeyboardButton("✏️ Rename",callback_data=f"rename|{cid}|{mid}")])
    else:
        rows.append([InlineKeyboardButton("📤 Send As-Is",callback_data=f"sendas|{cid}|{mid}"),
                     InlineKeyboardButton("✏️ Rename",callback_data=f"rename|{cid}|{mid}")])
        rows.append([InlineKeyboardButton("🗜 Add to ZIP",callback_data=f"addtozip|{cid}|{mid}")])
    if fsize_mb>Config.AUTO_SPLIT_MB:
        rows.append([InlineKeyboardButton("✂️ Split Parts",callback_data=f"split|{cid}|{mid}"),
                     InlineKeyboardButton("☁️ Cloud Upload",callback_data=f"cloudopt|{cid}|{mid}")])
    rows.append([_ob()]); return InlineKeyboardMarkup(rows)

# ════════════════════════════════════════════════════════════════════════════
# FORCE SUB
# ════════════════════════════════════════════════════════════════════════════
async def check_force_sub(client, message):
    if not message.from_user or not Config.FORCE_SUB_CHANNEL: return True
    try:
        m=await client.get_chat_member(Config.FORCE_SUB_CHANNEL,message.from_user.id)
        if m.status not in (enums.ChatMemberStatus.OWNER,enums.ChatMemberStatus.ADMINISTRATOR,enums.ChatMemberStatus.MEMBER): raise ValueError
        return True
    except:
        try:
            await message.reply_text(
                "⚠️ You must join our channel before using this bot!",
                reply_markup=InlineKeyboardMarkup([
                    [_cb()],[InlineKeyboardButton("✅ I Joined — Try Again",callback_data="retry_force_sub")]]))
        except: pass
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
                    try: await client.send_message(rid,f"🎉 {Config.REFERRAL_REQUIRED} referrals complete! You got <b>{Config.REFERRAL_REWARD_DAYS} days Premium</b> for FREE!")
                    except: pass
        except: pass
    name=message.from_user.first_name or "there"
    prem="⭐ Premium" if await is_premium_user(uid) else "Free"
    cap=(f"Hey <b>{name}</b>! 👋\n\nWelcome to <b>{Config.BOT_NAME}</b> [{prem}]\n\n"
         f"{random_emoji()} Unzip 20+ formats (ZIP/RAR/7Z/TAR + passwords)\n"
         f"{random_emoji()} Instagram, Twitter, Facebook, TikTok downloader\n"
         f"{random_emoji()} Video Compress (no limits!), Merge, Split, Watermark\n"
         f"{random_emoji()} Extract Audio, Subtitles, Screenshots\n"
         f"{random_emoji()} ZIP Creator, File Renamer, PDF Tools\n"
         f"{random_emoji()} Auto-process TXT/M3U8/GDrive links\n\nUse /help for full guide.")
    if Config.START_PIC: await message.reply_photo(Config.START_PIC,caption=cap,reply_markup=main_keyboard())
    else: await message.reply_text(cap,reply_markup=main_keyboard())


@app.on_message(filters.command("help") & (filters.private|filters.group))
async def help_cmd(client, message):
    text=(
        "✨ <b>Serena Unzip Bot v3 — Help</b>\n\n"
        "📦 <b>Archives</b>\n"
        "• Send archive → Unzip / Password / Auto-Try\n"
        "• ZIP, RAR, 7Z, TAR, GZ, BZ2, XZ supported\n\n"
        "🎬 <b>Video Tools</b>\n"
        "• Extract Audio  • Compress (360p–1080p + Smart)\n"
        "• Split File  • Extract Subtitles\n"
        "• Screenshot  • Watermark  • Rename\n\n"
        "⬇️ <b>Downloaders</b>\n"
        "• <code>/ytdl &lt;link&gt;</code> — Instagram, Twitter, TikTok, Facebook, Vimeo…\n"
        "• TXT / links → auto-download (direct, m3u8, GDrive)\n\n"
        "🗜 <b>ZIP Creator</b>: <code>/zip</code> → send files → ✅ Done\n"
        "🔗 <b>Merge Videos</b>: <code>/merge</code> → send videos → ✅ Merge\n"
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


@app.on_message(filters.command("settings") & filters.private)
async def settings_cmd(client, message):
    if not message.from_user: return
    uid=message.from_user.id; cfg=await get_user_settings(uid) or {}
    text=(f"{random_emoji()} <b>Your Settings</b>\n\n"
          f"📝 Caption base: <code>{cfg.get('caption_base') or 'None'}</code>\n"
          f"🔤 Replace: <code>{cfg.get('replace_from') or 'None'}</code> → <code>{cfg.get('replace_to') or 'None'}</code>\n"
          f"🖼 Thumbnail: <code>{cfg.get('thumb_mode','random')}</code>\n\n"
          "Use buttons below:")
    await message.reply_text(text, reply_markup=settings_keyboard())


# ── Per-user running FFmpeg process registry (for /cancel to kill them) ────
USER_FFMPEG_PROCS: Dict[int, list] = {}  # uid -> list of asyncio.subprocess.Process

def _register_proc(uid: int, proc) -> None:
    if uid not in USER_FFMPEG_PROCS:
        USER_FFMPEG_PROCS[uid] = []
    USER_FFMPEG_PROCS[uid].append(proc)

def _unregister_proc(uid: int, proc) -> None:
    procs = USER_FFMPEG_PROCS.get(uid, [])
    try:
        procs.remove(proc)
    except ValueError:
        pass

def _kill_user_procs(uid: int) -> int:
    """Kill all running FFmpeg processes for this user. Returns count killed."""
    procs = USER_FFMPEG_PROCS.pop(uid, [])
    killed = 0
    for proc in procs:
        try:
            proc.kill()
            killed += 1
        except Exception:
            pass
    return killed


@app.on_message(filters.command("cancel") & (filters.private|filters.group))
async def cancel_cmd(client, message):
    if not message.from_user: return
    uid = message.from_user.id
    user_cancelled[uid] = True

    # Kill any running FFmpeg processes for this user
    killed = _kill_user_procs(uid)

    # Clean up all task dicts for this user
    zip_sessions.pop(uid, None)
    merge_sessions.pop(uid, None)
    pending_state.pop(uid, None)
    pending_password.pop(uid, None)

    # Remove compress/split/pdf tasks belonging to this user
    for task_dict in (COMPRESS_TASKS, SPLIT_TASKS, SUB_TASKS, PDF_TASKS,
                      M3U8_TASKS, YTDL_TASKS, BIG_FILE_TASKS):
        dead = [k for k, v in task_dict.items() if v.get("user_id") == uid]
        for k in dead:
            task_dict.pop(k, None)

    # Release the user lock so next task can start
    lock = user_locks.get(uid)
    if lock and lock.locked():
        try:
            lock.release()
        except RuntimeError:
            pass

    proc_msg = f" (Stopped {killed} running process(es))" if killed else ""
    reply_text = "🛑 <b>All tasks cancelled!" + proc_msg + "</b>\n\n"\
        "✅ Compress, download, extract — everything stopped.\n"\
        "You can start a new task now."
    await message.reply_text(reply_text, parse_mode=enums.ParseMode.HTML)


@app.on_message(filters.command("mystats") & (filters.private|filters.group))
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


@app.on_message(filters.command("refer") & filters.private)
async def refer_cmd(client, message):
    if not message.from_user: return
    uid=message.from_user.id; me=await client.get_me()
    link=f"https://t.me/{me.username}?start=ref_{uid}"
    rc=await get_referral_count(uid); need=max(0,Config.REFERRAL_REQUIRED-rc)
    await message.reply_text(
        f"🎁 <b>Referral Program</b>\n\nYour referral link:\n<code>{link}</code>\n\n"
        f"✅ Referred: <b>{rc}/{Config.REFERRAL_REQUIRED}</b>  |  Need {need} more\n"
        f"🏆 Reward: <b>{Config.REFERRAL_REWARD_DAYS} days Premium FREE!</b>",
        reply_markup=InlineKeyboardMarkup([[
            InlineKeyboardButton("📤 Share",url=f"https://t.me/share/url?url={link}&text=Join+Serena+Bot!")
        ]]))


@app.on_message(filters.command("ytdl") & (filters.private|filters.group))
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
            "<i>Note: YouTube & paid education platforms are not supported.</i>"); return
    url=args[0].strip()
    if not is_supported_url(url):
        await message.reply_text("❌ This URL is not supported."); return
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
        buttons.append([InlineKeyboardButton(f"{f['label']}{sz}",callback_data=f"ytdlq|{task_id}|{i}")])
    buttons.append([InlineKeyboardButton("❌ Cancel",callback_data=f"ytdlcancel|{task_id}")])
    try: await status.edit_text(f"🎬 Choose quality:\n<code>{url}</code>",reply_markup=InlineKeyboardMarkup(buttons))
    except: pass


@app.on_message(filters.command("zip") & (filters.private|filters.group))
async def zip_cmd(client, message):
    if not message.from_user: return
    uid=message.from_user.id
    if await is_banned(uid): return
    if uid in zip_sessions:
        await message.reply_text("A ZIP session is already active. Use /cancel first."); return
    temp_root=Path(Config.TEMP_DIR)/str(uid)/uuid.uuid4().hex
    temp_root.mkdir(parents=True,exist_ok=True)
    await register_temp_path(uid,str(temp_root),Config.AUTO_DELETE_DEFAULT_MIN)
    zip_sessions[uid]={"files":[],"temp_root":str(temp_root),"chat_id":message.chat.id,"reply_to":message.id,"password":None}
    await message.reply_text(
        "📦 <b>ZIP Creator Mode ON!</b>\n\nSend files one by one.\nTap ✅ Done when finished.",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("✅ Done — Create ZIP",callback_data=f"zipfile|done|{uid}"),
             InlineKeyboardButton("🔐 Add Password",callback_data=f"zipfile|askpass|{uid}")],
            [InlineKeyboardButton("❌ Cancel",callback_data=f"zipfile|cancel|{uid}")]]))


@app.on_message(filters.command("merge") & (filters.private|filters.group))
async def merge_cmd(client, message):
    if not message.from_user: return
    uid=message.from_user.id
    if await is_banned(uid): return
    if uid in merge_sessions:
        await message.reply_text("A merge session is already active. Use /cancel."); return
    temp_root=Path(Config.TEMP_DIR)/str(uid)/uuid.uuid4().hex
    temp_root.mkdir(parents=True,exist_ok=True)
    await register_temp_path(uid,str(temp_root),Config.AUTO_DELETE_DEFAULT_MIN)
    merge_sessions[uid]={"files":[],"temp_root":str(temp_root),"chat_id":message.chat.id,"reply_to":message.id}
    await message.reply_text(
        "🔗 <b>Merge Mode ON!</b>\n\nSend videos (same format). Order will be preserved.",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("✅ Merge Now",callback_data=f"mergefile|done|{uid}"),
             InlineKeyboardButton("❌ Cancel",callback_data=f"mergefile|cancel|{uid}")]]))


@app.on_message(filters.command(["rename","info","subs","screenshot","pdf","compress","split","audio","unzip","watermark"]) & (filters.private|filters.group|filters.channel))
async def file_command_handler(client, message):
    if not message.from_user: return
    uid=message.from_user.id
    if await is_banned(uid): return
    cmd=message.command[0].lower()
    r=message.reply_to_message
    media=None
    if r: media=r.document or r.video or r.audio
    if not r or not media:
        hints={
            "rename":"Reply to a file and use /rename.",
            "info":"Reply to a file and use /info.",
            "subs":"Reply to an MKV/MP4 and use /subs.",
            "screenshot":"Reply to a video and use /screenshot HH:MM:SS.",
            "pdf":"Reply to a PDF and use /pdf.",
            "compress":"Reply to a video and use /compress.",
            "split":"Reply to a file and use /split.",
            "audio":"Reply to a video and use /audio.",
            "unzip":"Reply to an archive and use /unzip.",
            "watermark":"Reply to a video and use /watermark [text]."
        }
        await message.reply_text(hints.get(cmd,"Reply to a file and use the command.")); return
    cid,mid=r.chat.id,r.id
    fname=media.file_name or "file"
    if cmd=="rename":
        pending_state[uid]={"action":"rename","chat_id":cid,"msg_id":mid,"fname":fname}
        await message.reply_text(f"✏️ Send the new name for <code>{fname}</code>:")
    elif cmd=="info": await _handle_file_info(client,message,r)
    elif cmd=="subs": await _trigger_subs(client,message,r)
    elif cmd=="screenshot":
        args=message.command[1:]
        if args: await _handle_screenshot_with_time(client,message,r,args[0])
        else:
            pending_state[uid]={"action":"screenshot","chat_id":cid,"msg_id":mid}
            await message.reply_text("📸 Send timestamp: <code>MM:SS</code> or <code>HH:MM:SS</code>")
    elif cmd=="pdf": await _trigger_pdf_tools(client,message,r,fname)
    elif cmd=="compress":
        if not await check_rate_limit(uid,message): return
        await _trigger_compress(client,message,r,cid,mid,uid=uid)
    elif cmd=="split": await _trigger_split(client,message,r,cid,mid,fname)
    elif cmd=="audio": await handle_extract_audio(client,None,r,reply_to_msg=message)
    elif cmd=="unzip":
        if not is_archive_file(fname): await message.reply_text("This is not a supported archive."); return
        if not await check_rate_limit(uid,message): return
        await run_unzip_task(client,r,password=None,reply_msg=message)
    elif cmd=="watermark":
        args_list=message.command[1:]
        if args_list: await _handle_watermark(client,message,r," ".join(args_list))
        else:
            pending_state[uid]={"action":"watermark","chat_id":cid,"msg_id":mid}
            await message.reply_text("💧 Send the watermark text:")


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
            [InlineKeyboardButton("📊 Full Stats",callback_data="admin:status"),
             InlineKeyboardButton("📢 Broadcast hint",callback_data="admin:broadcast")],
            [InlineKeyboardButton("🧹 Clean Storage",callback_data="admin:clean")]]))


@app.on_message(filters.command("broadcast") & filters.private)
async def broadcast_cmd(client, message):
    if not message.from_user or not is_owner(message.from_user.id): return
    if not message.reply_to_message: await message.reply_text("Reply to a message first."); return
    users=await get_all_users(); sent=failed=0
    for u in users:
        try: await message.reply_to_message.copy(chat_id=u); sent+=1; await asyncio.sleep(0.05)
        except: failed+=1
    await message.reply_text(f"📢 Done. Sent: {sent}  Failed: {failed}")


@app.on_message(filters.command("premium") & filters.private)
async def premium_cmd(client, message):
    if not message.from_user or not is_owner(message.from_user.id): return
    parts=(message.text or "").split()
    if len(parts)<2: await message.reply_text("Usage: /premium <id> [days]"); return
    try: target=int(parts[1])
    except: await message.reply_text("ID must be an integer."); return
    days=int(parts[2]) if len(parts)>=3 else 30
    await set_premium(target,time.time()+days*86400)
    await message.reply_text(f"⭐ User {target} granted Premium for {days} days.")


@app.on_message(filters.command(["ban","unban"]) & filters.private)
async def ban_cmd(client, message):
    if not message.from_user or not is_owner(message.from_user.id): return
    cmd=message.command[0].lower(); target=None
    if message.reply_to_message: target=message.reply_to_message.from_user.id
    elif len(message.command)>1:
        try: target=int(message.command[1])
        except: pass
    if not target: await message.reply_text("Provide a user ID or reply to user."); return
    await set_ban(target,cmd=="ban")
    await message.reply_text(f"User {target} {'banned' if cmd=='ban' else 'unbanned'}.")

# ════════════════════════════════════════════════════════════════════════════
# FILE HANDLER
# ════════════════════════════════════════════════════════════════════════════
@app.on_message((filters.document|filters.video|filters.photo|filters.audio) & (filters.private|filters.group|filters.channel))
async def on_file(client, message):
    if not message.from_user: return
    uid=message.from_user.id
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
        await message.reply_text(f"📎 File {cnt}: <code>{fname}</code>\nSend more or tap ✅ Done.",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("✅ Done",callback_data=f"zipfile|done|{uid}"),
                 InlineKeyboardButton("🔐 Password",callback_data=f"zipfile|askpass|{uid}")],
                [InlineKeyboardButton("❌ Cancel",callback_data=f"zipfile|cancel|{uid}")]]))
        return
    # Merge session collect
    if uid in merge_sessions and message.video:
        sess=merge_sessions[uid]
        sess["files"].append({"file_id":message.video.file_id,"file_name":message.video.file_name or "video.mp4"})
        cnt=len(sess["files"])
        await message.reply_text(f"🎬 Video {cnt}: <code>{message.video.file_name or 'video'}</code>",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("✅ Merge Now",callback_data=f"mergefile|done|{uid}"),
                 InlineKeyboardButton("❌ Cancel",callback_data=f"mergefile|cancel|{uid}")]]))
        return
    # TXT link source
    if message.chat.type==enums.ChatType.PRIVATE and message.document and fname.lower().endswith(".txt"):
        temp_root=Path(Config.TEMP_DIR)/str(uid)/uuid.uuid4().hex
        temp_root.mkdir(parents=True,exist_ok=True)
        await register_temp_path(uid,str(temp_root),Config.AUTO_DELETE_DEFAULT_MIN)
        st=await message.reply_text("📥 Reading TXT file…")
        try:
            p=await client.download_media(message.document,file_name=str(temp_root))
            content=Path(p).read_text(encoding="utf-8",errors="ignore")
        except Exception as e: await st.edit_text(f"❌ TXT download failed:\n<code>{e}</code>"); return
        try: await st.delete()
        except: pass
        _safe_cleanup(str(temp_root))
        await process_links_message(client,message,content); return
    if not media: return
    fsize_mb=(getattr(media,"file_size",0) or 0)/(1024*1024)
    # Groups mein auto-respond nahi karo — sirf private chat mein action keyboard do
    if message.chat.type != enums.ChatType.PRIVATE:
        return
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
    if not links:
        try: await message.reply_text("No valid URLs found.")
        except Exception: pass
        return
    LINK_SESSIONS[(message.chat.id,message.id)]={"links":links,"content":content or ""}
    cats={}
    for u in links:
        k=classify_link(u); cats[k]=cats.get(k,0)+1
    em={"gdrive":"🗂","m3u8":"📺","direct":"💾","telegram":"✈️","unknown":"🔗"}
    lines=[f"{em.get(k,'🔗')} {k}: <b>{v}</b>" for k,v in cats.items()]
    try:
        await message.reply_text(
            f"🔗 <b>{len(links)} links found</b>\n\n"+"\n".join(lines)+"\n\nChoose action:",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("⬇️ Download All",callback_data=f"links|download_all|{message.chat.id}|{message.id}")],
                [InlineKeyboardButton("🧹 Cleaned TXT",callback_data=f"links|clean_txt|{message.chat.id}|{message.id}"),
                 InlineKeyboardButton("⏭ Skip",callback_data=f"links|skip|{message.chat.id}|{message.id}")]]))
    except (UserIsBlocked, PeerIdInvalid): pass
    except Exception: pass


@app.on_message((filters.text|filters.caption) & filters.private)
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
            if not txt: await message.reply_text("Caption cannot be empty."); return
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
                await message.reply_text(f"🔐 Password set: <code>{txt}</code>. Tap ✅ Done.",
                    reply_markup=InlineKeyboardMarkup([[
                        InlineKeyboardButton("✅ Done",callback_data=f"zipfile|done|{uid}"),
                        InlineKeyboardButton("❌ Cancel",callback_data=f"zipfile|cancel|{uid}")]]))
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


@app.on_message((filters.text|filters.caption) & (filters.group|filters.channel))
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
            f"<b>Premium:</b> Unlimited tasks, no limits, 10s cooldown\n\n"
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
            await cq.message.reply_text("📝 Send your base caption:\nExample: <code>My Pack</code>")
            await cq.answer(); return
        if action=="replace":
            pending_state[uid]={"action":"settings_replace"}
            await cq.message.reply_text("🔤 Send replace rule:\n<code>old -> new</code>")
            await cq.answer(); return
        if action.startswith("thumb:"):
            mode=action.split(":",1)[1]; cfg=await get_user_settings(uid) or {}
            cfg["thumb_mode"]=mode; await save_user_settings(uid,cfg)
            await cq.message.reply_text(f"✅ Thumbnail mode set to: <b>{mode}</b>"); await cq.answer(); return
    if data.startswith("unzip|"):
        try: _,cid,mid,mode=data.split("|",3); orig=await client.get_messages(int(cid),int(mid))
        except: await cq.answer("File not found.",show_alert=True); return
        await handle_unzip_button(client,cq,orig,mode); return
    if data.startswith("ucancel|"):
        _,tid=data.split("|",1); tasks.pop(tid,None)
        try: await cq.message.edit_text("❌ Unzip cancelled.")
        except: pass
        await cq.answer(); return
    if data.startswith("audio|"):
        try: _,cid,mid=data.split("|",2); orig=await client.get_messages(int(cid),int(mid))
        except: await cq.answer("Video not found.",show_alert=True); return
        await handle_extract_audio(client,cq,orig); return
    if data.startswith("sendall|"):
        _,tid=data.split("|",1); await handle_send_all(client,cq,tid); return
    if data.startswith("sendone|"):
        _,tid,idx=data.split("|",2); await handle_send_one(client,cq,tid,int(idx)); return
    if data.startswith("sendas|"):
        _,cid,mid=data.split("|",2)
        try: orig=await client.get_messages(int(cid),int(mid))
        except: await cq.answer("File not found.",show_alert=True); return
        await cq.answer()
        try:
            if orig.document: await client.send_document(cq.message.chat.id,orig.document.file_id,reply_to_message_id=cq.message.id)
            elif orig.video: await client.send_video(cq.message.chat.id,orig.video.file_id,reply_to_message_id=cq.message.id)
        except Exception as e: await cq.message.reply_text(f"❌ <code>{e}</code>")
        return
    if data.startswith("rename|"):
        _,cid,mid=data.split("|",2)
        try: orig=await client.get_messages(int(cid),int(mid))
        except: await cq.answer("File not found.",show_alert=True); return
        media=orig.document or orig.video or orig.audio
        fname=(media.file_name if media else None) or "file"
        pending_state[cq.from_user.id]={"action":"rename","chat_id":int(cid),"msg_id":int(mid),"fname":fname}
        await cq.message.reply_text(f"✏️ Send new name (current: <code>{fname}</code>):")
        await cq.answer(); return
    if data.startswith("finfo|"):
        _,cid,mid=data.split("|",2)
        try: orig=await client.get_messages(int(cid),int(mid))
        except: await cq.answer("File not found.",show_alert=True); return
        await cq.answer(); await _handle_file_info(client,cq.message,orig); return
    if data.startswith("compress|"):
        _,cid,mid=data.split("|",2)
        try: orig=await client.get_messages(int(cid),int(mid))
        except: await cq.answer("File not found.",show_alert=True); return
        await cq.answer(); await _trigger_compress(client,cq.message,orig,int(cid),int(mid),uid=cq.from_user.id); return
    if data.startswith("cmode|"):
        # cmode|tid|mode  — user picked Compress / Resize / Both
        _, tid, mode = data.split("|", 2)
        if tid in COMPRESS_TASKS:
            COMPRESS_TASKS[tid]["mode"] = mode
        await cq.answer()
        await _show_mode_options(cq, tid, mode); return
    if data.startswith("comprq|"):
        # Legacy handler — kept for old tasks
        parts = data.split("|", 3); tid = parts[1]; res = parts[2]
        crf   = int(parts[3]) if len(parts) > 3 else 28
        await cq.answer(); await _do_compress(client, cq, tid, res, crf); return
    if data.startswith("comprup|"):
        # Legacy handler
        parts = data.split("|", 4)
        tid = parts[1]; res = parts[2]; crf = int(parts[3]); method = parts[4]
        if tid in COMPRESS_TASKS:
            COMPRESS_TASKS[tid]["upload_method_chosen"] = method
        await cq.answer(); await _do_compress(client, cq, tid, res, crf); return

    # ── NEW 3-mode compress flow ──────────────────────────────────────────
    if data.startswith("cm_mode|"):
        _, tid, mode = data.split("|", 2)
        await cq.answer()
        if mode == "compress":
            await _show_crf_buttons(cq, tid, "compress", "orig")
        else:
            await _show_resolution_buttons(cq, tid, mode)
        return

    if data.startswith("cm_res|"):
        _, tid, mode, res = data.split("|", 3)
        await cq.answer()
        if mode == "resize":
            if tid in COMPRESS_TASKS:
                COMPRESS_TASKS[tid]["mode"] = "resize"
                COMPRESS_TASKS[tid]["resolution"] = res
                COMPRESS_TASKS[tid]["crf"] = 23
            await _show_upload_buttons(cq, tid, mode, res, 23)
        else:
            await _show_crf_buttons(cq, tid, mode, res)
        return

    if data.startswith("cm_crf|"):
        _, tid, mode, res, crf_s = data.split("|", 4)
        crf = int(crf_s)
        await cq.answer()
        await _show_upload_buttons(cq, tid, mode, res, crf)
        return

    if data.startswith("cm_up|"):
        _, tid, mode, res, crf_s, upload_m = data.split("|", 5)
        crf = int(crf_s)
        await cq.answer()
        if tid in COMPRESS_TASKS:
            COMPRESS_TASKS[tid]["mode"]          = mode
            COMPRESS_TASKS[tid]["resolution"]    = res if res != "orig" else None
            COMPRESS_TASKS[tid]["crf"]           = crf
            COMPRESS_TASKS[tid]["upload_method"] = upload_m
        await _run_compress_task(client, cq, tid)
        return
    if data.startswith("split|"):
        _,cid,mid=data.split("|",2)
        try: orig=await client.get_messages(int(cid),int(mid))
        except: await cq.answer("File not found.",show_alert=True); return
        await cq.answer()
        media=orig.document or orig.video; fname=(media.file_name if media else None) or "file"
        await _trigger_split(client,cq.message,orig,int(cid),int(mid),fname); return
    if data.startswith("splitq|"):
        _,tid,smb=data.split("|",2); await cq.answer(); await _do_split(client,cq,tid,int(smb)); return
    if data.startswith("subs|"):
        _,cid,mid=data.split("|",2)
        try: orig=await client.get_messages(int(cid),int(mid))
        except: await cq.answer("File not found.",show_alert=True); return
        await cq.answer(); await _trigger_subs(client,cq.message,orig); return
    if data.startswith("subsq|"):
        _,tid,sidx=data.split("|",2); await cq.answer()
        val=sidx if sidx=="all" else int(sidx)
        await _do_extract_sub(client,cq,tid,val); return
    if data.startswith("screenshot|"):
        _,cid,mid=data.split("|",2)
        pending_state[cq.from_user.id]={"action":"screenshot","chat_id":int(cid),"msg_id":int(mid)}
        await cq.message.reply_text("📸 Send timestamp: <code>MM:SS</code> or <code>HH:MM:SS</code>")
        await cq.answer(); return
    if data.startswith("watermark|"):
        _,cid,mid=data.split("|",2)
        pending_state[cq.from_user.id]={"action":"watermark","chat_id":int(cid),"msg_id":int(mid)}
        await cq.message.reply_text("💧 Send the watermark text:"); await cq.answer(); return
    if data.startswith("pdf|"):
        _,cid,mid=data.split("|",2)
        try: orig=await client.get_messages(int(cid),int(mid))
        except: await cq.answer("File not found.",show_alert=True); return
        await cq.answer()
        media=orig.document; fname=(media.file_name if media else None) or "file.pdf"
        await _trigger_pdf_tools(client,cq.message,orig,fname); return
    if data.startswith("pdfq|"):
        _,tid,action=data.split("|",2); await cq.answer(); await _do_pdf_action(client,cq,tid,action); return
    if data.startswith("zipfile|"):
        _,action,uid_str=data.split("|",2); uid=int(uid_str)
        if cq.from_user.id!=uid: await cq.answer("This is not your session.",show_alert=True); return
        await cq.answer()
        if action=="cancel":
            zip_sessions.pop(uid,None)
            try: await cq.message.edit_text("❌ ZIP session cancelled.")
            except: pass
        elif action=="askpass":
            pending_state[uid]={"action":"zip_password"}
            await cq.message.reply_text("🔐 Send password (or /cancel):")
        elif action=="done": await _do_create_zip(client,cq,uid)
        return
    if data.startswith("mergefile|"):
        _,action,uid_str=data.split("|",2); uid=int(uid_str)
        if cq.from_user.id!=uid: await cq.answer("This is not your session.",show_alert=True); return
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
        except: await cq.answer("Message not found.",show_alert=True); return
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
        await cq.message.reply_text("☁️ Choose cloud platform:",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("🌐 GoFile (No limit)",callback_data=f"cloudgo|{cid}|{mid}"),
                 InlineKeyboardButton("📦 Catbox (≤200MB)",callback_data=f"cloudcat|{cid}|{mid}")],
                [InlineKeyboardButton("❌ Cancel",callback_data="noop")]])); return
    if data.startswith("cloudgo|") or data.startswith("cloudcat|"):
        platform="gofile" if data.startswith("cloudgo|") else "catbox"
        _,cid,mid=data.split("|",2)
        try: orig=await client.get_messages(int(cid),int(mid))
        except: await cq.answer("File not found.",show_alert=True); return
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
            await cq.message.reply_text("📢 Reply to a message then use /broadcast.")
        elif action=="clean":
            await cq.message.reply_text("🧹 Cleanup running (background worker is active).")
        await cq.answer(); return
    if data=="noop": await cq.answer(); return
    if data=="report_bug": await cq.answer("🚨 Report sent! Contact @Technical_serenabot",show_alert=True); return
    await cq.answer()

# ════════════════════════════════════════════════════════════════════════════
# UNZIP
# ════════════════════════════════════════════════════════════════════════════
async def handle_unzip_button(client, cq, orig, mode):
    if not cq.from_user: await cq.answer(); return
    uid=cq.from_user.id
    if await is_banned(uid): await cq.answer("Banned.",show_alert=True); return
    if not await check_force_sub(client,orig): await cq.answer(); return
    doc=orig.document
    if not doc: await cq.answer("No document found.",show_alert=True); return
    fname=doc.file_name or "archive"
    if not is_archive_file(fname): await cq.answer("Not a supported archive.",show_alert=True); return
    if not await check_rate_limit(uid,cq.message): await cq.answer(); return
    if mode=="askpass":
        pending_password[uid]={"chat_id":orig.chat.id,"msg_id":orig.id,"file_name":fname}
        await cq.message.reply_text(f"🔐 Send password for <code>{fname}</code>:")
        await cq.answer(); return
    if mode=="autopass":
        await cq.answer(); await _auto_try_passwords(client,cq.message,orig); return
    await cq.answer(); await run_unzip_task(client,orig,password=None,reply_msg=cq.message)

async def _auto_try_passwords(client, reply_msg, orig):
    if not orig.from_user: return
    uid=orig.from_user.id
    status=await reply_msg.reply_text("🔑 Auto-trying common passwords…")
    doc=orig.document
    if not doc: await status.edit_text("No document found."); return
    if not await _check_disk_space_ok(status): return
    temp_root=Path(Config.TEMP_DIR)/str(uid)/uuid.uuid4().hex
    temp_root.mkdir(parents=True,exist_ok=True)
    await register_temp_path(uid,str(temp_root),Config.AUTO_DELETE_DEFAULT_MIN)
    async with GLOBAL_SEMAPHORE:
        try: dl=await client.download_media(doc,file_name=str(temp_root))
        except Exception as e: await status.edit_text(f"❌ Download failed:\n<code>{e}</code>"); _safe_cleanup(str(temp_root)); return
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
        _safe_cleanup(str(temp_root))
        await status.edit_text("❌ None of the common passwords worked.\n🔐 Please enter the password manually.")

async def handle_unzip_from_password(client, msg, info, password):
    orig=await client.get_messages(info["chat_id"],info["msg_id"])
    await msg.reply_text("✅ Password received, extracting…")
    await run_unzip_task(client,orig,password=password,reply_msg=msg)

async def run_unzip_task(client, msg, password, reply_msg=None):
    if not msg.from_user: return
    uid=msg.from_user.id; lock=get_lock(uid); dest=reply_msg or msg
    if lock.locked(): await dest.reply_text("⏳ A task is already running. Please wait."); return
    if not await _check_disk_space_ok(dest): return
    async with lock:
        async with GLOBAL_SEMAPHORE:
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
            except Exception as e: await status.edit_text(f"❌ Download failed:\n<code>{e}</code>"); _safe_cleanup(str(temp_root)); return
            if not dl: await status.edit_text("❌ Download failed."); _safe_cleanup(str(temp_root)); return
            if user_cancelled.get(uid): await status.edit_text("❌ Cancelled."); _safe_cleanup(str(temp_root)); return
            if not password and detect_encrypted(dl):
                await status.edit_text("🔐 Archive is encrypted!\nUse <b>With Password</b> or <b>Auto-Try</b>."); return
            await status.edit_text("📦 Extracting…")
            extract_dir=temp_root/"extracted"
            try: result=extract_archive(dl,str(extract_dir),password=password)
            except Exception as e: await status.edit_text(f"❌ Extraction error:\n<code>{e}</code>"); _safe_cleanup(str(temp_root)); return
            # Delete the downloaded archive immediately to free disk space
            try: os.remove(dl)
            except: pass
            if user_cancelled.get(uid): await status.edit_text("❌ Cancelled mid-extraction."); _safe_cleanup(str(temp_root)); return
            stats=result["stats"]; files=sorted(result["files"],key=lambda p:p.lower())
            links_map=extract_links_from_folder(str(extract_dir))
            tid=uuid.uuid4().hex
            tasks[tid]={"type":"unzip","user_id":uid,"base_dir":str(extract_dir),"files":files,"archive_name":fname}
            summary=(f"✅ <b>Extraction Complete!</b>\n\n📁 <code>{fname}</code>\n"
                     f"📊 Files: <b>{stats['total_files']}</b>  Folders: <b>{stats['folders']}</b>\n"
                     f"🎬 Videos: <b>{stats['videos']}</b>  📄 PDFs: <b>{stats['pdf']}</b>  "
                     f"📱 APKs: <b>{stats['apk']}</b>\n"
                     f"📝 TXT: <b>{stats['txt']}</b>  📺 M3U8: <b>{stats['m3u']}</b>  "
                     f"Others: <b>{stats['others']}</b>\n")
            tl=sum(len(v) for v in links_map.values())
            if tl: summary+=f"\n🔗 Links found: <b>{tl}</b> (Direct:{len(links_map.get('direct',[]))} M3U8:{len(links_map.get('m3u8',[]))} GDrive:{len(links_map.get('gdrive',[]))})\n"
            rows=[[InlineKeyboardButton("❌ Cancel",callback_data=f"ucancel|{tid}")],
                  [InlineKeyboardButton("🚀 Send ALL",callback_data=f"sendall|{tid}")]]
            for idx,rel in enumerate(files[:25]):
                short=rel if len(rel)<=42 else "…"+rel[-39:]
                rows.append([InlineKeyboardButton(short,callback_data=f"sendone|{tid}|{idx}")])
            if tl:
                all_links=[l for v in links_map.values() for l in v]
                LINK_SESSIONS[(status.chat.id,status.id)]={"links":all_links,"content":"\n".join(all_links)}
                rows.append([InlineKeyboardButton(f"⬇️ Download {tl} links",callback_data=f"links|download_all|{status.chat.id}|{status.id}")])
            await status.edit_text(summary,reply_markup=InlineKeyboardMarkup(rows))
            await update_user_stats(uid,size_mb)

async def handle_send_all(client, cq, tid):
    info=tasks.get(tid)
    if not info: await cq.answer("Task expired.",show_alert=True); return
    user=cq.from_user
    if user.id!=info["user_id"]: await cq.answer("This is not your task.",show_alert=True); return
    await cq.answer(); await cq.message.edit_text("📤 Sending all files…")
    base=Path(info["base_dir"]); files=info["files"]
    chat_id=cq.message.chat.id; reply_to=cq.message.id
    is_priv=cq.message.chat.type==enums.ChatType.PRIVATE; pinned=False
    if is_priv:
        try: await client.pin_chat_message(chat_id,reply_to); pinned=True
        except: pass
    async with GLOBAL_SEMAPHORE:
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
                    sent=await client.send_video(chat_id,str(full),caption=cap,thumb=thumb,
                        progress=progress_for_pyrogram,progress_args=(st,start_u,name,"to Telegram"),reply_to_message_id=reply_to)
                    try: await st.delete()
                    except: pass
                    if thumb: _safe_cleanup(thumb)
                else:
                    st=await client.send_message(chat_id,f"📤 {rel}",reply_to_message_id=reply_to)
                    start_u=time.time()
                    sent=await client.send_document(chat_id,str(full),caption=rel,
                        progress=progress_for_pyrogram,progress_args=(st,start_u,rel,"to Telegram"),reply_to_message_id=reply_to)
                    try: await st.delete()
                    except: pass
                # Immediately delete after sending to free disk
                _safe_cleanup(str(full))
                try: await log_output(client,user,sent,f"send_all {info.get('archive_name','?')}")
                except: pass
            except: pass
            await asyncio.sleep(0.4)
    if is_priv and pinned:
        try: await client.unpin_chat_message(chat_id,reply_to)
        except: pass
    await client.send_message(chat_id,"✅ All files sent!",reply_to_message_id=reply_to)
    tasks.pop(tid,None)

async def handle_send_one(client, cq, tid, index):
    info=tasks.get(tid)
    if not info: await cq.answer("Task expired.",show_alert=True); return
    user=cq.from_user
    if user.id!=info["user_id"]: await cq.answer("This is not your task.",show_alert=True); return
    files=info["files"]
    if index<0 or index>=len(files): await cq.answer("Invalid.",show_alert=True); return
    await cq.answer()
    base=Path(info["base_dir"]); rel=files[index]; full=base/rel
    if not full.is_file(): await cq.message.reply_text("File is missing."); return
    chat_id=cq.message.chat.id; reply_to=cq.message.id
    async with GLOBAL_SEMAPHORE:
        try:
            if is_video_path(rel):
                name=Path(rel).name; cap=await build_caption(user.id,name)
                thumb=await choose_thumbnail(user.id,str(full))
                st=await client.send_message(chat_id,f"📤 {name}",reply_to_message_id=reply_to)
                start_u=time.time()
                sent=await client.send_video(chat_id,str(full),caption=cap,thumb=thumb,
                    progress=progress_for_pyrogram,progress_args=(st,start_u,name,"to Telegram"),reply_to_message_id=reply_to)
                try: await st.delete()
                except: pass
                if thumb: _safe_cleanup(thumb)
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
        if cq: await cq.answer("No video found.",show_alert=True); return
    lock=get_lock(uid)
    if lock.locked():
        if cq: await cq.answer("A task is already running.",show_alert=True); return
    if cq: await cq.answer()
    dest_msg=(cq.message if cq else None) or reply_to_msg or msg
    if not await check_rate_limit(uid,dest_msg): return
    if not await _check_disk_space_ok(dest_msg): return
    async with lock:
        async with GLOBAL_SEMAPHORE:
            fname=video.file_name or "video.mp4"; base=os.path.splitext(fname)[0]
            temp_root=Path(Config.TEMP_DIR)/str(uid)/uuid.uuid4().hex
            temp_root.mkdir(parents=True,exist_ok=True)
            await register_temp_path(uid,str(temp_root),Config.AUTO_DELETE_DEFAULT_MIN)
            status=await dest_msg.reply_text("📥 Downloading video for audio extraction…"); start=time.time()
            try:
                dl=await client.download_media(video,file_name=str(temp_root),
                    progress=progress_for_pyrogram,progress_args=(status,start,fname,"to server"))
            except Exception as e: await status.edit_text(f"❌ Download failed:\n<code>{e}</code>"); _safe_cleanup(str(temp_root)); return
            audio_path=str(temp_root/f"{base}.m4a")
            try: await extract_audio(dl,audio_path)
            except Exception as e: await status.edit_text(f"❌ FFmpeg error:\n<code>{e}</code>"); _safe_cleanup(str(temp_root)); return
            # Delete source video immediately to free space
            _safe_cleanup(dl)
            await status.edit_text("📤 Uploading audio…"); start_u=time.time()
            try:
                sent=await client.send_audio(dest_msg.chat.id,audio_path,
                    caption=f"🎵 Extracted from: <b>{fname}</b>",title=base,performer="Serena Bot",
                    progress=progress_for_pyrogram,progress_args=(status,start_u,f"{base}.m4a","to Telegram"),
                    reply_to_message_id=dest_msg.id)
                try: await status.delete()
                except: pass
                _safe_cleanup(str(temp_root))
                try: await log_output(client,user,sent,"audio extracted")
                except: pass
            except Exception as e: await status.edit_text(f"❌ Upload failed:\n<code>{e}</code>")

# ════════════════════════════════════════════════════════════════════════════
# FILE INFO
# ════════════════════════════════════════════════════════════════════════════
async def _handle_file_info(client, dest, orig):
    media=orig.document or orig.video or orig.audio
    if not media: await dest.reply_text("No media found."); return
    uid=dest.from_user.id if dest.from_user else 0
    fname=media.file_name or "file"; size=getattr(media,"file_size",0) or 0
    status=await dest.reply_text("ℹ️ Fetching info…")
    if not await _check_disk_space_ok(status): return
    temp_root=Path(Config.TEMP_DIR)/str(uid)/uuid.uuid4().hex
    temp_root.mkdir(parents=True,exist_ok=True)
    await register_temp_path(uid,str(temp_root),Config.AUTO_DELETE_DEFAULT_MIN)
    async with GLOBAL_SEMAPHORE:
        try: dl=await client.download_media(media,file_name=str(temp_root))
        except Exception as e: await status.edit_text(f"❌ Download failed:\n<code>{e}</code>"); _safe_cleanup(str(temp_root)); return
        info=await get_media_info(dl)
    _safe_cleanup(str(temp_root))
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
    if info.get("subtitle_count"): txt+=f"🔤 {info['subtitle_count']} subtitle track(s)\n"
    await status.edit_text(txt)

# ════════════════════════════════════════════════════════════════════════════
# COMPRESS — No limits, multiple quality options
# ════════════════════════════════════════════════════════════════════════════
async def _trigger_compress(client, dest, orig, cid, mid, uid=None):
    media = orig.video or orig.document
    if not media or not is_video_file(media.file_name or ""):
        await dest.reply_text("This is not a video file."); return
    if not uid:
        uid = (dest.from_user.id if dest.from_user else None) or (orig.from_user.id if orig.from_user else 0)
    tid = uuid.uuid4().hex
    temp_root = Path(Config.TEMP_DIR) / str(uid) / tid
    temp_root.mkdir(parents=True, exist_ok=True)
    await register_temp_path(uid, str(temp_root), Config.AUTO_DELETE_DEFAULT_MIN)
    COMPRESS_TASKS[tid] = {
        "user_id": uid, "chat_id": cid, "msg_id": mid,
        "temp_root": str(temp_root), "fname": media.file_name or "video.mp4",
    }
    fsize_mb = (media.file_size or 0) / (1024 * 1024)
    fname_disp = media.file_name or "video"
    size_str = f"({fsize_mb:.1f} MB)" if fsize_mb > 0 else ""
    lines = [
        "<b>Video Tool</b>: <code>" + fname_disp + "</code> " + size_str,
        "",
        "Choose what you want to do:",
        "",
        "<b>1. Compress Only</b>",
        "   Reduces bitrate, keeps original resolution",
        "",
        "<b>2. Resize Only</b>",
        "   Scales down resolution — FASTEST method",
        "",
        "<b>3. Compress + Resize</b>",
        "   Best file size reduction (scale + re-encode)",
    ]
    await dest.reply_text(
        "\n".join(lines),
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("⚡ Compress Only",     callback_data="cmode|" + tid + "|compress")],
            [InlineKeyboardButton("📐 Resize Only",       callback_data="cmode|" + tid + "|resize")],
            [InlineKeyboardButton("🔥 Compress + Resize", callback_data="cmode|" + tid + "|both")],
            [InlineKeyboardButton("❌ Cancel",             callback_data="noop")],
        ])
    )




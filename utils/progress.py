# utils/progress.py — beautiful ETA display, throttled to 5 seconds
import asyncio
import os
import time
from typing import Dict, Optional, Tuple

from pyrogram.errors import FloodWait, MessageNotModified
from pyrogram.types import Message
from config import Config

_last_update: Dict[Tuple[int, int], float] = {}


def human_bytes(size: int) -> str:
    if size <= 0:
        return "0 B"
    size = float(size)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if size < 1024:
            return f"{size:.2f} {unit}"
        size /= 1024
    return f"{size:.2f} PB"


def human_time(seconds: int) -> str:
    if seconds <= 0:
        return "0s"
    d, rem = divmod(int(seconds), 86400)
    h, rem = divmod(rem, 3600)
    m, s = divmod(rem, 60)
    if d:
        return f"{d}d {h}h {m}m"
    if h:
        return f"{h}h {m}m {s}s"
    if m:
        return f"{m}m {s}s"
    return f"{s}s"


def _network_quality(speed_bps: float) -> str:
    mb = speed_bps / (1024 * 1024)
    if mb < 0.5:
        return "🐢 Slow"
    if mb < 3:
        return "📶 Normal"
    if mb < 10:
        return "⚡ Fast"
    return "🚀 Very Fast"


def _progress_key(message: Message) -> Tuple[int, int]:
    chat = getattr(message, "chat", None)
    return (int(getattr(chat, "id", 0) or 0), int(getattr(message, "id", 0) or 0))


def _update_interval() -> float:
    """Keep ETA edits at a safe 5-second minimum to avoid Telegram flood waits."""
    raw = os.getenv("ETA_UPDATE_INTERVAL", str(getattr(Config, "PROGRESS_UPDATE_INTERVAL", 5) or 5))
    try:
        val = float(raw)
    except Exception:
        val = 5.0
    # Never edit faster than every 5 seconds. Owners may raise the env value
    # if their bot still hits rate limits, but the default is exactly 5s.
    return max(5.0, val)


def _dot_bar(percent: float, width: int = 20) -> str:
    percent = max(0.0, min(100.0, percent))
    filled = int(round(width * percent / 100.0))
    return "●" * filled + "○" * (width - filled)


def _direction_icon(direction: str) -> str:
    d = (direction or "").lower()
    # "to server" is a download into the bot; "to Telegram" is an upload.
    if "telegram" in d or "upload" in d:
        return "📤"
    return "📥"


async def _safe_edit_msg(message: Message, text: str):
    """Edit text or caption depending on message type — handles errors gracefully."""
    try:
        if getattr(message, "animation", None) or getattr(message, "video", None):
            await message.edit_caption(text)
        else:
            await message.edit_text(text)
    except MessageNotModified:
        pass
    except FloodWait as e:
        await asyncio.sleep(min(int(getattr(e, "value", 5)), 10))
        try:
            if getattr(message, "animation", None) or getattr(message, "video", None):
                await message.edit_caption(text)
            else:
                await message.edit_text(text)
        except Exception:
            pass
    except Exception:
        pass


async def make_progress_message(client, chat_id: int, reply_to: int, text: str, thread_id: int = None) -> Optional[Message]:
    """
    Create progress status message.
    If PROGRESS_GIF is set → send animation with caption (GIF shows during download).
    Otherwise → plain text message.
    """
    gif = (Config.PROGRESS_GIF or "").strip()
    if gif:
        try:
            return await client.send_animation(
                chat_id, gif,
                caption=text,
                reply_to_message_id=reply_to,
                message_thread_id=thread_id,
            )
        except Exception:
            pass  # fallback to plain text
    try:
        return await client.send_message(
            chat_id, text,
            reply_to_message_id=reply_to,
            message_thread_id=thread_id,
        )
    except Exception:
        return None


async def progress_for_pyrogram(
    current: int,
    total: int,
    message: Message,
    start_time: float,
    file_name: str,
    direction: str = "Downloading",
    known_total: int = 0,    # pass known size when Telegram/HTTP total=0
):
    """
    Upload/download progress callback for Pyrogram and HTTP downloads.

    Display includes:
      • current size out of total size
      • filled/blank dot progress bar
      • percentage
      • network speed
      • remaining time / ETA
      • elapsed time

    Telegram edits are throttled to 5 seconds by default to avoid flood waits.
    """
    if not message:
        return

    now = time.time()
    key = _progress_key(message)
    last = _last_update.get(key, 0)

    actual_total = total if total and total > 0 else (known_total or 0)
    is_done = bool(actual_total and current >= actual_total) or (total and current == total)

    if now - last < _update_interval() and not is_done:
        return
    _last_update[key] = now

    elapsed = max(now - start_time, 0.001)
    speed = max(float(current) / elapsed, 0.0)  # bytes/sec

    if actual_total > 0:
        current = min(int(current), int(actual_total))
        percent = min((current * 100 / actual_total), 100.0)
        bar = _dot_bar(percent)
        remaining = max(actual_total - current, 0)
        eta = int(remaining / speed) if speed > 0 and remaining > 0 else 0
        size_str = f"{human_bytes(current)} / {human_bytes(actual_total)}"
        pct_str = f"{percent:.1f}%"
        eta_str = human_time(eta) if remaining > 0 else "done"
    else:
        percent = 0.0
        bar = _dot_bar(0)
        size_str = f"{human_bytes(current)} / detecting…"
        pct_str = "calculating…"
        eta_str = "calculating…"

    icon = _direction_icon(direction)
    title = direction or "Transferring"
    if title.lower() == "to server":
        title = "Downloading to server"
    elif title.lower() == "to telegram":
        title = "Uploading to Telegram"
    text = (
        f"{icon} <b>{title}</b>\n\n"
        f"📄 <code>{file_name}</code>\n"
        f"<code>[{bar}]</code>\n"
        f"📊 Progress : <b>{pct_str}</b>\n"
        f"📦 Size     : <b>{size_str}</b>\n"
        f"🚀 Speed    : <b>{human_bytes(int(speed))}/s</b>\n"
        f"⏳ ETA      : <b>{eta_str}</b>\n"
        f"⌛ Elapsed  : <b>{human_time(int(elapsed))}</b>\n"
        f"📶 Network  : <b>{_network_quality(speed)}</b>"
    )

    await _safe_edit_msg(message, text)

    if is_done:
        _last_update.pop(key, None)

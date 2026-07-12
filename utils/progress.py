# utils/progress.py — Fixed: no branding, handles total=0, PROGRESS_GIF support
import asyncio
import time
from typing import Dict, Optional

from pyrogram.errors import FloodWait, MessageNotModified
from pyrogram.types import Message
from config import Config

_last_update: Dict[int, float] = {}


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
    m, s = divmod(int(seconds), 60)
    h, m = divmod(m, 60)
    if h:
        return f"{h}h {m}m {s}s"
    if m:
        return f"{m}m {s}s"
    return f"{s}s"


def _network_quality(speed_bps: float) -> str:
    mb = speed_bps / (1024 * 1024)
    if mb < 0.5:  return "🐢 Slow"
    if mb < 3:    return "📶 Normal"
    if mb < 10:   return "⚡ Fast"
    return "🚀 Very Fast"


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
        await asyncio.sleep(min(e.value, 10))
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
    known_total: int = 0,    # ← pass finfo["size"] when Telegram total=0
):
    """
    Upload/download progress callback for Pyrogram.
    Handles total=0 (unknown file size) gracefully using known_total hint.
    No branding — clean progress display.
    """
    now    = time.time()
    msg_id = message.id
    last   = _last_update.get(msg_id, 0)

    # Throttle updates
    if now - last < float(Config.PROGRESS_UPDATE_INTERVAL) and current != total:
        return
    _last_update[msg_id] = now

    elapsed = max(now - start_time, 0.001)
    speed   = current / elapsed  # bytes/sec

    # Use known_total as fallback when Telegram gives 0
    actual_total = total if total > 0 else known_total

    if actual_total > 0:
        percent   = min((current * 100 / actual_total), 100.0)
        filled    = int(20 * percent / 100)
        bar       = "●" * filled + "○" * (20 - filled)   # dots style
        remaining = actual_total - current
        eta       = int(remaining / speed) if speed > 0 else 0
        size_str  = f"{human_bytes(current)} of {human_bytes(actual_total)}"
        pct_str   = f"{percent:.1f}%"
        eta_str   = human_time(eta)
    else:
        # Total unknown — empty dots indeterminate bar
        bar      = "○" * 20
        pct_str  = "..."
        size_str = human_bytes(current)
        eta_str  = "calculating..."

    icon = "📥" if "down" in direction.lower() else "📤"
    text = (
        f"{icon} <b>{direction}</b>\n\n"
        f"📄 <code>{file_name}</code>\n"
        f" [{bar}] \n"
        f"◌ Progress 😉 : 〘 {pct_str} 〙\n"
        f"✅ Done       : 〘 {size_str} 〙\n"
        f"🚀 Speed      : 〘 {human_bytes(int(speed))}/s 〙\n"
        f"⏳ ETA        : 〘 {eta_str} 〙\n"
        f"📶 Network    : {_network_quality(speed)}"
    )

    await _safe_edit_msg(message, text)

    if current == total and total > 0:
        _last_update.pop(msg_id, None)

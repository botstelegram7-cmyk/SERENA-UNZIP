# utils/progress.py
import time
from typing import Dict

from pyrogram.types import Message
from config import Config

_last_update: Dict[int, float] = {}


def human_bytes(size: int) -> str:
    if size == 0:
        return "0 B"
    size = float(size)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if size < 1024:
            return f"{size:.2f} {unit}"
        size /= 1024
    return f"{size:.2f} PB"


def human_time(seconds: int) -> str:
    if seconds < 0:
        seconds = 0
    m, s = divmod(seconds, 60)
    h, m = divmod(m, 60)
    if h:
        return f"{h}h {m}m {s}s"
    if m:
        return f"{m}m {s}s"
    return f"{s}s"


def _network_quality(speed_bps: float) -> str:
    mb = speed_bps / (1024 * 1024)
    if mb < 0.5:
        return "📶 Slow"
    if mb < 3:
        return "📶 Normal"
    if mb < 10:
        return "📶 Fast"
    return "📶 Very Fast"


async def progress_for_pyrogram(
    current: int,
    total: int,
    message: Message,
    start_time: float,
    file_name: str,
    direction: str = "to my server",
):
    now    = time.time()
    msg_id = message.id
    last   = _last_update.get(msg_id, 0)
    if now - last < Config.PROGRESS_UPDATE_INTERVAL and current != total:
        return
    _last_update[msg_id] = now

    percent = (current * 100 / total) if total > 0 else 0.0
    elapsed = max(now - start_time, 1e-3)
    speed   = current / elapsed
    eta     = int((total - current) / speed) if speed > 0 and total > 0 else 0

    filled = int(20 * percent / 100)
    bar    = "●" * filled + "○" * (20 - filled)

    text = (
        "➵⋆🪐ᴛᴇᴄʜɴɪᴄᴀʟ_sᴇʀᴇɴᴀ𓂃\n\n"
        f"📄 {file_name}\n"
        f"↔️ {direction}\n"
        f" [{bar}] \n"
        f"◌ Progress 😉 : 〘 {percent:.1f}% 〙\n"
        f"✅ Done       : 〘 {human_bytes(current)} of {human_bytes(total)} 〙\n"
        f"🚀 Speed      : 〘 {human_bytes(int(speed))}/s 〙\n"
        f"⏳ ETA        : 〘 {human_time(eta)} 〙\n"
        f"📶 Network    : {_network_quality(speed)}"
    )

    try:
        await message.edit_text(text)
    except Exception:
        pass

    if current == total:
        _last_update.pop(msg_id, None)

# utils/ytdl_tools.py
import asyncio
import json
import os
from pathlib import Path
from typing import Dict, List, Optional

from config import Config


def _write_cookie_file() -> Optional[str]:
    """Write INSTAGRAM_COOKIES env var (Netscape format) to a temp file."""
    content = Config.INSTAGRAM_COOKIES.strip()
    if not content:
        return None
    try:
        with open(Config.COOKIE_FILE_PATH, "w", encoding="utf-8") as f:
            if not content.startswith("# Netscape"):
                f.write("# Netscape HTTP Cookie File\n")
            f.write(content)
        return Config.COOKIE_FILE_PATH
    except Exception:
        return None


def _base_cmd(extra: Optional[List[str]] = None) -> List[str]:
    cmd = ["yt-dlp"]
    cookie_file = _write_cookie_file()
    if cookie_file:
        cmd += ["--cookies", cookie_file]
    if extra:
        cmd += extra
    return cmd


async def _run(cmd: List[str]) -> tuple:
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    out, err = await proc.communicate()
    return proc.returncode, out.decode(errors="ignore"), err.decode(errors="ignore")


async def get_video_info(url: str) -> Dict:
    """Fetch video metadata without downloading."""
    cmd = _base_cmd(["--dump-json", "--no-playlist", "--quiet", url])
    code, out, err = await _run(cmd)
    if code != 0:
        raise RuntimeError(err.strip()[:600] or "yt-dlp failed to fetch info")
    try:
        return json.loads(out)
    except Exception:
        raise RuntimeError("Could not parse video info")


async def get_formats(url: str) -> List[Dict]:
    """
    Return available video formats for quality selector.
    Each dict: { label, format_id, height, ext, size_approx }
    """
    try:
        info = await get_video_info(url)
    except Exception:
        return [{"label": "Best", "format_id": "best", "height": 0, "ext": "mp4", "size_approx": 0}]

    duration = info.get("duration", 0) or 0
    seen_h: set = set()
    formats: List[Dict] = []

    for f in info.get("formats", []):
        h = f.get("height")
        if not h or f.get("vcodec", "none") == "none":
            continue
        if h in seen_h:
            continue
        seen_h.add(h)
        tbr      = f.get("tbr") or f.get("vbr") or 0
        size_est = int(tbr * 1024 * duration / 8) if tbr and duration else 0
        formats.append({
            "label":       f"{h}p",
            "format_id":   f["format_id"],
            "height":      h,
            "ext":         f.get("ext", "mp4"),
            "size_approx": size_est,
        })

    formats.sort(key=lambda x: x["height"])

    # Audio-only option
    formats.append({
        "label":       "🎵 Audio Only",
        "format_id":   "bestaudio",
        "height":      0,
        "ext":         "m4a",
        "size_approx": 0,
    })

    if not formats:
        formats = [{"label": "Best", "format_id": "best", "height": 0, "ext": "mp4", "size_approx": 0}]

    return formats


async def download_video(
    url: str,
    output_dir: str,
    format_id: str = "best",
    height: int = 0,
) -> str:
    """
    Download video; returns path to downloaded file.
    """
    os.makedirs(output_dir, exist_ok=True)

    if format_id == "bestaudio" or height == 0 and format_id in ("bestaudio",):
        fmt = "bestaudio/best"
    elif format_id and format_id not in ("best",):
        fmt = f"{format_id}+bestaudio/{format_id}/best"
    elif height and height > 0:
        fmt = (
            f"bestvideo[height<={height}]+bestaudio/best[height<={height}]"
            f"/bestvideo[height<={height}]/best"
        )
    else:
        fmt = "bestvideo+bestaudio/best"

    output_tmpl = os.path.join(output_dir, "%(title).80s.%(ext)s")

    cmd = _base_cmd([
        "--format",               fmt,
        "--merge-output-format",  "mp4",
        "--output",               output_tmpl,
        "--no-playlist",
        "--no-warnings",
        "--quiet",
        url,
    ])

    # For audio-only
    if format_id == "bestaudio":
        cmd = _base_cmd([
            "--format",   "bestaudio/best",
            "--output",   output_tmpl.replace(".%(ext)s", ".%(ext)s"),
            "--no-playlist",
            "--no-warnings",
            "--quiet",
            url,
        ])

    code, _, err = await _run(cmd)
    if code != 0:
        raise RuntimeError(err.strip()[:600] or "yt-dlp download failed")

    # Find newest file in output_dir
    files = sorted(
        Path(output_dir).iterdir(),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    if not files:
        raise RuntimeError("No file was downloaded")
    return str(files[0])


def is_supported_url(url: str) -> bool:
    """Quick heuristic – returns True if yt-dlp is likely to support this URL."""
    blocked = [
        "youtube.com", "youtu.be",
        "pwvideo", "classplus", "unacademy", "byjus",
        "physicswallah", "vedantu", "toppr",
    ]
    u = url.lower()
    for b in blocked:
        if b in u:
            return False
    return True

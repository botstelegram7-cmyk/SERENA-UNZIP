# utils/ytdl_tools.py  ─ yt-dlp wrapper with Instagram fallback
import asyncio
import json
import os
import re
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from config import Config


def _write_cookie_file() -> Optional[str]:
    content = (Config.INSTAGRAM_COOKIES or "").strip()
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


def _cookie_file_exists() -> bool:
    return bool(Config.INSTAGRAM_COOKIES and Config.INSTAGRAM_COOKIES.strip())


async def _run(cmd: List[str], timeout: int = 300) -> Tuple[int, str, str]:
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        out, err = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        return proc.returncode, out.decode(errors="ignore"), err.decode(errors="ignore")
    except asyncio.TimeoutError:
        return -1, "", "Timeout: download took too long"
    except Exception as e:
        return -1, "", str(e)


def _build_cmd(url: str, extra_args: List[str], use_cookies: bool = True, use_impersonation: bool = True) -> List[str]:
    cmd = ["yt-dlp"]
    if use_cookies and _cookie_file_exists():
        cf = _write_cookie_file()
        if cf:
            cmd += ["--cookies", cf]
    if use_impersonation:
        cmd += [
            "--user-agent",
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
            "--add-header", "Accept-Language:en-US,en;q=0.9",
        ]
    cmd += [
        "--socket-timeout", "30",
        "--retries", "3",
        "--fragment-retries", "3",
        "--concurrent-fragments", "4",
        "--no-warnings",
    ]
    cmd += extra_args
    cmd.append(url)
    return cmd


async def get_video_info(url: str) -> Dict:
    base_args = ["--dump-json", "--no-playlist", "--quiet"]
    for use_cookies in (True, False):
        cmd = _build_cmd(url, base_args, use_cookies=use_cookies, use_impersonation=True)
        code, out, err = await _run(cmd, timeout=60)
        if code == 0 and out.strip():
            try:
                return json.loads(out)
            except Exception:
                pass
    raise RuntimeError(_clean_err(err) or "yt-dlp failed to fetch info")


async def get_formats(url: str) -> List[Dict]:
    try:
        info = await get_video_info(url)
    except Exception:
        return _generic_formats()

    duration = float(info.get("duration") or 0)
    seen_h: set = set()
    formats: List[Dict] = []

    for f in info.get("formats", []):
        h = f.get("height")
        if not h or f.get("vcodec", "none") == "none":
            continue
        if h in seen_h:
            continue
        seen_h.add(h)
        tbr = f.get("tbr") or f.get("vbr") or 0
        size_est = int(tbr * 1024 * duration / 8) if tbr and duration else 0
        formats.append({
            "label": f"{h}p",
            "format_id": f["format_id"],
            "height": h,
            "ext": f.get("ext", "mp4"),
            "size_approx": size_est,
        })

    formats.sort(key=lambda x: x["height"])
    formats.append({"label": "🎵 Audio Only", "format_id": "bestaudio", "height": 0, "ext": "m4a", "size_approx": 0})

    if not [f for f in formats if f["height"] > 0]:
        return _generic_formats()
    return formats


def _generic_formats() -> List[Dict]:
    return [
        {"label": "360p",          "format_id": "360p",      "height": 360,  "ext": "mp4", "size_approx": 0},
        {"label": "480p",          "format_id": "480p",      "height": 480,  "ext": "mp4", "size_approx": 0},
        {"label": "720p",          "format_id": "720p",      "height": 720,  "ext": "mp4", "size_approx": 0},
        {"label": "1080p",         "format_id": "1080p",     "height": 1080, "ext": "mp4", "size_approx": 0},
        {"label": "Best",          "format_id": "best",      "height": 0,    "ext": "mp4", "size_approx": 0},
        {"label": "🎵 Audio Only", "format_id": "bestaudio", "height": 0,    "ext": "m4a", "size_approx": 0},
    ]


async def download_video(url: str, output_dir: str, format_id: str = "best", height: int = 0) -> str:
    os.makedirs(output_dir, exist_ok=True)
    out_tmpl = os.path.join(output_dir, "%(title).80s.%(ext)s")

    if format_id == "bestaudio":
        fmt_str = "bestaudio/best"
    elif height and height > 0:
        fmt_str = (
            f"bestvideo[height<={height}]+bestaudio/best[height<={height}]"
            f"/bestvideo[height<={height}]/best"
        )
    elif format_id not in ("best", "bestaudio", ""):
        fmt_str = f"{format_id}+bestaudio/{format_id}/best"
    else:
        fmt_str = "bestvideo+bestaudio/best"

    if format_id == "bestaudio":
        base_args = ["--format", "bestaudio/best", "--output", out_tmpl, "--no-playlist", "--quiet"]
    else:
        base_args = [
            "--format", fmt_str,
            "--merge-output-format", "mp4",
            "--output", out_tmpl,
            "--no-playlist", "--quiet",
        ]

    last_err = ""
    # Strategy 1: with cookies + impersonation
    cmd = _build_cmd(url, base_args, use_cookies=True, use_impersonation=True)
    code, _, err = await _run(cmd, timeout=Config.YTDL_TIMEOUT_SEC)
    last_err = err

    if code != 0:
        # Strategy 2: no cookies, with impersonation
        cmd = _build_cmd(url, base_args, use_cookies=False, use_impersonation=True)
        code, _, err = await _run(cmd, timeout=Config.YTDL_TIMEOUT_SEC)
        last_err = err

    if code != 0:
        # Strategy 3: bare minimum
        bare = ["--format", "best", "--output", out_tmpl, "--no-playlist", "--quiet"]
        cmd = _build_cmd(url, bare, use_cookies=False, use_impersonation=False)
        code, _, err = await _run(cmd, timeout=Config.YTDL_TIMEOUT_SEC)
        last_err = err

    if code != 0:
        raise RuntimeError(_clean_err(last_err) or "yt-dlp download failed after all retries")

    return _latest_file(output_dir)


def _latest_file(directory: str) -> str:
    files = [p for p in Path(directory).iterdir() if p.is_file()]
    if not files:
        raise RuntimeError("No file was downloaded")
    return str(max(files, key=lambda p: p.stat().st_mtime))


def _clean_err(err: str) -> str:
    err = err.strip()
    err = re.sub(r'\x1b\[[0-9;]*m', '', err)
    lines = [l.strip() for l in err.splitlines() if l.strip() and not l.strip().startswith("[")]
    if lines:
        return lines[-1][:400]
    return err[:400]


def is_supported_url(url: str) -> bool:
    blocked_keywords = [
        "youtube.com", "youtu.be",
        "classplus.co", "classplusapp", "cpapp.live",
        "unacademy.com", "byjus.com",
        "physicswallah.live", "pw.live",
        "vedantu.com", "toppr.com",
        "doubtnut.com", "extramarks.com",
    ]
    u = url.lower()
    return not any(b in u for b in blocked_keywords)


def get_site_name(url: str) -> str:
    mapping = {
        "instagram.com": "Instagram", "twitter.com": "Twitter/X", "x.com": "Twitter/X",
        "facebook.com": "Facebook", "fb.watch": "Facebook", "tiktok.com": "TikTok",
        "vimeo.com": "Vimeo", "dailymotion.com": "Dailymotion", "reddit.com": "Reddit",
        "twitch.tv": "Twitch", "bilibili.com": "Bilibili", "ok.ru": "OK.ru",
        "vk.com": "VK", "pinterest.com": "Pinterest", "rumble.com": "Rumble",
        "streamable.com": "Streamable",
    }
    u = url.lower()
    for domain, name in mapping.items():
        if domain in u:
            return name
    return "Video"

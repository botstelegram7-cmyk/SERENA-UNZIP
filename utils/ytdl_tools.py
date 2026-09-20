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
    # Render / Railway env vars store newlines as literal \n — fix them
    if "\\n" in content:
        content = content.replace("\\n", "\n")
    try:
        with open(Config.COOKIE_FILE_PATH, "w", encoding="utf-8") as f:
            if not content.startswith("# Netscape"):
                f.write("# Netscape HTTP Cookie File\n")
            f.write(content)
            if not content.endswith("\n"):
                f.write("\n")
        return Config.COOKIE_FILE_PATH
    except Exception:
        return None


def _cookie_file_exists() -> bool:
    return bool(Config.INSTAGRAM_COOKIES and Config.INSTAGRAM_COOKIES.strip())


def write_youtube_cookie_file() -> Optional[str]:
    """Materialise YOUTUBE_COOKIES into a Netscape cookies.txt for yt-dlp.

    YouTube increasingly serves datacenter IPs a "Sign in to confirm you're
    not a bot" challenge. Cookies exported from a signed-in browser clear
    it, so this is the single most effective YouTube fix available.
    """
    content = (Config.YOUTUBE_COOKIES or "").strip()
    if not content:
        return None
    # Hosting panels commonly store newlines as the literal two characters \n
    if "\\n" in content and "\n" not in content:
        content = content.replace("\\n", "\n")
    try:
        with open(Config.YOUTUBE_COOKIE_FILE, "w", encoding="utf-8") as f:
            if not content.lstrip().startswith("# Netscape"):
                f.write("# Netscape HTTP Cookie File\n")
            f.write(content)
            if not content.endswith("\n"):
                f.write("\n")
        return Config.YOUTUBE_COOKIE_FILE
    except Exception:
        return None


def has_youtube_cookies() -> bool:
    return bool((Config.YOUTUBE_COOKIES or "").strip())


def _is_youtube(url: str) -> bool:
    u = (url or "").lower()
    return any(h in u for h in ("youtube.com", "youtu.be", "youtube-nocookie.com"))


def ytdl_network_args(url: str = "") -> List[str]:
    """Proxy and cookie flags shared by every yt-dlp invocation."""
    args: List[str] = []
    if Config.YTDL_PROXY:
        args += ["--proxy", Config.YTDL_PROXY]
    if _is_youtube(url) and has_youtube_cookies():
        cf = write_youtube_cookie_file()
        if cf:
            args += ["--cookies", cf]
    return args


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
    # YouTube gets its own cookie jar; everything else uses the Instagram one
    if _is_youtube(url) and has_youtube_cookies():
        cf = write_youtube_cookie_file()
        if cf:
            cmd += ["--cookies", cf]
    elif use_cookies and _cookie_file_exists():
        cf = _write_cookie_file()
        if cf:
            cmd += ["--cookies", cf]
    if Config.YTDL_PROXY:
        cmd += ["--proxy", Config.YTDL_PROXY]
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
    # ── Instagram: probe the post first so we never show a bogus menu ──
    if _is_instagram_url(url):
        return await _instagram_formats(url)

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


async def _instagram_formats(url: str) -> List[Dict]:
    """Instagram no longer shows a quality menu — bot.py downloads directly.

    Kept so any legacy callback path still resolves to the photo handler.
    """
    return [{"label": "📥 Download", "format_id": "insta_photo",
             "height": 0, "ext": "mp4", "size_approx": 0}]


def _instagram_photo_formats() -> List[Dict]:
    """For Instagram photo posts/carousels — no quality selection needed."""
    return [
        {"label": "📸 Best Quality (Auto)", "format_id": "insta_photo", "height": 0, "ext": "jpg", "size_approx": 0},
    ]


def _generic_formats() -> List[Dict]:
    return [
        {"label": "360p",          "format_id": "360p",      "height": 360,  "ext": "mp4", "size_approx": 0},
        {"label": "480p",          "format_id": "480p",      "height": 480,  "ext": "mp4", "size_approx": 0},
        {"label": "720p",          "format_id": "720p",      "height": 720,  "ext": "mp4", "size_approx": 0},
        {"label": "1080p",         "format_id": "1080p",     "height": 1080, "ext": "mp4", "size_approx": 0},
        {"label": "Best",          "format_id": "best",      "height": 0,    "ext": "mp4", "size_approx": 0},
        {"label": "🎵 Audio Only", "format_id": "bestaudio", "height": 0,    "ext": "m4a", "size_approx": 0},
    ]



def _normalize_instagram_url(url: str) -> str:
    from utils.instagram import normalize_url
    return normalize_url(url)


def _is_instagram_url(url: str) -> bool:
    from utils.instagram import is_instagram_url
    return is_instagram_url(url)


async def _download_instagram_photos(url: str, output_dir: str) -> List[str]:
    """Instagram downloader — delegates to the dedicated utils.instagram module.

    Kept as a thin wrapper so existing imports keep working. The real logic
    (GraphQL / API v1 / embed / OpenGraph / yt-dlp chain, full carousel
    support, high-res images) lives in utils/instagram.py.
    """
    from utils.instagram import download_instagram
    return await download_instagram(url, output_dir)


async def download_video(url: str, output_dir: str, format_id: str = "best", height: int = 0) -> str:
    os.makedirs(output_dir, exist_ok=True)
    out_tmpl = os.path.join(output_dir, "%(title).80s.%(ext)s")

    url = _normalize_instagram_url(url)  # handle encoded highlight URLs
    is_insta = _is_instagram_url(url)

    if format_id == "bestaudio":
        fmt_str = "bestaudio/best"
    elif is_insta:
        # Instagram reels FIX:
        # "best[ext=mp4]" = pre-merged progressive MP4 stream with BOTH audio+video
        # This avoids the bestvideo (video-only DASH) + bestaudio merge issue
        # Fallback: bestvideo+bestaudio (requires ffmpeg), then plain best
        if height and height > 0:
            fmt_str = (
                f"best[ext=mp4][height<={height}][vcodec!=none][acodec!=none]"
                f"/best[height<={height}][ext=mp4]"
                f"/bestvideo[height<={height}]+bestaudio"
                f"/best[height<={height}]/best"
            )
        else:
            fmt_str = (
                "best[ext=mp4][vcodec!=none][acodec!=none]"
                "/bestvideo+bestaudio"
                "/best[ext=mp4]/best"
            )
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


# Paid course platforms. These stay blocked deliberately: their content sits
# behind a paywall, so pulling it down is straightforward piracy. YouTube was
# previously lumped in with them, which was the wrong call - it serves public
# content and yt-dlp supports it - so it has been removed from this list.
BLOCKED_PLATFORMS = [
    "classplus.co", "classplusapp", "cpapp.live",
    "unacademy.com", "byjus.com",
    "physicswallah.live", "pw.live",
    "vedantu.com", "toppr.com",
    "doubtnut.com", "extramarks.com",
]


def is_supported_url(url: str) -> bool:
    u = (url or "").lower()
    return not any(b in u for b in BLOCKED_PLATFORMS)


def youtube_block_help() -> str:
    """Guidance for YouTube's bot-detection wall.

    Seen as either "Sign in to confirm you're not a bot" or "Failed to
    extract any player response" - the same refusal, worded differently
    depending on which extraction path was tried.
    """
    if has_youtube_cookies():
        return (
            "<b>YouTube refused this download.</b>\n\n"
            "Cookies are configured but were still rejected. Either they have "
            "expired, or this address is flagged regardless of the session.\n\n"
            "<i>Export fresh cookies from a signed-in browser, or set "
            "<code>YTDL_PROXY</code>.</i>")
    return (
        "<b>YouTube refused this download.</b>\n\n"
        "It asks hosted servers to confirm they are not a bot. The link is "
        "fine - the address making the request is the problem.\n\n"
        "<i>Operator: set <code>YOUTUBE_COOKIES</code> to a cookies.txt "
        "export from a signed-in browser. See <code>/limits</code>.</i>")


def blocked_reason(url: str) -> str:
    """Explain a refusal, so it does not look like a malfunction."""
    u = (url or "").lower()
    for b in BLOCKED_PLATFORMS:
        if b in u:
            return ("<b>This platform is not supported.</b>\n\n"
                    "It hosts paid course material, and downloading it would "
                    "bypass the paywall. That is deliberate, not a fault.")
    return ""


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


# ── Direct download URL detection ─────────────────────────────────────────────
_DIRECT_EXTS = {
    ".mp4",".mkv",".avi",".mov",".webm",".flv",".ts",
    ".mp3",".m4a",".aac",".flac",".ogg",".opus",".wav",
    ".zip",".rar",".7z",".tar",".gz",".pdf",
    ".jpg",".jpeg",".png",".gif",".webp",
    ".apk",".exe",".dmg",".iso",
}
_DIRECT_PATTERNS = [
    "drive.usercontent.google.com/download",
    "drive.google.com/uc?export=download",
    "?dl=1",         # Dropbox
    "?download=1",
    "/download?",
    "cdn.discordapp.com/attachments",
    "media.discordapp.net/attachments",
    "github.com/releases/download",
    "objects.githubusercontent.com",
]

# Hosts that always serve a file rather than a page.
_DIRECT_HOSTS = (
    "drive.usercontent.google.com",
    "drive.google.com/uc",
    "cdn.discordapp.com/attachments",
    "github.com/releases/download",
    "dl.dropboxusercontent.com",
)


def is_direct_download_url(url: str) -> bool:
    """True if URL is a direct file link (not a webpage to scrape).

    NOTE: this used to be defined twice in this module; the later copy
    silently shadowed the earlier one. The two are now merged here.
    """
    u = url.lower()
    base = u.split("?")[0].split("#")[0]
    for ext in _DIRECT_EXTS:
        if base.endswith(ext):
            return True
    for host in _DIRECT_HOSTS:
        if host in u:
            return True
    for pat in _DIRECT_PATTERNS:
        if pat in u:
            return True
    return False

async def download_direct(url: str, output_dir: str) -> str:
    import re as _re, aiohttp
    from urllib.parse import urlparse, unquote as uq
    os.makedirs(output_dir, exist_ok=True)
    hdrs = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)", "Accept": "*/*"}
    async with aiohttp.ClientSession(headers=hdrs) as s:
        async with s.get(url, allow_redirects=True,
                         timeout=aiohttp.ClientTimeout(total=600)) as r:
            if r.status not in (200, 206):
                raise RuntimeError(f"HTTP {r.status}: {url}")
            cd = r.headers.get("Content-Disposition", "")
            fname = None
            m = _re.search(r"filename[*]\s*=\s*[^']*'[^']*'([^;\r\n]+)", cd, _re.I)
            if m: fname = uq(m.group(1)).strip().strip("'\"")
            if not fname:
                m2 = _re.search(r'filename\s*=\s*"?([^";\r\n]+)"?', cd, _re.I)
                if m2: fname = m2.group(1).strip().strip("'\"")
            if not fname:
                fname = uq(Path(urlparse(url).path).name) or "download"
            ct = r.headers.get("Content-Type", "")
            if "." not in fname:
                for ck, ce in [("video/mp4",".mp4"),("audio/mpeg",".mp3"),
                                ("application/zip",".zip"),("application/pdf",".pdf"),
                                ("image/jpeg",".jpg"),("image/png",".png")]:
                    if ck in ct: fname += ce; break
            out = os.path.join(output_dir, fname)
            with open(out, "wb") as fh:
                async for chunk in r.content.iter_chunked(65536):
                    fh.write(chunk)
    return out


async def search_and_download_audio(query: str, output_dir: str) -> str:
    os.makedirs(output_dir, exist_ok=True)
    out_tmpl = os.path.join(output_dir, "%(title).80s.%(ext)s")
    cmd = [
        "yt-dlp", "ytsearch1:" + query,
        *ytdl_network_args("https://www.youtube.com/"),
        "--format", "bestaudio/best",
        "--extract-audio", "--audio-format", "mp3",
        "--audio-quality", "192K",
        "--embed-thumbnail", "--add-metadata",
        "--output", out_tmpl, "--no-playlist", "--quiet",
    ]
    ret, _, err = await _run(cmd, timeout=180)
    if ret != 0:
        raise RuntimeError(_clean_err(err) or "Song not found")
    files = sorted(
        [p for p in Path(output_dir).iterdir()
         if p.suffix.lower() in (".mp3",".m4a",".ogg",".aac",".flac")],
        key=lambda p: p.stat().st_mtime,
    )
    if not files:
        raise RuntimeError("Audio file not found after download")
    return str(files[-1])

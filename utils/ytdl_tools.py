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
        # For Instagram, if info fetch fails, return photo download option
        if _is_instagram_url(url):
            return _instagram_photo_formats()
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
        # No video formats found
        if _is_instagram_url(url):
            # Likely a photo post — offer photo download instead
            return _instagram_photo_formats()
        return _generic_formats()
    return formats


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
    """Convert encoded/shared Instagram URLs to standard format.
    
    Example: https://www.instagram.com/s/aGlnaGxpZ2h0OjE3ODY1NTYwMDE4OTk4NzU3
    Decodes to: highlight:17865560018998757
    Returns:    https://www.instagram.com/stories/highlights/17865560018998757/
    """
    if "/s/" not in url:
        return url
    try:
        import base64, re
        m = re.search(r"/s/([A-Za-z0-9_\-]+)", url)
        if not m:
            return url
        b64 = m.group(1).replace("-", "+").replace("_", "/")
        # Add padding
        b64 += "=" * (4 - len(b64) % 4)
        decoded = base64.b64decode(b64).decode("utf-8", errors="ignore")
        if decoded.startswith("highlight:"):
            hid = decoded.split(":", 1)[1]
            return f"https://www.instagram.com/stories/highlights/{hid}/"
    except Exception:
        pass
    return url

def _is_instagram_url(url: str) -> bool:
    return "instagram.com" in url.lower() or "instagr.am" in url.lower()


async def _download_instagram_photos(url: str, output_dir: str) -> List[str]:
    """Download Instagram post — photos, carousel, reels, stories, highlights.

    Priority order:
    1. --write-thumbnail --skip-download  → gets actual photo without video error
    2. JSON dump → CDN URL direct download → works for public carousels  
    3. Normal yt-dlp download (for videos/reels)
    4. Cookie-less retry
    """
    url = _normalize_instagram_url(url)  # decode encoded highlight/story URLs
    """Original docstring:
    
    Strategy (in order):
    1. yt-dlp --dump-json → extract image URLs from JSON → direct HTTP download
       (MOST reliable for photo posts — avoids "No video formats" error entirely)
    2. yt-dlp normal download with cookies (videos, reels, stories)
    3. yt-dlp without cookies (public posts)
    4. Clear error for login-required content
    """
    import json as _json, urllib.request as _req
    os.makedirs(output_dir, exist_ok=True)
    media_exts  = {".jpg", ".jpeg", ".png", ".webp", ".mp4", ".mov", ".mkv", ".m4v"}
    image_exts  = {".jpg", ".jpeg", ".png", ".webp"}
    out_tmpl    = os.path.join(output_dir, "%(id)s_%(autonumber)02d.%(ext)s")
    thumb_tmpl  = os.path.join(output_dir, "%(id)s_%(autonumber)02d.%(ext)s")

    def _collect_files(exts=None):
        if not Path(output_dir).exists(): return []
        check = exts or media_exts
        return sorted(
            [str(p) for p in Path(output_dir).iterdir()
             if p.is_file() and p.suffix.lower() in check],
            key=os.path.getmtime
        )

    def _dl_image(img_url: str, idx: int) -> str:
        """Download image from CDN URL directly."""
        ext = ".jpg"
        for e in (".png", ".webp"):
            if e in img_url: ext = e; break
        img_path = os.path.join(output_dir, f"photo_{idx:02d}{ext}")
        headers = {
            "User-Agent": "Mozilla/5.0 (iPhone; CPU iPhone OS 14_0 like Mac OS X) "
                          "AppleWebKit/605.1.15 (KHTML, like Gecko) Mobile/15E148 "
                          "Instagram/222.0.0.17.114",
            "Referer": "https://www.instagram.com/",
            "Accept": "image/avif,image/webp,image/apng,*/*;q=0.8",
        }
        req = _req.Request(img_url, headers=headers)
        with _req.urlopen(req, timeout=30) as r:
            with open(img_path, "wb") as f:
                f.write(r.read())
        return img_path

    def _extract_images_from_json(data: dict) -> list:
        """Pull all image/thumbnail URLs from yt-dlp info JSON."""
        urls = []
        entries = data.get("entries") or []
        if entries:
            for entry in entries:
                # For carousel: each entry is a photo
                for key in ("url", "thumbnail", "display_url"):
                    u = entry.get(key, "")
                    if u and any(x in u for x in ("cdninstagram", "fbcdn", ".jpg", ".jpeg", ".png", ".webp")):
                        urls.append(u); break
        else:
            for key in ("url", "thumbnail", "display_url"):
                u = data.get(key, "")
                if u and any(x in u for x in ("cdninstagram", "fbcdn", ".jpg", ".jpeg", ".png", ".webp")):
                    urls.append(u); break
        return urls

    last_err = ""

    # ── Strategy 0: --write-thumbnail --skip-download → gets actual photo ──
    # This avoids "There is no video in this post" completely
    thumb_args = [
        "--write-thumbnail", "--skip-download",
        "--ignore-errors",
        "--convert-thumbnails", "jpg",
        "--output", out_tmpl,
        "--no-warnings",
    ]
    cmd_t = _build_cmd(url, thumb_args, use_cookies=True, use_impersonation=True)
    ret_t, _, _ = await _run(cmd_t, timeout=60)
    if ret_t == 0:
        files_t = _collect_files(image_exts)
        if files_t: return files_t

    # Try without cookies (public posts)
    cmd_t = _build_cmd(url, thumb_args, use_cookies=False, use_impersonation=True)
    ret_t, _, _ = await _run(cmd_t, timeout=60)
    if ret_t == 0:
        files_t = _collect_files(image_exts)
        if files_t: return files_t

    # ── Strategy 1: Get JSON info → try direct image download (photo posts) ──
    info_args = ["--dump-single-json", "--yes-playlist", "--no-warnings", "--quiet"]
    cmd_info = _build_cmd(url, info_args, use_cookies=True, use_impersonation=True)
    _, info_out, _ = await _run(cmd_info, timeout=40)
    if not info_out.strip():
        cmd_info = _build_cmd(url, info_args, use_cookies=False, use_impersonation=True)
        _, info_out, _ = await _run(cmd_info, timeout=40)
    if info_out.strip():
        try:
            info_data = _json.loads(info_out.strip())
            img_urls = _extract_images_from_json(info_data)
            if img_urls:
                saved = []
                for idx, img_url in enumerate(img_urls, 1):
                    try: saved.append(_dl_image(img_url, idx))
                    except Exception: continue
                if saved: return saved
        except Exception:
            pass

    # ── Strategy 2: yt-dlp with explicit video format + cookies ──
    # For highlights: force video download with audio
    is_highlight = "highlights" in url or "stories" in url
    vid_fmt = (
        # Highlights use HLS streams — force ffmpeg merge for audio+video
        "bestvideo[ext=mp4]+bestaudio[ext=m4a]/bestvideo+bestaudio/best[ext=mp4]/best"
        if is_highlight else "best"
    )
    base_args = [
        "--format", vid_fmt,
        "--merge-output-format", "mp4",
        "--hls-prefer-ffmpeg",
        "--output", out_tmpl,
        "--yes-playlist", "--no-warnings", "--quiet",
    ]
    cmd = _build_cmd(url, base_args, use_cookies=True, use_impersonation=True)
    ret, _, err1 = await _run(cmd, timeout=300)
    last_err = err1
    if ret == 0:
        files = _collect_files()
        # Verify downloaded files are actual videos (not blank thumbnails)
        video_files = [f for f in files if Path(f).suffix.lower() in {".mp4",".mov",".mkv",".m4v"}]
        if video_files: return video_files
        if files: return files   # photos/images also OK

    # ── Strategy 3: yt-dlp without cookies (public posts) ──
    cmd = _build_cmd(url, base_args, use_cookies=False, use_impersonation=True)
    ret, _, err1 = await _run(cmd, timeout=300)
    if err1: last_err = err1
    if ret == 0:
        files = _collect_files()
        if files: return files

    # ── Classify error for helpful message ──
    err_lower = last_err.lower()
    is_login_err = any(k in err_lower for k in ("log in", "login", "cookies", "authentication", "private"))

    if is_login_err:
        has_cookies = _cookie_file_exists()
        if has_cookies:
            raise RuntimeError(
                "🔒 Login required.\n\n"
                "Cookies set hain — lekin shayad:\n"
                "• Session expire ho gayi ho\n"
                "• Account se Instagram ne logout kar diya ho\n\n"
                "✅ <b>Fix:</b> Browser se nayi fresh cookies export karo aur "
                "INSTAGRAM_COOKIES env variable update karo."
            )
        raise RuntimeError(
            "🔒 Login required (Stories/Highlights private hain).\n\n"
            "✅ <b>Fix:</b> INSTAGRAM_COOKIES env variable mein Netscape format "
            "mein cookies paste karo."
        )

    raise RuntimeError(_clean_err(last_err) or "Instagram download failed — try again later")


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

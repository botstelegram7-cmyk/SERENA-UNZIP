# utils/instagram.py
"""
Dedicated Instagram downloader for Serena Unzip Bot.

Why this module exists
──────────────────────
yt-dlp alone is unreliable for Instagram *photos*:
  • Photo posts raise "There is no video in this post"
  • Carousels (multi-image posts) only return the first item
  • `--write-thumbnail` gives a low-res crop, not the real image
  • Instagram now answers anonymous datacenter IPs with 401 / 429

This module talks to Instagram's own web endpoints directly and always
returns EVERY media item of a post (photos + videos, in order, full-res).

Strategy chain (first success wins):
  1. Web GraphQL  (`/graphql/query`, doc_id)     → full carousel, needs csrf
  2. Web API v1   (`/api/v1/media/<id>/info/`)   → full carousel
  3. Embed page   (`/p/<code>/embed/captioned/`) → single image, no auth
  4. OpenGraph    (`og:image` / `og:video`)      → last-resort single item
  5. yt-dlp       (video/reel/story fallback)

Cookies (Config.INSTAGRAM_COOKIES, Netscape format) are used when present
and make every strategy far more reliable — plus they unlock stories,
highlights and private-to-you accounts.
"""

from __future__ import annotations

import asyncio
import html
import json
import os
import re
import time
from http.cookies import SimpleCookie
from pathlib import Path
from typing import Dict, List, Optional, Tuple
from urllib.parse import urlparse

import aiohttp

from config import Config

# ── Constants ────────────────────────────────────────────────────────────────

DESKTOP_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"
)
MOBILE_UA = (
    "Mozilla/5.0 (iPhone; CPU iPhone OS 17_4 like Mac OS X) "
    "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.4 Mobile/15E148 Safari/604.1"
)
IG_APP_ID = "936619743392459"

# Public doc_ids for PolarisPostActionLoadPostQueryQuery — tried in order,
# Instagram rotates them so we keep a few known-good ones.
GRAPHQL_DOC_IDS = [
    "8845758582119845",
    "10015901848480474",
    "9510064595728286",
    "7950326061742207",
]

IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".webp", ".heic"}
VIDEO_EXTS = {".mp4", ".mov", ".mkv", ".m4v", ".webm"}

_SHORTCODE_ALPHABET = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_"

# Reused across calls so Instagram sees a stable "browser"
_SESSION_CACHE: Dict[str, object] = {"csrf": None, "ts": 0.0}


class InstagramError(RuntimeError):
    """Raised with a user-friendly, Hinglish message."""


# ── URL helpers ──────────────────────────────────────────────────────────────

def is_instagram_url(url: str) -> bool:
    u = (url or "").lower()
    return "instagram.com" in u or "instagr.am" in u or "ddinstagram.com" in u


def normalize_url(url: str) -> str:
    """Clean tracking params and decode share (`/s/...`) links."""
    url = (url or "").strip()
    url = url.split("?")[0].split("#")[0].rstrip("/")
    url = url.replace("ddinstagram.com", "instagram.com")
    url = url.replace("instagr.am", "instagram.com")

    # Encoded highlight / story share links:  /s/<base64>
    m = re.search(r"/s/([A-Za-z0-9_\-]+)", url)
    if m:
        try:
            import base64

            b64 = m.group(1).replace("-", "+").replace("_", "/")
            b64 += "=" * (-len(b64) % 4)
            decoded = base64.b64decode(b64).decode("utf-8", "ignore")
            if decoded.startswith("highlight:"):
                return f"https://www.instagram.com/stories/highlights/{decoded.split(':', 1)[1]}/"
            if decoded.startswith("story:"):
                parts = decoded.split(":")
                if len(parts) >= 2:
                    return f"https://www.instagram.com/stories/{parts[1]}/"
        except Exception:
            pass
    return url


def extract_shortcode(url: str) -> Optional[str]:
    """Get the post shortcode from /p/, /reel/, /reels/, /tv/ URLs."""
    m = re.search(r"instagram\.com/(?:[^/]+/)?(?:p|reel|reels|tv)/([A-Za-z0-9_\-]+)", url)
    return m.group(1) if m else None


def shortcode_to_media_id(shortcode: str) -> Optional[int]:
    """Base64-ish shortcode → numeric media id (used by the v1 API)."""
    try:
        n = 0
        for ch in shortcode:
            n = n * 64 + _SHORTCODE_ALPHABET.index(ch)
        return n
    except ValueError:
        return None


def content_kind(url: str) -> str:
    """'post' | 'reel' | 'story' | 'highlight' | 'profile' | 'unknown'"""
    u = url.lower()
    if "/stories/highlights/" in u:
        return "highlight"
    if "/stories/" in u:
        return "story"
    if "/reel" in u:
        return "reel"
    if re.search(r"/(p|tv)/", u):
        return "post"
    if re.match(r"^https?://(www\.)?instagram\.com/[A-Za-z0-9_.]+/?$", url):
        return "profile"
    return "unknown"


# ── Cookie handling ──────────────────────────────────────────────────────────

def write_cookie_file() -> Optional[str]:
    """Materialise Config.INSTAGRAM_COOKIES into a Netscape file for yt-dlp."""
    content = (Config.INSTAGRAM_COOKIES or "").strip()
    if not content:
        return None
    if "\\n" in content and "\n" not in content:
        content = content.replace("\\n", "\n")
    try:
        path = Config.COOKIE_FILE_PATH
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            if not content.lstrip().startswith("# Netscape"):
                f.write("# Netscape HTTP Cookie File\n")
            f.write(content)
            if not content.endswith("\n"):
                f.write("\n")
        return path
    except Exception:
        return None


def has_cookies() -> bool:
    return bool((Config.INSTAGRAM_COOKIES or "").strip())


def cookies_as_dict() -> Dict[str, str]:
    """Parse Netscape-format OR 'k=v; k=v' cookie string into a dict."""
    raw = (Config.INSTAGRAM_COOKIES or "").strip()
    if not raw:
        return {}
    if "\\n" in raw and "\n" not in raw:
        raw = raw.replace("\\n", "\n")

    jar: Dict[str, str] = {}

    # Netscape TSV format: domain  flag  path  secure  expiry  name  value
    for line in raw.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split("\t")
        if len(parts) >= 7:
            jar[parts[5].strip()] = parts[6].strip()

    # Header format fallback
    if not jar and "=" in raw:
        try:
            c = SimpleCookie()
            c.load(raw.replace("\n", " "))
            jar = {k: v.value for k, v in c.items()}
        except Exception:
            for piece in raw.replace("\n", ";").split(";"):
                if "=" in piece:
                    k, v = piece.split("=", 1)
                    jar[k.strip()] = v.strip()

    return {k: v for k, v in jar.items() if k and v}


def _base_headers(mobile: bool = False) -> Dict[str, str]:
    return {
        "User-Agent": MOBILE_UA if mobile else DESKTOP_UA,
        "Accept": "*/*",
        "Accept-Language": "en-US,en;q=0.9",
        "X-IG-App-ID": IG_APP_ID,
        "X-ASBD-ID": "129477",
        "X-Requested-With": "XMLHttpRequest",
        "Referer": "https://www.instagram.com/",
        "Origin": "https://www.instagram.com",
        "Sec-Fetch-Site": "same-origin",
        "Sec-Fetch-Mode": "cors",
        "Sec-Fetch-Dest": "empty",
    }


# ── Media item model ─────────────────────────────────────────────────────────

def _item(url: str, is_video: bool, idx: int, thumb: str = "") -> Dict:
    return {"url": url, "is_video": bool(is_video), "index": idx, "thumbnail": thumb}


def _pick_best(candidates: List[Dict]) -> Optional[str]:
    """Choose the highest resolution entry from an Instagram *_versions list."""
    if not candidates:
        return None
    try:
        best = max(candidates, key=lambda c: (c.get("width") or 0) * (c.get("height") or 0))
    except Exception:
        best = candidates[0]
    return best.get("url")


def _parse_v1_media(node: Dict) -> List[Dict]:
    """Parse a v1 API media object (handles carousel_media)."""
    out: List[Dict] = []

    def one(n: Dict, idx: int):
        vids = n.get("video_versions") or []
        if vids:
            url = _pick_best(vids)
            if url:
                thumb = _pick_best((n.get("image_versions2") or {}).get("candidates") or []) or ""
                out.append(_item(url, True, idx, thumb))
                return
        imgs = (n.get("image_versions2") or {}).get("candidates") or []
        url = _pick_best(imgs)
        if url:
            out.append(_item(url, False, idx))

    carousel = node.get("carousel_media") or []
    if carousel:
        for i, child in enumerate(carousel, 1):
            one(child, i)
    else:
        one(node, 1)
    return out


def _parse_graphql_media(node: Dict) -> List[Dict]:
    """Parse a GraphQL shortcode_media node (handles edge_sidecar_to_children)."""
    out: List[Dict] = []

    def one(n: Dict, idx: int):
        if n.get("is_video") and n.get("video_url"):
            out.append(_item(n["video_url"], True, idx, n.get("display_url", "")))
            return
        url = n.get("display_url") or _pick_best(n.get("display_resources") or [])
        if url:
            out.append(_item(url, False, idx))

    children = ((node.get("edge_sidecar_to_children") or {}).get("edges")) or []
    if children:
        for i, edge in enumerate(children, 1):
            one(edge.get("node") or {}, i)
    else:
        one(node, 1)
    return out


# ── Fetch strategies ─────────────────────────────────────────────────────────

async def _ensure_csrf(session: aiohttp.ClientSession) -> Optional[str]:
    """Warm up the session so Instagram hands us a csrftoken."""
    cached = _SESSION_CACHE.get("csrf")
    if cached and (time.time() - float(_SESSION_CACHE.get("ts") or 0)) < 1800:
        return str(cached)
    jar = cookies_as_dict()
    if jar.get("csrftoken"):
        _SESSION_CACHE.update({"csrf": jar["csrftoken"], "ts": time.time()})
        return jar["csrftoken"]
    try:
        async with session.get(
            "https://www.instagram.com/",
            headers={"User-Agent": DESKTOP_UA, "Accept-Language": "en-US,en;q=0.9"},
            timeout=aiohttp.ClientTimeout(total=25),
        ) as r:
            await r.read()
            for cookie in session.cookie_jar:
                if cookie.key == "csrftoken":
                    _SESSION_CACHE.update({"csrf": cookie.value, "ts": time.time()})
                    return cookie.value
    except Exception:
        pass
    return None


async def _try_graphql(session: aiohttp.ClientSession, shortcode: str) -> List[Dict]:
    csrf = await _ensure_csrf(session)
    headers = _base_headers()
    if csrf:
        headers["X-CSRFToken"] = csrf
    headers["Content-Type"] = "application/x-www-form-urlencoded"

    variables = json.dumps(
        {
            "shortcode": shortcode,
            "fetch_tagged_user_count": None,
            "hoisted_comment_id": None,
            "hoisted_reply_id": None,
        }
    )
    for doc_id in GRAPHQL_DOC_IDS:
        try:
            async with session.post(
                "https://www.instagram.com/graphql/query",
                data={"variables": variables, "doc_id": doc_id, "server_timestamps": "true"},
                headers=headers,
                timeout=aiohttp.ClientTimeout(total=30),
            ) as r:
                if r.status != 200:
                    continue
                payload = await r.json(content_type=None)
        except Exception:
            continue

        node = (
            (payload.get("data") or {}).get("xdt_shortcode_media")
            or (payload.get("data") or {}).get("shortcode_media")
        )
        if node:
            items = _parse_graphql_media(node)
            if items:
                return items
    return []


async def _try_api_v1(session: aiohttp.ClientSession, shortcode: str) -> List[Dict]:
    media_id = shortcode_to_media_id(shortcode)
    if not media_id:
        return []
    endpoints = [
        f"https://www.instagram.com/api/v1/media/{media_id}/info/",
        f"https://i.instagram.com/api/v1/media/{media_id}/info/",
    ]
    for idx, url in enumerate(endpoints):
        headers = _base_headers(mobile=(idx == 1))
        csrf = await _ensure_csrf(session)
        if csrf:
            headers["X-CSRFToken"] = csrf
        try:
            async with session.get(
                url, headers=headers, timeout=aiohttp.ClientTimeout(total=30)
            ) as r:
                if r.status != 200:
                    continue
                payload = await r.json(content_type=None)
        except Exception:
            continue
        items_raw = payload.get("items") or []
        if items_raw:
            parsed = _parse_v1_media(items_raw[0])
            if parsed:
                return parsed
    return []


async def _try_embed(session: aiohttp.ClientSession, shortcode: str) -> List[Dict]:
    """Embed page needs no auth — great fallback for single public photos."""
    url = f"https://www.instagram.com/p/{shortcode}/embed/captioned/"
    try:
        async with session.get(
            url,
            headers={"User-Agent": DESKTOP_UA, "Accept-Language": "en-US,en;q=0.9"},
            timeout=aiohttp.ClientTimeout(total=30),
        ) as r:
            if r.status != 200:
                return []
            body = await r.text()
    except Exception:
        return []

    # Newer embeds inline a JSON blob containing the real media
    for pattern in (
        r'"gql_data"\s*:\s*(\{.+?\})\s*,\s*"[a-z_]+"\s*:',
        r'window\.__additionalDataLoaded\s*\(\s*[^,]+,\s*(\{.+?\})\s*\)\s*;',
    ):
        m = re.search(pattern, body, re.DOTALL)
        if not m:
            continue
        try:
            data = json.loads(m.group(1))
        except Exception:
            continue
        node = data.get("shortcode_media") or (data.get("graphql") or {}).get("shortcode_media")
        if node:
            items = _parse_graphql_media(node)
            if items:
                return items

    # Plain scrape of the <img class="EmbeddedMediaImage"> / og:image
    for pattern in (
        r'class="EmbeddedMediaImage"[^>]*src="([^"]+)"',
        r'property="og:image"\s+content="([^"]+)"',
        r'"display_url"\s*:\s*"([^"]+)"',
    ):
        m = re.search(pattern, body)
        if m:
            img = html.unescape(m.group(1)).replace("\\u0026", "&").replace("\\/", "/")
            if "scontent" in img or "cdninstagram" in img or "fbcdn" in img:
                return [_item(img, False, 1)]
    return []


async def _try_opengraph(session: aiohttp.ClientSession, url: str) -> List[Dict]:
    try:
        async with session.get(
            url,
            headers={"User-Agent": DESKTOP_UA, "Accept-Language": "en-US,en;q=0.9"},
            timeout=aiohttp.ClientTimeout(total=30),
        ) as r:
            if r.status != 200:
                return []
            body = await r.text()
    except Exception:
        return []

    vid = re.search(r'property="og:video"\s+content="([^"]+)"', body)
    if vid:
        return [_item(html.unescape(vid.group(1)), True, 1)]
    img = re.search(r'property="og:image"\s+content="([^"]+)"', body)
    if img:
        return [_item(html.unescape(img.group(1)), False, 1)]
    return []


async def fetch_media_items(url: str) -> List[Dict]:
    """Return every media item of an Instagram post, in carousel order."""
    url = normalize_url(url)
    shortcode = extract_shortcode(url)

    jar = cookies_as_dict()
    cookie_jar = aiohttp.CookieJar(unsafe=True)
    connector = aiohttp.TCPConnector(limit=8, ttl_dns_cache=300)

    async with aiohttp.ClientSession(cookie_jar=cookie_jar, connector=connector) as session:
        if jar:
            session.cookie_jar.update_cookies(jar, response_url=aiohttp.helpers.URL("https://www.instagram.com"))

        if shortcode:
            for strategy in (_try_graphql, _try_api_v1, _try_embed):
                try:
                    items = await strategy(session, shortcode)
                except Exception:
                    items = []
                if items:
                    return items

        try:
            items = await _try_opengraph(session, url)
        except Exception:
            items = []
        return items


# ── Downloading ──────────────────────────────────────────────────────────────

def _guess_ext(media_url: str, is_video: bool) -> str:
    path = urlparse(media_url).path.lower()
    for ext in (".jpg", ".jpeg", ".png", ".webp", ".mp4", ".mov", ".webm"):
        if path.endswith(ext):
            return ext
    return ".mp4" if is_video else ".jpg"


# ── Real content-type detection ──────────────────────────────────────────────
# Instagram's CDN happily serves WebP (and sometimes HEIC) bytes from a URL
# that ends in ".jpg". Telegram then rejects the upload with
#   [400 PHOTO_EXT_INVALID] The photo extension is invalid
# so we must look at the actual magic bytes, never the filename.

def sniff_format(path: str) -> Optional[str]:
    """Return 'jpeg' | 'png' | 'webp' | 'heic' | 'gif' | 'mp4' | None."""
    try:
        with open(path, "rb") as f:
            head = f.read(32)
    except Exception:
        return None
    if len(head) < 12:
        return None
    if head[:3] == b"\xff\xd8\xff":
        return "jpeg"
    if head[:8] == b"\x89PNG\r\n\x1a\n":
        return "png"
    if head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        return "webp"
    if head[:6] in (b"GIF87a", b"GIF89a"):
        return "gif"
    if head[4:8] == b"ftyp":
        brand = head[8:12]
        if brand in (b"heic", b"heix", b"hevc", b"heim", b"heis", b"mif1", b"msf1"):
            return "heic"
        return "mp4"
    return None


def normalize_image(path: str) -> str:
    """Make an image safe for Telegram's send_photo.

    Converts WebP/HEIC/anything-odd to real JPEG, fixes wrong extensions,
    strips alpha, and downscales if it busts Telegram's limits
    (10 MB, 10000 px total, 20:1 aspect ratio).

    Returns the path to use — may differ from the input.
    """
    fmt = sniff_format(path)
    if fmt in ("mp4", None):
        return path

    p = Path(path)
    suffix = p.suffix.lower()
    correct = {"jpeg": ".jpg", "png": ".png", "webp": ".webp", "gif": ".gif", "heic": ".heic"}[fmt]

    needs_convert = fmt in ("webp", "heic")
    needs_rename = (not needs_convert) and suffix != correct and not (
        fmt == "jpeg" and suffix in (".jpg", ".jpeg")
    )

    # Telegram limits — check before deciding to leave the file alone
    too_big = False
    try:
        from PIL import Image

        with Image.open(path) as im:
            w, h = im.size
        if w + h > 10000 or max(w, h) / max(1, min(w, h)) > 20:
            too_big = True
    except Exception:
        pass
    if os.path.getsize(path) > 10 * 1024 * 1024:
        too_big = True

    if not needs_convert and not needs_rename and not too_big:
        return path

    if needs_rename and not too_big:
        target = str(p.with_suffix(correct))
        try:
            os.replace(path, target)
            return target
        except Exception:
            return path

    # Re-encode to a clean, Telegram-friendly JPEG.
    # Write to a scratch file first, then move onto the final ".jpg" name so
    # the user-visible filename stays tidy even when the source was ".jpg".
    final = str(p.with_suffix(".jpg"))
    target = str(p.with_name(p.stem + ".__tg_tmp.jpg"))
    try:
        from PIL import Image

        try:
            from pillow_heif import register_heif_opener  # optional HEIC support

            register_heif_opener()
        except Exception:
            pass

        with Image.open(path) as im:
            im = im.convert("RGB")
            w, h = im.size
            if w + h > 10000:
                scale = 10000 / (w + h)
                im = im.resize((max(1, int(w * scale)), max(1, int(h * scale))), Image.LANCZOS)
            quality = 90
            im.save(target, "JPEG", quality=quality, optimize=True)
            while os.path.getsize(target) > 10 * 1024 * 1024 and quality > 40:
                quality -= 10
                im.save(target, "JPEG", quality=quality, optimize=True)
    except Exception:
        try:
            if os.path.exists(target):
                os.remove(target)
        except Exception:
            pass
        return path  # conversion failed — caller will fall back to send_document

    try:
        if os.path.exists(path) and os.path.abspath(path) != os.path.abspath(final):
            os.remove(path)
        os.replace(target, final)
        return final
    except Exception:
        return target


async def _download_one(
    session: aiohttp.ClientSession, item: Dict, output_dir: str, shortcode: str
) -> Optional[str]:
    media_url = item["url"]
    ext = _guess_ext(media_url, item["is_video"])
    name = f"{shortcode or 'instagram'}_{item['index']:02d}{ext}"
    dest = os.path.join(output_dir, name)
    # Ask the CDN for JPEG first — avoids WebP whenever the CDN will honour it
    headers = {
        "User-Agent": DESKTOP_UA,
        "Referer": "https://www.instagram.com/",
        "Accept": ("video/*,*/*;q=0.8" if item["is_video"]
                   else "image/jpeg,image/png;q=0.9,*/*;q=0.5"),
    }
    try:
        async with session.get(
            media_url, headers=headers, timeout=aiohttp.ClientTimeout(total=600)
        ) as r:
            if r.status not in (200, 206):
                return None
            with open(dest, "wb") as fh:
                async for chunk in r.content.iter_chunked(1 << 16):
                    fh.write(chunk)
    except Exception:
        return None

    # Reject truncated / placeholder files
    if not os.path.exists(dest) or os.path.getsize(dest) < 1024:
        try:
            os.remove(dest)
        except Exception:
            pass
        return None

    # Instagram lies about extensions (.jpg URLs serving WebP/HEIC bytes),
    # which makes Telegram reject the upload with PHOTO_EXT_INVALID.
    # Normalise now, at the source, so every consumer gets a clean file.
    if not item["is_video"]:
        try:
            dest = await asyncio.to_thread(normalize_image, dest)
        except Exception:
            pass
    return dest


async def _ytdlp_fallback(url: str, output_dir: str) -> List[str]:
    """Last resort — good for reels, stories and highlights."""
    out_tmpl = os.path.join(output_dir, "%(id)s_%(autonumber)02d.%(ext)s")
    cmd = ["yt-dlp"]
    cookie_path = write_cookie_file()
    if cookie_path:
        cmd += ["--cookies", cookie_path]
    cmd += [
        "--user-agent", DESKTOP_UA,
        "--add-header", "Accept-Language:en-US,en;q=0.9",
        "--socket-timeout", "30",
        "--retries", "3",
        "--fragment-retries", "5",
        "--concurrent-fragments", "4",
        "--format", "bestvideo*+bestaudio/best",
        "--merge-output-format", "mp4",
        "--output", out_tmpl,
        "--yes-playlist",
        "--ignore-errors",
        "--no-warnings",
        url,
    ]
    before = {p.name for p in Path(output_dir).iterdir()} if Path(output_dir).exists() else set()
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
        )
        _, err = await asyncio.wait_for(proc.communicate(), timeout=Config.YTDL_TIMEOUT_SEC)
    except asyncio.TimeoutError:
        return []
    except Exception:
        return []

    fresh = [
        str(p)
        for p in sorted(Path(output_dir).iterdir(), key=lambda p: p.stat().st_mtime)
        if p.is_file()
        and p.name not in before
        and p.suffix.lower() in (IMAGE_EXTS | VIDEO_EXTS)
    ]
    # yt-dlp can also leave WebP thumbnails behind — normalise those too
    normalised = []
    for f in fresh:
        if Path(f).suffix.lower() in IMAGE_EXTS:
            try:
                f = await asyncio.to_thread(normalize_image, f)
            except Exception:
                pass
        normalised.append(f)
    return normalised


def _friendly_error(kind: str) -> str:
    if has_cookies():
        return (
            "🔒 <b>Instagram ne access block kiya.</b>\n\n"
            "Cookies set hain, lekin shayad:\n"
            "• Session expire ho gayi hai\n"
            "• Post delete / private hai\n"
            "• Instagram ne server IP ko rate-limit kiya hai\n\n"
            "✅ <b>Fix:</b> Browser se fresh cookies export karke "
            "<code>INSTAGRAM_COOKIES</code> env var update karo, phir retry."
        )
    extra = " (Stories/Highlights ke liye login zaroori hai.)" if kind in ("story", "highlight") else ""
    return (
        f"🔒 <b>Instagram login required.</b>{extra}\n\n"
        "Instagram ab bina login ke server se media nahi deta.\n\n"
        "✅ <b>Fix:</b> <code>INSTAGRAM_COOKIES</code> env variable mein "
        "Netscape-format cookies paste karo (browser extension "
        "'Get cookies.txt' se export karo), phir bot restart karo."
    )


async def download_instagram(url: str, output_dir: str) -> List[str]:
    """
    Download an Instagram post's media (photos + videos, full carousel).

    Returns a list of local file paths, ordered as in the post.
    Raises InstagramError with a helpful Hinglish message on failure.
    """
    url = normalize_url(url)
    kind = content_kind(url)
    shortcode = extract_shortcode(url) or ""
    os.makedirs(output_dir, exist_ok=True)

    saved: List[str] = []

    # ── Path A: direct CDN download via Instagram's own APIs ──
    try:
        items = await fetch_media_items(url)
    except Exception:
        items = []

    if items:
        connector = aiohttp.TCPConnector(limit=4)
        async with aiohttp.ClientSession(connector=connector) as session:
            results = await asyncio.gather(
                *[_download_one(session, it, output_dir, shortcode) for it in items],
                return_exceptions=True,
            )
        saved = [r for r in results if isinstance(r, str) and r]
        saved.sort(key=lambda p: os.path.basename(p))

    if saved:
        return saved

    # ── Path B: yt-dlp (reels, stories, highlights) ──
    saved = await _ytdlp_fallback(url, output_dir)
    if saved:
        return saved

    raise InstagramError(_friendly_error(kind))


# Backwards-compatible alias used by older bot.py code paths
async def download_instagram_photos(url: str, output_dir: str) -> List[str]:
    return await download_instagram(url, output_dir)

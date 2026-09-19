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


# ── Metadata extraction ──────────────────────────────────────────────────────

def _clean_text(txt: str) -> str:
    """Tidy up an Instagram caption for Telegram."""
    if not txt:
        return ""
    txt = html.unescape(str(txt))
    txt = txt.replace("\\n", "\n").replace("\\/", "/").replace("\\u0026", "&")
    # Collapse 3+ blank lines, trim trailing spaces on each line
    txt = re.sub(r"[ \t]+\n", "\n", txt)
    txt = re.sub(r"\n{3,}", "\n\n", txt)
    return txt.strip()


def _meta_from_v1(node: Dict) -> Dict:
    """Pull caption / owner / title / stats from a v1 API media object."""
    cap = node.get("caption")
    text = ""
    if isinstance(cap, dict):
        text = cap.get("text") or ""
    elif isinstance(cap, str):
        text = cap
    user = node.get("user") or node.get("owner") or {}
    return {
        "caption": _clean_text(text),
        "title": _clean_text(node.get("title") or ""),
        "username": user.get("username") or "",
        "full_name": _clean_text(user.get("full_name") or ""),
        "likes": node.get("like_count") or 0,
        "views": node.get("play_count") or node.get("view_count") or 0,
        "taken_at": node.get("taken_at") or 0,
    }


def _meta_from_graphql(node: Dict) -> Dict:
    """Pull caption / owner / title from a GraphQL shortcode_media node."""
    text = ""
    edges = ((node.get("edge_media_to_caption") or {}).get("edges")) or []
    if edges:
        text = ((edges[0] or {}).get("node") or {}).get("text") or ""
    if not text:
        text = node.get("caption") or ""
    owner = node.get("owner") or {}
    return {
        "caption": _clean_text(text),
        "title": _clean_text(node.get("title") or ""),
        "username": owner.get("username") or "",
        "full_name": _clean_text(owner.get("full_name") or ""),
        "likes": ((node.get("edge_media_preview_like") or {}).get("count")) or 0,
        "views": node.get("video_view_count") or 0,
        "taken_at": node.get("taken_at_timestamp") or 0,
    }


def _empty_meta() -> Dict:
    return {"caption": "", "title": "", "username": "", "full_name": "",
            "likes": 0, "views": 0, "taken_at": 0}


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


async def _try_graphql(session: aiohttp.ClientSession, shortcode: str) -> Tuple[List[Dict], Dict]:
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
                if r.status == 429:
                    note_rate_limit()
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
                return items, _meta_from_graphql(node)
    return [], _empty_meta()


async def _try_api_v1(session: aiohttp.ClientSession, shortcode: str) -> Tuple[List[Dict], Dict]:
    media_id = shortcode_to_media_id(shortcode)
    if not media_id:
        return [], _empty_meta()
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
                if r.status == 429:
                    note_rate_limit()
                if r.status != 200:
                    continue
                payload = await r.json(content_type=None)
        except Exception:
            continue
        items_raw = payload.get("items") or []
        if items_raw:
            parsed = _parse_v1_media(items_raw[0])
            if parsed:
                return parsed, _meta_from_v1(items_raw[0])
    return [], _empty_meta()


async def _try_embed(session: aiohttp.ClientSession, shortcode: str) -> Tuple[List[Dict], Dict]:
    """Embed page needs no auth — great fallback for single public photos."""
    url = f"https://www.instagram.com/p/{shortcode}/embed/captioned/"
    try:
        async with session.get(
            url,
            headers={"User-Agent": DESKTOP_UA, "Accept-Language": "en-US,en;q=0.9"},
            timeout=aiohttp.ClientTimeout(total=30),
        ) as r:
            if r.status != 200:
                return [], _empty_meta()
            body = await r.text()
    except Exception:
        return [], _empty_meta()

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
                return items, _meta_from_graphql(node)

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
                return [_item(img, False, 1)], _meta_from_embed(body)
    return [], _empty_meta()


def _meta_from_embed(body: str) -> Dict:
    """Scrape caption + author out of an embed / public HTML page."""
    meta = _empty_meta()

    # The embed page shows the caption inside the Caption div
    m = re.search(r'class="[^"]*Caption[^"]*"[^>]*>(.*?)</div>', body, re.DOTALL)
    if m:
        raw = m.group(1)
        # Drop the leading "<a>username</a>" and any nested tags
        raw = re.sub(r'<a[^>]*class="[^"]*CaptionUsername[^"]*"[^>]*>.*?</a>', "", raw, flags=re.DOTALL)
        raw = re.sub(r"<br\s*/?>", "\n", raw, flags=re.I)
        raw = re.sub(r"<[^>]+>", "", raw)
        meta["caption"] = _clean_text(raw)

    if not meta["caption"]:
        m = re.search(r'property="og:description"\s+content="([^"]*)"', body)
        if m:
            desc = html.unescape(m.group(1))
            # og:description looks like:  123 likes, 4 comments - user on date: "caption"
            q = re.search(r'[:\-]\s*[""\"](.+)[""\"]\s*$', desc, re.DOTALL)
            meta["caption"] = _clean_text(q.group(1) if q else desc)

    m = re.search(r'property="og:title"\s+content="([^"]*)"', body)
    if m:
        title = html.unescape(m.group(1))
        u = re.search(r"@([A-Za-z0-9_.]+)", title)
        if u:
            meta["username"] = u.group(1)
        meta["title"] = _clean_text(re.sub(r"\s*on Instagram.*$", "", title))

    if not meta["username"]:
        m = re.search(r'"owner"\s*:\s*\{[^}]*"username"\s*:\s*"([^"]+)"', body)
        if m:
            meta["username"] = m.group(1)
    return meta


async def _try_opengraph(session: aiohttp.ClientSession, url: str) -> Tuple[List[Dict], Dict]:
    try:
        async with session.get(
            url,
            headers={"User-Agent": DESKTOP_UA, "Accept-Language": "en-US,en;q=0.9"},
            timeout=aiohttp.ClientTimeout(total=30),
        ) as r:
            if r.status != 200:
                return [], _empty_meta()
            body = await r.text()
    except Exception:
        return [], _empty_meta()

    meta = _meta_from_embed(body)
    vid = re.search(r'property="og:video"\s+content="([^"]+)"', body)
    if vid:
        return [_item(html.unescape(vid.group(1)), True, 1)], meta
    img = re.search(r'property="og:image"\s+content="([^"]+)"', body)
    if img:
        return [_item(html.unescape(img.group(1)), False, 1)], meta
    return [], _empty_meta()


async def fetch_post(url: str) -> Tuple[List[Dict], Dict]:
    """Return (media items in carousel order, post metadata).

    Metadata keys: caption, title, username, full_name, likes, views, taken_at.
    """
    url = normalize_url(url)
    shortcode = extract_shortcode(url)

    jar = cookies_as_dict()
    cookie_jar = aiohttp.CookieJar(unsafe=True)
    connector = aiohttp.TCPConnector(limit=8, ttl_dns_cache=300)

    async with aiohttp.ClientSession(cookie_jar=cookie_jar, connector=connector) as session:
        if jar:
            session.cookie_jar.update_cookies(jar, response_url=aiohttp.helpers.URL("https://www.instagram.com"))

        best_items: List[Dict] = []
        best_meta: Dict = _empty_meta()

        if shortcode:
            for strategy in (_try_graphql, _try_api_v1, _try_embed):
                try:
                    items, meta = await strategy(session, shortcode)
                except Exception:
                    items, meta = [], _empty_meta()
                if items:
                    clear_rate_limit()      # success → cooldown is over
                    best_items, best_meta = items, meta
                    # Media found. If the caption is missing, try the embed page
                    # (public, no auth) purely to enrich the metadata.
                    if not best_meta.get("caption") and strategy is not _try_embed:
                        try:
                            _, extra = await _try_embed(session, shortcode)
                            for k, v in (extra or {}).items():
                                if v and not best_meta.get(k):
                                    best_meta[k] = v
                        except Exception:
                            pass
                    return best_items, best_meta

        try:
            items, meta = await _try_opengraph(session, url)
        except Exception:
            items, meta = [], _empty_meta()
        return items, meta


async def fetch_media_items(url: str) -> List[Dict]:
    """Backwards-compatible helper — media items only."""
    items, _ = await fetch_post(url)
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


async def _ytdlp_fallback_with_meta(url: str, output_dir: str) -> Tuple[List[str], Dict]:
    """yt-dlp fallback that also harvests caption/title from its info JSON."""
    files = await _ytdlp_fallback(url, output_dir, write_info=True)
    meta = _empty_meta()
    try:
        for info_file in sorted(Path(output_dir).glob("*.info.json")):
            try:
                data = json.loads(info_file.read_text(encoding="utf-8", errors="ignore"))
            except Exception:
                continue
            desc = data.get("description") or ""
            title = data.get("title") or ""
            # yt-dlp often duplicates the caption into the title
            if title and desc and desc.strip().startswith(title.strip()[:40]):
                title = ""
            meta["caption"] = _clean_text(desc)
            meta["title"] = _clean_text(title)
            meta["username"] = data.get("uploader_id") or data.get("channel_id") or ""
            meta["full_name"] = _clean_text(data.get("uploader") or "")
            meta["likes"] = data.get("like_count") or 0
            meta["views"] = data.get("view_count") or 0
            try:
                info_file.unlink()
            except Exception:
                pass
            break
    except Exception:
        pass
    return files, meta


async def _ytdlp_fallback(url: str, output_dir: str, write_info: bool = False) -> List[str]:
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
    ]
    if write_info:
        cmd += ["--write-info-json"]
    cmd += [url]
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


def _esc(txt: str) -> str:
    """Escape for Telegram HTML parse mode."""
    return (str(txt).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"))


# Telegram hard-limits captions to 1024 characters, and messages to 4096.
TG_CAPTION_LIMIT = 1024
TG_MESSAGE_LIMIT = 4096


# ── Fancy font helpers ───────────────────────────────────────────────────────
# Telegram renders these Unicode maths alphanumerics everywhere, so they give
# the caption a styled look without relying on entity formatting.

_BOLD_SANS = {}
for _a, _z, _base in ((0x41, 0x5A, 0x1D5D4), (0x61, 0x7A, 0x1D5EE), (0x30, 0x39, 0x1D7EC)):
    for _c in range(_a, _z + 1):
        _BOLD_SANS[chr(_c)] = chr(_base + _c - _a)


def to_bold_font(text: str) -> str:
    """Convert ASCII letters/digits to bold sans-serif Unicode."""
    return "".join(_BOLD_SANS.get(ch, ch) for ch in str(text))


def _visible_len(html_text: str) -> int:
    """Length Telegram actually counts: rendered text, in UTF-16 code units.

    Telegram counts the *parsed* caption (tags stripped, entities unescaped)
    and measures it in UTF-16 units, so emoji cost 2. Measuring raw HTML — as
    this code used to — wildly overcounts and silently drops the description.
    """
    txt = re.sub(r"<[^>]+>", "", html_text)
    txt = (txt.replace("&lt;", "<").replace("&gt;", ">")
              .replace("&quot;", '"').replace("&#39;", "'").replace("&amp;", "&"))
    return len(txt.encode("utf-16-le")) // 2


def _truncate_visible(text: str, max_units: int) -> str:
    """Trim raw (unescaped) text so its UTF-16 length fits `max_units`."""
    if max_units <= 1:
        return ""
    if len(text.encode("utf-16-le")) // 2 <= max_units:
        return text
    lo, hi, best = 0, len(text), ""
    while lo <= hi:
        mid = (lo + hi) // 2
        cut = text[:mid]
        if len(cut.encode("utf-16-le")) // 2 <= max_units - 1:
            best, lo = cut, mid + 1
        else:
            hi = mid - 1
    # Prefer a clean break at a newline or space
    for sep in ("\n", " "):
        idx = best.rfind(sep)
        if idx > len(best) * 0.6:
            best = best[:idx]
            break
    return best.rstrip() + "\u2026"


def build_post_caption(meta: Dict, url: str = "", extra: str = "") -> str:
    """Build a Telegram HTML caption for an Instagram post.

    The description sits inside an **expandable blockquote**
    (<blockquote expandable>), which Telegram collapses behind a "Show more"
    tap — ideal for long reel descriptions.

    Budgeting is done on *visible* UTF-16 length (what Telegram counts), not on
    raw HTML length, so markup and escaped characters no longer eat the budget.
    """
    meta = meta or {}

    title = (meta.get("title") or "").strip()
    username = (meta.get("username") or "").strip()
    full_name = (meta.get("full_name") or "").strip()
    caption = (meta.get("caption") or "").strip()

    parts: List[str] = []

    # ── Header ──
    if title and not caption.lower().startswith(title.lower()[:40]):
        parts.append(f"<b>{_esc(to_bold_font(title))}</b>")

    if username:
        who = f"<b>{_esc(full_name)}</b>\n" if full_name and full_name != username else ""
        parts.append(
            f"{who}\U0001F464 <a href=\"https://www.instagram.com/{_esc(username)}/\">"
            f"<b>@{_esc(username)}</b></a>"
        )
    elif full_name:
        parts.append(f"\U0001F464 <b>{_esc(full_name)}</b>")

    stats = []
    if meta.get("likes"):
        stats.append(f"\u2764\uFE0F <b>{int(meta['likes']):,}</b>")
    if meta.get("views"):
        stats.append(f"\U0001F441 <b>{int(meta['views']):,}</b>")
    if stats:
        parts.append("  \u2022  ".join(stats))

    header = "\n".join(p for p in parts if p)

    footer = ""
    if url:
        footer = f'<a href="{_esc(url)}">\U0001F517 <b>Open on Instagram</b></a>'

    extra = (extra or "").strip()

    # ── Budget the description against what Telegram really counts ──
    fixed = [p for p in (header, extra, footer) if p]
    # separators: "\n\n" between each block, plus the description block
    sep_cost = 2 * (len(fixed) + (1 if caption else 0))
    used = sum(_visible_len(p) for p in fixed) + sep_cost
    budget = TG_CAPTION_LIMIT - used - 2   # small safety margin

    body = ""
    if caption and budget >= 20:
        text = _truncate_visible(caption, budget)
        if text:
            body = f"<blockquote expandable>{_esc(text)}</blockquote>"

    out = "\n\n".join(p for p in (header, body, extra, footer) if p).strip()

    # Safety net — drop blocks only if we somehow still overflow
    if _visible_len(out) > TG_CAPTION_LIMIT:
        out = "\n\n".join(p for p in (header, body, footer) if p).strip()
    if _visible_len(out) > TG_CAPTION_LIMIT:
        out = "\n\n".join(p for p in (header, footer) if p).strip()
    return out


def caption_overflowed(meta: Dict, url: str = "", extra: str = "") -> bool:
    """True if the description had to be trimmed to fit the caption."""
    caption = ((meta or {}).get("caption") or "").strip()
    if not caption:
        return False
    built = build_post_caption(meta, url, extra)
    if "<blockquote" not in built:
        return True
    return "\u2026</blockquote>" in built


def build_description_messages(meta: Dict, url: str = "") -> List[str]:
    """Full description split into <=4096-char expandable-quote messages.

    Used when a reel's description is too long for the 1024-char caption:
    the media keeps a trimmed caption and the complete text follows in one
    or more separate messages, still collapsed behind "Show more".
    """
    caption = ((meta or {}).get("caption") or "").strip()
    if not caption:
        return []

    header = "\U0001F4DD <b>Full Description</b>"
    # Room for the wrapper tags, header and a safety margin
    chunk_budget = TG_MESSAGE_LIMIT - _visible_len(header) - 40

    def _fit(text: str, budget: int) -> int:
        """Largest prefix length whose UTF-16 size fits `budget`."""
        if len(text.encode("utf-16-le")) // 2 <= budget:
            return len(text)
        lo, hi, best = 0, len(text), 0
        while lo <= hi:
            mid = (lo + hi) // 2
            if len(text[:mid].encode("utf-16-le")) // 2 <= budget:
                best, lo = mid, mid + 1
            else:
                hi = mid - 1
        return best

    chunks: List[str] = []
    remaining = caption
    while remaining:
        cut = _fit(remaining, chunk_budget)
        if cut >= len(remaining):
            chunks.append(remaining)
            break
        # Break on a newline or space so words stay intact, but slice the
        # ORIGINAL string at that index so no character is ever dropped.
        window = remaining[:cut]
        brk = max(window.rfind("\n"), window.rfind(" "))
        if brk < cut * 0.6:
            brk = cut
        chunks.append(remaining[:brk].rstrip())
        remaining = remaining[brk:].lstrip()
        if not chunks[-1]:
            chunks.pop()

    out: List[str] = []
    for i, chunk in enumerate(chunks):
        head = header if i == 0 else f"\U0001F4DD <b>Full Description ({i + 1}/{len(chunks)})</b>"
        out.append(f"{head}\n<blockquote expandable>{_esc(chunk)}</blockquote>")
    return out


# ── Rate-limit tracking ──────────────────────────────────────────────────────
# Instagram returns HTTP 429 with no Retry-After header, so we track the
# cooldown ourselves and escalate it while the limit keeps being hit.

_RATE_LIMIT: Dict[str, float] = {"until": 0.0, "hits": 0.0, "last_hit": 0.0}

# Backoff ladder, in seconds. Repeated 429s walk further down the list.
_RATE_BACKOFF = [60, 180, 300, 600, 900, 1800, 3600]


def _fmt_duration(seconds: float) -> str:
    """Human-friendly duration: '45 seconds', '2 min 30 sec', '1 hour 5 min'."""
    seconds = max(0, int(round(seconds)))
    if seconds < 60:
        return f"{seconds} second{'s' if seconds != 1 else ''}"
    mins, secs = divmod(seconds, 60)
    if mins < 60:
        return f"{mins} min" + (f" {secs} sec" if secs else "")
    hours, mins = divmod(mins, 60)
    return f"{hours} hour{'s' if hours != 1 else ''}" + (f" {mins} min" if mins else "")


def note_rate_limit() -> float:
    """Record a 429 and return the UNIX timestamp when retrying is allowed."""
    now = time.time()
    # Reset the escalation if the last hit was long ago (limit has cooled off)
    if now - float(_RATE_LIMIT.get("last_hit") or 0) > 7200:
        _RATE_LIMIT["hits"] = 0.0
    idx = min(int(_RATE_LIMIT.get("hits") or 0), len(_RATE_BACKOFF) - 1)
    wait = _RATE_BACKOFF[idx]
    _RATE_LIMIT["hits"] = float(idx + 1)
    _RATE_LIMIT["last_hit"] = now
    _RATE_LIMIT["until"] = max(float(_RATE_LIMIT.get("until") or 0), now + wait)
    return float(_RATE_LIMIT["until"])


def clear_rate_limit():
    """Called after a success — the limit is evidently over."""
    _RATE_LIMIT["until"] = 0.0
    _RATE_LIMIT["hits"] = 0.0


def rate_limit_remaining() -> int:
    """Seconds left on the current cooldown (0 when clear)."""
    return max(0, int(round(float(_RATE_LIMIT.get("until") or 0) - time.time())))


def rate_limit_message(remaining: Optional[int] = None) -> str:
    """User-facing message with a concrete ETA."""
    if remaining is None:
        remaining = rate_limit_remaining()
    if remaining <= 0:
        return ("⏳ Instagram ne rate-limit kar diya tha.\n\n"
                "✅ Ab try kar sakte ho.")
    return (
        "⏳ <b>Instagram ne rate-limit kar diya.</b>\n\n"
        f"⏱ <b>{_fmt_duration(remaining)}</b> baad try karo  "
        f"(<code>{remaining}s</code>)\n\n"
        "<i>Instagram ek IP se zyada requests block kar deta hai. "
        "Itna wait karke dobara bhejo — cooldown khatam hote hi chal jayega.</i>"
    )


class RateLimited(InstagramError):
    """Raised when Instagram is rate-limiting us; carries the ETA."""

    def __init__(self, remaining: Optional[int] = None):
        self.remaining = remaining if remaining is not None else rate_limit_remaining()
        super().__init__(rate_limit_message(self.remaining))


def raise_if_rate_limited():
    """Fail fast with an ETA instead of making a doomed request."""
    remaining = rate_limit_remaining()
    if remaining > 0:
        raise RateLimited(remaining)


# ── Profile & stories ────────────────────────────────────────────────────────

def extract_username(text: str) -> Optional[str]:
    """Pull a username from '@name', 'name', or a profile URL."""
    t = (text or "").strip()
    if not t:
        return None
    m = re.search(r"instagram\.com/([A-Za-z0-9_.]+)", t)
    if m:
        name = m.group(1)
        if name.lower() in ("p", "reel", "reels", "tv", "stories", "s", "explore", "share"):
            return None
        return name
    t = t.lstrip("@").strip("/")
    return t if re.fullmatch(r"[A-Za-z0-9_.]{1,30}", t) else None


async def _profile_info(session: aiohttp.ClientSession, username: str) -> Dict:
    """Fetch a profile's web_profile_info payload."""
    headers = _base_headers()
    jar = cookies_as_dict()
    if jar.get("csrftoken"):
        headers["X-CSRFToken"] = jar["csrftoken"]
    url = f"https://www.instagram.com/api/v1/users/web_profile_info/?username={username}"
    async with session.get(url, headers=headers,
                           timeout=aiohttp.ClientTimeout(total=30)) as r:
        if r.status == 404:
            raise InstagramError(f"❌ <b>@{username}</b> nahi mila.")
        if r.status in (401, 403):
            raise InstagramError(_friendly_error("profile"))
        if r.status == 429:
            note_rate_limit()
            raise RateLimited()
        if r.status != 200:
            raise InstagramError(f"❌ Instagram ne HTTP {r.status} diya.")
        data = await r.json(content_type=None)
    user = (data.get("data") or {}).get("user")
    if not user:
        raise InstagramError(_friendly_error("profile"))
    return user


async def fetch_profile_posts(username: str, limit: int = 12) -> Tuple[List[Dict], Dict]:
    """Return (list of {shortcode,url,is_video,caption}, profile info)."""
    username = (username or "").lstrip("@")
    raise_if_rate_limited()
    jar = cookies_as_dict()
    cookie_jar = aiohttp.CookieJar(unsafe=True)
    async with aiohttp.ClientSession(cookie_jar=cookie_jar) as session:
        if jar:
            session.cookie_jar.update_cookies(
                jar, response_url=aiohttp.helpers.URL("https://www.instagram.com"))
        user = await _profile_info(session, username)

    if user.get("is_private") and not user.get("followed_by_viewer"):
        raise InstagramError(
            f"🔒 <b>@{username}</b> private hai aur aap follow nahi karte.")

    edges = ((user.get("edge_owner_to_timeline_media") or {}).get("edges")) or []
    posts: List[Dict] = []
    for edge in edges[: max(1, min(limit, 50))]:
        node = edge.get("node") or {}
        sc = node.get("shortcode")
        if not sc:
            continue
        cap = ""
        cap_edges = ((node.get("edge_media_to_caption") or {}).get("edges")) or []
        if cap_edges:
            cap = _clean_text(((cap_edges[0] or {}).get("node") or {}).get("text") or "")
        posts.append({
            "shortcode": sc,
            "url": f"https://www.instagram.com/p/{sc}/",
            "is_video": bool(node.get("is_video")),
            "caption": cap,
        })

    info = {
        "username": user.get("username") or username,
        "full_name": _clean_text(user.get("full_name") or ""),
        "biography": _clean_text(user.get("biography") or ""),
        "followers": ((user.get("edge_followed_by") or {}).get("count")) or 0,
        "posts_total": ((user.get("edge_owner_to_timeline_media") or {}).get("count")) or 0,
        "is_private": bool(user.get("is_private")),
        "profile_pic": user.get("profile_pic_url_hd") or user.get("profile_pic_url") or "",
    }
    return posts, info


async def fetch_stories(username: str) -> Tuple[List[Dict], Dict]:
    """Return (media items, info) for a user's currently active stories."""
    username = (username or "").lstrip("@")
    raise_if_rate_limited()
    if not has_cookies():
        raise InstagramError(
            "🔒 Stories ke liye login zaroori hai.\n\n"
            "✅ <b>Fix:</b> <code>INSTAGRAM_COOKIES</code> set karo.")

    jar = cookies_as_dict()
    cookie_jar = aiohttp.CookieJar(unsafe=True)
    async with aiohttp.ClientSession(cookie_jar=cookie_jar) as session:
        if jar:
            session.cookie_jar.update_cookies(
                jar, response_url=aiohttp.helpers.URL("https://www.instagram.com"))
        user = await _profile_info(session, username)
        uid = user.get("id")
        if not uid:
            raise InstagramError(f"❌ <b>@{username}</b> ki ID nahi mili.")

        headers = _base_headers()
        if jar.get("csrftoken"):
            headers["X-CSRFToken"] = jar["csrftoken"]
        url = ("https://i.instagram.com/api/v1/feed/reels_media/"
               f"?reel_ids={uid}")
        async with session.get(url, headers=headers,
                               timeout=aiohttp.ClientTimeout(total=30)) as r:
            if r.status in (401, 403):
                raise InstagramError(_friendly_error("story"))
            if r.status != 200:
                raise InstagramError(f"❌ Stories fetch failed (HTTP {r.status}).")
            data = await r.json(content_type=None)

    reels = data.get("reels") or data.get("reels_media") or {}
    node = reels.get(str(uid)) if isinstance(reels, dict) else None
    if isinstance(reels, list) and reels:
        node = reels[0]
    items_raw = (node or {}).get("items") or []
    if not items_raw:
        raise InstagramError(f"📭 <b>@{username}</b> ki koi active story nahi hai.")

    items: List[Dict] = []
    for i, it in enumerate(items_raw, 1):
        vids = it.get("video_versions") or []
        if vids:
            u = _pick_best(vids)
            if u:
                items.append(_item(u, True, i))
                continue
        imgs = (it.get("image_versions2") or {}).get("candidates") or []
        u = _pick_best(imgs)
        if u:
            items.append(_item(u, False, i))

    info = {"username": user.get("username") or username,
            "full_name": _clean_text(user.get("full_name") or ""),
            "count": len(items)}
    return items, info


async def download_media_items(items: List[Dict], output_dir: str,
                               prefix: str = "story") -> List[str]:
    """Download pre-resolved media items to disk."""
    os.makedirs(output_dir, exist_ok=True)
    async with aiohttp.ClientSession(connector=aiohttp.TCPConnector(limit=4)) as session:
        results = await asyncio.gather(
            *[_download_one(session, it, output_dir, prefix) for it in items],
            return_exceptions=True,
        )
    saved = [r for r in results if isinstance(r, str) and r]
    saved.sort(key=lambda p: os.path.basename(p))
    return saved


# ── Cookie health ────────────────────────────────────────────────────────────

# Cached result so we do not hammer Instagram: (ok, detail, checked_at)
_COOKIE_HEALTH: Dict[str, object] = {"ok": None, "detail": "", "ts": 0.0}


async def validate_cookies(force: bool = False) -> Tuple[bool, str]:
    """Check whether the configured Instagram cookies still authenticate.

    Returns (ok, human_readable_detail). Result is cached for 30 minutes
    unless `force` is set, so this is cheap to call on a schedule.
    """
    now = time.time()
    if (not force and _COOKIE_HEALTH["ok"] is not None
            and now - float(_COOKIE_HEALTH["ts"] or 0) < 1800):
        return bool(_COOKIE_HEALTH["ok"]), str(_COOKIE_HEALTH["detail"])

    jar = cookies_as_dict()
    if not jar:
        result = (False, "No cookies configured (INSTAGRAM_COOKIES is empty).")
        _COOKIE_HEALTH.update({"ok": False, "detail": result[1], "ts": now})
        return result

    if not jar.get("sessionid"):
        result = (False, "Cookies present but `sessionid` is missing — re-export them.")
        _COOKIE_HEALTH.update({"ok": False, "detail": result[1], "ts": now})
        return result

    ok, detail = False, "Could not reach Instagram."
    try:
        cookie_jar = aiohttp.CookieJar(unsafe=True)
        async with aiohttp.ClientSession(cookie_jar=cookie_jar) as session:
            session.cookie_jar.update_cookies(
                jar, response_url=aiohttp.helpers.URL("https://www.instagram.com"))
            headers = _base_headers()
            if jar.get("csrftoken"):
                headers["X-CSRFToken"] = jar["csrftoken"]
            async with session.get(
                "https://www.instagram.com/api/v1/users/web_profile_info/?username=instagram",
                headers=headers, timeout=aiohttp.ClientTimeout(total=20),
            ) as r:
                if r.status == 200:
                    try:
                        data = await r.json(content_type=None)
                    except Exception:
                        data = {}
                    if (data.get("data") or {}).get("user"):
                        ok, detail = True, "Cookies are valid and logged in."
                    else:
                        ok, detail = False, "Instagram returned an empty profile — session likely expired."
                elif r.status in (401, 403):
                    ok, detail = False, f"Instagram rejected the session (HTTP {r.status}) — cookies expired."
                elif r.status == 429:
                    # Rate limiting is not a cookie problem; do not cry wolf.
                    note_rate_limit()
                    ok, detail = True, "Rate-limited (HTTP 429) — cookies presumed valid."
                else:
                    ok, detail = False, f"Unexpected response from Instagram (HTTP {r.status})."
    except Exception as e:
        ok, detail = False, f"Cookie check failed: {str(e)[:120]}"

    _COOKIE_HEALTH.update({"ok": ok, "detail": detail, "ts": now})
    return ok, detail


def cookie_status_line() -> str:
    """Short status string for /version — uses the cached result only."""
    if not has_cookies():
        return "not set"
    state = _COOKIE_HEALTH["ok"]
    if state is None:
        return "set (not yet checked)"
    return "valid" if state else "EXPIRED"


def _friendly_error(kind: str) -> str:
    remaining = rate_limit_remaining()
    if remaining > 0:
        return rate_limit_message(remaining)
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


async def download_post(url: str, output_dir: str) -> Tuple[List[str], Dict]:
    """
    Download an Instagram post's media and return (file paths, metadata).

    Files are ordered exactly as in the post. Metadata carries the caption,
    title, author and stats so callers can build a rich Telegram caption.
    Raises InstagramError with a helpful Hinglish message on failure.
    """
    url = normalize_url(url)
    kind = content_kind(url)
    shortcode = extract_shortcode(url) or ""
    os.makedirs(output_dir, exist_ok=True)

    # Don't burn a doomed request while a cooldown is active — tell the
    # user exactly how long is left instead.
    raise_if_rate_limited()

    saved: List[str] = []

    # ── Path A: direct CDN download via Instagram's own APIs ──
    try:
        items, meta = await fetch_post(url)
    except Exception:
        items, meta = [], _empty_meta()

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
        clear_rate_limit()
        return saved, meta

    # ── Path B: yt-dlp (reels, stories, highlights) ──
    saved, yt_meta = await _ytdlp_fallback_with_meta(url, output_dir)
    if saved:
        for k, v in (yt_meta or {}).items():
            if v and not meta.get(k):
                meta[k] = v
        return saved, meta

    raise InstagramError(_friendly_error(kind))


async def download_instagram(url: str, output_dir: str) -> List[str]:
    """Backwards-compatible helper — file paths only."""
    files, _ = await download_post(url, output_dir)
    return files


# Backwards-compatible alias used by older bot.py code paths
async def download_instagram_photos(url: str, output_dir: str) -> List[str]:
    return await download_instagram(url, output_dir)

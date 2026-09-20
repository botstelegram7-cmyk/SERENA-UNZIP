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


_LAST_COOKIE_FINGERPRINT: Dict[str, str] = {"v": ""}


def _note_cookie_change():
    """Reset the cooldown when the cookies actually change.

    A fresh session is a different identity to Instagram, so a cooldown
    earned by the previous cookies should not keep blocking the user.
    """
    raw = (Config.INSTAGRAM_COOKIES or "").strip()
    fp = str(hash(raw))
    if _LAST_COOKIE_FINGERPRINT["v"] and _LAST_COOKIE_FINGERPRINT["v"] != fp:
        clear_rate_limit()
        _COOKIE_HEALTH.update({"ok": None, "detail": "", "ts": 0.0})
    _LAST_COOKIE_FINGERPRINT["v"] = fp


def cookies_as_dict() -> Dict[str, str]:
    """Parse Netscape-format OR 'k=v; k=v' cookie string into a dict."""
    _note_cookie_change()      # fresh cookies clear a stale cooldown
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


def ig_proxy() -> Optional[str]:
    """Configured Instagram proxy, if any."""
    return (Config.INSTAGRAM_PROXY or "").strip() or None


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


def _pick_best_video(candidates: List[Dict]) -> Optional[str]:
    """Choose a video rendition, avoiding silent preview tracks.

    Instagram stories sometimes include muted "preview"/"dash" renditions
    alongside the real one. Those often download fine but play with no
    sound, so prefer plain progressive MP4s and only fall back to the
    largest candidate when nothing better is available.
    """
    if not candidates:
        return None

    def _score(c: Dict) -> tuple:
        url = (c.get("url") or "")
        low = url.lower()
        # Penalise renditions that are typically video-only / silent
        silent = any(tag in low for tag in
                     ("_n.mp4", "dash", "preview", "novideo", "audio_only"))
        has_type = c.get("type")
        # type 101/102/103 are the standard progressive renditions
        good_type = 1 if (has_type in (101, 102, 103) or has_type is None) else 0
        area = (c.get("width") or 0) * (c.get("height") or 0)
        return (0 if silent else 1, good_type, area)

    try:
        best = max(candidates, key=_score)
    except Exception:
        best = candidates[0]
    return best.get("url")


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


def _extract_music(node: Dict) -> Dict:
    """Pull audio attribution out of a media node.

    Instagram exposes this as clips_metadata: music_info for a licensed
    track, original_sound_info when the creator recorded their own.
    Returns empty strings when neither is present (ordinary photo posts).
    """
    out = {"music_title": "", "music_artist": "", "music_is_original": False}
    clips = node.get("clips_metadata") or {}
    if not isinstance(clips, dict):
        return out

    mi = ((clips.get("music_info") or {}).get("music_asset_info")) or {}
    if mi.get("title") or mi.get("display_artist"):
        out["music_title"] = _clean_text(mi.get("title") or "")
        out["music_artist"] = _clean_text(mi.get("display_artist") or "")
        return out

    osi = clips.get("original_sound_info") or {}
    if osi:
        out["music_title"] = _clean_text(osi.get("original_audio_title") or "")
        artist = osi.get("ig_artist") or {}
        out["music_artist"] = _clean_text(
            artist.get("username") or osi.get("username") or "")
        out["music_is_original"] = True
    return out


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
        **_extract_music(node),
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
            "likes": 0, "views": 0, "taken_at": 0,
            # Audio attribution: reels carry either a licensed track or the
            # creator's own recording, under clips_metadata.
            "music_title": "", "music_artist": "", "music_is_original": False}


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
            proxy=ig_proxy(), headers={"User-Agent": DESKTOP_UA, "Accept-Language": "en-US,en;q=0.9"},
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
                url, proxy=ig_proxy(), headers=headers, timeout=aiohttp.ClientTimeout(total=30)
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


def _deep_unescape(text: str) -> str:
    """Collapse Instagram's nested backslash escaping.

    The embed payload is JSON inside JSON inside HTML, so a single slash
    can arrive as \\/ or \\\\/ or deeper, and the depth has changed over
    time. Unescaping repeatedly until it settles keeps the parser working
    when Instagram adds another layer.
    """
    prev = None
    out = text or ""
    for _ in range(6):
        if out == prev:
            break
        prev = out
        out = out.replace('\\\\/', '/').replace('\\/', '/').replace('\\"', '"')
    return out


async def _try_embed(session: aiohttp.ClientSession, shortcode: str) -> Tuple[List[Dict], Dict]:
    """Embed page needs no auth — great fallback for single public photos."""
    url = f"https://www.instagram.com/p/{shortcode}/embed/captioned/"
    try:
        # Use the full browser-like header set. With only User-Agent and
        # Accept-Language, Instagram serves a 628 KB shell that contains no
        # media at all; the same URL with _base_headers() returns the real
        # 273 KB payload carrying video_url. That difference is what broke
        # reel and story downloads.
        async with session.get(
            url,
            proxy=ig_proxy(), headers=_base_headers(),
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

    # Video first: reels have no usable image, and the embed inlines the
    # real CDN link. Instagram escapes it heavily and the depth varies -
    # currently \\\/ per slash - so normalise every depth before matching
    # rather than assuming one shape.
    flat = _deep_unescape(body)
    vm = re.search(r'"video_url"\s*:\s*"(https://[^"\\\s]+)"', flat)
    if vm:
        vurl = html.unescape(vm.group(1)).replace("\\u0026", "&")
        if "cdninstagram" in vurl or "fbcdn" in vurl:
            thumb = ""
            tm = re.search(r'"display_url"\s*:\s*"(https://[^"\\\s]+)"', flat)
            if tm:
                thumb = html.unescape(tm.group(1)).replace("\\u0026", "&")
            return [_item(vurl, True, 1, thumb)], _meta_from_embed(body)

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

    # Last resort: pull any media CDN link out of the flattened payload
    for pat in (r'(https://[^"\\\s]*cdninstagram[^"\\\s]*\.mp4[^"\\\s]*)',
                r'(https://[^"\\\s]*fbcdn[^"\\\s]*\.mp4[^"\\\s]*)'):
        m = re.search(pat, flat)
        if m:
            return [_item(html.unescape(m.group(1)), True, 1)], _meta_from_embed(body)
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
            proxy=ig_proxy(), headers={"User-Agent": DESKTOP_UA, "Accept-Language": "en-US,en;q=0.9"},
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


# How many times to retry a single media file before giving up.
_MEDIA_RETRIES = 4


async def _download_one(
    session: aiohttp.ClientSession, item: Dict, output_dir: str, shortcode: str
) -> Optional[str]:
    """Download one media file, retrying transient failures.

    Previously a single dropped connection lost the file outright, which is
    the main reason downloads "mostly failed": Instagram's CDN routinely
    resets long transfers. We now retry with backoff, resume partial
    transfers with a Range request, and verify the result.
    """
    media_url = item["url"]
    ext = _guess_ext(media_url, item["is_video"])
    name = f"{shortcode or 'instagram'}_{item['index']:02d}{ext}"
    dest = os.path.join(output_dir, name)
    base_headers = {
        # Ask the CDN for JPEG first — avoids WebP when the CDN will honour it
        "User-Agent": DESKTOP_UA,
        "Referer": "https://www.instagram.com/",
        "Accept": ("video/*,*/*;q=0.8" if item["is_video"]
                   else "image/jpeg,image/png;q=0.9,*/*;q=0.5"),
    }

    last_err = ""
    for attempt in range(1, _MEDIA_RETRIES + 1):
        headers = dict(base_headers)
        have = 0
        # Resume instead of restarting when a previous attempt left bytes
        if os.path.exists(dest):
            have = os.path.getsize(dest)
            if have > 1024:
                headers["Range"] = f"bytes={have}-"
            else:
                have = 0

        try:
            # sock_read guards against a CDN that stalls mid-body: without
            # it a stalled transfer hangs until the total timeout expires.
            async with session.get(
                media_url, proxy=ig_proxy(), headers=headers,
                timeout=aiohttp.ClientTimeout(total=900, sock_read=45,
                                              sock_connect=30)
            ) as r:
                # 403/410 mean the signed CDN URL expired — retrying the same
                # URL cannot help, so report it for a metadata refresh.
                if r.status in (403, 410):
                    return None
                if r.status == 416:          # range not satisfiable → restart
                    _unlink(dest)
                    last_err = "range rejected"
                    continue
                if r.status == 429:
                    note_rate_limit()
                    last_err = "rate limited"
                    await asyncio.sleep(min(5 * attempt, 20))
                    continue
                if r.status not in (200, 206):
                    last_err = f"HTTP {r.status}"
                    await asyncio.sleep(min(2 * attempt, 10))
                    continue

                # A 200 to a Range request means the server ignored it
                mode = "ab" if (r.status == 206 and have) else "wb"
                expected = r.content_length
                if mode == "ab" and expected is not None:
                    expected += have

                async def _pump():
                    with open(dest, mode) as fh:
                        async for chunk in r.content.iter_chunked(1 << 16):
                            fh.write(chunk)

                # Hard ceiling so a half-delivered body cannot hang forever
                await asyncio.wait_for(_pump(), timeout=900)

            size = os.path.getsize(dest) if os.path.exists(dest) else 0
            # Detect truncation so a half file is never passed off as success
            if expected and size < expected:
                last_err = f"truncated {size}/{expected}"
                await asyncio.sleep(min(2 * attempt, 10))
                continue
            if size >= 1024:
                return await _finalise_media(dest, item)
            last_err = f"too small ({size} bytes)"
            _unlink(dest)

        except (asyncio.TimeoutError, aiohttp.ClientError) as e:
            last_err = type(e).__name__
        except Exception as e:
            last_err = str(e)[:60]

        if attempt < _MEDIA_RETRIES:
            await asyncio.sleep(min(1.5 * attempt, 8))

    _log_media_failure(name, last_err)
    _unlink(dest)
    return None


def _unlink(path: str):
    try:
        if os.path.exists(path):
            os.remove(path)
    except Exception:
        pass


def _log_media_failure(name: str, err: str):
    try:
        print(f"[instagram] media failed after {_MEDIA_RETRIES} tries: {name} ({err})")
    except Exception:
        pass


async def _finalise_media(dest: str, item: Dict) -> Optional[str]:
    """Post-process a downloaded file and return its final path."""

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


# Last error yt-dlp reported, surfaced in messages so a failure explains itself
_LAST_YTDLP_ERROR: Dict[str, str] = {"v": ""}


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
        # More persistence: Instagram's CDN drops long transfers often
        "--retries", "10",
        "--fragment-retries", "10",
        "--retry-sleep", "exp=1:30",
        "--file-access-retries", "5",
        "--concurrent-fragments", "4",
        # Prefer a single progressive MP4 (no ffmpeg merge needed), then fall
        # back to merging. Avoids failures on hosts without ffmpeg.
        "--format",
        "best[ext=mp4][vcodec!=none][acodec!=none]/"
        "bestvideo*+bestaudio/bestvideo+bestaudio/best",
        "--merge-output-format", "mp4",
        "--output", out_tmpl,
        "--yes-playlist",
        "--ignore-errors",
        "--no-warnings",
        "--no-abort-on-error",
    ]
    if write_info:
        cmd += ["--write-info-json"]
    cmd += [url]
    before = {p.name for p in Path(output_dir).iterdir()} if Path(output_dir).exists() else set()
    err = b""
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
        )
        _, err = await asyncio.wait_for(proc.communicate(), timeout=Config.YTDL_TIMEOUT_SEC)
    except asyncio.TimeoutError:
        try:
            proc.kill()
        except Exception:
            pass
        _LAST_YTDLP_ERROR["v"] = "yt-dlp timed out"
        return []
    except FileNotFoundError:
        _LAST_YTDLP_ERROR["v"] = "yt-dlp is not installed on the server"
        return []
    except Exception as e:
        _LAST_YTDLP_ERROR["v"] = str(e)[:200]
        return []

    if err:
        text = err.decode("utf-8", "ignore")
        for line in text.splitlines():
            if "ERROR" in line or "error" in line.lower():
                _LAST_YTDLP_ERROR["v"] = line.strip()[:200]
                break

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
            f"{who}<a href=\"https://www.instagram.com/{_esc(username)}/\">"
            f"<b>@{_esc(username)}</b></a>"
        )
    elif full_name:
        parts.append(f"<b>{_esc(full_name)}</b>")

    music = ""
    mt, ma = (meta.get("music_title") or "").strip(), (meta.get("music_artist") or "").strip()
    if mt or ma:
        label = f"{mt} - {ma}" if (mt and ma) else (mt or ma)
        tag = "Original audio" if meta.get("music_is_original") else "Audio"
        music = f"{tag}: <b>{_esc(label)}</b>"

    stats = []
    if meta.get("likes"):
        stats.append(f"Likes <b>{int(meta['likes']):,}</b>")
    if meta.get("views"):
        stats.append(f"Views <b>{int(meta['views']):,}</b>")
    if stats:
        parts.append("  \u2022  ".join(stats))
    if music:
        parts.append(music)

    header = "\n".join(p for p in parts if p)

    footer = ""
    if url:
        footer = f'<a href="{_esc(url)}"><b>Open on Instagram</b></a>'

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

    header = "<b>Full Description</b>"
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
        head = header if i == 0 else f"<b>Full Description ({i + 1}/{len(chunks)})</b>"
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


# One download attempt fans out across several endpoints (4 GraphQL doc_ids,
# API v1, embed). Each can answer 429, but they are ONE incident — counting
# them separately escalated a single attempt straight to the hour-long step.
_INCIDENT_WINDOW = 90


def note_rate_limit() -> float:
    """Record a 429 and return the UNIX timestamp when retrying is allowed.

    429s arriving within _INCIDENT_WINDOW of each other are treated as a
    single incident, so the backoff only escalates when the *user* retries
    and is limited again — not once per internal sub-request.
    """
    now = time.time()
    last = float(_RATE_LIMIT.get("last_hit") or 0)

    # Same incident → just refresh the timestamp, keep the existing ETA.
    if last and now - last <= _INCIDENT_WINDOW:
        _RATE_LIMIT["last_hit"] = now
        return float(_RATE_LIMIT.get("until") or 0)

    # Reset the escalation if the limit has been quiet for a while.
    if now - last > 7200:
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
                "Ab try kar sakte ho.")
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
    async with session.get(url, proxy=ig_proxy(), headers=headers,
                           timeout=aiohttp.ClientTimeout(total=30)) as r:
        if r.status == 404:
            raise InstagramError(f"<b>@{username}</b> nahi mila.")
        if r.status in (401, 403):
            raise InstagramError(_friendly_error("profile"))
        if r.status == 429:
            note_rate_limit()
            raise RateLimited()
        if r.status != 200:
            raise InstagramError(f"Instagram ne HTTP {r.status} diya.")
        data = await r.json(content_type=None)
    user = (data.get("data") or {}).get("user")
    if not user:
        raise InstagramError(_friendly_error("profile"))
    return user


async def _profile_posts_from_embed(username: str, limit: int) -> Tuple[List[Dict], Dict]:
    """Read a profile's recent posts from the public embed page.

    The private web_profile_info API is refused outright to hosted
    addresses (HTTP 429), but /<user>/embed/ still answers 200 and its
    payload carries real shortcodes. Values are JSON-escaped inside the
    HTML, so the page is unescaped before scanning.

    Returns fewer posts than the API - the embed only exposes a recent
    window - but it works where the API cannot.
    """
    # Instagram's embed endpoint is case-sensitive: /Zhas_gfp/embed/ returns
    # a 200 with an empty shell, while /zhas_gfp/embed/ returns the real
    # payload. Usernames are lowercase canonically, so normalise.
    username = (username or "").strip().lstrip("@").lower()
    url = f"https://www.instagram.com/{username}/embed/"
    jar = cookies_as_dict()

    # Instagram sometimes drops the connection outright rather than
    # answering with a status code; aiohttp surfaces that as
    # ClientResponseError(status=0) or a ClientError. Retry briefly and
    # never let it escape - the caller treats an empty result as "no data".
    raw = ""
    for attempt in range(3):
        cookie_jar = aiohttp.CookieJar(unsafe=True)
        try:
            async with aiohttp.ClientSession(cookie_jar=cookie_jar) as session:
                if jar:
                    session.cookie_jar.update_cookies(
                        jar,
                        response_url=aiohttp.helpers.URL("https://www.instagram.com"))
                async with session.get(
                        url, proxy=ig_proxy(), headers=_base_headers(), allow_redirects=True,
                        timeout=aiohttp.ClientTimeout(total=30)) as r:
                    if r.status == 200:
                        raw = await r.text()
                        if raw:
                            break
                    elif r.status == 429:
                        note_rate_limit()
        except Exception:
            pass
        if attempt < 2:
            await asyncio.sleep(1.5 * (attempt + 1))

    if not raw:
        return [], {}

    try:
        txt = raw.encode("utf-8", "ignore").decode("unicode_escape", "ignore")
    except Exception:
        txt = raw
    txt = txt.replace("\\/", "/")

    seen, codes = set(), []
    for sc in re.findall(r'"shortcode"\s*:\s*"([A-Za-z0-9_-]{5,})"', txt):
        if sc not in seen:
            seen.add(sc)
            codes.append(sc)
    if not codes:
        return [], {}

    posts = [{"shortcode": sc,
              "url": f"https://www.instagram.com/p/{sc}/",
              "is_video": False, "caption": ""}
             for sc in codes[: max(1, min(limit, 50))]]

    def _first_int(pattern: str) -> int:
        m = re.search(pattern, txt)
        try:
            return int(m.group(1)) if m else 0
        except (TypeError, ValueError):
            return 0

    info = {
        "username": username,
        "full_name": "",
        "biography": "",
        "followers": _first_int(r'"edge_followed_by"\s*:\s*\{"count"\s*:\s*(\d+)'),
        "posts_total": _first_int(
            r'"edge_owner_to_timeline_media"\s*:\s*\{"count"\s*:\s*(\d+)'),
        "is_private": '"is_private":true' in txt.replace(" ", ""),
        "profile_pic": "",
        "source": "embed",
    }
    return posts, info


async def fetch_profile_posts(username: str, limit: int = 12) -> Tuple[List[Dict], Dict]:
    """Return (list of {shortcode,url,is_video,caption}, profile info)."""
    username = (username or "").lstrip("@")

    # The public embed is tried FIRST, always. web_profile_info answers 429
    # to hosted addresses on every single call, so gating the embed behind
    # "only when a cooldown is active" meant one failed embed attempt
    # surfaced the cooldown wall instead of simply retrying — which is
    # exactly the "instant timer" users saw.
    posts, info = await _profile_posts_from_embed(username, limit)
    if posts:
        return posts, info

    jar = cookies_as_dict()
    cookie_jar = aiohttp.CookieJar(unsafe=True)
    try:
        async with aiohttp.ClientSession(cookie_jar=cookie_jar) as session:
            if jar:
                session.cookie_jar.update_cookies(
                    jar, response_url=aiohttp.helpers.URL("https://www.instagram.com"))
            user = await _profile_info(session, username)
    except InstagramError:
        posts, info = await _profile_posts_from_embed(username, limit)
        if posts:
            return posts, info
        raise

    if user.get("is_private") and not user.get("followed_by_viewer"):
        raise InstagramError(
            f"<b>@{username}</b> private hai aur aap follow nahi karte.")

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
            "Stories ke liye login zaroori hai.\n\n"
            "<b>Fix:</b> <code>INSTAGRAM_COOKIES</code> set karo.")

    jar = cookies_as_dict()
    cookie_jar = aiohttp.CookieJar(unsafe=True)
    async with aiohttp.ClientSession(cookie_jar=cookie_jar) as session:
        if jar:
            session.cookie_jar.update_cookies(
                jar, response_url=aiohttp.helpers.URL("https://www.instagram.com"))
        user = await _profile_info(session, username)
        uid = user.get("id")
        if not uid:
            raise InstagramError(f"<b>@{username}</b> ki ID nahi mili.")

        headers = _base_headers()
        if jar.get("csrftoken"):
            headers["X-CSRFToken"] = jar["csrftoken"]
        url = ("https://i.instagram.com/api/v1/feed/reels_media/"
               f"?reel_ids={uid}")
        async with session.get(url, proxy=ig_proxy(), headers=headers,
                               timeout=aiohttp.ClientTimeout(total=30)) as r:
            if r.status in (401, 403):
                raise InstagramError(_friendly_error("story"))
            if r.status != 200:
                raise InstagramError(f"Stories fetch failed (HTTP {r.status}).")
            data = await r.json(content_type=None)

    reels = data.get("reels") or data.get("reels_media") or {}
    node = reels.get(str(uid)) if isinstance(reels, dict) else None
    if isinstance(reels, list) and reels:
        node = reels[0]
    items_raw = (node or {}).get("items") or []
    if not items_raw:
        raise InstagramError(f"<b>@{username}</b> ki koi active story nahi hai.")

    items: List[Dict] = []
    for i, it in enumerate(items_raw, 1):
        vids = it.get("video_versions") or []
        if vids:
            u = _pick_best_video(vids)
            if u:
                thumb = _pick_best((it.get("image_versions2") or {}).get("candidates") or [])
                items.append(_item(u, True, i, thumb or ""))
                continue
        imgs = (it.get("image_versions2") or {}).get("candidates") or []
        u = _pick_best(imgs)
        if u:
            items.append(_item(u, False, i))

    info = {"username": user.get("username") or username,
            "full_name": _clean_text(user.get("full_name") or ""),
            "count": len(items)}
    return items, info


async def has_audio_track(path: str) -> bool:
    """True if the file contains at least one audio stream.

    Tries ffprobe first, then falls back to parsing `ffmpeg -i` output
    (some images ship ffmpeg without ffprobe). If neither tool exists we
    return True, so a missing binary never produces a false "no audio"
    warning.
    """
    # Preferred: ffprobe, machine-readable
    try:
        proc = await asyncio.create_subprocess_exec(
            "ffprobe", "-v", "error", "-select_streams", "a",
            "-show_entries", "stream=codec_type", "-of", "csv=p=0", path,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=30)
        return b"audio" in (out or b"")
    except FileNotFoundError:
        pass
    except Exception:
        return True

    # Fallback: ffmpeg writes stream info to stderr and exits non-zero
    try:
        proc = await asyncio.create_subprocess_exec(
            "ffmpeg", "-hide_banner", "-i", path,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE)
        _, err = await asyncio.wait_for(proc.communicate(), timeout=30)
        return b"Audio:" in (err or b"")
    except Exception:
        return True


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
                proxy=ig_proxy(), headers=headers, timeout=aiohttp.ClientTimeout(total=20),
            ) as r:
                if r.status == 200:
                    try:
                        data = await r.json(content_type=None)
                    except Exception:
                        data = {}
                    if (data.get("data") or {}).get("user"):
                        # Instagram is answering us again — any cooldown is over.
                        clear_rate_limit()
                        ok, detail = True, "Cookies are valid and logged in."
                    else:
                        ok, detail = False, "Instagram returned an empty profile — session likely expired."
                elif r.status in (401, 403):
                    ok, detail = False, f"Instagram rejected the session (HTTP {r.status}) — cookies expired."
                elif r.status == 429:
                    # Rate limiting is not a cookie problem; do not cry wolf.
                    # Deliberately NOT calling note_rate_limit(): this is our
                    # own background probe, so it must never extend the
                    # cooldown the user is waiting on.
                    ok, detail = True, "Rate-limited (HTTP 429) — cookies presumed valid."
                else:
                    ok, detail = False, f"Unexpected response from Instagram (HTTP {r.status})."
    except Exception as e:
        ok, detail = False, f"Cookie check failed: {str(e)[:120]}"

    _COOKIE_HEALTH.update({"ok": ok, "detail": detail, "ts": now})
    return ok, detail


async def diagnose() -> str:
    """Probe each Instagram endpoint and report exactly what it returns.

    Used by /igtest so a failing deployment can be diagnosed from Telegram
    instead of guessing.
    """
    jar = cookies_as_dict()
    out = ["<b>Instagram Diagnostics</b>", ""]

    # 1. Cookie presence and shape
    if not jar:
        out.append("Cookies: <b>none configured</b>")
    else:
        keys = ", ".join(sorted(jar.keys())[:8])
        out.append(f"Cookies: {len(jar)} keys")
        out.append(f"   <code>{keys}</code>")
        for need in ("sessionid", "csrftoken", "ds_user_id"):
            out.append(f"   {'' if jar.get(need) else ''} {need}")
        sid = jar.get("sessionid", "")
        if sid and "%3A" not in sid and ":" not in sid:
            out.append("<i>sessionid looks malformed</i>")

    # 2. Current cooldown
    rl = rate_limit_remaining()
    out += ["", f"⏳ Cooldown: {'<b>' + _fmt_duration(rl) + '</b> left' if rl else ' clear'}"]

    # 3. Live endpoint probes
    out += ["", "<b>Endpoint checks</b>"]
    cookie_jar = aiohttp.CookieJar(unsafe=True)
    async with aiohttp.ClientSession(cookie_jar=cookie_jar) as session:
        if jar:
            session.cookie_jar.update_cookies(
                jar, response_url=aiohttp.helpers.URL("https://www.instagram.com"))
        headers = _base_headers()
        if jar.get("csrftoken"):
            headers["X-CSRFToken"] = jar["csrftoken"]

        probes = [
            ("web_profile_info",
             "https://www.instagram.com/api/v1/users/web_profile_info/?username=instagram"),
            # A real, permanently-public profile embed. The previous probe
            # used an invented shortcode, so its result said nothing about
            # whether the embed path actually works.
            ("embed page",
             "https://www.instagram.com/instagram/embed/"),
        ]
        results: Dict[str, int] = {}
        for name, url in probes:
            last_exc = None
            for attempt in range(2):
                try:
                    async with session.get(
                        url, proxy=ig_proxy(), headers=headers,
                        timeout=aiohttp.ClientTimeout(total=15)) as r:
                        body = await r.read()
                        results[name] = r.status
                        break
                except Exception as e:
                    last_exc = e
                    if attempt == 0:
                        await asyncio.sleep(1.0)
            else:
                results[name] = -1
                out.append(f"{name}: <code>{str(last_exc)[:60]}</code>")
                continue
            try:
                status = results[name]
                note = ""
                if status == 429:
                    note = " - rate-limited"
                elif status in (401, 403):
                    note = " - login required / cookies rejected"
                elif status == 200 and len(body) < 100:
                    note = " - empty response"
                out.append(f"{name}: <b>HTTP {status}</b>"
                           f" ({len(body)} bytes){note}")
            except Exception as e:
                out.append(f"{name}: <code>{str(e)[:60]}</code>")

    api_ok = results.get("web_profile_info") == 200
    embed_ok = results.get("embed page") == 200
    out.append("")
    if api_ok and embed_ok:
        out.append("<b>Verdict:</b> all endpoints are responding.")
    elif embed_ok and not api_ok:
        out.append(
            "<b>Verdict:</b> the embed works; the private API is refused.\n"
            "- <b>Reels and posts</b> download normally\n"
            "- <b>/profile</b> uses the embed and returns recent posts\n"
            "- <b>Stories</b> need the private API and will not work\n\n"
            "<i>A partial address block. Set INSTAGRAM_PROXY for a "
            "permanent fix.</i>")
    elif not embed_ok and not api_ok:
        out.append(
            "<b>Verdict:</b> no endpoint responded.\n"
            "<i>Cookies cannot fix an address block - set INSTAGRAM_PROXY "
            "or move to a different host.</i>")
    else:
        out.append("<b>Verdict:</b> mixed results - see the details above.")
    return "\n".join(out)


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
        # Don't lead with "cookies expired": when the embed path still works
        # the cookies are usually fine and it is the private API that is
        # blocked for this IP. Telling the user to re-export cookies then
        # sends them chasing the wrong fix.
        story_note = ""
        if kind in ("story", "highlight"):
            story_note = (
                "\n<b>Stories ke liye Instagram ki private API chahiye</b>, "
                "jo embed fallback se nahi milti — isliye reels chalne ke "
                "bawajood stories fail ho sakti hain.\n")
        return (
            "<b>Instagram ne ye request block kar di.</b>\n"
            f"{story_note}\n"
            "Possible wajah:\n"
            "• Server IP par API rate-limit (sabse aam)\n"
            "• Story expire ho gayi / delete ho gayi\n"
            "• Account private hai aur aap follow nahi karte\n"
            "• Session sach me expire ho gayi\n\n"
            "<b>Pehle ye chalao:</b> <code>/igtest</code> —"
            "wo batayega ki cookies ka issue hai ya IP ka.\n"
            "<i>Agar embed 200 aur API 429 dikhe, to cookies theek hain; "
            "IP block hai aur kuch ghante baad khud chalne lagega.</i>"
        )
    extra = " (Stories/Highlights ke liye login zaroori hai.)" if kind in ("story", "highlight") else ""
    return (
        f"<b>Instagram login required.</b>{extra}\n\n"
        "Instagram ab bina login ke server se media nahi deta.\n\n"
        "<b>Fix:</b> <code>INSTAGRAM_COOKIES</code> env variable mein"
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

    _LAST_YTDLP_ERROR["v"] = ""       # don't report a previous run's error
    saved: List[str] = []

    # ── Path A: direct CDN download via Instagram's own APIs ──
    try:
        items, meta = await fetch_post(url)
    except Exception:
        items, meta = [], _empty_meta()

    if items:
        saved = await _download_items(items, output_dir, shortcode)

        # Some items failed: the signed CDN URLs may simply have gone stale
        # between fetching metadata and downloading. Re-fetch once and retry
        # only the missing ones rather than losing the whole post.
        if len(saved) < len(items):
            try:
                fresh_items, fresh_meta = await fetch_post(url)
            except Exception:
                fresh_items, fresh_meta = [], None
            if fresh_items and len(fresh_items) == len(items):
                got = {os.path.basename(p).split("_")[-1].split(".")[0]
                       for p in saved}
                missing = [it for it in fresh_items
                           if f"{it['index']:02d}" not in got]
                if missing:
                    more = await _download_items(missing, output_dir, shortcode)
                    saved = sorted(set(saved) | set(more),
                                   key=lambda p: os.path.basename(p))
                if fresh_meta and not meta.get("caption"):
                    meta = fresh_meta

    if saved:
        clear_rate_limit()
        # Deliver what we have; a partial carousel beats a hard failure.
        if len(saved) < len(items):
            meta["partial"] = f"{len(saved)}/{len(items)}"
        return saved, meta

    # ── Path B: yt-dlp (reels, stories, highlights) ──
    saved, yt_meta = await _ytdlp_fallback_with_meta(url, output_dir)
    if saved:
        for k, v in (yt_meta or {}).items():
            if v and not meta.get(k):
                meta[k] = v
        return saved, meta

    # Nothing worked — include the concrete reason when we have one, so the
    # user is not left with a generic "blocked" message.
    detail = _LAST_YTDLP_ERROR.get("v") or ""
    msg = _friendly_error(kind)
    if detail:
        low = detail.lower()
        if "not installed" in low:
            msg = ("<b>yt-dlp server par installed nahi hai.</b>\n\n"
                   "Owner: <code>pip install -U yt-dlp</code> chalao "
                   "ya requirements.txt se redeploy karo.")
        elif "login" in low or "rate-limit" in low or "429" in low:
            pass      # the friendly message already covers this
        else:
            msg += f"\n\n<i>Technical: <code>{_esc(detail[:150])}</code></i>"
    raise InstagramError(msg)


async def _download_items(items: List[Dict], output_dir: str,
                          shortcode: str) -> List[str]:
    """Download a batch of media items concurrently."""
    connector = aiohttp.TCPConnector(limit=4, force_close=True)
    timeout = aiohttp.ClientTimeout(total=None, sock_connect=30)
    async with aiohttp.ClientSession(connector=connector, timeout=timeout) as session:
        results = await asyncio.gather(
            *[_download_one(session, it, output_dir, shortcode) for it in items],
            return_exceptions=True,
        )
    saved = [r for r in results if isinstance(r, str) and r]
    saved.sort(key=lambda p: os.path.basename(p))
    return saved


async def download_instagram(url: str, output_dir: str) -> List[str]:
    """Backwards-compatible helper — file paths only."""
    files, _ = await download_post(url, output_dir)
    return files


# Backwards-compatible alias used by older bot.py code paths
async def download_instagram_photos(url: str, output_dir: str) -> List[str]:
    return await download_instagram(url, output_dir)

import json
import os
import re
import time
from typing import Any, Optional
from urllib.parse import parse_qs, unquote, urlparse

import aiohttp
from pyrogram.types import Message

from utils.progress import progress_for_pyrogram


_VIDEO_CT_EXT = {
    "video/mp4": ".mp4",
    "video/x-matroska": ".mkv",
    "video/webm": ".webm",
    "video/quicktime": ".mov",
    "video/x-msvideo": ".avi",
    "video/mp2t": ".ts",
}
_AUDIO_CT_EXT = {
    "audio/mpeg": ".mp3",
    "audio/mp4": ".m4a",
    "audio/aac": ".aac",
    "audio/ogg": ".ogg",
    "audio/opus": ".opus",
    "audio/flac": ".flac",
    "audio/wav": ".wav",
    "audio/x-wav": ".wav",
}
_ARCHIVE_CT_EXT = {
    "application/zip": ".zip",
    "application/x-zip-compressed": ".zip",
    "application/x-rar-compressed": ".rar",
    "application/vnd.rar": ".rar",
    "application/x-7z-compressed": ".7z",
    "application/pdf": ".pdf",
    "application/vnd.android.package-archive": ".apk",
}
_CT_EXT = {**_VIDEO_CT_EXT, **_AUDIO_CT_EXT, **_ARCHIVE_CT_EXT}


def _safe_name(name: str) -> str:
    name = unquote(str(name or "")).strip().strip('"\'')
    name = re.sub(r"[\\/:*?\"<>|\r\n\t]+", "_", name).strip(". ")
    return (name or "file")[:180]


def _filename_from_cd(cd: str) -> Optional[str]:
    """
    Parse filename from Content-Disposition header.
    Supports: filename="..." and filename*=UTF-8''...
    """
    if not cd:
        return None

    # filename*=
    m = re.search(r"filename\*\s*=\s*[^']*'[^']*'(?P<fn>[^;]+)", cd, flags=re.I)
    if m:
        return _safe_name(m.group("fn"))

    # filename=
    m = re.search(r'filename\s*=\s*"?(?P<fn>[^";]+)"?', cd, flags=re.I)
    if m:
        return _safe_name(m.group("fn"))

    return None


def _ext_from_ctype(ctype: str) -> str:
    ct = (ctype or "").split(";", 1)[0].strip().lower()
    if ct in _CT_EXT:
        return _CT_EXT[ct]
    if ct.startswith("video/"):
        return ".mp4"
    if ct.startswith("audio/"):
        return ".mp3"
    return ""


def _filename_from_url(url: str) -> str:
    try:
        parsed = urlparse(url)
    except Exception:
        return ""
    base = os.path.basename(parsed.path or "")
    if base:
        return _safe_name(base)
    # Some direct gateways keep filename in query even when path is only '/'.
    qs = parse_qs(parsed.query or "")
    for key in ("filename", "file", "name", "title"):
        value = qs.get(key)
        if value and value[0]:
            return _safe_name(value[0])
    return ""


def _first_nested_url(obj: Any) -> str:
    keys = (
        "download_url", "downloadUrl", "download", "url", "file_url",
        "fileUrl", "direct_link", "directLink", "link", "href",
    )
    if isinstance(obj, dict):
        for k in keys:
            v = obj.get(k)
            if isinstance(v, str) and v.startswith(("http://", "https://")):
                return v
        for v in obj.values():
            got = _first_nested_url(v)
            if got:
                return got
    elif isinstance(obj, list):
        for v in obj:
            got = _first_nested_url(v)
            if got:
                return got
    return ""


def _text_url(text: str) -> str:
    m = re.search(r"https?://[^\s'\"<>]+", text or "")
    return m.group(0).rstrip(".,);") if m else ""


async def download_file(
    url: str,
    dest_path: str,
    chunk_size: int = 64 * 1024,
    timeout: Optional[int] = None,
    status_message: Optional[Message] = None,
    file_name: Optional[str] = None,
    direction: str = "from web",
) -> str:
    """
    HTTP downloader with optional Telegram-style progress bar.

    Returns: final saved file path (with proper filename if server sends it).
    """
    dest_dir = os.path.dirname(dest_path) or "."
    os.makedirs(dest_dir, exist_ok=True)

    timeout_cfg = aiohttp.ClientTimeout(total=timeout)
    # Some hosts reject requests with no User-Agent outright. Sending a
    # browser-like one costs nothing and avoids an avoidable class of 403.
    _hdrs = {
        "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                       "AppleWebKit/537.36 (KHTML, like Gecko) "
                       "Chrome/125.0.0.0 Safari/537.36"),
        "Accept": "*/*",
    }

    current_url = url
    async with aiohttp.ClientSession(timeout=timeout_cfg) as session:
        for hop in range(3):
            async with session.get(current_url, headers=_hdrs,
                                   allow_redirects=True) as resp:
                resp.raise_for_status()
                total = int(resp.headers.get("Content-Length") or 0)
                cd = resp.headers.get("Content-Disposition", "")
                ctype = resp.headers.get("Content-Type", "") or ""
                ctype_base = ctype.split(";", 1)[0].strip().lower()
                final_url = str(resp.url)

                # Some token endpoints return JSON/text containing the real
                # file URL. Follow it instead of saving that response as a file.
                if ctype_base.startswith("text/") or "json" in ctype_base:
                    text = await resp.text(errors="ignore")
                    nested = ""
                    if "json" in ctype_base:
                        try:
                            nested = _first_nested_url(json.loads(text))
                        except Exception:
                            nested = ""
                    nested = nested or _text_url(text)
                    if nested and nested != current_url and hop < 2:
                        current_url = nested
                        continue
                    snippet = re.sub(r"\s+", " ", text).strip()[:220]
                    raise RuntimeError(
                        f"Direct link did not return a file. Content-Type {ctype_base or 'unknown'}"
                        + (f": {snippet}" if snippet else "")
                    )

                header_name = _filename_from_cd(cd)

                # Guess base filename. Prefer response headers, then final URL
                # after redirects, then original URL, then caller-provided name.
                fname = (header_name or _filename_from_url(final_url)
                         or _filename_from_url(current_url)
                         or file_name or os.path.basename(dest_path) or "file")
                fname = _safe_name(fname)
                ext = os.path.splitext(fname)[1]
                ct_ext = _ext_from_ctype(ctype)
                if not ext and ct_ext:
                    fname += ct_ext

                final_path = os.path.join(dest_dir, fname)

                downloaded = 0
                start = time.time()

                with open(final_path, "wb") as f:
                    async for chunk in resp.content.iter_chunked(chunk_size):
                        if not chunk:
                            continue
                        f.write(chunk)
                        downloaded += len(chunk)

                        if status_message:
                            await progress_for_pyrogram(
                                downloaded,
                                total,
                                status_message,
                                start,
                                fname,
                                direction,
                                known_total=total,
                            )

                if downloaded < 1024:
                    raise RuntimeError("Direct link downloaded an empty file")

                # final 100% update
                if status_message:
                    await progress_for_pyrogram(
                        downloaded,
                        total,
                        status_message,
                        start,
                        fname,
                        direction,
                        known_total=total or downloaded,
                    )

                return final_path

    raise RuntimeError("Direct link redirect chain did not produce a file")

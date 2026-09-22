# utils/youtube_api.py
"""Apify-backed YouTube downloader.

YouTube increasingly blocks hosted/datacenter addresses before yt-dlp can reach
media URLs.  This module sends the YouTube URL to the configured Apify actor,
waits for a finished run, extracts the temporary download URL from the actor's
Dataset output, and downloads that file with the bot's normal ETA display.

No API token or TeraBox/YouTube cookie is ever written to the repository or sent
to third-party download URLs.  The Apify token is attached only to api.apify.com
requests (and to Apify KV-store download URLs when the actor returns one).
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import time
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple
from urllib.parse import quote, urlparse

import aiohttp
from pyrogram.errors import FloodWait, MessageNotModified

from config import Config
from utils.progress import human_bytes, human_time, progress_for_pyrogram


class YouTubeApiError(RuntimeError):
    """Raised with a user-facing YouTube API message."""


_TOKEN_STATE: Dict[str, float] = {}
_TOKEN_COOLDOWN = 6 * 3600
_APIFY_BASE = "https://api.apify.com/v2"

_VIDEO_EXTS = (".mp4", ".mkv", ".webm", ".mov", ".m4v")
_AUDIO_EXTS = (".mp3", ".m4a", ".aac", ".opus", ".ogg", ".flac", ".wav")
_SUBTITLE_EXTS = (".srt", ".vtt", ".ass", ".ssa", ".txt")
_IMAGE_HINTS = ("thumb", "thumbnail", "avatar", "image", "poster", "cover")
_DOWNLOAD_KEY_HINTS = (
    "downloadurl", "download_url", "download", "fileurl", "file_url",
    "mediaurl", "media_url", "signedurl", "signed_url", "publicurl",
    "public_url", "recordurl", "record_url", "url",
)
_SOURCE_KEY_HINTS = ("sourceurl", "source_url", "youtubeurl", "youtube_url", "originalurl")


def is_youtube_url(url: str) -> bool:
    host = ""
    try:
        host = (urlparse(url if "://" in url else "https://" + url).hostname or "").lower()
    except Exception:
        pass
    return host in ("youtu.be", "youtube.com", "www.youtube.com", "m.youtube.com", "music.youtube.com", "youtube-nocookie.com", "www.youtube-nocookie.com") or host.endswith(".youtube.com")


def _token_attrs() -> Iterable[str]:
    return (
        "APIFY_API_TOKEN", "APIFY_API_TOKEN_2", "APIFY_API_TOKEN_3",
        "APIFY_API_TOKEN_4", "APIFY_API_TOKEN_5",
    )


def api_tokens() -> List[str]:
    keys: List[str] = []
    for name in _token_attrs():
        k = (getattr(Config, name, "") or "").strip()
        if k and k not in keys:
            keys.append(k)
    return keys


def has_api_tokens() -> bool:
    return bool(api_tokens())


def _usable_tokens() -> List[str]:
    now = time.time()
    fresh = [k for k in api_tokens() if now - _TOKEN_STATE.get(k, 0) > _TOKEN_COOLDOWN]
    # If every configured token is parked, try them all again.  This avoids a
    # hard outage if a quota reset happened earlier than expected.
    return fresh or api_tokens()


def api_key_status() -> str:
    keys = api_tokens()
    if not keys:
        return "no API token set"
    now = time.time()
    bits = []
    for i, k in enumerate(keys, 1):
        parked = now - _TOKEN_STATE.get(k, 0) <= _TOKEN_COOLDOWN
        if parked:
            left = max(1, int(_TOKEN_COOLDOWN - (now - _TOKEN_STATE.get(k, 0))) // 60)
            bits.append(f"#{i} exhausted/limited ({left}m)")
        else:
            bits.append(f"#{i} ready")
    return " · ".join(bits)


def _actor_api_id() -> str:
    # Apify API accepts user~actor-name.  If the operator pastes a public actor
    # name in user/actor form, translate it.  A raw Actor ID is left untouched.
    return (Config.APIFY_YOUTUBE_ACTOR_ID or "UUhJDfKJT2SsXdclR").strip().replace("/", "~")


def quality_from_choice(format_id: str = "", height: int = 0) -> str:
    if height and height > 0:
        return f"{int(height)}p"
    raw = (format_id or "").lower().replace("ytapi_", "")
    m = re.search(r"(\d{3,4})p?", raw)
    if m:
        return f"{m.group(1)}p"
    return (Config.APIFY_YOUTUBE_DEFAULT_QUALITY or "720p").strip() or "720p"


def youtube_api_formats() -> List[Dict[str, Any]]:
    """Fixed quality menu used when Apify is available.

    This deliberately avoids probing YouTube with yt-dlp just to build a menu,
    because the whole point of the API path is to avoid YouTube's hosted-IP
    verification wall.
    """
    return [
        {"label": "360p",  "format_id": "ytapi_360p",  "height": 360,  "ext": "mp4", "size_approx": 0},
        {"label": "480p",  "format_id": "ytapi_480p",  "height": 480,  "ext": "mp4", "size_approx": 0},
        {"label": "720p",  "format_id": "ytapi_720p",  "height": 720,  "ext": "mp4", "size_approx": 0},
        {"label": "1080p", "format_id": "ytapi_1080p", "height": 1080, "ext": "mp4", "size_approx": 0},
        {"label": "Best",  "format_id": "ytapi_best",  "height": 0,    "ext": "mp4", "size_approx": 0},
    ]


def build_run_input(url: str, quality: str, preferred_format: Optional[str] = None) -> Dict[str, Any]:
    fmt = (preferred_format or Config.APIFY_YOUTUBE_FORMAT or "mp4").strip() or "mp4"
    q = (quality or Config.APIFY_YOUTUBE_DEFAULT_QUALITY or "720p").strip() or "720p"
    data: Dict[str, Any] = {
        "videos": [{"url": url}],
        "storeInKVStore": bool(Config.APIFY_YOUTUBE_STORE_IN_KVSTORE),
        "preferredQuality": q,
        "preferredFormat": fmt,
        "filenameTemplateParts": ["title"],
    }

    # Do not send None/null cloud storage values. This actor's schema declares
    # these optional fields as strings, so JSON null causes HTTP 400 validation
    # errors such as "input.s3AccessKeyId must be string". Only include cloud
    # upload fields when the operator explicitly configured them.
    optional_cloud_fields = {
        "s3AccessKeyId": os.getenv("APIFY_YOUTUBE_S3_ACCESS_KEY_ID", "").strip(),
        "s3SecretAccessKey": os.getenv("APIFY_YOUTUBE_S3_SECRET_ACCESS_KEY", "").strip(),
        "s3Bucket": os.getenv("APIFY_YOUTUBE_S3_BUCKET", "").strip(),
        "s3Region": os.getenv("APIFY_YOUTUBE_S3_REGION", "").strip(),
        "azureConnectionString": os.getenv("APIFY_YOUTUBE_AZURE_CONNECTION_STRING", "").strip(),
        "azureContainerName": os.getenv("APIFY_YOUTUBE_AZURE_CONTAINER", "").strip(),
        "googleCloudServiceKey": os.getenv("APIFY_YOUTUBE_GCS_SERVICE_KEY", "").strip(),
        "googleCloudBucketName": os.getenv("APIFY_YOUTUBE_GCS_BUCKET", "").strip(),
    }
    data.update({k: v for k, v in optional_cloud_fields.items() if v})
    transcribe = (Config.APIFY_YOUTUBE_TRANSCRIPTION or "").strip()
    if transcribe and transcribe.lower() not in ("0", "false", "off", "no", "none", "disabled"):
        data["transcriptionAndSubtitle"] = transcribe
    return data


async def _safe_edit(message, text: str) -> None:
    if not message:
        return
    try:
        if getattr(message, "animation", None) or getattr(message, "video", None):
            await message.edit_caption(text)
        else:
            await message.edit_text(text)
    except MessageNotModified:
        pass
    except FloodWait as e:
        await asyncio.sleep(min(int(getattr(e, "value", 5)), 10))
        try:
            if getattr(message, "animation", None) or getattr(message, "video", None):
                await message.edit_caption(text)
            else:
                await message.edit_text(text)
        except Exception:
            pass
    except Exception:
        pass


def _pulse_bar(tick: int, width: int = 18) -> str:
    fill = (tick % width) + 1
    return "●" * fill + "○" * (width - fill)


async def _show_waiting(message, url: str, quality: str, start: float, tick: int) -> None:
    elapsed = human_time(int(time.time() - start))
    text = (
        "🎬 <b>YouTube API</b>\n\n"
        f"📺 <code>{url[:80]}</code>\n"
        f"🎚 Quality: <b>{quality}</b>\n"
        f"[{_pulse_bar(tick)}]\n"
        "⚙️ Preparing secure download link…\n"
        f"⌛ Elapsed: <b>{elapsed}</b>\n"
        "⏱ ETA: <i>waiting for Apify actor</i>"
    )
    await _safe_edit(message, text)


async def _request_json(session: aiohttp.ClientSession, method: str, url: str,
                        token: str, **kwargs) -> Tuple[int, Any, str]:
    headers = kwargs.pop("headers", {}) or {}
    headers.setdefault("Authorization", f"Bearer {token}")
    if kwargs.get("json") is not None:
        headers.setdefault("Content-Type", "application/json")
    async with session.request(method, url, headers=headers, **kwargs) as r:
        text = await r.text()
        try:
            data = json.loads(text) if text else {}
        except Exception:
            data = {"raw": text}
        return r.status, data, text


def _api_error(status: int, data: Any, text: str) -> str:
    if isinstance(data, dict):
        err = data.get("error") or data.get("message") or data.get("detail")
        if isinstance(err, dict):
            err = err.get("message") or json.dumps(err)[:160]
        if err:
            return str(err)[:220]
    return (text or f"HTTP {status}").strip()[:220]


def _quota_or_auth_status(status: int, message: str = "") -> bool:
    low = (message or "").lower()
    return status in (401, 402, 403, 429) or any(k in low for k in (
        "quota", "credit", "monthly usage", "rate limit", "token", "unauthorized",
        "forbidden", "payment", "insufficient",
    ))


async def _run_actor_once(session: aiohttp.ClientSession, token: str,
                          run_input: Dict[str, Any], status_message=None,
                          source_url: str = "", quality: str = "") -> List[Dict[str, Any]]:
    actor = _actor_api_id()
    start_url = f"{_APIFY_BASE}/acts/{actor}/runs"
    status, data, text = await _request_json(session, "POST", start_url, token, json=run_input)
    if status >= 400:
        raise YouTubeApiError(f"APIFY_HTTP_{status}: {_api_error(status, data, text)}")

    run = data.get("data", data) if isinstance(data, dict) else {}
    run_id = run.get("id")
    dataset_id = run.get("defaultDatasetId")
    if not run_id:
        raise YouTubeApiError("Apify did not return a run id")

    started = time.time()
    timeout = max(60, int(getattr(Config, "APIFY_YOUTUBE_TIMEOUT_SEC", 900) or 900))
    tick = 0
    await _show_waiting(status_message, source_url, quality, started, tick)

    while True:
        elapsed = time.time() - started
        if elapsed > timeout:
            raise YouTubeApiError(f"YouTube API timed out after {human_time(int(elapsed))}")

        await asyncio.sleep(5.5)
        tick += 1
        await _show_waiting(status_message, source_url, quality, started, tick)

        poll_url = f"{_APIFY_BASE}/actor-runs/{run_id}"
        p_status, p_data, p_text = await _request_json(session, "GET", poll_url, token)
        if p_status >= 400:
            raise YouTubeApiError(f"APIFY_HTTP_{p_status}: {_api_error(p_status, p_data, p_text)}")
        run = p_data.get("data", p_data) if isinstance(p_data, dict) else {}
        state = str(run.get("status") or "").upper()
        dataset_id = run.get("defaultDatasetId") or dataset_id

        if state == "SUCCEEDED":
            break
        if state in ("FAILED", "ABORTED", "TIMED-OUT"):
            msg = run.get("statusMessage") or run.get("exitCode") or state
            raise YouTubeApiError(f"Apify run {state.lower()}: {msg}")
        # READY/RUNNING/etc: keep waiting.

    if not dataset_id:
        raise YouTubeApiError("Apify run finished without a dataset id")

    items_url = f"{_APIFY_BASE}/datasets/{dataset_id}/items"
    i_status, i_data, i_text = await _request_json(
        session, "GET", items_url, token,
        params={"clean": "true", "format": "json"},
    )
    if i_status >= 400:
        raise YouTubeApiError(f"APIFY_HTTP_{i_status}: {_api_error(i_status, i_data, i_text)}")
    kv_id = run.get("defaultKeyValueStoreId") or ""
    if isinstance(i_data, list):
        items = [x for x in i_data if isinstance(x, dict)]
        for item in items:
            if kv_id:
                item.setdefault("__defaultKeyValueStoreId", kv_id)
        return items
    if isinstance(i_data, dict):
        maybe = i_data.get("items") or i_data.get("data") or []
        if isinstance(maybe, list):
            items = [x for x in maybe if isinstance(x, dict)]
            for item in items:
                if kv_id:
                    item.setdefault("__defaultKeyValueStoreId", kv_id)
            return items
    raise YouTubeApiError("Apify returned no dataset items")


async def run_youtube_actor(url: str, quality: str,
                            status_message=None) -> Tuple[List[Dict[str, Any]], str]:
    tokens = _usable_tokens()
    if not tokens:
        raise YouTubeApiError("No Apify API token configured")

    run_input = build_run_input(url, quality)
    last_err = ""
    timeout = aiohttp.ClientTimeout(total=None, sock_connect=30, sock_read=60)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        for token in tokens:
            try:
                items = await _run_actor_once(session, token, run_input,
                                              status_message=status_message,
                                              source_url=url, quality=quality)
                if items:
                    return items, token
                last_err = "dataset was empty"
            except YouTubeApiError as e:
                msg = str(e)
                last_err = msg
                m = re.match(r"APIFY_HTTP_(\d+):\s*(.*)", msg)
                status = int(m.group(1)) if m else 0
                if _quota_or_auth_status(status, msg):
                    _TOKEN_STATE[token] = time.time()
                    continue
                raise
            except Exception as e:
                last_err = f"{type(e).__name__}: {str(e)[:160]}"
                continue
    raise YouTubeApiError(
        "<b>YouTube API could not download this video.</b>\n\n"
        f"<i>{last_err or 'unknown error'}</i>\n\n"
        "<i>If every Apify token is out of credit, add "
        "<code>APIFY_API_TOKEN_2</code>.</i>"
    )


def _walk(obj: Any, key_path: Tuple[str, ...] = ()) -> Iterable[Tuple[Tuple[str, ...], Any, Any]]:
    if isinstance(obj, dict):
        for k, v in obj.items():
            path = key_path + (str(k),)
            yield path, v, obj
            yield from _walk(v, path)
    elif isinstance(obj, list):
        for i, v in enumerate(obj):
            path = key_path + (str(i),)
            yield path, v, obj
            yield from _walk(v, path)


def _is_http_url(s: Any) -> bool:
    return isinstance(s, str) and re.match(r"^https?://", s.strip(), re.I) is not None


def _looks_like_source_youtube(url: str) -> bool:
    low = url.lower()
    return ("youtube.com/watch" in low or "youtube.com/shorts" in low or "youtu.be/" in low or "music.youtube.com/watch" in low)


def _url_ext(url: str) -> str:
    try:
        path = urlparse(url).path.lower()
    except Exception:
        path = url.lower().split("?", 1)[0]
    return os.path.splitext(path)[1]


def _looks_download_url(url: str, key: str) -> bool:
    if not _is_http_url(url) or _looks_like_source_youtube(url):
        return False
    low_url = url.lower()
    low_key = (key or "").lower()
    ext = _url_ext(url)
    if ext in _VIDEO_EXTS + _AUDIO_EXTS + _SUBTITLE_EXTS:
        return True
    if any(h in low_key for h in _DOWNLOAD_KEY_HINTS) and not any(h in low_key for h in _SOURCE_KEY_HINTS):
        return True
    if any(h in low_url for h in ("googlevideo.com/videoplayback", "api.apify.com/v2/key-value-stores", "amazonaws.com", "storage.googleapis.com", "blob.core.windows.net", "cdn")):
        return True
    return False


def _parse_size(value: Any) -> int:
    if value is None:
        return 0
    if isinstance(value, (int, float)):
        return int(value)
    text = str(value).strip()
    if not text:
        return 0
    if text.isdigit():
        return int(text)
    m = re.match(r"([0-9]+(?:\.[0-9]+)?)\s*([kmgt]?i?b|bytes?)", text, re.I)
    if not m:
        return 0
    num = float(m.group(1))
    unit = m.group(2).lower()
    mult = 1
    if unit.startswith("k"):
        mult = 1024
    elif unit.startswith("m"):
        mult = 1024 ** 2
    elif unit.startswith("g"):
        mult = 1024 ** 3
    elif unit.startswith("t"):
        mult = 1024 ** 4
    return int(num * mult)


def _first_str(*objs: Any, keys: Iterable[str]) -> str:
    for obj in objs:
        if not isinstance(obj, dict):
            continue
        for k in keys:
            v = obj.get(k)
            if isinstance(v, str) and v.strip():
                return v.strip()
    return ""


def _first_size(*objs: Any) -> int:
    size_keys = ("fileSize", "filesize", "sizeBytes", "size_bytes", "bytes", "size", "contentLength", "content_length")
    for obj in objs:
        if not isinstance(obj, dict):
            continue
        for k in size_keys:
            n = _parse_size(obj.get(k))
            if n > 0:
                return n
    return 0


def _safe_name(name: str, default_ext: str = ".mp4") -> str:
    name = (name or "YouTube_video").strip()
    name = re.sub(r"[\\/:*?\"<>|\r\n\t]+", "_", name).strip(". ")
    if not name:
        name = "YouTube_video"
    if not os.path.splitext(name)[1] and default_ext:
        name += default_ext
    return name[:140]


def _asset_score(asset: Dict[str, Any]) -> int:
    key = (asset.get("key") or "").lower()
    url = (asset.get("url") or "").lower()
    ext = _url_ext(url)
    ctype = (asset.get("content_type") or "").lower()
    score = 0
    if "download" in key:
        score += 80
    if ext in _VIDEO_EXTS:
        score += 70
    if "video/" in ctype:
        score += 70
    if ext in _AUDIO_EXTS:
        score += 35
    if "audio/" in ctype:
        score += 35
    if any(h in key for h in _IMAGE_HINTS) or "image/" in ctype or ext in (".jpg", ".jpeg", ".png", ".webp"):
        score -= 100
    if ext in _SUBTITLE_EXTS or "subtitle" in key or "caption" in key:
        score -= 20
    if "api.apify.com/v2/key-value-stores" in url:
        score += 25
    return score


def extract_download_assets(items: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    assets: List[Dict[str, Any]] = []
    seen = set()
    for item in items:
        for path, value, parent in _walk(item):
            if not _is_http_url(value):
                continue
            key = path[-1] if path else "url"
            if not _looks_download_url(value, key):
                continue
            if value in seen:
                continue
            seen.add(value)
            default_ext = _url_ext(value)
            if default_ext not in _VIDEO_EXTS + _AUDIO_EXTS + _SUBTITLE_EXTS:
                default_ext = ".mp4"
            title = _first_str(parent, item, keys=("filename", "fileName", "name", "title", "videoTitle"))
            ctype = _first_str(parent, item, keys=("contentType", "content_type", "mimeType", "mime"))
            assets.append({
                "url": value,
                "key": key,
                "filename": _safe_name(title or "YouTube_video", default_ext),
                "size": _first_size(parent, item),
                "content_type": ctype,
                "item": item,
                "parent": parent,
            })

        # Some actors store the binary in the run's default key-value store and
        # output only the record key. Construct the authenticated record URL.
        kv_id = item.get("__defaultKeyValueStoreId")
        if kv_id:
            for rk in ("keyValueStoreKey", "kvStoreKey", "kvKey", "storeKey", "recordKey", "fileKey", "outputKey"):
                rec = item.get(rk)
                if isinstance(rec, str) and rec.strip():
                    value = f"{_APIFY_BASE}/key-value-stores/{kv_id}/records/{quote(rec.strip(), safe='')}"
                    if value in seen:
                        continue
                    seen.add(value)
                    title = _first_str(item, keys=("filename", "fileName", "name", "title", "videoTitle"))
                    assets.append({
                        "url": value,
                        "key": rk,
                        "filename": _safe_name(title or rec, ".mp4"),
                        "size": _first_size(item),
                        "content_type": "video/mp4",
                        "item": item,
                        "parent": item,
                    })
                    break
    assets.sort(key=_asset_score, reverse=True)
    return assets


def _filename_from_cd(cd: str) -> str:
    if not cd:
        return ""
    m = re.search(r"filename\*\s*=\s*[^']*'[^']*'([^;\r\n]+)", cd, re.I)
    if m:
        from urllib.parse import unquote
        return unquote(m.group(1)).strip().strip('"')
    m = re.search(r'filename\s*=\s*"?([^";\r\n]+)"?', cd, re.I)
    if m:
        return m.group(1).strip().strip('"')
    return ""


def _ext_from_ctype(ctype: str) -> str:
    c = (ctype or "").lower()
    if "mp4" in c:
        return ".mp4"
    if "webm" in c:
        return ".webm"
    if "mpeg" in c or "mp3" in c:
        return ".mp3"
    if "vtt" in c:
        return ".vtt"
    if "srt" in c:
        return ".srt"
    if "plain" in c:
        return ".txt"
    return ""


async def download_asset(asset: Dict[str, Any], output_dir: str, token: str,
                         status_message=None, label: str = "YouTube") -> str:
    os.makedirs(output_dir, exist_ok=True)
    url = asset["url"]
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125 Safari/537.36",
        "Accept": "*/*",
    }
    try:
        host = (urlparse(url).hostname or "").lower()
    except Exception:
        host = ""
    if host == "api.apify.com" or host.endswith(".apify.com"):
        headers["Authorization"] = f"Bearer {token}"

    timeout = aiohttp.ClientTimeout(total=None, sock_connect=30, sock_read=90)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        async with session.get(url, headers=headers, allow_redirects=True) as r:
            if r.status not in (200, 206):
                body = (await r.text())[:200]
                raise YouTubeApiError(f"API file download failed: HTTP {r.status} {body}")
            cd_name = _filename_from_cd(r.headers.get("Content-Disposition", ""))
            ctype = r.headers.get("Content-Type", "") or asset.get("content_type") or ""
            fallback_ext = _ext_from_ctype(ctype) or _url_ext(url) or ".mp4"
            fname = _safe_name(cd_name or asset.get("filename") or "YouTube_video", fallback_ext)
            # If the actor returned a title without extension and the response
            # revealed the real MIME type, keep Telegram happy by adding it.
            if not os.path.splitext(fname)[1] and fallback_ext:
                fname += fallback_ext
            dest = str(Path(output_dir) / fname)
            total = int(r.headers.get("Content-Length") or 0) or int(asset.get("size") or 0)
            done = 0
            start = time.time()
            with open(dest, "wb") as fh:
                async for chunk in r.content.iter_chunked(1 << 16):
                    if not chunk:
                        continue
                    fh.write(chunk)
                    done += len(chunk)
                    if status_message:
                        await progress_for_pyrogram(
                            done, total, status_message, start, fname,
                            f"Downloading {label}", known_total=total)
            if status_message:
                final_total = total or done
                await progress_for_pyrogram(
                    done, final_total, status_message, start, fname,
                    f"Downloading {label}", known_total=final_total)
            if os.path.getsize(dest) < 1024:
                raise YouTubeApiError("API downloaded an empty file")
            return dest


def _stringify_transcript(value: Any) -> str:
    if not value:
        return ""
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, list):
        lines = []
        for x in value:
            if isinstance(x, str):
                lines.append(x)
            elif isinstance(x, dict):
                txt = x.get("text") or x.get("caption") or x.get("line") or x.get("sentence")
                if txt:
                    start = x.get("start") or x.get("startTime") or x.get("offset")
                    lines.append((f"[{start}] " if start is not None else "") + str(txt))
        return "\n".join(lines).strip()
    if isinstance(value, dict):
        for k in ("text", "transcript", "transcription", "subtitles", "captions", "content"):
            if k in value:
                s = _stringify_transcript(value[k])
                if s:
                    return s
    return ""


def write_transcripts(items: List[Dict[str, Any]], output_dir: str, base_name: str) -> List[str]:
    wanted = ("transcript", "transcription", "subtitle", "subtitles", "caption", "captions")
    written: List[str] = []
    seen_texts = set()
    for item in items:
        for path, value, _ in _walk(item):
            key = (path[-1] if path else "").lower()
            if not any(w in key for w in wanted):
                continue
            text = _stringify_transcript(value)
            if not text or _is_http_url(text) or len(text) < 20:
                continue
            digest = text[:200]
            if digest in seen_texts:
                continue
            seen_texts.add(digest)
            suffix = ".srt" if "srt" in key else (".vtt" if "vtt" in key else ".txt")
            path_out = Path(output_dir) / _safe_name(f"{base_name}_transcript_{len(written)+1}{suffix}", suffix)
            path_out.write_text(text, encoding="utf-8")
            written.append(str(path_out))
    return written


async def download_youtube_via_api(url: str, output_dir: str, quality: str,
                                   status_message=None) -> str:
    """Run the Apify YouTube actor and download the produced media file.

    Returns the main media path.  If the actor also returns transcript/subtitle
    text, sidecar files are written to the same output directory so existing
    callers that upload every downloaded file will deliver them too.
    """
    if not has_api_tokens():
        raise YouTubeApiError("No Apify API token configured")
    quality = (quality or Config.APIFY_YOUTUBE_DEFAULT_QUALITY or "720p").strip() or "720p"
    items, token = await run_youtube_actor(url, quality, status_message=status_message)
    assets = extract_download_assets(items)
    if not assets:
        err_bits = []
        for item in items:
            for k in ("error", "message", "statusMessage", "reason"):
                v = item.get(k) if isinstance(item, dict) else None
                if v:
                    err_bits.append(str(v)[:180])
        keys = sorted({".".join(path) for item in items for path, _, _ in _walk(item)})[:20]
        raise YouTubeApiError(
            "Apify finished, but no downloadable file URL was found in the dataset.\n"
            + (("API message: " + " | ".join(err_bits[:2]) + "\n") if err_bits else "")
            + f"Dataset keys: {', '.join(keys)[:220]}"
        )
    asset = assets[0]
    await _safe_edit(
        status_message,
        "🎬 <b>YouTube API</b>\n\n"
        f"📄 <code>{asset.get('filename') or 'YouTube_video.mp4'}</code>\n"
        f"📦 Size: <b>{human_bytes(int(asset.get('size') or 0)) if asset.get('size') else 'detecting...'}</b>\n"
        "⬇️ Starting file download…"
    )
    path = await download_asset(asset, output_dir, token, status_message=status_message)
    try:
        write_transcripts(items, output_dir, Path(path).stem)
    except Exception:
        pass
    return path




async def check_actor_access() -> str:
    """Cheap diagnostics: verify token + actor visibility without running it."""
    tokens = _usable_tokens()
    if not tokens:
        return "not configured"
    token = tokens[0]
    timeout = aiohttp.ClientTimeout(total=30)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        try:
            status, data, text = await _request_json(
                session, "GET", f"{_APIFY_BASE}/acts/{_actor_api_id()}", token)
        except Exception as e:
            return f"failed ({type(e).__name__}: {str(e)[:60]})"
    if status == 200:
        return "reachable"
    return f"HTTP {status}: {_api_error(status, data, text)[:120]}"

async def diagnose(url: str = "") -> str:
    lines = ["<b>YouTube API Diagnostics</b>", ""]
    lines.append(f"Apify actor: <code>{_actor_api_id()}</code>")
    lines.append(f"API tokens: {api_key_status()}")
    lines.append(f"Default quality: <b>{Config.APIFY_YOUTUBE_DEFAULT_QUALITY}</b>")
    lines.append(f"Format: <b>{Config.APIFY_YOUTUBE_FORMAT}</b>")
    lines.append(f"Store in KV: <b>{'yes' if Config.APIFY_YOUTUBE_STORE_IN_KVSTORE else 'no'}</b>")
    lines.append(f"Transcribe: <b>{Config.APIFY_YOUTUBE_TRANSCRIPTION or 'disabled'}</b>")
    if url:
        lines.append(f"Link parsed as YouTube: <b>{'yes' if is_youtube_url(url) else 'no'}</b>")
    lines.append("")
    if has_api_tokens():
        lines.append("API mode: <b>ready</b> — YouTube downloads will use Apify first.")
    else:
        lines.append("API mode: <b>not configured</b> — set <code>APIFY_API_TOKEN</code>.")
    return "\n".join(lines)

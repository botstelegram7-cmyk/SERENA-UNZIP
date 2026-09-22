# utils/terabox.py
"""
TeraBox share-link resolver.

yt-dlp ships no TeraBox extractor, so share links have to be resolved
against TeraBox's own (undocumented) share API:

    GET <mirror>/main                     → session cookies + jsToken
    GET <mirror>/sharing/link?surl=…      → share page, more cookies
    GET <mirror>/share/list?shorturl=1<surl>&root=1
                                          → file list with signed `dlink`s
    GET <dlink>                           → 302 → CDN bytes

Important, learned by probing the live API from this server:

  * A *nonexistent* share answers errno 105.
  * A *real* share answers errno 140 when the caller is an anonymous
    datacenter IP.

So errno 140 means "TeraBox recognised the link but refused to serve it
to this IP" — not a dead link. The only reliable fix is to present a
logged-in session cookie (`ndus`), which is why Config.TERABOX_COOKIE
exists. Messages are written to say that plainly instead of blaming the
user's link.

Signed dlinks expire within minutes, so they are resolved immediately
before download and never cached.
"""

from __future__ import annotations

import asyncio
import inspect
import json
import os
import re
import time
from typing import Any, Dict, Iterable, List, Optional, Tuple
from urllib.parse import parse_qs, quote, urlparse

import aiohttp

from config import Config

DESKTOP_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"
)

# TeraBox runs the same service behind many brand domains. If one mirror
# refuses, another sometimes answers, so every request sweeps the list.
MIRRORS = [
    "www.terabox.com",
    "www.1024terabox.com",
    "www.terabox.app",
    "dm.1024tera.com",
    "www.1024tera.com",
    "teraboxlink.com",
    "www.mirrobox.com",
    "www.momerybox.com",
    "www.tibibox.com",
    "www.nephobox.com",
    "www.4funbox.com",
    "www.freeterabox.com",
    "www.terasharelink.com",
]

TERABOX_HOSTS = (
    "terabox.com", "1024terabox.com", "teraboxapp.com", "terabox.app",
    "1024tera.com", "teraboxlink.com", "mirrobox.com", "momerybox.com",
    "tibibox.com", "nephobox.com", "4funbox.com", "freeterabox.com",
    "terasharelink.com", "terafileshare.com", "teraboxshare.com",
)

APP_ID = "250528"


class TeraboxError(RuntimeError):
    """Raised with a user-facing Hinglish message."""


def is_terabox_url(url: str) -> bool:
    host = _host_of(url)
    return any(host == h or host.endswith("." + h) for h in TERABOX_HOSTS)


def _host_of(url: str) -> str:
    u = (url or "").strip()
    if "://" not in u:
        u = "https://" + u
    try:
        host = (urlparse(u).hostname or "").lower()
    except Exception:
        return ""
    return host[4:] if host.startswith("www.") else host


def extract_surl(url: str) -> Optional[str]:
    """Pull the share id out of any TeraBox link shape.

    Handles /s/1xxxx, /sharing/link?surl=xxxx and /wap/share/filelist?surl=…
    The leading "1" that /s/ URLs carry is stripped; the API wants it added
    back explicitly, which _list_files does.
    """
    if not url:
        return None
    try:
        parsed = urlparse(url if "://" in url else "https://" + url)
    except Exception:
        return None

    qs = parse_qs(parsed.query or "")
    for key in ("surl", "shorturl"):
        if key in qs and qs[key][0]:
            return qs[key][0].lstrip("1") or qs[key][0]

    m = re.search(r"/s/1?([A-Za-z0-9_\-]{5,})", parsed.path or "")
    if m:
        return m.group(1)

    parts = [p for p in (parsed.path or "").split("/") if p]
    if parts and re.fullmatch(r"1?[A-Za-z0-9_\-]{10,}", parts[-1]):
        return parts[-1].lstrip("1")
    return None


# Control characters in a header value make aiohttp raise ValueError
# ("Potential header injection"). Pasting a cookie out of devtools very
# easily carries a trailing newline, so sanitise before use — otherwise
# every request dies before it is sent and the failure looks like a
# TeraBox API change rather than a bad cookie.
_CTRL_RE = re.compile(r"[\r\n\t\x00-\x1f\x7f]")


def _clean_cookie(raw: str) -> str:
    """Normalise a pasted cookie into a safe single-line header value."""
    if not raw:
        return ""
    raw = _CTRL_RE.sub(" ", str(raw))
    raw = raw.strip().strip('"').strip("'").strip()
    raw = re.sub(r"\s*;\s*", "; ", raw)
    raw = re.sub(r"\s{2,}", " ", raw)
    return raw.strip("; ").strip()


def _parse_netscape(raw: str) -> Dict[str, str]:
    """Parse a Netscape cookies.txt blob into {name: value}.

    Format is TSV: domain, flag, path, secure, expiry, name, value.
    People often export this file rather than copying a single value, so
    it has to be supported — treating it as a header string produced a
    Cookie header containing the whole file.
    """
    jar: Dict[str, str] = {}
    text = (raw or "").replace("\\n", "\n")
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        parts = re.split(r"\t+", line)
        if len(parts) < 7:
            parts = re.split(r"\s{2,}|\t", line)
        if len(parts) >= 7:
            name, value = parts[5].strip(), parts[6].strip()
            if name:
                jar[name] = value
    return jar


def _looks_netscape(raw: str) -> bool:
    low = (raw or "").lower()
    if "netscape http cookie file" in low:
        return True
    # A TSV line with 7 fields and TRUE/FALSE flags is the giveaway
    for line in (raw or "").replace("\\n", "\n").splitlines():
        if line.strip().startswith("#") or not line.strip():
            continue
        parts = re.split(r"\t+|\s{2,}", line.strip())
        if len(parts) >= 7 and parts[1].upper() in ("TRUE", "FALSE"):
            return True
    return False


def _cookie_header() -> str:
    """Build a Cookie header from the configured session value.

    Accepts three shapes people actually paste:
      * a bare ndus value
      * a browser "k=v; k=v" cookie string
      * a full Netscape cookies.txt export
    """
    raw = (Config.TERABOX_COOKIE or "")
    if not raw.strip():
        return ""

    if _looks_netscape(raw):
        jar = _parse_netscape(raw)
        if jar:
            wanted = [f"{k}={v}" for k, v in jar.items()
                      if k.lower() in ("ndus", "browserid", "csrftoken",
                                       "lang", "ndut_fmt", "pcsett", "stoken")]
            pairs = wanted or [f"{k}={v}" for k, v in jar.items()]
            return _clean_cookie("; ".join(pairs))

    raw = _clean_cookie(raw)
    if not raw:
        return ""
    # Accept either a bare ndus value or a full "k=v; k=v" string
    if "=" not in raw:
        return f"ndus={raw}"
    return raw


def cookie_problem() -> str:
    """Return a human-readable problem with the configured cookie, if any."""
    raw = (Config.TERABOX_COOKIE or "")
    if not raw.strip():
        return "not set"
    cleaned = _cookie_header()
    if not cleaned:
        return "empty after cleanup"
    if "ndus" not in cleaned.lower():
        return "no `ndus` key found — copy the ndus cookie specifically"
    m = re.search(r"ndus=([^;]+)", cleaned, re.I)
    val = (m.group(1).strip() if m else "")
    if len(val) < 20:
        return f"`ndus` looks too short ({len(val)} chars) — copy the full value"
    return ""


def has_cookie() -> bool:
    return bool((Config.TERABOX_COOKIE or "").strip())


def _headers(referer: str = "") -> Dict[str, str]:
    h = {
        "User-Agent": DESKTOP_UA,
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "en-US,en;q=0.9",
    }
    if referer:
        h["Referer"] = referer
    ck = _cookie_header()
    if ck:
        h["Cookie"] = ck
    return h


_JS_TOKEN_RE = [
    re.compile(r"jsToken[\"']?\s*[:=]\s*[\"']([0-9A-Fa-f]{32,})"),
    re.compile(r"fn%28%22([0-9A-Fa-f]{32,})%22%29"),
    re.compile(r'fn\("([0-9A-Fa-f]{32,})"\)'),
]


def _find_js_token(html: str) -> Optional[str]:
    for rx in _JS_TOKEN_RE:
        m = rx.search(html or "")
        if m:
            return m.group(1)
    return None


# TeraBox errno meanings, confirmed by probing the live API.
_ERRNO_HELP = {
    105: ("<b>Ye TeraBox link valid nahi hai.</b>\n\n"
          "Link galat hai, expire ho gaya, ya delete kar diya gaya."),
    -9:  ("<b>Is share par password laga hai.</b>\n\n"
          "Password-protected TeraBox links abhi support nahi hain."),
}


def _wall_message() -> str:
    """errno 140 / 400210: real link, but the server IP is refused."""
    if has_cookie():
        return (
            "<b>TeraBox ne is request ko block kar diya.</b>\n\n"
            "File list mil gayi, lekin signed download link server IP ko "
            "nahi diya gaya. Cookie set hone ke baad bhi ye IP-level block "
            "ho sakta hai.\n\n"
            "<b>Fix:</b> <code>XAPIVERSE_KEY</code> API ko sahi/active rakho "
            "ya residential proxy laga kar <code>TERABOX_PROXY</code> set karo.")
    return (
        "<b>TeraBox ne server IP se access block kiya.</b>\n\n"
        "Link real hai, par datacenter IP ko TeraBox download link nahi de raha.\n\n"
        "<b>Fix:</b> <code>XAPIVERSE_KEY</code> API set karo, ya residential "
        "proxy ke saath <code>TERABOX_PROXY</code> use karo.")

# Remembers why the last request failed, so a transport-level problem
# (bad cookie, DNS, timeout) is reported instead of being mistaken for
# "TeraBox changed its API".
_LAST_TRANSPORT_ERROR: Dict[str, str] = {"v": ""}


def _proxy() -> Optional[str]:
    """Configured proxy, if any. Only http(s):// is usable by aiohttp."""
    px = (Config.TERABOX_PROXY or "").strip()
    return px or None


async def _get_json(session: aiohttp.ClientSession, url: str,
                    referer: str = "") -> Optional[Dict]:
    try:
        async with session.get(url, headers=_headers(referer),
                               timeout=aiohttp.ClientTimeout(total=25),
                               proxy=_proxy(),
                               allow_redirects=True) as r:
            if r.status != 200:
                _LAST_TRANSPORT_ERROR["v"] = f"HTTP {r.status}"
                return None
            txt = await r.text()
            try:
                return json.loads(txt)
            except ValueError:
                _LAST_TRANSPORT_ERROR["v"] = "response was not JSON"
                return None
    except ValueError as e:
        # aiohttp raises this for control characters in headers — i.e. a
        # cookie pasted with a newline in it.
        _LAST_TRANSPORT_ERROR["v"] = f"bad request headers ({str(e)[:60]})"
        return None
    except asyncio.TimeoutError:
        _LAST_TRANSPORT_ERROR["v"] = "timeout"
        return None
    except Exception as e:
        _LAST_TRANSPORT_ERROR["v"] = f"{type(e).__name__}: {str(e)[:60]}"
        return None


async def _warm_up(session: aiohttp.ClientSession, mirror: str,
                   surl: str) -> Optional[str]:
    """Visit /main and the share page to collect cookies and a jsToken."""
    token = None
    for path in (f"https://{mirror}/main",
                 f"https://{mirror}/sharing/link?surl={surl}"):
        try:
            async with session.get(path, headers=_headers(),
                                   proxy=_proxy(),
                                   timeout=aiohttp.ClientTimeout(total=25)) as r:
                html = await r.text()
                token = token or _find_js_token(html)
        except Exception:
            continue
    return token


async def _list_on_mirror(session: aiohttp.ClientSession, mirror: str,
                          surl: str, token: Optional[str],
                          dir_path: str = "") -> Tuple[Optional[List[Dict]], int]:
    """Return (entries, errno) for one mirror."""
    referer = f"https://{mirror}/sharing/link?surl={surl}"

    # The API is inconsistent about the leading "1" of a /s/1xxxx link.
    # Some shares only resolve with it, others only WITHOUT it (those
    # answer errno 105 — "invalid link" — when it is present), so try
    # both forms before giving up. Getting this wrong made perfectly
    # good links look deleted.
    last_errno = 0
    for variant in (surl, "1" + surl):
        base = (f"https://{mirror}/share/list?app_id={APP_ID}"
                f"&shorturl={variant}&root={'0' if dir_path else '1'}")
        if dir_path:
            from urllib.parse import quote
            base += f"&dir={quote(dir_path)}"

        attempts = [base]
        if token:
            attempts.insert(0, base + f"&jsToken={token}")

        for url in attempts:
            data = await _get_json(session, url, referer)
            if not data:
                continue
            errno = int(data.get("errno", -1) or 0)
            if errno == 0:
                return (data.get("list") or []), 0
            last_errno = errno
    return None, last_errno


# ── xAPIverse API ────────────────────────────────────────────────────────────
# TeraBox refuses signed download links to datacenter addresses, so scraping
# it from a hosted server fails no matter how good the cookies are. This API
# resolves the share on its own infrastructure and hands back ready URLs.
#
# Free tier is 100 credits/month per key, so several keys are supported and
# an exhausted one steps aside for the next.

XAPIVERSE_URL = "https://xapiverse.com/api/terabox"

# Keys observed to be out of credit, with the time they were parked.
_KEY_STATE: Dict[str, float] = {}
_KEY_COOLDOWN = 6 * 3600


def _api_keys() -> List[str]:
    keys = []
    for name in ("XAPIVERSE_KEY", "XAPIVERSE_KEY_2", "XAPIVERSE_KEY_3",
                 "XAPIVERSE_KEY_4", "XAPIVERSE_KEY_5"):
        k = (getattr(Config, name, "") or "").strip()
        if k and k not in keys:
            keys.append(k)
    return keys


def has_api_key() -> bool:
    return bool(_api_keys())


def _first_api_key() -> str:
    keys = _usable_keys()
    return keys[0] if keys else ""


def _is_api_provider_url(url: str) -> bool:
    try:
        host = (urlparse(url).hostname or "").lower()
    except Exception:
        return False
    return host == "xapiverse.com" or host.endswith(".xapiverse.com") or host == "iteraplay.com" or host.endswith(".iteraplay.com")


def _usable_keys() -> List[str]:
    """Keys not currently parked for being out of credit."""
    now = time.time()
    fresh = [k for k in _api_keys() if now - _KEY_STATE.get(k, 0) > _KEY_COOLDOWN]
    # If every key is parked, try them all again rather than refusing.
    return fresh or _api_keys()


def api_key_status() -> str:
    keys = _api_keys()
    if not keys:
        return "no API key set"
    now = time.time()
    bits = []
    for i, k in enumerate(keys, 1):
        parked = now - _KEY_STATE.get(k, 0) <= _KEY_COOLDOWN
        left = int(_KEY_COOLDOWN - (now - _KEY_STATE.get(k, 0))) // 60
        bits.append(f"#{i} " + (f"exhausted ({left}m)" if parked else "ready"))
    return " · ".join(bits)


def _api_walk(obj: Any) -> Iterable[Dict]:
    """Yield every dict inside an API response, regardless of nesting."""
    if isinstance(obj, dict):
        yield obj
        for v in obj.values():
            yield from _api_walk(v)
    elif isinstance(obj, list):
        for v in obj:
            yield from _api_walk(v)


def _api_size(value: Any) -> int:
    if value is None:
        return 0
    if isinstance(value, (int, float)):
        return int(value)
    text = str(value).strip()
    if text.isdigit():
        return int(text)
    m = re.match(r"([0-9]+(?:\.[0-9]+)?)\s*([kmgt]?i?b|bytes?)", text, re.I)
    if not m:
        return 0
    num = float(m.group(1)); unit = m.group(2).lower(); mult = 1
    if unit.startswith("k"): mult = 1024
    elif unit.startswith("m"): mult = 1024 ** 2
    elif unit.startswith("g"): mult = 1024 ** 3
    elif unit.startswith("t"): mult = 1024 ** 4
    return int(num * mult)


def _first_api_link(entry: Dict) -> str:
    """Return the best downloadable URL from a xAPIverse item.

    xAPIverse has used a few names over time. Prefer actual download links,
    then stream URLs; ignore thumbnails/subtitles.
    """
    keys = (
        "fast_download_link", "fastDownloadLink", "fast_download_url",
        "fastDownloadUrl", "fast_download", "fastDownload", "normal_dlink",
        "normalDlink", "download_url", "downloadUrl", "download_link",
        "downloadLink", "dlink", "direct_link", "directLink",
        "file_url", "fileUrl", "stream_url", "streamUrl", "url", "link",
    )
    for key in keys:
        value = entry.get(key)
        if isinstance(value, dict):
            # fast_stream_url can be {"360p": "...m3u8"}; use the first URL.
            value = next((v for v in value.values() if isinstance(v, str)), "")
        if isinstance(value, list):
            value = next((v for v in value if isinstance(v, str)), "")
        if isinstance(value, str) and value.startswith(("http://", "https://")):
            if is_terabox_url(value):
                continue
            low_key = key.lower()
            low_val = value.lower()
            if any(bad in low_key or bad in low_val for bad in ("thumb", "subtitle", "caption")):
                continue
            return value.strip()
    return ""


def _items_from_api(payload: Dict) -> List[Dict]:
    """Map many possible xAPIverse response shapes onto list_files() entries."""
    out: List[Dict] = []
    seen = set()

    for e in _api_walk(payload):
        # Skip directories/folders; only downloadable files should be returned.
        if str(e.get("is_dir") or e.get("isdir") or "0") == "1":
            continue
        if str(e.get("type") or "").lower() in ("folder", "dir", "directory"):
            continue

        link = _first_api_link(e)
        if not link or link in seen:
            continue
        seen.add(link)

        name = (e.get("name") or e.get("server_filename") or e.get("filename")
                or e.get("file_name") or os.path.basename(e.get("file_path") or "")
                or os.path.basename(urlparse(link).path) or "terabox_file")
        size = 0
        for sk in ("size", "fileSize", "filesize", "size_bytes", "sizeBytes", "contentLength"):
            size = _api_size(e.get(sk))
            if size:
                break

        out.append({
            "name": _safe_name(str(name)),
            "size": size,
            "fs_id": str(e.get("fs_id") or e.get("fsId") or ""),
            "dlink": link,
            "path": e.get("file_path") or e.get("path") or "",
            "is_dir": False,
            "mirror": "xapiverse",
            "thumbnail": e.get("thumbnail") or "",
        })
    return out

async def list_files_via_api(url: str) -> List[Dict]:
    """Resolve a share through xAPIverse. Raises TeraboxError on failure."""
    keys = _usable_keys()
    if not keys:
        raise TeraboxError("No xAPIverse key configured.")

    last_err = ""
    timeout = aiohttp.ClientTimeout(total=60)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        for key in keys:
            try:
                async with session.post(
                    XAPIVERSE_URL,
                    json={"url": url},
                    headers={"Content-Type": "application/json",
                             "xAPIverse-Key": key},
                ) as r:
                    body = await r.text()
                    status = r.status
            except Exception as e:
                last_err = f"{type(e).__name__}: {str(e)[:80]}"
                continue

            # Out of credit / bad key: park it and try the next.
            if status in (401, 402, 403, 429):
                _KEY_STATE[key] = time.time()
                last_err = f"HTTP {status}"
                continue
            if status != 200:
                last_err = f"HTTP {status}"
                continue

            try:
                data = json.loads(body)
            except Exception:
                last_err = "response was not JSON"
                continue

            if str(data.get("status", "")).lower() not in ("success", "ok", ""):
                last_err = str(data.get("message")
                               or data.get("error") or "API reported failure")
                # A credit message means this key is spent, not that the
                # link is bad.
                if "credit" in last_err.lower() or "quota" in last_err.lower():
                    _KEY_STATE[key] = time.time()
                    continue
                raise TeraboxError(
                    f"<b>TeraBox API could not read this link.</b>\n\n"
                    f"<i>{last_err[:160]}</i>")

            items = _items_from_api(data)
            if items:
                return items
            last_err = "API returned no files"

    raise TeraboxError(
        "<b>The TeraBox API could not resolve this link.</b>\n\n"
        f"<i>{last_err or 'unknown error'}</i>\n\n"
        "<i>If every key is out of credit, add another with "
        "<code>XAPIVERSE_KEY_2</code>.</i>")


async def list_files(url: str) -> List[Dict]:
    """Resolve a share link through xAPIverse only.

    Cookies/direct scraping are deliberately not used for downloads. They are
    kept only as low-level helpers for legacy diagnostics when API-only mode is
    disabled by an operator.
    """
    if not is_terabox_url(url):
        raise TeraboxError("Ye TeraBox ka link nahi hai.")
    surl = extract_surl(url)
    if not surl:
        raise TeraboxError(
            "<b>Is link se share ID nahi mila.</b>\n\n"
            "Format aisa hona chahiye: <code>terabox.com/s/1xxxxxxx</code>")
    if not has_api_key():
        raise TeraboxError(
            "<b>TeraBox API is not configured.</b>\n\n"
            "Set <code>XAPIVERSE_KEY</code>. Cookies are intentionally ignored.")
    return await list_files_via_api(url)


async def _share_meta(session: aiohttp.ClientSession, mirror: str,
                      surl: str, token: Optional[str]) -> Dict:
    """Fetch sign/timestamp/shareid/uk, needed to mint download links."""
    referer = f"https://{mirror}/sharing/link?surl={surl}"
    for variant in ("1" + surl, surl):
        url = (f"https://{mirror}/api/shorturlinfo?app_id={APP_ID}"
               f"&shorturl={variant}&root=1")
        if token:
            url += f"&jsToken={token}"
        data = await _get_json(session, url, referer)
        if data and int(data.get("errno", -1) or 0) == 0 and data.get("sign"):
            return data
    return {}


async def _mint_dlinks(session: aiohttp.ClientSession, mirror: str, surl: str,
                       token: Optional[str], files: List[Dict]) -> None:
    """Populate `dlink` on entries that came back without one.

    /share/list frequently omits dlink. The signed /share/download call
    mints a fresh one, so try that (and the signed list, which sometimes
    includes dlinks the unsigned one hides) before declaring failure.
    Mutates `files` in place; never raises.
    """
    referer = f"https://{mirror}/sharing/link?surl={surl}"
    meta = await _share_meta(session, mirror, surl, token)
    if not meta:
        return

    sign = meta.get("sign")
    ts = meta.get("timestamp")
    shareid = meta.get("shareid")
    uk = meta.get("uk")
    sekey = meta.get("randsk") or ""
    if not (sign and ts and shareid and uk):
        return

    sig = f"&sign={sign}&timestamp={ts}&shareid={shareid}&uk={uk}"

    # 1) Signed listing — cheapest, covers every file at once.
    for variant in (surl, "1" + surl):
        url = (f"https://{mirror}/share/list?app_id={APP_ID}"
               f"&shorturl={variant}&root=1{sig}")
        if token:
            url += f"&jsToken={token}"
        data = await _get_json(session, url, referer)
        if not data or int(data.get("errno", -1) or 0) != 0:
            continue
        by_id = {str(e.get("fs_id")): e.get("dlink")
                 for e in (data.get("list") or []) if e.get("dlink")}
        if by_id:
            for f in files:
                if not f.get("dlink"):
                    f["dlink"] = by_id.get(f.get("fs_id"), "") or f.get("dlink", "")
            if all(f.get("dlink") for f in files):
                return

    # 2) Per-file /share/download, plus the data.* REST twin as a backup.
    for f in files:
        if f.get("dlink") or not f.get("fs_id"):
            continue
        fid = quote(f'[{f["fs_id"]}]')
        candidates = [
            (f"https://{mirror}/share/download?app_id={APP_ID}"
             f"&channel=chunlei&clienttype=0&web=1{sig}&fid_list={fid}"
             + (f"&sekey={sekey}" if sekey else "")
             + (f"&jsToken={token}" if token else "")),
            (f"https://{mirror}/api/download?app_id={APP_ID}"
             f"&channel=chunlei&clienttype=0&web=1{sig}&fid_list={fid}"
             + (f"&jsToken={token}" if token else "")),
        ]
        for url in candidates:
            data = await _get_json(session, url, referer)
            if not data or int(data.get("errno", -1) or 0) != 0:
                continue
            link = data.get("dlink")
            if isinstance(link, list) and link:
                link = (link[0] or {}).get("dlink") if isinstance(link[0], dict) else link[0]
            if not link:
                info = data.get("info") or []
                if info and isinstance(info[0], dict):
                    link = info[0].get("dlink")
            if link:
                f["dlink"] = link
                break


async def _flatten(session: aiohttp.ClientSession, mirror: str, surl: str,
                   token: Optional[str], entries: List[Dict],
                   depth: int = 0) -> List[Dict]:
    """Walk folders so a shared directory yields its files."""
    out: List[Dict] = []
    for e in entries or []:
        is_dir = str(e.get("isdir", "0")) == "1"
        path = e.get("path") or ""
        if is_dir:
            if depth >= 3:          # guard against pathological nesting
                continue
            sub, _ = await _list_on_mirror(session, mirror, surl, token, path)
            if sub:
                out += await _flatten(session, mirror, surl, token, sub, depth + 1)
            continue
        try:
            size = int(e.get("size") or 0)
        except (TypeError, ValueError):
            size = 0
        out.append({
            "name": e.get("server_filename") or os.path.basename(path) or "file",
            "size": size,
            "fs_id": str(e.get("fs_id") or ""),
            "dlink": e.get("dlink") or "",
            "path": path,
            "is_dir": False,
            "mirror": mirror,
        })
    return out



async def _range_probe(session: aiohttp.ClientSession, url: str,
                       headers: Dict[str, str]) -> Tuple[int, str, str]:
    """Return (total_bytes, final_url, content_type) if byte ranges work."""
    h = dict(headers)
    h["Range"] = "bytes=0-0"
    async with session.get(url, headers=h, allow_redirects=True) as r:
        ctype = r.headers.get("Content-Type", "") or ""
        if r.status != 206:
            return 0, str(r.url), ctype
        cr = r.headers.get("Content-Range", "")
        m = re.search(r"/(\d+)$", cr)
        total = int(m.group(1)) if m else int(r.headers.get("Content-Length") or 0)
        # Drain the one-byte body so aiohttp can reuse the connection.
        await r.read()
        return total, str(r.url), ctype


async def _parallel_api_download(session: aiohttp.ClientSession, url: str,
                                 headers: Dict[str, str], dest: str,
                                 expected_size: int = 0,
                                 progress=None) -> bool:
    """Fast xAPIverse/Iteraplay file fetch using parallel Range requests.

    API provider links can be slow per TCP connection. If the server supports
    Range requests, splitting the file into several ranges usually improves
    throughput. Falls back silently to the normal single stream when unsupported.
    """
    workers = max(1, min(int(getattr(Config, "TERABOX_API_CONNECTIONS", 6) or 6), 12))
    if workers <= 1:
        return False
    try:
        total, final_url, ctype = await _range_probe(session, url, headers)
    except Exception:
        return False
    if not total or total < 8 * 1024 * 1024:
        return False
    if "json" in ctype.lower() or "text/" in ctype.lower():
        return False
    if expected_size and abs(total - expected_size) > max(2 * 1024 * 1024, expected_size * 0.05):
        # The range endpoint is not returning the expected binary file.
        return False

    tmp = dest + ".part"
    try:
        os.makedirs(os.path.dirname(dest) or ".", exist_ok=True)
        with open(tmp, "wb") as fh:
            fh.truncate(total)

        chunk = (total + workers - 1) // workers
        ranges = []
        for i in range(workers):
            start = i * chunk
            end = min(total - 1, ((i + 1) * chunk) - 1)
            if start <= end:
                ranges.append((i, start, end))
        done = [0 for _ in ranges]

        async def one(slot: int, start: int, end: int):
            pos = start
            # A small retry loop per range. Any hard failure falls back to the
            # normal downloader by returning False from the wrapper.
            for attempt in range(3):
                try:
                    h = dict(headers)
                    h["Range"] = f"bytes={pos}-{end}"
                    async with session.get(final_url, headers=h, allow_redirects=True) as r:
                        if r.status != 206:
                            raise RuntimeError(f"range HTTP {r.status}")
                        with open(tmp, "r+b") as fh:
                            fh.seek(pos)
                            async for data in r.content.iter_chunked(256 * 1024):
                                if not data:
                                    continue
                                fh.write(data)
                                pos += len(data)
                                done[slot] = pos - start
                                if progress:
                                    ret = progress(min(sum(done), total), total)
                                    if inspect.isawaitable(ret):
                                        await ret
                    if pos > end:
                        return
                except Exception:
                    await asyncio.sleep(1.5 * (attempt + 1))
            raise RuntimeError("range failed")

        await asyncio.gather(*(one(slot, st, en) for slot, st, en in ranges))
        if os.path.getsize(tmp) != total:
            return False
        if progress:
            ret = progress(total, total)
            if inspect.isawaitable(ret):
                await ret
        os.replace(tmp, dest)
        return True
    except Exception:
        try:
            if os.path.exists(tmp):
                os.remove(tmp)
        except Exception:
            pass
        return False


async def download_file(entry: Dict, output_dir: str,
                        progress=None) -> Optional[str]:
    """Download one API-resolved TeraBox entry. Returns the saved path."""
    dlink = entry.get("dlink")
    if not dlink:
        raise TeraboxError("TeraBox API returned a file without a download URL.")
    os.makedirs(output_dir, exist_ok=True)
    name = _safe_name(entry.get("name") or "terabox_file")
    dest = os.path.join(output_dir, name)
    mirror = entry.get("mirror") or "xapiverse"

    if mirror != "xapiverse" and Config.TERABOX_API_ONLY:
        raise TeraboxError("TeraBox direct/cookie downloads are disabled; API link required.")

    if mirror == "xapiverse":
        headers = {"User-Agent": DESKTOP_UA, "Accept": "*/*"}
        if _is_api_provider_url(dlink):
            k = _first_api_key()
            if k:
                headers["xAPIverse-Key"] = k
    else:
        headers = _headers(f"https://{mirror}/")
        headers["Accept"] = "*/*"

    jar = aiohttp.CookieJar(unsafe=True)
    timeout = aiohttp.ClientTimeout(total=None, sock_connect=30, sock_read=60)
    last_err = ""
    async with aiohttp.ClientSession(cookie_jar=jar, timeout=timeout) as session:
        current_url = dlink
        for attempt in range(1, 4):
            try:
                have = os.path.getsize(dest) if os.path.exists(dest) else 0
                h = dict(headers)
                if mirror == "xapiverse" and not os.path.exists(dest):
                    if await _parallel_api_download(
                        session, current_url, h, dest,
                        int(entry.get("size") or 0), progress):
                        return dest
                if have > 1024:
                    h["Range"] = f"bytes={have}-"
                async with session.get(current_url, headers=h,
                                       proxy=None if mirror == "xapiverse" else _proxy(),
                                       allow_redirects=True) as r:
                    ctype = (r.headers.get("Content-Type") or "").lower()
                    if r.status in (403, 410):
                        body = (await r.text())[:200]
                        raise TeraboxError(f"TeraBox API download URL was refused: HTTP {r.status} {body}")
                    if r.status not in (200, 206):
                        body = (await r.text())[:200]
                        last_err = f"HTTP {r.status} {body}"
                        await asyncio.sleep(2 * attempt)
                        continue

                    # Some API/provider endpoints answer JSON containing the
                    # actual file URL. Follow that URL instead of saving JSON.
                    if "json" in ctype or "text/" in ctype:
                        text = await r.text()
                        try:
                            data = json.loads(text)
                        except Exception:
                            data = None
                        if isinstance(data, dict):
                            nested = _first_api_link(data)
                            if nested and nested != current_url:
                                current_url = nested
                                if _is_api_provider_url(current_url):
                                    k = _first_api_key()
                                    if k:
                                        headers["xAPIverse-Key"] = k
                                continue
                        last_err = text[:200]
                        await asyncio.sleep(2 * attempt)
                        continue

                    mode = "ab" if (r.status == 206 and have) else "wb"
                    total = r.content_length or int(entry.get("size") or 0) or 0
                    done = have if mode == "ab" else 0
                    with open(dest, mode) as fh:
                        async for chunk in r.content.iter_chunked(1 << 16):
                            fh.write(chunk)
                            done += len(chunk)
                            if progress:
                                try:
                                    ret = progress(done, total + (have if mode == "ab" else 0))
                                    if inspect.isawaitable(ret):
                                        await ret
                                except Exception:
                                    pass
                if os.path.exists(dest) and os.path.getsize(dest) > 1024:
                    return dest
            except TeraboxError:
                raise
            except Exception as e:
                last_err = f"{type(e).__name__}: {str(e)[:160]}"
                await asyncio.sleep(2 * attempt)
    if os.path.exists(dest) and os.path.getsize(dest) > 1024:
        return dest
    raise TeraboxError(f"TeraBox API file download failed. {last_err}")


def _safe_name(name: str) -> str:
    name = re.sub(r'[\\/:*?"<>|\r\n\t]+', "_", str(name)).strip(". ")
    return (name or "file")[:120]


def human_size(n: int) -> str:
    n = float(n or 0)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return f"{n:.0f} {unit}" if unit in ("B", "KB") else f"{n:.2f} {unit}"
        n /= 1024
    return f"{n:.2f} TB"


async def diagnose(url: str = "") -> str:
    """Owner-facing API-only TeraBox probe."""
    out = ["<b>TeraBox Diagnostics</b>", ""]
    if has_cookie():
        out.append("Cookie: <b>ignored</b> (API-only mode)")
    else:
        out.append("Cookie: <b>not used</b> (API-only mode)")
    out.append(f"API keys: {api_key_status()}")
    if url:
        out.append(f"surl: <code>{extract_surl(url) or 'not parsed'}</code>")
    out.append("")

    surl = extract_surl(url) if url else ""
    if surl and has_api_key():
        try:
            api_items = await list_files_via_api(url)
            api_links = sum(1 for f in api_items if f.get("dlink"))
            api_size = sum(int(f.get("size") or 0) for f in api_items)
            out.append(
                f"API resolve: <b>{len(api_items)} files</b> · "
                f"links {api_links}/{len(api_items)}"
                + (f" · {human_size(api_size)}" if api_size else ""))
            for i, item in enumerate(api_items[:5], 1):
                out.append(f"{i}. <code>{item.get('name') or 'file'}</code> — {human_size(item.get('size') or 0)}")
        except Exception as e:
            out.append(f"API resolve: <b>failed</b> — <code>{str(e)[:220]}</code>")
    elif surl and not has_api_key():
        out.append("API resolve: <b>not configured</b> — set <code>XAPIVERSE_KEY</code>")
    else:
        out.append("<i>Tip: <code>/tbtest &lt;link&gt;</code> chalao — bina link ke sirf API/token status dikhata hai.</i>")

    out.append("")
    out.append("<i>Direct mirror/cookie scraping is disabled. Downloader uses the API result only.</i>")
    return "\n".join(out)

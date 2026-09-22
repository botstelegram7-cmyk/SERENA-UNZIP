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
        "fast_download_link", "fastDownloadLink", "normal_dlink",
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
    """Resolve a share link to a flat list of downloadable files.

    Each entry: {name, size, fs_id, dlink, is_dir, path}
    """
    if not is_terabox_url(url):
        raise TeraboxError("Ye TeraBox ka link nahi hai.")
    surl = extract_surl(url)
    if not surl:
        raise TeraboxError(
            "<b>Is link se share ID nahi mila.</b>\n\n"
            "Format aisa hona chahiye: <code>terabox.com/s/1xxxxxxx</code>")

    # The API resolves the share on its own infrastructure, so it sidesteps
    # the address block entirely. Try it first when a key is configured.
    # If it fails and direct scraping also fails, include the API reason in the
    # final error instead of hiding it behind the old IP-block message.
    api_error = ""
    if has_api_key():
        try:
            items = await list_files_via_api(url)
            if items:
                return items
        except TeraboxError as e:
            api_error = str(e)
            if not has_cookie() and not _proxy():
                raise          # nothing else to try
        except Exception as e:
            api_error = f"{type(e).__name__}: {str(e)[:160]}"
            if not has_cookie() and not _proxy():
                raise TeraboxError(api_error)

    problem = cookie_problem()
    if problem and problem != "not set":
        raise TeraboxError(
            f"<b>TERABOX_COOKIE is not usable:</b> {problem}.\n\n"
            "Chrome → F12 → Application → Cookies → terabox.com → "
            "copy the full <code>ndus</code> value.")

    _LAST_TRANSPORT_ERROR["v"] = ""
    jar = aiohttp.CookieJar(unsafe=True)
    timeout = aiohttp.ClientTimeout(total=None, sock_connect=20, sock_read=30)
    seen_errno = 0
    withheld_names = ""

    async with aiohttp.ClientSession(cookie_jar=jar, timeout=timeout) as session:
        # Prefer the mirror the user actually pasted, then sweep the rest.
        host = _host_of(url)
        order = ([m for m in MIRRORS if host in m] +
                 [m for m in MIRRORS if host not in m])

        for mirror in order:
            token = await _warm_up(session, mirror, surl)
            entries, errno = await _list_on_mirror(session, mirror, surl, token)
            if entries is not None:
                files = await _flatten(session, mirror, surl, token, entries)
                if files:
                    if not any(f.get("dlink") for f in files):
                        # /share/list often omits dlink even when the share
                        # lists fine. Mint the links explicitly before
                        # giving up — that path frequently works when the
                        # listing one does not.
                        await _mint_dlinks(session, mirror, surl, token, files)
                    if not any(f.get("dlink") for f in files):
                        # This mirror listed the file but withheld the dlink.
                        # Do NOT stop here: /tbtest often shows another mirror
                        # (for example dm.1024tera.com) can mint the same file.
                        withheld_names = ", ".join(f["name"][:40] for f in files[:2])
                        seen_errno = seen_errno or 140
                        continue
                    return files
                raise TeraboxError(
                    "<b>Is share me koi file nahi mili.</b>\n\n"
                    "Folder khali hai ya uska content hata diya gaya hai.")
            if errno:
                seen_errno = errno
            # Keep sweeping: 105 from one mirror does not mean the share is
            # dead, since another mirror often resolves the same link.

    if seen_errno in _ERRNO_HELP:
        raise TeraboxError(_ERRNO_HELP[seen_errno])
    if seen_errno in (140, 400210, 460020, -6):
        msg = _wall_message()
        if withheld_names:
            msg += f"\n\n<i>File seen: {withheld_names}</i>"
        if api_error:
            msg += f"\n\n<b>API attempt failed:</b>\n<code>{api_error[:220]}</code>"
        raise TeraboxError(msg)
    transport = _LAST_TRANSPORT_ERROR.get("v") or ""
    if not seen_errno and transport:
        # Never reached TeraBox at all — say so rather than blaming its API
        raise TeraboxError(
            "<b>TeraBox tak request pahunch hi nahi payi.</b>\n\n"
            f"<i>Reason: {transport}</i>\n\n"
            + ("Cookie check karo — usme newline ya extra character to nahi?"
               if "header" in transport.lower()
               else "Thodi der baad try karo."))
    raise TeraboxError(
        "<b>TeraBox se file list nahi mili.</b>"
        + (f"\n\n<i>errno: {seen_errno}</i>" if seen_errno else "")
        + (f"\n<i>{transport}</i>" if transport else "")
        + "\n\n<i>Owner: <code>/tbtest</code> chala kar mirrors ka status dekho.</i>")


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


async def download_file(entry: Dict, output_dir: str,
                        progress=None) -> Optional[str]:
    """Download one resolved entry. Returns the saved path, or None."""
    dlink = entry.get("dlink")
    if not dlink:
        return None
    os.makedirs(output_dir, exist_ok=True)
    name = _safe_name(entry.get("name") or "terabox_file")
    dest = os.path.join(output_dir, name)
    mirror = entry.get("mirror") or MIRRORS[0]

    if mirror == "xapiverse":
        # These URLs point at the API provider, not TeraBox. Sending the
        # TeraBox session cookie there would hand a third party a
        # logged-in credential it has no need for.
        headers = {"User-Agent": DESKTOP_UA, "Accept": "*/*"}
    else:
        headers = _headers(f"https://{mirror}/")
        headers["Accept"] = "*/*"

    jar = aiohttp.CookieJar(unsafe=True)
    timeout = aiohttp.ClientTimeout(total=None, sock_connect=30, sock_read=60)
    async with aiohttp.ClientSession(cookie_jar=jar, timeout=timeout) as session:
        for attempt in range(1, 4):
            try:
                have = os.path.getsize(dest) if os.path.exists(dest) else 0
                h = dict(headers)
                if have > 1024:
                    h["Range"] = f"bytes={have}-"
                async with session.get(dlink, headers=h, proxy=_proxy(),
                                       allow_redirects=True) as r:
                    if r.status in (403, 410):
                        return None          # signed link expired
                    if r.status not in (200, 206):
                        await asyncio.sleep(2 * attempt)
                        continue
                    mode = "ab" if (r.status == 206 and have) else "wb"
                    total = r.content_length or 0
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
            except Exception:
                await asyncio.sleep(2 * attempt)
    return dest if os.path.exists(dest) and os.path.getsize(dest) > 1024 else None


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
    """Owner-facing probe, mirrored on /igtest's style."""
    out = ["<b>TeraBox Diagnostics</b>", ""]
    problem = cookie_problem()
    if not has_cookie():
        out.append("Cookie: <b>not set</b>")
    elif problem:
        out.append(f"Cookie: <b>{problem}</b>")
    else:
        ck = _cookie_header()
        m = re.search(r"ndus=([^;]+)", ck, re.I)
        val = m.group(1) if m else ""
        out.append(f"Cookie: set (ndus, {len(val)} chars)")
    out.append(f"API keys: {api_key_status()}")
    px = _proxy()
    out.append(f"Proxy: {px.split('@')[-1][:32] if px else 'not set'}")
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
        except Exception as e:
            out.append(f"API resolve: <b>failed</b> — <code>{str(e)[:180]}</code>")
        out.append("")
    if not surl:
        out += ["<i>Tip: <code>/tbtest &lt;link&gt;</code> chalao — bina link ke "
                "sirf reachability test hoti hai, file list nahi.</i>", ""]
    jar = aiohttp.CookieJar(unsafe=True)
    async with aiohttp.ClientSession(cookie_jar=jar) as session:
        for mirror in MIRRORS[:5]:
            token = await _warm_up(session, mirror, surl or "")
            entries, errno = await _list_on_mirror(session, mirror,
                                                   surl or "", token)
            if entries is not None:
                line = f"{mirror}: {len(entries)} entries"
                # Listing working is only half the job — report whether a
                # download link can actually be minted, which is the step
                # that fails on a walled IP.
                try:
                    files = await _flatten(session, mirror, surl, token, entries)
                    if files:
                        have = sum(1 for f in files if f.get("dlink"))
                        if not have:
                            await _mint_dlinks(session, mirror, surl, token, files)
                            have = sum(1 for f in files if f.get("dlink"))
                        line += (f" · dlink {have}/{len(files)} "
                                 + ("" if have else "withheld"))
                except Exception:
                    pass
                out.append(line)
            elif not surl:
                # No link given: reaching the API at all is the useful signal
                out.append(f"{'' if token else ''} {mirror}: reachable"
                           f" (jsToken {'' if token else ''})")
            else:
                tag = {105: "share not found", 140: "IP walled",
                       2: "download refused (IP/cookie)",
                       4000020: "token rejected", -9: "password protected",
                       400210: "verification required"}.get(errno, f"errno {errno}")
                out.append(f"{mirror}: {tag}"
                           + (f" (jsToken {'' if token else ''})"))
    out += ["", "<i>Listing lekin dlink = TeraBox file dikhata hai par "
            "download link rok raha hai. Ye IP-level block hai — cookie se "
            "theek nahi hota.</i>"]
    if not _proxy():
        out.append("<i>Fix: API key working rakho, ya residential proxy laga kar "
                   "<code>TERABOX_PROXY</code> set karo.</i>")
    return "\n".join(out)

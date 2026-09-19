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
import json
import os
import re
from typing import Dict, List, Optional, Tuple
from urllib.parse import parse_qs, urlparse

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
    105: ("❌ <b>Ye TeraBox link valid nahi hai.</b>\n\n"
          "Link galat hai, expire ho gaya, ya delete kar diya gaya."),
    -9:  ("🔑 <b>Is share par password laga hai.</b>\n\n"
          "Password-protected TeraBox links abhi support nahi hain."),
}


def _wall_message() -> str:
    """errno 140 / 400210: real link, but the server IP is refused."""
    if has_cookie():
        return (
            "🚫 <b>TeraBox ne is request ko block kar diya.</b>\n\n"
            "Cookie set hai lekin phir bhi refuse kar raha hai. Wajah:\n"
            "• Cookie expire ho gayi hai\n"
            "• File adult/restricted flag wali hai\n"
            "• Server IP par temporary limit hai\n\n"
            "✅ <b>Fix:</b> browser se fresh <code>ndus</code> cookie "
            "lekar <code>TERABOX_COOKIE</code> update karo.")
    return (
        "🚫 <b>TeraBox ne server IP se access block kiya.</b>\n\n"
        "<i>Link bilkul sahi hai</i> — TeraBox anonymous datacenter IPs ko "
        "file list nahi deta. Browser me khulta hai, server se nahi.\n\n"
        "✅ <b>Fix:</b> TeraBox me login karke browser se <code>ndus</code> "
        "cookie copy karo aur <code>TERABOX_COOKIE</code> env var me daalo.\n\n"
        "<i>Chrome → F12 → Application → Cookies → terabox.com → ndus</i>")


# Remembers why the last request failed, so a transport-level problem
# (bad cookie, DNS, timeout) is reported instead of being mistaken for
# "TeraBox changed its API".
_LAST_TRANSPORT_ERROR: Dict[str, str] = {"v": ""}


async def _get_json(session: aiohttp.ClientSession, url: str,
                    referer: str = "") -> Optional[Dict]:
    try:
        async with session.get(url, headers=_headers(referer),
                               timeout=aiohttp.ClientTimeout(total=25),
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
    base = (f"https://{mirror}/share/list?app_id={APP_ID}"
            f"&shorturl=1{surl}&root={'0' if dir_path else '1'}")
    if dir_path:
        from urllib.parse import quote
        base += f"&dir={quote(dir_path)}"

    attempts = [base]
    if token:
        attempts.insert(0, base + f"&jsToken={token}")

    last_errno = 0
    for url in attempts:
        data = await _get_json(session, url, referer)
        if not data:
            continue
        errno = int(data.get("errno", -1) or 0)
        if errno == 0:
            return (data.get("list") or []), 0
        last_errno = errno
    return None, last_errno


async def list_files(url: str) -> List[Dict]:
    """Resolve a share link to a flat list of downloadable files.

    Each entry: {name, size, fs_id, dlink, is_dir, path}
    """
    if not is_terabox_url(url):
        raise TeraboxError("❌ Ye TeraBox ka link nahi hai.")
    surl = extract_surl(url)
    if not surl:
        raise TeraboxError(
            "❌ <b>Is link se share ID nahi mila.</b>\n\n"
            "Format aisa hona chahiye: <code>terabox.com/s/1xxxxxxx</code>")

    problem = cookie_problem()
    if problem and problem != "not set":
        raise TeraboxError(
            f"🍪 <b>TERABOX_COOKIE thik nahi hai:</b> {problem}.\n\n"
            "Chrome → F12 → Application → Cookies → terabox.com → "
            "<code>ndus</code> → poori value copy karo.")

    _LAST_TRANSPORT_ERROR["v"] = ""
    jar = aiohttp.CookieJar(unsafe=True)
    timeout = aiohttp.ClientTimeout(total=None, sock_connect=20, sock_read=30)
    seen_errno = 0

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
                    return files
                raise TeraboxError(
                    "📭 <b>Is share me koi file nahi mili.</b>\n\n"
                    "Folder khali hai ya uska content hata diya gaya hai.")
            if errno:
                seen_errno = errno
            # 105 means the share genuinely does not exist — no point sweeping
            if errno == 105:
                break

    if seen_errno in _ERRNO_HELP:
        raise TeraboxError(_ERRNO_HELP[seen_errno])
    if seen_errno in (140, 400210, 460020, -6):
        raise TeraboxError(_wall_message())
    transport = _LAST_TRANSPORT_ERROR.get("v") or ""
    if not seen_errno and transport:
        # Never reached TeraBox at all — say so rather than blaming its API
        raise TeraboxError(
            "❌ <b>TeraBox tak request pahunch hi nahi payi.</b>\n\n"
            f"<i>Reason: {transport}</i>\n\n"
            + ("✅ Cookie check karo — usme newline ya extra character to nahi?"
               if "header" in transport.lower()
               else "✅ Thodi der baad try karo."))
    raise TeraboxError(
        "❌ <b>TeraBox se file list nahi mili.</b>"
        + (f"\n\n<i>errno: {seen_errno}</i>" if seen_errno else "")
        + (f"\n<i>{transport}</i>" if transport else "")
        + "\n\n<i>Owner: <code>/tbtest</code> chala kar mirrors ka status dekho.</i>")


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
                async with session.get(dlink, headers=h,
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
                                    progress(done, total + (have if mode == "ab" else 0))
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
    out = ["🔬 <b>TeraBox Diagnostics</b>", ""]
    problem = cookie_problem()
    if not has_cookie():
        out.append("🍪 Cookie: ❌ <b>not set</b>")
    elif problem:
        out.append(f"🍪 Cookie: ⚠️ <b>{problem}</b>")
    else:
        ck = _cookie_header()
        m = re.search(r"ndus=([^;]+)", ck, re.I)
        val = m.group(1) if m else ""
        out.append(f"🍪 Cookie: ✅ set (ndus, {len(val)} chars)")
    if url:
        out.append(f"🔗 surl: <code>{extract_surl(url) or 'not parsed'}</code>")
    out.append("")

    surl = extract_surl(url) if url else "1zzzzzzzzzzzzzzz"
    jar = aiohttp.CookieJar(unsafe=True)
    async with aiohttp.ClientSession(cookie_jar=jar) as session:
        for mirror in MIRRORS[:5]:
            token = await _warm_up(session, mirror, surl or "")
            entries, errno = await _list_on_mirror(session, mirror,
                                                   surl or "", token)
            if entries is not None:
                out.append(f"✅ {mirror}: {len(entries)} entries")
            else:
                tag = {105: "invalid link", 140: "IP walled",
                       -9: "password"}.get(errno, f"errno {errno}")
                out.append(f"⚠️ {mirror}: {tag}"
                           + (f" (jsToken {'✅' if token else '❌'})"))
    out += ["", "<i>errno 140 = link sahi hai par IP block hai; "
            "cookie set karne se chalega.</i>"]
    return "\n".join(out)

# utils/link_parser.py
import os
import re
from pathlib import Path
from typing import List, Dict

URL_REGEX = re.compile(
    r"(https?://[^\s]+)",
    re.IGNORECASE
)

# Markdown-style links appear in pasted text from many apps:
# [name](https://example.com/file). Extract the URL inside parentheses first so
# the generic URL regex does not keep the leading display text plus `](`.
MARKDOWN_LINK_REGEX = re.compile(
    r"\[[^\]]*\]\((https?://[^)\s]+)\)",
    re.IGNORECASE,
)

# Links pasted without a scheme, e.g. "instagram.com/reel/ABC/" or
# "www.youtube.com/watch?v=..". People copy these from mobile share sheets
# all the time, so treat them as real URLs.
BARE_URL_REGEX = re.compile(
    r"(?<![\w@/.])((?:www\.)?[a-z0-9][a-z0-9\-]*(?:\.[a-z0-9\-]+)*"
    r"\.(?:com|net|org|tv|me|co|io|live|app|be|ly|in|to|cc|xyz|watch|site|dev|cloud)"
    r"(?:/[^\s]*)?)",
    re.IGNORECASE,
)

VIDEO_EXT = {
    ".mp4", ".mkv", ".mov", ".avi", ".webm", ".ts"
}
ARCHIVE_EXT = {
    ".zip", ".rar", ".7z", ".tar", ".gz", ".tgz", ".tar.gz", ".tar.bz2", ".tbz2", ".bz2", ".xz"
}
AUDIO_EXT = {
    ".mp3", ".m4a", ".aac", ".ogg", ".opus", ".flac", ".wav"
}
APK_EXT = {
    ".apk", ".xapk", ".apks"
}

FILE_EXT = VIDEO_EXT | ARCHIVE_EXT | AUDIO_EXT | APK_EXT


def find_links_in_text(text: str) -> List[str]:
    """Extract URLs from text, including ones pasted without http(s)://."""
    text = text or ""
    out: List[str] = []
    seen = set()

    def _add(url: str):
        url = url.strip().strip(".,)\u201d\"'<>")
        if not url:
            return
        key = url.lower().rstrip("/")
        if key in seen:
            return
        seen.add(key)
        out.append(url)

    spans = []
    for m in MARKDOWN_LINK_REGEX.finditer(text):
        spans.append(m.span())
        _add(m.group(1))

    for m in URL_REGEX.finditer(text):
        if any(s <= m.start() < e for s, e in spans):
            continue
        spans.append(m.span())
        _add(m.group(1))

    # Bare domains — skip anything already inside a full URL match
    for m in BARE_URL_REGEX.finditer(text):
        if any(s <= m.start() < e for s, e in spans):
            continue
        cand = m.group(1)
        if "." not in cand.split("/")[0]:
            continue
        key = ("https://" + cand).lower().rstrip("/")
        if key in seen or cand.lower().rstrip("/") in seen:
            continue
        seen.add(key)
        out.append("https://" + cand)

    return out



def extract_links_from_folder(base_dir: str) -> Dict[str, List[str]]:
    """
    Scan .txt and .m3u/.m3u8 files inside extracted archive for links.
    """
    base = Path(base_dir)
    all_links: Dict[str, List[str]] = {
        "direct": [],
        "m3u8": [],
        "gdrive": [],
        "telegram": [],
        "unknown": [],
    }

    for root, dirs, files in os.walk(base):
        for f in files:
            p = Path(root) / f
            ext = p.suffix.lower()
            if ext not in {".txt", ".m3u", ".m3u8"}:
                continue
            try:
                text = p.read_text(encoding="utf-8", errors="ignore")
            except Exception:
                continue

            urls = find_links_in_text(text)
            for url in urls:
                kind = classify_link(url)
                all_links.setdefault(kind, [])
                if url not in all_links[kind]:
                    all_links[kind].append(url)

    return all_links


INSTAGRAM_DOMAINS = ["instagram.com", "instagr.am", "ddinstagram.com"]

# Sites yt-dlp can extract media from. yt-dlp supports 1800+ sites, so this
# list is only a fast path — anything not listed still falls through to
# yt-dlp via the "unknown" branch rather than being HTTP-fetched as a page.
YTDL_DOMAINS = [
    # video
    "youtube.com", "youtu.be", "youtube-nocookie.com", "m.youtube.com",
    "twitter.com", "x.com", "t.co", "facebook.com", "fb.watch", "fb.com",
    "tiktok.com", "vt.tiktok.com", "vm.tiktok.com",
    "vimeo.com", "dailymotion.com", "dai.ly", "reddit.com", "redd.it",
    "twitch.tv", "clips.twitch.tv", "bilibili.com", "ok.ru", "vk.com",
    "rumble.com", "odysee.com", "bitchute.com", "streamable.com",
    "9gag.com", "imgur.com", "gfycat.com", "tumblr.com",
    "linkedin.com", "pinterest.com", "pin.it", "snapchat.com",
    "threads.net", "threads.com", "kick.com", "nicovideo.jp",
    "douyin.com", "kuaishou.com", "weibo.com", "xiaohongshu.com",
    "likee.video", "josh.in", "chingari.io", "moj.share",
    "sharechat.com", "roposo.com", "mxtakatak.com",
    # news / broadcast
    "bbc.co.uk", "bbc.com", "cnn.com", "nytimes.com", "aajtak.in",
    "ndtv.com", "zeenews.india.com", "abplive.com", "news18.com",
    "espn.com", "hotstar.com", "voot.com", "sonyliv.com", "zee5.com",
    "jiocinema.com", "mxplayer.in", "ullu.app", "altbalaji.com",
    # audio / music
    "soundcloud.com", "on.soundcloud.com", "bandcamp.com",
    "mixcloud.com", "audiomack.com", "spotify.com", "open.spotify.com",
    "deezer.com", "audius.co", "jiosaavn.com", "gaana.com", "wynk.in",
    # education / misc
    "ted.com", "coursera.org", "udemy.com", "khanacademy.org",
    "archive.org", "rutube.ru", "pornhub.com", "xvideos.com",
]

# Hosts that need a browser-style page scrape or a special extractor rather
# than a plain GET. Kept separate so they are never treated as direct files.
FILEHOST_DOMAINS = [
    "mega.nz", "mediafire.com", "pixeldrain.com", "anonfiles.com",
    "gofile.io", "1fichier.com", "krakenfiles.com", "bayfiles.com",
    "workupload.com", "send.cm", "dropbox.com", "we.tl", "wetransfer.com",
    "terabox.com", "1024terabox.com", "teraboxapp.com", "4funbox.com",
    "mirrobox.com", "nephobox.com", "terabox.app", "1024tera.com",
    "teraboxlink.com", "momerybox.com", "tibibox.com", "freeterabox.com",
    "terasharelink.com", "terafileshare.com", "teraboxshare.com",
]

def _host_of(url: str) -> str:
    """Hostname of a URL, lowercased and without a leading www."""
    u = (url or "").strip()
    if "://" not in u:
        u = "https://" + u
    try:
        from urllib.parse import urlparse
        host = (urlparse(u).hostname or "").lower()
    except Exception:
        host = ""
    return host[4:] if host.startswith("www.") else host


def _host_matches(host: str, domain: str) -> bool:
    """True if `host` is `domain` or a subdomain of it.

    Substring matching is wrong here: "ok.co" would match "ex.com" and
    "4funbox.com" would match "terabox.com", mis-routing ordinary links.
    """
    domain = domain.lower().lstrip(".")
    if "/" in domain:                      # entries like "drive.google.com/uc"
        return domain in host
    return host == domain or host.endswith("." + domain)


def classify_link(url: str) -> str:
    """
    Return: 'gdrive' | 'telegram' | 'instagram' | 'm3u8' | 'ytdl' |
            'filehost' | 'direct' | 'unknown'
    """
    u = url.strip()
    u_low = u.lower()
    host = _host_of(u)

    if _host_matches(host, "drive.google.com"):
        return "gdrive"
    if _host_matches(host, "t.me") or _host_matches(host, "telegram.me"):
        return "telegram"

    for domain in INSTAGRAM_DOMAINS:
        if _host_matches(host, domain):
            return "instagram"

    # A real file extension wins over the domain list: a direct .mp4 on a
    # listed site should be fetched directly, not handed to yt-dlp.
    base_early = u_low.split("?", 1)[0].split("#", 1)[0]
    for ext in FILE_EXT:
        if base_early.endswith(ext):
            return "direct"
    if base_early.endswith(".m3u8") or base_early.endswith(".mpd"):
        return "m3u8"

    for domain in YTDL_DOMAINS:
        if _host_matches(host, domain):
            return "ytdl"

    for domain in FILEHOST_DOMAINS:
        if _host_matches(host, domain):
            return "filehost"

    base = u_low.split("?", 1)[0].split("#", 1)[0]
    if base.endswith(".m3u8"):
        return "m3u8"
    if base.endswith(".mpd"):
        return "m3u8"
    for ext in FILE_EXT:
        if base.endswith(ext):
            return "direct"

    # Query strings often carry the real filename, e.g. ?file=movie.mkv
    for ext in FILE_EXT:
        if ext in u_low:
            return "direct"

    # Download-gateway shapes: a token endpoint that hands back a file.
    # These have no extension in the path, so without this they fell
    # through to yt-dlp, which has no extractor for them.
    if any(k in u_low for k in ("dlink", "/dl?", "/dl/", "download",
                                "getfile", "get_file", "fetch?", "token=")):
        return "direct"

    return "unknown"


# ── Runtime probing ──────────────────────────────────────────────────────────
# Extensions lie and many CDNs serve files from extension-less URLs, so when
# static classification says "unknown" we ask the server what it actually is.

_MEDIA_CT_PREFIXES = ("video/", "audio/", "image/")
_DIRECT_CT = {
    "application/zip", "application/x-zip-compressed",
    "application/x-rar-compressed", "application/vnd.rar",
    "application/x-7z-compressed", "application/x-tar",
    "application/gzip", "application/x-gzip",
    "application/pdf", "application/epub+zip",
    "application/vnd.android.package-archive",
    "application/octet-stream",
    "application/x-msdownload", "application/x-iso9660-image",
}
_PLAYLIST_CT = {
    "application/vnd.apple.mpegurl", "application/x-mpegurl",
    "audio/x-mpegurl", "application/dash+xml",
}


async def probe_link_kind(url: str, timeout: int = 15) -> str:
    """Ask the server what a URL really serves.

    Returns 'direct', 'm3u8', 'ytdl' or 'unknown'. Never raises — an
    unreachable URL simply comes back as 'unknown'.
    """
    import aiohttp

    def _from_ct(ct: str, disp: str = "") -> str:
        ct = (ct or "").split(";")[0].strip().lower()
        if ct in _PLAYLIST_CT:
            return "m3u8"
        if ct in _DIRECT_CT or ct.startswith(_MEDIA_CT_PREFIXES):
            return "direct"
        # A download disposition means a file regardless of content type
        if "attachment" in (disp or "").lower():
            return "direct"
        if ct.startswith("text/html"):
            return "ytdl"      # an HTML page → let yt-dlp try to extract
        return ""

    headers = {
        "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                       "AppleWebKit/537.36 (KHTML, like Gecko) "
                       "Chrome/125.0.0.0 Safari/537.36"),
        "Accept": "*/*",
    }
    try:
        conn = aiohttp.TCPConnector(ssl=False)
        async with aiohttp.ClientSession(connector=conn) as session:
            # HEAD first — cheap, and enough for most servers
            try:
                async with session.head(
                    url, headers=headers, allow_redirects=True,
                    timeout=aiohttp.ClientTimeout(total=timeout)) as r:
                    kind = _from_ct(r.headers.get("Content-Type", ""),
                                    r.headers.get("Content-Disposition", ""))
                    if kind:
                        return kind
            except Exception:
                pass

            # Some servers reject HEAD; fetch a single byte instead
            try:
                rng = dict(headers, Range="bytes=0-0")
                async with session.get(
                    url, headers=rng, allow_redirects=True,
                    timeout=aiohttp.ClientTimeout(total=timeout)) as r:
                    kind = _from_ct(r.headers.get("Content-Type", ""),
                                    r.headers.get("Content-Disposition", ""))
                    if kind:
                        return kind
            except Exception:
                pass
    except Exception:
        pass
    return "unknown"

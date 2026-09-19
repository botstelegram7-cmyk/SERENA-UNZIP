# utils/link_parser.py
import os
import re
from pathlib import Path
from typing import List, Dict

URL_REGEX = re.compile(
    r"(https?://[^\s]+)",
    re.IGNORECASE
)

# Links pasted without a scheme, e.g. "instagram.com/reel/ABC/" or
# "www.youtube.com/watch?v=..". People copy these from mobile share sheets
# all the time, so treat them as real URLs.
BARE_URL_REGEX = re.compile(
    r"(?<![\w@/.])((?:www\.)?[a-z0-9][a-z0-9\-]*(?:\.[a-z0-9\-]+)*"
    r"\.(?:com|net|org|tv|me|co|io|live|app|be|ly|in|to|cc|xyz|watch)"
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
    for m in URL_REGEX.finditer(text):
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

YTDL_DOMAINS = [
    "twitter.com", "x.com", "facebook.com", "fb.watch",
    "tiktok.com", "vimeo.com", "dailymotion.com", "reddit.com", "twitch.tv",
    "bilibili.com", "ok.ru", "vk.com",
]

def classify_link(url: str) -> str:
    """
    Return: 'gdrive' | 'telegram' | 'instagram' | 'm3u8' | 'ytdl' | 'direct' | 'unknown'
    """
    u = url.strip()
    u_low = u.lower()

    if "drive.google.com" in u_low:
        return "gdrive"
    if "t.me/" in u_low or "telegram.me/" in u_low:
        return "telegram"

    for domain in INSTAGRAM_DOMAINS:
        if domain in u_low:
            return "instagram"

    for domain in YTDL_DOMAINS:
        if domain in u_low:
            return "ytdl"

    base = u_low.split("?", 1)[0].split("#", 1)[0]
    if base.endswith(".m3u8"):
        return "m3u8"
    for ext in FILE_EXT:
        if base.endswith(ext):
            return "direct"

    return "unknown"

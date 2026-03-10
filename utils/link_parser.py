# utils/link_parser.py
import os
import re
from pathlib import Path
from typing import List, Dict

URL_REGEX = re.compile(
    r"(https?://[^\s]+)",
    re.IGNORECASE
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
    return [m.group(1).strip().strip(".,)") for m in URL_REGEX.finditer(text)]



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


YTDL_DOMAINS = [
    "instagram.com", "twitter.com", "x.com", "facebook.com", "fb.watch",
    "tiktok.com", "vimeo.com", "dailymotion.com", "reddit.com", "twitch.tv",
    "bilibili.com", "ok.ru", "vk.com",
]

def classify_link(url: str) -> str:
    """
    Return: 'gdrive' | 'telegram' | 'm3u8' | 'ytdl' | 'direct' | 'unknown'
    """
    u = url.strip()
    u_low = u.lower()

    if "drive.google.com" in u_low:
        return "gdrive"
    if "t.me/" in u_low or "telegram.me/" in u_low:
        return "telegram"

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

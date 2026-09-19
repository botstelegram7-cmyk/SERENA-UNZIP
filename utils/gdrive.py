# utils/gdrive.py
from urllib.parse import urlparse, parse_qs
from typing import Optional


def _extract_file_id(url: str) -> Optional[str]:
    """
    Handle variants:
    - https://drive.google.com/file/d/FILE_ID/view?usp=sharing
    - https://drive.google.com/open?id=FILE_ID
    - https://drive.google.com/uc?export=download&id=FILE_ID
    """
    parsed = urlparse(url)
    qs = parse_qs(parsed.query)

    if "id" in qs:
        return qs["id"][0]

    parts = [p for p in parsed.path.split("/") if p]
    if "d" in parts:
        idx = parts.index("d")
        if idx + 1 < len(parts):
            return parts[idx + 1]

    return None


def get_gdrive_direct_link(url: str) -> Optional[str]:
    """
    Convert normal Google Drive share link to direct download link.
    Big files (virus scan bypass) may still fail (confirm token),
    but small/medium files usually work.
    """
    file_id = _extract_file_id(url)
    if not file_id:
        return None
    return f"https://drive.google.com/uc?export=download&id={file_id}"


# ── Folder & robust file support ─────────────────────────────────────────────
# The share links people actually paste are often folders, and mobile links
# nest several ids in the path:
#   /drive/mobile/folders/<parent>/<child>/<grandchild>?sort=13
# The last id in the path is the folder actually being viewed.

_FOLDER_MARKERS = ("/folders/", "/drive/folders/", "/drive/mobile/folders/")


def is_gdrive_folder(url: str) -> bool:
    """True if the link points at a Drive folder rather than a single file."""
    u = (url or "").lower()
    return any(m in u for m in _FOLDER_MARKERS)


def extract_folder_id(url: str) -> Optional[str]:
    """Return the id of the folder being viewed.

    For nested mobile links the *last* path segment is the open folder, so
    that is the one to download.
    """
    if not is_gdrive_folder(url):
        return None
    parsed = urlparse(url)
    parts = [p for p in parsed.path.split("/") if p]
    try:
        idx = parts.index("folders")
    except ValueError:
        return None
    ids = [p for p in parts[idx + 1:] if _looks_like_id(p)]
    return ids[-1] if ids else None


def _looks_like_id(value: str) -> bool:
    """Drive ids are long opaque strings; filter out path words."""
    if len(value) < 10:
        return False
    import re as _re
    return bool(_re.fullmatch(r"[A-Za-z0-9_-]+", value))


def gdrive_kind(url: str) -> str:
    """Classify a Drive link: 'folder', 'file' or 'unknown'."""
    if is_gdrive_folder(url):
        return "folder" if extract_folder_id(url) else "unknown"
    return "file" if _extract_file_id(url) else "unknown"


# ── Downloading ──────────────────────────────────────────────────────────────
# gdown is used because it handles the two things a plain GET cannot:
# the "file too large to scan for viruses" confirm token, and folders.

import asyncio
import os
from typing import Dict, List, Tuple


class GDriveError(RuntimeError):
    """Raised with a user-facing Hinglish message."""


def _friendly(err: str, kind: str = "file") -> str:
    low = (err or "").lower()
    what = "folder" if kind == "folder" else "file"
    if "permission" in low or "public link" in low or "anyone with the link" in low:
        return (
            f"🔒 <b>Ye Drive {what} public nahi hai.</b>\n\n"
            "Owner ko bolo sharing badalne ke liye:\n"
            "<b>Share → General access → Anyone with the link</b>\n\n"
            "<i>Private files bina login ke download nahi ho sakti.</i>")
    if "too many" in low or "quota" in low or "429" in low:
        return (
            "⏳ <b>Google ne download quota limit kar diya.</b>\n\n"
            "Ye file aaj bahut baar download hui hai.\n"
            "<i>24 ghante baad try karo, ya file ko apni Drive me "
            "copy karke uska link bhejo.</i>")
    if "status code 500" in low or "retrieve folder" in low:
        return (
            "❌ <b>Folder ka content nahi mil paya.</b>\n\n"
            "Wajah: folder private hai, ya usme bahut zyada files hain.\n"
            "<i>Sharing 'Anyone with the link' karo, ya andar ki "
            "files ka direct link bhejo.</i>")
    if "not found" in low or "404" in low:
        return f"❌ <b>Drive {what} nahi mila</b> — link galat hai ya delete ho chuka hai."
    return f"❌ Google Drive {what} download failed:\n<code>{(err or '')[:200]}</code>"


async def download_gdrive_file(url: str, output_dir: str) -> str:
    """Download a single Drive file. Returns the saved path."""
    import gdown

    file_id = _extract_file_id(url)
    if not file_id:
        raise GDriveError("❌ Is link se Drive file ID nahi mila.")
    os.makedirs(output_dir, exist_ok=True)

    def _run() -> str:
        return gdown.download(
            id=file_id, output=os.path.join(output_dir, ""),
            quiet=True, use_cookies=False, resume=True, retries=3)

    try:
        path = await asyncio.to_thread(_run)
    except Exception as e:
        raise GDriveError(_friendly(str(e), "file")) from e
    if not path or not os.path.exists(path):
        raise GDriveError(_friendly("", "file"))
    return path


async def list_gdrive_folder(url: str) -> List[Dict]:
    """List a folder's contents without downloading (id, path)."""
    import gdown

    def _run():
        return gdown.download_folder(
            url=url, skip_download=True, quiet=True, use_cookies=False)

    try:
        items = await asyncio.to_thread(_run)
    except Exception as e:
        raise GDriveError(_friendly(str(e), "folder")) from e
    out = []
    for it in (items or []):
        out.append({"id": getattr(it, "id", ""),
                    "path": getattr(it, "path", str(it))})
    return out


async def download_gdrive_folder(url: str, output_dir: str,
                                 timeout: int = 3600) -> List[str]:
    """Download every file in a Drive folder. Returns saved paths."""
    import gdown

    os.makedirs(output_dir, exist_ok=True)

    def _run():
        return gdown.download_folder(
            url=url, output=output_dir, quiet=True,
            use_cookies=False, resume=True, retries=3)

    try:
        paths = await asyncio.wait_for(asyncio.to_thread(_run), timeout=timeout)
    except asyncio.TimeoutError:
        # Partial results are still useful — return whatever landed on disk
        got = _walk_files(output_dir)
        if got:
            return got
        raise GDriveError("⏳ Folder download timeout ho gaya (bahut bada hai).")
    except Exception as e:
        got = _walk_files(output_dir)
        if got:
            return got
        raise GDriveError(_friendly(str(e), "folder")) from e

    files = [p for p in (paths or []) if p and os.path.isfile(p)]
    return files or _walk_files(output_dir)


def _walk_files(root: str) -> List[str]:
    out: List[str] = []
    for base, _dirs, names in os.walk(root):
        for n in names:
            p = os.path.join(base, n)
            try:
                if os.path.getsize(p) > 0:
                    out.append(p)
            except OSError:
                pass
    return sorted(out)

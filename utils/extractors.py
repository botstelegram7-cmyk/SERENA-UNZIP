# utils/extractors.py — Fixed: 7z magic bytes + system 7za binary + task persistence
import os
import shutil
import subprocess
import zipfile
import tarfile
from pathlib import Path
from typing import Dict, Any, List, Optional

import py7zr
import rarfile

VIDEO_EXT = {".mp4", ".mkv", ".mov", ".avi", ".webm"}
PDF_EXT = {".pdf"}
APK_EXT = {".apk", ".xapk", ".apks"}
TXT_EXT = {".txt"}
M3U_EXT = {".m3u", ".m3u8"}


def _scan_stats(base_dir: Path) -> Dict[str, Any]:
    stats = {
        "total_files": 0, "videos": 0, "pdf": 0,
        "apk": 0, "txt": 0, "m3u": 0, "others": 0, "folders": 0,
    }
    files: List[str] = []
    for root, dirs, fls in os.walk(base_dir):
        rel_root = os.path.relpath(root, base_dir)
        if rel_root != ".":
            stats["folders"] += 1
        for f in fls:
            stats["total_files"] += 1
            p = Path(root) / f
            rel_path = os.path.relpath(p, base_dir)
            ext = p.suffix.lower()
            if ext in VIDEO_EXT:         stats["videos"] += 1
            elif ext in PDF_EXT:         stats["pdf"] += 1
            elif ext in APK_EXT:         stats["apk"] += 1
            elif ext in TXT_EXT:         stats["txt"] += 1
            elif ext in M3U_EXT:         stats["m3u"] += 1
            else:                        stats["others"] += 1
            files.append(rel_path)
    return {"stats": stats, "files": files}


# ── Magic byte signatures ─────────────────────────────────────────────────────
_MAGIC = {
    b"PK\x03\x04": "zip",          # ZIP
    b"PK\x05\x06": "zip",          # ZIP empty
    b"PK\x07\x08": "zip",          # ZIP spanned
    b"Rar!":        "rar",          # RAR
    b"7z\xbc\xaf":  "7z",          # 7-Zip  (first 4 bytes of 6-byte sig)
    b"\x1f\x8b":    "tar",          # .tar.gz / .tgz
    b"BZh":         "tar",          # .tar.bz2
    b"\xfd7zXZ":    "tar",          # .tar.xz
    b"\x1f\x9d":    "tar",          # .Z compress
}

def _archive_type(path: str) -> Optional[str]:
    """
    Robust archive detection — checks suffix first, then magic bytes.
    Returns: 'zip' | 'tar' | '7z' | 'rar' | None
    """
    p = Path(path)
    suffixes = "".join(p.suffixes).lower()

    # Suffix-based detection (fast path)
    if suffixes.endswith(".zip"):                                   return "zip"
    if suffixes.endswith((".tar.gz", ".tgz", ".tar.bz2",
                          ".tbz2", ".tar.xz", ".txz")):            return "tar"
    if suffixes.endswith(".tar"):                                   return "tar"
    if suffixes.endswith(".7z"):                                    return "7z"
    if suffixes.endswith(".rar"):                                   return "rar"
    s = p.suffix.lower()
    if s == ".zip":                                                 return "zip"
    if s in (".tar", ".gz", ".bz2", ".xz"):                        return "tar"
    if s == ".7z":                                                  return "7z"
    if s == ".rar":                                                 return "rar"

    # Magic bytes fallback (handles files downloaded without extension)
    try:
        with open(path, "rb") as f:
            header = f.read(8)
        for magic, fmt in _MAGIC.items():
            if header.startswith(magic):
                return fmt
    except Exception:
        pass

    return None


def is_zip_encrypted(path: str) -> bool:
    try:
        with zipfile.ZipFile(path) as z:
            for zinfo in z.infolist():
                if zinfo.flag_bits & 0x1:
                    return True
    except Exception:
        return False
    return False


def detect_encrypted(path: str) -> bool:
    t = _archive_type(path)
    if t == "zip":
        return is_zip_encrypted(path)
    try:
        if t == "rar":
            with rarfile.RarFile(path) as rf:
                _ = rf.infolist()
        elif t == "7z":
            with py7zr.SevenZipFile(path, mode="r") as z:
                _ = z.getnames()
        else:
            return False
    except (rarfile.NeedFirstVolume, rarfile.PasswordRequired,
            py7zr.exceptions.PasswordRequired):
        return True
    except Exception:
        return False
    return False


def _7z_system_extract(archive_path: str, dest_dir: str, password: Optional[str] = None) -> bool:
    """
    Use system p7zip (7za) binary to extract 7z archives.
    Much more reliable than py7zr for split archives, solid archives, etc.
    Returns True on success.
    """
    bin7z = shutil.which("7za") or shutil.which("7z")
    if not bin7z:
        return False
    cmd = [bin7z, "x", archive_path, f"-o{dest_dir}", "-y"]
    if password:
        cmd.append(f"-p{password}")
    try:
        result = subprocess.run(cmd, capture_output=True, timeout=600)
        return result.returncode == 0
    except Exception:
        return False


def extract_archive(
    archive_path: str,
    dest_dir: str,
    password: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Extract archive to dest_dir.
    Supports: zip, rar, 7z (system+py7zr), tar, tar.gz, tgz, tar.bz2, bz2, xz
    Returns: { "stats": {...}, "files": [relative paths] }
    """
    Path(dest_dir).mkdir(parents=True, exist_ok=True)
    t = _archive_type(archive_path)

    if t is None:
        raise ValueError(f"Unsupported archive format. (file: {Path(archive_path).name})")

    if t == "zip":
        with zipfile.ZipFile(archive_path) as z:
            if password:
                z.setpassword(password.encode("utf-8"))
            z.extractall(dest_dir)

    elif t == "tar":
        with tarfile.open(archive_path, "r:*") as tf:
            tf.extractall(dest_dir)

    elif t == "7z":
        # Strategy 1: system 7za (handles all 7z variants including split/solid)
        if not _7z_system_extract(archive_path, dest_dir, password):
            # Strategy 2: py7zr fallback
            try:
                with py7zr.SevenZipFile(archive_path, mode="r", password=password) as z:
                    z.extractall(dest_dir)
            except Exception as e:
                raise RuntimeError(f"7z extraction failed: {e}")

    elif t == "rar":
        with rarfile.RarFile(archive_path) as rf:
            if password:
                rf.setpassword(password)
            rf.extractall(dest_dir)

    else:
        raise ValueError("Unsupported archive format.")

    return _scan_stats(Path(dest_dir))

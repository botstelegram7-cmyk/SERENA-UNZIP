# utils/extractors.py — Fixed: 7z magic bytes + system 7za + multi-password + BCJ2
import os
import shutil
import subprocess
import zipfile
import tarfile
from pathlib import Path
from typing import Dict, Any, List, Optional, Sequence

import py7zr
import rarfile

VIDEO_EXT = {".mp4", ".mkv", ".mov", ".avi", ".webm"}
PDF_EXT   = {".pdf"}
APK_EXT   = {".apk", ".xapk", ".apks"}
TXT_EXT   = {".txt"}
M3U_EXT   = {".m3u", ".m3u8"}


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
            if ext in VIDEO_EXT:   stats["videos"] += 1
            elif ext in PDF_EXT:   stats["pdf"]    += 1
            elif ext in APK_EXT:   stats["apk"]    += 1
            elif ext in TXT_EXT:   stats["txt"]    += 1
            elif ext in M3U_EXT:   stats["m3u"]    += 1
            else:                  stats["others"] += 1
            files.append(rel_path)
    return {"stats": stats, "files": files}


# ── Magic byte signatures ─────────────────────────────────────────────────────
_MAGIC = {
    b"PK\x03\x04": "zip",
    b"PK\x05\x06": "zip",
    b"Rar!":        "rar",
    b"7z\xbc\xaf":  "7z",    # 7-Zip 4-byte prefix of 6-byte sig
    b"\x1f\x8b":    "tar",   # .tar.gz
    b"BZh":         "tar",   # .tar.bz2
    b"\xfd7zXZ":    "tar",   # .tar.xz
}

def _archive_type(path: str) -> Optional[str]:
    """Detect archive type by suffix first, then magic bytes."""
    p = Path(path)
    suffixes = "".join(p.suffixes).lower()

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

    # Magic byte fallback (handles files downloaded without extension)
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


def _7z_extract(archive_path: str, dest_dir: str, password: Optional[str] = None) -> bool:
    """
    Use system p7zip / 7za binary for 7z extraction.

    WHY system binary instead of py7zr:
    - py7zr does NOT support BCJ2 filter (method 0x06F10701) — very common in
      7z archives made with 7-Zip on Windows containing executables/videos.
    - py7zr does NOT support some AES-256 + BCJ2 combos at all.
    - p7zip-full (installed in Dockerfile) handles ALL 7z variants including BCJ2.

    Returns True on success.
    """
    bin7z = shutil.which("7za") or shutil.which("7z") or shutil.which("7zz")
    if not bin7z:
        return False

    cmd = [bin7z, "x", archive_path, f"-o{dest_dir}", "-y", "-bd"]
    if password:
        # MUST be adjacent to -p with no space: -pMyPassword
        cmd.append(f"-p{password}")
    else:
        # No password: tell 7za to skip password prompt (non-interactive)
        cmd.append("-p")

    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            timeout=600,   # 10 min max
        )
        return result.returncode == 0
    except Exception:
        return False


def _try_extract_7z(archive_path: str, dest_dir: str,
                    passwords: Sequence[Optional[str]]) -> bool:
    """
    Try a list of passwords against a 7z archive.
    Returns True as soon as one works.
    Empty list → try no-password only.
    """
    tried = set()
    for pw in (passwords or [None]):
        key = pw or ""
        if key in tried:
            continue
        tried.add(key)
        if _7z_extract(archive_path, dest_dir, pw):
            # Verify something was actually extracted (password might just not crash)
            if any(True for _ in Path(dest_dir).rglob("*") if Path(_).is_file()):
                return True
            # Nothing extracted → wrong password, clean and try next
            shutil.rmtree(dest_dir, ignore_errors=True)
            Path(dest_dir).mkdir(parents=True, exist_ok=True)
    return False


def extract_archive(
    archive_path: str,
    dest_dir: str,
    password: Optional[str] = None,
    extra_passwords: Optional[List[str]] = None,
) -> Dict[str, Any]:
    """
    Extract archive to dest_dir.

    Args:
        archive_path: Path to archive file.
        dest_dir:     Destination directory.
        password:     Primary password (from user or /zqpass).
        extra_passwords: Additional passwords to try if primary fails.

    Supports: zip, rar, 7z (system 7za + BCJ2), tar.gz, tar.bz2, tar.xz, etc.
    Returns: { "stats": {...}, "files": [relative_paths] }
    """
    Path(dest_dir).mkdir(parents=True, exist_ok=True)
    t = _archive_type(archive_path)

    if t is None:
        raise ValueError(
            f"Unsupported archive format. (file: {Path(archive_path).name})"
        )

    # Build full password list: primary first, then extras
    all_passwords: List[Optional[str]] = []
    if password:
        all_passwords.append(password)
    if extra_passwords:
        all_passwords.extend(p for p in extra_passwords if p and p != password)
    if not all_passwords:
        all_passwords.append(None)  # try no-password

    if t == "zip":
        last_err = None
        for pw in all_passwords:
            try:
                with zipfile.ZipFile(archive_path) as z:
                    if pw:
                        z.setpassword(pw.encode("utf-8"))
                    z.extractall(dest_dir)
                break
            except (RuntimeError, zipfile.BadZipFile) as e:
                last_err = e
                shutil.rmtree(dest_dir, ignore_errors=True)
                Path(dest_dir).mkdir(parents=True, exist_ok=True)
        else:
            raise RuntimeError(f"ZIP extraction failed (wrong password?): {last_err}")

    elif t == "tar":
        with tarfile.open(archive_path, "r:*") as tf:
            tf.extractall(dest_dir)

    elif t == "7z":
        # PRIMARY: system 7za (handles BCJ2, AES-256, all filters)
        if not _try_extract_7z(archive_path, dest_dir, all_passwords):
            # FALLBACK: py7zr (works for basic LZMA2 without BCJ2)
            last_err = None
            for pw in all_passwords:
                try:
                    with py7zr.SevenZipFile(archive_path, mode="r", password=pw) as z:
                        z.extractall(dest_dir)
                    break
                except Exception as e:
                    last_err = e
                    shutil.rmtree(dest_dir, ignore_errors=True)
                    Path(dest_dir).mkdir(parents=True, exist_ok=True)
            else:
                raise RuntimeError(
                    f"7z extraction failed (BCJ2/wrong password/corrupt?): {last_err}"
                )

    elif t == "rar":
        last_err = None
        for pw in all_passwords:
            try:
                with rarfile.RarFile(archive_path) as rf:
                    if pw:
                        rf.setpassword(pw)
                    rf.extractall(dest_dir)
                break
            except Exception as e:
                last_err = e
                shutil.rmtree(dest_dir, ignore_errors=True)
                Path(dest_dir).mkdir(parents=True, exist_ok=True)
        else:
            raise RuntimeError(f"RAR extraction failed: {last_err}")

    else:
        raise ValueError("Unsupported archive format.")

    return _scan_stats(Path(dest_dir))

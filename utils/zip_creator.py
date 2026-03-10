# utils/zip_creator.py
import os
import zipfile
from pathlib import Path
from typing import List, Optional

import py7zr


def create_zip(file_paths: List[str], output_path: str) -> str:
    """Create a ZIP archive (no password) from a list of file paths."""
    with zipfile.ZipFile(output_path, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as zf:
        for fp in file_paths:
            if os.path.isfile(fp):
                zf.write(fp, os.path.basename(fp))
    return output_path


def create_7z(
    file_paths: List[str],
    output_path: str,
    password: Optional[str] = None,
) -> str:
    """Create a 7Z archive (with optional password) from a list of file paths."""
    kwargs = {"mode": "w"}
    if password:
        kwargs["password"] = password

    with py7zr.SevenZipFile(output_path, **kwargs) as zf:
        for fp in file_paths:
            if os.path.isfile(fp):
                zf.write(fp, os.path.basename(fp))
    return output_path


def create_archive(
    file_paths: List[str],
    output_dir: str,
    archive_name: str = "archive",
    password: Optional[str] = None,
) -> str:
    """
    Create archive from file_paths.
    If password is given → .7z with encryption.
    Otherwise → .zip (no password).
    """
    if password:
        out = os.path.join(output_dir, f"{archive_name}.7z")
        return create_7z(file_paths, out, password)
    else:
        out = os.path.join(output_dir, f"{archive_name}.zip")
        return create_zip(file_paths, out)

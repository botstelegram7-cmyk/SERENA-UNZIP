# utils/file_splitter.py
import os
import math
from pathlib import Path
from typing import List


async def split_file(file_path: str, part_size_mb: int = 1900) -> List[str]:
    """
    Splits any file into parts of given MB size.
    Returns sorted list of part file paths.
    """
    part_size = part_size_mb * 1024 * 1024
    total_size = os.path.getsize(file_path)
    if total_size <= part_size:
        return [file_path]

    num_parts = math.ceil(total_size / part_size)
    base_name = os.path.basename(file_path)
    out_dir   = os.path.dirname(file_path)
    parts: List[str] = []

    with open(file_path, "rb") as src:
        for i in range(num_parts):
            part_path = os.path.join(out_dir, f"{base_name}.part{i + 1:03d}")
            data = src.read(part_size)
            if not data:
                break
            with open(part_path, "wb") as dst:
                dst.write(data)
            parts.append(part_path)

    return parts


def human_size(size_bytes: int) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if size_bytes < 1024:
            return f"{size_bytes:.1f} {unit}"
        size_bytes /= 1024
    return f"{size_bytes:.1f} TB"

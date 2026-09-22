# utils/http_downloader.py
import os
import re
import time
from typing import Optional

import aiohttp
from pyrogram.types import Message

from utils.progress import progress_for_pyrogram


def _filename_from_cd(cd: str) -> Optional[str]:
    """
    Parse filename from Content-Disposition header.
    Supports: filename="..." and filename*=UTF-8''...
    """
    if not cd:
        return None

    # filename*=
    m = re.search(r"filename\*\s*=\s*[^']*'[^']*'(?P<fn>[^;]+)", cd, flags=re.I)
    if m:
        from urllib.parse import unquote

        return unquote(m.group("fn")).strip().strip('"')

    # filename=
    m = re.search(r'filename\s*=\s*"?(?P<fn>[^";]+)"?', cd, flags=re.I)
    if m:
        return m.group("fn").strip().strip('"')

    return None


async def download_file(
    url: str,
    dest_path: str,
    chunk_size: int = 64 * 1024,
    timeout: Optional[int] = None,
    status_message: Optional[Message] = None,
    file_name: Optional[str] = None,
    direction: str = "from web",
) -> str:
    """
    HTTP downloader with optional Telegram-style progress bar.

    Returns: final saved file path (with proper filename if server sends it).
    """
    dest_dir = os.path.dirname(dest_path) or "."
    os.makedirs(dest_dir, exist_ok=True)

    timeout_cfg = aiohttp.ClientTimeout(total=timeout)
    # Some hosts reject requests with no User-Agent outright. Sending a
    # browser-like one costs nothing and avoids an avoidable class of 403.
    _hdrs = {
        "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                       "AppleWebKit/537.36 (KHTML, like Gecko) "
                       "Chrome/125.0.0.0 Safari/537.36"),
        "Accept": "*/*",
    }
    async with aiohttp.ClientSession(timeout=timeout_cfg) as session:
        async with session.get(url, headers=_hdrs,
                               allow_redirects=True) as resp:
            resp.raise_for_status()
            total = int(resp.headers.get("Content-Length") or 0)
            cd = resp.headers.get("Content-Disposition", "")

            header_name = _filename_from_cd(cd)

            # Guess base filename
            if header_name:
                fname = header_name
            else:
                # from URL
                base_from_url = url.split("?", 1)[0].split("#", 1)[0].rsplit("/", 1)[-1]
                if base_from_url:
                    fname = base_from_url
                else:
                    fname = file_name or os.path.basename(dest_path) or "file"

            final_path = os.path.join(dest_dir, fname)

            downloaded = 0
            start = time.time()

            with open(final_path, "wb") as f:
                async for chunk in resp.content.iter_chunked(chunk_size):
                    if not chunk:
                        continue
                    f.write(chunk)
                    downloaded += len(chunk)

                    if status_message and total > 0:
                        await progress_for_pyrogram(
                            downloaded,
                            total,
                            status_message,
                            start,
                            fname,
                            direction,
                        )

            # final 100% update agar total > 0
            if status_message and total > 0 and downloaded == total:
                await progress_for_pyrogram(
                    downloaded,
                    total,
                    status_message,
                    start,
                    fname,
                    direction,
                )

    return final_path

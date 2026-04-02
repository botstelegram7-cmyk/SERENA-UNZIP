# utils/cleanup.py — Upgraded: aggressive cleanup every 30s + stale folder sweep
import asyncio
import os
import shutil
import time

from database import get_expired_temp_paths
from config import Config


async def cleanup_worker():
    """
    Background worker that:
    1. Deletes expired temp paths from DB every 30 seconds.
    2. Every 5 minutes sweeps the downloads dir for folders older than AUTO_DELETE_DEFAULT_MIN.
    """
    sweep_counter = 0

    while True:
        try:
            # 1. DB-registered expired paths
            expired = await get_expired_temp_paths()
            for path in expired:
                try:
                    if os.path.isdir(path):
                        shutil.rmtree(path, ignore_errors=True)
                    elif os.path.isfile(path):
                        os.remove(path)
                except Exception:
                    pass
        except Exception:
            pass

        # 2. Every 10 cycles (~5 min): sweep downloads dir for stale user folders
        sweep_counter += 1
        if sweep_counter >= 10:
            sweep_counter = 0
            try:
                _sweep_downloads_dir()
            except Exception:
                pass

        await asyncio.sleep(30)  # Run every 30 seconds (was 60)


def _sweep_downloads_dir():
    """Delete any user temp folder older than AUTO_DELETE_DEFAULT_MIN minutes."""
    base = Config.TEMP_DIR
    if not os.path.isdir(base):
        return
    cutoff = time.time() - (Config.AUTO_DELETE_DEFAULT_MIN * 60)
    for uid_dir in os.scandir(base):
        if not uid_dir.is_dir():
            continue
        for task_dir in os.scandir(uid_dir.path):
            if not task_dir.is_dir():
                continue
            try:
                if task_dir.stat().st_mtime < cutoff:
                    shutil.rmtree(task_dir.path, ignore_errors=True)
            except Exception:
                pass
        # If the uid folder is now empty, remove it too
        try:
            if not any(True for _ in os.scandir(uid_dir.path)):
                os.rmdir(uid_dir.path)
        except Exception:
            pass

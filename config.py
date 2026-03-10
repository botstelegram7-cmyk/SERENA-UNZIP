# config.py
import os
from dotenv import load_dotenv

load_dotenv()


class Config:
    # ── Telegram API ────────────────────────────────────────────
    API_ID   = int(os.getenv("API_ID",   "123456"))
    API_HASH = os.getenv("API_HASH", "change_me")
    BOT_TOKEN = os.getenv("BOT_TOKEN", "123:ABC")

    # ── MongoDB ─────────────────────────────────────────────────
    MONGO_URI = os.getenv("MONGO_URI", "mongodb://localhost:27017/serena_unzip")
    DB_NAME   = os.getenv("DB_NAME", "serena_unzip")

    # ── Hard-coded IDs ──────────────────────────────────────────
    LOG_CHANNEL_ID    = int(os.getenv("LOG_CHANNEL_ID", "-1003286415377"))
    FORCE_SUB_CHANNEL = os.getenv("FORCE_SUB_CHANNEL", "serenaunzipbot")
    OWNER_IDS         = {6518065496, 1598576202}
    OWNER_USERNAME    = os.getenv("OWNER_USERNAME", "technicalserena")

    # ── General ─────────────────────────────────────────────────
    BOT_NAME  = "Serena Unzip"
    START_PIC = os.getenv("START_PIC", None)   # file_id or http url
    TEMP_DIR  = os.getenv("TEMP_DIR", "downloads")

    # ── Progress bar ────────────────────────────────────────────
    PROGRESS_UPDATE_INTERVAL = int(os.getenv("PROGRESS_UPDATE_INTERVAL", "5"))

    # ── Cleanup / daily limits ───────────────────────────────────
    AUTO_DELETE_DEFAULT_MIN  = int(os.getenv("AUTO_DELETE_DEFAULT_MIN", "30"))
    FREE_DAILY_TASK_LIMIT    = int(os.getenv("FREE_DAILY_TASK_LIMIT",   "30"))
    FREE_DAILY_SIZE_MB       = int(os.getenv("FREE_DAILY_SIZE_MB",      "4096"))
    FREE_MIN_WAIT_SEC        = int(os.getenv("FREE_MIN_WAIT_SEC",       "300"))   # 5 min
    PREMIUM_MIN_WAIT_SEC     = int(os.getenv("PREMIUM_MIN_WAIT_SEC",    "10"))

    # ── File size caps (MB) ──────────────────────────────────────
    MAX_ARCHIVE_SIZE_FREE_MB    = int(os.getenv("MAX_ARCHIVE_SIZE_FREE_MB",    "2048"))
    MAX_ARCHIVE_SIZE_PREMIUM_MB = int(os.getenv("MAX_ARCHIVE_SIZE_PREMIUM_MB", "10240"))

    # ── yt-dlp / Instagram ───────────────────────────────────────
    # Paste your full Netscape-format cookie file content in this env var
    INSTAGRAM_COOKIES  = os.getenv("INSTAGRAM_COOKIES", "")
    YTDL_MAX_SIZE_MB   = int(os.getenv("YTDL_MAX_SIZE_MB", "2000"))
    COOKIE_FILE_PATH   = "/tmp/yt_cookies.txt"

    # ── Cloud upload ─────────────────────────────────────────────
    GOFILE_API_KEY = os.getenv("GOFILE_API_KEY", "")    # optional GoFile token

    # ── Auto-split threshold (Telegram 2 GB limit) ───────────────
    AUTO_SPLIT_MB = int(os.getenv("AUTO_SPLIT_MB", "1900"))

    # ── Queue ────────────────────────────────────────────────────
    MAX_QUEUE_PER_USER = int(os.getenv("MAX_QUEUE_PER_USER", "5"))

    # ── Auto-password ────────────────────────────────────────────
    ENABLE_AUTO_PASSWORD = os.getenv("ENABLE_AUTO_PASSWORD", "1") == "1"

    # ── Referral reward (days of premium) ───────────────────────
    REFERRAL_REQUIRED = int(os.getenv("REFERRAL_REQUIRED", "5"))
    REFERRAL_REWARD_DAYS = int(os.getenv("REFERRAL_REWARD_DAYS", "7"))

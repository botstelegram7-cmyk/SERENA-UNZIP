<div align="center">

```
╔══════════════════════════════════════════════════════════════╗
║                                                              ║
║    ███████╗███████╗██████╗ ███████╗███╗   ██╗ █████╗        ║
║    ██╔════╝██╔════╝██╔══██╗██╔════╝████╗  ██║██╔══██╗       ║
║    ███████╗█████╗  ██████╔╝█████╗  ██╔██╗ ██║███████║       ║
║    ╚════██║██╔══╝  ██╔══██╗██╔══╝  ██║╚██╗██║██╔══██║       ║
║    ███████║███████╗██║  ██║███████╗██║ ╚████║██║  ██║       ║
║    ╚══════╝╚══════╝╚═╝  ╚═╝╚══════╝╚═╝  ╚═══╝╚═╝  ╚═╝       ║
║                                                              ║
║              🌸  SERENA UNZIP BOT v2  🌸                     ║
║         Advanced Telegram File Processing Bot                ║
╚══════════════════════════════════════════════════════════════╝
```

[![Python](https://img.shields.io/badge/Python-3.10%2B-blue?style=for-the-badge&logo=python)](https://python.org)
[![Pyrogram](https://img.shields.io/badge/Pyrogram-2.x-green?style=for-the-badge)](https://pyrogram.org)
[![MongoDB](https://img.shields.io/badge/MongoDB-Motor-brightgreen?style=for-the-badge&logo=mongodb)](https://mongodb.com)
[![License](https://img.shields.io/badge/License-MIT-yellow?style=for-the-badge)](LICENSE)
[![Telegram](https://img.shields.io/badge/Bot-@SerenaBotHere-blue?style=for-the-badge&logo=telegram)](https://t.me)

> **All-in-one Telegram bot for extracting archives, downloading media, compressing videos, and much more.**

---

</div>

## ✨ Features

| Category | Features |
|----------|----------|
| 📦 **Archives** | ZIP, RAR, 7z, TAR extract · Password auto-detect · Nested archives · Preview before extract |
| 🎬 **Video** | Compress (CRF control) · Resize · Watermark · Merge · Split · Screenshot · Subtitle extract |
| 📥 **Downloader** | Instagram Photos/Reels/Stories/Highlights · YouTube · Twitter · TikTok · Facebook · M3U8 |
| ☁️ **Upload** | Telegram · GoFile cloud · Auto thumbnail · Custom caption |
| 📦 **ZIP Queue** | Batch extract up to 100 ZIPs · Sequence preserved · Progress ETA · Auto cache delete |
| 🔒 **Security** | Per-user rate limit · Daily task/size limit · Premium system · Ban/unban users |
| 🛠️ **Utilities** | Rename · PDF tools · Audio extract · File info · Auto watermark · Google Drive |

---

## 🚀 Quick Deploy

### 🟣 Render (Recommended — Free Tier Available)

1. Fork this repo on GitHub
2. Go to [render.com](https://render.com) → **New Web Service**
3. Connect your GitHub repo
4. Set **Build Command**: `pip install -r requirements.txt`
5. Set **Start Command**: `uvicorn server:fastapi_app --host 0.0.0.0 --port $PORT`
6. Add all [Environment Variables](#-environment-variables) in Render dashboard
7. Click **Deploy** ✅

---

### 🔵 Railway

```bash
# 1. Install Railway CLI
npm install -g @railway/cli

# 2. Login
railway login

# 3. Init project
railway init

# 4. Deploy
railway up
```

Add env variables in Railway dashboard → **Variables** tab.

---

### 🟠 Fly.io

```bash
# Install flyctl
curl -L https://fly.io/install.sh | sh

# Login
fly auth login

# Launch (first time)
fly launch

# Set secrets
fly secrets set API_ID=your_id API_HASH=your_hash BOT_TOKEN=your_token MONGO_URI=your_mongo

# Deploy
fly deploy
```

---

### 🐳 Docker (Any VPS)

```bash
# 1. Clone repo
git clone https://github.com/youruser/serena-unzip-bot
cd serena-unzip-bot

# 2. Copy env file
cp .env.example .env
nano .env   # fill in your values

# 3. Build & run
docker build -t serena-bot .
docker run -d --name serena --env-file .env -p 8000:8000 serena-bot

# View logs
docker logs -f serena
```

---

### 🖥️ Ubuntu / Debian VPS (Manual)

```bash
# 1. Update system
sudo apt update && sudo apt upgrade -y

# 2. Install dependencies
sudo apt install -y python3 python3-pip python3-venv ffmpeg p7zip-full unrar git

# 3. Clone repo
git clone https://github.com/youruser/serena-unzip-bot
cd serena-unzip-bot

# 4. Create virtual environment
python3 -m venv .venv
source .venv/bin/activate

# 5. Install Python packages
pip install -r requirements.txt

# 6. Setup environment
cp .env.example .env
nano .env   # fill in your values

# 7. Run with screen (stays alive after SSH disconnect)
screen -S serena
python -m uvicorn server:fastapi_app --host 0.0.0.0 --port 8000
# Press Ctrl+A then D to detach

# Reconnect later:
screen -r serena
```

**Auto-restart with systemd:**
```bash
sudo nano /etc/systemd/system/serena.service
```
```ini
[Unit]
Description=Serena Unzip Bot
After=network.target

[Service]
User=ubuntu
WorkingDirectory=/home/ubuntu/serena-unzip-bot
Environment="PATH=/home/ubuntu/serena-unzip-bot/.venv/bin"
ExecStart=/home/ubuntu/serena-unzip-bot/.venv/bin/uvicorn server:fastapi_app --host 0.0.0.0 --port 8000
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
```
```bash
sudo systemctl daemon-reload
sudo systemctl enable serena
sudo systemctl start serena
sudo systemctl status serena  # check status
```

---

### 💻 Local PC (Windows / Mac / Linux)

```bash
# 1. Install Python 3.10+ from python.org
# 2. Install FFmpeg:
#    Windows: https://ffmpeg.org/download.html (add to PATH)
#    Mac:     brew install ffmpeg
#    Linux:   sudo apt install ffmpeg

# 3. Clone
git clone https://github.com/youruser/serena-unzip-bot
cd serena-unzip-bot

# 4. Install deps
pip install -r requirements.txt

# 5. Setup .env
cp .env.example .env
# Edit .env with your values

# 6. Run
uvicorn server:fastapi_app --host 0.0.0.0 --port 8000
```

---

## 🔑 Environment Variables

| Variable | Required | Description |
|----------|----------|-------------|
| `API_ID` | ✅ | Telegram API ID from [my.telegram.org](https://my.telegram.org) |
| `API_HASH` | ✅ | Telegram API Hash from [my.telegram.org](https://my.telegram.org) |
| `BOT_TOKEN` | ✅ | Bot token from [@BotFather](https://t.me/BotFather) |
| `MONGO_URI` | ✅ | MongoDB connection string (free at [mongodb.com](https://mongodb.com)) |
| `OWNER_ID` | ✅ | Your Telegram user ID |
| `LOG_CHANNEL` | ✅ | Channel ID for bot logs (e.g. `-100xxxxxxxxx`) |
| `FORCE_SUB_CHANNEL` | ⬜ | Channel username users must join |
| `INSTAGRAM_COOKIES` | ⬜ | Netscape format cookies for private Instagram content |
| `QUEUE_END_GIF` | ⬜ | Giphy MP4 URL or Telegram sticker file_id — sent after each ZIP extract |
| `TEMP_DIR` | ⬜ | Temp folder path (default: `./downloads`) |
| `MAX_FILE_SIZE_MB` | ⬜ | Max file size to process (default: `2000`) |
| `FREE_DAILY_TASK_LIMIT` | ⬜ | Daily task limit for free users (default: `30`) |
| `AUTO_DELETE_MINUTES` | ⬜ | Delete temp files after N minutes (default: `30`) |

---

## 🍪 Instagram Cookies Setup

For downloading **Stories, Highlights, and private content**:

1. Install [Cookie Editor](https://chrome.google.com/webstore/detail/cookie-editor/hlkenndednhfkekhgcdicdfddnkalmdm) browser extension
2. Open Instagram and log in
3. Click Cookie Editor → **Export → Netscape**
4. Copy the exported text
5. In Render/Railway → Environment Variables → `INSTAGRAM_COOKIES` → paste the text

---

## 📋 Bot Commands

```
/start       — Welcome message
/help        — Full command list
/unzip       — Extract archive (reply to file)
/zipqueue    — Start ZIP batch queue (up to 100 files)
/zq          — Short alias for /zipqueue
/zqpass      — Set queue password
/cancelqueue — Cancel active queue
/ytdl        — Download from Instagram/YouTube/Twitter etc.
/compress    — Compress video (reply to video)
/resize      — Resize video
/merge       — Merge multiple videos
/split       — Split video by time
/audio       — Extract audio from video
/watermark   — Add text watermark
/screenshot  — Capture frame at timestamp
/subs        — Extract subtitles
/rename      — Rename file
/info        — Show file metadata
/pdf         — PDF tools
/zip         — Create ZIP from files
/mystats     — Your usage statistics
/cancel      — Cancel current task
```

---

## 🗂️ Project Structure

```
serena-unzip-bot/
├── bot.py              # Main bot — all handlers
├── server.py           # FastAPI health check server
├── config.py           # Environment config
├── database.py         # MongoDB operations
├── requirements.txt    # Python dependencies
├── Dockerfile          # Docker deployment
├── .env.example        # Environment template
└── utils/
    ├── media_tools.py  # FFmpeg video processing
    ├── ytdl_tools.py   # yt-dlp download wrapper
    ├── extractors.py   # Archive extraction
    ├── progress.py     # Upload/download progress
    ├── cloud_upload.py # GoFile upload
    ├── cleanup.py      # Temp file management
    ├── zip_creator.py  # ZIP creation
    ├── pdf_tools.py    # PDF operations
    ├── m3u8_tools.py   # M3U8/HLS download
    ├── http_downloader.py
    ├── link_parser.py
    ├── file_splitter.py
    ├── gdrive.py
    └── password_list.py
```

---

## ⚡ Suggested Improvements

- **`/preview`** — List files inside ZIP before extracting
- **`/convert`** — Convert between archive formats (ZIP ↔ TAR ↔ 7z)
- **Batch Instagram** — Download last N posts from a profile
- **Auto-thumbnail** — Generate video preview grid
- **Webhook mode** — Faster response via webhook instead of polling
- **Scheduled tasks** — Queue tasks for later
- **File deduplication** — Skip duplicate files in ZIP
- **Password manager** — Save frequently used passwords per user

---

## 🛠️ Built With

- **[Pyrogram](https://pyrogram.org)** — Telegram MTProto client
- **[FFmpeg](https://ffmpeg.org)** — Video/audio processing
- **[yt-dlp](https://github.com/yt-dlp/yt-dlp)** — Media downloader
- **[Motor](https://motor.readthedocs.io)** — Async MongoDB driver
- **[FastAPI](https://fastapi.tiangolo.com)** — Health check server
- **[py7zr](https://py7zr.readthedocs.io)** — 7-zip support

---

## 👨‍💻 Credits

<div align="center">

| Role | Contact |
|------|---------|
| 💻 **Developer & Owner** | [![Telegram](https://img.shields.io/badge/Telegram-@TechnicalSerena-blue?logo=telegram)](https://t.me/TechnicalSerena) |
| 🎨 **Co-Developer** | [![Telegram](https://img.shields.io/badge/Telegram-@Xioqui__Xin-blue?logo=telegram)](https://t.me/Xioqui_Xin) |
| 📸 **Instagram** | [![Instagram](https://img.shields.io/badge/Instagram-@Prince572002-E4405F?logo=instagram)](https://instagram.com/Prince572002) |

</div>

---

## 📄 License

```
MIT License — Free to use, modify and distribute with attribution.
```

<div align="center">

**Made with ❤️ by [@TechnicalSerena](https://t.me/TechnicalSerena)**

*Star ⭐ this repo if it helped you!*

</div>

# 🤖 Serena Unzip Bot v2

## Features
- 📦 Unzip 20+ archive formats (ZIP/RAR/7Z/TAR + auto-password)
- 🎬 Video: Compress, Split, Merge, Watermark, Extract Audio/Subs/Screenshot
- ⬇️ Instagram, Twitter, Facebook, TikTok, Vimeo, + 1000 sites (yt-dlp)
- 🗜 ZIP Creator with optional password encryption
- 📄 PDF Tools: Split, Merge, Extract Text
- ✂️ File Splitter (for 2GB+ files)
- ☁️ Cloud Upload (GoFile/Catbox)
- ✏️ File Rename, File Info
- ⭐ Premium System + Referral Program
- 🛡 Full Admin Panel

## Deploy on Render

1. Push code to GitHub
2. Create new **Web Service** on Render
3. Set **Start Command**: `uvicorn server:fastapi_app --host 0.0.0.0 --port $PORT`
4. Set all environment variables from `.env.example`
5. Install command: `pip install -r requirements.txt`

## Groups Support
Reply to any file + command:
- `/unzip` `/audio` `/compress` `/split` `/subs`
- `/screenshot HH:MM:SS` `/info` `/rename` `/pdf` `/watermark text`

## Instagram Cookies (Render)
1. Export cookies from browser as **Netscape format**
2. Paste entire content in `INSTAGRAM_COOKIES` env variable on Render

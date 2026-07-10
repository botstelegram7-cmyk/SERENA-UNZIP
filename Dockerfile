# ╔══════════════════════════════════════════════════════════════════╗
# ║          SERENA UNZIP BOT — Production Dockerfile              ║
# ║  Includes: FFmpeg (for video/audio merge), 7zip, unrar, etc.  ║
# ╚══════════════════════════════════════════════════════════════════╝

FROM python:3.11-slim-bookworm

# ── System packages ──
RUN apt-get update && apt-get install -y --no-install-recommends \
        ffmpeg \
        p7zip-full \
        unrar-free \
        wget \
        curl \
        ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# ── Working directory ──
WORKDIR /app

# ── Install Python deps first (layer cache) ──
COPY requirements.txt .
RUN pip install --no-cache-dir --upgrade pip \
 && pip install --no-cache-dir -r requirements.txt

# ── Copy source ──
COPY . .

# ── Create temp/downloads directory ──
RUN mkdir -p /app/downloads /app/temp

# ── Port for health check (Render / Railway / Fly.io) ──
EXPOSE 8000

# ── Start bot + web server ──
CMD ["python", "-m", "uvicorn", "server:fastapi_app", \
     "--host", "0.0.0.0", "--port", "8000", "--workers", "1"]

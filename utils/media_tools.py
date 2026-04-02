# utils/media_tools.py — v4: FFmpeg real-time progress via stderr parsing
import asyncio
import json
import os
import re
import time
from typing import Callable, List, Optional


class FFmpegError(RuntimeError):
    pass


# ── Low-level runner (no progress) ──────────────────────────────────────────
async def run_ffmpeg(cmd: list, timeout: int = 1800):
    """Run ffmpeg, wait for completion. Raises FFmpegError on failure."""
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        out, err = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        try:
            proc.kill()
        except Exception:
            pass
        raise FFmpegError("FFmpeg timed out.")
    if proc.returncode != 0:
        raise FFmpegError(err.decode(errors="ignore"))


# ── Progress-aware runner ────────────────────────────────────────────────────
_TS_RE    = re.compile(r"time=(\d+):(\d+):(\d+)\.(\d+)")
_DUR_RE   = re.compile(r"Duration:\s*(\d+):(\d+):(\d+)\.(\d+)")
_SPEED_RE = re.compile(r"speed=\s*([\d.]+)x")
_SIZE_RE  = re.compile(r"size=\s*(\d+)kB")


def _hms_to_sec(h, m, s, cs) -> float:
    return int(h) * 3600 + int(m) * 60 + int(s) + int(cs) / 100


def _fmt_eta(sec: float) -> str:
    sec = max(0, int(sec))
    m, s = divmod(sec, 60)
    h, m = divmod(m, 60)
    if h:
        return f"{h}h {m}m {s}s"
    if m:
        return f"{m}m {s}s"
    return f"{s}s"


async def run_ffmpeg_with_progress(
    cmd: list,
    on_progress: Callable,   # async callable(percent, eta_str, speed_str, size_str)
    update_interval: float = 5.0,
    timeout: int = 1800,
):
    """
    Run ffmpeg and periodically call on_progress with live stats.
    FFmpeg writes progress to stderr — we parse it line-by-line.
    """
    # -progress pipe:1 gives machine-readable output on stdout
    # but simplest reliable approach: parse stderr lines
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.PIPE,
    )

    total_sec: float = 0.0
    last_update: float = 0.0
    stderr_buf: list = []
    start_time = time.time()

    async def read_stderr():
        nonlocal total_sec
        while True:
            try:
                line_b = await asyncio.wait_for(
                    proc.stderr.readline(), timeout=timeout
                )
            except asyncio.TimeoutError:
                try:
                    proc.kill()
                except Exception:
                    pass
                raise FFmpegError("FFmpeg timed out.")
            if not line_b:
                break
            line = line_b.decode(errors="ignore")
            stderr_buf.append(line)

            # Parse total duration from first pass
            if total_sec == 0:
                dm = _DUR_RE.search(line)
                if dm:
                    total_sec = _hms_to_sec(*dm.groups())

    async def send_updates():
        nonlocal last_update
        while proc.returncode is None:
            await asyncio.sleep(0.5)
            now = time.time()
            if now - last_update < update_interval:
                continue
            # Search recent stderr for progress
            recent = "".join(stderr_buf[-30:])
            tm = _TS_RE.findall(recent)
            if not tm:
                continue
            h, m, s, cs = tm[-1]
            cur_sec = _hms_to_sec(h, m, s, cs)
            pct = min((cur_sec / total_sec * 100) if total_sec > 0 else 0, 99.9)

            speed_m = _SPEED_RE.search(recent)
            speed_x = float(speed_m.group(1)) if speed_m else 0.0
            speed_str = f"{speed_x:.1f}x" if speed_x else "…"

            size_m = _SIZE_RE.findall(recent)
            size_kb = int(size_m[-1]) if size_m else 0
            size_str = f"{size_kb/1024:.1f} MB" if size_kb >= 1024 else f"{size_kb} KB"

            if total_sec > 0 and speed_x > 0:
                remaining_sec = (total_sec - cur_sec) / speed_x
                eta_str = _fmt_eta(remaining_sec)
            else:
                elapsed = now - start_time
                eta_str = "…"

            try:
                await on_progress(pct, eta_str, speed_str, size_str)
            except Exception:
                pass
            last_update = now

    try:
        await asyncio.wait_for(
            asyncio.gather(read_stderr(), send_updates()),
            timeout=timeout + 10,
        )
    except asyncio.TimeoutError:
        try:
            proc.kill()
        except Exception:
            pass
        raise FFmpegError("FFmpeg timed out.")
    finally:
        try:
            await proc.wait()
        except Exception:
            pass

    if proc.returncode != 0:
        err = "".join(stderr_buf[-30:])
        raise FFmpegError(err)


# ── Audio ────────────────────────────────────────────────────────────────────
async def extract_audio(video_path: str, output_path: str):
    await run_ffmpeg([
        "ffmpeg", "-y", "-i", video_path,
        "-vn", "-acodec", "copy", output_path,
    ])


# ── Merge ────────────────────────────────────────────────────────────────────
async def merge_videos(video_paths: List[str], output_path: str):
    list_file = output_path + ".txt"
    with open(list_file, "w", encoding="utf-8") as f:
        for p in video_paths:
            f.write(f"file '{os.path.abspath(p)}'\n")
    await run_ffmpeg([
        "ffmpeg", "-y", "-f", "concat", "-safe", "0",
        "-i", list_file, "-c", "copy", output_path,
    ])
    try:
        os.remove(list_file)
    except OSError:
        pass


# ── Split ────────────────────────────────────────────────────────────────────
async def split_video(video_path: str, start: str, duration: str, output_path: str):
    await run_ffmpeg([
        "ffmpeg", "-y", "-i", video_path,
        "-ss", start, "-t", duration, "-c", "copy", output_path,
    ])


# ── Compress (with live progress) ────────────────────────────────────────────
async def compress_video(
    input_path: str,
    output_path: str,
    resolution=None,
    crf: int = 28,
    on_progress: Optional[Callable] = None,
    update_interval: float = 5.0,
):
    """
    Re-encode video with libx264.
    resolution: '360','480','720','1080' or None/'orig' for smart compress.
    on_progress: async callable(percent, eta_str, speed_str, size_str) or None.
    """
    if resolution and str(resolution) not in ("orig", "None", "none", ""):
        vf_args = ["-vf", f"scale=-2:{resolution}"]
    else:
        vf_args = []

    cmd = [
        "ffmpeg", "-y", "-i", input_path,
        *vf_args,
        "-c:v",    "libx264",
        "-crf",    str(crf),
        "-preset", "fast",
        "-c:a",    "aac",
        "-b:a",    "128k",
        output_path,
    ]

    if on_progress:
        await run_ffmpeg_with_progress(
            cmd, on_progress=on_progress, update_interval=update_interval
        )
    else:
        await run_ffmpeg(cmd)


# ── Watermark ────────────────────────────────────────────────────────────────
async def add_watermark(
    input_path: str,
    output_path: str,
    text: str,
    position: str = "bottomright",
    opacity: float = 0.6,
):
    pos_map = {
        "topleft":     "x=10:y=10",
        "topright":    "x=w-tw-10:y=10",
        "bottomleft":  "x=10:y=h-th-10",
        "bottomright": "x=w-tw-10:y=h-th-10",
        "center":      "x=(w-tw)/2:y=(h-th)/2",
    }
    pos = pos_map.get(position, pos_map["bottomright"])
    safe_text = text.replace("'", "\\'").replace(":", "\\:")
    vf = (
        f"drawtext=text='{safe_text}'"
        f":fontsize=28"
        f":fontcolor=white@{opacity}"
        f":{pos}"
        f":box=1:boxcolor=black@0.3:boxborderw=4"
    )
    await run_ffmpeg([
        "ffmpeg", "-y", "-i", input_path,
        "-vf", vf, "-c:a", "copy", output_path,
    ])


# ── Thumbnail ────────────────────────────────────────────────────────────────
async def generate_thumbnail(
    video_path: str,
    thumb_path: str,
    time_pos: str = "00:00:02",
):
    await run_ffmpeg([
        "ffmpeg", "-y",
        "-ss", time_pos,
        "-i", video_path,
        "-vframes", "1",
        "-q:v", "2",
        thumb_path,
    ])
    return thumb_path


# ── Screenshot ───────────────────────────────────────────────────────────────
async def take_screenshot(
    video_path: str,
    output_path: str,
    time_str: str,
):
    parts = time_str.strip().split(":")
    if len(parts) == 2:
        time_str = f"00:{parts[0].zfill(2)}:{parts[1].zfill(2)}"
    elif len(parts) == 3:
        time_str = ":".join(p.zfill(2) for p in parts)
    await generate_thumbnail(video_path, output_path, time_pos=time_str)


# ── Subtitles ────────────────────────────────────────────────────────────────
async def extract_subtitles(video_path: str, output_dir: str) -> List[dict]:
    os.makedirs(output_dir, exist_ok=True)
    proc = await asyncio.create_subprocess_exec(
        "ffprobe", "-v", "quiet", "-print_format", "json", "-show_streams", video_path,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    out, _ = await proc.communicate()
    try:
        streams_data = json.loads(out.decode())
        streams = streams_data.get("streams", [])
    except Exception:
        return []

    sub_streams = [s for s in streams if s.get("codec_type") == "subtitle"]
    extracted = []
    for i, s in enumerate(sub_streams):
        lang     = s.get("tags", {}).get("language", "und")
        codec    = s.get("codec_name", "unknown")
        ext      = "ass" if codec in ("ass", "ssa") else "srt"
        out_file = os.path.join(output_dir, f"sub_{i}_{lang}.{ext}")
        try:
            await run_ffmpeg([
                "ffmpeg", "-y", "-i", video_path,
                "-map", f"0:s:{i}",
                out_file,
            ])
            extracted.append({
                "stream_index": i,
                "language":     lang,
                "codec":        codec,
                "ext":          ext,
                "output_path":  out_file,
                "label":        f"{lang.upper()} ({ext.upper()})",
            })
        except FFmpegError:
            pass
    return extracted


# ── Media Info ───────────────────────────────────────────────────────────────
async def get_media_info(file_path: str) -> Optional[dict]:
    proc = await asyncio.create_subprocess_exec(
        "ffprobe",
        "-v", "quiet",
        "-print_format", "json",
        "-show_format",
        "-show_streams",
        file_path,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    out, _ = await proc.communicate()
    try:
        data = json.loads(out.decode())
    except Exception:
        return None

    fmt      = data.get("format", {})
    streams  = data.get("streams", [])
    video_s  = next((s for s in streams if s.get("codec_type") == "video"), None)
    audio_s  = next((s for s in streams if s.get("codec_type") == "audio"), None)
    sub_cnt  = sum(1 for s in streams if s.get("codec_type") == "subtitle")
    duration = float(fmt.get("duration") or 0)

    info = {
        "size_bytes": int(fmt.get("size", 0)),
        "duration":   duration,
        "format":     fmt.get("format_long_name", "Unknown"),
    }

    if video_s:
        w = video_s.get("width", 0)
        h = video_s.get("height", 0)
        info["resolution"]    = f"{w}×{h}" if w and h else "N/A"
        info["video_codec"]   = video_s.get("codec_name", "Unknown")
        info["video_bitrate"] = video_s.get("bit_rate", "N/A")
        afr = video_s.get("avg_frame_rate", "0/0")
        try:
            num, den = afr.split("/")
            info["fps"] = round(int(num) / int(den), 2) if int(den) else 0
        except Exception:
            info["fps"] = 0

    if audio_s:
        lang = audio_s.get("tags", {}).get("language", "")
        info["audio_codec"]    = audio_s.get("codec_name", "Unknown")
        info["audio_channels"] = audio_s.get("channels", 0)
        info["audio_bitrate"]  = audio_s.get("bit_rate", "N/A")
        info["audio_lang"]     = lang

    info["subtitle_count"] = sub_cnt
    return info

# utils/media_tools.py — Upgraded: safe fps parsing, flexible compression
import asyncio
import json
import os
from typing import List, Optional


class FFmpegError(RuntimeError):
    pass


async def run_ffmpeg(cmd: list, timeout: int = 1800):
    """Run ffmpeg with a configurable timeout (default 30 min)."""
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        out, err = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        try: proc.kill()
        except: pass
        raise FFmpegError("FFmpeg timed out.")
    if proc.returncode != 0:
        raise FFmpegError(err.decode(errors="ignore"))


async def extract_audio(video_path: str, output_path: str):
    """Extract audio stream (copy codec — fast, lossless)."""
    await run_ffmpeg([
        "ffmpeg", "-y", "-i", video_path,
        "-vn", "-acodec", "copy", output_path,
    ])


async def merge_videos(video_paths: List[str], output_path: str):
    """Concatenate videos using ffmpeg concat demuxer."""
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


async def split_video(video_path: str, start: str, duration: str, output_path: str):
    """Cut a portion of a video (stream copy — no re-encode)."""
    await run_ffmpeg([
        "ffmpeg", "-y", "-i", video_path,
        "-ss", start, "-t", duration, "-c", "copy", output_path,
    ])


async def compress_video(
    input_path: str,
    output_path: str,
    resolution: Optional[str] = None,
    crf: int = 28,
    preset: str = "fast",
    audio_bitrate: str = "128k",
):
    """
    Re-encode video with libx264.

    Args:
        resolution: Target height ('360','480','720','1080') or None to keep original.
        crf: Quality (18=best, 28=default, 35=small file). Lower = better quality.
        preset: FFmpeg preset ('ultrafast','fast','medium','slow').
        audio_bitrate: Audio bitrate e.g. '128k'.

    No size limits — works on any file size.
    """
    vf_args = []
    if resolution and str(resolution).isdigit():
        vf_args = ["-vf", f"scale=-2:{resolution}"]

    cmd = ["ffmpeg", "-y", "-i", input_path]
    if vf_args:
        cmd += vf_args
    cmd += [
        "-c:v",   "libx264",
        "-crf",   str(crf),
        "-preset", preset,
        "-c:a",   "aac",
        "-b:a",   audio_bitrate,
        output_path,
    ]
    await run_ffmpeg(cmd)


async def add_watermark(
    input_path: str,
    output_path: str,
    text: str,
    position: str = "bottomright",
    opacity: float = 0.6,
):
    """Overlay text watermark on a video."""
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


async def generate_thumbnail(
    video_path: str,
    thumb_path: str,
    time_pos: str = "00:00:02",
):
    """Extract a single frame as JPEG thumbnail."""
    await run_ffmpeg([
        "ffmpeg", "-y",
        "-ss", time_pos,
        "-i", video_path,
        "-vframes", "1",
        "-q:v", "2",
        thumb_path,
    ])
    return thumb_path


async def take_screenshot(
    video_path: str,
    output_path: str,
    time_str: str,
):
    """Take a screenshot at exact time. time_str = '01:23:45' or '83:25'."""
    parts = time_str.strip().split(":")
    if len(parts) == 2:
        time_str = f"00:{parts[0].zfill(2)}:{parts[1].zfill(2)}"
    elif len(parts) == 3:
        time_str = ":".join(p.zfill(2) for p in parts)
    await generate_thumbnail(video_path, output_path, time_pos=time_str)


async def extract_subtitles(video_path: str, output_dir: str) -> List[dict]:
    """
    Extract all subtitle streams from an MKV/MP4.
    Returns list of dicts: {stream_index, language, codec, output_path, label}
    """
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
        lang    = s.get("tags", {}).get("language", "und")
        codec   = s.get("codec_name", "unknown")
        ext     = "ass" if codec in ("ass", "ssa") else "srt"
        out_file = os.path.join(output_dir, f"sub_{i}_{lang}.{ext}")

        try:
            await run_ffmpeg([
                "ffmpeg", "-y", "-i", video_path,
                "-map", f"0:s:{i}",
                out_file,
            ])
            extracted.append({
                "stream_index": i,
                "language": lang,
                "codec": codec,
                "ext": ext,
                "output_path": out_file,
                "label": f"{lang.upper()} ({ext.upper()})",
            })
        except FFmpegError:
            pass

    return extracted


def _safe_fps(avg_frame_rate: str) -> float:
    """Parse avg_frame_rate fraction safely — no eval()."""
    try:
        parts = avg_frame_rate.split("/")
        if len(parts) == 2:
            num, den = int(parts[0]), int(parts[1])
            return num / den if den else 0.0
        return float(avg_frame_rate)
    except Exception:
        return 0.0


async def get_media_info(file_path: str) -> Optional[dict]:
    """
    Return media metadata via ffprobe.
    Returns dict with duration, resolution, video_codec, audio_codec, etc.
    """
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

    fmt     = data.get("format", {})
    streams = data.get("streams", [])

    video_s = next((s for s in streams if s.get("codec_type") == "video"), None)
    audio_s = next((s for s in streams if s.get("codec_type") == "audio"), None)
    sub_cnt = sum(1 for s in streams if s.get("codec_type") == "subtitle")

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
        # FIXED: safe fps parsing (no eval)
        info["fps"] = _safe_fps(video_s.get("avg_frame_rate", "0/1"))

    if audio_s:
        lang = audio_s.get("tags", {}).get("language", "")
        info["audio_codec"]    = audio_s.get("codec_name", "Unknown")
        info["audio_channels"] = audio_s.get("channels", 0)
        info["audio_bitrate"]  = audio_s.get("bit_rate", "N/A")
        info["audio_lang"]     = lang

    info["subtitle_count"] = sub_cnt
    return info

# utils/media_tools.py
import asyncio
import json
import os
from typing import List, Optional


class FFmpegError(RuntimeError):
    pass


async def run_ffmpeg(cmd: list):
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    out, err = await proc.communicate()
    if proc.returncode != 0:
        raise FFmpegError(err.decode(errors="ignore"))


async def extract_audio(video_path: str, output_path: str):
    """Extract audio stream (copy codec → fast, lossless)."""
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
    """Cut a portion of a video (stream copy – no re-encode)."""
    await run_ffmpeg([
        "ffmpeg", "-y", "-i", video_path,
        "-ss", start, "-t", duration, "-c", "copy", output_path,
    ])


async def compress_video(
    input_path: str,
    output_path: str,
    resolution: str = "720",
    crf: int = 28,
):
    """
    Re-encode video to target resolution with libx264.
    resolution: '360','480','720','1080' (height).
    """
    await run_ffmpeg([
        "ffmpeg", "-y", "-i", input_path,
        "-vf",    f"scale=-2:{resolution}",
        "-c:v",   "libx264",
        "-crf",   str(crf),
        "-preset","fast",
        "-c:a",   "aac",
        "-b:a",   "128k",
        output_path,
    ])


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
    # Escape special characters in text
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
    # normalise MM:SS → HH:MM:SS if needed
    parts = time_str.strip().split(":")
    if len(parts) == 2:
        time_str = f"00:{parts[0].zfill(2)}:{parts[1].zfill(2)}"
    elif len(parts) == 3:
        time_str = ":".join(p.zfill(2) for p in parts)
    await generate_thumbnail(video_path, output_path, time_pos=time_str)


async def extract_subtitles(video_path: str, output_dir: str) -> List[dict]:
    """
    Extract all subtitle streams from an MKV/MP4.
    Returns list of dicts: {index, language, codec, output_path}
    """
    os.makedirs(output_dir, exist_ok=True)

    # Get stream info via ffprobe
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
        info["resolution"]   = f"{w}×{h}" if w and h else "N/A"
        info["video_codec"]  = video_s.get("codec_name", "Unknown")
        info["video_bitrate"] = video_s.get("bit_rate", "N/A")
        info["fps"] = eval(video_s["avg_frame_rate"]) if video_s.get("avg_frame_rate", "0/0") != "0/0" else 0

    if audio_s:
        lang = audio_s.get("tags", {}).get("language", "")
        info["audio_codec"]   = audio_s.get("codec_name", "Unknown")
        info["audio_channels"] = audio_s.get("channels", 0)
        info["audio_bitrate"] = audio_s.get("bit_rate", "N/A")
        info["audio_lang"]    = lang

    info["subtitle_count"] = sub_cnt
    return info

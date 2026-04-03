# utils/media_tools.py — ULTIMATE FIXED (no readuntil, no pipe, no separator errors)
import asyncio
import json
import os
import time
from typing import Callable, List, Optional


class FFmpegError(RuntimeError):
    pass


def _fmt_eta(sec: float) -> str:
    sec = max(0, int(sec))
    m, s = divmod(sec, 60)
    h, m = divmod(m, 60)
    if h:
        return f"{h}h {m}m {s}s"
    if m:
        return f"{m}m {s}s"
    return f"{s}s"


async def _probe_duration(path: str) -> float:
    proc = await asyncio.create_subprocess_exec(
        "ffprobe", "-v", "quiet",
        "-show_entries", "format=duration",
        "-of", "default=noprint_wrappers=1:nokey=1",
        path,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL,
    )
    out, _ = await proc.communicate()
    try:
        return float(out.decode().strip())
    except Exception:
        return 0.0


async def run_ffmpeg(cmd: list, timeout: int = 1800):
    """Run ffmpeg silently. Raises FFmpegError on failure."""
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        _, err = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        proc.kill()
        raise FFmpegError("FFmpeg timed out.")
    except asyncio.CancelledError:
        proc.kill()
        raise
    if proc.returncode != 0:
        raise FFmpegError(err.decode(errors="ignore")[-600:])


async def run_ffmpeg_with_progress(
    cmd: list,
    on_progress: Callable,
    update_interval: float = 5.0,
    timeout: int = 7200,
):
    """
    Run ffmpeg with progress by polling output file size and parsing stderr safely.
    Uses readline() (not readuntil) and has a fallback for missing newlines.
    """
    # Get total duration
    total_sec = 0.0
    for i, arg in enumerate(cmd):
        if arg == "-i" and i + 1 < len(cmd):
            total_sec = await _probe_duration(cmd[i + 1])
            break

    output_path = cmd[-1]

    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.PIPE,
    )

    last_update = 0.0
    start_time = time.time()
    out_sec = 0.0
    speed_x = 0.0

    def get_out_size():
        try:
            return os.path.getsize(output_path)
        except OSError:
            return 0

    try:
        # Read stderr line by line with a safety timeout
        while True:
            try:
                line = await asyncio.wait_for(proc.stderr.readline(), timeout=5.0)
            except asyncio.TimeoutError:
                # No stderr line for 5 seconds – still running, force progress update
                if time.time() - start_time > timeout:
                    proc.kill()
                    raise FFmpegError("FFmpeg timed out.")
                now = time.time()
                if now - last_update >= update_interval:
                    cur_size = get_out_size()
                    pct = min(out_sec / total_sec * 100, 99.9) if total_sec > 0 else 0.0
                    spd_str = f"{speed_x:.1f}x" if speed_x > 0 else "calculating..."
                    size_str = f"{cur_size / 1_048_576:.1f} MB" if cur_size >= 1_048_576 else f"{cur_size // 1024} KB"
                    eta_str = _fmt_eta((total_sec - out_sec) / speed_x) if total_sec > 0 and speed_x > 0 else "calculating..."
                    try:
                        await on_progress(pct, eta_str, spd_str, size_str)
                    except Exception:
                        pass
                    last_update = now
                continue

            if not line:   # EOF
                break

            text = line.decode(errors="ignore").strip()

            # Parse time=HH:MM:SS.ms
            if text.startswith("time="):
                time_part = text.split("time=")[1].split(" ")[0]
                try:
                    parts = time_part.split(":")
                    if len(parts) == 3:
                        h, m, s = int(parts[0]), int(parts[1]), float(parts[2])
                        out_sec = h * 3600 + m * 60 + s
                except Exception:
                    pass

            # Parse speed=1.23x
            elif text.startswith("speed="):
                speed_part = text.split("speed=")[1].split(" ")[0]
                if speed_part != "N/A" and speed_part.endswith("x"):
                    try:
                        speed_x = float(speed_part[:-1])
                    except Exception:
                        pass

            now = time.time()
            if now - last_update >= update_interval and out_sec > 0:
                pct = min(out_sec / total_sec * 100, 99.9) if total_sec > 0 else 0.0
                spd_str = f"{speed_x:.1f}x" if speed_x > 0 else "calculating..."
                cur_size = get_out_size()
                size_str = f"{cur_size / 1_048_576:.1f} MB" if cur_size >= 1_048_576 else f"{cur_size // 1024} KB"
                eta_str = _fmt_eta((total_sec - out_sec) / speed_x) if total_sec > 0 and speed_x > 0 else "calculating..."
                try:
                    await on_progress(pct, eta_str, spd_str, size_str)
                except Exception:
                    pass
                last_update = now

    except asyncio.CancelledError:
        proc.kill()
        raise
    finally:
        if proc.returncode is None:
            proc.kill()
            await proc.wait()

    if proc.returncode != 0:
        err_bytes = await proc.stderr.read(8000)
        err_text = err_bytes.decode(errors="ignore")
        lines = err_text.splitlines()
        error_lines = [l for l in lines if any(k in l.lower() for k in
                       ("error", "invalid", "no such", "cannot", "failed",
                        "unknown", "unrecognized", "unable", "permission"))]
        msg = "\n".join(error_lines[-5:]) if error_lines else err_text[-400:]
        raise FFmpegError(msg or f"FFmpeg exited with code {proc.returncode}")


# ---------- All other functions remain exactly as in your original file ----------
# (compress_only, resize_only, compress_and_resize, extract_audio, merge_videos,
#  add_watermark, generate_thumbnail, take_screenshot, extract_subtitles,
#  get_media_info, etc.) are unchanged. They are provided below for completeness.

async def _scale_encode(input_path, output_path, out_w, out_h, cap_fps, crf,
                        on_progress, update_interval, timeout=7200):
    out_w = out_w + (out_w % 2)
    out_h = out_h + (out_h % 2)
    cmd = [
        "ffmpeg", "-y", "-threads", "0",
        "-i", input_path,
        "-vf", f"scale={out_w}:{out_h}:flags=fast_bilinear,fps=fps={int(cap_fps)}",
        "-c:v", "libx264", "-preset", "ultrafast", "-tune", "zerolatency",
        "-crf", str(crf), "-movflags", "+faststart",
        "-c:a", "aac", "-b:a", "96k", "-ac", "2",
        output_path,
    ]
    if on_progress:
        await run_ffmpeg_with_progress(cmd, on_progress=on_progress,
                                       update_interval=update_interval, timeout=timeout)
    else:
        await run_ffmpeg(cmd, timeout=timeout)


async def extract_audio(video_path: str, output_path: str):
    await run_ffmpeg(["ffmpeg", "-y", "-i", video_path, "-vn", "-acodec", "copy", output_path])


async def merge_videos(video_paths: List[str], output_path: str):
    list_file = output_path + ".txt"
    with open(list_file, "w", encoding="utf-8") as f:
        for p in video_paths:
            f.write(f"file '{os.path.abspath(p)}'\n")
    await run_ffmpeg(["ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", list_file, "-c", "copy", output_path])
    os.remove(list_file)


async def split_video(video_path: str, start: str, duration: str, output_path: str):
    await run_ffmpeg(["ffmpeg", "-y", "-i", video_path, "-ss", start, "-t", duration, "-c", "copy", output_path])


async def _probe_video(input_path):
    proc = await asyncio.create_subprocess_exec(
        "ffprobe", "-v", "quiet", "-select_streams", "v:0",
        "-show_entries", "stream=width,height,r_frame_rate",
        "-of", "csv=p=0", input_path,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
    out, _ = await proc.communicate()
    line = (out.decode().strip().splitlines() or [""])[0]
    parts = line.split(",")
    try:
        w, h = int(parts[0]), int(parts[1])
    except:
        w, h = 1920, 1080
    try:
        fn, fd = parts[2].split("/")
        fps = round(int(fn) / int(fd), 2)
    except:
        fps = 30.0
    return w, h, fps


async def compress_only(input_path, output_path, crf=28, on_progress=None, update_interval=5.0):
    cmd = [
        "ffmpeg", "-y", "-threads", "0",
        "-i", input_path,
        "-vf", "fps=fps=30",
        "-c:v", "libx264", "-preset", "ultrafast", "-tune", "zerolatency",
        "-crf", str(crf), "-movflags", "+faststart",
        "-c:a", "aac", "-b:a", "96k", "-ac", "2",
        output_path,
    ]
    if on_progress:
        await run_ffmpeg_with_progress(cmd, on_progress=on_progress, update_interval=update_interval)
    else:
        await run_ffmpeg(cmd)


async def resize_only(input_path, output_path, target_height, on_progress=None, update_interval=5.0):
    w, h, fps = await _probe_video(input_path)
    cap_fps = min(fps, 30.0)
    scale = target_height / h
    out_w = int(w * scale)
    await _scale_encode(input_path, output_path, out_w, target_height, cap_fps,
                        crf=23, on_progress=on_progress, update_interval=update_interval)


async def compress_and_resize(input_path, output_path, target_height, crf=28,
                              on_progress=None, update_interval=5.0):
    w, h, fps = await _probe_video(input_path)
    cap_fps = min(fps, 30.0)
    scale = target_height / h
    out_w = int(w * scale)
    await _scale_encode(input_path, output_path, out_w, target_height, cap_fps,
                        crf=crf, on_progress=on_progress, update_interval=update_interval)


async def compress_video(input_path, output_path, resolution=None, crf=28,
                         on_progress=None, update_interval=5.0, preset="ultrafast"):
    res_str = str(resolution) if resolution else ""
    if res_str and res_str not in ("orig", "None", "none", ""):
        await compress_and_resize(input_path, output_path, target_height=int(res_str),
                                  crf=crf, on_progress=on_progress, update_interval=update_interval)
    else:
        await compress_only(input_path, output_path, crf=crf,
                            on_progress=on_progress, update_interval=update_interval)


async def add_watermark(input_path, output_path, text, position="bottomright", opacity=0.6):
    pos_map = {
        "topleft": "x=10:y=10",
        "topright": "x=w-tw-10:y=10",
        "bottomleft": "x=10:y=h-th-10",
        "bottomright": "x=w-tw-10:y=h-th-10",
        "center": "x=(w-tw)/2:y=(h-th)/2",
    }
    pos = pos_map.get(position, pos_map["bottomright"])
    safe_text = text.replace("'", "\\'").replace(":", "\\:")
    vf = f"drawtext=text='{safe_text}':fontsize=28:fontcolor=white@{opacity}:{pos}:box=1:boxcolor=black@0.3:boxborderw=4"
    await run_ffmpeg(["ffmpeg", "-y", "-i", input_path, "-vf", vf, "-c:a", "copy", output_path])


async def generate_thumbnail(video_path, thumb_path, time_pos="00:00:02"):
    await run_ffmpeg(["ffmpeg", "-y", "-ss", time_pos, "-i", video_path, "-vframes", "1", "-q:v", "2", thumb_path])
    return thumb_path


async def take_screenshot(video_path, output_path, time_str):
    parts = time_str.strip().split(":")
    if len(parts) == 2:
        time_str = f"00:{parts[0].zfill(2)}:{parts[1].zfill(2)}"
    elif len(parts) == 3:
        time_str = ":".join(p.zfill(2) for p in parts)
    await generate_thumbnail(video_path, output_path, time_pos=time_str)


async def extract_subtitles(video_path: str, output_dir: str) -> List[dict]:
    os.makedirs(output_dir, exist_ok=True)
    proc = await asyncio.create_subprocess_exec(
        "ffprobe", "-v", "quiet", "-print_format", "json", "-show_streams", video_path,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    out, _ = await proc.communicate()
    try:
        streams_data = json.loads(out.decode())
        streams = streams_data.get("streams", [])
    except Exception:
        return []
    sub_streams = [s for s in streams if s.get("codec_type") == "subtitle"]
    extracted = []
    for i, s in enumerate(sub_streams):
        lang = s.get("tags", {}).get("language", "und")
        codec = s.get("codec_name", "unknown")
        ext = "ass" if codec in ("ass", "ssa") else "srt"
        out_file = os.path.join(output_dir, f"sub_{i}_{lang}.{ext}")
        try:
            await run_ffmpeg(["ffmpeg", "-y", "-i", video_path, "-map", f"0:s:{i}", out_file])
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
    proc = await asyncio.create_subprocess_exec(
        "ffprobe", "-v", "quiet", "-print_format", "json", "-show_format", "-show_streams", file_path,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    out, _ = await proc.communicate()
    try:
        data = json.loads(out.decode())
    except Exception:
        return None
    fmt = data.get("format", {})
    streams = data.get("streams", [])
    video_s = next((s for s in streams if s.get("codec_type") == "video"), None)
    audio_s = next((s for s in streams if s.get("codec_type") == "audio"), None)
    sub_cnt = sum(1 for s in streams if s.get("codec_type") == "subtitle")
    duration = float(fmt.get("duration") or 0)
    info = {"size_bytes": int(fmt.get("size", 0)), "duration": duration, "format": fmt.get("format_long_name", "Unknown")}
    if video_s:
        w = video_s.get("width", 0)
        h = video_s.get("height", 0)
        info["resolution"] = f"{w}x{h}" if w and h else "N/A"
        info["video_codec"] = video_s.get("codec_name", "Unknown")
        info["video_bitrate"] = video_s.get("bit_rate", "N/A")
        afr = video_s.get("avg_frame_rate", "0/0")
        try:
            num, den = afr.split("/")
            info["fps"] = round(int(num) / int(den), 2) if int(den) else 0
        except Exception:
            info["fps"] = 0
    if audio_s:
        lang = audio_s.get("tags", {}).get("language", "")
        info["audio_codec"] = audio_s.get("codec_name", "Unknown")
        info["audio_channels"] = audio_s.get("channels", 0)
        info["audio_bitrate"] = audio_s.get("bit_rate", "N/A")
        info["audio_lang"] = lang
    info["subtitle_count"] = sub_cnt
    return info

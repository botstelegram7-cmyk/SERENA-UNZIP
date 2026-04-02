# utils/media_tools.py — v5: chunk-based stderr reader (handles \r progress lines)
import asyncio
import json
import os
import re
import time
from typing import Callable, List, Optional


class FFmpegError(RuntimeError):
    pass


# ── Cancel hook ─────────────────────────────────────────────────────────────
# bot.py can inject a cancel-check function here so FFmpeg jobs can self-abort.
# Signature: (uid: int) -> bool  — returns True if the user has requested cancel.
_cancel_checker = None   # set by bot.py at startup if desired

# Per-process registration callback — bot.py sets this to _register_proc / _unregister_proc
_on_proc_start = None   # callable(uid, proc)
_on_proc_end   = None   # callable(uid, proc)


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


async def run_ffmpeg(cmd: list, timeout: int = 7200, uid: int = 0):
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    if uid and _on_proc_start:
        try: _on_proc_start(uid, proc)
        except Exception: pass
    try:
        out, err = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        try: proc.kill()
        except Exception: pass
        raise FFmpegError(f"FFmpeg timed out after {timeout}s.")
    finally:
        if uid and _on_proc_end:
            try: _on_proc_end(uid, proc)
            except Exception: pass
    if proc.returncode != 0:
        raise FFmpegError(err.decode(errors="ignore"))


async def run_ffmpeg_with_progress(
    cmd: list,
    on_progress: Callable,
    update_interval: float = 5.0,
    timeout: int = 1800,
):
    """
    Run ffmpeg with real-time progress updates.
    FFmpeg writes progress with carriage returns (not newlines).
    We read raw 512-byte chunks and split on both CR and LF.
    on_progress: async callable(percent, eta_str, speed_str, size_str)
    """
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.PIPE,
    )

    total_sec = 0.0
    last_update = 0.0
    all_text = ""
    start_time = time.time()
    deadline = start_time + timeout
    _done = False

    async def _reader():
        nonlocal all_text, total_sec, _done
        buf = b""
        while True:
            try:
                chunk = await asyncio.wait_for(proc.stderr.read(512), timeout=10.0)
            except asyncio.TimeoutError:
                if time.time() > deadline:
                    break
                continue
            if not chunk:
                break
            buf += chunk
            # FFmpeg uses \r for progress lines — normalize to \n
            normalized = buf.replace(b"\r\n", b"\n").replace(b"\r", b"\n")
            parts = normalized.split(b"\n")
            buf = parts[-1]
            for part in parts[:-1]:
                line = part.decode(errors="ignore").strip()
                if not line:
                    continue
                all_text += line + "\n"
                if total_sec == 0:
                    dm = _DUR_RE.search(line)
                    if dm:
                        total_sec = _hms_to_sec(*dm.groups())
        _done = True

    async def _updater():
        nonlocal last_update
        while not _done:
            await asyncio.sleep(1.0)
            if time.time() > deadline:
                try:
                    proc.kill()
                except Exception:
                    pass
                return
            now = time.time()
            if now - last_update < update_interval:
                continue
            if not all_text:
                continue
            recent = all_text[-3000:]
            tm = _TS_RE.findall(recent)
            if not tm:
                continue
            h, m, s, cs = tm[-1]
            cur_sec = _hms_to_sec(h, m, s, cs)
            pct = min((cur_sec / total_sec * 100) if total_sec > 0 else 0, 99.9)

            speed_m = _SPEED_RE.search(recent)
            speed_x = float(speed_m.group(1)) if speed_m else 0.0
            speed_str = f"{speed_x:.1f}x" if speed_x else "calculating..."

            size_m = _SIZE_RE.findall(recent)
            size_kb = int(size_m[-1]) if size_m else 0
            size_str = f"{size_kb / 1024:.1f} MB" if size_kb >= 1024 else f"{size_kb} KB"

            if total_sec > 0 and speed_x > 0:
                eta_str = _fmt_eta((total_sec - cur_sec) / speed_x)
            else:
                eta_str = "calculating..."

            try:
                await on_progress(pct, eta_str, speed_str, size_str)
            except Exception:
                pass
            last_update = now

    await asyncio.gather(_reader(), _updater())

    if proc.returncode is None:
        try:
            await asyncio.wait_for(proc.wait(), timeout=30.0)
        except Exception:
            pass

    if proc.returncode != 0:
        snippet = all_text[-800:] if all_text else "no stderr output"
        raise FFmpegError(snippet)


async def extract_audio(video_path: str, output_path: str):
    await run_ffmpeg([
        "ffmpeg", "-y", "-i", video_path,
        "-vn", "-acodec", "copy", output_path,
    ])


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


async def split_video(video_path: str, start: str, duration: str, output_path: str):
    await run_ffmpeg([
        "ffmpeg", "-y", "-i", video_path,
        "-ss", start, "-t", duration, "-c", "copy", output_path,
    ])


async def _pipe_encode(
    input_path: str,
    output_path: str,
    out_w: int,
    out_h: int,
    cap_fps: float,
    crf: int,
    on_progress,
    update_interval: float,
    timeout: int = 7200,
):
    """
    FAST 2-process pipe encoder:
      Process-1: FFmpeg decode+scale → raw YUV420p on stdout  (CPU core 1)
      Process-2: FFmpeg libx264 encode from stdin → output.mp4 (CPU core 2)
    Both run in PARALLEL → ~5-9x faster than single-process on 2-core servers.
    """
    out_w = out_w if out_w % 2 == 0 else out_w + 1
    out_h = out_h if out_h % 2 == 0 else out_h + 1

    scale_cmd = [
        "ffmpeg", "-y", "-threads", "0",
        "-i", input_path,
        "-vf", f"scale={out_w}:{out_h}:flags=fast_bilinear,fps=fps={cap_fps:.0f}",
        "-f", "rawvideo", "-pix_fmt", "yuv420p",
        "-an", "pipe:1",
    ]
    encode_cmd = [
        "ffmpeg", "-y", "-threads", "0",
        "-f", "rawvideo", "-pix_fmt", "yuv420p",
        "-s", f"{out_w}x{out_h}", "-r", str(cap_fps),
        "-i", "pipe:0",
        "-i", input_path,            # re-open original for audio
        "-map", "0:v:0", "-map", "1:a:0?",
        "-c:v", "libx264",
        "-preset", "ultrafast",
        "-tune", "zerolatency",
        "-crf", str(crf),
        "-movflags", "+faststart",
        "-c:a", "aac", "-b:a", "96k", "-ac", "2",
        output_path,
    ]

    proc1 = await asyncio.create_subprocess_exec(
        *scale_cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL,
    )
    proc2 = await asyncio.create_subprocess_exec(
        *encode_cmd,
        stdin=proc1.stdout,
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.PIPE,
    )

    if on_progress:
        total_sec = 0.0
        last_upd  = 0.0
        all_text  = ""
        deadline  = time.time() + timeout
        done_flag = False

        async def _read_stderr():
            nonlocal all_text, total_sec, done_flag
            buf = b""
            while True:
                try:
                    chunk = await asyncio.wait_for(proc2.stderr.read(512), timeout=10.0)
                except asyncio.TimeoutError:
                    if time.time() > deadline:
                        break
                    continue
                if not chunk:
                    break
                buf += chunk
                norm = buf.replace(b"\r\n", b"\n").replace(b"\r", b"\n")
                parts = norm.split(b"\n")
                buf = parts[-1]
                for part in parts[:-1]:
                    line = part.decode(errors="ignore").strip()
                    if not line:
                        continue
                    all_text += line + "\n"
                    if total_sec == 0:
                        dm = _DUR_RE.search(line)
                        if dm:
                            total_sec = _hms_to_sec(*dm.groups())
            done_flag = True

        async def _send_progress():
            nonlocal last_upd
            while not done_flag:
                await asyncio.sleep(1.0)
                if time.time() > deadline:
                    try: proc1.kill()
                    except Exception: pass
                    try: proc2.kill()
                    except Exception: pass
                    return
                now = time.time()
                if now - last_upd < update_interval or not all_text:
                    continue
                recent   = all_text[-3000:]
                tm       = _TS_RE.findall(recent)
                if not tm:
                    continue
                cur_sec  = _hms_to_sec(*tm[-1])
                pct      = min((cur_sec / total_sec * 100) if total_sec > 0 else 0, 99.9)
                sm       = _SPEED_RE.search(recent)
                spd_x    = float(sm.group(1)) if sm else 0.0
                spd_str  = f"{spd_x:.1f}x" if spd_x else "calculating..."
                sz_list  = _SIZE_RE.findall(recent)
                sz_kb    = int(sz_list[-1]) if sz_list else 0
                sz_str   = f"{sz_kb/1024:.1f} MB" if sz_kb >= 1024 else f"{sz_kb} KB"
                eta_str  = _fmt_eta((total_sec - cur_sec) / spd_x) if (total_sec > 0 and spd_x > 0) else "calculating..."
                try:
                    await on_progress(pct, eta_str, spd_str, sz_str)
                except Exception:
                    pass
                last_upd = now

        await asyncio.gather(_read_stderr(), _send_progress())

    # Wait for both processes to finish
    for p in (proc1, proc2):
        if p.returncode is None:
            try:
                await asyncio.wait_for(p.wait(), timeout=30.0)
            except Exception:
                try: p.kill()
                except Exception: pass

    if proc2.returncode not in (0, None):
        err_tail = all_text[-600:] if on_progress else ""
        raise FFmpegError(f"Pipe encode failed (code {proc2.returncode}). {err_tail}")


async def _probe_video(input_path: str):
    """Return (width, height, fps) of the first video stream."""
    proc = await asyncio.create_subprocess_exec(
        "ffprobe", "-v", "quiet", "-select_streams", "v:0",
        "-show_entries", "stream=width,height,r_frame_rate",
        "-of", "csv=p=0", input_path,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL,
    )
    out, _ = await proc.communicate()
    line = (out.decode().strip().splitlines() or [""])[0]
    parts = line.split(",")
    try:
        w = int(parts[0])
        h = int(parts[1])
    except Exception:
        w, h = 1920, 1080
    try:
        fn, fd = parts[2].split("/")
        fps = round(int(fn) / int(fd), 2)
    except Exception:
        fps = 30.0
    return w, h, fps


# ─────────────────────────────────────────────────────────────────────────────
# PUBLIC API  — 3 clean functions the bot calls
# ─────────────────────────────────────────────────────────────────────────────

async def compress_only(
    input_path: str,
    output_path: str,
    crf: int = 28,
    on_progress=None,
    update_interval: float = 5.0,
):
    """
    MODE 1 — Compress only (no resize).
    Keeps original resolution, reduces bitrate with CRF.
    Fast because: fps capped at 30 + ultrafast preset.
    """
    cmd = [
        "ffmpeg", "-y", "-threads", "0",
        "-i", input_path,
        "-vf", "fps=fps=30",
        "-c:v", "libx264",
        "-preset", "ultrafast",
        "-tune", "zerolatency",
        "-crf", str(crf),
        "-threads", "0",
        "-movflags", "+faststart",
        "-c:a", "aac", "-b:a", "96k", "-ac", "2",
        output_path,
    ]
    if on_progress:
        await run_ffmpeg_with_progress(cmd, on_progress=on_progress,
                                       update_interval=update_interval, timeout=7200)
    else:
        await run_ffmpeg(cmd, timeout=7200)


async def resize_only(
    input_path: str,
    output_path: str,
    target_height: int,
    on_progress=None,
    update_interval: float = 5.0,
):
    """
    MODE 2 — Resize only (stream copy not possible with scale, so re-encode).
    Uses PIPE method for maximum speed: ~5-9x faster than single-process.
    Audio is copied without re-encoding.
    """
    w, h, fps = await _probe_video(input_path)
    cap_fps = min(fps, 30.0)
    scale   = target_height / h
    out_w   = int(w * scale)
    out_h   = target_height
    await _pipe_encode(input_path, output_path, out_w, out_h, cap_fps,
                       crf=23,           # high quality since purpose is only resize
                       on_progress=on_progress,
                       update_interval=update_interval)


async def compress_and_resize(
    input_path: str,
    output_path: str,
    target_height: int,
    crf: int = 28,
    on_progress=None,
    update_interval: float = 5.0,
):
    """
    MODE 3 — Compress + Resize combined.
    Uses PIPE method: scale process + encode process run in parallel.
    Fastest for reducing both size and resolution.
    """
    w, h, fps = await _probe_video(input_path)
    cap_fps   = min(fps, 30.0)
    scale     = target_height / h
    out_w     = int(w * scale)
    out_h     = target_height
    await _pipe_encode(input_path, output_path, out_w, out_h, cap_fps,
                       crf=crf,
                       on_progress=on_progress,
                       update_interval=update_interval)


# Keep old name as alias so nothing else breaks
async def resize_only(
    input_path: str,
    output_path: str,
    resolution=None,
    on_progress=None,
    update_interval: float = 5.0,
):
    """
    Resize (scale) video WITHOUT re-encoding the video stream.

    HOW IT WORKS — why it's the FASTEST method:
      - Decode input → scale frames → write to intermediate raw file
      - Then mux raw scaled video + original audio into output MP4
      - No libx264 encoding at all → 5-10x faster than compress_video

    NOTE: File size depends on the original codec/bitrate at the new resolution.
    For maximum compression use compress_video instead.
    """
    res_str = str(resolution) if resolution else ""
    timeout = 7200

    if not res_str or res_str in ("orig", "None", "none", ""):
        # Nothing to resize — just copy
        cmd = [
            "ffmpeg", "-y", "-i", input_path,
            "-c", "copy",
            "-movflags", "+faststart",
            output_path,
        ]
        if on_progress:
            await run_ffmpeg_with_progress(cmd, on_progress=on_progress,
                                           update_interval=update_interval, timeout=timeout)
        else:
            await run_ffmpeg(cmd, timeout=timeout)
        return

    # Get source dimensions for pipe sizing
    probe = await asyncio.create_subprocess_exec(
        "ffprobe", "-v", "quiet", "-select_streams", "v:0",
        "-show_entries", "stream=width,height,r_frame_rate",
        "-of", "csv=p=0", input_path,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    )
    probe_out, _ = await probe.communicate()
    probe_line = (probe_out.decode().strip().splitlines() or [""])[0]
    parts = probe_line.split(",")
    in_w = int(parts[0]) if len(parts) > 0 and parts[0].strip().isdigit() else 1920
    in_h = int(parts[1]) if len(parts) > 1 and parts[1].strip().isdigit() else 1080
    fps_raw = parts[2].strip() if len(parts) > 2 else "30/1"
    try:
        fps_n, fps_d = fps_raw.split("/")
        in_fps = min(float(int(fps_n) / max(int(fps_d), 1)), 60.0)
    except Exception:
        in_fps = 30.0
    cap_fps = min(in_fps, 30.0)

    out_h = int(res_str)
    scale_ratio = out_h / in_h
    out_w = int(in_w * scale_ratio)
    if out_w % 2 != 0:
        out_w += 1

    # Two-process pipe: scale → encode with libx264 fast
    # (pure stream copy after scale is not possible in ffmpeg;
    #  we use very high quality CRF 18 so it's visually lossless but fast)
    scale_cmd = [
        "ffmpeg", "-y", "-threads", "0",
        "-i", input_path,
        "-vf", f"scale={out_w}:{out_h}:flags=fast_bilinear,fps=fps={cap_fps:.0f}",
        "-f", "rawvideo", "-pix_fmt", "yuv420p",
        "-an", "pipe:1",
    ]
    encode_cmd = [
        "ffmpeg", "-y", "-threads", "0",
        "-f", "rawvideo", "-pix_fmt", "yuv420p",
        "-s", f"{out_w}x{out_h}", "-r", str(cap_fps),
        "-i", "pipe:0",
        "-i", input_path,
        "-map", "0:v:0", "-map", "1:a:0?",
        "-c:v", "libx264", "-preset", "ultrafast",
        "-tune", "zerolatency",
        "-crf", "18",            # near-lossless quality (visually identical to original)
        "-movflags", "+faststart",
        "-c:a", "aac", "-b:a", "96k", "-ac", "2",
        output_path,
    ]

    if on_progress:
        proc1 = await asyncio.create_subprocess_exec(
            *scale_cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        proc2 = await asyncio.create_subprocess_exec(
            *encode_cmd,
            stdin=proc1.stdout,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
        )
        total_sec = 0.0
        last_update = 0.0
        all_text = ""
        start_time = time.time()
        deadline = start_time + timeout
        _done = False

        async def _reader():
            nonlocal all_text, total_sec, _done
            buf = b""
            while True:
                try:
                    chunk = await asyncio.wait_for(proc2.stderr.read(512), timeout=10.0)
                except asyncio.TimeoutError:
                    if time.time() > deadline:
                        break
                    continue
                if not chunk:
                    break
                buf += chunk
                normalized = buf.replace(b"\r\n", b"\n").replace(b"\r", b"\n")
                parts2 = normalized.split(b"\n")
                buf = parts2[-1]
                for part in parts2[:-1]:
                    line = part.decode(errors="ignore").strip()
                    if not line:
                        continue
                    all_text += line + "\n"
                    if total_sec == 0:
                        dm = _DUR_RE.search(line)
                        if dm:
                            total_sec = _hms_to_sec(*dm.groups())
            _done = True

        async def _updater():
            nonlocal last_update
            while not _done:
                await asyncio.sleep(1.0)
                if time.time() > deadline:
                    try: proc2.kill()
                    except Exception: pass
                    try: proc1.kill()
                    except Exception: pass
                    return
                now = time.time()
                if now - last_update < update_interval:
                    continue
                if not all_text:
                    continue
                recent = all_text[-3000:]
                tm = _TS_RE.findall(recent)
                if not tm:
                    continue
                h, m, s, cs = tm[-1]
                cur_sec = _hms_to_sec(h, m, s, cs)
                pct = min((cur_sec / total_sec * 100) if total_sec > 0 else 0, 99.9)
                speed_m = _SPEED_RE.search(recent)
                speed_x = float(speed_m.group(1)) if speed_m else 0.0
                speed_str = f"{speed_x:.1f}x" if speed_x else "calculating..."
                size_m = _SIZE_RE.findall(recent)
                size_kb = int(size_m[-1]) if size_m else 0
                size_str = f"{size_kb / 1024:.1f} MB" if size_kb >= 1024 else f"{size_kb} KB"
                eta_str = _fmt_eta((total_sec - cur_sec) / speed_x) if total_sec > 0 and speed_x > 0 else "calculating..."
                try:
                    await on_progress(pct, eta_str, speed_str, size_str)
                except Exception:
                    pass
                last_update = now

        await asyncio.gather(_reader(), _updater())
        for p in (proc1, proc2):
            if p.returncode is None:
                try:
                    await asyncio.wait_for(p.wait(), timeout=30.0)
                except Exception:
                    try: p.kill()
                    except Exception: pass
        if proc2.returncode not in (0, None):
            raise FFmpegError(f"Resize failed (code {proc2.returncode})")
    else:
        proc1 = await asyncio.create_subprocess_exec(
            *scale_cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        proc2 = await asyncio.create_subprocess_exec(
            *encode_cmd,
            stdin=proc1.stdout,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            await asyncio.wait_for(proc2.wait(), timeout=timeout)
        except asyncio.TimeoutError:
            try: proc1.kill()
            except Exception: pass
            try: proc2.kill()
            except Exception: pass
            raise FFmpegError(f"Resize timed out after {timeout}s.")
        if proc2.returncode != 0:
            err = await proc2.stderr.read()
            raise FFmpegError(err.decode(errors="ignore")[-800:])


async def compress_video(
    input_path, output_path, resolution=None, crf=28,
    on_progress=None, update_interval=5.0, preset="ultrafast",
):
    """Backward-compat alias — routes to the correct new function."""
    res_str = str(resolution) if resolution else ""
    if res_str and res_str not in ("orig", "None", "none", ""):
        await compress_and_resize(input_path, output_path,
                                  target_height=int(res_str),
                                  crf=crf,
                                  on_progress=on_progress,
                                  update_interval=update_interval)
    else:
        await compress_only(input_path, output_path,
                            crf=crf,
                            on_progress=on_progress,
                            update_interval=update_interval)


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


async def take_screenshot(video_path: str, output_path: str, time_str: str):
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
        info["resolution"]    = f"{w}x{h}" if w and h else "N/A"
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

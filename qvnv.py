#!/usr/bin/env python3

import argparse
import json
import os
import shutil
import signal
import subprocess
import sys
import time

try:
    from PIL import Image
except ImportError:
    sys.stderr.write("qvnp: PIL is not installed. 'pip install --break-system-packages pillow' or search for it in your system repositories\n")
    sys.exit(1)

# ---- hardcoded control points ------------------------------------------
DEFAULT_FPS = 15.0
MAX_CATCHUP_DROP = 15
AUDIO_START_GRACE = 0.05
FFMPEG_SCALE_FLAGS = "flags=lanczos"
FFMPEG_SCALE_FLAGS_PIXELIZE = "flags=neighbor"  # --pixelize: point-sampling
HIGHLIGHT_AMOUNT_STEP = 0.5
PIXEL_BLEND_MIN = 1
PIXEL_BLEND_MAX = 8
DEFAULT_PIXEL_BLEND = 8  # 8 = pure point-sample (sharpest), 1 = fully averaged
# ------------------------------------------------------------------------

RESET = "\033[0m"
UPPER_HALF = "\u2580"  # ▀
FULL_BLOCK = "\u2588"  # █
HIDE_CURSOR = "\033[?25l"
SHOW_CURSOR = "\033[?25h"
CLEAR = "\033[2J"
HOME = "\033[H"


def fg(r, g, b):
    return f"\033[38;2;{r};{g};{b}m"


def bg(r, g, b):
    return f"\033[48;2;{r};{g};{b}m"


def is_linux_console() -> bool:
    return os.environ.get("TERM", "").lower() == "linux"


def check_binaries():
    for b in ("ffmpeg", "ffprobe"):
        if shutil.which(b) is None:
            sys.stderr.write(f"qvnp: not found {b} in PATH\n")
            sys.exit(1)


def ffprobe_json(path: str, extra_args) -> dict:
    cmd = ["ffprobe", "-v", "error", "-of", "json"] + extra_args + [path]
    try:
        out = subprocess.check_output(cmd, stderr=subprocess.DEVNULL)
    except subprocess.CalledProcessError as e:
        sys.stderr.write(f"qvnp: ffprobe crashed: {e}\n")
        sys.exit(1)
    return json.loads(out or b"{}")


def probe_video(path: str):
    data = ffprobe_json(
        path,
        ["-select_streams", "v:0", "-show_entries", "stream=width,height,r_frame_rate"],
    )
    streams = data.get("streams") or []
    if not streams:
        sys.stderr.write("qvnp: video stream not found\n")
        sys.exit(1)
    s = streams[0]
    w, h = int(s["width"]), int(s["height"])
    num, den = s.get("r_frame_rate", "25/1").split("/")
    src_fps = float(num) / float(den) if float(den) else 25.0
    return w, h, src_fps


def has_audio(path: str) -> bool:
    data = ffprobe_json(path, ["-select_streams", "a", "-show_entries", "stream=index"])
    return bool(data.get("streams"))


def fit_size(img_w, img_h, max_cols, max_rows, col_mult, row_mult):
    avail_w = max(col_mult, max_cols * col_mult)
    avail_h = max(row_mult, max_rows * row_mult)
    scale = min(avail_w / img_w, avail_h / img_h)
    new_w = max(col_mult, round(img_w * scale))
    new_h = max(row_mult, round(img_h * scale))
    new_w -= new_w % col_mult
    new_h -= new_h % row_mult
    return new_w or col_mult, new_h or row_mult


def render_half(img: Image.Image) -> str:
    w, h = img.size
    px = img.load()
    out = []
    for y in range(0, h, 2):
        row = []
        last_fg = last_bg = None
        for x in range(w):
            top = px[x, y]
            bottom = px[x, y + 1] if y + 1 < h else top
            if top != last_fg:
                row.append(fg(*top))
                last_fg = top
            if bottom != last_bg:
                row.append(bg(*bottom))
                last_bg = bottom
            row.append(UPPER_HALF)
        row.append(RESET)
        out.append("".join(row))
    return "\n".join(out)


def _row_average(px, x, y, row_mult, h):
    r_s = g_s = b_s = n = 0
    for dy in range(row_mult):
        yy = y + dy
        if yy >= h:
            break
        r, g, b = px[x, yy]
        r_s += r; g_s += g; b_s += b; n += 1
    if n == 0:
        return px[x, min(y, h - 1)]
    return (r_s // n, g_s // n, b_s // n)


def render_full(img: Image.Image, row_mult: int, pixelize: bool = False, blend_frac: float = 0.0) -> str:
    """
    pixelize=False: always full box-average per block (original 'smooth' mode).
    pixelize=True: blend between a single point-sample (blend_frac=0.0, sharpest)
                   and a full box-average (blend_frac=1.0, smoothest).
    """
    w, h = img.size
    px = img.load()
    out = []
    for y in range(0, h, row_mult):
        row = []
        last_fg = None
        for x in range(w):
            if not pixelize:
                color = _row_average(px, x, y, row_mult, h)
            else:
                yy = min(y + row_mult // 2, h - 1)
                point_color = px[x, yy]
                if blend_frac <= 0.0:
                    color = point_color
                elif blend_frac >= 1.0:
                    color = _row_average(px, x, y, row_mult, h)
                else:
                    avg_color = _row_average(px, x, y, row_mult, h)
                    color = tuple(
                        round(pt * (1.0 - blend_frac) + av * blend_frac)
                        for pt, av in zip(point_color, avg_color)
                    )
            if color != last_fg:
                row.append(fg(*color))
                last_fg = color
            row.append(FULL_BLOCK)
        row.append(RESET)
        out.append("".join(row))
    return "\n".join(out)


def build_vf(fps: float, w: int, h: int, highlight: int, pixelize: bool) -> str:
    parts = [f"fps={fps}"]
    if highlight:
        amt = round(highlight * HIGHLIGHT_AMOUNT_STEP, 2)
        parts.append(f"unsharp=5:5:{amt}:5:5:{amt}")
    scale_flags = FFMPEG_SCALE_FLAGS_PIXELIZE if pixelize else FFMPEG_SCALE_FLAGS
    parts.append(f"scale={w}:{h}:{scale_flags}")
    return ",".join(parts)


def start_ffmpeg(path, vf, w, h):
    cmd = [
        "ffmpeg", "-loglevel", "error", "-i", path,
        "-vf", vf,
        "-pix_fmt", "rgb24", "-f", "rawvideo", "pipe:1",
    ]
    return subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, bufsize=w * h * 3 * 4)


def start_audio(path):
    return subprocess.Popen(
        ["ffplay", "-nodisp", "-autoexit", "-loglevel", "quiet", "-vn", path],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=False,
    )


def read_exact(stream, n):
    buf = bytearray()
    while len(buf) < n:
        chunk = stream.read(n - len(buf))
        if not chunk:
            return None  # EOF
        buf.extend(chunk)
    return bytes(buf)


def main():
    p = argparse.ArgumentParser(description="Video/photo playback in terminal.")
    p.add_argument("path", help="path to media-file")
    p.add_argument("--width", type=int, help="width in symbols, terminal's width by default")
    p.add_argument("--height", type=int, help="height in symbols, terminal's height minus one by default")
    p.add_argument("--fps", type=float, default=DEFAULT_FPS, help=f"target fps (by default: {DEFAULT_FPS})")
    p.add_argument("--mode", choices=["auto", "half", "full"], default="auto",
                    help="half = more pixels, full = less pixels, full works better in tty, half in graphical terminal")
    p.add_argument("--font-aspect", type=float, default=2.0, help="height/width of the font symbol (for '--mode full')")
    p.add_argument("--no-audio", action="store_true", help="launch without sound")
    p.add_argument("--loop", action="store_true", help="play file cyclically until stopped manually")
    p.add_argument("--highlight", type=int, choices=range(1, 9), metavar="1-8", default=0,
                    help="enforcement of small contrast details (e.g stars in the sky)")
    p.add_argument("--no-pixelize", dest="pixelize", action="store_false", default=True,
                    help="disable pixelize mode (use smooth lanczos downscale + full box-average instead of point-sampling); pixelize is ON by default")
    # p.add_argument("--pixel-blend", type=int, choices=range(PIXEL_BLEND_MIN, PIXEL_BLEND_MAX + 1),
    #                 metavar=f"{PIXEL_BLEND_MIN}-{PIXEL_BLEND_MAX}", default=DEFAULT_PIXEL_BLEND,
    #                 help=f"only in pixelize mode: how much to mix the point-sample with the arithmetic mean of the block; "
    #                      f"{PIXEL_BLEND_MAX} = pure point-sample (sharpest), {PIXEL_BLEND_MIN} = fully averaged (smoothest) "
    #                      f"(default: {DEFAULT_PIXEL_BLEND})")
    args = p.parse_args()

    args.pixel_blend = DEFAULT_PIXEL_BLEND

    check_binaries()

    if args.mode == "full":
        use_full = True
    elif args.mode == "half":
        use_full = False
    else:
        use_full = is_linux_console()

    col_mult, row_mult = (1, max(1, round(args.font_aspect))) if use_full else (1, 2)

    term_cols, term_rows = shutil.get_terminal_size(fallback=(80, 24))
    max_cols = args.width or term_cols
    max_rows = args.height or max(1, term_rows - 1)

    src_w, src_h, _src_fps = probe_video(args.path)
    target_w, target_h = fit_size(src_w, src_h, max_cols, max_rows, col_mult, row_mult)
    frame_bytes = target_w * target_h * 3

    audio_available = has_audio(args.path) and not args.no_audio

    vf = build_vf(args.fps, target_w, target_h, args.highlight, args.pixelize)

    # map 1..8 (smooth..sharp) to a 0.0..1.0 blend-toward-average fraction
    blend_frac = (PIXEL_BLEND_MAX - args.pixel_blend) / (PIXEL_BLEND_MAX - PIXEL_BLEND_MIN)

    sys.stdout.write(HIDE_CURSOR + CLEAR)
    sys.stdout.flush()

    audio_proc = None
    ffmpeg_proc = None

    def cleanup(*_a):
        for proc in (ffmpeg_proc, audio_proc):
            if proc and proc.poll() is None:
                proc.terminate()
        sys.stdout.write(SHOW_CURSOR + RESET + "\n")
        sys.stdout.flush()

    signal.signal(signal.SIGINT, lambda *a: (cleanup(), sys.exit(0)))
    signal.signal(signal.SIGTERM, lambda *a: (cleanup(), sys.exit(0)))

    try:
        while True:
            ffmpeg_proc = start_ffmpeg(args.path, vf, target_w, target_h)
            if audio_available:
                audio_proc = start_audio(args.path)
                time.sleep(AUDIO_START_GRACE)

            start_time = time.monotonic()
            frame_idx = 0

            while True:
                raw = read_exact(ffmpeg_proc.stdout, frame_bytes)
                if raw is None:
                    break

                target_t = frame_idx / args.fps
                now = time.monotonic() - start_time
                lag = now - target_t

                if lag > (1.0 / args.fps) and frame_idx % (MAX_CATCHUP_DROP + 1) != 0:
                    frame_idx += 1
                    continue

                img = Image.frombytes("RGB", (target_w, target_h), raw)
                body = render_full(img, row_mult, args.pixelize, blend_frac) if use_full else render_half(img)
                sys.stdout.write(HOME + body)
                sys.stdout.flush()

                frame_idx += 1
                sleep_for = target_t - (time.monotonic() - start_time)
                if sleep_for > 0:
                    time.sleep(sleep_for)

            ffmpeg_proc.wait()
            if audio_proc and audio_proc.poll() is None:
                audio_proc.wait()

            if not args.loop:
                break
    finally:
        cleanup()


if __name__ == "__main__":
    main()

#!/usr/bin/env python3

import argparse
import json
import os
import shutil
import signal
import subprocess
import sys
import time
import math

try:
    from PIL import Image, ImageFilter, ImageChops

except ImportError:
    sys.stderr.write("qvnp: PIL is not installed. 'pip install --break-system-packages pillow' or search for it in your system repositories\n")
    sys.exit(1)

# ---- hardcoded control points ------------------------------------------
DEFAULT_FPS = 25.0
MAX_CATCHUP_DROP = 25
AUDIO_START_GRACE = 0.01
FFMPEG_SCALE_FLAGS = "flags=lanczos"
FFMPEG_SCALE_FLAGS_PIXELIZE = "flags=neighbor"  # --pixelize: point-sampling
PIXEL_BLEND_MIN = 1
PIXEL_BLEND_MAX = 8
DEFAULT_PIXEL_BLEND = 8

HIGHLIGHT_RADIUS_BASE = 8
HIGHLIGHT_RADIUS_STEP = 4
HIGHLIGHT_DIFF_MIN = 180

HIGHLIGHT_CROSSHAIR_EPS = 0.08

# --------------------------------------------------------------------------

# Per-effect defaults when the effect flag is NOT given explicitly.
EFFECT_NORMAL_DEFAULTS = {
    "gamma": 1.0, "brightness": 0.0, "contrast": 1.0, "saturation": 1.0,
    "highlight": 0, "red": 1.0, "green": 1.0, "blue": 1.0,
}
# Preset applied by -e/--enhance to whichever of the above the user did NOT
# explicitly set themselves. Tune here.
EFFECT_ENHANCE_DEFAULTS = {
    "gamma": 1.0, "brightness": 0.0, "contrast": 1.0, "saturation": 1.0,
    "highlight": 4, "red": 1.0, "green": 1.0, "blue": 1.0,
}
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

def is_static_image(path):
    try:
        with Image.open(path) as img:
            return img.format in {"JPEG", "PNG", "BMP", "WEBP", "TIFF"}
    except Exception:
        return False

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
    src_fps = float(num) / float(den) if float(den) else "undefined "
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



def build_filter_complex(fps: float, src_w: int, src_h: int, target_w: int, target_h: int,
                          highlight: int, pixelize: bool,
                          gamma: float, brightness: float, contrast: float, saturation: float,
                          red: float, green: float, blue: float):
    stages = []
    cur = "s0"
    stages.append(f"[0:v]fps={fps}[{cur}]")

    eq_parts = []
    if gamma != 1.0:
        eq_parts.append(f"gamma={gamma}")
    if brightness != 0.0:
        eq_parts.append(f"brightness={brightness}")
    if contrast != 1.0:
        eq_parts.append(f"contrast={contrast}")
    if saturation != 1.0:
        eq_parts.append(f"saturation={saturation}")
    if eq_parts:
        nxt = "s2"
        stages.append(f"[{cur}]eq=" + ":".join(eq_parts) + f"[{nxt}]")
        cur = nxt

    if (red, green, blue) != (1.0, 1.0, 1.0):
        nxt = "s3"
        stages.append(f"[{cur}]colorchannelmixer=rr={red}:gg={green}:bb={blue}[{nxt}]")
        cur = nxt


    needs_python_scale = bool(highlight) and pixelize
    if needs_python_scale:
        stages.append(f"[{cur}]null[vout]")
        out_w, out_h = src_w, src_h
    else:
        scale_flags = FFMPEG_SCALE_FLAGS_PIXELIZE if pixelize else FFMPEG_SCALE_FLAGS
        stages.append(f"[{cur}]scale={target_w}:{target_h}:{scale_flags}[vout]")
        out_w, out_h = target_w, target_h

    return ";".join(stages), out_w, out_h, needs_python_scale


def _find_highlight_candidates(img: Image.Image, highlight_level: int):
    radius = max(1, round(HIGHLIGHT_RADIUS_BASE + HIGHLIGHT_RADIUS_STEP * (highlight_level - 1)))

    gray = img.convert("L")
    local_mean = gray.filter(ImageFilter.BoxBlur(radius))
    diff = ImageChops.difference(gray, local_mean)
    mask = diff.point(lambda v: 255 if v > HIGHLIGHT_DIFF_MIN else 0)

    w, h = mask.size
    mask_px = mask.load()
    diff_px = diff.load()

    candidates = []
    for y in range(h):
        for x in range(w):
            if mask_px[x, y]:
                candidates.append((x, y, diff_px[x, y]))
    return candidates


def smart_downscale(img: Image.Image, target_w: int, target_h: int, highlight_level: int) -> Image.Image:
    base = img.resize((target_w, target_h), Image.NEAREST)
    if not highlight_level:
        return base

    candidates = _find_highlight_candidates(img, highlight_level)
    if not candidates:
        return base

    w, h = img.size
    sx, sy = w / target_w, h / target_h
    base_px = base.load()
    src_px = img.load()

    best = {}
    for x, y, strength in candidates:
        out_x = (x + 0.5) / sx - 0.5
        out_y = (y + 0.5) / sy - 0.5

        fx = out_x - math.floor(out_x)
        fy = out_y - math.floor(out_y)
        if abs(fx - 0.5) < HIGHLIGHT_CROSSHAIR_EPS and abs(fy - 0.5) < HIGHLIGHT_CROSSHAIR_EPS:
            out_x += 1.0 / sx
            out_y += 1.0 / sy

        ox = min(max(round(out_x), 0), target_w - 1)
        oy = min(max(round(out_y), 0), target_h - 1)

        prev = best.get((ox, oy))
        if prev is None or strength > prev[0]:
            best[(ox, oy)] = (strength, src_px[x, y])

    for (ox, oy), (_, color) in best.items():
        base_px[ox, oy] = color

    return base

def start_ffmpeg(path, filter_complex, w, h):
    cmd = [
        "ffmpeg", "-loglevel", "error", "-i", path,
        "-filter_complex", filter_complex, "-map", "[vout]",
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

def probe_duration(path: str) -> float:
    data = ffprobe_json(path, ["-show_entries", "format=duration"])
    duration = data.get("format", {}).get("duration")
    return float(duration) if duration else 0.0

def format_properties(path: str, src_w: int, src_h: int, src_fps, has_audio: bool) -> str:
    size = os.path.getsize(path)
    duration = probe_duration(path)

    if size >= 1024 ** 4:
        size_str = f"{size / 1024 ** 4:.4f} TB"
    elif size >= 1024 ** 3:
        size_str = f"{size / 1024 ** 3:.2f} GB"
    elif size >= 1024 ** 2:
        size_str = f"{size / 1024 ** 2:.2f} MB"
    elif size >= 1024:
        size_str = f"{size / 1024:.1f} KB"
    else:
        size_str = f"{size} B"

    if duration >= 3600:
        dur_str = f"{int(duration // 3600)}:{int((duration % 3600) // 60):02d}:{int(duration % 60):02d}"
    elif duration >= 60:
        dur_str = f"{int(duration // 60)}:{int(duration % 60):02d}"
    else:
        dur_str = f"{duration:.1f}s"

    audio_str = "audio" if has_audio else "no audio"

    if src_fps is float:
        return f"| {src_w}x{src_h} | {src_fps:.2f}fps | {dur_str} | {size_str} | {audio_str} |"
    else:
        return f"| {src_w}x{src_h} | static image | {size_str} | {audio_str} |"

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
    p.add_argument("-W", "--width", type=int, help="width in symbols, terminal's width by default")
    p.add_argument("-H", "--height", type=int, help="height in symbols, terminal's height minus one by default")
    p.add_argument("-f", "--fps", type=float, default=DEFAULT_FPS, help=f"target fps (by default: {DEFAULT_FPS})")
    p.add_argument("-m", "--mode", choices=["auto", "half", "full"], default="auto",
                    help="half = more pixels, full = less pixels, full works better in tty, half in graphical terminal")
    p.add_argument("--font-aspect", type=float, default=2.0, help="height/width of the font symbol (for '--mode full')")
    p.add_argument("-na", "--no-audio", action="store_true", help="launch without sound")
    p.add_argument("-l", "--loop", action="store_true", help="play file cyclically until stopped manually")
    p.add_argument("-e", "--enhance", action="store_true",
                    help="turn on a sensible default set of effects at once (highlight, brightness, contrast, "
                         "saturation, gamma - see EFFECT_ENHANCE_DEFAULTS) instead of picking each one by hand; "
                         "any effect flag you also pass explicitly overrides its enhance value")
    p.add_argument("--highlight", type=int, choices=range(1, 9), metavar="1-8", default=None,
                    help="grow small isolated bright/dark details (e.g. stars in the sky) before downscale, "
                         "so pixelize point-sampling doesn't skip over them; only affects spots that stand out "
                         "against a near-uniform local background, large/flat areas are left untouched; "
                         "value = strength (dilation passes / mask blur radius) (default: 0, or "
                         f"{EFFECT_ENHANCE_DEFAULTS['highlight']} with --enhance)")
    p.add_argument("-np", "--no-pixelize", dest="pixelize", action="store_false", default=True,
                    help="disable pixelize mode (use smooth lanczos downscale + full box-average instead of point-sampling); pixelize is ON by default")
    p.add_argument("-g", "--gamma", type=float, default=None,
                    help=f"gamma correction, 1.0 = no change (default: 1.0, or {EFFECT_ENHANCE_DEFAULTS['gamma']} with --enhance)")
    p.add_argument("-b", "--brightness", type=float, default=None,
                    help=f"brightness offset, range -1..1, 0 = no change (default: 0.0, or {EFFECT_ENHANCE_DEFAULTS['brightness']} with --enhance)")
    p.add_argument("-c", "--contrast", type=float, default=None,
                    help=f"contrast, range -2..2, 1.0 = no change (default: 1.0, or {EFFECT_ENHANCE_DEFAULTS['contrast']} with --enhance)")
    p.add_argument("-s", "--saturation", type=float, default=None,
                    help=f"saturation, range 0..3, 1.0 = no change (default: 1.0, or {EFFECT_ENHANCE_DEFAULTS['saturation']} with --enhance)")
    p.add_argument("--red", type=float, default=None, help="red channel multiplier (default: 1.0)")
    p.add_argument("--green", type=float, default=None, help="green channel multiplier (default: 1.0)")
    p.add_argument("--blue", type=float, default=None, help="blue channel multiplier (default: 1.0)")
    p.add_argument("-p", "--show-file-properties", action="store_true",
                   help="show resolution, fps, duration, file size and audio status in the top line")
    # p.add_argument("--pixel-blend", type=int, choices=range(PIXEL_BLEND_MIN, PIXEL_BLEND_MAX + 1),
    #                 metavar=f"{PIXEL_BLEND_MIN}-{PIXEL_BLEND_MAX}", default=DEFAULT_PIXEL_BLEND,
    #                 help=f"only in pixelize mode: how much to mix the point-sample with the arithmetic mean of the block; "
    #                      f"{PIXEL_BLEND_MAX} = pure point-sample (sharpest), {PIXEL_BLEND_MIN} = fully averaged (smoothest) "
    #                      f"(default: {DEFAULT_PIXEL_BLEND})")
    args = p.parse_args()

    # --enhance only makes sense for static images, silently disable for video
    if args.enhance and not is_static_image(args.path):
        args.enhance = False
        print("No enhance for video yet.")

    # Resolve effect flags: explicit CLI value > --enhance preset > normal default.
    for _name, _normal in EFFECT_NORMAL_DEFAULTS.items():
        _val = getattr(args, _name)
        if _val is None:
            _val = EFFECT_ENHANCE_DEFAULTS[_name] if args.enhance else _normal
        setattr(args, _name, _val)

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
    if args.show_file_properties:
        max_rows = max(1, max_rows - 1)

    src_w, src_h, _src_fps = probe_video(args.path)
    target_w, target_h = fit_size(src_w, src_h, max_cols, max_rows, col_mult, row_mult)
    frame_bytes = target_w * target_h * 3

    audio_available = has_audio(args.path) and not args.no_audio

    filter_complex, ff_w, ff_h, needs_python_scale = build_filter_complex(
            args.fps, src_w, src_h, target_w, target_h, args.highlight, args.pixelize,
            args.gamma, args.brightness, args.contrast, args.saturation,
            args.red, args.green, args.blue,
        )
    frame_bytes = ff_w * ff_h * 3

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
            ffmpeg_proc = start_ffmpeg(args.path, filter_complex, ff_w, ff_h)
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

                img = Image.frombytes("RGB", (ff_w, ff_h), raw)
                if needs_python_scale:
                    img = smart_downscale(img, target_w, target_h, args.highlight)
                body = render_full(img, row_mult, args.pixelize, blend_frac) if use_full else render_half(img)
                if args.show_file_properties:
                    if is_static_image(args.path):
                        props = format_properties(args.path, src_w, src_h, "no ", audio_available)
                    else:
                        props = format_properties(args.path, src_w, src_h, _src_fps, audio_available)
                    sys.stdout.write(HOME + props + "\n" + body)
                else:
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

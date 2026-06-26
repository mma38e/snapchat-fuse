#!/usr/bin/env python3
import json
import os
import shutil
import subprocess
import sys
from concurrent.futures import ProcessPoolExecutor, as_completed
from pathlib import Path

from PIL import Image
from tqdm import tqdm

INPUT_DIR = Path("/input")
OUTPUT_DIR = Path("/output")


def output_path(base: str, ext: str) -> Path:
    return OUTPUT_DIR / f"{base}{ext}"


def even(n: int) -> int:
    return n if n % 2 == 0 else n - 1


def ffmpeg_encode(args: list, out: Path) -> str:
    """Run FFmpeg, trying h264_nvenc first then falling back to libx264."""
    for codec, extra in [
        ("h264_nvenc", ["-preset", "p4", "-rc", "vbr", "-cq", "20"]),
        ("libx264",    ["-preset", "fast", "-crf", "18"]),
    ]:
        cmd = ["ffmpeg", "-y"] + args + ["-c:v", codec] + extra + ["-c:a", "copy", str(out)]
        r = subprocess.run(cmd, capture_output=True, text=True)
        if r.returncode == 0:
            return "ok"
        if codec == "h264_nvenc":
            continue  # try fallback
        return f"ERROR: {r.stderr[-300:]}"
    return "ERROR: all codecs failed"


def get_video_dims(path: Path) -> tuple[int, int]:
    """Return display dimensions, accounting for rotation metadata."""
    r = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "v:0",
         "-show_entries", "stream=width,height",
         "-show_entries", "stream_tags=rotate",
         "-of", "json", str(path)],
        capture_output=True, text=True, check=True,
    )
    stream = json.loads(r.stdout)["streams"][0]
    w, h = stream["width"], stream["height"]
    rotate = int(stream.get("tags", {}).get("rotate", "0"))
    if rotate in (90, 270):
        return h, w
    return w, h


def fuse_jpg(base: str) -> tuple[str, str]:
    try:
        main = Image.open(INPUT_DIR / f"{base}-main.jpg").convert("RGB")
        overlay = Image.open(INPUT_DIR / f"{base}-overlay.png").convert("RGBA")
        if overlay.size != main.size:
            overlay = overlay.resize(main.size, Image.LANCZOS)
        main.paste(overlay, (0, 0), overlay)
        main.save(output_path(base, ".jpg"), "JPEG", quality=95, subsampling=0)
        return base, "ok"
    except Exception as e:
        return base, f"ERROR: {e}"


def fuse_mp4(base: str) -> tuple[str, str]:
    try:
        main_path = INPUT_DIR / f"{base}-main.mp4"
        overlay_path = INPUT_DIR / f"{base}-overlay.png"
        out = output_path(base, ".mp4")

        vw, vh = get_video_dims(main_path)
        ov_img = Image.open(str(overlay_path))
        ow, oh = ov_img.size

        if (vw, vh) == (ow, oh):
            # Same size — overlay directly
            filt = f"[1:v]scale={vw}:{vh}[ov];[0:v][ov]overlay=0:0"
        else:
            # Scale overlay to match video dimensions (same AR in most cases)
            scale = min(vw / ow, vh / oh)
            sw, sh = even(int(ow * scale)), even(int(oh * scale))
            x_off, y_off = (vw - sw) // 2, (vh - sh) // 2
            filt = f"[1:v]scale={sw}:{sh}[ov];[0:v][ov]overlay={x_off}:{y_off}"

        return base, ffmpeg_encode(
            ["-i", str(main_path), "-i", str(overlay_path), "-filter_complex", filt],
            out,
        )
    except Exception as e:
        return base, f"ERROR: {e}"


def copy_main(base: str, ext: str) -> tuple[str, str]:
    try:
        shutil.copy2(INPUT_DIR / f"{base}-main{ext}", output_path(base, ext))
        return base, "ok"
    except Exception as e:
        return base, f"ERROR: {e}"


def main():
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    files = list(INPUT_DIR.iterdir())
    mains = [f for f in files if "-main." in f.name]

    jpg_with_overlay, mp4_with_overlay, jpg_only, mp4_only = [], [], [], []
    for f in mains:
        base = f.stem.replace("-main", "")
        ext = f.suffix
        has_overlay = (INPUT_DIR / f"{base}-overlay.png").exists()
        if ext == ".jpg":
            (jpg_with_overlay if has_overlay else jpg_only).append(base)
        elif ext == ".mp4":
            (mp4_with_overlay if has_overlay else mp4_only).append(base)

    print(f"JPG + overlay:  {len(jpg_with_overlay)}")
    print(f"MP4 + overlay:  {len(mp4_with_overlay)}")
    print(f"JPG only:       {len(jpg_only)}")
    print(f"MP4 only:       {len(mp4_only)}")
    print()

    errors = []

    print("Fusing JPGs...")
    workers = min(8, os.cpu_count() or 4)
    with ProcessPoolExecutor(max_workers=workers) as pool:
        futures = {pool.submit(fuse_jpg, b): b for b in jpg_with_overlay}
        for fut in tqdm(as_completed(futures), total=len(jpg_with_overlay), unit="img"):
            base, status = fut.result()
            if status != "ok":
                errors.append((base, status))

    print("Copying JPGs (no overlay)...")
    for base in tqdm(jpg_only, unit="img"):
        _, status = copy_main(base, ".jpg")
        if status != "ok":
            errors.append((base, status))

    print("Fusing MP4s...")
    for base in tqdm(mp4_with_overlay, unit="vid"):
        _, status = fuse_mp4(base)
        if status != "ok":
            errors.append((base, status))

    print("Copying MP4s (no overlay)...")
    for base in tqdm(mp4_only, unit="vid"):
        _, status = copy_main(base, ".mp4")
        if status != "ok":
            errors.append((base, status))

    print(f"\nDone. {len(errors)} error(s).")
    if errors:
        for b, e in errors:
            print(f"  {b}: {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()

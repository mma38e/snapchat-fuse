#!/usr/bin/env python3
import json
import os
import re
import shutil
import subprocess
import sys
from collections import defaultdict
from concurrent.futures import ProcessPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path

import piexif
from PIL import Image
from tqdm import tqdm

INPUT_DIR = Path("/input")
OUTPUT_DIR = Path("/output")
METADATA_FILE = Path("/metadata.json")


# ---------------------------------------------------------------------------
# Metadata loading & matching
# ---------------------------------------------------------------------------

def _parse_entry(entry: dict) -> dict:
    dt = datetime.strptime(entry["Date"], "%Y-%m-%d %H:%M:%S UTC").replace(tzinfo=timezone.utc)
    lat = lon = None
    m = re.search(r"([-\d.]+),\s*([-\d.]+)", entry.get("Location", ""))
    if m:
        lat_v, lon_v = float(m.group(1)), float(m.group(2))
        if lat_v != 0.0 or lon_v != 0.0:
            lat, lon = lat_v, lon_v
    return {"dt": dt, "lat": lat, "lon": lon}


def load_metadata() -> dict[str, dict]:
    """Match memories_history.json entries to filenames. Returns base->meta dict."""
    if not METADATA_FILE.exists():
        return {}

    with open(METADATA_FILE) as f:
        entries = json.load(f)["Saved Media"]

    # Group JSON entries by (date, ext), ascending time within each group
    json_groups: dict[tuple, list] = defaultdict(list)
    for e in reversed(entries):          # JSON is newest-first; reverse → oldest-first
        date = e["Date"].split(" ")[0]
        ext = "mp4" if e["Media Type"] == "Video" else "jpg"
        json_groups[(date, ext)].append(_parse_entry(e))

    # Group files by (date, ext), sorted alphabetically within each group
    file_groups: dict[tuple, list] = defaultdict(list)
    for f in INPUT_DIR.iterdir():
        if "-main." not in f.name:
            continue
        date = f.name.split("_")[0]
        ext = f.suffix.lstrip(".")
        file_groups[(date, ext)].append(f.stem.replace("-main", ""))
    for key in file_groups:
        file_groups[key].sort()

    # Match: only when counts agree
    result: dict[str, dict] = {}
    for key, bases in file_groups.items():
        jlist = json_groups.get(key, [])
        if len(bases) == len(jlist):
            for base, meta in zip(bases, jlist):
                result[base] = meta

    matched = len(result)
    total = sum(len(v) for v in file_groups.values())
    print(f"Metadata matched: {matched}/{total} files")
    return result


# ---------------------------------------------------------------------------
# EXIF / container tag helpers
# ---------------------------------------------------------------------------

def _to_dms(deg: float) -> tuple:
    d = int(abs(deg))
    m_f = (abs(deg) - d) * 60
    m = int(m_f)
    s = int((m_f - m) * 60 * 10000)
    return (d, 1), (m, 1), (s, 10000)


def write_jpg_exif(path: Path, meta: dict) -> None:
    try:
        try:
            exif = piexif.load(str(path))
        except Exception:
            exif = {"0th": {}, "Exif": {}, "GPS": {}, "Interop": {}, "1st": {}}

        dt_str = meta["dt"].strftime("%Y:%m:%d %H:%M:%S").encode()
        exif["0th"][piexif.ImageIFD.DateTime] = dt_str
        exif["Exif"][piexif.ExifIFD.DateTimeOriginal] = dt_str
        exif["Exif"][piexif.ExifIFD.DateTimeDigitized] = dt_str

        if meta["lat"] is not None:
            lat, lon = meta["lat"], meta["lon"]
            exif["GPS"][piexif.GPSIFD.GPSLatitudeRef] = b"N" if lat >= 0 else b"S"
            exif["GPS"][piexif.GPSIFD.GPSLatitude] = _to_dms(lat)
            exif["GPS"][piexif.GPSIFD.GPSLongitudeRef] = b"E" if lon >= 0 else b"W"
            exif["GPS"][piexif.GPSIFD.GPSLongitude] = _to_dms(lon)

        piexif.insert(piexif.dump(exif), str(path))
    except Exception:
        pass  # never fail the job over metadata


def _mp4_meta_args(meta: dict | None) -> list[str]:
    if not meta:
        return []
    args = ["-metadata", f"creation_time={meta['dt'].strftime('%Y-%m-%dT%H:%M:%SZ')}"]
    if meta["lat"] is not None:
        args += ["-metadata", f"location={meta['lat']:+.6f}{meta['lon']:+.6f}/"]
        args += ["-metadata", f"location-eng={meta['lat']:+.6f}{meta['lon']:+.6f}/"]
    return args


# ---------------------------------------------------------------------------
# Core processing
# ---------------------------------------------------------------------------

def output_path(base: str, ext: str) -> Path:
    return OUTPUT_DIR / f"{base}{ext}"


def even(n: int) -> int:
    return n if n % 2 == 0 else n - 1


def ffmpeg_encode(args: list, out: Path) -> str:
    for codec, extra in [
        ("h264_nvenc", ["-preset", "p4", "-rc", "vbr", "-cq", "20"]),
        ("libx264",    ["-preset", "fast", "-crf", "18"]),
    ]:
        cmd = ["ffmpeg", "-y"] + args + ["-c:v", codec] + extra + ["-c:a", "copy", str(out)]
        r = subprocess.run(cmd, capture_output=True, text=True)
        if r.returncode == 0:
            return "ok"
        if codec == "h264_nvenc":
            continue
        return f"ERROR: {r.stderr[-300:]}"
    return "ERROR: all codecs failed"


def get_video_dims(path: Path) -> tuple[int, int]:
    r = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "v:0",
         "-show_entries", "stream=width,height",
         "-show_entries", "stream_tags=rotate",
         "-of", "json", str(path)],
        capture_output=True, text=True, check=True,
    )
    stream = json.loads(r.stdout)["streams"][0]
    w, h = stream["width"], stream["height"]
    if int(stream.get("tags", {}).get("rotate", "0")) in (90, 270):
        return h, w
    return w, h


def fuse_jpg(base: str, meta: dict | None) -> tuple[str, str]:
    try:
        main = Image.open(INPUT_DIR / f"{base}-main.jpg").convert("RGB")
        overlay = Image.open(INPUT_DIR / f"{base}-overlay.png").convert("RGBA")
        if overlay.size != main.size:
            overlay = overlay.resize(main.size, Image.LANCZOS)
        main.paste(overlay, (0, 0), overlay)
        out = output_path(base, ".jpg")
        main.save(out, "JPEG", quality=95, subsampling=0)
        if meta:
            write_jpg_exif(out, meta)
        return base, "ok"
    except Exception as e:
        return base, f"ERROR: {e}"


def fuse_mp4(base: str, meta: dict | None) -> tuple[str, str]:
    try:
        main_path = INPUT_DIR / f"{base}-main.mp4"
        overlay_path = INPUT_DIR / f"{base}-overlay.png"
        out = output_path(base, ".mp4")

        vw, vh = get_video_dims(main_path)
        ov_img = Image.open(str(overlay_path))
        ow, oh = ov_img.size

        if (vw, vh) == (ow, oh):
            filt = f"[1:v]scale={vw}:{vh}[ov];[0:v][ov]overlay=0:0"
        else:
            scale = min(vw / ow, vh / oh)
            sw, sh = even(int(ow * scale)), even(int(oh * scale))
            x_off, y_off = (vw - sw) // 2, (vh - sh) // 2
            filt = f"[1:v]scale={sw}:{sh}[ov];[0:v][ov]overlay={x_off}:{y_off}"

        return base, ffmpeg_encode(
            ["-i", str(main_path), "-i", str(overlay_path),
             "-filter_complex", filt] + _mp4_meta_args(meta),
            out,
        )
    except Exception as e:
        return base, f"ERROR: {e}"


def copy_main(base: str, ext: str, meta: dict | None) -> tuple[str, str]:
    try:
        src = INPUT_DIR / f"{base}-main{ext}"
        dst = output_path(base, ext)
        if ext == ".mp4" and meta:
            r = subprocess.run(
                ["ffmpeg", "-y", "-i", str(src)]
                + _mp4_meta_args(meta)
                + ["-c", "copy", str(dst)],
                capture_output=True, text=True,
            )
            if r.returncode != 0:
                # fall back to plain copy
                shutil.copy2(src, dst)
        else:
            shutil.copy2(src, dst)
            if ext == ".jpg" and meta:
                write_jpg_exif(dst, meta)
        return base, "ok"
    except Exception as e:
        return base, f"ERROR: {e}"


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    metadata = load_metadata()

    files = list(INPUT_DIR.iterdir())
    mains = [f for f in files if "-main." in f.name]

    jpg_overlay, mp4_overlay, jpg_only, mp4_only = [], [], [], []
    for f in mains:
        base = f.stem.replace("-main", "")
        ext = f.suffix
        has_overlay = (INPUT_DIR / f"{base}-overlay.png").exists()
        if ext == ".jpg":
            (jpg_overlay if has_overlay else jpg_only).append(base)
        elif ext == ".mp4":
            (mp4_overlay if has_overlay else mp4_only).append(base)

    print(f"JPG + overlay:  {len(jpg_overlay)}")
    print(f"MP4 + overlay:  {len(mp4_overlay)}")
    print(f"JPG only:       {len(jpg_only)}")
    print(f"MP4 only:       {len(mp4_only)}")
    print()

    errors = []

    print("Fusing JPGs...")
    workers = min(8, os.cpu_count() or 4)
    with ProcessPoolExecutor(max_workers=workers) as pool:
        futures = {pool.submit(fuse_jpg, b, metadata.get(b)): b for b in jpg_overlay}
        for fut in tqdm(as_completed(futures), total=len(jpg_overlay), unit="img"):
            base, status = fut.result()
            if status != "ok":
                errors.append((base, status))

    print("Copying JPGs (no overlay)...")
    for base in tqdm(jpg_only, unit="img"):
        _, status = copy_main(base, ".jpg", metadata.get(base))
        if status != "ok":
            errors.append((base, status))

    print("Fusing MP4s...")
    for base in tqdm(mp4_overlay, unit="vid"):
        _, status = fuse_mp4(base, metadata.get(base))
        if status != "ok":
            errors.append((base, status))

    print("Copying MP4s (no overlay)...")
    for base in tqdm(mp4_only, unit="vid"):
        _, status = copy_main(base, ".mp4", metadata.get(base))
        if status != "ok":
            errors.append((base, status))

    print(f"\nDone. {len(errors)} error(s).")
    if errors:
        for b, e in errors:
            print(f"  {b}: {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()

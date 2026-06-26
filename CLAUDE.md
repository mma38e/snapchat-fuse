# CLAUDE.md — snapchat-fuse

Context and lessons learned for working on this codebase.

---

## What this does

Processes a Snapchat data takeout. The `memories/` folder splits every snap into separate files:
- `<date>_<uuid>-main.jpg` / `-main.mp4` — the raw photo or video
- `<date>_<uuid>-overlay.png` — the RGBA overlay (filters, stickers, text)

`fuse.py` composites each overlay onto its source file, optionally embeds date/GPS metadata from `json/memories_history.json`, and writes output to a separate folder.

## Branch structure

| Branch | What it adds |
|--------|-------------|
| `main` | Core overlay fusion (JPG + MP4), GPU encoding, rotation handling |
| `embed-metadata` | Date + GPS embedding from `memories_history.json` |

`embed-metadata` is the current working branch — it includes everything in `main` plus metadata.

## Docker setup

Base image: `nvidia/cuda:12.3.0-base-ubuntu22.04`

The CUDA base is required for h264_nvenc — Ubuntu's apt ffmpeg package has NVENC compiled in but needs the NVIDIA encode runtime library at launch.

### WSL2 NVENC fix (critical)

On WSL2, `libnvidia-encode.so.1` lives at `/usr/lib/wsl/lib`, **not** `/usr/local/nvidia/lib64` where the nvidia-container-toolkit mounts host driver libs on native Linux. Without explicitly mounting and exposing this path, `h264_nvenc` fails silently at runtime and falls back to `libx264` with no visible error — you only notice by checking `nvidia-smi dmon` and seeing the `enc` column stay at 0%.

Two changes required:
1. `Dockerfile`: `ENV LD_LIBRARY_PATH=/usr/lib/wsl/lib:/usr/local/nvidia/lib64:/usr/local/nvidia/lib`
2. `docker run`: `-v /usr/lib/wsl/lib:/usr/lib/wsl/lib:ro`

`run.sh` detects WSL2 by checking `[ -d "/usr/lib/wsl/lib" ]` and adds the flag automatically.

To verify GPU encoding is actually active: run `nvidia-smi dmon -s um -d 1` alongside the encode and watch the `enc` column — it should show non-zero values during video processing.

## Video encoding

`ffmpeg_encode()` tries codecs in order with automatic fallback:
1. `h264_nvenc` — GPU (RTX 2070 Super, ~10× faster)
2. `libx264` — CPU fallback

On macOS there is no NVENC. If adding macOS support, insert `h264_videotoolbox` between the two (Apple Silicon and Intel both support VideoToolbox).

## Overlay handling

### Rotation-aware dimensions

Many Snapchat videos are stored in landscape orientation (e.g. 960×540) but have a `rotate: 90` metadata tag, meaning they display as portrait (540×960). FFmpeg applies autorotation by default on decode.

`get_video_dims()` reads the rotation tag via ffprobe JSON output and swaps width/height for 90°/270° rotations. **Always use this function** — never read raw `stream.width`/`stream.height` directly for overlay scaling.

```python
# Wrong — uses stored dimensions, overlay will be wrong for rotated videos
r = subprocess.run(["ffprobe", ... "-of", "csv=p=0", ...])
w, h = r.stdout.strip().split(",")[:2]

# Correct — accounts for rotation
vw, vh = get_video_dims(path)  # returns display dimensions
```

ffprobe `csv=p=0` output has a trailing comma (`640,384,`) — always use `split(",")[:2]` or the JSON format.

### Option A vs Option B (decided: Option A)

For landscape-stored/portrait-display videos with portrait overlays there were two options considered:

- **Option A**: scale overlay to fit within the video's display frame — output stays at video resolution, overlay is centered. Chosen because it keeps file sizes close to the original.
- **Option B**: letterbox the video into the overlay's canvas — output is at overlay resolution (often 2–4× more pixels) with no real quality gain.

Option A was chosen. The `fuse_mp4` function implements it.

### FFmpeg pad filter gotcha

When using `scale=W:H:force_original_aspect_ratio=decrease` followed by `pad=W:H:...`, rounding in the scale step can produce dimensions 1px larger than the pad target, causing:
```
[Parsed_pad_1] Padded dimensions cannot be smaller than input dimensions.
```

Fix: compute scale dimensions explicitly in Python using `even()` (round down to nearest even number for libx264/nvenc compatibility), then pass explicit pixel values to FFmpeg rather than using `force_original_aspect_ratio`.

### Infinite stream bug

The `color=black:size=WxH:rate=30` FFmpeg source filter generates an infinite stream. When used as a canvas background with `overlay`, FFmpeg never terminates. Always add `-shortest` to the command when using a synthetic source. Only relevant in `make_samples.py` (Option B implementation) — `fuse.py` doesn't use this pattern.

## Metadata matching

`memories_history.json` has 1,636 entries; the `memories/` folder has 1,626 files (10 extras in JSON are snaps partially absent from the takeout, mostly early 2016).

The JSON has **no filenames** — only `Date`, `Media Type`, and `Location`. Matching is done by grouping on `(date, ext)` and pairing in order within each group.

- **1,612 files matched** (~99%): groups where file count == JSON entry count
- **14 files unmatched**: days with count mismatches — skipped gracefully, no metadata written

For same-day same-type multiples the JSON is paired in ascending time order; files are paired alphabetically by UUID. Since UUIDs are random, the per-file time precision within a day is approximate — but the date and general location are always correct.

GPS coordinates are street-level precision (avg 5.6 decimal places ≈ ~1m accuracy).

### EXIF / container tag details

| | JPG | MP4 |
|--|-----|-----|
| Date | `DateTimeOriginal`, `DateTimeDigitized`, `DateTime` via piexif | `creation_time` via FFmpeg `-metadata` |
| GPS | `GPSLatitude`, `GPSLongitude`, `GPSLatitudeRef`, `GPSLongitudeRef` via piexif | `location`, `location-eng` (ISO 6709 format `±DD.DDDD±DDD.DDDD/`) |

For copy-only MP4s (no overlay), use `ffmpeg -c copy -metadata ...` rather than `shutil.copy2` — it's fast (stream copy, no re-encode) and correctly writes container tags.

piexif's `insert()` method writes EXIF into an existing JPEG in-place. If the file has no existing EXIF, construct a fresh dict with all four IFDs (`0th`, `Exif`, `GPS`, `Interop`). Always wrap in try/except — never fail a job over metadata.

## Testing

The scratchpad test set lives at:
`/tmp/claude-1000/-mnt-e-Snapchat-memories-Snapchat-Memories/.../scratchpad/test_input/`

It contains 8 files covering all 4 processing paths:
- JPG + overlay (including one size-mismatch pair)
- JPG only
- MP4 + overlay (landscape with rotate:90 tag)
- MP4 only

Standard smoke test command:
```bash
docker run --rm --gpus all \
  -v /usr/lib/wsl/lib:/usr/lib/wsl/lib:ro \
  -v "$SCRATCH/test_input":/input:ro \
  -v "$SCRATCH/test_output":/output:rw \
  -v "/path/to/json/memories_history.json":/metadata.json:ro \
  snapchat-fuse
```

After running, verify with:
```bash
# JPG EXIF
docker run --rm --entrypoint bash snapchat-fuse -c \
  'python3 -c "import piexif; e=piexif.load(\"/output/file.jpg\"); print(e[\"Exif\"])"'

# MP4 tags
ffprobe -v quiet -show_entries format_tags=creation_time,location -of default output.mp4
```

## Future work considered (not yet built)

- **`python3-run` branch**: native macOS/Windows support without Docker. Needs argparse for paths, `h264_videotoolbox` codec added to fallback chain for macOS, and step-by-step README for non-CLI users. Main barrier: FFmpeg install (Homebrew required on macOS).
- **GUI app**: PyInstaller + CustomTkinter for a double-click `.app`. Needs bundled static FFmpeg binary (~60–100MB), `multiprocessing.freeze_support()` call in entry point (required for frozen PyInstaller apps on macOS), and Apple Developer signing ($99/year) or right-click-Open workaround for Gatekeeper.
- **Self-hosted webapp**: high complexity — multi-GB uploads, async job queue (Celery + Redis), significant privacy concerns for personal photo/video data. Not recommended over the GUI or local approach.

## Data facts (this specific takeout)

- 1,626 total files: 536 JPGs + 1,090 MP4s
- 881 overlays: 358 pair with JPGs, 523 pair with MP4s
- 745 files have no overlay (copied as-is)
- Date range: 2016-07-20 to 2023-03-15
- 25 distinct video/overlay resolution combinations
- Most landscape videos have `rotate: 90` tag — they're portrait snaps shot on a phone

# snapchat-fuse

Fuses Snapchat memory overlays (filters, stickers, text) onto their source photos and videos from a Snapchat data takeout, and optionally embeds the original date and GPS location into each file's metadata.

## Background

A Snapchat takeout contains a `memories/` folder where each snap is split into two files:

| File | Description |
|------|-------------|
| `<date>_<uuid>-main.jpg` | Original photo |
| `<date>_<uuid>-main.mp4` | Original video |
| `<date>_<uuid>-overlay.png` | RGBA overlay (filter, text, stickers) |

The takeout also includes `json/memories_history.json` which holds the capture timestamp and GPS coordinates for each snap.

This tool composites each overlay onto its source file, embeds the original date and location metadata, and writes the results to an output folder. Files with no overlay are copied with metadata embedded.

## Requirements

- Docker
- NVIDIA GPU + `nvidia-container-toolkit` (optional but recommended for video encoding speed)

## Usage

### Build

```bash
docker build -t snapchat-fuse .
```

### Run

```bash
# With NVIDIA GPU + metadata embedding (recommended)
docker run --rm --gpus all \
  -v /path/to/memories:/input:ro \
  -v /path/to/memories_fused:/output:rw \
  -v /path/to/json/memories_history.json:/metadata.json:ro \
  snapchat-fuse

# CPU only, no metadata
docker run --rm \
  -v /path/to/memories:/input:ro \
  -v /path/to/memories_fused:/output:rw \
  snapchat-fuse
```

> **WSL2 users:** Add `-v /usr/lib/wsl/lib:/usr/lib/wsl/lib:ro` to your `docker run` command. On WSL2 the NVIDIA encode library lives at `/usr/lib/wsl/lib` rather than the path the container toolkit normally injects, so without this mount `h264_nvenc` silently falls back to CPU encoding. To verify GPU is active, run `nvidia-smi dmon -s um -d 1` in a second terminal while encoding — the `enc` column should show non-zero values.

Or use the convenience script (auto-detects GPU and WSL2, optional json dir):

```bash
chmod +x run.sh
./run.sh /path/to/memories /path/to/memories_fused /path/to/json
```

## Output

- `memories_fused/<date>_<uuid>.jpg` — fused photo (overlay composited at 95% JPEG quality, EXIF date + GPS written)
- `memories_fused/<date>_<uuid>.mp4` — fused video (overlay burned in, audio copied, `creation_time` + `location` tags written)

Once imported into Photos, Google Photos, or Lightroom the files will appear at the correct date and on the map exactly where they were taken.

## Metadata embedding

When `memories_history.json` is provided, each output file receives:

| Tag | Photos (JPG) | Videos (MP4) |
|-----|-------------|--------------|
| Capture date | EXIF `DateTimeOriginal` | `creation_time` container tag |
| GPS location | EXIF `GPSLatitude` / `GPSLongitude` | `location` / `location-eng` container tags |

**Coverage:** ~99% of files can be matched. The JSON has no filenames — matching is done by date and media type. On days with multiple snaps of the same type, files are matched to JSON entries in chronological order; the date will always be correct and the GPS will be accurate to the day's location(s).

## How it works

**Photos (JPG):** Pillow alpha-composites the overlay onto the main image. If the overlay dimensions differ from the photo (different source resolution), the overlay is rescaled with LANCZOS before compositing. EXIF is written with `piexif`. Processed in parallel across CPU cores.

**Videos (MP4):** FFmpeg overlays the PNG onto each frame. Rotation metadata (`rotate` tag) is detected so that portrait videos stored in landscape orientation are handled correctly — the overlay is scaled to the display dimensions, not the stored dimensions. Encoding uses `h264_nvenc` (GPU) with automatic fallback to `libx264` (CPU). Metadata is passed as FFmpeg `-metadata` flags at encode time.

**No overlay:** Files are copied to the output folder with metadata written directly (EXIF insert for JPG, `ffmpeg -c copy` for MP4).

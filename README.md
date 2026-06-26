# snapchat-fuse

Fuses Snapchat memory overlays (filters, stickers, text) onto their source photos and videos from a Snapchat data takeout.

## Background

A Snapchat takeout contains a `memories/` folder where each snap is split into two files:

| File | Description |
|------|-------------|
| `<date>_<uuid>-main.jpg` | Original photo |
| `<date>_<uuid>-main.mp4` | Original video |
| `<date>_<uuid>-overlay.png` | RGBA overlay (filter, text, stickers) |

This tool composites each overlay onto its source file and writes the result to an output folder. Files with no overlay are copied as-is.

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
# With NVIDIA GPU (recommended)
docker run --rm --gpus all \
  -v /path/to/memories:/input:ro \
  -v /path/to/memories_fused:/output:rw \
  snapchat-fuse

# CPU only
docker run --rm \
  -v /path/to/memories:/input:ro \
  -v /path/to/memories_fused:/output:rw \
  snapchat-fuse
```

Or use the convenience script:

```bash
chmod +x run.sh
./run.sh /path/to/memories /path/to/memories_fused
```

## Output

- `memories_fused/<date>_<uuid>.jpg` — fused photo (overlay composited at 95% JPEG quality)
- `memories_fused/<date>_<uuid>.mp4` — fused video (overlay burned in, audio copied)

## How it works

**Photos (JPG):** Pillow alpha-composites the overlay onto the main image. If the overlay dimensions differ from the photo (different source resolution), the overlay is rescaled with LANCZOS before compositing. Processed in parallel across CPU cores.

**Videos (MP4):** FFmpeg overlays the PNG onto each frame. Rotation metadata (`rotate` tag) is detected so that portrait videos stored in landscape orientation are handled correctly — the overlay is scaled to the display dimensions, not the stored dimensions. Encoding uses `h264_nvenc` (GPU) with automatic fallback to `libx264` (CPU).

**No overlay:** Files are copied directly to the output folder.

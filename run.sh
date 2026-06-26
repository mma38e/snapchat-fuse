#!/usr/bin/env bash
# Usage: ./run.sh <memories_dir> <output_dir> [json_dir]
# Example: ./run.sh ~/Snapchat/memories ~/Snapchat/memories_fused ~/Snapchat/json
set -euo pipefail

MEMORIES="${1:?Usage: $0 <memories_dir> <output_dir> [json_dir]}"
OUTPUT="${2:?Usage: $0 <memories_dir> <output_dir> [json_dir]}"
JSON_DIR="${3:-}"

mkdir -p "$OUTPUT"

# Detect NVIDIA GPU
GPU_FLAG=""
if docker run --rm --gpus all nvidia/cuda:12.3.0-base-ubuntu22.04 nvidia-smi &>/dev/null 2>&1; then
  GPU_FLAG="--gpus all"
  echo "NVIDIA GPU detected — using h264_nvenc"
else
  echo "No GPU detected — falling back to libx264"
fi

# WSL2: mount NVIDIA encode/decode libs which live outside the normal container path
WSL_LIB_FLAG=""
if [ -d "/usr/lib/wsl/lib" ]; then
  WSL_LIB_FLAG="-v /usr/lib/wsl/lib:/usr/lib/wsl/lib:ro"
  echo "WSL2 detected — mounting NVIDIA libs from /usr/lib/wsl/lib"
fi

# Mount metadata JSON if json_dir provided and file exists
META_FLAG=""
JSON_FILE="${JSON_DIR}/memories_history.json"
if [ -n "$JSON_DIR" ] && [ -f "$JSON_FILE" ]; then
  META_FLAG="-v $(realpath "$JSON_FILE"):/metadata.json:ro"
  echo "Metadata: $(realpath "$JSON_FILE")"
else
  echo "Metadata: not provided (skipping date/location embedding)"
fi

docker run --rm $GPU_FLAG \
  -v "$(realpath "$MEMORIES")":/input:ro \
  -v "$(realpath "$OUTPUT")":/output:rw \
  $META_FLAG $WSL_LIB_FLAG \
  snapchat-fuse

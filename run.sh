#!/usr/bin/env bash
# Usage: ./run.sh <memories_dir> <output_dir>
# Example: ./run.sh ~/Snapchat/memories ~/Snapchat/memories_fused
set -euo pipefail

MEMORIES="${1:?Usage: $0 <memories_dir> <output_dir>}"
OUTPUT="${2:?Usage: $0 <memories_dir> <output_dir>}"

mkdir -p "$OUTPUT"

# Detect NVIDIA GPU
GPU_FLAG=""
if docker run --rm --gpus all nvidia/cuda:12.3.0-base-ubuntu22.04 nvidia-smi &>/dev/null; then
  GPU_FLAG="--gpus all"
  echo "NVIDIA GPU detected — using h264_nvenc"
else
  echo "No GPU detected — falling back to libx264"
fi

docker run --rm $GPU_FLAG \
  -v "$(realpath "$MEMORIES")":/input:ro \
  -v "$(realpath "$OUTPUT")":/output:rw \
  snapchat-fuse

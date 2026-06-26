FROM nvidia/cuda:12.3.0-base-ubuntu22.04

ENV DEBIAN_FRONTEND=noninteractive

RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg \
    python3 \
    python3-pip \
    && rm -rf /var/lib/apt/lists/*

RUN pip3 install --no-cache-dir pillow tqdm piexif

WORKDIR /app
COPY fuse.py .

ENTRYPOINT ["python3", "fuse.py"]

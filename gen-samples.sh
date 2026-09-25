#!/usr/bin/env bash
# Regenerate the demo clips in samples/ (gitignored) with ffmpeg.
set -euo pipefail
cd "$(dirname "$0")"
mkdir -p samples

# A clean festival master: ProRes 1080p25, stereo PCM 48k, normalised to -23 LUFS.
ffmpeg -y -v error -f lavfi -i testsrc2=size=1920x1080:rate=25:duration=3 \
  -f lavfi -i "sine=frequency=1000:duration=3" \
  -c:v prores_ks -profile:v 3 -pix_fmt yuv422p10le \
  -af "loudnorm=I=-23:TP=-1.5" -c:a pcm_s16le -ar 48000 -ac 2 samples/good.mov

# An off-spec web clip: H.264 720p30, AAC stereo 44.1k.
ffmpeg -y -v error -f lavfi -i testsrc2=size=1280x720:rate=30:duration=3 \
  -f lavfi -i "sine=frequency=1000:duration=3" \
  -c:v libx264 -pix_fmt yuv420p -c:a aac -ar 44100 -ac 2 samples/bad.mp4

echo "wrote samples/good.mov and samples/bad.mp4"

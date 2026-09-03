#!/bin/bash
# Build ffmpeg-vpu (ffmpeg 7.1.5 + Allwinner CedarC VPU decoders).
# Run on the board with /tmp/ffmpeg-n7.1.5 already extracted and
# /tmp/cedar_dec.c uploaded.
set -e

SRC=/tmp/ffmpeg-n7.1.5
cd "$SRC"

echo "=== sanity checks ==="
grep -n "bsfs" libavcodec/codec_internal.h | head -3
grep -n "AV_CODEC_CAP_HARDWARE" libavcodec/avcodec.h | head -2

echo "=== install sources ==="
cp /tmp/cedar_dec.c libavcodec/cedar_dec.c

grep -q ff_h264_cedar_decoder libavcodec/allcodecs.c || \
  sed -i '/^extern const FFCodec ff_h264_decoder;$/a extern const FFCodec ff_h264_cedar_decoder;' libavcodec/allcodecs.c
grep -q ff_hevc_cedar_decoder libavcodec/allcodecs.c || \
  sed -i '/^extern const FFCodec ff_hevc_decoder;$/a extern const FFCodec ff_hevc_cedar_decoder;' libavcodec/allcodecs.c
grep -q ff_vp9_cedar_decoder libavcodec/allcodecs.c || \
  sed -i '/^extern const FFCodec ff_vp9_decoder;$/a extern const FFCodec ff_vp9_cedar_decoder;' libavcodec/allcodecs.c

grep -q cedar_dec.o libavcodec/Makefile || cat >> libavcodec/Makefile <<'EOF'

# Allwinner CedarC VPU decoders
OBJS-$(CONFIG_H264_CEDAR_DECODER) += cedar_dec.o
OBJS-$(CONFIG_HEVC_CEDAR_DECODER) += cedar_dec.o
OBJS-$(CONFIG_VP9_CEDAR_DECODER)  += cedar_dec.o
EOF

echo "=== configure ==="
./configure --prefix=/usr/local \
  --enable-gpl --enable-libx264 \
  --disable-doc \
  --extra-libs="-lvdecoder -lMemAdapter -lVE -lcdc_base -lvideoengine -ldl -lm -lpthread" \
  2>&1 | tail -20

echo "=== build (this takes several minutes) ==="
make -j"$(nproc)" ffmpeg 2>&1 | tail -30

ls -la ffmpeg
echo "=== decoders ==="
./ffmpeg -hide_banner -decoders 2>/dev/null | grep cedar || echo NO_CEDAR_DECODERS

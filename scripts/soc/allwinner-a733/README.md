# Allwinner A733 / AW2511

本目录下的工具基于 Allwinner **CedarC**（`libvdecoder` / `libcedarc`），
只适用于 AW2511 / A733 系列 SoC 的内核（`sunxi_ve`）和厂商库。

## 已验证

| 板子 | 内核 | libcedarc | 状态 |
|------|------|-----------|------|
| Radxa Cubie A7A | 6.6.98-4-aw2511 | 1.0.7 (v2) | VPU H264 213fps / VP9 215fps / HEVC 有堆损坏 bug |

## VPU 能力

- H.264: 硬解 1080p60 ≈ 213fps, CPU 6.7x 减载
- VP9: 硬解 1080p30 ≈ 215fps, CPU ≈ 22x 减载
- HEVC: 厂商 libvdecoder 存在堆损坏 bug，`FFMPEG_VPU_HEVC=1` 可强制试开
- AV1: 无硬件解码器

## 文件

- `vpu-decode`: GStreamer OMX 解码 CLI（部署到板上 `/usr/local/bin/`）
- `ffmpeg/cedar_dec.c`: ffmpeg 7.1.5 源码级 CedarC 解码器
- `ffmpeg/build.sh`: 板上构建脚本

详见根目录 README 的「ffmpeg-vpu」章节。

/*
 * Allwinner CedarC VPU video decoder for FFmpeg (libvdecoder backend).
 *
 * Exposes h264_cedar / hevc_cedar / vp9_cedar decoders that decode on the
 * Allwinner Cedar video engine and copy frames back to system memory
 * (NV12/NV21/YV12). Intended for streaming/transcoding pipelines where the
 * CPU should be left free for x264 encoding.
 *
 * Vendor API: libcedarc (vdecoder.h + memoryAdapter.h), same library that
 * Allwinner's own OMX component (libOmxVdec) drives.
 */

#include <stdint.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>

#include "libavutil/intreadwrite.h"
#include "libavutil/mathematics.h"
#include "libavutil/mem.h"
#include "avcodec.h"
#include "codec_internal.h"
#include "decode.h"

#include "memoryAdapter.h"
#include "sc_interface.h"
#include "vdecoder.h"

#define CEDAR_FRAME_QUEUE 16

typedef struct CedarVContext {
    VideoDecoder *dec;
    VConfig cfg;
    struct ScMemOpsS *memops;

    uint8_t *extradata_annexb;
    int extradata_annexb_size;

    AVFrame *queue[CEDAR_FRAME_QUEUE];
    int queue_len;

    int eos_pushed;
    int64_t frame_count;
} CedarVContext;

static int cedar_map_codec(enum AVCodecID id, int *format)
{
    switch (id) {
    case AV_CODEC_ID_H264: *format = VIDEO_CODEC_FORMAT_H264; return 0;
    case AV_CODEC_ID_HEVC: *format = VIDEO_CODEC_FORMAT_H265; return 0;
    case AV_CODEC_ID_VP9:  *format = VIDEO_CODEC_FORMAT_VP9;  return 0;
    default: return AVERROR(EINVAL);
    }
}

static int is_annexb(const uint8_t *p, int size)
{
    int i;
    if (size < 4)
        return 0;
    for (i = 0; i < size - 3; i++) {
        if (!p[i] && !p[i + 1] && p[i + 2] == 1)
            return 1;
    }
    return 0;
}

static int annexb_append(uint8_t **buf, int *cap, int *len,
                         const uint8_t *nalu, int nalu_size)
{
    static const uint8_t sc[4] = { 0, 0, 0, 1 };
    int need = nalu_size + 4;
    uint8_t *nb;

    if (*len + need > *cap) {
        *cap = (*len + need + 4095) & ~4095;
        nb = av_realloc(*buf, *cap);
        if (!nb)
            return AVERROR(ENOMEM);
        *buf = nb;
    }
    memcpy(*buf + *len, sc, 4);
    memcpy(*buf + *len + 4, nalu, nalu_size);
    *len += need;
    return 0;
}

/* Convert avcC / hvcC extradata to Annex B. Returns allocated buffer or NULL. */
static int extradata_to_annexb(const uint8_t *d, int size, int is_h264,
                                uint8_t **out, int *out_size)
{
    uint8_t *buf = NULL;
    int cap = 0, len = 0;
    int off, i, j, ret;

    *out = NULL;
    *out_size = 0;
    if (!d || size <= 0)
        return 0;

    if (is_annexb(d, size)) {
        buf = av_memdup(d, size);
        if (!buf)
            return AVERROR(ENOMEM);
        *out = buf;
        *out_size = size;
        return 0;
    }

    if (is_h264) {
        int num_sps, num_pps;
        if (size < 7 || d[0] != 1)
            return 0; /* unknown; let the decoder cope */
        off = 6;
        num_sps = d[5] & 0x1f;
        for (i = 0; i < num_sps && off + 2 <= size; i++) {
            int n = AV_RB16(d + off); off += 2;
            if (n <= 0 || off + n > size) break;
            ret = annexb_append(&buf, &cap, &len, d + off, n);
            if (ret < 0) goto fail;
            off += n;
        }
        if (off >= size) goto done;
        num_pps = d[off++];
        for (i = 0; i < num_pps && off + 2 <= size; i++) {
            int n = AV_RB16(d + off); off += 2;
            if (n <= 0 || off + n > size) break;
            ret = annexb_append(&buf, &cap, &len, d + off, n);
            if (ret < 0) goto fail;
            off += n;
        }
    } else {
        int num_arrays;
        if (size < 23 || d[0] != 1)
            return 0;
        num_arrays = d[22];
        off = 23;
        for (i = 0; i < num_arrays && off + 3 <= size; i++) {
            off++; /* array_completeness + NAL type */
            int num_nalus = AV_RB16(d + off); off += 2;
            for (j = 0; j < num_nalus && off + 2 <= size; j++) {
                int n = AV_RB16(d + off); off += 2;
                if (n <= 0 || off + n > size) break;
                ret = annexb_append(&buf, &cap, &len, d + off, n);
                if (ret < 0) goto fail;
                off += n;
            }
        }
    }

done:
    if (buf && len > 0) {
        *out = buf;
        *out_size = len;
    } else {
        av_free(buf);
    }
    return 0;

fail:
    av_free(buf);
    return ret;
}

static int map_pix_format(int e, enum AVPixelFormat *fmt)
{
    switch (e) {
    case PIXEL_FORMAT_NV12: *fmt = AV_PIX_FMT_NV12; return 0;
    case PIXEL_FORMAT_NV21: *fmt = AV_PIX_FMT_NV21; return 0;
    case PIXEL_FORMAT_YV12: *fmt = AV_PIX_FMT_YUV420P; return 0;
    default: return -1;
    }
}

static int cedar_queue_picture(AVCodecContext *avctx, VideoPicture *pic)
{
    CedarVContext *ctx = avctx->priv_data;
    enum AVPixelFormat fmt;
    AVFrame *f;
    int w = pic->nWidth, h = pic->nHeight, stride = pic->nLineStride;
    int ret, y;

    if (w <= 0 || h <= 0 || stride <= 0 || !pic->pData0 || !pic->pData1) {
        av_log(avctx, AV_LOG_ERROR, "cedar: invalid picture geometry\n");
        return AVERROR(EIO);
    }
    if (map_pix_format(pic->ePixelFormat, &fmt) < 0) {
        av_log(avctx, AV_LOG_ERROR, "cedar: unsupported pixel format %d\n",
               pic->ePixelFormat);
        return AVERROR(ENOSYS);
    }

    if (avctx->pix_fmt != fmt || avctx->width != w || avctx->height != h) {
        ret = ff_set_dimensions(avctx, w, h);
        if (ret < 0)
            return ret;
        avctx->pix_fmt = fmt;
    }

    f = av_frame_alloc();
    if (!f)
        return AVERROR(ENOMEM);
    f->format = fmt;
    f->width  = w;
    f->height = h;
    ret = ff_get_buffer(avctx, f, 0);
    if (ret < 0) {
        av_frame_free(&f);
        return ret;
    }

    if (fmt == AV_PIX_FMT_NV12 || fmt == AV_PIX_FMT_NV21) {
        /* NV12/NV21 planes live in one contiguous ION buffer. */
        CdcMemFlushCache(ctx->memops, pic->pData0, stride * h * 3 / 2);
        for (y = 0; y < h; y++)
            memcpy(f->data[0] + y * f->linesize[0],
                   pic->pData0 + y * stride, w);
        for (y = 0; y < (h + 1) / 2; y++)
            memcpy(f->data[1] + y * f->linesize[1],
                   pic->pData1 + y * stride, w);
    } else { /* YV12: classic order Y, V(data1), U(data2) */
        int cs = stride / 2;
        CdcMemFlushCache(ctx->memops, pic->pData0, stride * h);
        CdcMemFlushCache(ctx->memops, pic->pData1, cs * ((h + 1) / 2));
        if (pic->pData2)
            CdcMemFlushCache(ctx->memops, pic->pData2, cs * ((h + 1) / 2));
        for (y = 0; y < h; y++)
            memcpy(f->data[0] + y * f->linesize[0],
                   pic->pData0 + y * stride, w);
        if (pic->pData2) /* U */
            for (y = 0; y < (h + 1) / 2; y++)
                memcpy(f->data[1] + y * f->linesize[1],
                       pic->pData2 + y * cs, (w + 1) / 2);
        for (y = 0; y < (h + 1) / 2; y++) /* V */
            memcpy(f->data[2] + y * f->linesize[2],
                   pic->pData1 + y * cs, (w + 1) / 2);
    }

    if (pic->nPts > 0 && avctx->pkt_timebase.num)
        f->pts = av_rescale_q(pic->nPts, AV_TIME_BASE_Q, avctx->pkt_timebase);
    else
        f->pts = AV_NOPTS_VALUE;

    if (ctx->queue_len == CEDAR_FRAME_QUEUE) {
        av_log(avctx, AV_LOG_WARNING, "cedar: frame queue overflow\n");
        av_frame_free(&ctx->queue[0]);
        memmove(&ctx->queue[0], &ctx->queue[1],
                sizeof(AVFrame *) * (CEDAR_FRAME_QUEUE - 1));
        ctx->queue_len--;
    }
    ctx->queue[ctx->queue_len++] = f;
    ctx->frame_count++;
    return 0;
}

static int cedar_drain_pictures(AVCodecContext *avctx)
{
    CedarVContext *ctx = avctx->priv_data;
    VideoPicture *pic;
    int ret;

    while ((pic = RequestPicture(ctx->dec, 0)) != NULL) {
        ret = cedar_queue_picture(avctx, pic);
        ReturnPicture(ctx->dec, pic);
        if (ret < 0)
            return ret;
    }
    return 0;
}

static int cedar_submit(AVCodecContext *avctx, const uint8_t *data, int size,
                        int64_t pts)
{
    CedarVContext *ctx = avctx->priv_data;
    VideoStreamDataInfo di;
    char *b0 = NULL, *b1 = NULL;
    int s0 = 0, s1 = 0, try, r;
    int64_t pts_us = 0;

    for (try = 0; try < 64; try++) {
        if (RequestVideoStreamBuffer(ctx->dec, size, &b0, &s0, &b1, &s1, 0) == 0 &&
            s0 + s1 >= size)
            break;
        if (try == 0)
            av_log(avctx, AV_LOG_DEBUG,
                   "cedar: stream buffer retry (size=%d got=%d+%d)\n",
                   size, s0, s1);
        r = DecodeVideoStream(ctx->dec, 0, 0, 0, 0);
        if (r == VDECODE_RESULT_RESOLUTION_CHANGE)
            av_log(avctx, AV_LOG_WARNING, "cedar: resolution change mid-stream\n");
        cedar_drain_pictures(avctx);
        usleep(1000);
    }
    if (!b0 || s0 + s1 < size) {
        av_log(avctx, AV_LOG_ERROR, "cedar: no stream buffer (%d bytes)\n", size);
        return AVERROR(EIO);
    }

    memcpy(b0, data, FFMIN(size, s0));
    if (size > s0)
        memcpy(b1, data + s0, size - s0);

    memset(&di, 0, sizeof(di));
    di.pData = b0;
    di.nLength = size;
    di.bIsFirstPart = 1;
    di.bIsLastPart = 1;
    di.bValid = 1;
    if (pts != AV_NOPTS_VALUE && avctx->pkt_timebase.num)
        pts_us = av_rescale_q(pts, avctx->pkt_timebase, AV_TIME_BASE_Q);
    di.nPts = pts_us;
    di.nPcr = pts_us;
    if (SubmitVideoStreamData(ctx->dec, &di, 0) != 0) {
        av_log(avctx, AV_LOG_ERROR, "cedar: SubmitVideoStreamData failed\n");
        return AVERROR(EIO);
    }
    return 0;
}

static int cedar_decode(AVCodecContext *avctx, AVFrame *frame,
                        int *got_frame, AVPacket *pkt)
{
    CedarVContext *ctx = avctx->priv_data;
    int ret, r;

    *got_frame = 0;

    if (pkt && pkt->size) {
        ret = cedar_submit(avctx, pkt->data, pkt->size, pkt->pts);
        if (ret < 0)
            return ret;
        {
            int guard = 0;
            do {
                r = DecodeVideoStream(ctx->dec, 0, 0, 0, 0);
                if (r < 0)
                    av_log(avctx, AV_LOG_DEBUG, "cedar: decode result %d\n", r);
                if (r < 0)
                    av_log(avctx, AV_LOG_WARNING, "cedar: decode result %d\n", r);
                if (r == VDECODE_RESULT_RESOLUTION_CHANGE)
                    av_log(avctx, AV_LOG_WARNING, "cedar: resolution change mid-stream\n");
                ret = cedar_drain_pictures(avctx);
                if (ret < 0)
                    return ret;
            } while ((r == VDECODE_RESULT_FRAME_DECODED ||
                      r == VDECODE_RESULT_KEYFRAME_DECODED ||
                      r == VDECODE_RESULT_CONTINUE) && ++guard < 64);
        }
    } else if (!pkt) {
        if (!ctx->eos_pushed) {
            int guard = 0;
            do {
                r = DecodeVideoStream(ctx->dec, 1, 0, 0, 0);
                cedar_drain_pictures(avctx);
                usleep(2000);
            } while (r != VDECODE_RESULT_NO_BITSTREAM && ++guard < 1000);
            ctx->eos_pushed = 1;
            cedar_drain_pictures(avctx);
        }
    }

    if (ctx->queue_len > 0) {
        av_frame_unref(frame);
        av_frame_move_ref(frame, ctx->queue[0]);
        memmove(&ctx->queue[0], &ctx->queue[1],
                sizeof(AVFrame *) * (ctx->queue_len - 1));
        ctx->queue_len--;
        *got_frame = 1;
    }

    return pkt ? pkt->size : 0;
}

static void cedar_flush(AVCodecContext *avctx)
{
    CedarVContext *ctx = avctx->priv_data;
    int i;

    for (i = 0; i < ctx->queue_len; i++)
        av_frame_free(&ctx->queue[i]);
    ctx->queue_len = 0;
    if (ctx->dec)
        ResetVideoDecoder(ctx->dec);
    ctx->eos_pushed = 0;
}

static av_cold int cedar_close(AVCodecContext *avctx)
{
    CedarVContext *ctx = avctx->priv_data;
    int i;

    for (i = 0; i < ctx->queue_len; i++)
        av_frame_free(&ctx->queue[i]);
    ctx->queue_len = 0;
    if (ctx->dec) {
        DestroyVideoDecoder(ctx->dec);
        ctx->dec = NULL;
    }
    if (ctx->memops) {
        CdcMemClose(ctx->memops);
        ctx->memops = NULL;
    }
    av_freep(&ctx->extradata_annexb);
    ctx->extradata_annexb_size = 0;
    return 0;
}

static av_cold int cedar_init(AVCodecContext *avctx)
{
    CedarVContext *ctx = avctx->priv_data;
    VideoStreamInfo si;
    int codec_format, ret;

    ret = cedar_map_codec(avctx->codec_id, &codec_format);
    if (ret < 0)
        return ret;

    if (avctx->codec_id == AV_CODEC_ID_HEVC &&
        !getenv("FFMPEG_VPU_HEVC")) {
        av_log(avctx, AV_LOG_ERROR,
               "hevc_cedar is disabled by default: this libvdecoder build has a "
               "known heap corruption bug in its HEVC path (the vendor's own "
               "vdecoderdemo crashes the same way). Set FFMPEG_VPU_HEVC=1 to "
               "test it anyway.\n");
        return AVERROR(EIO);
    }

    AddVDPlugin();

    memset(&si, 0, sizeof(si));
    si.eCodecFormat = codec_format;
    /* Match vdecoderdemo: only the codec ID; the VPU parses SPS itself. */

    if (avctx->codec_id != AV_CODEC_ID_VP9 && avctx->extradata_size > 0) {
        ret = extradata_to_annexb(avctx->extradata, avctx->extradata_size,
                                  avctx->codec_id == AV_CODEC_ID_H264,
                                  &ctx->extradata_annexb,
                                  &ctx->extradata_annexb_size);
        if (ret < 0)
            return ret;
        si.pCodecSpecificData = (char *)ctx->extradata_annexb;
        si.nCodecSpecificDataLen = ctx->extradata_annexb_size;
    }

    memset(&ctx->cfg, 0, sizeof(ctx->cfg));
    ctx->cfg.eOutputPixelFormat = PIXEL_FORMAT_NV12;
    ctx->cfg.bDispErrorFrame = 1;
    ctx->cfg.nDeInterlaceHoldingFrameBufferNum = 0;
    ctx->cfg.eCtlAfbcMode = DISABLE_AFBC_ALL_SIZE;

    ctx->memops = MemAdapterGetOpsS();
    if (!ctx->memops) {
        av_log(avctx, AV_LOG_ERROR, "cedar: MemAdapterGetOpsS failed\n");
        return AVERROR(ENODEV);
    }
    if (CdcMemOpen(ctx->memops) != 0) {
        av_log(avctx, AV_LOG_ERROR, "cedar: CdcMemOpen failed\n");
        return AVERROR(ENODEV);
    }
    ctx->cfg.memops = ctx->memops;

    ctx->dec = CreateVideoDecoder();
    if (!ctx->dec) {
        av_log(avctx, AV_LOG_ERROR, "cedar: CreateVideoDecoder failed\n");
        CdcMemClose(ctx->memops);
        ctx->memops = NULL;
        return AVERROR(ENODEV);
    }
    if (InitializeVideoDecoder(ctx->dec, &si, &ctx->cfg) != 0) {
        av_log(avctx, AV_LOG_ERROR, "cedar: InitializeVideoDecoder failed\n");
        cedar_close(avctx);
        return AVERROR(EIO);
    }

    av_log(avctx, AV_LOG_INFO,
           "cedar: initialized %dx%d codec=0x%x out=NV12\n",
           si.nWidth, si.nHeight, codec_format);
    return 0;
}

#define CEDAR_DECODER(codec_id, short_name, long_name)                    \
const FFCodec ff_##short_name##_decoder = {                               \
    .p.name         = #short_name,                                        \
    CODEC_LONG_NAME(long_name),                                           \
    .p.type         = AVMEDIA_TYPE_VIDEO,                                 \
    .p.id           = codec_id,                                           \
    .priv_data_size = sizeof(CedarVContext),                              \
    .init           = cedar_init,                                         \
    FF_CODEC_DECODE_CB(cedar_decode),                                     \
    .flush          = cedar_flush,                                        \
    .close          = cedar_close,                                        \
    .p.capabilities = AV_CODEC_CAP_DR1 | AV_CODEC_CAP_DELAY |             \
                      AV_CODEC_CAP_HARDWARE,                              \
    .p.pix_fmts     = (const enum AVPixelFormat[]) {                      \
        AV_PIX_FMT_NV12, AV_PIX_FMT_NV21, AV_PIX_FMT_YUV420P,             \
        AV_PIX_FMT_NONE                                                   \
    },                                                                    \
    .p.wrapper_name = "cedarc",                                           \
};

#define CEDAR_DECODER_BSF(codec_id, short_name, bsf_list, long_name)      \
const FFCodec ff_##short_name##_decoder = {                               \
    .p.name         = #short_name,                                        \
    CODEC_LONG_NAME(long_name),                                           \
    .p.type         = AVMEDIA_TYPE_VIDEO,                                 \
    .p.id           = codec_id,                                           \
    .priv_data_size = sizeof(CedarVContext),                              \
    .init           = cedar_init,                                         \
    FF_CODEC_DECODE_CB(cedar_decode),                                     \
    .flush          = cedar_flush,                                        \
    .close          = cedar_close,                                        \
    .p.capabilities = AV_CODEC_CAP_DR1 | AV_CODEC_CAP_DELAY |             \
                      AV_CODEC_CAP_HARDWARE,                              \
    .bsfs           = bsf_list,                                           \
    .p.pix_fmts     = (const enum AVPixelFormat[]) {                      \
        AV_PIX_FMT_NV12, AV_PIX_FMT_NV21, AV_PIX_FMT_YUV420P,             \
        AV_PIX_FMT_NONE                                                   \
    },                                                                    \
    .p.wrapper_name = "cedarc",                                           \
};

CEDAR_DECODER_BSF(AV_CODEC_ID_H264, h264_cedar, "h264_mp4toannexb",
                  "H.264 (Allwinner CedarC VPU)")
CEDAR_DECODER_BSF(AV_CODEC_ID_HEVC, hevc_cedar, "hevc_mp4toannexb",
                  "H.265 / HEVC (Allwinner CedarC VPU)")
CEDAR_DECODER(AV_CODEC_ID_VP9, vp9_cedar,
              "VP9 (Allwinner CedarC VPU)")

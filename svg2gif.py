#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""svg2gif.py —— 把动画 SVG 转成 GIF

支持 SMIL（<animate>）和 CSS 动画（@keyframes）。
可以按分辨率转，也可以按"文件不超过 N MB"自动挑参数。

    python svg2gif.py fig.svg fig.gif --width 1200
    python svg2gif.py fig.svg fig.gif --max-size 3MB
    python svg2gif.py fig.svg fig.gif --max-size 3MB --trim --crossfade 0.4

依赖：playwright（含 chromium）、Pillow
    pip install playwright pillow
    python -m playwright install chromium

几点说明
--------
* 动画没法直接读进 PIL，所以用 Chromium 打开它：SMIL 用 `pauseAnimations()` +
  `setCurrentTime(t)`，CSS 动画走 Web Animations API 逐个设 `currentTime`。
  两套时间轴都要停住，否则 CSS 那部分会按真实时间自己跑，转出来会随机闪。
* 片头淡入 / 片尾淡出会让循环 GIF 每隔一轮"糊一下"，`--trim` 可以自动切掉。
* 周期默认取 SVG 里最大的 `dur`，可用 --duration 覆盖。
* 背景默认透明（截图时 omit_background=True）。要白底就加 --bg #ffffff。
* 透明 GIF 只有 1 bit 透明度，而且"透明像素 = 不绘制"，所以不能用普通帧间差分：
  任何消失的东西都不会被擦掉，会留下鬼影。这里的做法是给需要的过渡额外插一帧
  disposal=2 只清掉"消失的那块"，紧接着再补画一次。详见 GifEncoder 里的注释。
* 指定文件大小时按像素面积开方迭代缩放（GIF 体积近似正比于像素数），
  通常 2~3 轮就能收敛；缩到下限还超标就降色数。
"""

import argparse
import io
import math
import os
import re
import shutil
import sys
import tempfile
from pathlib import Path

from PIL import Image, ImageChops, ImageDraw, ImageStat

TRANSPARENT_INDEX = 255          # 保留给透明的调色板下标
VIVID_SLOTS = 24                 # 留给原子那种高饱和色的调色板槽位


# --------------------------------------------------------------------------
# 小工具
# --------------------------------------------------------------------------
def _fix_stdout():
    """Windows 控制台默认 GBK，中文输出会 UnicodeEncodeError。"""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass


def parse_size(text):
    """把 '3MB' / '500kb' / '2500000' 解析成字节数。"""
    s = str(text).strip().lower().replace(" ", "")
    m = re.fullmatch(r"([\d.]+)(b|kb|mb|gb|k|m|g)?", s)
    if not m:
        raise ValueError("看不懂的大小: %r" % text)
    value = float(m.group(1))
    unit = m.group(2) or "b"
    # 用十进制：3MB 就是 3 000 000 字节，这样不管按哪种口径理解都不会超
    factor = {"b": 1, "k": 1000, "kb": 1000, "m": 1000 ** 2, "mb": 1000 ** 2,
              "g": 1000 ** 3, "gb": 1000 ** 3}[unit]
    return int(value * factor)


def human(nbytes):
    return "%.2f MB" % (nbytes / 1e6) if nbytes >= 1e6 else "%.0f KB" % (nbytes / 1e3)


def svg_meta(path):
    """读 viewBox 宽高比 + 所有动画周期（SMIL 的 dur 和 CSS 的 animation 时长）。"""
    text = Path(path).read_text(encoding="utf-8", errors="replace")
    vb = re.search(r'viewBox\s*=\s*["\']([-\d.eE\s,]+)["\']', text)
    if vb:
        nums = [float(v) for v in re.split(r"[\s,]+", vb.group(1).strip()) if v]
        if len(nums) == 4 and nums[2] > 0 and nums[3] > 0:
            vw, vh = nums[2], nums[3]
        else:
            vw, vh = 1600.0, 900.0
    else:
        w = re.search(r'\bwidth\s*=\s*["\']([\d.]+)', text)
        h = re.search(r'\bheight\s*=\s*["\']([\d.]+)', text)
        vw = float(w.group(1)) if w else 1600.0
        vh = float(h.group(1)) if h else 900.0

    durs = [float(v) for v in re.findall(r'\bdur\s*=\s*["\']([\d.]+)\s*s["\']', text)]
    # CSS 动画：animation: name 1.2s ... / animation-duration: 1.2s
    for body in re.findall(r'animation\s*:\s*([^;}{]+)', text):
        ts = re.findall(r'([\d.]+)s\b', body)
        if ts:
            durs.append(float(ts[0]))          # 简写里第一个时间值才是 duration
    for body in re.findall(r'animation-duration\s*:\s*([^;}{]+)', text):
        for ts in re.findall(r'([\d.]+)s\b', body):
            durs.append(float(ts))
    return vw, vh, durs


def pick_cycle(durs):
    """选循环周期：以最长的动画为准。

    只对"显著"的动画（时长 ≥ 最长者 1/4）要求整除，否则像 0.55 s 这种小装饰动画
    会把周期推到 132 s 这种荒谬的最小公倍数上。
    """
    if not durs:
        return None
    base = max(durs)
    major = [d for d in durs if d >= base / 4.0]
    for k in range(1, 4):
        cand = base * k
        if all(abs(cand / d - round(cand / d)) < 0.02 for d in major):
            return cand
    return base


# --------------------------------------------------------------------------
# 取帧
# --------------------------------------------------------------------------
def render_frames(svg_path, out_dir, width, height, n_frames, duration,
                  transparent, times=None, log=None):
    """用 Chromium 逐帧截图，返回 (PNG 路径列表, 是否有动画)。"""
    from playwright.sync_api import sync_playwright

    svg_text = Path(svg_path).read_text(encoding="utf-8", errors="replace")
    body = svg_text[svg_text.find("<svg"):] if "<svg" in svg_text else svg_text
    bg = "transparent" if transparent else "#ffffff"
    page = out_dir / "page.html"
    page.write_text(
        '<!doctype html><html><head><meta charset="utf-8">'
        '<style>html,body{margin:0;padding:0;background:%s;overflow:hidden}'
        'svg{display:block;width:100vw;height:auto}</style></head><body>%s</body></html>'
        % (bg, body), encoding="utf-8")

    out_dir.mkdir(parents=True, exist_ok=True)
    paths = []
    with sync_playwright() as p:
        # Linux 容器里 Chromium 常因沙箱起不来，可用环境变量补参数：
        #   SVG2GIF_CHROME_ARGS="--no-sandbox"
        extra = os.environ.get("SVG2GIF_CHROME_ARGS", "").split()
        browser = p.chromium.launch(args=extra) if extra else p.chromium.launch()
        pg = browser.new_page(viewport={"width": width, "height": height},
                              device_scale_factor=1)
        errors = []
        pg.on("pageerror", lambda e: errors.append(str(e)))
        pg.goto(page.as_uri())
        pg.wait_for_timeout(1500)
        # SMIL 用 svg.setCurrentTime() 定位；CSS 动画走 Web Animations API。
        # 两套时间轴都要停住并逐帧对齐，否则 CSS 那部分会按真实时间乱跑。
        state = pg.evaluate("""() => {
            const s = document.querySelector('svg');
            if (s && s.pauseAnimations) s.pauseAnimations();
            const anims = (document.getAnimations ? document.getAnimations() : [])
                          .filter(a => a.constructor.name !== 'SVGAnimation');
            anims.forEach(a => { try { a.pause(); } catch (e) {} });
            window.__anims = anims;
            return {smil: !!(s && s.setCurrentTime), waapi: anims.length};
        }""")
        animated = bool(state["smil"] or state["waapi"])
        for i, t in enumerate(times):
            if state["smil"]:
                pg.evaluate("(t) => { const s = document.querySelector('svg');"
                            " if (s && s.setCurrentTime) s.setCurrentTime(t); }", t)
            if state["waapi"]:
                pg.evaluate("(t) => { (window.__anims || []).forEach(a => {"
                            " try { a.currentTime = t * 1000; } catch (e) {} }); }", t)
            if animated:
                pg.wait_for_timeout(60)
            fp = out_dir / ("f%04d.png" % i)
            pg.screenshot(path=str(fp), omit_background=transparent,
                          clip={"x": 0, "y": 0, "width": width, "height": height})
            paths.append(fp)
            if log and (i + 1) % 20 == 0:
                log("  取帧 %d/%d" % (i + 1, len(times)))
        if errors:
            print("  [警告] 页面报错:", errors[:3])
        browser.close()
    return paths, animated


# --------------------------------------------------------------------------
# 调色板
# --------------------------------------------------------------------------
def build_palette(frames, colors):
    """从若干帧里做一张全局调色板（所有帧共用，避免逐帧调色板造成的闪烁）。

    median cut 几乎不会给那几个很小的饱和色原子分配槽位，所以额外预留
    VIVID_SLOTS 个槽，用"高频 + 最小色距"挑出画面里真实存在的鲜艳色填进去。
    """
    white = Image.new("RGBA", frames[0].size, (255, 255, 255, 255))
    step = max(1, len(frames) // 10)
    tiles = []
    for im in frames[::step]:
        a = im.getchannel("A").point(lambda v: 255 if v > 8 else 0)
        bb = a.getbbox() or (0, 0, im.width, im.height)
        tiles.append(Image.alpha_composite(white.copy(), im).convert("RGB").crop(bb))
    tw = max(t.width for t in tiles)
    th = max(t.height for t in tiles)
    cols = 4
    rows = (len(tiles) + cols - 1) // cols
    sheet = Image.new("RGB", (tw * cols, th * rows), (255, 255, 255))
    for k, t in enumerate(tiles):
        sheet.paste(t, ((k % cols) * tw, (k // cols) * th))

    base = max(8, colors - VIVID_SLOTS) if colors > VIVID_SLOTS * 2 else colors
    pal = sheet.quantize(colors=base, method=Image.MEDIANCUT)
    if colors <= VIVID_SLOTS * 2:
        return pal

    from collections import Counter
    vivid = Counter()
    for im in frames[::max(1, len(frames) // 16)]:
        small = im.convert("RGB").resize((max(1, im.width // 3), max(1, im.height // 3)))
        pixels = (small.get_flattened_data() if hasattr(small, 'get_flattened_data')
                  else small.getdata())
        for px in pixels:
            if max(px) - min(px) > 70:
                vivid[tuple(v // 12 * 12 for v in px)] += 1
    extra = []
    for colour, _ in vivid.most_common(4000):
        if all(sum((a - b) ** 2 for a, b in zip(colour, e)) > 3600 for e in extra):
            extra.append(colour)
        if len(extra) == VIVID_SLOTS:
            break

    table = pal.getpalette()[:3 * base]
    for colour in extra:
        table.extend(colour)
    table.extend([0, 0, 0] * (256 - len(table) // 3))
    out = Image.new("P", (1, 1))
    out.putpalette(table)
    return out


# --------------------------------------------------------------------------
# GIF 编码
# --------------------------------------------------------------------------
def _diff_l(a, b):
    return ImageChops.difference(a.convert("RGB"), b.convert("RGB")).convert("L")


def _image_block(im):
    """把一张 P 图用 Pillow 编码成单个 GIF image block，返回 (palette, block)。

    optimize=False 是必须的：开着的话 Pillow 会按裁剪图重新压缩调色板并重映射
    索引，而拼接时用的是全局调色板，颜色会整体错位（表现为满屏闪烁）。
    """
    buf = io.BytesIO()
    im.save(buf, format="GIF", transparency=TRANSPARENT_INDEX, optimize=False)
    d = buf.getvalue()
    packed = d[10]
    n = 2 ** ((packed & 7) + 1) if packed & 0x80 else 0
    gct = d[13:13 + 3 * n]
    pos = 13 + 3 * n
    while d[pos] == 0x21:                       # 跳过扩展块
        pos += 2
        while d[pos]:
            pos += d[pos] + 1
        pos += 1
    assert d[pos] == 0x2C, "不是图像块"
    assert not (d[pos + 9] & 0x80), "意外的局部色表"
    start = pos
    pos += 11
    while d[pos]:
        pos += d[pos] + 1
    pos += 1
    return gct, d[start:pos]


def _rebox(block, box):
    w, h = box[2] - box[0], box[3] - box[1]
    return (block[:1] + bytes([box[0] & 255, box[0] >> 8, box[1] & 255, box[1] >> 8,
                               w & 255, w >> 8, h & 255, h >> 8]) + block[9:])


def _gce(disposal, delay_cs):
    packed = (disposal << 2) | 1        # 始终带透明标记：差分帧靠它表示"不绘制"
    return bytes([0x21, 0xF9, 0x04, packed,
                  delay_cs & 255, delay_cs >> 8, TRANSPARENT_INDEX, 0x00])


def encode_gif(frames, out_path, delays_cs, transparent=True, loop=0,
               erase_ratio=0.20):
    """frames: [(P 图, 透明度掩码 L)]，delays_cs: 每步的厘秒数。

    每一步按如下规则写帧：
      * 普通帧 disposal=1，矩形取与上一帧的差异外框，只绘制变化的像素
        （未变化的像素写成透明，LZW 会把它们压成很长的游程）。
      * 若这一步有像素要"消失"，先补一帧 disposal=2（恢复背景）只清掉消失区域，
        再让正常帧把该区域重画一遍 —— 透明 GIF 里这是擦除的唯一手段。
      * 最后一帧整幅重绘并清理，循环回到第 0 帧时是干净的画布。
    """
    W, H = frames[0][0].size
    gct = frames[0][0].getpalette()
    gct = bytes(gct + [0] * (768 - len(gct))) if len(gct) < 768 else bytes(gct[:768])
    bg_index = TRANSPARENT_INDEX

    parts = [b"GIF89a",
             bytes([W & 255, W >> 8, H & 255, H >> 8]),
             bytes([0xF7, bg_index, 0]),
             gct,
             b"\x21\xFF\x0BNETSCAPE2.0" + bytes([3, 1]) + (loop & 0xFFFF).to_bytes(2, "little") + b"\x00"]

    prev = prev_mask = None
    pending = 0
    mapping = []          # 动画步 -> GIF 帧号（一条过渡可能拆成两帧）
    nf = 0
    for k, ((fr, mask), dcs) in enumerate(zip(frames, delays_cs)):
        last = k == len(frames) - 1
        if prev is None:
            box = mask.getbbox() or (0, 0, 1, 1)
            parts.append(_gce(1, dcs))
            parts.append(_rebox(_image_block(fr.crop(box))[1], box))
            nf += 1
            mapping.append((k, nf - 1))
            prev, prev_mask = fr, mask
            continue

        diff = _diff_l(prev, fr)
        box = diff.getbbox()
        if box is None and not last:                      # 完全相同的帧：并到上一帧
            pending += dcs
            continue

        vbox = ImageChops.subtract(prev_mask, mask).getbbox()   # 由不透明变透明的像素
        area_v = (vbox[2] - vbox[0]) * (vbox[3] - vbox[1]) if vbox else 0
        can_erase = bool(vbox) and area_v < erase_ratio * W * H

        if last:                                          # 整幅重绘 + 清屏，供循环复位
            if can_erase and dcs >= 4:
                parts.append(_gce(2, dcs // 2))
                parts.append(_rebox(_image_block(fr.crop(vbox))[1], vbox))
                nf += 1
                dcs -= dcs // 2
            parts.append(_gce(2, pending + dcs))
            parts.append(_rebox(_image_block(fr)[1], (0, 0, W, H)))
            nf += 1
            mapping.append((k, nf - 1))
            prev, prev_mask, pending = fr, mask, 0
            continue

        keep = dcs
        if can_erase and dcs >= 4:                        # 先清掉消失的那块
            parts.append(_gce(2, dcs // 2))
            parts.append(_rebox(_image_block(fr.crop(vbox))[1], vbox))
            nf += 1
            keep = dcs - dcs // 2

        crop = fr.crop(box)
        same = _diff_l(prev.crop(box), crop).point(lambda v: 255 if v == 0 else 0)
        if can_erase and dcs >= 4:
            # 上一帧的 disposal 已经清空了 vbox，这里必须整块补画
            same.paste(0, (vbox[0] - box[0], vbox[1] - box[1],
                           vbox[2] - box[0], vbox[3] - box[1]))
        crop.paste(TRANSPARENT_INDEX, mask=same)
        parts.append(_gce(1, pending + keep))
        parts.append(_rebox(_image_block(crop)[1], box))
        nf += 1
        mapping.append((k, nf - 1))
        prev, prev_mask, pending = fr, mask, 0

    parts.append(b"\x3B")
    data = b"".join(parts)
    Path(out_path).write_bytes(data)
    encode_gif.mapping = mapping
    return len(data)


def to_gif_frames(rgba_frames, colours):
    """RGBA 帧 -> [(P 图, 掩码)]。"""
    pal = build_palette(rgba_frames, colours)
    white = Image.new("RGBA", rgba_frames[0].size, (255, 255, 255, 255))
    out = []
    for im in rgba_frames:
        im = im.convert("RGBA")
        mask = im.getchannel("A").point(lambda v: 255 if v >= 128 else 0)
        p = Image.alpha_composite(white.copy(), im).convert("RGB").quantize(
            palette=pal, dither=Image.NONE)
        p.paste(TRANSPARENT_INDEX, mask=mask.point(lambda v: 255 if v == 0 else 0))
        out.append((p, mask))
    return out


def delays_for(n, duration, fps):
    """按厘秒取整，保证总时长准确。"""
    total = int(round(duration * 100))
    out, acc = [], 0
    for i in range(n):
        t = int(round(total * (i + 1) / n))
        out.append(max(2, t - acc))
        acc = t
    return out


# --------------------------------------------------------------------------
# 片头 / 片尾淡入淡出
# --------------------------------------------------------------------------
def scene_contrast(frames):
    """用亮度标准差粗略衡量"画面有多满"。空白/半透明淡入时很小，画满时很大。"""
    return [ImageStat.Stat(f.convert("L")).stddev[0] for f in frames]


def auto_trim(frames, level=0.72, pad=2):
    """找到画面已经"成形"的区间，返回 (起, 止) 下标。

    很多 SVG 的片头是从全透明淡入、片尾再淡出到空白。对循环播放的 GIF 来说，
    这段几乎空白的时间看起来就是"模糊闪屏"：整幅图先糊一下再清晰。
    """
    if len(frames) < 6:
        return 0, len(frames) - 1
    vals = scene_contrast(frames)
    peak = max(vals) or 1.0
    keep = [i for i, v in enumerate(vals) if v >= level * peak]
    if len(keep) < 4 or (keep[0] == 0 and keep[-1] == len(frames) - 1):
        return 0, len(frames) - 1
    return max(0, keep[0] - pad), min(len(frames) - 1, keep[-1] + pad)


def seam_jump(frames):
    """末帧→首帧的差异相对"正常步长"的倍数。≈1 说明这个循环本来就是接续的。"""
    if len(frames) < 4:
        return 0.0

    def mean_diff(a, b):
        return sum(ImageStat.Stat(
            ImageChops.difference(a.convert("RGB"), b.convert("RGB"))).mean) / 3.0

    steps = [mean_diff(frames[i], frames[i + 1]) for i in range(min(len(frames) - 1, 8))]
    step = (sum(steps) / len(steps)) or 1e-6
    return mean_diff(frames[-1], frames[0]) / step


def loop_crossfade(frames, k):
    """把结尾 k 帧与开头 k 帧做交叉淡化，接缝处不再硬跳。

    输出长度变成 len-k：末尾 k 帧是 [尾部 → 头部] 的渐变，最后一帧约等于
    开头第 k 帧，于是循环回第一帧时只差一帧的运动量。
    """
    n = len(frames)
    k = max(0, min(k, n // 3))
    if k < 2:
        return frames
    body = frames[k:n - k]
    tail = frames[n - k:]
    head = frames[:k]
    blend = [Image.blend(tail[i], head[i], (i + 1) / float(k)) for i in range(k)]
    return list(body) + blend


# --------------------------------------------------------------------------
# 主流程
# --------------------------------------------------------------------------
def load_frames(paths):
    return [Image.open(p).convert("RGBA") for p in paths]


def resize_frames(frames, width, height):
    if frames[0].width == width and frames[0].height == height:
        return frames
    return [f.resize((width, height), Image.LANCZOS) for f in frames]


def convert(svg, out, width=None, height=None, max_size=None, fps=20.0,
            duration=None, colours=255, bg=None, loop=0, still=False,
            verify=False, keep=False, verbose=True, log=None,
            trim=False, crossfade=0.4):
    """完整流程。log 是一个字符串回调，GUI 用它把进度接进日志框。"""
    say = log or (print if verbose else (lambda *a: None))
    svg = Path(svg); out = Path(out)
    vw, vh, durs = svg_meta(svg)
    aspect = vw / vh
    animated = bool(durs)

    if duration is None:
        duration = pick_cycle(durs)
    if duration is None:
        duration = 0.0
        still = True
    if not animated:
        still = True

    if width is None and height is None:
        width = 1200
    elif width is None:
        width = int(round(height * aspect))
    height = int(round(width / aspect)) if height is None else height

    n_frames = 1 if still else max(2, int(round(duration * fps)))
    transparent = bg is None

    tmp = Path(tempfile.mkdtemp(prefix="svg2gif-"))
    try:
        say("SVG   : %s" % svg)
        say("尺寸  : %dx%d   周期 %.2fs   帧数 %d   %s"
            % (width, height, duration, n_frames,
               "透明背景" if transparent else "背景 %s" % bg))
        times = [0.0] if still else [i * duration / n_frames for i in range(n_frames)]
        paths, _ = render_frames(svg, tmp, width, height, n_frames, duration,
                                 transparent, times, log=say)
        frames = load_frames(paths)
        say("取帧完成: %d 帧" % len(frames))

        # 每帧代表的时间跨度（裁掉片头片尾后仍然准确）
        dt = duration / len(frames) if len(frames) else 0.0

        if trim and not still:
            a, b = auto_trim(frames, pad=max(1, int(round(0.1 * fps))))
            if (a, b) != (0, len(frames) - 1):
                say("跳过淡入淡出: 保留第 %d~%d 帧（%.2f–%.2f s）"
                    % (a, b, a * dt, (b + 1) * dt))
                frames = frames[a:b + 1]
                paths = paths[a:b + 1]
        if crossfade and not still and crossfade > 0:
            jump = seam_jump(frames)
            if jump <= 1.5:
                say("接缝偏差 %.2f×，循环本来就是连续的，不做交叉淡化" % jump)
            else:
                k = int(round(crossfade * fps))
                before = len(frames)
                frames = loop_crossfade(frames, k)
                if len(frames) != before:
                    say("循环交叉淡化: 末尾 %d 帧（%.2f s），接缝偏差 %.1f×"
                        % (k, k * dt, jump))

        base_w, base_h = width, height
        attempts = []
        used = list(frames)                    # 可被子采样
        span = len(frames) * dt                # 输出总时长

        def try_encode(w, cols):
            h = max(1, int(round(w / aspect)))
            fs = resize_frames(used, w, h)
            gif_frames = to_gif_frames(fs, cols)
            total = 0.0 if still else span * len(gif_frames) / float(len(used))
            dcs = [100] if still else delays_for(len(gif_frames), total, fps)
            n = encode_gif(gif_frames, out, dcs, transparent=transparent, loop=loop)
            try_encode.delays = dcs
            return n, w, h, cols

        say("编码中…")
        n, w, h, cols = try_encode(width, colours)
        attempts.append((n, w, h, cols))
        say("   %dx%d / %d 色 -> %s" % (w, h, cols, human(n)))

        if max_size:
            limit = parse_size(max_size)
            guard = 0
            while n > limit and guard < 8:
                guard += 1
                if w <= 420 and len(used) >= 40 and len(used) % 2 == 0:
                    # 分辨率已经不高了，改用帧率换清晰度（体积几乎正比于帧数）
                    used = used[::2]
                    say("   帧率减半（%d 帧），换取更高分辨率" % len(used))
                    n, w, h, cols = try_encode(w, cols)
                    attempts.append((n, w, h, cols))
                    say("   %dx%d / %d 色 -> %s" % (w, h, cols, human(n)))
                    continue
                if cols > 96 and w <= 420:      # 尺寸已经很小，改降色数
                    cols = max(64, int(cols * 0.75))
                else:
                    scale = math.sqrt(limit / float(n)) * 0.97
                    w = max(240, int(w * max(0.45, min(scale, 0.95))))
                say("   超出上限 %s，再试 %dx%d / %d 色"
                    % (human(limit), w, max(1, int(round(w / aspect))), cols))
                n, w, h, cols = try_encode(w, cols)
                attempts.append((n, w, h, cols))
                say("   %dx%d / %d 色 -> %s" % (w, h, cols, human(n)))
            if n > limit:
                raise ValueError("无法压缩到 %s 以内（最小 %s），请降低帧率或色数"
                                 % (max_size, human(n)))

            # 降完帧率往往还剩不少预算，把分辨率加回去
            for _ in range(3):
                if n > limit * 0.75 or w >= base_w:
                    break
                new_w = int(min(base_w, w * 1.30))
                if new_w <= w:
                    break
                n2, w2, h2, _c = try_encode(new_w, cols)
                if n2 <= limit:
                    n, w, h = n2, w2, h2
                    say("   还有余量，放大到 %dx%d -> %s" % (w, h, human(n)))
                else:
                    n, w, h, cols = try_encode(w, cols)
                    break

        say("GIF   : %s  %dx%d  %d 色  %d 帧  %.2f s  %s"
            % (out, w, h, cols, len(used), sum(try_encode.delays) / 100.0, human(n)))

        if verify:
            info = check_gif(out, used, still, getattr(encode_gif, "mapping", None))
            say("校验  : %s" % info)
        convert.result = dict(width=w, height=h, colours=cols, frames=len(used),
                              bytes=n, duration=sum(try_encode.delays) / 100.0,
                              path=str(out), limit=parse_size(max_size) if max_size else None)
        return out
    finally:
        if not keep:
            shutil.rmtree(tmp, ignore_errors=True)
        elif verbose:
            say("临时帧保留在: %s" % tmp)


def check_gif(path, ref_frames, still, mapping=None):
    """解码回来和编码前的帧逐帧比对（透明通道 + 颜色）。

    ref_frames 是真正送去编码的 RGBA 帧（已经过裁剪、交叉淡化、缩放）。
    mapping 是"动画步 -> GIF 帧号"的映射：一条过渡可能被拆成两帧（先清屏再补画），
    所以不能简单按下标对齐。
    """
    gif = Image.open(path)
    total = 0
    for i in range(gif.n_frames):
        gif.seek(i)
        total += gif.info.get("duration", 0)
    if mapping is None:
        mapping = [(min(i, len(ref_frames) - 1), i) for i in range(gif.n_frames)]
    worst_a = worst_c = 0
    for step, gi in mapping:
        gif.seek(gi)
        cur = gif.convert("RGBA")
        ref = ref_frames[min(step, len(ref_frames) - 1)].convert("RGBA")
        if ref.size != cur.size:
            ref = ref.resize(cur.size, Image.LANCZOS)
        am = lambda im: im.getchannel("A").point(lambda v: 255 if v >= 128 else 0)
        a = ImageChops.difference(am(cur), am(ref))
        worst_a = max(worst_a, sum(a.histogram()[1:]))
        d = ImageChops.difference(cur.convert("RGB"), ref.convert("RGB")).convert("L")
        both = ImageChops.multiply(am(ref), am(cur))
        d = Image.composite(d, Image.new("L", d.size, 0), both)
        worst_c = max(worst_c, sum(d.point(lambda v: 255 if v > 24 else 0).histogram()[1:]))
    return "%d 帧, 总时长 %.2fs, 最大透明差 %d px, 最大色差 %d px" % (
        gif.n_frames, total / 1000.0, worst_a, worst_c)


def main(argv=None):
    _fix_stdout()
    ap = argparse.ArgumentParser(
        description="SMIL 动画 SVG -> GIF（可按分辨率或文件大小）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="示例:\n"
               "  python svg2gif.py fig.svg fig.gif --width 1200\n"
               "  python svg2gif.py fig.svg fig.gif --max-size 3MB\n"
               "  python svg2gif.py fig.svg fig.gif --max-size 3MB --colors 224 --fps 20\n")
    ap.add_argument("svg", help="输入 SVG")
    ap.add_argument("gif", help="输出 GIF")
    ap.add_argument("--width", type=int, help="输出宽度（像素）")
    ap.add_argument("--height", type=int, help="输出高度（像素，与 --width 二选一）")
    ap.add_argument("--max-size", help="文件大小上限，如 3MB / 800KB")
    ap.add_argument("--fps", type=float, default=20.0, help="帧率，默认 20")
    ap.add_argument("--duration", type=float, help="动画周期（秒），默认取 SVG 中最大的 dur")
    ap.add_argument("--colors", type=int, default=255, help="调色板颜色数，默认 255")
    ap.add_argument("--bg", help="不透明背景色，如 #ffffff；默认透明")
    ap.add_argument("--loop", type=int, default=0, help="循环次数，0 = 无限")
    ap.add_argument("--still", action="store_true", help="只导出第一帧")
    ap.add_argument("--trim", dest="trim", action="store_true",
                    help="自动跳过片头淡入 / 片尾淡出的近空白段")
    ap.add_argument("--no-trim", dest="trim", action="store_false",
                    help="保留完整周期（默认）")
    ap.add_argument("--crossfade", type=float, default=0.4,
                    help="循环交叉淡化时长（秒），0 = 关闭，默认 0.4")
    ap.add_argument("--verify", action="store_true", help="导出后解码自检")
    ap.add_argument("--keep-frames", action="store_true", help="保留临时帧")
    ap.set_defaults(trim=False)
    a = ap.parse_args(argv)

    if not Path(a.svg).exists():
        raise SystemExit("找不到文件: %s" % a.svg)
    try:
        convert(a.svg, a.gif, width=a.width, height=a.height, max_size=a.max_size,
                fps=a.fps, duration=a.duration, colours=max(8, min(255, a.colors)),
                bg=a.bg, loop=a.loop, still=a.still, verify=a.verify,
                keep=a.keep_frames, trim=a.trim, crossfade=a.crossfade)
    except ValueError as exc:
        raise SystemExit(str(exc))
    return 0


if __name__ == "__main__":
    sys.exit(main())

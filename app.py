# -*- coding: utf-8 -*-
"""
轻压 LightPress  —  图片 / GIF / 视频 体积优化工具
------------------------------------------------
核心思路：以「目标体积」为收敛条件，自动迭代编码参数
  · 图片：Pillow 质量二分搜索 + 必要时等比缩放
  · GIF ：ffmpeg 生成专属调色板（palettegen/paletteuse）+ 缩放迭代
  · 视频：x264 两遍编码，按目标体积反算码率，超了自动降码率重编
"""
import os
import io
import re
import sys
import json
import math
import shutil
import argparse
import tempfile
import threading
import subprocess
import queue
import time
from dataclasses import dataclass, field
from pathlib import Path

APP_NAME = "轻压"
APP_EN = "LightPress"
APP_VER = "1.1.0"

# ----------------------------------------------------------------------------
# 常量 / 主题
# ----------------------------------------------------------------------------
C_BG = "#F4F6FA"          # 窗口底色
C_CARD = "#FFFFFF"        # 卡片
C_LINE = "#E6EAF2"        # 描边
C_TEXT = "#1E2433"        # 主文字
C_SUB = "#8A93A6"         # 次要文字
C_PRIMARY = "#3B6BF5"     # 主色
C_PRIMARY_D = "#2E58D8"
C_PRIMARY_L = "#EEF3FF"
C_OK = "#16A34A"
C_ERR = "#E5484D"
C_WARN = "#D97706"

FONT_UI = "Microsoft YaHei UI"
FALLBACK_FONTS = ("Microsoft YaHei UI", "Microsoft YaHei", "PingFang SC", "Segoe UI")

IMAGE_EXT = {".jpg", ".jpeg", ".png", ".webp", ".bmp", ".tif", ".tiff"}
GIF_EXT = {".gif"}
VIDEO_EXT = {".mp4", ".mov", ".mkv", ".avi", ".webm", ".m4v", ".wmv", ".flv", ".ts", ".mpg", ".mpeg"}

FMT_LABELS = ["自动（保持原格式）", "JPEG", "WebP", "PNG"]
FMT_MAP = {FMT_LABELS[0]: "auto", "JPEG": "jpeg", "WebP": "webp", "PNG": "png"}
FMT_RMAP = {v: k for k, v in FMT_MAP.items()}

NO_WINDOW = 0
if sys.platform == "win32":
    NO_WINDOW = 0x08000000  # CREATE_NO_WINDOW

try:
    from PIL import Image, ImageOps, ImageFile
    ImageFile.LOAD_TRUNCATED_IMAGES = True
    Image.MAX_IMAGE_PIXELS = None
    HAS_PIL = True
except Exception:  # pragma: no cover
    HAS_PIL = False


# ----------------------------------------------------------------------------
# 基础工具
# ----------------------------------------------------------------------------
def human_size(n):
    if n is None:
        return "-"
    n = float(n)
    if n < 1024:
        return "%d B" % n
    for unit in ("KB", "MB", "GB"):
        n /= 1024.0
        if n < 1024 or unit == "GB":
            return ("%.1f %s" % (n, unit)) if n >= 100 else ("%.2f %s" % (n, unit))
    return "-"


def app_dir():
    """程序所在目录（打包后为 exe 所在目录）"""
    if getattr(sys, "frozen", False):
        return os.path.dirname(os.path.abspath(sys.executable))
    return os.path.dirname(os.path.abspath(__file__))


def res_dir():
    """打包后的临时资源目录"""
    return getattr(sys, "_MEIPASS", app_dir())


def find_binary(name):
    exe = name + ".exe" if sys.platform == "win32" else name
    cands = [
        os.path.join(app_dir(), exe),
        os.path.join(app_dir(), "bin", exe),
        os.path.join(res_dir(), exe),
        os.path.join(res_dir(), "bin", exe),
    ]
    for c in cands:
        if os.path.isfile(c):
            return c
    p = shutil.which(name)
    if p:
        return p
    for base in (
        r"C:\ffmpeg\bin",
        r"C:\Program Files\ffmpeg\bin",
        os.path.expandvars(r"%USERPROFILE%\scoop\shims"),
        os.path.expandvars(r"%LOCALAPPDATA%\Microsoft\WinGet\Links"),
    ):
        c = os.path.join(base, exe)
        if os.path.isfile(c):
            return c
    return None


FFMPEG = find_binary("ffmpeg")
PNGQUANT = find_binary("pngquant")
GIFSICLE = find_binary("gifsicle")


def classify(path):
    ext = os.path.splitext(path)[1].lower()
    if ext in GIF_EXT:
        return "GIF"
    if ext in IMAGE_EXT:
        return "图片"
    if ext in VIDEO_EXT:
        return "视频"
    return "未知"


DUR_RE = re.compile(r"Duration:\s*(\d+):(\d+):(\d+(?:\.\d+)?)")
DIM_RE = re.compile(r"(\d{2,5})x(\d{2,5})")
FPS_RE = re.compile(r"([\d.]+)\s*fps")


def probe_media(path):
    """不依赖 ffprobe，直接从 ffmpeg -i 的 stderr 里解析信息"""
    info = {"w": 0, "h": 0, "dur": 0.0, "fps": 0.0, "audio": False, "frames": 0}
    if not FFMPEG:
        return info
    try:
        p = subprocess.run(
            [FFMPEG, "-hide_banner", "-i", path],
            capture_output=True, text=True, encoding="utf-8", errors="ignore",
            creationflags=NO_WINDOW, timeout=60,
        )
        txt = p.stderr or ""
    except Exception:
        return info
    m = DUR_RE.search(txt)
    if m:
        info["dur"] = int(m.group(1)) * 3600 + int(m.group(2)) * 60 + float(m.group(3))
    for line in txt.splitlines():
        ls = line.strip()
        if "Video:" in ls and not info["w"]:
            m = DIM_RE.search(ls)
            if m:
                info["w"], info["h"] = int(m.group(1)), int(m.group(2))
            m = FPS_RE.search(ls)
            if m:
                try:
                    info["fps"] = float(m.group(1))
                except Exception:
                    pass
        if "Audio:" in ls:
            info["audio"] = True
    if info["fps"] > 0 and info["dur"] > 0:
        info["frames"] = int(round(info["fps"] * info["dur"]))
    return info


def run_ffmpeg(args, duration=None, on_prog=None, cancel=None):
    """执行 ffmpeg，带进度回调；返回 (ok, 错误信息)"""
    cmd = [FFMPEG, "-hide_banner", "-nostdin", "-y", "-progress", "pipe:1", "-nostats"] + args
    try:
        p = subprocess.Popen(
            cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            creationflags=NO_WINDOW, text=True, encoding="utf-8", errors="ignore",
            bufsize=1,
        )
    except Exception as e:
        return False, "无法启动 ffmpeg：%s" % e

    err_lines = []

    def drain_err():
        for line in p.stderr:
            line = line.rstrip()
            if line:
                err_lines.append(line)
                if len(err_lines) > 40:
                    del err_lines[0]

    t = threading.Thread(target=drain_err, daemon=True)
    t.start()

    for line in p.stdout:
        if cancel is not None and cancel.is_set():
            try:
                p.kill()
            except Exception:
                pass
            break
        line = line.strip()
        if duration and on_prog and line.startswith("out_time="):
            try:
                hh, mm, ss = line.split("=", 1)[1].split(":")
                done = int(hh) * 3600 + int(mm) * 60 + float(ss)
                on_prog(max(0.0, min(1.0, done / duration)))
            except Exception:
                pass
    p.wait()
    t.join(timeout=2)
    if p.returncode != 0:
        msg = "\n".join(err_lines[-6:]) or ("ffmpeg 退出码 %s" % p.returncode)
        return False, msg
    return True, ""


def even(n):
    n = int(round(n))
    return max(2, n - (n % 2))


def fit_size(w, h, max_side):
    if not max_side or max_side <= 0:
        return w, h
    if max(w, h) <= max_side:
        return w, h
    k = max_side / float(max(w, h))
    return max(2, int(round(w * k))), max(2, int(round(h * k)))


# ----------------------------------------------------------------------------
# 压缩参数
# ----------------------------------------------------------------------------
@dataclass
class Options:
    target_bytes: int = 0          # 0 表示质量优先
    max_side: int = 0              # 0 表示保持原尺寸
    image_format: str = "auto"     # auto / jpeg / webp / png
    gif_keep_fps: bool = True
    video_audio_kbps: int = 128
    out_dir: str = ""
    use_pngquant: bool = True      # PNG 走 pngquant 引擎（需已安装）
    use_gifsicle: bool = True      # GIF 走 gifsicle 有损优化（需已安装）


# ----------------------------------------------------------------------------
# 图片压缩
# ----------------------------------------------------------------------------
def _encode_image(im, fmt, quality):
    buf = io.BytesIO()
    if fmt == "JPEG":
        if im.mode not in ("RGB", "L"):
            bg = Image.new("RGB", im.size, (255, 255, 255))
            if im.mode in ("RGBA", "LA", "P"):
                im = im.convert("RGBA")
                bg.paste(im, mask=im.split()[-1])
                im = bg
            else:
                im = im.convert("RGB")
        im.save(buf, "JPEG", quality=int(quality), optimize=True,
                progressive=True, subsampling=1)
    elif fmt == "WEBP":
        if im.mode == "P":
            im = im.convert("RGBA")
        im.save(buf, "WEBP", quality=int(quality), method=5)
    elif fmt == "PNG":
        if im.mode == "P":
            im.save(buf, "PNG", optimize=True, compress_level=9)
        elif quality >= 255:
            im.save(buf, "PNG", optimize=True, compress_level=9)
        else:
            colors = max(8, min(256, int(quality)))
            q = im.convert("RGBA").quantize(colors=colors, method=Image.FASTOCTREE)
            q.save(buf, "PNG", optimize=True)
    else:
        im.save(buf, "GIF", optimize=True)
    return buf.getvalue()


def _pngquant_encode(im, q_max, tmpdir):
    """用 pngquant 把图压到「最高不超过 q_max 的质量」，返回字节；失败返回 None"""
    inp = os.path.join(tmpdir, "pq_in.png")
    outp = os.path.join(tmpdir, "pq_out.png")
    _safe_rm(outp)
    try:
        im.save(inp, "PNG", compress_level=1)
        cmd = [PNGQUANT, "--quality=0-%d" % int(max(0, min(100, q_max))),
               "--speed", "3", "--strip", "--force", "--output", outp, inp]
        subprocess.run(cmd, capture_output=True, creationflags=NO_WINDOW, timeout=600)
    except Exception:
        return None
    if not os.path.exists(outp):
        return None
    try:
        with open(outp, "rb") as f:
            data = f.read()
    except Exception:
        data = None
    _safe_rm(outp)
    return data or None


def _png_via_pngquant(im, target, on_prog=None):
    """pngquant 路线：二分质量上限逼近目标体积，返回 (字节, 说明) 或 (None, '')"""
    tmpdir = tempfile.mkdtemp(prefix="lp_pq_")
    try:
        if target <= 0:
            data = _pngquant_encode(im, 95, tmpdir)
            return (data, "PNG · pngquant q≤95") if data else (None, "")
        lo, hi, best = 25, 100, None
        while lo <= hi:
            mid = (lo + hi) // 2
            d = _pngquant_encode(im, mid, tmpdir)
            if d is None:
                return None, ""
            if len(d) <= target:
                best = d
                lo = mid + 1
            else:
                hi = mid - 1
        if best is None:
            return None, ""
        return best, "PNG · pngquant"
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


def compress_image(src, opts, out_path, on_prog=None):
    """返回 (ok, 说明, 输出路径)"""
    if not HAS_PIL:
        return False, "缺少 Pillow 组件", None
    im = Image.open(src)
    im = ImageOps.exif_transpose(im)
    orig_size = os.path.getsize(src)
    ext = os.path.splitext(src)[1].lower()
    has_alpha = im.mode in ("RGBA", "LA") or (im.mode == "P" and "transparency" in im.info)

    # ---- 输出格式
    fmt = opts.image_format
    if fmt == "auto":
        if ext in (".jpg", ".jpeg"):
            fmt = "JPEG"
        elif ext == ".webp":
            fmt = "WEBP"
        elif ext == ".png":
            fmt = "PNG"
        else:
            fmt = "JPEG"
    if fmt in ("JPEG", "WEBP") and has_alpha and opts.image_format == "auto":
        fmt = "PNG"  # 有透明通道就不硬转
    if fmt == "JPEG" and has_alpha:
        fmt = "PNG"

    if opts.max_side:
        w, h = fit_size(im.width, im.height, opts.max_side)
        if (w, h) != (im.width, im.height):
            im = im.resize((w, h), Image.LANCZOS)

    target = opts.target_bytes
    work = im
    data = None
    engine = ""

    # ---- 引擎一：PNG 优先交给 pngquant（同体积下色彩保留明显更好）
    if fmt == "PNG" and opts.use_pngquant and PNGQUANT:
        if on_prog:
            on_prog(0.2)
        data, engine = _png_via_pngquant(work, target, on_prog)
        if on_prog:
            on_prog(0.8)

    # ---- 引擎二：内置编码器（质量二分 / 量化阶梯，必要时缩图）
    if data is None:
        engine = fmt
        if target <= 0:
            q = 82 if fmt == "WEBP" else (255 if fmt == "PNG" else 88)
            data = _encode_image(work, fmt, q)
        else:
            for round_i in range(4):
                if fmt == "PNG":
                    cands = [255, 192, 128, 96, 64, 48, 32, 16]
                else:
                    cands = None
                if cands:
                    got = None
                    for c in cands:
                        d = _encode_image(work, fmt, c)
                        if len(d) <= target:
                            got = d
                            break
                    if got is not None:
                        data = got
                        break
                    smallest = len(_encode_image(work, fmt, cands[-1]))
                else:
                    lo, hi, got = 25, 96, None
                    smallest = None
                    while lo <= hi:
                        mid = (lo + hi) // 2
                        d = _encode_image(work, fmt, mid)
                        if len(d) <= target:
                            got = d
                            lo = mid + 1
                        else:
                            smallest = len(d)
                            hi = mid - 1
                    if got is not None:
                        data = got
                        break
                # 缩放后重试
                ratio = math.sqrt(target / float(max(smallest or 1, 1))) * 0.97
                ratio = max(0.35, min(0.95, ratio))
                nw, nh = max(8, int(work.width * ratio)), max(8, int(work.height * ratio))
                work = work.resize((nw, nh), Image.LANCZOS)
                if on_prog:
                    on_prog(0.15 + 0.2 * (round_i + 1))
            if data is None:
                data = _encode_image(work, fmt, 30 if fmt != "PNG" else 16)

    with open(out_path, "wb") as f:
        f.write(data)

    note = "%s · %dx%d" % (engine, work.width, work.height)
    if os.path.getsize(out_path) >= orig_size:
        shutil.copy2(src, out_path)
        note += " · 原文件已更优，保留原图"
    return True, note, out_path


# ----------------------------------------------------------------------------
# GIF 压缩（ffmpeg 调色板方案）
# ----------------------------------------------------------------------------
def _gif_encode(src, out_path, w, h, fps, colors):
    vf = ("scale=%d:%d:flags=lanczos,fps=%s,split[a][b];"
          "[a]palettegen=max_colors=%d:stats_mode=full[p];"
          "[b][p]paletteuse=dither=sierra2_4a:diff_mode=rectangle"
          % (w, h, fps, colors))
    ok, err = run_ffmpeg(["-i", src, "-vf", vf, "-loop", "0", out_path])
    return ok, err


def _gifsicle_optimize(src, dst, lossy=0, level="-O2"):
    """调用 gifsicle 做帧间优化（lossy>0 时允许有损 LZW 压缩）
    -O2 与 -O3 在实测中体积几乎一致但快 3 倍，搜索阶段用 -O2，最终成品可用 -O3"""
    if not GIFSICLE:
        return False
    args = [GIFSICLE, level]
    if lossy > 0:
        args.append("--lossy=%d" % int(lossy))
    args += ["-o", dst, src]
    try:
        r = subprocess.run(args, capture_output=True, creationflags=NO_WINDOW, timeout=600)
    except Exception:
        return False
    return r.returncode == 0 and os.path.exists(dst)


def _gifsicle_fit(path, target):
    """用递增的有损档位逼近目标体积，尽量用最小档位；返回 (最终大小, 实际档位)"""
    if not GIFSICLE or target <= 0:
        return None, 0
    out = path + ".gs.gif"
    base = os.path.getsize(path)
    best = None
    for lossy in (0, 20, 40, 60, 80, 100):
        if not _gifsicle_optimize(path, out, lossy):
            continue
        sz = os.path.getsize(out)
        if best is None or sz < best[0]:
            best = (sz, lossy)
        if sz <= target:
            break
    if best is None:
        _safe_rm(out)
        return None, 0
    if best[0] < base:
        try:
            os.replace(out, path)
        except Exception:
            _safe_rm(out)
            return None, 0
        return best[0], best[1]
    _safe_rm(out)
    return base, 0


def compress_gif(src, opts, out_path, on_prog=None, cancel=None):
    if not FFMPEG:
        return False, "未找到 ffmpeg，无法处理 GIF", None
    info = probe_media(src)
    w0, h0 = info["w"] or 0, info["h"] or 0
    if not w0 or not h0:
        return False, "无法读取 GIF 尺寸", None
    fps0 = info["fps"] or 10.0
    fps = fps0 if opts.gif_keep_fps else min(fps0, 8.0)
    fps = min(fps, 15.0)
    fps_s = ("%g" % fps)

    w, h = fit_size(w0, h0, opts.max_side)
    target = opts.target_bytes

    if target <= 0:
        tmp = out_path + ".part.gif"
        ok, err = _gif_encode(src, tmp, even(w), even(h), fps_s, 256)
        if not ok:
            _safe_rm(tmp)
            return False, "编码失败：" + err.splitlines()[-1][:80], None
        extra = ""
        if opts.use_gifsicle and GIFSICLE:
            opt = tmp + ".o.gif"
            if _gifsicle_optimize(tmp, opt, 0, "-O3") and os.path.getsize(opt) < os.path.getsize(tmp):
                os.replace(opt, tmp)
                extra = " · gifsicle 优化"
            else:
                _safe_rm(opt)
        os.replace(tmp, out_path)
        return True, "GIF · %dx%d · %sfps%s" % (even(w), even(h), fps_s, extra), out_path

    # 关键：一旦缩放，重采样 + 重新抖动会让「每像素体积」明显变大，
    # 所以不能拿原尺寸的体积线性外推。先用小尺寸探针标定 每像素字节数 + gifsicle 折扣率。
    attempts = []
    use_gs = bool(opts.use_gifsicle and GIFSICLE)
    gs_level = [40]          # 默认有损档位：肉眼几乎无感，体积约省 25%
    gs_gain = [1.0]          # gifsicle 实测折扣率（输出/输入）
    bpp = [0.0]              # 每像素字节数（含 gifsicle 优化后）

    pw, ph = even(max(32, w * 0.35)), even(max(32, h * 0.35))
    probe = "%s.probe.gif" % out_path
    ok, _ = _gif_encode(src, probe, pw, ph, fps_s, 256)
    if ok:
        p0 = os.path.getsize(probe)
        post0 = p0
        if use_gs:
            po = probe + ".o.gif"
            if _gifsicle_optimize(probe, po, gs_level[0]):
                s1 = os.path.getsize(po)
                if p0 > 0 and 0 < s1 < p0:
                    gs_gain[0] = s1 / float(p0)
                    post0 = s1
                _safe_rm(po)
        if pw * ph > 0:
            bpp[0] = post0 / float(pw * ph)
        _safe_rm(probe)

    def enc(scale, tag, skip_gs=False):
        """按缩放比编一版，返回优化后的体积；pre 存在 attempts 第 7 位"""
        nw, nh = even(max(32, w * scale)), even(max(32, h * scale))
        p = "%s.%s.gif" % (out_path, tag)
        ok, err = _gif_encode(src, p, nw, nh, fps_s, 256)
        if not ok:
            _safe_rm(p)
            return None
        pre = os.path.getsize(p)
        sz, lv = pre, 0
        # 折算后仍远超目标就跳过 gifsicle（它救不回来，只会白花时间）
        if use_gs and not skip_gs and pre * gs_gain[0] <= max(target * 1.8, 1):
            out = p + ".gs.gif"
            if _gifsicle_optimize(p, out, gs_level[0]):
                s2 = os.path.getsize(out)
                if s2 < pre:
                    os.replace(out, p)
                    sz, lv = s2, gs_level[0]
                else:
                    _safe_rm(out)
            else:
                _safe_rm(out)
        attempts.append((sz, nw, nh, 256, p, lv, pre))
        return sz

    if on_prog:
        on_prog(0.15)
    # 用标定值直接算出目标缩放比，一步到位，少走弯路
    if bpp[0] > 0:
        area_need = target / bpp[0]
        s_first = math.sqrt(max(area_need, 1) / float(w * h))
        s_first = max(0.1, min(1.0, s_first))
    else:
        s_first = 0.6
    size = enc(s_first, "a")
    if size is None:
        return False, "编码失败", None
    eff = size

    for i in range(3):
        # 已经有真正达标的版本就停；注意不能只看预估，必须有一次实测达标
        if any(a[0] <= target for a in attempts):
            break
        ratio = math.sqrt(target / float(max(eff, 1))) * 0.99
        ratio = max(0.2, min(0.95, ratio))
        new_scale = max(0.15, min(1.0, (attempts[-1][1] / float(max(w, 1))) * ratio))
        s2 = enc(new_scale, "b%d" % i)
        if s2 is None:
            break
        eff = s2
        if on_prog:
            on_prog(0.25 + 0.2 * i)

    fit = [a for a in attempts if a[0] <= target]
    bad = [a for a in attempts if a[0] > target]
    # 收敛优化：体积≈面积，直接解出理想缩放比再编一版，尽量吃满配额
    for _i in range(2):
        if not fit:
            break
        cur = max(fit, key=lambda a: a[0])
        if cur[0] >= target * 0.90:
            break
        s_cur = cur[1] / float(w)
        s_next = s_cur * math.sqrt(target / float(max(cur[0], 1))) * 0.99
        if bad:
            s_hi = min(bad, key=lambda a: a[0])[1] / float(w)
            s_next = min(s_next, (s_cur + s_hi) / 2.0)
        s_next = max(s_cur * 1.02, min(1.0, s_next))
        if s_next - s_cur < 0.02:
            break
        sz = enc(s_next, "u%d" % _i)
        if sz is None:
            break
        if sz <= target:
            fit.append(attempts[-1])
        else:
            bad.append(attempts[-1])

    fit = [a for a in attempts if a[0] <= target]
    best = max(fit, key=lambda a: a[0]) if fit else min(attempts, key=lambda a: a[0])
    if not fit and use_gs:
        # 还是超标：把有损档位往上推（40 → 60 → 80 → 100），保住分辨率
        sz, lv = _gifsicle_fit(best[4], target)
        if sz and sz <= target:
            best = (sz, best[1], best[2], 256, best[4], lv, best[6])
            fit = [best]
    if not fit:
        # 仍超标：再降色数搏一次
        p = out_path + ".c.gif"
        ok, _ = _gif_encode(src, p, best[1], best[2], fps_s, 96)
        if ok and os.path.getsize(p) < best[0]:
            _safe_rm(best[4])
            best = (os.path.getsize(p), best[1], best[2], 96, p, 0, 0)
            if best[0] <= target:
                fit = [best]
        else:
            _safe_rm(p)

    if best[0] > target:
        # 确实压不进去：如实报错，别糊弄一个超标文件
        for a in attempts:
            _safe_rm(a[4])
        _safe_rm(best[4])
        return False, ("压不到目标体积（最小只能到 %s），建议调大目标或降低帧率"
                       % human_size(best[0])), None

    for a in attempts:
        if a[4] != best[4]:
            _safe_rm(a[4])
    os.replace(best[4], out_path)
    if on_prog:
        on_prog(1.0)
    note = "GIF · %dx%d · %sfps · %d色" % (best[1], best[2], fps_s, best[3])
    if len(best) > 5 and best[5]:
        note += " · lossy%d" % best[5]
    return True, note, out_path


def _safe_rm(p):
    try:
        if p and os.path.exists(p):
            os.remove(p)
    except Exception:
        pass


# ----------------------------------------------------------------------------
# 视频压缩（x264 两遍）
# ----------------------------------------------------------------------------
def compress_video(src, opts, out_path, on_prog=None, cancel=None):
    if not FFMPEG:
        return False, "未找到 ffmpeg，无法处理视频", None
    info = probe_media(src)
    dur = info["dur"]
    if dur <= 0:
        return False, "无法读取视频时长", None
    w0, h0 = info["w"], info["h"]
    max_side = opts.max_side or 0
    target = opts.target_bytes
    audio_kbps = opts.video_audio_kbps if info["audio"] else 0

    if target > 0:
        total_kbps = target * 8.0 / 1000.0 / dur
        vk = total_kbps - audio_kbps - 10
        # 码率过低时自动降分辨率，避免糊成一团
        if vk < 220 and (not max_side or max_side > 720):
            max_side = 720
        if vk < 110 and (not max_side or max_side > 480):
            max_side = 480
    else:
        vk = 0

    w, h = fit_size(w0 or 1280, h0 or 720, max_side)
    w, h = even(w), even(h)
    scale = "scale=%d:%d:flags=lanczos" % (w, h)
    passlog = os.path.join(os.path.dirname(out_path), ".lp_passlog")

    def one_pass(bitrate_kbps, crf=None, attempt=0):
        base = ["-i", src, "-map", "0:v:0", "-vf", scale]
        if attempt == 0:
            base += ["-c:v", "libx264", "-preset", "medium", "-pix_fmt", "yuv420p"]
        else:
            base += ["-c:v", "libx264", "-preset", "medium", "-pix_fmt", "yuv420p"]
        if crf is not None:
            vargs = ["-crf", str(crf)]
        else:
            vargs = ["-b:v", "%dk" % max(60, int(bitrate_kbps)),
                     "-maxrate", "%dk" % int(bitrate_kbps * 1.6),
                     "-bufsize", "%dk" % int(bitrate_kbps * 3)]
        aargs = []
        if audio_kbps:
            aargs = ["-map", "0:a:0", "-c:a", "aac", "-b:a", "%dk" % audio_kbps]
        else:
            aargs = ["-an"]
        common = ["-movflags", "+faststart"]

        # pass 1
        cmd1 = base + vargs + ["-pass", "1", "-passlogfile", passlog, "-an", "-f", "mp4",
                               os.devnull]
        ok, err = run_ffmpeg(cmd1, dur, (lambda p: on_prog(p * 0.45)) if on_prog else None, cancel)
        if not ok:
            return False, err
        if cancel is not None and cancel.is_set():
            return False, "已取消"
        # pass 2
        cmd2 = base + vargs + ["-pass", "2", "-passlogfile", passlog] + aargs + common + [out_path]
        ok, err = run_ffmpeg(cmd2, dur,
                             (lambda p: on_prog(0.45 + p * 0.55)) if on_prog else None, cancel)
        return ok, err

    if target <= 0:
        ok, err = one_pass(0, crf=23)
        if not ok:
            return False, "编码失败：" + (err.splitlines()[-1][:80] if err else ""), None
        return True, "MP4 · %dx%d · CRF23" % (w, h), out_path

    ok = False
    err = ""
    tgt = float(target)
    for i in range(3):
        kbps = tgt * 8.0 / 1000.0 / dur - audio_kbps - 10
        if kbps < 80:
            audio_kbps = max(64, audio_kbps // 2)
            kbps = tgt * 8.0 / 1000.0 / dur - audio_kbps - 10
        ok, err = one_pass(kbps, None, attempt=i)
        if not ok:
            break
        if cancel is not None and cancel.is_set():
            return False, "已取消", None
        got = os.path.getsize(out_path)
        if got <= target * 1.02:
            break
        tgt *= 0.94  # 略微收紧后重编

    for f in os.listdir(os.path.dirname(out_path)):
        if f.startswith(".lp_passlog"):
            _safe_rm(os.path.join(os.path.dirname(out_path), f))

    if not ok:
        return False, "编码失败：" + (err.splitlines()[-1][:80] if err else ""), None
    if on_prog:
        on_prog(1.0)
    return True, "MP4 · %dx%d · H.264" % (w, h), out_path


# ----------------------------------------------------------------------------
# 统一入口
# ----------------------------------------------------------------------------
def output_path_for(src, opts):
    out_dir = opts.out_dir or os.path.join(os.path.dirname(src), "_压缩")
    os.makedirs(out_dir, exist_ok=True)
    stem, ext = os.path.splitext(os.path.basename(src))
    kind = classify(src)
    if kind == "图片":
        fmt = opts.image_format
        if fmt == "jpeg":
            ext = ".jpg"
        elif fmt == "webp":
            ext = ".webp"
        elif fmt == "png":
            ext = ".png"
    elif kind == "视频":
        ext = ".mp4"
    cand = os.path.join(out_dir, stem + "_压缩" + ext.lower())
    n = 2
    while os.path.exists(cand) and os.path.abspath(cand) != os.path.abspath(src):
        cand = os.path.join(out_dir, "%s_压缩(%d)%s" % (stem, n, ext.lower()))
        n += 1
    return cand


def compress_file(src, opts, on_prog=None, cancel=None):
    kind = classify(src)
    out = output_path_for(src, opts)
    if kind == "图片":
        return compress_image(src, opts, out, on_prog)
    if kind == "GIF":
        return compress_gif(src, opts, out, on_prog, cancel)
    if kind == "视频":
        return compress_video(src, opts, out, on_prog, cancel)
    return False, "不支持的文件类型", None


# ----------------------------------------------------------------------------
# 命令行模式
# ----------------------------------------------------------------------------
def _attach_console():
    """打包成窗口程序后，命令行模式需要把输出接回调用它的控制台"""
    if sys.platform != "win32":
        return
    try:
        import ctypes
        try:
            ctypes.windll.kernel32.SetConsoleOutputCP(65001)
        except Exception:
            pass
        if ctypes.windll.kernel32.AttachConsole(-1):
            sys.stdout = open("CONOUT$", "w", encoding="utf-8", errors="ignore", buffering=1)
            sys.stderr = open("CONOUT$", "w", encoding="utf-8", errors="ignore", buffering=1)
    except Exception:
        pass


def _enable_dpi():
    """开启高 DPI 感知，避免 125%/150% 缩放下界面发虚"""
    if sys.platform != "win32":
        return
    try:
        import ctypes
        try:
            ctypes.windll.shcore.SetProcessDpiAwareness(1)
        except Exception:
            ctypes.windll.user32.SetProcessDPIAware()
    except Exception:
        pass


def cli_main(argv):
    if getattr(sys, "frozen", False):
        _attach_console()
    ap = argparse.ArgumentParser(prog="LightPress", description="轻压 · 体积优化命令行")
    ap.add_argument("inputs", nargs="+")
    ap.add_argument("-t", "--target", type=float, default=0, help="目标体积 MB，0=质量优先")
    ap.add_argument("-s", "--max-side", type=int, default=0, help="最长边像素，0=原尺寸")
    ap.add_argument("-o", "--out", default="", help="输出目录")
    ap.add_argument("-f", "--format", default="auto", choices=["auto", "jpeg", "webp", "png"])
    ap.add_argument("--no-pngquant", action="store_true", help="关闭 pngquant 引擎")
    ap.add_argument("--no-gifsicle", action="store_true", help="关闭 gifsicle 引擎")
    a = ap.parse_args(argv)
    opts = Options(
        target_bytes=int(a.target * 1000 * 1000) if a.target else 0,
        max_side=a.max_side, image_format=a.format, out_dir=a.out,
        use_pngquant=not a.no_pngquant, use_gifsicle=not a.no_gifsicle,
    )
    rc = 0
    for src in a.inputs:
        t0 = time.time()
        before = os.path.getsize(src)
        ok, note, out = compress_file(src, opts)
        if ok:
            after = os.path.getsize(out)
            print("[OK] %s  %s -> %s  (%.0f%%)  %.1fs  %s" % (
                os.path.basename(src), human_size(before), human_size(after),
                after * 100.0 / before, time.time() - t0, note))
        else:
            rc = 1
            print("[FAIL] %s  %s" % (os.path.basename(src), note))
    return rc


# ----------------------------------------------------------------------------
# 界面
# ----------------------------------------------------------------------------
import tkinter as tk
from tkinter import ttk, filedialog, messagebox

try:
    from tkinterdnd2 import TkinterDnD, DND_FILES
    HAS_DND = True
except Exception:
    HAS_DND = False


class RButton(tk.Canvas):
    """圆角按钮（Canvas 绘制，观感比原生 ttk 干净）"""

    def __init__(self, master, text, command=None, kind="primary",
                 width=132, height=38, radius=10, bg=C_CARD, font=None, bg_color=None):
        super().__init__(master, width=width, height=height, bg=bg_color or bg,
                         highlightthickness=0, bd=0, cursor="hand2")
        self._cmd = command
        self._cw, self._ch, self._cr = width, height, radius
        self._text = text
        self._font = font or (FONT_UI, 10)
        self._enabled = True
        self._hover = False
        if kind == "primary":
            self._c = (C_PRIMARY, C_PRIMARY_D, "#FFFFFF")
        elif kind == "ghost":
            self._c = ("#FFFFFF", C_PRIMARY_L, C_PRIMARY)
        else:
            self._c = ("#F1F4F9", "#E5E9F2", "#43506B")
        self._draw()
        self.bind("<Button-1>", self._press)
        self.bind("<ButtonRelease-1>", self._release)
        self.bind("<Enter>", self._enter)
        self.bind("<Leave>", self._leave)

    def _draw(self):
        self.delete("all")
        if not self._enabled:
            fill, fg = "#E8ECF4", "#9AA4B8"
        elif self._hover:
            fill, fg = self._c[1], self._c[2]
        else:
            fill, fg = self._c[0], self._c[2]
        x1, y1, x2, y2, r = 0.5, 0.5, self._cw - 0.5, self._ch - 0.5, self._cr
        pts = [x1 + r, y1, x2 - r, y1, x2, y1, x2, y1 + r, x2, y2 - r, x2, y2,
               x2 - r, y2, x1 + r, y2, x1, y2, x1, y2 - r, x1, y1 + r, x1, y1]
        self.create_polygon(pts, smooth=True, splinesteps=24, fill=fill, outline=fill)
        if self._enabled and self._c[0] == "#FFFFFF":
            self.create_polygon(pts, smooth=True, splinesteps=24, fill="", outline=C_LINE)
        self.create_text(self._cw / 2, self._ch / 2, text=self._text, fill=fg, font=self._font)

    def _press(self, _):
        if self._enabled:
            self._hover = True
            self._draw()

    def _release(self, _):
        if self._enabled and self._cmd:
            self._cmd()

    def _enter(self, _):
        self._hover = True
        self._draw()

    def _leave(self, _):
        self._hover = False
        self._draw()

    def set_enabled(self, v):
        self._enabled = bool(v)
        self.configure(cursor="hand2" if v else "arrow")
        self._draw()

    def set_text(self, t):
        self._text = t
        self._draw()


def make_card(parent, pad=14, bg=C_CARD):
    outer = tk.Frame(parent, bg=C_CARD, highlightbackground=C_LINE,
                     highlightcolor=C_LINE, highlightthickness=1, bd=0)
    inner = tk.Frame(outer, bg=bg)
    inner.pack(fill="both", expand=True, padx=pad, pady=pad)
    return outer, inner


class App:
    def __init__(self):
        root_cls = TkinterDnD.Tk if HAS_DND else tk.Tk
        self.root = root_cls()
        self.root.title("%s %s · 图片视频体积优化" % (APP_NAME, APP_EN))
        self.root.configure(bg=C_BG)
        ico = os.path.join(res_dir(), "app.ico")
        if os.path.isfile(ico):
            try:
                self.root.iconbitmap(ico)
            except Exception:
                pass

        # 缩放
        try:
            self.dpi = self.root.winfo_fpixels("1i")
        except Exception:
            self.dpi = 96.0
        self.k = max(1.0, self.dpi / 96.0)

        w, h = self.S(1040), self.S(806)
        sw, sh = self.root.winfo_screenwidth(), self.root.winfo_screenheight()
        self.root.geometry("%dx%d+%d+%d" % (w, h, max(0, (sw - w) // 2), max(0, (sh - h) // 3)))
        self.root.minsize(self.S(900), self.S(700))

        self.jobs = []            # list[dict]
        self.q = queue.Queue()
        self.worker = None
        self.cancel = threading.Event()
        self.running = False
        self.cfg = self.load_cfg()

        self._style()
        self._build()
        self._poll()

    # ---------------- 尺寸换算
    def S(self, v):
        return int(round(v * self.k))

    def _style(self):
        st = ttk.Style(self.root)
        try:
            st.theme_use("clam")
        except Exception:
            pass
        st.configure("LP.Treeview", background=C_CARD, fieldbackground=C_CARD,
                     foreground=C_TEXT, rowheight=self.S(30), borderwidth=0,
                     font=(FONT_UI, 10))
        st.configure("LP.Treeview.Heading", background="#F7F9FC", foreground="#6B7688",
                     relief="flat", borderwidth=0, font=(FONT_UI, 9, "bold"),
                     padding=(self.S(6), self.S(6)))
        st.map("LP.Treeview", background=[("selected", C_PRIMARY_L)],
               foreground=[("selected", C_TEXT)])
        st.map("LP.Treeview.Heading", background=[("active", "#EFF2F8")])
        st.layout("LP.Treeview", [("LP.Treeview.treearea", {"sticky": "nswe"})])
        st.configure("LP.Horizontal.TProgressbar", troughcolor="#E9EDF5",
                     background=C_PRIMARY, thickness=self.S(8), borderwidth=0)
        st.configure("LP.TEntry", fieldbackground="#FFFFFF", bordercolor=C_LINE,
                     lightcolor=C_LINE, darkcolor=C_LINE, padding=self.S(6))
        st.configure("LP.TCombobox", fieldbackground="#FFFFFF", bordercolor=C_LINE,
                     padding=self.S(5))
        st.configure("LP.TCheckbutton", background=C_CARD, foreground=C_TEXT,
                     font=(FONT_UI, 9), focuscolor=C_CARD)
        st.map("LP.TCheckbutton",
               background=[("active", C_CARD), ("disabled", C_CARD)],
               foreground=[("disabled", "#B4BCCB")])

    # ---------------- 布局
    def _build(self):
        # 顶栏
        head = tk.Frame(self.root, bg=C_CARD, height=self.S(66))
        head.pack(fill="x")
        head.pack_propagate(False)
        tk.Frame(self.root, bg=C_LINE, height=1).pack(fill="x")

        box = tk.Frame(head, bg=C_CARD)
        box.pack(side="left", padx=self.S(20), pady=self.S(12))
        tk.Label(box, text=APP_NAME, bg=C_CARD, fg=C_TEXT,
                 font=(FONT_UI, 16, "bold")).pack(side="left")
        tk.Label(box, text="  %s · 图片 / GIF / 视频 压到指定体积" % APP_EN, bg=C_CARD,
                 fg=C_SUB, font=(FONT_UI, 9)).pack(side="left", pady=(self.S(4), 0))

        self.ff_lbl = tk.Label(head, text="", bg=C_CARD, font=(FONT_UI, 9))
        self.ff_lbl.pack(side="right", padx=self.S(20))
        parts = []
        parts.append(("ffmpeg", FFMPEG, True))
        parts.append(("pngquant", PNGQUANT, False))
        parts.append(("gifsicle", GIFSICLE, False))
        txt = "   ".join(("● " if p[1] else "○ ") + p[0] for p in parts)
        ok_all = bool(FFMPEG)
        self.ff_lbl.configure(text=txt, fg=C_OK if ok_all else C_ERR)
        if not FFMPEG:
            self.ff_lbl.configure(text=txt + "（缺 ffmpeg，视频/GIF 不可用）", fg=C_ERR)

        body = tk.Frame(self.root, bg=C_BG)
        body.pack(fill="both", expand=True, padx=self.S(16), pady=(self.S(14), self.S(6)))

        # ---- 工具条
        bar = tk.Frame(body, bg=C_BG)
        bar.pack(fill="x")
        RButton(bar, "+  添加文件", self.add_files, "ghost", self.S(112), self.S(36),
                bg_color=C_BG).pack(side="left")
        RButton(bar, "添加文件夹", self.add_folder, "plain", self.S(100), self.S(36),
                bg_color=C_BG).pack(side="left", padx=(self.S(8), 0))
        RButton(bar, "移除选中", self.remove_selected, "plain", self.S(88), self.S(36),
                bg_color=C_BG).pack(side="left", padx=(self.S(8), 0))
        RButton(bar, "清空", self.clear_all, "plain", self.S(64), self.S(36),
                bg_color=C_BG).pack(side="left", padx=(self.S(8), 0))
        self.sum_lbl = tk.Label(bar, text="0 个文件", bg=C_BG, fg=C_SUB, font=(FONT_UI, 9))
        self.sum_lbl.pack(side="right")

        # ---- 列表卡片
        card, inner = make_card(body, pad=self.S(1))
        card.pack(fill="both", expand=True, pady=(self.S(10), self.S(10)))

        cols = ("name", "kind", "before", "after", "ratio", "status")
        self.tree = ttk.Treeview(inner, columns=cols, show="headings",
                                 style="LP.Treeview", selectmode="extended")
        heads = {"name": "文件名", "kind": "类型", "before": "原始大小",
                 "after": "处理后", "ratio": "压缩", "status": "状态"}
        widths = {"name": 360, "kind": 70, "before": 100, "after": 100, "ratio": 80, "status": 190}
        for c in cols:
            self.tree.heading(c, text=heads[c], anchor="w")
            self.tree.column(c, width=self.S(widths[c]), anchor="w",
                             stretch=(c == "name"))
        vs = ttk.Scrollbar(inner, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=vs.set)
        vs.pack(side="right", fill="y")
        self.tree.pack(side="left", fill="both", expand=True)
        self.tree.tag_configure("ok", foreground=C_OK)
        self.tree.tag_configure("err", foreground=C_ERR)
        self.tree.tag_configure("skip", foreground=C_WARN)
        self.tree.bind("<Double-1>", self.open_selected)
        self.tree.bind("<Delete>", lambda e: self.remove_selected())

        if HAS_DND:
            try:
                self.tree.drop_target_register(DND_FILES)
                self.tree.dnd_bind("<<Drop>>", self.on_drop)
                self._drop_hint = tk.Label(inner, text="把图片 / GIF / 视频拖到这里，或点左上角添加",
                                           bg=C_CARD, fg="#B7BFCE", font=(FONT_UI, 9))
                self._drop_hint.place(relx=0.5, rely=0.5, anchor="center")
                # 提示文字盖在列表上，也要能接住拖放
                for w in (self._drop_hint, self.root):
                    try:
                        w.drop_target_register(DND_FILES)
                        w.dnd_bind("<<Drop>>", self.on_drop)
                    except Exception:
                        pass
            except Exception:
                pass

        # ---- 设置卡片
        card2, s = make_card(body, pad=self.S(14))
        card2.pack(fill="x")

        row1 = tk.Frame(s, bg=C_CARD)
        row1.pack(fill="x")
        tk.Label(row1, text="目标体积", bg=C_CARD, fg=C_TEXT,
                 font=(FONT_UI, 10)).pack(side="left")
        self.target_var = tk.StringVar(value=self.cfg.get("target", "7.0"))
        self.target_entry = ttk.Entry(row1, textvariable=self.target_var, width=7,
                                      style="LP.TEntry", font=(FONT_UI, 10))
        self.target_entry.pack(side="left", padx=(self.S(10), self.S(4)))
        tk.Label(row1, text="MB", bg=C_CARD, fg=C_SUB, font=(FONT_UI, 9)).pack(side="left")

        for mb in ("2", "5", "7", "10"):
            b = tk.Label(row1, text=mb + "MB", bg="#F1F4F9", fg="#5A6478",
                         font=(FONT_UI, 9), padx=self.S(8), pady=self.S(3), cursor="hand2")
            b.pack(side="left", padx=(self.S(6), 0))
            b.bind("<Button-1>", lambda e, v=mb: self.target_var.set(v + ".0"))

        tk.Label(row1, text="（按 1MB=1,000,000 字节计算；留空或 0 = 质量优先）", bg=C_CARD,
                 fg="#AEB6C6", font=(FONT_UI, 9)).pack(side="left", padx=(self.S(12), 0))

        row2 = tk.Frame(s, bg=C_CARD)
        row2.pack(fill="x", pady=(self.S(12), 0))
        tk.Label(row2, text="最长边", bg=C_CARD, fg=C_TEXT, font=(FONT_UI, 10)).pack(side="left")
        self.side_var = tk.StringVar(value=str(self.cfg.get("max_side", "0")))
        ttk.Entry(row2, textvariable=self.side_var, width=7, style="LP.TEntry",
                  font=(FONT_UI, 10)).pack(side="left", padx=(self.S(10), self.S(4)))
        tk.Label(row2, text="px", bg=C_CARD, fg=C_SUB, font=(FONT_UI, 9)).pack(side="left")
        tk.Label(row2, text="0 = 保持原始尺寸；填 1920 则长边不超过 1920", bg=C_CARD,
                 fg="#AEB6C6", font=(FONT_UI, 9)).pack(side="left", padx=(self.S(8), 0))

        tk.Label(row2, text="图片格式", bg=C_CARD, fg=C_TEXT,
                 font=(FONT_UI, 10)).pack(side="left", padx=(self.S(28), self.S(8)))
        self.fmt_var = tk.StringVar(value=self._fmt_label(self.cfg.get("image_format")))
        cb = ttk.Combobox(row2, textvariable=self.fmt_var, width=16, state="readonly",
                          values=FMT_LABELS, style="LP.TCombobox",
                          font=(FONT_UI, 9))
        cb.pack(side="left")

        row3 = tk.Frame(s, bg=C_CARD)
        row3.pack(fill="x", pady=(self.S(12), 0))
        tk.Label(row3, text="输出到", bg=C_CARD, fg=C_TEXT, font=(FONT_UI, 10)).pack(side="left")
        self.out_var = tk.StringVar(value=self.cfg.get("out_dir", ""))
        self.out_entry = ttk.Entry(row3, textvariable=self.out_var, style="LP.TEntry",
                                   font=(FONT_UI, 9), width=44)
        self.out_entry.pack(side="left", padx=(self.S(10), self.S(6)), fill="x", expand=True)
        RButton(row3, "选择…", self.pick_out_dir, "plain", self.S(66), self.S(30),
                bg_color=C_CARD).pack(side="left")
        tk.Label(row3, text="留空 = 每个文件旁新建「_压缩」文件夹", bg=C_CARD, fg="#AEB6C6",
                 font=(FONT_UI, 9)).pack(side="left", padx=(self.S(8), 0))

        row4 = tk.Frame(s, bg=C_CARD)
        row4.pack(fill="x", pady=(self.S(12), 0))
        tk.Label(row4, text="增强引擎", bg=C_CARD, fg=C_TEXT,
                 font=(FONT_UI, 10)).pack(side="left")
        self.pq_var = tk.BooleanVar(value=bool(self.cfg.get("use_pngquant", True)))
        self.gs_var = tk.BooleanVar(value=bool(self.cfg.get("use_gifsicle", True)))
        self._engine_check(row4, "pngquant 压 PNG", self.pq_var, PNGQUANT)
        self._engine_check(row4, "gifsicle 压 GIF", self.gs_var, GIFSICLE)
        tk.Label(row4, text="（PNG 同体积色彩更完整；GIF 用轻微有损换体积，比降分辨率划算）",
                 bg=C_CARD, fg="#AEB6C6", font=(FONT_UI, 9)).pack(side="left", padx=(self.S(14), 0))

        # ---- 底部
        foot = tk.Frame(body, bg=C_BG)
        foot.pack(fill="x", pady=(self.S(4), self.S(10)))
        self.pbar = ttk.Progressbar(foot, style="LP.Horizontal.TProgressbar",
                                    mode="determinate", maximum=1000)
        self.pbar.pack(fill="x", pady=(0, self.S(10)))
        line = tk.Frame(foot, bg=C_BG)
        line.pack(fill="x")
        self.go_btn = RButton(line, "开始压缩", self.start, "primary", self.S(132), self.S(40),
                              bg_color=C_BG, font=(FONT_UI, 10, "bold"))
        self.go_btn.pack(side="left")
        self.cancel_btn = RButton(line, "取消", self.do_cancel, "plain", self.S(80), self.S(40),
                                  bg_color=C_BG)
        self.cancel_btn.pack(side="left", padx=(self.S(10), 0))
        self.cancel_btn.set_enabled(False)
        RButton(line, "打开输出目录", self.open_out_dir, "plain", self.S(116), self.S(40),
                bg_color=C_BG).pack(side="left", padx=(self.S(10), 0))
        self.status_lbl = tk.Label(line, text="就绪", bg=C_BG, fg=C_SUB, font=(FONT_UI, 9))
        self.status_lbl.pack(side="right")

    # ---------------- 配置
    def _fmt_label(self, v):
        if not v:
            return FMT_LABELS[0]
        if v in FMT_MAP:
            return v
        return FMT_RMAP.get(v, FMT_LABELS[0])

    def cfg_path(self):
        base = os.path.join(os.path.expanduser("~"), ".lightpress")
        try:
            os.makedirs(base, exist_ok=True)
        except Exception:
            return None
        return os.path.join(base, "config.json")

    def load_cfg(self):
        p = self.cfg_path()
        if p and os.path.isfile(p):
            try:
                with open(p, "r", encoding="utf-8") as f:
                    return json.load(f)
            except Exception:
                pass
        return {}

    def save_cfg(self):
        p = self.cfg_path()
        if not p:
            return
        try:
            with open(p, "w", encoding="utf-8") as f:
                json.dump({"target": self.target_var.get(), "max_side": self.side_var.get(),
                           "image_format": self.fmt_var.get(), "out_dir": self.out_var.get(),
                           "use_pngquant": bool(self.pq_var.get()),
                           "use_gifsicle": bool(self.gs_var.get())}, f)
        except Exception:
            pass

    # ---------------- 文件管理
    def add_paths(self, paths):
        added = 0
        for p in paths:
            p = os.path.abspath(p)
            if os.path.isdir(p):
                for root, _, files in os.walk(p):
                    for f in files:
                        if os.path.splitext(f)[1].lower() in (IMAGE_EXT | GIF_EXT | VIDEO_EXT):
                            added += self._add_one(os.path.join(root, f))
                continue
            added += self._add_one(p)
        self.refresh_sum()
        if added and hasattr(self, "_drop_hint"):
            self._drop_hint.place_forget()

    def _add_one(self, p):
        if not os.path.isfile(p):
            return 0
        kind = classify(p)
        if kind == "未知":
            return 0
        if any(j["path"] == p for j in self.jobs):
            return 0
        try:
            size = os.path.getsize(p)
        except Exception:
            return 0
        job = {"path": p, "name": os.path.basename(p), "kind": kind, "size": size,
               "after": None, "status": "待处理", "tag": "", "note": ""}
        self.jobs.append(job)
        self.tree.insert("", "end", iid=str(len(self.jobs) - 1),
                         values=(job["name"], kind, human_size(size), "-", "-", "待处理"))
        return 1

    def add_files(self):
        types = [("支持的文件", "*.jpg *.jpeg *.png *.webp *.bmp *.gif *.mp4 *.mov *.mkv *.avi *.webm *.m4v"),
                 ("所有文件", "*.*")]
        fs = filedialog.askopenfilenames(title="选择文件", filetypes=types)
        if fs:
            self.add_paths(list(fs))

    def add_folder(self):
        d = filedialog.askdirectory(title="选择文件夹")
        if d:
            self.add_paths([d])

    def on_drop(self, event):
        try:
            paths = self.root.tk.splitlist(event.data)
        except Exception:
            paths = [event.data]
        self.add_paths([p for p in paths])

    def remove_selected(self):
        if self.running:
            return
        sel = list(self.tree.selection())
        if not sel:
            return
        for iid in sorted(sel, key=lambda x: int(x), reverse=True):
            idx = int(iid)
            self.jobs[idx]["removed"] = True
            self.tree.delete(iid)
        self.jobs = [j for j in self.jobs if not j.get("removed")]
        self._reindex()

    def _reindex(self):
        for iid in self.tree.get_children():
            self.tree.delete(iid)
        for i, j in enumerate(self.jobs):
            self.tree.insert("", "end", iid=str(i), values=self._row_values(j), tags=(j.get("tag", ""),))
        self.refresh_sum()

    def _row_values(self, j):
        ratio = "-"
        if j["after"] and j["size"]:
            ratio = "%.0f%%" % (j["after"] * 100.0 / j["size"])
        return (j["name"], j["kind"], human_size(j["size"]),
                human_size(j["after"]) if j["after"] else "-", ratio, j["status"])

    def clear_all(self):
        if self.running:
            return
        self.jobs.clear()
        for iid in self.tree.get_children():
            self.tree.delete(iid)
        self.pbar["value"] = 0
        self.status_lbl.configure(text="就绪")
        self.refresh_sum()
        if hasattr(self, "_drop_hint"):
            self._drop_hint.place(relx=0.5, rely=0.5, anchor="center")

    def refresh_sum(self):
        n = len(self.jobs)
        total = sum(j["size"] for j in self.jobs)
        done = [j for j in self.jobs if j["after"]]
        txt = "%d 个文件 · 共 %s" % (n, human_size(total))
        if done:
            saved = sum(j["size"] for j in done) - sum(j["after"] for j in done)
            txt += "  |  已节省 %s" % human_size(max(0, saved))
        self.sum_lbl.configure(text=txt)

    def _engine_check(self, parent, text, var, path):
        cb = tk.Checkbutton(parent, text=text, variable=var, bg=C_CARD, fg=C_TEXT,
                            activebackground=C_CARD, activeforeground=C_TEXT,
                            selectcolor="#FFFFFF", font=(FONT_UI, 9), bd=0,
                            highlightthickness=0, cursor="hand2", anchor="w",
                            padx=self.S(2), pady=0)
        cb.pack(side="left", padx=(self.S(12), 0))
        if not path:
            cb.configure(state="disabled", fg="#B4BCCB", cursor="arrow")
            var.set(False)
            tk.Label(parent, text="未安装", bg=C_CARD, fg="#C2C9D6",
                     font=(FONT_UI, 8)).pack(side="left", padx=(self.S(2), 0))
        return cb

    def pick_out_dir(self):
        d = filedialog.askdirectory(title="选择输出目录")
        if d:
            self.out_var.set(d)

    def open_out_dir(self):
        d = self.out_var.get().strip()
        if not d:
            if self.jobs:
                d = os.path.join(os.path.dirname(self.jobs[0]["path"]), "_压缩")
            else:
                return
        if os.path.isdir(d):
            os.startfile(d) if sys.platform == "win32" else subprocess.Popen(["xdg-open", d])
        else:
            messagebox.showinfo("提示", "输出目录还不存在，先压缩一次吧。")

    def open_selected(self, _=None):
        sel = self.tree.selection()
        if not sel:
            return
        j = self.jobs[int(sel[0])]
        if j.get("out") and os.path.isfile(j["out"]):
            try:
                os.startfile(j["out"])
            except Exception:
                pass

    # ---------------- 运行
    def collect_opts(self):
        t = self.target_var.get().strip()
        try:
            target_mb = float(t) if t else 0.0
        except ValueError:
            raise ValueError("目标体积需要填数字（MB）")
        try:
            side = int(float(self.side_var.get().strip() or 0))
        except ValueError:
            raise ValueError("最长边需要填整数")
        return Options(
            target_bytes=int(target_mb * 1000 * 1000) if target_mb > 0 else 0,
            max_side=max(0, side),
            image_format=FMT_MAP.get(self.fmt_var.get(), "auto"),
            out_dir=self.out_var.get().strip(),
            use_pngquant=bool(self.pq_var.get()) and bool(PNGQUANT),
            use_gifsicle=bool(self.gs_var.get()) and bool(GIFSICLE),
        )

    def start(self):
        if self.running:
            return
        if not self.jobs:
            messagebox.showinfo("提示", "先添加一些文件吧。")
            return
        try:
            opts = self.collect_opts()
        except ValueError as e:
            messagebox.showwarning("参数有误", str(e))
            return
        if not FFMPEG and any(j["kind"] in ("GIF", "视频") for j in self.jobs):
            if not messagebox.askokcancel(
                    "缺少 ffmpeg",
                    "没有找到 ffmpeg，GIF 和视频无法处理（图片仍可正常压缩）。\n"
                    "把 ffmpeg.exe 放到本程序同目录即可。\n\n继续只处理图片吗？"):
                return

        self.save_cfg()
        self.cancel.clear()
        self.running = True
        self.go_btn.set_enabled(False)
        self.go_btn.set_text("处理中…")
        self.cancel_btn.set_enabled(True)
        self.pbar["value"] = 0
        for j in self.jobs:
            j.update({"after": None, "status": "排队中", "tag": "", "out": None})
        self._refresh_rows()

        self.worker = threading.Thread(target=self._work, args=(opts,), daemon=True)
        self.worker.start()

    def do_cancel(self):
        if self.running:
            self.cancel.set()
            self.status_lbl.configure(text="正在取消…")

    def _work(self, opts):
        total = len(self.jobs)
        ok_cnt = 0
        saved = 0
        for i, job in enumerate(self.jobs):
            if self.cancel.is_set():
                self.q.put(("status", i, "已取消", "", None))
                continue
            self.q.put(("status", i, "处理中…", "", None))
            self.q.put(("overall", (i / float(total))))

            def prog(p, i=i, total=total):
                self.q.put(("overall", (i + max(0.0, min(1.0, p))) / float(total)))

            t0 = time.time()
            try:
                ok, note, out = compress_file(job["path"], opts, prog, self.cancel)
            except Exception as e:
                ok, note, out = False, "异常：%s" % e, None

            if ok and out:
                after = os.path.getsize(out)
                job["after"] = after
                job["out"] = out
                if after < job["size"]:
                    saved += job["size"] - after
                ok_cnt += 1
                self.q.put(("status", i, "完成 · %.1fs" % (time.time() - t0), "ok", after))
                self.q.put(("log", "%s → %s（%s → %s，%s）" % (
                    job["name"], os.path.basename(out), human_size(job["size"]),
                    human_size(after), note)))
            else:
                self.q.put(("status", i, "失败", "err", None))
                self.q.put(("log", "✗ %s：%s" % (job["name"], note)))

        self.q.put(("overall", 1.0))
        self.q.put(("done", (ok_cnt, total, saved)))

    def _refresh_rows(self):
        for i, j in enumerate(self.jobs):
            iid = str(i)
            if self.tree.exists(iid):
                self.tree.item(iid, values=self._row_values(j), tags=(j["tag"],))

    def _poll(self):
        try:
            while True:
                kind, *rest = self.q.get_nowait()
                if kind == "overall":
                    self.pbar["value"] = int(rest[0] * 1000)
                elif kind == "status":
                    idx, text, tag, after = rest
                    if idx < len(self.jobs):
                        j = self.jobs[idx]
                        j["status"] = text
                        j["tag"] = tag
                        if after:
                            j["after"] = after
                        iid = str(idx)
                        if self.tree.exists(iid):
                            self.tree.item(iid, values=self._row_values(j), tags=(tag,))
                            self.tree.see(iid)
                    self.status_lbl.configure(text=text if idx is not None else "就绪")
                elif kind == "log":
                    self.status_lbl.configure(text=rest[0][:80])
                elif kind == "done":
                    ok_cnt, total, saved = rest[0]
                    self.running = False
                    self.go_btn.set_enabled(True)
                    self.go_btn.set_text("开始压缩")
                    self.cancel_btn.set_enabled(False)
                    self.pbar["value"] = 1000
                    self.refresh_sum()
                    self.status_lbl.configure(
                        text="完成 %d/%d · 共节省 %s" % (ok_cnt, total, human_size(max(0, saved))))
        except queue.Empty:
            pass
        finally:
            self.root.after(80, self._poll)

    def run(self):
        self.root.mainloop()


def main():
    if "--cli" in sys.argv:
        argv = [a for a in sys.argv[1:] if a != "--cli"]
        sys.exit(cli_main(argv))
    _enable_dpi()
    app = App()
    app.run()


if __name__ == "__main__":
    main()

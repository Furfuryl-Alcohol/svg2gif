#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""svg2gif 的图形界面。

    python svg2gif_gui.py            # 打开空界面
    python svg2gif_gui.py fig.svg    # 顺便把文件填进去

底层全部走 svg2gif.convert()，命令行和 GUI 的行为完全一致。
"""

import os
import queue
import subprocess
import sys
import threading
import traceback

import tkinter as tk
from tkinter import ttk, filedialog, messagebox, colorchooser

import svg2gif as core

try:
    from PIL import Image, ImageTk
    HAVE_PREVIEW = True
except Exception:                     # 没有 Pillow 也能开界面，只是没有预览
    HAVE_PREVIEW = False

# 跨平台字体：Windows / macOS / Linux 上各挑一个实际装了的
FONT_FAMILIES = ("Microsoft YaHei UI", "Microsoft YaHei", "PingFang SC",
                 "Hiragino Sans GB", "Noto Sans CJK SC", "Source Han Sans SC",
                 "WenQuanYi Micro Hei", "SimHei", "Arial Unicode MS")
MONO_FAMILIES = ("Consolas", "Menlo", "DejaVu Sans Mono", "Courier New")


def pick_family(root, names, fallback):
    try:
        from tkinter import font as tkfont
        available = set(tkfont.families(root))
        for name in names:
            if name in available:
                return name
    except Exception:
        pass
    return fallback


class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("SVG → GIF 转换器")
        self.minsize(820, 660)
        self.geometry("940x720+80+60")
        self.configure(padx=12, pady=10)
        try:
            self.tk.call("tk", "scaling", 1.25)
        except Exception:
            pass

        self.font = (pick_family(self, FONT_FAMILIES, "TkDefaultFont"), 9)
        self.mono = (pick_family(self, MONO_FAMILIES, "TkFixedFont"), 9)

        style = ttk.Style(self)
        for theme in ("vista" if sys.platform.startswith("win") else "clam", "clam"):
            try:
                style.theme_use(theme)
                break
            except Exception:
                continue
        style.configure(".", font=self.font)
        style.configure("TLabelframe.Label", font=self.font)
        style.configure("Hint.TLabel", foreground="#666")

        self.q = queue.Queue()
        self.worker = None
        self.preview_img = None
        self.last_result = None

        self._build()
        self.after(120, self._drain)

    # ------------------------------------------------------------------ UI
    def _build(self):
        root = ttk.Frame(self)
        root.pack(fill="both", expand=True)
        root.columnconfigure(1, weight=1)

        # --- 文件 -----------------------------------------------------
        ttk.Label(root, text="源 SVG").grid(row=0, column=0, sticky="w", pady=3)
        self.src = tk.StringVar()
        ttk.Entry(root, textvariable=self.src, font=self.font).grid(
            row=0, column=1, sticky="ew", padx=6)
        ttk.Button(root, text="浏览…", command=self.pick_src, width=8).grid(row=0, column=2)

        ttk.Label(root, text="输出 GIF").grid(row=1, column=0, sticky="w", pady=3)
        self.dst = tk.StringVar()
        ttk.Entry(root, textvariable=self.dst, font=self.font).grid(
            row=1, column=1, sticky="ew", padx=6)
        ttk.Button(root, text="浏览…", command=self.pick_dst, width=8).grid(row=1, column=2)

        # --- 尺寸 -----------------------------------------------------
        box = ttk.LabelFrame(root, text="输出尺寸", padding=10)
        box.grid(row=2, column=0, columnspan=3, sticky="ew", pady=(10, 6))
        self.mode = tk.StringVar(value="size")

        ttk.Radiobutton(box, text="按宽度", variable=self.mode, value="width",
                        command=self._sync).grid(row=0, column=0, sticky="w")
        self.spin_w = ttk.Spinbox(box, from_=100, to=4000, width=7, font=self.font)
        self.spin_w.set(1000)
        self.spin_w.grid(row=0, column=1, padx=(4, 2))
        ttk.Label(box, text="px", style="Hint.TLabel").grid(row=0, column=2, sticky="w")

        ttk.Radiobutton(box, text="按高度", variable=self.mode, value="height",
                        command=self._sync).grid(row=0, column=3, padx=(18, 0), sticky="w")
        self.spin_h = ttk.Spinbox(box, from_=100, to=4000, width=7, font=self.font)
        self.spin_h.set(700)
        self.spin_h.grid(row=0, column=4, padx=(4, 2))
        ttk.Label(box, text="px", style="Hint.TLabel").grid(row=0, column=5, sticky="w")

        ttk.Radiobutton(box, text="限制文件大小 ≤", variable=self.mode, value="size",
                        command=self._sync).grid(row=1, column=0, pady=(8, 0), sticky="w")
        self.spin_mb = ttk.Spinbox(box, from_=0.1, to=100, increment=0.5, width=7, font=self.font)
        self.spin_mb.set(3)
        self.spin_mb.grid(row=1, column=1, padx=(4, 2), pady=(8, 0))
        ttk.Label(box, text="MB（自动挑分辨率与色数）", style="Hint.TLabel").grid(
            row=1, column=2, columnspan=4, sticky="w", pady=(8, 0))

        # --- 选项 -----------------------------------------------------
        opt = ttk.LabelFrame(root, text="选项", padding=10)
        opt.grid(row=3, column=0, columnspan=3, sticky="ew", pady=(4, 6))

        ttk.Label(opt, text="帧率").grid(row=0, column=0, sticky="w")
        self.spin_fps = ttk.Spinbox(opt, from_=2, to=60, width=6, font=self.font)
        self.spin_fps.set(20)
        self.spin_fps.grid(row=0, column=1, padx=(4, 16))

        ttk.Label(opt, text="色数").grid(row=0, column=2, sticky="w")
        self.spin_col = ttk.Spinbox(opt, from_=32, to=255, increment=16, width=6, font=self.font)
        self.spin_col.set(255)
        self.spin_col.grid(row=0, column=3, padx=(4, 16))

        ttk.Label(opt, text="循环").grid(row=0, column=4, sticky="w")
        self.spin_loop = ttk.Spinbox(opt, from_=0, to=999, width=6, font=self.font)
        self.spin_loop.set(0)
        self.spin_loop.grid(row=0, column=5, padx=(4, 4))
        ttk.Label(opt, text="次（0 = 无限）", style="Hint.TLabel").grid(row=0, column=6, sticky="w")

        ttk.Label(opt, text="背景").grid(row=1, column=0, sticky="w", pady=(8, 0))
        self.bgmode = tk.StringVar(value="透明")
        cb = ttk.Combobox(opt, textvariable=self.bgmode, width=8, state="readonly",
                          values=("透明", "白色", "自定义"), font=self.font)
        cb.grid(row=1, column=1, padx=(4, 6), pady=(8, 0))
        cb.bind("<<ComboboxSelected>>", lambda e: self._sync())
        self.bg_color = "#ffffff"
        self.swatch = tk.Button(opt, text="  ", bg=self.bg_color, width=3,
                                command=self.pick_color, relief="flat", bd=1)
        self.swatch.grid(row=1, column=2, sticky="w", pady=(8, 0))

        self.var_verify = tk.BooleanVar(value=True)
        ttk.Checkbutton(opt, text="导出后自检", variable=self.var_verify).grid(
            row=1, column=3, columnspan=4, sticky="w", pady=(8, 0))

        self.var_trim = tk.BooleanVar(value=True)
        ttk.Checkbutton(opt, text="跳过片头淡入/片尾淡出", variable=self.var_trim).grid(
            row=2, column=0, columnspan=3, sticky="w", pady=(8, 0))
        ttk.Label(opt, text="循环交叉淡化", style="Hint.TLabel").grid(
            row=2, column=3, columnspan=2, sticky="e", pady=(8, 0))
        self.spin_fade = ttk.Spinbox(opt, from_=0, to=3, increment=0.1, width=6, font=self.font)
        self.spin_fade.set(0.4)
        self.spin_fade.grid(row=2, column=5, padx=(4, 2), pady=(8, 0))
        ttk.Label(opt, text="s", style="Hint.TLabel").grid(row=2, column=6, sticky="w", pady=(8, 0))

        # --- 操作 -----------------------------------------------------
        bar = ttk.Frame(root)
        bar.grid(row=4, column=0, columnspan=3, sticky="ew", pady=(2, 6))
        self.btn_run = ttk.Button(bar, text="开始转换", command=self.run, width=14)
        self.btn_run.pack(side="left")
        ttk.Button(bar, text="打开输出文件夹", command=self.open_folder,
                   width=16).pack(side="left", padx=8)
        self.status = ttk.Label(bar, text="", style="Hint.TLabel")
        self.status.pack(side="left", padx=8)

        # --- 日志 + 预览 ----------------------------------------------
        bottom = ttk.Frame(root)
        bottom.grid(row=5, column=0, columnspan=3, sticky="nsew")
        root.rowconfigure(5, weight=1)
        bottom.columnconfigure(0, weight=1)
        bottom.rowconfigure(0, weight=1)

        self.log = tk.Text(bottom, height=12, font=self.mono, wrap="none",
                           bg="#fbfbfb", relief="solid", bd=1)
        sb = ttk.Scrollbar(bottom, command=self.log.yview)
        self.log.configure(yscrollcommand=sb.set)
        self.log.grid(row=0, column=0, sticky="nsew")
        sb.grid(row=0, column=1, sticky="ns")

        if HAVE_PREVIEW:
            self.preview = tk.Label(bottom, bg="#eef2f6", width=34, height=14,
                                    relief="solid", bd=1, text="预览", compound="center")
            self.preview.grid(row=0, column=2, sticky="n", padx=(10, 0))

        self._sync()

    # -------------------------------------------------------------- 交互
    def _sync(self):
        m = self.mode.get()
        for w, on in ((self.spin_w, m == "width"), (self.spin_h, m == "height"),
                      (self.spin_mb, m == "size")):
            w.configure(state="normal" if on else "disabled")
        custom = self.bgmode.get() == "自定义"
        self.swatch.configure(state="normal" if custom else "disabled")

    def pick_src(self):
        p = filedialog.askopenfilename(
            title="选择 SVG", filetypes=[("SVG", "*.svg"), ("所有文件", "*.*")])
        if not p:
            return
        self.src.set(p)
        if not self.dst.get():
            self.dst.set(os.path.splitext(p)[0] + ".gif")

    def pick_dst(self):
        p = filedialog.asksaveasfilename(
            title="保存为", defaultextension=".gif",
            filetypes=[("GIF", "*.gif")], initialfile=os.path.basename(self.dst.get() or "out.gif"))
        if p:
            self.dst.set(p)

    def pick_color(self):
        _, hx = colorchooser.askcolor(self.bg_color, title="背景色")
        if hx:
            self.bg_color = hx
            self.swatch.configure(bg=hx)

    def open_folder(self):
        target = self.dst.get() or self.src.get()
        if not target:
            return
        folder = os.path.dirname(os.path.abspath(target))
        if os.path.isdir(folder):
            try:
                if sys.platform.startswith("win"):
                    os.startfile(folder)
                elif sys.platform == "darwin":
                    subprocess.Popen(["open", folder])
                else:
                    subprocess.Popen(["xdg-open", folder])
            except Exception:
                pass

    # -------------------------------------------------------------- 运行
    def run(self):
        if self.worker and self.worker.is_alive():
            return
        svg = self.src.get().strip()
        out = self.dst.get().strip()
        if not svg or not os.path.exists(svg):
            messagebox.showerror("找不到文件", "请选择一个存在的 SVG 文件。")
            return
        if not out:
            out = os.path.splitext(svg)[0] + ".gif"
            self.dst.set(out)
        if not out.lower().endswith(".gif"):
            out += ".gif"
            self.dst.set(out)

        mode = self.mode.get()
        kwargs = dict(fps=float(self.spin_fps.get()),
                      colours=int(self.spin_col.get()),
                      loop=int(self.spin_loop.get()),
                      verify=self.var_verify.get(),
                      trim=self.var_trim.get(),
                      crossfade=float(self.spin_fade.get()),
                      verbose=False)
        if mode == "width":
            kwargs["width"] = int(self.spin_w.get())
        elif mode == "height":
            kwargs["height"] = int(self.spin_h.get())
        else:
            kwargs["max_size"] = "%sMB" % self.spin_mb.get()

        bgm = self.bgmode.get()
        kwargs["bg"] = None if bgm == "透明" else (
            "#ffffff" if bgm == "白色" else self.bg_color)

        self.log.delete("1.0", "end")
        self.btn_run.configure(state="disabled")
        self.status.configure(text="转换中…")
        self.q.put(("log", "开始转换 …"))
        self.worker = threading.Thread(target=self._work, args=(svg, out, kwargs), daemon=True)
        self.worker.start()

    def _work(self, svg, out, kwargs):
        try:
            core.convert(svg, out, log=lambda s: self.q.put(("log", s)), **kwargs)
            res = getattr(core.convert, "result", {})
            self.q.put(("done", res))
        except Exception as exc:
            self.q.put(("log", traceback.format_exc()))
            self.q.put(("error", str(exc)))

    # -------------------------------------------------------------- 消息泵
    def _drain(self):
        try:
            while True:
                kind, payload = self.q.get_nowait()
                if kind == "log":
                    self.log.insert("end", payload + "\n")
                    self.log.see("end")
                elif kind == "done":
                    self._finish(payload)
                elif kind == "error":
                    self._fail(payload)
        except queue.Empty:
            pass
        self.after(120, self._drain)

    def _finish(self, res):
        self.btn_run.configure(state="normal")
        limit = res.get("limit")
        size = res.get("bytes", 0)
        ok = (limit is None) or (size <= limit)
        text = "%dx%d · %d 色 · %d 帧 · %.2f s · %.2f MB" % (
            res.get("width", 0), res.get("height", 0), res.get("colours", 0),
            res.get("frames", 0), res.get("duration", 0), size / 1048576.0)
        self.status.configure(text=("✔ " if ok else "✘ ") + text)
        self.q.put(("log", ""))
        self.log.insert("end", "完成: " + text + "\n")
        if limit:
            self.log.insert("end", "上限 %s，%s\n" % (core.human(limit),
                                                     "已满足" if ok else "仍超标"))
        self._show_preview(res.get("path"))

    def _fail(self, msg):
        self.btn_run.configure(state="normal")
        self.status.configure(text="✘ 转换失败")
        messagebox.showerror("转换失败", msg)

    def _show_preview(self, path):
        if not HAVE_PREVIEW or not path or not os.path.exists(path):
            return
        try:
            im = Image.open(path)
            im.seek(0)
            im = im.convert("RGBA")
            w, h = im.size
            scale = min(260.0 / w, 150.0 / h, 1.0)
            im = im.resize((max(1, int(w * scale)), max(1, int(h * scale))), Image.LANCZOS)
            bg = Image.new("RGB", im.size, (255, 255, 255))
            bg = Image.alpha_composite(bg.convert("RGBA"), im).convert("RGB")
            self.preview_img = ImageTk.PhotoImage(bg)
            self.preview.configure(image=self.preview_img, text="", width=bg.width, height=bg.height)
        except Exception:
            pass


def main():
    app = App()
    if len(sys.argv) > 1 and os.path.exists(sys.argv[1]):
        app.src.set(sys.argv[1])
        app.dst.set(os.path.splitext(sys.argv[1])[0] + ".gif")
    app.mainloop()


if __name__ == "__main__":
    main()

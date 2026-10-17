# -*- coding: utf-8 -*-
"""
界面逻辑测试（无需显示器）
==========================

在没有交互桌面的环境里，图形界面无法“看见”，但 Tk 依然可以正常初始化。
本脚本因此直接在无头模式下驱动真实的界面对象：

  1. 现场生成 3 种格式的测试音频；
  2. 创建真实的 AudioToMp3App 窗口对象；
  3. 通过控件把文件“加进列表”，设置输出目录，然后点击“转换为 MP3”
     （实际调用 start_convert，走真实的“后台线程 + 队列 + 主线程刷新”流程）；
  4. 手动泵送 Tk 事件循环，直到转换结束；
  5. 校验进度条、状态文字、日志、输出文件与 zip。

对话框会被替换成空实现，避免在自动化环境中阻塞。

用法：python tools/gui_test.py
"""

import os
import shutil
import sys
import time
import tkinter as tk

import numpy as np

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

try:
    sys.stdout.reconfigure(errors="replace")
except Exception:
    pass

import codecpod  # noqa: E402
import main as app  # noqa: E402

WORK_DIR = os.path.join(ROOT, "build", "_gui")
SAMPLE_DIR = os.path.join(WORK_DIR, "samples")
OUT_DIR = os.path.join(WORK_DIR, "out")

SPECS = [
    ("界面测试1.wav", lambda: codecpod.Wav()),
    ("界面测试2.flac", lambda: codecpod.Flac()),
    ("界面测试3.ogg", lambda: codecpod.Vorbis()),
]


def generate_samples():
    if os.path.isdir(SAMPLE_DIR):
        shutil.rmtree(SAMPLE_DIR, ignore_errors=True)
    os.makedirs(SAMPLE_DIR, exist_ok=True)
    paths = []
    for idx, (name, factory) in enumerate(SPECS):
        sr = 44100
        t = np.linspace(0, 1.0, sr, endpoint=False)
        left = (0.3 * np.sin(2 * np.pi * (300 + idx * 70) * t)).astype(np.float32)
        right = (0.25 * np.sin(2 * np.pi * (450 + idx * 70) * t)).astype(np.float32)
        path = os.path.join(SAMPLE_DIR, name)
        codecpod.save(path, np.stack([left, right]), sr, codec=factory())
        paths.append(path)
    return paths


def main():
    print("=" * 64)
    print("音频转 MP3 便携工具 —— 界面逻辑测试（无头模式）")
    print("=" * 64)

    paths = generate_samples()
    print(f"\n[1/4] 已生成 {len(paths)} 个测试音频：")
    for p in paths:
        print(f"    {os.path.basename(p)}  ({os.path.getsize(p)} 字节)")

    # 对话框在自动化环境中会阻塞，这里全部替换为空实现
    dialogs = []
    app.messagebox.showinfo = lambda *a, **k: dialogs.append(("info", a))
    app.messagebox.showwarning = lambda *a, **k: dialogs.append(("warning", a))
    app.messagebox.showerror = lambda *a, **k: dialogs.append(("error", a))
    app.messagebox.askyesno = lambda *a, **k: False

    print("\n[2/4] 创建窗口并注入文件 ...")
    root = tk.Tk()
    ui = app.AudioToMp3App(root)

    for p in paths:
        ui.files.append(p)
        ui._insert_row(p)
    ui.count_var.set(f"共 {len(ui.files)} 个文件")

    if os.path.isdir(OUT_DIR):
        shutil.rmtree(OUT_DIR, ignore_errors=True)
    os.makedirs(OUT_DIR, exist_ok=True)
    ui.outdir_var.set(OUT_DIR)
    ui.bitrate_var.set("192")
    ui.zip_var.set(True)

    rows = ui.tree.get_children()
    print(f"    列表行数={len(rows)}  计数标签='{ui.count_var.get()}'")
    if len(rows) != len(paths):
        print("[FAIL] 文件列表与预期不符")
        return 1

    print("\n[3/4] 触发转换并泵送事件循环 ...")
    ui.start_convert()
    if not ui.busy:
        print("[FAIL] start_convert 之后 busy 仍为 False")
        return 1

    deadline = time.time() + 180
    while ui.busy and time.time() < deadline:
        root.update()          # 让主线程处理队列事件，模拟真实的界面刷新
        time.sleep(0.05)

    if ui.busy:
        print("[FAIL] 转换超时未结束")
        return 1
    print(f"    busy=False  状态='{ui.status_var.get()}'")
    print(f"    进度条 value={ui.progress['value']}/{ui.progress['maximum']}")

    log_text = ui.log_text.get("1.0", "end").strip()
    print("    --- 界面日志 ---")
    for line in log_text.splitlines():
        print("      " + line)

    # 关闭窗口，避免残留
    root.destroy()

    print("\n[4/4] 校验结果 ...")
    problems = []
    mp3s = [os.path.join(OUT_DIR, n) for n in os.listdir(OUT_DIR) if n.lower().endswith(".mp3")]
    zips = [os.path.join(OUT_DIR, n) for n in os.listdir(OUT_DIR) if n.lower().endswith(".zip")]

    print(f"    生成 mp3：{len(mp3s)} 个；生成 zip：{len(zips)} 个")
    if len(mp3s) != len(paths):
        problems.append(f"mp3 数量 {len(mp3s)} != {len(paths)}")
    if not zips:
        problems.append("没有生成 zip 压缩包")
    if not dialogs:
        problems.append("结束时没有弹出完成提示")
    else:
        print(f"    完成提示对话框：{dialogs[-1][0]}")
    if any(kind == "error" for kind, _ in dialogs):
        problems.append("出现了错误对话框")
    if "[失败]" in log_text:
        problems.append("日志中出现失败记录")
    if "进度" not in ui.status_var.get() and "完成" not in ui.status_var.get():
        problems.append(f"状态文字异常：{ui.status_var.get()}")

    print("\n" + "=" * 64)
    if problems:
        print("界面逻辑测试未通过：")
        for item in problems:
            print("  - " + item)
        return 1
    print("界面逻辑测试通过：后台线程 + 队列刷新 + 输出 + zip 全部正常。")
    return 0


if __name__ == "__main__":
    sys.exit(main())

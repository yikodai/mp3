# -*- coding: utf-8 -*-
"""
离线自检脚本
============

不依赖图形界面，直接验证“解码 -> 编码 MP3 -> 打包 zip”整条链路：

  1. 用 numpy + codecpod 现场生成多种格式的测试音频（wav/flac/ogg/aiff/aac），
     并额外生成一个中文文件名的文件，用于验证非 ASCII 路径；
  2. 故意放一个损坏文件，验证程序能否“优雅跳过”而不是崩溃；
  3. 调用 main.run_conversion 批量转换；
  4. 校验每个 mp3 的文件头与可解码性，校验 zip 内容。

用法：python tools/smoke_test.py
退出码 0 表示全部通过。
"""

import os
import shutil
import sys
import zipfile

import numpy as np

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

try:
    sys.stdout.reconfigure(errors="replace")
except Exception:
    pass

import codecpod  # noqa: E402
import main as app  # noqa: E402  复用 run_conversion

WORK_DIR = os.path.join(ROOT, "build", "_smoke")
SAMPLE_DIR = os.path.join(WORK_DIR, "samples")
OUT_DIR = os.path.join(WORK_DIR, "out")

# (文件名, 编解码器工厂) —— 覆盖多种常见输入格式
SAMPLE_SPECS = [
    ("测试样本1.wav", lambda: codecpod.Wav()),
    ("测试样本2.flac", lambda: codecpod.Flac()),
    ("测试样本3.ogg", lambda: codecpod.Vorbis()),
    ("测试样本4.aiff", lambda: codecpod.Aiff()),
    ("测试样本5.aac", lambda: codecpod.Aac()),
]


def make_tone(freq, seconds=1.5, sample_rate=44100):
    """生成一段立体声正弦波（channels_first，float32）。"""
    t = np.linspace(0, seconds, int(sample_rate * seconds), endpoint=False)
    left = (0.35 * np.sin(2 * np.pi * freq * t)).astype(np.float32)
    right = (0.30 * np.sin(2 * np.pi * freq * 1.5 * t)).astype(np.float32)
    return np.stack([left, right]), sample_rate


def looks_like_mp3(path):
    """粗略校验 mp3 文件头：ID3 标签 或 MPEG 帧同步字。"""
    try:
        with open(path, "rb") as fh:
            head = fh.read(4)
    except OSError:
        return False
    if head[:3] == b"ID3":
        return True
    return len(head) >= 2 and head[0] == 0xFF and (head[1] & 0xE0) == 0xE0


def main():
    print("=" * 64)
    print("音频转 MP3 便携工具 —— 离线链路自检")
    print("=" * 64)

    if os.path.isdir(WORK_DIR):
        shutil.rmtree(WORK_DIR, ignore_errors=True)
    os.makedirs(SAMPLE_DIR, exist_ok=True)
    os.makedirs(OUT_DIR, exist_ok=True)

    # ---- 1. 生成测试样本 ----
    print("\n[1/5] 生成测试音频 ...")
    inputs = []
    for idx, (name, factory) in enumerate(SAMPLE_SPECS):
        waveform, sr = make_tone(330.0 + idx * 55.0)
        path = os.path.join(SAMPLE_DIR, name)
        try:
            codecpod.save(path, waveform, sr, codec=factory())
            size = os.path.getsize(path)
            print(f"    [OK]   {name}  ({size} 字节)")
            inputs.append(path)
        except Exception as exc:
            print(f"    [跳过] {name}：生成失败 {exc}")

    # ---- 2. 损坏文件（验证优雅失败） ----
    print("\n[2/5] 生成一个损坏文件用于验证容错 ...")
    broken = os.path.join(SAMPLE_DIR, "损坏文件.wav")
    with open(broken, "wb") as fh:
        fh.write(b"this is not a real audio file" * 50)
    inputs.append(broken)
    print(f"    [OK]   {os.path.basename(broken)}（预期会转换失败）")

    if len(inputs) < 4:
        print("\n[FAIL] 生成的测试样本太少，无法继续。")
        return 1

    # ---- 3. 批量转换 ----
    print(f"\n[3/5] 批量转换为 MP3（共 {len(inputs)} 个输入）...")
    logs = []
    succeeded, failed, zip_path = app.run_conversion(
        inputs, OUT_DIR, 192000, True,
        on_event=lambda kind, **data: logs.append(f"{kind}: {data.get('text', '')}"),
    )
    for line in logs:
        print("    " + line)

    # ---- 4. 校验输出 ----
    print("\n[4/5] 校验输出 ...")
    problems = []
    for src, dst in succeeded:
        size = os.path.getsize(dst)
        ok_header = looks_like_mp3(dst)
        try:
            info = codecpod.info(dst)
            ok_decode = info.codec == "mp3" or "mp3" in str(info.codec).lower()
        except Exception as exc:
            ok_decode = False
            problems.append(f"{os.path.basename(dst)} 无法被解码：{exc}")
        if not ok_header:
            problems.append(f"{os.path.basename(dst)} 文件头不像 MP3")
        if size < 1024:
            problems.append(f"{os.path.basename(dst)} 体积异常（{size} 字节）")
        print(f"    [OK]   {os.path.basename(src)} -> {os.path.basename(dst)}"
              f"  头={ok_header} 可解码={ok_decode} {size} 字节")

    # 损坏文件应当出现在失败列表里
    broken_handled = any(os.path.basename(s) == os.path.basename(broken) for s, _ in failed)
    if not broken_handled:
        problems.append("损坏文件没有被正确识别为失败")
    else:
        print("    [OK]   损坏文件已被优雅跳过（未崩溃）")

    if len(succeeded) < 3:
        problems.append(f"成功转换的文件不足 3 个（实际 {len(succeeded)}）")

    # ---- 5. 校验 zip ----
    print("\n[5/5] 校验 ZIP 压缩包 ...")
    if not zip_path or not os.path.isfile(zip_path):
        problems.append("ZIP 没有生成")
    else:
        with zipfile.ZipFile(zip_path) as zf:
            names = zf.namelist()
            bad = zf.testzip()
        mp3_names = [n for n in names if n.lower().endswith(".mp3")]
        print(f"    ZIP：{os.path.basename(zip_path)}  条目数={len(names)}  mp3={len(mp3_names)}")
        if bad:
            problems.append(f"ZIP 内容损坏：{bad}")
        if len(mp3_names) != len(succeeded):
            problems.append(f"ZIP 中的 mp3 数量({len(mp3_names)})与成功数({len(succeeded)})不一致")

    print("\n" + "=" * 64)
    if problems:
        print("自检未通过，问题如下：")
        for item in problems:
            print("  - " + item)
        return 1

    print(f"自检通过：成功 {len(succeeded)} 个，失败 {len(failed)} 个（含预期的损坏文件）。")
    return 0


if __name__ == "__main__":
    sys.exit(main())

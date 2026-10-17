# -*- mode: python ; coding: utf-8 -*-
"""
PyInstaller 打包配置 —— 音频转 MP3 便携工具（--onefile 单文件 exe）
=================================================================

打包关键点（决定了 exe 能否在“干净的、没有 Python / FFmpeg / VC++ 运行库”的
Windows 11 上直接双击运行）：

1. codecpod：静态链接了 FFmpeg 的扩展模块，PyInstaller 没有对应的官方 hook，
   因此用 collect_all 把包本体和 codecpod.pyd 一并收集。

2. codecpod.libs：这个 wheel 由 delvewheel 重定位过，它把 codecpod.pyd 依赖的
   MinGW 运行库（libunwind / libwinpthread）放在 site-packages/codecpod.libs 下，
   并在 codecpod/__init__.py 里插入了一段补丁：
       os.add_dll_directory(<...>/codecpod.libs)
   所以必须把这一目录原样打进 exe（目标路径仍为 codecpod.libs），
   否则导入 codecpod 时会因为找不到这两个 DLL 而失败。

3. python3.dll：codecpod.pyd 是 abi3（稳定 ABI）扩展，导入表中引用的是
   python3.dll 转发 DLL，需要一并打包。

4. numpy：其官方 hook 会自动收集 numpy.libs（含 OpenBLAS 与重定位后的
   msvcp140）；VCRUNTIME140 / VCRUNTIME140_1 由 PyInstaller 从 Python 安装目录
   自动打包，所以目标机器不需要安装 VC++ Redistributable。

5. 所有路径都基于 SPECPATH 计算，保证从任意工作目录调用都能正确打包。
"""

import glob
import os
import sys

from PyInstaller.utils.hooks import collect_all, get_package_paths

# 本 spec 所在目录（PyInstaller 会注入 SPECPATH）
_here = os.path.abspath(globals().get("SPECPATH", os.getcwd()))
_entry = os.path.join(_here, "main.py")

datas = []
binaries = []
hiddenimports = []

# ---------------------------------------------------------------------------
# 1) codecpod 包本体 + codecpod.pyd
# ---------------------------------------------------------------------------
_d, _b, _h = collect_all("codecpod")
datas += _d
binaries += _b
hiddenimports += _h

# codecpod 是由 Rust 写的原生扩展，它在运行时通过 Python C-API 动态 import numpy
# 来访问 NumPy array API capsule —— 这个 import 发生在原生代码里，PyInstaller
# 的静态分析看不见。如果不显式声明，打包后会报
# “Failed to access NumPy array API capsule: No module named 'numpy'”，
# 并且 numpy 的 hook 也不会执行（numpy.libs / VC++ 运行库就不会被收集）。
hiddenimports += ["numpy"]

# ---------------------------------------------------------------------------
# 2) codecpod.libs（delvewheel 重定位的 MinGW 运行库），保持目录结构
# ---------------------------------------------------------------------------
_pkg_base, _pkg_dir = get_package_paths("codecpod")
_libs_dir = None
for _candidate in (
    os.path.join(_pkg_dir or "", "codecpod.libs"),
    os.path.join(_pkg_base or "", "codecpod.libs"),
    os.path.join(_pkg_dir or "", os.pardir, "codecpod.libs"),
    os.path.join(_pkg_base or "", os.pardir, "codecpod.libs"),
):
    if _candidate and os.path.isdir(_candidate):
        _libs_dir = os.path.abspath(_candidate)
        break

if _libs_dir:
    for _dll in glob.glob(os.path.join(_libs_dir, "*.dll")):
        binaries.append((_dll, "codecpod.libs"))
    print(f"[spec] 已收集 codecpod.libs：{_libs_dir}")
    for _name in sorted(os.listdir(_libs_dir)):
        print(f"[spec]     - {_name}")
else:
    print("[spec] 警告：未找到 codecpod.libs 目录！exe 可能无法加载 codecpod。")

# ---------------------------------------------------------------------------
# 3) python3.dll（abi3 扩展依赖的转发 DLL）
# ---------------------------------------------------------------------------
for _root in {getattr(sys, "base_prefix", ""), getattr(sys, "prefix", "")}:
    if not _root:
        continue
    _python3_dll = os.path.join(_root, "python3.dll")
    if os.path.isfile(_python3_dll):
        binaries.append((_python3_dll, "."))
        print(f"[spec] 已收集 python3.dll：{_python3_dll}")
        break

# ---------------------------------------------------------------------------
# 4) 图标（随包分发，运行时由 main.py 通过 sys._MEIPASS 解析）
# ---------------------------------------------------------------------------
_icon = os.path.join(_here, "assets", "app.ico")
if not os.path.isfile(_icon):
    print("[spec] 提示：未找到 assets/app.ico，将使用默认图标。")
    _icon = None

# ---------------------------------------------------------------------------
# PyInstaller 构建描述
# ---------------------------------------------------------------------------
a = Analysis(
    [_entry],
    pathex=[_here],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=["pytest", "pip", "wheel", "setuptools"],
    noarchive=False,
    optimize=0,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name="AudioToMP3",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,                     # 不使用 UPX：避免杀毒软件误报，也避免额外依赖
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,                 # GUI 程序，不弹控制台窗口
    disable_windowed_traceback=True,   # 关闭 PyInstaller 的模态报错框（会阻塞自动化）；
                                       # 异常由 main.py 自行记录到 AudioToMP3_error.log
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=_icon,
)

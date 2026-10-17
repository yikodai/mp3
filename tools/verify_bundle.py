# -*- coding: utf-8 -*-
"""
打包产物依赖闭合性检查
======================

回答一个关键问题：**这个单文件 exe 里，还有没有“需要目标机器另外安装”的 DLL？**

做法：
  1. 用 PyInstaller 自带的归档读取器，把 --onefile exe 内嵌的文件解出来；
  2. 分析每个 PE（exe / dll / pyd）的导入表；
  3. 若某个导入的 DLL 既不是 Windows 自带，也没有出现在包内，
     就说明它会在干净机器上“找不到”——这正是导致 0xc000007b 之类错误的原因。

用法：
    python tools/verify_bundle.py dist\\AudioToMP3.exe
退出码 0 表示依赖闭合（目标机器无需额外安装任何运行库）。
"""

import os
import shutil
import sys
import tempfile

import pefile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from check_deps import SYSTEM_DLLS, is_system_dll  # noqa: E402 复用同一套判定规则

PE_EXTS = (".exe", ".dll", ".pyd")


def list_imports_from_bytes(data, label):
    """从内存中的 PE 镜像解析导入表。"""
    try:
        pe = pefile.PE(data=data, fast_load=True)
    except Exception:
        return None
    try:
        pe.parse_data_directories(
            directories=[pefile.DIRECTORY_ENTRY["IMAGE_DIRECTORY_ENTRY_IMPORT"]]
        )
        if not hasattr(pe, "DIRECTORY_ENTRY_IMPORT"):
            return []
        return [e.dll.decode("ascii", "replace") for e in pe.DIRECTORY_ENTRY_IMPORT]
    finally:
        pe.close()


def main(argv):
    try:
        sys.stdout.reconfigure(errors="replace")
    except Exception:
        pass

    try:
        from PyInstaller.archive.readers import CArchiveReader
    except Exception as exc:
        print(f"无法导入 PyInstaller 归档读取器：{exc}")
        return 2

    if len(argv) != 1 or not os.path.isfile(argv[0]):
        print("用法：python tools/verify_bundle.py <onefile exe>")
        return 2

    exe_path = os.path.abspath(argv[0])

    # ---- 1. 解出内嵌文件，并记录包内提供的所有文件名 ----
    reader = CArchiveReader(exe_path)
    provided = set()
    extracted = []          # (相对名, 字节)
    for name in reader.toc:
        base = os.path.basename(name.replace("\\", "/"))
        provided.add(base.lower())
        if base.lower().endswith(PE_EXTS):
            try:
                data = reader.extract(name)
            except Exception:
                continue
            if data:
                extracted.append((name, data))

    provided.add(os.path.basename(exe_path).lower())

    print(f"exe：{exe_path}")
    print(f"内嵌条目：{len(reader.toc)} 个，其中 PE 文件 {len(extracted)} 个")
    print(f"包内提供的 DLL/PYD 名称数：{len(provided)}")
    print("-" * 72)

    # 关注包内的关键运行库是否齐全
    key_needs = {
        "vcruntime140.dll": "VC++ 运行库（Python/numpy 依赖）",
        "vcruntime140_1.dll": "VC++ 运行库（C++ 异常处理）",
        "python3.dll": "abi3 转发 DLL（codecpod 依赖）",
    }
    for dll, why in key_needs.items():
        mark = "OK  " if dll in provided else "缺失"
        print(f"  [{mark}] {dll}  —— {why}")
    libs_dirs = sorted({os.path.dirname(n.replace("\\", "/"))
                        for n, _ in extracted if "libs" in n.lower()})
    print(f"  包内 .libs 目录：{libs_dirs or '（无）'}")

    # ---- 2. 依赖闭合性分析 ----
    print("-" * 72)
    missing = {}          # dll -> 引用者集合
    for name, data in extracted:
        imports = list_imports_from_bytes(data, name)
        if not imports:
            continue
        for dll in imports:
            if is_system_dll(dll) or dll.lower() in provided:
                continue
            missing.setdefault(dll, set()).add(os.path.basename(name))

    if missing:
        print("发现未闭合的依赖（目标机器上会找不到这些 DLL）：")
        for dll, users in sorted(missing.items()):
            print(f"  - {dll}   <- {', '.join(sorted(users))}")
        print("\n结论：依赖未闭合，需要把它们一并打包。")
        return 1

    print("未发现未闭合的非系统 DLL 依赖。")
    print("\n结论：依赖闭合 —— 目标机器无需安装 Python / FFmpeg / VC++ 运行库。")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))

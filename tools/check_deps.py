# -*- coding: utf-8 -*-
"""
依赖检查工具（交付验证用）
==========================

扫描给定的 exe / dll / pyd 文件（或目录），列出它们导入的 DLL，并标出
“非系统 DLL”——也就是必须随程序一起分发、否则会在干净机器上启动失败的依赖。

用法：
    python tools/check_deps.py dist\\AudioToMP3.exe
    python tools/check_deps.py .venv\\Lib\\site-packages\\codecpod

判定规则：
    - kernel32/user32/ucrtbase/ntdll 等 Windows 自带的 DLL 视为“系统 DLL”；
    - api-ms-win-* / ext-ms-win-* 属于 Windows API Set，也视为系统 DLL；
    - 其余（如 VCRUNTIME140.dll、libgcc_s_seh-1.dll）一律视为需要打包的外部依赖。
"""

import os
import sys

import pefile

# Windows 自带、无需随程序分发的 DLL（小写）
SYSTEM_DLLS = {
    "kernel32.dll", "kernelbase.dll", "user32.dll", "gdi32.dll", "gdi32full.dll",
    "advapi32.dll", "ole32.dll", "oleaut32.dll", "shell32.dll", "shlwapi.dll",
    "ws2_32.dll", "rpcrt4.dll", "comdlg32.dll", "comctl32.dll", "uxtheme.dll",
    "dwmapi.dll", "mpr.dll", "winmm.dll", "version.dll", "imm32.dll",
    "bcrypt.dll", "crypt32.dll", "secur32.dll", "ntdll.dll", "ucrtbase.dll",
    "msvcrt.dll", "psapi.dll", "dbghelp.dll", "userenv.dll", "netapi32.dll",
    "iphlpapi.dll", "dnsapi.dll", "wintrust.dll", "powrprof.dll", "setupapi.dll",
    "cfgmgr32.dll", "mswsock.dll", "winhttp.dll", "urlmon.dll", "wininet.dll",
    "oleacc.dll", "propsys.dll", "msimg32.dll", "gdiplus.dll", "hid.dll",
    "d3d11.dll", "dxgi.dll", "dxva2.dll", "wtsapi32.dll", "authz.dll",
    "imagehlp.dll", "normaliz.dll", "sechost.dll", "kernel.appcore.dll",
    "bcryptprimitives.dll", "windows.storage.dll", "combase.dll", "shcore.dll",
    "nvapi64.dll", "opengl32.dll", "glu32.dll", "avrt.dll", "mfplat.dll",
}

# 明确属于 VC++ 运行库的 DLL（出现即说明目标机器可能需要 VC++ Redistributable）
VCRUNTIME_HINTS = ("vcruntime", "msvcp", "msvcr", "concrt", "vccorlib")

PE_EXTS = (".exe", ".dll", ".pyd", ".so")


def is_system_dll(name):
    low = name.lower()
    if low.startswith("api-ms-win-") or low.startswith("ext-ms-win-"):
        return True
    return low in SYSTEM_DLLS


def list_imports(path):
    """返回该 PE 文件导入的 DLL 名称列表（失败时返回 None）。"""
    try:
        pe = pefile.PE(path, fast_load=True)
    except Exception:
        return None
    try:
        pe.parse_data_directories(
            directories=[pefile.DIRECTORY_ENTRY["IMAGE_DIRECTORY_ENTRY_IMPORT"]]
        )
        result = []
        if hasattr(pe, "DIRECTORY_ENTRY_IMPORT"):
            for entry in pe.DIRECTORY_ENTRY_IMPORT:
                result.append(entry.dll.decode("ascii", "replace"))
        return result
    finally:
        pe.close()


def collect_files(paths):
    found = []
    for p in paths:
        if os.path.isdir(p):
            for root, _dirs, files in os.walk(p):
                for name in files:
                    if name.lower().endswith(PE_EXTS):
                        found.append(os.path.join(root, name))
        elif os.path.isfile(p):
            found.append(p)
    return sorted(found)


def main(argv):
    # 避免在 GBK 控制台下因无法编码某些字符而崩溃
    try:
        sys.stdout.reconfigure(errors="replace")
    except Exception:
        pass

    if not argv:
        print("用法：python tools/check_deps.py <文件或目录> [更多…]")
        return 2

    files = collect_files(argv)
    if not files:
        print("没有找到任何 exe/dll/pyd 文件。")
        return 1

    external = {}     # dll -> 引用它的文件集合
    vcruntime = {}    # vcruntime 类 dll -> 引用它的文件集合

    for path in files:
        imports = list_imports(path)
        rel = os.path.relpath(path)
        if imports is None:
            print(f"[跳过] 不是有效的 PE 文件：{rel}")
            continue
        print(f"\n{rel}")
        if not imports:
            print("    （无导入表）")
        for dll in imports:
            mark = "" if is_system_dll(dll) else "   <== 非系统 DLL"
            print(f"    导入 {dll}{mark}")
            if not is_system_dll(dll):
                external.setdefault(dll, set()).add(rel)
            if any(h in dll.lower() for h in VCRUNTIME_HINTS):
                vcruntime.setdefault(dll, set()).add(rel)

    print("\n" + "=" * 72)
    print(f"扫描了 {len(files)} 个 PE 文件")
    if external:
        print("\n以下非系统 DLL 必须随程序一起分发（否则目标机器可能启动失败）：")
        for dll, users in sorted(external.items()):
            print(f"  - {dll}   <- {', '.join(sorted(users))}")
    else:
        print("\n未发现非系统 DLL 依赖。")

    if vcruntime:
        print("\n[!] 检测到 VC++ 运行库依赖（目标机器需要相应运行库）：")
        for dll, users in sorted(vcruntime.items()):
            print(f"  - {dll}   <- {', '.join(sorted(users))}")
    else:
        print("\n未检测到 VCRUNTIME/MSVCP 等 VC++ 运行库依赖。")

    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))

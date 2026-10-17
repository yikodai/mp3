# -*- coding: utf-8 -*-
"""
一键构建脚本 —— 音频转 MP3 便携工具
====================================

按顺序完成：

  1. 创建 / 复用虚拟环境 .venv
  2. 安装依赖（先默认源，失败后自动切换到国内镜像）
  3. 生成应用图标 assets/app.ico（若不存在）
  4. 运行离线自检 tools/smoke_test.py（验证解码+编码+打包链路）
  5. 用 PyInstaller + AudioToMP3.spec 打出单文件 dist/AudioToMP3.exe
  6. 把 exe 与 使用说明.txt 打包成 dist/音频转MP3_便携版.zip

用法：
    python build.py            # 也可以直接双击 build.bat

说明：
    - 构建机需要 Python 3.10–3.13（64 位）。脚本会自动用 .venv 隔离依赖。
    - 产物不依赖目标机器的 Python / FFmpeg / VC++ 运行库。
"""

import os
import subprocess
import sys
import zipfile

ROOT = os.path.dirname(os.path.abspath(__file__))
VENV_DIR = os.path.join(ROOT, ".venv")
VENV_PY = os.path.join(VENV_DIR, "Scripts", "python.exe")
SPEC_FILE = os.path.join(ROOT, "AudioToMP3.spec")
REQUIREMENTS = os.path.join(ROOT, "requirements.txt")
ICON_FILE = os.path.join(ROOT, "assets", "app.ico")
SMOKE_TEST = os.path.join(ROOT, "tools", "smoke_test.py")
GUI_TEST = os.path.join(ROOT, "tools", "gui_test.py")
ICON_MAKER = os.path.join(ROOT, "tools", "make_icon.py")
DIST_DIR = os.path.join(ROOT, "dist")
TMP_DIR = os.path.join(ROOT, ".tmp")
PIP_CACHE_DIR = os.path.join(ROOT, ".cache", "pip")

EXE_NAME = "AudioToMP3.exe"
README_NAME = "使用说明.txt"
ZIP_NAME = "音频转MP3_便携版.zip"

# 依赖源：优先国内镜像，最后回退默认源。
# codecpod 较新，部分镜像（如清华 TUNA）可能尚未同步，故此处的镜像均已确认可用。
MIRRORS = [
    "https://mirrors.aliyun.com/pypi/simple/",
    "https://mirrors.cloud.tencent.com/pypi/simple/",
]


def redirect_temp():
    """把临时目录与 pip 缓存重定向到项目目录内，避免污染系统目录。"""
    os.makedirs(TMP_DIR, exist_ok=True)
    os.makedirs(PIP_CACHE_DIR, exist_ok=True)
    for key in ("TMP", "TEMP", "TMPDIR"):
        os.environ[key] = TMP_DIR
    os.environ["PIP_CACHE_DIR"] = PIP_CACHE_DIR


def log(message=""):
    print(message, flush=True)


def run(cmd, cwd=ROOT):
    log("  $ " + " ".join(cmd))
    return subprocess.call(cmd, cwd=cwd)


def step(index, total, title):
    log()
    log(f"[{index}/{total}] {title}")


def ensure_venv():
    if os.path.isfile(VENV_PY):
        log(f"复用虚拟环境：{VENV_DIR}")
        return
    log(f"创建虚拟环境：{VENV_DIR}")
    if run([sys.executable, "-m", "venv", VENV_DIR]) != 0:
        raise SystemExit("创建虚拟环境失败，请确认已安装 Python 3.10–3.13。")


def pip_install(extra_args):
    """优先走国内镜像，全部失败后再回退默认源。"""
    for mirror in MIRRORS:
        log(f"尝试镜像源：{mirror}")
        if run([VENV_PY, "-m", "pip", "install", "-i", mirror,
                "--cache-dir", PIP_CACHE_DIR, *extra_args]) == 0:
            return True
    log("镜像源均失败，回退默认源。")
    return run([VENV_PY, "-m", "pip", "install",
                "--cache-dir", PIP_CACHE_DIR, *extra_args]) == 0


def install_deps():
    run([VENV_PY, "-m", "pip", "install", "--upgrade", "pip", "--quiet"])
    if not pip_install(["--only-binary=:all:", "-r", REQUIREMENTS]):
        raise SystemExit("依赖安装失败，请检查网络或手动执行 pip install -r requirements.txt。")


def ensure_icon():
    if os.path.isfile(ICON_FILE):
        log("图标已存在，跳过生成。")
        return
    if run([VENV_PY, ICON_MAKER]) != 0:
        log("提示：图标生成失败，将使用 PyInstaller 默认图标。")


def run_smoke():
    if run([VENV_PY, SMOKE_TEST]) != 0:
        log("提示：离线自检未通过，请查看上面的输出（构建仍会继续）。")


def run_gui_test():
    if run([VENV_PY, GUI_TEST]) != 0:
        log("提示：界面逻辑测试未通过，请查看上面的输出（构建仍会继续）。")


def build_exe():
    if run([VENV_PY, "-m", "PyInstaller", "--noconfirm", "--clean", SPEC_FILE]) != 0:
        raise SystemExit("PyInstaller 打包失败。")


def make_zip():
    exe_path = os.path.join(DIST_DIR, EXE_NAME)
    if not os.path.isfile(exe_path):
        raise SystemExit(f"未找到 {exe_path}，打包可能失败。")

    readme_path = os.path.join(ROOT, README_NAME)
    zip_path = os.path.join(DIST_DIR, ZIP_NAME)

    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
        # 压缩包根目录下只放 exe（可选附带 使用说明.txt）
        zf.write(exe_path, arcname=EXE_NAME)
        if os.path.isfile(readme_path):
            zf.write(readme_path, arcname=README_NAME)

    size_mb = os.path.getsize(exe_path) / 1024 / 1024
    log(f"已生成交付压缩包：{zip_path}")
    log(f"单文件 exe 大小：{size_mb:.1f} MB")


def main():
    redirect_temp()

    # 只准备环境（建 venv + 装依赖），不打包。启动脚本首次运行时会用到。
    env_only = "--env-only" in sys.argv[1:]

    log("=" * 64)
    log("  音频转 MP3 便携工具 —— 构建")
    log("=" * 64)
    log(f"构建机 Python：{sys.version.splitlines()[0]}")
    log(f"项目目录：{ROOT}")

    total = 2 if env_only else 7
    step(1, total, "准备虚拟环境")
    ensure_venv()

    step(2, total, "安装依赖（codecpod / numpy / pyinstaller）")
    install_deps()

    if env_only:
        log()
        log("=" * 64)
        log("  环境准备完成。")
        log("  现在可以启动 Web 版服务，例如：")
        log("      启动Web版.bat")
        log("      .venv\\Scripts\\python.exe web_server.py --no-browser")
        log("=" * 64)
        return 0

    # 完整打包流程只对桌面版有效；Web 版工作区缺少桌面版打包配置时给出明确提示。
    if not os.path.isfile(SPEC_FILE):
        log()
        log("=" * 64)
        log("  [提示] 未找到桌面版打包配置 AudioToMP3.spec，当前工作区是 Web 版。")
        log("         环境已就绪，请直接用 启动Web版.bat 启动 Web 服务。")
        log("=" * 64)
        return 0

    step(3, total, "准备图标")
    ensure_icon()

    step(4, total, "运行离线自检（解码 + 编码 + 打包）")
    run_smoke()

    step(5, total, "运行界面逻辑测试（无头模式）")
    run_gui_test()

    step(6, total, "PyInstaller 单文件打包")
    build_exe()

    step(7, total, "生成交付压缩包")
    make_zip()

    log()
    log("=" * 64)
    log("  构建完成")
    log(f"  单文件程序：dist\\{EXE_NAME}")
    log(f"  交付压缩包：dist\\{ZIP_NAME}")
    log("=" * 64)
    return 0


if __name__ == "__main__":
    sys.exit(main())

# -*- coding: utf-8 -*-
"""
音频转 MP3 · Web 版 —— 双击启动 / 关闭即停（急停）
==================================================

用法
----
    * 在资源管理器里双击本文件，或
    * 命令行：python start.py [参数...]

行为
----
    1. 自动使用项目 .venv 里已安装的依赖；环境或依赖缺失会自动准备（首次较慢）
    2. 默认以「外网可访问、不校验令牌」的配置启动转换服务
    3. **本窗口就是服务进程本身**：
           关闭窗口 / 按 Ctrl+C / 任务管理器结束 python.exe  =  立即停止服务（急停）
       服务随窗口一起结束，不会留下后台残留进程继续占用端口
    4. 出错时窗口不会一闪而过，会停下来把原因显示给你看

常用参数（其余参数原样透传给 web_server.py）
----
    --local                只监听 127.0.0.1（仅本机可用，最安全）
    --host / --port        指定监听地址与端口
    --no-token             不校验令牌（默认就是这个）
    --token <令牌>         改为需要令牌才能访问
    --allowed-origins ...  Origin 白名单
    --max-file-mb / --concurrency
    --check                只做启动前自检，不真正启动
    --no-pause             结束时不停留（默认在控制台里会停留一下）
"""

import os
import subprocess
import sys

ROOT = os.path.dirname(os.path.abspath(__file__))
VENV_PY = os.path.join(ROOT, ".venv", "Scripts", "python.exe")
VENV_SITE = os.path.join(ROOT, ".venv", "Lib", "site-packages")
BUILD_PY = os.path.join(ROOT, "build.py")

# 只给 start.py 自己用的开关，不往服务端传
META_FLAGS = {"--local", "--pause", "--no-pause"}

# 子进程兜底模式的标记，防止无限套娃
CHILD_ENV = "A2M_START_CHILD"


# ---------------------------------------------------------------------------
# 基础工具
# ---------------------------------------------------------------------------
def setup_console():
    """让中文在 Windows 控制台正常显示。"""
    if os.name == "nt":
        try:
            os.system("chcp 65001 >nul 2>nul")
        except Exception:
            pass
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass


def is_tty():
    try:
        return bool(sys.stdin) and sys.stdin.isatty()
    except Exception:
        return False


def pause_at_exit():
    """双击运行时停留一下，方便看清信息；管道/重定向等非交互环境绝不卡住。"""
    if not is_tty():
        return
    try:
        input("\n按回车键关闭此窗口... ")
    except (EOFError, KeyboardInterrupt):
        pass


def die(message):
    print()
    print("[错误] " + message)
    pause_at_exit()
    raise SystemExit(1)


def same_file(a, b):
    try:
        return os.path.samefile(a, b)
    except OSError:
        return os.path.normcase(os.path.abspath(a)) == os.path.normcase(os.path.abspath(b))


def banner():
    print("=" * 64)
    print("  音频转 MP3 · Web 版 —— 双击启动 / 关闭即停")
    print("=" * 64)
    print(f"  [信息] 工作目录：{ROOT}")
    print(f"  [信息] 解释器  ：{sys.executable}")


# ---------------------------------------------------------------------------
# 依赖 / 环境
# ---------------------------------------------------------------------------
def deps_ok():
    """真正 import 一次，才能发现 ABI/版本不匹配这类问题。"""
    try:
        import codecpod  # noqa: F401
        import numpy     # noqa: F401
        return True
    except Exception:
        return False


def use_venv_site_packages():
    """把 .venv 的 site-packages 加进当前进程的搜索路径。

    这样即使双击时用的是系统 Python，也能直接复用 .venv 里装好的依赖，
    不必另起进程 —— 单进程是「关闭窗口即停止服务」的前提。
    """
    if os.path.isdir(VENV_SITE) and VENV_SITE not in sys.path:
        sys.path.insert(0, VENV_SITE)


def prepare_env():
    if not os.path.isfile(BUILD_PY):
        die(f"未找到 {BUILD_PY}")
    print("  [信息] 正在准备运行环境（首次运行较慢，需要联网）...")
    exe = VENV_PY if os.path.isfile(VENV_PY) else sys.executable
    if subprocess.call([exe, BUILD_PY, "--env-only"], cwd=ROOT) != 0:
        die("环境准备失败，请检查网络后重试，或改用 启动Web版.bat。")


def ensure_deps():
    """确保当前进程能 import codecpod / numpy。"""
    if deps_ok():
        return
    use_venv_site_packages()
    if deps_ok():
        return
    prepare_env()
    use_venv_site_packages()
    if deps_ok():
        return
    die("依赖仍不可用（可能是解释器版本与 .venv 不一致）。\n"
        "       请改用：启动Web版.bat，或 .venv\\Scripts\\python.exe start.py")


def run_with_venv_python():
    """兜底：用 .venv 解释器以子进程方式运行。

    子进程与父进程共用同一个控制台，关闭窗口时 Windows 会把两者一起结束，
    因此同样满足「关闭即停」。
    """
    env = dict(os.environ)
    env[CHILD_ENV] = "1"
    cmd = [VENV_PY, os.path.abspath(__file__)] + sys.argv[1:]
    print("  [信息] 改用 .venv 解释器启动（子进程，随本窗口一起结束）...")
    try:
        return subprocess.call(cmd, cwd=ROOT, env=env)
    except KeyboardInterrupt:
        print("\n[信息] 已中断。")
        return 0


# ---------------------------------------------------------------------------
# 参数
# ---------------------------------------------------------------------------
def strip_host(args):
    """去掉 --host 及其取值，便于 --local 覆盖。"""
    out, i = [], 0
    while i < len(args):
        if args[i] == "--host" and i + 1 < len(args):
            i += 2
            continue
        out.append(args[i])
        i += 1
    return out


def build_forward_args(raw):
    forward = [a for a in raw if a not in META_FLAGS]
    if "--local" in raw:
        forward = strip_host(forward) + ["--host", "127.0.0.1"]
    # 默认不校验令牌；只有显式给 --token 时才启用
    if not ({"--token", "--no-token", "--allow-insecure"} & set(forward)):
        forward += ["--no-token"]
    return forward


# ---------------------------------------------------------------------------
# 启动服务（始终在当前进程内）
# ---------------------------------------------------------------------------
def run_server():
    sys.path.insert(0, ROOT)
    import start_external

    forward = build_forward_args(sys.argv[1:])
    print()
    print("  关闭本窗口 / 按 Ctrl+C  =  立即停止服务（急停）")
    print("=" * 64)
    return start_external.main(forward)


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------
def main():
    setup_console()

    if "-h" in sys.argv[1:] or "--help" in sys.argv[1:]:
        print(__doc__)
        return 0

    no_pause = "--no-pause" in sys.argv[1:]

    try:
        banner()

        in_venv = os.path.isfile(VENV_PY) and same_file(sys.executable, VENV_PY)
        if in_venv or os.environ.get(CHILD_ENV) == "1":
            ensure_deps()
            rc = run_server()
        else:
            # 双击可能用的是系统 Python：先尝试就地复用 .venv 的依赖（单进程）
            if not os.path.isfile(VENV_PY):
                prepare_env()
            use_venv_site_packages()
            if deps_ok():
                print("  [信息] 已复用 .venv 中的运行依赖（单进程运行）")
                rc = run_server()
            else:
                rc = run_with_venv_python()
    except KeyboardInterrupt:
        print("\n[信息] 已中断，服务已停止。")
        rc = 0
    except SystemExit as exc:
        rc = exc.code if isinstance(exc.code, int) else 0
    except Exception as exc:
        print(f"\n[错误] {type(exc).__name__}: {exc}")
        rc = 1

    print()
    print("=" * 64)
    print("  服务已停止，现在关闭窗口是安全的。")
    print("=" * 64)
    if not no_pause:
        pause_at_exit()
    return rc


if __name__ == "__main__":
    sys.exit(main())

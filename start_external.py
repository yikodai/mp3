# -*- coding: utf-8 -*-
"""
音频转 MP3 · Web 版 —— 「外网访问」启动器
=========================================

用途
----
以「对外开放」的配置启动服务：
    绑定公网可路由地址（默认 IPv6 双栈 ::）
    -> 自动准备访问令牌（非回环地址强制要求）
    -> 自动挑选可用的高位端口
    -> 把可直接点开的访问地址打印出来

为什么需要它
------------
服务端出于安全考虑，绑定非回环地址时**强制要求 --token**，手动拼参数容易出错。
本脚本把这套流程自动化，并把防火墙放行命令一并准备好。

它会做什么 / 不会做什么
-----------------------
会：
    * 生成或复用访问令牌（保存在工作区内的 .access_token）
    * 探测本机公网 IPv6 地址，拼出带方括号的访问 URL
    * 挑选空闲端口、做启动前自检、打印防火墙放行命令
不会：
    * 自动修改防火墙 / 路由 / 光猫（除非你显式加 --run-firewall 并具备管理员权限）
    * 关闭防火墙、写工作区以外的任何文件

用法
----
    .venv\\Scripts\\python.exe start_external.py                 # 默认 IPv6 双栈
    .venv\\Scripts\\python.exe start_external.py --check         # 只做自检，不启动
    .venv\\Scripts\\python.exe start_external.py --no-token       # 不校验令牌，访问到即可用
    .venv\\Scripts\\python.exe start_external.py --port 38443
    .venv\\Scripts\\python.exe start_external.py --new-token      # 重新生成令牌
    .venv\\Scripts\\python.exe start_external.py --host 0.0.0.0   # 改走 IPv4
    .venv\\Scripts\\python.exe start_external.py --run-firewall    # 顺带放行防火墙（需管理员）
"""

import argparse
import ipaddress
import os
import secrets
import socket
import subprocess
import sys

ROOT = os.path.dirname(os.path.abspath(__file__))
TOKEN_FILE = os.path.join(ROOT, ".access_token")
FIREWALL_SCRIPT = os.path.join(ROOT, "scripts", "firewall_ipv6.ps1")

DEFAULT_HOST = "::"          # IPv6 双栈（同时接受 IPv4-mapped）
DEFAULT_PORT = 38443         # 高位非默认端口，降低被扫描概率

sys.path.insert(0, ROOT)
import web_server as ws      # noqa: E402  （复用地址工具与启动逻辑）


# ---------------------------------------------------------------------------
# 输出
# ---------------------------------------------------------------------------
def setup_console():
    """让中文在 Windows 控制台正常显示（与 启动Web版.bat 保持一致）。"""
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


def rule(char="-", width=64):
    print(char * width)


def section(title):
    print()
    print(f"== {title} ==")


# ---------------------------------------------------------------------------
# 令牌
# ---------------------------------------------------------------------------
def load_or_create_token(explicit, rotate):
    """令牌优先级：命令行 > 工作区内文件 > 新生成。"""
    if explicit:
        return explicit, "来自命令行"
    if not rotate and os.path.isfile(TOKEN_FILE):
        try:
            with open(TOKEN_FILE, "r", encoding="utf-8") as fh:
                saved = fh.read().strip()
            if saved:
                return saved, f"复用 {os.path.basename(TOKEN_FILE)}"
        except OSError:
            pass
    token = secrets.token_urlsafe(24)          # 32 个字符左右
    with open(TOKEN_FILE, "w", encoding="utf-8", newline="") as fh:
        fh.write(token + "\n")
    try:
        os.chmod(TOKEN_FILE, 0o600)            # 尽力收紧权限（Windows 上意义有限）
    except OSError:
        pass
    return token, f"新生成并保存到 {os.path.basename(TOKEN_FILE)}"


# ---------------------------------------------------------------------------
# 自检
# ---------------------------------------------------------------------------
def probe_bind(host, port=0):
    """能否绑定该地址（与正式监听保持同样的 V6ONLY 处理）。"""
    family = socket.AF_INET6 if ws.is_ipv6_host(host) else socket.AF_INET
    s = socket.socket(family, socket.SOCK_STREAM)
    try:
        if family == socket.AF_INET6 and ws.host_is_unspecified(host):
            ws.enable_dual_stack(s)
        s.bind((ws.strip_brackets(host), port))
        return True, ""
    except OSError as exc:
        return False, str(exc)
    finally:
        s.close()


def guess_source_prefix():
    """用本机第一个全局 IPv6 推出 /64，作为防火墙来源前缀的建议。"""
    for addr in ws.local_ipv6_addresses():
        try:
            return str(ipaddress.ip_network(addr + "/64", strict=False))
        except ValueError:
            continue
    return None


# ---------------------------------------------------------------------------
# 防火墙
# ---------------------------------------------------------------------------
def print_firewall_hint(port, prefix):
    src = prefix or "你的来源IPv6/64前缀"
    section("第 3 步：放行 Windows 防火墙 IPv6 入站（需管理员手动执行）")
    print("  本机暂时只能从内网访问；要让外网连上，必须放行入站端口：")
    print()
    print(f'    powershell -ExecutionPolicy Bypass -File .\\scripts\\firewall_ipv6.ps1 '
          f'-Port {port} -SourcePrefix "{src}"')
    print()
    if prefix:
        print(f"  上面用的是本机所在的 {prefix}（只允许同一 /64 的客户端）。")
        print("  若要允许任意 IPv6 来源，用 -SourcePrefix \"::/0\"（不推荐，安全性差）。")
    print("  等价 netsh 写法：")
    print(f'    netsh advfirewall firewall add rule name="AudioToMP3 Web (IPv6)" '
          f'dir=in action=allow protocol=TCP localport={port} remoteip={src}')


def run_firewall(port, prefix):
    if not os.path.isfile(FIREWALL_SCRIPT):
        print(f"[错误] 未找到脚本：{FIREWALL_SCRIPT}")
        return 1
    cmd = ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass",
           "-File", FIREWALL_SCRIPT, "-Port", str(port)]
    if prefix:
        cmd += ["-SourcePrefix", prefix]
    print("  执行：" + " ".join(cmd))
    return subprocess.call(cmd)


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------
def main(argv=None):
    setup_console()

    parser = argparse.ArgumentParser(
        description="以「外网可访问」的配置启动音频转 MP3 Web 服务",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter)
    parser.add_argument("--host", default=DEFAULT_HOST,
                        help="监听地址。:: 为 IPv6 双栈；0.0.0.0 为 IPv4；也可填具体公网地址")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT,
                        help="监听端口（建议用 20000-65000 的高位端口）")
    parser.add_argument("--token", default=None, help="访问令牌；不给则自动生成/复用")
    parser.add_argument("--new-token", action="store_true", help="丢弃旧令牌，重新生成")
    parser.add_argument("--no-token", action="store_true",
                        help="不校验令牌：任何能访问到该地址的人都能直接使用转换服务")
    parser.add_argument("--run-firewall", action="store_true",
                        help="顺带执行防火墙放行脚本（需管理员）；默认只打印命令")
    parser.add_argument("--allow-insecure", action="store_true",
                        help="同 --no-token（保留旧名字）")
    parser.add_argument("--max-file-mb", type=int, default=None, help="单文件上限 MB")
    parser.add_argument("--concurrency", type=int, default=None, help="并发转换上限")
    parser.add_argument("--allowed-origins", default=None,
                        help="逗号分隔的 Origin 白名单（无令牌时可用于限制来源）")
    parser.add_argument("--no-browser", action="store_true", help="不自动打开浏览器")
    parser.add_argument("--check", action="store_true",
                        help="只做启动前自检并打印访问信息，不真正启动服务")
    args = parser.parse_args(argv)

    host = ws.strip_brackets(args.host)
    is_v6 = ws.is_ipv6_host(host)
    loopback = ws.host_is_loopback(host)

    rule("=")
    print("  音频转 MP3 · Web 版 —— 外网访问启动器")
    rule("=")

    # ---- 第 1 步：自检 -----------------------------------------------------
    section("第 1 步：启动前自检")
    ok, why = probe_bind(host)
    if not ok:
        print(f"  [失败] 无法绑定 {ws.host_for_url(host)}：{why}")
        if is_v6:
            print("         本机可能没有可用的 IPv6。可用 --host 0.0.0.0 改走 IPv4。")
        return 1
    print(f"  [通过] 可绑定 {ws.host_for_url(host)}（{'IPv6' if is_v6 else 'IPv4'}）")

    port = ws.find_free_port(host, args.port)
    if port != args.port:
        print(f"  [提示] 端口 {args.port} 被占用，改用 {port}")
    print(f"  [通过] 使用端口 {port}")

    if loopback:
        print("  [警告] 该地址是回环地址，只能在服务器本机访问，不构成外网访问。")

    no_token = args.no_token or args.allow_insecure
    if no_token and args.token:
        print("  [失败] --no-token 与 --token 互斥，请二选一。")
        return 1

    if no_token:
        token, token_src = None, "未启用（任何能访问到的人都能直接使用）"
    else:
        try:
            token, token_src = load_or_create_token(args.token, args.new_token)
        except OSError as exc:
            print(f"  [失败] 无法读写令牌文件：{exc}")
            return 1
    print(f"  [通过] 访问令牌：{token_src}")

    # ---- 第 2 步：访问地址 -------------------------------------------------
    section("第 2 步：访问地址")
    suffix = f"?token={token}" if token else ""

    if loopback:
        print(f"  本机： http://{ws.host_for_url(host)}:{port}/{suffix}")
    else:
        if is_v6:
            print(f"  本机自测： http://[::1]:{port}/{suffix}")
            global6 = ws.local_ipv6_addresses()
            if global6:
                print("  外网 IPv6（需先放行防火墙）：")
                for addr in global6:
                    print(f"    http://[{addr}]:{port}/{suffix}")
            else:
                print("  [警告] 未探测到全局 IPv6 地址，外网可能无法访问。")
        else:
            print(f"  外网 IPv4： http://<本机公网IPv4>:{port}/{suffix}")
            print("  可用 ipconfig 查看本机地址；注意 IPv4 通常还需在路由器上做端口映射。")

    # ---- 第 3 步：防火墙 ---------------------------------------------------
    if not loopback:
        prefix = guess_source_prefix() if is_v6 else None
        print_firewall_hint(port, prefix)
        if args.run_firewall and is_v6:
            print()
            rc = run_firewall(port, prefix)
            if rc != 0:
                print("  [提示] 防火墙脚本未成功执行（多半是没有管理员权限）。")
                print("         请右键 PowerShell -> 以管理员身份运行，再执行上面的命令。")

    # ---- 第 4 步：安全提醒 -------------------------------------------------
    section("安全提醒")
    if loopback:
        print("  当前仅本机可访问。")
    elif no_token:
        print("  * 已关闭令牌校验：任何能连到该地址的人都能直接上传音频、占用 CPU/内存。")
        print("  * IPv6 无 NAT，放行防火墙后等于对全网 IPv6 开放。")
        print("  * 可用 --allowed-origins 做来源白名单，或在防火墙里只放行可信 IPv6 前缀。")
        print("  * 音频以 HTTP 明文传输。")
        if is_v6:
            print("  * 若本机 IPv6 使用隐私扩展，地址可能变化，会影响固定 URL。")
    else:
        print("  * 你正在把服务对外开放。IPv6 没有 NAT，放行后任何 IPv6 客户端都能连到，")
        print("    因此必须依赖 --token 保护（本脚本已自动配置）。")
        print("  * 音频以 HTTP 明文传输；长期方案建议反向代理 + HTTPS。")
        print("  * 令牌保存在工作区的 .access_token 中，请勿把该文件分享出去。")
        if is_v6:
            print("  * 若本机 IPv6 使用隐私扩展，地址可能变化，会影响固定 URL。")

    if args.check:
        section("自检模式")
        print("  已完成自检，未启动服务。去掉 --check 即可真正启动。")
        rule("=")
        return 0

    # ---- 启动 --------------------------------------------------------------
    section("启动服务")
    print("  按 Ctrl+C 或关闭本窗口即可停止。")
    print()
    rule("=")

    server_argv = ["--host", host, "--port", str(port)]
    if token:
        server_argv += ["--token", token]
    elif no_token:
        server_argv += ["--allow-insecure"]
    if args.no_browser or not loopback:
        # 外网场景下默认不弹浏览器（服务器通常没有桌面）
        server_argv += ["--no-browser"]
    if args.max_file_mb:
        server_argv += ["--max-file-mb", str(args.max_file_mb)]
    if args.concurrency:
        server_argv += ["--concurrency", str(args.concurrency)]
    if args.allowed_origins:
        server_argv += ["--allowed-origins", args.allowed_origins]

    return ws.main(server_argv)


if __name__ == "__main__":
    sys.exit(main())

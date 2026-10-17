# -*- coding: utf-8 -*-
"""
音频转 MP3 · Web 版服务端（Windows 可部署）
==========================================

单进程实现，只用 Python 标准库 http.server + 现有依赖 codecpod / numpy，
可部署在 Windows 服务器上，浏览器访问后批量把音频转成 MP3。

核心约束（不可违反）
--------------------
    * 音频全程驻留内存：
        请求体 -> bytes -> codecpod.load(bytes) -> numpy
        -> codecpod.save_bytes(Mp3) -> HTTP 响应
      不落任何磁盘临时文件（输入 / 中间 PCM / 输出 MP3 / ZIP 一律不落盘）。
    * 不使用 tempfile、磁盘缓存、上传落盘、转换落盘方案。
    * 不引入 FastAPI / Flask 等默认会把上传体 spool 到磁盘的框架。

安全默认
--------
    * 默认只监听 127.0.0.1（仅本机可访问）。
    * 监听非回环地址（含 IPv6 公网地址）时必须显式提供访问令牌 --token，
      或明确接受风险 --allow-insecure，否则拒绝启动。
    * 可选的 Origin/Host 白名单，用于缓解 DNS rebinding。

IPv6 / 双栈
-----------
    * 支持 IPv6 绑定：
        --host ::                    双栈监听（IPv6 全部地址 + IPv4-mapped）
        --host ::1                   仅 IPv6 本机
        --host 2409:xxxx:xxxx::1     指定公网/内网 IPv6 地址
    * 命令行也接受方括号形式（如 --host [::1]），内部会自动去掉方括号。
    * URL 中的 IPv6 必须使用方括号：http://[::1]:8765/ 、http://[2409:...]:8765/
    * 绑定 :: 时会显式设置 IPV6_V6ONLY=0，因此在 Windows 上也能同时服务 IPv4。
      （Windows 的 AF_INET6 套接字默认 V6ONLY=1，只监听 :: 会拒绝 IPv4 客户端。）

配置（CLI 参数优先于环境变量）
------------------------------
    --host              / A2M_HOST             监听地址（默认 127.0.0.1；支持 IPv6）
    --port              / A2M_PORT             监听端口（默认 8765，占用则自动后移）
    --max-file-mb       / A2M_MAX_FILE_MB      单文件上限 MB（默认 512）
    --max-pcm-mb        / A2M_MAX_PCM_MB       解码后 PCM 上限 MB（默认 4×单文件上限）
    --concurrency       / A2M_CONCURRENCY      并发转换上限（默认 2）
    --timeout           / A2M_TIMEOUT          单请求读超时秒（默认 300）
    --token             / A2M_TOKEN            访问令牌（非回环绑定时必须）
    --allow-insecure    / A2M_ALLOW_INSECURE   非回环且无令牌时明确接受风险
    --allowed-origins   / A2M_ALLOWED_ORIGINS  逗号分隔 Origin 白名单（可选）
    --no-browser        / —                    不自动打开浏览器
    --log-file          / A2M_LOG_FILE         追加日志到指定文件（须在项目内，不含音频数据）

接口
----
    GET  /                     静态页面
    GET  /style.css /app.js    静态资源
    GET  /api/health           健康检查（JSON）
    GET  /api/formats          支持格式与比特率（JSON）
    POST /api/convert?bitrate=192&name=xxx.mp3
                               请求体 = 单个音频原始字节；返回 audio/mpeg
    POST /api/zip              请求体 = 自定义帧（见下）；返回 application/zip

自定义帧格式（POST /api/zip，与 web/app.js 约定一致，大端序）：
    重复若干块：[u32 名称长度][名称 UTF-8][u32 数据长度][数据]
"""

import argparse
import hmac
import io
import ipaddress
import json
import os
import socket
import struct
import sys
import threading
import time
import urllib.parse
import uuid
import webbrowser
import zipfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

ROOT = os.path.dirname(os.path.abspath(__file__))
WEB_DIR = os.path.join(ROOT, "web")

# 支持的输入扩展名（与桌面版 main.py 保持一致）
SUPPORTED_EXTS = (
    "wav", "flac", "m4a", "aac", "ogg", "oga", "wma", "aiff", "aif",
    "mp3", "ape", "opus", "wv", "ac3", "alac", "mka", "mp2", "mp1",
    "amr", "caf", "au",
)

BITRATES = (128, 192, 256, 320)
DEFAULT_BITRATE = 192

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8765
DEFAULT_MAX_FILE_MB = 512
DEFAULT_CONCURRENCY = 2
DEFAULT_TIMEOUT = 300
COOKIE_NAME = "a2m_token"

# 拒绝超大请求体时，最多再"丢弃"读取这么多字节，好让客户端把余量发完并正常读到错误响应；
# 超过则直接断开连接。丢掉的字节不进入内存，不影响内存保护。
DRAIN_MAX = 8 * 1024 * 1024

CONTENT_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".js": "text/javascript; charset=utf-8",
    ".json": "application/json; charset=utf-8",
    ".svg": "image/svg+xml",
    ".ico": "image/x-icon",
    ".png": "image/png",
    ".txt": "text/plain; charset=utf-8",
    ".woff2": "font/woff2",
}

STAMP = time.strftime("%Y%m%d_%H%M%S")
SESSION_ID = uuid.uuid4().hex[:12]
STARTED_AT = time.time()

_LOG = None
_LOG_LOCK = threading.Lock()


def log(message):
    """控制台 / 日志文件输出。只写元信息，绝不包含音频内容。"""
    line = "[%s] %s" % (time.strftime("%Y-%m-%d %H:%M:%S"), message)
    sys.stderr.write(line + "\n")
    sys.stderr.flush()
    if _LOG is not None:
        with _LOG_LOCK:
            try:
                _LOG.write(line + "\n")
                _LOG.flush()
            except Exception:
                pass


# ---------------------------------------------------------------------------
# 编解码引擎：codecpod（静态链接 FFmpeg）。导入失败时给出可操作的中文提示。
# ---------------------------------------------------------------------------
try:
    import codecpod

    # FFmpeg 的诊断信息默认只进 stderr；这里全部丢弃，避免任何日志噪声/落盘。
    codecpod.set_log(lambda level, message: None)
    _IMPORT_ERROR = None
except Exception as _exc:  # pragma: no cover
    codecpod = None
    _IMPORT_ERROR = _exc


# ---------------------------------------------------------------------------
# 监听地址工具（IPv4 / IPv6 统一处理）
# ---------------------------------------------------------------------------
def strip_brackets(host):
    """去掉 IPv6 的方括号，例如 [::1] -> ::1。"""
    h = (host or "").strip()
    if len(h) >= 2 and h[0] == "[" and h[-1] == "]":
        return h[1:-1].strip()
    return h


def _bare_ip(host):
    """去掉方括号与区域索引(zone id)，得到可用于 ipaddress 解析的裸地址。"""
    return strip_brackets(host).split("%", 1)[0]


def is_ipv6_host(host):
    """该监听地址是否应使用 AF_INET6。"""
    h = _bare_ip(host)
    try:
        return ipaddress.ip_address(h).version == 6
    except ValueError:
        return ":" in h          # 含冒号但解析失败，仍按 IPv6 处理


def host_is_loopback(host):
    """仅 127.0.0.0/8、::1、localhost 视为回环；空串/未指定地址不算回环。"""
    h = strip_brackets(host).lower()
    if h == "localhost":
        return True
    try:
        return ipaddress.ip_address(_bare_ip(h)).is_loopback
    except ValueError:
        return False


def host_is_unspecified(host):
    """0.0.0.0 / :: / 空串 表示监听全部地址。"""
    h = strip_brackets(host)
    if h == "":
        return True
    try:
        return ipaddress.ip_address(_bare_ip(h)).is_unspecified
    except ValueError:
        return False


def host_for_url(host):
    """URL 中使用的 host 形式：IPv6 必须加方括号。"""
    h = strip_brackets(host)
    return f"[{h}]" if is_ipv6_host(h) else h


def addr_family(host):
    return socket.AF_INET6 if is_ipv6_host(host) else socket.AF_INET


def enable_dual_stack(sock):
    """在 IPv6 套接字上关闭 V6ONLY，使其同时接受 IPv4-mapped 连接。"""
    try:
        sock.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 0)
        return True
    except OSError:
        return False


# ---------------------------------------------------------------------------
# 配置
# ---------------------------------------------------------------------------
class Config:
    def __init__(self):
        self.host = DEFAULT_HOST
        self.port = DEFAULT_PORT
        self.max_file_mb = DEFAULT_MAX_FILE_MB
        self.max_pcm_mb = DEFAULT_MAX_FILE_MB * 4
        self.concurrency = DEFAULT_CONCURRENCY
        self.timeout = DEFAULT_TIMEOUT
        self.token = None
        self.allow_insecure = False
        self.allowed_origins = set()
        self.open_browser = True
        self.log_file = None

    @property
    def max_body_bytes(self):
        return self.max_file_mb * 1024 * 1024

    @property
    def max_zip_bytes(self):
        return self.max_body_bytes * 4

    @property
    def max_pcm_bytes(self):
        return self.max_pcm_mb * 1024 * 1024

    @property
    def is_loopback(self):
        return host_is_loopback(self.host)

    @property
    def is_unspecified(self):
        return host_is_unspecified(self.host)

    @property
    def family(self):
        return addr_family(self.host)

    @property
    def family_name(self):
        return "IPv6" if is_ipv6_host(self.host) else "IPv4"

    @property
    def dual_stack(self):
        """仅 IPv6 未指定地址(::)启用双栈，使 IPv4 客户端也能连上。"""
        return is_ipv6_host(self.host) and host_is_unspecified(self.host)

    @property
    def url_host(self):
        """用于拼访问地址：未指定地址回退到本机回环地址。"""
        if host_is_unspecified(self.host):
            return "::1" if is_ipv6_host(self.host) else "127.0.0.1"
        return strip_brackets(self.host)

    @property
    def display_host(self):
        """用于日志/提示：IPv6 带方括号。"""
        return host_for_url(self.host)


def _env(name, default=None):
    v = os.environ.get(name)
    return v if v not in (None, "") else default


def parse_args(argv=None):
    env_host = _env("A2M_HOST")
    env_port = _env("A2M_PORT")
    env_file = _env("A2M_MAX_FILE_MB")
    env_pcm = _env("A2M_MAX_PCM_MB")
    env_conc = _env("A2M_CONCURRENCY")
    env_timeout = _env("A2M_TIMEOUT")

    parser = argparse.ArgumentParser(
        description="音频转 MP3 · Web 版服务端（转换全程内存化）")
    parser.add_argument("--host", default=env_host or DEFAULT_HOST,
                        help=f"监听地址，默认 {DEFAULT_HOST}（仅本机）。"
                             "支持 IPv6：::（双栈）/ ::1 / [::1] / 2409:xxxx::1")
    parser.add_argument("--port", type=int,
                        default=int(env_port) if env_port else DEFAULT_PORT,
                        help=f"监听端口，默认 {DEFAULT_PORT}（占用则自动后移）")
    parser.add_argument("--max-file-mb", type=int,
                        default=int(env_file) if env_file else DEFAULT_MAX_FILE_MB,
                        help=f"单文件上限 MB，默认 {DEFAULT_MAX_FILE_MB}")
    parser.add_argument("--max-pcm-mb", type=int,
                        default=int(env_pcm) if env_pcm else None,
                        help="解码后 PCM 上限 MB，默认 4×单文件上限")
    parser.add_argument("--concurrency", type=int,
                        default=int(env_conc) if env_conc else DEFAULT_CONCURRENCY,
                        help=f"并发转换上限，默认 {DEFAULT_CONCURRENCY}")
    parser.add_argument("--timeout", type=int,
                        default=int(env_timeout) if env_timeout else DEFAULT_TIMEOUT,
                        help=f"单请求读超时秒，默认 {DEFAULT_TIMEOUT}")
    parser.add_argument("--token", default=_env("A2M_TOKEN"),
                        help="访问令牌；绑定非回环地址时必填")
    parser.add_argument("--allow-insecure", action="store_true",
                        default=_env("A2M_ALLOW_INSECURE") == "1",
                        help="非回环且无令牌时，明确接受无保护暴露的风险")
    parser.add_argument("--allowed-origins", default=_env("A2M_ALLOWED_ORIGINS"),
                        help="逗号分隔的 Origin 白名单，例如 http://10.0.0.5:8765")
    parser.add_argument("--no-browser", action="store_true", help="不自动打开浏览器")
    parser.add_argument("--log-file", default=_env("A2M_LOG_FILE"),
                        help="追加日志到文件（须位于项目目录内）")
    args = parser.parse_args(argv)

    cfg = Config()
    cfg.host = strip_brackets(args.host)      # 容许 --host [::1] 这种写法
    cfg.port = args.port
    cfg.max_file_mb = max(1, args.max_file_mb)
    cfg.max_pcm_mb = max(1, args.max_pcm_mb or args.max_file_mb * 4)
    cfg.concurrency = max(1, args.concurrency)
    cfg.timeout = max(5, args.timeout)
    cfg.token = args.token or None
    cfg.allow_insecure = bool(args.allow_insecure)
    cfg.open_browser = not args.no_browser
    cfg.log_file = args.log_file
    if args.allowed_origins:
        cfg.allowed_origins = {
            o.strip().rstrip("/") for o in args.allowed_origins.split(",") if o.strip()
        }
    return cfg


# ---------------------------------------------------------------------------
# 内存化转换核心
# ---------------------------------------------------------------------------
def convert_bytes(data, bit_rate_bps):
    """
    把任意受支持音频的字节流，在内存中转换成 MP3 字节流。

    解码失败会抛出 codecpod 的异常（可能是继承自 BaseException 的
    PyO3 PanicException），由调用方统一兜底。
    """
    samples, sample_rate = codecpod.load(data)          # bytes -> numpy（不落盘）
    return codecpod.save_bytes(                          # numpy -> bytes（不落盘）
        samples, sample_rate, codec=codecpod.Mp3(bit_rate=bit_rate_bps)
    )


def pcm_bytes_of(samples):
    """尽力估算解码后 PCM 占用（用于内存保护）；取不到时返回 0（跳过检查）。"""
    nbytes = getattr(samples, "nbytes", None)
    if isinstance(nbytes, int):
        return nbytes
    size = getattr(samples, "size", None)
    itemsize = getattr(samples, "itemsize", None)
    if isinstance(size, int) and isinstance(itemsize, int):
        return size * itemsize
    return 0


def safe_arcname(name):
    """把条目名裁剪成安全的纯文件名（防止 zip 路径穿越与非法字符）。"""
    name = os.path.basename(name.replace("\\", "/")).strip() or "output.mp3"
    name = name.replace("\x00", "")
    if not name.lower().endswith(".mp3"):
        name += ".mp3"
    return name


def unique_name(name, used):
    """在同一批 ZIP 内保证条目名不重复。"""
    stem, ext = os.path.splitext(name)
    candidate, i = name, 1
    while candidate.lower() in used:
        candidate = f"{stem}_{i}{ext}"
        i += 1
    used.add(candidate.lower())
    return candidate


def parse_frames(buf):
    """解析 /api/zip 的自定义帧，返回 [(名称, 数据字节)]。"""
    files = []
    off, n = 0, len(buf)
    while off < n:
        if off + 4 > n:
            raise ValueError("帧格式错误：名称长度字段不完整")
        (name_len,) = struct.unpack_from(">I", buf, off)
        off += 4
        if off + name_len > n:
            raise ValueError("帧格式错误：名称字段不完整")
        name = buf[off:off + name_len].decode("utf-8", "replace")
        off += name_len
        if off + 4 > n:
            raise ValueError("帧格式错误：数据长度字段不完整")
        (data_len,) = struct.unpack_from(">I", buf, off)
        off += 4
        if off + data_len > n:
            raise ValueError("帧格式错误：数据字段不完整")
        files.append((name, buf[off:off + data_len]))
        off += data_len
    return files


# ---------------------------------------------------------------------------
# HTTP 处理器
# ---------------------------------------------------------------------------
class Handler(BaseHTTPRequestHandler):
    server_version = "AudioToMP3-Web"
    protocol_version = "HTTP/1.1"          # 开启 keep-alive，需保证所有响应都有 Content-Length

    cfg = None                              # 由 main() 注入
    gate = None                             # threading.BoundedSemaphore 并发闸门

    # ---------------------------------------------------------- 工具方法
    def log_message(self, fmt, *args):
        """默认会把每个请求打到 stderr 且带时间；这里统一走 log()。"""
        log("%s - %s" % (self.address_string(), fmt % args))

    def _send_bytes(self, status, body, content_type, extra_headers=None):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        if self.close_connection:
            self.send_header("Connection", "close")
        for k, v in (extra_headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _send_json(self, status, obj, extra_headers=None):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self._send_bytes(status, body, "application/json; charset=utf-8", extra_headers)

    def _drain(self, n):
        """丢弃读取最多 n 字节（不缓存），用于让被拒请求的连接体面收尾。"""
        remaining = n
        while remaining > 0:
            chunk = self.rfile.read(min(64 * 1024, remaining))
            if not chunk:
                break
            remaining -= len(chunk)

    def _read_body(self, limit):
        """把请求体完整读进内存；超过上限抛 ValueError。"""
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            raise ValueError("Content-Length 非法")
        if length <= 0:
            return b""
        if length > limit:
            # 关键：先把客户端余量读完再回应，否则 keep-alive 连接会因残留体而错位。
            self._drain(min(length, DRAIN_MAX))
            self.close_connection = True
            self._body_consumed = True          # 已丢弃，勿在错误处理里重复 drain
            raise ValueError(f"请求体超过上限（{limit // 1024 // 1024} MB）")
        buf = bytearray()
        remaining = length
        while remaining > 0:
            chunk = self.rfile.read(min(1024 * 1024, remaining))
            if not chunk:
                break
            buf.extend(chunk)
            remaining -= len(chunk)
        if remaining > 0:
            self.close_connection = True
            self._body_consumed = True
            raise ValueError("请求体不完整（连接被中断或超时）")
        self._body_consumed = True
        return bytes(buf)

    # ------------------------------------------------------------- 鉴权
    def _request_token(self):
        q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
        if q.get("token"):
            return q["token"][0]
        header = self.headers.get("X-Auth-Token")
        if header:
            return header
        cookie = self.headers.get("Cookie", "")
        for part in cookie.split(";"):
            k, _, v = part.strip().partition("=")
            if k == COOKIE_NAME and v:
                return urllib.parse.unquote(v)
        return None

    def _authorized(self):
        if not self.cfg.token:
            return True
        supplied = self._request_token()
        return bool(supplied) and hmac.compare_digest(supplied, self.cfg.token)

    def _origin_allowed(self):
        """可选 Origin 白名单，用于缓解 DNS rebinding（仅对携带 Origin 的请求生效）。"""
        if not self.cfg.allowed_origins:
            return True
        origin = self.headers.get("Origin")
        if not origin:
            return True                     # 非浏览器/同源简化请求，跳过
        return origin.rstrip("/") in self.cfg.allowed_origins

    def _deny(self, status, message):
        """拒绝请求：先丢弃未读的请求体再回应，随后关闭连接（避免 keep-alive 错位）。"""
        remaining = self._declared_length()
        if remaining > 0:
            self._drain(min(remaining, DRAIN_MAX))
        if urllib.parse.urlparse(self.path).path.startswith("/api/"):
            self._send_json(status, {"error": message})
        else:
            self._send_bytes(status, message.encode("utf-8"),
                             "text/plain; charset=utf-8")
        self.close_connection = True

    def _declared_length(self):
        try:
            return int(self.headers.get("Content-Length", "0") or "0")
        except ValueError:
            return 0

    def _fail(self, status, obj, extra_headers=None):
        """错误响应：若请求体尚未读完，先丢弃余量并关闭连接，避免 keep-alive 错位。"""
        if not getattr(self, "_body_consumed", False):
            remaining = self._declared_length()
            if remaining > 0:
                self._drain(min(remaining, DRAIN_MAX))
            self.close_connection = True
        self._send_json(status, obj, extra_headers)

    # --------------------------------------------------------------- GET
    def do_HEAD(self):
        self.do_GET()

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        query = urllib.parse.parse_qs(parsed.query)

        cookie_headers = None
        if self.cfg.token:
            supplied = self._request_token()
            if not supplied or not hmac.compare_digest(supplied, self.cfg.token):
                self._deny(401, "需要访问令牌：请在地址栏使用 ?token=你的令牌 访问")
                return
            if query.get("token"):
                # 用 URL 里的令牌首次访问：下发 Cookie，之后静态资源自动带上
                cookie_headers = {
                    "Set-Cookie": f"{COOKIE_NAME}={urllib.parse.quote(self.cfg.token)}"
                                  "; Path=/; HttpOnly; SameSite=Strict"
                }

        if path == "/api/health":
            self._send_json(200, {
                "status": "ok",
                "engine": f"codecpod {_codecpod_version()}",
                "python": sys.version.split()[0],
                "session": SESSION_ID,
                "uptime_s": round(time.time() - STARTED_AT, 1),
                # ---- 绑定信息（便于确认部署状态）----
                "listen": f"{self.cfg.display_host}:{self.cfg.port}",
                "bind_host": self.cfg.host,
                "bind_port": self.cfg.port,
                "bind_family": self.cfg.family_name,
                "dual_stack": self.cfg.dual_stack,
                "is_loopback": self.cfg.is_loopback,
                "is_unspecified": self.cfg.is_unspecified,
                "auth_required": bool(self.cfg.token),
                "local_url": f"http://{host_for_url(self.cfg.url_host)}:{self.cfg.port}/",
                # ---- 运行限制 ----
                "max_file_mb": self.cfg.max_file_mb,
                "max_pcm_mb": self.cfg.max_pcm_mb,
                "concurrency": self.cfg.concurrency,
                "max_body_mb": self.cfg.max_body_bytes // 1024 // 1024,
                "extensions": list(SUPPORTED_EXTS),
                "bitrates": list(BITRATES),
                "default_bitrate": DEFAULT_BITRATE,
            })
            return

        if path == "/api/formats":
            self._send_json(200, {
                "extensions": list(SUPPORTED_EXTS),
                "bitrates": list(BITRATES),
                "default_bitrate": DEFAULT_BITRATE,
                "max_file_mb": self.cfg.max_file_mb,
            })
            return

        if path == "/favicon.ico":
            # 页面已用 data: URI 内置图标；这里只作为兜底，避免 404 噪声。
            self.send_response(204)
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            return

        if path in ("/", "/index.html"):
            self._serve_static(os.path.join(WEB_DIR, "index.html"), cookie_headers)
            return

        # 其它静态资源：仅允许 web 目录内的文件（防路径穿越）
        rel = urllib.parse.unquote(path).lstrip("/\\")
        target = os.path.realpath(os.path.join(WEB_DIR, rel))
        if not _is_within(target, os.path.realpath(WEB_DIR)):
            self._send_json(403, {"error": "禁止访问"})
            return
        self._serve_static(target, cookie_headers)

    def _serve_static(self, target, extra_headers=None):
        if not os.path.isfile(target):
            self._send_json(404, {"error": "文件不存在"})
            return
        ctype = CONTENT_TYPES.get(os.path.splitext(target)[1].lower(), "application/octet-stream")
        with open(target, "rb") as fh:          # 只读打开，不写入
            body = fh.read()
        self._send_bytes(200, body, ctype, extra_headers)

    # -------------------------------------------------------------- POST
    def do_POST(self):
        if not self._authorized():
            self._deny(401, "需要访问令牌（请求头 X-Auth-Token 或 Cookie）")
            return
        if not self._origin_allowed():
            self._deny(403, "Origin 不在白名单内")
            return

        path = urllib.parse.urlparse(self.path).path
        self._body_consumed = False
        acquired = False
        try:
            if path not in ("/api/convert", "/api/zip"):
                self._fail(404, {"error": "未知接口"})
                return

            # 并发闸门：拿不到名额立即返回 503，避免大文件并发堆积导致 OOM
            acquired = self.gate.acquire(blocking=False)
            if not acquired:
                self._fail(503, {"error": "服务器繁忙，请稍后重试"},
                           extra_headers={"Retry-After": "3"})
                return

            if path == "/api/convert":
                self._handle_convert()
            else:
                self._handle_zip()
        except ValueError as exc:
            self._fail(400, {"error": str(exc)})
        except MemoryError:
            self._fail(507, {"error": "服务器内存不足，请减小文件或降低并发"})
        except socket.timeout:
            self.close_connection = True
            self._fail(408, {"error": "请求读取超时"})
        except BaseException as exc:            # 兜底：含 PanicException 等非 Exception 异常
            self._fail(500, {"error": f"{type(exc).__name__}: {exc}"})
        finally:
            if acquired:
                self.gate.release()

    def _handle_convert(self):
        if codecpod is None:
            self._fail(500, {"error": f"音频引擎加载失败：{_IMPORT_ERROR}"})
            return

        query = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
        raw_name = (query.get("name", [""])[0] or "").strip()
        try:
            bitrate = int(query.get("bitrate", [str(DEFAULT_BITRATE)])[0])
        except ValueError:
            raise ValueError("比特率参数非法")
        if bitrate not in BITRATES:
            raise ValueError(f"不支持的比特率：{bitrate}（可选 {', '.join(map(str, BITRATES))}）")

        data = self._read_body(self.cfg.max_body_bytes)
        if not data:
            raise ValueError("请求体为空")

        data = _strip_data_url(data)             # 容忍 data:audio/...;base64, 前缀

        samples = None
        try:
            samples, sample_rate = codecpod.load(data)      # bytes -> numpy（不落盘）
        except MemoryError:
            raise
        except BaseException as exc:             # 坏文件：返回 400 + 中文原因
            self._fail(400, {"error": f"{type(exc).__name__}: {exc}"})
            return

        pcm = pcm_bytes_of(samples)
        if pcm and pcm > self.cfg.max_pcm_bytes:
            del samples
            self._fail(413, {
                "error": f"解码后音频过大（约 {pcm // 1024 // 1024} MB，"
                         f"上限 {self.cfg.max_pcm_mb} MB），请调大 --max-pcm-mb 或降低源文件规格"
            })
            return

        try:
            mp3 = codecpod.save_bytes(                     # numpy -> bytes（不落盘）
                samples, sample_rate,
                codec=codecpod.Mp3(bit_rate=bitrate * 1000),
            )
        finally:
            del samples

        stem = os.path.splitext(os.path.basename(raw_name or "output"))[0] or "output"
        out_name = stem + ".mp3"
        ascii_fallback = out_name.encode("ascii", "ignore").decode() or "output.mp3"
        self._send_bytes(
            200, mp3, "audio/mpeg",
            extra_headers={
                # 头部只能放 latin-1，非 ASCII 文件名用百分号编码承载
                "X-Out-Name": urllib.parse.quote(out_name),
                "X-Out-Size": str(len(mp3)),
                "Content-Disposition": (
                    f'attachment; filename="{ascii_fallback}"; '
                    f"filename*=UTF-8''{urllib.parse.quote(out_name)}"
                ),
            },
        )

    def _handle_zip(self):
        data = self._read_body(self.cfg.max_zip_bytes)
        if not data:
            raise ValueError("请求体为空")
        items = parse_frames(data)
        if not items:
            raise ValueError("没有可打包的内容")

        used = set()
        buf = io.BytesIO()                       # 内存 zip，不落盘
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
            for name, blob in items:
                zf.writestr(unique_name(safe_arcname(name), used), blob)

        zip_name = f"mp3_output_{time.strftime('%Y%m%d_%H%M%S')}.zip"
        self._send_bytes(
            200, buf.getvalue(), "application/zip",
            extra_headers={
                "X-Out-Name": urllib.parse.quote(zip_name),
                "Content-Disposition": f'attachment; filename="{zip_name}"',
            },
        )


# ---------------------------------------------------------------------------
# 辅助
# ---------------------------------------------------------------------------
def _is_within(target, base):
    try:
        return os.path.commonpath([target, base]) == base
    except ValueError:                          # 不同盘符
        return False


def _strip_data_url(data):
    """容忍前端误传的 data URL 前缀，惰性剥掉，避免误报解码失败。"""
    if data[:5].lower() == b"data:":
        comma = data.find(b",")
        if comma != -1:
            return data[comma + 1:]
    return data


def _codecpod_version():
    try:
        from importlib.metadata import version
        return version("codecpod")
    except Exception:
        return "未知"


def find_free_port(host, preferred, span=20):
    """优先使用 preferred，被占用则向后找一个空闲端口（自动匹配 IPv4/IPv6）。"""
    host = strip_brackets(host)
    family = addr_family(host)
    dual = is_ipv6_host(host) and host_is_unspecified(host)
    candidates = [preferred] + [p for p in range(preferred + 1, preferred + span)]
    for port in candidates:
        s = socket.socket(family, socket.SOCK_STREAM)
        try:
            if dual:
                enable_dual_stack(s)     # 与正式监听保持一致，避免占用判断失真
            s.bind((host, port))
            return port
        except OSError:
            continue
        finally:
            s.close()
    raise SystemExit(f"端口 {preferred} 起连续 {span} 个端口均被占用，无法启动。")


class DualStackHTTPServer(ThreadingHTTPServer):
    """
    按监听地址选择 IPv4/IPv6 的 HTTP 服务端。

    * IPv6 地址使用 AF_INET6 套接字；
    * 绑定 :: 时显式关闭 IPV6_V6ONLY，使 IPv4-mapped 客户端也能连接（真正的双栈）。
      Windows 的 AF_INET6 套接字默认 V6ONLY=1，若不处理，只绑 :: 会拒绝所有 IPv4 客户端。
    """

    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, server_address, handler,
                 family=socket.AF_INET, dual_stack=False):
        self.address_family = family
        self._dual_stack = dual_stack
        super().__init__(server_address, handler)

    def server_bind(self):
        if self.address_family == socket.AF_INET6 and self._dual_stack:
            enable_dual_stack(self.socket)
        super().server_bind()

    @property
    def dual_stack_enabled(self):
        return self.address_family == socket.AF_INET6 and self._dual_stack


def check_security(cfg):
    """非回环绑定的安全闸门：必须显式提供令牌或明确接受风险。"""
    if cfg.is_loopback:
        return None
    if cfg.token:
        return None
    if cfg.allow_insecure:
        return (f"已按 --allow-insecure 以无鉴权方式对外暴露（{cfg.display_host}），风险自负。")
    hint = (
        "\n[安全] 拒绝启动：你正在把服务绑定到非回环地址 %s，但没有设置访问令牌。\n"
        "       这会让同网段（或公网 IPv6）上的任何人都能使用你的转换服务。\n"
        "       请二选一：\n"
        "         1) 加访问令牌（推荐）：--token 你的随机口令\n"
        "            例如：python web_server.py --host %s --port 8765 --token <32位随机串>\n"
        "         2) 明确接受无保护风险：追加 --allow-insecure\n"
        "       更稳妥的做法：保持默认只监听 127.0.0.1，用 Nginx/IIS 反向代理 + HTTPS 对外。\n"
        % (cfg.display_host, cfg.display_host)
    )
    if cfg.is_unspecified:
        hint += (
            "       [注意] %s 表示监听本机全部地址。若本机有公网 IPv6，\n"
            "              这等于直接暴露到公网；请务必用 Windows 防火墙限制来源 IPv6 前缀，\n"
            "              并优先改用反向代理 + HTTPS。\n" % cfg.display_host
        )
    raise SystemExit(hint)


def local_ipv6_addresses():
    """列出本机可用于对外访问的全局 IPv6 地址（排除回环、link-local 与 Teredo）。"""
    teredo = ipaddress.ip_network("2001::/32")   # Teredo 隧道地址，通常不接受入站连接
    found = []
    try:
        infos = socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET6)
    except OSError:
        infos = []
    for info in infos:
        addr = str(info[4][0]).split("%", 1)[0]
        try:
            ip = ipaddress.ip_address(addr)
        except ValueError:
            continue
        if ip.is_loopback or ip.is_link_local or ip.is_unspecified:
            continue
        if ip in teredo:
            continue
        if addr not in found:
            found.append(addr)
    return found


def main(argv=None):
    global _LOG
    cfg = parse_args(argv)

    if cfg.log_file:
        log_path = os.path.realpath(cfg.log_file)
        if not _is_within(log_path, os.path.realpath(ROOT)):
            raise SystemExit(f"[错误] --log-file 必须位于项目目录内：{ROOT}")
        _LOG = open(log_path, "a", encoding="utf-8", newline="")

    warning = check_security(cfg)

    print("=" * 64)
    print("  音频转 MP3 · Web 版服务端")
    print("=" * 64)

    if codecpod is None:
        print("[错误] 无法加载 codecpod 音频引擎：", _IMPORT_ERROR)
        print("       请先准备依赖：python build.py --env-only")
        print("       或：.venv\\Scripts\\python.exe -m pip install -r requirements.txt")
        return 1

    if not os.path.isdir(WEB_DIR):
        print(f"[错误] 未找到前端目录：{WEB_DIR}")
        return 1

    port = find_free_port(cfg.host, cfg.port)
    cfg.port = port

    Handler.cfg = cfg
    Handler.gate = threading.BoundedSemaphore(cfg.concurrency)
    Handler.timeout = cfg.timeout                # 单请求 socket 读超时

    httpd = DualStackHTTPServer(
        (cfg.host, port), Handler,
        family=cfg.family, dual_stack=cfg.dual_stack,
    )
    url = f"http://{host_for_url(cfg.url_host)}:{port}/" + (f"?token={cfg.token}" if cfg.token else "")

    stack_note = cfg.family_name
    if getattr(httpd, "dual_stack_enabled", False):
        stack_note += " + IPv4（双栈）"

    print(f"[信息] 前端目录：{WEB_DIR}")
    print(f"[信息] 监听地址：{cfg.display_host}:{port}"
          f"（{stack_note}，线程并发上限 {cfg.concurrency}）")
    print(f"[信息] 单文件上限：{cfg.max_file_mb} MB；PCM 上限：{cfg.max_pcm_mb} MB")
    print(f"[信息] 访问令牌：{'已启用' if cfg.token else '未启用（仅建议本机使用）'}")
    print(f"[信息] 转换全程内存化：不写入任何磁盘临时文件")
    print()
    print(f"  访问地址：{url}")
    print("  停止服务：在本窗口按 Ctrl+C，或直接关闭本窗口。")
    if cfg.is_unspecified and not cfg.is_loopback:
        global6 = local_ipv6_addresses()
        if global6:
            print()
            print("  外网 IPv6 访问（请确认已放行防火墙，见 部署说明.md）：")
            for addr in global6:
                token_suffix = f"?token={cfg.token}" if cfg.token else ""
                print(f"    http://[{addr}]:{port}/{token_suffix}")
    if warning:
        print()
        print(f"  [安全] {warning}")
    if not cfg.is_loopback:
        print()
        print("  [安全] 已对外监听，请确保：")
        print("         - 使用 --token 或反向代理做访问控制；")
        print("         - 必要时用 --allowed-origins 限制来源；")
        print("         - 仅在内网/受信网络暴露，不要直接对公网开放。")
        if cfg.family_name == "IPv6" or getattr(httpd, "dual_stack_enabled", False):
            print("         - IPv6 公网访问需在 Windows 防火墙放行入站端口，")
            print("           并建议限制来源 IPv6 前缀（见 部署说明.md / scripts/）。")
        print("         - 对外长期服务推荐：反向代理 + HTTPS。")
    print("=" * 64)

    log(f"启动服务 {cfg.display_host}:{port} family={cfg.family_name} "
        f"dual_stack={cfg.dual_stack} session={SESSION_ID} "
        f"token={'on' if cfg.token else 'off'}")

    if cfg.open_browser:
        threading.Timer(0.6, lambda: webbrowser.open(url)).start()

    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n[信息] 正在停止服务……")
    finally:
        httpd.shutdown()
        httpd.server_close()
        if _LOG is not None:
            _LOG.close()
        print("[信息] 服务已停止。")
    return 0


if __name__ == "__main__":
    sys.exit(main())

# -*- coding: utf-8 -*-
"""
端到端验证脚本（全部产物只写入本工作区）
========================================

覆盖：
    1. 纯代码链路：内存化转换 wav/flac -> mp3，坏文件抛错
    2. HTTP：/api/health、/api/formats、/static、路径穿越
    3. HTTP：/api/convert 多比特率、中文名、坏文件 400、非法比特率 400
    4. HTTP：/api/zip 内存打包、中文条目、zip 路径穿越防护
    5. HTTP：请求体上限 400
    6. HTTP：并发闸门 503
    7. HTTP：--token 鉴权 401 / ?token= Cookie 放行 / 请求头放行
    8. 启动安全闸门：非回环无令牌拒绝启动；--allow-insecure 放行
    9. 内存化审计：把 TMP/TEMP/TMPDIR 指向工作区内空目录，转换后必须仍为空
   10. 地址判定单元测试（:: / [::1] / 0.0.0.0 / localhost / 127.0.0.1）
   11. IPv6 绑定：--host ::1 / --host [::1] / 双栈 --host :: 、[::1] URL 访问、
       IPv6 上完成一次真实转换、IPv6 非回环无令牌拒绝启动
       （本机不支持 IPv6 绑定时会明确标记为 SKIP，而不是记为通过）

用法：
    .venv\\Scripts\\python.exe tests\\run_tests.py
"""

import io
import itertools
import json
import os
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import wave

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PY = os.path.join(ROOT, ".venv", "Scripts", "python.exe")
OUT_DIR = os.path.join(ROOT, "tests", "out")
AUDIT_TMP = os.path.join(ROOT, ".tmp", "audit")

_results = []


def record(name, ok, detail=""):
    _results.append({"name": name, "ok": bool(ok), "detail": detail})
    flag = "PASS" if ok else "FAIL"
    print(f"  [{flag}] {name}" + (f"  -> {detail}" if detail else ""), flush=True)


def record_skip(name, reason):
    """无法验证的项：显式标记为 SKIP，不计入通过。"""
    _results.append({"name": name, "ok": None, "skipped": True, "detail": reason})
    print(f"  [SKIP] {name}  -> {reason}", flush=True)


# --------------------------------------------------------------------------
# 测试音频生成（内存中）
# --------------------------------------------------------------------------
def make_wav(seconds, sr=44100, channels=2):
    import numpy as np
    n = int(sr * seconds)
    t = np.arange(n) / sr
    mono = (0.3 * 32767 * np.sin(2 * np.pi * 440.0 * t)).astype("<i2")
    if channels == 1:
        inter = mono
    else:
        inter = np.column_stack([mono] * channels).reshape(-1)
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(channels)
        w.setsampwidth(2)
        w.setframerate(sr)
        w.writeframes(inter.tobytes())
    return buf.getvalue()


def make_flac(seconds, sr=44100):
    import codecpod
    import numpy as np
    n = int(sr * seconds)
    t = np.arange(n) / sr
    mono = (0.3 * np.sin(2 * np.pi * 330.0 * t)).astype("float32")
    return codecpod.save_bytes(mono, sr, codec=codecpod.Flac())


def is_mp3(data):
    return len(data) > 4 and (data[:3] == b"ID3" or (data[0] == 0xFF and (data[1] & 0xE0) == 0xE0))


# --------------------------------------------------------------------------
# HTTP 辅助
# --------------------------------------------------------------------------
def free_port(host="127.0.0.1"):
    """按地址族找空闲端口；host 可为 IPv6 地址（如 ::1 / ::）。"""
    family = socket.AF_INET6 if ":" in host else socket.AF_INET
    s = socket.socket(family, socket.SOCK_STREAM)
    try:
        if family == socket.AF_INET6 and host == "::":
            try:                                  # 与服务端一致，避免占用判断失真
                s.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 0)
            except OSError:
                pass
        s.bind((host, 0))
        return s.getsockname()[1]
    finally:
        s.close()


def ipv6_bindable():
    """本机是否可绑定 IPv6 回环地址（用于决定跑还是跳过 IPv6 测试）。"""
    if not socket.has_ipv6:
        return False, "socket.has_ipv6 为 False"
    try:
        s = socket.socket(socket.AF_INET6, socket.SOCK_STREAM)
    except OSError as exc:
        return False, f"无法创建 AF_INET6 套接字：{exc}"
    try:
        s.bind(("::1", 0))
        return True, ""
    except OSError as exc:
        return False, f"绑定 ::1 失败：{exc}"
    finally:
        s.close()


def server_env():
    env = dict(os.environ)
    env["PYTHONUTF8"] = "1"
    env["TMP"] = AUDIT_TMP
    env["TEMP"] = AUDIT_TMP
    env["TMPDIR"] = AUDIT_TMP
    return env


_server_seq = itertools.count(1)


def start_server(extra_args, port):
    n = next(_server_seq)
    logpath = os.path.join(OUT_DIR, f"server_{n}_{port}.log")
    logf = open(logpath, "wb")
    proc = subprocess.Popen(
        [PY, os.path.join(ROOT, "web_server.py"), "--no-browser",
         "--port", str(port), *extra_args],
        cwd=ROOT, env=server_env(), stdout=logf, stderr=subprocess.STDOUT,
    )
    return proc, logpath, logf


def wait_health(port, host="127.0.0.1", token=None, timeout=25):
    """host 需为 URL 中的形式：IPv6 要带方括号，例如 [::1]。"""
    url = f"http://{host}:{port}/api/health"
    if token:
        url += f"?token={token}"
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=2) as r:
                return json.loads(r.read())
        except Exception:
            time.sleep(0.3)
    return None


def http(method, url, body=None, headers=None):
    req = urllib.request.Request(url, data=body, method=method, headers=headers or {})
    try:
        with urllib.request.urlopen(req, timeout=120) as r:
            return r.status, dict(r.headers), r.read()
    except urllib.error.HTTPError as e:
        return e.code, dict(e.headers), e.read()


def post_convert(port, wav, name, bitrate=192, headers=None):
    q = urllib.parse.urlencode({"bitrate": bitrate, "name": name})
    return http("POST", f"http://127.0.0.1:{port}/api/convert?{q}", body=wav, headers=headers)


def build_frames(items):
    import struct
    out = bytearray()
    for name, blob in items:
        nb = name.encode("utf-8")
        out += struct.pack(">I", len(nb)) + nb + struct.pack(">I", len(blob)) + blob
    return bytes(out)


# --------------------------------------------------------------------------
# 测试
# --------------------------------------------------------------------------
def test_code_path():
    print("\n[1] 纯代码链路（内存化）")
    import codecpod
    sys.path.insert(0, ROOT)
    from web_server import convert_bytes, pcm_bytes_of

    wav = make_wav(1.0)
    mp3 = convert_bytes(wav, 192000)
    record("convert_bytes(wav->mp3)", is_mp3(mp3), f"{len(mp3)} bytes")

    flac = make_flac(1.0)
    mp3b = convert_bytes(flac, 320000)
    record("convert_bytes(flac->mp3)", is_mp3(mp3b), f"{len(mp3b)} bytes")

    samples, sr = codecpod.load(wav)
    pcm = pcm_bytes_of(samples)
    record("pcm_bytes_of 可用", pcm > 0, f"{pcm} bytes @ {sr}Hz")

    try:
        convert_bytes(b"this is not audio at all", 192000)
        record("坏文件抛异常", False, "未抛异常")
    except BaseException as e:
        record("坏文件抛异常", True, type(e).__name__)


def test_main_http():
    print("\n[2-6] HTTP 主流程")
    port = free_port()
    proc, logpath, logf = start_server(["--max-file-mb", "2"], port)
    try:
        h = wait_health(port)
        if not h:
            record("服务启动", False, "健康检查超时")
            return
        record("GET /api/health", h.get("status") == "ok",
               f"engine={h.get('engine')} python={h.get('python')}")

        st, hd, body = http("GET", f"http://127.0.0.1:{port}/api/formats")
        fm = json.loads(body)
        record("GET /api/formats", st == 200 and len(fm["extensions"]) == 21,
               f"{len(fm['extensions'])} 格式 / {fm['bitrates']}")

        st, hd, body = http("GET", f"http://127.0.0.1:{port}/")
        record("GET / 静态页", st == 200 and b"<!doctype html>" in body.lower(),
               f"{len(body)} bytes")

        st, hd, body = http("HEAD", f"http://127.0.0.1:{port}/api/health")
        record("HEAD 支持", st == 200 and body == b"", f"status={st} body={len(body)}")

        st, hd, body = http("GET", f"http://127.0.0.1:{port}/favicon.ico")
        record("favicon 返回 204（无 404 噪声）", st == 204, f"status={st}")

        # 路径穿越
        st, hd, body = http("GET", f"http://127.0.0.1:{port}/../web_server.py")
        record("静态路径穿越被拦截", st in (403, 404), f"status={st}")
        st, hd, body = http("GET", f"http://127.0.0.1:{port}/%2e%2e%2fweb_server.py")
        record("编码路径穿越被拦截", st in (403, 404), f"status={st}")

        # 多比特率 + 中文名
        wav = make_wav(1.0)
        for br in (128, 192, 256, 320):
            st, hd, body = post_convert(port, wav, "测试 音频#1.wav", br)
            outname = urllib.parse.unquote(hd.get("X-Out-Name", ""))
            ok = st == 200 and is_mp3(body) and outname == "测试 音频#1.mp3"
            record(f"convert {br}kbps + 中文名", ok, f"status={st} name={outname} {len(body)}B")

        # 非法比特率 / 空体 / 坏文件
        st, hd, body = post_convert(port, wav, "a.wav", 999)
        record("非法比特率 400", st == 400 and b"error" in body, body[:80].decode("utf-8", "replace"))

        st, hd, body = http("POST", f"http://127.0.0.1:{port}/api/convert?bitrate=192&name=a.wav", body=b"")
        record("空请求体 400", st == 400, body[:60].decode("utf-8", "replace"))

        st, hd, body = post_convert(port, b"not audio bytes", "broken.wav")
        record("坏文件 400 + 可读错误", st == 400 and b"error" in body,
               body[:90].decode("utf-8", "replace"))

        # 请求体上限（服务器 --max-file-mb 2）
        big = b"\x00" * (3 * 1024 * 1024)
        st, hd, body = post_convert(port, big, "big.wav")
        record("超大请求体 400", st == 400 and b"\xe4\xb8\x8a\xe9\x99\x90" in body,
               body[:80].decode("utf-8", "replace"))

        # 较大文件（约 3.5MB > 2MB 上限，改用 30s 单声道 ~2.6MB；这里用 20s 立体声会被拦）
        # 使用 2s 立体声 + 320kbps 验证正常路径已足够；大文件走默认上限服务器（见 test_large）

        # ZIP 内存打包 + 中文条目 + 路径穿越
        b1 = convert_bytes_local(wav, 192000)
        b2 = convert_bytes_local(wav, 128000)
        frames = build_frames([("中文 歌曲.mp3", b1), ("../../evil.mp3", b2)])
        st, hd, body = http("POST", f"http://127.0.0.1:{port}/api/zip", body=frames)
        if st == 200:
            import zipfile
            with zipfile.ZipFile(io.BytesIO(body)) as zf:
                names = zf.namelist()
            ok = "中文 歌曲.mp3" in names and "evil.mp3" in names and not any(
                "/" in n or ".." in n for n in names)
            record("ZIP 内存打包 + 中文名 + 防穿越", ok, f"entries={names}")
        else:
            record("ZIP 内存打包", False, f"status={st} {body[:80]!r}")

        # 并发闸门：--concurrency 1，4 个并发大文件
        port2 = free_port()
        proc2, log2, logf2 = start_server(["--concurrency", "1", "--max-file-mb", "64"], port2)
        try:
            if wait_health(port2):
                big_wav = make_wav(20.0)
                codes = []
                lock = threading.Lock()

                def worker():
                    st, hd, body = post_convert(port2, big_wav, "big.wav", 320)
                    with lock:
                        codes.append(st)

                ts = [threading.Thread(target=worker) for _ in range(4)]
                for t in ts:
                    t.start()
                for t in ts:
                    t.join()
                n503 = codes.count(503)
                record("并发闸门返回 503", n503 >= 1,
                       f"statuses={sorted(codes)}（503×{n503}）")
            else:
                record("并发测试服务启动", False, "超时")
        finally:
            proc2.terminate()
            logf2.close()

        # 审计：TMP 指向的空目录必须仍为空
        left = sorted(os.listdir(AUDIT_TMP))
        record("转换期间工作区 TMP 目录为空（无落盘）", len(left) == 0, f"残留={left}")
    finally:
        proc.terminate()
        logf.close()


def convert_bytes_local(data, bitrate):
    sys.path.insert(0, ROOT)
    from web_server import convert_bytes
    return convert_bytes(data, bitrate)


def test_auth():
    print("\n[7] 令牌鉴权")
    port = free_port()
    proc, logpath, logf = start_server(["--token", "secret123"], port)
    try:
        time.sleep(1.5)
        st, hd, body = http("GET", f"http://127.0.0.1:{port}/api/health")
        record("无令牌访问 API -> 401", st == 401, f"status={st}")

        st, hd, body = http("GET", f"http://127.0.0.1:{port}/")
        record("无令牌访问页面 -> 401", st == 401, f"status={st}")

        st, hd, body = http("GET", f"http://127.0.0.1:{port}/?token=secret123")
        setcookie = hd.get("Set-Cookie", "")
        record("?token= 访问页面 -> 200 + Cookie",
               st == 200 and "a2m_token" in setcookie, f"status={st} cookie={setcookie[:40]}")

        st, hd, body = http("GET", f"http://127.0.0.1:{port}/api/health",
                            headers={"X-Auth-Token": "secret123"})
        record("请求头令牌访问 API -> 200", st == 200, f"status={st}")

        st, hd, body = http("GET", f"http://127.0.0.1:{port}/api/health", headers={"Cookie": "a2m_token=secret123"})
        record("Cookie 令牌访问 API -> 200", st == 200, f"status={st}")
    finally:
        proc.terminate()
        logf.close()


def test_security_gate():
    print("\n[8] 启动安全闸门")
    port = free_port()
    p = subprocess.run(
        [PY, os.path.join(ROOT, "web_server.py"), "--no-browser",
         "--host", "0.0.0.0", "--port", str(port)],
        cwd=ROOT, env=server_env(), capture_output=True, timeout=30,
    )
    out = (p.stdout + p.stderr).decode("utf-8", "replace")
    record("非回环无令牌拒绝启动", p.returncode != 0 and "拒绝启动" in out,
           f"exit={p.returncode}")

    port = free_port()
    proc, logpath, logf = start_server(["--host", "0.0.0.0", "--allow-insecure"], port)
    try:
        h = wait_health(port)
        record("--allow-insecure 可启动", bool(h), f"health={bool(h)}")
    finally:
        proc.terminate()
        logf.close()


def test_origin_allowlist():
    print("\n[9] Origin 白名单")
    port = free_port()
    allowed = f"http://127.0.0.1:{port}"
    proc, logpath, logf = start_server(["--allowed-origins", allowed], port)
    try:
        if not wait_health(port):
            record("Origin 测试服务启动", False, "超时")
            return
        wav = make_wav(1.0)
        st, hd, body = post_convert(port, wav, "a.wav", 192, headers={"Origin": allowed})
        record("白名单内 Origin 放行", st == 200, f"status={st}")

        st, hd, body = post_convert(port, wav, "a.wav", 192,
                                    headers={"Origin": "http://evil.example.com"})
        record("白名单外 Origin 403", st == 403 and b"Origin" in body, f"status={st}")
    finally:
        proc.terminate()
        logf.close()


def test_unit_addresses():
    print("\n[10] 地址判定单元测试")
    sys.path.insert(0, ROOT)
    import web_server as w

    cases = [
        # host,          回环,  未指定, IPv6,  URL 形式
        ("::",           False, True,  True,  "[::]"),
        ("[::1]",        True,  False, True,  "[::1]"),
        ("0.0.0.0",      False, True,  False, "0.0.0.0"),
        ("localhost",    True,  False, False, "localhost"),
        ("127.0.0.1",    True,  False, False, "127.0.0.1"),
        ("2001:db8:1234:5678::1", False, False, True, "[2001:db8:1234:5678::1]"),
    ]
    for host, loop, unspec, v6, url in cases:
        got = (w.host_is_loopback(host), w.host_is_unspecified(host),
               w.is_ipv6_host(host), w.host_for_url(host))
        want = (loop, unspec, v6, url)
        record(f"地址判定 {host}", got == want,
               f"loopback={got[0]} unspec={got[1]} v6={got[2]} url={got[3]}")

    record("空 host 视为非回环（更安全）", w.host_is_loopback("") is False, "")

    c = w.Config(); c.host = "::"
    record("Config(::).dual_stack = True", c.dual_stack is True, "")
    c2 = w.Config(); c2.host = "::1"
    record("Config(::1).dual_stack = False", c2.dual_stack is False, "")
    c3 = w.Config(); c3.host = "::"
    record("Config(::).url_host 回退到 ::1", c3.url_host == "::1", c3.url_host)


def test_ipv6_bind():
    print("\n[11] IPv6 绑定与访问")
    ok, why = ipv6_bindable()
    if not ok:
        record_skip("IPv6 绑定（--host ::1）", f"本机不支持绑定 IPv6：{why}")
        record_skip("IPv6 双栈（--host ::）", f"本机不支持绑定 IPv6：{why}")
        record_skip("IPv6 非回环无令牌拒绝启动", f"本机不支持绑定 IPv6：{why}")
        return

    # ---- 11.1 仅 IPv6 回环：--host ::1 -------------------------------------
    port = free_port("::1")
    proc, logpath, logf = start_server(["--host", "::1"], port)
    try:
        h = wait_health(port, host="[::1]")
        if not h:
            record("IPv6 回环启动 + [::1] 健康检查", False, "健康检查超时")
        else:
            okh = (h.get("bind_family") == "IPv6" and h.get("is_loopback") is True
                   and h.get("auth_required") is False and h.get("bind_port") == port)
            record("IPv6 回环启动 + [::1] 健康检查", okh,
                   f"family={h.get('bind_family')} is_loopback={h.get('is_loopback')} "
                   f"listen={h.get('listen')} bind_port={h.get('bind_port')}")
            record("IPv6 local_url 使用方括号", str(h.get("local_url", "")).startswith("http://[::1]:"),
                   h.get("local_url"))

            wav = make_wav(1.0)
            st, hd, body = http(
                "POST",
                f"http://[::1]:{port}/api/convert?bitrate=192&name=%E4%B8%AD%E6%96%87%E6%B5%8B%E8%AF%95.wav",
                body=wav)
            name = urllib.parse.unquote(hd.get("X-Out-Name", ""))
            record("IPv6 上完成一次真实转换（中文名）",
                   st == 200 and is_mp3(body) and name == "中文测试.mp3",
                   f"status={st} bytes={len(body)} name={name}")
    finally:
        proc.terminate()
        logf.close()

    # ---- 11.2 双栈：--host :: （IPv6 + IPv4-mapped）------------------------
    port = free_port("::")
    proc, logpath, logf = start_server(["--host", "::", "--token", "ipv6tok"], port)
    try:
        h6 = wait_health(port, host="[::1]", token="ipv6tok")
        h4 = wait_health(port, host="127.0.0.1", token="ipv6tok")
        record("双栈 :: 经 IPv6 可达", bool(h6),
               f"dual_stack={h6.get('dual_stack') if h6 else None}")
        record("双栈 :: 经 IPv4(mapped) 可达", bool(h4),
               "IPv4-mapped 客户端连接成功" if h4 else "IPv4 连接失败")
        record("双栈 :: 健康检查 is_loopback=False / auth_required=True",
               bool(h6) and h6.get("is_loopback") is False and h6.get("auth_required") is True,
               f"is_loopback={h6.get('is_loopback') if h6 else None} "
               f"auth_required={h6.get('auth_required') if h6 else None}")

        st, hd, body = http("GET", f"http://[::1]:{port}/api/health")
        record("IPv6 无令牌访问 API -> 401", st == 401, f"status={st}")

        st, hd, body = http("GET", f"http://[::1]:{port}/?token=ipv6tok")
        record("IPv6 ?token= 访问页面 -> 200 + Cookie",
               st == 200 and "a2m_token" in hd.get("Set-Cookie", ""),
               f"status={st} cookie={hd.get('Set-Cookie','')[:36]}")
    finally:
        proc.terminate()
        logf.close()

    # ---- 11.3 方括号写法：--host [::1] -------------------------------------
    port = free_port("::1")
    proc, logpath, logf = start_server(["--host", "[::1]"], port)
    try:
        h = wait_health(port, host="[::1]")
        record("--host [::1] 方括号写法可用",
               bool(h) and h.get("bind_host") == "::1",
               f"bind_host={h.get('bind_host') if h else None}")
    finally:
        proc.terminate()
        logf.close()

    # ---- 11.4 IPv6 非回环无令牌 -> 拒绝启动 --------------------------------
    port = free_port("::")
    p = subprocess.run(
        [PY, os.path.join(ROOT, "web_server.py"), "--no-browser",
         "--host", "::", "--port", str(port)],
        cwd=ROOT, env=server_env(), capture_output=True, timeout=30,
    )
    out = (p.stdout + p.stderr).decode("utf-8", "replace")
    record("IPv6 非回环无令牌拒绝启动",
           p.returncode != 0 and "拒绝启动" in out, f"exit={p.returncode}")


def main():
    os.makedirs(OUT_DIR, exist_ok=True)
    os.makedirs(AUDIT_TMP, exist_ok=True)
    # 审计目录必须初始为空
    for f in os.listdir(AUDIT_TMP):
        os.remove(os.path.join(AUDIT_TMP, f))

    print("=" * 60)
    print("  AudioToMP3 Web 端到端验证")
    print("=" * 60)

    for group in (test_code_path, test_main_http, test_auth, test_security_gate,
                  test_origin_allowlist, test_unit_addresses, test_ipv6_bind):
        try:
            group()
        except Exception as exc:  # 单个分组异常不阻断其余验证
            record(f"{group.__name__} 分组异常", False, f"{type(exc).__name__}: {exc}")

    passed = sum(1 for r in _results if r["ok"] is True)
    skipped = sum(1 for r in _results if r.get("skipped"))
    failed = [r for r in _results if r["ok"] is False]
    executed = len(_results) - skipped

    print("\n" + "=" * 60)
    print(f"  结果：{passed}/{executed} 通过，{skipped} 跳过，{len(failed)} 失败")
    for r in _results:
        if r.get("skipped"):
            print(f"    SKIP: {r['name']} -> {r['detail']}")
    for r in failed:
        print(f"    FAIL: {r['name']} -> {r['detail']}")
    print("=" * 60)

    with open(os.path.join(OUT_DIR, "results.json"), "w", encoding="utf-8") as fh:
        json.dump(_results, fh, ensure_ascii=False, indent=2)
    return 0 if not failed else 1


if __name__ == "__main__":
    sys.exit(main())

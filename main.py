# -*- coding: utf-8 -*-
"""
音频转 MP3 便携工具
====================

功能：
  - tkinter 图形界面，无需任何额外的 GUI 第三方库
  - 批量添加 .wav/.flac/.m4a/.aac/.ogg/.wma/.aiff 等常见音频格式
  - 统一转换为 MP3（默认 192 kbps，保持源文件采样率）
  - 可选把本次输出的所有 .mp3 打包成一个 zip

技术要点：
  - 编解码核心使用 codecpod：它在构建时把 FFmpeg 静态链接进扩展模块，
    运行时零外部共享库依赖，因此不依赖目标机器上的 FFmpeg、VC++ 运行库。
  - 转换放到后台线程执行，主线程只负责刷新界面，窗口不会“未响应”。
  - 打包为 PyInstaller --onefile 单文件 exe，资源路径通过 sys._MEIPASS 解析。
"""

import datetime
import os
import queue
import sys
import threading
import traceback
import zipfile
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

# ---------------------------------------------------------------------------
# 编解码引擎：codecpod（静态链接 FFmpeg）。导入失败时不直接崩溃，而是给出中文提示。
# ---------------------------------------------------------------------------
try:
    import codecpod

    _IMPORT_ERROR = None
except Exception as _exc:  # pragma: no cover - 仅在打包异常时触发
    codecpod = None
    _IMPORT_ERROR = _exc


# 支持作为输入的文件扩展名（codecpod 静态 FFmpeg 可解码的常见格式）
SUPPORTED_EXTS = (
    ".wav", ".flac", ".m4a", ".aac", ".ogg", ".oga", ".wma", ".aiff", ".aif",
    ".mp3", ".ape", ".opus", ".wv", ".ac3", ".alac", ".mka", ".mp2", ".mp1",
    ".amr", ".caf", ".au",
)

# MP3 可选比特率（kbps），默认 192
BITRATE_CHOICES = ("128", "192", "256", "320")
DEFAULT_BITRATE = "192"


def resource_path(rel_path):
    """
    解析随程序分发的资源文件路径。

    PyInstaller --onefile 运行时会把资源解压到临时目录 sys._MEIPASS，
    因此这里必须优先使用该目录，绝对不能写死绝对路径。
    """
    base = getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(base, rel_path)


def default_output_dir():
    """默认输出目录：优先桌面，其次用户主目录。"""
    home = os.path.expanduser("~")
    desktop = os.path.join(home, "Desktop")
    return desktop if os.path.isdir(desktop) else home


def human_size(num_bytes):
    """把字节数格式化为易读的大小。"""
    size = float(num_bytes)
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024.0 or unit == "GB":
            return f"{size:.1f} {unit}" if unit != "B" else f"{int(size)} B"
        size /= 1024.0
    return f"{size:.1f} GB"


def _unique_output_path(outdir, stem, used_lower):
    """在输出目录内生成不冲突的 .mp3 路径（同一批次内去重，避免同名覆盖）。"""
    name = stem + ".mp3"
    i = 1
    while name.lower() in used_lower:
        name = f"{stem}_{i}.mp3"
        i += 1
    used_lower.add(name.lower())
    return os.path.join(outdir, name)


def run_conversion(files, outdir, bit_rate, make_zip, on_event=None):
    """
    执行批量转换（纯逻辑，不涉及界面，便于单独测试）。

    参数：
      files      : 输入文件绝对路径列表
      outdir     : 输出目录
      bit_rate   : MP3 比特率，单位 bit/s（例如 192000）
      make_zip   : 是否把成功的 mp3 打包为 zip
      on_event   : 回调 on_event(kind, **data)，kind 取值 status/log/progress

    返回 (成功列表, 失败列表, zip 路径或 None)：
      成功列表元素为 (源路径, 目标 mp3 路径)
      失败列表元素为 (源路径, 错误说明)
    """
    def emit(kind, **data):
        if on_event is not None:
            on_event(kind, **data)

    os.makedirs(outdir, exist_ok=True)
    used_lower = set()
    succeeded = []
    failed = []
    total = len(files)

    for idx, src in enumerate(files, 1):
        emit("status", text=f"正在处理 {idx}/{total}：{os.path.basename(src)}")
        emit("progress", done=idx - 1, total=total)
        try:
            if not os.path.isfile(src):
                raise FileNotFoundError("文件不存在或已被移动")

            stem = os.path.splitext(os.path.basename(src))[0]
            dst = _unique_output_path(outdir, stem, used_lower)

            # 关键：解码为波形 + 采样率（默认保持源采样率），再交给 MP3(LAME) 编码器。
            waveform, sample_rate = codecpod.load(src)
            codecpod.save(
                dst,
                waveform,
                sample_rate,
                codec=codecpod.Mp3(bit_rate=bit_rate),
            )

            succeeded.append((src, dst))
            emit("log", text=f"[成功] {os.path.basename(src)}  →  {os.path.basename(dst)}")
        except (KeyboardInterrupt, SystemExit):
            raise
        except BaseException as exc:
            # 单个文件失败不影响整批，记录原因后继续。
            # 注意：codecpod 是 Rust 原生扩展，解码失败时可能抛出 PanicException，
            # 它继承自 BaseException 而不是 Exception，必须一并捕获，否则会直接崩溃。
            failed.append((src, f"{type(exc).__name__}: {exc}"))
            emit("log", text=f"[失败] {os.path.basename(src)}：{exc}")
        emit("progress", done=idx, total=total)

    zip_path = None
    if make_zip and succeeded:
        emit("status", text="正在打包 zip……")
        stamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
        zip_path = os.path.join(outdir, f"mp3_output_{stamp}.zip")
        with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
            for _src, dst in succeeded:
                zf.write(dst, arcname=os.path.basename(dst))
        emit("log", text=f"[打包] {os.path.basename(zip_path)}（{len(succeeded)} 个文件）")

    return succeeded, failed, zip_path


class AudioToMp3App:
    """主窗口。"""

    def __init__(self, root):
        self.root = root
        self.files = []                 # 待转换文件绝对路径列表
        self.busy = False               # 是否正在转换
        self.msg_queue = queue.Queue()  # 后台线程 -> 主线程的事件队列

        root.title("音频转 MP3 便携工具")
        root.geometry("760x560")
        root.minsize(680, 480)

        self._set_window_icon()
        self._build_ui()

        if _IMPORT_ERROR is not None:
            self.convert_btn.config(state="disabled")
            self.root.after(200, self._report_engine_error)

        self._poll_queue()

    def _report_engine_error(self):
        """音频引擎加载失败时，给出可操作的排查提示，而不是只抛一句 ModuleNotFoundError。"""
        frozen = bool(getattr(sys, "frozen", False))
        if frozen:
            hint = (
                "程序每次启动都会先把内置文件解压到临时目录再加载。\n"
                "若反复出现此提示，通常是杀毒软件拦截了其中的扩展模块，\n"
                "请把本程序加入白名单后重试。"
            )
        else:
            hint = (
                "当前是以「源码」方式运行，而运行它的 Python 没有安装 codecpod。\n\n"
                "请改用项目自带的虚拟环境启动（虚拟环境里已装好依赖）：\n"
                "    双击 run.bat\n"
                "或执行：.venv\\Scripts\\python.exe main.py\n\n"
                "如果还没有 .venv，请先双击 build.bat 完成环境准备。\n"
                "（注意：不要直接双击 main.py，那样会用系统 Python 运行。）"
            )
        detail = (
            f"无法加载 codecpod 音频引擎：\n{_IMPORT_ERROR}\n\n"
            f"运行方式：{'打包 exe（frozen）' if frozen else '源码运行'}\n"
            f"当前解释器：\n{sys.executable}\n\n{hint}"
        )
        self._log_append("[错误] " + detail.replace("\n", "\n         "))
        messagebox.showerror("音频引擎加载失败", detail)

    # ------------------------------------------------------------------ 界面
    def _set_window_icon(self):
        try:
            ico = resource_path(os.path.join("assets", "app.ico"))
            if os.path.isfile(ico):
                self.root.iconbitmap(ico)
        except Exception:
            pass

    def _build_ui(self):
        pad = {"padx": 8, "pady": 4}

        # ---- 文件列表 ----
        list_frame = ttk.LabelFrame(self.root, text="待转换文件列表")
        list_frame.pack(fill="both", expand=True, padx=10, pady=(10, 4))

        columns = ("name", "fmt", "size")
        self.tree = ttk.Treeview(list_frame, columns=columns, show="headings", selectmode="extended")
        self.tree.heading("name", text="文件名")
        self.tree.heading("fmt", text="格式")
        self.tree.heading("size", text="大小")
        self.tree.column("name", width=430, anchor="w")
        self.tree.column("fmt", width=90, anchor="center")
        self.tree.column("size", width=110, anchor="e")

        vsb = ttk.Scrollbar(list_frame, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=vsb.set)
        self.tree.grid(row=0, column=0, sticky="nsew", padx=(6, 0), pady=6)
        vsb.grid(row=0, column=1, sticky="ns", padx=(0, 6), pady=6)
        list_frame.rowconfigure(0, weight=1)
        list_frame.columnconfigure(0, weight=1)

        btn_row = ttk.Frame(list_frame)
        btn_row.grid(row=1, column=0, columnspan=2, sticky="ew", padx=6, pady=(0, 6))
        self.add_btn = ttk.Button(btn_row, text="添加文件", command=self.add_files)
        self.add_btn.pack(side="left")
        ttk.Button(btn_row, text="移除选中", command=self.remove_selected).pack(side="left", padx=6)
        ttk.Button(btn_row, text="清空列表", command=self.clear_files).pack(side="left")
        self.count_var = tk.StringVar(value="共 0 个文件")
        ttk.Label(btn_row, textvariable=self.count_var).pack(side="right")

        # ---- 输出与参数 ----
        opt_frame = ttk.LabelFrame(self.root, text="输出与参数")
        opt_frame.pack(fill="x", padx=10, pady=4)
        opt_frame.columnconfigure(1, weight=1)

        ttk.Label(opt_frame, text="输出目录：").grid(row=0, column=0, sticky="w", **pad)
        self.outdir_var = tk.StringVar(value=default_output_dir())
        ttk.Entry(opt_frame, textvariable=self.outdir_var).grid(row=0, column=1, sticky="ew", **pad)
        ttk.Button(opt_frame, text="浏览…", command=self.choose_outdir).grid(row=0, column=2, sticky="e", **pad)

        ttk.Label(opt_frame, text="MP3 比特率：").grid(row=1, column=0, sticky="w", **pad)
        self.bitrate_var = tk.StringVar(value=DEFAULT_BITRATE)
        combo = ttk.Combobox(
            opt_frame, textvariable=self.bitrate_var, values=BITRATE_CHOICES,
            width=8, state="readonly",
        )
        combo.grid(row=1, column=1, sticky="w", **pad)
        ttk.Label(opt_frame, text="kbps（采样率保持源文件）").grid(row=1, column=2, sticky="w", **pad)

        self.zip_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(opt_frame, text="转换完成后打包为 ZIP", variable=self.zip_var)\
            .grid(row=2, column=0, columnspan=3, sticky="w", **pad)

        # ---- 转换按钮 ----
        self.convert_btn = ttk.Button(self.root, text="转换为 MP3", command=self.start_convert)
        self.convert_btn.pack(fill="x", padx=10, pady=(6, 2))

        # ---- 进度 ----
        prog_frame = ttk.Frame(self.root)
        prog_frame.pack(fill="x", padx=10, pady=2)
        self.progress = ttk.Progressbar(prog_frame, mode="determinate", maximum=1, value=0)
        self.progress.pack(fill="x")
        self.status_var = tk.StringVar(value="就绪")
        ttk.Label(prog_frame, textvariable=self.status_var, anchor="w").pack(fill="x")

        # ---- 日志 ----
        log_frame = ttk.LabelFrame(self.root, text="日志")
        log_frame.pack(fill="both", expand=True, padx=10, pady=(4, 10))
        self.log_text = tk.Text(log_frame, height=8, wrap="word", state="disabled")
        log_vsb = ttk.Scrollbar(log_frame, orient="vertical", command=self.log_text.yview)
        self.log_text.configure(yscrollcommand=log_vsb.set)
        self.log_text.pack(side="left", fill="both", expand=True, padx=(6, 0), pady=6)
        log_vsb.pack(side="right", fill="y", padx=(0, 6), pady=6)

    # -------------------------------------------------------------- 文件操作
    def add_files(self):
        patterns = " ".join("*" + ext for ext in SUPPORTED_EXTS)
        paths = filedialog.askopenfilenames(
            title="选择音频文件",
            filetypes=[("音频文件", patterns), ("所有文件", "*.*")],
        )
        added = 0
        for path in paths:
            path = os.path.abspath(path)
            if path in self.files:
                continue
            self.files.append(path)
            self._insert_row(path)
            added += 1
        if added:
            self.count_var.set(f"共 {len(self.files)} 个文件")

    def _insert_row(self, path):
        name = os.path.basename(path)
        ext = os.path.splitext(name)[1].lstrip(".").upper() or "未知"
        try:
            size = human_size(os.path.getsize(path))
        except OSError:
            size = "-"
        self.tree.insert("", "end", iid=path, values=(name, ext, size))

    def remove_selected(self):
        for iid in self.tree.selection():
            if iid in self.files:
                self.files.remove(iid)
            self.tree.delete(iid)
        self.count_var.set(f"共 {len(self.files)} 个文件")

    def clear_files(self):
        self.files.clear()
        for iid in self.tree.get_children():
            self.tree.delete(iid)
        self.count_var.set("共 0 个文件")

    def choose_outdir(self):
        chosen = filedialog.askdirectory(title="选择输出目录", initialdir=self.outdir_var.get() or None)
        if chosen:
            self.outdir_var.set(os.path.abspath(chosen))

    # ---------------------------------------------------------------- 转换
    def start_convert(self):
        if self.busy:
            return
        if not self.files:
            messagebox.showwarning("提示", "请先添加至少一个音频文件。")
            return

        outdir = self.outdir_var.get().strip()
        if not outdir:
            messagebox.showwarning("提示", "请选择输出目录。")
            return
        try:
            os.makedirs(outdir, exist_ok=True)
        except Exception as exc:
            messagebox.showerror("错误", f"无法创建输出目录：\n{exc}")
            return

        bit_rate = int(self.bitrate_var.get()) * 1000   # kbps -> bit/s
        make_zip = bool(self.zip_var.get())
        files = list(self.files)

        self.busy = True
        self.convert_btn.config(state="disabled")
        self.add_btn.config(state="disabled")
        self.progress.config(maximum=len(files), value=0)
        self._log_clear()
        self.status_var.set("准备开始……")

        # 后台线程执行转换，避免阻塞界面（主线程只轮询队列刷新 UI）
        threading.Thread(
            target=self._worker_entry,
            args=(files, outdir, bit_rate, make_zip),
            daemon=True,
        ).start()

    def _worker_entry(self, files, outdir, bit_rate, make_zip):
        """后台线程入口：只做计算，所有界面更新都通过队列交给主线程。"""
        try:
            succeeded, failed, zip_path = run_conversion(
                files, outdir, bit_rate, make_zip,
                on_event=lambda kind, **data: self.msg_queue.put((kind, data)),
            )
            self.msg_queue.put(("finished", {
                "ok": succeeded, "failed": failed, "zip": zip_path, "outdir": outdir,
            }))
        except (KeyboardInterrupt, SystemExit):
            raise
        except BaseException:
            # 兜底：也覆盖 PanicException 这类非 Exception 异常，
            # 确保后台线程出问题时界面不会一直卡在“转换中”
            self.msg_queue.put(("fatal", {"text": traceback.format_exc()}))
            self.msg_queue.put(("finished", {
                "ok": [], "failed": [(f, "未处理的异常") for f in files], "zip": None,
                "outdir": outdir,
            }))

    # ------------------------------------------------------------ 队列轮询
    def _poll_queue(self):
        try:
            while True:
                kind, data = self.msg_queue.get_nowait()
                self._handle_event(kind, data)
        except queue.Empty:
            pass
        self.root.after(80, self._poll_queue)

    def _handle_event(self, kind, data):
        if kind == "status":
            self.status_var.set(data["text"])
        elif kind == "log":
            self._log_append(data["text"])
        elif kind == "progress":
            # 只更新进度条；状态栏的“正在处理 x/N：文件名”由 status 事件负责
            self.progress.config(value=data["done"], maximum=data["total"])
        elif kind == "fatal":
            self._log_append("[异常]\n" + data["text"])
        elif kind == "finished":
            self._on_finished(data)

    def _on_finished(self, data):
        self.busy = False
        self.convert_btn.config(state="normal")
        self.add_btn.config(state="normal")

        ok, failed, zip_path, outdir = data["ok"], data["failed"], data["zip"], data["outdir"]
        self.status_var.set(f"完成：成功 {len(ok)} 个，失败 {len(failed)} 个")

        lines = [
            "转换完成。",
            f"成功：{len(ok)} 个",
            f"失败：{len(failed)} 个",
        ]
        if zip_path:
            lines.append(f"压缩包：{zip_path}")
        text = "\n".join(lines)
        if failed:
            messagebox.showwarning("完成（部分失败）", text)
        else:
            messagebox.showinfo("完成", text)

        if (ok or zip_path) and messagebox.askyesno("打开目录", "是否打开输出目录？"):
            try:
                os.startfile(outdir)  # noqa: S606 - Windows 专用，打开资源管理器
            except Exception:
                pass

    # ---------------------------------------------------------------- 日志
    def _log_append(self, text):
        self.log_text.config(state="normal")
        self.log_text.insert("end", text + "\n")
        self.log_text.see("end")
        self.log_text.config(state="disabled")

    def _log_clear(self):
        self.log_text.config(state="normal")
        self.log_text.delete("1.0", "end")
        self.log_text.config(state="disabled")


def _safe_print(text=""):
    """
    安全输出。以 --windowed 方式打包后 sys.stdout 为 None，
    此时 print 会抛异常，因此这里统一包一层。
    """
    try:
        if sys.stdout is not None:
            print(text)
    except Exception:
        pass


def _finish_self_test(report_lines, outdir, success):
    """把自检报告写入文件（必要时也打印），并返回退出码。"""
    text = "\n".join(report_lines)
    try:
        os.makedirs(outdir, exist_ok=True)
        report_path = os.path.join(outdir, "self_test_report.txt")
        with open(report_path, "w", encoding="utf-8") as fh:
            fh.write(text)
        _safe_print(f"自检报告已写入：{report_path}")
    except Exception as exc:
        _safe_print(f"无法写入自检报告：{exc}")
    _safe_print(text)
    return 0 if success else 1


def run_self_test(args):
    """
    无界面自检模式，便于在干净机器上验证 exe 能否真正完成一次转换：

        AudioToMP3.exe --self-test <输入文件或目录> [输出目录]

    会把诊断信息与转换结果写入 <输出目录>\\self_test_report.txt，
    全部成功返回 0，否则返回 1。
    """
    if not args:
        _safe_print("用法：AudioToMP3.exe --self-test <输入文件或目录> [输出目录]")
        return 2

    source = os.path.abspath(args[0])
    if len(args) >= 2:
        outdir = os.path.abspath(args[1])
    else:
        base = source if os.path.isdir(source) else os.path.dirname(source)
        outdir = os.path.join(base, "selftest_out")

    if os.path.isdir(source):
        files = [
            os.path.join(source, name)
            for name in sorted(os.listdir(source))
            if os.path.splitext(name)[1].lower() in SUPPORTED_EXTS
            and os.path.isfile(os.path.join(source, name))
        ]
    else:
        files = [source]

    engine_status = "已加载" if codecpod is not None else f"加载失败：{_IMPORT_ERROR}"

    lines = [
        "音频转 MP3 便携工具 — 自检报告",
        "=" * 60,
        f"时间：{datetime.datetime.now():%Y-%m-%d %H:%M:%S}",
        f"Python：{sys.version.splitlines()[0]}",
        f"运行模式：{'打包 exe（frozen）' if getattr(sys, 'frozen', False) else '源码运行'}",
        f"可执行文件：{sys.executable}",
        f"codecpod：{engine_status}",
        f"输入：{source}",
        f"输出目录：{outdir}",
        f"待转换文件数：{len(files)}",
        "-" * 60,
    ]

    if codecpod is None:
        lines.append("codecpod 未能加载，自检终止。")
        return _finish_self_test(lines, outdir, False)

    logs = []
    succeeded, failed, zip_path = run_conversion(
        files, outdir, 192000, True,
        on_event=lambda kind, **data: logs.append(f"{kind}: {data.get('text', '')}"),
    )

    lines.append("转换日志：")
    lines.extend("  " + line for line in logs)
    lines.append("-" * 60)
    lines.append(f"成功：{len(succeeded)}")
    for src, dst in succeeded:
        lines.append(f"    {os.path.basename(src)} -> {os.path.basename(dst)}  ({os.path.getsize(dst)} 字节)")
    lines.append(f"失败：{len(failed)}")
    for src, err in failed:
        lines.append(f"    {os.path.basename(src)}：{err}")
    lines.append(f"ZIP：{zip_path or '（未生成）'}")
    lines.append("-" * 60)

    success = bool(succeeded) and not failed
    lines.append("结论：" + ("通过" if success else "未通过"))
    return _finish_self_test(lines, outdir, success)


def _crash_log_candidates():
    """未处理异常日志的候选位置（exe 同级目录 + 系统临时目录）。"""
    candidates = []
    try:
        exe_dir = os.path.dirname(os.path.abspath(sys.executable))
        candidates.append(os.path.join(exe_dir, "AudioToMP3_error.log"))
    except Exception:
        pass
    try:
        import tempfile
        candidates.append(os.path.join(tempfile.gettempdir(), "AudioToMP3_error.log"))
    except Exception:
        pass
    return candidates


def _write_crash_log(text):
    """
    把未处理异常写入日志文件。

    --windowed 打包后程序没有控制台，一旦出现未处理异常，
    用户界面上什么也看不到，因此这里把堆栈写到磁盘，便于排查。
    """
    for path in _crash_log_candidates():
        try:
            with open(path, "w", encoding="utf-8") as fh:
                fh.write(text)
            return path
        except Exception:
            continue
    return None


def main():
    # 命令行自检模式（不打开界面），用于验收测试
    is_self_test = len(sys.argv) > 1 and sys.argv[1] == "--self-test"

    try:
        if is_self_test:
            sys.exit(run_self_test(sys.argv[2:]))

        root = tk.Tk()
        AudioToMp3App(root)
        root.mainloop()
    except SystemExit:
        raise
    except BaseException:
        detail = traceback.format_exc()
        log_path = _write_crash_log(detail)
        # 自检模式（自动化场景）不能弹窗，否则会一直阻塞
        if not is_self_test:
            try:
                messagebox.showerror(
                    "程序发生异常",
                    detail[-1500:] + f"\n\n详细日志：{log_path}",
                )
            except Exception:
                pass
        raise


if __name__ == "__main__":
    main()

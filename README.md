<div align="center">

# 音频转 MP3 便携工具

**把常见格式的音频批量转成 MP3，打包成一个「双击即用」的 Windows 单文件程序。**

打包后的单个 `exe` 已内置 Python 运行时、静态链接的 FFmpeg 与全部依赖，
在**干净的 Windows 11 x64** 上解压即可运行 —— 无需安装 Python、FFmpeg 或 VC++ 运行库。

![Platform](https://img.shields.io/badge/platform-Windows%20x64-0078D6?logo=windows&logoColor=white)
![Python](https://img.shields.io/badge/python-3.10%20%7C%203.11%20%7C%203.12%20%7C%203.13-3776AB?logo=python&logoColor=white)
![Runtime deps](https://img.shields.io/badge/runtime%20dependencies-none-success)
![Codec](https://img.shields.io/badge/codec-FFmpeg%20(static)-orange)
![License](https://img.shields.io/badge/license-TODO-lightgrey)

</div>

---

## 目录

- [功能特性](#功能特性)
- [快速开始（普通用户）](#快速开始普通用户)
- [支持的输入格式](#支持的输入格式)
- [从源码运行](#从源码运行)
- [构建单文件 exe](#构建单文件-exe)
- [目录结构](#目录结构)
- [环境要求](#环境要求)
- [配置说明](#配置说明)
- [开发与测试](#开发与测试)
- [实现原理：三个打包陷阱](#实现原理三个打包陷阱)
- [安全说明](#安全说明)
- [已知限制](#已知限制)
- [贡献指南](#贡献指南)
- [许可证](#许可证)
- [联系方式](#联系方式)

---

## 功能特性

- **批量转换** —— 一次添加多个文件，队列式依次转换。
- **图形界面** —— 基于标准库 `tkinter`，不引入任何额外的 GUI 依赖。
- **界面不卡死** —— 转换放在后台线程，主线程只负责刷新界面，实时显示「正在处理 x/N」与进度条。
- **容错健壮** —— 遇到损坏或不受支持的文件会给出明确的中文提示并**继续处理其余文件**，不会崩溃。
- **一键打包 ZIP** —— 可选把本次输出的所有 `.mp3` 打包为 `mp3_output_YYYYMMDD_HHMMSS.zip`。
- **零依赖分发** —— PyInstaller `--onefile` 单文件产物，目标机器无需任何运行时。
- **可复现构建** —— `build.bat` / `build.py` 一条命令完成建环境、装依赖、自检、打包、出交付包。

## 快速开始（普通用户）

1. 下载 `AudioToMP3.exe`（或解压 `音频转MP3_便携版.zip`）。
2. 双击运行。首次启动需要把内置文件解压到临时目录，界面出现可能稍慢几秒。
3. 点击 **添加文件**（支持多选）→ 选择 **输出目录** → 点击 **转换为 MP3**。

> 若希望免开界面地在命令行验证一次转换：
>
> ```bat
> AudioToMP3.exe --self-test "D:\某个音频目录" "D:\输出目录"
> ```
>
> 会在输出目录生成 `self_test_report.txt`；全部成功时进程退出码为 `0`。

## 支持的输入格式

`wav` `flac` `m4a` `aac` `ogg` `wma` `aiff` `aif` `mp3` `ape` `opus` `wv` `ac3`
`alac` `mka` `mp2` `mp1` `amr` `caf` `au` 等常见格式。

输出 MP3 默认 **192 kbps**，采样率**保持与源文件一致**（界面可选 128 / 192 / 256 / 320 kbps）。

## 从源码运行

需要 Python 3.10–3.13（64 位）。

```bat
git clone https://github.com/yikodai/mp3.git
cd mp3
run.bat
```

`run.bat` 会自动使用项目内的 `.venv`；如果 `.venv` 还不存在，会先自动把环境装好。

> ⚠️ **不要直接双击 `main.py`。** 那会用系统 Python 运行，而依赖只装在 `.venv` 里，
> 于是界面会弹出「无法加载 codecpod 音频引擎：No module named 'codecpod'」。
> 该提示里会同时显示当前使用的解释器路径，便于定位。

只想准备环境、不执行打包：

```bat
python build.py --env-only
```

## 构建单文件 exe

```bat
build.bat
```

`build.bat` 只是入口，真正的逻辑在 `build.py`，依次完成：

1. 创建 / 复用 `.venv`
2. 安装依赖（默认源失败时自动切换阿里云 / 腾讯云镜像）
3. 生成图标 `assets/app.ico`
4. 运行离线自检（解码 + 编码 + 打包）
5. 运行界面逻辑测试（无头模式）
6. 用 `AudioToMP3.spec` 执行 PyInstaller 单文件打包
7. 生成交付压缩包 `dist/音频转MP3_便携版.zip`

产物：

```
dist/AudioToMP3.exe            # 单文件程序
dist/音频转MP3_便携版.zip       # 交付压缩包（exe + 使用说明.txt）
```

## 目录结构

```
mp3/
├─ main.py                 # 图形界面 + 转换逻辑 + ZIP 打包（含 --self-test 自检模式）
├─ run.bat                 # 从源码启动（自动使用 .venv）
├─ build.bat               # 构建入口（双击即可）
├─ build.py                # 一键构建脚本
├─ AudioToMP3.spec         # PyInstaller 打包配置（--onefile）
├─ requirements.txt        # 运行时与构建依赖
├─ 使用说明.txt             # 面向最终用户的中文说明（会被打进交付压缩包）
├─ assets/
│  └─ app.ico              # 应用图标（由 tools/make_icon.py 生成）
└─ tools/
   ├─ make_icon.py         # 纯标准库生成多尺寸 .ico
   ├─ smoke_test.py        # 离线链路自检（生成样本 → 转换 → 校验）
   ├─ gui_test.py          # 无头模式驱动真实界面对象，验证线程 / 队列 / 输出
   ├─ check_deps.py        # PE 依赖检查（列出非系统 DLL）
   └─ verify_bundle.py     # 打包产物依赖闭合性检查（解包后逐一核对导入表）
```

## 环境要求

| 场景 | 要求 |
| --- | --- |
| **运行打包好的 exe** | Windows 10 / 11 x64，**无其他要求** |
| **从源码运行 / 构建** | Windows x64 + Python 3.10–3.13（64 位） |

运行时依赖：`codecpod`（静态链接 FFmpeg 的音频编解码库）、`numpy`。
构建依赖：`pyinstaller`。

## 配置说明

**本项目不需要任何配置文件、环境变量或密钥。** 它不访问网络，也不读写任何凭据。

因此仓库中**没有** `.env` 或 `.env.example` —— 没有需要填写的配置项。

## 开发与测试

```bat
REM 转换链路自检：生成 wav/flac/ogg/aiff/aac 样本，批量转换并校验 mp3 与 zip
.venv\Scripts\python.exe tools\smoke_test.py

REM 界面逻辑测试：无头模式驱动真实窗口对象，验证后台线程 + 队列刷新 + 输出
.venv\Scripts\python.exe tools\gui_test.py

REM 打包产物依赖闭合性检查（解包后逐一核对导入表）
.venv\Scripts\python.exe tools\verify_bundle.py dist\AudioToMP3.exe

REM 单独查看某个 exe / dll / pyd 的导入表
.venv\Scripts\python.exe tools\check_deps.py dist\AudioToMP3.exe
```

> `gui_test.py` 需要 Tk 能初始化，但**不需要可见桌面**，因此可以在 CI 或远程会话中运行；
> 界面「实际显示效果」仍需在真实桌面上人工确认。

### 发布前检查清单

- [ ] `tools\smoke_test.py` 通过
- [ ] `tools\gui_test.py` 通过
- [ ] `tools\verify_bundle.py dist\AudioToMP3.exe` 结论为「依赖闭合」
- [ ] 在**未安装 Python / 未打 VC++ 运行库补丁**的干净 Windows 上双击 exe，确认能启动并完成一次转换

## 实现原理：三个打包陷阱

PyInstaller 对多数包都有官方 hook，但本项目有三处必须手动处理 —— 它们**不会在构建期报错，只在运行期炸**：

| 项 | 问题 | 处理方式 |
| --- | --- | --- |
| `numpy` | `codecpod` 是 Rust 原生扩展，运行时通过 Python C-API **动态 import numpy**，静态分析看不见 → 运行时报 `Failed to access NumPy array API capsule: No module named 'numpy'` | `.spec` 中 `hiddenimports += ["numpy"]` |
| `codecpod.libs` | 该 wheel 由 delvewheel 重定位，MinGW 运行库（`libunwind` / `libwinpthread`）在 `site-packages/codecpod.libs`，`codecpod/__init__.py` 运行时用 `os.add_dll_directory` 加载 | `.spec` 中显式收集为 `codecpod.libs/`，**保持目录结构** |
| `python3.dll` | `codecpod.pyd` 是 abi3 扩展，导入表引用 `python3.dll` 转发 DLL | `.spec` 中从 Python 安装目录显式收集 |

另外两个容易忽略的点：

- `codecpod` 解码失败时抛出 PyO3 的 `PanicException`，它继承自 `BaseException` 而**不是** `Exception`。
  因此单文件错误处理捕获的是 `BaseException`（同时放行 `KeyboardInterrupt` / `SystemExit`），
  否则一个坏文件就会拖垮整批任务。
- `VCRUNTIME140` / `VCRUNTIME140_1` 由 PyInstaller 从 Python 安装目录自动打包，
  所以目标机器**不需要**安装 VC++ Redistributable。

## 安全说明

- 程序**完全离线**运行：不发起任何网络请求，不上传任何文件。
- **不收集、不存储任何凭据或个人数据**；项目中不含密钥、令牌或私有配置。
- 转换在本地内存中完成，仅写入你指定的输出目录。
- 打包产物**未使用 UPX 加壳**；若杀毒软件报警，通常是 PyInstaller 打包程序的常见误报。
- 图标 `assets/app.ico` 由 `tools/make_icon.py` 用标准库生成，来源可复现。

## 已知限制

- 输出为恒定码率 MP3，暂不支持 VBR 或元数据（ID3）编辑。
- 单文件 exe 体积较大（内含 Python 运行时、静态 FFmpeg、numpy），属正常现象。
- 单文件模式首次启动需自解压，界面出现可能慢几秒。
- 目前仅面向 Windows x64 构建与测试。

## 贡献指南

欢迎提交 Issue 与 Pull Request。

1. Fork 本仓库并新建分支：`git checkout -b feature/your-feature`
2. 本地运行自检，确认未破坏既有行为：
   ```bat
   .venv\Scripts\python.exe tools\smoke_test.py
   .venv\Scripts\python.exe tools\gui_test.py
   ```
3. 提交时请写清动机与影响范围，必要时附上复现步骤。
4. 发起 Pull Request。

代码风格：保持与现有代码一致；用户可见文案使用中文；关键逻辑补简短中文注释。

## 许可证

**TODO**：本项目尚未指定开源许可证。在补充许可证之前，默认保留所有权利。

> 请注意：本项目的运行时依赖 `codecpod` 以 **LGPL-2.1-or-later** 分发，
> 并静态链接了 FFmpeg（LGPL）、LAME 等第三方组件。
> 若你计划分发打包后的 `exe`，请自行核对并遵守这些第三方组件的许可证义务
> （例如随附许可证文本、提供相应的源码获取方式等）。此处仅为提示，不构成法律意见。

## 联系方式

**TODO**：作者与联系方式待补充。

- 作者：TODO
- 邮箱：TODO
- 仓库：<https://github.com/yikodai/mp3>

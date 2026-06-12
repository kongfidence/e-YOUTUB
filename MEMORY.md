# MEMORY.md — e-YOUTUB 项目记忆

> 工作中学到的东西回写这里：架构决策、踩坑、用户纠正、外部资源位置。
> 凭据只记位置不记值；代码能查到的不往这里抄。

## 环境事实（开发机 Windows）

- Python 3.12.10，yt-dlp 2026.03.17，node v22，ffmpeg 8.0.1 均已安装。
- 端口 5000 当前空闲（Windows）。
- 不是 git 仓库（删文件无版本回退）。
- `~/.codex/templates/` 不存在，AGENTS/MEMORY 由 agent 按项目实况手写。

## 关键踩坑 / 决策

- **端口 5000 跨平台坑**：macOS 上 5000/7000 被 AirPlay Receiver（ControlCenter）占用，
  写死 5000 会导致 Mac 上「开唔到」或连到错误服务。→ 改为自动选空闲端口。
- **cookie 提取可能致命**：`--cookies-from-browser` 若浏览器锁库/无权限会让 yt-dlp 整条失败。
  → 应做成「失败就退回不带 cookie 重试」，而非硬依赖。
- **JS runtime**：app.py 原本硬编码 `--js-runtimes node`，托盘版是动态探测。
  统一改成动态探测（node/deno/bun/qjs），没有就不加这个 flag。

## 文件分类（清理参考）

- 核心保留：`app.py`、`templates/index.html`、`scraper.py`、`requirements.txt`、启动脚本。
- 提取功能依赖：`stealth_fetch.py`、`content_extractor.py`（被 app.py import）。
- 工具箱（README 记录的 6 模块）：`document_extractor.py`、`dedup.py`、`resilience.py`、`scraper_v2.py`。
- 实验性垃圾（一次性 Instagram 抓取脚本，未被 app 引用）：`ig_*.py` 一堆 + `ig_subtitles/` 数据。
- 测试：`test_*.py`。

## 已完成（2026-06-12）

- 修复下载根因：`--cookies-from-browser` 在 Chrome 开启锁库时致命。
  改为「先不带 cookie 下载，失败才用浏览器 cookie 重试」。实测公开影片成功。
- `app.py` JS runtime 改动态探测（`_get_js_runtime`，带缓存），不再硬编码 node。
- 端口改自动选空闲口（`_find_free_port`，扫 8731-8770，避开 5000/7000），
  app.py 自己探测就绪后开浏览器。
- 启动脚本：`萬能下載器.bat` 改成只调 `app_launcher.vbs`（删掉硬编码 :5000 逻辑）；
  新增 macOS `萬能下載器.command`（首次需 `chmod +x`）。
- 删除垃圾：13 个 `ig_*.py` 实验脚本 + `ig_subtitles/`（40 个字幕文件），永久删除。

## 已完成（2026-06-12 第二轮：扫全项目修 bug）

- `yt_tray.pyw` 托盘版同样的 cookie 致命 bug 已修：`_build_cmd(use_cookies=False)`，
  `_run_download` 拆出 `_run_once`，先不带 cookie 跑、失败再带 cookie 重试一次。
- `app.py` cookie 重试加判据：只在 yt-dlp 输出含「登入/bot/age/private」等关键词时才重试，
  避免普通网页白跑第二次。
- 新增**单实例守卫**：app.py 启动把端口写 `.run_port`，再次启动若该端口仍响应就只开浏览器并退出，
  解决「动态端口下重复双击会起多个服务」的回归。退出时 atexit 清掉 `.run_port`。
- `scraper.py` 审过，无真实 bug。

## 已完成（2026-06-12 第三轮：启动器整理 + 隐藏控制台）

- 砍掉 6 个混乱旧启动器（萬能下載器.bat/.vbs/.command、app_launcher.vbs、start_web.bat、start_ytdl.bat），
  换成 3 个英文清楚命名：
  - `Browser_Windows.vbs`（浏览器版 / Win）
  - `Browser_Mac.command`（浏览器版 / Mac）
  - `Tray_Windows.vbs`（托盘版 / 仅 Win）
- 全部隐藏控制台：Win 用 `wscript + pythonw`（无黑窗），Mac 用 `nohup &` + osascript 自关 Terminal。
- 实测：Browser_Windows.vbs 起服务 health 200、`/api/shutdown` 后 2s 内进程退出且 `.run_port` 已清；
  Tray_Windows.vbs 以 pythonw 隐藏启动成功。
- 关键发现：Windows 上 `os.kill(getpid, SIGINT)` 是 TerminateProcess 硬杀，atexit 不会跑 →
  改成在 shutdown / auto-shutdown 路径里**先 `_cleanup_port_file()` 再 kill**，`_PORT_FILE` 提为模块全局。

## 已完成（2026-06-12 第四轮：下载完成通知）

- 浏览器版下载完成时弹通知（`templates/index.html`）：
  - 优先桌面通知（Web Notification API），点击 → 调 `openOutputFolder()` 开下载文件夹；
    不点 8 秒 `n.close()` 自动消散。`tag:'download-done'` 防堆积。
  - 权限被拒/不支持 → 页内 toast 后备（右下角，点击开文件夹，8 秒自动消散）。
  - 通知权限在「点下载」那一刻请求（需用户手势）。
- 托盘版本身已有 `self.icon.notify(...)` 完成通知，且窗口内有「開啟下載資料夾」按钮，未改。

## 已完成（2026-06-12 第五轮：macOS 原生 .app）

- 删 `Browser_Mac.command`，改建 `Universal Downloader.app`（Info.plist + Contents/MacOS/launch）。
  优点：双击**无终端窗**，可拖 Dock；launch 上溯 3 层定位项目根跑 app.py。
- 关键限制（已向 Terry 说明，不要假装能绕过）：从 Windows 整的 .app，macOS 第一次必须解锁
  （`chmod +x launch` + `xattr -dr com.apple.quarantine .app`）。Apple 安全机制，除非 Mac 上签名+公证。
- 真‧零手续方案 = git：clone 的文件不带 quarantine + git 保留 exec bit → Mac 双击直接开。
  需 `git update-index --chmod=+x "Universal Downloader.app/Contents/MacOS/launch"`。
- ⚠ Windows 文件系统存不了 exec bit，故 .app 的 launch 现在没有可执行位，必须靠上述解锁或 git。

## 运行时文件

- `.run_port`：app.py 运行时写当前端口，退出自动删，stale 也会被存活探测兜底。非垃圾，勿手删进代码。

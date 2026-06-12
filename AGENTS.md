# AGENTS.md — e-YOUTUB / 萬能下載器

> 给 AI agent 的项目说明书。开工前必读，改实践前先改这里。

## 这是什么

一个**纯本地**运行的「萬能下載器」：贴上任意网址（YouTube / 视频平台 / 普通网页 / 直链媒体），
后端用 `yt-dlp` + 网页爬虫把图片和视频抓下来。不上传任何资料。

两套界面，共用底层下载逻辑：
- **Web 版（主）**：`app.py`（Flask）+ `templates/index.html`，浏览器操作，一键启动后自动开浏览器。
- **托盘版（次）**：`yt_tray.pyw`，Windows 系统匣 GUI，仅 Windows。

底层爬虫工具箱（被 Web 版的「正文提取」功能调用）：
`scraper.py`（核心，必需）、`stealth_fetch.py`、`content_extractor.py`、
`document_extractor.py`、`dedup.py`、`resilience.py`、`scraper_v2.py`。

## 技术栈

- Python 3.8+（开发机 3.12.10）
- Flask（Web 后端，相对路径 API，无硬编码端口给前端）
- yt-dlp（视频下载引擎，需要 ffmpeg 合并、node/deno 等 JS runtime 解 YouTube challenge）
- 前端：单文件 `templates/index.html`，原生 JS，全部用相对 URL `/api/...`

## 一键启动（3 个英文命名的入口，双击即用，全部隐藏控制台）

| 入口文件 | 版本 | 平台 | 行为 / 自动关闭 |
|------|------|------|------|
| `Browser_Windows.vbs` | 浏览器版（Flask Web） | Windows | pythonw 隐藏起服务，自动开浏览器；**关掉浏览器分页 → 服务自动退出** |
| `Universal Downloader.app` | 浏览器版（Flask Web） | macOS | 原生 App，双击**无终端窗**；开浏览器；**关掉分页 → App 自动退出**。必须放在项目根目录内（launch 用相对路径上溯 3 层找 app.py） |
| `Tray_Windows.vbs` | 托盘版（系统匣 GUI） | Windows ONLY | pythonw 隐藏，系统匣出现红色图标；**托盘「退出」→ 程序自动关** |

> macOS 限制：从 Windows 传过去的 .app 第一次要「解锁」一次（exec bit + 清 quarantine），
> 之后双击即开。真‧零手续路径 = 用 `git clone` 把项目弄到 Mac（git 保留 exec bit，
> clone 的文件不带 quarantine，故双击直接开，无需解锁）。launch 的 exec bit 需在 git 里
> 用 `git update-index --chmod=+x "Universal Downloader.app/Contents/MacOS/launch"` 标记。

- 浏览器版 = `app.py` + `templates/index.html`，跨平台（Win + Mac）。
- 托盘版 = `yt_tray.pyw`，仅 Windows（用了 `os.startfile` 等 Windows API）。
- macOS 没有托盘版，统一用浏览器版。

启动哲学：
- **端口自动选空闲口**（不写死 5000，因 macOS AirPlay 占 5000/7000），app.py 自己探测就绪后开浏览器。
- **无黑窗**：Windows 用 `wscript + pythonw`，Mac 用 `nohup &` 后自关 Terminal。
- **单实例**：app.py 把端口写 `.run_port`，重复启动只开浏览器不再起第二个；关闭时清掉该文件。
- **自动关闭**：浏览器版靠心跳，关掉分页即停（`/api/shutdown` + 60s 无心跳兜底）；托盘版退出即停。

## 开发约定

- 默认中文沟通；代码、命令、变量名用英文。
- 改完主动跑验证（运行 app.py / 真实下载一条测试链接）。
- 不为了跑起来而注释掉报错，找根因。
- 密钥/cookie 不进代码（cookie 由 yt-dlp 从本地浏览器实时读取）。
- 本项目**目前不是 git 仓库** → 删除文件不可逆，动手前确认。

## 部署

无服务器部署，纯本地。用对应平台的一键启动脚本运行。

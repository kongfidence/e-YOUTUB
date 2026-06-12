"""
萬能下載器 - Flask 後端伺服器
支援 YouTube / 任何網站 的圖片和影片下載
完全本地運作，不上傳任何資料
"""

from flask import Flask, render_template, request, jsonify
import json
import os
import re
import threading
import time
import uuid
import subprocess
import sys
import hashlib
import string
from urllib.parse import urlparse, unquote

from scraper import (
    extract_urls_from_text, is_ytdlp_supported, is_direct_media_url,
    scrape_media, download_file, generate_filename, fetch_page, HEADERS,
    MediaItem, IMAGE_EXTENSIONS, VIDEO_EXTENSIONS
)

import asyncio
try:
    from stealth_fetch import StealthFetcher
    from content_extractor import ContentExtractor
    _HAS_EXTRACT = True
except ImportError:
    _HAS_EXTRACT = False

# 確保模板和靜態文件路徑正確
try:
    _base_dir = os.path.dirname(os.path.abspath(__file__))
except NameError:
    _base_dir = os.getcwd()
app = Flask(__name__,
            template_folder=os.path.join(_base_dir, 'templates'),
            static_folder=os.path.join(_base_dir, 'static'))

# ═══════════════════════════════════════════════════════════════
# 全域狀態管理
# ═══════════════════════════════════════════════════════════════

download_tasks = {}  # task_id -> task dict
tasks_lock = threading.Lock()

# 心跳 & 自動關閉（瀏覽器關閉後伺服器自動退出）
_heartbeat_time = None          # None = 尚未收到任何心跳
_heartbeat_lock = threading.Lock()

# 由 __main__ 設定；記錄當前端口嘅檔案，關閉前清掉避免留下 stale 檔
_PORT_FILE = None


def _cleanup_port_file():
    """關閉前刪掉 .run_port（Windows 上 os.kill 係硬殺，atexit 唔會行）"""
    try:
        if _PORT_FILE and os.path.exists(_PORT_FILE):
            os.remove(_PORT_FILE)
    except Exception:
        pass


def _auto_shutdown_watcher():
    """If no heartbeat from browser for 60s, shut down.
    Source: https://stackoverflow.com/questions/71864189 (Flask shutdown)
    """
    import signal
    while True:
        time.sleep(15)
        with _heartbeat_lock:
            if _heartbeat_time is None:
                continue
            elapsed = time.time() - _heartbeat_time
        if elapsed > 60:
            _cleanup_port_file()
            os.kill(os.getpid(), signal.SIGINT)

# ═══════════════════════════════════════════════════════════════
# 路由
# ═══════════════════════════════════════════════════════════════


@app.route('/')
def index():
    return render_template('index.html')


@app.route('/api/analyze', methods=['POST'])
def analyze():
    """分析文字，提取所有 URL 並分類"""
    data = request.json or {}
    text = data.get('text', '')

    if not text.strip():
        return jsonify({'urls': [], 'count': 0})

    urls = extract_urls_from_text(text)

    results = []
    for url in urls:
        is_video = is_ytdlp_supported(url)
        is_media, media_type = is_direct_media_url(url)

        if is_media:
            url_type = f'direct_{media_type}'
            label = '直接媒體文件'
        elif is_video:
            url_type = 'video_platform'
            label = '影片平台'
        else:
            url_type = 'webpage'
            label = '一般網頁'

        domain = ''
        try:
            domain = urlparse(url).netloc
        except:
            pass

        results.append({
            'url': url,
            'type': url_type,
            'label': label,
            'domain': domain,
        })

    return jsonify({'urls': results, 'count': len(results)})


@app.route('/api/download', methods=['POST'])
def start_download():
    """開始下載任務"""
    data = request.json or {}
    urls = data.get('urls', [])
    output_dir = data.get('output_dir', '')
    video_format = data.get('video_format', 'best_video')
    video_quality = data.get('video_quality', 'best')
    audio_format = data.get('audio_format', 'mp3')
    download_images = data.get('download_images', True)
    download_videos = data.get('download_videos', True)
    max_duration = data.get('max_duration', 0)       # 最大下載時長（秒），0=不限
    max_filesize = data.get('max_filesize', '')       # 最大檔案大小，如 '200M'

    if not urls:
        return jsonify({'error': '沒有提供任何 URL'}), 400
    if not output_dir:
        return jsonify({'error': '請指定輸出資料夾'}), 400

    # 建立輸出資料夾
    try:
        os.makedirs(output_dir, exist_ok=True)
    except Exception as e:
        return jsonify({'error': f'無法建立輸出資料夾: {str(e)}'}), 400

    task_id = str(uuid.uuid4())[:8]
    task = {
        'id': task_id,
        'status': 'running',
        'total': len(urls),
        'completed': 0,
        'failed': 0,
        'skipped': 0,
        'downloaded_files': [],
        'logs': [],
        'current': '',
        'progress_detail': '',
    }

    with tasks_lock:
        download_tasks[task_id] = task

    thread = threading.Thread(
        target=download_worker,
        args=(task_id, urls, output_dir, video_format, video_quality,
              audio_format, download_images, download_videos,
              max_duration, max_filesize),
        daemon=True
    )
    thread.start()

    return jsonify({'task_id': task_id})


@app.route('/api/status/<task_id>')
def get_status(task_id):
    """獲取下載任務狀態"""
    task = download_tasks.get(task_id)
    if not task:
        return jsonify({'error': '找不到任務'}), 404

    # 只回傳最近的日誌（避免太多資料）
    last_n = request.args.get('last_logs', 50, type=int)
    response_data = {
        'id': task['id'],
        'status': task['status'],
        'total': task['total'],
        'completed': task['completed'],
        'failed': task['failed'],
        'skipped': task['skipped'],
        'current': task['current'],
        'progress_detail': task['progress_detail'],
        'downloaded_files': task['downloaded_files'][-20:],
        'logs': task['logs'][-last_n:],
        'log_count': len(task['logs']),
    }
    return jsonify(response_data)


@app.route('/api/logs/<task_id>')
def get_logs(task_id):
    """獲取完整日誌"""
    task = download_tasks.get(task_id)
    if not task:
        return jsonify({'error': '找不到任務'}), 404

    offset = request.args.get('offset', 0, type=int)
    return jsonify({
        'logs': task['logs'][offset:],
        'total': len(task['logs']),
    })


@app.route('/api/stop/<task_id>', methods=['POST'])
def stop_task(task_id):
    """停止下載任務"""
    task = download_tasks.get(task_id)
    if task and task['status'] == 'running':
        task['status'] = 'stopping'
        add_log(task_id, '⏹ 正在停止下載...')
    return jsonify({'ok': True})


@app.route('/api/pick_folder', methods=['POST'])
def pick_folder():
    """開啟原生 OS 資料夾選擇對話框"""
    data = request.json or {}
    initial_dir = data.get('initial_dir', '')

    # 用 subprocess 啟動 tkinter 原生對話框（避免主線程衝突）
    script = (
        'import tkinter as tk\n'
        'from tkinter import filedialog\n'
        'root = tk.Tk()\n'
        'root.withdraw()\n'
        'root.wm_attributes("-topmost", True)\n'
        f'folder = filedialog.askdirectory(initialdir=r"{initial_dir}")\n'
        'if folder:\n'
        '    print(folder)\n'
        'root.destroy()\n'
    )
    try:
        result = subprocess.run(
            [sys.executable, '-c', script],
            capture_output=True, text=True, encoding='utf-8',
            errors='replace', timeout=120,
        )
        folder = result.stdout.strip()
        if folder:
            return jsonify({'folder': folder})
        else:
            return jsonify({'folder': '', 'cancelled': True})
    except subprocess.TimeoutExpired:
        return jsonify({'error': '對話框逾時'}), 408
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/api/heartbeat', methods=['POST'])
def heartbeat():
    """瀏覽器定時心跳，保持伺服器存活"""
    global _heartbeat_time
    with _heartbeat_lock:
        _heartbeat_time = time.time()
    return jsonify({'ok': True})


@app.route('/api/shutdown', methods=['POST'])
def shutdown_server():
    """Browser closed -> shut down after 2s.
    Source: https://www.sqlpey.com/python/top-12-ways-to-stop-flask-application/
    Uses SIGINT instead of os._exit for graceful cleanup.
    """
    import signal
    def _do():
        time.sleep(2)
        _cleanup_port_file()
        os.kill(os.getpid(), signal.SIGINT)
    threading.Thread(target=_do, daemon=True).start()
    return jsonify({'ok': True})


@app.route('/api/open_folder', methods=['POST'])
def open_folder():
    """用系統檔案總管開啟資料夾"""
    data = request.json or {}
    folder = data.get('folder', '')

    if not folder or not os.path.isdir(folder):
        return jsonify({'error': '資料夾不存在'}), 400

    try:
        if sys.platform == 'win32':
            os.startfile(folder)
        elif sys.platform == 'darwin':
            subprocess.Popen(['open', folder])
        else:
            subprocess.Popen(['xdg-open', folder])
        return jsonify({'ok': True})
    except Exception as e:
        return jsonify({'error': str(e)}), 500


# ═══════════════════════════════════════════════════════════════
# 正文提取 API
# ═══════════════════════════════════════════════════════════════

@app.route('/api/extract', methods=['POST'])
def extract_text_api():
    """提取網頁正文（markdown）"""
    if not _HAS_EXTRACT:
        return jsonify({'error': '提取模組未安裝（需要 stealth_fetch + content_extractor）'}), 500

    data = request.json or {}
    urls = data.get('urls', [])
    if not urls:
        return jsonify({'error': '沒有提供 URL'}), 400

    results = []

    async def _do_extract():
        fetcher = StealthFetcher(max_level=1)
        extractor = ContentExtractor(output_format="markdown")
        sem = asyncio.Semaphore(10)

        async def _one(url):
            async with sem:
                try:
                    fr = await fetcher.fetch(url, timeout=30)
                    if not fr.ok:
                        return {'url': url, 'error': fr.error or f'HTTP {fr.status_code}'}
                    er = extractor.extract(fr.html, url)
                    return {
                        'url': url,
                        'title': er.title or url,
                        'markdown': er.text_markdown or er.text or '',
                        'word_count': er.word_count,
                        'language': er.language,
                    }
                except Exception as e:
                    return {'url': url, 'error': str(e)}

        return await asyncio.gather(*[_one(u) for u in urls])

    try:
        loop = asyncio.new_event_loop()
        extracted = loop.run_until_complete(_do_extract())
        loop.close()
        results = list(extracted)
    except Exception as e:
        return jsonify({'error': str(e)}), 500

    return jsonify({'results': results, 'count': len(results)})


# ═══════════════════════════════════════════════════════════════
# 下載工作邏輯
# ═══════════════════════════════════════════════════════════════

def add_log(task_id, msg):
    """新增日誌"""
    task = download_tasks.get(task_id)
    if task:
        task['logs'].append({
            'time': time.strftime('%H:%M:%S'),
            'msg': msg
        })


def download_worker(task_id, urls, output_dir, video_format, video_quality,
                    audio_format, download_images, download_videos,
                    max_duration=0, max_filesize=''):
    """後台下載工作執行緒"""
    task = download_tasks[task_id]

    add_log(task_id, f'🚀 開始處理 {len(urls)} 條連結...')
    add_log(task_id, f'📁 輸出至: {output_dir}')
    if max_duration > 0:
        add_log(task_id, f'⏱ 最大時長: {max_duration} 秒')
    if max_filesize:
        add_log(task_id, f'📏 最大檔案: {max_filesize}')

    for i, url_info in enumerate(urls):
        if task['status'] in ('stopping', 'stopped'):
            task['status'] = 'stopped'
            add_log(task_id, '⛔ 已停止下載')
            return

        url = url_info if isinstance(url_info, str) else url_info.get('url', '')
        url_type = 'auto' if isinstance(url_info, str) else url_info.get('type', 'auto')

        task['current'] = url
        task['progress_detail'] = f'({i + 1}/{len(urls)})'
        add_log(task_id, f'\n{"=" * 50}')
        add_log(task_id, f'📥 [{i + 1}/{len(urls)}] {url}')

        try:
            success = False

            # 判斷 URL 類型並選擇下載策略
            is_media, media_type = is_direct_media_url(url)

            if is_media:
                # 直接媒體文件
                add_log(task_id, f'📎 直接媒體文件 ({media_type})')
                if (media_type in ('image',) and download_images) or \
                   (media_type in ('video', 'audio') and download_videos):
                    success = download_direct_media(task_id, url, output_dir)
                else:
                    add_log(task_id, f'⏭ 跳過 ({media_type} 下載已關閉)')
                    task['skipped'] += 1
                    continue

            elif url_type == 'video_platform' or is_ytdlp_supported(url):
                # yt-dlp 支援的平台
                add_log(task_id, '🎬 影片平台，使用 yt-dlp 下載')
                success = download_with_ytdlp(
                    task_id, url, output_dir,
                    video_format, video_quality, audio_format,
                    max_duration=max_duration, max_filesize=max_filesize
                )

            else:
                # 一般網頁：先嘗試 yt-dlp，失敗則用爬蟲
                add_log(task_id, '🌐 一般網頁，嘗試 yt-dlp...')
                success = download_with_ytdlp(
                    task_id, url, output_dir,
                    video_format, video_quality, audio_format,
                    max_duration=max_duration, max_filesize=max_filesize
                )

                if not success:
                    add_log(task_id, '🔄 yt-dlp 無法處理，切換到網頁爬蟲模式...')
                    success = download_with_scraper(
                        task_id, url, output_dir,
                        download_images, download_videos
                    )

            if success:
                task['completed'] += 1
                add_log(task_id, f'✅ 完成: {url[:80]}')
            else:
                task['failed'] += 1
                add_log(task_id, f'❌ 失敗: {url[:80]}')

        except Exception as e:
            task['failed'] += 1
            add_log(task_id, f'❌ 錯誤: {str(e)}')

    task['status'] = 'completed'
    task['current'] = ''
    task['progress_detail'] = ''

    total_files = len(task['downloaded_files'])
    add_log(task_id, f'\n{"=" * 50}')
    add_log(task_id, f'🎉 全部完成！')
    add_log(task_id, f'   成功: {task["completed"]} | 失敗: {task["failed"]} | 跳過: {task["skipped"]}')
    add_log(task_id, f'   共下載 {total_files} 個文件')


def download_direct_media(task_id, url, output_dir):
    """直接下載媒體文件"""
    filename = generate_filename(url)
    output_path = os.path.join(output_dir, filename)

    # 避免覆蓋
    output_path = get_unique_path(output_path)

    add_log(task_id, f'   ⬇ 下載: {filename}')
    success, result = download_file(url, output_path)

    if success:
        download_tasks[task_id]['downloaded_files'].append({
            'name': os.path.basename(output_path),
            'path': output_path,
            'size': result,
        })
        add_log(task_id, f'   ✅ 已儲存: {os.path.basename(output_path)} ({format_size(result)})')
        return True
    else:
        add_log(task_id, f'   ❌ 下載失敗: {result}')
        return False


# ── 快取瀏覽器 cookie 偵測（一次偵測，全局使用）──
_cookie_browser_cache = None
_cookie_browser_detected = False

def _get_cookie_browser():
    """偵測可用嘅瀏覽器 cookies（YouTube 需要 cookies 繞過 bot 偵測）"""
    global _cookie_browser_cache, _cookie_browser_detected
    if _cookie_browser_detected:
        return _cookie_browser_cache
    _cookie_browser_detected = True

    # 透過檢查瀏覽器配置檔路徑來偵測（快速且可靠）
    import platform
    if platform.system() == 'Windows':
        appdata = os.environ.get('APPDATA', '')
        localappdata = os.environ.get('LOCALAPPDATA', '')
        browser_paths = [
            ('firefox', os.path.join(appdata, 'Mozilla', 'Firefox', 'Profiles')),
            ('chrome',  os.path.join(localappdata, 'Google', 'Chrome', 'User Data')),
            ('edge',    os.path.join(localappdata, 'Microsoft', 'Edge', 'User Data')),
            ('brave',   os.path.join(localappdata, 'BraveSoftware', 'Brave-Browser', 'User Data')),
        ]
    else:
        home = os.path.expanduser('~')
        browser_paths = [
            ('firefox', os.path.join(home, '.mozilla', 'firefox')),
            ('chrome',  os.path.join(home, '.config', 'google-chrome')),
        ]

    for browser, check_path in browser_paths:
        if os.path.exists(check_path):
            _cookie_browser_cache = browser
            print(f'[yt-dlp] 🍪 偵測到 {browser} — 使用其 cookies')
            return browser

    print('[yt-dlp] ⚠ 無可用瀏覽器 cookies，YouTube 下載可能受限')
    return None


# ── 快取 JS runtime 偵測（yt-dlp 需要佢解 YouTube JS challenge）──
_js_runtime_cache = None
_js_runtime_detected = False

def _get_js_runtime():
    """動態偵測可用嘅 JS runtime，搵唔到就回 None（唔好硬編碼 node）"""
    global _js_runtime_cache, _js_runtime_detected
    if _js_runtime_detected:
        return _js_runtime_cache
    _js_runtime_detected = True

    cf = subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0
    for name in ('node', 'deno', 'bun', 'qjs'):
        try:
            subprocess.run([name, '--version'], capture_output=True, creationflags=cf)
            _js_runtime_cache = 'quickjs' if name == 'qjs' else name
            print(f'[yt-dlp] 🟢 偵測到 JS runtime: {_js_runtime_cache}')
            return _js_runtime_cache
        except FileNotFoundError:
            continue
    print('[yt-dlp] ⚠ 無 JS runtime（node/deno）— YouTube 下載可能失敗')
    return None


def download_with_ytdlp(task_id, url, output_dir, video_format, video_quality, audio_format,
                        max_duration=0, max_filesize=''):
    """使用 yt-dlp 下載。

    Cookie 策略：先試「不帶 cookie」（多數公開影片唔需要），
    失敗先用瀏覽器 cookie 重試。避免瀏覽器鎖住 cookie DB 時
    （Chrome 開住會 lock）令每條下載都直接失敗。
    """
    task = download_tasks[task_id]

    # 記錄下載前已有的檔案
    existing_files = set()
    if os.path.exists(output_dir):
        existing_files = set(os.listdir(output_dir))

    def _build_cmd(use_cookies):
        cmd = [sys.executable, "-m", "yt_dlp", "--no-playlist"]

        js_rt = _get_js_runtime()
        if js_rt:
            cmd += ["--js-runtimes", js_rt]

        if use_cookies:
            cookie_browser = _get_cookie_browser()
            if cookie_browser:
                cmd += ["--cookies-from-browser", cookie_browser]

        # 根據格式設定參數
        if video_format == 'mp3':
            cmd += ["-x", "--audio-format", "mp3"]
        elif video_format == 'm4a':
            cmd += ["-x", "--audio-format", "m4a"]
        elif video_format == 'wav':
            cmd += ["-x", "--audio-format", "wav"]
        elif video_format == 'flac':
            cmd += ["-x", "--audio-format", "flac"]
        elif video_format == 'audio_only':
            cmd += ["-x", "--audio-format", audio_format]
        else:
            quality_map = {
                'best': 'bestvideo[ext=mp4]+bestaudio[ext=m4a]/best[ext=mp4]/best',
                '2160p': 'bestvideo[height<=2160][ext=mp4]+bestaudio[ext=m4a]/best[height<=2160][ext=mp4]/best',
                '1440p': 'bestvideo[height<=1440][ext=mp4]+bestaudio[ext=m4a]/best[height<=1440][ext=mp4]/best',
                '1080p': 'bestvideo[height<=1080][ext=mp4]+bestaudio[ext=m4a]/best[height<=1080][ext=mp4]/best',
                '720p': 'bestvideo[height<=720][ext=mp4]+bestaudio[ext=m4a]/best[height<=720][ext=mp4]/best',
                '480p': 'bestvideo[height<=480][ext=mp4]+bestaudio[ext=m4a]/best[height<=480][ext=mp4]/best',
                '360p': 'bestvideo[height<=360][ext=mp4]+bestaudio[ext=m4a]/best[height<=360][ext=mp4]/best',
            }
            fmt_str = quality_map.get(video_quality, quality_map['best'])
            cmd += ["-f", fmt_str]

        if max_duration and max_duration > 0:
            cmd += ["--download-sections", f"*00:00:00-00:{max_duration // 60:02d}:{max_duration % 60:02d}"]
            cmd += ["--force-keyframes-at-cuts"]

        if max_filesize:
            cmd += ["--max-filesize", max_filesize]

        cmd += ["-o", os.path.join(output_dir, "%(title)s.%(ext)s")]
        cmd += ["--no-overwrites"]
        cmd += [url]
        return cmd

    def _run(cmd):
        """跑一次 yt-dlp，回傳 (returncode, needs_cookies)。"""
        creationflags = subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0
        needs_cookies = False
        process = subprocess.Popen(
            cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, encoding="utf-8", errors="replace",
            creationflags=creationflags
        )
        for line in process.stdout:
            line = line.strip()
            if not line:
                continue
            # 偵測「需要登入 / bot 驗證」類錯誤 → 值得帶 cookie 重試
            low = line.lower()
            if any(k in low for k in ('sign in', 'log in', 'login required',
                                      'confirm you', 'not a bot', 'age',
                                      'private video', 'members-only', 'cookies')):
                needs_cookies = True
            if '[download]' in line and '%' in line:
                task['progress_detail'] = line.split(']')[-1].strip()
            add_log(task_id, f'   {line}')
        process.wait(timeout=600)
        return process.returncode, needs_cookies

    if max_duration and max_duration > 0:
        add_log(task_id, f'   ⏱ 限制下載時長: {max_duration} 秒')
    if max_filesize:
        add_log(task_id, f'   📏 限制檔案大小: {max_filesize}')

    try:
        # 第一次：不帶 cookie
        rc, needs_cookies = _run(_build_cmd(use_cookies=False))

        # 失敗 + 輸出顯示需要登入/bot 驗證 + 有可用瀏覽器 → 帶 cookie 重試一次
        # （只在真係需要時重試，避免普通網頁白白多跑一次）
        if rc != 0 and needs_cookies and _get_cookie_browser():
            add_log(task_id, '   🍪 偵測到需要登入，改用瀏覽器 cookies 重試...')
            rc, _ = _run(_build_cmd(use_cookies=True))

        if rc == 0:
            add_log(task_id, '   ✅ yt-dlp 下載成功')

            if os.path.exists(output_dir):
                new_files = set(os.listdir(output_dir)) - existing_files
                for fname in new_files:
                    fpath = os.path.join(output_dir, fname)
                    if os.path.isfile(fpath):
                        if any(fname.endswith(ext) for ext in ['.part', '.ytdl', '.temp']):
                            continue
                        fsize = os.path.getsize(fpath)
                        task['downloaded_files'].append({
                            'name': fname,
                            'path': fpath,
                            'size': fsize,
                        })
                        add_log(task_id, f'   📁 已記錄: {fname} ({format_size(fsize)})')
            return True
        else:
            return False

    except subprocess.TimeoutExpired:
        add_log(task_id, '   ⏰ yt-dlp 下載超時')
        return False
    except Exception as e:
        add_log(task_id, f'   ❌ yt-dlp 錯誤: {str(e)}')
        return False


def download_with_scraper(task_id, url, output_dir, download_images=True, download_videos=True):
    """使用網頁爬蟲下載相關媒體"""
    task = download_tasks[task_id]

    add_log(task_id, '   🔍 正在分析網頁內容...')

    try:
        media_items = scrape_media(url, check_sizes=True)
    except Exception as e:
        add_log(task_id, f'   ❌ 爬蟲錯誤: {str(e)}')
        return False

    if not media_items:
        add_log(task_id, '   ⚠ 未找到相關媒體')
        return False

    # 過濾類型
    filtered = []
    for item in media_items:
        if item.media_type == 'image' and download_images:
            filtered.append(item)
        elif item.media_type == 'video' and download_videos:
            filtered.append(item)

    if not filtered:
        add_log(task_id, '   ⚠ 沒有符合條件的媒體')
        return False

    add_log(task_id, f'   📊 找到 {len(filtered)} 個相關媒體項目')

    # 建立子資料夾（用域名+哈希命名）
    parsed = urlparse(url)
    domain = parsed.netloc.replace('www.', '')
    url_hash = hashlib.md5(url.encode()).hexdigest()[:6]
    subfolder_name = re.sub(r'[<>:"/\\|?*]', '_', f"{domain}_{url_hash}")
    subfolder = os.path.join(output_dir, subfolder_name)
    os.makedirs(subfolder, exist_ok=True)

    import requests as req
    session = req.Session()
    downloaded_count = 0
    any_success = False

    for idx, item in enumerate(filtered):
        if task['status'] in ('stopping', 'stopped'):
            break

        # 影片類型先嘗試 yt-dlp
        if item.media_type == 'video' and is_ytdlp_supported(item.url):
            add_log(task_id, f'   🎬 [{idx + 1}/{len(filtered)}] 嵌入影片，使用 yt-dlp...')
            success = download_with_ytdlp(task_id, item.url, subfolder, 'best_video', 'best', 'mp3')
            if success:
                downloaded_count += 1
                any_success = True
            continue

        filename = generate_filename(item.url, item.alt, idx)
        output_path = os.path.join(subfolder, filename)
        output_path = get_unique_path(output_path)

        add_log(task_id, f'   ⬇ [{idx + 1}/{len(filtered)}] {filename} (相關度: {item.score})')

        success, result = download_file(item.url, output_path, session)

        if success:
            downloaded_count += 1
            any_success = True
            task['downloaded_files'].append({
                'name': filename,
                'path': output_path,
                'size': result,
            })
            add_log(task_id, f'   ✅ {filename} ({format_size(result)})')
        else:
            add_log(task_id, f'   ❌ 下載失敗: {filename}')

    add_log(task_id, f'   📦 共下載 {downloaded_count}/{len(filtered)} 個文件到 {subfolder_name}/')
    return any_success


def get_unique_path(path):
    """確保文件路徑唯一（避免覆蓋）"""
    if not os.path.exists(path):
        return path

    base, ext = os.path.splitext(path)
    counter = 1
    while os.path.exists(f"{base}_{counter}{ext}"):
        counter += 1
    return f"{base}_{counter}{ext}"


def format_size(size_bytes):
    """格式化文件大小"""
    if not isinstance(size_bytes, (int, float)):
        return '未知大小'
    if size_bytes < 1024:
        return f"{size_bytes} B"
    elif size_bytes < 1024 * 1024:
        return f"{size_bytes / 1024:.1f} KB"
    elif size_bytes < 1024 * 1024 * 1024:
        return f"{size_bytes / (1024 * 1024):.1f} MB"
    else:
        return f"{size_bytes / (1024 * 1024 * 1024):.2f} GB"


# ═══════════════════════════════════════════════════════════════
# 啟動
# ═══════════════════════════════════════════════════════════════

if __name__ == '__main__':
    import io

    # [S7] pythonw.exe sets stdout/stderr to None — fix before any print
    # Source: https://stackoverflow.com/questions/1689015
    if sys.stdout is None:
        sys.stdout = open(os.devnull, 'w', encoding='utf-8')
    if sys.stderr is None:
        sys.stderr = open(os.devnull, 'w', encoding='utf-8')

    try:
        if hasattr(sys.stdout, 'encoding') and sys.stdout.encoding \
                and sys.stdout.encoding.lower() not in ('utf-8', 'utf8'):
            sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')
            sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding='utf-8', errors='replace')
    except Exception:
        pass

    import socket

    def _find_free_port():
        """自動選一個空閒端口。

        避開 5000/7000：macOS 上被 AirPlay Receiver（ControlCenter）佔用，
        寫死 5000 會令 Mac「開唔到」或連到錯誤服務。
        """
        env_port = os.environ.get('FLASK_RUN_PORT')
        candidates = [int(env_port)] if env_port else []
        candidates += list(range(8731, 8771))
        for p in candidates:
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
                s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                try:
                    s.bind(('127.0.0.1', p))
                    return p
                except OSError:
                    continue
        # 最後手段：交由 OS 分配
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.bind(('127.0.0.1', 0))
            return s.getsockname()[1]

    _PORT_FILE = os.path.join(_base_dir, '.run_port')
    _port_file = _PORT_FILE

    def _existing_server_port():
        """若已有實例在跑（.run_port 記錄嘅端口仍回應），回傳該端口，否則 None。
        令重複雙擊啟動變成「只開瀏覽器」而唔係再起一個伺服器。
        """
        try:
            with open(_port_file, encoding='utf-8') as f:
                p = int(f.read().strip())
        except Exception:
            return None
        import urllib.request
        try:
            urllib.request.urlopen(f'http://127.0.0.1:{p}/', timeout=1)
            return p
        except Exception:
            return None

    _running = _existing_server_port()
    if _running:
        import webbrowser
        webbrowser.open(f'http://127.0.0.1:{_running}')
        sys.exit(0)

    port = _find_free_port()
    try:
        with open(_port_file, 'w', encoding='utf-8') as f:
            f.write(str(port))
    except Exception:
        pass

    import atexit
    atexit.register(lambda: os.path.exists(_port_file) and os.remove(_port_file))

    print("=" * 50)
    print("  Universal Downloader v2.0")
    print("=" * 50)
    print("  Server starting...")
    print(f"  Open in browser: http://127.0.0.1:{port}")
    print("  Press Ctrl+C to stop")
    print("=" * 50)

    def _open_browser_when_ready(p):
        """等伺服器就緒後才開啟瀏覽器（避免競態條件）"""
        import urllib.request
        for _ in range(40):
            try:
                urllib.request.urlopen(f'http://127.0.0.1:{p}/', timeout=1)
                break
            except Exception:
                time.sleep(0.5)
        import webbrowser
        webbrowser.open(f'http://127.0.0.1:{p}')

    # 啟動腳本唔再自己開瀏覽器（佢哋唔知道動態端口），由 app.py 自己開
    threading.Thread(target=_open_browser_when_ready, args=(port,),
                     daemon=True).start()

    threading.Thread(target=_auto_shutdown_watcher, daemon=True).start()

    app.run(host='127.0.0.1', port=port, debug=False, threaded=True)

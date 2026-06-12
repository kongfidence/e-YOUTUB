"""
YouTube 下載管理器 — Windows 系統匣應用程式

技術來源：
  [S1] pystray PIL 圖示:            https://pystray.readthedocs.io/en/latest/usage.html
  [S2] pystray 執行緒:              https://pystray.readthedocs.io/en/latest/faq.html
  [S3] pystray + tkinter:           https://stackoverflow.com/questions/54835399
  [S4] yt-dlp extract_info:         https://stackoverflow.com/questions/76594471
  [S5] yt-dlp --download-sections:  https://github.com/yt-dlp/yt-dlp/issues/11124
  [S6] yt-dlp 格式選擇:             https://yt-dlp-yt-dlp.mintlify.app/core-concepts/format-selection
  [S7] pythonw stdout=None:         https://stackoverflow.com/questions/1689015
  [S8] yt-dlp --progress-template:  https://github.com/yt-dlp/yt-dlp/issues/4956
  [S9] tkinter 右下角定位:           https://stackoverflow.com/questions/5365172
  [S10] pystray notify:             https://pystray.readthedocs.io/en/latest/reference.html
  [S11] --force-keyframes-at-cuts:  https://github.com/yt-dlp/yt-dlp/issues/9996
  [S12] yt-dlp progress fields:     https://github.com/yt-dlp/yt-dlp/issues/2684

  [S13] -S 排序代替 -f:       https://github.com/yt-dlp/yt-dlp/issues/11295
  [S14] JS runtime (EJS):     https://github.com/yt-dlp/yt-dlp/wiki/EJS
  [S15] 錯誤擷取:              https://github.com/yt-dlp/yt-dlp/issues/8835

已實測驗證：
  - yt-dlp 2026.03.17 + ffmpeg 6.1.1 + node v22.19.0
  - --js-runtimes node 解決 "No JS runtime" 問題
  - -S "vcodec:h264,res,acodec:aac" 彈性格式選擇
  - --download-sections "*00:00:03-00:00:10" 裁剪成功
  - --progress-template 輸出格式確認可解析
  - 全片 / 片段 / 音訊 三種模式皆可下載
  - 用 qRyOh0FEXqU 實測通過 exit code 0
"""

import json
import os
import platform
import re
import subprocess
import sys
import threading
import time

# --- [S7] pythonw.exe stdout/stderr = None ---
if sys.stdout is None:
    sys.stdout = open(os.devnull, 'w', encoding='utf-8')
if sys.stderr is None:
    sys.stderr = open(os.devnull, 'w', encoding='utf-8')

import tkinter as tk
from tkinter import ttk, filedialog, messagebox


def _ensure_deps():
    missing = []
    try:
        import pystray          # noqa: F401
    except ImportError:
        missing.append('pystray')
    try:
        from PIL import Image   # noqa: F401
    except ImportError:
        missing.append('Pillow')
    try:
        import yt_dlp           # noqa: F401
    except ImportError:
        missing.append('yt-dlp')
    if missing:
        cf = subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0
        subprocess.check_call(
            [sys.executable, '-m', 'pip', 'install', '--quiet'] + missing,
            creationflags=cf,
        )


_ensure_deps()

import pystray                        # [S1][S2][S3]
from PIL import Image, ImageDraw      # [S1]

# ======================================================================
# 設定
# ======================================================================

_BASE = os.path.dirname(os.path.abspath(__file__))
_CFG_PATH = os.path.join(_BASE, 'yt_tray_config.json')

QUALITY_OPTIONS = [
    ('最佳畫質', None),
    ('2160p (4K)', 2160),
    ('1440p (2K)', 1440),
    ('1080p', 1080),
    ('720p', 720),
    ('480p', 480),
    ('360p', 360),
    ('僅音訊 MP3', 'mp3'),
    ('僅音訊 M4A', 'm4a'),
]


def _load_cfg():
    default = {
        'download_dir': os.path.join(os.path.expanduser('~'), 'Downloads'),
        'quality_idx': 0,
    }
    try:
        with open(_CFG_PATH, 'r', encoding='utf-8') as f:
            default.update(json.load(f))
    except Exception:
        pass
    return default


def _save_cfg(cfg):
    try:
        with open(_CFG_PATH, 'w', encoding='utf-8') as f:
            json.dump(cfg, f, ensure_ascii=False, indent=2)
    except Exception:
        pass


# ======================================================================
# 工具函數
# ======================================================================

def _detect_cookie_browser():
    if platform.system() != 'Windows':
        home = os.path.expanduser('~')
        for name, p in [('firefox', f'{home}/.mozilla/firefox'),
                        ('chrome', f'{home}/.config/google-chrome')]:
            if os.path.exists(p):
                return name
        return None
    ad = os.environ.get('APPDATA', '')
    lad = os.environ.get('LOCALAPPDATA', '')
    for name, p in [
        ('firefox', os.path.join(ad, 'Mozilla', 'Firefox', 'Profiles')),
        ('chrome', os.path.join(lad, 'Google', 'Chrome', 'User Data')),
        ('edge', os.path.join(lad, 'Microsoft', 'Edge', 'User Data')),
        ('brave', os.path.join(lad, 'BraveSoftware', 'Brave-Browser', 'User Data')),
    ]:
        if os.path.exists(p):
            return name
    return None


def _parse_time(s):
    """HH:MM:SS / MM:SS / 純秒數 → 秒"""
    if not s or not s.strip():
        return None
    s = s.strip()
    try:
        return float(s)
    except ValueError:
        pass
    parts = s.split(':')
    try:
        if len(parts) == 2:
            return int(parts[0]) * 60 + float(parts[1])
        if len(parts) == 3:
            return int(parts[0]) * 3600 + int(parts[1]) * 60 + float(parts[2])
    except ValueError:
        pass
    return None


def _fmt_time(sec):
    if sec is None:
        return ''
    t = int(sec)
    h, t = divmod(t, 3600)
    m, s = divmod(t, 60)
    return f'{h:02d}:{m:02d}:{s:02d}' if h else f'{m:02d}:{s:02d}'


def _fmt_speed(bps):
    if not bps:
        return ''
    if bps >= 1 << 20:
        return f'{bps / (1 << 20):.1f} MB/s'
    if bps >= 1 << 10:
        return f'{bps / (1 << 10):.0f} KB/s'
    return f'{bps:.0f} B/s'


def _check_ffmpeg():
    try:
        subprocess.run(
            ['ffmpeg', '-version'], capture_output=True,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0,
        )
        return True
    except FileNotFoundError:
        return False


# [S14] 偵測可用嘅 JS runtime（yt-dlp 需要佢嚟解 YouTube JS challenge）
def _detect_js_runtime():
    cf = subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0
    for name in ('deno', 'node', 'bun', 'qjs'):
        try:
            subprocess.run(
                [name, '--version'], capture_output=True, creationflags=cf,
            )
            return name
        except FileNotFoundError:
            continue
    return None


# --- [S1] PIL 記憶體圖示 ---
def _make_icon_image():
    img = Image.new('RGBA', (64, 64), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.rounded_rectangle([2, 12, 62, 52], radius=10, fill='#FF0000')
    d.polygon([(25, 20), (25, 44), (47, 32)], fill='white')
    return img


# ======================================================================
# 下載任務
# ======================================================================

class DownloadTask:
    _counter = 0

    def __init__(self, url, start_time=None, end_time=None):
        DownloadTask._counter += 1
        self.num = DownloadTask._counter
        self.url = url
        self.start_time = start_time
        self.end_time = end_time
        self.title = self._short_url(url)
        self.status = 'pending'
        self.progress = 0.0
        self.speed_str = ''
        self.eta_str = ''
        self.error_msg = ''
        self.proc = None
        self.cancel_event = threading.Event()
        self._streams_done = 0
        self._title_set = False
        self._widget = None

    @staticmethod
    def _short_url(url):
        return url[:50] + '…' if len(url) > 50 else url

    @property
    def time_range_str(self):
        if self.start_time is None and self.end_time is None:
            return '全片'
        s = _fmt_time(self.start_time) if self.start_time is not None else '開頭'
        e = _fmt_time(self.end_time) if self.end_time is not None else '結尾'
        return f'{s} → {e}'


# ======================================================================
# 主程式
# ======================================================================

class App:
    BG = '#1e1e2e'
    BG2 = '#313244'
    BG3 = '#45475a'
    ACCENT = '#f38ba8'
    GREEN = '#a6e3a1'
    YELLOW = '#f9e2af'
    RED = '#f38ba8'
    FG = '#cdd6f4'
    DIM = '#6c7086'

    PROG_TAG = 'YTPROG'  # [S8]

    def __init__(self):
        self.cfg = _load_cfg()
        self.tasks: list[DownloadTask] = []
        self._has_ff = _check_ffmpeg()
        self._js_rt = _detect_js_runtime()  # [S14]

        self.root = tk.Tk()
        self.root.withdraw()             # [S3] 啟動時隱藏
        self._build_ui()
        self.root.protocol('WM_DELETE_WINDOW', self._hide)

        # [S2] pystray daemon thread
        menu = pystray.Menu(
            pystray.MenuItem('打開下載器', self._tray_show, default=True),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem('退出', self._tray_quit),
        )
        self.icon = pystray.Icon('yt_dl', _make_icon_image(),
                                 'YouTube 下載器', menu)
        threading.Thread(target=self.icon.run, daemon=True).start()

        self.root.after(500, self._show)

    # ------------------------------------------------------------------
    # 視窗控制
    # ------------------------------------------------------------------

    def _show(self):
        self.root.deiconify()
        self.root.lift()
        self.root.attributes('-topmost', True)
        self.root.after(100, lambda: self.root.attributes('-topmost', False))
        self.root.focus_force()

    def _hide(self):
        self._save_settings()
        self.root.withdraw()

    def _tray_show(self, _icon=None, _item=None):
        self.root.after(0, self._show)   # [S3] 排到主執行緒

    def _tray_quit(self, _icon=None, _item=None):
        self._save_settings()
        for t in self.tasks:
            t.cancel_event.set()
            if t.proc and t.proc.poll() is None:
                t.proc.terminate()
        self.icon.stop()
        self.root.after(0, self.root.destroy)

    def _save_settings(self):
        d = self.dir_var.get().strip()
        if d:
            self.cfg['download_dir'] = d
        self.cfg['quality_idx'] = self.quality_cb.current()
        _save_cfg(self.cfg)

    # ==================================================================
    # 介面
    # ==================================================================

    def _build_ui(self):
        root = self.root
        root.title('YouTube 下載器')

        W, H = 540, 640
        # 螢幕正中間
        sw = root.winfo_screenwidth()
        sh = root.winfo_screenheight()
        x = (sw - W) // 2
        y = (sh - H) // 2
        root.geometry(f'{W}x{H}+{x}+{y}')
        root.minsize(480, 520)
        root.configure(bg=self.BG)
        root.attributes('-alpha', 0.97)

        s = ttk.Style(root)
        s.theme_use('clam')
        s.configure('TFrame', background=self.BG)
        s.configure('TLabel', background=self.BG, foreground=self.FG,
                    font=('Microsoft JhengHei UI', 10))
        s.configure('H.TLabel', background=self.BG, foreground=self.ACCENT,
                    font=('Microsoft JhengHei UI', 13, 'bold'))
        s.configure('D.TLabel', background=self.BG, foreground=self.DIM,
                    font=('Microsoft JhengHei UI', 9))
        s.configure('TButton', font=('Microsoft JhengHei UI', 9), padding=4)
        s.configure('A.TButton', font=('Microsoft JhengHei UI', 10, 'bold'),
                    padding=6)
        s.configure('TLabelframe', background=self.BG, foreground=self.FG)
        s.configure('TLabelframe.Label', background=self.BG,
                    foreground=self.ACCENT,
                    font=('Microsoft JhengHei UI', 10, 'bold'))
        s.configure('R.Horizontal.TProgressbar',
                    troughcolor=self.BG2, background=self.ACCENT)
        s.configure('G.Horizontal.TProgressbar',
                    troughcolor=self.BG2, background=self.GREEN)
        s.map('TButton',
              background=[('active', self.ACCENT), ('!active', self.BG3)],
              foreground=[('active', 'white'), ('!active', self.FG)])
        s.map('A.TButton',
              background=[('active', '#ff6b81'), ('!active', self.ACCENT)],
              foreground=[('active', 'white'), ('!active', 'white')])

        main = ttk.Frame(root, padding=12)
        main.pack(fill='both', expand=True)

        ttk.Label(main, text='▶ YouTube 下載器',
                  style='H.TLabel').pack(anchor='w', pady=(0, 8))

        # --- 設定 ---
        sf = ttk.LabelFrame(main, text=' ⚙ 設定 ', padding=8)
        sf.pack(fill='x', pady=(0, 8))

        qr = ttk.Frame(sf)
        qr.pack(fill='x', pady=(0, 4))
        ttk.Label(qr, text='清晰度:').pack(side='left')
        self.quality_var = tk.StringVar()
        self.quality_cb = ttk.Combobox(
            qr, textvariable=self.quality_var, state='readonly',
            font=('Microsoft JhengHei UI', 10), width=22)
        self.quality_cb['values'] = [q[0] for q in QUALITY_OPTIONS]
        idx = self.cfg.get('quality_idx', 0)
        self.quality_cb.current(
            idx if 0 <= idx < len(QUALITY_OPTIONS) else 0)
        self.quality_cb.pack(side='left', padx=(8, 0), fill='x', expand=True)

        dr = ttk.Frame(sf)
        dr.pack(fill='x')
        ttk.Label(dr, text='位置:').pack(side='left')
        self.dir_var = tk.StringVar(
            value=self.cfg.get('download_dir', ''))
        self._dir_entry = tk.Entry(
            dr, textvariable=self.dir_var, font=('Consolas', 9),
            bg=self.BG2, fg=self.FG, insertbackground=self.FG, relief='flat',
            highlightthickness=1, highlightcolor=self.ACCENT,
            highlightbackground='#444')
        self._dir_entry.pack(side='left', fill='x', expand=True,
                             ipady=2, padx=(8, 4))
        ttk.Button(dr, text='📁', width=3,
                   command=self._browse).pack(side='right')

        if not self._has_ff:
            ttk.Label(
                sf,
                text='⚠ 未偵測到 ffmpeg — 裁剪/合併功能可能無法使用',
                foreground='#fab387', background=self.BG,
                font=('Microsoft JhengHei UI', 9),
            ).pack(anchor='w', pady=(4, 0))

        if not self._js_rt:
            ttk.Label(
                sf,
                text='⚠ 未偵測到 JS runtime (deno/node) — YouTube 下載可能失敗',
                foreground='#f38ba8', background=self.BG,
                font=('Microsoft JhengHei UI', 9),
            ).pack(anchor='w', pady=(4, 0))

        # --- 新增下載 ---
        nf = ttk.LabelFrame(main, text=' ＋ 新增下載 ', padding=8)
        nf.pack(fill='x', pady=(0, 8))

        ur = ttk.Frame(nf)
        ur.pack(fill='x', pady=(0, 4))
        self.url_var = tk.StringVar()
        url_entry = tk.Entry(
            ur, textvariable=self.url_var, font=('Consolas', 10),
            bg=self.BG2, fg=self.FG, insertbackground=self.FG, relief='flat',
            highlightthickness=1, highlightcolor=self.ACCENT,
            highlightbackground='#444')
        url_entry.pack(side='left', fill='x', expand=True, ipady=3,
                       padx=(0, 4))
        url_entry.bind('<Return>', lambda _: self._add_task())
        ttk.Button(ur, text='貼上', command=self._paste).pack(side='right')

        tr = ttk.Frame(nf)
        tr.pack(fill='x')
        ttk.Label(tr, text='開始:').pack(side='left')
        self.start_var = tk.StringVar()
        tk.Entry(
            tr, textvariable=self.start_var, width=10,
            font=('Consolas', 10), bg=self.BG2, fg=self.FG,
            insertbackground=self.FG, relief='flat',
            highlightthickness=1, highlightcolor=self.ACCENT,
            highlightbackground='#444',
        ).pack(side='left', padx=(4, 14), ipady=2)
        ttk.Label(tr, text='結束:').pack(side='left')
        self.end_var = tk.StringVar()
        tk.Entry(
            tr, textvariable=self.end_var, width=10,
            font=('Consolas', 10), bg=self.BG2, fg=self.FG,
            insertbackground=self.FG, relief='flat',
            highlightthickness=1, highlightcolor=self.ACCENT,
            highlightbackground='#444',
        ).pack(side='left', padx=(4, 14), ipady=2)
        ttk.Button(tr, text='＋ 新增', style='A.TButton',
                   command=self._add_task).pack(side='right')

        ttk.Label(
            nf,
            text='時間格式: HH:MM:SS / MM:SS / 秒數（留空 = 下載全片）',
            style='D.TLabel',
        ).pack(anchor='w', pady=(4, 0))

        # --- 下載列表 ---
        lf = ttk.LabelFrame(main, text=' 📋 下載列表 ', padding=4)
        lf.pack(fill='both', expand=True, pady=(0, 8))

        self.task_canvas = tk.Canvas(
            lf, bg=self.BG, highlightthickness=0)
        self._scrollbar = ttk.Scrollbar(
            lf, orient='vertical', command=self.task_canvas.yview)
        self.task_inner = ttk.Frame(self.task_canvas)

        self.task_inner.bind(
            '<Configure>',
            lambda _: self.task_canvas.configure(
                scrollregion=self.task_canvas.bbox('all')))
        self._canvas_win = self.task_canvas.create_window(
            (0, 0), window=self.task_inner, anchor='nw')
        self.task_canvas.configure(yscrollcommand=self._scrollbar.set)

        self.task_canvas.pack(side='left', fill='both', expand=True)
        self._scrollbar.pack(side='right', fill='y')

        self.task_canvas.bind(
            '<Configure>',
            lambda e: self.task_canvas.itemconfigure(
                self._canvas_win, width=e.width))
        self.task_canvas.bind(
            '<MouseWheel>',
            lambda e: self.task_canvas.yview_scroll(
                -1 * (e.delta // 120), 'units'))

        self._empty_lbl = ttk.Label(
            self.task_inner,
            text='尚無下載任務\n貼上連結並點擊「＋ 新增」開始',
            style='D.TLabel', justify='center')
        self._empty_lbl.pack(pady=30)
        self._empty_visible = True

        # --- 底部按鈕 ---
        bf = ttk.Frame(main)
        bf.pack(fill='x')
        ttk.Button(bf, text='開啟下載資料夾',
                   command=self._open_dir).pack(side='left')
        ttk.Button(bf, text='清除已完成',
                   command=self._clear_done).pack(side='right')

    # ==================================================================
    # 使用者動作
    # ==================================================================

    def _paste(self):
        try:
            self.url_var.set(self.root.clipboard_get().strip())
        except Exception:
            pass

    def _browse(self):
        d = filedialog.askdirectory(initialdir=self.dir_var.get() or None)
        if d:
            self.dir_var.set(d)
            self.cfg['download_dir'] = d
            _save_cfg(self.cfg)

    def _open_dir(self):
        d = self.dir_var.get().strip()
        if d and os.path.isdir(d):
            os.startfile(d)
        else:
            messagebox.showinfo('提示', '資料夾不存在或尚未設定')

    def _add_task(self):
        url = self.url_var.get().strip()
        if not url:
            messagebox.showwarning('提示', '請先輸入影片連結')
            return
        out = self.dir_var.get().strip()
        if not out:
            messagebox.showwarning('提示', '請先設定下載位置')
            return

        ss = _parse_time(self.start_var.get())
        ee = _parse_time(self.end_var.get())
        task = DownloadTask(url, ss, ee)
        self.tasks.append(task)

        self.url_var.set('')
        self.start_var.set('')
        self.end_var.set('')

        self._build_task_widget(task)
        self._save_settings()
        self._try_start_next()

    # ==================================================================
    # 任務列表 UI
    # ==================================================================

    def _build_task_widget(self, task):
        if self._empty_visible:
            self._empty_lbl.destroy()
            self._empty_visible = False

        frame = tk.Frame(self.task_inner, bg=self.BG2, padx=8, pady=6)
        frame.pack(fill='x', pady=(0, 3), padx=2)

        # 第一行：標題 + 時間範圍 + 取消
        r1 = tk.Frame(frame, bg=self.BG2)
        r1.pack(fill='x')

        title_lbl = tk.Label(
            r1, text=f'#{task.num}  {task.title}',
            bg=self.BG2, fg=self.FG,
            font=('Microsoft JhengHei UI', 9), anchor='w')
        title_lbl.pack(side='left', fill='x', expand=True)

        time_lbl = tk.Label(
            r1, text=task.time_range_str, bg=self.BG2, fg=self.DIM,
            font=('Consolas', 8))
        time_lbl.pack(side='left', padx=(8, 4))

        cancel_btn = tk.Button(
            r1, text='✕', bg=self.BG2, fg=self.RED,
            font=('Consolas', 9, 'bold'), relief='flat', bd=0,
            activebackground=self.BG3, activeforeground='#fff',
            command=lambda t=task: self._cancel_task(t))
        cancel_btn.pack(side='right')

        # 第二行：進度條 + 狀態
        r2 = tk.Frame(frame, bg=self.BG2)
        r2.pack(fill='x', pady=(3, 0))

        pbar = ttk.Progressbar(
            r2, maximum=100, style='R.Horizontal.TProgressbar')
        pbar.pack(side='left', fill='x', expand=True, padx=(0, 8))

        status_lbl = tk.Label(
            r2, text='等待中', bg=self.BG2, fg=self.DIM,
            font=('Consolas', 8), width=28, anchor='e')
        status_lbl.pack(side='right')

        task._widget = {
            'frame': frame,
            'title': title_lbl,
            'time': time_lbl,
            'cancel': cancel_btn,
            'pbar': pbar,
            'status': status_lbl,
        }

        self.task_inner.update_idletasks()
        self.task_canvas.configure(
            scrollregion=self.task_canvas.bbox('all'))
        self.task_canvas.yview_moveto(1.0)

    def _update_task_ui(self, task):
        w = task._widget
        if not w:
            return

        w['title'].config(text=f'#{task.num}  {task.title}')
        w['pbar']['value'] = task.progress

        if task.status == 'pending':
            w['status'].config(text='等待中', fg=self.DIM)
        elif task.status == 'downloading':
            parts = [f'{task.progress:.1f}%']
            if task.speed_str:
                parts.append(task.speed_str)
            if task.eta_str:
                parts.append(f'剩餘 {task.eta_str}')
            w['status'].config(text='  '.join(parts), fg=self.YELLOW)
        elif task.status == 'merging':
            w['status'].config(text='合併中…', fg=self.YELLOW)
        elif task.status == 'complete':
            w['pbar'].configure(style='G.Horizontal.TProgressbar')
            w['status'].config(text='✓ 完成', fg=self.GREEN)
            w['cancel'].config(state='disabled')
        elif task.status == 'error':
            err = task.error_msg[:30] if task.error_msg else '未知錯誤'
            w['status'].config(text=f'✕ {err}', fg=self.RED)
        elif task.status == 'cancelled':
            w['status'].config(text='已取消', fg=self.DIM)

    def _cancel_task(self, task):
        if task.status in ('pending', 'downloading', 'merging'):
            task.cancel_event.set()
            if task.proc and task.proc.poll() is None:
                task.proc.terminate()
            task.status = 'cancelled'
            self._update_task_ui(task)
            self._try_start_next()

    def _clear_done(self):
        remove = [t for t in self.tasks
                  if t.status in ('complete', 'error', 'cancelled')]
        for t in remove:
            self.tasks.remove(t)
            if t._widget:
                t._widget['frame'].destroy()

        if not self.tasks and not self._empty_visible:
            self._empty_lbl = ttk.Label(
                self.task_inner,
                text='尚無下載任務\n貼上連結並點擊「＋ 新增」開始',
                style='D.TLabel', justify='center')
            self._empty_lbl.pack(pady=30)
            self._empty_visible = True

        self.task_inner.update_idletasks()
        self.task_canvas.configure(
            scrollregion=self.task_canvas.bbox('all'))

    # ==================================================================
    # 下載引擎
    # ==================================================================

    def _try_start_next(self):
        active = any(t.status in ('downloading', 'merging')
                     for t in self.tasks)
        if active:
            return
        for task in self.tasks:
            if task.status == 'pending':
                task.status = 'downloading'
                self.root.after(0, lambda t=task: self._update_task_ui(t))
                threading.Thread(
                    target=self._run_download, args=(task,),
                    daemon=True).start()
                return

    def _build_cmd(self, task, use_cookies=False):
        """組建 yt-dlp subprocess 指令 [S5][S8][S11][S13][S14]

        use_cookies=False 時唔加 --cookies-from-browser，避免瀏覽器鎖住
        cookie DB（Chrome 開住會 lock）令下載直接失敗。需要登入嘅影片
        會喺第一次失敗後帶 cookie 重試。
        """
        out = self.dir_var.get().strip()
        os.makedirs(out, exist_ok=True)

        qi = self.quality_cb.current()
        qval = QUALITY_OPTIONS[qi][1] if 0 <= qi < len(QUALITY_OPTIONS) else None

        cmd = [
            sys.executable, '-m', 'yt_dlp',
            '--no-playlist', '--newline', '--no-colors',
            '--progress-template',
            f'download:{self.PROG_TAG}|'
            '%(progress.status)s|'
            '%(progress.downloaded_bytes)s|'
            '%(progress.total_bytes)s|'
            '%(progress.speed)s|'
            '%(progress.eta)s',
        ]

        # [S14] JS runtime — YouTube 需要佢解 JS challenge
        if self._js_rt:
            rt = self._js_rt
            if rt == 'qjs':
                rt = 'quickjs'
            cmd += ['--js-runtimes', rt]

        if use_cookies:
            cb = _detect_cookie_browser()
            if cb:
                cmd += ['--cookies-from-browser', cb]

        # [S13] 用 -S 排序代替 -f 過濾，避免格式唔存在時 exit code 1
        if qval == 'mp3':
            cmd += ['-x', '--audio-format', 'mp3']
        elif qval == 'm4a':
            cmd += ['-x', '--audio-format', 'm4a']
        elif isinstance(qval, int):
            cmd += ['-S', f'vcodec:h264,res:{qval},acodec:aac']
        else:
            cmd += ['-S', 'vcodec:h264,res,acodec:aac']

        # [S5][S11] 時間裁剪
        ss, ee = task.start_time, task.end_time
        if ss is not None or ee is not None:
            s_str = _fmt_time(ss) if ss is not None else '00:00:00'
            e_str = _fmt_time(ee) if ee is not None else 'inf'
            cmd += ['--download-sections', f'*{s_str}-{e_str}',
                    '--force-keyframes-at-cuts']

        cmd += ['-o', os.path.join(out, '%(title)s.%(ext)s'),
                '--no-overwrites', task.url]
        return cmd

    def _run_once(self, task, cmd):
        """跑一次 yt-dlp subprocess 並解析進度 [S8][S12][S15]。

        回傳 (rc, last_error)；rc 為 None 表示被使用者取消。
        """
        cf = subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0

        # [SO#76750006] 強制子進程用 UTF-8 輸出，避免 CP950 亂碼
        env = os.environ.copy()
        env['PYTHONIOENCODING'] = 'utf-8'

        try:
            task.proc = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True, encoding='utf-8', errors='replace',
                env=env,
                creationflags=cf,
            )
        except Exception as exc:
            return -1, str(exc)

        last_error = ''

        for line in task.proc.stdout:
            if task.cancel_event.is_set():
                task.proc.terminate()
                return None, ''

            line = line.strip()
            if not line:
                continue

            # [S15] 擷取 ERROR 行作為錯誤訊息
            if 'ERROR:' in line:
                last_error = re.sub(r'^.*?ERROR:\s*', '', line)[:200]

            # [S8] 自訂 progress template 解析
            if line.startswith(f'{self.PROG_TAG}|'):
                self._parse_progress_line(task, line)
                continue

            # 從 [download] Destination 行抽取標題
            if 'Destination:' in line and not task._title_set:
                m = re.search(r'Destination:\s+(.+)', line)
                if m:
                    name = os.path.basename(m.group(1).strip())
                    name = re.sub(r'\.f\d+\.\w+$', '', name)
                    name = re.sub(r'\.\w+$', '', name)
                    name = name.replace('.part', '')
                    if name:
                        task.title = name[:60]
                        task._title_set = True
                        self.root.after(
                            0, lambda t=task: self._update_task_ui(t))
                continue

            if '[Merger]' in line or 'Merging formats' in line:
                task.status = 'merging'
                task.progress = 95
                self.root.after(
                    0, lambda t=task: self._update_task_ui(t))
                continue

            # 後備：解析標準 [download] XX% 行
            pm = re.search(r'\[download\]\s+([\d.]+)%', line)
            if pm:
                pct = float(pm.group(1))
                if task._streams_done == 0:
                    task.progress = pct * 0.5
                else:
                    task.progress = 50 + pct * 0.4
                self.root.after(
                    0, lambda t=task: self._update_task_ui(t))

        task.proc.wait()
        rc = task.proc.returncode
        task.proc = None
        return rc, last_error

    def _run_download(self, task):
        """背景執行緒：先唔帶 cookie 跑，失敗先帶瀏覽器 cookie 重試一次。"""
        rc, last_error = self._run_once(task, self._build_cmd(task, use_cookies=False))

        # 第一次失敗 + 未取消 + 有可用瀏覽器 → 帶 cookie 重試
        if (rc is not None and rc != 0 and not task.cancel_event.is_set()
                and _detect_cookie_browser()):
            task._streams_done = 0
            task.progress = 0.0
            task.status = 'downloading'
            self.root.after(0, lambda t=task: self._update_task_ui(t))
            rc, last_error = self._run_once(task, self._build_cmd(task, use_cookies=True))

        if task.cancel_event.is_set() or rc is None:
            task.status = 'cancelled'
        elif rc == 0:
            task.status = 'complete'
            task.progress = 100.0
        else:
            task.status = 'error'
            task.error_msg = last_error or f'yt-dlp 退出碼 {rc}'

        self.root.after(0, lambda: self._on_task_done(task))

    def _parse_progress_line(self, task, line):
        """解析 YTPROG|status|dl_bytes|total|speed|eta"""
        parts = line.split('|')
        if len(parts) < 6:
            return

        status = parts[1]
        dl_bytes = self._float(parts[2])
        total = self._float(parts[3])
        speed = self._float(parts[4])
        eta = self._float(parts[5])

        if status == 'finished':
            task._streams_done += 1
            if task._streams_done == 1:
                task.progress = 50
            else:
                task.progress = 90
            task.speed_str = ''
            task.eta_str = ''
        elif total and total > 0 and dl_bytes is not None:
            stream_pct = min(100, dl_bytes / total * 100)
            if task._streams_done == 0:
                task.progress = stream_pct * 0.5
            else:
                task.progress = 50 + stream_pct * 0.4

        if speed:
            task.speed_str = _fmt_speed(speed)
        if eta is not None and eta > 0:
            task.eta_str = _fmt_time(eta)
        elif status == 'finished':
            task.eta_str = ''

        self.root.after(0, lambda t=task: self._update_task_ui(t))

    def _on_task_done(self, task):
        self._update_task_ui(task)

        # [S10] 系統通知
        try:
            if task.status == 'complete':
                self.icon.notify(
                    f'✓ {task.title}', 'YouTube 下載器 — 完成')
            elif task.status == 'error':
                self.icon.notify(
                    f'✕ {task.title}\n{task.error_msg}',
                    'YouTube 下載器 — 失敗')
        except Exception:
            pass

        self._try_start_next()

    @staticmethod
    def _float(s):
        try:
            v = float(s)
            return v if v == v else None
        except (ValueError, TypeError):
            return None

    # ==================================================================
    def run(self):
        self.root.mainloop()


# ======================================================================
if __name__ == '__main__':
    App().run()

"""
萬能下載器 - 網頁爬蟲與媒體提取模組
使用啟發式方法（無AI）判斷網頁中的相關圖片和影片
"""

import requests
import urllib3
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

from bs4 import BeautifulSoup, Tag
from urllib.parse import urljoin, urlparse, unquote
import re
import os
from io import BytesIO
import logging
import hashlib
import time

logger = logging.getLogger(__name__)

# 多組 User-Agent 輪換（模擬不同瀏覽器）
USER_AGENTS = [
    'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36',
    'Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:123.0) Gecko/20100101 Firefox/123.0',
    'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.3 Safari/605.1.15',
    'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36 Edg/122.0.0.0',
    'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36',
]

import random

def _get_headers(referer=None, ua_index=0):
    """動態生成請求頭"""
    headers = {
        'User-Agent': USER_AGENTS[ua_index % len(USER_AGENTS)],
        'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8',
        'Accept-Language': 'en-US,en;q=0.9,zh-TW;q=0.8,zh;q=0.7,ja;q=0.6',
        'Accept-Encoding': 'gzip, deflate, br',
        'DNT': '1',
        'Connection': 'keep-alive',
        'Upgrade-Insecure-Requests': '1',
        'Sec-Fetch-Dest': 'document',
        'Sec-Fetch-Mode': 'navigate',
        'Sec-Fetch-Site': 'none',
        'Sec-Fetch-User': '?1',
        'Cache-Control': 'max-age=0',
        'Sec-Ch-Ua': '"Chromium";v="122", "Not(A:Brand";v="24", "Google Chrome";v="122"',
        'Sec-Ch-Ua-Mobile': '?0',
        'Sec-Ch-Ua-Platform': '"Windows"',
    }
    if referer:
        headers['Referer'] = referer
        headers['Sec-Fetch-Site'] = 'same-origin'
    return headers

# 保持向後兼容
HEADERS = _get_headers()

# 非內容圖片的 URL 模式（負面信號）
NEGATIVE_URL_PATTERNS = [
    r'logo', r'icon', r'favicon', r'sprite', r'avatar',
    r'badge', r'button', r'arrow', r'social[\-_]', r'share[\-_]',
    r'comment', r'author[\-_]', r'profile[\-_]', r'gravatar',
    r'emoji', r'smiley', r'ad[\-_]', r'ads[\-_]', r'advert',
    r'tracking', r'pixel', r'spacer', r'blank',
    r'widget[\-_]', r'sidebar[\-_]', r'related[\-_]',
    r'footer[\-_]', r'header[\-_]logo', r'nav[\-_]',
    r'1x1', r'2x2', r'transparent\.', r'placeholder',
    r'loading[\-_]', r'spinner', r'loader',
    r'placeholder', r'default[\-_]image', r'no[\-_]image',
    r'thumb[\-_]default', r'missing', r'fallback',
    r'lazy[\-_]placeholder', r'grey\.', r'gray\.',
]

# 主內容區域的 CSS 選擇器
MAIN_CONTENT_SELECTORS = [
    'article', 'main', '[role="main"]',
    '.post-content', '.article-content', '.entry-content',
    '.post-body', '.article-body', '.story-body',
    '.content-area', '.main-content', '.page-content',
    '#content', '#main-content', '#article-content',
    '.td-post-content', '.post_content',
    '.single-content', '.blog-content',
    '.hentry', '.post', '.entry',
]

# 非內容區域選擇器
NON_CONTENT_SELECTORS = [
    'header', 'footer', 'nav', 'aside',
    '.sidebar', '.widget', '.nav', '.menu',
    '.footer', '.header', '.breadcrumb',
    '.related', '.recommended', '.advertisement',
    '.social', '.share', '.comment', '.comments',
    '#sidebar', '#footer', '#header', '#nav',
    '.ad', '.ads', '.advert',
]

# 常見直接媒體文件擴展名
IMAGE_EXTENSIONS = {'.jpg', '.jpeg', '.png', '.webp', '.bmp', '.tiff', '.tif', '.avif'}
VIDEO_EXTENSIONS = {'.mp4', '.webm', '.mkv', '.avi', '.mov', '.flv', '.wmv', '.m4v', '.mpg', '.mpeg', '.3gp'}
AUDIO_EXTENSIONS = {'.mp3', '.m4a', '.wav', '.flac', '.ogg', '.aac', '.wma', '.opus'}

# yt-dlp 已知支援的域名
YTDLP_DOMAINS = [
    'youtube.com', 'youtu.be', 'vimeo.com', 'dailymotion.com',
    'twitter.com', 'x.com', 'instagram.com', 'facebook.com',
    'fb.watch', 'tiktok.com', 'bilibili.com', 'b23.tv',
    'twitch.tv', 'soundcloud.com', 'mixcloud.com',
    'bandcamp.com', 'reddit.com', 'v.redd.it',
    'streamable.com', 'nicovideo.jp', 'niconico.com',
    'ted.com', 'rumble.com', 'odysee.com', 'bitchute.com',
    'drive.google.com', 'dropbox.com', 'pinterest.com',
    'flickr.com', 'tumblr.com', 'vine.co',
    'coub.com', 'rutube.ru', 'ok.ru',
    'weibo.com', 'douyin.com', 'kuaishou.com',
    'xiaohongshu.com', 'xhs.com',
]

# 最小圖片尺寸
MIN_WIDTH = 200
MIN_HEIGHT = 150


class MediaItem:
    """表示網頁中找到的媒體項目"""

    def __init__(self, url, media_type, score=0, alt='', context='', source_url=''):
        self.url = url
        self.media_type = media_type  # 'image' / 'video'
        self.score = score
        self.alt = alt
        self.context = context
        self.source_url = source_url
        self.filename = None
        self.size = None  # (width, height) 或檔案大小

    def to_dict(self):
        return {
            'url': self.url,
            'type': self.media_type,
            'score': self.score,
            'alt': self.alt,
            'context': self.context,
            'filename': self.filename,
            'size': self.size,
        }

    def __repr__(self):
        return f"MediaItem({self.media_type}, score={self.score}, url={self.url[:60]})"


def extract_urls_from_text(text):
    """從任意文字中提取所有 URL"""
    url_pattern = re.compile(
        r'https?://[^\s<>"\')\]},;\u3000\u3001\u3002\uff0c\uff01\uff1f\uff1b\u300a\u300b\u3010\u3011]+'
    )
    urls = url_pattern.findall(text)

    cleaned = []
    for url in urls:
        # 移除尾端標點
        url = url.rstrip('.,;:!?)]\'">')
        # 移除尾端的特殊字元
        url = re.sub(r'["\'>)\]]+$', '', url)
        if url and url.startswith('http'):
            cleaned.append(url)

    return list(dict.fromkeys(cleaned))  # 去重但保持順序


def is_ytdlp_supported(url):
    """檢查 URL 是否為 yt-dlp 已知支援的網站"""
    try:
        domain = urlparse(url).netloc.lower()
        for known in YTDLP_DOMAINS:
            if known in domain:
                return True
    except:
        pass
    return False


def is_direct_media_url(url):
    """檢查 URL 是否直接指向媒體文件"""
    try:
        path = urlparse(url).path.lower()
        ext = os.path.splitext(path)[1]
        if ext in IMAGE_EXTENSIONS:
            return True, 'image'
        if ext in VIDEO_EXTENSIONS:
            return True, 'video'
        if ext in AUDIO_EXTENSIONS:
            return True, 'audio'
    except:
        pass
    return False, None


def fetch_page(url, session=None, retries=3):
    """獲取網頁內容，支援多種反封鎖策略"""
    if session is None:
        session = requests.Session()

    parsed = urlparse(url)
    domain = parsed.netloc
    base_url = f"{parsed.scheme}://{domain}"

    # 策略列表：逐一嘗試
    strategies = [
        # 策略 1: 標準 Chrome headers
        lambda: session.get(url, headers=_get_headers(ua_index=0), timeout=30,
                           allow_redirects=True, verify=True),
        # 策略 2: 帶 Referer (模擬從首頁點入)
        lambda: session.get(url, headers=_get_headers(referer=base_url, ua_index=0), timeout=30,
                           allow_redirects=True, verify=True),
        # 策略 3: Firefox UA
        lambda: session.get(url, headers=_get_headers(ua_index=1), timeout=30,
                           allow_redirects=True, verify=True),
        # 策略 4: 先訪問首頁取 cookies，再訪問目標頁
        lambda: _fetch_with_cookie_warmup(session, url, base_url),
        # 策略 5: Safari UA + 不同 Accept-Language
        lambda: session.get(url, headers={
            **_get_headers(ua_index=2),
            'Accept-Language': 'en-US,en;q=0.5',
        }, timeout=30, allow_redirects=True, verify=True),
        # 策略 6: Edge UA + Referer=Google
        lambda: session.get(url, headers={
            **_get_headers(referer='https://www.google.com/', ua_index=3),
            'Sec-Fetch-Site': 'cross-site',
        }, timeout=30, allow_redirects=True, verify=True),
        # 策略 7: 禁用 SSL 驗證 (某些網站證書問題)
        lambda: session.get(url, headers=_get_headers(ua_index=0), timeout=30,
                           allow_redirects=True, verify=False),
    ]

    for i, strategy in enumerate(strategies):
        try:
            response = strategy()
            if response is not None:
                response.raise_for_status()
                # 自動偵測編碼
                if response.encoding and response.encoding.lower() in ('iso-8859-1', 'windows-1252'):
                    response.encoding = response.apparent_encoding
                return response
        except requests.exceptions.HTTPError as e:
            status = e.response.status_code if e.response is not None else 0
            logger.info(f"Strategy {i+1} got HTTP {status} for {url}")
            if status == 429:  # Too Many Requests
                time.sleep(3)
            elif status >= 500:
                time.sleep(1)
            continue
        except requests.exceptions.SSLError:
            continue
        except requests.exceptions.ConnectionError:
            time.sleep(1)
            continue
        except requests.exceptions.Timeout:
            continue
        except Exception as e:
            logger.warning(f"Strategy {i+1} failed for {url}: {e}")
            continue

    return None


def _fetch_with_cookie_warmup(session, target_url, base_url):
    """先訪問首頁取得 cookies，再訪問目標頁面"""
    try:
        # 先訪問首頁
        session.get(base_url, headers=_get_headers(ua_index=0), timeout=15,
                   allow_redirects=True, verify=True)
        time.sleep(0.5)
        # 再訪問目標
        return session.get(target_url,
                          headers=_get_headers(referer=base_url, ua_index=0),
                          timeout=30, allow_redirects=True, verify=True)
    except:
        return None


def find_main_content(soup):
    """找到網頁的主要內容區域"""
    for selector in MAIN_CONTENT_SELECTORS:
        content = soup.select_one(selector)
        if content:
            # 驗證它有足夠的文字內容
            text_len = len(content.get_text(strip=True))
            if text_len > 100:
                return content

    # 回退：找最大的含文字 div
    divs = soup.find_all('div')
    best_div = None
    max_text_length = 0
    for div in divs:
        text_length = len(div.get_text(strip=True))
        if text_length > max_text_length:
            if div.parent and div.parent.name != '[document]':
                # 排除太靠近 body 的大 div
                depth = 0
                p = div
                while p and depth < 20:
                    p = p.parent
                    depth += 1
                if depth >= 3:
                    max_text_length = text_length
                    best_div = div

    return best_div


def is_in_non_content_area(element):
    """檢查元素是否在非內容區域中"""
    current = element.parent
    depth = 0
    while current and depth < 15:
        if isinstance(current, Tag):
            tag_name = current.name or ''
            classes = ' '.join(current.get('class', [])).lower()
            tag_id = (current.get('id', '') or '').lower()

            if tag_name in ('header', 'footer', 'nav', 'aside'):
                return True

            non_content_keywords = [
                'sidebar', 'widget', 'nav', 'menu', 'footer', 'header',
                'breadcrumb', 'related', 'recommend', 'advert',
                'social', 'share', 'comment', 'ad-container',
            ]
            for kw in non_content_keywords:
                if kw in classes or kw in tag_id:
                    return True

        current = current.parent if hasattr(current, 'parent') else None
        depth += 1
    return False


def is_in_main_content(element, main_content):
    """檢查元素是否在主要內容區域內"""
    if main_content is None:
        return True  # 找不到主內容區就不限制
    current = element
    depth = 0
    while current and depth < 30:
        if current == main_content:
            return True
        current = current.parent if hasattr(current, 'parent') else None
        depth += 1
    return False


def score_image_element(img, main_content, page_url):
    """對圖片元素進行相關性評分，分數越高越相關"""
    score = 0
    src = (
        img.get('src', '') or
        img.get('data-src', '') or
        img.get('data-lazy-src', '') or
        img.get('data-original', '') or
        img.get('data-lazy', '') or
        img.get('data-url', '') or ''
    )

    # 過濾 base64 和 SVG 小圖片
    if not src:
        return -100, ''
    if src.startswith('data:image/svg'):
        return -100, ''
    if src.startswith('data:image/'):
        # 允許大型 base64 圖片 (>1KB)，過濾 1x1 pixel 等佔位符
        if len(src) < 1500:
            return -100, ''

    src = urljoin(page_url, src)
    alt = (img.get('alt', '') or '').strip()
    title_attr = (img.get('title', '') or '').strip()
    classes = ' '.join(img.get('class', [])).lower()

    # 檢查負面 URL 模式
    src_lower = src.lower()
    for pattern in NEGATIVE_URL_PATTERNS:
        if re.search(pattern, src_lower):
            score -= 30

    # 檢查是否在主內容中
    if main_content:
        if is_in_main_content(img, main_content):
            score += 40
        else:
            score -= 20

    # 檢查是否在非內容區域
    if is_in_non_content_area(img):
        score -= 50

    # 從屬性判斷大小
    width = img.get('width')
    height = img.get('height')
    if width and height:
        try:
            w = int(re.sub(r'[^\d]', '', str(width)))
            h = int(re.sub(r'[^\d]', '', str(height)))
            if w >= 600 and h >= 400:
                score += 30
            elif w >= 300 and h >= 200:
                score += 15
            elif w < 80 or h < 80:
                score -= 40
            elif w < 50 or h < 50:
                score -= 60
        except:
            pass

    # 有意義的 alt 文字
    if alt and len(alt) > 10:
        score += 10

    # CSS class 正面信號
    content_classes = ['featured', 'hero', 'main-image', 'content', 'article',
                       'post-image', 'full', 'photo', 'gallery', 'wp-image']
    for cls in content_classes:
        if cls in classes:
            score += 15
            break

    # CSS class 負面信號
    bad_classes = ['logo', 'icon', 'avatar', 'thumb-small', 'tiny', 'ad']
    for cls in bad_classes:
        if cls in classes:
            score -= 25
            break

    # SVG 和小 GIF 通常是裝飾性的
    if src_lower.endswith('.svg'):
        score -= 40
    if src_lower.endswith('.gif') and not ('content' in src_lower or 'upload' in src_lower):
        score -= 10

    # Data URI（base64 圖片）通常是小圖
    if src.startswith('data:'):
        score -= 30

    # 內容性 URL 模式
    content_url_patterns = ['content', 'article', 'post', 'upload', 'media',
                            'image', 'photo', 'pic', 'wp-content']
    for pattern in content_url_patterns:
        if pattern in src_lower:
            score += 10
            break

    return score, src


def _is_generic_og_image(url):
    """判斷是否為通用/預設的 OG 圖片"""
    url_lower = url.lower()
    generic_patterns = [
        'default', 'og_image', 'ogimage', 'og-image',
        'facebook_share', 'social_share', 'share_image',
        'defaultpromocrop', 'default_logo', 'yahoo_default',
        'site-logo', 'website-logo', 'brand-logo',
        'default-thumbnail', 'no-image', 'no_image',
    ]
    for pattern in generic_patterns:
        if pattern in url_lower:
            return True
    return False


def extract_og_images(soup, page_url):
    """提取 Open Graph 和 Twitter Card 圖片（過濾通用預設圖片）"""
    images = []

    og_image = soup.find('meta', property='og:image')
    if og_image and og_image.get('content'):
        img_url = urljoin(page_url, og_image['content'])
        if not _is_generic_og_image(img_url):
            images.append(img_url)

    twitter_image = soup.find('meta', attrs={'name': 'twitter:image'})
    if twitter_image and twitter_image.get('content'):
        img_url = urljoin(page_url, twitter_image['content'])
        if not _is_generic_og_image(img_url):
            images.append(img_url)

    for og in soup.find_all('meta', property='og:image:url'):
        if og.get('content'):
            img_url = urljoin(page_url, og['content'])
            if not _is_generic_og_image(img_url):
                images.append(img_url)

    return list(dict.fromkeys(images))


def extract_videos_from_page(soup, page_url, main_content):
    """從網頁中提取影片 URL"""
    videos = []

    # <video> 標籤
    for video in soup.find_all('video'):
        # 優先主內容中的影片
        in_content = is_in_main_content(video, main_content)
        src = video.get('src')
        if src:
            url = urljoin(page_url, src)
            videos.append(MediaItem(url, 'video', score=60 if in_content else 20))
        for source in video.find_all('source'):
            src = source.get('src')
            if src:
                url = urljoin(page_url, src)
                videos.append(MediaItem(url, 'video', score=60 if in_content else 20))

    # <iframe> 嵌入（影片平台）
    for iframe in soup.find_all('iframe'):
        src = iframe.get('src', '')
        if not src:
            continue
        in_content = is_in_main_content(iframe, main_content)
        video_domains = ['youtube', 'vimeo', 'dailymotion', 'youtu.be', 'bilibili',
                         'twitch', 'facebook.com/plugins/video']
        if any(d in src.lower() for d in video_domains):
            # 轉換嵌入 URL 為正常 URL
            if 'youtube.com/embed/' in src:
                video_id = src.split('embed/')[1].split('?')[0].split('/')[0]
                url = f'https://www.youtube.com/watch?v={video_id}'
            elif 'youtu.be' in src:
                url = src
            elif 'vimeo.com' in src:
                url = src.replace('/video/', '/').split('?')[0]
                if not url.startswith('http'):
                    url = 'https:' + url if url.startswith('//') else 'https://' + url
            else:
                url = urljoin(page_url, src)
            videos.append(MediaItem(url, 'video', score=70 if in_content else 30))

    # OG 影片
    og_video = soup.find('meta', property='og:video')
    if og_video and og_video.get('content'):
        videos.append(MediaItem(urljoin(page_url, og_video['content']), 'video', score=80))

    og_video_url = soup.find('meta', property='og:video:url')
    if og_video_url and og_video_url.get('content'):
        url = urljoin(page_url, og_video_url['content'])
        videos.append(MediaItem(url, 'video', score=80))

    # JSON-LD 結構化資料
    for script in soup.find_all('script', type='application/ld+json'):
        try:
            import json
            data = json.loads(script.string or '{}')
            if isinstance(data, list):
                for item in data:
                    _extract_jsonld_video(item, videos, page_url)
            else:
                _extract_jsonld_video(data, videos, page_url)
        except:
            pass

    return videos


def _extract_jsonld_video(data, videos, page_url):
    """從 JSON-LD 中提取影片"""
    if not isinstance(data, dict):
        return
    vid_type = data.get('@type', '')
    if 'Video' in vid_type:
        content_url = data.get('contentUrl') or data.get('embedUrl')
        if content_url:
            videos.append(MediaItem(urljoin(page_url, content_url), 'video', score=75))


def get_best_srcset(srcset_str, page_url):
    """從 srcset 屬性中獲取最高解析度的圖片"""
    if not srcset_str:
        return None
    parts = srcset_str.split(',')
    best_src = None
    best_width = 0
    for part in parts:
        tokens = part.strip().split()
        if len(tokens) >= 1:
            src_url = urljoin(page_url, tokens[0])
            width = 0
            if len(tokens) >= 2:
                try:
                    descriptor = tokens[1].lower()
                    if descriptor.endswith('w'):
                        width = int(descriptor[:-1])
                    elif descriptor.endswith('x'):
                        width = int(float(descriptor[:-1]) * 1000)
                except:
                    pass
            if width > best_width or best_src is None:
                best_width = width
                best_src = src_url
    return best_src


def check_image_size_fast(url, session=None):
    """快速檢查圖片尺寸（只下載足夠判斷的資料量）"""
    if session is None:
        session = requests.Session()
    try:
        # 先檢查 Content-Length
        response = session.head(url, headers=HEADERS, timeout=8, allow_redirects=True)
        content_length = int(response.headers.get('content-length', 0))
        content_type = response.headers.get('content-type', '').lower()

        # 太小的文件必定不是主要內容圖片
        if content_length > 0 and content_length < 3000:
            return False, (0, 0)

        # 不是圖片的直接跳過
        if content_type and 'image' not in content_type and content_type != 'application/octet-stream':
            return False, (0, 0)

        # 下載前 64KB 來偵測尺寸
        response = session.get(
            url, headers=HEADERS, timeout=12,
            allow_redirects=True, stream=True
        )
        data = b''
        for chunk in response.iter_content(chunk_size=8192):
            data += chunk
            if len(data) >= 65536:
                break

        try:
            from PIL import Image
            img = Image.open(BytesIO(data))
            width, height = img.size
            return (width >= MIN_WIDTH and height >= MIN_HEIGHT), (width, height)
        except:
            # 無法解碼但檔案夠大就假設合格
            if content_length > 20000:
                return True, (0, 0)
            return False, (0, 0)
    except:
        return True, (0, 0)  # 無法檢查就給予通過


def scrape_media(url, check_sizes=True):
    """
    爬取網頁並提取相關媒體（圖片和影片）
    回傳 MediaItem 列表，按相關性排序
    """
    session = requests.Session()
    response = fetch_page(url, session)

    if not response:
        return []

    soup = BeautifulSoup(response.text, 'lxml')
    main_content = find_main_content(soup)

    media_items = []
    seen_urls = set()

    # 1. OG/Meta 圖片（最高優先）
    og_images = extract_og_images(soup, url)
    for img_url in og_images:
        if img_url not in seen_urls:
            seen_urls.add(img_url)
            media_items.append(MediaItem(
                img_url, 'image', score=80,
                context='Open Graph / Meta', source_url=url
            ))

    # 2. 所有 img 標籤評分
    for img in soup.find_all('img'):
        score, src = score_image_element(img, main_content, url)
        if src and src not in seen_urls and score > -10:
            seen_urls.add(src)
            alt = (img.get('alt', '') or '').strip()
            item = MediaItem(src, 'image', score=score, alt=alt, source_url=url)

            # 檢查是否有更高解析度的 srcset
            srcset = img.get('srcset', '')
            if srcset:
                best_src = get_best_srcset(srcset, url)
                if best_src and best_src not in seen_urls:
                    seen_urls.add(best_src)
                    item.url = best_src
                    item.score += 5

            media_items.append(item)

    # 3. <picture> 元素
    for picture in soup.find_all('picture'):
        sources = picture.find_all('source')
        best_src = None
        best_width = 0
        for source in sources:
            srcset = source.get('srcset', '')
            src = get_best_srcset(srcset, url)
            if src:
                # 嘗試獲取寬度
                media_query = source.get('media', '')
                w = 0
                match = re.search(r'(\d+)px', media_query)
                if match:
                    w = int(match.group(1))
                if w > best_width or best_src is None:
                    best_width = w
                    best_src = src

        if best_src and best_src not in seen_urls:
            seen_urls.add(best_src)
            in_content = is_in_main_content(picture, main_content)
            media_items.append(MediaItem(
                best_src, 'image', score=30 if in_content else 5, source_url=url
            ))

    # 4. 背景圖片（在主內容區域內的 style 屬性）
    if main_content:
        for tag in main_content.find_all(True, style=True):
            style = tag.get('style', '')
            bg_match = re.findall(r'url\(["\']?(.*?)["\']?\)', style)
            for bg_url in bg_match:
                full_url = urljoin(url, bg_url)
                if full_url not in seen_urls:
                    seen_urls.add(full_url)
                    media_items.append(MediaItem(
                        full_url, 'image', score=15,
                        context='CSS Background', source_url=url
                    ))

    # 5. 影片
    video_items = extract_videos_from_page(soup, url, main_content)
    for vid in video_items:
        if vid.url not in seen_urls:
            seen_urls.add(vid.url)
            vid.source_url = url
            media_items.append(vid)

    # 6. 連結中的直接媒體文件
    if main_content:
        for a_tag in main_content.find_all('a', href=True):
            href = urljoin(url, a_tag['href'])
            is_media, media_type = is_direct_media_url(href)
            if is_media and href not in seen_urls:
                seen_urls.add(href)
                media_items.append(MediaItem(
                    href, media_type, score=35,
                    alt=a_tag.get_text(strip=True)[:100], source_url=url
                ))

    # 7. 過濾低分項目
    media_items = [m for m in media_items if m.score > 0]

    # 8. 對圖片候選項做尺寸檢查（可選）
    if check_sizes and media_items:
        checked = 0
        for item in media_items:
            if item.media_type == 'image' and checked < 30:  # 最多檢查 30 張
                is_large, (w, h) = check_image_size_fast(item.url, session)
                if not is_large and w > 0:
                    item.score -= 30
                elif w >= 600:
                    item.score += 20
                elif w >= 300:
                    item.score += 5
                item.size = (w, h) if w > 0 else None
                checked += 1

    # 9. 最終過濾和排序
    media_items = [m for m in media_items if m.score > 0]
    media_items.sort(key=lambda x: x.score, reverse=True)

    # 10. 去重（同一圖片不同尺寸只保留最高分的）
    final_items = []
    seen_hashes = set()
    for item in media_items:
        # 簡單的 URL 去重（去掉尺寸參數）
        normalized = re.sub(r'[\?&](w|width|h|height|size|resize|fit)=\d+', '', item.url)
        normalized = re.sub(r'-\d+x\d+\.', '.', normalized)
        url_hash = hashlib.md5(normalized.encode()).hexdigest()
        if url_hash not in seen_hashes:
            seen_hashes.add(url_hash)
            final_items.append(item)

    return final_items


def generate_filename(url, alt='', index=0):
    """為媒體文件生成合適的檔名"""
    parsed = urlparse(url)
    path = unquote(parsed.path)
    basename = os.path.basename(path)

    if basename and '.' in basename:
        # 清理檔名
        name = re.sub(r'[<>:"/\\|?*]', '_', basename)
        if len(name) > 150:
            name = name[:100] + name[name.rfind('.'):]
        return name

    # 沒有好的檔名就用 alt 或 hash
    ext = '.jpg'  # 預設
    content_type_map = {
        'png': '.png', 'webp': '.webp', 'gif': '.gif',
        'jpeg': '.jpg', 'jpg': '.jpg', 'mp4': '.mp4',
        'webm': '.webm', 'mkv': '.mkv',
    }
    for key, val in content_type_map.items():
        if key in path.lower():
            ext = val
            break

    if alt and len(alt) > 3:
        name = re.sub(r'[<>:"/\\|?*\s]+', '_', alt)[:80]
        return f"{name}{ext}"

    url_hash = hashlib.md5(url.encode()).hexdigest()[:10]
    return f"media_{index:03d}_{url_hash}{ext}"


def download_file(url, output_path, session=None):
    """下載檔案到指定路徑"""
    if session is None:
        session = requests.Session()

    try:
        response = session.get(
            url, headers=HEADERS, timeout=60,
            allow_redirects=True, stream=True
        )
        response.raise_for_status()

        os.makedirs(os.path.dirname(output_path), exist_ok=True)

        total_size = int(response.headers.get('content-length', 0))
        downloaded = 0

        with open(output_path, 'wb') as f:
            for chunk in response.iter_content(chunk_size=8192):
                if chunk:
                    f.write(chunk)
                    downloaded += len(chunk)

        return True, downloaded
    except Exception as e:
        logger.error(f"Download failed for {url}: {e}")
        # 清理不完整的文件
        try:
            if os.path.exists(output_path):
                os.remove(output_path)
        except:
            pass
        return False, str(e)

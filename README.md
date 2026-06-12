# 爬蟲工具箱

六個獨立模組，用邊個 import 邊個。

## 快速上手

```python
# 攞網頁
from stealth_fetch import fetch
result = await fetch("https://example.com")
print(result.html)

# HTML → markdown
from content_extractor import extract
page = extract(result.html, "https://example.com")
print(page.text_markdown)

# PDF → 文字
from document_extractor import extract
doc = extract("report.pdf")
print(doc.text)
```

## 六個模組

| 模組 | 一句講晒 | 獨立用法 |
|------|---------|---------|
| `stealth_fetch` | 扮瀏覽器攞 HTML | `await fetch(url)` → `FetchResult` |
| `content_extractor` | HTML → 正文 markdown | `extract(html, url)` → `ExtractionResult` |
| `document_extractor` | PDF/Word → 文字 | `extract(filepath)` → `DocumentResult` |
| `dedup` | 內容去重 | `ContentDeduplicator().add_and_check(text, id)` |
| `resilience` | 限速/重試/熔斷/代理 | 包住任何 async 函數 |
| `scraper` | 媒體提取（圖片/影片） | `scrape_media(url)` → `list[MediaItem]` |

## 組合示範

### 攞網頁正文（最常用）

```python
from stealth_fetch import fetch
from content_extractor import extract

result = await fetch("https://example.com")
if result.ok:
    page = extract(result.html, result.url)
    print(page.text_markdown)
```

### 批量攞 + 去重

```python
from stealth_fetch import fetch_many
from content_extractor import extract
from dedup import ContentDeduplicator

dedup = ContentDeduplicator(threshold=0.8)
results = await fetch_many(urls, concurrency=10)
for r in results:
    if not r.ok:
        continue
    page = extract(r.html, r.url)
    is_dup, dup_of = dedup.add_and_check(page.text or "", r.url)
    if not is_dup:
        print(page.text_markdown)
```

### 大規模爬蟲（加限速 + 重試）

```python
from stealth_fetch import StealthFetcher
from resilience import RateLimiter, RetryHandler

limiter = RateLimiter(default_delay=2.0)
retry = RetryHandler(max_retries=3)
fetcher = StealthFetcher()

async def safe_fetch(url):
    domain = urlparse(url).netloc
    await limiter.acquire(domain)
    result = await retry.execute(fetcher.fetch, url)
    limiter.update_delay(domain, result.status_code)
    return result
```

### 完整 pipeline（scraper_v2）

```python
from scraper_v2 import UniversalScraper

scraper = UniversalScraper()
result = await scraper.scrape(url, extract_text=True, extract_media=False)
print(result.content.text_markdown)
```

## 依賴

```
pip install curl_cffi trafilatura beautifulsoup4 lxml
```

可選（按需裝）：
- `browserforge` — 更真實嘅 HTTP headers
- `extruct` — JSON-LD / Microdata 結構化數據
- `pandas` — HTML 表格提取
- `datasketch` — MinHash LSH 去重
- `aiohttp` — resilience 模組嘅 robots.txt / sitemap
- `pymupdf4llm pymupdf` — PDF 提取
- `pdfplumber` — PDF 表格提取
- `docling` — Office 文件提取

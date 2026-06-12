"""內容去重與變更偵測模組 — MinHash LSH 近似去重 + SHA256 精確 hash。"""

from __future__ import annotations

import hashlib
import logging
import re
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    pass

logger = logging.getLogger(__name__)

try:
    from datasketch import MinHash, MinHashLSH

    HAS_DATASKETCH = True
except ImportError:
    HAS_DATASKETCH = False

__all__ = ["ContentDeduplicator", "ContentHasher"]


def _shingle(text: str, k: int = 5) -> list[str]:
    """將文字切成 k-gram shingles 用於 MinHash。"""
    text = re.sub(r"\s+", " ", text.strip().lower())
    if len(text) < k:
        return [text] if text else []
    return [text[i : i + k] for i in range(len(text) - k + 1)]


class ContentDeduplicator:
    """基於 MinHash LSH 嘅近似內容去重。

    threshold 控制 Jaccard similarity 閾值（0-1），越高越嚴格。
    """

    def __init__(
        self,
        *,
        threshold: float = 0.8,
        num_perm: int = 128,
        backend: str = "memory",
    ):
        if not HAS_DATASKETCH:
            raise RuntimeError("datasketch is required: pip install datasketch")

        self._threshold = threshold
        self._num_perm = num_perm
        self._backend = backend

        self._lsh = MinHashLSH(threshold=threshold, num_perm=num_perm)
        self._hashes: dict[str, MinHash] = {}

    def _make_minhash(self, content: str) -> MinHash:
        m = MinHash(num_perm=self._num_perm)
        for s in _shingle(content):
            m.update(s.encode("utf-8"))
        return m

    def is_duplicate(
        self, content: str, doc_id: str | None = None
    ) -> tuple[bool, str | None]:
        """檢查 content 是否同已索引嘅內容重複。

        Returns:
            (is_dup, matching_doc_id) — 如果重複，返回最相似嘅 doc_id。
        """
        mh = self._make_minhash(content)
        matches = self._lsh.query(mh)

        if doc_id and doc_id in matches:
            matches.remove(doc_id)

        if not matches:
            return False, None

        best_id = None
        best_sim = 0.0
        for mid in matches:
            if mid in self._hashes:
                sim = mh.jaccard(self._hashes[mid])
                if sim > best_sim:
                    best_sim = sim
                    best_id = mid

        if best_id and best_sim >= self._threshold:
            return True, best_id
        return False, None

    def add(self, content: str, doc_id: str) -> None:
        """將內容加入去重索引。"""
        if doc_id in self._hashes:
            self.remove(doc_id)
        mh = self._make_minhash(content)
        self._hashes[doc_id] = mh
        self._lsh.insert(doc_id, mh)

    def add_and_check(
        self, content: str, doc_id: str
    ) -> tuple[bool, str | None]:
        """檢查重複後加入索引（原子操作）。"""
        is_dup, match_id = self.is_duplicate(content, doc_id=doc_id)
        if not is_dup:
            self.add(content, doc_id)
        return is_dup, match_id

    def remove(self, doc_id: str) -> bool:
        """從索引移除指定文檔。"""
        if doc_id not in self._hashes:
            return False
        try:
            self._lsh.remove(doc_id)
        except ValueError:
            pass
        del self._hashes[doc_id]
        return True

    def clear(self) -> None:
        """清空全部索引。"""
        self._lsh = MinHashLSH(
            threshold=self._threshold, num_perm=self._num_perm
        )
        self._hashes.clear()

    @property
    def size(self) -> int:
        return len(self._hashes)


class ContentHasher:
    """精確內容 hash 工具，用於變更監測。"""

    @staticmethod
    def hash_content(content: str) -> str:
        """SHA256 hash of content (normalized whitespace)."""
        normalized = re.sub(r"\s+", " ", content.strip())
        return hashlib.sha256(normalized.encode("utf-8")).hexdigest()

    @staticmethod
    def hash_etag(headers: dict) -> str | None:
        """從 HTTP response headers 提取 ETag。"""
        etag = headers.get("ETag") or headers.get("etag")
        if etag:
            return etag.strip().strip('"').strip("'")
        return None

    @staticmethod
    def hash_last_modified(headers: dict) -> str | None:
        """從 HTTP response headers 提取 Last-Modified。"""
        return headers.get("Last-Modified") or headers.get("last-modified")

    @staticmethod
    def has_changed(old_hash: str, new_hash: str) -> bool:
        """比較兩個 hash 判斷內容是否有變。"""
        return old_hash != new_hash

    @staticmethod
    def content_fingerprint(content: str) -> dict:
        """生成內容指紋（hash + 長度 + 前 100 字元）。"""
        return {
            "hash": ContentHasher.hash_content(content),
            "length": len(content),
            "preview": content[:100] if content else "",
        }

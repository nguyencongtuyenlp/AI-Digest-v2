"""
memory.py — Long-term Memory Layer bằng ChromaDB.

Chức năng:
- Lưu embedding các bài viết đã phân tích
- Truy vấn bài viết cùng chủ đề trong quá khứ
- Giúp Agent biết "tôi đã đưa tin này chưa?" và "có gì mới so với bài cũ?"

Storage: ~/.daily-digest-agent/chromadb/ (persist trên disk)
"""

from __future__ import annotations

import logging
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# ── ChromaDB lưu tại thư mục cố định, không phụ thuộc project path ──
CHROMA_DIR = os.path.expanduser("~/.daily-digest-agent/chromadb")
COLLECTION_NAME = "digest_articles"


def _get_collection():
    """
    Lấy (hoặc tạo) ChromaDB collection cho bài viết.
    Singleton-like: import 1 lần, dùng mãi trong cùng process.
    """
    import chromadb

    Path(CHROMA_DIR).mkdir(parents=True, exist_ok=True)
    client = chromadb.PersistentClient(path=CHROMA_DIR)
    return client.get_or_create_collection(
        name=COLLECTION_NAME,
        metadata={"hnsw:space": "cosine"},
    )


def store_article(
    article_id: str,
    title: str,
    summary: str,
    primary_type: str = "",
    score: int = 0,
    url: str = "",
    metadata: dict[str, Any] | None = None,
) -> None:
    """
    Lưu 1 bài viết vào ChromaDB long-term memory.
    Dùng title + summary làm nội dung embed.

    Args:
        article_id: ID duy nhất (thường dùng URL hash)
        title: Tiêu đề bài viết
        summary: Tóm tắt nội dung
        primary_type: Loại tin (Research, Product, Business, ...)
        score: Điểm đánh giá (1-100)
        url: Link gốc
        metadata: Thông tin bổ sung tùy chọn
    """
    collection = _get_collection()

    # Nội dung để embed = title + summary
    doc_text = f"{title}\n{summary}"

    meta = {
        "title": title[:500],
        "primary_type": primary_type,
        "score": score,
        "url": url[:2000],
        "stored_at": datetime.now(timezone.utc).isoformat(),
    }
    if metadata:
        meta.update({k: str(v)[:500] for k, v in metadata.items()})

    try:
        collection.upsert(
            ids=[article_id],
            documents=[doc_text[:5000]],
            metadatas=[meta],
        )
        logger.debug("📝 Memory: stored article '%s'", title[:60])
    except Exception as e:
        logger.error("❌ Memory store failed: %s", e)


def recall_similar(
    query: str,
    n_results: int = 5,
    min_score: int = 0,
) -> list[dict[str, Any]]:
    """
    Tìm bài viết tương tự trong memory dựa trên nội dung.

    Args:
        query: Nội dung để tìm bài tương tự (thường là title + summary bài mới)
        n_results: Số bài trả về tối đa
        min_score: Chỉ trả về bài có score >= min_score

    Returns:
        List các dict: {id, title, summary, primary_type, score, url, distance}
    """
    collection = _get_collection()

    try:
        results = collection.query(
            query_texts=[query[:5000]],
            n_results=min(n_results, collection.count() or 1),
        )
    except Exception as e:
        logger.error("❌ Memory recall failed: %s", e)
        return []

    articles = []
    if results and results.get("ids") and results["ids"][0]:
        for i, doc_id in enumerate(results["ids"][0]):
            meta = results["metadatas"][0][i] if results.get("metadatas") else {}
            dist = results["distances"][0][i] if results.get("distances") else 1.0
            article_score = int(meta.get("score", 0))
            if article_score >= min_score:
                articles.append({
                    "id": doc_id,
                    "title": meta.get("title", ""),
                    "primary_type": meta.get("primary_type", ""),
                    "score": article_score,
                    "url": meta.get("url", ""),
                    "stored_at": meta.get("stored_at", ""),
                    "distance": dist,  # 0 = giống hệt, 1 = khác hoàn toàn
                    "document": results["documents"][0][i] if results.get("documents") else "",
                })

    return articles


def recall_by_topic(topic: str, n_results: int = 3) -> list[dict[str, Any]]:
    """
    Tìm bài viết theo chủ đề cụ thể.
    Wrapper tiện lợi cho recall_similar.

    Args:
        topic: Chủ đề cần tìm, ví dụ "OpenAI GPT-5", "Luật AI Việt Nam"
        n_results: Số bài trả về

    Returns:
        List bài viết liên quan (đã sắp xếp theo relevance)
    """
    return recall_similar(query=topic, n_results=n_results)


def get_memory_stats() -> dict[str, Any]:
    """
    Lấy thống kê memory: số bài đã lưu, dung lượng, ...
    Dùng để log / debug.
    """
    try:
        collection = _get_collection()
        count = collection.count()
        return {
            "total_articles": count,
            "chroma_dir": CHROMA_DIR,
            "collection_name": COLLECTION_NAME,
        }
    except Exception as e:
        return {"error": str(e)}

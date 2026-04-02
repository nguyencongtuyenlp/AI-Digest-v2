"""
recommend_idea.py — LangGraph node: Đề xuất ý tưởng cho startup.

Nhận bài viết đã phân tích sâu → tạo recommend cụ thể, actionable
cho startup AI tại Hà Nội. Gắn recommend vào từng bài.

Tập trung vào 4 MVP hiện tại:
  1. AI News Digest Agent
  2. AI Revenue Calculator
  3. AI Enterprise Management
  4. AI Product general
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from mlx_runner import run_inference

logger = logging.getLogger(__name__)

RECOMMEND_SYSTEM = """Bạn là product strategist kiêm CTO của một startup AI tại Hà Nội.
Công ty đang phát triển 4 sản phẩm:
1. AI News Digest Agent — tự động thu thập, phân tích, tổng hợp tin tức AI
2. AI Revenue Calculator — tính toán doanh thu, giao diện Telegram
3. AI Enterprise Management — quản lý toàn bộ hoạt động công ty bằng AI
4. AI Product B2B cho SME Việt Nam — chatbot, automation, document processing

Dựa trên bài viết và phân tích sâu, hãy đề xuất ý tưởng ACTIONABLE, thiên về đóng gói thành sản phẩm kinh doanh được.

## Format output:
### 💡 Ý tưởng chính
[1-2 câu mô tả ý tưởng cốt lõi]

### 🎯 Áp dụng cho MVP nào?
[Chỉ rõ MVP 1/2/3/4 và cách áp dụng cụ thể]

### 📋 Bước thực hiện
1. [Bước 1 — cụ thể, có thể làm ngay]
2. [Bước 2]
3. [Bước 3]

### ⏱️ Timeline ước tính
[Quick win (1 ngày) / Short-term (1 tuần) / Medium-term (1 tháng)]

### ⚠️ Rủi ro / Lưu ý
[Điều gì cần cẩn thận?]

Quy tắc:
- Ưu tiên đề xuất có thể bán được, triển khai được, hoặc giúp sản phẩm khác biệt hơn.
- Tránh ý tưởng chung chung kiểu "nghiên cứu thêm".
- Nếu bài không đủ mạnh để tạo ý tưởng mới, hãy nói rõ nên chỉ theo dõi thay vì ép ra ý tưởng.

Viết bằng tiếng Việt, ngắn gọn nhưng đầy đủ (150-250 từ)."""

RECOMMEND_USER_TEMPLATE = """Dựa trên bài viết và phân tích sau, đề xuất ý tưởng cho startup:

Tiêu đề: {title}
Type: {primary_type}
Score: {total_score}/100
Tóm tắt: {summary_vi}

Phân tích sâu:
{deep_analysis}

Hãy đề xuất ý tưởng actionable theo format yêu cầu."""


def recommend_idea_node(state: dict[str, Any]) -> dict[str, Any]:
    """
    LangGraph node: tạo recommend idea cho từng bài đã phân tích.

    Input: analyzed_articles (từ deep_analysis)
    Output: analyzed_articles (bổ sung field recommend_idea)
    """
    articles = state.get("analyzed_articles", [])
    if not articles:
        logger.info("📭 Không có bài nào để recommend.")
        return {"analyzed_articles": []}

    total = len(articles)

    for i, article in enumerate(articles, 1):
        title = article.get("title", "N/A")
        logger.info("💡 Recommend [%d/%d]: %s", i, total, title[:60])

        user_prompt = RECOMMEND_USER_TEMPLATE.format(
            title=title,
            primary_type=article.get("primary_type", "Unknown"),
            total_score=article.get("total_score", 0),
            summary_vi=article.get("summary_vi", ""),
            deep_analysis=article.get("deep_analysis", "")[:5000],
        )

        try:
            recommend = run_inference(
                RECOMMEND_SYSTEM,
                user_prompt,
                max_tokens=1000,
                temperature=0.6,
            )
            article["recommend_idea"] = recommend
            logger.info("   ✅ Recommend xong (%d chars)", len(recommend))
        except Exception as e:
            logger.error("   ❌ Recommend failed: %s", e)
            article["recommend_idea"] = "Không thể tạo recommendation — lỗi hệ thống."

    logger.info("✅ Recommend idea hoàn tất: %d bài", len(articles))
    return {"analyzed_articles": articles}

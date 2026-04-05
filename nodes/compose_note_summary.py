"""
compose_note_summary.py — LangGraph node: Nén mỗi bài thành short note chuyên dùng cho delivery.

Node này giúp tách rõ:
  - content_page_md / deep_analysis: bản phân tích dài cho Notion page
  - note_summary_vi: bản nén 1 đoạn cho property Notion + Telegram preview
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from editorial_guardrails import build_article_grounding, sanitize_delivery_text
from mlx_runner import run_inference

logger = logging.getLogger(__name__)

NOTE_SUMMARY_SYSTEM = """Bạn là biên tập viên cho một sản phẩm AI Daily Digest.
Nhiệm vụ: nén một bài phân tích thành đúng 1 đoạn bản tin ngắn, chuyên nghiệp, dùng cho Telegram và Notion.

Yêu cầu bắt buộc:
- Viết bằng tiếng Việt.
- Chỉ 1 đoạn, không bullet, không markdown.
- Độ dài mục tiêu: 45-85 từ.
- Chỉ tóm tin: ai làm gì, nội dung/số liệu chính, bối cảnh đã nêu trong nguồn (nếu có).
- Không đưa ý nghĩa cho “doanh nghiệp/độc giả phải làm gì”, không cảnh báo hay khuyên hành động; đó không phải vai trò của brief tin.
- Chỉ được dùng thông tin có trong fact anchors, metadata nguồn, tóm tắt sẵn có và bài phân tích.
- Không được biến inference thành fact.
- Không lặp lại nguyên tiêu đề.
- Giọng văn phải trung tính, chuyên nghiệp, giống bản tin ngắn (wire-style).
- Không dùng các cụm như: "ý chính của tin này là", "nên theo dõi", "nên thử", "cần thận trọng", "cảnh báo", "doanh nghiệp cần", "tín hiệu yếu", "nguồn tin chưa được xác thực", "dữ liệu yếu", "Điều này có ý nghĩa với".
- Không đưa khuyến nghị hành động hoặc chấm độ tin cậy nguồn, trừ khi đó là một phần của chính sự kiện.
- Nếu thiếu dữ liệu, chỉ viết phần đã biết; không thêm boilerplate meta.
"""

NOTE_SUMMARY_USER_TEMPLATE = """Hãy nén bài sau thành short note:

Tiêu đề: {title}
Type: {primary_type}
Score: {total_score}/100
Published_at: {published_at}
Source: {source}
Editorial angle: {editorial_angle}
Tóm tắt ngắn hiện có: {summary_vi}
Fact anchors:
{fact_anchors}

Phân tích dài:
{analysis}
"""


def _analysis_excerpt(article: dict[str, Any]) -> str:
    text = str(
        article.get("content_page_md")
        or article.get("deep_analysis")
        or article.get("summary_vi", "")
        or ""
    ).strip()
    for marker in ("\n## Action For Us", "\n## Recommendation"):
        if marker in text:
            text = text.split(marker, 1)[0].rstrip()
    return text


def _fallback_note(article: dict[str, Any]) -> str:
    """Fallback an toàn nếu model lỗi hoặc trả text quá tệ."""
    summary = (article.get("summary_vi", "") or "").strip()
    angle = (article.get("editorial_angle", "") or "").strip()
    core = summary or article.get("title", "Bài viết này")
    note = core
    if angle and angle.lower() not in core.lower():
        note = f"{note} {angle}"
    note = sanitize_delivery_text(note)
    return note or "Đang cập nhật chi tiết trong bài viết."


def compose_note_summary_node(state: dict[str, Any]) -> dict[str, Any]:
    """
    LangGraph node: tạo short note cho tất cả bài sẽ được lưu/gửi.

    Input ưu tiên:
      - analyzed_articles + low_score_articles
      - fallback scored_articles

    Output:
      - final_articles: list bài đã có note_summary_vi
    """
    analyzed = state.get("analyzed_articles", [])
    low_score = state.get("low_score_articles", [])
    final_articles = analyzed + low_score

    if not final_articles:
        final_articles = list(state.get("scored_articles", []))

    if not final_articles:
        logger.info("📭 Không có bài nào để compose short note.")
        return {"final_articles": []}

    runtime_config = dict(state.get("runtime_config", {}) or {})
    run_profile = str(state.get("run_profile", "") or "").strip().lower()
    fast_mode = run_profile == "fast" or bool(runtime_config.get("deterministic_note_summary", False))

    if fast_mode:
        # Fast preview ưu tiên tốc độ review format/chọn tin, nên dùng fallback deterministic.
        for article in final_articles:
            grounding = build_article_grounding(article)
            article.update(grounding)
            article["note_summary_vi"] = _fallback_note(article)
        logger.info("⚡ Fast note summary: dùng deterministic fallback cho %d bài", len(final_articles))
        return {"final_articles": final_articles}

    total = len(final_articles)
    for i, article in enumerate(final_articles, 1):
        title = article.get("title", "N/A")
        logger.info("🧾 Note Summary [%d/%d]: %s", i, total, title[:60])

        grounding = build_article_grounding(article)
        article.update(grounding)

        long_analysis = _analysis_excerpt(article)
        user_prompt = NOTE_SUMMARY_USER_TEMPLATE.format(
            title=title,
            primary_type=article.get("primary_type", "Unknown"),
            total_score=article.get("total_score", 0),
            published_at=article.get("published_at", ""),
            source=article.get("source", "Unknown"),
            editorial_angle=article.get("editorial_angle", "N/A"),
            summary_vi=article.get("summary_vi", ""),
            fact_anchors=grounding.get("fact_anchors_text", "- Chưa có fact anchor mạnh."),
            analysis=long_analysis[:4500],
        )

        try:
            note = run_inference(
                NOTE_SUMMARY_SYSTEM,
                user_prompt,
                max_tokens=220,
                temperature=0.3,
            ).strip()
            article["note_summary_vi"] = sanitize_delivery_text(note) or _fallback_note(article)
        except Exception as e:
            logger.error("   ❌ Note summary failed: %s", e)
            article["note_summary_vi"] = _fallback_note(article)

    logger.info("✅ Short note hoàn tất: %d bài", len(final_articles))
    return {"final_articles": final_articles}

"""
deep_analysis.py — LangGraph node: Phân tích sâu bài top score theo vai "research + thinking".

Node này mô phỏng kiểu làm việc của ChatGPT research/thinking nhưng chạy local bằng Qwen:
  1. Tìm thêm tín hiệu cộng đồng / tin bổ sung
  2. Đối chiếu với lịch sử trong memory
  3. Viết một bản phân tích dài, có thể dùng trực tiếp làm content page trong Notion

Output:
  - deep_analysis: nội dung phân tích dài
  - content_page_md: alias của deep_analysis để downstream dùng rõ nghĩa hơn
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

# Đảm bảo project root nằm trong sys.path
from digest.editorial.editorial_guardrails import build_article_grounding
from digest.runtime.mlx_runner import resolve_pipeline_mlx_path, run_inference_large

logger = logging.getLogger(__name__)
SAFE_DDGS_TEXT_BACKEND = "duckduckgo"


# ── Search cộng đồng ─────────────────────────────────────────────────

def _search_ddg_text(query: str, *, max_results: int) -> list[dict[str, Any]]:
    try:
        from ddgs import DDGS
    except ImportError:
        logger.warning("ddgs not available, skipping community search")
        return []

    try:
        with DDGS(timeout=10) as ddgs:
            return list(
                ddgs.text(
                    query,
                    max_results=max_results,
                    backend=SAFE_DDGS_TEXT_BACKEND,
                )
            )
    except Exception as exc:
        logger.debug("DDG text search failed for '%s': %s", query, exc)
        return []


def _search_community_reactions(keyword: str) -> str:
    """
    Tìm kiếm phản ứng cộng đồng về 1 chủ đề qua DuckDuckGo.
    Tìm trên Reddit, Hacker News, các forum AI.

    Args:
        keyword: Từ khóa tìm kiếm (thường là tên sản phẩm/sự kiện)

    Returns:
        Tóm tắt text các bài viết/bình luận tìm được
    """
    results_text = []

    # Tìm trên Reddit
    for result in _search_ddg_text(f"site:reddit.com {keyword}", max_results=3):
        results_text.append(
            f"[Reddit] {result.get('title', '')}: {result.get('body', '')[:300]}"
        )

    # Dùng text search thay vì news backend để tránh nhánh runtime có thể abort process.
    for result in _search_ddg_text(f"\"{keyword}\" AI news", max_results=3):
        results_text.append(
            f"[News] {result.get('title', '')}: {result.get('body', '')[:300]}"
        )

    for result in _search_ddg_text(f"site:news.ycombinator.com {keyword}", max_results=2):
        results_text.append(
            f"[HackerNews] {result.get('title', '')}: {result.get('body', '')[:300]}"
        )

    return "\n".join(results_text) if results_text else ""


# ── Prompt phân tích sâu ─────────────────────────────────────────────

DEEP_ANALYSIS_SYSTEM = """Bạn là Principal Research Analyst cho một sản phẩm AI Daily Digest dùng trong vận hành doanh nghiệp.
Bạn không chỉ tóm tắt tin, mà phải biến tin thành quyết định: điều gì là thật, điều gì đáng quan tâm,
giá trị thực tế là gì, và với người làm sản phẩm/doanh nghiệp thì nên nhìn tin này theo lăng kính nào.

Hãy viết một bản content page bằng tiếng Việt, súc tích nhưng giàu ý, theo cấu trúc sau:

## Executive Note
- Mở đầu bằng 1 đoạn ngắn kiểu "điều này có nghĩa gì trong thực tế"
- Nêu bản chất tin + giá trị thực tế + điều kiện/giới hạn quan trọng nhất

## Source Snapshot
- Tóm tắt nhanh nguồn, độ tin cậy, thời điểm, và vì sao nên/không nên tin mạnh

## What Happened
- Tin này thực chất đang nói điều gì?
- Điều gì là mới, khác, hoặc đáng chú ý nhất?

## Why It Matters
- Giá trị thực tế với founder/PM/operator/team AI là gì?
- Trường hợp nào bài này hữu ích thật, trường hợp nào chỉ mang tính trình diễn?

## Evidence And Caveats
- Độ mạnh của nguồn và bằng chứng
- Phần nào là fact, phần nào mới là claim/marketing
- Nếu thiếu dữ liệu, nói rõ là thiếu
- BẮT BUỘC có đúng 3 tiểu mục con:
  ### Fact Anchors
  ### Reasonable Inferences
  ### Unknown / Need Verification

## Market Reaction
- Tóm tắt phản ứng từ dữ liệu được cung cấp
- Nếu không có dữ liệu cộng đồng, ghi rõ "Chưa có dữ liệu cộng đồng"

## Action For Us
- Điều kiện để áp dụng
- Rủi ro, chi phí, giới hạn, hoặc friction khi triển khai
- Team nên theo dõi, thử nhanh, hay bỏ qua?

## Recommendation
- Kết luận ngắn, thẳng, không marketing
- Chỉ ra đây là cơ hội đáng hành động, nên theo dõi thêm, hay chỉ nên tham khảo

Quy tắc:
- Tuyệt đối không bịa dữ liệu hoặc phản ứng cộng đồng.
- Không dùng giọng điệu cường điệu.
- Ưu tiên insight thực tế hơn là kể lại nội dung nguồn.
- Nếu có bài cũ liên quan, dùng để chỉ ra bối cảnh và mức độ mới của tin.

Độ dài mục tiêu: 450-700 từ."""

DEEP_ANALYSIS_USER_TEMPLATE = """Phân tích sâu bài viết sau để dùng làm content page:

Tiêu đề: {title}
Type: {primary_type}
Score: {total_score}/100
Editorial angle: {editorial_angle}
URL: {url}
Source: {source}
Source domain: {source_domain}
Published_at (UTC ISO): {published_at}
Source_verified (heuristic): {source_verified}
Source_tier: {source_tier}
Grounding note: {grounding_note}
Fact anchors:
{fact_anchors}
Reasonable inferences:
{reasonable_inferences}
Unknown / need verification:
{unknowns}
Nội dung gốc: {content}

--- PHẢN ỨNG CỘNG ĐỒNG (từ internet search) ---
{community_reactions}

--- BÀI CŨ LIÊN QUAN (từ memory) ---
{related_past}

Hãy viết content page theo cấu trúc yêu cầu."""


def _ensure_evidence_sections(analysis: str, grounding: dict[str, Any]) -> str:
    """Append deterministic evidence sections when the model omits them."""
    required_markers = (
        "### Fact Anchors",
        "### Reasonable Inferences",
        "### Unknown / Need Verification",
    )
    if all(marker in analysis for marker in required_markers):
        return analysis

    appendix = (
        "\n\n## Evidence And Caveats\n"
        "### Fact Anchors\n"
        f"{grounding.get('fact_anchors_text', '- Chưa có fact anchor mạnh.')}\n\n"
        "### Reasonable Inferences\n"
        f"{grounding.get('reasonable_inferences_text', '- Không có suy luận bổ sung.')}\n\n"
        "### Unknown / Need Verification\n"
        f"{grounding.get('unknowns_text', '- Không có unknown lớn từ metadata.')}"
    )
    return analysis.rstrip() + appendix


def deep_analysis_node(state: dict[str, Any]) -> dict[str, Any]:
    """
    LangGraph node: phân tích sâu cho top articles.

    Input: top_articles (từ classify_and_score, score >= 60)
    Output: analyzed_articles (top articles + deep_analysis field)
    """
    top_articles = state.get("top_articles", [])
    if not top_articles:
        logger.info("📭 Không có bài đủ điểm để phân tích sâu.")
        return {"analyzed_articles": []}

    analyzed = []
    total = len(top_articles)
    runtime_config = dict(state.get("runtime_config", {}) or {})
    heavy_mlx = resolve_pipeline_mlx_path("heavy", runtime_config)

    async def _analyze_one(index: int, article: dict[str, Any], sem: asyncio.Semaphore) -> dict[str, Any]:
        async with sem:
            title = article.get("title", "N/A")
            logger.info("🔬 Deep Analysis [%d/%d]: %s", index, total, title[:60])

            keyword = title.split(" – ")[0].split(" | ")[0][:80]
            community = await asyncio.to_thread(_search_community_reactions, keyword)
            if community:
                logger.info("   📡 Tìm thấy %d dòng phản ứng cộng đồng", community.count("\n") + 1)
            else:
                community = "Chưa có dữ liệu cộng đồng"

            related = article.get("related_past", [])
            if related:
                lines = [
                    f"- [{r.get('primary_type', '?')}] {r.get('title', 'N/A')} (score: {r.get('score', 0)})"
                    for r in related[:3]
                ]
                related_text = "\n".join(lines)
            else:
                related_text = "(Không có bài cũ cùng chủ đề trong memory)"

            article["community_reactions"] = community
            grounding = build_article_grounding(article)
            article.update(grounding)

            user_prompt = DEEP_ANALYSIS_USER_TEMPLATE.format(
                title=title,
                primary_type=article.get("primary_type", "Unknown"),
                total_score=article.get("total_score", 0),
                editorial_angle=article.get("editorial_angle", "N/A"),
                url=article.get("url", ""),
                source=article.get("source", "Unknown"),
                source_domain=article.get("source_domain", ""),
                published_at=article.get("published_at", article.get("published", "")),
                source_verified=article.get("source_verified", False),
                source_tier=article.get("source_tier", "unknown"),
                grounding_note=grounding.get("grounding_note", ""),
                fact_anchors=grounding.get("fact_anchors_text", "- Chưa có fact anchor mạnh."),
                reasonable_inferences=grounding.get("reasonable_inferences_text", "- Không có suy luận bổ sung."),
                unknowns=grounding.get("unknowns_text", "- Không có unknown lớn từ metadata."),
                content=(article.get("content", "") or article.get("snippet", ""))[:3000],
                community_reactions=community[:2000],
                related_past=related_text,
            )

            try:
                analysis = await asyncio.to_thread(
                    run_inference_large,
                    DEEP_ANALYSIS_SYSTEM,
                    user_prompt,
                    2200,
                    0.3,
                    heavy_mlx,
                )
                analysis = _ensure_evidence_sections(analysis, grounding)
                article["deep_analysis"] = analysis
                article["content_page_md"] = analysis
                logger.info("   ✅ Phân tích xong (%d chars)", len(analysis))
            except Exception as e:
                logger.error("   ❌ Deep analysis failed: %s", e)
                article["deep_analysis"] = "Không thể phân tích sâu — lỗi hệ thống."
                article["content_page_md"] = article["deep_analysis"]
            return article

    async def _run_parallel() -> list[dict[str, Any]]:
        sem = asyncio.Semaphore(2)
        tasks = [_analyze_one(index, article, sem) for index, article in enumerate(top_articles, 1)]
        return await asyncio.gather(*tasks)

    analyzed = asyncio.run(_run_parallel())

    logger.info("✅ Deep Analysis hoàn tất: %d bài", len(analyzed))
    return {"analyzed_articles": analyzed}

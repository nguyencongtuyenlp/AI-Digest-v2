"""
runtime_presets.py — Các preset runtime nhẹ cho UI/CLI.

Tách riêng file này để:
- test được mà không phụ thuộc server/UI
- dùng lại preset logic ở nhiều nơi nếu cần
"""

from __future__ import annotations

import os

from typing import Any


def _safe_int(value: Any, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _safe_bool(value: Any, default: bool) -> bool:
    if value in (None, ""):
        return default
    return str(value).strip().lower() not in {"0", "false", "no", "off"}


def apply_runtime_preset(profile: str, current: dict[str, Any]) -> dict[str, Any]:
    """
    Preset chỉ override config cho từng kiểu run.
    Không tự thay đổi production nếu user không chọn preset đó.
    """
    normalized_profile = str(profile or "").strip().lower()
    merged = dict(current)

    if normalized_profile == "fast":
        # Fast preview ưu tiên tốc độ review:
        # - giảm nguồn chậm
        # - giảm bớt breadth GitHub để main brief đỡ bị repo noise lấn át
        # - vẫn giữ tối thiểu một chút classify/deep để preview còn phản ánh chất lượng thật
        current_min_score = _safe_int(merged.get("min_deep_analysis_score", 55), 55)
        current_max_classify = _safe_int(merged.get("max_classify_articles", 12), 12)
        current_max_deep = _safe_int(merged.get("max_deep_analysis_articles", 2), 2)
        current_rss_hours = _safe_int(merged.get("gather_rss_hours", 72), 72)
        current_github_repos = _safe_int(merged.get("github_max_watchlist_repos", 6), 6)
        current_github_orgs = _safe_int(merged.get("github_max_orgs", 4), 4)
        current_github_queries = _safe_int(merged.get("github_max_queries", 4), 4)
        current_github_org_repos = _safe_int(merged.get("github_max_org_repos", 4), 4)
        current_github_search = _safe_int(merged.get("github_max_search_results", 4), 4)
        current_github_releases = _safe_int(merged.get("github_max_releases_per_repo", 1), 1)
        current_classify_chars = _safe_int(merged.get("classify_content_char_limit", 900), 900)
        current_classify_tokens = _safe_int(merged.get("classify_max_tokens", 320), 320)
        current_social_enabled = _safe_bool(
            merged.get("enable_social_signals", os.getenv("ENABLE_SOCIAL_SIGNALS", "0")),
            False,
        )
        merged.update(
            {
                "min_deep_analysis_score": max(65, current_min_score),
                "max_classify_articles": min(8, max(6, current_max_classify)),
                "max_deep_analysis_articles": min(2, max(1, current_max_deep)),
                "gather_rss_hours": min(36, current_rss_hours),
                "github_max_watchlist_repos": min(6, max(4, current_github_repos)),
                "github_max_orgs": min(4, max(2, current_github_orgs)),
                "github_max_queries": min(4, max(2, current_github_queries)),
                "github_max_org_repos": min(1, current_github_org_repos),
                "github_max_search_results": min(1, current_github_search),
                "github_max_releases_per_repo": min(1, current_github_releases),
                "classify_content_char_limit": min(520, current_classify_chars),
                "classify_max_tokens": min(220, current_classify_tokens),
                "skip_feedback_sync": True,
                "enable_rss": True,
                "enable_github": True,
                "enable_social_signals": current_social_enabled,
                "enable_watchlist": True,
                "enable_ddg": False,
                "enable_hn": False,
                "enable_reddit": False,
                "enable_telegram_channels": False,
                "enable_facebook_auto": False,
            }
        )
        fast_model = os.getenv("MLX_FAST_MODEL", "").strip()
        if fast_model:
            merged["runtime_mlx_model"] = fast_model
        elif str(merged.get("runtime_mlx_model", "")).strip():
            merged["runtime_mlx_model"] = str(merged.get("runtime_mlx_model")).strip()

    if normalized_profile in {"grok_smart", "smart"}:
        # Grok Smart:
        # - giữ backbone local + routing hiện tại
        # - mở rộng shortlist và search rescue vừa phải để so output với mode thường
        # - vẫn giữ budget cap để một run/ngày không phình chi phí quá mạnh
        current_max_classify = _safe_int(merged.get("max_classify_articles", 25), 25)
        current_max_deep = _safe_int(merged.get("max_deep_analysis_articles", 5), 5)
        current_rss_hours = _safe_int(merged.get("gather_rss_hours", 72), 72)
        current_github_repos = _safe_int(merged.get("github_max_watchlist_repos", 6), 6)
        current_github_orgs = _safe_int(merged.get("github_max_orgs", 4), 4)
        current_github_queries = _safe_int(merged.get("github_max_queries", 4), 4)
        current_github_org_repos = _safe_int(merged.get("github_max_org_repos", 4), 4)
        current_github_search = _safe_int(merged.get("github_max_search_results", 4), 4)
        current_classify_chars = _safe_int(merged.get("classify_content_char_limit", 900), 900)
        current_classify_tokens = _safe_int(merged.get("classify_max_tokens", 320), 320)

        merged.update(
            {
                "max_classify_articles": min(25, max(12, current_max_classify)),
                "min_deep_analysis_score": min(58, max(52, _safe_int(merged.get("min_deep_analysis_score", 55), 55))),
                "ddg_max_results_per_query": min(2, max(1, _safe_int(merged.get("ddg_max_results_per_query", 2), 2))),
                "max_deep_analysis_articles": min(6, max(5, current_max_deep)),
                "gather_rss_hours": max(72, min(96, current_rss_hours)),
                "github_max_watchlist_repos": min(8, max(6, current_github_repos)),
                "github_max_orgs": min(5, max(4, current_github_orgs)),
                "github_max_queries": min(5, max(4, current_github_queries)),
                "github_max_org_repos": min(4, max(3, current_github_org_repos)),
                "github_max_search_results": min(4, max(3, current_github_search)),
                "classify_content_char_limit": min(1400, max(1100, current_classify_chars)),
                "classify_max_tokens": min(480, max(360, current_classify_tokens)),
                "skip_feedback_sync": False,
                "enable_rss": True,
                "enable_github": True,
                "enable_watchlist": True,
                "enable_ddg": True,
                "enable_hn": True,
                "enable_reddit": True,
                "enable_telegram_channels": True,
                "use_grok_for_classify": False,
                "grok_classify_mode": "retry",
                "use_grok_for_delivery_rerank": True,
                "enable_grok_delivery_judge": True,
                "grok_delivery_max_articles": 14,
                "enable_grok_prefilter": True,
                "grok_prefilter_max_articles": 24,
                "enable_grok_final_editor": True,
                "grok_final_editor_max_articles": 10,
                "use_grok_for_final_polish": True,
                "enable_grok_news_copy": True,
                "grok_news_copy_max_articles": 24,
                "enable_grok_facebook_score": False,
                "grok_facebook_max_articles": 10,
                "use_grok_for_source_gap": True,
                "enable_grok_source_gap": True,
                "grok_source_gap_max_articles": 14,
                "use_grok_for_scout": True,
                "enable_grok_scout": True,
                "grok_scout_max_queries": 3,
                "grok_scout_max_articles": 8,
                "grok_scout_min_official_articles": 10,
                "grok_scout_min_official_plus_media": 12,
                "grok_scout_min_non_github_articles": 24,
                "enable_facebook_auto": False,
                "enable_social_signals": True,
                "enable_grok_x_scout": True,
                "grok_x_scout_max_queries": 3,
                "grok_x_scout_max_articles": 6,
                "grok_x_scout_allowed_handles": [
                    "openai",
                    "OpenAIDevs",
                    "sama",
                    "AnthropicAI",
                    "GoogleDeepMind",
                    "GoogleAI",
                    "huggingface",
                    "cursor_ai",
                    "Replit",
                    "MistralAI",
                ],
            }
        )
        smart_model = os.getenv("MLX_SMART_MODEL", "").strip()
        if smart_model:
            merged["runtime_mlx_model"] = smart_model

    if normalized_profile in {"grok_3_stage", "grok_full", "full_grok"}:
        # Grok 3-stage:
        # - bật đúng 3 stage Grok đã ghi trong README
        # - không kéo thêm scout/prefilter/editor layers của preset smart
        merged.update(
            {
                "use_grok_for_classify": True,
                "grok_classify_mode": "retry",
                "use_grok_for_delivery_rerank": True,
                "enable_grok_delivery_judge": True,
                "use_grok_for_final_polish": True,
                "enable_grok_news_copy": True,
            }
        )

    return merged

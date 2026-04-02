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
        current_min_score = _safe_int(merged.get("min_deep_analysis_score", 60), 60)
        current_max_classify = _safe_int(merged.get("max_classify_articles", 8), 8)
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
            }
        )
        fast_model = os.getenv("MLX_FAST_MODEL", "").strip()
        if fast_model:
            merged["runtime_mlx_model"] = fast_model
        elif str(merged.get("runtime_mlx_model", "")).strip():
            merged["runtime_mlx_model"] = str(merged.get("runtime_mlx_model")).strip()

    return merged

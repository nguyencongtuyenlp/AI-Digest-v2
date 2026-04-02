"""
classify_types.py — LangGraph node: Classify articles using local MLX model.

Loads the master prompt from config/prompt_daily_digest.md, sends each article
to the local Qwen model via mlx_runner, and parses JSON output.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

import sys, os
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from mlx_runner import run_inference

logger = logging.getLogger(__name__)

PROMPT_PATH = Path(__file__).resolve().parent.parent / "config" / "prompt_daily_digest.md"


def _load_master_prompt() -> str:
    return PROMPT_PATH.read_text(encoding="utf-8")


def _build_user_prompt(article: dict) -> str:
    title = article.get("title", "N/A")
    source = article.get("source", "N/A")
    content = (article.get("full_content") or article.get("snippet", ""))[:3000]
    return (
        f"Phân loại bài viết sau:\n\n"
        f"**Title:** {title}\n"
        f"**Source:** {source}\n"
        f"**Content:**\n{content}\n\n"
        f"Trả về JSON theo format trong system prompt."
    )


def _parse_llm_json(raw_text: str) -> dict | None:
    text = raw_text.strip()
    if "```json" in text:
        text = text.split("```json", 1)[1].split("```", 1)[0]
    elif "```" in text:
        text = text.split("```", 1)[1].split("```", 1)[0]
    start, end = text.find("{"), text.rfind("}")
    if start != -1 and end != -1 and end > start:
        try:
            return json.loads(text[start: end + 1])
        except json.JSONDecodeError:
            pass
    logger.warning("Could not parse JSON from LLM output: %s", text[:200])
    return None


def classify_types_node(state: dict[str, Any]) -> dict[str, Any]:
    raw_articles = state.get("raw_articles", [])
    if not raw_articles:
        logger.info("No articles to classify.")
        return {"classified_articles": []}

    system_prompt = _load_master_prompt()
    classified: list[dict] = []

    for i, article in enumerate(raw_articles, 1):
        logger.info("🏷️  Classifying [%d/%d]: %s", i, len(raw_articles), article.get("title", "")[:60])
        try:
            raw_output = run_inference(system_prompt, _build_user_prompt(article), max_tokens=800)
            parsed = _parse_llm_json(raw_output)
            if parsed:
                classified.append({**article, **parsed})
            else:
                raise ValueError("JSON parse failed")
        except Exception as e:
            logger.error("Classification failed for '%s': %s", article.get("title", ""), e)
            article.update({
                "primary_type": "Practical", "primary_emoji": "🛠️",
                "relevance_score": 3, "summary_vi": article.get("snippet", "")[:200],
                "key_takeaways": [], "actionable_for_vn_startup": "N/A",
            })
            classified.append(article)

    classified.sort(key=lambda a: a.get("relevance_score", 0), reverse=True)
    logger.info("✅ Classified %d articles.", len(classified))
    return {"classified_articles": classified}


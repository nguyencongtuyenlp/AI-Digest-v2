"""
classify_and_score.py — LangGraph node: Phân loại + chấm relevance theo vai "editorial triage".

Mục tiêu là để Qwen local đóng vai tương tự một bộ lọc kiểu Claude:
  1. Classify: gán 1 trong 6 Primary Types
  2. Score: chấm 1-100 dựa trên 3 tiêu chí
  3. Decision: quyết định bài nào cần phân tích sâu, bài nào chỉ lưu cơ bản

6 Primary Types:
  🔬 Research    — Nghiên cứu mới, paper, benchmark, thuật toán mới, bài báo công nghệ tốt cho tối ưu AI trên phần cứng nhỏ mà có sức mạnh lớn,...
  🚀 Product     — Ra mắt sản phẩm, tính năng, API mới, mô hình AI mới, các sản phẩm AI mới, các sản phẩm AI thiết bị biên, ...
  💼 Business    — M&A, funding, chiến lược, nhân sự, doanh thu, lợi nhuận, ...
  ⚖️ Policy      — Luật, quy định, đạo đức AI, chính sách,...
  🌍 Society     — Tác động xã hội, văn hóa, giáo dục, ứng dụng AI vào giáo dục, vào xã hội, vào y tế, vào kinh doanh, ...
  🛠️ Practical   — Hướng dẫn, tips, tools, tutorials, ứng dụng AI vào đời sống,...

3 Tiêu chí chấm điểm (mỗi tiêu chí 0-33 điểm, tổng max 100):
  C1. Chất lượng tin: Relevance, Timeliness, Impact, Source credibility, những người đầu ngành nói gì về nó,...
  C2. Phù hợp startup AI: Ứng dụng được vào SME/startup Việt Nam không? Có thể học hỏi được gì từ nó, có thể làm gì với nó, có thể bán cho ai, có thể hợp tác với ai, ...
  C3. Phù hợp dự án hiện tại: AI Agent, Revenue automation, Enterprise mgmt, AI Product general, ...

Output ghi vào state:
  scored_articles: tất cả bài đã classify + score
  top_articles: bài có score >= MIN_DEEP_ANALYSIS_SCORE
  low_score_articles: bài có score thấp
"""

from __future__ import annotations

import logging
import os
import re
import sys
import unicodedata
from pathlib import Path
from typing import Any

# Đảm bảo project root nằm trong sys.path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from mlx_runner import run_json_inference_meta
from source_catalog import classify_source_kind
from xai_grok import (
    grok_prefilter_enabled,
    grok_prefilter_max_articles,
    rerank_prefilter_articles,
)

logger = logging.getLogger(__name__)


def _runtime_config(state: dict[str, Any]) -> dict[str, Any]:
    return dict(state.get("runtime_config", {}) or {})


def _cfg_int(state: dict[str, Any], key: str, env_key: str, default: int) -> int:
    value = _runtime_config(state).get(key)
    if value in (None, ""):
        value = os.getenv(env_key, str(default))
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _clean_reason_snippet(text: str, limit: int = 120) -> str:
    cleaned = re.sub(r"\s+", " ", str(text or "").strip())
    if len(cleaned) <= limit:
        return cleaned
    return cleaned[: limit - 1].rstrip() + "…"


STRATEGIC_KEYWORDS = (
    "open-source",
    "open source",
    "dominance",
    "partnership",
    "partners with",
    "market lead",
    "threatens",
    "race",
    "ecosystem",
    "infrastructure",
    "robotics",
    "advisory body",
    "warns",
    "competition",
)

BUSINESS_KEYWORDS = (
    "startup",
    "funding",
    "gọi vốn",
    "goi von",
    "partnership",
    "partners with",
    "market",
    "competition",
    "competitive",
    "cạnh tranh",
    "canh tranh",
    "race",
    "lead",
    "dominance",
    "robotics",
    "chiến lược",
    "chien luoc",
)

POLICY_KEYWORDS = (
    "law",
    "regulation",
    "policy",
    "governance",
    "compliance",
    "safety",
    "security",
    "cyberattack",
    "cyber attack",
    "breach",
    "compromise",
    "hack",
    "hacked",
    "leak",
    "lawsuit",
    "investigation",
)

SOCIETY_KEYWORDS = (
    "ecosystem",
    "student",
    "students",
    "education",
    "community",
)

AI_SIGNAL_KEYWORDS = (
    "ai",
    "agent",
    "model",
    "llm",
    "openai",
    "anthropic",
    "claude",
    "gpt",
    "deepmind",
    "gemini",
    "meta",
    "xai",
    "grok",
    "hugging face",
    "nvidia",
    "robot",
    "robotics",
    "chip",
    "inference",
    "training",
    "benchmark",
    "research",
    "startup",
    "funding",
    "acquisition",
    "partnership",
    "regulation",
    "policy",
)

OFF_SCOPE_KEYWORDS = (
    "smartphone",
    "camera",
    "melania trump",
    "meteor",
    "mảnh thiên thạch",
    "thien thach",
    "dịch vụ công",
    "dich vu cong",
    "weather",
    "football",
    "showbiz",
)

FOUNDER_SIGNAL_KEYWORDS = (
    "openai",
    "anthropic",
    "claude",
    "gpt",
    "gemini",
    "deepmind",
    "xai",
    "grok",
    "agent",
    "agents",
    "api",
    "sdk",
    "platform",
    "model",
    "llm",
    "research",
    "benchmark",
    "inference",
    "training",
    "chip",
    "gpu",
    "startup",
    "funding",
    "revenue",
    "enterprise",
    "workflow",
    "automation",
    "robot",
    "robotics",
    "regulation",
    "policy",
    "safety",
    "security",
    "vietnam",
    "asean",
)

EDITORIAL_NOISE_KEYWORDS = (
    "task manager",
    "quan ly tac vu",
    "quản lý tác vụ",
    "任务管理器",
    "win10",
    "win11",
    "jingyan",
    "điện thoại",
    "dien thoai",
    "smartphone",
    "camera",
    "mặt trăng",
    "mat trang",
    "xăng dầu",
    "xang dau",
    "dầu mỏ",
    "dau mo",
    "sephora",
    "benefit",
    "geforce now",
)

EDITORIAL_BLOCKED_DOMAINS = {
    "jingyan.baidu.com",
}

EDITORIAL_SOFT_BLOCKED_DOMAINS = {
    "zhihu.com",
}

TITLE_STOPWORDS = {
    "the", "a", "an", "and", "or", "for", "with", "into", "from", "that", "this",
    "news", "today", "latest", "update", "updates", "report", "reports", "says",
    "new", "launches", "launch", "announces", "introduces", "about", "after",
    "tai", "cua", "cho", "voi", "trong", "mot", "nhung", "nhat", "moi", "bao",
    "ve", "sau", "tren", "khi", "nguoi", "viet", "nam", "tri", "tue", "nhan", "tao",
}

TAG_TAXONOMY: dict[str, str] = {
    "model_release": "Model launches, upgrades, or notable capability jumps.",
    "product_update": "Major product, app, or feature releases.",
    "api_platform": "APIs, SDKs, integrations, and developer platforms.",
    "developer_tools": "Coding, workflow, and software-building tools.",
    "ai_agents": "Agents, copilots, assistants, and autonomous workflows.",
    "enterprise_ai": "Enterprise deployment, governance, and B2B adoption.",
    "open_source": "Open-source models, tooling, or ecosystem moves.",
    "infrastructure": "Compute, chips, cloud, data centers, inference, training.",
    "robotics": "Robots, embodied AI, and industrial automation.",
    "funding": "Funding, revenue milestones, and monetization signals.",
    "partnership": "Commercial, channel, or ecosystem partnerships.",
    "acquisition": "M&A, acquihires, and asset purchases.",
    "market_competition": "Competitive strategy, market-share moves, positioning.",
    "regulation": "Law, compliance, governance, and regulatory intervention.",
    "safety": "AI safety, security, alignment, red teaming, misuse prevention.",
    "government": "Public sector adoption, sovereign AI, state programs.",
    "education": "Schools, students, learning, and education use cases.",
    "healthcare": "Healthcare, hospitals, biotech, and medical applications.",
    "vietnam": "Vietnam-specific market, policy, or deployment angle.",
    "southeast_asia": "ASEAN or Southeast Asia regional angle.",
    "research": "Research papers, benchmarks, and scientific findings.",
}

TAG_ALIASES: dict[str, tuple[str, ...]] = {
    "model_release": (
        "model",
        "models",
        "foundation model",
        "llm",
        "reasoning model",
        "small language model",
    ),
    "product_update": ("product", "feature", "release", "rollout", "launch"),
    "api_platform": ("api", "apis", "sdk", "platform", "integration", "plugin"),
    "developer_tools": ("developer", "developers", "coding", "code", "tooling", "workflow"),
    "ai_agents": ("agent", "agents", "agentic", "copilot", "assistant", "automation"),
    "enterprise_ai": ("enterprise", "b2b", "workspace", "admin", "operations"),
    "open_source": ("open source", "open-source", "oss"),
    "infrastructure": (
        "infrastructure",
        "gpu",
        "chip",
        "chips",
        "compute",
        "cloud",
        "data center",
        "datacenter",
        "inference",
        "training",
        "semiconductor",
    ),
    "robotics": ("robot", "robots", "robotics", "humanoid"),
    "funding": ("funding", "fundraise", "fundraising", "revenue", "monetization", "valuation"),
    "partnership": ("partnership", "partnerships", "partner", "partners", "alliance", "collaboration"),
    "acquisition": ("acquisition", "acquire", "acquires", "merger", "m&a", "acquihire"),
    "market_competition": ("competition", "competitive", "race", "market", "dominance", "positioning"),
    "regulation": ("regulation", "policy", "compliance", "law", "legal", "governance", "ban"),
    "safety": ("safety", "security", "alignment", "guardrail", "guardrails", "red team", "red teaming"),
    "government": ("government", "public sector", "state", "ministry", "sovereign", "national"),
    "education": ("education", "school", "schools", "student", "students", "teacher", "learning"),
    "healthcare": ("healthcare", "medical", "medicine", "hospital", "biotech", "drug discovery"),
    "vietnam": ("vietnam", "viet nam", "hanoi", "ho chi minh"),
    "southeast_asia": ("southeast asia", "asean", "singapore", "indonesia", "thailand", "malaysia", "philippines"),
    "research": ("research", "paper", "papers", "benchmark", "study", "scientific"),
}

TAG_SIGNAL_KEYWORDS: dict[str, tuple[str, ...]] = {
    "funding": (
        "funding",
        "raised",
        "raises",
        "series a",
        "series b",
        "valuation",
        "revenue",
        "annual recurring revenue",
    ),
    "acquisition": ("acquisition", "acquire", "acquires", "acquired", "merger", "buying"),
    "partnership": ("partnership", "partnered", "partners with", "collaborates with", "alliance"),
    "market_competition": ("competition", "competitive", "race", "dominance", "market share", "versus"),
    "regulation": ("regulation", "policy", "compliance", "law", "legal", "governance", "ban"),
    "safety": ("safety", "security", "alignment", "red team", "guardrail", "misuse"),
    "government": ("government", "ministry", "public sector", "state", "sovereign ai", "national strategy"),
    "research": ("research", "paper", "benchmark", "study", "scientists"),
    "model_release": (
        "new model",
        "model release",
        "foundation model",
        "reasoning model",
        "llm",
        "language model",
    ),
    "product_update": ("launches", "launch", "released", "release", "rollout", "feature"),
    "api_platform": ("api", "sdk", "platform", "integration", "plugin", "developer api"),
    "developer_tools": ("developer", "developers", "coding", "code generation", "cli", "tooling", "workflow"),
    "ai_agents": ("agent", "agents", "agentic", "copilot", "assistant", "autonomous workflow"),
    "enterprise_ai": ("enterprise", "workspace", "admin", "governance", "b2b", "deployment"),
    "open_source": ("open source", "open-source", "weights", "model weights", "oss"),
    "infrastructure": (
        "gpu",
        "chip",
        "chips",
        "compute",
        "cloud",
        "data center",
        "datacenter",
        "inference",
        "training cluster",
    ),
    "robotics": ("robot", "robots", "robotics", "humanoid", "warehouse automation"),
    "education": ("education", "school", "schools", "student", "students", "teacher"),
    "healthcare": ("healthcare", "medical", "hospital", "clinic", "biotech", "drug discovery"),
    "vietnam": ("vietnam", "viet nam", "hanoi", "ho chi minh"),
    "southeast_asia": ("asean", "southeast asia", "singapore", "indonesia", "thailand", "malaysia", "philippines"),
}

TAG_PRIORITY: tuple[str, ...] = (
    "funding",
    "acquisition",
    "partnership",
    "market_competition",
    "regulation",
    "safety",
    "government",
    "research",
    "model_release",
    "product_update",
    "api_platform",
    "developer_tools",
    "ai_agents",
    "enterprise_ai",
    "open_source",
    "infrastructure",
    "robotics",
    "education",
    "healthcare",
    "vietnam",
    "southeast_asia",
)

TAG_TYPE_DEFAULTS: dict[str, str] = {
    "Research": "research",
    "Product": "product_update",
    "Policy": "regulation",
    "Practical": "developer_tools",
}

TAG_TAXONOMY_BLOCK = "\n".join(
    f"- `{tag}`: {description}"
    for tag, description in TAG_TAXONOMY.items()
)

# ── Prompt Master cho classify + score ───────────────────────────────
CLASSIFY_SCORE_SYSTEM = """Bạn là Editorial Triage Lead cho một sản phẩm AI Daily Digest trả phí.
Nhiệm vụ: đọc nhanh từng nguồn tin AI/Tech, chấm điểm relevance như một biên tập viên khó tính,
và quyết định bài nào đáng được đưa vào phân tích sâu.

## 6 Primary Types (CHỌN ĐÚNG 1):
- 🔬 Research: Nghiên cứu mới, paper khoa học, benchmark, thuật toán mới
- 🚀 Product: Ra mắt sản phẩm, tính năng mới, API, platform update
- 💼 Business: M&A, funding, chiến lược kinh doanh, tuyển dụng, cạnh tranh
- ⚖️ Policy: Luật pháp, quy định, đạo đức AI, governance
- 🌍 Society: Tác động xã hội, văn hóa, giáo dục, việc làm
- 🛠️ Practical: Hướng dẫn, tips, tools, tutorials, best practices

## 3 Tiêu chí chấm điểm (mỗi tiêu chí 0-33, tổng max 100):

### C1: Chất lượng tin tức (0-33)
- Nguồn đáng tin cậy? (Reuters, TechCrunch, MIT = cao; blog random = thấp)
- Tin mới (24-48h qua) hay cũ?
- Tác động lớn đến ngành AI?
- Có dữ liệu/dẫn chứng cụ thể?
- Tin chiến lược ở nguồn mạnh về cạnh tranh AI, open-source, partnership, đầu tư hạ tầng, hay cảnh báo từ cơ quan lớn phải được xem là impact cao, kể cả khi nội dung đầu vào ngắn.

### C2: Phù hợp startup AI Việt Nam (0-33)
- Startup AI tại Việt Nam có thể ứng dụng/học hỏi?
- Liên quan đến thị trường Đông Nam Á?
- Có cơ hội kinh doanh?

### C3: Phù hợp dự án hiện tại (0-34)
Công ty đang phát triển 4 MVP:
1. AI News Digest (thu thập, phân tích, tổng hợp tin tức AI tự động)
2. AI Revenue Calculator (tính toán doanh thu, truy cập mọi nơi trong công ty)
3. AI Enterprise Management (quản lý toàn bộ công ty)
4. AI Product general (hướng phát triển sản phẩm AI)
- Tin này có giúp cải thiện sản phẩm nào ở trên không?
- Có công nghệ/ý tưởng mới áp dụng được?

## Rules bổ sung:
- Ưu tiên phân loại `Business` nếu bài nói về cạnh tranh thị trường, open-source race, partnership, strategic move, market lead, ecosystem control, hoặc tác động đến vị thế công ty/quốc gia trong ngành.
- Chỉ chọn `Policy` nếu trọng tâm chính là luật, quy định, compliance, governance, an toàn, hoặc can thiệp của cơ quan quản lý.
- Cấp độ phù hợp (relevance_level): High (Tổng C1+C2+C3 >= 70), Medium (40-69), Low (< 40)
- Mức xử lý (analysis_tier):
  - deep: bài đủ mạnh để đầu tư research/thinking sâu
  - basic: nên lưu và đưa vào digest, nhưng không cần research dài
  - skip: tín hiệu yếu, ít giá trị với sản phẩm kinh doanh
- Nếu nguồn mạnh (đặc biệt Reuters/CNBC/Bloomberg/TechCrunch) và chủ đề mang tính chiến lược cấp ngành, mặc định nghiêng về `deep` trừ khi bài quá mỏng hoặc không liên quan AI.
- editorial_angle: 1 câu nói rõ "điểm đáng quan tâm nhất" của bài này dưới góc nhìn người vận hành startup AI
- Tags: Chỉ được chọn 1-3 tag từ taxonomy chuẩn bên dưới.
- Không được bịa tag mới, không dùng tên công ty/người/số tiền làm tag, không copy nguyên title.
- Nếu không có tag nào đủ chắc, trả `[]`.
- KHÔNG TỰ TÍNH TỔNG ĐIỂM trong JSON (bộ phận Python sẽ lo việc tính tổng).
- Không hype. Không dùng ngôn ngữ quảng cáo. Nếu thiếu dữ liệu, nói rõ là thiếu dữ liệu.
- Nếu Content_available=false (thiếu nội dung) hoặc Published_at trống: hạ điểm C1, tránh kết luận mạnh.

## Tag Taxonomy
__TAG_TAXONOMY__

## Output: JSON (KHÔNG markdown, KHÔNG giải thích thêm)
{
  "primary_type": "Research|Product|Business|Policy|Society|Practical",
  "primary_emoji": "🔬|🚀|💼|⚖️|🌍|🛠️",
  "c1_score": 0-33,
  "c1_reason": "Giải thích ngắn gọn (1 câu)",
  "c2_score": 0-33,
  "c2_reason": "Giải thích ngắn gọn (1 câu)",
  "c3_score": 0-34,
  "c3_reason": "Giải thích ngắn gọn (1 câu)",
  "summary_vi": "Tóm tắt 2-3 câu bằng tiếng Việt",
  "editorial_angle": "1 câu về điểm đáng quan tâm nhất",
  "analysis_tier": "deep|basic|skip",
  "tags": ["tag1", "tag2", "tag3"],
  "relevance_level": "High|Medium|Low"
}""".replace("__TAG_TAXONOMY__", TAG_TAXONOMY_BLOCK)

CLASSIFY_SCORE_USER_TEMPLATE = """Phân loại và chấm điểm bài viết sau:

Tiêu đề: {title}
URL: {url}
Nguồn: {source}
Domain: {source_domain}
Published_at (UTC ISO): {published_at}
Published_at_source: {published_at_source}
Discovered_at: {discovered_at}
Age_hours: {age_hours}
Freshness_unknown: {freshness_unknown}
Is_stale_candidate: {is_stale_candidate}
Source_verified (heuristic): {source_verified}
Content_available: {content_available}
Nội dung: {content}

{related_context}

--- FEEDBACK GAN DAY TU TEAM ---
{feedback_context}

Trả về JSON theo format yêu cầu."""

CLASSIFY_RETRY_SUFFIX = """

YÊU CẦU LẦN 2:
- Chỉ trả về đúng 1 JSON object hợp lệ.
- Không dùng markdown fence.
- Không giải thích thêm trước hoặc sau JSON.
- Đảm bảo field `tags` là list tag trong taxonomy hoặc [].
"""

CLASSIFY_LAST_CHANCE_SUFFIX = """

YÊU CẦU LẦN 3:
- Trả về duy nhất 1 JSON object trên MỘT khối văn bản, không prose.
- Tất cả key PHẢI có double quotes.
- Không markdown fence.
- Không giải thích.
- Nếu thiếu dữ liệu, vẫn phải điền score thấp và chọn `skip` hoặc `basic`, không được bỏ JSON.
"""

PROSE_TYPE_HINTS: dict[str, tuple[str, ...]] = {
    "Policy": POLICY_KEYWORDS + ("luật", "pháp lý", "quan ly"),
    "Business": BUSINESS_KEYWORDS,
    "Product": (
        "launch",
        "launches",
        "release",
        "released",
        "release notes",
        "api",
        "model",
        "feature",
        "introduces",
        "ships",
        "highlights",
        "ra mắt",
        "ra mat",
        "công bố",
        "cong bo",
    ),
    "Research": ("paper", "research", "benchmark", "study", "nghiên cứu"),
    "Practical": ("tutorial", "guide", "tool", "workflow", "tips"),
}


def _prefilter_primary_type(title: str) -> tuple[str, str]:
    lowered = str(title or "").lower()
    if any(token in lowered for token in POLICY_KEYWORDS + ("luật", "pháp lý", "quan ly")):
        return "Policy", "⚖️"
    if any(token in lowered for token in ("paper", "research", "benchmark", "study", "nghiên cứu")):
        return "Research", "🔬"
    if any(token in lowered for token in BUSINESS_KEYWORDS):
        return "Business", "💼"
    if any(
        token in lowered
        for token in (
            "launch",
            "launches",
            "release",
            "released",
            "release notes",
            "api",
            "model",
            "feature",
            "introduces",
            "ships",
            "highlights",
            "ra mắt",
            "ra mat",
            "công bố",
            "cong bo",
        )
    ):
        return "Product", "🚀"
    if any(token in lowered for token in ("tutorial", "guide", "tool", "workflow", "tips")):
        return "Practical", "🛠️"
    return "Society", "🌍"


def run_json_inference(
    system_prompt: str,
    user_prompt: str,
    *,
    max_tokens: int,
    temperature: float,
) -> tuple[dict | list | None, str, bool]:
    """
    Local compatibility wrapper for classify inference.

    Một số test cũ patch `nodes.classify_and_score.run_json_inference`, nên giữ
    hook này ổn định dù runtime hiện tại dùng `run_json_inference_meta`.
    """
    return run_json_inference_meta(
        system_prompt,
        user_prompt,
        max_tokens=max_tokens,
        temperature=temperature,
    )


def _normalize_classify_inference_response(response: Any) -> tuple[dict[str, Any] | None, str, bool]:
    if isinstance(response, tuple) and len(response) == 3:
        parsed, raw, looks_structured = response
        return parsed if isinstance(parsed, dict) else None, str(raw or ""), bool(looks_structured)
    if isinstance(response, dict):
        return response, "", True
    if response is None:
        return None, "", True
    raw = str(response or "")
    stripped = raw.lstrip()
    looks_structured = stripped.startswith("{") or stripped.startswith("[") or "```json" in raw.lower()
    return None, raw, looks_structured


def _is_likely_prose_response(raw: str) -> bool:
    text = str(raw or "").strip()
    if not text:
        return False
    lowered = text.lower()
    if "```json" in lowered or text.startswith("{") or text.startswith("["):
        return False
    if "primary_type" in lowered or '"primary_type"' in lowered:
        return False
    if text.count("{") >= 1 and text.count("}") >= 1:
        return False
    sentence_count = len(re.findall(r"[.!?…]\s+", text))
    return len(text) >= 120 and sentence_count >= 2


def _clean_prose_snippet(text: str, limit: int = 280) -> str:
    cleaned = " ".join(str(text or "").replace("\ufeff", "").split())
    cleaned = re.sub(r"^\s*(đây là|tóm tắt:|summary:)\s*", "", cleaned, flags=re.IGNORECASE)
    if len(cleaned) <= limit:
        return cleaned
    return cleaned[: limit - 1].rsplit(" ", 1)[0].rstrip(" ,;:-") + "…"


def _extract_model_sentences(raw: str) -> list[str]:
    text = _clean_prose_snippet(raw, limit=900)
    if not text:
        return []
    chunks = re.split(r"(?<=[.!?…])\s+", text)
    return [chunk.strip() for chunk in chunks if chunk.strip()]


def _infer_type_from_prose(raw: str, fallback_type: str) -> str:
    lowered = str(raw or "").lower()
    for candidate, keywords in PROSE_TYPE_HINTS.items():
        if any(keyword in lowered for keyword in keywords):
            return candidate
    return fallback_type


def _classify_prose_rescue(article: dict[str, Any], raw: str, min_score: int) -> None:
    """
    Khi model trả prose thay vì JSON, vẫn tận dụng copy của model để nâng chất
    lượng summary thay vì rơi thẳng về fallback thô.
    """
    _llm_failure_fallback(article, min_score)

    sentences = _extract_model_sentences(raw)
    if not sentences:
        return

    fallback_type = str(article.get("primary_type", "Practical") or "Practical")
    rescued_type = _infer_type_from_prose(raw, fallback_type)
    article["primary_type"] = rescued_type

    summary = " ".join(sentences[:2]).strip()
    if summary:
        article["summary_vi"] = _clean_prose_snippet(summary, limit=260)

    if len(sentences) >= 2:
        article["editorial_angle"] = _clean_prose_snippet(sentences[1], limit=180)
    else:
        article["editorial_angle"] = _clean_prose_snippet(
            f"Điểm đáng chú ý là {sentences[0][:140]}",
            limit=180,
        )

    if article.get("analysis_tier") == "skip" and int(article.get("total_score", 0) or 0) >= max(38, min_score - 22):
        article["analysis_tier"] = "basic"

    _normalize_primary_type(article)
    _normalize_article_tags(article)


def _classify_inference_with_retry(
    user_prompt: str,
    *,
    max_tokens: int,
    temperature: float,
    initial_response: Any | None = None,
) -> dict[str, Any] | None:
    if initial_response is None:
        initial_response = run_json_inference(
            CLASSIFY_SCORE_SYSTEM,
            user_prompt,
            max_tokens=max_tokens,
            temperature=temperature,
        )
    result, raw, looks_structured = _normalize_classify_inference_response(initial_response)
    if result and isinstance(result, dict):
        return result
    if _is_likely_prose_response(raw):
        logger.warning(
            "⚠️ Classify trả prose rõ ràng, bỏ retry để dùng prose rescue/fallback nhanh hơn. Snippet=%s",
            raw[:180].replace("\n", " "),
        )
        return None
    if not looks_structured:
        logger.warning(
            "⚠️ Classify trả prose thay vì JSON, bỏ retry để dùng fallback nhanh hơn. Snippet=%s",
            raw[:180].replace("\n", " "),
        )
        return None

    logger.warning("⚠️ Classify JSON parse failed, retrying once with stricter JSON-only prompt.")
    retry_prompt = user_prompt + CLASSIFY_RETRY_SUFFIX
    retry_result, retry_raw, retry_looks_structured = _normalize_classify_inference_response(
        run_json_inference(
            CLASSIFY_SCORE_SYSTEM,
            retry_prompt,
            max_tokens=max_tokens,
            temperature=0.0,
        )
    )
    if retry_result and isinstance(retry_result, dict):
        return retry_result
    if not retry_looks_structured:
        logger.warning(
            "⚠️ Retry classify vẫn trả prose, dừng tại đây để dùng fallback. Snippet=%s",
            retry_raw[:180].replace("\n", " "),
        )
        return None

    logger.warning("⚠️ Classify vẫn chưa ra JSON, thử thêm 1 lần với compact JSON prompt.")
    last_chance_prompt = user_prompt + CLASSIFY_LAST_CHANCE_SUFFIX
    final_result, final_raw, _ = _normalize_classify_inference_response(
        run_json_inference(
            CLASSIFY_SCORE_SYSTEM,
            last_chance_prompt,
            max_tokens=min(max_tokens, 220),
            temperature=0.0,
        )
    )
    if final_result and isinstance(final_result, dict):
        return final_result
    logger.debug("Last chance raw classify output: %s", final_raw[:500])
    return None


def _llm_failure_fallback(article: dict[str, Any], min_score: int) -> None:
    title = str(article.get("title", "") or "Bài viết")
    lowered = title.lower()
    source_tier = str(article.get("source_tier", "unknown") or "unknown").lower()
    prefilter_score = int(article.get("prefilter_score", 0) or 0)
    ai_relevant = article.get("is_ai_relevant") is not False
    content_available = bool(article.get("content_available", False))
    primary_type, primary_emoji = _prefilter_primary_type(title)

    total_score = max(12, min(72, prefilter_score + 18))
    if source_tier == "a":
        total_score = min(78, total_score + 6)
    elif source_tier == "b":
        total_score = min(74, total_score + 3)

    if not ai_relevant:
        total_score = min(total_score, 18)

    analysis_tier = "skip"
    strong_basic_signal = (
        ai_relevant
        and content_available
        and source_tier in {"a", "b"}
        and any(
            token in lowered
            for token in (
                BUSINESS_KEYWORDS
                + POLICY_KEYWORDS
                + (
                    "launch",
                    "release",
                    "feature",
                    "api",
                    "model",
                    "highlights",
                    "recap",
                    "glasses",
                )
            )
        )
    )
    if total_score >= max(40, min_score - 20):
        analysis_tier = "basic"
    elif strong_basic_signal and total_score >= max(30, min_score - 30):
        analysis_tier = "basic"
    if (
        total_score >= min_score
        or (
            source_tier == "a"
            and prefilter_score >= max(26, min_score - 28)
            and any(token in lowered for token in ("model", "api", "agent", "agents", "release", "benchmark"))
        )
    ):
        analysis_tier = "deep"

    summary = "Tin này có tín hiệu đáng theo dõi nhưng model classify chưa trả JSON ổn định, nên hệ đang giữ ở mức fallback có kiểm soát."
    editorial_angle = "Nên giữ bài này trong workspace review vì có tín hiệu founder-grade, nhưng chưa đủ chắc để kết luận mạnh."

    if not ai_relevant:
        summary = "Tin này chưa đủ liên quan trực tiếp tới AI để ưu tiên cao trong brief hiện tại."
        editorial_angle = "Không nên chiếm slot brief khi chưa có góc AI rõ ràng."
        analysis_tier = "skip"

    article.update(
        {
            "primary_type": primary_type,
            "primary_emoji": primary_emoji,
            "c1_score": max(0, min(33, total_score // 2)),
            "c1_reason": "Nguồn hoặc tiêu đề có tín hiệu đủ mạnh để giữ lại review, nhưng classify JSON bị lỗi nên chỉ chấm fallback.",
            "c2_score": max(0, min(24, total_score // 4)),
            "c2_reason": "Bài có mức liên quan nhất định tới nhu cầu startup AI/founder workflow.",
            "c3_score": max(0, min(24, total_score // 4)),
            "c3_reason": "Có thể hữu ích cho định hướng agent, sản phẩm AI hoặc theo dõi đối thủ.",
            "total_score": total_score,
            "summary_vi": summary,
            "editorial_angle": editorial_angle,
            "analysis_tier": analysis_tier,
            "tags": [],
        }
    )
    _recompute_relevance_level(article)
    _normalize_primary_type(article)
    _normalize_article_tags(article)


def _prefilter_score(article: dict[str, Any]) -> tuple[int, list[str]]:
    score = 0
    reasons: list[str] = []
    title = str(article.get("title", "") or "")
    lowered_title = title.lower()
    lowered_text = " ".join(
        part for part in [
            title,
            str(article.get("snippet", "") or ""),
            str(article.get("url", "") or ""),
            str(article.get("source_domain", "") or ""),
        ]
        if part
    ).lower()
    source_tier = str(article.get("source_tier", "unknown")).lower()
    source_domain = str(article.get("source_domain", "") or "").lower()
    freshness_bucket = str(article.get("freshness_bucket", "unknown")).lower()
    source_kind = str(article.get("source_kind", "unknown") or "unknown").lower()
    source_priority = int(article.get("source_priority", 0) or 0)
    community_strength = int(article.get("community_signal_strength", 0) or 0)
    watchlist_hit = bool(article.get("watchlist_hit", False))

    tier_bonus = {"a": 10, "b": 7, "c": 3, "unknown": 1}.get(source_tier, 0)
    score += tier_bonus
    reasons.append(f"tier:{source_tier}+{tier_bonus}")

    priority_bonus = 0
    if source_priority >= 90:
        priority_bonus = 4
    elif source_priority >= 82:
        priority_bonus = 3
    elif source_priority >= 74:
        priority_bonus = 2
    elif source_priority >= 60:
        priority_bonus = 1
    if priority_bonus:
        score += priority_bonus
        reasons.append(f"source_kind:{source_kind}+{priority_bonus}")

    if watchlist_hit:
        score += 2
        reasons.append("watchlist_hit+2")

    if community_strength:
        bonus = min(4, community_strength)
        score += bonus
        reasons.append(f"community_signal+{bonus}")

    freshness_bonus = {
        "breaking": 10,
        "fresh": 8,
        "recent": 5,
        "aging": -3,
        "stale": -8,
        "unknown": 0,
    }.get(freshness_bucket, 0)
    score += freshness_bonus
    if freshness_bonus:
        reasons.append(f"freshness:{freshness_bucket}{freshness_bonus:+d}")

    if article.get("content_available"):
        score += 4
        reasons.append("content+4")
    else:
        score -= 3
        reasons.append("thin_content-3")

    if article.get("is_news_candidate") is False:
        score -= 12
        reasons.append("not_news_candidate-12")

    if article.get("source_verified"):
        score += 3
        reasons.append("verified+3")

    if article.get("related_past"):
        score += 1
        reasons.append("related_history+1")

    if article.get("is_ai_relevant") is False:
        score -= 20
        reasons.append("not_ai_relevant-20")
    elif article.get("is_ai_relevant") is True:
        score += 4
        reasons.append("ai_relevant+4")

    ai_hits = sum(1 for keyword in AI_SIGNAL_KEYWORDS if keyword in lowered_title)
    if ai_hits:
        ai_bonus = min(8, ai_hits * 2)
        score += ai_bonus
        reasons.append(f"ai_hits+{ai_bonus}")

    founder_hits = sum(1 for keyword in FOUNDER_SIGNAL_KEYWORDS if keyword in lowered_text)
    if founder_hits:
        founder_bonus = min(6, founder_hits)
        score += founder_bonus
        reasons.append(f"founder_hits+{founder_bonus}")
    elif source_tier == "c":
        score -= 6
        reasons.append("no_founder_signal_c_source-6")

    offscope_hits = [keyword for keyword in OFF_SCOPE_KEYWORDS if keyword in lowered_title]
    if offscope_hits:
        penalty = min(10, len(offscope_hits) * 4)
        score -= penalty
        reasons.append(f"offscope-{penalty}")

    noise_hits = [keyword for keyword in EDITORIAL_NOISE_KEYWORDS if keyword in lowered_text]
    if noise_hits:
        penalty = min(18, 8 + len(noise_hits) * 3)
        score -= penalty
        reasons.append(f"editorial_noise-{penalty}")

    if source_domain in EDITORIAL_BLOCKED_DOMAINS:
        score -= 30
        reasons.append("blocked_domain-30")
    elif source_domain in EDITORIAL_SOFT_BLOCKED_DOMAINS and article.get("is_ai_relevant") is not True:
        score -= 18
        reasons.append("soft_blocked_domain-18")

    if article.get("is_old_news"):
        score -= 8
        reasons.append("old_news-8")

    if article.get("is_stale_candidate"):
        score -= 12
        reasons.append("stale-12")

    return score, reasons


def _prefilter_sort_key(article: dict[str, Any]) -> tuple[int, int, int, int, int, int]:
    freshness_rank = {
        "breaking": 5,
        "fresh": 4,
        "recent": 3,
        "unknown": 2,
        "aging": 1,
        "stale": 0,
    }
    tier_rank = {"a": 3, "b": 2, "c": 1, "unknown": 0}
    return (
        1 if article.get("grok_prefilter_keep") else 0,
        int(article.get("grok_prefilter_priority_score", -1) or -1),
        int(article.get("prefilter_score", 0) or 0),
        freshness_rank.get(str(article.get("freshness_bucket", "unknown")).lower(), 0),
        tier_rank.get(str(article.get("source_tier", "unknown")).lower(), 0),
        1 if article.get("content_available") else 0,
    )


def _is_github_signal_article(article: dict[str, Any]) -> bool:
    source_domain = str(article.get("source_domain", "") or "").strip().lower()
    return (
        source_domain == "github.com"
        or bool(str(article.get("github_full_name", "") or "").strip())
        or str(article.get("github_signal_type", "") or "").strip().lower() in {"repository", "release"}
    )


def _prefilter_predicted_type(article: dict[str, Any]) -> str:
    return _prefilter_primary_type(str(article.get("title", "") or ""))[0]


def _apply_grok_prefilter_rerank(
    ranked_articles: list[dict[str, Any]],
    *,
    runtime_config: dict[str, Any] | None = None,
    feedback_summary_text: str = "",
) -> None:
    if not grok_prefilter_enabled(runtime_config):
        return

    shortlist = [
        article
        for article in ranked_articles
        if not _is_github_signal_article(article)
    ][:grok_prefilter_max_articles(runtime_config)]
    if not shortlist:
        return

    logger.info(
        "🧠 Grok headline prefilter: sending %d candidates before local 32B classify.",
        len(shortlist),
    )
    reranked = rerank_prefilter_articles(shortlist, feedback_summary_text=feedback_summary_text)
    if not reranked:
        return

    updated = 0
    for article in shortlist:
        article_key = article.get("url", "") or article.get("title", "")
        judged = reranked.get(article_key)
        if not judged:
            continue
        article["grok_prefilter_keep"] = bool(judged.get("keep_for_local", False))
        article["grok_prefilter_priority_score"] = int(judged.get("priority_score", 0) or 0)
        rationale = str(judged.get("rationale", "") or "").strip()
        if rationale:
            article["grok_prefilter_rationale"] = rationale
        updated += 1

    logger.info("✅ Grok headline prefilter annotated %d/%d candidates.", updated, len(shortlist))


def _take_diverse_prefilter_articles(articles: list[dict[str, Any]], limit: int) -> list[dict[str, Any]]:
    if limit <= 0:
        return []

    ordered = sorted(articles, key=_prefilter_sort_key, reverse=True)
    selected: list[dict[str, Any]] = []
    seen_types: set[str] = set()

    for article in ordered:
        predicted_type = _prefilter_predicted_type(article)
        if predicted_type in seen_types:
            continue
        selected.append(article)
        seen_types.add(predicted_type)
        if len(selected) >= limit:
            return selected

    for article in ordered:
        if article in selected:
            continue
        selected.append(article)
        if len(selected) >= limit:
            break

    return selected


def _prepare_classify_candidates(
    articles: list[dict[str, Any]],
    max_candidates: int,
    *,
    runtime_config: dict[str, Any] | None = None,
    feedback_summary_text: str = "",
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    ranked: list[dict[str, Any]] = []
    deprioritized: list[dict[str, Any]] = []
    for article in articles:
        prefilter_score, reasons = _prefilter_score(article)
        article["prefilter_score"] = prefilter_score
        article["prefilter_reasons"] = reasons
        article["prefilter_primary_type"] = _prefilter_predicted_type(article)
        if prefilter_score <= 0 and any(
            reason.startswith(("editorial_noise", "blocked_domain", "soft_blocked_domain", "not_ai_relevant"))
            for reason in reasons
        ):
            deprioritized.append(article)
            continue
        ranked.append(article)

    ranked.sort(key=_prefilter_sort_key, reverse=True)
    _apply_grok_prefilter_rerank(
        ranked,
        runtime_config=runtime_config,
        feedback_summary_text=feedback_summary_text,
    )
    ranked.sort(key=_prefilter_sort_key, reverse=True)
    if max_candidates <= 0:
        return [], ranked + deprioritized

    main_ranked = [article for article in ranked if not _is_github_signal_article(article)]
    github_ranked = [article for article in ranked if _is_github_signal_article(article)]

    main_target = min(
        len(main_ranked),
        max(1, max_candidates if not github_ranked else (max_candidates * 2 + 2) // 3),
    )
    github_target = min(len(github_ranked), max_candidates - main_target)

    selected = _take_diverse_prefilter_articles(main_ranked, main_target)
    selected.extend(github_ranked[:github_target])

    selected_ids = {id(article) for article in selected}
    remaining = [article for article in ranked if id(article) not in selected_ids]
    if len(selected) < max_candidates:
        selected.extend(remaining[: max_candidates - len(selected)])
        selected_ids = {id(article) for article in selected}
        remaining = [article for article in ranked if id(article) not in selected_ids]

    return selected[:max_candidates], remaining + deprioritized


def _held_out_article_fallback(article: dict[str, Any]) -> None:
    title = str(article.get("title", "") or "Bài viết")
    prefilter_score = int(article.get("prefilter_score", 0) or 0)
    primary_type, primary_emoji = _prefilter_primary_type(title)
    ai_relevant = article.get("is_ai_relevant") is not False
    source_tier = str(article.get("source_tier", "unknown") or "unknown").lower()
    freshness_bucket = str(article.get("freshness_bucket", "unknown") or "unknown").lower()
    content_available = bool(article.get("content_available", False))
    prefilter_reasons = [str(reason or "") for reason in article.get("prefilter_reasons", [])]
    editorial_noise = any(
        reason.startswith(("editorial_noise", "blocked_domain", "soft_blocked_domain"))
        for reason in prefilter_reasons
    )
    strong_main_signal = (
        not _is_github_signal_article(article)
        and ai_relevant
        and not editorial_noise
        and source_tier in {"a", "b"}
        and freshness_bucket in {"breaking", "fresh", "recent"}
        and content_available
    )
    total_cap = 52 if strong_main_signal else 36
    total_score = max(0, min(total_cap, prefilter_score + (16 if strong_main_signal else 12)))

    article.update({
        "primary_type": primary_type,
        "primary_emoji": primary_emoji,
        "c1_score": max(0, min(20 if strong_main_signal else 16, total_score // 2)),
        "c1_reason": "Bài này mới chỉ có tín hiệu sơ bộ nên chưa được ưu tiên chấm sâu ở vòng đầu.",
        "c2_score": max(0, min(16 if strong_main_signal else 10, total_score // 4)),
        "c2_reason": "Giá trị thực tế hiện chưa đủ rõ để ưu tiên cao hơn các tin mới hơn.",
        "c3_score": max(0, min(16 if strong_main_signal else 10, total_score // 4)),
        "c3_reason": "Nếu chủ đề này xuất hiện lại ở nguồn mạnh hơn thì nên xét lại.",
        "total_score": total_score,
        "summary_vi": (
            "Tin này hiện phù hợp để theo dõi thêm, chưa phải ưu tiên cao nhất trong lượt chọn hiện tại."
        ),
        "editorial_angle": (
            "Tạm thời chỉ nên theo dõi thêm và chờ tín hiệu rõ hơn từ nguồn mạnh."
            if not strong_main_signal
            else "Nguồn và độ mới khá ổn, nên vẫn đáng cân nhắc ở lane review dù chưa được 32B chấm sâu."
        ),
        "analysis_tier": "basic" if total_score >= (28 if strong_main_signal else 24) and ai_relevant else "skip",
        "tags": [],
    })
    if not ai_relevant:
        article["summary_vi"] = "Tin này chưa đủ liên quan trực tiếp tới AI để ưu tiên đưa vào brief."
        article["editorial_angle"] = "Không nên chiếm slot brief khi chưa có góc AI rõ ràng."
        article["analysis_tier"] = "skip"
        article["total_score"] = min(article["total_score"], 20)
    if editorial_noise:
        article["summary_vi"] = "Tin này lệch khá xa nhu cầu founder-grade hiện tại, nên không nên chiếm chỗ trong brief."
        article["editorial_angle"] = "Bỏ qua ở vòng đầu để dành tài nguyên cho tín hiệu AI/product/business mạnh hơn."
        article["analysis_tier"] = "skip"
        article["total_score"] = min(article["total_score"], 8)
    _recompute_relevance_level(article)
    _normalize_article_tags(article)


def _build_related_context(article: dict) -> str:
    """
    Nếu bài viết có related_past (từ deduplicate), thêm vào context
    để model biết "đã từng đưa tin chủ đề này".
    """
    related = article.get("related_past", [])
    if not related:
        return ""

    lines = ["Bài viết LIÊN QUAN ĐÃ ĐĂNG trước đó:"]
    for r in related[:3]:
        lines.append(f"  - [{r.get('primary_type', '?')}] {r.get('title', 'N/A')}")
    lines.append("Hãy xem xét: bài mới có điểm gì khác so với bài cũ?")
    return "\n".join(lines)


def _apply_strategic_boost(article: dict[str, Any], min_score: int) -> None:
    """
    Heuristic nhẹ để tránh under-score các tin chiến lược nguồn mạnh.
    Không thay thế model; chỉ kéo lên khi hội đủ tín hiệu rõ ràng.
    """
    domain = str(article.get("source_domain", "")).lower()
    title = str(article.get("title", "")).lower()
    source_tier = str(article.get("source_tier", "")).lower()
    content_available = bool(article.get("content_available", False))

    strategic = any(keyword in title for keyword in STRATEGIC_KEYWORDS)
    strong_source = source_tier == "a" or domain in {"reuters.com", "cnbc.com", "bloomberg.com", "techcrunch.com"}

    if not (strategic and strong_source):
        return

    current_type = str(article.get("primary_type", ""))
    score = int(article.get("total_score", 0) or 0)

    # Kéo `Policy` về `Business` nếu bản chất là cuộc đua/chien luoc thi truong.
    if current_type == "Policy":
        article["primary_type"] = "Business"
        article["primary_emoji"] = "💼"

    # Nguồn mạnh + tín hiệu chiến lược + score đã khá gần ngưỡng thì đẩy lên deep.
    if score >= max(40, min_score - 20):
        article["analysis_tier"] = "deep"
        article["relevance_level"] = "High" if score >= 55 else article.get("relevance_level", "Medium")

        # Nếu content đầy đủ, boost thêm chút để tăng cơ hội vào top list.
        if content_available and score < min_score:
            article["total_score"] = min(min_score, score + 8)


def _normalize_key(text: str) -> str:
    ascii_text = unicodedata.normalize("NFKD", str(text or "")).encode("ascii", "ignore").decode("ascii")
    return ascii_text.lower()


def _title_tokens(title: str) -> set[str]:
    normalized = _normalize_key(title)
    tokens = {
        token for token in re.findall(r"[a-z0-9]+", normalized)
        if len(token) >= 3 and token not in TITLE_STOPWORDS
    }
    return tokens


TAG_ALIAS_LOOKUP = {
    re.sub(r"\s+", " ", _normalize_key(alias).replace("_", " ")).strip(): canonical
    for canonical, aliases in TAG_ALIASES.items()
    for alias in (canonical, *aliases)
}


def _normalize_tag_candidate(candidate: str) -> str:
    normalized = re.sub(r"\s+", " ", _normalize_key(candidate).replace("_", " ")).strip()
    if not normalized:
        return ""
    if normalized in TAG_ALIAS_LOOKUP:
        return TAG_ALIAS_LOOKUP[normalized]
    return ""


def _contains_signal(text: str, signal: str) -> bool:
    normalized_signal = re.sub(r"\s+", " ", _normalize_key(signal)).strip()
    if not normalized_signal:
        return False
    if " " in normalized_signal:
        return normalized_signal in text
    return re.search(rf"\b{re.escape(normalized_signal)}\b", text) is not None


def _article_tag_text(article: dict[str, Any], raw_tags: list[str]) -> str:
    fields = [
        article.get("title", ""),
        article.get("summary_vi", ""),
        article.get("editorial_angle", ""),
        article.get("content", ""),
        article.get("snippet", ""),
        article.get("source", ""),
        article.get("source_domain", ""),
        " ".join(raw_tags),
    ]
    combined = " ".join(str(field or "") for field in fields)
    normalized = re.sub(r"\s+", " ", _normalize_key(combined))
    return normalized.strip()


def _infer_taxonomy_tags(article: dict[str, Any], raw_tags: list[str] | None = None, limit: int = 3) -> list[str]:
    normalized_tags: list[str] = []

    def add_tag(tag: str) -> None:
        if tag and tag in TAG_TAXONOMY and tag not in normalized_tags:
            normalized_tags.append(tag)

    raw_tag_list = [str(tag or "") for tag in (raw_tags or [])]
    for raw_tag in raw_tag_list:
        add_tag(_normalize_tag_candidate(raw_tag))

    text = _article_tag_text(article, raw_tag_list)
    for tag in TAG_PRIORITY:
        signals = TAG_SIGNAL_KEYWORDS.get(tag, ())
        if any(_contains_signal(text, signal) for signal in signals):
            add_tag(tag)

    primary_type = str(article.get("primary_type", "") or "")
    score = int(article.get("total_score", 0) or 0)
    if not normalized_tags and score >= 35:
        add_tag(TAG_TYPE_DEFAULTS.get(primary_type, ""))

    return normalized_tags[:limit]


def _normalize_article_tags(article: dict[str, Any]) -> None:
    raw_tags = article.get("tags", [])
    if not isinstance(raw_tags, list):
        raw_tags = []
    article["tags"] = _infer_taxonomy_tags(article, raw_tags=raw_tags)


def _recompute_relevance_level(article: dict[str, Any]) -> None:
    score = int(article.get("total_score", 0) or 0)
    if score >= 70:
        article["relevance_level"] = "High"
    elif score >= 40:
        article["relevance_level"] = "Medium"
    else:
        article["relevance_level"] = "Low"


def _apply_freshness_penalty(article: dict[str, Any], min_score: int) -> None:
    """
    Phạt deterministic cho bài stale hoặc không rõ freshness để tránh old-news leakage.
    """
    score = int(article.get("total_score", 0) or 0)
    source_tier = str(article.get("source_tier", "unknown")).lower()
    freshness_unknown = bool(article.get("freshness_unknown", False))
    is_stale_candidate = bool(article.get("is_stale_candidate", False))
    is_old_news = bool(article.get("is_old_news", False))
    content_available = bool(article.get("content_available", False))
    age_hours = article.get("age_hours")

    if is_stale_candidate:
        article["total_score"] = max(0, score - 25)
        article["freshness_status"] = "stale_candidate"
        article["analysis_tier"] = "skip" if article.get("total_score", 0) < min_score + 10 else "basic"
    elif is_old_news:
        article["total_score"] = max(0, score - 15)
        article["freshness_status"] = "old_news"
        if article.get("analysis_tier") == "deep":
            article["analysis_tier"] = "basic"
    elif freshness_unknown and source_tier == "c":
        article["total_score"] = max(0, score - 12)
        article["freshness_status"] = "unknown_weak_source"
        if article.get("analysis_tier") == "deep":
            article["analysis_tier"] = "basic"
    elif freshness_unknown and not content_available:
        article["total_score"] = max(0, score - 10)
        article["freshness_status"] = "unknown_thin_content"
        if article.get("analysis_tier") == "deep":
            article["analysis_tier"] = "basic"
    else:
        article["freshness_status"] = "ok"

    if isinstance(age_hours, (int, float)) and age_hours <= 48 and article["freshness_status"] == "ok":
        article["total_score"] = min(100, int(article.get("total_score", 0) or 0) + 5)
        article["freshness_status"] = "fresh_boost"
        if article.get("analysis_tier") == "basic" and article.get("total_score", 0) >= min_score - 5:
            article["analysis_tier"] = "deep"

    _recompute_relevance_level(article)


def _build_score_breakdown(article: dict[str, Any]) -> dict[str, Any]:
    prefilter_reasons = [str(reason or "") for reason in article.get("prefilter_reasons", [])]
    c1_reason = str(article.get("c1_reason", "") or "")
    c2_reason = str(article.get("c2_reason", "") or "")
    c3_reason = str(article.get("c3_reason", "") or "")
    source_kind = str(article.get("source_kind", "unknown") or "unknown")

    surfaced_reasons = prefilter_reasons[:4]
    surfaced_reasons.extend(
        reason for reason in [
            _clean_reason_snippet(c1_reason, 80),
            _clean_reason_snippet(c2_reason, 80),
            _clean_reason_snippet(c3_reason, 80),
        ]
        if reason
    )

    return {
        "source_kind": source_kind,
        "source_priority": int(article.get("source_priority", 0) or 0),
        "community_signal_strength": int(article.get("community_signal_strength", 0) or 0),
        "watchlist_hit": bool(article.get("watchlist_hit", False)),
        "prefilter_score": int(article.get("prefilter_score", 0) or 0),
        "c1_score": int(article.get("c1_score", 0) or 0),
        "c2_score": int(article.get("c2_score", 0) or 0),
        "c3_score": int(article.get("c3_score", 0) or 0),
        "total_score": int(article.get("total_score", 0) or 0),
        "why_surfaced": surfaced_reasons[:5],
        "why_skipped": [
            reason
            for reason in prefilter_reasons
            if reason.startswith(("editorial_noise", "blocked_domain", "soft_blocked_domain", "not_ai_relevant", "stale", "old_news"))
        ][:5],
    }


def _articles_same_event(left: dict[str, Any], right: dict[str, Any]) -> bool:
    left_tokens = _title_tokens(left.get("title", ""))
    right_tokens = _title_tokens(right.get("title", ""))

    if not left_tokens or not right_tokens:
        return False

    intersection = left_tokens & right_tokens
    union = left_tokens | right_tokens
    jaccard = len(intersection) / max(1, len(union))

    if jaccard >= 0.6:
        return True

    # Nếu title overlap đủ mạnh và có ít nhất 3 token chung, coi là cùng event.
    if len(intersection) >= 3 and jaccard >= 0.4:
        return True

    return False


def _event_sort_key(article: dict[str, Any]) -> tuple[int, int, int, int]:
    tier_order = {"a": 3, "b": 2, "c": 1, "unknown": 0}
    return (
        int(article.get("total_score", 0) or 0),
        tier_order.get(str(article.get("source_tier", "unknown")).lower(), 0),
        1 if article.get("content_available") else 0,
        0 if article.get("freshness_unknown") else 1,
    )


def _cluster_events(scored_articles: list[dict[str, Any]]) -> list[list[dict[str, Any]]]:
    clusters: list[list[dict[str, Any]]] = []

    for article in scored_articles:
        matched_cluster: list[dict[str, Any]] | None = None
        for cluster in clusters:
            representative = cluster[0]
            if _articles_same_event(article, representative):
                matched_cluster = cluster
                break

        if matched_cluster is None:
            clusters.append([article])
        else:
            matched_cluster.append(article)

    return clusters


def _annotate_event_clusters(scored_articles: list[dict[str, Any]], min_score: int) -> list[dict[str, Any]]:
    """
    Gắn metadata event cho từng article và boost nhẹ event có nhiều nguồn đồng thuận.
    """
    clusters = _cluster_events(scored_articles)
    primaries: list[dict[str, Any]] = []

    for idx, cluster in enumerate(clusters, 1):
        cluster.sort(key=_event_sort_key, reverse=True)
        event_id = f"evt_{idx:03d}"
        titles = [str(item.get("title", "")) for item in cluster[:4] if item.get("title")]
        domains = sorted({str(item.get("source_domain", "")) for item in cluster if item.get("source_domain")})
        source_count = len(domains)

        for rank, article in enumerate(cluster, 1):
            article["event_id"] = event_id
            article["event_cluster_size"] = len(cluster)
            article["event_source_count"] = source_count
            article["event_titles"] = titles
            article["event_domains"] = domains
            article["event_is_primary"] = rank == 1
            article["event_consensus"] = source_count >= 2
            article["event_rank"] = rank

        primary = cluster[0]
        event_bonus = min(6, max(0, source_count - 1) * 3)
        if event_bonus:
            primary["total_score"] = min(100, int(primary.get("total_score", 0) or 0) + event_bonus)
            if int(primary.get("total_score", 0) or 0) >= min_score - 5 and primary.get("analysis_tier") == "basic":
                primary["analysis_tier"] = "deep"
            _recompute_relevance_level(primary)
        primaries.append(primary)

    return primaries


def _normalize_primary_type(article: dict[str, Any]) -> None:
    """
    Heuristic hẹp để sửa type ở một số case biên.
    Giữ minimal để không phá kết quả tier vốn đã ổn.
    """
    title = str(article.get("title", "")).lower()
    source_domain = str(article.get("source_domain", "") or "").lower()
    surface_text = " ".join(
        part for part in [
            article.get("title", ""),
            article.get("snippet", ""),
            article.get("summary_vi", ""),
            article.get("content", ""),
        ]
        if part
    ).lower()
    current_type = str(article.get("primary_type", "Practical"))
    score = int(article.get("total_score", 0) or 0)
    content_available = bool(article.get("content_available", False))

    # Trang danh mục / landing page thiếu nội dung: ép về Practical để dễ hiểu hơn.
    if not content_available and score <= 15:
        if any(token in title for token in ("cập nhật tin", "bao", "báo", "khoa hoc", "khoa học", "cong nghe", "công nghệ")):
            article["primary_type"] = "Practical"
            article["primary_emoji"] = "🛠️"
            return

    # Ecosystem/community stories nên ưu tiên Society, kể cả có chữ "cạnh tranh".
    if "ecosystem" in title or "hệ sinh thái" in title or "he sinh thai" in title:
        if "startup" not in title and "partnership" not in title and "partners with" not in title:
            article["primary_type"] = "Society"
            article["primary_emoji"] = "🌍"
            return

    # Tin về startup/thi trường/cạnh tranh nên nghiêng về Business.
    if any(keyword in title for keyword in BUSINESS_KEYWORDS):
        article["primary_type"] = "Business"
        article["primary_emoji"] = "💼"
        return

    # Incident/security/compliance nên nghiêng về Policy & Risk hơn Society fallback.
    if any(keyword in title for keyword in POLICY_KEYWORDS):
        article["primary_type"] = "Policy"
        article["primary_emoji"] = "⚖️"
        return

    # Nguồn official mà bề mặt bài rõ là update sản phẩm thì đừng rơi nhầm về Society.
    if source_domain in {
        "openai.com",
        "anthropic.com",
        "about.fb.com",
        "blog.google",
        "deepmind.google",
        "huggingface.co",
        "blogs.microsoft.com",
        "aws.amazon.com",
    }:
        if any(
            keyword in surface_text
            for keyword in (
                "product",
                "feature",
                "features",
                "capability",
                "capabilities",
                "release notes",
                "launch",
                "launched",
                "model",
                "api",
                "sdk",
                "glasses",
            )
        ):
            article["primary_type"] = "Product"
            article["primary_emoji"] = "🚀"
            return

    # Tin về hệ sinh thái / cộng đồng / giáo dục / bối cảnh Việt Nam nên nghiêng về Society.
    if any(keyword in title for keyword in SOCIETY_KEYWORDS):
        if current_type not in {"Product", "Business"} and "startup" not in title and "partnership" not in title and "partners with" not in title:
            article["primary_type"] = "Society"
            article["primary_emoji"] = "🌍"


def classify_and_score_node(state: dict[str, Any]) -> dict[str, Any]:
    """
    LangGraph node: phân loại + chấm điểm mỗi bài viết.

    Input: new_articles (từ deduplicate)
    Output: scored_articles, top_articles, low_score_articles
    """
    articles = state.get("new_articles", [])
    if not articles:
        logger.info("📭 Không có bài mới để classify.")
        return {
            "scored_articles": [],
            "top_articles": [],
            "low_score_articles": [],
        }

    # Các ngưỡng này được cho phép override từ UI để test nhanh mà không phải sửa .env.
    min_score = _cfg_int(state, "min_deep_analysis_score", "MIN_DEEP_ANALYSIS_SCORE", 60)
    max_top = _cfg_int(state, "max_deep_analysis_articles", "MAX_DEEP_ANALYSIS_ARTICLES", 10)
    max_classify = _cfg_int(state, "max_classify_articles", "MAX_CLASSIFY_ARTICLES", 8)
    classify_content_limit = _cfg_int(state, "classify_content_char_limit", "CLASSIFY_CONTENT_CHAR_LIMIT", 900)
    classify_max_tokens = _cfg_int(state, "classify_max_tokens", "CLASSIFY_MAX_TOKENS", 320)

    llm_articles, held_out_articles = _prepare_classify_candidates(
        list(articles),
        max_classify,
        runtime_config=state.get("runtime_config", {}),
        feedback_summary_text=state.get("feedback_summary_text", ""),
    )
    logger.info(
        "🧮 Prefilter giữ %d/%d bài cho 32B classify (held_out=%d, max=%d)",
        len(llm_articles),
        len(articles),
        len(held_out_articles),
        max_classify,
    )
    if llm_articles:
        logger.info(
            "   Top prefilter: %s",
            " | ".join(
                f"{a.get('title', 'N/A')[:42]}[{a.get('prefilter_score', 0)}]"
                for a in llm_articles[:5]
            ),
        )

    scored = []
    total = len(llm_articles)

    for i, article in enumerate(llm_articles, 1):
        title = article.get("title", "N/A")
        logger.info("🏷️  Classify+Score [%d/%d]: %s", i, total, title[:60])

        related_ctx = _build_related_context(article)
        user_prompt = CLASSIFY_SCORE_USER_TEMPLATE.format(
            title=title,
            url=article.get("url", ""),
            source=article.get("source", "Unknown"),
            source_domain=article.get("source_domain", ""),
            published_at=article.get("published_at", article.get("published", "")),
            published_at_source=article.get("published_at_source", "unknown"),
            discovered_at=article.get("discovered_at", article.get("fetched_at", "")),
            age_hours=article.get("age_hours", ""),
            freshness_unknown=article.get("freshness_unknown", False),
            is_stale_candidate=article.get("is_stale_candidate", False),
            source_verified=article.get("source_verified", False),
            content_available=article.get("content_available", False),
            content=(article.get("content", "") or article.get("snippet", ""))[:classify_content_limit],
            related_context=related_ctx,
            feedback_context=state.get("feedback_summary_text", "Chưa có feedback mới từ team."),
        )

        try:
            inference = _normalize_classify_inference_response(
                run_json_inference(
                    CLASSIFY_SCORE_SYSTEM,
                    user_prompt,
                    max_tokens=classify_max_tokens,
                    temperature=0.1,
                )
            )
            result, raw_output, looks_structured = inference

            if result is None:
                if _is_likely_prose_response(raw_output) or not looks_structured:
                    logger.warning("⚠️ Model không trả JSON ổn định cho '%s'; dùng prose rescue/fallback.", title[:40])
                else:
                    result = _classify_inference_with_retry(
                        user_prompt,
                        max_tokens=classify_max_tokens,
                        temperature=0.1,
                        initial_response=inference,
                    )

            if result and isinstance(result, dict):
                try:
                    c1 = int(result.get("c1_score", 0) or 0)
                    c2 = int(result.get("c2_score", 0) or 0)
                    c3 = int(result.get("c3_score", 0) or 0)
                except ValueError:
                    c1, c2, c3 = 0, 0, 0
                    
                analysis_tier = str(result.get("analysis_tier", "")).strip().lower()
                if analysis_tier not in {"deep", "basic", "skip"}:
                    projected_total = c1 + c2 + c3
                    if projected_total >= min_score:
                        analysis_tier = "deep"
                    elif projected_total >= 30:
                        analysis_tier = "basic"
                    else:
                        analysis_tier = "skip"

                article.update({
                    "primary_type": result.get("primary_type", "Practical"),
                    "primary_emoji": result.get("primary_emoji", "🛠️"),
                    "c1_score": c1,
                    "c1_reason": str(result.get("c1_reason", "")),
                    "c2_score": c2,
                    "c2_reason": str(result.get("c2_reason", "")),
                    "c3_score": c3,
                    "c3_reason": str(result.get("c3_reason", "")),
                    "total_score": c1 + c2 + c3,
                    "summary_vi": str(result.get("summary_vi", "")),
                    "editorial_angle": str(result.get("editorial_angle", "")),
                    "analysis_tier": analysis_tier,
                    "tags": result.get("tags", []) if isinstance(result.get("tags"), list) else [],
                    "relevance_level": str(result.get("relevance_level", "Low")),
                })
                _apply_strategic_boost(article, min_score)
                _apply_freshness_penalty(article, min_score)
                _normalize_primary_type(article)
                _normalize_article_tags(article)
                article["score_breakdown"] = _build_score_breakdown(article)
                article["why_surfaced"] = article["score_breakdown"]["why_surfaced"]
            else:
                logger.warning("⚠️ Model không trả JSON cho '%s'", title[:40])
                _classify_prose_rescue(article, raw_output, min_score)
                article["score_breakdown"] = _build_score_breakdown(article)
                article["why_surfaced"] = article["score_breakdown"]["why_surfaced"]
        except Exception as e:
            logger.error("❌ Classify failed: '%s': %s", title[:40], e)
            _llm_failure_fallback(article, min_score)
            article["score_breakdown"] = _build_score_breakdown(article)
            article["why_surfaced"] = article["score_breakdown"]["why_surfaced"]

        scored.append(article)

    for article in held_out_articles:
        _held_out_article_fallback(article)
        article["score_breakdown"] = _build_score_breakdown(article)
        article["why_skipped"] = article["score_breakdown"]["why_skipped"] or article["score_breakdown"]["why_surfaced"][:2]
        scored.append(article)

    # Sắp xếp theo score giảm dần
    scored.sort(key=lambda a: a.get("total_score", 0), reverse=True)
    primary_event_articles = _annotate_event_clusters(scored, min_score)
    primary_event_articles.sort(key=lambda a: a.get("total_score", 0), reverse=True)

    # Chỉ deep-dive 1 bài đại diện cho mỗi event để tránh lãng phí reasoning.
    top = [
        a for a in primary_event_articles
        if a.get("total_score", 0) >= min_score or a.get("analysis_tier") == "deep"
    ][:max_top]
    low = [a for a in scored if a not in top]

    logger.info(
        "✅ Classify+Score xong: %d bài / %d event → %d top (≥%d) + %d low",
        len(scored), len(primary_event_articles), len(top), min_score, len(low)
    )

    return {
        "scored_articles": scored,
        "top_articles": top,
        "low_score_articles": low,
    }

"""
classify_and_score.py — LangGraph node: Phân loại + chấm relevance theo vai "editorial triage".

Mục tiêu là để Qwen local đóng vai tương tự một bộ lọc kiểu Claude:
  1. Classify: gán 1 trong 3 editorial lanes
  2. Score: chấm 1-100 dựa trên 3 tiêu chí
  3. Decision: quyết định bài nào cần phân tích sâu, bài nào chỉ lưu cơ bản

3 editorial lanes:
  🚀 Product            — Ra mắt sản phẩm, tính năng, API, model, platform update
  🌍 Society & Culture  — Tác động xã hội, giáo dục, công việc, cộng đồng, policy/public response
  🛠️ Practical          — Hướng dẫn, tips, workflows, tools, best practices

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

import ast
import json
import logging
import os
import re
import sys
import unicodedata
from pathlib import Path
from typing import Any

# Đảm bảo project root nằm trong sys.path
from digest.editorial.delivery_policy import (
    apply_main_brief_routing,
    is_github_main_brief_significant,
    is_github_signal_article,
)
from digest.runtime.mlx_runner import resolve_pipeline_mlx_path, run_json_inference_meta
from digest.editorial.editorial_guardrails import sanitize_delivery_text
from digest.sources.source_catalog import classify_source_kind
from digest.runtime.temporal_snapshots import write_temporal_snapshot
from digest.runtime.xai_grok import (
    call_xai_structured_json,
    grok_classify_enabled,
    grok_classify_mode,
    merge_grok_observability,
    grok_prefilter_enabled,
    grok_prefilter_max_articles,
    rerank_prefilter_articles,
)

logger = logging.getLogger(__name__)

CLASSIFY_JSON_STATUS_VALID = "valid_json"
CLASSIFY_JSON_STATUS_REPAIRED = "repaired_json"
CLASSIFY_JSON_STATUS_PARTIAL = "partial_recovery"
CLASSIFY_JSON_STATUS_FALLBACK = "hard_fallback"

CLASSIFY_TEXT_FIELDS = (
    "summary_vi",
    "factual_summary_vi",
    "editorial_angle",
    "why_it_matters_vi",
    "optional_editorial_angle",
)


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


SCORE_COMPONENT_CAPS = (33, 33, 34)


WORKFLOW_SYSTEM_SIGNAL_KEYWORDS = (
    "agent workflow",
    "workflow automation",
    "multi-step workflow",
    "multi step workflow",
    "orchestration",
    "orchestrator",
    "handoff",
    "handoffs",
    "review loop",
    "approval loop",
    "human-in-the-loop",
    "human in the loop",
    "tool use",
    "tool-use",
)

HEALTHCARE_WORKFLOW_SIGNAL_KEYWORDS = (
    "ai clinic",
    "clinic workflow",
    "clinical workflow",
    "healthcare workflow",
    "medical workflow",
    "patient workflow",
    "patient scheduling",
    "appointment automation",
    "intake automation",
    "medical triage",
)

OPERATIONS_RELIABILITY_SIGNAL_KEYWORDS = (
    "operations workflow",
    "system automation",
    "model monitoring",
    "observability",
    "reliability",
    "incident response",
    "runbook",
    "guardrails",
    "human review",
    "eval",
    "evaluation",
)

SIMULATION_DEPLOYMENT_SIGNAL_KEYWORDS = (
    "simulation",
    "simulator",
    "scenario",
    "scenario-based",
    "local deployment",
    "private deployment",
    "local-first",
    "local first",
    "on-device",
    "on device",
    "edge deployment",
    "self-hosted",
    "self hosted",
    "cost-aware deployment",
    "cost-aware",
)

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
    "agent workflow",
    "workflow automation",
    "orchestration",
    "multi-step workflow",
    "human-in-the-loop",
    "clinic workflow",
    "healthcare workflow",
    "medical workflow",
    "patient workflow",
    "operations workflow",
    "system automation",
    "model monitoring",
    "observability",
    "reliability",
    "incident response",
    "simulation",
    "simulator",
    "scenario-based",
    "local deployment",
    "private deployment",
    "on-device",
    "edge deployment",
    "self-hosted",
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

AI_SIGNAL_KEYWORDS = tuple(
    dict.fromkeys(
        (
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
        + WORKFLOW_SYSTEM_SIGNAL_KEYWORDS
        + HEALTHCARE_WORKFLOW_SIGNAL_KEYWORDS
        + OPERATIONS_RELIABILITY_SIGNAL_KEYWORDS
        + (
            "simulation",
            "scenario-based",
            "local deployment",
            "private deployment",
            "on-device",
            "edge deployment",
        )
    )
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

STRATEGIC_SIGNAL_KEYWORDS = tuple(
    dict.fromkeys(
        (
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
            "orchestration",
            "handoff",
            "review loop",
            "approval loop",
            "multi-step",
            "multi step",
            "human-in-the-loop",
            "human in the loop",
            "tool use",
            "tool-use",
            "healthcare",
            "medical",
            "clinic",
            "patient",
            "patient workflow",
            "scheduling",
            "appointment",
            "intake",
            "triage",
            "operations",
            "system automation",
            "monitoring",
            "observability",
            "reliability",
            "incident response",
            "runbook",
            "guardrails",
            "simulation",
            "scenario",
            "simulator",
            "local deployment",
            "private deployment",
            "local-first",
            "local first",
            "on-device",
            "on device",
            "edge deployment",
            "self-hosted",
            "self hosted",
            "cost-aware",
            "cost-aware deployment",
            "robot",
            "robotics",
            "regulation",
            "policy",
            "safety",
            "security",
            "vietnam",
            "asean",
        )
        + WORKFLOW_SYSTEM_SIGNAL_KEYWORDS
        + HEALTHCARE_WORKFLOW_SIGNAL_KEYWORDS
        + OPERATIONS_RELIABILITY_SIGNAL_KEYWORDS
        + SIMULATION_DEPLOYMENT_SIGNAL_KEYWORDS
    )
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
    "Product": "product_update",
    "Society & Culture": "education",
    "Practical": "developer_tools",
}

TAG_TAXONOMY_BLOCK = "\n".join(
    f"- `{tag}`: {description}"
    for tag, description in TAG_TAXONOMY.items()
)

# ── Prompt Master cho classify + score ───────────────────────────────
CLASSIFY_SCORE_SYSTEM = """Bạn là Editorial Triage Lead cho một sản phẩm AI Daily Digest trả phí.
Nhiệm vụ: đọc nhanh từng nguồn tin AI/Tech, chấm điểm relevance như một biên tập viên khó tính, và quyết định bài nào đáng được đưa vào phân tích sâu.

Mục tiêu của bản tin không phải là gom mọi tin AI đang hot, mà là chọn ra những tín hiệu thực sự hữu ích cho một team đang xây hệ thống AI ứng dụng thực tế:
- AI agent cho workflow nhiều bước
- AI product theo domain cụ thể
- hệ thống AI hỗ trợ vận hành / quản lý / automation
- các sản phẩm AI cần reliability, observability, guardrails, human-in-the-loop
- local-first / integration-friendly / deployable AI khi phù hợp

## 3 Editorial Lanes (CHỌN ĐÚNG 1)
- 🚀 Product: Ra mắt sản phẩm, tính năng mới, API, model, platform update, capability jump có tính sản phẩm
- 🌍 Society & Culture: Tác động xã hội, giáo dục, việc làm, cộng đồng, policy/public response
- 🛠️ Practical: Hướng dẫn, tips, workflows, playbook, tool usage, implementation lessons

## 3 Tiêu chí chấm điểm

### C1: Chất lượng tín hiệu tin tức (0-33)
Đánh giá bài này mạnh đến đâu về mặt editorial signal:

Ưu tiên điểm cao nếu:
- Nguồn đáng tin cậy, có thẩm quyền hoặc là nguồn gốc trực tiếp
- Tin còn mới, đặc biệt trong 24-72h gần đây
- Có tác động rõ lên hệ sinh thái AI / product / developer / operator
- Có dữ kiện cụ thể, không chỉ headline bề mặt
- Là tín hiệu chiến lược cấp ngành: model release, pricing change, API/platform shift, partnership, hạ tầng, safety/security incident, regulation quan trọng
- Là update có thể làm thay đổi cách build, deploy, vận hành hoặc cạnh tranh sản phẩm AI

Giảm điểm nếu:
- Nguồn yếu, mỏng, giật tít, ít dữ kiện
- Tin cũ, bị trùng, hoặc chủ yếu là buzz/social reaction
- Chỉ là event promo, recap marketing, hoặc headline mơ hồ
- Không đủ dữ kiện để kết luận mạnh
- Không thật sự liên quan đến AI/product/workflow/system

### C2: Giá trị thực tế với team builder/operator (0-33)
Đánh giá bài này có giúp ích gì cho một team đang build AI systems thực tế không.

Ưu tiên điểm cao nếu bài giúp ích rõ cho:
- founder, operator, PM, engineer, product builder
- team đang triển khai AI vào workflow thực
- bài toán cost / latency / deployment / integration / governance
- lựa chọn model, toolchain, API, infra, orchestration
- human-in-the-loop, eval, observability, safety, reliability
- cơ hội sản phẩm / cơ hội thương mại / tín hiệu cạnh tranh đáng theo dõi

Ví dụ tín hiệu có giá trị cao:
- model/API/platform update có thể dùng được ngay
- workflow/tooling giúp tăng tốc triển khai AI
- safety/security/compliance ảnh hưởng vận hành thực tế
- pricing / cost structure / infra shift có ý nghĩa ra quyết định
- case study deployment, enterprise adoption, operator lesson
- domain AI có thể chuyển hóa thành sản phẩm hoặc workflow hữu ích

Giảm điểm nếu:
- thú vị nhưng không actionable
- thiên về giải trí, opinion, meme, hoặc tranh luận cộng đồng
- không giúp ích cho việc ra quyết định sản phẩm / hệ thống / vận hành

### C3: Phù hợp định hướng sản phẩm và hệ thống hiện tại (0-34)
Nhóm hiện tại ưu tiên các hướng sau:
1. AI agent thực chiến cho workflow nhiều bước và nghiệp vụ thực
   - ví dụ: AI Clinic cho hỗ trợ quy trình phòng khám sản phụ
   - các agent phối hợp nhiều bước, hỗ trợ nhân sự, giảm thao tác thủ công
2. Hệ thống AI cho quản lý vận hành / workflow / orchestration / automation
   - vận hành nội bộ, quản lý dự án, phối hợp tool, process support
3. AI product áp dụng theo domain cụ thể
   - healthcare-support workflow
   - operations workflow
   - vertical AI / applied AI
4. Sản phẩm mô phỏng / interactive / scenario-based AI
   - ví dụ game giả lập, simulation-driven product, scenario engine
5. Các thành phần nền giúp build AI system bền vững
   - memory, tool-use, eval, monitoring, guardrails, reliability, observability, cost-aware deployment, local/private deployment khi phù hợp

Ưu tiên điểm cao nếu bài có ích rõ cho:
- agent systems, tool use, memory, planning, coordination
- orchestration, multi-step workflow, handoff, review loop
- deployment, integration, monitoring, reliability, safety
- human-in-the-loop systems
- domain deployment như healthcare, operations, simulation
- applied AI / vertical AI / real-world AI product
- local-first / private / cost-aware AI system design khi phù hợp
- những gì giúp build sản phẩm AI dùng được trong môi trường thực, không chỉ demo

Giảm điểm nếu:
- chỉ hot nhưng không giúp xây hệ thống tốt hơn
- không liên quan nhiều tới agent, workflow, deployment, productization hoặc domain AI
- quá xa khỏi hướng applied AI / operational AI / system AI
- chỉ là social buzz hoặc showcase mỏng

## Rules bổ sung
- Nếu bài vốn mang màu research/business/policy nhưng có giá trị rõ nhất ở góc sản phẩm, xếp vào `Product`.
- Nếu bài phản ánh tác động tới con người, xã hội, giáo dục, công việc hoặc phản ứng chính sách/cộng đồng, xếp vào `Society & Culture`.
- Nếu bài thiên về cách dùng tool, workflow, implementation, operator lesson, case study thực chiến, xếp vào `Practical`.
- Nếu bài research/business/policy không fit rõ vào 3 lane trên, hãy hạ điểm và nghiêng về `skip` hoặc `basic` thay vì cố nhét.
- `relevance_level`:
  - High nếu tổng điểm >= 70
  - Medium nếu 40-69
  - Low nếu < 40
- `analysis_tier`:
  - deep: bài đủ mạnh để đầu tư research/thinking sâu
  - basic: nên lưu và có thể đưa vào digest, nhưng không cần research dài
  - skip: tín hiệu yếu, ít giá trị với hướng sản phẩm/hệ thống hiện tại
- Nếu nguồn mạnh (đặc biệt official source, Reuters, Bloomberg, CNBC, TechCrunch, MIT, DeepMind, OpenAI, Anthropic, Meta, NVIDIA, Hugging Face) và chủ đề mang tính chiến lược cấp ngành, mặc định nghiêng về `deep` trừ khi bài quá mỏng hoặc không liên quan AI.
- `editorial_angle`: 1 câu nói rõ điểm đáng quan tâm nhất của bài này dưới góc nhìn người build sản phẩm/hệ thống AI thực tế.
- `summary_vi`: tóm tắt 2-3 câu, factual, rõ ý, không hype.
- Tags: Chỉ được chọn 1-3 tag từ taxonomy chuẩn bên dưới.
- Không được bịa tag mới.
- Không dùng tên công ty/người/số tiền làm tag.
- Không copy nguyên title làm tag.
- Nếu không có tag nào đủ chắc, trả `[]`.
- KHÔNG TỰ TÍNH TỔNG ĐIỂM trong JSON (bộ phận Python sẽ lo việc tính tổng).
- Không hype. Không dùng ngôn ngữ quảng cáo.
- Nếu dữ liệu thiếu, nói rõ là tín hiệu còn mỏng hoặc chưa đủ dữ kiện.
- Nếu `Content_available=false` hoặc `Published_at` trống: hạ điểm C1 và tránh kết luận mạnh.
- Ưu tiên factual judgement hơn là viết văn hay. Nhiệm vụ ở đây là triage và structured scoring, không phải final newsletter writing.

## Tag Taxonomy
__TAG_TAXONOMY__

## Output: JSON (KHÔNG markdown, KHÔNG giải thích thêm)
{
  "primary_type": "Product|Society & Culture|Practical",
  "primary_emoji": "🚀|🌍|🛠️",
  "c1_score": 0-33,
  "c1_reason": "Giải thích ngắn gọn (1 câu)",
  "c2_score": 0-33,
  "c2_reason": "Giải thích ngắn gọn (1 câu)",
  "c3_score": 0-34,
  "c3_reason": "Giải thích ngắn gọn (1 câu)",
"summary_vi": "Tóm tắt 2-3 câu bằng tiếng Việt",
"factual_summary_vi": "Tóm tắt thực chứng 1-2 câu, tập trung dữ kiện và tín hiệu",
"editorial_angle": "1 câu về điểm đáng quan tâm nhất",
"why_it_matters_vi": "1-2 câu ngắn nêu lý do đáng chú ý cho team operator/developer",
"optional_editorial_angle": "Câu ngắn về bối cảnh/điểm nhấn, có thể ngắn hơn editorial_angle",
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

Bắt buộc:
- Điền đủ `summary_vi`, `factual_summary_vi`, `editorial_angle`, `why_it_matters_vi`, `optional_editorial_angle`.
- Nếu dữ kiện mỏng, vẫn phải viết ngắn gọn và thận trọng thay vì bỏ trống field.

Trả về JSON theo format yêu cầu."""

CLASSIFY_RETRY_SUFFIX = """

YÊU CẦU LẦN 2:
- Chỉ trả về đúng 1 JSON object hợp lệ.
- Không dùng markdown fence.
- Không giải thích thêm trước hoặc sau JSON.
- Đảm bảo field `tags` là list tag trong taxonomy hoặc [].
- Không được bỏ trống `factual_summary_vi`, `why_it_matters_vi`, `optional_editorial_angle`.
"""

CLASSIFY_LAST_CHANCE_SUFFIX = """

YÊU CẦU LẦN 3:
- Trả về duy nhất 1 JSON object trên MỘT khối văn bản, không prose.
- Tất cả key PHẢI có double quotes.
- Không markdown fence.
- Không giải thích.
- Nếu thiếu dữ liệu, vẫn phải điền score thấp và chọn `skip` hoặc `basic`, không được bỏ JSON.
- Không được bỏ trống `summary_vi`, `factual_summary_vi`, `why_it_matters_vi`, `optional_editorial_angle`.
"""

CLASSIFY_RESPONSE_FORMAT: dict[str, Any] = {
    "type": "json_schema",
    "json_schema": {
        "name": "classify_score_article",
        "strict": True,
        "schema": {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "primary_type": {"type": "string", "enum": ["Product", "Society & Culture", "Practical"]},
                "primary_emoji": {"type": "string", "enum": ["🚀", "🌍", "🛠️"]},
                "c1_score": {"type": "integer", "minimum": 0, "maximum": 33},
                "c1_reason": {"type": "string"},
                "c2_score": {"type": "integer", "minimum": 0, "maximum": 33},
                "c2_reason": {"type": "string"},
                "c3_score": {"type": "integer", "minimum": 0, "maximum": 34},
                "c3_reason": {"type": "string"},
                "summary_vi": {"type": "string"},
                "factual_summary_vi": {"type": "string"},
                "editorial_angle": {"type": "string"},
                "why_it_matters_vi": {"type": "string"},
                "optional_editorial_angle": {"type": "string"},
                "analysis_tier": {"type": "string", "enum": ["deep", "basic", "skip"]},
                "tags": {"type": "array", "items": {"type": "string"}, "maxItems": 3},
                "relevance_level": {"type": "string", "enum": ["High", "Medium", "Low"]},
            },
            "required": [
                "primary_type",
                "primary_emoji",
                "c1_score",
                "c1_reason",
                "c2_score",
                "c2_reason",
                "c3_score",
                "c3_reason",
                "summary_vi",
                "factual_summary_vi",
                "why_it_matters_vi",
                "optional_editorial_angle",
                "editorial_angle",
                "analysis_tier",
                "tags",
                "relevance_level",
            ],
        },
    },
}

PROSE_TYPE_HINTS: dict[str, tuple[str, ...]] = {
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
        "benchmark",
        "paper",
        "research",
        "funding",
        "partnership",
        "partners with",
    ),
    "Society & Culture": POLICY_KEYWORDS + BUSINESS_KEYWORDS + SOCIETY_KEYWORDS + (
        "jobs",
        "workforce",
        "law",
        "regulation",
        "policy",
        "education",
        "community",
    ),
    "Practical": ("tutorial", "guide", "tool", "workflow", "tips"),
}


def _prefilter_primary_type(title: str) -> tuple[str, str]:
    lowered = str(title or "").lower()
    if any(token in lowered for token in ("tutorial", "guide", "tool", "workflow", "tips", "playbook", "how to")):
        return "Practical", "🛠️"
    if any(token in lowered for token in POLICY_KEYWORDS + SOCIETY_KEYWORDS + ("luật", "pháp lý", "quan ly", "jobs", "workforce")):
        return "Society & Culture", "🌍"
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
            "paper",
            "research",
            "benchmark",
            "study",
        )
    ) or any(token in lowered for token in BUSINESS_KEYWORDS):
        return "Product", "🚀"
    return "Society & Culture", "🌍"


def run_json_inference(
    system_prompt: str,
    user_prompt: str,
    *,
    max_tokens: int,
    temperature: float,
    response_format: dict[str, Any] | None = None,
    model_path: str | None = None,
) -> tuple[dict | list | None, str, bool]:
    """
    Local compatibility wrapper for classify inference.

    Một số test cũ patch `digest.workflow.nodes.classify_and_score.run_json_inference`, nên giữ
    hook này ổn định dù runtime hiện tại dùng `run_json_inference_meta`.
    """
    return run_json_inference_meta(
        system_prompt,
        user_prompt,
        max_tokens=max_tokens,
        temperature=temperature,
        model_path=model_path,
        response_format=response_format,
    )


def _normalize_jsonish_candidate(text: str) -> str:
    cleaned = str(text or "").strip()
    cleaned = re.sub(r"^```(?:json)?\s*", "", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r"\s*```$", "", cleaned)
    cleaned = cleaned.replace("“", '"').replace("”", '"').replace("’", "'").replace("‘", "'")
    cleaned = cleaned.replace("：", ":").replace("，", ",")
    cleaned = cleaned.replace("\u00a0", " ")
    cleaned = re.sub(r"^\s*json\s*", "", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r",(\s*[}\]])", r"\1", cleaned)
    return cleaned.strip()


def _extract_jsonish_payload(text: str) -> str:
    raw = str(text or "").strip()
    if not raw:
        return ""

    fenced = re.search(r"```(?:json)?\s*(.*?)\s*```", raw, flags=re.IGNORECASE | re.DOTALL)
    if fenced:
        return _normalize_jsonish_candidate(fenced.group(1))

    for opener, closer in (("{", "}"), ("[", "]")):
        start = raw.find(opener)
        if start == -1:
            continue
        depth = 0
        in_string = False
        escape_next = False
        quote_char = ""
        for index, char in enumerate(raw[start:], start=start):
            if in_string:
                if escape_next:
                    escape_next = False
                elif char == "\\":
                    escape_next = True
                elif char == quote_char:
                    in_string = False
                continue
            if char in {'"', "'"}:
                in_string = True
                quote_char = char
                continue
            if char == opener:
                depth += 1
            elif char == closer:
                depth -= 1
                if depth == 0:
                    return _normalize_jsonish_candidate(raw[start:index + 1])
    return _normalize_jsonish_candidate(raw) if raw.startswith("{") or raw.startswith("[") else ""


def _parse_jsonish_object(candidate: str) -> dict[str, Any] | None:
    normalized = _normalize_jsonish_candidate(candidate)
    if not normalized:
        return None

    candidates = [normalized]
    quoted_keys = re.sub(r'([{\[,]\s*)([A-Za-z_][A-Za-z0-9_]*)\s*:', r'\1"\2":', normalized)
    if quoted_keys != normalized:
        candidates.append(quoted_keys)

    open_braces = normalized.count("{")
    close_braces = normalized.count("}")
    if open_braces > close_braces:
        balanced = normalized + ("}" * (open_braces - close_braces))
        if balanced not in candidates:
            candidates.append(balanced)
        balanced_quoted = re.sub(r'([{\[,]\s*)([A-Za-z_][A-Za-z0-9_]*)\s*:', r'\1"\2":', balanced)
        if balanced_quoted not in candidates:
            candidates.append(balanced_quoted)

    for candidate_text in candidates:
        try:
            parsed = json.loads(candidate_text)
            if isinstance(parsed, dict):
                return parsed
        except json.JSONDecodeError:
            pythonish = re.sub(r"\btrue\b", "True", candidate_text, flags=re.IGNORECASE)
            pythonish = re.sub(r"\bfalse\b", "False", pythonish, flags=re.IGNORECASE)
            pythonish = re.sub(r"\bnull\b", "None", pythonish, flags=re.IGNORECASE)
            try:
                parsed = ast.literal_eval(pythonish)
                if isinstance(parsed, dict):
                    return parsed
            except Exception:
                continue
    return None


def _recover_structured_json_dict(raw: str) -> dict[str, Any] | None:
    payload = _extract_jsonish_payload(raw)
    if payload:
        parsed = _parse_jsonish_object(payload)
        if isinstance(parsed, dict):
            return parsed
    raw_text = str(raw or "")
    if "{" in raw_text:
        return _parse_jsonish_object(raw_text[raw_text.find("{"):])
    return None


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


def _seed_summary_from_article(article: dict[str, Any], *, max_len: int = 260) -> str:
    seed = (
        str(article.get("snippet", "") or "").strip()
        or str(article.get("content", "") or "").strip()
        or str(article.get("title", "") or "").strip()
    )
    if not seed:
        return ""
    if seed == str(article.get("title", "") or "").strip():
        seed = f"Bài viết đề cập tới: {seed}"
    return sanitize_delivery_text(_clean_prose_snippet(seed, limit=max_len), max_len=max_len)


def _seed_editorial_from_article(article: dict[str, Any], *, max_len: int = 180) -> str:
    title = str(article.get("title", "") or "").strip()
    seed = f"Điểm đáng chú ý là {title}" if title else str(article.get("snippet", "") or "").strip()
    return sanitize_delivery_text(_clean_prose_snippet(seed, limit=max_len), max_len=max_len)


def _coerce_tag_list(raw_tags: Any) -> list[str]:
    if isinstance(raw_tags, list):
        return [str(tag or "").strip() for tag in raw_tags if str(tag or "").strip()]
    if isinstance(raw_tags, str):
        return [part.strip() for part in re.split(r"[;,|/]", raw_tags) if part.strip()]
    return []


def _derive_analysis_tier(raw_value: Any, *, total_score: int, min_score: int) -> str:
    analysis_tier = str(raw_value or "").strip().lower()
    if analysis_tier in {"deep", "basic", "skip"}:
        return analysis_tier
    if total_score >= min_score:
        return "deep"
    if total_score >= 30:
        return "basic"
    return "skip"


def _derive_relevance_level(raw_value: Any, *, total_score: int) -> str:
    relevance_level = str(raw_value or "").strip()
    if relevance_level in {"High", "Medium", "Low"}:
        return relevance_level
    if total_score >= 70:
        return "High"
    if total_score >= 40:
        return "Medium"
    return "Low"


def _set_classify_json_debug(
    article: dict[str, Any],
    *,
    status: str,
    missing_fields: list[str] | None = None,
    recovered_fields: list[str] | None = None,
) -> None:
    article["classify_json_status"] = status
    article["classify_json_missing_fields"] = sorted({str(field) for field in (missing_fields or []) if str(field)})
    article["classify_json_recovered_fields"] = sorted({str(field) for field in (recovered_fields or []) if str(field)})


def _apply_structured_classify_result(
    article: dict[str, Any],
    result: dict[str, Any],
    min_score: int,
    *,
    json_status: str,
) -> str:
    fallback_type, fallback_emoji = _prefilter_primary_type(str(article.get("title", "") or ""))
    missing_fields: set[str] = set()
    recovered_fields: set[str] = set()

    def _text_field(name: str, *, fallback: str = "", max_len: int | None = None) -> str:
        raw_value = str(result.get(name, "") or "").strip()
        if raw_value:
            return sanitize_delivery_text(raw_value, max_len=max_len) if max_len else raw_value
        missing_fields.add(name)
        if fallback:
            recovered_fields.add(name)
        return sanitize_delivery_text(fallback, max_len=max_len) if max_len and fallback else fallback

    primary_type = str(result.get("primary_type", "") or "").strip() or fallback_type
    if not str(result.get("primary_type", "") or "").strip():
        missing_fields.add("primary_type")
        recovered_fields.add("primary_type")

    primary_emoji = str(result.get("primary_emoji", "") or "").strip()
    if not primary_emoji:
        missing_fields.add("primary_emoji")
        recovered_fields.add("primary_emoji")
        primary_emoji = {"Product": "🚀", "Society & Culture": "🌍", "Practical": "🛠️"}.get(primary_type, fallback_emoji)

    score_missing = False
    try:
        c1 = int(result.get("c1_score", 0) or 0)
    except (TypeError, ValueError):
        c1 = 0
        score_missing = True
        missing_fields.add("c1_score")
        recovered_fields.add("c1_score")
    try:
        c2 = int(result.get("c2_score", 0) or 0)
    except (TypeError, ValueError):
        c2 = 0
        score_missing = True
        missing_fields.add("c2_score")
        recovered_fields.add("c2_score")
    try:
        c3 = int(result.get("c3_score", 0) or 0)
    except (TypeError, ValueError):
        c3 = 0
        score_missing = True
        missing_fields.add("c3_score")
        recovered_fields.add("c3_score")

    c1 = _clamp_score(c1, low=0, high=33)
    c2 = _clamp_score(c2, low=0, high=33)
    c3 = _clamp_score(c3, low=0, high=34)
    total_score = c1 + c2 + c3
    if score_missing and total_score == 0 and int(article.get("prefilter_score", 0) or 0) > 0:
        c1, c2, c3 = _allocate_component_scores(max(12, min(72, int(article.get("prefilter_score", 0) or 0) + 18)))
        total_score = c1 + c2 + c3

    summary_vi = _text_field("summary_vi", fallback=_seed_summary_from_article(article), max_len=260)
    factual_summary_vi = _text_field("factual_summary_vi", fallback=summary_vi, max_len=260)
    editorial_angle = _text_field("editorial_angle", fallback=_seed_editorial_from_article(article), max_len=180)
    why_it_matters_vi = _text_field("why_it_matters_vi", fallback=editorial_angle or factual_summary_vi, max_len=180)
    optional_editorial_angle = _text_field(
        "optional_editorial_angle",
        fallback=editorial_angle or why_it_matters_vi,
        max_len=180,
    )
    c1_reason = _text_field("c1_reason", fallback="Model không trả rõ lý do C1; hệ giữ mô tả ngắn để không mất trace.", max_len=160)
    c2_reason = _text_field("c2_reason", fallback="Model không trả rõ lý do C2; hệ giữ mô tả ngắn để không mất trace.", max_len=160)
    c3_reason = _text_field("c3_reason", fallback="Model không trả rõ lý do C3; hệ giữ mô tả ngắn để không mất trace.", max_len=160)

    raw_tags = result.get("tags", [])
    tags = _coerce_tag_list(raw_tags)
    if raw_tags in (None, ""):
        missing_fields.add("tags")
        recovered_fields.add("tags")
    elif not tags and raw_tags:
        recovered_fields.add("tags")

    analysis_tier = _derive_analysis_tier(result.get("analysis_tier", ""), total_score=total_score, min_score=min_score)
    if str(result.get("analysis_tier", "") or "").strip().lower() not in {"deep", "basic", "skip"}:
        missing_fields.add("analysis_tier")
        recovered_fields.add("analysis_tier")
    relevance_level = _derive_relevance_level(result.get("relevance_level", ""), total_score=total_score)
    if str(result.get("relevance_level", "") or "").strip() not in {"High", "Medium", "Low"}:
        missing_fields.add("relevance_level")
        recovered_fields.add("relevance_level")

    article.update(
        {
            "primary_type": primary_type,
            "primary_emoji": primary_emoji,
            "c1_score": c1,
            "c1_reason": c1_reason,
            "c2_score": c2,
            "c2_reason": c2_reason,
            "c3_score": c3,
            "c3_reason": c3_reason,
            "total_score": total_score,
            "summary_vi": summary_vi,
            "factual_summary_vi": factual_summary_vi,
            "why_it_matters_vi": why_it_matters_vi,
            "optional_editorial_angle": optional_editorial_angle,
            "editorial_angle": editorial_angle,
            "analysis_tier": analysis_tier,
            "tags": tags,
            "relevance_level": relevance_level,
        }
    )

    final_status = CLASSIFY_JSON_STATUS_PARTIAL if recovered_fields else json_status
    _set_classify_json_debug(
        article,
        status=final_status,
        missing_fields=sorted(missing_fields),
        recovered_fields=sorted(recovered_fields),
    )
    return final_status


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

    factual_summary = _clean_prose_snippet(" ".join(sentences[:2]).strip(), limit=260)
    if factual_summary:
        factual_summary = sanitize_delivery_text(factual_summary, max_len=260)
    why_raw = " ".join(sentences[1:2]).strip()
    if not why_raw:
        why_raw = f"Điểm đáng chú ý là {sentences[0][:160]}"
    why_it_matters = sanitize_delivery_text(_clean_prose_snippet(why_raw, limit=180), max_len=180)
    optional_angle = sanitize_delivery_text(_clean_prose_snippet(why_raw, limit=180), max_len=180)

    article["factual_summary_vi"] = factual_summary
    article["why_it_matters_vi"] = why_it_matters or (
        sanitize_delivery_text(_clean_prose_snippet(sentences[0], limit=160), max_len=160)
        if sentences
        else ""
    )
    article["optional_editorial_angle"] = optional_angle
    article["summary_vi"] = factual_summary or sanitize_delivery_text(
        _clean_prose_snippet(sentences[0], limit=260),
        max_len=260,
    )
    article["editorial_angle"] = optional_angle or sanitize_delivery_text(
        _clean_prose_snippet(sentences[-1], limit=180),
        max_len=180,
    )

    if article.get("analysis_tier") == "skip" and int(article.get("total_score", 0) or 0) >= max(38, min_score - 22):
        article["analysis_tier"] = "basic"

    _normalize_primary_type(article)
    _normalize_article_tags(article)
    _set_classify_json_debug(
        article,
        status=CLASSIFY_JSON_STATUS_PARTIAL,
        missing_fields=list(CLASSIFY_TEXT_FIELDS),
        recovered_fields=["summary_vi", "factual_summary_vi", "why_it_matters_vi", "optional_editorial_angle", "editorial_angle"],
    )
    article["classify_provider_used"] = str(article.get("classify_provider_used", "local") or "local")


def _call_grok_classify_inference(
    user_prompt: str,
    *,
    max_tokens: int,
) -> dict[str, Any] | None:
    schema = dict(CLASSIFY_RESPONSE_FORMAT.get("json_schema", {}).get("schema", {}))
    if not schema:
        return None
    parsed = call_xai_structured_json(
        system_prompt=CLASSIFY_SCORE_SYSTEM,
        user_prompt=user_prompt,
        schema_name="grok_classify_score_article",
        schema=schema,
        max_tokens=max_tokens,
    )
    return parsed if isinstance(parsed, dict) and parsed else None


def _resolve_classify_inference_details(
    user_prompt: str,
    *,
    max_tokens: int,
    temperature: float,
    initial_response: Any | None = None,
    runtime_config: dict[str, Any] | None = None,
    local_model_path: str | None = None,
) -> tuple[dict[str, Any] | None, str | None, str, str, dict[str, Any]]:
    def _parsed_or_repaired(response: Any) -> tuple[dict[str, Any] | None, str | None, str, bool]:
        parsed, raw, looks_structured = _normalize_classify_inference_response(response)
        if parsed and isinstance(parsed, dict):
            return parsed, CLASSIFY_JSON_STATUS_VALID, raw, looks_structured
        repaired = _recover_structured_json_dict(raw)
        if repaired and isinstance(repaired, dict):
            return repaired, CLASSIFY_JSON_STATUS_REPAIRED, raw, True
        return None, None, raw, looks_structured

    details: dict[str, Any] = {
        "provider_used": "local",
        "grok_request_count": 0,
        "grok_success_count": 0,
        "grok_fallback_count": 0,
        "grok_items_processed": 0,
        "classify_local_failure_count": 0,
        "classify_grok_rescue_count": 0,
        "classify_benchmark_request_count": 0,
        "classify_benchmark_success_count": 0,
    }
    grok_enabled = grok_classify_enabled(runtime_config)
    classify_mode = grok_classify_mode(runtime_config)

    if initial_response is None:
        initial_response = run_json_inference(
            CLASSIFY_SCORE_SYSTEM,
            user_prompt,
            max_tokens=max_tokens,
            temperature=temperature,
            response_format=CLASSIFY_RESPONSE_FORMAT,
            model_path=local_model_path,
        )

    result, status, raw, _ = _parsed_or_repaired(initial_response)
    if result is not None:
        if grok_enabled and classify_mode == "benchmark":
            details["grok_request_count"] += 1
            details["grok_items_processed"] += 1
            details["classify_benchmark_request_count"] += 1
            benchmark_result = _call_grok_classify_inference(
                user_prompt,
                max_tokens=max_tokens,
            )
            if benchmark_result:
                details["grok_success_count"] += 1
                details["classify_benchmark_success_count"] += 1
        return result, status, raw, str(details["provider_used"]), details

    logger.warning("⚠️ Classify chưa ra JSON ổn định, retry với prompt siết format.")
    retry_result, retry_status, retry_raw, retry_looks_structured = _parsed_or_repaired(
        run_json_inference(
            CLASSIFY_SCORE_SYSTEM,
            user_prompt + CLASSIFY_RETRY_SUFFIX,
            max_tokens=max_tokens,
            temperature=0.0,
            response_format=CLASSIFY_RESPONSE_FORMAT,
            model_path=local_model_path,
        )
    )
    if retry_result is not None:
        if grok_enabled and classify_mode == "benchmark":
            details["grok_request_count"] += 1
            details["grok_items_processed"] += 1
            details["classify_benchmark_request_count"] += 1
            benchmark_result = _call_grok_classify_inference(
                user_prompt,
                max_tokens=max_tokens,
            )
            if benchmark_result:
                details["grok_success_count"] += 1
                details["classify_benchmark_success_count"] += 1
        return retry_result, retry_status, retry_raw, str(details["provider_used"]), details

    logger.warning(
        "⚠️ Retry classify vẫn chưa ổn định (structured=%s); dùng prompt compact lần cuối.",
        retry_looks_structured,
    )
    final_result, final_status, final_raw, _ = _parsed_or_repaired(
        run_json_inference(
            CLASSIFY_SCORE_SYSTEM,
            user_prompt + CLASSIFY_LAST_CHANCE_SUFFIX,
            max_tokens=min(max_tokens, 220),
            temperature=0.0,
            response_format=CLASSIFY_RESPONSE_FORMAT,
            model_path=local_model_path,
        )
    )
    if final_result is not None:
        if grok_enabled and classify_mode == "benchmark":
            details["grok_request_count"] += 1
            details["grok_items_processed"] += 1
            details["classify_benchmark_request_count"] += 1
            benchmark_result = _call_grok_classify_inference(
                user_prompt,
                max_tokens=max_tokens,
            )
            if benchmark_result:
                details["grok_success_count"] += 1
                details["classify_benchmark_success_count"] += 1
        return final_result, final_status, final_raw, str(details["provider_used"]), details

    details["classify_local_failure_count"] += 1
    if grok_enabled:
        details["provider_used"] = "local_then_grok"
        details["grok_request_count"] += 1
        details["grok_items_processed"] += 1
        grok_result = _call_grok_classify_inference(
            user_prompt,
            max_tokens=max_tokens,
        )
        if grok_result is not None:
            details["grok_success_count"] += 1
            details["classify_grok_rescue_count"] += 1
            return grok_result, CLASSIFY_JSON_STATUS_VALID, final_raw or retry_raw or raw, str(details["provider_used"]), details
        details["grok_fallback_count"] += 1

    logger.debug("Last chance raw classify output: %s", final_raw[:500])
    return None, None, final_raw or retry_raw or raw, str(details["provider_used"]), details


def _resolve_classify_inference(
    user_prompt: str,
    *,
    max_tokens: int,
    temperature: float,
    initial_response: Any | None = None,
    runtime_config: dict[str, Any] | None = None,
    local_model_path: str | None = None,
) -> tuple[dict[str, Any] | None, str | None, str]:
    result, status, raw, _provider_used, _details = _resolve_classify_inference_details(
        user_prompt,
        max_tokens=max_tokens,
        temperature=temperature,
        initial_response=initial_response,
        runtime_config=runtime_config,
        local_model_path=local_model_path,
    )
    return result, status, raw


def _classify_inference_with_retry(
    user_prompt: str,
    *,
    max_tokens: int,
    temperature: float,
    initial_response: Any | None = None,
) -> dict[str, Any] | None:
    result, _status, _raw = _resolve_classify_inference(
        user_prompt,
        max_tokens=max_tokens,
        temperature=temperature,
        initial_response=initial_response,
    )
    return result


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
                    "workflow",
                    "orchestration",
                    "human-in-the-loop",
                    "clinic",
                    "healthcare",
                    "medical",
                    "observability",
                    "reliability",
                    "incident response",
                    "simulation",
                    "scenario",
                    "local deployment",
                    "private deployment",
                    "on-device",
                    "edge deployment",
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

    summary = (
        "Bài này ghi nhận một cập nhật mới trong hệ sinh thái AI, nhưng lượt classify hiện tại không trả JSON ổn định "
        "nên hệ giữ ở chế độ fallback an toàn."
    )
    editorial = (
        "Bài này có tín hiệu đủ gần để theo dõi trong batch chính."
    )
    editorial_angle = (
        "Điểm đáng chú ý là chủ đề và nguồn vẫn đủ rõ để giữ lại trong batch, nhưng phần diễn giải cần bám chặt dữ kiện sẵn có."
    )

    if not ai_relevant:
        summary = "Tin này chưa đủ liên quan trực tiếp tới AI để ưu tiên cao trong brief hiện tại."
        editorial_angle = "Bài này không phù hợp trọng tâm AI/product/business của brief sáng nay."
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
            "factual_summary_vi": summary,
            "why_it_matters_vi": editorial,
            "optional_editorial_angle": editorial_angle,
            "editorial_angle": editorial_angle,
            "analysis_tier": analysis_tier,
            "tags": [],
        }
    )
    _initialize_score_tracking(article, component_source="fallback")
    _recompute_relevance_level(article)
    _normalize_primary_type(article)
    _normalize_article_tags(article)
    article["summary_vi"] = sanitize_delivery_text(article.get("summary_vi", ""), max_len=260)
    article["editorial_angle"] = sanitize_delivery_text(article.get("editorial_angle", ""), max_len=180)
    _set_classify_json_debug(article, status=CLASSIFY_JSON_STATUS_FALLBACK)
    article["classify_provider_used"] = str(article.get("classify_provider_used", "local") or "local")


def _prefilter_score(
    article: dict[str, Any],
    feedback_preferences: dict[str, Any] | None = None,
) -> tuple[int, list[str]]:
    score = 0
    reasons: list[str] = []
    preferences = dict(feedback_preferences or {})
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
    source_history_runs = int(article.get("source_history_runs", 0) or 0)
    source_history_bonus = int(article.get("source_history_bonus", 0) or 0)
    source_history_penalty = int(article.get("source_history_penalty", 0) or 0)
    source_history_quality = int(article.get("source_history_quality_score", 50) or 50)
    source_history_noise_rate = float(article.get("source_history_noise_rate", 0.0) or 0.0)

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
    if source_kind == "github":
        priority_bonus = min(priority_bonus, 2 if is_github_main_brief_significant(article) else 1)
    elif source_kind == "community":
        priority_bonus = min(priority_bonus, 1)

    if priority_bonus:
        score += priority_bonus
        reasons.append(f"source_priority:{source_kind}+{priority_bonus}")

    if source_history_runs >= 3 and source_history_bonus > 0:
        learned_bonus = min(2, source_history_bonus)
        score += learned_bonus
        reasons.append(f"source_history+{learned_bonus}")
    if source_history_runs >= 3 and source_history_penalty > 0:
        learned_penalty = min(6, source_history_penalty)
        score -= learned_penalty
        reasons.append(f"source_history-{learned_penalty}")
    if source_history_runs >= 3 and source_history_quality <= 35 and source_kind in {"community", "search"}:
        score -= 4
        reasons.append("source_history_noise-4")
    if source_history_runs >= 3 and source_history_noise_rate >= 0.35:
        score -= 2
        reasons.append("source_noise_rate-2")

    if watchlist_hit:
        score += 2
        reasons.append("watchlist_hit+2")

    if community_strength:
        bonus = min(4, community_strength)
        if source_kind == "github":
            bonus = min(bonus, 2 if is_github_main_brief_significant(article) else 1)
        score += bonus
        reasons.append(f"community_signal+{bonus}")

    if source_kind == "github" and not is_github_main_brief_significant(article):
        score -= 3
        reasons.append("github_generic-3")

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

    strategic_hits = sum(1 for keyword in STRATEGIC_SIGNAL_KEYWORDS if keyword in lowered_text)
    if strategic_hits:
        strategic_bonus = min(6, strategic_hits)
        score += strategic_bonus
        reasons.append(f"strategic_hits+{strategic_bonus}")
    elif source_tier == "c":
        score -= 6
        reasons.append("no_strategic_signal_c_source-6")

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

    predicted_type = str(_prefilter_primary_type(title)[0] or "").strip().lower()
    predicted_type_aliases = {
        predicted_type,
        predicted_type.replace(" & culture", ""),
        predicted_type.replace(" ", "_"),
        predicted_type.replace(" & ", "_"),
    }
    preferred_types = {
        str(item or "").strip().lower()
        for item in preferences.get("preferred_types", [])
        if str(item or "").strip()
    }
    if preferences.get("strict_source_review") and source_tier in {"c", "unknown"}:
        score -= 2
        reasons.append("feedback_strict_source-2")
    if preferences.get("prefer_founder_angle") and (watchlist_hit or strategic_hits > 0):
        score += 2
        reasons.append("feedback_strategic+2")
    if preferences.get("prefer_depth") and article.get("content_available") and source_tier in {"a", "b"}:
        score += 1
        reasons.append("feedback_depth+1")
    if preferences.get("prefer_freshness") and freshness_bucket in {"breaking", "fresh", "recent"}:
        score += 1
        reasons.append("feedback_freshness+1")
    if preferred_types and predicted_type_aliases & preferred_types:
        score += 1
        reasons.append("feedback_type_fit+1")

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
        if not is_github_signal_article(article)
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
    feedback_preferences: dict[str, Any] | None = None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    ranked: list[dict[str, Any]] = []
    deprioritized: list[dict[str, Any]] = []
    for article in articles:
        prefilter_score, reasons = _prefilter_score(article, feedback_preferences=feedback_preferences)
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

    main_ranked = [article for article in ranked if not is_github_signal_article(article)]
    github_ranked = [article for article in ranked if is_github_signal_article(article)]

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
        not is_github_signal_article(article)
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
            "Bài này mới dừng ở mức tín hiệu sơ bộ và chưa vượt được nhóm ứng viên mạnh hơn trong batch hiện tại."
            if not strong_main_signal
            else "Bài này ghi nhận một cập nhật mới có giá trị, nhưng vẫn đứng sau các tín hiệu mạnh hơn ở vòng shortlist chính."
        ),
        "factual_summary_vi": (
            "Bài này mới dừng ở mức tín hiệu sơ bộ và chưa vượt được nhóm ứng viên mạnh hơn trong batch hiện tại."
            if not strong_main_signal
            else "Bài này ghi nhận một cập nhật mới có giá trị, nhưng vẫn đứng sau các tín hiệu mạnh hơn ở vòng shortlist chính."
        ),
        "why_it_matters_vi": (
            "Nếu chủ đề này xuất hiện lặp lại trong nguồn chính thống, có thể cân nhắc theo dõi để bắt kịp thay đổi vận hành."
            if not strong_main_signal
            else "Bài này hữu ích như tín hiệu phụ để theo dõi tiến trình vận hành sản phẩm AI trong ngắn hạn."
        ),
        "optional_editorial_angle": (
            "Giá trị hiện tại nằm ở việc bổ sung bối cảnh, chưa phải bài dẫn nhịp cho brief sáng."
            if not strong_main_signal
            else "Điểm đáng chú ý là nguồn và độ mới vẫn ổn, nên bài này còn hữu ích như một tín hiệu phụ của batch."
        ),
        "editorial_angle": (
            "Giá trị hiện tại nằm ở việc bổ sung bối cảnh, chưa phải bài dẫn nhịp cho brief sáng."
            if not strong_main_signal
            else "Điểm đáng chú ý là nguồn và độ mới vẫn ổn, nên bài này còn hữu ích như một tín hiệu phụ của batch."
        ),
        "analysis_tier": "basic" if total_score >= (28 if strong_main_signal else 24) and ai_relevant else "skip",
        "tags": [],
    })
    _initialize_score_tracking(article, component_source="held_out")
    if not ai_relevant:
        article["summary_vi"] = "Tin này chưa đủ liên quan trực tiếp tới AI để ưu tiên đưa vào brief."
        article["factual_summary_vi"] = article["summary_vi"]
        article["why_it_matters_vi"] = "Bài này chưa đáp ứng điều kiện AI/product/deep-opportunity của brief hiện tại."
        article["editorial_angle"] = "Bài này không phù hợp trọng tâm AI/product/business của brief sáng nay."
        article["optional_editorial_angle"] = article["editorial_angle"]
        article["analysis_tier"] = "skip"
        _record_score_adjustment(
            article,
            kind="held_out_cap",
            reason="not_ai_cap",
            new_total=min(int(article.get("total_score", 0) or 0), 20),
        )
    if editorial_noise:
        article["summary_vi"] = "Bài này lệch khá xa trọng tâm AI/product/business của brief sáng nay."
        article["factual_summary_vi"] = article["summary_vi"]
        article["why_it_matters_vi"] = "Nội dung chưa đủ tín hiệu vận hành thực tế để đẩy lên main brief."
        article["editorial_angle"] = "Nội dung gần với noise bề mặt hơn là tín hiệu quyết định cho batch hiện tại."
        article["optional_editorial_angle"] = article["editorial_angle"]
        article["analysis_tier"] = "skip"
        _record_score_adjustment(
            article,
            kind="held_out_cap",
            reason="editorial_noise_cap",
            new_total=min(int(article.get("total_score", 0) or 0), 8),
        )
    _recompute_relevance_level(article)
    _normalize_article_tags(article)
    _set_classify_json_debug(article, status=CLASSIFY_JSON_STATUS_FALLBACK)
    article["classify_provider_used"] = str(article.get("classify_provider_used", "local") or "local")
    article["summary_vi"] = sanitize_delivery_text(article.get("summary_vi", ""), max_len=260)
    article["editorial_angle"] = sanitize_delivery_text(article.get("editorial_angle", ""), max_len=180)


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

    # Tin chiến lược nguồn mạnh thường nên nằm ở lane Product thay vì bị drift sang legacy policy/business.
    if current_type in {"Policy", "Policy & Ethics", "Business", "Research"}:
        article["primary_type"] = "Product"
        article["primary_emoji"] = "🚀"

    # Nguồn mạnh + tín hiệu chiến lược + score đã khá gần ngưỡng thì đẩy lên deep.
    if score >= max(40, min_score - 20):
        article["analysis_tier"] = "deep"
        article["relevance_level"] = "High" if score >= 55 else article.get("relevance_level", "Medium")

        # Nếu content đầy đủ, boost thêm chút để tăng cơ hội vào top list.
        if content_available and score < min_score:
            _record_score_adjustment(
                article,
                kind="strategic_boost",
                reason="strong_source_strategic_signal",
                new_total=min(min_score, score + 8),
            )


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


def _compact_title_slug(title: str) -> str:
    """Chuỗi chữ-số-thường dùng để so khớp tiêu đề ngắn nằm trong tiêu đề dài."""
    base = _normalize_key(title)
    base = re.sub(r"[^a-z0-9]+", " ", base).strip()
    return re.sub(r"\s+", " ", base)


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
        article.get("factual_summary_vi", ""),
        article.get("why_it_matters_vi", ""),
        article.get("optional_editorial_angle", ""),
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


def _clamp_score(value: Any, *, low: int = 0, high: int = 100) -> int:
    try:
        numeric = int(value or 0)
    except (TypeError, ValueError):
        numeric = 0
    return max(low, min(high, numeric))


def _allocate_component_scores(total_score: int) -> tuple[int, int, int]:
    total = _clamp_score(total_score)
    raw_scores = [total * cap / 100 for cap in SCORE_COMPONENT_CAPS]
    allocated = [min(cap, int(raw_score)) for cap, raw_score in zip(SCORE_COMPONENT_CAPS, raw_scores)]
    remaining = total - sum(allocated)
    if remaining > 0:
        remainders = sorted(
            range(len(SCORE_COMPONENT_CAPS)),
            key=lambda idx: (raw_scores[idx] - int(raw_scores[idx]), SCORE_COMPONENT_CAPS[idx] - allocated[idx]),
            reverse=True,
        )
        for idx in remainders:
            if remaining <= 0:
                break
            if allocated[idx] >= SCORE_COMPONENT_CAPS[idx]:
                continue
            allocated[idx] += 1
            remaining -= 1
    return allocated[0], allocated[1], allocated[2]


def _normalize_component_scores(article: dict[str, Any]) -> int:
    c1 = _clamp_score(article.get("c1_score", 0), low=0, high=33)
    c2 = _clamp_score(article.get("c2_score", 0), low=0, high=33)
    c3 = _clamp_score(article.get("c3_score", 0), low=0, high=34)
    component_total = c1 + c2 + c3
    fallback_total = _clamp_score(article.get("base_total_score", article.get("total_score", 0)))

    if component_total <= 0 and fallback_total > 0:
        c1, c2, c3 = _allocate_component_scores(fallback_total)
        component_total = c1 + c2 + c3
        article["component_score_source"] = "backfilled_from_total"
        article.setdefault(
            "c1_reason",
            "Điểm C1 được nội suy từ tổng điểm gốc vì run này không giữ đủ output chi tiết của model.",
        )
        article.setdefault(
            "c2_reason",
            "Điểm C2 được nội suy từ tổng điểm gốc để giữ báo cáo debug nhất quán.",
        )
        article.setdefault(
            "c3_reason",
            "Điểm C3 được nội suy từ tổng điểm gốc để tránh trạng thái report không giải thích được.",
        )
    else:
        article.setdefault("component_score_source", "explicit")

    article["c1_score"] = c1
    article["c2_score"] = c2
    article["c3_score"] = c3
    return component_total


def _initialize_score_tracking(article: dict[str, Any], *, component_source: str) -> None:
    base_total = _normalize_component_scores(article)
    article["component_score_source"] = component_source or article.get("component_score_source", "explicit")
    article["base_total_score"] = base_total
    article["adjusted_total_score"] = base_total
    article["total_score"] = base_total
    article["score_adjustment_total"] = 0
    article["applied_adjustments"] = []


def _ensure_score_tracking(article: dict[str, Any]) -> None:
    base_total = _normalize_component_scores(article)
    adjusted_total = _clamp_score(article.get("adjusted_total_score", article.get("total_score", base_total)))
    adjustments: list[dict[str, Any]] = []
    running_total = base_total

    for raw_item in article.get("applied_adjustments", []) or []:
        if not isinstance(raw_item, dict):
            continue
        delta = _clamp_score(raw_item.get("delta", 0), low=-100, high=100)
        if delta == 0:
            continue
        before = _clamp_score(raw_item.get("before", running_total))
        after = _clamp_score(raw_item.get("after", before + delta))
        actual_delta = after - before
        if actual_delta == 0:
            continue
        adjustments.append(
            {
                "kind": str(raw_item.get("kind", "") or "score_adjustment"),
                "reason": str(raw_item.get("reason", "") or "unspecified_adjustment"),
                "delta": actual_delta,
                "before": before,
                "after": after,
            }
        )
        running_total = after

    residual_delta = adjusted_total - (base_total + sum(int(item["delta"]) for item in adjustments))
    if residual_delta != 0:
        before = adjusted_total - residual_delta
        adjustments.append(
            {
                "kind": "compat_backfill",
                "reason": "legacy_total_delta",
                "delta": residual_delta,
                "before": before,
                "after": adjusted_total,
            }
        )

    article["base_total_score"] = base_total
    article["adjusted_total_score"] = adjusted_total
    article["total_score"] = adjusted_total
    article["applied_adjustments"] = adjustments
    article["score_adjustment_total"] = adjusted_total - base_total


def _record_score_adjustment(
    article: dict[str, Any],
    *,
    kind: str,
    reason: str,
    new_total: int,
) -> int:
    _ensure_score_tracking(article)
    before = _clamp_score(article.get("adjusted_total_score", article.get("total_score", 0)))
    after = _clamp_score(new_total)
    delta = after - before
    article["adjusted_total_score"] = after
    article["total_score"] = after
    if delta != 0:
        adjustments = list(article.get("applied_adjustments", []) or [])
        adjustments.append(
            {
                "kind": kind,
                "reason": reason,
                "delta": delta,
                "before": before,
                "after": after,
            }
        )
        article["applied_adjustments"] = adjustments
    article["score_adjustment_total"] = after - _clamp_score(article.get("base_total_score", before))
    return delta


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
        _record_score_adjustment(
            article,
            kind="freshness",
            reason="stale_candidate",
            new_total=max(0, score - 25),
        )
        article["freshness_status"] = "stale_candidate"
        article["analysis_tier"] = "skip" if article.get("total_score", 0) < min_score + 10 else "basic"
    elif is_old_news:
        _record_score_adjustment(
            article,
            kind="freshness",
            reason="old_news",
            new_total=max(0, score - 15),
        )
        article["freshness_status"] = "old_news"
        if article.get("analysis_tier") == "deep":
            article["analysis_tier"] = "basic"
    elif freshness_unknown and source_tier == "c":
        _record_score_adjustment(
            article,
            kind="freshness",
            reason="unknown_weak_source",
            new_total=max(0, score - 12),
        )
        article["freshness_status"] = "unknown_weak_source"
        if article.get("analysis_tier") == "deep":
            article["analysis_tier"] = "basic"
    elif freshness_unknown and not content_available:
        _record_score_adjustment(
            article,
            kind="freshness",
            reason="unknown_thin_content",
            new_total=max(0, score - 10),
        )
        article["freshness_status"] = "unknown_thin_content"
        if article.get("analysis_tier") == "deep":
            article["analysis_tier"] = "basic"
    else:
        article["freshness_status"] = "ok"

    if isinstance(age_hours, (int, float)) and age_hours <= 48 and article["freshness_status"] == "ok":
        _record_score_adjustment(
            article,
            kind="freshness",
            reason="fresh_boost",
            new_total=min(100, int(article.get("total_score", 0) or 0) + 5),
        )
        article["freshness_status"] = "fresh_boost"
        if article.get("analysis_tier") == "basic" and article.get("total_score", 0) >= min_score - 5:
            article["analysis_tier"] = "deep"

    _recompute_relevance_level(article)


def _apply_source_history_adjustment(article: dict[str, Any], min_score: int) -> None:
    source_history_runs = int(article.get("source_history_runs", 0) or 0)
    if source_history_runs < 3:
        return

    source_kind = str(article.get("source_kind", "unknown") or "unknown").lower()
    source_history_bonus = int(article.get("source_history_bonus", 0) or 0)
    source_history_penalty = int(article.get("source_history_penalty", 0) or 0)
    noise_rate = float(article.get("source_history_noise_rate", 0.0) or 0.0)
    total_score = int(article.get("total_score", 0) or 0)
    adjustment = 0

    if source_history_penalty > 0 and source_kind in {"community", "search"}:
        adjustment -= min(8, source_history_penalty + (2 if noise_rate >= 0.35 else 0))
    elif source_history_bonus > 0 and source_kind in {"official", "strong_media", "watchlist"}:
        if not article.get("is_old_news") and not article.get("is_stale_candidate"):
            adjustment += min(4, source_history_bonus)

    if adjustment == 0:
        return

    article["source_history_adjustment"] = adjustment
    _record_score_adjustment(
        article,
        kind="source_history",
        reason="source_history_adjustment",
        new_total=max(0, min(100, total_score + adjustment)),
    )
    if adjustment < 0:
        if article.get("analysis_tier") == "deep" and article["total_score"] < min_score:
            article["analysis_tier"] = "basic"
        if article.get("analysis_tier") == "basic" and article["total_score"] < 28:
            article["analysis_tier"] = "skip"
    elif adjustment > 0 and article.get("analysis_tier") == "basic" and article["total_score"] >= max(40, min_score - 4):
        article["analysis_tier"] = "deep"

    _recompute_relevance_level(article)


def _finalize_scored_article(article: dict[str, Any], min_score: int) -> None:
    _ensure_score_tracking(article)
    article["score"] = int(article.get("total_score", 0) or 0)
    _normalize_primary_type(article)
    _normalize_article_tags(article)
    article["summary_vi"] = sanitize_delivery_text(article.get("summary_vi", ""), max_len=260)
    if not article.get("factual_summary_vi"):
        article["factual_summary_vi"] = article.get("summary_vi", "")
    if not article.get("why_it_matters_vi"):
        article["why_it_matters_vi"] = article.get("editorial_angle", "")
    if not article.get("optional_editorial_angle"):
        article["optional_editorial_angle"] = article.get("editorial_angle", "")

    article["factual_summary_vi"] = sanitize_delivery_text(article.get("factual_summary_vi", ""), max_len=260)
    article["why_it_matters_vi"] = sanitize_delivery_text(article.get("why_it_matters_vi", ""), max_len=180)
    article["optional_editorial_angle"] = sanitize_delivery_text(article.get("optional_editorial_angle", ""), max_len=180)
    article["editorial_angle"] = sanitize_delivery_text(article.get("editorial_angle", ""), max_len=180)
    apply_main_brief_routing(article)
    article["score_breakdown"] = _build_score_breakdown(article)
    article["why_surfaced"] = article["score_breakdown"]["why_surfaced"]
    if article.get("analysis_tier") == "skip":
        article["why_skipped"] = article["score_breakdown"]["why_skipped"] or article["why_surfaced"][:2]


def _build_score_breakdown(article: dict[str, Any]) -> dict[str, Any]:
    _ensure_score_tracking(article)
    prefilter_reasons = [str(reason or "") for reason in article.get("prefilter_reasons", [])]
    c1_reason = str(article.get("c1_reason", "") or "")
    c2_reason = str(article.get("c2_reason", "") or "")
    c3_reason = str(article.get("c3_reason", "") or "")
    source_kind = str(article.get("source_kind", "unknown") or "unknown")
    route_reason = str(article.get("main_brief_skip_reason", "") or "").strip().lower()
    applied_adjustments = list(article.get("applied_adjustments", []) or [])
    adjustment_labels = [
        f"{item.get('reason', item.get('kind', 'adjustment'))}{int(item.get('delta', 0) or 0):+d}"
        for item in applied_adjustments[:4]
        if int(item.get("delta", 0) or 0) != 0
    ]

    surfaced_reasons = prefilter_reasons[:4]
    surfaced_reasons.extend(
        reason for reason in [
            _clean_reason_snippet(c1_reason, 80),
            _clean_reason_snippet(c2_reason, 80),
            _clean_reason_snippet(c3_reason, 80),
        ]
        if reason
    )
    surfaced_reasons.extend(adjustment_labels)

    return {
        "source_kind": source_kind,
        "source_priority": int(article.get("source_priority", 0) or 0),
        "source_priority_base": int(article.get("source_priority_base", article.get("source_priority", 0)) or 0),
        "community_signal_strength": int(article.get("community_signal_strength", 0) or 0),
        "watchlist_hit": bool(article.get("watchlist_hit", False)),
        "source_history_quality_score": int(article.get("source_history_quality_score", 50) or 50),
        "source_history_bonus": int(article.get("source_history_bonus", 0) or 0),
        "source_history_penalty": int(article.get("source_history_penalty", 0) or 0),
        "source_history_adjustment": int(article.get("source_history_adjustment", 0) or 0),
        "prefilter_score": int(article.get("prefilter_score", 0) or 0),
        "interesting_signal_score": int(article.get("interesting_signal_score", article.get("total_score", 0)) or 0),
        "delivery_lane_candidate": str(article.get("delivery_lane_candidate", "") or ""),
        "main_brief_eligibility": str(article.get("main_brief_eligibility", "") or ""),
        "main_brief_score": int(article.get("main_brief_score", 0) or 0),
        "main_brief_reason_codes": list(article.get("main_brief_reason_codes", []) or []),
        "main_brief_skip_reason": route_reason,
        "component_score_source": str(article.get("component_score_source", "explicit") or "explicit"),
        "classify_provider_used": str(article.get("classify_provider_used", "local") or "local"),
        "component_score_sum": int(article.get("base_total_score", 0) or 0),
        "c1_score": int(article.get("c1_score", 0) or 0),
        "c2_score": int(article.get("c2_score", 0) or 0),
        "c3_score": int(article.get("c3_score", 0) or 0),
        "base_total_score": int(article.get("base_total_score", 0) or 0),
        "adjusted_total_score": int(article.get("adjusted_total_score", article.get("total_score", 0)) or 0),
        "score_adjustment_total": int(article.get("score_adjustment_total", 0) or 0),
        "score_display": "adjusted" if int(article.get("score_adjustment_total", 0) or 0) != 0 else "base",
        "applied_adjustments": applied_adjustments,
        "total_score": int(article.get("total_score", 0) or 0),
        "why_surfaced": surfaced_reasons[:5],
        "why_skipped": [
            reason
            for reason in (
                ([f"main_brief:{route_reason}"] if route_reason else [])
                + [
                    reason
                    for reason in prefilter_reasons
                    if reason.startswith(("editorial_noise", "blocked_domain", "soft_blocked_domain", "not_ai_relevant", "stale", "old_news", "source_history"))
                ]
            )
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

    # Toàn bộ token phía tiêu đề ngắn nằm trong tiêu đề dài (vd: "OpenAI acquires TBPN"
    # vs bài dài cùng cụm đầu) — Jaccard thấp vì phía dài thêm nhiều từ phụ.
    if len(left_tokens) <= len(right_tokens) and left_tokens <= right_tokens and len(left_tokens) >= 3:
        return True
    if len(right_tokens) <= len(left_tokens) and right_tokens <= left_tokens and len(right_tokens) >= 3:
        return True

    # Chuỗi slug: tiêu đề ngắn (đủ dài) là tiền tố nằm trong tiêu đề dài sau chuẩn hóa.
    ca = _compact_title_slug(str(left.get("title", "") or ""))
    cb = _compact_title_slug(str(right.get("title", "") or ""))
    if len(ca) >= 18 and len(cb) >= 18:
        shorter, longer = (ca, cb) if len(ca) <= len(cb) else (cb, ca)
        if shorter in longer:
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
            _record_score_adjustment(
                primary,
                kind="event_consensus",
                reason="event_consensus_bonus",
                new_total=min(100, int(primary.get("total_score", 0) or 0) + event_bonus),
            )
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

    if current_type in {"Research", "Business"}:
        article["primary_type"] = "Product"
        article["primary_emoji"] = "🚀"
        current_type = "Product"
    elif current_type in {"Policy", "Policy & Ethics", "Society"}:
        article["primary_type"] = "Society & Culture"
        article["primary_emoji"] = "🌍"
        current_type = "Society & Culture"

    # Ecosystem/community stories nên ưu tiên Society & Culture.
    if "ecosystem" in title or "hệ sinh thái" in title or "he sinh thai" in title:
        if "startup" not in title and "partnership" not in title and "partners with" not in title:
            article["primary_type"] = "Society & Culture"
            article["primary_emoji"] = "🌍"
            return

    # Incident/security/compliance nên nghiêng về Society & Culture hơn legacy policy bucket.
    if any(keyword in title for keyword in POLICY_KEYWORDS):
        article["primary_type"] = "Society & Culture"
        article["primary_emoji"] = "🌍"
        return

    # Nguồn official mà bề mặt bài rõ là update sản phẩm thì đừng rơi nhầm về Society & Culture.
    if source_domain in {
        "openai.com",
        "anthropic.com",
        "ai.meta.com",
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
                "benchmark",
            )
        ):
            article["primary_type"] = "Product"
            article["primary_emoji"] = "🚀"
            return

    # Tin về hệ sinh thái / cộng đồng / giáo dục / bối cảnh Việt Nam nên nghiêng về Society & Culture.
    if any(keyword in title for keyword in SOCIETY_KEYWORDS):
        if current_type not in {"Product"} and "startup" not in title and "partnership" not in title and "partners with" not in title:
            article["primary_type"] = "Society & Culture"
            article["primary_emoji"] = "🌍"


def _select_top_articles(
    primary_event_articles: list[dict[str, Any]],
    *,
    min_items: int = 3,
    max_items: int = 10,
) -> tuple[list[dict[str, Any]], int]:
    if not primary_event_articles:
        return [], 0

    ranked_articles = sorted(
        primary_event_articles,
        key=lambda article: int(article.get("score", article.get("total_score", 0)) or 0),
        reverse=True,
    )
    scores = [int(article.get("score", article.get("total_score", 0)) or 0) for article in ranked_articles]
    ordered_scores = sorted(scores)
    score_cutoff = ordered_scores[int(len(scores) * 0.7)] if len(scores) > 5 else 55
    selected = [
        article
        for article in ranked_articles
        if int(article.get("score", article.get("total_score", 0)) or 0) >= score_cutoff
        or str(article.get("analysis_tier", "") or "").strip().lower() == "deep"
    ]
    minimum_target = min(max(min_items, 0), len(ranked_articles), max_items)
    if len(selected) < minimum_target:
        selected = ranked_articles[:minimum_target]
        score_cutoff = int(selected[-1].get("score", selected[-1].get("total_score", 0)) or 0)
    return selected[:max_items], score_cutoff


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
    min_score = _cfg_int(state, "min_deep_analysis_score", "MIN_DEEP_ANALYSIS_SCORE", 55)
    max_top = _cfg_int(state, "max_deep_analysis_articles", "MAX_DEEP_ANALYSIS_ARTICLES", 10)
    max_classify = _cfg_int(state, "max_classify_articles", "MAX_CLASSIFY_ARTICLES", 25)
    classify_content_limit = _cfg_int(state, "classify_content_char_limit", "CLASSIFY_CONTENT_CHAR_LIMIT", 900)
    classify_max_tokens = _cfg_int(state, "classify_max_tokens", "CLASSIFY_MAX_TOKENS", 320)
    runtime_config = dict(state.get("runtime_config", {}) or {})
    grok_classify_is_enabled = grok_classify_enabled(runtime_config)

    llm_articles, held_out_articles = _prepare_classify_candidates(
        list(articles),
        max_classify,
        runtime_config=runtime_config,
        feedback_summary_text=state.get("feedback_summary_text", ""),
        feedback_preferences=state.get("feedback_preference_profile", {}),
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
    classify_grok_request_count = 0
    classify_grok_success_count = 0
    classify_grok_fallback_count = 0
    classify_grok_items_processed = 0
    classify_local_failure_count = 0
    classify_grok_rescue_count = 0
    classify_benchmark_request_count = 0
    classify_benchmark_success_count = 0
    classify_provider_counts = {"local": 0, "grok": 0, "local_then_grok": 0}

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
            light_mlx = resolve_pipeline_mlx_path("light", runtime_config)
            initial_inference = run_json_inference(
                CLASSIFY_SCORE_SYSTEM,
                user_prompt,
                max_tokens=classify_max_tokens,
                temperature=0.1,
                response_format=CLASSIFY_RESPONSE_FORMAT,
                model_path=light_mlx,
            )
            result, json_status, raw_output, provider_used, provider_details = _resolve_classify_inference_details(
                user_prompt,
                max_tokens=classify_max_tokens,
                temperature=0.1,
                initial_response=initial_inference,
                runtime_config=runtime_config,
                local_model_path=light_mlx,
            )
            provider_key = str(provider_used or "local")
            if provider_key not in classify_provider_counts:
                provider_key = "local"
            classify_provider_counts[provider_key] += 1
            article["classify_provider_used"] = provider_key
            classify_grok_request_count += int(provider_details.get("grok_request_count", 0) or 0)
            classify_grok_success_count += int(provider_details.get("grok_success_count", 0) or 0)
            classify_grok_fallback_count += int(provider_details.get("grok_fallback_count", 0) or 0)
            classify_grok_items_processed += int(provider_details.get("grok_items_processed", 0) or 0)
            classify_local_failure_count += int(provider_details.get("classify_local_failure_count", 0) or 0)
            classify_grok_rescue_count += int(provider_details.get("classify_grok_rescue_count", 0) or 0)
            classify_benchmark_request_count += int(provider_details.get("classify_benchmark_request_count", 0) or 0)
            classify_benchmark_success_count += int(provider_details.get("classify_benchmark_success_count", 0) or 0)

            if result and isinstance(result, dict):
                _apply_structured_classify_result(
                    article,
                    result,
                    min_score,
                    json_status=json_status or CLASSIFY_JSON_STATUS_VALID,
                )
                _initialize_score_tracking(article, component_source="model")
                _apply_strategic_boost(article, min_score)
                _apply_freshness_penalty(article, min_score)
                _apply_source_history_adjustment(article, min_score)
                _finalize_scored_article(article, min_score)
            else:
                logger.warning("⚠️ Model không trả JSON ổn định cho '%s'; dùng prose rescue/fallback.", title[:40])
                _classify_prose_rescue(article, raw_output, min_score)
                _apply_source_history_adjustment(article, min_score)
                _finalize_scored_article(article, min_score)
        except Exception as e:
            logger.error("❌ Classify failed: '%s': %s", title[:40], e)
            article["classify_provider_used"] = str(article.get("classify_provider_used", "local") or "local")
            classify_provider_counts[str(article.get("classify_provider_used", "local") or "local")] = (
                classify_provider_counts.get(str(article.get("classify_provider_used", "local") or "local"), 0) + 1
            )
            _llm_failure_fallback(article, min_score)
            _apply_source_history_adjustment(article, min_score)
            _finalize_scored_article(article, min_score)

        scored.append(article)

    for article in held_out_articles:
        _held_out_article_fallback(article)
        _apply_source_history_adjustment(article, min_score)
        _finalize_scored_article(article, min_score)
        scored.append(article)

    # Sắp xếp theo score giảm dần
    scored.sort(key=lambda a: a.get("total_score", 0), reverse=True)
    primary_event_articles = _annotate_event_clusters(scored, min_score)
    for article in scored:
        _finalize_scored_article(article, min_score)
    primary_event_articles.sort(key=lambda a: a.get("total_score", 0), reverse=True)

    # Chỉ deep-dive 1 bài đại diện cho mỗi event để tránh lãng phí reasoning.
    top, score_cutoff = _select_top_articles(
        primary_event_articles,
        max_items=max_top,
    )
    low = [a for a in scored if a not in top]

    logger.info(
        "✅ Classify+Score xong: %d bài / %d event → %d top (cutoff=%d, max=%d) + %d low",
        len(scored), len(primary_event_articles), len(top), score_cutoff, max_top, len(low)
    )
    scored_snapshot_path = write_temporal_snapshot(
        state=state,
        stage="scored",
        articles=scored,
        extra={
            "scored_count": len(scored),
            "primary_event_count": len(primary_event_articles),
            "top_count": len(top),
            "low_score_count": len(low),
            "min_deep_analysis_score": min_score,
            "dynamic_score_cutoff": score_cutoff,
            "max_deep_analysis_articles": max_top,
            "max_classify_articles": max_classify,
        },
    )

    grok_metrics = merge_grok_observability(
        state,
        stage="classify",
        enabled=grok_classify_is_enabled,
        request_count=classify_grok_request_count,
        success_count=classify_grok_success_count,
        fallback_count=classify_grok_fallback_count,
        items_processed=classify_grok_items_processed,
        applied=classify_grok_rescue_count > 0,
        extra={
            "local_failure_count": classify_local_failure_count,
            "grok_rescue_count": classify_grok_rescue_count,
            "benchmark_request_count": classify_benchmark_request_count,
            "benchmark_success_count": classify_benchmark_success_count,
            "provider_local_count": classify_provider_counts.get("local", 0),
            "provider_grok_count": classify_provider_counts.get("grok", 0),
            "provider_local_then_grok_count": classify_provider_counts.get("local_then_grok", 0),
        },
    )

    return {
        "scored_articles": scored,
        "top_articles": top,
        "low_score_articles": low,
        "scored_snapshot_path": scored_snapshot_path,
        **grok_metrics,
    }

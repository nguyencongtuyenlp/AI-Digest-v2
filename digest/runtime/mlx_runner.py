"""
mlx_runner.py - Shared MLX model cache and schema-first JSON inference.

Supports optional dual-model runs:
- MLX_MODEL (heavy): deep analysis, delivery judge, batch fallback
- MLX_LIGHT_MODEL (light): batch classify + quick compose when set

When runtime_config["runtime_mlx_model"] is set (e.g. fast preset), both tiers use that path.
"""

from __future__ import annotations

import ast
import gc
import inspect
import json
import logging
import os
import re
import threading
from typing import Any

from jsonschema import ValidationError, validate

logger = logging.getLogger(__name__)

_mlx_cache: dict[str, tuple[Any, Any]] = {}
_sampler_param = None
_runtime_fallback_model_path = None
_runtime_mlx_model_path: str | None = None

# LangGraph có thể chạy batch_deep và batch_quick song song — hai luồng cùng gọi Metal dễ gây assert MTLCommandBuffer.
_mlx_inference_lock = threading.Lock()


def _mlx_inference_serialize_enabled() -> bool:
    return os.getenv("MLX_INFERENCE_SERIALIZE", "1").strip().lower() not in {"0", "false", "no"}


# Schema batch: model đôi khi trả JSON mảng hoặc một object bài thay vì {"articles":[...]} — ép hình trước khi validate.
_CLASSIFY_BATCH_FINGERPRINT = frozenset(
    {
        "item_id",
        "primary_type",
        "primary_emoji",
        "c1_score",
        "c2_score",
        "c3_score",
        "summary_vi",
        "relevance_level",
        "analysis_tier",
        "editorial_angle",
        "factual_summary_vi",
        "why_it_matters_vi",
        "optional_editorial_angle",
    }
)
_QUICK_BATCH_FINGERPRINT = frozenset({"item_id", "note_summary_vi", "summary_vi"})
_DEEP_BATCH_FINGERPRINT = frozenset(
    {"item_id", "deep_analysis", "recommend_idea", "note_summary_vi", "content_page_md"}
)
_KNOWN_BATCH_SCHEMA_COERCION: dict[str, tuple[str, frozenset[str]]] = {
    "batch_classify_score_articles": (
        "articles",
        frozenset({"item_id", "primary_type", "c1_score", "summary_vi"}),
    ),
    "batch_quick_note_summary": (
        "articles",
        frozenset({"item_id", "note_summary_vi"}),
    ),
    "batch_deep_process_articles": (
        "articles",
        frozenset({"item_id", "deep_analysis", "recommend_idea", "note_summary_vi"}),
    ),
}


def _coerce_batch_articles_dict(
    parsed: dict[str, Any],
    array_key: str,
    hint_keys: frozenset[str],
    *,
    loose_fingerprint: frozenset[str] | None = None,
    loose_min_matches: int = 0,
) -> dict[str, Any]:
    """Chuẩn hoá key bọc mảng + giá trị articles dạng chuỗi JSON."""
    if array_key in parsed:
        inner = parsed[array_key]
        if isinstance(inner, str) and inner.strip():
            try:
                loaded = json.loads(inner.strip())
                if isinstance(loaded, list):
                    return {array_key: loaded}
                if isinstance(loaded, dict):
                    return {array_key: [loaded]}
            except json.JSONDecodeError:
                pass
        if isinstance(inner, dict):
            return {array_key: [inner]}
        return parsed

    for alt in ("Articles", "items", "Items", "results", "data"):
        if alt in parsed:
            val = parsed[alt]
            if isinstance(val, list):
                return {array_key: val}
            if isinstance(val, dict):
                return {array_key: [val]}
    if "article" in parsed and isinstance(parsed["article"], dict):
        return {array_key: [parsed["article"]]}

    keys = frozenset(str(k) for k in parsed.keys())
    if len(keys & hint_keys) >= 1:
        return {array_key: [parsed]}
    if (
        loose_fingerprint
        and loose_min_matches > 0
        and len(keys & loose_fingerprint) >= loose_min_matches
    ):
        return {array_key: [parsed]}
    return parsed


def _coerce_parsed_for_json_schema(parsed: Any, response_format: dict[str, Any] | None) -> Any:
    if not isinstance(response_format, dict) or not isinstance(parsed, (dict, list)):
        return parsed
    jsw = dict(response_format.get("json_schema") or {})
    name = str(jsw.get("name", "")).strip()
    entry = _KNOWN_BATCH_SCHEMA_COERCION.get(name)
    if not entry:
        return parsed
    array_key, hint_keys = entry
    if isinstance(parsed, list) and parsed and all(isinstance(x, dict) for x in parsed):
        return {array_key: parsed}
    if isinstance(parsed, dict):
        if name == "batch_classify_score_articles":
            return _coerce_batch_articles_dict(
                parsed,
                array_key,
                hint_keys,
                loose_fingerprint=_CLASSIFY_BATCH_FINGERPRINT,
                loose_min_matches=4,
            )
        if name == "batch_quick_note_summary":
            return _coerce_batch_articles_dict(
                parsed,
                array_key,
                hint_keys,
                loose_fingerprint=_QUICK_BATCH_FINGERPRINT,
                loose_min_matches=2,
            )
        if name == "batch_deep_process_articles":
            return _coerce_batch_articles_dict(
                parsed,
                array_key,
                hint_keys,
                loose_fingerprint=_DEEP_BATCH_FINGERPRINT,
                loose_min_matches=2,
            )
        if array_key in parsed:
            inner = parsed[array_key]
            if isinstance(inner, dict):
                return {array_key: [inner]}
            return parsed
        if hint_keys.intersection(parsed.keys()):
            return {array_key: [parsed]}
    return parsed


def _default_model_path() -> str:
    return os.getenv("MLX_MODEL", "mlx-community/Qwen2.5-32B-Instruct-4bit")


def _fallback_model_path() -> str:
    return os.getenv("MLX_FALLBACK_MODEL", "mlx-community/Qwen2.5-14B-Instruct-4bit")


def resolve_pipeline_mlx_path(tier: str, runtime_config: dict[str, Any] | None = None) -> str:
    """
    Resolve MLX repo id for this pipeline tier.

    tier: "light" | "heavy"
    If runtime_mlx_model is set, it wins for both tiers (single-model preset).
    """
    rc = dict(runtime_config or {})
    forced = str(rc.get("runtime_mlx_model", "") or "").strip()
    if forced:
        return forced
    if str(tier or "").strip().lower() == "light":
        light = os.getenv("MLX_LIGHT_MODEL", "").strip()
        if light:
            return light
    return _default_model_path()


def _is_oom_error(exc: Exception) -> bool:
    text = str(exc or "").lower()
    return any(
        token in text
        for token in (
            "insufficient memory",
            "outofmemory",
            "out of memory",
            "kio gpu command buffer callback error out of memory",
            "metal",
        )
    )


def _effective_model_path(requested_model_path: str | None = None) -> str:
    # Explicit path from caller wins (dual-tier); then global run override; then OOM fallback; then default.
    if requested_model_path and str(requested_model_path).strip():
        return str(requested_model_path).strip()
    if _runtime_mlx_model_path:
        return _runtime_mlx_model_path
    if _runtime_fallback_model_path:
        return _runtime_fallback_model_path
    return _default_model_path()


def set_runtime_mlx_model_path(model_path: str | None) -> None:
    global _runtime_mlx_model_path
    _runtime_mlx_model_path = str(model_path).strip() if model_path else None


def clear_runtime_mlx_model_path() -> None:
    global _runtime_mlx_model_path
    _runtime_mlx_model_path = None


def _release_mlx_cache_all() -> None:
    global _mlx_cache
    _mlx_cache.clear()
    gc.collect()
    try:
        import mlx.core as mx

        clear_cache = getattr(mx, "clear_cache", None)
        if callable(clear_cache):
            clear_cache()
        metal = getattr(mx, "metal", None)
        metal_clear_cache = getattr(metal, "clear_cache", None) if metal else None
        if callable(metal_clear_cache):
            metal_clear_cache()
    except Exception:
        logger.debug("MLX cache release skipped", exc_info=True)


def _evict_mlx_path(resolved_path: str) -> None:
    if resolved_path in _mlx_cache:
        del _mlx_cache[resolved_path]
        logger.info("Evicted MLX model from cache: %s", resolved_path)
    gc.collect()
    try:
        import mlx.core as mx

        clear_cache = getattr(mx, "clear_cache", None)
        if callable(clear_cache):
            clear_cache()
        metal = getattr(mx, "metal", None)
        metal_clear_cache = getattr(metal, "clear_cache", None) if metal else None
        if callable(metal_clear_cache):
            metal_clear_cache()
    except Exception:
        logger.debug("MLX partial cache release skipped", exc_info=True)


# Backwards compat for callers/tests that still reference _release_model
def _release_model() -> None:
    _release_mlx_cache_all()


def get_model(model_path: str | None = None):
    resolved_model_path = _effective_model_path(model_path)
    if resolved_model_path not in _mlx_cache:
        from mlx_lm import load

        logger.info("Loading MLX model: %s", resolved_model_path)
        _mlx_cache[resolved_model_path] = load(resolved_model_path)
        logger.info("MLX model loaded successfully: %s", resolved_model_path)
    return _mlx_cache[resolved_model_path]


def _make_sampler_compatible(temperature: float):
    from mlx_lm.sample_utils import make_sampler

    global _sampler_param
    if _sampler_param is None:
        params = inspect.signature(make_sampler).parameters
        if "temperature" in params:
            _sampler_param = "temperature"
        elif "temp" in params:
            _sampler_param = "temp"
        else:
            _sampler_param = ""

    if _sampler_param == "temperature":
        return make_sampler(temperature=temperature)
    if _sampler_param == "temp":
        return make_sampler(temp=temperature)
    return make_sampler()


def _schema_instruction(response_format: dict[str, Any] | None) -> str:
    if not isinstance(response_format, dict):
        return ""
    if str(response_format.get("type", "") or "").strip().lower() != "json_schema":
        return ""

    schema_wrapper = dict(response_format.get("json_schema", {}) or {})
    schema = dict(schema_wrapper.get("schema", {}) or {})
    if not schema:
        return ""

    schema_name = str(schema_wrapper.get("name", "structured_output") or "structured_output").strip()
    strict = bool(schema_wrapper.get("strict", True))
    schema_text = json.dumps(schema, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
    return (
        "\n\nReturn exactly one valid JSON object."
        f"\nSchema name: {schema_name}"
        f"\nStrict mode: {'true' if strict else 'false'}"
        f"\nJSON schema: {schema_text}"
        "\nDo not add markdown fences, prose, or extra keys."
    )


def _generate_with_model(
    system_prompt: str,
    user_prompt: str,
    max_tokens: int,
    temperature: float,
    model_path: str | None = None,
    response_format: dict[str, Any] | None = None,
) -> str:
    from mlx_lm import generate

    def _run() -> str:
        model, tokenizer = get_model(model_path=model_path)
        messages = [
            {
                "role": "system",
                "content": f"{system_prompt.rstrip()}{_schema_instruction(response_format)}",
            },
            {"role": "user", "content": user_prompt},
        ]

        prompt_text = tokenizer.apply_chat_template(messages, tokenize=False, add_generation_prompt=True)
        sampler = _make_sampler_compatible(temperature)
        return generate(
            model,
            tokenizer,
            prompt=prompt_text,
            max_tokens=max_tokens,
            verbose=False,
            sampler=sampler,
        )

    if _mlx_inference_serialize_enabled():
        with _mlx_inference_lock:
            return _run()
    return _run()


def run_inference(
    system_prompt: str,
    user_prompt: str,
    max_tokens: int = 1000,
    temperature: float = 0.7,
    model_path: str | None = None,
    response_format: dict[str, Any] | None = None,
) -> str:
    global _runtime_fallback_model_path

    requested_model_path = _effective_model_path(model_path)
    try:
        return _generate_with_model(
            system_prompt,
            user_prompt,
            max_tokens=max_tokens,
            temperature=temperature,
            model_path=requested_model_path,
            response_format=response_format,
        )
    except Exception as exc:
        auto_fallback = os.getenv("MLX_AUTO_FALLBACK", "1").strip().lower() not in {"0", "false", "no"}
        fallback_model = _fallback_model_path()
        if auto_fallback and _is_oom_error(exc) and requested_model_path != fallback_model:
            logger.warning(
                "MLX OOM on %s; retrying with fallback model %s for the rest of this run.",
                requested_model_path,
                fallback_model,
            )
            _runtime_fallback_model_path = fallback_model
            _evict_mlx_path(requested_model_path)
            return _generate_with_model(
                system_prompt,
                user_prompt,
                max_tokens=max_tokens,
                temperature=temperature,
                model_path=fallback_model,
                response_format=response_format,
            )
        raise


def _normalize_json_candidate(text: str) -> str:
    cleaned = str(text or "").strip()
    cleaned = re.sub(r"^```(?:json)?\s*", "", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r"\s*```$", "", cleaned)
    cleaned = cleaned.replace("“", '"').replace("”", '"').replace("’", "'").replace("‘", "'")
    cleaned = cleaned.replace("：", ":").replace("，", ",")
    cleaned = cleaned.replace("\u00a0", " ")
    cleaned = re.sub(r"^\s*json\s*", "", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r",(\s*[}\]])", r"\1", cleaned)
    return cleaned.strip()


def _extract_balanced_json_segment(raw: str, start: int, opener: str, closer: str) -> str:
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
                return _normalize_json_candidate(raw[start : index + 1])
    return ""


def _extract_json_payload(text: str) -> str:
    raw = str(text or "").strip()
    if not raw:
        return ""

    fenced = re.search(r"```(?:json)?\s*(.*?)\s*```", raw, flags=re.IGNORECASE | re.DOTALL)
    if fenced:
        return _normalize_json_candidate(fenced.group(1))

    # Mảng ở root: nếu thử `{` trước `[`, có thể cắt nhầm chỉ phần tử đầu của [{...},...].
    scan_order: list[tuple[str, str]] = []
    if raw.lstrip().startswith("["):
        scan_order.append(("[", "]"))
    scan_order.extend((("{", "}"), ("[", "]")))

    seen: set[tuple[str, str]] = set()
    for opener, closer in scan_order:
        key = (opener, closer)
        if key in seen:
            continue
        seen.add(key)
        start = raw.find(opener)
        if start == -1:
            continue
        segment = _extract_balanced_json_segment(raw, start, opener, closer)
        if segment:
            return segment
    return _normalize_json_candidate(raw) if raw.startswith("{") or raw.startswith("[") else ""


def _parse_json_candidate(candidate: str) -> dict | list | None:
    normalized = _normalize_json_candidate(candidate)
    if not normalized:
        return None
    try:
        parsed = json.loads(normalized)
        return parsed if isinstance(parsed, (dict, list)) else None
    except json.JSONDecodeError:
        pythonish = re.sub(r"\btrue\b", "True", normalized, flags=re.IGNORECASE)
        pythonish = re.sub(r"\bfalse\b", "False", pythonish, flags=re.IGNORECASE)
        pythonish = re.sub(r"\bnull\b", "None", pythonish, flags=re.IGNORECASE)
        try:
            parsed = ast.literal_eval(pythonish)
            if isinstance(parsed, (dict, list)):
                logger.info("JSON rescue: parsed python-ish structured output from model.")
                return parsed
        except Exception:
            return None
    return None


def _looks_structured_output(text: str) -> bool:
    raw = str(text or "").strip()
    return bool(
        raw.startswith("{")
        or raw.startswith("[")
        or "```json" in raw.lower()
        or re.search(r'"[A-Za-z_][^"]*"\s*:', raw)
    )


def _validate_response_format(parsed: dict | list, response_format: dict[str, Any] | None) -> dict | list | None:
    if not isinstance(response_format, dict):
        return parsed
    if str(response_format.get("type", "") or "").strip().lower() != "json_schema":
        return parsed

    schema_wrapper = dict(response_format.get("json_schema", {}) or {})
    schema = dict(schema_wrapper.get("schema", {}) or {})
    if not schema:
        return parsed

    try:
        validate(instance=parsed, schema=schema)
        return parsed
    except ValidationError as exc:
        logger.warning("Structured output failed schema validation: %s", exc.message)
        return None


def run_json_inference(
    system_prompt: str,
    user_prompt: str,
    max_tokens: int = 1500,
    temperature: float = 0.1,
    model_path: str | None = None,
    response_format: dict[str, Any] | None = None,
) -> dict | list | None:
    parsed, _, _ = run_json_inference_meta(
        system_prompt,
        user_prompt,
        max_tokens=max_tokens,
        temperature=temperature,
        model_path=model_path,
        response_format=response_format,
    )
    return parsed


def run_json_inference_meta(
    system_prompt: str,
    user_prompt: str,
    max_tokens: int = 1500,
    temperature: float = 0.1,
    model_path: str | None = None,
    response_format: dict[str, Any] | None = None,
) -> tuple[dict | list | None, str, bool]:
    raw = run_inference(
        system_prompt,
        user_prompt,
        max_tokens=max_tokens,
        temperature=temperature,
        model_path=model_path,
        response_format=response_format,
    )

    candidate = _extract_json_payload(raw)
    parsed = _parse_json_candidate(candidate)
    if parsed is not None:
        coerced = _coerce_parsed_for_json_schema(parsed, response_format)
        validated = _validate_response_format(coerced, response_format)
        if validated is not None:
            return validated, raw, True
        # Thử parse cả chuỗi thô (khi _extract_json_payload cắt nhầm chỉ 1 object con).
        if coerced is parsed and candidate != raw.strip():
            parsed_full = _parse_json_candidate(raw.strip())
            if isinstance(parsed_full, (dict, list)) and parsed_full != parsed:
                coerced2 = _coerce_parsed_for_json_schema(parsed_full, response_format)
                validated2 = _validate_response_format(coerced2, response_format)
                if validated2 is not None:
                    return validated2, raw, True

    looks_structured = _looks_structured_output(raw)
    logger.warning("Could not parse JSON from model output (%d chars)", len(raw))
    logger.debug("Raw output: %s", raw[:500])
    return None, raw, looks_structured

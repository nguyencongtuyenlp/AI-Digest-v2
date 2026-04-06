"""
mlx_runner.py - Shared MLX model singleton and schema-first JSON inference.

The runtime API stays intentionally small:
- run_inference(): system + user prompt -> raw text
- run_json_inference(): system + user prompt -> parsed JSON

JSON mode is now schema-first:
- callers can pass response_format={"type":"json_schema","json_schema": {...}}
- prompts are tightened around the schema contract
- parsed output is validated with jsonschema when a schema is supplied
"""

from __future__ import annotations

import ast
import gc
import inspect
import json
import logging
import os
import re
from typing import Any

from jsonschema import ValidationError, validate

logger = logging.getLogger(__name__)

_model = None
_tokenizer = None
_sampler_param = None
_loaded_model_path = None
_runtime_fallback_model_path = None
_runtime_mlx_model_path: str | None = None


def _default_model_path() -> str:
    return os.getenv("MLX_MODEL", "mlx-community/Qwen2.5-32B-Instruct-4bit")


def _fallback_model_path() -> str:
    return os.getenv("MLX_FALLBACK_MODEL", "mlx-community/Qwen2.5-14B-Instruct-4bit")


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
    if _runtime_mlx_model_path:
        return _runtime_mlx_model_path
    if _runtime_fallback_model_path:
        return _runtime_fallback_model_path
    return requested_model_path or _default_model_path()


def set_runtime_mlx_model_path(model_path: str | None) -> None:
    global _runtime_mlx_model_path
    _runtime_mlx_model_path = str(model_path).strip() if model_path else None


def clear_runtime_mlx_model_path() -> None:
    global _runtime_mlx_model_path
    _runtime_mlx_model_path = None


def _release_model() -> None:
    global _model, _tokenizer, _loaded_model_path
    _model = None
    _tokenizer = None
    _loaded_model_path = None
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


def get_model(model_path: str | None = None):
    global _model, _tokenizer, _loaded_model_path
    resolved_model_path = _effective_model_path(model_path)

    if _model is None or _loaded_model_path != resolved_model_path:
        from mlx_lm import load

        if _model is not None and _loaded_model_path != resolved_model_path:
            logger.info("Switching MLX model: %s -> %s", _loaded_model_path, resolved_model_path)
            _release_model()
        logger.info("Loading MLX model: %s", resolved_model_path)
        _model, _tokenizer = load(resolved_model_path)
        _loaded_model_path = resolved_model_path
        logger.info("MLX model loaded successfully.")
    return _model, _tokenizer


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
            _release_model()
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


def _extract_json_payload(text: str) -> str:
    raw = str(text or "").strip()
    if not raw:
        return ""

    fenced = re.search(r"```(?:json)?\s*(.*?)\s*```", raw, flags=re.IGNORECASE | re.DOTALL)
    if fenced:
        return _normalize_json_candidate(fenced.group(1))

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
                    return _normalize_json_candidate(raw[start:index + 1])
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
        validated = _validate_response_format(parsed, response_format)
        if validated is not None:
            return validated, raw, True

    looks_structured = _looks_structured_output(raw)
    logger.warning("Could not parse JSON from model output (%d chars)", len(raw))
    logger.debug("Raw output: %s", raw[:500])
    return None, raw, looks_structured

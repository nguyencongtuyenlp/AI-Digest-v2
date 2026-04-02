"""
mlx_runner.py — Shared MLX model singleton (Qwen2.5-72B).

Load model một lần duy nhất khi process khởi động,
tất cả nodes dùng chung instance này.

Model mặc định: mlx-community/Qwen2.5-72B-Instruct-4bit
  - 72B params dense → chất lượng ngang GPT-4o
  - ~40GB RAM trên M4 Pro 48GB
  - ~3-5 tok/s (chậm hơn 14B nhưng reasoning mạnh hơn nhiều)

Hỗ trợ 2 chế độ inference:
  1. run_inference(): System + User prompt → raw text
  2. run_json_inference(): System + User prompt → parse JSON output
"""

from __future__ import annotations

import ast
import gc
import json
import inspect
import logging
import os
import re

logger = logging.getLogger(__name__)

# ── Singleton model cache ────────────────────────────────────────────
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
    """
    Trả về (model, tokenizer) MLX đã load.
    Load lần đầu từ HuggingFace → cache vào ~/.cache/huggingface/.
    Các lần sau dùng cache local, không cần internet.
    """
    global _model, _tokenizer, _loaded_model_path
    resolved_model_path = _effective_model_path(model_path)

    if _model is None or _loaded_model_path != resolved_model_path:
        from mlx_lm import load
        if _model is not None and _loaded_model_path != resolved_model_path:
            logger.info("Switching MLX model: %s → %s", _loaded_model_path, resolved_model_path)
            _release_model()
        logger.info("Loading MLX model: %s", resolved_model_path)
        _model, _tokenizer = load(resolved_model_path)
        _loaded_model_path = resolved_model_path
        logger.info("MLX model loaded successfully.")
    return _model, _tokenizer


def _generate_with_model(
    system_prompt: str,
    user_prompt: str,
    max_tokens: int,
    temperature: float,
    model_path: str | None = None,
) -> str:
    from mlx_lm import generate

    model, tokenizer = get_model(model_path=model_path)

    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_prompt},
    ]

    prompt_text = tokenizer.apply_chat_template(
        messages, tokenize=False, add_generation_prompt=True
    )

    sampler = _make_sampler_compatible(temperature)
    return generate(
        model,
        tokenizer,
        prompt=prompt_text,
        max_tokens=max_tokens,
        verbose=False,
        sampler=sampler,
    )


def _make_sampler_compatible(temperature: float):
    """
    Tạo sampler tương thích với nhiều version `mlx_lm`.
    Một số bản dùng `temp`, một số bản dùng `temperature`.
    """
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


def run_inference(
    system_prompt: str,
    user_prompt: str,
    max_tokens: int = 1000,
    temperature: float = 0.7,
    model_path: str | None = None,
) -> str:
    """
    Chạy inference cơ bản: system + user prompt → raw text.

    Args:
        system_prompt: Vai trò / hướng dẫn cho model
        user_prompt: Nội dung cần xử lý
        max_tokens: Số token tối đa trong response

    Returns:
        Raw string output từ model
    """
    global _runtime_fallback_model_path

    requested_model_path = _effective_model_path(model_path)
    try:
        return _generate_with_model(
            system_prompt,
            user_prompt,
            max_tokens=max_tokens,
            temperature=temperature,
            model_path=requested_model_path,
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
            )
        raise


def run_json_inference(
    system_prompt: str,
    user_prompt: str,
    max_tokens: int = 1500,
    temperature: float = 0.1,
    model_path: str | None = None,
) -> dict | list | None:
    """
    Chạy inference và parse kết quả thành JSON.
    Model được hướng dẫn trả về JSON, hàm này trích xuất + parse.

    Nếu model trả về text có chứa JSON block (```json ... ```),
    hàm sẽ tự extract.

    Args:
        system_prompt: Vai trò / hướng dẫn (nên yêu cầu trả JSON)
        user_prompt: Nội dung cần xử lý
        max_tokens: Số token tối đa

    Returns:
        dict/list nếu parse thành công, None nếu thất bại
    """
    parsed, _, _ = run_json_inference_meta(
        system_prompt,
        user_prompt,
        max_tokens=max_tokens,
        temperature=temperature,
        model_path=model_path,
    )
    return parsed


def run_json_inference_meta(
    system_prompt: str,
    user_prompt: str,
    max_tokens: int = 1500,
    temperature: float = 0.1,
    model_path: str | None = None,
) -> tuple[dict | list | None, str, bool]:
    """
    Chạy inference dạng JSON nhưng trả thêm raw output và tín hiệu xem
    model có cố gắng trả structured output hay không.

    Returns:
        (parsed_json_or_none, raw_output, looks_structured)
    """
    raw = run_inference(
        system_prompt,
        user_prompt,
        max_tokens=max_tokens,
        temperature=temperature,
        model_path=model_path,
    )

    def _normalize_jsonish(text: str) -> str:
        cleaned = str(text or "").strip()
        cleaned = cleaned.replace("“", '"').replace("”", '"').replace("’", "'").replace("‘", "'")
        cleaned = cleaned.replace("：", ":").replace("，", ",")
        cleaned = cleaned.replace("\u00a0", " ")
        cleaned = re.sub(r"^\s*json\s*", "", cleaned, flags=re.IGNORECASE)
        cleaned = re.sub(r"//.*?$", "", cleaned, flags=re.MULTILINE)
        cleaned = re.sub(r"#.*?$", "", cleaned, flags=re.MULTILINE)
        cleaned = re.sub(r",(\s*[}\]])", r"\1", cleaned)
        return cleaned

    def _extract_balanced_json_blocks(text: str) -> list[str]:
        blocks: list[str] = []
        stack: list[str] = []
        start_index: int | None = None
        in_string = False
        string_quote = ""
        escaped = False

        for index, char in enumerate(text):
            if in_string:
                if escaped:
                    escaped = False
                elif char == "\\":
                    escaped = True
                elif char == string_quote:
                    in_string = False
                continue

            if char in {'"', "'"}:
                in_string = True
                string_quote = char
                continue

            if char in "{[":
                if not stack:
                    start_index = index
                stack.append("}" if char == "{" else "]")
                continue

            if char in "}]":
                if not stack or char != stack[-1]:
                    continue
                stack.pop()
                if not stack and start_index is not None:
                    blocks.append(_normalize_jsonish(text[start_index:index + 1]))
                    start_index = None

        return blocks

    def _quote_unquoted_keys(text: str) -> str:
        return re.sub(
            r'([{\[,]\s*)([A-Za-z_][A-Za-z0-9_\- ]*)(\s*:)',
            lambda match: f'{match.group(1)}"{match.group(2).strip()}"{match.group(3)}',
            text,
        )

    def _json_rescue_variants(text: str) -> list[str]:
        normalized = _normalize_jsonish(text)
        variants = [normalized]
        quoted_keys = _quote_unquoted_keys(normalized)
        if quoted_keys != normalized:
            variants.append(quoted_keys)
        if "'" in normalized:
            variants.append(quoted_keys.replace("'", '"'))
        deduped: list[str] = []
        seen: set[str] = set()
        for item in variants:
            if item and item not in seen:
                seen.add(item)
                deduped.append(item)
        return deduped

    def _extract_json_candidates(text: str) -> list[str]:
        cleaned = _normalize_jsonish(text)
        candidates: list[str] = [cleaned]

        json_match = re.search(r"```(?:json)?\s*\n?(.*?)\n?```", cleaned, re.DOTALL)
        if json_match:
            candidates.append(_normalize_jsonish(json_match.group(1)))

        for pattern in [r"\{.*\}", r"\[.*\]"]:
            match = re.search(pattern, cleaned, re.DOTALL)
            if match:
                candidates.append(_normalize_jsonish(match.group(0)))

        candidates.extend(_extract_balanced_json_blocks(cleaned))

        seen: set[str] = set()
        unique_candidates: list[str] = []
        for item in candidates:
            if item and item not in seen:
                seen.add(item)
                unique_candidates.append(item)
        return unique_candidates

    def _parse_pythonish_json(text: str):
        pythonish = re.sub(r"\btrue\b", "True", text, flags=re.IGNORECASE)
        pythonish = re.sub(r"\bfalse\b", "False", pythonish, flags=re.IGNORECASE)
        pythonish = re.sub(r"\bnull\b", "None", pythonish, flags=re.IGNORECASE)
        try:
            parsed = ast.literal_eval(pythonish)
            if isinstance(parsed, (dict, list)):
                return parsed
        except Exception:
            return None
        return None

    def _parse_line_based_object(text: str) -> dict | None:
        expected_anchor_keys = {
            "primary_type",
            "analysis_tier",
            "decision",
            "groundedness_score",
            "freshness_score",
            "operator_value_score",
            "c1_score",
            "c2_score",
            "c3_score",
            "summary_vi",
            "editorial_angle",
            "rationale",
            "tags",
            "relevance_level",
        }
        parsed: dict[str, object] = {}

        def normalize_key_name(key: str) -> str:
            key = re.sub(r"[^A-Za-z0-9_\- ]+", "", str(key or "").strip())
            key = key.replace("-", "_")
            key = re.sub(r"\s+", "_", key)
            return key.lower().strip("_")

        def normalize_scalar(value: str):
            cleaned = str(value or "").strip().rstrip(",;")
            cleaned = cleaned.strip("`")
            if not cleaned:
                return ""
            if cleaned[0] == cleaned[-1] and cleaned[0] in {"'", '"'}:
                return cleaned[1:-1].strip()
            if re.fullmatch(r"-?\d+", cleaned):
                try:
                    return int(cleaned)
                except ValueError:
                    return cleaned
            if re.fullmatch(r"-?\d+\.\d+", cleaned):
                try:
                    return float(cleaned)
                except ValueError:
                    return cleaned
            lowered = cleaned.lower()
            if lowered == "true":
                return True
            if lowered == "false":
                return False
            if lowered in {"null", "none"}:
                return None
            return cleaned

        current_key = ""
        for raw_line in str(text or "").splitlines():
            line = raw_line.strip()
            if not line:
                continue
            match = re.match(r'^(?:[-*]\s*)?["\']?([A-Za-z_][A-Za-z0-9_\- ]*)["\']?\s*:\s*(.+?)\s*$', line)
            if match:
                key = normalize_key_name(match.group(1))
                value = match.group(2).strip()
                current_key = key
                if value.startswith("[") and value.endswith("]"):
                    rescued = None
                    for variant in _json_rescue_variants(value):
                        try:
                            rescued = json.loads(variant)
                            break
                        except json.JSONDecodeError:
                            rescued = _parse_pythonish_json(variant)
                            if rescued is not None:
                                break
                    if isinstance(rescued, list):
                        parsed[key] = rescued
                    else:
                        parsed[key] = [item.strip() for item in value.strip("[]").split(",") if item.strip()]
                else:
                    parsed[key] = normalize_scalar(value)
                continue

            if current_key and isinstance(parsed.get(current_key), str):
                parsed[current_key] = f"{parsed[current_key]} {line}".strip()

        if len(parsed) < 2:
            return None
        if not (set(parsed.keys()) & expected_anchor_keys):
            return None
        return parsed

    def _looks_structured_output(text: str) -> bool:
        cleaned = _normalize_jsonish(text)
        if not cleaned:
            return False
        if re.search(r"```(?:json)?", cleaned, re.IGNORECASE):
            return True
        if cleaned.startswith("{") or cleaned.startswith("["):
            return True
        if _extract_balanced_json_blocks(cleaned):
            return True
        if re.search(r'"[A-Za-z_][^"]*"\s*:', cleaned):
            return True
        if re.search(r"(^|\n)\s*[A-Za-z_][A-Za-z0-9_\- ]+\s*:\s*", cleaned):
            return True
        return False

    for candidate in _extract_json_candidates(raw):
        for variant in _json_rescue_variants(candidate):
            try:
                return json.loads(variant), raw, True
            except json.JSONDecodeError:
                parsed = _parse_pythonish_json(variant)
                if parsed is not None:
                    logger.info("🛟 JSON rescue: parsed python-ish structured output from model.")
                    return parsed, raw, True

    line_based = _parse_line_based_object(raw)
    if isinstance(line_based, dict):
        logger.info("🛟 JSON rescue: parsed line-based structured output from model.")
        return line_based, raw, True

    looks_structured = _looks_structured_output(raw)
    logger.warning("Không thể parse JSON từ model output (%d chars)", len(raw))
    logger.debug("Raw output: %s", raw[:500])
    return None, raw, looks_structured

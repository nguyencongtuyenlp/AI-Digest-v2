#!/usr/bin/env python3
"""
eval_digest.py - Lightweight regression harness for daily digest triage.

This script does not try to emulate the full online pipeline.
It focuses on deterministic editorial layers so the team can catch regressions
in type routing, fallback triage, and delivery decisions quickly.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from digest.editorial.editorial_guardrails import build_article_grounding
from digest.workflow.nodes.classify_and_score import (
    _apply_strategic_boost,
    _llm_failure_fallback,
    _normalize_primary_type,
    _prefilter_score,
)
from digest.workflow.nodes.delivery_judge import _deterministic_delivery_assessment
from digest.workflow.nodes.normalize_source import normalize_source_node

DEFAULT_CASES_PATH = PROJECT_ROOT / "config" / "prompt_tuning_cases.jsonl"
DEFAULT_REPORTS_DIR = PROJECT_ROOT / "reports"


def _load_cases(path: Path) -> list[dict[str, Any]]:
    cases: list[dict[str, Any]] = []
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line:
            continue
        cases.append(json.loads(line))
    return cases


def _build_case_article(case: dict[str, Any]) -> dict[str, Any]:
    return {
        "title": str(case.get("title", "") or ""),
        "source": str(case.get("source", "Eval Case") or "Eval Case"),
        "url": str(case.get("url", "") or ""),
        "snippet": str(case.get("snippet", case.get("notes", "")) or ""),
        "content": str(case.get("content", case.get("snippet", "")) or ""),
        "published": str(case.get("published", "") or ""),
        "fetched_at": datetime.now(timezone.utc).isoformat(),
    }


def _evaluate_case(case: dict[str, Any], min_score: int) -> dict[str, Any]:
    normalized = normalize_source_node({"raw_articles": [_build_case_article(case)]}).get("raw_articles", [{}])[0]
    prefilter_score, reasons = _prefilter_score(normalized)
    normalized["prefilter_score"] = prefilter_score
    normalized["prefilter_reasons"] = reasons
    _llm_failure_fallback(normalized, min_score)
    _apply_strategic_boost(normalized, min_score)
    _normalize_primary_type(normalized)
    normalized.update(build_article_grounding(normalized))
    delivery = _deterministic_delivery_assessment(normalized)

    expected_type = str(case.get("expected_primary_type", "") or "").strip()
    expected_tier = str(case.get("expected_analysis_tier", "") or "").strip()
    expected_delivery = str(case.get("expected_delivery_decision", "") or "").strip()
    expected_source_kind = str(case.get("expected_source_kind", "") or "").strip()

    checks = []
    if expected_type:
        checks.append(("primary_type", expected_type, str(normalized.get("primary_type", ""))))
    if expected_tier:
        checks.append(("analysis_tier", expected_tier, str(normalized.get("analysis_tier", ""))))
    if expected_delivery:
        checks.append(("delivery_decision", expected_delivery, str(delivery.get("decision", ""))))
    if expected_source_kind:
        checks.append(("source_kind", expected_source_kind, str(normalized.get("source_kind", ""))))

    failures = [
        {
            "field": field,
            "expected": expected,
            "actual": actual,
        }
        for field, expected, actual in checks
        if expected != actual
    ]

    return {
        "case_id": str(case.get("case_id", "") or ""),
        "title": str(case.get("title", "") or ""),
        "predicted_primary_type": str(normalized.get("primary_type", "")),
        "predicted_analysis_tier": str(normalized.get("analysis_tier", "")),
        "predicted_delivery_decision": str(delivery.get("decision", "")),
        "predicted_source_kind": str(normalized.get("source_kind", "")),
        "total_score": int(normalized.get("total_score", 0) or 0),
        "prefilter_score": int(prefilter_score or 0),
        "failures": failures,
        "passed": not failures,
        "notes": str(case.get("notes", "") or ""),
    }


def _build_report(results: list[dict[str, Any]], cases_path: Path, min_score: int) -> str:
    passed = sum(1 for result in results if result["passed"])
    failed = len(results) - passed
    pass_rate = (passed / len(results) * 100) if results else 0.0

    lines = [
        "# Daily Digest Eval Report",
        "",
        f"- Generated at: {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC')}",
        f"- Cases file: {cases_path}",
        f"- Min deep analysis score: {min_score}",
        f"- Total cases: {len(results)}",
        f"- Passed: {passed}",
        f"- Failed: {failed}",
        f"- Pass rate: {pass_rate:.1f}%",
        "",
    ]

    lines.extend(["## Failed Cases", ""])
    failed_results = [result for result in results if not result["passed"]]
    if failed_results:
        for result in failed_results:
            lines.append(f"- {result['case_id']} | {result['title']}")
            for failure in result["failures"]:
                lines.append(
                    f"  - {failure['field']}: expected={failure['expected']} actual={failure['actual']}"
                )
    else:
        lines.append("- Không có case fail.")
    lines.append("")

    lines.extend(["## All Cases", ""])
    for result in results:
        lines.append(
            "- "
            f"{result['case_id']} | "
            f"type={result['predicted_primary_type']} | "
            f"tier={result['predicted_analysis_tier']} | "
            f"source={result['predicted_source_kind']} | "
            f"delivery={result['predicted_delivery_decision']} | "
            f"score={result['total_score']} | "
            f"{'PASS' if result['passed'] else 'FAIL'}"
        )
    lines.append("")
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description="Run deterministic regression checks for digest triage.")
    parser.add_argument("--cases", default=str(DEFAULT_CASES_PATH), help="Path to JSONL eval cases.")
    parser.add_argument("--min-score", type=int, default=60, help="Min score used by fallback triage.")
    parser.add_argument("--min-pass-rate", type=float, default=80.0, help="Return non-zero if pass rate drops below this.")
    parser.add_argument("--write-report", action="store_true", help="Write markdown report into reports/.")
    args = parser.parse_args()

    cases_path = Path(args.cases).resolve()
    cases = _load_cases(cases_path)
    results = [_evaluate_case(case, args.min_score) for case in cases]
    passed = sum(1 for result in results if result["passed"])
    pass_rate = (passed / len(results) * 100) if results else 0.0

    report_text = _build_report(results, cases_path, args.min_score)
    print(report_text)

    if args.write_report:
        DEFAULT_REPORTS_DIR.mkdir(parents=True, exist_ok=True)
        report_path = DEFAULT_REPORTS_DIR / f"eval_digest_{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')}.md"
        report_path.write_text(report_text, encoding="utf-8")
        print(f"report_path={report_path}")

    return 0 if pass_rate >= args.min_pass_rate else 1


if __name__ == "__main__":
    raise SystemExit(main())

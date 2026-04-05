"""
artifact_retention.py - Archive runtime artifacts so the repo stays readable.

Goals:
- keep a small recent working set in the repo for fast debugging
- move older reports/snapshots/checkpoints into a hidden local archive
- avoid deleting history blindly while still stopping artifact sprawl
"""

from __future__ import annotations

import logging
import os
import shutil
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parent
DEFAULT_ARTIFACT_ARCHIVE_DIR = ".runtime_archive"
DEFAULT_TEMPORAL_SNAPSHOT_DIR = "reports/temporal_snapshots"
FALSE_VALUES = {"0", "false", "no", "off"}
DEBUG_LOG_MAX_AGE_HOURS = 12.0

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class RetentionRule:
    label: str
    relative_dir: str
    pattern: str
    keep_latest: int
    max_age_days: float


BASE_RETENTION_RULES: tuple[RetentionRule, ...] = (
    RetentionRule(
        label="daily_reports",
        relative_dir="reports",
        pattern="daily_digest_run_*.md",
        keep_latest=12,
        max_age_days=7.0,
    ),
    RetentionRule(
        label="eval_reports",
        relative_dir="reports",
        pattern="eval_digest_*.md",
        keep_latest=6,
        max_age_days=14.0,
    ),
    RetentionRule(
        label="github_briefs",
        relative_dir="reports",
        pattern="github_agent_brief_*.md",
        keep_latest=6,
        max_age_days=14.0,
    ),
    RetentionRule(
        label="checkpoints",
        relative_dir=".checkpoints",
        pattern="*.tar.gz",
        keep_latest=2,
        max_age_days=21.0,
    ),
)


def artifact_cleanup_enabled(state: dict[str, Any] | None = None) -> bool:
    runtime_config = dict((state or {}).get("runtime_config", {}) or {})
    raw = runtime_config.get("enable_artifact_cleanup")
    if raw in (None, ""):
        raw = os.getenv("DIGEST_ARTIFACT_CLEANUP_ENABLED", "1")
    return str(raw).strip().lower() not in FALSE_VALUES


def _snapshot_dir(state: dict[str, Any] | None = None, *, project_root: Path) -> Path:
    runtime_config = dict((state or {}).get("runtime_config", {}) or {})
    configured = str(runtime_config.get("temporal_snapshot_dir", "") or os.getenv("TEMPORAL_SNAPSHOT_DIR", "")).strip()
    if configured:
        path = Path(configured).expanduser()
        return path.resolve() if path.is_absolute() else (project_root / path).resolve()
    return (project_root / DEFAULT_TEMPORAL_SNAPSHOT_DIR).resolve()


def _archive_root(state: dict[str, Any] | None = None, *, project_root: Path) -> Path:
    runtime_config = dict((state or {}).get("runtime_config", {}) or {})
    configured = str(runtime_config.get("artifact_archive_dir", "") or os.getenv("DIGEST_ARTIFACT_ARCHIVE_DIR", "")).strip()
    if configured:
        path = Path(configured).expanduser()
        return path.resolve() if path.is_absolute() else (project_root / path).resolve()
    return (project_root / DEFAULT_ARTIFACT_ARCHIVE_DIR).resolve()


def _snapshot_rule(state: dict[str, Any] | None = None, *, project_root: Path) -> RetentionRule | None:
    snapshot_dir = _snapshot_dir(state, project_root=project_root)
    try:
        relative_dir = snapshot_dir.relative_to(project_root).as_posix()
    except ValueError:
        return None
    return RetentionRule(
        label="temporal_snapshots",
        relative_dir=relative_dir,
        pattern="*.json",
        keep_latest=8,
        max_age_days=3.0,
    )


def _relative_archive_path(path: Path, *, project_root: Path) -> Path:
    try:
        return path.resolve().relative_to(project_root.resolve())
    except ValueError:
        sanitized = "_".join(part for part in path.resolve().parts if part not in {"/", ""})
        return Path("external") / sanitized


def _age_days(path: Path, *, now_ts: float) -> float:
    return max(0.0, (now_ts - path.stat().st_mtime) / 86400.0)


def _sorted_files(directory: Path, pattern: str) -> list[Path]:
    if not directory.exists():
        return []
    return sorted(
        [path for path in directory.glob(pattern) if path.is_file()],
        key=lambda path: (path.stat().st_mtime, path.name),
        reverse=True,
    )


def _move_to_archive(path: Path, *, archive_root: Path, project_root: Path, dry_run: bool = False) -> str:
    relative = _relative_archive_path(path, project_root=project_root)
    destination = archive_root / relative
    if destination.exists():
        stamp = datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc).strftime("%Y%m%d_%H%M%S")
        destination = destination.with_name(f"{destination.stem}_{stamp}{destination.suffix}")
    if not dry_run:
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(path), str(destination))
    return str(destination)


def _apply_rule(
    rule: RetentionRule,
    *,
    project_root: Path,
    archive_root: Path,
    preserve_paths: set[Path],
    dry_run: bool,
) -> tuple[dict[str, Any], list[str]]:
    source_dir = (project_root / rule.relative_dir).resolve()
    files = _sorted_files(source_dir, rule.pattern)
    now_ts = datetime.now(timezone.utc).timestamp()
    archived_files: list[str] = []
    result = {
        "label": rule.label,
        "relative_dir": rule.relative_dir,
        "pattern": rule.pattern,
        "scanned": len(files),
        "kept": 0,
        "archived": 0,
    }

    for index, path in enumerate(files):
        resolved = path.resolve()
        if resolved in preserve_paths:
            result["kept"] += 1
            continue
        age_days = _age_days(path, now_ts=now_ts)
        should_keep = index < rule.keep_latest and age_days <= rule.max_age_days
        if should_keep:
            result["kept"] += 1
            continue
        archived_path = _move_to_archive(
            path,
            archive_root=archive_root,
            project_root=project_root,
            dry_run=dry_run,
        )
        archived_files.append(archived_path)
        result["archived"] += 1

    return result, archived_files


def _archive_debug_logs(
    *,
    project_root: Path,
    archive_root: Path,
    preserve_paths: set[Path],
    dry_run: bool,
) -> tuple[dict[str, Any], list[str]]:
    candidates = [
        project_root / "debug_output.txt",
        project_root / "digest.log",
        project_root / "digest_error.log",
    ]
    now_ts = datetime.now(timezone.utc).timestamp()
    archived_files: list[str] = []
    result = {
        "label": "debug_logs",
        "relative_dir": ".",
        "pattern": "debug_output.txt|digest.log|digest_error.log",
        "scanned": 0,
        "kept": 0,
        "archived": 0,
    }

    for path in candidates:
        if not path.exists() or not path.is_file():
            continue
        result["scanned"] += 1
        resolved = path.resolve()
        if resolved in preserve_paths:
            result["kept"] += 1
            continue
        age_hours = max(0.0, (now_ts - path.stat().st_mtime) / 3600.0)
        if age_hours <= DEBUG_LOG_MAX_AGE_HOURS:
            result["kept"] += 1
            continue
        stamp = datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc).strftime("%Y%m%d_%H%M%S")
        destination = archive_root / "logs" / f"{path.stem}_{stamp}{path.suffix}"
        if destination.exists():
            destination = destination.with_name(f"{destination.stem}_1{destination.suffix}")
        if not dry_run:
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(path), str(destination))
        archived_files.append(str(destination))
        result["archived"] += 1

    return result, archived_files


def cleanup_runtime_artifacts(
    *,
    state: dict[str, Any] | None = None,
    project_root: str | Path | None = None,
    preserve_paths: list[str | Path] | None = None,
    dry_run: bool = False,
) -> dict[str, Any]:
    root = Path(project_root or PROJECT_ROOT).resolve()
    enabled = artifact_cleanup_enabled(state)
    archive_root = _archive_root(state, project_root=root)
    preserved = {
        (Path(path).resolve() if Path(path).is_absolute() else (root / Path(path)).resolve())
        for path in list(preserve_paths or [])
        if str(path or "").strip()
    }
    summary: dict[str, Any] = {
        "enabled": enabled,
        "archive_root": str(archive_root),
        "archived_count": 0,
        "kept_count": 0,
        "rules": [],
        "archived_files": [],
        "dry_run": bool(dry_run),
    }
    if not enabled:
        return summary

    rules = list(BASE_RETENTION_RULES)
    snapshot_rule = _snapshot_rule(state, project_root=root)
    if snapshot_rule is not None:
        rules.append(snapshot_rule)

    for rule in rules:
        rule_result, archived_files = _apply_rule(
            rule,
            project_root=root,
            archive_root=archive_root,
            preserve_paths=preserved,
            dry_run=dry_run,
        )
        summary["rules"].append(rule_result)
        summary["archived_files"].extend(archived_files)
        summary["archived_count"] += int(rule_result["archived"])
        summary["kept_count"] += int(rule_result["kept"])

    debug_result, debug_archived = _archive_debug_logs(
        project_root=root,
        archive_root=archive_root,
        preserve_paths=preserved,
        dry_run=dry_run,
    )
    summary["rules"].append(debug_result)
    summary["archived_files"].extend(debug_archived)
    summary["archived_count"] += int(debug_result["archived"])
    summary["kept_count"] += int(debug_result["kept"])

    if summary["archived_count"] > 0:
        logger.info(
            "🧹 Runtime artifact cleanup archived %d files into %s",
            summary["archived_count"],
            archive_root,
        )
    return summary


def build_artifact_cleanup_markdown(summary: dict[str, Any] | None) -> list[str]:
    payload = dict(summary or {})
    if not payload or not payload.get("enabled"):
        return []

    lines = [
        "## Artifact Cleanup",
        "",
        f"- Archive dir: {payload.get('archive_root', '') or '(not set)'}",
        f"- Archived this run: {int(payload.get('archived_count', 0) or 0)}",
        f"- Kept in workspace: {int(payload.get('kept_count', 0) or 0)}",
        "",
    ]

    visible_rules = [rule for rule in payload.get("rules", []) if int(rule.get("scanned", 0) or 0) > 0]
    if visible_rules:
        lines.append("### Retention results")
        lines.append("")
        for rule in visible_rules:
            lines.append(
                "- "
                f"{rule.get('label', 'unknown')}: "
                f"scanned={int(rule.get('scanned', 0) or 0)} "
                f"kept={int(rule.get('kept', 0) or 0)} "
                f"archived={int(rule.get('archived', 0) or 0)}"
            )
        lines.append("")

    archived_files = [Path(path).name for path in payload.get("archived_files", [])[:6] if str(path or "").strip()]
    if archived_files:
        lines.append(f"- Sample archived: {', '.join(archived_files)}")
        lines.append("")

    return lines

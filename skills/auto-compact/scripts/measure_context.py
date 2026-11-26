#!/usr/bin/env python3
"""Measure active context pressure across transcripts, always-on rules, and hot ledgers.

Pure Python >= 3.10 standard library implementation with UTF-8 output.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

BYTES_PER_TOKEN = 3.8
POINTER_MARKER = "<!-- AUTO-COMPACT-ARCHIVE-POINTER -->"
POINTER_END_MARKER = "<!-- /AUTO-COMPACT-ARCHIVE-POINTER -->"
POINTER_HEADING_PREFIX = "## 🗄 Kho lưu trữ lịch sử nén"

MERGED_STUB_PATTERNS = (
    "đã hợp nhất đầy đủ vào",
    "merged into",
    "consolidated into",
)

STOPWORDS = {
    "that", "this", "with", "from", "have", "will", "your", "when", "into",
    "only", "must", "each", "more", "than", "then", "also", "used", "using",
    "trong", "khong", "không", "được", "duoc", "nhưng", "nhung", "hoặc", "hoac",
    "phải", "phai", "trên", "tren", "dưới", "duoi", "theo", "từng", "tung",
    "toàn", "toan", "cũng", "cung", "đang", "dang", "này", "nay", "cho",
}

TOKEN_RE = re.compile(r"[a-zA-ZÀ-ỹ0-9_]{4,}", re.UNICODE)
HEADING_RE = re.compile(r"^(#{2,3})\s+(.+?)\s*$")
FENCE_OPEN_RE = re.compile(r"^\s*(?:(`{3,})[^`]*|(~{3,}).*)$")
FENCE_CLOSE_RE = re.compile(r"^\s*(`{3,}|~{3,})\s*$")


def _reconfigure_stdio() -> None:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            try:
                stream.reconfigure(encoding="utf-8", errors="replace")
            except OSError:
                pass


def atomic_write_text(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(
        prefix=f".{path.name}.",
        suffix=".tmp",
        dir=str(path.parent),
    )
    tmp_path = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_path, path)
    except Exception:
        if tmp_path.exists():
            try:
                tmp_path.unlink()
            except OSError:
                pass
        raise


def _estimate_tokens(byte_count: int) -> int:
    return round(byte_count / BYTES_PER_TOKEN) if byte_count > 0 else 0


def _contains_key(obj: Any, target_key: str) -> bool:
    if isinstance(obj, dict):
        if target_key in obj:
            return True
        return any(_contains_key(v, target_key) for v in obj.values())
    if isinstance(obj, list):
        return any(_contains_key(item, target_key) for item in obj)
    return False


def _count_subagent_calls(obj: Any) -> int:
    """Count invoke_subagent calls structurally (no raw substring scans on text/tool output)."""
    if not isinstance(obj, dict):
        return 0

    step_type = str(obj.get("type", "")).upper()
    role = str(obj.get("role", "")).lower()

    if step_type in ("USER_INPUT", "GENERIC", "TOOL_RESULT") or role in ("user", "system", "tool"):
        return 0

    # Branch 1: Antigravity transcript.jsonl (e.g. type == "PLANNER_RESPONSE") tool_calls array
    tool_calls = obj.get("tool_calls")
    if isinstance(tool_calls, list):
        return sum(
            1
            for tc in tool_calls
            if isinstance(tc, dict) and tc.get("name") == "invoke_subagent"
        )

    # Branch 2: Claude / generic assistant tool_use blocks or flat tool/tool_name field
    content = obj.get("content")
    if isinstance(content, list):
        count = sum(
            1
            for block in content
            if isinstance(block, dict)
            and block.get("type") in ("tool_use", "tool_call")
            and block.get("name") == "invoke_subagent"
        )
        if count > 0:
            return count

    if obj.get("tool") == "invoke_subagent" or obj.get("tool_name") == "invoke_subagent":
        return 1

    return 0


def _parse_iso_timestamp(value: Any) -> float | None:
    if isinstance(value, (int, float)):
        return float(value)
    if not isinstance(value, str) or not value.strip():
        return None
    raw = value.strip()
    if raw.endswith("Z"):
        raw = raw[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(raw)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.timestamp()
    except ValueError:
        return None


def _extract_step_timestamp(obj: Any) -> float | None:
    if not isinstance(obj, dict):
        return None
    for key in ("created_at", "timestamp", "createdAt"):
        if key in obj:
            parsed = _parse_iso_timestamp(obj.get(key))
            if parsed is not None:
                return parsed
    return None


def _update_fence_state(
    line: str,
    in_fence: bool,
    fence_char: str,
    fence_len: int,
) -> tuple[bool, str, int]:
    """Update CommonMark fenced code block state for a single line."""
    if not in_fence:
        m_open = FENCE_OPEN_RE.match(line)
        if m_open:
            marker = m_open.group(1) or m_open.group(2)
            return True, marker[0], len(marker)
        return False, "", 0

    m_close = FENCE_CLOSE_RE.match(line)
    if m_close:
        marker = m_close.group(1)
        if marker[0] == fence_char and len(marker) >= fence_len:
            return False, "", 0
    return True, fence_char, fence_len


def discover_transcript(brain_dir: Path | None = None) -> dict[str, Path | None]:
    """Locate transcript.jsonl and sibling transcript_full.jsonl."""
    if brain_dir is not None:
        candidate = brain_dir.expanduser()
        if candidate.is_file():
            transcript_path = candidate
            if candidate.name == "transcript.jsonl":
                full_path = candidate.parent / "transcript_full.jsonl"
            elif candidate.name == "transcript_full.jsonl":
                sibling = candidate.parent / "transcript.jsonl"
                transcript_path = sibling if sibling.is_file() else candidate
                full_path = candidate
            else:
                full_path = candidate.parent / "transcript_full.jsonl"
            if (
                transcript_path.parent.name == "logs"
                and transcript_path.parent.parent.name == ".system_generated"
            ):
                conv_dir = transcript_path.parent.parent.parent
            else:
                conv_dir = transcript_path.parent
            return {
                "conversation_dir": conv_dir,
                "transcript_path": transcript_path,
                "transcript_full_path": full_path if full_path.is_file() else None,
            }

        if candidate.is_dir():
            direct_log = candidate / ".system_generated" / "logs" / "transcript.jsonl"
            if direct_log.is_file():
                full_log = direct_log.parent / "transcript_full.jsonl"
                return {
                    "conversation_dir": candidate,
                    "transcript_path": direct_log,
                    "transcript_full_path": full_log if full_log.is_file() else None,
                }
            flat_log = candidate / "transcript.jsonl"
            if flat_log.is_file():
                full_log = candidate / "transcript_full.jsonl"
                return {
                    "conversation_dir": candidate,
                    "transcript_path": flat_log,
                    "transcript_full_path": full_log if full_log.is_file() else None,
                }
            roots = [candidate]
        else:
            return {
                "conversation_dir": None,
                "transcript_path": None,
                "transcript_full_path": None,
            }
    else:
        roots = [
            Path.home() / ".gemini" / "antigravity" / "brain",
            Path.home() / ".gemini" / "antigravity-cli" / "brain",
        ]

    newest_transcript: Path | None = None
    newest_mtime = -1.0
    newest_conv: Path | None = None

    for root in roots:
        if not root.is_dir():
            continue
        try:
            entries = list(root.iterdir())
        except OSError:
            continue
        for child in entries:
            if not child.is_dir():
                continue
            log_file = child / ".system_generated" / "logs" / "transcript.jsonl"
            if not log_file.is_file():
                continue
            try:
                mtime = log_file.stat().st_mtime
            except OSError:
                continue
            if mtime > newest_mtime:
                newest_mtime = mtime
                newest_transcript = log_file
                newest_conv = child

    if newest_transcript is None:
        return {
            "conversation_dir": None,
            "transcript_path": None,
            "transcript_full_path": None,
        }

    full_path = newest_transcript.parent / "transcript_full.jsonl"
    return {
        "conversation_dir": newest_conv,
        "transcript_path": newest_transcript,
        "transcript_full_path": full_path if full_path.is_file() else None,
    }


def _summary_matches_project(summary_path: Path, resolved_project_norm: str) -> bool:
    """Return False if summary.json explicitly declares a different project_root."""
    if not summary_path.is_file():
        return True
    try:
        data = json.loads(summary_path.read_text(encoding="utf-8", errors="replace"))
    except (OSError, json.JSONDecodeError):
        return True
    if not isinstance(data, dict):
        return True
    raw_root = data.get("project_root")
    if isinstance(raw_root, str) and raw_root.strip():
        try:
            candidate_norm = os.path.normcase(str(Path(raw_root).expanduser().resolve()))
        except OSError:
            return False
        return candidate_norm == resolved_project_norm
    return True


def _count_orchestrator_runs(
    project_dir: Path,
    gemini_config_dir: Path,
    session_start_ts: float | None = None,
) -> tuple[int, int]:
    """Count (session_orchestrator_runs, lifetime_orchestrator_runs) for this project."""
    runtime_root = gemini_config_dir.expanduser().resolve().parent / "runtime" / "universal-orchestrator"
    if not runtime_root.is_dir():
        return 0, 0
    resolved_project = project_dir.expanduser().resolve()
    resolved_project_norm = os.path.normcase(str(resolved_project))
    proj_name = resolved_project.name.lower()

    session_count = 0
    lifetime_count = 0
    try:
        entries = list(runtime_root.iterdir())
    except OSError:
        return 0, 0

    for child in entries:
        if not child.is_dir():
            continue
        cname = child.name.lower()
        if cname != proj_name and not cname.startswith(f"{proj_name}_"):
            continue
        runs_dir = child / "runs"
        if runs_dir.is_dir():
            try:
                for run_item in runs_dir.iterdir():
                    if run_item.is_dir():
                        summary_file = run_item / "summary.json"
                    elif run_item.suffix == ".json":
                        summary_file = run_item
                    else:
                        continue
                    if not _summary_matches_project(summary_file, resolved_project_norm):
                        continue
                    try:
                        mtime = (
                            summary_file.stat().st_mtime
                            if summary_file.is_file()
                            else run_item.stat().st_mtime
                        )
                    except OSError:
                        continue
                    lifetime_count += 1
                    if session_start_ts is not None and mtime >= session_start_ts - 60.0:
                        session_count += 1
            except OSError:
                continue
        else:
            try:
                for marker in child.rglob("summary.json"):
                    if not marker.is_file():
                        continue
                    if not _summary_matches_project(marker, resolved_project_norm):
                        continue
                    try:
                        mtime = marker.stat().st_mtime
                    except OSError:
                        continue
                    lifetime_count += 1
                    if session_start_ts is not None and mtime >= session_start_ts - 60.0:
                        session_count += 1
            except OSError:
                continue
    return session_count, lifetime_count


def measure_transcript(
    project_dir: Path,
    brain_dir: Path | None,
    gemini_config_dir: Path,
) -> dict[str, Any]:
    discovered = discover_transcript(brain_dir)
    transcript_path = discovered["transcript_path"]
    transcript_full_path = discovered["transcript_full_path"]
    conv_dir = discovered["conversation_dir"]

    if transcript_path is None or not transcript_path.is_file():
        session_orch, lifetime_orch = _count_orchestrator_runs(
            project_dir, gemini_config_dir, session_start_ts=None
        )
        return {
            "found": False,
            "conversation_dir": None,
            "transcript_path": None,
            "transcript_full_path": None,
            "session_start_ts": None,
            "transcript_bytes": 0,
            "transcript_mb": 0.0,
            "transcript_full_bytes": 0,
            "transcript_full_mb": 0.0,
            "step_count": 0,
            "invalid_lines": 0,
            "truncated_steps": 0,
            "transcript_subagent_dispatches": 0,
            "session_orchestrator_runs": session_orch,
            "lifetime_orchestrator_runs": lifetime_orch,
            "orchestrator_runs": session_orch,
            "subagent_dispatches": session_orch,
            "bytes_per_step": 0.0,
            "estimated_tokens": 0,
        }

    transcript_stat = transcript_path.stat()
    transcript_bytes = transcript_stat.st_size
    transcript_full_bytes = (
        transcript_full_path.stat().st_size
        if transcript_full_path is not None and transcript_full_path.is_file()
        else 0
    )

    step_count = 0
    invalid_lines = 0
    truncated_steps = 0
    transcript_subagents = 0
    session_start_ts: float | None = None

    with transcript_path.open("r", encoding="utf-8", errors="replace") as handle:
        for raw_line in handle:
            stripped = raw_line.strip()
            if not stripped:
                continue
            try:
                obj = json.loads(stripped)
            except json.JSONDecodeError:
                invalid_lines += 1
                continue
            step_count += 1
            if step_count == 1:
                session_start_ts = _extract_step_timestamp(obj)
            if _contains_key(obj, "truncated_fields"):
                truncated_steps += 1
            transcript_subagents += _count_subagent_calls(obj)

    if session_start_ts is None:
        session_start_ts = transcript_stat.st_ctime

    session_orch, lifetime_orch = _count_orchestrator_runs(
        project_dir, gemini_config_dir, session_start_ts=session_start_ts
    )
    total_subagents = transcript_subagents + session_orch
    bytes_per_step = round(transcript_bytes / step_count, 2) if step_count > 0 else 0.0

    return {
        "found": True,
        "conversation_dir": str(conv_dir) if conv_dir else None,
        "transcript_path": str(transcript_path),
        "transcript_full_path": str(transcript_full_path) if transcript_full_path else None,
        "session_start_ts": session_start_ts,
        "transcript_bytes": transcript_bytes,
        "transcript_mb": round(transcript_bytes / (1024 * 1024), 3),
        "transcript_full_bytes": transcript_full_bytes,
        "transcript_full_mb": round(transcript_full_bytes / (1024 * 1024), 3),
        "step_count": step_count,
        "invalid_lines": invalid_lines,
        "truncated_steps": truncated_steps,
        "transcript_subagent_dispatches": transcript_subagents,
        "session_orchestrator_runs": session_orch,
        "lifetime_orchestrator_runs": lifetime_orch,
        "orchestrator_runs": session_orch,
        "subagent_dispatches": total_subagents,
        "bytes_per_step": bytes_per_step,
        "estimated_tokens": _estimate_tokens(transcript_bytes),
    }


def _split_frontmatter(text: str) -> tuple[dict[str, str], str]:
    normalized = text.replace("\r\n", "\n").replace("\r", "\n")
    if not normalized.startswith("---\n"):
        return {}, normalized
    end_idx = normalized.find("\n---", 4)
    if end_idx == -1:
        return {}, normalized
    fm_block = normalized[4:end_idx]
    body = normalized[end_idx + 4 :]
    if body.startswith("\n"):
        body = body[1:]
    meta: dict[str, str] = {}
    for line in fm_block.splitlines():
        if ":" in line:
            key, val = line.split(":", 1)
            meta[key.strip().lower()] = val.strip().strip("\"'")
    return meta, body


def _normalize_text(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip().casefold()


def _extract_headings(body: str) -> set[str]:
    headings: set[str] = set()
    for line in body.splitlines():
        m = HEADING_RE.match(line)
        if m:
            norm = _normalize_text(m.group(2))
            if len(norm) >= 8:
                headings.add(norm)
    return headings


def _extract_keywords(body: str) -> set[str]:
    tokens = {tok.casefold() for tok in TOKEN_RE.findall(body)}
    return {t for t in tokens if t not in STOPWORDS and not t.isdigit()}


def scan_rule_tax(
    project_dir: Path,
    gemini_config_dir: Path,
    agents_dir: Path,
) -> dict[str, Any]:
    gcfg = gemini_config_dir.expanduser()
    adir = agents_dir.expanduser()
    pdir = project_dir.expanduser()

    candidates: list[tuple[str, Path]] = [
        ("global_root", gcfg / "GEMINI.md"),
        ("global_root", gcfg / "AGENTS.md"),
    ]
    global_rules_dir = gcfg / "rules"
    if global_rules_dir.is_dir():
        for rule_file in sorted(global_rules_dir.glob("*.md")):
            candidates.append(("global_rule", rule_file))

    if adir.is_dir():
        candidates.append(("agents_root", adir / "AGENTS.md"))
        agents_rules_dir = adir / "rules"
        if agents_rules_dir.is_dir():
            for rule_file in sorted(agents_rules_dir.glob("*.md")):
                candidates.append(("agents_rule", rule_file))

    for fname in ("AGENTS.md", "CLAUDE.md", "GEMINI.md"):
        candidates.append(("project_root", pdir / fname))

    seen_resolved: set[Path] = set()
    files_info: list[dict[str, Any]] = []
    internal_records: list[dict[str, Any]] = []

    for scope, fpath in candidates:
        if not fpath.is_file():
            continue
        try:
            resolved = fpath.resolve()
        except OSError:
            resolved = fpath
        if resolved in seen_resolved:
            continue
        seen_resolved.add(resolved)

        raw_bytes = fpath.read_bytes()
        text = raw_bytes.decode("utf-8", errors="replace")
        fm, body = _split_frontmatter(text)
        trigger = fm.get("trigger", "").strip().lower()
        if scope in ("global_rule", "agents_rule"):
            always_on = trigger in ("", "always_on")
        else:
            always_on = trigger != "manual"

        norm_body = _normalize_text(body)
        norm_sha = hashlib.sha256(norm_body.encode("utf-8")).hexdigest()
        size = len(raw_bytes)
        lines = len(text.replace("\r\n", "\n").splitlines())

        entry = {
            "path": str(fpath),
            "name": fpath.name,
            "scope": scope,
            "trigger": trigger or "always_on",
            "always_on": always_on,
            "bytes": size,
            "lines": lines,
            "estimated_tokens": _estimate_tokens(size),
            "normalized_sha256": norm_sha,
        }
        files_info.append(entry)
        internal_records.append(
            {
                "entry": entry,
                "body": body,
                "norm_body": norm_body,
                "headings": _extract_headings(body),
                "keywords": _extract_keywords(body),
            }
        )

    always_on_records = [rec for rec in internal_records if rec["entry"]["always_on"]]
    always_on_bytes = sum(rec["entry"]["bytes"] for rec in always_on_records)
    waste_findings: list[dict[str, Any]] = []

    # 1. Exact whitespace-normalized duplicate content among always-on files
    by_hash: dict[str, list[dict[str, Any]]] = {}
    for rec in always_on_records:
        if not rec["norm_body"]:
            continue
        by_hash.setdefault(rec["entry"]["normalized_sha256"], []).append(rec)

    for group in by_hash.values():
        if len(group) >= 2:
            wasted = sum(r["entry"]["bytes"] for r in group[1:])
            waste_findings.append(
                {
                    "kind": "duplicate_content",
                    "severity": "high",
                    "files": [r["entry"]["path"] for r in group],
                    "wasted_bytes": wasted,
                    "wasted_tokens": _estimate_tokens(wasted),
                    "detail": (
                        f"{len(group)} always-on rule files have identical normalized content "
                        f"({', '.join(r['entry']['name'] for r in group)})."
                    ),
                }
            )

    # 2. Always-on forwarding stubs that were already merged elsewhere
    merged_stub_paths: set[str] = set()
    for rec in always_on_records:
        if rec["entry"]["scope"] not in ("global_rule", "agents_rule"):
            continue
        lower_body = rec["norm_body"]
        if any(pat in lower_body for pat in MERGED_STUB_PATTERNS):
            merged_stub_paths.add(rec["entry"]["path"])
            wasted = rec["entry"]["bytes"]
            waste_findings.append(
                {
                    "kind": "redundant_merged_stub",
                    "severity": "medium",
                    "files": [rec["entry"]["path"]],
                    "wasted_bytes": wasted,
                    "wasted_tokens": _estimate_tokens(wasted),
                    "detail": (
                        f"Always-on rule {rec['entry']['name']} is a forwarding stub "
                        "already merged into another rule file."
                    ),
                }
            )

    # 3. Topic/file overlap between global root (GEMINI.md/AGENTS.md) and rules/*.md
    global_roots_unique: list[dict[str, Any]] = []
    seen_root_hashes: set[str] = set()
    for rec in always_on_records:
        if rec["entry"]["scope"] == "global_root":
            h = rec["entry"]["normalized_sha256"]
            if h not in seen_root_hashes:
                seen_root_hashes.add(h)
                global_roots_unique.append(rec)

    global_rules = [
        rec
        for rec in always_on_records
        if rec["entry"]["scope"] == "global_rule"
        and rec["entry"]["path"] not in merged_stub_paths
    ]

    for root_rec in global_roots_unique:
        overlapping_rules: list[dict[str, Any]] = []
        root_norm = root_rec["norm_body"]
        for rule_rec in global_rules:
            if rule_rec["entry"]["normalized_sha256"] == root_rec["entry"]["normalized_sha256"]:
                continue
            rule_name_lower = rule_rec["entry"]["name"].casefold()
            mentions_file = rule_name_lower in root_norm
            shared_headings = root_rec["headings"] & rule_rec["headings"]
            union_kw = root_rec["keywords"] | rule_rec["keywords"]
            shared_kw = root_rec["keywords"] & rule_rec["keywords"]
            jaccard = (len(shared_kw) / len(union_kw)) if union_kw else 0.0
            if mentions_file or len(shared_headings) >= 1 or (len(shared_kw) >= 6 and jaccard >= 0.25):
                overlapping_rules.append(rule_rec)

        if overlapping_rules:
            wasted_est = min(
                root_rec["entry"]["bytes"],
                sum(r["entry"]["bytes"] for r in overlapping_rules),
            )
            waste_findings.append(
                {
                    "kind": "global_rule_topic_overlap",
                    "severity": "low",
                    "files": [root_rec["entry"]["path"]]
                    + [r["entry"]["path"] for r in overlapping_rules],
                    "wasted_bytes": wasted_est,
                    "wasted_tokens": _estimate_tokens(wasted_est),
                    "detail": (
                        f"Global root {root_rec['entry']['name']} overlaps with detailed "
                        f"always-on rules: {', '.join(r['entry']['name'] for r in overlapping_rules)}."
                    ),
                }
            )

    return {
        "files": files_info,
        "always_on_file_count": len(always_on_records),
        "always_on_bytes": always_on_bytes,
        "estimated_turn_tax_tokens": _estimate_tokens(always_on_bytes),
        "rule_waste_findings": waste_findings,
    }


def _split_h2_for_estimate(text: str) -> tuple[str, list[str]]:
    normalized = text.replace("\r\n", "\n").replace("\r", "\n")
    has_end_marker = POINTER_END_MARKER in normalized
    lines = normalized.split("\n")
    in_fence = False
    fence_char = ""
    fence_len = 0
    in_pointer_block = False

    preamble_lines: list[str] = []
    sections: list[list[str]] = []
    current: list[str] | None = None

    for line in lines:
        was_in_fence = in_fence
        in_fence, fence_char, fence_len = _update_fence_state(line, in_fence, fence_char, fence_len)

        if has_end_marker and not was_in_fence and not in_fence:
            stripped = line.strip()
            if stripped == POINTER_MARKER:
                in_pointer_block = True
                continue
            if stripped == POINTER_END_MARKER:
                in_pointer_block = False
                continue
            if in_pointer_block:
                continue

        if not was_in_fence and not in_fence and line.startswith("## "):
            if current is not None:
                sections.append(current)
            current = [line]
        else:
            if current is None:
                preamble_lines.append(line)
            else:
                current.append(line)

    if current is not None:
        sections.append(current)

    milestone_sections: list[str] = []
    for sec_lines in sections:
        sec_text = "\n".join(sec_lines)
        if POINTER_MARKER in sec_text or POINTER_END_MARKER in sec_text or sec_lines[0].startswith(POINTER_HEADING_PREFIX):
            continue
        milestone_sections.append(sec_text)

    return "\n".join(preamble_lines), milestone_sections


def scan_hot_ledgers(project_dir: Path) -> dict[str, Any]:
    pdir = project_dir.expanduser().resolve()
    candidates: list[Path] = [
        pdir / "CHECKPOINT.md",
        pdir / "HANDOFF.md",
        pdir / "STATE.md",
    ]
    plan_ops_dir = pdir / "plan" / "ops"
    if plan_ops_dir.is_dir():
        candidates.extend(sorted(plan_ops_dir.glob("*.md")))

    ledgers: list[dict[str, Any]] = []
    checkpoint_exists = False
    checkpoint_lines = 0
    checkpoint_bytes = 0
    checkpoint_h2_count = 0
    est_savings_bytes = 0
    est_savings_pct = 0.0

    for lpath in candidates:
        if not lpath.is_file():
            continue
        raw_bytes = lpath.read_bytes()
        text = raw_bytes.decode("utf-8", errors="replace")
        norm_text = text.replace("\r\n", "\n").replace("\r", "\n")
        line_count = len(norm_text.splitlines())
        byte_count = len(raw_bytes)
        preamble, milestones = _split_h2_for_estimate(norm_text)
        rel_path = lpath.relative_to(pdir).as_posix()

        entry = {
            "path": rel_path,
            "bytes": byte_count,
            "lines": line_count,
            "h2_count": len(milestones),
            "estimated_tokens": _estimate_tokens(byte_count),
        }
        ledgers.append(entry)

        if rel_path == "CHECKPOINT.md":
            checkpoint_exists = True
            checkpoint_lines = line_count
            checkpoint_bytes = byte_count
            checkpoint_h2_count = len(milestones)
            if len(milestones) > 1:
                kept_sample = (preamble.rstrip() + "\n\n" + milestones[0].strip() + "\n").encode("utf-8")
                pointer_overhead = 280
                projected = len(kept_sample) + pointer_overhead
                est_savings_bytes = max(0, byte_count - projected)
                est_savings_pct = round((est_savings_bytes / byte_count) * 100, 2) if byte_count > 0 else 0.0

    total_bytes = sum(item["bytes"] for item in ledgers)
    total_lines = sum(item["lines"] for item in ledgers)

    return {
        "files": ledgers,
        "checkpoint_exists": checkpoint_exists,
        "checkpoint_lines": checkpoint_lines,
        "checkpoint_bytes": checkpoint_bytes,
        "checkpoint_h2_count": checkpoint_h2_count,
        "estimated_checkpoint_savings_bytes": est_savings_bytes,
        "estimated_checkpoint_savings_pct": est_savings_pct,
        "total_hot_ledger_bytes": total_bytes,
        "total_hot_ledger_lines": total_lines,
        "estimated_hot_ledger_tokens": _estimate_tokens(total_bytes),
    }


def evaluate_verdict(
    transcript: dict[str, Any],
    rule_tax: dict[str, Any],
    hot_ledgers: dict[str, Any],
) -> tuple[str, list[str], list[str]]:
    critical_reasons: list[str] = []
    warn_reasons: list[str] = []

    if transcript["transcript_mb"] >= 5.0:
        critical_reasons.append(f"transcript_mb={transcript['transcript_mb']} >= 5.0 MB")
    if transcript["step_count"] >= 2000:
        critical_reasons.append(f"step_count={transcript['step_count']} >= 2000")
    if transcript["subagent_dispatches"] >= 30:
        critical_reasons.append(f"subagent_dispatches={transcript['subagent_dispatches']} >= 30")

    if transcript["transcript_mb"] >= 2.0:
        warn_reasons.append(f"transcript_mb={transcript['transcript_mb']} >= 2.0 MB")
    if transcript["step_count"] >= 800:
        warn_reasons.append(f"step_count={transcript['step_count']} >= 800")
    if hot_ledgers["checkpoint_lines"] > 150:
        warn_reasons.append(f"checkpoint_lines={hot_ledgers['checkpoint_lines']} > 150")
    if len(rule_tax["rule_waste_findings"]) > 0:
        warn_reasons.append(
            f"rule_waste_findings={len(rule_tax['rule_waste_findings'])} detected"
        )

    if critical_reasons:
        return (
            "CRITICAL_ENGINE_COMPACT",
            critical_reasons + [r for r in warn_reasons if r not in critical_reasons],
            [
                "Run Tier 1 compaction first: compact_ledger.py --project-dir . --keep-latest 1 --json",
                "Verify zero_drop_verified=true in .context-compact/STATE.json",
                "Recommend user run /compact at a clean milestone boundary",
            ],
        )
    if warn_reasons:
        return (
            "WARN_COMPACT_LEDGER",
            warn_reasons,
            [
                "Run Tier 1 in-session compaction: compact_ledger.py --project-dir . --keep-latest 1 --json",
                "Continue working from .context-compact/COMPACT_PACKET.md and STATE.json",
            ],
        )
    return (
        "HEALTHY",
        ["All transcript, rule-tax, and hot-ledger metrics are within healthy thresholds."],
        ["No compaction required right now."],
    )


def measure_context(
    project_dir: Path,
    brain_dir: Path | None = None,
    gemini_config_dir: Path | None = None,
    agents_dir: Path | None = None,
) -> dict[str, Any]:
    resolved_project = project_dir.expanduser().resolve()
    resolved_gcfg = (
        gemini_config_dir.expanduser()
        if gemini_config_dir is not None
        else Path.home() / ".gemini" / "config"
    )
    resolved_agents = (
        agents_dir.expanduser()
        if agents_dir is not None
        else Path.home() / ".agents"
    )

    transcript = measure_transcript(resolved_project, brain_dir, resolved_gcfg)
    rule_tax = scan_rule_tax(resolved_project, resolved_gcfg, resolved_agents)
    hot_ledgers = scan_hot_ledgers(resolved_project)
    verdict, reasons, actions = evaluate_verdict(transcript, rule_tax, hot_ledgers)

    return {
        "schema_version": "1.0.0",
        "measured_at": datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds"),
        "project_dir": str(resolved_project),
        "verdict": verdict,
        "reasons": reasons,
        "recommended_actions": actions,
        "transcript": transcript,
        "rule_tax": rule_tax,
        "hot_ledgers": hot_ledgers,
    }


def format_human_report(report: dict[str, Any]) -> str:
    t = report["transcript"]
    r = report["rule_tax"]
    h = report["hot_ledgers"]
    lines = [
        f"=== Auto-Compact Context Pressure Report [{report['verdict']}] ===",
        f"Project : {report['project_dir']}",
        f"Reasons : {'; '.join(report['reasons'])}",
        "",
        "[1] Transcript",
        (
            f"  - Path            : {t['transcript_path'] or 'Not found'}"
        ),
        (
            f"  - Size            : {t['transcript_mb']} MB ({t['transcript_bytes']} B) "
            f"| Full: {t['transcript_full_mb']} MB"
        ),
        (
            f"  - Steps           : {t['step_count']} (truncated: {t['truncated_steps']}, "
            f"bytes/step: {t['bytes_per_step']}, est. tokens: {t['estimated_tokens']})"
        ),
        (
            f"  - Subagents       : {t['subagent_dispatches']} "
            f"(transcript: {t['transcript_subagent_dispatches']}, "
            f"orchestrator session/lifetime: {t['session_orchestrator_runs']}/{t['lifetime_orchestrator_runs']})"
        ),
        "",
        "[2] Always-On Rule Tax",
        (
            f"  - Always-on files : {r['always_on_file_count']} "
            f"| Bytes: {r['always_on_bytes']} B (~{r['estimated_turn_tax_tokens']} tokens/turn)"
        ),
        f"  - Waste findings  : {len(r['rule_waste_findings'])}",
    ]
    for finding in r["rule_waste_findings"]:
        lines.append(
            f"    * [{finding['kind']}] ~{finding['wasted_tokens']} tokens — {finding['detail']}"
        )

    lines.extend(
        [
            "",
            "[3] Hot Ledgers",
            (
                f"  - CHECKPOINT.md   : {'present' if h['checkpoint_exists'] else 'absent'} "
                f"| {h['checkpoint_lines']} lines, {h['checkpoint_bytes']} B, "
                f"{h['checkpoint_h2_count']} milestones"
            ),
            (
                f"  - Est. savings    : {h['estimated_checkpoint_savings_bytes']} B "
                f"({h['estimated_checkpoint_savings_pct']}%) when keeping latest 1 milestone"
            ),
            (
                f"  - All hot ledgers : {len(h['files'])} files, "
                f"{h['total_hot_ledger_lines']} lines, ~{h['estimated_hot_ledger_tokens']} tokens"
            ),
        ]
    )
    return "\n".join(lines)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Measure context pressure (transcript, always-on rule tax, hot ledgers)."
    )
    parser.add_argument(
        "--project-dir",
        default=".",
        help="Project directory containing CHECKPOINT.md / hot ledgers (default: .).",
    )
    parser.add_argument(
        "--brain-dir",
        default=None,
        help="Optional conversation dir, brain root dir, or direct path to transcript.jsonl.",
    )
    parser.add_argument(
        "--gemini-config-dir",
        default=str(Path.home() / ".gemini" / "config"),
        help="Path to ~/.gemini/config (default: ~/.gemini/config).",
    )
    parser.add_argument(
        "--agents-dir",
        default=str(Path.home() / ".agents"),
        help="Path to ~/.agents (default: ~/.agents).",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Output machine-readable JSON to stdout.",
    )
    parser.add_argument(
        "--out",
        default=None,
        help="Optional output file path to write JSON report atomically.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    _reconfigure_stdio()
    parser = build_parser()
    args = parser.parse_args(argv)

    report = measure_context(
        project_dir=Path(args.project_dir),
        brain_dir=Path(args.brain_dir) if args.brain_dir else None,
        gemini_config_dir=Path(args.gemini_config_dir),
        agents_dir=Path(args.agents_dir),
    )

    if args.out:
        out_path = Path(args.out).expanduser()
        atomic_write_text(out_path, json.dumps(report, ensure_ascii=False, indent=2) + "\n")

    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        print(format_human_report(report))

    return 0


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env python3
"""Idempotent hot/cold CHECKPOINT.md compaction with fail-closed zero-drop verification.

Pure Python >= 3.10 standard library implementation with UTF-8 atomic writes.
Exit codes:
  0 - Success (compacted, dry-run passed, or verify-only passed)
  1 - Invalid arguments or missing input checkpoint
  2 - Fail-closed Zero-Drop Entity Verification or archive integrity failure
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

BYTES_PER_TOKEN = 3.8
POINTER_HEADING = "## 🗄 Kho lưu trữ lịch sử nén"
POINTER_MARKER = "<!-- AUTO-COMPACT-ARCHIVE-POINTER -->"
POINTER_END_MARKER = "<!-- /AUTO-COMPACT-ARCHIVE-POINTER -->"
TRAILING_NOTES_HEADING = "## 📌 Ghi chú bổ sung cuối phiên"
ARCHIVE_HEADER_MARKER = "<!-- AUTO-COMPACT-ARCHIVE-FILE -->"
ARCHIVE_HEADER = (
    "# Kho lưu trữ lịch sử CHECKPOINT (Auto-Compact Archive)\n"
    f"{ARCHIVE_HEADER_MARKER}\n"
    "> Lưu trữ bất biến các mốc lịch sử đã tách khỏi `CHECKPOINT.md` nóng. "
    "Không sửa thủ công phần đầu tệp."
)

# CommonMark-compliant fenced code block delimiters:
# - Backtick opening fence cannot contain backticks in its info string (avoids matching inline ```foo```).
# - Closing fence contains only the same fence char (>= opening length) and optional trailing whitespace.
FENCE_OPEN_RE = re.compile(r"^\s*(?:(`{3,})[^`]*|(~{3,}).*)$")
FENCE_CLOSE_RE = re.compile(r"^\s*(`{3,}|~{3,})\s*$")
POINTER_META_LINE_RES = (
    re.compile(r"^\s*>\s*.*$"),
    re.compile(r"^\s*-\s*\*\*(?:Kho lạnh \(Cold Archive\)|Cold Archive):?\*\*"),
    re.compile(r"^\s*-\s*\*\*Archive SHA-256:?\*\*"),
    re.compile(r"^\s*-\s*\*\*(?:Số mốc đã lưu kho lạnh|Archived Milestones[^*]*):?\*\*"),
    re.compile(r"^\s*-\s*\*\*Danh sách mốc trong kho lạnh:?\*\*"),
    re.compile(r"^\s{2,}-\s+.*$"),
    re.compile(r"^\s*-\s*`##\s+.*`\s*$"),
)
LONG_SHA_RE = re.compile(r"\b[0-9a-fA-F]{40,64}\b")
SHORT_SHA_RE = re.compile(r"`([0-9a-f]{7,16})(?:…|\.\.\.)?`")
DOMAIN_ID_RE = re.compile(
    r"\b(?:TQ|BL|ADR|REQ|WB|SV|CG|EX|AC|GK|K1|K2|P\d|G\d|HL|SB|RISK|HYP)-[A-Za-z0-9_.-]+\b"
)
CODE_PATH_RE = re.compile(
    r"`([^`\r\n]*[\\/][^`\r\n]*\.(?:md|ts|mjs|js|json|py|exe|dll|patch|yml|yaml|html))`",
    re.IGNORECASE,
)
POINTER_SHA_RE = re.compile(r"\*\*Archive SHA-256:?\*\*:?\s*`([0-9a-fA-F]{64})`")

MAX_POINTER_LIST_ITEMS = 25
MAX_PACKET_LINES = 78


class ZeroDropVerificationError(RuntimeError):
    """Raised when fail-closed entity verification detects any dropped entity."""

    def __init__(self, message: str, verification: dict[str, Any]) -> None:
        super().__init__(message)
        self.verification = verification


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


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _normalize_newlines(text: str) -> str:
    return text.replace("\r\n", "\n").replace("\r", "\n")


def _normalize_heading(heading: str) -> str:
    return re.sub(r"\s+", " ", heading.strip()).casefold()


def _normalize_ws(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


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


def _is_pointer_metadata_line(line: str) -> bool:
    return any(pattern.match(line) for pattern in POINTER_META_LINE_RES)


def extract_pointer_and_user_content(text: str) -> dict[str, Any]:
    """Separate machine-generated archive pointer block from user content in raw CHECKPOINT.md.

    Supports both explicitly delimited blocks:
      <!-- AUTO-COMPACT-ARCHIVE-POINTER --> ... <!-- /AUTO-COMPACT-ARCHIVE-POINTER -->
    and legacy blocks starting with `## 🗄 Kho lưu trữ lịch sử nén` without a closing tag.
    Any user content appended after the pointer block is strictly preserved.
    """
    normalized = _normalize_newlines(text)
    lines = normalized.split("\n")

    # Locate markers outside fenced code blocks
    in_fence = False
    fence_char = ""
    fence_len = 0
    explicit_start: int | None = None
    explicit_end: int | None = None
    first_indicator: int | None = None

    for idx, line in enumerate(lines):
        was_in_fence = in_fence
        in_fence, fence_char, fence_len = _update_fence_state(line, in_fence, fence_char, fence_len)
        if was_in_fence or in_fence:
            continue

        stripped = line.strip()
        is_start_marker = stripped == POINTER_MARKER
        is_end_marker = stripped == POINTER_END_MARKER
        is_pointer_heading = line.startswith(POINTER_HEADING)

        if first_indicator is None and (is_start_marker or is_end_marker or is_pointer_heading):
            first_indicator = idx

        if explicit_start is None and is_start_marker:
            explicit_start = idx
        elif explicit_start is not None and explicit_end is None and is_end_marker:
            explicit_end = idx
            break

    if first_indicator is None:
        return {
            "has_pointer_indicator": False,
            "pointer_block_text": "",
            "recorded_pointer_sha": None,
            "user_before_pointer": normalized.strip(),
            "user_after_pointer": "",
            "orig_user_text": normalized,
        }

    before_lines: list[str]
    pointer_lines: list[str]
    after_lines: list[str]

    if explicit_start is not None and explicit_end is not None:
        before_lines = list(lines[:explicit_start])
        inner_lines = lines[explicit_start + 1 : explicit_end]
        after_lines = list(lines[explicit_end + 1 :])

        # Defensive preservation: if a user manually spliced non-pointer lines inside the markers
        # (for example right before `## 🗄`), keep those user lines instead of dropping them.
        inner_heading_idx: int | None = None
        inner_in_fence = False
        inner_fchar = ""
        inner_flen = 0
        for j, ln in enumerate(inner_lines):
            was_f = inner_in_fence
            inner_in_fence, inner_fchar, inner_flen = _update_fence_state(
                ln, inner_in_fence, inner_fchar, inner_flen
            )
            if not was_f and not inner_in_fence and ln.startswith(POINTER_HEADING):
                inner_heading_idx = j
                break

        valid_inner: list[str] = []
        extra_after: list[str] = []
        start_meta = 0
        if inner_heading_idx is not None and inner_heading_idx > 0:
            pre_heading = inner_lines[:inner_heading_idx]
            if any(ln.strip() and not _is_pointer_metadata_line(ln) for ln in pre_heading):
                before_lines.extend(pre_heading)
                start_meta = inner_heading_idx

        in_meta = True
        pending_blanks: list[str] = []
        for j in range(start_meta, len(inner_lines)):
            ln = inner_lines[j]
            s = ln.strip()
            if in_meta:
                if not s:
                    pending_blanks.append(ln)
                elif (
                    ln.startswith(POINTER_HEADING)
                    or s in (POINTER_MARKER, POINTER_END_MARKER)
                    or _is_pointer_metadata_line(ln)
                ):
                    valid_inner.extend(pending_blanks)
                    pending_blanks = []
                    valid_inner.append(ln)
                else:
                    in_meta = False
                    extra_after.extend(pending_blanks)
                    pending_blanks = []
                    extra_after.append(ln)
            else:
                extra_after.append(ln)

        if extra_after:
            after_lines = extra_after + after_lines

        pointer_lines = [lines[explicit_start], *valid_inner, lines[explicit_end]]
    else:
        # Legacy pointer section without explicit closing tag: consume strictly machine-generated lines only.
        before_lines = list(lines[:first_indicator])
        pointer_lines = []
        after_lines = []
        pending_blanks = []
        seen_heading = False
        idx = first_indicator
        while idx < len(lines):
            ln = lines[idx]
            s = ln.strip()
            if not s:
                pending_blanks.append(ln)
                idx += 1
                continue
            if s in (POINTER_MARKER, POINTER_END_MARKER):
                pointer_lines.extend(pending_blanks)
                pending_blanks = []
                pointer_lines.append(ln)
                idx += 1
                if s == POINTER_END_MARKER:
                    break
                continue
            if ln.startswith(POINTER_HEADING) and not seen_heading:
                seen_heading = True
                pointer_lines.extend(pending_blanks)
                pending_blanks = []
                pointer_lines.append(ln)
                idx += 1
                continue
            if _is_pointer_metadata_line(ln):
                pointer_lines.extend(pending_blanks)
                pending_blanks = []
                pointer_lines.append(ln)
                idx += 1
                continue
            # First non-pointer user line stops pointer consumption; preserve it and everything after it.
            break

        after_lines = list(lines[idx:])

    user_before = "\n".join(before_lines).strip()
    user_after = "\n".join(after_lines).strip()
    pointer_block_text = "\n".join(pointer_lines).strip()
    orig_user_parts = [part for part in (user_before, user_after) if part]
    orig_user_text = ("\n\n".join(orig_user_parts).rstrip() + "\n") if orig_user_parts else ""

    m_sha = POINTER_SHA_RE.search(pointer_block_text)
    recorded_sha = m_sha.group(1).lower() if m_sha else None

    return {
        "has_pointer_indicator": True,
        "pointer_block_text": pointer_block_text,
        "recorded_pointer_sha": recorded_sha,
        "user_before_pointer": user_before,
        "user_after_pointer": user_after,
        "orig_user_text": orig_user_text,
    }


def parse_markdown_sections(text: str) -> tuple[str, list[dict[str, Any]]]:
    """Split markdown into preamble and top-level H2 sections outside fenced code blocks."""
    normalized = _normalize_newlines(text)
    lines = normalized.split("\n")

    in_fence = False
    fence_char = ""
    fence_len = 0

    preamble_lines: list[str] = []
    raw_sections: list[list[str]] = []
    current: list[str] | None = None

    for line in lines:
        was_in_fence = in_fence
        in_fence, fence_char, fence_len = _update_fence_state(line, in_fence, fence_char, fence_len)

        if not was_in_fence and not in_fence and line.startswith("## "):
            if current is not None:
                raw_sections.append(current)
            current = [line]
        else:
            if current is None:
                preamble_lines.append(line)
            else:
                current.append(line)

    if current is not None:
        raw_sections.append(current)

    preamble = "\n".join(preamble_lines).strip()
    sections: list[dict[str, Any]] = []
    for sec_lines in raw_sections:
        heading = sec_lines[0].rstrip()
        body = "\n".join(sec_lines[1:]).strip()
        raw = f"{heading}\n{body}".strip() if body else heading
        is_pointer = POINTER_MARKER in raw or POINTER_END_MARKER in raw or heading.startswith(POINTER_HEADING)
        sections.append(
            {
                "heading": heading,
                "norm_heading": _normalize_heading(heading),
                "body": body,
                "raw": raw,
                "is_pointer": is_pointer,
            }
        )
    return preamble, sections


def extract_entities(text: str) -> dict[str, list[str]]:
    """Extract immutable technical entities: SHA hashes, domain IDs, and backticked code paths."""
    long_shas = {m.lower() for m in LONG_SHA_RE.findall(text)}
    short_shas = {m.lower() for m in SHORT_SHA_RE.findall(text)}
    sha_hashes = sorted(long_shas | short_shas)
    domain_ids = sorted(set(DOMAIN_ID_RE.findall(text)))
    code_paths = sorted(set(CODE_PATH_RE.findall(text)))
    return {
        "sha_hashes": sha_hashes,
        "domain_ids": domain_ids,
        "code_paths": code_paths,
    }


def verify_zero_drop(source_text: str, target_text: str) -> dict[str, Any]:
    """Verify 100% of immutable entities from source_text exist in target_text."""
    src = extract_entities(source_text)
    tgt = extract_entities(target_text)

    missing_sha = sorted(set(src["sha_hashes"]) - set(tgt["sha_hashes"]))
    missing_ids = sorted(set(src["domain_ids"]) - set(tgt["domain_ids"]))
    missing_paths = sorted(set(src["code_paths"]) - set(tgt["code_paths"]))

    verified = not (missing_sha or missing_ids or missing_paths)
    return {
        "verified": verified,
        "counts": {
            "sha_hashes": len(src["sha_hashes"]),
            "domain_ids": len(src["domain_ids"]),
            "code_paths": len(src["code_paths"]),
        },
        "missing": {
            "sha_hashes": missing_sha,
            "domain_ids": missing_ids,
            "code_paths": missing_paths,
        },
    }


def _split_atomic_blocks(body: str) -> list[str]:
    """Split section body into atomic units: intact fenced code blocks and non-empty lines."""
    blocks: list[str] = []
    in_fence = False
    fence_char = ""
    fence_len = 0
    fence_buf: list[str] = []

    for line in body.splitlines():
        if not in_fence:
            m_open = FENCE_OPEN_RE.match(line)
            if m_open:
                marker = m_open.group(1) or m_open.group(2)
                in_fence = True
                fence_char = marker[0]
                fence_len = len(marker)
                fence_buf = [line]
            elif line.strip():
                blocks.append(line)
        else:
            fence_buf.append(line)
            m_close = FENCE_CLOSE_RE.match(line)
            if m_close:
                marker = m_close.group(1)
                if marker[0] == fence_char and len(marker) >= fence_len:
                    blocks.append("\n".join(fence_buf))
                    in_fence = False
                    fence_char = ""
                    fence_len = 0
                    fence_buf = []

    if in_fence and fence_buf:
        blocks.append("\n".join(fence_buf))

    return blocks


def _normalize_atomic_block(block: str) -> str:
    if "\n" in block:
        return "\n".join(ln.rstrip() for ln in block.splitlines()).strip()
    return block.strip()


def _merge_duplicate_section(newer: dict[str, Any], older: dict[str, Any]) -> dict[str, Any]:
    """Merge two sections sharing a normalized heading using atomic blocks (preserving intact fences)."""
    if _normalize_ws(newer["raw"]) == _normalize_ws(older["raw"]):
        return newer

    newer_blocks = _split_atomic_blocks(newer["body"])
    older_blocks = _split_atomic_blocks(older["body"])
    newer_keys = {_normalize_atomic_block(b) for b in newer_blocks}
    older_keys = {_normalize_atomic_block(b) for b in older_blocks}

    missing_from_newer: list[str] = []
    seen_newer = set(newer_keys)
    for blk in older_blocks:
        key = _normalize_atomic_block(blk)
        if key not in seen_newer:
            missing_from_newer.append(blk)
            seen_newer.add(key)

    if not missing_from_newer:
        return newer

    missing_from_older = [
        blk for blk in newer_blocks if _normalize_atomic_block(blk) not in older_keys
    ]
    if not missing_from_older:
        return older

    merged_body = (
        newer["body"].rstrip()
        + "\n"
        + "\n".join(missing_from_newer)
    ).strip()
    merged_raw = f"{newer['heading']}\n{merged_body}".strip()
    return {
        "heading": newer["heading"],
        "norm_heading": newer["norm_heading"],
        "body": merged_body,
        "raw": merged_raw,
        "is_pointer": False,
    }


def merge_archive_sections(
    newly_archived: list[dict[str, Any]],
    existing_archived: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Merge newly archived milestones ahead of existing ones, deduplicating by heading."""
    merged: dict[str, dict[str, Any]] = {}
    for sec in list(newly_archived) + list(existing_archived):
        if sec.get("is_pointer"):
            continue
        key = sec["norm_heading"]
        if key not in merged:
            merged[key] = sec
        else:
            merged[key] = _merge_duplicate_section(merged[key], sec)
    return list(merged.values())


def render_archive(
    archived_sections: list[dict[str, Any]],
    extra_preamble: str = "",
) -> str:
    parts = [ARCHIVE_HEADER]
    if extra_preamble.strip():
        parts.append(extra_preamble.strip())
    for sec in archived_sections:
        parts.append(sec["raw"].strip())
    return "\n\n".join(parts).rstrip() + "\n"


def render_pointer_section(
    archive_rel_path: str,
    archive_sha256: str,
    archived_sections: list[dict[str, Any]],
) -> str:
    lines = [
        POINTER_MARKER,
        POINTER_HEADING,
        f"- **Kho lạnh (Cold Archive):** `{archive_rel_path}`",
        f"- **Archive SHA-256:** `{archive_sha256}`",
        f"- **Số mốc đã lưu kho lạnh:** {len(archived_sections)}",
        "- **Danh sách mốc trong kho lạnh:**",
    ]
    shown = archived_sections[:MAX_POINTER_LIST_ITEMS]
    for sec in shown:
        title = sec["heading"].removeprefix("## ").strip()
        lines.append(f"  - {title}")
    remaining = len(archived_sections) - len(shown)
    if remaining > 0:
        lines.append(f"  - ... và {remaining} mốc cũ hơn (xem đầy đủ trong kho lạnh)")
    lines.append(POINTER_END_MARKER)
    return "\n".join(lines)


def render_hot_checkpoint(
    preamble: str,
    hot_sections: list[dict[str, Any]],
    pointer_section: str | None,
) -> str:
    parts: list[str] = []
    if preamble.strip():
        parts.append(preamble.strip())
    for sec in hot_sections:
        parts.append(sec["raw"].strip())
    if pointer_section:
        parts.append(pointer_section.strip())
    return "\n\n".join(parts).rstrip() + "\n"


def _detect_git_info(project_dir: Path) -> dict[str, Any]:
    def _run_git(args: list[str]) -> str | None:
        try:
            proc = subprocess.run(
                ["git", *args],
                cwd=str(project_dir),
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=5,
                check=False,
            )
            if proc.returncode != 0:
                return None
            return proc.stdout.strip()
        except (OSError, subprocess.SubprocessError):
            return None

    is_inside = _run_git(["rev-parse", "--is-inside-work-tree"])
    if is_inside != "true":
        return {"is_repo": False, "branch": None, "head_sha": None, "dirty": False}

    branch = _run_git(["rev-parse", "--abbrev-ref", "HEAD"])
    head_sha = _run_git(["rev-parse", "HEAD"])
    status = _run_git(["status", "--porcelain"])
    return {
        "is_repo": True,
        "branch": branch,
        "head_sha": head_sha,
        "dirty": bool(status),
    }


def _format_entity_line(label: str, items: list[str], limit: int = 8) -> str:
    if not items:
        return f"- **{label}:** *(none)*"
    shown = ", ".join(f"`{item}`" for item in items[:limit])
    extra = len(items) - limit
    if extra > 0:
        return f"- **{label} ({len(items)}):** {shown} *(+{extra} more in STATE.json)*"
    return f"- **{label} ({len(items)}):** {shown}"


def render_compact_packet(state: dict[str, Any], hot_sections: list[dict[str, Any]]) -> str:
    git_info = state["git"]
    metrics = state["metrics"]
    archive = state["archive"]
    hot_ent = state["entities"]["hot_entities"]
    total_ent = state["entities"]["total_tracked"]

    git_summary = (
        f"`{git_info['branch']}` @ `{(git_info['head_sha'] or '')[:12]}`"
        f"{' (dirty)' if git_info['dirty'] else ''}"
        if git_info["is_repo"]
        else "not a git repository"
    )

    lines = [
        "# COMPACT PACKET — Working-Set Snapshot",
        "",
        "## Trạng Thái Chốt (Verified State)",
        f"- **Generated:** `{state['generated_at']}`",
        f"- **Git:** {git_summary}",
        f"- **Hot Ledger:** `{state['checkpoint']['path']}` (`{metrics['lines_after']}` lines, `{metrics['bytes_after']}` B)",
        (
            f"- **Cold Archive:** `{archive['path']}` (`{archive['lines']}` lines, "
            f"SHA-256 `{archive['sha256'] or 'none'}`)"
        ),
        (
            f"- **Compaction Savings:** `{metrics['bytes_before']} B -> {metrics['bytes_after']} B` "
            f"(`-{metrics['reduction_pct']}%`, ~`{metrics['estimated_tokens_saved_per_read']}` tokens saved/read)"
        ),
        (
            f"- **Zero-Drop Gate:** `zero_drop_verified: {str(state['entities']['zero_drop_verified']).lower()}` "
            f"(SHA: `{total_ent['sha_hashes']}`, IDs: `{total_ent['domain_ids']}`, Paths: `{total_ent['code_paths']}`)"
        ),
        "",
        "## Mốc Nóng Hiện Hành (Active Hot Milestone)",
    ]

    for sec in hot_sections[:2]:
        lines.append(f"- **{sec['heading'].removeprefix('## ').strip()}**")
        highlights = [
            ln.strip()
            for ln in sec["body"].splitlines()
            if ln.strip() and not ln.strip().startswith("```")
        ]
        for bullet in highlights[:8]:
            clean = bullet.lstrip("-* ").strip()
            if len(clean) > 140:
                clean = clean[:137] + "..."
            lines.append(f"  - {clean}")

    lines.extend(
        [
            "",
            "## Thực Thể Cốt Lõi Trong Mốc Nóng",
            _format_entity_line("Domain IDs", hot_ent["domain_ids"], limit=10),
            _format_entity_line("SHA Hashes", hot_ent["sha_hashes"], limit=6),
            _format_entity_line("Code Paths", hot_ent["code_paths"], limit=6),
            "",
            "## Quy Ước Tra Cứu Kho Lạnh",
            f"- Không nạp toàn bộ `{archive['path']}` vào phiên chính.",
            f"- Tra cứu theo mã/tiêu đề bằng `Select-String -Path {archive['path']} -Pattern <ID>`.",
        ]
    )

    if len(lines) > MAX_PACKET_LINES:
        lines = lines[: MAX_PACKET_LINES - 1] + ["- *(Truncated for <80-line packet budget; see STATE.json)*"]
    return "\n".join(lines).rstrip() + "\n"


def _resolve_relative_or_abs(base_dir: Path, raw_path: str) -> Path:
    p = Path(raw_path).expanduser()
    return p if p.is_absolute() else (base_dir / p)


def _display_rel_path(path: Path, base_dir: Path) -> str:
    try:
        return path.resolve().relative_to(base_dir.resolve()).as_posix()
    except ValueError:
        return path.as_posix()


def compact_checkpoint(
    project_dir: Path,
    checkpoint: str = "CHECKPOINT.md",
    keep_latest: int = 1,
    compact_dir: str = ".context-compact",
    dry_run: bool = False,
    verify_only: bool = False,
) -> dict[str, Any]:
    if keep_latest < 1:
        raise ValueError("--keep-latest must be >= 1")

    resolved_project = project_dir.expanduser().resolve()
    checkpoint_path = _resolve_relative_or_abs(resolved_project, checkpoint)
    compact_root = _resolve_relative_or_abs(resolved_project, compact_dir)
    archive_path = compact_root / "archive" / "checkpoint-archive.md"
    state_path = compact_root / "STATE.json"
    packet_path = compact_root / "COMPACT_PACKET.md"

    if not checkpoint_path.is_file():
        raise FileNotFoundError(f"Checkpoint file not found: {checkpoint_path}")

    raw_checkpoint_bytes = checkpoint_path.read_bytes()
    orig_checkpoint_text = _normalize_newlines(raw_checkpoint_bytes.decode("utf-8", errors="replace"))
    extracted = extract_pointer_and_user_content(orig_checkpoint_text)

    preamble, before_sections = parse_markdown_sections(extracted["user_before_pointer"])
    trailing_sections: list[dict[str, Any]] = []
    if extracted["user_after_pointer"]:
        after_preamble, after_sections = parse_markdown_sections(extracted["user_after_pointer"])
        if after_preamble:
            trailing_sections.append(
                {
                    "heading": TRAILING_NOTES_HEADING,
                    "norm_heading": _normalize_heading(TRAILING_NOTES_HEADING),
                    "body": after_preamble,
                    "raw": f"{TRAILING_NOTES_HEADING}\n{after_preamble}",
                    "is_pointer": False,
                }
            )
        trailing_sections.extend(s for s in after_sections if not s["is_pointer"])

    milestone_sections = trailing_sections + [s for s in before_sections if not s["is_pointer"]]

    existing_archive_bytes: bytes | None = None
    existing_archive_text = ""
    existing_archived_sections: list[dict[str, Any]] = []
    extra_archive_preamble = ""
    if archive_path.is_file():
        existing_archive_bytes = archive_path.read_bytes()
        existing_archive_text = _normalize_newlines(
            existing_archive_bytes.decode("utf-8", errors="replace")
        )
        arch_preamble, arch_sections = parse_markdown_sections(existing_archive_text)
        existing_archived_sections = [s for s in arch_sections if not s["is_pointer"]]
        if ARCHIVE_HEADER_MARKER not in arch_preamble and arch_preamble.strip():
            extra_archive_preamble = arch_preamble

    # Per spec §3.3: source_combined is derived directly from raw orig_checkpoint_text
    # with ONLY the machine-generated archive pointer block stripped.
    orig_user_text = extracted["orig_user_text"]
    source_combined = orig_user_text + ("\n" + existing_archive_text if existing_archive_text else "")

    if verify_only:
        integrity_errors: list[str] = []
        recorded_pointer_sha = extracted["recorded_pointer_sha"]
        actual_archive_sha = (
            hashlib.sha256(existing_archive_bytes).hexdigest()
            if existing_archive_bytes is not None
            else None
        )

        if extracted["has_pointer_indicator"]:
            if not recorded_pointer_sha:
                integrity_errors.append(
                    "CHECKPOINT.md contains an archive pointer indicator but is missing a valid 64-hex Archive SHA-256."
                )
            if not archive_path.is_file():
                integrity_errors.append(f"Archive file missing: {archive_path}")
            elif recorded_pointer_sha and actual_archive_sha != recorded_pointer_sha:
                integrity_errors.append(
                    f"Archive SHA-256 mismatch vs CHECKPOINT pointer: "
                    f"expected {recorded_pointer_sha}, got {actual_archive_sha}"
                )

        current_entities = extract_entities(source_combined)
        missing_entities: dict[str, list[str]] = {
            "sha_hashes": [],
            "domain_ids": [],
            "code_paths": [],
        }
        tracked_counts = {
            "sha_hashes": len(current_entities["sha_hashes"]),
            "domain_ids": len(current_entities["domain_ids"]),
            "code_paths": len(current_entities["code_paths"]),
        }

        if state_path.is_file():
            try:
                state_data = json.loads(state_path.read_text(encoding="utf-8"))
                state_sha = (state_data.get("archive") or {}).get("sha256")
                if state_sha:
                    if not archive_path.is_file():
                        msg = f"Archive file missing (expected by STATE.json): {archive_path}"
                        if msg not in integrity_errors:
                            integrity_errors.append(msg)
                    elif actual_archive_sha != state_sha:
                        integrity_errors.append(
                            f"Archive SHA-256 mismatch vs STATE.json: "
                            f"expected {state_sha}, got {actual_archive_sha}"
                        )

                recorded_entities = state_data.get("verified_entities")
                if isinstance(recorded_entities, dict):
                    for key in ("sha_hashes", "domain_ids", "code_paths"):
                        expected_set = set(recorded_entities.get(key) or [])
                        actual_set = set(current_entities[key])
                        tracked_counts[key] = len(expected_set)
                        missing_for_key = sorted(expected_set - actual_set)
                        if missing_for_key:
                            missing_entities[key] = missing_for_key
                    if any(missing_entities.values()):
                        integrity_errors.append(
                            "Verified entities missing compared to STATE.json['verified_entities']."
                        )
            except (OSError, json.JSONDecodeError) as exc:
                integrity_errors.append(f"Failed to parse STATE.json: {exc}")

        entities_verified = not any(missing_entities.values())
        verification = {
            "verified": entities_verified,
            "counts": tracked_counts,
            "missing": missing_entities,
        }
        verified = entities_verified and not integrity_errors

        result = {
            "mode": "verify_only",
            "project_dir": str(resolved_project),
            "checkpoint_path": str(checkpoint_path),
            "archive_path": str(archive_path),
            "archive_sha256": actual_archive_sha,
            "zero_drop_verified": verified,
            "verification": verification,
            "integrity_errors": integrity_errors,
        }
        if not verified:
            raise ZeroDropVerificationError(
                "Verify-only failed: " + "; ".join(integrity_errors or ["Missing entities detected."]),
                result,
            )
        return result

    hot_sections = milestone_sections[:keep_latest]
    newly_archived = milestone_sections[keep_latest:]
    merged_archived = merge_archive_sections(newly_archived, existing_archived_sections)

    archive_rel = _display_rel_path(archive_path, resolved_project)
    checkpoint_rel = _display_rel_path(checkpoint_path, resolved_project)

    if merged_archived:
        new_archive_text = render_archive(merged_archived, extra_archive_preamble)
        archive_sha = sha256_text(new_archive_text)
        pointer_text = render_pointer_section(archive_rel, archive_sha, merged_archived)
        archive_exists = True
    else:
        new_archive_text = ""
        archive_sha = None
        pointer_text = None
        archive_exists = False

    new_hot_text = render_hot_checkpoint(preamble, hot_sections, pointer_text)

    # Zero-Drop Entity Verification Gate (FAIL-CLOSED)
    target_without_pointer = render_hot_checkpoint(preamble, hot_sections, None)
    target_combined = target_without_pointer + ("\n" + new_archive_text if new_archive_text else "")
    verification = verify_zero_drop(source_combined, target_combined)

    if not verification["verified"]:
        raise ZeroDropVerificationError(
            "Zero-drop verification failed; refusing to overwrite CHECKPOINT.md.",
            verification,
        )

    bytes_before = len(orig_checkpoint_text.encode("utf-8"))
    bytes_after = len(new_hot_text.encode("utf-8"))
    lines_before = len(orig_checkpoint_text.splitlines())
    lines_after = len(new_hot_text.splitlines())
    bytes_saved = max(0, bytes_before - bytes_after)
    reduction_pct = round((bytes_saved / bytes_before) * 100, 2) if bytes_before > 0 else 0.0
    est_tokens_saved = round(bytes_saved / BYTES_PER_TOKEN) if bytes_saved > 0 else 0

    hot_text_for_entities = "\n\n".join(s["raw"] for s in hot_sections)
    hot_entities = extract_entities(hot_text_for_entities)
    verified_entities = extract_entities(target_combined)
    git_info = _detect_git_info(resolved_project)

    state: dict[str, Any] = {
        "schema_version": "1.0.0",
        "generated_at": datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds"),
        "project_dir": str(resolved_project),
        "git": git_info,
        "checkpoint": {
            "path": checkpoint_rel,
            "keep_latest": keep_latest,
            "hot_milestones": [s["heading"] for s in hot_sections],
            "archived_milestone_count": len(merged_archived),
            "archived_milestones": [s["heading"] for s in merged_archived],
        },
        "archive": {
            "path": archive_rel,
            "exists": archive_exists,
            "sha256": archive_sha,
            "bytes": len(new_archive_text.encode("utf-8")),
            "lines": len(new_archive_text.splitlines()) if new_archive_text else 0,
        },
        "metrics": {
            "bytes_before": bytes_before,
            "bytes_after": bytes_after,
            "lines_before": lines_before,
            "lines_after": lines_after,
            "bytes_saved": bytes_saved,
            "reduction_pct": reduction_pct,
            "estimated_tokens_saved_per_read": est_tokens_saved,
        },
        "entities": {
            "zero_drop_verified": True,
            "total_tracked": verification["counts"],
            "hot_entities": hot_entities,
        },
        "verified_entities": verified_entities,
    }

    packet_text = render_compact_packet(state, hot_sections)

    if not dry_run:
        if archive_exists:
            atomic_write_text(archive_path, new_archive_text)
        atomic_write_text(checkpoint_path, new_hot_text)
        atomic_write_text(state_path, json.dumps(state, ensure_ascii=False, indent=2) + "\n")
        atomic_write_text(packet_path, packet_text)

    return {
        "mode": "dry_run" if dry_run else "compacted",
        "dry_run": dry_run,
        "checkpoint_path": str(checkpoint_path),
        "archive_path": str(archive_path),
        "state_path": str(state_path),
        "packet_path": str(packet_path),
        "packet_lines": len(packet_text.splitlines()),
        "zero_drop_verified": True,
        "verification": verification,
        "state": state,
    }


def format_human_summary(result: dict[str, Any]) -> str:
    if result.get("mode") == "verify_only":
        return (
            "=== Auto-Compact Verify-Only [PASS] ===\n"
            f"Checkpoint : {result['checkpoint_path']}\n"
            f"Archive    : {result['archive_path']} (SHA-256: {result['archive_sha256'] or 'none'})\n"
            f"Tracked    : {result['verification']['counts']}\n"
            "Zero-Drop  : VERIFIED (0 dropped entities)"
        )

    st = result["state"]
    m = st["metrics"]
    a = st["archive"]
    tag = "DRY-RUN" if result["dry_run"] else "COMPACTED"
    return (
        f"=== Auto-Compact Ledger [{tag}] ===\n"
        f"Checkpoint : {result['checkpoint_path']}\n"
        f"Size       : {m['bytes_before']} B -> {m['bytes_after']} B "
        f"({m['lines_before']} -> {m['lines_after']} lines, -{m['reduction_pct']}%)\n"
        f"Saved/Read : ~{m['estimated_tokens_saved_per_read']} tokens\n"
        f"Archive    : {a['path']} ({st['checkpoint']['archived_milestone_count']} milestones, "
        f"SHA-256: {a['sha256'] or 'none'})\n"
        f"Packet     : {result['packet_path']} ({result['packet_lines']} lines)\n"
        f"Zero-Drop  : VERIFIED ({st['entities']['total_tracked']})"
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Idempotent hot/cold CHECKPOINT.md compaction with fail-closed zero-drop verification."
    )
    parser.add_argument(
        "--project-dir",
        default=".",
        help="Project root directory (default: .).",
    )
    parser.add_argument(
        "--checkpoint",
        default="CHECKPOINT.md",
        help="Path to CHECKPOINT.md relative to --project-dir or absolute (default: CHECKPOINT.md).",
    )
    parser.add_argument(
        "--keep-latest",
        type=int,
        default=1,
        help="Number of most recent H2 milestone sections to keep hot (default: 1).",
    )
    parser.add_argument(
        "--compact-dir",
        default=".context-compact",
        help="Directory for cold archive, STATE.json, and COMPACT_PACKET.md (default: .context-compact).",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Simulate compaction and run zero-drop verification without modifying any files.",
    )
    parser.add_argument(
        "--verify-only",
        action="store_true",
        help="Verify existing CHECKPOINT.md, cold archive SHA-256, and zero-drop integrity without writing.",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Output machine-readable JSON to stdout.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    _reconfigure_stdio()
    parser = build_parser()
    args = parser.parse_args(argv)

    try:
        result = compact_checkpoint(
            project_dir=Path(args.project_dir),
            checkpoint=args.checkpoint,
            keep_latest=args.keep_latest,
            compact_dir=args.compact_dir,
            dry_run=args.dry_run,
            verify_only=args.verify_only,
        )
    except ZeroDropVerificationError as exc:
        print(f"ERROR [EXIT 2]: {exc}", file=sys.stderr)
        print(json.dumps(exc.verification, ensure_ascii=False, indent=2), file=sys.stderr)
        if args.json:
            print(
                json.dumps(
                    {
                        "zero_drop_verified": False,
                        "error": str(exc),
                        "details": exc.verification,
                    },
                    ensure_ascii=False,
                    indent=2,
                )
            )
        return 2
    except (FileNotFoundError, ValueError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1

    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        print(format_human_summary(result))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

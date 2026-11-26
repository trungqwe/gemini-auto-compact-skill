#!/usr/bin/env python3
"""Comprehensive unittest suite for gemini-auto-compact-skill (pure Python stdlib)."""

from __future__ import annotations

import contextlib
import hashlib
import io
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

# Prevent bytecode pollution inside skills/auto-compact/scripts/
sys.dont_write_bytecode = True

REPO_ROOT = Path(__file__).resolve().parent.parent
SKILL_DIR = REPO_ROOT / "skills" / "auto-compact"
SCRIPTS_DIR = SKILL_DIR / "scripts"
MEASURE_SCRIPT = SCRIPTS_DIR / "measure_context.py"
COMPACT_SCRIPT = SCRIPTS_DIR / "compact_ledger.py"
INSTALL_SCRIPT = REPO_ROOT / "install.py"

sys.path.insert(0, str(SCRIPTS_DIR))
sys.path.insert(0, str(REPO_ROOT))

import compact_ledger  # noqa: E402
import install as installer  # noqa: E402
import measure_context  # noqa: E402


def _run_py(script: Path, args: list[str], cwd: Path | None = None) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    env["PYTHONUTF8"] = "1"
    return subprocess.run(
        [sys.executable, str(script), *args],
        cwd=str(cwd or REPO_ROOT),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        env=env,
        timeout=30,
        check=False,
    )


def _make_vietnamese_checkpoint(num_milestones: int = 8) -> str:
    """Build a >400-line Vietnamese UTF-8 CHECKPOINT.md with rich immutable entities."""
    preamble = [
        "# CHECKPOINT — Nhật Ký Tiến Độ Dự Án",
        "",
        "> Nguyên tắc bất biến: Không bao giờ dùng `rm -rf`, luôn giữ nguyên mã `ADR-001` và `REQ-100`.",
        "> Kho nguồn chính: `src/core/engine.ts` và `D:\\epower-tools\\scripts\\runner.py`.",
        "",
    ]
    blocks = ["\n".join(preamble)]

    for idx in range(num_milestones, 0, -1):
        tq_start = (idx - 1) * 8 + 1
        bl_num = idx * 32
        sha_full = hashlib.sha256(f"milestone-{idx}-artifact".encode("utf-8")).hexdigest()
        short_sha = sha_full[:10]
        short_ellipsis = sha_full[10:20]
        lines = [
            f"## Mốc {idx}: Hoàn thiện phân hệ xử lý tiếng Việt & kiểm thử tự động #{idx}",
            f"- Trạng thái: Đã nghiệm thu `TQ-{tq_start:03d}`, `TQ-{tq_start + 5:03d}` và `BL-{bl_num:04d}`.",
            f"- Quyết định kiến trúc: `ADR-{idx:03d}`, yêu cầu `REQ-{idx:03d}`, cổng kiểm soát `P{idx}-F1` / `G{idx}-C2`.",
            f"- Commit chốt: `{short_sha}` (bản kế tiếp `{short_ellipsis}…`), gói SHA-256: `{sha_full}`.",
            f"- Tệp nguồn POSIX: `src/modules/mod_{idx}/handler.ts`, `tests/unit/mod_{idx}.mjs`, `config/pipeline_{idx}.yml`.",
            f"- Tệp nguồn Windows: `C:\\workspace\\build\\out_{idx}.json`, `D:\\tools\\bin\\worker_{idx}.exe`, `patches\\fix_{idx}.patch`.",
            "```markdown",
            "## Tiêu đề giả bên trong fenced code block không được phép tách nhầm",
            f"verify --milestone {idx} --id BL-{bl_num:04d}",
            "```",
        ]
        # Pad with realistic Vietnamese audit/verification notes so 8 milestones exceed 400 lines
        for note_i in range(1, 46):
            lines.append(
                f"- Ghi chú kiểm định {idx}.{note_i}: Xác nhận luồng dữ liệu đồng bộ UTF-8, "
                f"độ trễ ổn định dưới ngưỡng, không rò rỉ tài nguyên trên Windows/Linux."
            )
        blocks.append("\n".join(lines))

    # Include exact IDs mentioned in brief (TQ-001, TQ-066, TQ-069, BL-0001, BL-0262)
    extra_footer_into_oldest = (
        "\n- Tham chiếu truy vết tổng hợp: `TQ-001`, `TQ-066`, `TQ-069`, `BL-0001`, `BL-0262`, "
        "`RISK-09`, `HYP-04`, `docs/architecture/overview.md`."
    )
    blocks[-1] = blocks[-1] + extra_footer_into_oldest
    return "\n\n".join(blocks) + "\n"


class TestAutoCompactSuite(unittest.TestCase):
    def test_01_measure_transcript_metrics_and_verdict(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            proj = root / "proj"
            other_proj = root / "other_proj"
            gcfg = root / "gemini" / "config"
            runtime_runs = root / "gemini" / "runtime" / "universal-orchestrator" / "proj_abc123" / "runs"
            adir = root / "agents"
            conv = root / "brain" / "conv-123"
            logs = conv / ".system_generated" / "logs"
            proj.mkdir(parents=True)
            other_proj.mkdir(parents=True)
            gcfg.mkdir(parents=True)
            runtime_runs.mkdir(parents=True)
            adir.mkdir(parents=True)
            logs.mkdir(parents=True)

            session_start_iso = "2026-11-26T08:00:00+00:00"
            session_start_ts = 1795680000.0  # 2026-11-26T08:00:00Z

            # 35 old orchestrator runs from previous days -> lifetime only, must NOT trigger CRITICAL
            for idx in range(35):
                rdir = runtime_runs / f"old_run_{idx:02d}"
                rdir.mkdir()
                sfile = rdir / "summary.json"
                sfile.write_text(json.dumps({"project_root": str(proj)}), encoding="utf-8")
                old_ts = session_start_ts - 86400.0 - idx
                os.utime(sfile, (old_ts, old_ts))
                os.utime(rdir, (old_ts, old_ts))

            # 2 recent runs belonging to a DIFFERENT project_root -> must be ignored completely
            for idx in range(2):
                rdir = runtime_runs / f"other_proj_run_{idx}"
                rdir.mkdir()
                sfile = rdir / "summary.json"
                sfile.write_text(json.dumps({"project_root": str(other_proj)}), encoding="utf-8")
                now_ts = session_start_ts + 120.0 + idx
                os.utime(sfile, (now_ts, now_ts))

            # 2 recent runs belonging to THIS project in current session -> counted in session + lifetime
            for idx in range(2):
                rdir = runtime_runs / f"cur_run_{idx}"
                rdir.mkdir()
                sfile = rdir / "summary.json"
                sfile.write_text(json.dumps({"project_root": str(proj)}), encoding="utf-8")
                now_ts = session_start_ts + 300.0 + idx
                os.utime(sfile, (now_ts, now_ts))

            transcript = logs / "transcript.jsonl"
            transcript_full = logs / "transcript_full.jsonl"

            lines = [
                # Step 1: USER_INPUT mentioning "invoke_subagent" -> must NOT count
                json.dumps(
                    {
                        "type": "USER_INPUT",
                        "created_at": session_start_iso,
                        "text": "Please check invoke_subagent usage in transcript.jsonl",
                    }
                ),
                # Step 2: GENERIC step mentioning "invoke_subagent" in file output -> must NOT count
                json.dumps(
                    {
                        "type": "GENERIC",
                        "text": "def invoke_subagent(): pass # grep result",
                        "truncated_fields": ["text"],
                    }
                ),
                # Step 3: Antigravity PLANNER_RESPONSE with 3 parallel invoke_subagent calls + 1 read_file -> counts 3
                json.dumps(
                    {
                        "type": "PLANNER_RESPONSE",
                        "tool_calls": [
                            {"name": "read_file", "arguments": {"Path": "a.py"}},
                            {"name": "invoke_subagent", "arguments": {"Role": "Recon 1"}},
                            {"name": "invoke_subagent", "arguments": {"Role": "Recon 2"}},
                            {"name": "invoke_subagent", "arguments": {"Role": "Recon 3"}},
                        ],
                    }
                ),
                # Step 4: Claude/generic assistant tool_use block -> counts 2
                json.dumps(
                    {
                        "role": "assistant",
                        "content": [
                            {"type": "tool_use", "name": "invoke_subagent", "input": {}},
                            {"type": "tool_call", "name": "invoke_subagent", "input": {}},
                            {"type": "text", "text": "invoke_subagent text should not double count"},
                        ],
                        "truncated_fields": ["output"],
                    }
                ),
                # Step 5: Assistant plain text mentioning invoke_subagent -> must NOT count
                json.dumps({"role": "assistant", "text": "Discussing invoke_subagent behavior"}),
                "not valid json line",
                # Step 6: TOOL_RESULT mentioning invoke_subagent -> must NOT count
                json.dumps({"type": "TOOL_RESULT", "role": "tool", "content": "invoke_subagent returned ok"}),
            ]
            transcript.write_text("\n".join(lines) + "\n", encoding="utf-8")
            transcript_full.write_text("\n".join(lines) * 4 + "\n", encoding="utf-8")

            report = measure_context.measure_context(
                project_dir=proj,
                brain_dir=conv,
                gemini_config_dir=gcfg,
                agents_dir=adir,
            )

            t = report["transcript"]
            self.assertTrue(t["found"])
            self.assertEqual(t["step_count"], 6)
            self.assertEqual(t["invalid_lines"], 1)
            self.assertEqual(t["truncated_steps"], 2)
            self.assertEqual(t["transcript_subagent_dispatches"], 5)
            self.assertEqual(t["session_orchestrator_runs"], 2)
            self.assertEqual(t["lifetime_orchestrator_runs"], 37)
            self.assertEqual(t["subagent_dispatches"], 7)
            self.assertGreater(t["transcript_full_bytes"], t["transcript_bytes"])
            self.assertGreater(t["bytes_per_step"], 0)
            self.assertEqual(t["estimated_tokens"], round(t["transcript_bytes"] / 3.8))
            self.assertEqual(report["verdict"], "HEALTHY")

            # Standalone run with no transcript must NOT trigger CRITICAL from lifetime orchestrator runs
            standalone = measure_context.measure_context(
                project_dir=proj,
                brain_dir=root / "missing-brain",
                gemini_config_dir=gcfg,
                agents_dir=adir,
            )
            self.assertFalse(standalone["transcript"]["found"])
            self.assertEqual(standalone["transcript"]["session_orchestrator_runs"], 0)
            self.assertEqual(standalone["transcript"]["lifetime_orchestrator_runs"], 37)
            self.assertEqual(standalone["transcript"]["subagent_dispatches"], 0)
            self.assertEqual(standalone["verdict"], "HEALTHY")

            # Push session subagent_dispatches >= 30 via PLANNER_RESPONSE -> CRITICAL_ENGINE_COMPACT
            extra_subagents = [
                json.dumps(
                    {
                        "type": "PLANNER_RESPONSE",
                        "tool_calls": [
                            {"name": "invoke_subagent"},
                            {"name": "invoke_subagent"},
                            {"name": "invoke_subagent"},
                        ],
                    }
                )
                for _ in range(8)  # +24 => 5 + 24 + 2 session orch = 31 >= 30
            ]
            transcript.write_text("\n".join(lines + extra_subagents) + "\n", encoding="utf-8")
            crit_report = measure_context.measure_context(
                project_dir=proj,
                brain_dir=transcript,
                gemini_config_dir=gcfg,
                agents_dir=adir,
            )
            self.assertEqual(crit_report["transcript"]["transcript_subagent_dispatches"], 29)
            self.assertEqual(crit_report["transcript"]["subagent_dispatches"], 31)
            self.assertEqual(crit_report["verdict"], "CRITICAL_ENGINE_COMPACT")

    def test_02_measure_rule_tax_duplicates_merged_stub_and_overlap(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            proj = root / "proj"
            gcfg = root / "gcfg"
            rules = gcfg / "rules"
            adir = root / "agents"
            proj.mkdir(parents=True)
            rules.mkdir(parents=True)
            adir.mkdir(parents=True)

            shared_global = (
                "# GLOBAL RULES\n\n"
                "## Watchdog 15m Protocol\n"
                "See full details in WATCHDOG_HEARTBEAT_RULE.md for timer policy.\n"
            )
            (gcfg / "GEMINI.md").write_text(shared_global, encoding="utf-8")
            (gcfg / "AGENTS.md").write_text(shared_global + "\n   \n", encoding="utf-8")

            (rules / "HEARTBEAT_15M_RULE.md").write_text(
                "---\ntrigger: always_on\n---\n# Heartbeat\nQuy tắc này đã hợp nhất đầy đủ vào WATCHDOG_HEARTBEAT_RULE.md.\n",
                encoding="utf-8",
            )
            (rules / "WATCHDOG_HEARTBEAT_RULE.md").write_text(
                "---\ntrigger: always_on\n---\n# Watchdog\n## Watchdog 15m Protocol\nDetailed watchdog instructions.\n",
                encoding="utf-8",
            )
            (rules / "OPTIONAL_MANUAL.md").write_text(
                "---\ntrigger: manual\n---\n# Manual rule\nLoaded only when requested.\n",
                encoding="utf-8",
            )

            report = measure_context.measure_context(
                project_dir=proj,
                brain_dir=root / "no-brain",
                gemini_config_dir=gcfg,
                agents_dir=adir,
            )

            rt = report["rule_tax"]
            self.assertEqual(rt["always_on_file_count"], 4)
            kinds = {f["kind"] for f in rt["rule_waste_findings"]}
            self.assertIn("duplicate_content", kinds)
            self.assertIn("redundant_merged_stub", kinds)
            self.assertIn("global_rule_topic_overlap", kinds)
            self.assertEqual(report["verdict"], "WARN_COMPACT_LEDGER")

    def test_03_measure_hot_ledgers_and_cli_json_out(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            proj = root / "proj"
            (proj / "plan" / "ops").mkdir(parents=True)
            (proj / "CHECKPOINT.md").write_text(_make_vietnamese_checkpoint(6), encoding="utf-8")
            (proj / "HANDOFF.md").write_text("# Handoff\n## Active\n- Item 1\n", encoding="utf-8")
            (proj / "plan" / "ops" / "runbook.md").write_text("# Runbook\n- Step 1\n", encoding="utf-8")

            out_file = root / "metrics.json"
            proc = _run_py(
                MEASURE_SCRIPT,
                [
                    "--project-dir",
                    str(proj),
                    "--brain-dir",
                    str(root / "absent"),
                    "--gemini-config-dir",
                    str(root / "empty-gcfg"),
                    "--agents-dir",
                    str(root / "empty-agents"),
                    "--json",
                    "--out",
                    str(out_file),
                ],
            )
            self.assertEqual(proc.returncode, 0, msg=proc.stderr)
            payload = json.loads(proc.stdout)
            self.assertTrue(out_file.is_file())
            self.assertEqual(json.loads(out_file.read_text(encoding="utf-8"))["verdict"], "WARN_COMPACT_LEDGER")
            hl = payload["hot_ledgers"]
            self.assertTrue(hl["checkpoint_exists"])
            self.assertEqual(hl["checkpoint_h2_count"], 6)
            self.assertGreater(hl["checkpoint_lines"], 150)
            self.assertGreater(hl["estimated_checkpoint_savings_bytes"], 1000)
            self.assertEqual(len(hl["files"]), 3)

    def test_04_compact_vietnamese_checkpoint_over_400_lines_zero_drop(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            proj = Path(tmp)
            cp = proj / "CHECKPOINT.md"
            original = _make_vietnamese_checkpoint(8)
            orig_lines = len(original.splitlines())
            self.assertGreater(orig_lines, 400)
            cp.write_text(original, encoding="utf-8")

            proc = _run_py(
                COMPACT_SCRIPT,
                ["--project-dir", str(proj), "--keep-latest", "1", "--json"],
            )
            self.assertEqual(proc.returncode, 0, msg=proc.stderr)
            res = json.loads(proc.stdout)
            self.assertTrue(res["zero_drop_verified"])

            hot_text = cp.read_text(encoding="utf-8")
            hot_lines = len(hot_text.splitlines())
            self.assertLess(hot_lines, 90, msg=f"Hot CHECKPOINT.md has {hot_lines} lines, expected < 90")
            self.assertIn("<!-- AUTO-COMPACT-ARCHIVE-POINTER -->", hot_text)
            self.assertIn("<!-- /AUTO-COMPACT-ARCHIVE-POINTER -->", hot_text)
            self.assertIn("## Mốc 8:", hot_text)
            self.assertNotIn("## Mốc 7:", hot_text)

            archive_path = proj / ".context-compact" / "archive" / "checkpoint-archive.md"
            state_path = proj / ".context-compact" / "STATE.json"
            packet_path = proj / ".context-compact" / "COMPACT_PACKET.md"
            self.assertTrue(archive_path.is_file())
            self.assertTrue(state_path.is_file())
            self.assertTrue(packet_path.is_file())

            packet_lines = len(packet_path.read_text(encoding="utf-8").splitlines())
            self.assertLess(packet_lines, 80)

            state = json.loads(state_path.read_text(encoding="utf-8"))
            self.assertTrue(state["entities"]["zero_drop_verified"])
            self.assertEqual(state["checkpoint"]["archived_milestone_count"], 7)
            self.assertGreater(state["metrics"]["reduction_pct"], 70.0)

            # Independent entity check across hot + archive
            archive_text = archive_path.read_text(encoding="utf-8")
            ver = compact_ledger.verify_zero_drop(original, hot_text + "\n" + archive_text)
            self.assertTrue(ver["verified"], msg=str(ver["missing"]))
            self.assertGreaterEqual(ver["counts"]["sha_hashes"], 16)
            self.assertGreaterEqual(ver["counts"]["domain_ids"], 30)
            self.assertGreaterEqual(ver["counts"]["code_paths"], 20)

    def test_05_compact_idempotent_multiple_runs(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            proj = Path(tmp)
            cp = proj / "CHECKPOINT.md"
            cp.write_text(_make_vietnamese_checkpoint(5), encoding="utf-8")

            r1 = compact_ledger.compact_checkpoint(proj, keep_latest=1)
            cp_after_1 = cp.read_text(encoding="utf-8")
            arch_path = proj / ".context-compact" / "archive" / "checkpoint-archive.md"
            arch_after_1 = arch_path.read_text(encoding="utf-8")

            r2 = compact_ledger.compact_checkpoint(proj, keep_latest=1)
            r3 = compact_ledger.compact_checkpoint(proj, keep_latest=1)

            self.assertEqual(cp.read_text(encoding="utf-8"), cp_after_1)
            self.assertEqual(arch_path.read_text(encoding="utf-8"), arch_after_1)
            self.assertEqual(r1["state"]["archive"]["sha256"], r2["state"]["archive"]["sha256"])
            self.assertEqual(r2["state"]["archive"]["sha256"], r3["state"]["archive"]["sha256"])
            self.assertEqual(r3["state"]["checkpoint"]["archived_milestone_count"], 4)

    def test_06_incremental_new_milestone_moves_previous_hot_to_cold(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            proj = Path(tmp)
            cp = proj / "CHECKPOINT.md"
            initial = _make_vietnamese_checkpoint(5)
            cp.write_text(initial, encoding="utf-8")

            res1 = compact_ledger.compact_checkpoint(proj, keep_latest=1)
            self.assertEqual(res1["state"]["checkpoint"]["archived_milestone_count"], 4)
            sha1 = res1["state"]["archive"]["sha256"]

            # Prepend Mốc 6 into hot CHECKPOINT.md right before Mốc 5
            hot_v1 = cp.read_text(encoding="utf-8")
            sha_m6 = hashlib.sha256(b"milestone-6-new-release").hexdigest()
            m6_block = (
                "## Mốc 6: Triển khai cổng kiểm toán phòng sạch\n"
                f"- Chốt mã `TQ-099`, `BL-0300`, commit `{sha_m6[:10]}`, SHA-256 `{sha_m6}`.\n"
                "- Tệp: `src/audit/gate.py`.\n\n"
            )
            marker = "## Mốc 5:"
            self.assertIn(marker, hot_v1)
            cp.write_text(hot_v1.replace(marker, m6_block + marker, 1), encoding="utf-8")

            res2 = compact_ledger.compact_checkpoint(proj, keep_latest=1)
            self.assertTrue(res2["zero_drop_verified"])
            self.assertEqual(res2["state"]["checkpoint"]["archived_milestone_count"], 5)
            self.assertNotEqual(res2["state"]["archive"]["sha256"], sha1)

            hot_v2 = cp.read_text(encoding="utf-8")
            arch_v2 = (proj / ".context-compact" / "archive" / "checkpoint-archive.md").read_text(encoding="utf-8")
            self.assertIn("## Mốc 6:", hot_v2)
            self.assertNotIn("## Mốc 5:", hot_v2)
            # All Mốc 1..5 must be in archive in descending order (Mốc 5 above Mốc 4)
            for idx in range(1, 6):
                self.assertIn(f"## Mốc {idx}:", arch_v2)
            self.assertLess(arch_v2.index("## Mốc 5:"), arch_v2.index("## Mốc 4:"))

            # Every entity from initial + Mốc 6 preserved
            ver = compact_ledger.verify_zero_drop(initial + "\n" + m6_block, hot_v2 + "\n" + arch_v2)
            self.assertTrue(ver["verified"], msg=str(ver["missing"]))

    def test_07_verify_zero_drop_fail_closed_detects_missing_id_sha_path(self) -> None:
        sha = "a" * 64
        source = (
            "## Mốc 1\n"
            f"- ID `TQ-066` và `BL-0262`, SHA `{sha}`, short `deadbeef01`, path `src/core/main.py`.\n"
        )
        dropped_id = source.replace("TQ-066", "TQ-999")
        v1 = compact_ledger.verify_zero_drop(source, dropped_id)
        self.assertFalse(v1["verified"])
        self.assertIn("TQ-066", v1["missing"]["domain_ids"])

        dropped_sha = source.replace(sha, "")
        v2 = compact_ledger.verify_zero_drop(source, dropped_sha)
        self.assertFalse(v2["verified"])
        self.assertIn(sha, v2["missing"]["sha_hashes"])

        dropped_path = source.replace("src/core/main.py", "src/core/other.py")
        v3 = compact_ledger.verify_zero_drop(source, dropped_path)
        self.assertFalse(v3["verified"])
        self.assertIn("src/core/main.py", v3["missing"]["code_paths"])

    def test_08_compact_fail_closed_exit_2_does_not_overwrite_checkpoint(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            proj = Path(tmp)
            cp = proj / "CHECKPOINT.md"
            original = _make_vietnamese_checkpoint(3)
            cp.write_text(original, encoding="utf-8")

            # Compact once, then tamper with archive to trigger fail-closed exit 2 on --verify-only
            proc_ok = _run_py(COMPACT_SCRIPT, ["--project-dir", str(proj), "--keep-latest", "1"])
            self.assertEqual(proc_ok.returncode, 0, msg=proc_ok.stderr)
            hot_before = cp.read_text(encoding="utf-8")

            arch_path = proj / ".context-compact" / "archive" / "checkpoint-archive.md"
            arch_path.write_text(
                arch_path.read_text(encoding="utf-8").replace("TQ-066", "REDACTED"),
                encoding="utf-8",
            )

            proc_fail = _run_py(COMPACT_SCRIPT, ["--project-dir", str(proj), "--verify-only", "--json"])
            self.assertEqual(proc_fail.returncode, 2)
            self.assertIn("ERROR [EXIT 2]", proc_fail.stderr)
            # CHECKPOINT.md must remain untouched
            self.assertEqual(cp.read_text(encoding="utf-8"), hot_before)

    def test_09_dry_run_and_verify_only_never_modify_disk(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            proj = Path(tmp)
            cp = proj / "CHECKPOINT.md"
            original = _make_vietnamese_checkpoint(4)
            cp.write_text(original, encoding="utf-8")

            proc_dry = _run_py(
                COMPACT_SCRIPT,
                ["--project-dir", str(proj), "--dry-run", "--json"],
            )
            self.assertEqual(proc_dry.returncode, 0, msg=proc_dry.stderr)
            dry_data = json.loads(proc_dry.stdout)
            self.assertTrue(dry_data["dry_run"])
            self.assertTrue(dry_data["zero_drop_verified"])
            self.assertEqual(cp.read_text(encoding="utf-8"), original)
            self.assertFalse((proj / ".context-compact").exists())

            # Now compact for real, then verify-only must not touch timestamps/files
            proc_run = _run_py(COMPACT_SCRIPT, ["--project-dir", str(proj), "--json"])
            self.assertEqual(proc_run.returncode, 0, msg=proc_run.stderr)

            cp_mtime_before = cp.stat().st_mtime_ns
            state_path = proj / ".context-compact" / "STATE.json"
            state_mtime_before = state_path.stat().st_mtime_ns

            time.sleep(0.02)
            proc_ver = _run_py(COMPACT_SCRIPT, ["--project-dir", str(proj), "--verify-only", "--json"])
            self.assertEqual(proc_ver.returncode, 0, msg=proc_ver.stderr)
            self.assertEqual(cp.stat().st_mtime_ns, cp_mtime_before)
            self.assertEqual(state_path.stat().st_mtime_ns, state_mtime_before)

    def test_10_dedup_same_heading_merges_without_dropping_lines(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            proj = Path(tmp)
            cp = proj / "CHECKPOINT.md"
            cp.write_text(
                "# Header\n\n"
                "## Mốc 2: Hiện tại\n- Mới `TQ-020`.\n\n"
                "## Mốc 1: Khởi tạo\n- Dòng cũ `TQ-010` và `BL-0010`.\n",
                encoding="utf-8",
            )
            compact_ledger.compact_checkpoint(proj, keep_latest=1)

            # Re-introduce Mốc 1 into CHECKPOINT.md with an updated extra line
            hot = cp.read_text(encoding="utf-8")
            updated_m1 = "## Mốc 1: Khởi tạo\n- Dòng bổ sung `TQ-011` và `src/init/setup.py`.\n\n"
            cp.write_text(hot.replace("## 🗄", updated_m1 + "## 🗄"), encoding="utf-8")

            res = compact_ledger.compact_checkpoint(proj, keep_latest=1)
            self.assertTrue(res["zero_drop_verified"])
            arch = (proj / ".context-compact" / "archive" / "checkpoint-archive.md").read_text(encoding="utf-8")
            self.assertEqual(arch.count("## Mốc 1: Khởi tạo"), 1)
            self.assertIn("TQ-010", arch)
            self.assertIn("BL-0010", arch)
            self.assertIn("TQ-011", arch)
            self.assertIn("src/init/setup.py", arch)

    def test_11_install_script_safe_copy_verify_and_forbidden_block(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "installed-auto-compact"

            # Dry-run writes nothing
            proc_dry = _run_py(INSTALL_SCRIPT, ["--target-dir", str(target), "--dry-run"])
            self.assertEqual(proc_dry.returncode, 0, msg=proc_dry.stderr)
            self.assertFalse(target.exists())

            # Real install
            proc_inst = _run_py(INSTALL_SCRIPT, ["--target-dir", str(target)])
            self.assertEqual(proc_inst.returncode, 0, msg=proc_inst.stderr)
            self.assertTrue((target / "SKILL.md").is_file())
            self.assertTrue((target / "references" / "ledger-schema.md").is_file())
            self.assertTrue((target / "scripts" / "measure_context.py").is_file())
            self.assertTrue((target / "scripts" / "compact_ledger.py").is_file())
            self.assertFalse((target / "README.md").exists())
            self.assertFalse((target / "CHANGELOG.md").exists())
            self.assertEqual(installer.scan_forbidden_in_target(target), [])

            # Verify passes
            proc_ver = _run_py(INSTALL_SCRIPT, ["--target-dir", str(target), "--verify"])
            self.assertEqual(proc_ver.returncode, 0, msg=proc_ver.stderr)

            # Inject forbidden README.md into target -> verify must fail (exit 2)
            (target / "README.md").write_text("forbidden", encoding="utf-8")
            proc_bad = _run_py(INSTALL_SCRIPT, ["--target-dir", str(target), "--verify"])
            self.assertEqual(proc_bad.returncode, 2)

    def test_12_skill_doc_budgets_and_brain_auto_discovery(self) -> None:
        skill_md = (SKILL_DIR / "SKILL.md").read_text(encoding="utf-8")
        schema_md = (SKILL_DIR / "references" / "ledger-schema.md").read_text(encoding="utf-8")

        self.assertLess(len(skill_md.splitlines()), 180)
        self.assertIn("name: auto-compact", skill_md)
        self.assertLess(len(schema_md.splitlines()), 100)
        self.assertFalse((SKILL_DIR / "README.md").exists())
        self.assertFalse((SKILL_DIR / "CHANGELOG.md").exists())

        # Verify brain root auto-discovery selects the newest conversation transcript
        with tempfile.TemporaryDirectory() as tmp:
            brain_root = Path(tmp) / "brain"
            old_log = brain_root / "conv-old" / ".system_generated" / "logs" / "transcript.jsonl"
            new_log = brain_root / "conv-new" / ".system_generated" / "logs" / "transcript.jsonl"
            old_log.parent.mkdir(parents=True)
            new_log.parent.mkdir(parents=True)
            old_log.write_text('{"step": 1}\n', encoding="utf-8")
            os.utime(old_log, (1_700_000_000, 1_700_000_000))
            new_log.write_text('{"step": 1}\n{"step": 2}\n', encoding="utf-8")
            os.utime(new_log, (1_700_000_500, 1_700_000_500))

            found = measure_context.discover_transcript(brain_root)
            self.assertEqual(found["transcript_path"], new_log)

    def test_13_compact_write_gate_fail_closed_preserves_original_checkpoint(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            proj = Path(tmp)
            cp = proj / "CHECKPOINT.md"
            original = _make_vietnamese_checkpoint(3)
            cp.write_text(original, encoding="utf-8")

            orig_render = compact_ledger.render_archive

            def _buggy_render(sections, extra_preamble=""):
                return orig_render(sections, extra_preamble).replace("TQ-066", "DROPPED")

            err_buf = io.StringIO()
            with mock.patch.object(compact_ledger, "render_archive", side_effect=_buggy_render):
                with contextlib.redirect_stderr(err_buf):
                    rc = compact_ledger.main(["--project-dir", str(proj), "--keep-latest", "1"])

            self.assertEqual(rc, 2)
            self.assertIn("TQ-066", err_buf.getvalue())
            # Fail-closed guarantee: original CHECKPOINT.md untouched and no archive written
            self.assertEqual(cp.read_text(encoding="utf-8"), original)
            self.assertFalse((proj / ".context-compact" / "archive" / "checkpoint-archive.md").exists())

    def test_14_f1_trailing_append_after_pointer_preserved_in_hot_checkpoint(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            proj = Path(tmp)
            cp = proj / "CHECKPOINT.md"
            cp.write_text(_make_vietnamese_checkpoint(3), encoding="utf-8")

            r1 = compact_ledger.compact_checkpoint(proj, keep_latest=1)
            self.assertTrue(r1["zero_drop_verified"])
            hot_v1 = cp.read_text(encoding="utf-8")
            self.assertIn("<!-- AUTO-COMPACT-ARCHIVE-POINTER -->", hot_v1)
            self.assertIn("<!-- /AUTO-COMPACT-ARCHIVE-POINTER -->", hot_v1)

            # Append urgent notes after closing pointer tag (simulating `>> CHECKPOINT.md` / Add-Content)
            appended = (
                "\n### Ghi chú khẩn cuối phiên\n"
                "- Chốt `TQ-999`, commit `deadbeef01`, tệp `src/hotfix/urgent.ts`.\n"
            )
            cp.write_text(hot_v1 + appended, encoding="utf-8")

            r2 = compact_ledger.compact_checkpoint(proj, keep_latest=1)
            self.assertTrue(r2["zero_drop_verified"])
            hot_v2 = cp.read_text(encoding="utf-8")
            arch_v2 = (proj / ".context-compact" / "archive" / "checkpoint-archive.md").read_text(encoding="utf-8")

            self.assertIn("## 📌 Ghi chú bổ sung cuối phiên", hot_v2)
            self.assertIn("TQ-999", hot_v2)
            self.assertIn("deadbeef01", hot_v2)
            self.assertIn("src/hotfix/urgent.ts", hot_v2)
            # Previous hot milestone (Mốc 3) safely archived
            self.assertIn("## Mốc 3:", arch_v2)
            # Re-running compaction is idempotent
            r3 = compact_ledger.compact_checkpoint(proj, keep_latest=1)
            self.assertTrue(r3["zero_drop_verified"])
            self.assertEqual(cp.read_text(encoding="utf-8"), hot_v2)

            # Backward-compatibility: legacy pointer without closing tag + appended notes
            legacy_proj = Path(tmp) / "legacy"
            legacy_proj.mkdir()
            legacy_cp = legacy_proj / "CHECKPOINT.md"
            legacy_arch = legacy_proj / ".context-compact" / "archive" / "checkpoint-archive.md"
            legacy_arch.parent.mkdir(parents=True)
            legacy_arch_text = (
                f"{compact_ledger.ARCHIVE_HEADER}\n\n"
                "## Mốc 1: Cũ\n- Chốt `TQ-101`.\n"
            )
            legacy_arch.write_text(legacy_arch_text, encoding="utf-8")
            legacy_sha = compact_ledger.sha256_text(legacy_arch_text)
            legacy_cp.write_text(
                "# Header\n\n"
                "## Mốc 2: Nóng\n- Chốt `TQ-102`.\n\n"
                "## 🗄 Kho lưu trữ lịch sử nén\n"
                "<!-- AUTO-COMPACT-ARCHIVE-POINTER -->\n"
                "- **Cold Archive**: `.context-compact/archive/checkpoint-archive.md`\n"
                f"- **Archive SHA-256**: `{legacy_sha}`\n"
                "- **Archived Milestones (1)**:\n"
                "  - `## Mốc 1: Cũ`\n\n"
                "### Ghi chú khẩn cuối phiên\n"
                "- Chốt `TQ-998`, commit `cafebabe12`, tệp `src/hotfix/legacy.ts`.\n",
                encoding="utf-8",
            )
            r_legacy = compact_ledger.compact_checkpoint(legacy_proj, keep_latest=1)
            self.assertTrue(r_legacy["zero_drop_verified"])
            legacy_hot = legacy_cp.read_text(encoding="utf-8")
            self.assertIn("TQ-998", legacy_hot)
            self.assertIn("cafebabe12", legacy_hot)
            self.assertIn("src/hotfix/legacy.ts", legacy_hot)
            self.assertIn("<!-- /AUTO-COMPACT-ARCHIVE-POINTER -->", legacy_hot)

    def test_15_f1_verify_only_detects_hot_entity_deletion_and_missing_state_integrity(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            proj = Path(tmp)
            cp = proj / "CHECKPOINT.md"
            cp.write_text(_make_vietnamese_checkpoint(3), encoding="utf-8")

            r1 = compact_ledger.compact_checkpoint(proj, keep_latest=1)
            self.assertTrue(r1["zero_drop_verified"])
            state_path = proj / ".context-compact" / "STATE.json"
            arch_path = proj / ".context-compact" / "archive" / "checkpoint-archive.md"

            state = json.loads(state_path.read_text(encoding="utf-8"))
            self.assertIn("verified_entities", state)
            self.assertIn("TQ-017", state["verified_entities"]["domain_ids"])

            # Case A: Secretly delete a TQ-* ID from hot CHECKPOINT.md -> --verify-only must exit 2
            hot_orig = cp.read_text(encoding="utf-8")
            self.assertIn("TQ-017", hot_orig)
            cp.write_text(hot_orig.replace("TQ-017", "REMOVED"), encoding="utf-8")
            proc_hot_drop = _run_py(COMPACT_SCRIPT, ["--project-dir", str(proj), "--verify-only", "--json"])
            self.assertEqual(proc_hot_drop.returncode, 2)
            payload_drop = json.loads(proc_hot_drop.stdout)
            self.assertFalse(payload_drop["zero_drop_verified"])
            self.assertIn("TQ-017", payload_drop["details"]["verification"]["missing"]["domain_ids"])

            # Restore hot CHECKPOINT.md and remove STATE.json to test pointer-only fail-closed checks
            cp.write_text(hot_orig, encoding="utf-8")
            state_path.unlink()
            # With intact archive and pointer SHA, verify-only passes even without STATE.json
            proc_no_state_ok = _run_py(COMPACT_SCRIPT, ["--project-dir", str(proj), "--verify-only", "--json"])
            self.assertEqual(proc_no_state_ok.returncode, 0, msg=proc_no_state_ok.stderr)

            # Case B: Without STATE.json, tamper with cold archive -> exit 2
            arch_orig = arch_path.read_text(encoding="utf-8")
            arch_path.write_text(arch_orig + "\n<!-- tampered -->\n", encoding="utf-8")
            proc_tampered = _run_py(COMPACT_SCRIPT, ["--project-dir", str(proj), "--verify-only", "--json"])
            self.assertEqual(proc_tampered.returncode, 2)

            # Case C: Without STATE.json, delete cold archive -> exit 2
            arch_path.unlink()
            proc_missing_arch = _run_py(COMPACT_SCRIPT, ["--project-dir", str(proj), "--verify-only", "--json"])
            self.assertEqual(proc_missing_arch.returncode, 2)

            # Case D: Restore archive, delete Archive SHA-256 line from pointer block -> exit 2
            arch_path.write_text(arch_orig, encoding="utf-8")
            hot_no_sha = "\n".join(
                ln for ln in hot_orig.splitlines() if "Archive SHA-256" not in ln
            ) + "\n"
            cp.write_text(hot_no_sha, encoding="utf-8")
            proc_no_sha = _run_py(COMPACT_SCRIPT, ["--project-dir", str(proj), "--verify-only", "--json"])
            self.assertEqual(proc_no_sha.returncode, 2)

    def test_16_f2_duplicate_heading_with_distinct_fenced_blocks_preserves_fences_and_older_milestone(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            proj = Path(tmp)
            cp = proj / "CHECKPOINT.md"
            cp.write_text(
                "# Header\n\n"
                "## Mốc 2: Hiện tại\n- Trạng thái `TQ-200`.\n\n"
                "## Mốc 1: Trùng tiêu đề\n"
                "- Phần Python `TQ-101`:\n"
                "```python\n"
                "print('hello')\n"
                "```\n\n"
                "## Mốc 1: Trùng tiêu đề\n"
                "- Phần Bash `TQ-102`:\n"
                "```bash\n"
                "echo 'world'\n"
                "```\n\n"
                "## Mốc 0: Older\n"
                "- Dữ liệu lịch sử `TQ-000` và `src/legacy/core.py`.\n",
                encoding="utf-8",
            )

            # Pass 1: merges the two duplicate `## Mốc 1: Trùng tiêu đề` blocks ahead of `## Mốc 0: Older`
            r1 = compact_ledger.compact_checkpoint(proj, keep_latest=1)
            self.assertTrue(r1["zero_drop_verified"])
            arch_path = proj / ".context-compact" / "archive" / "checkpoint-archive.md"
            arch_1 = arch_path.read_text(encoding="utf-8")
            self.assertIn("```python\nprint('hello')\n```", arch_1)
            self.assertIn("```bash\necho 'world'\n```", arch_1)
            self.assertIn("## Mốc 0: Older", arch_1)
            self.assertEqual(r1["state"]["checkpoint"]["archived_milestone_count"], 2)

            # Pass 2: prepend Mốc 3 and re-compact; Mốc 0: Older MUST NOT be swallowed by an unclosed fence
            hot_1 = cp.read_text(encoding="utf-8")
            m3 = "## Mốc 3: Kế tiếp\n- Chốt `TQ-300`.\n\n"
            cp.write_text(hot_1.replace("## Mốc 2:", m3 + "## Mốc 2:", 1), encoding="utf-8")

            r2 = compact_ledger.compact_checkpoint(proj, keep_latest=1)
            self.assertTrue(r2["zero_drop_verified"])
            arch_2 = arch_path.read_text(encoding="utf-8")
            _, arch_sections = compact_ledger.parse_markdown_sections(arch_2)
            headings = [s["heading"] for s in arch_sections]
            self.assertIn("## Mốc 2: Hiện tại", headings)
            self.assertIn("## Mốc 1: Trùng tiêu đề", headings)
            self.assertIn("## Mốc 0: Older", headings)
            self.assertEqual(len(headings), 3)
            self.assertIn("TQ-000", arch_2)
            self.assertIn("src/legacy/core.py", arch_2)

    def test_17_m1_m2_m3_gitignore_bom_agents_dir_and_inline_triple_backticks(self) -> None:
        # M1: .gitignore must NOT start with UTF-8 BOM
        gitignore_bytes = (REPO_ROOT / ".gitignore").read_bytes()
        self.assertFalse(gitignore_bytes.startswith(b"\xef\xbb\xbf"), msg=".gitignore must not have UTF-8 BOM")

        # M2: install.py --also-agents --agents-dir <custom>
        with tempfile.TemporaryDirectory() as tmp:
            primary_target = Path(tmp) / "primary-auto-compact"
            custom_agents_base = Path(tmp) / "custom-agents-skills"
            expected_agents_target = custom_agents_base / "auto-compact"

            proc_inst = _run_py(
                INSTALL_SCRIPT,
                [
                    "--target-dir",
                    str(primary_target),
                    "--also-agents",
                    "--agents-dir",
                    str(custom_agents_base),
                ],
            )
            self.assertEqual(proc_inst.returncode, 0, msg=proc_inst.stderr)
            self.assertTrue((primary_target / "SKILL.md").is_file())
            self.assertTrue((expected_agents_target / "SKILL.md").is_file())
            self.assertTrue((expected_agents_target / "scripts" / "compact_ledger.py").is_file())

            proc_ver = _run_py(
                INSTALL_SCRIPT,
                [
                    "--target-dir",
                    str(primary_target),
                    "--also-agents",
                    "--agents-dir",
                    str(custom_agents_base),
                    "--verify",
                ],
            )
            self.assertEqual(proc_ver.returncode, 0, msg=proc_ver.stderr)

        # M3: Inline triple-backtick code ```inline``` must NOT open a fenced code block
        md_with_inline = (
            "# Header\n\n"
            "## Mốc 2: Có inline triple backtick\n"
            "```inline``` không phải dòng mở fence.\n"
            "- Ghi chú `TQ-502`.\n\n"
            "## Mốc 1: Phải được tách riêng\n"
            "- Ghi chú `TQ-501`.\n"
        )
        _, sections = compact_ledger.parse_markdown_sections(md_with_inline)
        self.assertEqual([s["heading"] for s in sections], ["## Mốc 2: Có inline triple backtick", "## Mốc 1: Phải được tách riêng"])

        _, est_milestones = measure_context._split_h2_for_estimate(md_with_inline)
        self.assertEqual(len(est_milestones), 2)


if __name__ == "__main__":
    unittest.main()

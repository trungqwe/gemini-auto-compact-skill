# `gemini-auto-compact-skill` (`auto-compact`)

**Tự đo áp lực ngữ cảnh & Tự nén sổ trạng thái không mất mát cho AI Agent**  
*Autonomous Context Pressure Telemetry & Lossless Working-Set Compaction Skill for Antigravity IDE, Gemini CLI, Claude Code, and Codex.*

---

## 1. Tổng Quan / Overview

Trong các phiên làm việc dài, AI Agent thường phải đọc lặp lại `CHECKPOINT.md` hàng trăm dòng, gánh thuế quy tắc `always_on` trùng lặp, và chạm ngưỡng kích thước transcript (`>= 5 MB` hoặc `>= 2000` bước) dẫn tới phình token hoặc mất chi tiết kỹ thuật khi nén.

`auto-compact` giải quyết triệt để vấn đề này bằng hai công cụ Python thuần thư viện chuẩn (`>= 3.10`, UTF-8, ghi nguyên tử `os.replace`, không dùng lệnh xoá đệ quy):
- Tiết kiệm **70%–90%** token đọc lặp trên mỗi lượt làm việc.
- **Bảo toàn 100%** thực thể kỹ thuật bất biến (SHA-256, commit SHA ngắn, mã `TQ-*`/`BL-*`/`ADR-*`/`REQ-*`/`P*-F*`/`G*-C*`, đường dẫn tệp) thông qua cổng kiểm chứng **fail-closed (`exit 2`)**.

---

## 2. Kiến Trúc 2 Tầng Trung Thực / Two-Tier Architecture

```text
┌─────────────────────────────────────────────────────────────────────────┐
│ TIER 1 — In-Session Autonomous Compaction (runs directly inside chat)   │
│  1. measure_context.py  -> Transcript MB/steps + Rule Tax + Hot Ledgers │
│  2. compact_ledger.py   -> Split CHECKPOINT.md (Hot <= 120 lines)       │
│                            + Cold Archive (.context-compact/archive/)   │
│                            + Fail-Closed Zero-Drop Gate (exit 2)        │
│                            + STATE.json & COMPACT_PACKET.md (<80 lines) │
│  3. Agent keeps working in-session from COMPACT_PACKET.md / STATE.json  │
└───────────────────────────────────┬─────────────────────────────────────┘
                                    │ When CRITICAL_ENGINE_COMPACT
                                    ▼
┌─────────────────────────────────────────────────────────────────────────┐
│ TIER 2 — Engine Context Compaction Gate (Pre-/compact Safety Barrier)   │
│  • /compact is a UI-side slash command (agents cannot invoke it directly│
│    via tools on Antigravity IDE/CLI).                                   │
│  • Agent ALWAYS executes Tier 1 first so <CONTEXT_SUMMARY> or the next  │
│    session inherits 100% verified state, then recommends `/compact`.    │
└─────────────────────────────────────────────────────────────────────────┘
```

### Ngưỡng Phân Hạng / Threshold Verdicts

| Verdict | Điều kiện / Trigger Conditions | Hành động / Action |
| --- | --- | --- |
| `HEALTHY` | Dưới mọi ngưỡng cảnh báo | Tiếp tục làm việc bình thường |
| `WARN_COMPACT_LEDGER` | `transcript >= 2.0 MB` \| `steps >= 800` \| `CHECKPOINT.md > 150` dòng \| có `rule_waste_findings` | Chạy Tầng 1 ngay trong phiên |
| `CRITICAL_ENGINE_COMPACT` | `transcript >= 5.0 MB` \| `steps >= 2000` \| `subagents >= 30` | Chạy Tầng 1 chốt trạng thái trước, rồi đề xuất `/compact` |

---

## 3. Cài Đặt / Installation

Yêu cầu Python `>= 3.10` (không cần cài gói ngoài).

```powershell
# Cài mặc định vào ~/.gemini/config/skills/auto-compact
python install.py

# Cài đồng thời vào ~/.agents/skills/auto-compact (hoặc thư mục tuỳ chọn với --agents-dir)
python install.py --also-agents
python install.py --also-agents --agents-dir "D:\custom-agents\skills"

# Kiểm tra trước mà không ghi tệp (dry-run)
python install.py --dry-run

# Xác minh SHA-256 và không có tệp rác/cấm tại thư mục đích
python install.py --verify
```

> `install.py` tuân thủ nguyên tắc an toàn: **không bao giờ dùng `shutil.rmtree`**, chỉ ghi nguyên tử từng tệp và chặn tuyệt đối các tệp cấm (`README.md`, `CHANGELOG.md`, `__pycache__`, `*.pyc`) lọt vào thư mục skill.

---

## 4. Hướng Dẫn CLI / CLI Usage

### 4.1 Đo áp lực ngữ cảnh (`measure_context.py`)

```powershell
python skills/auto-compact/scripts/measure_context.py --project-dir .
python skills/auto-compact/scripts/measure_context.py --project-dir . --json --out .context-compact/metrics.json
```

Tham số hỗ trợ:
- `--project-dir`: thư mục dự án (mặc định `.`).
- `--brain-dir`: đường dẫn hội thoại, thư mục gốc `brain/`, hoặc trỏ thẳng `transcript.jsonl` (tự động tìm hội thoại mới nhất trong `~/.gemini/antigravity/brain/*` nếu bỏ trống).
- `--gemini-config-dir`: đường dẫn `~/.gemini/config`.
- `--agents-dir`: đường dẫn `~/.agents`.
- `--json`, `--out <file>`.

### 4.2 Nén sổ trạng thái bất biến (`compact_ledger.py`)

```powershell
# Mô phỏng và chạy kiểm chứng không mất mát (không ghi tệp)
python skills/auto-compact/scripts/compact_ledger.py --project-dir . --dry-run --json

# Tách nóng/lạnh: giữ 1 mốc mới nhất trong CHECKPOINT.md, đẩy lịch sử cũ vào kho lạnh
python skills/auto-compact/scripts/compact_ledger.py --project-dir . --keep-latest 1 --json

# Kiểm chứng toàn vẹn SHA-256 kho lạnh & thực thể (không sửa tệp)
python skills/auto-compact/scripts/compact_ledger.py --project-dir . --verify-only --json
```

---

## 5. Cổng Kiểm Chứng Không Mất Mát / Zero-Drop Entity Verification Gate

Trước khi ghi đè `CHECKPOINT.md`, `compact_ledger.py` trích xuất toàn bộ 3 tập thực thể bất biến từ nguồn (`CHECKPOINT.md` gốc + kho lạnh hiện có):
1. **`sha_hashes`**: chuỗi hex `40..64` ký tự và commit/hash ngắn `7..16` ký tự trong backtick (`` `a1b2c3d` ``, `` `a1b2c3d…` ``).
2. **`domain_ids`**: mã quy chuẩn `TQ-*`, `BL-*`, `ADR-*`, `REQ-*`, `WB-*`, `SV-*`, `CG-*`, `EX-*`, `AC-*`, `GK-*`, `K1-*`, `K2-*`, `P\d-*`, `G\d-*`, `HL-*`, `SB-*`, `RISK-*`, `HYP-*`.
3. **`code_paths`**: đường dẫn tệp Windows/POSIX trong backtick có đuôi `.md`, `.ts`, `.mjs`, `.js`, `.json`, `.py`, `.exe`, `.dll`, `.patch`, `.yml`, `.yaml`, `.html`.

Nếu thiếu **dù chỉ 1 thực thể** trong hợp `(hot_checkpoint + cold_archive)`:
- `CHECKPOINT.md` gốc được giữ nguyên 100%.
- Danh sách thực thể thiếu được in ra `stderr`.
- Tiến trình thoát mã **`2`** (fail-closed).

---

## 6. Kiểm Thử / Running Tests

```powershell
python -m unittest discover -s tests -v
```

---

## 7. Cấu Trúc Kho Mã Nguồn / Repository Layout

```text
gemini-auto-compact-skill/
├── .gitignore
├── LICENSE
├── README.md
├── install.py
├── skills/
│   └── auto-compact/
│       ├── SKILL.md
│       ├── references/
│       │   └── ledger-schema.md
│       └── scripts/
│           ├── measure_context.py
│           └── compact_ledger.py
└── tests/
    └── test_auto_compact.py
```

## License

MIT — see [LICENSE](LICENSE).

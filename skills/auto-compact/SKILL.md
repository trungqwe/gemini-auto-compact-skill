---
name: auto-compact
description: >-
  Measure active context pressure (transcript size, step count, sub-agent dispatches, always-on rule tax,
  hot ledger bloat) and autonomously compact working-set ledgers (CHECKPOINT.md hot/cold split with fail-closed
  zero-drop entity verification, STATE.json, COMPACT_PACKET.md) in-session or as a lossless pre-gate before
  engine /compact. Activate when sessions grow long, hit context thresholds, at major milestone boundaries,
  or when the user asks to "tự nén ngữ cảnh", "auto-compact", "nén sổ trạng thái", "đo áp lực context",
  or prepare a lossless handoff before /compact.
---

# Auto-Compact — Tự Đo Áp Lực & Nén Ngữ Cảnh Không Mất Mát

Skill giúp AI Agent tự đo áp lực ngữ cảnh và tự nén sổ trạng thái nóng (`CHECKPOINT.md`) ngay trong phiên đang chạy, giảm **70%–90%** token đọc lặp mà **bảo toàn 100%** thực thể kỹ thuật bất biến (SHA-256, commit SHA, mã `TQ-*`/`BL-*`/`ADR-*`/`REQ-*`/`P*-F*`/`G*-C*`, đường dẫn tệp).

## Kiến Trúc 2 Tầng Trung Thực Kỹ Thuật

### Tầng 1 — Tự nén vùng làm việc ngay trong phiên (`In-Session Autonomous Compaction`)
Chạy hoàn toàn tự động mà **không gián đoạn phiên chat**:
1. **Đo áp lực thực tế**:
   ```powershell
   python "<skill_dir>/scripts/measure_context.py" --project-dir . --json
   ```
2. **Nén tách nóng/lạnh bất biến (`CHECKPOINT.md`)**:
   ```powershell
   python "<skill_dir>/scripts/compact_ledger.py" --project-dir . --keep-latest 1 --json
   ```
   - Giữ phần nóng `CHECKPOINT.md` gọn (`<= 120` dòng): mở đầu + `N` mốc mới nhất + khối con trỏ kho lạnh kèm `SHA-256`.
   - Chuyển toàn bộ các mốc cũ vào `.context-compact/archive/checkpoint-archive.md` (hợp nhất giảm dần theo thời gian, chống trùng tiêu đề, không xoá bất kỳ dòng nào).
   - Chạy cổng **Zero-Drop Entity Verification** (fail-closed `exit 2`): chỉ ghi nguyên tử (`os.replace`) khi 100% SHA-256, short hash, mã định danh và đường dẫn tệp đều còn nguyên vẹn trong hợp `(hot + archive)`.
   - Xuất `.context-compact/STATE.json` và `.context-compact/COMPACT_PACKET.md` (`< 80` dòng).
3. **Tiếp tục phiên trên tập làm việc gọn**:
   - Đọc `.context-compact/COMPACT_PACKET.md` hoặc `STATE.json` thay vì nạp lại lịch sử dài.
   - Uỷ quyền các lượt quét rộng (log dài, nhiều tệp mã nguồn, kho lạnh) sang sub-agent tóm tắt thay vì đọc trực tiếp vào phiên chính.

### Tầng 2 — Cổng bảo vệ trước `/compact` của Engine (`Engine Context Compaction Gate`)
- Trên Antigravity IDE / CLI, `/compact` là **slash command phía UI** — agent không có tool để tự kích hoạt lệnh này.
- Khi `measure_context.py` trả `CRITICAL_ENGINE_COMPACT`, agent **BẮT BUỘC chạy Tầng 1 trước** để chốt trạng thái bất biến vào `STATE.json`, `COMPACT_PACKET.md`, và `CHECKPOINT.md` gọn (giúp `<CONTEXT_SUMMARY>` hoặc phiên mới kế thừa 100% dữ kiện chuẩn xác), sau đó mới khuyến nghị **đúng 1 dòng** cho người dùng chạy `/compact` tại ranh giới mốc an toàn.

## Ngưỡng Phân Hạng & Hành Động

| Verdict | Điều kiện kích hoạt | Hành động bắt buộc |
| --- | --- | --- |
| `HEALTHY` | Dưới mọi ngưỡng cảnh báo | Không cần nén; tiếp tục công việc. |
| `WARN_COMPACT_LEDGER` | `transcript >= 2.0 MB` hoặc `steps >= 800` hoặc `CHECKPOINT.md > 150` dòng hoặc phát hiện `rule_waste_findings` | Chạy **Tầng 1** ngay trong phiên (`compact_ledger.py`), báo gọn các mục rule thừa nếu có. |
| `CRITICAL_ENGINE_COMPACT` | `transcript >= 5.0 MB` hoặc `steps >= 2000` hoặc `subagents >= 30` | Chạy **Tầng 1** chốt gói bàn giao không mất mát, xác minh `zero_drop_verified: true`, rồi đề xuất 1 dòng `/compact`. |

## Quy Trình Vận Hành Nhanh

1. **Kiểm tra an toàn trước (tuỳ chọn)**:
   ```powershell
   python "<skill_dir>/scripts/compact_ledger.py" --project-dir . --dry-run --json
   ```
2. **Nén thực tế**:
   ```powershell
   python "<skill_dir>/scripts/compact_ledger.py" --project-dir . --keep-latest 1 --json
   ```
3. **Xác minh toàn vẹn không sửa tệp**:
   ```powershell
   python "<skill_dir>/scripts/compact_ledger.py" --project-dir . --verify-only --json
   ```

## Quy Tắc Cứng & Xử Lý Lỗi
- **Fail-closed (`exit 2`)**: Nếu `verify_zero_drop` báo thiếu bất kỳ mã/hash/đường dẫn nào, `CHECKPOINT.md` gốc **không bị chạm tới**. Phải kiểm tra danh sách thiếu trên `stderr` trước khi thử lại.
- **Bất biến (Idempotent)**: Chạy `compact_ledger.py` nhiều lần liên tiếp không nhân đôi mục kho lạnh và không làm phình `CHECKPOINT.md`.
- **Không dùng lệnh xoá đệ quy**: Mọi cập nhật tệp dùng ghi tạm + `os.replace`.
- **Không nạp kho lạnh vào ngữ cảnh chính**: Khi cần tra cứu mốc cũ, dùng tìm kiếm theo từ khoá/mục tiêu cụ thể trong `.context-compact/archive/checkpoint-archive.md`.

## Tham Chiếu Lược Đồ, Cài Đặt & Khôi Phục
- Cài đặt/xác minh skill: `python install.py [--also-agents] [--agents-dir <path>] [--verify]`.
- Đọc [references/ledger-schema.md](references/ledger-schema.md) khi cần xem chi tiết cấu trúc `STATE.json`, `COMPACT_PACKET.md`, và quy ước tra cứu/khôi phục từ kho lạnh.

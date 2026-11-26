# Lược Đồ `STATE.json` & Quy Ước Khôi Phục Kho Lạnh

## 1. Cấu Trúc `.context-compact/STATE.json`

```json
{
  "schema_version": "1.0.0",
  "generated_at": "2026-11-26T08:30:00+07:00",
  "project_dir": "/abs/path/to/project",
  "git": {
    "is_repo": true,
    "branch": "main",
    "head_sha": "abc1234...",
    "dirty": false
  },
  "checkpoint": {
    "path": "CHECKPOINT.md",
    "keep_latest": 1,
    "hot_milestones": ["## Mốc mới nhất"],
    "archived_milestone_count": 7,
    "archived_milestones": ["## Mốc cũ 1", "## Mốc cũ 2"]
  },
  "archive": {
    "path": ".context-compact/archive/checkpoint-archive.md",
    "exists": true,
    "sha256": "<64-hex>",
    "bytes": 14200,
    "lines": 360
  },
  "metrics": {
    "bytes_before": 16500,
    "bytes_after": 2100,
    "lines_before": 420,
    "lines_after": 54,
    "bytes_saved": 14400,
    "reduction_pct": 87.27,
    "estimated_tokens_saved_per_read": 3789
  },
  "entities": {
    "zero_drop_verified": true,
    "total_tracked": {
      "sha_hashes": 12,
      "domain_ids": 34,
      "code_paths": 18
    },
    "hot_entities": {
      "sha_hashes": [],
      "domain_ids": [],
      "code_paths": []
    }
  },
  "verified_entities": {
    "sha_hashes": [],
    "domain_ids": [],
    "code_paths": []
  }
}
```

## 2. Cấu Trúc `.context-compact/COMPACT_PACKET.md`
- Luôn giới hạn **< 80 dòng** để nạp ngay vào đầu lượt hoặc làm nguồn chân lý cho `<CONTEXT_SUMMARY>` trước `/compact`.
- Chứa: thời điểm chốt, Git `branch`/`HEAD`, đường dẫn + `SHA-256` kho lạnh, mức tiết kiệm (`bytes_before -> bytes_after`, `reduction_pct`), tiêu đề + tóm tắt mốc nóng, danh sách thực thể cốt lõi trong mốc nóng, và xác nhận `zero_drop_verified: true`.

## 3. Khối Con Trỏ Kho Lạnh Trong `CHECKPOINT.md`
Mọi tệp `CHECKPOINT.md` đã nén kết thúc bằng khối chuẩn bọc trong cặp thẻ mở–đóng:
```markdown
<!-- AUTO-COMPACT-ARCHIVE-POINTER -->
## 🗄 Kho lưu trữ lịch sử nén
- **Kho lạnh (Cold Archive):** `.context-compact/archive/checkpoint-archive.md`
- **Archive SHA-256:** `<64-hex>`
- **Số mốc đã lưu kho lạnh:** `<N>`
- **Danh sách mốc trong kho lạnh:**
  - `## ...`
<!-- /AUTO-COMPACT-ARCHIVE-POINTER -->
```
Cặp thẻ `<!-- AUTO-COMPACT-ARCHIVE-POINTER -->` ... `<!-- /AUTO-COMPACT-ARCHIVE-POINTER -->` giúp `compact_ledger.py` nhận diện đúng khối máy sinh, bảo toàn mọi ghi chú nối cuối tệp và tái tạo idempotent.

## 4. Quy Ước Kiểm Chứng & Khôi Phục Từ Kho Lạnh
1. **Kiểm chứng toàn vẹn không sửa tệp**:
   `python "<skill_dir>/scripts/compact_ledger.py" --project-dir . --verify-only --json`
   Xác nhận `SHA-256` của tệp kho lạnh khớp với `STATE.json` và khối con trỏ trong `CHECKPOINT.md`.
2. **Tra cứu đích danh (không đọc cả tệp kho lạnh vào phiên chính)**:
   Dùng `Select-String -Path .context-compact/archive/checkpoint-archive.md -Pattern "<mã hoặc tiêu đề>"` (PowerShell) hoặc `rg` để lấy đúng đoạn cần thiết.
3. **Khôi phục một mốc về phần nóng**:
   Sao chép khối `## ...` tương ứng từ `.context-compact/archive/checkpoint-archive.md` dán vào `CHECKPOINT.md` (trước khối con trỏ); các lượt nén sau vẫn tự động khử trùng theo tiêu đề chuẩn hoá mà không mất dòng nào.

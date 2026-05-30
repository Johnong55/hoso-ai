# Thư mục xlsx danh sách thủ tục (OPTIONAL — chế độ LOCAL/offline)

> ⚠ Từ bản cập nhật mới, crawler **mặc định lấy danh sách mã TTHC ONLINE** qua API
> (không cần file local). Folder này chỉ dùng khi muốn crawl offline từ file đã tải sẵn.

## Chế độ ONLINE (mặc định)

`DocumentSource.source_url`:
- `""` hoặc `"all"` → lấy tất cả cơ quan qua API rồi crawl
- `"Bộ Công an"` / `"6369"` → chỉ 1 cơ quan (theo tên hoặc agency_id)

Luồng online:
1. POST `rest.jsp` `{service: procedure_get_list_agency_by_type_service_v2}` → danh sách cơ quan
2. GET `export_exel_list_tthc.jsp?impl_agency_id=<ID>` → file Excel của cơ quan
3. Parse Excel (in-memory) → mã TTHC

## Chế độ LOCAL (offline fallback)

`DocumentSource.source_url = "<tên>.xlsx"` → đọc file trong folder này.

Yêu cầu format file:
- Có cột `Mã TTHC` ở hàng header (tự dò)
- File lock Excel (`~$*.xlsx`) bị bỏ qua
- KHÔNG dùng read_only mode (merged cells header làm openpyxl chỉ thấy 4 rows)

Đường dẫn config qua `.env`: `XLSX_DATA_DIR=./data/tthc`.

Sau khi có danh sách mã, với mỗi mã:
1. POST `rest.jsp` `{service: procedure_advanced_search_service_v2, keyword:<mã>}` → idTTHC
2. GET `export_word_detail_tthc.jsp?maTTHC=...&idTTHC=...` → file Word chi tiết
3. Parse Word → lưu DB + embed sang Qdrant

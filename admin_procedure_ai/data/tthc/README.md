# Thư mục chứa file .xlsx danh sách thủ tục

Mỗi file = 1 bộ/ngành, xuất từ Cổng DVCQG.

Yêu cầu format:
- Phải có cột `Mã TTHC` ở hàng header (tự dò trong tối đa N dòng đầu)
- Các cột khác (Tên TTHC, Lĩnh vực, Cơ quan công khai...) được dùng làm fallback metadata
- File lock của Excel (`~$*.xlsx`) sẽ bị bỏ qua

Crawler đọc tất cả `*.xlsx` trong folder này, dedupe mã TTHC, rồi với mỗi mã:
1. POST `https://thutuc.dichvucong.gov.vn/jsp/rest.jsp` để lấy `idTTHC`
2. GET `https://thutuc.dichvucong.gov.vn/jsp/tthc/export/export_word_detail_tthc.jsp?maTTHC=...&idTTHC=...` để tải file Word chi tiết
3. Parse file Word → lưu vào DB + embed sang Qdrant

Đường dẫn config qua `.env`: `XLSX_DATA_DIR=./data/tthc`.

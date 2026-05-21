# RAG Pipeline — Admin Procedure AI

> **Mục đích:** Tài liệu này mô tả toàn bộ pipeline RAG (Retrieval-Augmented Generation) — từ crawl dữ liệu → chunk → embed → store → retrieve → generate. Đọc file này TRƯỚC khi query vector database hoặc debug retrieval, để hiểu đúng cấu trúc dữ liệu và tránh các pitfall thường gặp.

---

## 1. Tổng quan Pipeline

```
┌─────────────────────────────────────────────────────────────────────────┐
│                          INGESTION (offline)                            │
│                                                                         │
│   DVCQG website                                                         │
│        │                                                                │
│        ▼                                                                │
│   ┌──────────────┐    ┌──────────────┐    ┌──────────────┐              │
│   │  Crawler     │ →  │  Parser      │ →  │  Chunker     │              │
│   │  (Playwright)│    │ (BeautifulSoup) │   │ (semantic)   │           │
│   └──────────────┘    └──────────────┘    └──────────────┘              │
│                                                   │                     │
│                                                   ▼                     │
│                              ┌──────────────────────────┐               │
│                              │  Cohere Embed API        │               │
│                              │  multilingual-v3 (1024d) │               │
│                              │  input_type=search_document │            │
│                              └──────────────────────────┘               │
│                                                   │                     │
│                                                   ▼                     │
│                              ┌──────────────────────────┐               │
│                              │  Qdrant Cloud            │               │
│                              │  collection=procedure_chunks │            │
│                              └──────────────────────────┘               │
└─────────────────────────────────────────────────────────────────────────┘

┌─────────────────────────────────────────────────────────────────────────┐
│                            QUERY (online)                               │
│                                                                         │
│   User query (Vietnamese)                                               │
│        │                                                                │
│        ▼                                                                │
│   ┌──────────────────┐                                                  │
│   │ Query Rewriter   │  (LLM expand: "thuê nhà" → "đăng ký thường trú  │
│   │ (LLM)            │   tại chỗ ở thuê, mượn, ở nhờ")                 │
│   └──────────────────┘                                                  │
│        │                                                                │
│        ▼                                                                │
│   ┌──────────────────┐                                                  │
│   │ Cohere Embed     │  input_type=search_query                         │
│   └──────────────────┘                                                  │
│        │                                                                │
│        ▼                                                                │
│   ┌──────────────────────────────────────────────────────────┐          │
│   │ Qdrant Search                                            │          │
│   │  • cosine similarity                                     │          │
│   │  • payload pre-filter (locality, domain, chunk_type)     │          │
│   │  • top_k * 2 candidates → threshold → top_k              │          │
│   └──────────────────────────────────────────────────────────┘          │
│        │                                                                │
│        ▼                                                                │
│   ┌──────────────────┐                                                  │
│   │ LLM Generator    │  prompt = system + context + query               │
│   │ (OpenRouter)     │  → answer + citations                            │
│   └──────────────────┘                                                  │
└─────────────────────────────────────────────────────────────────────────┘
```

**Code entry point:** `app/rag/pipeline.py` → `RAGPipeline.run(query, locality, domain)`

---

## 2. Cấu trúc Chunk — quan trọng nhất để query đúng

Một thủ tục được tách thành **nhiều chunks**, mỗi chunk = 1 đơn vị ngữ nghĩa. **KHÔNG bao giờ** trộn 2 section khác nhau vào 1 chunk.

### 2.1 Các loại chunk (`ChunkType`)

| `chunk_type` | Nội dung | Khi nào dùng |
|---|---|---|
| `GENERAL` | Mô tả tổng quan + các "Lưu ý quan trọng" | Câu hỏi tổng quát "thủ tục X là gì" |
| `FEE` | Lệ phí + thời gian xử lý | "bao nhiêu tiền", "mất bao lâu" |
| `RESULT` | Kết quả nhận được | "nhận được gì", "kết quả là gì" |
| `LEGAL_BASIS` | Căn cứ pháp lý (luật, nghị định...) | "theo luật nào", "căn cứ ở đâu" |
| `REQUIREMENT` | Danh sách giấy tờ — gom theo `case_group` | "cần giấy tờ gì", "hồ sơ gồm gì" |
| `STEP` | Trình tự thực hiện — 1 chunk / step | "làm như thế nào", "các bước" |
| `FORM` | 1 biểu mẫu đã parse (fields + nội dung mẫu) | "mẫu đơn", "cách điền form" |

### 2.2 Payload của 1 chunk trong Qdrant

```json
{
  "source_id": "uuid",
  "chunk_type": "REQUIREMENT",
  "content": "Thủ tục: Đăng ký thường trú\nTrường hợp: Đăng ký thường trú tại chỗ ở thuê, mượn, ở nhờ\nGiấy tờ cần nộp:\n1. Tờ khai thay đổi thông tin cư trú (Mẫu CT01)\n2. Hợp đồng thuê nhà có công chứng (1 bản)\n...",
  "procedure_id": "uuid",
  "procedure_code": "1.001234",
  "domain": "Cư trú",
  "authority_level": "CENTRAL",
  "locality": "",
  "section": "Thành phần hồ sơ",
  "case_group": "Đăng ký thường trú tại chỗ ở thuê, mượn, ở nhờ"
}
```

**Field bắt buộc nhớ:**
- `content` — text gốc đã embed (Cohere thấy gì → đây là cái đó)
- `chunk_type` — KEY filter chính khi query
- `procedure_code` — định danh thủ tục, dùng để dedupe
- `case_group` — tách các "trường hợp" của cùng 1 thủ tục (vd: thuê nhà vs nhà sở hữu)
- `section` — luôn ≤ 200 chars, KHÔNG dùng để filter ngữ nghĩa (chỉ để hiển thị)

### 2.3 Quy tắc chunking (xem `app/rag/chunking/strategy.py`)

| Section đầu vào | Output chunks |
|---|---|
| `description` | 1+ chunks `GENERAL` (sliding window nếu >512 chars) |
| `fee` + `processing_time` | **1** chunk `FEE` (gộp 2 trường) |
| `result` | 1 chunk `RESULT` |
| `legal_basis` | 1 chunk `LEGAL_BASIS` |
| `requirements` (group "Bao gồm", "Giấy tờ phải nộp", "Giấy tờ phải xuất trình") | 1 chunk/group, `chunk_type=REQUIREMENT` |
| `requirements` (group "Lưu ý") | 1 chunk `GENERAL` (không phải REQUIREMENT vì là hướng dẫn) |
| `requirements` (case-specific: "Đăng ký thường trú tại chỗ ở thuê...") | 1 chunk/case, prefix `Trường hợp: ...` để khớp ngữ nghĩa |
| `steps` (mỗi bước) | 1 chunk `STEP`, nếu description >512 chars → split sliding window thành "phần N/M" |
| `forms` (biểu mẫu .doc/.docx) | 1 chunk `FORM`/biểu mẫu |

**KHÔNG có chunk nào mất context của tên thủ tục** — mọi chunk đều prefix `Thủ tục: <tên>` ở dòng đầu.

---

## 3. Query Best Practices — Đọc kỹ trước khi search

### 3.1 Luôn embed query với `input_type="search_query"`

```python
# ✅ ĐÚNG
query_vec = embedder.embed_query("tôi cần giấy tờ gì để đăng ký thường trú?")

# ❌ SAI — dùng input_type=search_document sẽ giảm chất lượng 5-10%
client.embed(texts=[q], input_type="search_document", ...)
```

Cohere multilingual-v3 train 2 chiều riêng cho document và query. Asymmetric retrieval **bắt buộc** dùng đúng `input_type`.

### 3.2 Pre-filter trước khi vector search

Qdrant filter chạy TRƯỚC scoring → giảm latency + tăng precision. Dùng khi biết chắc context:

```python
# Người dùng ở Hà Nội hỏi → filter locality
retriever.retrieve(query_embedding, locality="Hà Nội")

# Câu hỏi chỉ về fees → filter chunk_type
retriever.retrieve(query_embedding, chunk_type="FEE")

# Câu hỏi chỉ trong domain "Cư trú"
retriever.retrieve(query_embedding, domain="Cư trú")
```

**Không filter khi:** câu hỏi mơ hồ, hoặc bạn muốn LLM tự chọn từ nhiều chunk_type khác nhau.

### 3.3 Score threshold (`RAG_SCORE_THRESHOLD=0.35`)

- **>0.7** — match rất tốt, gần như chắc chắn relevant
- **0.5–0.7** — relevant, dùng được
- **0.35–0.5** — borderline, có thể relevant hoặc noise
- **<0.35** — bỏ (mặc định cắt ở đây)

Nếu retrieve **0 chunks** → giảm threshold xuống 0.25 hoặc bỏ filter trước khi nghi ngờ chunking.

### 3.4 Query rewriting (LLM expand)

Pipeline tự động rewrite query bằng LLM trước khi embed. Ví dụ:
- User: "thuê nhà thì sao"
- Rewritten: "đăng ký thường trú khi đang thuê, mượn, ở nhờ nhà của người khác cần giấy tờ gì"

Nếu retrieval sai, **log `rewritten_query`** trước — thường lỗi ở đây chứ không phải embedding.

---

## 4. Cấu hình & Tham số quan trọng

| ENV var | Mặc định | Ý nghĩa |
|---|---|---|
| `RAG_TOP_K` | 5 | Số chunks trả về cuối cùng |
| `RAG_SCORE_THRESHOLD` | 0.35 | Min cosine similarity |
| `RAG_MAX_CONTEXT_CHUNKS` | 8 | Max chunks gửi cho LLM (cap để tránh overflow context) |
| `RAG_CHUNK_SIZE` | 512 | Kích thước sliding window cho text dài |
| `RAG_CHUNK_OVERLAP` | 64 | Overlap giữa 2 chunks liền kề |
| `EMBEDDING_MODEL` | `embed-multilingual-v3.0` | Cohere model |
| `EMBEDDING_DIMENSIONS` | 1024 | Vector dim — phải khớp với collection schema |
| `QDRANT_COLLECTION_NAME` | `procedure_chunks` | Collection name |

**⚠ Đổi `EMBEDDING_DIMENSIONS` = drop collection và re-index toàn bộ.**

---

## 5. Common Pitfalls & Cách xử lý

### 5.1 "Retrieve được 0 chunks dù dữ liệu có trong DB"

Checklist theo thứ tự:
1. Collection có tồn tại không? → kiểm tra Qdrant dashboard
2. Collection có > 0 points không?
3. `chunk_type` filter có khớp không? (case-sensitive: `"FEE"` ≠ `"fee"`)
4. `score_threshold` có quá cao không? → thử bỏ threshold
5. `query` và `chunks` có cùng ngôn ngữ không? (multilingual-v3 OK Vietnamese ↔ English nhưng đồng ngôn ngữ luôn tốt hơn)

### 5.2 "Cùng 1 thủ tục bị trả về nhiều chunk giống nhau"

- Bình thường! 1 thủ tục có 5–15 chunks (mỗi section / case_group / step là 1 chunk)
- Để dedupe ở response: group theo `procedure_code` trong `Generator`
- Nếu thấy **chunk duplicate exact** → có thể do crawl lại nhiều lần không cleanup. Xóa và re-crawl:
  ```python
  embedder.delete_by_source(source_id)
  ```

### 5.3 "Section overflow: data too long for column 'section'"

`section` trong MySQL chỉ VARCHAR(255). Chunker đã đảm bảo `section` ≤ 200 chars, nhưng `case_group` có thể dài 500+ chars → **lưu vào field `case_group` riêng**, không nhồi vào `section`.

### 5.4 "STEP chunks bị thiếu"

Một số thủ tục dùng paragraph thay vì "Bước 1:, Bước 2:". Parser có fallback gom hết `<p>` thành 1 step. Nếu vẫn thiếu:
- Check HTML gốc có `div.item.active` không
- Check `parsed["steps"]` trước khi chunking

### 5.5 "Event loop is closed" trong Celery

Đã fix bằng `engine.dispose()` trước khi đóng loop. Nếu lại gặp → check `_run_async()` trong `app/worker/tasks.py`.

### 5.6 "Collection doesn't exist (404)"

Singleton `_qdrant_client` được init 1 lần. Nếu xóa collection từ dashboard giữa chừng → singleton stale → 404. Fix: **restart worker** để re-trigger `_ensure_collection()`.

---

## 6. Cheatsheet — Query thủ công Qdrant để debug

```python
from app.rag.embedding.embedder import _get_qdrant_client, Embedder
from qdrant_client.models import Filter, FieldCondition, MatchValue

client = _get_qdrant_client()
emb = Embedder()

# 1. Đếm tổng số points
info = client.get_collection("procedure_chunks")
print(f"Total points: {info.points_count}")

# 2. Xem 10 chunks bất kỳ
points, _ = client.scroll("procedure_chunks", limit=10, with_payload=True)
for p in points:
    print(p.payload["chunk_type"], "|", p.payload["content"][:100])

# 3. Filter theo procedure_code
points, _ = client.scroll(
    "procedure_chunks",
    scroll_filter=Filter(must=[
        FieldCondition(key="procedure_code", match=MatchValue(value="1.001234"))
    ]),
    limit=50,
    with_payload=True,
)

# 4. Semantic search thủ công (không qua pipeline)
q_vec = emb.embed_query("lệ phí đăng ký khai sinh bao nhiêu")
results = client.search(
    "procedure_chunks",
    query_vector=q_vec,
    limit=5,
    with_payload=True,
)
for r in results:
    print(f"{r.score:.3f} | {r.payload['chunk_type']} | {r.payload['content'][:120]}")

# 5. Xóa toàn bộ chunks của 1 source
emb.delete_by_source("<source_id>")
```

---

## 7. Đánh giá chất lượng (Evaluation)

| Mức độ | Phương pháp | Output |
|---|---|---|
| **Smoke test** | Đọc thủ công 20 chunks random | Tự đủ nghĩa? cắt đúng không? |
| **Distribution** | `scripts/eval_chunks.py` (count + length stats) | Median 200–800 chars, không có chunk <50 hoặc >2000 |
| **Retrieval** | 30–50 cặp Q&A → tính Recall@5, MRR | Recall@5 > 0.80 = pass |
| **End-to-end** | RAGAS (faithfulness, answer_relevancy...) | Cho luận văn Chapter 4 |

Xem chi tiết các metric ở `docs/EVALUATION.md` (nếu có).

---

## 8. File map

```
app/rag/
├── README.md                   ← bạn đang đọc
├── pipeline.py                 ← orchestrator: rewrite → embed → retrieve → generate
├── chunking/
│   └── strategy.py             ← ProcedureChunker — semantic chunking rules
├── embedding/
│   └── embedder.py             ← Cohere + Qdrant client (singleton)
├── retrieval/
│   └── retriever.py            ← Qdrant search + payload pre-filter
└── generation/
    └── generator.py            ← LLM prompt + query rewrite + answer + fallback
```

**Đọc theo thứ tự khi onboarding:** `pipeline.py` → `chunking/strategy.py` → `embedding/embedder.py` → `retrieval/retriever.py` → `generation/generator.py`.

---

## 9. TL;DR — 5 dòng tối thiểu cần nhớ

1. **1 chunk = 1 đơn vị ngữ nghĩa**, không trộn section. Mọi chunk có prefix `Thủ tục: <tên>`.
2. **`chunk_type`** là filter chính khi query (FEE/STEP/REQUIREMENT/...).
3. **Embed query** phải dùng `input_type="search_query"`, KHÔNG dùng `search_document`.
4. **Pre-filter** trước vector search nếu biết context (locality/domain/chunk_type) → nhanh + chính xác.
5. **Threshold 0.35** là sàn — retrieve 0 chunk thì giảm threshold trước, đừng đổ tại embedding ngay.

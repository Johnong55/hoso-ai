# app/rag/embedding/embedder.py
import re
import threading
import time
import uuid
from typing import Any

from google import genai
from google.genai import types as genai_types
from loguru import logger
from qdrant_client import QdrantClient
from qdrant_client.models import (
    Distance,
    FieldCondition,
    Filter,
    FilterSelector,
    MatchValue,
    PointIdsList,
    PointStruct,
    VectorParams,
)

from app.core.config import settings
from app.rag.chunking.strategy import Chunk


# ── Singleton clients ─────────────────────────────────────────────────────────

_qdrant_client: QdrantClient | None = None
_gemini_client: genai.Client | None = None


def _get_qdrant_client() -> QdrantClient:
    global _qdrant_client
    if _qdrant_client is None:
        if settings.QDRANT_URL and settings.QDRANT_API_KEY:
            # ☁️  Qdrant Cloud
            _qdrant_client = QdrantClient(
                url=settings.QDRANT_URL,
                api_key=settings.QDRANT_API_KEY,
            )
        elif settings.QDRANT_PERSIST_DIR:
            # 💾 Local / embedded — no Docker needed
            _qdrant_client = QdrantClient(path=settings.QDRANT_PERSIST_DIR)
        else:
            # 🐳 Self-hosted Docker
            _qdrant_client = QdrantClient(
                host=settings.QDRANT_HOST,
                port=settings.QDRANT_PORT,
            )
        _ensure_collection(_qdrant_client)
    return _qdrant_client


def _ensure_collection(client: QdrantClient) -> None:
    """Create the collection if it doesn't exist yet."""
    existing = {c.name for c in client.get_collections().collections}
    if settings.QDRANT_COLLECTION_NAME not in existing:
        client.create_collection(
            collection_name=settings.QDRANT_COLLECTION_NAME,
            vectors_config=VectorParams(
                size=settings.EMBEDDING_DIMENSIONS,
                distance=Distance.COSINE,
            ),
        )
        logger.info(
            f"Qdrant | created collection '{settings.QDRANT_COLLECTION_NAME}' "
            f"| dims={settings.EMBEDDING_DIMENSIONS}"
        )


def _get_gemini_client() -> genai.Client:
    global _gemini_client
    if _gemini_client is None:
        _gemini_client = genai.Client(api_key=settings.GEMINI_API_KEY)
    return _gemini_client


# Cohere input_type → Gemini task_type
_TASK_TYPE_MAP = {
    "search_document": "RETRIEVAL_DOCUMENT",
    "search_query": "RETRIEVAL_QUERY",
}


# ── Rate limiting + 429 retry ──────────────────────────────────────────────────

# Throttle chủ động: đảm bảo cách nhau tối thiểu EMBEDDING_MIN_INTERVAL_SEC giữa
# 2 lần gọi embed (chia sẻ giữa các thread của celery --pool=solo là 1 thread,
# nhưng vẫn an toàn nếu sau này đổi pool).
_rate_lock = threading.Lock()
_last_call_ts = 0.0


def _throttle() -> None:
    global _last_call_ts
    min_interval = settings.EMBEDDING_MIN_INTERVAL_SEC
    if min_interval <= 0:
        return
    with _rate_lock:
        now = time.monotonic()
        wait = _last_call_ts + min_interval - now
        if wait > 0:
            time.sleep(wait)
        _last_call_ts = time.monotonic()


def _is_rate_limit_error(exc: Exception) -> bool:
    s = str(exc)
    return "429" in s or "RESOURCE_EXHAUSTED" in s


def _parse_retry_delay(exc: Exception, default: float = 30.0) -> float:
    """Lấy số giây cần chờ từ message lỗi 429 (retryDelay hoặc 'retry in Xs')."""
    s = str(exc)
    m = re.search(r"retry in ([\d.]+)s", s) or re.search(r"retryDelay['\"]?:\s*['\"]?(\d+)s", s)
    if m:
        try:
            return float(m.group(1)) + 1.0  # +1s đệm
        except ValueError:
            pass
    return default


# ── Cloudflare Workers AI embedding ────────────────────────────────────────────

_cf_http: "httpx.Client | None" = None


def _get_cf_client():
    global _cf_http
    import httpx
    if _cf_http is None:
        _cf_http = httpx.Client(timeout=60)
    return _cf_http


def _embed_cloudflare_batch(batch: list[str]) -> list[list[float]]:
    """
    Gọi Cloudflare Workers AI (@cf/baai/bge-m3) cho 1 batch text.
    bge-m3 là embedding đối xứng (không phân biệt query/document) nên bỏ qua input_type.
    Có retry khi gặp 429.
    """
    import httpx

    if not settings.CLOUDFLARE_ACCOUNT_ID or not settings.CLOUDFLARE_API_TOKEN:
        raise RuntimeError(
            "Thiếu CLOUDFLARE_ACCOUNT_ID / CLOUDFLARE_API_TOKEN trong .env "
            "(cần khi EMBEDDING_PROVIDER=cloudflare)"
        )

    url = (
        f"https://api.cloudflare.com/client/v4/accounts/"
        f"{settings.CLOUDFLARE_ACCOUNT_ID}/ai/run/{settings.CLOUDFLARE_EMBEDDING_MODEL}"
    )
    headers = {"Authorization": f"Bearer {settings.CLOUDFLARE_API_TOKEN}"}
    client = _get_cf_client()
    max_retries = settings.EMBEDDING_MAX_RETRIES
    attempt = 0

    while True:
        try:
            resp = client.post(url, headers=headers, json={"text": batch})
            if resp.status_code == 429:
                raise httpx.HTTPStatusError("429", request=resp.request, response=resp)
            resp.raise_for_status()
            body = resp.json()
            if not body.get("success", False):
                raise RuntimeError(f"Cloudflare AI error: {body.get('errors')}")
            return body["result"]["data"]
        except Exception as exc:
            is_429 = "429" in str(exc)
            if not is_429 or attempt >= max_retries:
                raise
            attempt += 1
            delay = _parse_retry_delay(exc, default=10.0)
            logger.warning(
                f"Embedder(CF) | 429 rate limit, retry {attempt}/{max_retries} after {delay:.0f}s"
            )
            time.sleep(delay)


# ── Embedder ──────────────────────────────────────────────────────────────────

class Embedder:
    """
    Embedding via Gemini (gemini-embedding-001) + Qdrant vector store.

    Gemini task_type convention:
      - "RETRIEVAL_DOCUMENT" → embed chunks before storing
      - "RETRIEVAL_QUERY"    → embed user query at search time
    """

    def __init__(self) -> None:
        self._qdrant = _get_qdrant_client()

    # ── Public API ────────────────────────────────────────────────────────────

    def embed_chunks(self, chunks: list[Chunk], source_id: str) -> list[dict[str, Any]]:
        """Embed a list of Chunk objects and upsert them into Qdrant."""
        if not chunks:
            return []

        texts = [c.content for c in chunks]
        vector_ids = [str(uuid.uuid4()) for _ in chunks]

        embeddings = self._get_embeddings(texts, input_type="search_document")

        points: list[PointStruct] = []
        for i, chunk in enumerate(chunks):
            payload: dict[str, Any] = {
                "source_id": source_id,
                "chunk_type": chunk.chunk_type.value,
                "content": chunk.content,
                **{k: (v if v is not None else "") for k, v in chunk.metadata.items()},
            }
            points.append(PointStruct(
                id=vector_ids[i],
                vector=embeddings[i],
                payload=payload,
            ))

        self._qdrant.upsert(
            collection_name=settings.QDRANT_COLLECTION_NAME,
            points=points,
        )
        logger.info(f"Embedder | upserted {len(chunks)} chunks | source_id={source_id}")

        return [
            {
                "vector_id": vector_ids[i],
                "content": chunks[i].content,
                "chunk_type": chunks[i].chunk_type,
                "metadata": chunks[i].metadata,
            }
            for i in range(len(chunks))
        ]

    def embed_query(self, query: str) -> list[float]:
        """Embed a user query string (RETRIEVAL_QUERY task_type)."""
        return self._get_embeddings([query], input_type="search_query")[0]

    def delete_by_source(self, source_id: str) -> None:
        """Delete all vectors whose payload.source_id matches."""
        self._qdrant.delete(
            collection_name=settings.QDRANT_COLLECTION_NAME,
            points_selector=FilterSelector(
                filter=Filter(
                    must=[
                        FieldCondition(
                            key="source_id",
                            match=MatchValue(value=source_id),
                        )
                    ]
                )
            ),
        )
        logger.info(f"Embedder | deleted all vectors | source_id={source_id}")

    def delete_by_ids(self, vector_ids: list[str]) -> None:
        """Delete specific vectors by their UUID string IDs."""
        if not vector_ids:
            return
        self._qdrant.delete(
            collection_name=settings.QDRANT_COLLECTION_NAME,
            points_selector=PointIdsList(points=vector_ids),
        )

    # ── Private ───────────────────────────────────────────────────────────────

    def _get_embeddings(
        self,
        texts: list[str],
        input_type: str = "search_document",
    ) -> list[list[float]]:
        """
        Embed texts qua provider đang cấu hình (settings.EMBEDDING_PROVIDER).
        Auto-batch 100 texts/request (giới hạn chung của cả Gemini & Cloudflare).
        """
        provider = settings.EMBEDDING_PROVIDER.lower()
        batch_size = 100
        all_embeddings: list[list[float]] = []

        if provider == "cloudflare":
            for i in range(0, len(texts), batch_size):
                batch = texts[i: i + batch_size]
                all_embeddings.extend(_embed_cloudflare_batch(batch))
            return all_embeddings

        # default: gemini
        client = _get_gemini_client()
        task_type = _TASK_TYPE_MAP.get(input_type, "RETRIEVAL_DOCUMENT")
        for i in range(0, len(texts), batch_size):
            batch = texts[i: i + batch_size]
            response = self._embed_batch_with_retry(client, batch, task_type)
            all_embeddings.extend(e.values for e in response.embeddings)
        return all_embeddings

    def _embed_batch_with_retry(self, client, batch: list[str], task_type: str):
        """Gọi embed_content với throttle + retry khi gặp 429 RESOURCE_EXHAUSTED."""
        max_retries = settings.EMBEDDING_MAX_RETRIES
        attempt = 0
        while True:
            _throttle()
            try:
                return client.models.embed_content(
                    model=settings.EMBEDDING_MODEL,
                    contents=batch,
                    config=genai_types.EmbedContentConfig(
                        task_type=task_type,
                        output_dimensionality=settings.EMBEDDING_DIMENSIONS,
                    ),
                )
            except Exception as exc:
                if not _is_rate_limit_error(exc) or attempt >= max_retries:
                    raise
                delay = _parse_retry_delay(exc)
                attempt += 1
                logger.warning(
                    f"Embedder | 429 rate limit, retry {attempt}/{max_retries} "
                    f"after {delay:.0f}s"
                )
                time.sleep(delay)

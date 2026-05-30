# app/core/config.py
from functools import lru_cache
from typing import List

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # Application
    APP_NAME: str = "Admin Procedure AI"
    APP_VERSION: str = "1.0.0"
    DEBUG: bool = False
    ENVIRONMENT: str = "development"

    # API
    API_V1_PREFIX: str = "/api/v1"
    ALLOWED_ORIGINS: str = "http://localhost:3000,http://localhost:5173"

    @property
    def allowed_origins_list(self) -> List[str]:
        """Parse ALLOWED_ORIGINS từ string hoặc JSON array."""
        import json
        v = self.ALLOWED_ORIGINS.strip()
        if v.startswith("["):
            try:
                return json.loads(v)
            except json.JSONDecodeError:
                pass
        return [origin.strip() for origin in v.split(",") if origin.strip()]

    # Database
    DB_HOST: str = "localhost"
    DB_PORT: int = 3306
    DB_USER: str = "admin_ai_user"
    DB_PASSWORD: str = "password"
    DB_NAME: str = "admin_procedure_ai"
    DB_POOL_SIZE: int = 10
    DB_MAX_OVERFLOW: int = 20
    DB_ECHO: bool = False

    @property
    def DATABASE_URL(self) -> str:
        return (
            f"mysql+aiomysql://{self.DB_USER}:{self.DB_PASSWORD}"
            f"@{self.DB_HOST}:{self.DB_PORT}/{self.DB_NAME}"
            f"?charset=utf8mb4"
        )

    # Redis
    REDIS_URL: str = "redis://localhost:6379/0"
    CELERY_BROKER_URL: str = "redis://localhost:6379/1"
    CELERY_RESULT_BACKEND: str = "redis://localhost:6379/2"

    # Qdrant
    QDRANT_COLLECTION_NAME: str = "procedure_chunks"
    # Cloud mode: set QDRANT_URL + QDRANT_API_KEY, leave QDRANT_PERSIST_DIR empty
    QDRANT_URL: str = ""
    QDRANT_API_KEY: str = ""
    # Local/embedded mode: set QDRANT_PERSIST_DIR to a folder path
    QDRANT_PERSIST_DIR: str = ""
    # Self-hosted Docker fallback
    QDRANT_HOST: str = "localhost"
    QDRANT_PORT: int = 6333

    # ── LLM (Chat) — OpenRouter ───────────────────────────────────────────────
    LLM_API_KEY: str = ""
    LLM_BASE_URL: str = "https://openrouter.ai/api/v1"
    LLM_MODEL: str = "qwen/qwen3-6b:free"              # model sinh câu trả lời
    # Gemini 2.5 là thinking model → thinking tokens và output dùng CHUNG budget này.
    # Để đủ chỗ cho cả thinking (~1000-1500) lẫn output dài (vài Bước thực hiện),
    # đặt 4000 trở lên. Gemini 2.5 Flash hỗ trợ tới 8192.
    LLM_MAX_TOKENS: int = 4000
    LLM_TEMPERATURE: float = 0.1

    # ── Embedding (provider switch) ───────────────────────────────────────────
    # "gemini"     → Google gemini-embedding-001 (3072d), chất lượng tốt nhưng
    #                free tier giới hạn chặt (100/phút, 1000/ngày).
    # "cloudflare" → Cloudflare Workers AI @cf/baai/bge-m3 (1024d), đa ngôn ngữ,
    #                free tier thoáng (~10k Neurons/ngày), ít bị 429.
    # ⚠ Đổi provider → BẮT BUỘC reset Qdrant collection (dimensions khác nhau).
    EMBEDDING_PROVIDER: str = "gemini"

    # -- Gemini --
    # Lấy API key tại: https://aistudio.google.com/app/apikey
    GEMINI_API_KEY: str = ""
    GEMINI_EMBEDDING_MODEL: str = "gemini-embedding-001"
    GEMINI_EMBEDDING_DIMENSIONS: int = 3072
    # Throttle chủ động (chỉ áp dụng cho Gemini free tier ~100 req/phút).
    # 0.7s ≈ 85 req/phút. Đặt 0 để tắt (khi đã lên Tier 1 / dùng Cloudflare).
    EMBEDDING_MIN_INTERVAL_SEC: float = 0.7
    # Số lần retry khi gặp 429 (tôn trọng retryDelay từ API).
    EMBEDDING_MAX_RETRIES: int = 6

    # -- Cloudflare Workers AI --
    # Account ID + API token tại: https://dash.cloudflare.com → AI → Workers AI
    CLOUDFLARE_ACCOUNT_ID: str = ""
    CLOUDFLARE_API_TOKEN: str = ""
    CLOUDFLARE_EMBEDDING_MODEL: str = "@cf/baai/bge-m3"
    CLOUDFLARE_EMBEDDING_DIMENSIONS: int = 1024

    # -- Active model/dims (resolve theo provider) --
    @property
    def EMBEDDING_MODEL(self) -> str:
        if self.EMBEDDING_PROVIDER.lower() == "cloudflare":
            return self.CLOUDFLARE_EMBEDDING_MODEL
        return self.GEMINI_EMBEDDING_MODEL

    @property
    def EMBEDDING_DIMENSIONS(self) -> int:
        if self.EMBEDDING_PROVIDER.lower() == "cloudflare":
            return self.CLOUDFLARE_EMBEDDING_DIMENSIONS
        return self.GEMINI_EMBEDDING_DIMENSIONS

    # --- Backward compat aliases ---
    @property
    def OPENAI_API_KEY(self) -> str:
        return self.LLM_API_KEY

    @property
    def OPENAI_LLM_MODEL(self) -> str:
        return self.LLM_MODEL

    @property
    def OPENAI_EMBEDDING_MODEL(self) -> str:
        return self.EMBEDDING_MODEL

    @property
    def OPENAI_MAX_TOKENS(self) -> int:
        return self.LLM_MAX_TOKENS

    @property
    def OPENAI_TEMPERATURE(self) -> float:
        return self.LLM_TEMPERATURE

    # JWT
    JWT_SECRET_KEY: str = "change-this-in-production"
    JWT_ALGORITHM: str = "HS256"
    ACCESS_TOKEN_EXPIRE_MINUTES: int = 60
    REFRESH_TOKEN_EXPIRE_DAYS: int = 30

    # RAG
    RAG_TOP_K: int = 5
    RAG_SCORE_THRESHOLD: float = 0.35
    RAG_MAX_CONTEXT_CHUNKS: int = 8
    RAG_CHUNK_SIZE: int = 512
    RAG_CHUNK_OVERLAP: int = 64

    # Crawler
    CRAWLER_DELAY_MIN: float = 1.5
    CRAWLER_DELAY_MAX: float = 3.5
    CRAWLER_MAX_RETRIES: int = 3
    CRAWLER_TIMEOUT: int = 30
    CRAWL_SCHEDULE_HOUR: int = 2
    CRAWL_SCHEDULE_MINUTE: int = 0
    # Thư mục chứa các file .xlsx danh sách thủ tục (1 file/bộ-ngành)
    # Code lấy mã TTHC từ cột "Mã TTHC" rồi gọi rest.jsp + export_word_detail_tthc.jsp.
    XLSX_DATA_DIR: str = "./data/tthc"
    # Concurrency cho fetch song song khi crawl theo danh sách mã
    XLSX_CRAWL_CONCURRENCY: int = 5


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()

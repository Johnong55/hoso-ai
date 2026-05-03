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

    # ChromaDB
    CHROMA_HOST: str = "localhost"
    CHROMA_PORT: int = 8000
    CHROMA_COLLECTION_NAME: str = "procedure_chunks"
    CHROMA_PERSIST_DIR: str = "./chroma_data"

    # ── LLM (Chat) — OpenRouter ───────────────────────────────────────────────
    LLM_API_KEY: str = ""
    LLM_BASE_URL: str = "https://openrouter.ai/api/v1"
    LLM_MODEL: str = "qwen/qwen3-6b:free"              # model sinh câu trả lời
    LLM_MAX_TOKENS: int = 1500
    LLM_TEMPERATURE: float = 0.1

    # ── Embedding — Cohere ────────────────────────────────────────────────────
    # Lấy API key tại: https://dashboard.cohere.com/api-keys
    # Models: embed-multilingual-v3.0 (1024 dims) | embed-multilingual-light-v3.0 (384 dims)
    COHERE_API_KEY: str = ""
    EMBEDDING_MODEL: str = "embed-multilingual-v3.0"
    EMBEDDING_DIMENSIONS: int = 1024

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


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()

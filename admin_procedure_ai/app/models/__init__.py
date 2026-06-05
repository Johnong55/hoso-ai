# app/models/__init__.py
"""
Import all models here so SQLAlchemy metadata is fully populated
and Alembic can auto-generate migrations that include every table.
"""
from app.models.user import User, UserRole  # noqa: F401
from app.models.procedure import (  # noqa: F401
    Procedure,
    ProcedureRequirement,
    ProcedureStep,
    ProcedureFee,
    ProcedureLocality,
    Locality,
    ProcedureStatus,
    AuthorityLevel,
)
from app.models.document import (  # noqa: F401
    DocumentSource,
    DocumentChunk,
    CrawlFrequency,
    CrawlStatus,
    ProcessingStatus,
    EmbeddingStatus,
    ChunkType,
)
from app.models.conversation import (  # noqa: F401
    ConversationSession,
    Message,
    MessageRole,
    RAGQuery,
    RAGRetrieval,
    RAGGenerationLog,
)
from app.models.feedback import Feedback  # noqa: F401
from app.models.settings import AISettings, SystemLog  # noqa: F401

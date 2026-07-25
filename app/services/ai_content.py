from sqlalchemy.orm import Session

from app.config import get_settings
from app.errors import AppError
from app.models import AIAnalysis, XPost
from app.providers.llm_provider import LLMUnavailableError, OpenAICompatibleProvider


def analyze_x_post(db: Session, post: XPost) -> AIAnalysis:
    settings = get_settings()
    provider = OpenAICompatibleProvider(settings)
    try:
        result = provider.analyze(post.content)
    except LLMUnavailableError as exc:
        analysis = AIAnalysis(
            content_type="x_post",
            content_id=post.id,
            source_url=post.url,
            source_time=post.published_at,
            model=settings.llm_model or "not-configured",
            status="failed",
            error=str(exc),
        )
        db.add(analysis)
        db.commit()
        raise AppError(503, "LLM_UNAVAILABLE", str(exc)) from exc
    analysis = AIAnalysis(
        content_type="x_post",
        content_id=post.id,
        source_url=post.url,
        source_time=post.published_at,
        model=settings.llm_model,
        status="success",
        result=result.model_dump(),
    )
    db.add(analysis)
    db.commit()
    db.refresh(analysis)
    return analysis


def test_llm_provider_connection() -> dict:
    try:
        return OpenAICompatibleProvider(get_settings()).test_connection()
    except LLMUnavailableError as exc:
        raise AppError(503, "LLM_CONNECTION_FAILED", str(exc)) from exc

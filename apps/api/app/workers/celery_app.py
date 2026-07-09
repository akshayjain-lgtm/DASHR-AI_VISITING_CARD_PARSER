from celery import Celery

from app.core.config import settings

celery_app = Celery(
    "dashr_ai",
    broker=settings.redis_url,
    backend=settings.redis_url,
    include=["app.workers.card_processing", "app.workers.enrichment_processing"],
)

celery_app.conf.update(
    task_serializer="json",
    accept_content=["json"],
    result_serializer="json",
    task_default_queue="cards",
)

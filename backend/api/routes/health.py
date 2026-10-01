"""GET /api/health — liveness probe for the local API."""

from fastapi import APIRouter, Request

from ..schemas import HealthResponse

router = APIRouter()


@router.get("/api/health", response_model=HealthResponse, tags=["health"])
def health(request: Request) -> HealthResponse:
    """Confirm the API process is running. Exposes no secrets.

    ``cache_backend`` reports the configured cache backend (``sqlite``
    or ``redis``) — never URLs, passwords or environment values.
    """
    service = request.app.state.service
    backend = getattr(service, "cache_backend", "sqlite")
    return HealthResponse(
        status="ok", service="LLMCacheX", cache_backend=backend
    )

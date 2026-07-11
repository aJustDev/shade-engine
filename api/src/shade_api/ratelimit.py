"""Minimal per-IP rate limiting as pure ASGI middleware.

Built directly on the ``limits`` library. slowapi (its FastAPI wrapper) was
the plan, but its middleware resolves the route handler by looking for an
``endpoint`` attribute on ``app.routes`` entries, and fastapi >= 0.139 wraps
included routers in objects without one -- every route silently becomes
exempt. Checking the limit ourselves is fewer lines than working around it.
"""

from limits import RateLimitItem
from limits.storage import MemoryStorage
from limits.strategies import MovingWindowRateLimiter
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send


class RateLimitMiddleware:
    """Reject requests over ``limit`` per client IP and path with a 429.

    The moving-window counters live in process memory: like any in-memory
    limiter this is per-worker, and behind a proxy the client IP needs
    forwarded-headers handling (uvicorn --proxy-headers) -- both deploy-phase
    concerns.
    """

    def __init__(self, app: ASGIApp, limit: RateLimitItem) -> None:
        self.app = app
        self.limit = limit
        self.strategy = MovingWindowRateLimiter(MemoryStorage())

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        client = scope.get("client")
        host = client[0] if client else "unknown"
        if not self.strategy.hit(self.limit, host, scope["path"]):
            response = JSONResponse(
                {"detail": f"rate limit exceeded: {self.limit}"}, status_code=429
            )
            await response(scope, receive, send)
            return
        await self.app(scope, receive, send)

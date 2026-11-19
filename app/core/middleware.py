"""Rate limiting and security headers.

The rate limiter is in-process and therefore per-worker. That is a real
limitation and it is documented rather than hidden: with N uvicorn workers the
effective limit is N times the configured value. Redis is the production answer
and the interface here is deliberately small enough to swap.
"""

from __future__ import annotations

import time
from collections import defaultdict, deque
from collections.abc import Callable

from fastapi import Request, Response, status
from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware

from app.core.config import settings


class SlidingWindowLimiter:
    """A sliding-window counter keyed by client.

    A fixed window lets a client send the full quota at 0:59 and again at 1:00,
    which is double the intended rate at the boundary. A deque of timestamps
    costs a little more memory and has no boundary artefact.
    """

    def __init__(self, max_requests: int, window_seconds: int) -> None:
        self.max_requests = max_requests
        self.window_seconds = window_seconds
        self._hits: dict[str, deque[float]] = defaultdict(deque)

    def check(self, key: str, now: float | None = None) -> tuple[bool, int]:
        """Return (allowed, remaining)."""
        now = now if now is not None else time.monotonic()
        cutoff = now - self.window_seconds
        bucket = self._hits[key]

        while bucket and bucket[0] <= cutoff:
            bucket.popleft()

        if len(bucket) >= self.max_requests:
            return False, 0

        bucket.append(now)
        return True, self.max_requests - len(bucket)

    def reset(self) -> None:
        self._hits.clear()


def _client_key(request: Request) -> str:
    """Identify the caller.

    X-Forwarded-For is only trusted for the first hop because a client can set
    the header themselves; behind a proxy the left-most entry is the real
    client, and without a proxy the header is attacker-controlled, so the socket
    address is used instead.
    """
    forwarded = request.headers.get("x-forwarded-for")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


class RateLimitMiddleware(BaseHTTPMiddleware):
    """Two buckets: a generous one for reads, a tight one for auth."""

    def __init__(self, app) -> None:
        super().__init__(app)
        self.general = SlidingWindowLimiter(
            settings.rate_limit_requests, settings.rate_limit_window_seconds
        )
        self.auth = SlidingWindowLimiter(
            settings.auth_rate_limit_requests,
            settings.auth_rate_limit_window_seconds,
        )

    async def dispatch(self, request: Request, call_next: Callable) -> Response:
        path = request.url.path

        # Health checks and static assets must never be rate limited: a probe
        # that gets a 429 looks like an outage.
        if path.startswith("/static") or path in ("/health", "/healthz", "/"):
            return await call_next(request)

        limiter = self.auth if path.startswith("/api/auth") else self.general
        key = _client_key(request)
        allowed, remaining = limiter.check(key)

        if not allowed:
            return JSONResponse(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                content={"detail": "Too many requests. Slow down and try again shortly."},
                headers={
                    "Retry-After": str(limiter.window_seconds),
                    "X-RateLimit-Limit": str(limiter.max_requests),
                    "X-RateLimit-Remaining": "0",
                },
            )

        response = await call_next(request)
        response.headers["X-RateLimit-Limit"] = str(limiter.max_requests)
        response.headers["X-RateLimit-Remaining"] = str(remaining)
        return response


class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    """Baseline hardening headers.

    The CSP allows the inline styles and scripts the dashboard needs while
    blocking everything external, which is the right trade for a single-page
    operator console with no third-party assets.
    """

    async def dispatch(self, request: Request, call_next: Callable) -> Response:
        response = await call_next(request)
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("X-Frame-Options", "DENY")
        response.headers.setdefault("Referrer-Policy", "no-referrer")
        response.headers.setdefault(
            "Permissions-Policy", "geolocation=(), microphone=(), camera=()"
        )
        response.headers.setdefault(
            "Content-Security-Policy",
            "default-src 'self'; "
            "script-src 'self' 'unsafe-inline'; "
            "style-src 'self' 'unsafe-inline'; "
            "img-src 'self' data:; "
            "connect-src 'self'; "
            "font-src 'self'; "
            "object-src 'none'; "
            "base-uri 'self'; "
            "frame-ancestors 'none'",
        )
        if settings.is_production:
            response.headers.setdefault(
                "Strict-Transport-Security", "max-age=31536000; includeSubDomains"
            )
        return response

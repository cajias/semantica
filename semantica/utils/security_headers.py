"""Semantica : security response headers shared by both ASGI entry points.

``semantica/server.py`` (the ``semantica-server`` command) and
``semantica/explorer/app.py`` (what the Docker image and the App Runner service
run) are two FastAPI applications mounting the same routers. A header policy
installed on only one of them is a policy the internet-facing app may not have,
which is exactly what happened before this module existed: ``server.py`` set the
headers and the Explorer app -- the one the container starts -- set none. This
is the single definition both add, for the same reason ``semantica.utils.cors``
is the single CORS resolver.

HSTS keys off ``X-Forwarded-Proto`` as well as the request scheme. TLS is
terminated by the load balancer (App Runner, an ALB, a reverse proxy), so the
request uvicorn sees is plain HTTP and ``request.url.scheme`` is never
``https`` on a deployed service. A forged ``X-Forwarded-Proto`` only causes an
HSTS header on a plain-HTTP response, which browsers are required to ignore.
"""

from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.requests import Request
from starlette.responses import Response

#: Sent on every response, whatever the transport.
STATIC_SECURITY_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "X-XSS-Protection": "1; mode=block",
    "Referrer-Policy": "strict-origin-when-cross-origin",
    "Permissions-Policy": "geolocation=(), microphone=(), camera=()",
}

HSTS_HEADER = "Strict-Transport-Security"
HSTS_VALUE = "max-age=31536000; includeSubDomains"


class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    """Add the static security headers, plus HSTS when the client used TLS."""

    async def dispatch(
        self, request: Request, call_next: RequestResponseEndpoint
    ) -> Response:
        response = await call_next(request)
        response.headers.update(STATIC_SECURITY_HEADERS)
        # A proxy chain appends, so the client-facing scheme is the first hop.
        forwarded = request.headers.get("x-forwarded-proto", "").split(",")[0]
        if (forwarded.strip().lower() or request.url.scheme) == "https":
            response.headers[HSTS_HEADER] = HSTS_VALUE
        return response

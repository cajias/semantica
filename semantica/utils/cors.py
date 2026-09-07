"""
Semantica : CORS configuration shared by both ASGI entry points.

``semantica/server.py`` (the ``semantica-server`` command) and
``semantica/explorer/app.py`` (what the published Docker image runs) are two
FastAPI applications mounting the same routers, and both need the same answer to
one question: which browser origins may talk to this deployment?

They used to answer it independently. ``server.py`` read
``SEMANTICA_CORS_ORIGINS``; the Explorer read ``ALLOWED_ORIGINS``, falling back
to ``EXPLORER_CORS_ORIGINS``. Every default named localhost, so an operator who
configured the name the *other* entry point honoured got localhost-only CORS
with nothing in the logs to explain why the browser refused every request. This
module is the only reader of those names, so the two apps cannot drift apart
again.

``SEMANTICA_CORS_ORIGINS`` is canonical. It matches the namespace the rest of
the deployment surface already uses (``SEMANTICA_API_KEY``,
``SEMANTICA_ALLOW_ANONYMOUS``, ``SEMANTICA_SNAPSHOT_URI``), and an unprefixed
``ALLOWED_ORIGINS`` is a name any co-located process or platform-injected
ConfigMap can occupy by accident -- the same silent-capture failure this module
exists to close. The older names keep working one rung lower, each with a
warning naming its replacement, so no manifest that already ships them breaks.

Precedence, highest first:

1. ``SEMANTICA_CORS_ORIGINS``  (canonical)
2. ``ALLOWED_ORIGINS``         (deprecated)
3. ``EXPLORER_CORS_ORIGINS``   (deprecated)
4. the localhost development default

The canonical name outranks the others deliberately: the Docker image bakes
``ALLOWED_ORIGINS`` into its own ``ENV``, so a container always has it set. Were
it to win, setting ``SEMANTICA_CORS_ORIGINS`` on a container would do nothing at
all. Between the two deprecated names the order is the one the Explorer already
used, so an operator setting both sees no change in effective configuration.

Credentials follow the same shape (``SEMANTICA_CORS_CREDENTIALS``, with
``EXPLORER_CORS_CREDENTIALS`` honoured and deprecated) and default to off.
Credentials let browsers attach cookies and HTTP auth to cross-origin requests;
the ``X-API-Key`` scheme both apps use is an ordinary header and needs none of
that, so enabling them only widens cross-site exposure.

A ``"*"`` origin is reported rather than refused -- raising here would abort
``semantica.server`` during module import, where the traceback is least legible
and the operator learns least. The one case that *is* corrected is ``"*"``
together with credentials. Starlette does not reject that pair: it answers it by
reflecting the request's own ``Origin`` alongside
``Access-Control-Allow-Credentials: true``, turning "wildcard, therefore no
credentials" into "every site may make credentialed requests". Credentials are
dropped in that case and the reason logged.
"""

import logging
import os
from typing import List, NamedTuple, Optional, Tuple

logger = logging.getLogger(__name__)

CANONICAL_ORIGINS_ENV = "SEMANTICA_CORS_ORIGINS"
DEPRECATED_ORIGINS_ENV = ("ALLOWED_ORIGINS", "EXPLORER_CORS_ORIGINS")

CANONICAL_CREDENTIALS_ENV = "SEMANTICA_CORS_CREDENTIALS"
DEPRECATED_CREDENTIALS_ENV = ("EXPLORER_CORS_CREDENTIALS",)

#: Vite's dev server on both spellings of loopback. Correct for development,
#: which is why an unconfigured process is not warned at; production is expected
#: to set CANONICAL_ORIGINS_ENV to its real domain.
DEFAULT_ORIGINS = "http://localhost:5173,http://127.0.0.1:5173"


class CorsSettings(NamedTuple):
    """The resolved answer both entry points hand to ``CORSMiddleware``."""

    origins: List[str]
    allow_credentials: bool


def _read_first_configured(
    canonical: str, deprecated: Tuple[str, ...]
) -> Optional[str]:
    """Return the value of the highest-precedence name that is set, or None.

    Presence decides, not truthiness: an explicit ``SEMANTICA_CORS_ORIGINS=""``
    means "no cross-origin access", a deliberate choice that must not fall
    through to the localhost default. Both entry points behaved this way before.

    Warning on the deprecated names lives here rather than at the call sites so
    that a setting cannot be added later and quietly skip its deprecation
    notice. Resolution happens once per process (both apps build their
    middleware at construction), so this warns once.
    """
    for name in (canonical,) + deprecated:
        if name in os.environ:
            if name != canonical:
                logger.warning(
                    "%s is deprecated and will stop being read in a future "
                    "release; set %s instead. Honouring %s for now.",
                    name,
                    canonical,
                    name,
                )
            return os.environ[name]
    return None


def resolve_cors_settings() -> CorsSettings:
    """Resolve the CORS origins and credentials flag from the environment.

    The single source of truth for both ``semantica/server.py`` and
    ``semantica/explorer/app.py``. See the module docstring for the precedence
    order and the reasoning behind it.
    """
    raw_origins = _read_first_configured(CANONICAL_ORIGINS_ENV, DEPRECATED_ORIGINS_ENV)
    if raw_origins is None:
        raw_origins = DEFAULT_ORIGINS
    origins = [origin.strip() for origin in raw_origins.split(",") if origin.strip()]

    raw_credentials = _read_first_configured(
        CANONICAL_CREDENTIALS_ENV, DEPRECATED_CREDENTIALS_ENV
    )
    allow_credentials = (raw_credentials or "false").strip().lower() == "true"

    if "*" in origins:
        if allow_credentials:
            logger.warning(
                'CORS origins include "*" while credentialed cross-origin '
                "requests are enabled. Starlette answers that pair by "
                "reflecting the request's own Origin together with "
                "Access-Control-Allow-Credentials: true, which lets any site "
                "make credentialed requests to this deployment. Ignoring the "
                "credentials opt-in -- set %s to the deployment's real "
                "origin(s) if credentials are genuinely required.",
                CANONICAL_ORIGINS_ENV,
            )
            allow_credentials = False
        else:
            logger.warning(
                'CORS origins include "*", so any site may make cross-origin '
                "requests to this deployment. Set %s to the deployment's "
                "actual origin(s).",
                CANONICAL_ORIGINS_ENV,
            )

    return CorsSettings(origins=origins, allow_credentials=allow_credentials)

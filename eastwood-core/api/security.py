"""Route-security policy for every externally reachable eastwood-core route.

One explicit classification table decides how each path is protected; a single
app-level dependency (enforce_route_security) applies it. No route carries its
own scattered auth check.

  PUBLIC                   /health, /metrics — no credentials. /health reveals
                           nothing but liveness; /metrics is scraped by
                           Prometheus on the internal Docker network and is not
                           proxied by nginx.
  TWILIO_WEBHOOK           /twiml/*, /twilio/* — X-Twilio-Signature validated
                           against the public URL Twilio signed. No Bearer key:
                           Twilio cannot send one.
  PROVIDER_SIGNED_WEBHOOK  routes carrying a third-party signature scheme the
                           route itself enforces (Facebook X-Hub-Signature-256).
  AUTHENTICATED            every other /api/* route — Authorization: Bearer
                           with the EASTWOOD_API_KEY, compared constant-time.

Anything not classified defaults to AUTHENTICATED, so a new route is protected
until someone deliberately opens it.

Logging discipline: only the route path, the result, a request id, and a safe
reason code are ever logged here — never tokens, signatures, bodies, phone
numbers, or health data.
"""
from __future__ import annotations

import hmac
import logging
import uuid
from enum import Enum

from fastapi import HTTPException, Request, status

from config.settings import get_settings

logger = logging.getLogger(__name__)


class RoutePolicy(str, Enum):
    PUBLIC = "public"
    TWILIO_WEBHOOK = "twilio_webhook"
    PROVIDER_SIGNED_WEBHOOK = "provider_signed_webhook"
    AUTHENTICATED = "authenticated"


EXACT_ROUTE_POLICIES: dict[str, RoutePolicy] = {
    "/health": RoutePolicy.PUBLIC,
    "/metrics": RoutePolicy.PUBLIC,
    "/api/webhooks/facebook": RoutePolicy.PROVIDER_SIGNED_WEBHOOK,
}

PREFIX_ROUTE_POLICIES: tuple[tuple[str, RoutePolicy], ...] = (
    ("/twiml/", RoutePolicy.TWILIO_WEBHOOK),
    ("/twilio/", RoutePolicy.TWILIO_WEBHOOK),
    ("/api/", RoutePolicy.AUTHENTICATED),
)


def classify_route(path: str) -> RoutePolicy:
    policy = EXACT_ROUTE_POLICIES.get(path)
    if policy is not None:
        return policy
    for prefix, prefix_policy in PREFIX_ROUTE_POLICIES:
        if path.startswith(prefix):
            return prefix_policy
    # Unlisted paths (docs, openapi, future additions) fail closed.
    return RoutePolicy.AUTHENTICATED


def pinned_tenant_id() -> str:
    """The tenant every authenticated request operates on.

    Single-tenant deployment: the API credential maps to the configured default
    tenant. Callers can never select a tenant through query or body data — a
    full multi-tenant identity system is deliberately out of scope here.
    """
    return get_settings().default_tenant_id


def _request_id(request: Request) -> str:
    return request.headers.get("x-request-id") or uuid.uuid4().hex[:12]


def _deny(request: Request, status_code: int, reason: str) -> HTTPException:
    logger.warning(
        "route security denied: route=%s result=denied reason=%s request_id=%s",
        request.url.path,
        reason,
        _request_id(request),
    )
    headers = {"WWW-Authenticate": "Bearer"} if status_code == status.HTTP_401_UNAUTHORIZED else None
    detail = "authentication required" if status_code == status.HTTP_401_UNAUTHORIZED else "forbidden"
    return HTTPException(status_code=status_code, detail=detail, headers=headers)


# ── Operational API authentication ───────────────────────────────────────────

def require_api_key(request: Request) -> None:
    """Authorization: Bearer <EASTWOOD_API_KEY> — 401 absent, 403 invalid.

    The comparison is constant-time (hmac.compare_digest). An unconfigured
    server key means no credential can succeed: fail closed, never open.
    """
    authorization = request.headers.get("authorization", "")
    if not authorization:
        raise _deny(request, status.HTTP_401_UNAUTHORIZED, "missing_credentials")

    scheme, _, token = authorization.partition(" ")
    token = token.strip()
    if scheme.lower() != "bearer" or not token:
        raise _deny(request, status.HTTP_401_UNAUTHORIZED, "malformed_credentials")

    configured = get_settings().eastwood_api_key.strip()
    if not configured:
        raise _deny(request, status.HTTP_403_FORBIDDEN, "server_key_unconfigured")

    if not hmac.compare_digest(token.encode(), configured.encode()):
        raise _deny(request, status.HTTP_403_FORBIDDEN, "invalid_credentials")


# ── Twilio webhook signature validation ──────────────────────────────────────

def _external_url(request: Request) -> str:
    """Reconstruct the exact public URL Twilio signed.

    PUBLIC_BASE_URL is the canonical origin — it is the base every callback URL
    handed to Twilio is built from (workers/brain.py), so attacker-supplied
    forwarded headers can never alter the validated URL when it is set. Without
    it, the X-Forwarded-Proto / X-Forwarded-Host values apply — nginx overwrites
    those deterministically at the edge, so they are trusted-proxy values, not
    client input. Path and query string always come from the request itself.
    """
    settings = get_settings()
    base = settings.public_base_url.strip().rstrip("/")
    if base:
        origin = base
    else:
        proto = request.headers.get("x-forwarded-proto") or request.url.scheme
        host = (
            request.headers.get("x-forwarded-host")
            or request.headers.get("host")
            or request.url.netloc
        )
        origin = f"{proto}://{host}"
    url = f"{origin}{request.url.path}"
    if request.url.query:
        url = f"{url}?{request.url.query}"
    return url


async def require_twilio_signature(request: Request) -> None:
    """Reject any Twilio-originated request whose X-Twilio-Signature is missing
    or does not validate — HTTP 403 either way.

    Uses Twilio's official RequestValidator. Form-encoded requests validate over
    the exact submitted form parameters; JSON requests follow Twilio's
    documented behavior (the signed URL carries a bodySHA256 query parameter and
    the validator checks the body hash); GET requests validate over the URL
    including its query string.
    """
    settings = get_settings()
    request_id = _request_id(request)

    if not settings.twilio_signature_validation_enabled:
        # Development-only escape hatch — explicit configuration, loudly logged
        # both here and at startup; production refuses to boot with it off.
        logger.warning(
            "twilio signature validation skipped: route=%s result=skipped "
            "reason=validation_disabled request_id=%s",
            request.url.path,
            request_id,
        )
        return

    signature = request.headers.get("x-twilio-signature")
    if not signature:
        raise _deny(request, status.HTTP_403_FORBIDDEN, "missing_signature")

    if not settings.twilio_auth_token:
        # Without the auth token no signature can be verified. Fail closed —
        # never wave a webhook through because configuration is incomplete.
        raise _deny(request, status.HTTP_403_FORBIDDEN, "auth_token_unconfigured")

    from twilio.request_validator import RequestValidator

    validator = RequestValidator(settings.twilio_auth_token)
    url = _external_url(request)
    content_type = request.headers.get("content-type", "").lower()

    if content_type.startswith(("application/x-www-form-urlencoded", "multipart/form-data")):
        form = await request.form()
        valid = validator.validate(url, dict(form), signature)
    elif "application/json" in content_type:
        # Twilio signs JSON webhooks over the URL with a bodySHA256 query
        # parameter; a JSON request without one cannot be a valid Twilio
        # webhook and is rejected rather than guessed at.
        if "bodySHA256" not in request.query_params:
            raise _deny(request, status.HTTP_403_FORBIDDEN, "missing_body_hash")
        body = (await request.body()).decode("utf-8", errors="replace")
        valid = validator.validate(url, body, signature)
    else:
        valid = validator.validate(url, {}, signature)

    if not valid:
        raise _deny(request, status.HTTP_403_FORBIDDEN, "invalid_signature")

    logger.info(
        "twilio signature validated: route=%s result=ok request_id=%s",
        request.url.path,
        request_id,
    )


# ── Single enforcement point ─────────────────────────────────────────────────

async def enforce_route_security(request: Request) -> None:
    policy = classify_route(request.url.path)
    if policy is RoutePolicy.PUBLIC:
        return
    if policy is RoutePolicy.TWILIO_WEBHOOK:
        await require_twilio_signature(request)
        return
    if policy is RoutePolicy.PROVIDER_SIGNED_WEBHOOK:
        # The route enforces its provider's own signature scheme (and fails
        # closed when that provider's secret is unconfigured).
        return
    require_api_key(request)

"""
FastAPI surface for the product backend.

Runs on :8001, beside the AI service on :8000. They are separate processes with
separate database roles and separate dependency sets, connected by exactly one
thing: this service publishes public keys, and that one verifies against them.

Endpoints are `def`, not `async def`. SQLAlchemy and argon2 are synchronous and
argon2 is *deliberately* slow — declaring these coroutines would block the
event loop for the whole hash and stall every other request. As plain `def`,
Starlette runs them in its threadpool, where blocking is fine.
"""

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from product.api import admin, auth, orgs, wellknown
from product.config import settings
from product.db.base import engine
from product.security import keys

log = logging.getLogger("product.api")


@asynccontextmanager
async def lifespan(app: FastAPI):
    engine()  # fail fast on a bad DATABASE_URL rather than on first request
    try:
        active = keys.active()
        log.info("signing key ready, kid=%s", active.kid)
    except keys.NoSigningKey as exc:
        # Not fatal at boot: the service can still serve health checks, and a
        # loud log plus a failing /readyz is more useful than a crash loop that
        # hides the one-line fix.
        log.error("%s", exc)
    yield


app = FastAPI(
    title="Law Agent Product Backend",
    version="1.0",
    lifespan=lifespan,
    docs_url="/docs",
)

app.add_middleware(
    CORSMiddleware,
    allow_origin_regex=settings().cors_origin_regex,
    allow_credentials=False,
    allow_methods=["GET", "POST", "DELETE", "OPTIONS"],
    allow_headers=["Authorization", "Content-Type", "X-Admin-Key"],
)


def _error(status: int, code: str, message: str) -> JSONResponse:
    return JSONResponse(
        status_code=status, content={"error": {"code": code, "message": message}}
    )


@app.exception_handler(HTTPException)
def _http_error(request: Request, exc: HTTPException):
    """One envelope for everything, matching the AI service's shape.

    Handlers raise HTTPException with a dict detail carrying `code`; anything
    that raises with a plain string still comes out in the same envelope rather
    than FastAPI's bare `{"detail": ...}`.
    """
    detail = exc.detail
    if isinstance(detail, dict) and "code" in detail:
        body = {"error": {"code": detail["code"], "message": detail.get("message", "")}}
    else:
        body = {"error": {"code": "error", "message": str(detail)}}
    return JSONResponse(status_code=exc.status_code, content=body, headers=exc.headers)


@app.exception_handler(RequestValidationError)
def _validation_error(request: Request, exc: RequestValidationError):
    """422 in the same envelope.

    The AI service has a known gap here — it still returns FastAPI's raw
    `detail` array, so a client has to branch on status for that one endpoint
    family. Not repeating that: one envelope, every status.
    """
    first = exc.errors()[0] if exc.errors() else {}
    location = ".".join(str(p) for p in first.get("loc", ())[1:]) or "body"
    return _error(
        422,
        "invalid_request",
        f"{location}: {first.get('msg', 'invalid value')}",
    )


# ── routes ────────────────────────────────────────────────────────────────

app.include_router(wellknown.router)
app.include_router(auth.router)
app.include_router(orgs.router)
app.include_router(admin.router)


@app.get("/healthz", tags=["health"])
def healthz():
    return {"ok": True}


@app.get("/readyz", tags=["health"])
def readyz():
    """Ready means it can actually issue a token: a signing key is loaded and
    the pool hands out a connection. A process that is up but keyless would
    accept traffic and fail every login."""
    try:
        active = keys.active()
    except keys.NoSigningKey as exc:
        return _error(503, "not_ready", str(exc)[:200])

    try:
        with engine().connect() as conn:
            conn.exec_driver_sql("SELECT 1")
    except Exception as exc:
        return _error(503, "not_ready", f"database unavailable: {exc}"[:200])

    return {"ok": True, "kid": active.kid, "jwks_url": settings().jwks_url}


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("product.main:app", host="127.0.0.1", port=8001, workers=1)

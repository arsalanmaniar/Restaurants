import logging

from fastapi import FastAPI, Request, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from sqlalchemy.exc import IntegrityError

from app.api import admin, auth, payments, restaurant, webhooks
from app.core.config import settings

logging.basicConfig(
    level=logging.DEBUG if settings.debug else logging.INFO,
    format="%(asctime)s %(levelname)-8s %(name)s: %(message)s",
)
# Even under DEBUG=true, httpx/httpcore's per-request connection/handshake logs
# drown out the app's own logs (a single Groq call produces ~15 DEBUG lines about
# TCP/TLS mechanics that no one debugging AbhiAya cares about). Pin them at
# WARNING so real errors still surface.
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)
logger = logging.getLogger(__name__)

app = FastAPI(title=settings.app_name, version="0.1.0")

app.add_middleware(
    CORSMiddleware,
    # effective_cors_origins merges the static dev list with CORS_ALLOWED_ORIGINS (Vercel
    # URL(s) set as an HF Spaces secret). In production this is the only allowlist —
    # the dev regex below is gated on debug=True so it never runs in HF Spaces.
    allow_origins=settings.effective_cors_origins,
    # Dev only: any localhost/127.0.0.1 port. In production this is None and only the
    # explicit allowlist applies, so we never open the API to arbitrary origins.
    allow_origin_regex=settings.cors_dev_origin_regex if settings.debug else None,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.exception_handler(IntegrityError)
async def integrity_error_handler(request: Request, exc: IntegrityError) -> JSONResponse:
    """A violated unique/check/FK constraint is a client mistake, not a server fault.

    Without this it surfaces as a 500 with a Postgres stack trace, which tells an
    attacker our schema and tells the dashboard nothing useful.
    """
    logger.warning("integrity error on %s %s: %s", request.method, request.url.path, exc.orig)
    return JSONResponse(
        status_code=status.HTTP_409_CONFLICT,
        content={"detail": "That conflicts with data that already exists."},
    )


@app.exception_handler(Exception)
async def unhandled_error_handler(request: Request, exc: Exception) -> JSONResponse:
    # Log the full trace for us; return an opaque message to the caller. Never echo
    # exception text to a client — it leaks internals.
    logger.exception("unhandled error on %s %s", request.method, request.url.path)
    return JSONResponse(
        status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
        content={"detail": "Something went wrong. Please try again."},
    )


app.include_router(auth.router)
app.include_router(restaurant.router)
app.include_router(admin.router)
app.include_router(payments.router)
app.include_router(webhooks.router)


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}

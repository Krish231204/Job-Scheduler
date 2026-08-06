"""Structured error responses.

Without this, FastAPI's default error body is just `{"detail": "..."}`
(or, for validation errors, `{"detail": [...]}`) with no machine-readable
error code and no consistent shape between "you sent bad input" and "you're
not authorized" and "the server blew up." Clients (and the dashboard's own
fetch() calls) shouldn't have to guess which shape they're getting back.

Every error response from this API now looks like:

    {"error": {"code": "not_found", "message": "Queue not found", "details": null}}

`code` is a short machine-readable slug derived from the HTTP status (see
`_CODE_BY_STATUS`); `message` is human-readable; `details` carries extra
structured info when we have it (e.g. per-field validation errors).
"""
import logging

from fastapi import FastAPI, Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, Response
from slowapi.errors import RateLimitExceeded
from starlette.exceptions import HTTPException as StarletteHTTPException

logger = logging.getLogger("jobsched.api")

_CODE_BY_STATUS = {
    400: "bad_request",
    401: "unauthorized",
    403: "forbidden",
    404: "not_found",
    409: "conflict",
    422: "validation_error",
    429: "rate_limited",
    500: "internal_error",
}


def _error_body(code: str, message: str, details=None) -> dict:
    return {"error": {"code": code, "message": message, "details": details}}


def register_error_handlers(app: FastAPI) -> None:
    @app.exception_handler(StarletteHTTPException)
    async def handle_http_exception(request: Request, exc: StarletteHTTPException):
        # Redirects (e.g. the dashboard's "not logged in" -> /login bounce)
        # aren't errors -- pass them through untouched rather than wrapping
        # a redirect in a JSON error body.
        if exc.status_code < 400:
            return Response(status_code=exc.status_code, headers=getattr(exc, "headers", None))

        code = _CODE_BY_STATUS.get(exc.status_code, "error")
        if exc.status_code >= 500:
            logger.error("Unhandled HTTP %s on %s %s: %s", exc.status_code, request.method, request.url.path, exc.detail)
        elif exc.status_code >= 400:
            logger.info("HTTP %s on %s %s: %s", exc.status_code, request.method, request.url.path, exc.detail)
        return JSONResponse(
            status_code=exc.status_code,
            content=_error_body(code, str(exc.detail)),
            headers=getattr(exc, "headers", None),
        )

    @app.exception_handler(RequestValidationError)
    async def handle_validation_error(request: Request, exc: RequestValidationError):
        logger.info("Validation error on %s %s: %s", request.method, request.url.path, exc.errors())
        return JSONResponse(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            content=_error_body("validation_error", "Request validation failed", details=exc.errors()),
        )

    @app.exception_handler(RateLimitExceeded)
    async def handle_rate_limit_exceeded(request: Request, exc: RateLimitExceeded):
        logger.info("Rate limit exceeded on %s %s: %s", request.method, request.url.path, exc.detail)
        return JSONResponse(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            content=_error_body("rate_limited", "Too many requests, please slow down."),
        )

    @app.exception_handler(Exception)
    async def handle_unexpected_error(request: Request, exc: Exception):
        logger.exception("Unhandled exception on %s %s", request.method, request.url.path)
        return JSONResponse(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            content=_error_body("internal_error", "An unexpected error occurred"),
        )

"""The V3 error envelope: `{"detail": {"error_type": …, "message": …}}`.

llm-studio's frontend switches on `detail.error_type`, so every error this API
emits — including FastAPI's own request-validation failures, converted by the
handler below — carries a stable machine-readable type next to the human
sentence. Extra keys (e.g. `errors` for lint findings, `refusals` for
pre-flight) ride along in the same object.
"""
from __future__ import annotations

from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse


# The route families this API owns. /v1/jobs is NOT here on purpose.
V3_PREFIXES = (
    "/v1/runs",
    "/v1/run-batches",
    "/v1/scenarios",
    "/v1/variable-sets",
    "/v1/targets",
)


def api_error(status_code: int, error_type: str, message: str, **extra) -> HTTPException:
    return HTTPException(
        status_code=status_code,
        detail={"error_type": error_type, "message": message, **extra},
    )


def install_error_envelope(app: FastAPI) -> None:
    @app.exception_handler(RequestValidationError)
    async def _validation_envelope(request: Request, exc: RequestValidationError):
        # Only rewrap V3 routes — /v1/jobs and the Twilio webhooks keep
        # FastAPI's stock shape, which their existing callers already parse.
        if not request.url.path.startswith(V3_PREFIXES):
            return JSONResponse(status_code=422, content={"detail": exc.errors()})
        errors = [
            {
                "field": ".".join(str(p) for p in err.get("loc", []) if p != "body"),
                "message": err.get("msg", ""),
            }
            for err in exc.errors()
        ]
        return JSONResponse(
            status_code=422,
            content={
                "detail": {
                    "error_type": "validation_error",
                    "message": "request payload failed validation",
                    "errors": errors,
                }
            },
        )

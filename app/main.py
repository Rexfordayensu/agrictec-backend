"""The AgriCtec API.

Run it with:  uvicorn app.main:app --reload   (from the backend folder; docs at /docs)."""

import asyncio
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import ValidationError

from app.config import get_settings
from app.core.errors import AppError
from app.core.events import bus
from app.core.sessions import IMPERSONATION_HEADER
from app.db import SessionLocal
from app.routers import admin_auth, admin_lending, admin_people, admin_platform, auth, me, platform
from app.services.backup import bootstrap


def _message(errors: list[dict]) -> str:
    """The first validation problem, readable: "amount: Input should be greater than 0"."""
    if not errors:
        return "Invalid data."
    first = errors[0]
    where = ".".join(str(p) for p in first.get("loc", ()) if p not in ("body", "query", "path"))
    text = str(first.get("msg", "Invalid value.")).removeprefix("Value error, ")
    return f"{where}: {text}" if where else text


@asynccontextmanager
async def lifespan(_: FastAPI):
    with SessionLocal() as db:
        bootstrap(db, seed_demo_data=get_settings().seed_demo_data)
    bus.start(asyncio.get_running_loop())
    yield
    bus.stop()


def create_app() -> FastAPI:
    settings = get_settings()
    app = FastAPI(
        title="AgriCtec API",
        version="1.0.0",
        description=(
            "Farm records, credit scores and loan access for farmers in Ghana, and the Super Admin "
            "Console that runs the platform. JSON is camelCase, matching the frontend's types. "
            "Browsers authenticate with httpOnly cookies; other clients send the sign-in token as "
            "`Authorization: Bearer <token>`."
        ),
        lifespan=lifespan,
        docs_url="/docs",
        openapi_url=f"{settings.api_prefix}/openapi.json",
    )
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins,
        allow_credentials=True,
        allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
        allow_headers=["Content-Type", "Authorization", IMPERSONATION_HEADER],
        expose_headers=["Content-Disposition", "X-Row-Count"],
    )

    @app.exception_handler(AppError)
    async def app_error(_: Request, error: AppError):
        return JSONResponse(status_code=error.status_code, content={"detail": error.message, "code": error.code})

    @app.exception_handler(RequestValidationError)
    async def request_invalid(_: Request, error: RequestValidationError):
        errors = error.errors()
        return JSONResponse(
            status_code=422,
            content={"detail": _message(errors), "code": "invalid", "errors": _jsonable(errors)},
        )

    @app.exception_handler(ValidationError)
    async def model_invalid(_: Request, error: ValidationError):
        errors = error.errors()
        return JSONResponse(
            status_code=422,
            content={"detail": _message(errors), "code": "invalid", "errors": _jsonable(errors)},
        )

    for router in (auth.router, me.router, platform.router, admin_auth.router, admin_people.router,
                   admin_lending.router, admin_platform.router):
        app.include_router(router, prefix=settings.api_prefix)
    return app


def _jsonable(errors: list[dict]) -> list[dict]:
    return [{"loc": list(e.get("loc", ())), "msg": str(e.get("msg")), "type": e.get("type")} for e in errors]


app = create_app()

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from sqlalchemy.exc import IntegrityError


class AppError(Exception):
    def __init__(self, status_code: int, code: str, message: str):
        self.status_code = status_code
        self.code = code
        self.message = message


def error_body(code: str, message: str, details=None) -> dict:
    body = {"error": {"code": code, "message": message}}
    if details is not None:
        body["error"]["details"] = details
    return body


def install_error_handlers(app: FastAPI) -> None:
    @app.exception_handler(AppError)
    async def app_error_handler(_: Request, exc: AppError):
        return JSONResponse(status_code=exc.status_code, content=error_body(exc.code, exc.message))

    @app.exception_handler(RequestValidationError)
    async def validation_handler(_: Request, exc: RequestValidationError):
        details = [
            {"field": ".".join(str(item) for item in err["loc"]), "message": err["msg"]}
            for err in exc.errors()
        ]
        return JSONResponse(
            status_code=422,
            content=error_body("VALIDATION_ERROR", "请求参数不符合要求", details),
        )

    @app.exception_handler(IntegrityError)
    async def integrity_handler(_: Request, __: IntegrityError):
        return JSONResponse(
            status_code=409,
            content=error_body("DUPLICATE_RESOURCE", "记录已存在或违反唯一约束"),
        )

    @app.exception_handler(404)
    async def not_found_handler(_: Request, __):
        return JSONResponse(status_code=404, content=error_body("NOT_FOUND", "请求的资源不存在"))

from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from app.api.v1 import router
from app.api.advanced import router as advanced_router
from app.api.company_research import router as company_research_router
from app.api.technical import router as technical_router
from app.api.workflow import router as workflow_router
from app.api.watchlist import router as watchlist_router
from app.api.discovery import router as discovery_router
from app.api.history import router as history_router
from app.api.selected_stock import router as selected_stock_router
from app.config import get_settings
from app.errors import install_error_handlers
from app.logging_config import configure_logging
from app.scheduler import start_scheduler, stop_scheduler


BASE_DIR = Path(__file__).resolve().parent


@asynccontextmanager
async def lifespan(_: FastAPI):
    start_scheduler()
    yield
    stop_scheduler()


def create_app() -> FastAPI:
    settings = get_settings()
    configure_logging(settings.log_level)
    app = FastAPI(title=settings.app_name, version="0.1.0", lifespan=lifespan)
    app.mount("/static", StaticFiles(directory=BASE_DIR / "static"), name="static")
    templates = Jinja2Templates(directory=BASE_DIR / "templates")

    @app.get("/", response_class=HTMLResponse, include_in_schema=False)
    async def home(request: Request):
        return templates.TemplateResponse(request=request, name="index.html", context={})

    @app.get("/{page_name}", response_class=HTMLResponse, include_in_schema=False)
    async def app_page(request: Request, page_name: str):
        allowed = {
            "dashboard",
            "holdings",
            "trades",
            "reviews",
            "market",
            "x-monitor",
            "backtests",
            "settings",
            "technical",
            "company-research",
            "trade-plans",
            "watchlist",
            "opportunities",
            "selected-stock-analysis",
        }
        if page_name not in allowed:
            from app.errors import AppError

            raise AppError(404, "PAGE_NOT_FOUND", "页面不存在")
        return templates.TemplateResponse(
            request=request, name="index.html", context={"page": page_name}
        )

    app.include_router(router)
    app.include_router(advanced_router)
    app.include_router(technical_router)
    app.include_router(company_research_router)
    app.include_router(workflow_router)
    app.include_router(watchlist_router)
    app.include_router(discovery_router)
    app.include_router(history_router)
    app.include_router(selected_stock_router)
    install_error_handlers(app)
    return app


app = create_app()

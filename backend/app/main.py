import os
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import urlsplit

from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse
from sqlalchemy import text
from starlette.staticfiles import StaticFiles

from . import auth, authors, catalog, devices, koreader, opds, personal, reading, series_routes
from .db import APP_URL, DATA, MAX_BYTES, Session

WEB = Path(os.getenv("WEB_ROOT", "web")).resolve()
CSP = ("default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline' blob:; "
       "img-src 'self' blob: data:; font-src 'self' blob: data:; connect-src 'self' blob:; "
       "frame-src 'self' blob:; worker-src 'self' blob:; object-src 'none'; "
       "base-uri 'none'; frame-ancestors 'none'; form-action 'self'")


@asynccontextmanager
async def lifespan(app):
    DATA.mkdir(parents=True, exist_ok=True)
    yield


app = FastAPI(title="Полка", version="0.1.0", lifespan=lifespan, docs_url=None, redoc_url=None)
app.include_router(auth.router)
app.include_router(catalog.router)
app.include_router(authors.router)
app.include_router(series_routes.router)
app.include_router(reading.router)
app.include_router(personal.router)
app.include_router(opds.router)
app.include_router(koreader.router)
app.include_router(devices.router)


@app.exception_handler(RequestValidationError)
async def invalid_input(request: Request, exc: RequestValidationError):
    return JSONResponse({"detail": "Проверьте введённые данные",
                         "fields": [list(error["loc"]) for error in exc.errors()]}, status_code=422)


@app.middleware("http")
async def protect(request: Request, call_next):
    if request.method in {"POST", "PUT", "DELETE", "PATCH"}:
        origin = request.headers.get("origin")
        local = urlsplit(APP_URL)
        origins = {APP_URL}
        if local.hostname in {"localhost", "127.0.0.1"}:
            origins.update(f"{local.scheme}://{host}" + (f":{local.port}" if local.port else "") for host in {"127.0.0.1", "localhost"})
        if origin and origin not in origins:
            return JSONResponse({"detail": "Недопустимый источник запроса"}, status_code=403)
        length = request.headers.get("content-length")
        path = request.url.path
        is_upload = ((path.startswith("/api/v1/libraries/") and path.endswith("/upload"))
                     or (path.startswith("/api/v1/books/") and path.endswith("/editions/upload")))
        limit = MAX_BYTES + 1024 * 1024 if is_upload else 256 * 1024
        if length is None and is_upload:
            return JSONResponse({"detail": "Требуется размер запроса"}, status_code=411)
        if length and (not length.isdigit() or int(length) > limit):
            return JSONResponse({"detail": "Запрос слишком велик"}, status_code=413)
    response = await call_next(request)
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["Referrer-Policy"] = "no-referrer"
    if response.headers.get("content-type", "").startswith("text/html"):
        response.headers["Content-Security-Policy"] = CSP
    return response


@app.get("/healthz", include_in_schema=False)
def health():
    with Session() as db:
        db.execute(text("SELECT 1"))
    return {"status": "ok"}


if (WEB / "assets").is_dir():
    app.mount("/assets", StaticFiles(directory=WEB / "assets"), name="assets")


@app.get("/offline-worker.js", include_in_schema=False)
def offline_worker():
    worker = WEB / "offline-worker.js"
    if not worker.is_file():
        raise HTTPException(404, "Офлайн-читалка недоступна")
    return FileResponse(worker, media_type="text/javascript", headers={"Cache-Control": "no-cache"})


@app.get("/{path:path}", include_in_schema=False)
def frontend(path: str):
    if path.split("/")[0] in {"api", "opds", "assets"} or not (WEB / "index.html").is_file():
        raise HTTPException(404, "Страница не найдена")
    return FileResponse(WEB / "index.html", headers={"Cache-Control": "no-cache"})


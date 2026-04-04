"""Signal Forge v2 — Dashboard FastAPI Application."""

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
from pathlib import Path

from dashboard.routers import status

app = FastAPI(title="Signal Forge v2", version="2.0.0")
app.include_router(status.router, prefix="/api")

STATIC_DIR = Path(__file__).parent / "static"


@app.get("/")
async def index():
    return FileResponse(str(STATIC_DIR / "index.html"))


import os
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse

app = FastAPI(
    title="VentiEdge API",
    description="Backend API for VentiEdge.",
    version="1.0.0",
)

BASE_DIR = Path(__file__).resolve().parent
INDEX_FILE = BASE_DIR / "index.html"

app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "https://veecee-creator.github.io",
    ],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/")
def home():
    if INDEX_FILE.is_file():
        return FileResponse(str(INDEX_FILE))

    return {
        "app": "VentiEdge",
        "status": "online",
        "message": "API is running. Open /docs to view available endpoints.",
    }


@app.get("/api/health")
def health():
    return {
        "status": "ok",
        "app": "VentiEdge",
    }


@app.get("/api")
def api_info():
    return {
        "app": "VentiEdge",
        "status": "online",
        "docs": "/docs",
    }

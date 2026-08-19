"""Entrypoint FastAPI.

    uvicorn app.main:app --host 0.0.0.0 --port 8000

Model embedding + reranker dimuat SEKALI di lifespan, bukan per permintaan:
BGE-M3 makan ~1 menit dan sebuah GPU. Karena itu jalankan dengan SATU worker --
tiap worker memuat salinannya sendiri.
"""
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.api.v1.router import router as router_v1
from app.core.config import setel_log, settings
from app.services import retrieval
from app.services.llm import pakai

log = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    setel_log()
    log.info("memuat model & indeks ...")
    retrieval.siapkan()          # Qdrant + BGE-M3 + reranker + tabel dokumen
    pakai(settings.model_planner)
    log.info("siap")
    yield


app = FastAPI(
    title="RAG Regulasi Pajak",
    description="Tanya-jawab regulasi pajak Indonesia di atas korpus peraturan "
                "yang sudah diindeks (Qdrant + BGE-M3 + reranker).",
    version="1.0.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=[a.strip() for a in settings.cors_asal.split(",") if a.strip()],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(router_v1, prefix="/api/v1")


@app.get("/health", tags=["infra"])
def health() -> dict:
    """Bukan cuma 'proses hidup': model yang belum dimuat berarti permintaan
    pertama akan gagal, dan itu harus kelihatan dari sini."""
    return {"status": "ok" if retrieval.qc is not None else "memuat",
            "koleksi": retrieval.COLL_UNIT,
            "dokumen_di_tabel": len(retrieval.TABEL)}

"""Endpoint tanya-jawab."""
import json
import logging
import time

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import StreamingResponse

from app.schemas.chat import ChatRequest, ChatResponse, Penerus, Rantai, Sumber
from app.services import rag

log = logging.getLogger(__name__)
router = APIRouter()


@router.post("/ask", response_model=ChatResponse, summary="Tanya regulasi pajak")
def ask(req: ChatRequest,
        sertakan_bahan: bool = Query(False, description="Ikutkan BAHAN mentah "
                                     "di respons, untuk penelusuran.")) -> ChatResponse:
    t0 = time.perf_counter()
    try:
        r = rag.jawab(req.pertanyaan, k=req.k,
                      perencana=req.perencana, penjawab=req.penjawab,
                      pakai_saran=req.saran)
    except AssertionError as e:
        # penyedia tidak dikenal / .env belum diisi -- salah pemanggil, bukan bug
        raise HTTPException(status_code=400, detail=str(e)) from e
    except RuntimeError as e:
        # LLM hulu menolak atau tidak menjawab -- kita perantara, bukan sumbernya
        log.exception("LLM gagal")
        raise HTTPException(status_code=502, detail=str(e)) from e

    return _balas(r, t0, sertakan_bahan)


def _balas(r: dict, t0: float, sertakan_bahan: bool) -> ChatResponse:
    """dict jawab() -> ChatResponse. Dipakai dua endpoint, jadi bentuk
    responsnya tidak bisa menyimpang antara yang mengalir dan yang tidak."""
    return ChatResponse(
        pertanyaan=r["pertanyaan"],
        jawaban=r["jawaban"],
        detik_proses=round(time.perf_counter() - t0, 2),
        detik_planner=r["_detik_planner"],
        detik_jawab=r["_detik_jawab"],
        model_planner=r["_model_planner"],
        model_jawab=r["_model_jawab"],
        jumlah_blok=len(r["hasil"]),
        sumber=[Sumber(**x) for x in r["sumber"]],
        saran=r["saran"],
        rantai=[Rantai(pasal=x["pasal"], akar=x["akar"], sebab=x["sebab"],
                       mode=x["mode"], jumlah_versi=len(x["semua"]),
                       penerus=[Penerus(**p) for p in x["_penerus"]])
                for x in r["rantai"]],
        bahan=r["bahan"] if sertakan_bahan else None,
    )


def _sse(peristiwa: str, data) -> str:
    """Satu peristiwa SSE. Baris kosong di ujung itu pemisah antar-peristiwa --
    tanpa itu penerima menunggu selamanya."""
    return f"event: {peristiwa}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"


@router.post("/ask/stream", summary="Tanya regulasi pajak, jawaban mengalir")
def ask_stream(req: ChatRequest,
               sertakan_bahan: bool = Query(False, description="Ikutkan BAHAN "
                                            "mentah di peristiwa selesai.")):
    """SSE. Tiga jenis peristiwa:

        potong   {"teks": "..."}   potongan jawaban, disambung berurutan
        selesai  ChatResponse      jawaban utuh + sumber + saran
        galat    {"pesan": "..."}  gagal di tengah jalan

    Nomor sitasi di `potong` masih penomoran BAHAN; yang di `selesai` sudah
    dinomori ulang 1..N. Penerima WAJIB mengganti teksnya waktu `selesai`
    datang, bukan menyambungnya -- lihat rag.jawab_alir().

    Galat dikirim sebagai peristiwa, bukan status HTTP: begitu potongan pertama
    terkirim, status responsnya sudah 200 dan tidak bisa ditarik lagi.
    """
    t0 = time.perf_counter()

    def aliran():
        try:
            for jenis, isi in rag.jawab_alir(
                    req.pertanyaan, k=req.k, perencana=req.perencana,
                    penjawab=req.penjawab, pakai_saran=req.saran):
                if jenis == "potong":
                    yield _sse("potong", {"teks": isi})
                else:
                    yield _sse("selesai",
                               _balas(isi, t0, sertakan_bahan).model_dump())
        except (AssertionError, RuntimeError) as e:
            log.exception("aliran gagal")
            yield _sse("galat", {"pesan": str(e)})

    return StreamingResponse(aliran(), media_type="text/event-stream",
                             # nginx menahan SSE sampai buffernya penuh, dan
                             # streaming yang tiba sekaligus di akhir sama saja
                             # dengan tidak streaming.
                             headers={"Cache-Control": "no-cache",
                                      "X-Accel-Buffering": "no"})

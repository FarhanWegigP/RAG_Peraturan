"""Planner: pertanyaan -> rencana retrieval terstruktur, plus turunannya."""
import json
import logging
from typing import Any

from app.core.prompts import SISTEM_PLANNER
from app.services.llm import panggil_llm

log = logging.getLogger(__name__)

TIPE = ["definisi-otoritatif", "definisi-tabrakan", "tarif", "prosedur",
        "multi-dokumen", "bernomor", "perubahan"]

SUB_MIN, SUB_MAX = 2, 4

# Berapa potongan peraturan yang diambil. Bukan "seberapa sulit pertanyaannya",
# tapi "jawabannya tersebar di berapa tempat": satu pasal yang ditunjuk sendiri
# oleh penanya butuh 3, daftar pemungut PPh 22 yang berserak butuh 9.
CAKUPAN = {"sempit": 3, "sedang": 6, "luas": 9}

# Maksud waktu -> berapa versi yang benar-benar dikirim. Lima jalur, bukan dua:
# "bunyi pada 2013" butuh SATU versi (yang berlaku waktu itu), dan "sudah pernah
# diubah belum" tidak butuh teks pasal sama sekali -- cukup daftar perubahannya.
MODE = ["latest", "as_of", "riwayat", "diff", "all"]


def obj(props: dict) -> dict:
    """Objek strict: semua properti wajib, tidak boleh ada properti liar."""
    return {"type": "object", "properties": props,
            "required": list(props), "additionalProperties": False}


STR, BOOL = {"type": "string"}, {"type": "boolean"}
ARR_STR = {"type": "array", "items": STR}

SKEMA_PLANNER = {
    "type": "json_schema",
    "json_schema": {
        "name": "pemahaman_pertanyaan",
        "strict": True,
        "schema": obj({
            "peraturan": {**ARR_STR, "maxItems": 5},
            "pasal": {**ARR_STR, "maxItems": 4},
            "temporal_mode": {"type": "string", "enum": MODE},
            "tahun": STR,
            "tipe": {"type": "string", "enum": TIPE},
            "cakupan": {"type": "string", "enum": list(CAKUPAN)},
            "rewritten_query": STR,
            "sub_queries": {"type": "array", "items": STR,
                            "minItems": SUB_MIN, "maxItems": SUB_MAX},
        }),
    },
}

BENTUK: dict[str, Any] = {
    "peraturan": [], "pasal": [], "temporal_mode": "latest", "tahun": "",
    "tipe": "", "cakupan": "sedang", "rewritten_query": "", "sub_queries": [],
}

PEMERINGKAT = {"bernomor": "lexical", "perubahan": "lexical"}  # sisanya dense

SARINGAN_KOSONG = {"definisi-tabrakan", "multi-dokumen"}


def turunkan(paham: dict) -> dict:
    """Keluaran planner -> strategi retrieval. TIDAK menyentuh kalimat pengguna.

    Karena masukannya sudah terstruktur, fungsi ini bisa dites tanpa LLM sama
    sekali -- itu seluruh alasan keberadaannya.
    """
    tipe = paham["tipe"]
    mode = paham.get("temporal_mode") or "latest"
    kosong = tipe in SARINGAN_KOSONG
    return {
        "pemeringkat": PEMERINGKAT.get(tipe, "dense"),
        "saringan": {"peraturan": [], "pasal": []} if kosong else {
            "peraturan": paham.get("peraturan", []),
            "pasal": paham.get("pasal", []),
        },
        # Tidak ada flag cek_versi lagi: rantai SELALU diambil, tipe apa pun.
        # Daftar tipe yang dulu dilewati itu menebak di depan apa yang riwayat()
        # jawab dengan pasti di belakang -- pasal yang tak pernah diubah balik
        # daftar kosong, jadi ceknya gratis. Yang ditebak justru menerbitkan
        # bunyi usang tanpa satu pun tanda.
        "temporal": mode,
        # Berapa blok yang masuk BAHAN. Dulu 10 untuk semua pertanyaan: yang
        # sempit kebanyakan, yang luas justru kekurangan.
        "k": CAKUPAN.get(paham.get("cakupan"), CAKUPAN["sedang"]),
    }


def _lengkapi(hasil: dict, bentuk: dict = BENTUK, jalur: str = "") -> dict:
    """Isi field yang tidak dikirim model dengan default, dan laporkan yang mana."""
    for k, v in bentuk.items():
        if k not in hasil:
            log.warning("field hilang: %s%s -> default %r", jalur, k, v)
            hasil[k] = json.loads(json.dumps(v))  # salinan, jangan bagi objek default
        elif isinstance(v, dict):
            _lengkapi(hasil[k], v, f"{jalur}{k}.")
    return hasil


def planner(pertanyaan: str, penyedia: str | None = None) -> dict:
    """-> pemahaman model + strategi yang DITURUNKAN darinya."""
    paham, detik, nama = panggil_llm(SISTEM_PLANNER, SKEMA_PLANNER, pertanyaan,
                                     penyedia=penyedia)
    paham = _lengkapi(paham)

    if not paham["rewritten_query"]:
        log.warning("rewritten_query kosong")
    # aturan 2-4 cuma dipaksa server kalau json_schema jalan; kalau tidak, di sini
    if not (SUB_MIN <= len(paham["sub_queries"]) <= SUB_MAX):
        log.warning("sub_queries %d butir, harusnya %d-%d",
                    len(paham["sub_queries"]), SUB_MIN, SUB_MAX)
    if not paham["sub_queries"]:  # jangan biarkan tahap retrieval jalan tanpa kunci
        paham["sub_queries"] = [paham["rewritten_query"] or pertanyaan]
        log.warning("sub_queries kosong -> dipakai %r", paham["sub_queries"][0])
    # json_object tidak memaksa tipe: kalau model mengirim string, iterasinya
    # jadi per-huruf dan cari_dokumen dipanggil untuk "P", "M", "K", ...
    for f in ("peraturan", "pasal"):
        if isinstance(paham[f], str):
            paham[f] = [paham[f]] if paham[f] else []
            log.warning("%s dikirim sebagai string -> %s", f, paham[f])
    if paham["tipe"] not in TIPE:
        log.warning("tipe di luar daftar: %r", paham["tipe"])
    if paham["temporal_mode"] not in MODE:   # -> pilih_versi() jatuh ke "latest"
        log.warning("temporal_mode di luar daftar: %r", paham["temporal_mode"])
    if paham["cakupan"] not in CAKUPAN:      # -> turunkan() jatuh ke "sedang"
        log.warning("cakupan di luar daftar: %r", paham["cakupan"])

    paham["strategi"] = turunkan(paham)   # <- keputusan, dihitung bukan diminta
    paham["_detik"] = round(detik, 2)
    paham["_model"] = nama
    return paham


def kunci_pencarian(paham: dict, pertanyaan: str) -> list[str]:
    return [pertanyaan] + paham["sub_queries"]

"""Pembacaan .env dan setelan aplikasi.

Dua lapis, dan dua-duanya perlu:

  Settings   field yang BENTUKNYA tetap (LLM_BASE, K, PROBE, ...) -- divalidasi
             pydantic, jadi PROBE="lima puluh" mati di start-up, bukan di
             tengah pencarian.
  ENV        kamus mentah, untuk penyedia yang namanya BEBAS. `LLM_KEY_Groq`,
             `LLM_MODEL_OpenAI` -- akhirannya tidak bisa didaftarkan di depan,
             jadi sumber() tetap membacanya dari sini (extra="allow" yang
             menampungnya).
"""
import logging
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

AKAR = Path(__file__).resolve().parents[2]     # rag_api/
BERKAS_ENV = AKAR / ".env"

# Penyedia yang sudah dikenal, dipakai kalau .env tidak menyebutkan apa-apa.
BAWAAN = {
    "": ("http://192.168.18.185:1234/v1", "google/gemma-4-12b-qat"),
    "groq": ("https://api.groq.com/openai/v1", "llama-3.3-70b-versatile"),
}


class Settings(BaseSettings):
    # protected_namespaces=(): pydantic mengunci awalan `model_`, sementara
    # nama field di sini memang MODEL_PLANNER / MODEL_PENJAWAB.
    # extra="allow": LLM_BASE_GROQ, LLM_KEY_OPENAI, dan penyedia lain yang
    # belum ada namanya ikut terbaca -- itu yang dipakai sumber().
    model_config = SettingsConfigDict(
        env_file=BERKAS_ENV, env_file_encoding="utf-8", case_sensitive=False,
        extra="allow", protected_namespaces=())

    # --- LLM
    llm_base: str = ""
    llm_model: str = ""
    llm_key: str = ""
    model_planner: str = ""          # "" = penyedia lokal
    model_penjawab: str = "OpenAI"
    timeout_llm: int = 420

    # --- retrieval
    varian: str = "c"                # varian indeks di Qdrant
    probe: int = 50                  # calon per kunci; RRF menggabung
    k: int = 10                      # cadangan kalau planner tidak menyebut cakupan
    # Berapa calon teratas RRF yang diadu di reranker. Ini tombol waktu yang
    # paling besar: cross-encoder itu satu lintasan model per calon, jadi
    # waktunya lurus terhadap angka ini -- 232 calon di CPU ~230 detik, 60
    # calon ~60 detik. Yang dikorbankan cuma calon peringkat bawah RRF, yang
    # nyaris tidak pernah naik ke k teratas setelah diadu. 0 = adu semuanya.
    dalam_rerank: int = 60
    reranker: str = "BAAI/bge-reranker-v2-m3"
    perangkat: str = "cuda"
    qdrant_url: str = ""             # kosong -> ikut embed.QDRANT
    # Tempat embed.py tinggal. Kode RAG-nya tinggal satu salinan -- jangan
    # menyalin embed.py ke sini, arahkan saja path-nya.
    akar_proyek: str = str(AKAR.parent)

    # --- server
    cors_asal: str = "*"             # dipisah koma kalau lebih dari satu


settings = Settings()


def muat_env() -> dict[str, str]:
    """.env + environment proses -> kamus MENTAH, kunci HURUF BESAR.

    Dibaca ulang tiap dipanggil, jadi kunci yang baru diedit bisa dipakai tanpa
    merestart proses -- persis kelakuan muat_env() di notebook.
    """
    global ENV
    ENV = {k.upper(): str(v) for k, v in Settings().model_dump().items()
           if v is not None}
    return ENV


ENV: dict[str, str] = muat_env()


def sumber(nama: str = "") -> tuple[str, str, str]:
    """Nama penyedia -> (base url, nama model, kunci). Tidak mengubah apa pun."""
    s = f"_{nama}".upper() if nama else ""
    base_bawaan, model_bawaan = BAWAAN.get(nama.lower(), ("", ""))
    return (ENV.get(f"LLM_BASE{s}") or ENV.get(f"LLM{s}") or base_bawaan,
            ENV.get(f"LLM_MODEL{s}") or model_bawaan,
            ENV.get(f"LLM_KEY{s}", ""))


def setel_log(taraf: int = logging.INFO) -> None:
    """Naikkan modul kita ke INFO, biarkan httpx & huggingface diam.

    Jejak _lapor() dan tingkat saringan yang kepakai itu satu-satunya cara
    melihat salah tebak dokumen -- salah tebak tidak memunculkan error apa pun,
    yang terjadi cuma hasil nol.
    """
    logging.basicConfig(
        level=logging.WARNING,
        format="%(levelname)s %(name)s: %(message)s")
    logging.getLogger("app").setLevel(taraf)

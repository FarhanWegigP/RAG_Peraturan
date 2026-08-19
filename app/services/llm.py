"""Pemanggil LLM. Satu fungsi untuk planner dan penjawab -- bedanya cuma skema."""
import json
import logging
import re
import time
from collections.abc import Iterator
from typing import Any

import requests

from app.core.config import settings, sumber

log = logging.getLogger(__name__)

# Penyedia default -- dipakai panggilan yang tidak menyebut penyedianya sendiri.
LLM: str = ""
MODEL_LLM: str = ""
LLM_KEY: str = ""


def _siap(nama: str = "") -> tuple[str, str, str]:
    """-> (endpoint chat, nama model, kunci). Tidak mengubah apa pun."""
    base, model, kunci = sumber(nama)
    assert base and model, (
        f"penyedia {nama or 'lokal'!r} tidak dikenal. Isi LLM_BASE{'_' + nama if nama else ''}"
        f" dan LLM_MODEL{'_' + nama if nama else ''} di .env, atau tambahkan ke BAWAAN.")
    return base.rstrip("/") + "/chat/completions", model, kunci


def pakai(nama: str = "") -> str:
    """Ganti penyedia DEFAULT. -> nama modelnya."""
    global LLM, MODEL_LLM, LLM_KEY
    LLM, MODEL_LLM, LLM_KEY = _siap(nama)
    log.info("default -> %s", MODEL_LLM)
    return MODEL_LLM


def _bersih(isi: str) -> str:
    return re.sub(r"<think>.*?</think>", "", isi, flags=re.S).strip()


def _json(isi: str) -> Any:
    isi = _bersih(isi)
    try:
        return json.loads(isi)
    except json.JSONDecodeError:
        m = re.search(r"\{.*\}", isi, re.S)  # kadang masih terbungkus prosa
        if not m:
            raise
        return json.loads(m.group())


_TOLAK_SKEMA: set[str] = set()

SETELAN: dict[str, dict[str, Any]] = {
    "gpt-5":  {},                    # generasi baru: temperature harus default
    "gemma":  {"temperature": 0},
    "llama":  {"temperature": 0},
    "qwen":   {"temperature": 0},
    "":       {"temperature": 0},    # bawaan untuk model yang belum terdaftar
}

_TOLAK_TEMP: set[str] = set()


def setelan(nama: str) -> dict[str, Any]:
    """-> field tambahan untuk badan permintaan model ini."""
    n = nama.lower()
    for potongan, isi in SETELAN.items():
        if potongan and potongan in n:
            return dict(isi)
    return dict(SETELAN[""])


def panggil_llm(sistem: str, skema: dict | None, pertanyaan: str,
                timeout: int | None = None,
                penyedia: str | None = None) -> tuple[Any, float, str]:
    """skema=None -> jawaban dikembalikan sebagai teks biasa, bukan JSON.
    Penjawab tidak punya bentuk tetap, jadi memaksanya jadi JSON cuma menambah
    satu cara gagal tanpa menambah apa pun.

    penyedia=None -> pakai yang sedang aktif (lihat pakai()).
    penyedia="groq" -> paksa penyedia itu UNTUK PANGGILAN INI SAJA. Dipakai
    supaya planner dan penjawab bisa jalan di model berbeda tanpa saling
    menimpa setelan global.
    """
    timeout = timeout or settings.timeout_llm
    if penyedia is None and not MODEL_LLM:
        pakai(settings.model_planner)
    ujung, nama, kunci = (LLM, MODEL_LLM, LLM_KEY) if penyedia is None else _siap(penyedia)
    badan: dict[str, Any] = {
        "model": nama,
        "messages": [{"role": "system", "content": sistem},
                     {"role": "user", "content": pertanyaan}],
    }
    if nama not in _TOLAK_TEMP:
        badan.update(setelan(nama))
    if skema:
        badan["response_format"] = {"type": "json_object"} if nama in _TOLAK_SKEMA else skema
    kepala = {"Authorization": f"Bearer {kunci}"} if kunci else {}
    t0 = time.perf_counter()
    for percobaan in range(4):
        try:
            r = requests.post(ujung, timeout=timeout, json=badan, headers=kepala)
        except requests.exceptions.RequestException as e:
            if percobaan == 3:
                raise RuntimeError(
                    f"{nama} tidak menjawab setelah 4 percobaan "
                    f"(timeout {timeout}s): {type(e).__name__}") from e
            log.warning("%s -> ulangi (%d/4)", type(e).__name__, percobaan + 2)
            continue
        if r.status_code != 429:
            break
        jeda = float(r.headers.get("retry-after", 2 * (percobaan + 1)))
        log.warning("429, tunggu %ss", jeda)
        time.sleep(jeda)
    if r.status_code == 400 and "temperature" in r.text:
        log.warning("%s tolak temperature -> tidak dikirim seterusnya", nama)
        _TOLAK_TEMP.add(nama)
        badan.pop("temperature", None)
        r = requests.post(ujung, timeout=timeout, json=badan, headers=kepala)
    if r.status_code == 400 and "json_schema" in r.text:
        log.warning("%s tolak json_schema -> pakai json_object seterusnya", nama)
        _TOLAK_SKEMA.add(nama)
        badan["response_format"] = {"type": "json_object"}
        r = requests.post(ujung, timeout=timeout, json=badan, headers=kepala)
    if not r.ok:  # pesan server ikut dibawa; "400 Bad Request" saja tidak menjelaskan apa pun
        raise RuntimeError(f"{nama} tolak permintaan ({r.status_code}): {r.text[:400]}")
    isi = r.json()["choices"][0]["message"]["content"]
    return (_json(isi) if skema else _bersih(isi)), time.perf_counter() - t0, nama


def alir_llm(sistem: str, pertanyaan: str, timeout: int | None = None,
             penyedia: str | None = None) -> Iterator[str]:
    """panggil_llm(skema=None) versi mengalir. -> potongan teks, berurutan.

    Cuma untuk penjawab. Planner dan saran menunggu JSON utuh sebelum bisa
    dipakai, jadi mengalirkannya tidak memberi apa pun selain satu parser lagi
    yang bisa gagal separuh jalan.

    Tanpa ulang-percobaan: pengulangan hanya masuk akal sebelum satu byte pun
    sampai ke layar. Begitu potongan pertama terkirim, mengulang berarti
    menulis jawaban kedua di bawah jawaban pertama yang sudah terbaca.
    """
    timeout = timeout or settings.timeout_llm
    ujung, nama, kunci = _siap(penyedia or "")
    badan: dict[str, Any] = {
        "model": nama, "stream": True,
        "messages": [{"role": "system", "content": sistem},
                     {"role": "user", "content": pertanyaan}],
    }
    if nama not in _TOLAK_TEMP:
        badan.update(setelan(nama))
    kepala = {"Authorization": f"Bearer {kunci}"} if kunci else {}
    r = requests.post(ujung, timeout=timeout, json=badan, headers=kepala, stream=True)
    if not r.ok:
        raise RuntimeError(f"{nama} tolak permintaan ({r.status_code}): {r.text[:400]}")
    # ponytail: <think> TIDAK disaring di sini -- penyaringnya butuh melihat tag
    # penutup, dan itu berarti menahan aliran sampai penalaran selesai, persis
    # yang mau dihindari streaming. Teks final di peristiwa "selesai" tetap lewat
    # _bersih(), jadi yang tersisa cuma tag yang sempat lewat di layar. Kalau
    # nanti pakai model lokal yang menalar, saring DI SINI dengan penyangga.
    for baris in r.iter_lines(decode_unicode=True):
        if not baris or not baris.startswith("data:"):
            continue
        data = baris[5:].strip()
        if data == "[DONE]":
            break
        try:
            potong = json.loads(data)["choices"][0]["delta"].get("content")
        except (json.JSONDecodeError, KeyError, IndexError):
            continue          # baris keep-alive / bentuk tak terduga -- lewati
        if potong:
            yield potong

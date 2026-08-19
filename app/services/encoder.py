"""Encoder pertanyaan: BGE-M3 dense + sparse, dan nama-nama koleksi Qdrant.

SALINAN dari `embed.py` di repo pengindeksan, dan salinan itu DISENGAJA supaya
layanan ini bisa berdiri sendiri. Yang disalin cuma bagian yang dipakai waktu
melayani -- pengindeksan (baca unit-html, upsert, riwayat) tidak ikut, karena
tidak pernah dipanggil dari sini.

BAHAYANYA nyata dan sudah pernah menggigit proyek ini: dua salinan, perbaikan
dipasang di satu sisi, sisi lain diam-diam memakai yang lama. Yang menjaganya
`test_encoder_sama_dengan_embed` -- kalau `embed.py` terjangkau (AKAR_PROYEK
diisi), test itu mengadu tiap konstanta di bawah dengan aslinya dan gagal kalau
menceng. Kalau tidak terjangkau, test-nya melewat.

Kenapa harus sama: vektor pertanyaan WAJIB dibuat dengan cara yang persis sama
dengan vektor korpus. `max_length` beda, atau sparse tidak ikut, tidak
memunculkan error apa pun -- yang terjadi cuma hasil pencarian yang memburuk
tanpa ada yang bersuara.
"""
from FlagEmbedding import BGEM3FlagModel
from qdrant_client import models

QDRANT = "http://10.104.0.24:33333"
COLL = "peraturan_unit"
COLL_DOK = "peraturan_dokumen"
MODEL = "BAAI/bge-m3"
MAX_SEQ = 1024


def coll(varian: str = "a") -> str:
    return COLL if varian == "a" else f"{COLL}_{varian}"


def load_model():
    # ponytail: perangkatnya dipatok "cuda:0", disalin apa adanya dari embed.py
    # supaya vektornya dibuat sama persis. Akibatnya PERANGKAT=cpu di .env cuma
    # berlaku untuk reranker, TIDAK untuk encoder ini -- kalau nanti perlu jalan
    # tanpa GPU, ganti di sini DAN di embed.py sekaligus, jangan sebelah.
    return BGEM3FlagModel(MODEL, use_fp16=True, devices="cuda:0")


def encode(model, texts, batch_size: int = 32):
    """-> (dense np.array, list[models.SparseVector]). Satu forward pass untuk dua-duanya."""
    out = model.encode(
        texts,
        batch_size=batch_size,
        max_length=MAX_SEQ,
        return_dense=True,
        return_sparse=True,
    )
    sparse = [
        models.SparseVector(
            indices=[int(t) for t, w in lw.items() if w > 0],
            values=[float(w) for w in lw.values() if w > 0],
        )
        for lw in out["lexical_weights"]
    ]
    return out["dense_vecs"], sparse

"""Ekspansi versi satu pasal, dicetak UTUH -- Qdrant saja, tanpa GPU/LLM.

    python cek_pasal17.py                       # bawaan: Pasal 17 PER-32/PJ/2011
    python cek_pasal17.py <document_id> "Pasal 17"
    python cek_pasal17.py <document_id> "Pasal 17" as_of 2016

Sengaja TIDAK lewat siapkan(): BGE-M3 + reranker makan ~1 menit dan sebuah GPU,
padahal yang dilihat di sini cuma rantai versi dan perakitan teksnya. akar(),
riwayat(), dan _di() murni scroll -- tidak ada vektor, tidak ada model.

Yang dicetak sengaja TIDAK dipotong. Kalau teksnya kepanjangan buat terminal:
    python cek_pasal17.py > hasil.txt
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from qdrant_client import QdrantClient

from app.core.config import settings
from app.services import retrieval as R
from app.services.rag import sumber_blok

sys.path.insert(0, str(Path(settings.akar_proyek).resolve()))
import embed

R.qc = QdrantClient(settings.qdrant_url or embed.QDRANT, timeout=120)

DOK = sys.argv[1] if len(sys.argv) > 1 else \
    "peraturan-direktur-jenderal-pajak-per-32-pj-2011-db6311"
LABEL = sys.argv[2] if len(sys.argv) > 2 else "Pasal 17"
MODE = sys.argv[3] if len(sys.argv) > 3 else "latest"
TAHUN = sys.argv[4] if len(sys.argv) > 4 else ""

GARIS = "=" * 78


def judul(t):
    print(f"\n{GARIS}\n{t}\n{GARIS}")


judul(f"1. NAIK KE AKAR   {LABEL} @ {DOK}")
root, lab, sebab_akar = R.akar(DOK, LABEL)
print(f"akar   : {root}")
print(f"label  : {lab}")
print(f"berhenti karena: {sebab_akar}")
print(f"sebutan: {R._sebut(R._dok(root))}")

judul("2. RIWAYAT -- semua versi yang ada di korpus")
semua, sebab = R.riwayat(root, lab)
print(f"sebab: {sebab} | {len(semua)} versi\n")
for i, (waktu, unit) in enumerate(semua):
    p = unit[0].payload
    peran = "ASLI" if p.get("role") != "target" else "PERUBAHAN"
    bag = sorted(u.payload.get("bagian") or "1/1" for u in unit)
    print(f"  [{i}] {waktu}  {peran:9} {R._sebut(p)}")
    print(f"      {len(unit)} unit, bagian {bag}, {sum(len(u.payload['teks']) for u in unit)} karakter mentah")

judul(f"3. PILIH VERSI   mode={MODE} tahun={TAHUN or '-'}")
pilih, nota = R.pilih_versi(semua, MODE, TAHUN)
print(f"terpilih: {[w for w, _ in pilih]}   catatan: {nota or '-'}")

rantai = [{"pasal": lab, "sebab": sebab, "akar": root, "versi": pilih,
           "semua": semua, "mode": MODE, "tahun": TAHUN, "catatan": nota}]

judul("4. BAHAN -- teks PERSIS yang dibaca LLM penjawab")
print(R.blok_versi(rantai, mulai=1))

judul("5. KARTU SUMBER -- yang dikirim ke frontend")
for k in sumber_blok([], rantai):
    print(f"\n--- blok [{k['blok']}]  {k['sebutan']}, {k['label']} "
          f"({len(k['teks'])} karakter)  berlaku {k['berlaku']} ---")
    print(f"url  : {k['url']}")
    print(f"wakil: {k['unit_id']}")
    print(f"perubahan={k['perubahan']}  akar={k['akar_document_id']}\n")
    print(k["teks"])

judul("6. PERIKSA")
kartu = sumber_blok([], rantai)
bahan = R.blok_versi(rantai, mulai=1)
for k in kartu:
    hilang = [a for a in ("(1)", "(2)", "(3)", "(4)", "(5)", "(6)", "(7)", "(8)", "(9)")
              if a not in k["teks"]]
    # Kartu HARUS memuat teks yang sama dengan yang masuk BAHAN -- kalau tidak,
    # pembaca menilai jawaban memakai kutipan yang tidak dibaca penjawab.
    print(f"blok [{k['blok']}] {len(k['teks'])} karakter | ayat hilang: {hilang or 'tidak ada'} "
          f"| teks kartu ada di BAHAN: {k['teks'] in bahan}")

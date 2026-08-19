"""Retrieval korpus peraturan: tabel dokumen, pencarian, RRF, rerank, dan
ekspansi versi.

`embed.py` tinggal SATU salinan, di akar proyek bareng parser & etl -- modul ini
MEMAKAINYA, bukan menyalinnya. Dulu ada salinan kedua dan itu sempat menipu:
perbaikan dipasang di akar, eval tetap memakai salinan lama yang menunjuk korpus
PDF 139 dokumen. Kalau letaknya pindah, ubah AKAR_PROYEK di .env.
"""
import logging
import re
import sys
from pathlib import Path
from typing import Any

from qdrant_client import QdrantClient, models

from app.core.config import settings
from app.services.planner import kunci_pencarian

log = logging.getLogger(__name__)

sys.path.insert(0, str(Path(settings.akar_proyek).resolve()))
import embed  # noqa: E402  (harus setelah sys.path di atas)

# Diisi siapkan(). Dibiarkan None sampai itu supaya impor modul ini tetap
# murah -- BGE-M3 dan reranker makan ~1 menit dan sebuah GPU.
qc: QdrantClient | None = None
model: Any = None
reranker: Any = None
COLL_UNIT: str = embed.coll(settings.varian)
TABEL: dict[tuple[str, str, str], str] = {}
KUNCI_GANDA: set = set()
JENIS_ASING: set = set()

PROBE = settings.probe   # calon per kunci; RRF menggabung, reranker yang menyaring
K = settings.k           # yang akhirnya dipakai kalau planner tidak bersuara


def siapkan() -> None:
    """Muat Qdrant, model embedding, reranker, dan tabel dokumen. Sekali per
    proses -- dipanggil dari lifespan FastAPI, bukan saat impor."""
    global qc, model, reranker, TABEL, KUNCI_GANDA, JENIS_ASING
    if qc is not None:
        return
    qc = QdrantClient(settings.qdrant_url or embed.QDRANT, timeout=120)
    model = embed.load_model()          # BGE-M3, ~1 menit pertama kali
    # Reranker membaca pertanyaan dan teks BERSAMAAN, jadi jauh lebih teliti
    # daripada mencocokkan dua vektor -- tapi juga jauh lebih lambat. Karena itu
    # dia cuma dikasih calon hasil RRF, bukan seluruh korpus.
    from sentence_transformers import CrossEncoder
    reranker = CrossEncoder(settings.reranker, max_length=512, device=settings.perangkat)
    TABEL, KUNCI_GANDA, JENIS_ASING = muat_tabel()
    log.info("%s %d unit · %s %d dokumen · probe %d/kunci",
             COLL_UNIT, qc.count(COLL_UNIT).count,
             embed.COLL_DOK, qc.count(embed.COLL_DOK).count, PROBE)


# --------------------------------------------------------------------------
# Kamus julukan peraturan
# Vektor tidak pernah bisa bilang "tidak ketemu" -- 'UU KUP' dicocokkan lewat
# tabel dulu.
# --------------------------------------------------------------------------
_JULUKAN = [
    (("kup", "ketentuan umum dan tata cara perpajakan"), ("uu", "6", "1983")),
    (("pph", "pajak penghasilan"), ("uu", "7", "1983")),
    (("ppn", "ppn dan ppnbm", "pajak pertambahan nilai"), ("uu", "8", "1983")),
    (("hpp", "harmonisasi peraturan perpajakan"), ("uu", "7", "2021")),
    (("ppsp", "penagihan pajak", "penagihan pajak dengan surat paksa"), ("uu", "19", "1997")),
    (("pbb", "pajak bumi dan bangunan"), ("uu", "12", "1985")),
    (("bm", "bea meterai", "bea materai"), ("uu", "10", "2020")),
    (("ta", "tax amnesty", "pengampunan pajak"), ("uu", "11", "2016")),
    (("pengadilan pajak",), ("uu", "14", "2002")),
    (("kepabeanan",), ("uu", "10", "1995")),
    (("ciptaker", "cipta kerja"), ("uu", "6", "2023")),
]
JULUKAN = {f"uu {a}": t for alias, t in _JULUKAN for a in alias}

# Jenis peraturan -> kode pendek. Dicocokkan dari yang PALING PANJANG duluan:
# "PERATURAN PEMERINTAH PENGGANTI UNDANG-UNDANG" berawalan "PERATURAN PEMERINTAH",
# jadi kalau urutannya terbalik satu-satunya Perpu di korpus tercatat sebagai PP.
# Dicocokkan setelah di-UPPER (lihat muat_tabel) -- `jenis_peraturan` dari kolom
# DB bentuknya Title Case, dari kop naskah HURUF BESAR.
JENIS = sorted([
    ("PERATURAN PEMERINTAH PENGGANTI UNDANG-UNDANG", "perpu"),
    ("PERATURAN DIREKTUR JENDERAL BEA DAN CUKAI", "perdjbc"),
    ("PERATURAN DIREKTUR JENDERAL PERBENDAHARAAN", "perdjpb"),
    ("PERATURAN MENTERI PERDAGANGAN", "permendag"),
    ("PERATURAN MENTERI DALAM NEGERI", "permendagri"),
    ("PERATURAN MENTERI KETENAGAKERJAAN", "permenaker"),
    ("PERATURAN MENTERI TENAGA KERJA", "permenaker"),
    ("PERATURAN MAHKAMAH AGUNG", "perma"),
    ("PERATURAN BANK INDONESIA", "pbi"),
    ("PERATURAN DIREKTUR JENDERAL PAJAK", "perdjp"),
    ("KEPUTUSAN DIREKTUR JENDERAL PAJAK", "kepdjp"),
    ("PERATURAN OTORITAS JASA KEUANGAN", "pojk"),
    ("PERATURAN MENTERI KEUANGAN", "pmk"),
    ("KEPUTUSAN MENTERI KEUANGAN", "kmk"),
    ("PERATURAN PRESIDEN", "perpres"),
    ("PERATURAN PEMERINTAH", "pp"),
    ("UNDANG-UNDANG", "uu"),
], key=lambda x: -len(x[0]))

# Sebutan yang ditulis orang -> kode yang sama. Urutan juga panjang duluan.
ALIAS = sorted([
    ("peraturan pemerintah pengganti undang undang", "perpu"), ("perppu", "perpu"), ("perpu", "perpu"),
    ("peraturan direktur jenderal bea dan cukai", "perdjbc"), ("perdirjen bc", "perdjbc"),
    ("per bc", "perdjbc"), ("djbc", "perdjbc"),
    ("peraturan direktur jenderal perbendaharaan", "perdjpb"), ("perdirjen perbendaharaan", "perdjpb"),
    ("peraturan menteri perdagangan", "permendag"), ("permendag", "permendag"),
    ("peraturan menteri dalam negeri", "permendagri"), ("permendagri", "permendagri"),
    ("peraturan menteri ketenagakerjaan", "permenaker"),
    ("peraturan menteri tenaga kerja", "permenaker"), ("permenaker", "permenaker"),
    ("peraturan mahkamah agung", "perma"), ("perma", "perma"),
    ("peraturan bank indonesia", "pbi"), ("pbi", "pbi"),
    ("peraturan otoritas jasa keuangan", "pojk"), ("pojk", "pojk"),
    ("peraturan menteri keuangan", "pmk"), ("pmk", "pmk"),
    ("keputusan menteri keuangan", "kmk"), ("kmk", "kmk"),
    ("peraturan presiden", "perpres"), ("perpres", "perpres"),
    ("peraturan pemerintah", "pp"), ("pp", "pp"),
    ("undang undang", "uu"), ("uu", "uu"),
], key=lambda x: -len(x[0]))


def _pokok(nomor: str | None) -> str | None:
    m = re.search(r"\d+", nomor or "")
    return str(int(m.group())) if m else None


def muat_tabel() -> tuple[dict, set, set]:
    """(kode jenis, nomor pokok, tahun) -> document_id, untuk SELURUH koleksi.

    Tiga hal yang berubah sejak korpus pindah ke Postgres, dan ketiganya diam:

    1. HURUF. `jenis_peraturan` dulu dibaca dari kop naskah -- HURUF BESAR SEMUA.
       Sekarang dari kolom DB dan bentuknya Title Case, jadi `startswith` yang
       mematok huruf besar cocok NOL dari 6.580 dokumen -- tabelnya kosong dan
       SEMUA sebutan bernomor jatuh ke pencarian vektor, yang tidak pernah bisa
       bilang "tidak ketemu".

    2. HALAMAN. `scroll(limit=1000)` cuma mengambil 1.000 titik pertama. Waktu
       korpusnya 139 dokumen itu cukup; di 6.580 dokumen, 85% tidak pernah masuk
       tabel. Sekarang ditelusuri sampai habis.

    3. TABRAKAN. 20 kunci menunjuk lebih dari satu dokumen -- nomor yang beda
       tapi angka pokoknya sama: 160/PMK.01/2008 vs 160.1/PMK.07/2008. Dulu yang
       terakhir dibaca menang diam-diam. Sekarang kunci begitu DIBUANG: lebih
       baik jatuh ke vektor daripada menunjuk dokumen yang salah dengan yakin.
    """
    tabel, ganda, asing = {}, set(), set()
    titik, offset = [], None
    while True:
        batch, offset = qc.scroll(embed.COLL_DOK, limit=1000, offset=offset, with_payload=True)
        titik += batch
        if offset is None:
            break
    for t in titik:
        d = t.payload
        jenis = (d["jenis_peraturan"] or "").upper()
        kode = next((k for awalan, k in JENIS if jenis.startswith(awalan)), None)
        pokok = _pokok(d["nomor"])
        if kode is None or pokok is None:
            asing.add(d["jenis_peraturan"])
            continue
        kunci = (kode, pokok, d["tahun"])
        if kunci in tabel and tabel[kunci] != d["document_id"]:
            ganda.add(kunci)
        tabel[kunci] = d["document_id"]
    for k in ganda:
        del tabel[k]
    # `ganda` ikut dikembalikan, bukan cuma dihitung lalu dibuang. Tanpa itu
    # cari_dokumen() tidak bisa membedakan dua sebab kunci tidak ada di tabel:
    # dokumennya memang tidak ada, atau kuncinya sengaja dilepas karena ambigu.
    # Yang pertama layak dijawab "tidak ada", yang kedua justru harus ke vektor.
    log.info("tabel dokumen: %d kunci dari %d dokumen (ambigu dibuang: %d · "
             "jenis tak dikenal: %d)", len(tabel), len(titik), len(ganda), len(asing))
    return tabel, ganda, asing


def julukan(sebutan: str) -> str | None:
    s = re.sub(r"[^a-z0-9]+", " ", (sebutan or "").lower()).strip()
    kunci = JULUKAN.get(re.sub(r"^undang undang\b", "uu", s))
    return TABEL.get(kunci) if kunci else None


def baca_sebutan(sebutan: str) -> tuple[str, str, str] | None:
    s = re.sub(r"[^a-z0-9]+", " ", (sebutan or "").lower()).strip()
    if not s:
        return None
    # PER-57/PJ/2010 dan KEP-220/PJ/2002: jenisnya menempel di nomor, bukan
    # ditulis terpisah. Penanda "pj" cukup -- tidak ada peraturan lain memakainya.
    if re.search(r"\bpj\b", s):
        kode = "kepdjp" if s.startswith("kep") else "perdjp"
    # Bea Cukai menomori "P-26/BC/2009" -- jenisnya menempel di nomor, persis
    # seperti /PJ/ di atas, dan awalannya cuma satu huruf ("P-") sehingga tidak
    # tertangkap daftar ALIAS mana pun. 411 dokumen di korpus.
    elif re.search(r"\bbc\b", s):
        kode = "perdjbc"
    else:
        kode = next((k for a, k in ALIAS if re.search(rf"\b{a}\b", s)), None)
    if kode is None:
        return None
    s = re.sub(r"\bpasal\s+\S+", " ", s)   # buang "Pasal 57" kalau ikut kebawa
    angka = re.findall(r"\d+", s)
    tahun = next((a for a in reversed(angka) if len(a) == 4 and a[:2] in ("19", "20")), None)
    nomor = next((a for a in angka if not (len(a) == 4 and a[:2] in ("19", "20"))), None)
    if not (tahun and nomor):
        return None
    return kode, str(int(nomor)), tahun


# --------------------------------------------------------------------------
# Pencarian, resolusi dokumen, tingkat saringan
# --------------------------------------------------------------------------
def _vektor(teks: str):
    """-> (dense list, SparseVector). .lower() WAJIB: korpus diindeks lowercase."""
    d, s = embed.encode(model, [teks.lower()])
    return d[0].tolist(), s[0]


def _cari(coll: str, teks: str, using: str, limit: int, filt=None) -> list:
    """Satu kunci, satu collection. using: 'dense' (makna) atau 'sparse' (kata persis)."""
    d, s = _vektor(teks)
    return qc.query_points(coll, query=d if using == "dense" else s, using=using,
                           limit=limit, query_filter=filt, with_payload=True).points


def _lapor(sebutan: str, doc_id: str | None, jalur: str) -> str | None:
    """Catat lalu kembalikan doc_id. SELALU dicatat: salah tebak dokumen tidak
    memunculkan error -- yang terjadi cuma hasil nol, dan sebabnya tak terlihat.

    Yang dicatat ID, bukan judul: ID inilah yang dipasang jadi saringan, jadi
    kalau hasilnya nol, ini yang perlu dicek langsung ke Qdrant.
    """
    log.info("dokumen: %r -> %s   [%s]", sebutan, doc_id, jalur)
    return doc_id


def dokumen_serupa(teks: str, limit: int = 5) -> list[str]:
    """Pertanyaan -> document_id yang JUDULNYA paling nyambung. -> daftar id.

    Dipakai HANYA kalau penanya tidak menyebut peraturan apa pun. Judul dokumen
    sering merupakan rumusan pertanyaannya sendiri ("PEMUNGUTAN PPh PASAL 22
    SEHUBUNGAN DENGAN PEMBAYARAN ATAS PENYERAHAN BARANG ..."), dan judul tidak
    peduli penanya menulis "PPh 22" atau "PPh Pasal 22".

    Pencarian unit tidak sekuat itu. "Siapa saja yang wajib memungut PPh Pasal
    22" menarik unit yang kebetulan BERLABEL "Pasal 22" milik peraturan PPh
    21/26, dan pasal yang benar-benar memuat daftar pemungutnya tersingkir dari
    9 besar. Menghapus satu kata "Pasal" dari pertanyaan sudah cukup membalik
    hasilnya -- marginnya setipis itu.
    """
    d, s = _vektor(teks)
    h = qc.query_points(
        embed.COLL_DOK,
        prefetch=[models.Prefetch(query=d, using="dense", limit=limit * 4),
                  models.Prefetch(query=s, using="sparse", limit=limit * 4)],
        query=models.FusionQuery(fusion=models.Fusion.RRF),
        limit=limit, with_payload=True).points
    return [x.payload["document_id"] for x in h]


def cari_dokumen(sebutan: str) -> str | None:
    """'PP 55 tahun 2022' -> document_id asli. Tanpa ini saringan tidak bisa
    dipasang: ID punya hash yang tidak mungkin ditebak siapa pun."""
    if not sebutan:
        return None
    # Julukan diperiksa DULUAN. Vektor tidak pernah bisa bilang "tidak ketemu" --
    # dia selalu mengembalikan yang terdekat, sejauh apa pun; untuk "UU KUP"
    # kata "kup" bahkan nol kemunculan di seluruh judul.
    if (dok := julukan(sebutan)):
        return _lapor(sebutan, dok, "kamus")
    # Sebutan bernomor: cocokkan PERSIS dulu, dan kalau tidak ketemu BEDAKAN
    # dua sebabnya. Dulu keduanya sama-sama jatuh ke vektor, dan vektor tidak
    # pernah bisa bilang "tidak ketemu" -- "PMK 12/2024" yang memang tidak ada
    # dijawab dengan PP 29/2024, lalu dua tingkat saringan terbuang menyisir
    # dokumen yang salah.
    if kunci := baca_sebutan(sebutan):
        if kunci in TABEL:
            return _lapor(sebutan, TABEL[kunci], "tabel")
        if kunci not in KUNCI_GANDA:
            # Terbaca sempurna tapi tidak ada di tabel = memang tidak ada.
            return _lapor(sebutan, None, "TIDAK ADA di korpus")
        # Kunci ambigu (KEP-01/PJ/1995 vs KEP-01/PJ.24/1995): dokumennya ADA,
        # cuma kuncinya tidak bisa dipakai. Dilepas ke vektor dengan sengaja.
        # ponytail: kalau ini mulai sering meleset, simpan calonnya sebagai
        # daftar lalu pilih dengan mencocokkan sisa sebutan penanya.
    d, s = _vektor(sebutan)
    h = qc.query_points(
        embed.COLL_DOK,
        prefetch=[models.Prefetch(query=d, using="dense", limit=10),
                  models.Prefetch(query=s, using="sparse", limit=10)],
        query=models.FusionQuery(fusion=models.Fusion.RRF),
        limit=1, with_payload=True).points
    if not h:
        return None
    return _lapor(sebutan, h[0].payload["document_id"], "vektor")


def bikin_filter(doc_ids, pasal):
    """-> Filter Qdrant. Bagian yang kosong tidak dipasang.

    Beberapa dokumen digabung pakai MatchAny (ATAU), bukan beberapa kondisi
    must (DAN): satu unit cuma milik satu dokumen, jadi must dua dokumen
    selalu nol hasil.
    """
    must = []
    if doc_ids:
        must.append(models.FieldCondition(key="document_id",
                                          match=models.MatchAny(any=doc_ids)))
    if pasal:
        # planner keluarkan ["57"], payload menyimpan "Pasal 57".
        # ponytail: dokumen dan pasal disaring sebagai dua daftar terpisah, jadi
        # "Pasal 57 PP 55 dan Pasal 9 PMK 231" ikut memungut Pasal 9 PP 55 kalau
        # ada. Yang mahal itu kehilangan pasal kedua, bukan kelebihan blok --
        # reranker yang menyingkirkannya. Kalau nyasarnya kelihatan mengganggu,
        # naikkan ke pasangan (dokumen, pasal) pakai should[] berisi must[].
        #
        # Dua bentuk label dirakit sekaligus: dokumen berbentuk penetapan
        # memakai `diktum KEEMPAT BELAS`, bukan `Pasal 14`. MatchAny itu ATAU,
        # jadi bentuk yang tidak terpakai cuma tidak pernah cocok. Tanpa ini
        # "Diktum keempat belas KEP-238/PJ/2012" jatuh ke saringan dokumen saja:
        # 16 calon, lalu `cakupan sempit` memotongnya jadi 3 dan unit yang
        # DISEBUT PENANYA sendiri tidak ikut terangkut.
        label = ([f"Pasal {p}" for p in pasal]
                 + [f"diktum {p.upper()}" for p in pasal])
        must.append(models.FieldCondition(
            key="label", match=models.MatchAny(any=label)))
    return models.Filter(must=must) if must else None


def tingkat_saringan(doc_ids, pasal) -> list[tuple[str, Any]]:
    """Saringan dari paling ketat ke paling longgar, tanpa tingkat kembar.

    Tingkat tengah bukan kasus pinggir: 85 dokumen memecah glosariumnya jadi
    unit per angka ("angka 64") dan NOL di antaranya punya unit berlabel
    "Pasal 1". Tiap pertanyaan "definisi X di Pasal 1 PMK Y" dijamin nol di
    tingkat pertama -- yang harus dilepas cuma pasalnya, dokumennya tidak
    salah apa-apa. Melepas dua-duanya sekaligus melebarkan pencarian dari satu
    dokumen ke seluruh korpus.
    """
    # Nama tingkat diturunkan dari ISI saringannya, bukan dari posisinya di
    # daftar. Sejak cari_dokumen() bisa mengembalikan None ("tidak ada di
    # korpus"), doc_ids bisa kosong walau penanya menyebut peraturan -- dan
    # tingkat pertama tetap tercatat "dokumen+pasal" padahal yang terpasang
    # cuma label pasal. Jejaknya jadi bohong di tempat yang paling sering dibaca.
    def _nama(d, p):
        if d and p:
            return "dokumen+pasal"
        if d:
            return "dokumen saja"
        if p:
            return "pasal saja"
        return "tanpa saringan"

    urut = [(_nama(doc_ids, pasal), bikin_filter(doc_ids, pasal)),
            (_nama(doc_ids, ""), bikin_filter(doc_ids, "")),
            ("tanpa saringan", None)]
    keluar: list[tuple[str, Any]] = []
    for nama, f in urut:
        if keluar and f == keluar[-1][1]:
            keluar[-1] = (nama, f)   # tingkat kembar: pakai nama yang lebih longgar
        else:
            keluar.append((nama, f))
    return keluar


# --------------------------------------------------------------------------
# RRF, rerank, retrieve(), perakit konteks
# --------------------------------------------------------------------------
def rrf(daftar, konstanta: int = 60) -> list:
    """Gabung beberapa daftar peringkat. Pakai PERINGKAT, bukan skor: skor dense
    dan skor sparse beda skala, menjumlahkannya tidak berarti apa-apa."""
    skor, isi = {}, {}
    for hasil in daftar:
        for r, h in enumerate(hasil):
            skor[h.id] = skor.get(h.id, 0) + 1 / (konstanta + r + 1)
            isi[h.id] = h
    return [isi[i] for i in sorted(skor, key=skor.get, reverse=True)]


def rerank(query: str, hasil: list, k: int = K, dalam: int | None = None) -> list:
    """Urutkan ulang dengan CrossEncoder. Skornya ditempel ke tiap point.

    `dalam` memotong daftar SEBELUM diadu -- lihat settings.dalam_rerank. Yang
    masuk sini urutan RRF, jadi yang dipotong ekornya: calon yang kalah di dua
    kunci sekaligus dan tetap harus dibayar satu lintasan cross-encoder penuh.
    """
    if not hasil:
        return hasil
    dalam = settings.dalam_rerank if dalam is None else dalam
    if dalam and len(hasil) > dalam:
        log.info("rerank: %d calon dipotong ke %d teratas RRF", len(hasil), dalam)
        hasil = hasil[:dalam]
    skor = reranker.predict([(query, h.payload["teks"]) for h in hasil])
    for h, s in zip(hasil, skor):
        h.payload["_rerank"] = float(s)
    return sorted(hasil, key=lambda h: h.payload["_rerank"], reverse=True)[:k]


def retrieve(paham: dict, pertanyaan: str, k: int | None = None,
             probe: int = PROBE, pakai_rerank: bool = True) -> list:
    """Jalankan strategi planner. -> daftar point Qdrant, sudah diurutkan.

    k=None -> ikut cakupan yang dibaca planner. Sebut angkanya sendiri cuma
    kalau sedang membandingkan, bukan saat dipakai normal.
    """
    st = paham["strategi"]
    k = k or st.get("k", K)
    using = "sparse" if st["pemeringkat"] == "lexical" else "dense"
    doc_ids = [d for s in st["saringan"]["peraturan"] if (d := cari_dokumen(s))]
    kunci = kunci_pencarian(paham, pertanyaan)

    # Penanya tidak menyebut peraturan apa pun: cari dulu DOKUMEN yang judulnya
    # nyambung, lalu ikutkan unit dari dokumen itu sebagai satu daftar peringkat
    # TAMBAHAN buat RRF.
    #
    # Tambahan, bukan saringan. Dipasang jadi saringan, hasil yang tidak nol tapi
    # salah akan menghentikan pelonggaran -- dan lima dokumen tebakan judul jelas
    # bisa salah. Sebagai daftar tambahan, dia cuma bisa menaikkan yang relevan:
    # unit yang muncul di daftar biasa DAN di daftar ini mengumpulkan poin dari
    # dua tempat, yang cuma muncul di sini tetap harus lolos reranker.
    #
    # Tidak dijalankan kalau peraturannya disebut: di situ jawaban "tidak ada di
    # korpus" memang jawaban, dan menebak dokumen lewat judul mengembalikan
    # tepat kebiasaan menebak yang baru saja dibuang dari cari_dokumen().
    tambahan = []
    if not st["saringan"]["peraturan"]:
        kunci_judul = paham.get("rewritten_query") or pertanyaan
        if judul_ids := dokumen_serupa(kunci_judul):
            log.info("judul cocok: %d dokumen -> daftar tambahan", len(judul_ids))
            # ponytail: satu kueri, bukan satu per kunci -- ruangnya sudah
            # dipersempit ke 5 dokumen, jadi rewritten_query saja cukup.
            tambahan = [_cari(COLL_UNIT, kunci_judul, using, probe,
                              bikin_filter(judul_ids, []))]

    # Saringan terlalu ketat lebih berbahaya daripada terlalu longgar: hasilnya
    # nol, tanpa error. Turun SETINGKAT, bukan langsung lepas semuanya.
    for nama, filt in tingkat_saringan(doc_ids, st["saringan"]["pasal"]):
        hasil = rrf([_cari(COLL_UNIT, q, using, probe, filt) for q in kunci] + tambahan)
        if hasil:
            break
        # filt None = tingkat terakhir, tidak ada lagi yang bisa dilonggarkan
        log.warning("0 hasil dengan saringan %s%s", nama,
                    " -> dilonggarkan" if filt is not None else "")

    log.info("%d kunci x %d -> %d calon unik  [saringan: %s, ambil %d]",
             len(kunci), probe, len(hasil), nama, k)
    # Daftar tingkat SELALU berujung di "tanpa saringan" dan dijalani sekali,
    # dari ketat ke longgar -- tidak pernah balik mengetat. Jadi nol di sini
    # berarti korpusnya yang tidak punya, bukan saringan yang kekencangan:
    # berhenti, mengulang cuma menghasilkan nol yang sama.
    if not hasil:
        log.warning("nol hasil bahkan tanpa saringan -- pencarian dihentikan")
        return hasil
    if not pakai_rerank:  # buat membandingkan: seberapa besar sumbangan reranker
        return hasil[:k]
    # ponytail: yang dikirim ke reranker rewritten_query, bukan kalimat asli --
    # korpusnya bahasa peraturan, dan rewritten_query sudah dalam bahasa itu.
    return rerank(paham["rewritten_query"] or pertanyaan, hasil, k)


def _sebut(p: dict | None) -> str:
    """Sebutan resmi satu dokumen. Dipakai di DASAR HUKUM yang keluar ke pengguna,
    jadi bentuknya harus benar.

    "Tahun {tahun}" cuma ditempel kalau nomornya belum memuat tahun. Sejak
    korpus pindah ke Postgres, `nomor` diambil apa adanya dari kolom DB dan
    hampir selalu SUDAH memuat tahunnya:
        "1 Tahun 2019"      -> "Nomor 1 Tahun 2019 Tahun 2019"     (kembar)
        "234/PMK.011/2008"  -> "Nomor 234/PMK.011/2008 Tahun 2008" (mubazir)
    Dua-duanya salah kutip, dan penjawab menyalinnya apa adanya sebagai dasar
    hukum.
    """
    if not p:
        return "(?)"
    nomor, tahun = str(p.get("nomor") or ""), str(p.get("tahun") or "")
    ekor = "" if (tahun and tahun in nomor) else f" Tahun {tahun}"
    return f"{p['jenis_peraturan']} Nomor {nomor}{ekor}".strip()


SITUS = "https://perpajakan.ddtc.co.id/id/sumber-hukum/peraturan-pusat"
# DDTC cuma menyingkat SATU jenis di slug-nya; 80 jenis lain ditulis penuh.
SINGKATAN = {"peraturan pemerintah pengganti undang-undang": "perpu"}


def _slug(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", s.lower()).strip("-")


def tautan(p: dict | None) -> str:
    """URL halaman peraturan di perpajakan.ddtc.co.id. "" kalau tidak bisa dirakit.

    Slug = slug(jenis) + "-" + slug(nomor). Dua bagian itu TIDAK diperlakukan
    sama, dan bedanya diverifikasi ke situsnya, bukan ditebak:

        nomor   "/" DIHAPUS          48/PMK.03/2021 -> 48pmk-032021
        jenis   "/" jadi "-"         ...Investasi/Kepala... -> ...investasi-kepala...

    Kalau "/" di nomor ikut jadi "-", halamannya 404 -- begitu juga sebaliknya.
    Sisanya sama: apa pun yang bukan huruf/angka jadi satu "-".
    """
    if not p:
        return ""
    jenis, nomor = str(p.get("jenis_peraturan") or ""), str(p.get("nomor") or "")
    if not (jenis.strip() and nomor.strip()):
        return ""
    # Keterangan dalam kurung ikut ke slug: "2 Tahun 2022 (KLASTER KEMUDAHAN
    # BERUSAHA, BIDANG PERPAJAKAN)" itu satu isi kolom `nomor`, bukan judul
    # yang nyasar -- dan DDTC memang memakainya utuh.
    return (f"{SITUS}/{_slug(SINGKATAN.get(jenis.strip().lower(), jenis))}"
            f"-{_slug(nomor.replace('/', ''))}")


def blok(h, n: int, tanda: dict | None = None) -> str:
    """Satu unit, PERSIS seperti yang nanti dikirim ke LLM penjawab.

    Identitas dokumen dirakit balik di sini, bukan disimpan di tiap unit:
    payload memisahkannya supaya bisa disaring, tapi LLM butuh utuh -- tanpa
    nomor dan tahun, "Pasal 5" bisa milik ribuan peraturan yang berbeda.
    """
    p = h.payload
    return (f"[{n}] {_sebut(p)}\n"
            f"    tentang {p['tentang']}\n"
            f"    {p['alamat'] or '-'}"
            + (" [PERUBAHAN]" if p.get("role") == "target" else "")
            # Versi lama yang terjaring pencarian: tanpa ini teks mati tampil
            # persis seperti teks yang berlaku. tanda dari penanda_lama().
            + (f" [{lama}]" if (lama := (tanda or {}).get(p["unit_id"])) else "")
            + f"\n\n{p['teks']}")


PEMISAH = "\n\n" + "-" * 70 + "\n\n"


def konteks(hasil: list, n: int | None = None, tanda: dict | None = None) -> str:
    """Bahan jawaban untuk LLM. Satu-satunya perakit -- tampil() memakai fungsi
    yang sama, jadi yang dibaca di layar tidak bisa menyimpang dari yang dibaca
    model."""
    return PEMISAH.join(blok(h, i, tanda) for i, h in enumerate(hasil[:n or len(hasil)], 1))


def tampil(hasil: list, n: int | None = None) -> None:
    """konteks() + unit_id + skor rerank, untuk penelusuran."""
    for i, h in enumerate(hasil[:n or len(hasil)], 1):
        p = h.payload
        skor = f"   rerank {p['_rerank']:+.3f}" if "_rerank" in p else ""
        print("\n" + "=" * 70)
        print(f"unit_id : {p['unit_id']}{skor}")
        print(blok(h, i))


# --------------------------------------------------------------------------
# Ekspansi versi: naik ke akar, turun ke semua versi
# --------------------------------------------------------------------------
ROMAWI = re.compile(r"Pasal [IVXLC]+$")


def _waktu(p: dict) -> str:
    """Kunci urut satu versi.

    ponytail: tanggal_berlaku jatuh ke `tahun` kalau kosong. Dua-duanya ISO
    ("2011-06-06" / "2011"), jadi perbandingan string sudah urut benar tanpa
    parsing tanggal.
    """
    return p.get("tanggal_berlaku") or str(p.get("tahun") or "")


def _di(doc_id: str, label: str, limit: int = 256) -> list:
    """Unit berlabel `label` DI dalam dokumen `doc_id`.

    scroll, bukan query_points: tidak ada vektor, tidak ada model, tidak ada
    peringkat. Itu sebabnya cek versi praktis gratis.

    Batasnya dinaikkan dari 64 waktu korpus pindah ke Postgres. Diukur ke
    206.946 unit: satu pasal terpanjang punya 65 bagian, jadi batas lama
    memotongnya DIAM-DIAM -- rantai versinya jadi kurang satu tanpa ada yang
    bersuara.
    """
    return qc.scroll(COLL_UNIT, limit=limit, with_payload=True,
                     scroll_filter=models.Filter(must=[
                         models.FieldCondition(key="document_id",
                                               match=models.MatchValue(value=doc_id)),
                         models.FieldCondition(key="label",
                                               match=models.MatchValue(value=label))]))[0]


def _pengganti(doc_id: str, label: str, limit: int = 256) -> list:
    """Unit yang MENGGANTI (doc_id, label) -- arah kebalikan dari _di()."""
    return qc.scroll(COLL_UNIT, limit=limit, with_payload=True,
                     scroll_filter=models.Filter(must=[
                         models.FieldCondition(key="target_document_id",
                                               match=models.MatchValue(value=doc_id)),
                         models.FieldCondition(key="label",
                                               match=models.MatchValue(value=label))]))[0]


def akar(doc_id: str, label: str, batas: int = 8) -> tuple[str, str, str]:
    """Naik ke versi paling asal. -> (doc_id, label, sebab berhenti).

    Nomor pasal dibawa terus sepanjang naik: PMK 53/2025 menulis "Pasal 313",
    yang diubahnya juga "Pasal 313", akarnya juga. Yang berganti cuma
    dokumennya -- itu sebabnya satu label cukup jadi pegangan.

    Empat sebab berhenti, dan bedanya penting:
      asli        ketemu unit standalone -- ini teks aslinya
      luar-korpus yang diubah belum ada di korpus. BUKAN "tidak pernah berubah",
                  tapi "tidak tahu" (581 unit menunjuk ke luar)
      sisipan     pasalnya tidak ada di dokumen target: pasal baru yang
                  disisipkan ("Pasal 3B", "6A", "1A"). Rantainya mulai dari
                  tengah, dan itu memang benar -- 116 kasus
      putaran     data melingkar; berhenti daripada menggantung
    """
    jejak = set()
    for _ in range(batas):
        if (doc_id, label) in jejak:
            return doc_id, label, "putaran"
        jejak.add((doc_id, label))
        di_sini = _di(doc_id, label)
        if not di_sini:
            return doc_id, label, "sisipan"
        # Standalone menang kalau satu dokumen punya dua-duanya (1,6% label
        # ambigu): berhenti di tempat lebih aman daripada naik ke silsilah lain.
        if any(u.payload.get("role") != "target" for u in di_sini):
            return doc_id, label, "asli"
        naik = next((u.payload["target_document_id"] for u in di_sini
                     if u.payload.get("target_document_id")), None)
        if naik is None:
            return doc_id, label, "luar-korpus"
        doc_id = naik
    return doc_id, label, "terlalu dalam"


def _urut_bagian(u) -> int:
    """Nomor pecahan `bagian` ("2/3" -> 2); unit yang tidak terpecah -> 1.

    Dipakai gabung() DAN sumber_blok(): urutan scroll Qdrant tidak menjanjikan
    apa pun, jadi "potongan pertama" harus ditentukan di sini, bukan dipungut
    dari hasil scroll.
    """
    return int(str(u.payload.get("bagian") or "1/1").split("/")[0])


def gabung(unit: list) -> str:
    """Pecahan `bagian` -> satu teks utuh.

    Tiap pecahan MENGULANG kepala yang sama supaya bisa dibaca berdiri sendiri:
    preamble ("Ketentuan Pasal 1 diubah, sehingga ..."), label pasalnya, dan
    sering batang ayatnya juga ("(1) Pemungut pajak adalah:"). Disambung mentah,
    kepala itu muncul berkali-kali -- dan yang mahal bukan panjangnya, melainkan
    LLM membaca SATU daftar yang terpotong sebagai beberapa aturan berbeda.

    Tiap potongan dibandingkan terhadap potongan SEBELUMNYA, bukan potongan
    pertama. Potongan yang masih berada di dalam ayat yang sama meneruskan
    batang ayat tetangganya, bukan batang ayat potongan pertama. Diadu ke
    potongan pertama, baris itu dikira isi baru dan lolos, lalu penjawab membaca
    satu daftar terpotong sebagai dua aturan berbeda.

    Yang dibuang cuma baris yang SAMA PERSIS dan berurutan dari atas. Baris
    kembar yang sah di tengah teks ("dihapus.") tidak tersentuh.
    """
    unit = sorted(unit, key=_urut_bagian)
    teks = [u.payload["teks"] for u in unit]
    if len(teks) == 1:
        return teks[0]
    kepala = teks[0].split("\n")
    keluar = [teks[0]]
    for t in teks[1:]:
        baris = t.split("\n")
        n = 0
        while n < len(baris) and n < len(kepala) and baris[n] == kepala[n]:
            n += 1
        keluar.append("\n".join(baris[n:]))
        kepala = baris   # pembanding bergeser ke potongan ini
    return "\n".join(x for x in keluar if x.strip())


BULAN = ("januari", "februari", "maret", "april", "mei", "juni", "juli",
         "agustus", "september", "oktober", "november", "desember")


def _batas_waktu(teks: str) -> tuple[str | None, str | None]:
    """Sebutan waktu dari planner -> (batas "YYYY-MM-DD", presisi).

    presisi "hari" kalau tanggalnya disebut lengkap, "tahun" kalau cuma tahunnya --
    dan yang cuma tahun dijadikan 31 Desember. Presisi itu yang dipakai pemanggil
    untuk tahu jawabannya tunggal atau tidak: pada satu TANGGAL cuma ada satu
    bunyi yang berlaku, tapi dalam satu TAHUN bisa ada dua.

    -> (None, None) kalau tidak ada angka tahun sama sekali.

    ponytail: "Juni 2014" -- bulan tanpa tanggal -- masih dihitung presisi tahun.
    Kalau penanya mulai sering menyebut bulan saja, pecah jadi presisi "bulan"
    pakai calendar.monthrange.
    """
    t = (teks or "").lower()
    if not (m := re.search(r"(19|20)\d{2}", t)):
        return None, None
    thn = m.group()
    if iso := re.search(rf"\b{thn}-\d{{2}}-\d{{2}}\b", t):
        return iso.group(), "hari"
    bln = next((i for i, n in enumerate(BULAN, 1) if n in t), None)
    hari = re.search(r"\b(\d{1,2})\b", t[:m.start()])
    if bln and hari:
        return f"{thn}-{bln:02d}-{int(hari.group(1)):02d}", "hari"
    return f"{thn}-12-31", "tahun"


CATATAN = {
    "sisipan": "pasal SISIPAN -- pasal baru yang ditambahkan, tidak ada teks asli",
    "luar-korpus": "peraturan yang diubah TIDAK ADA di korpus -- riwayat TIDAK LENGKAP, "
                   "jangan simpulkan 'tidak pernah berubah'",
    "diganti": "peraturan ini SUDAH DIGANTI seluruhnya oleh peraturan lain -- "
               "pasalnya memang tidak pernah diubah satu per satu, tapi JANGAN "
               "disimpulkan masih berlaku; sebutkan penggantinya",
    "putaran": "data melingkar -- riwayat tidak bisa dipercaya penuh",
    "terlalu dalam": "rantai lebih dalam dari batas -- riwayat mungkin terpotong",
    "sebelum-versi-awal": "tahun yang ditanya lebih awal dari versi paling awal yang ada -- "
                          "yang ditampilkan versi PERTAMA, bukan yang berlaku saat itu",
    "tanggal-tidak-lengkap": "tanggal berlaku tidak lengkap -- versi yang berlaku pada tahun "
                             "itu tidak bisa dipastikan, dua versi terakhir ditampilkan",
    "ganti-di-tahun-itu": "di tahun yang ditanyakan ada PERGANTIAN versi -- mana yang berlaku "
                          "tergantung tanggalnya, dan penanya tidak menyebut tanggal; dua versi "
                          "terakhir ditampilkan beserta tanggal mulai berlakunya",
}


def pilih_versi(semua: list, mode: str, tahun: str = "") -> tuple[list, str]:
    """Rantai versi + maksud waktu -> versi yang BENAR-BENAR dikirim.

    -> (daftar versi, catatan). Murni perbandingan tanggal: tidak menyentuh
    Qdrant maupun LLM, jadi bisa diuji apa adanya.

    Pada satu TANGGAL cuma ada SATU bunyi yang berlaku -- itu sebabnya "as_of"
    mengirim satu versi, bukan mengirim semuanya lalu menyuruh penjawab
    memilih. Memilih berdasarkan tanggal justru pekerjaan yang paling tidak
    bisa diandalkan kalau diserahkan ke model, dan datanya sudah ada di sini.

    Yang tidak tunggal itu TAHUN. Kalau penanya cuma menyebut tahun dan di
    tahun itu ada pergantian, dua versi dikirim -- lihat "ganti-di-tahun-itu".
    """
    if not semua:
        return [], ""
    if mode == "all":
        return semua, ""
    if mode == "riwayat":       # daftar perubahannya sudah ada di pengantar
        return [], ""
    if mode == "diff":
        return semua[-2:], ""
    if mode == "as_of" and (bw := _batas_waktu(tahun))[0]:
        batas, presisi = bw
        # Tanggal berlaku yang kosong membuat perbandingan apa pun tidak berarti.
        if any(not w for w, _ in semua):
            return semua[-2:], "tanggal-tidak-lengkap"
        # Tanggal UTUH, bukan tahunnya saja. Dipotong jadi 4 huruf, versi yang
        # baru berlaku 19 Desember dianggap sudah berlaku sejak 1 Januari --
        # jawaban salah untuk hampir sepanjang tahun, tanpa satu pun tanda.
        berlaku = [v for v in semua if v[0] <= batas]
        if not berlaku:
            return semua[:1], "sebelum-versi-awal"
        # Cuma tahun yang disebut, dan versi terpilih memang MULAI di tahun itu:
        # sepanjang tahun tersebut ada dua bunyi, dan mana yang benar tergantung
        # tanggal yang tidak disebut penanya. Kirim dua-duanya.
        if presisi == "tahun" and len(berlaku) > 1 and berlaku[-1][0][:4] == batas[:4]:
            return berlaku[-2:], "ganti-di-tahun-itu"
        return berlaku[-1:], ""
    return semua[-1:], ""       # latest, dan mode apa pun yang tidak dikenal


def riwayat(doc_id: str, label: str) -> tuple[list, str]:
    """Semua versi (doc_id, label), urut waktu. -> (daftar versi, sebab).

    Satu lookup dari akar sudah cukup -- tidak perlu meloncat satu per satu:
    pengubah menunjuk AKAR, bukan pengubah sebelumnya. PMK 11/2025 dan PMK
    53/2025 dua-duanya menunjuk PMK 81/2024, jadi sekali turun dari akar
    rantainya sudah utuh.

    Daftar kosong BELUM TENTU "tidak pernah diubah". Peraturan yang DICABUT
    UTUH tidak meninggalkan jejak apa pun di sini: penggantinya tidak mengutip
    satu pasal pun, dia menulis ulang dari nol. 2.355 dokumen di korpus begitu --
    dan untuk semuanya jawaban lama "tidak pernah diubah" itu justru kebalikan
    dari kenyataan.

    Yang menangkapnya `penerus` di payload, dari `peraturan_relasi`. Karena itu
    daftar kosong dipisah jadi dua sebab:
      diganti      ada penerus -> peraturannya sudah mati, sebutkan penggantinya
      <sebab akar> tidak ada penerus -> memang tidak pernah diubah
    """
    root, lab, sebab = akar(doc_id, label)
    ganti = [u for u in _pengganti(root, lab)
             if not ROMAWI.match(u.payload.get("label") or "")]
    if not ganti:
        # cek dulu apakah peraturannya punya penerus sebelum menyimpulkan apa pun
        di_akar = _di(root, lab)
        if any(u.payload.get("penerus") for u in di_akar):
            return [], "diganti"
        return [], sebab

    per_dok: dict[str, list] = {}
    for u in ganti:
        per_dok.setdefault(u.payload["document_id"], []).append(u)

    versi = []
    if sebab == "asli":
        asli = [u for u in _di(root, lab) if u.payload.get("role") != "target"]
        if asli:
            versi.append((_waktu(asli[0].payload), asli))
    versi += [(_waktu(us[0].payload), us) for us in per_dok.values()]
    versi.sort(key=lambda v: v[0])
    return versi, sebab


def ekspansi(hasil: list, paham: dict, maks_pasal: int | None = None) -> list[dict]:
    """Cek versi atas hasil pencarian. -> daftar rantai, satu per pasal.

    Kunci dikumpulkan dulu baru ditanya: satu pasal sering pecah jadi 6 unit,
    jadi 10 hasil bisa cuma 3 pasal berbeda -- bertanya per hit membuang 7
    kueri untuk jawaban yang sama.

    `semua` disimpan terpisah dari `versi`: mode latest cuma MENAMPILKAN bunyi
    terakhir, tapi tetap harus MEMBERI TAHU berapa kali diubah dan oleh siapa --
    tanpa itu LLM menulis dasar hukum "PER-31/PJ/2015" padahal yang benar
    "Pasal 1 PER-57/PJ/2010 sebagaimana telah diubah dengan PER-31/PJ/2015".

    Rantai dipasang SEKALIPUN bunyinya sudah ikut terjaring pencarian. Dulu
    dilewati kalau sudah ada, dan itu justru membuang keterangannya. Teks yang
    dobel bukan urusan di sini -- rapikan() membuangnya.

    maks_pasal=None -> ikut `k`, jadi SEMUA blok yang dikirim ikut dicek. Dulu
    dipatok 4 sementara k bisa 9, dan lima blok terakhir lewat tanpa rantai --
    juga tanpa penanda [ASLI], karena penanda itu diturunkan dari rantai.
    """
    mode = paham["strategi"]["temporal"]
    maks = maks_pasal or paham["strategi"].get("k") or len(hasil)
    dilihat, keluar = set(), []

    for h in hasil:
        p = h.payload
        lab = p.get("label") or ""
        if not lab or ROMAWI.match(lab):
            continue
        # Kalau hit ini sendiri bunyi baru (role=target), pijakannya AKAR-nya,
        # bukan dokumennya sendiri: tidak ada yang mengubah peraturan pengubah,
        # jadi mencari dari situ dijamin nol.
        kunci = (p.get("target_document_id") or p["document_id"], lab)
        if kunci in dilihat:
            continue
        dilihat.add(kunci)

        versi, sebab = riwayat(*kunci)
        if versi:
            pilih, nota = pilih_versi(versi, mode, paham.get("tahun", ""))
            keluar.append({"pasal": lab, "sebab": sebab, "akar": kunci[0],
                           "versi": pilih, "semua": versi, "mode": mode,
                           "tahun": paham.get("tahun", ""), "catatan": nota})
        elif sebab == "diganti":
            # Rantai kosong TAPI peraturannya sudah punya pengganti. Dulu kasus
            # ini ikut terbuang bersama "tidak pernah diubah", dan itu justru
            # kebalikan dari kenyataan. Teksnya tidak ada yang perlu dikirim;
            # yang perlu cuma keterangannya, supaya penjawab tidak menyimpulkan
            # masih berlaku.
            pen = next((u.payload["penerus"] for u in _di(*kunci)
                        if u.payload.get("penerus")), [])
            keluar.append({"pasal": lab, "sebab": sebab, "akar": kunci[0],
                           "versi": [], "semua": [], "mode": mode,
                           "tahun": paham.get("tahun", ""), "catatan": None,
                           "penerus": pen})
        if len(dilihat) >= maks:
            break
    return keluar


def _dok(doc_id: str) -> dict | None:
    """Identitas dokumen dari koleksi dokumen. Dipakai kalau akarnya tidak punya
    unit sendiri (pasal sisipan) -- namanya tetap harus ikut disebut."""
    t = qc.scroll(embed.COLL_DOK, limit=1, with_payload=True,
                  scroll_filter=models.Filter(must=[models.FieldCondition(
                      key="document_id", match=models.MatchValue(value=doc_id))]))[0]
    return t[0].payload if t else None


def _rumus(ganti: list, batas: str) -> str:
    """Bentuk penyebutan resmi untuk bunyi yang berlaku pada tanggal `batas`.

    Batas yang lebih awal dari perubahan pertama -> daftar kosong, dan itu
    bukan kasus pinggir: as_of tahun tua memang menunjuk bunyi asli. Dulu
    daftar kosong diam-diam diganti SELURUH rantai, jadi bunyi 2001 disebut
    "terakhir diubah dengan peraturan 2019".
    """
    sampai = [g for g in ganti if g[0] <= batas]
    if not sampai:
        return "(bunyi asli, belum pernah diubah)"
    akhir = _sebut(sampai[-1][1])
    return (f"sebagaimana telah diubah dengan {akhir}" if len(sampai) == 1 else
            f"sebagaimana telah beberapa kali diubah, terakhir dengan {akhir}")


def _pengantar(r: dict) -> str:
    """Kalimat pembuka satu rantai: FAKTA saja -- dokumen akarnya apa, berapa
    kali diubah, oleh siapa, mana yang berlaku, dan apa yang tidak kita tahu.

    Cara MENJAWAB tidak ditulis di sini; itu tugas system prompt penjawab.
    """
    pokok = _dok(r["akar"])
    ganti = [(w, us[0].payload) for w, us in r["semua"]
             if us[0].payload.get("role") == "target"]
    baris = [f"### {r['pasal']} — {_sebut(pokok)}"]

    if ganti:
        # Versi yang TIDAK dikirim tetap disebut di sini -- namanya dan tanggalnya
        # saja. Penjawab jadi tahu ada versi lain tanpa harus dikirimi teksnya,
        # dan tidak bisa bilang "tidak pernah diubah lagi".
        dipilih = {w for w, _ in r["versi"]}
        baris.append(f"### diubah {len(ganti)} kali:")
        baris += [f"###   {w}  {_sebut(p)}"
                  + ("   <- INI yang ditampilkan di bawah" if w in dipilih else "")
                  for w, p in ganti]
        # Rumus penyebutan mengikuti versi yang DITAMPILKAN. Kalau yang dikirim
        # bunyi 2013, menyebut "terakhir dengan PER-31/PJ/2015" itu salah zaman:
        # penjawab akan menyalin kalimat ini apa adanya sebagai dasar hukum.
        urut = sorted(dipilih)
        if r["mode"] == "as_of" and len(urut) > 1:
            # Dua versi dikirim justru karena penanya cuma menyebut tahun --
            # satu rumus penyebutan di sini akan dipakai untuk dua periode
            # sekaligus, dan salah untuk salah satunya.
            baris.append(f"### bunyi yang berlaku pada {r['tahun']} tergantung TANGGAL:")
            baris += [f"###   sejak {w}"
                      + (f" (sampai sebelum {urut[i + 1]})" if i + 1 < len(urut) else "")
                      + f": {r['pasal']} {_sebut(pokok)} {_rumus(ganti, w)}"
                      for i, w in enumerate(urut)]
        else:
            batas = max(dipilih) if (r["mode"] == "as_of" and dipilih) else ganti[-1][0]
            kapan = f" pada {r['tahun']}" if r["mode"] == "as_of" and r.get("tahun") else ""
            baris.append(f"### bunyi yang berlaku{kapan}: {r['pasal']} {_sebut(pokok)} "
                         f"{_rumus(ganti, batas)}")

    # Nama penggantinya ikut disebut -- "sudah diganti" tanpa menyebut oleh apa
    # tidak bisa dipakai penjawab untuk menunjukkan aturan yang benar.
    for did in r.get("penerus") or []:
        baris.append(f"###   diganti oleh: {_sebut(_dok(did))}")

    if not r["versi"]:
        # tidak ada teks yang dikirim -- baris "ditampilkan" cuma menyesatkan
        for k in (r["sebab"], r.get("catatan")):
            if k in CATATAN:
                baris.append(f"### !! {CATATAN[k]}")
        return "\n".join(baris)

    baris.append("### ditampilkan: " + {
        "all": "SEMUA versi, urut waktu -- yang PALING BAWAH yang berlaku",
        "riwayat": "DAFTAR PERUBAHAN saja -- bunyi pasalnya sengaja tidak dikirim",
        "diff": "DUA versi: sebelum dan sesudah perubahan yang ditanyakan",
        "as_of": f"bunyi yang BERLAKU pada {r.get('tahun') or '-'}",
    }.get(r["mode"], "bunyi TERAKHIR saja"))
    for k in (r["sebab"], r.get("catatan")):
        if k in CATATAN:
            baris.append(f"### !! {CATATAN[k]}")
    return "\n".join(baris)


def penanda_lama(rantai: list) -> dict[str, str]:
    """unit_id -> penanda versi, khusus untuk blok hasil PENCARIAN.

    Hasil pencarian tidak pernah lewat blok_versi(), jadi selama ini polos:
    bunyi 2001 yang sudah tujuh kali diganti tampil tanpa satu pun tanda, dan
    penjawab pernah menyitirnya berdampingan dengan bunyi terkini sebagai dasar
    hukum. Salahnya tidak memunculkan error apa pun.

    Yang IKUT dikirim lewat rantai tidak masuk sini -- rapikan() sudah
    membuangnya dari hasil, jadi menandainya cuma bikin penanda yatim.
    """
    tanda: dict[str, str] = {}
    for r in rantai:
        dikirim = {u.payload["unit_id"] for _, us in r["versi"] for u in us}
        for i, (_, us) in enumerate(r["semua"]):
            lama = "ASLI" if us[0].payload.get("role") != "target" else f"PERUBAHAN {i}"
            for u in us:
                if u.payload["unit_id"] not in dikirim:
                    tanda.setdefault(u.payload["unit_id"], lama)
    return tanda


def blok_versi(rantai: list, mulai: int = 1) -> str:
    """Rantai versi -> teks, format sama dengan blok().

    Satu perakit saja, seperti konteks(): yang dibaca di layar tidak boleh
    menyimpang dari yang dibaca model.
    """
    potong, n = [], mulai
    for r in rantai:
        potong.append(_pengantar(r))
        akhir = len(r["versi"]) - 1
        for i, (waktu, unit) in enumerate(r["versi"]):
            p = unit[0].payload
            # as_of yang mengirim DUA versi: keduanya diberi "BERLAKU PADA 2016"
            # berarti dua blok sama-sama mengaku yang berlaku, dan penjawab tidak
            # punya dasar memilih. Yang membedakan cuma tanggal mulainya.
            tag = ((f"BERLAKU SEJAK {waktu}" if akhir else
                    f"BERLAKU PADA {r.get('tahun') or '?'}") if r["mode"] == "as_of" else
                   ("SEBELUM" if i == 0 else "SESUDAH") if r["mode"] == "diff" and akhir else
                   "BERLAKU" if i == akhir else
                   "ASLI" if p.get("role") != "target" else f"PERUBAHAN {i}")
            potong.append(
                f"[{n}] [{tag}] berlaku {waktu}\n"
                f"    {_sebut(p)}\n"
                f"    tentang {p['tentang']}\n"
                f"    {p['alamat'] or '-'}\n\n{gabung(unit)}")
            n += 1
    return PEMISAH.join(potong)


def rapikan(hasil: list, rantai: tuple | list = (), rakit: bool = True) -> list:
    """Buang yang dobel sebelum masuk LLM. -> daftar point, urutan dipertahankan.

    Tiga sumber dobel, ketiganya terukur di korpus ini:

      1. pecahan `bagian` -- 866 pasal terpecah jadi 2.522 unit. Sepuluh hasil
         pencarian bisa cuma 3 pasal, dengan kalimat pembuka yang sama diulang
         sepuluh kali.
      2. unit yang sudah ikut di rantai ekspansi -- kalau tidak dibuang, teks
         yang sama muncul dua kali dengan tanda yang berbeda.
      3. pasal yang BOLONG: kalau cuma potongan 2 dan 5 yang terambil, yang
         dibaca LLM pasal berlubang tanpa ada tandanya.
    """
    dipakai = {u.payload["unit_id"] for r in rantai for _, us in r["versi"] for u in us}
    keluar, sudah = [], set()
    for h in hasil:
        p = h.payload
        if p["unit_id"] in dipakai:
            continue
        kunci = (p["document_id"], p.get("alamat"))
        if kunci in sudah:
            continue
        sudah.add(kunci)
        if rakit and p.get("bagian"):
            # ponytail: pasal dirakit UTUH, bukan sepotong yang kebetulan
            # terambil. Batasnya: pasal 7 potongan jadi satu blok panjang --
            # kalau konteks membengkak, turunkan K, JANGAN memotong pasalnya.
            saudara = [u for u in _di(p["document_id"], p["label"])
                       if u.payload.get("alamat") == p.get("alamat")]
            if len(saudara) > 1:
                p["teks"] = gabung(saudara)
                p["bagian"] = f"utuh ({len(saudara)} bagian)"
        keluar.append(h)
    return keluar

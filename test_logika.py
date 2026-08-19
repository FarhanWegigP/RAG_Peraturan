"""Cek logika murni -- tanpa Qdrant, tanpa GPU, tanpa LLM.

    cd rag_api && pytest -q

Isinya sel `#### cek:` dari rag.ipynb yang tidak menyentuh indeks. Yang butuh
Qdrant hidup (riwayat(), ekspansi(), rapikan()) sengaja tidak ikut: kalau mati
di sini, yang salah kodenya -- bukan jaringan.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import pytest

from app.services.llm import SETELAN, _bersih, _json, setelan
from app.services.planner import (CAKUPAN, SKEMA_PLANNER, SUB_MAX, SUB_MIN,
                                  turunkan)
from app.services.rag import sumber_blok
from app.services.retrieval import gabung, tautan


def test_setelan_per_model():
    """Salah petak di sini tidak bikin error -- cuma diam-diam mengirim
    temperature ke model yang menolaknya, dan itu 400 di tengah eval."""
    assert setelan("gpt-5.6-luna") == {}
    assert setelan("google/gemma-4-12b-qat") == {"temperature": 0}
    assert setelan("llama-3.3-70b-versatile") == {"temperature": 0}
    assert setelan("model-yang-belum-ada") == {"temperature": 0}
    # dict yang dikembalikan harus salinan; kalau tidak, satu panggilan yang
    # menambah field ke badan ikut mengubah SETELAN untuk panggilan berikutnya
    x = setelan("gemma")
    x["temperature"] = 9
    assert SETELAN["gemma"] == {"temperature": 0}


def test_bersih_dan_json():
    assert _bersih("<think>ngoceh</think>  hasil ") == "hasil"
    assert _json('```json\n{"a": 1}\n```') == {"a": 1}     # terbungkus prosa
    assert _json('<think>x</think>{"a": [1, 2]}') == {"a": [1, 2]}


def test_skema_planner():
    p = SKEMA_PLANNER["json_schema"]["schema"]["properties"]
    assert list(p) == ["peraturan", "pasal", "temporal_mode", "tahun", "tipe",
                       "cakupan", "rewritten_query", "sub_queries"]
    assert p["sub_queries"]["minItems"] == SUB_MIN
    assert p["sub_queries"]["maxItems"] == SUB_MAX
    assert set(p["cakupan"]["enum"]) == set(CAKUPAN)


def test_turunan_rencana():
    """Soal asli dari ground_truth, dijalankan tanpa menyentuh LLM."""
    # "PP 55 tahun 2022" -- tahun di situ bagian NAMA, bukan titik waktu
    r = turunkan({"tipe": "bernomor", "peraturan": ["PP 55 tahun 2022"],
                  "pasal": ["57"], "temporal_mode": "latest"})
    assert r["pemeringkat"] == "lexical"
    assert r["saringan"]["pasal"] == ["57"]
    assert "cek_versi" not in r, "flag sudah dihapus -- rantai diambil untuk semua tipe"

    # dokumen disebut, versi tidak
    r = turunkan({"tipe": "definisi-otoritatif", "peraturan": ["UU Penagihan Pajak"],
                  "pasal": [], "temporal_mode": "latest"})
    assert r["pemeringkat"] == "dense"

    # "NITKU itu apa? Bedanya dengan NPWP?" -- saringan WAJIB kosong
    r = turunkan({"tipe": "definisi-tabrakan", "peraturan": ["PMK 81/2024"],
                  "pasal": [], "temporal_mode": "latest"})
    assert r["saringan"]["peraturan"] == [], "definisi-tabrakan tidak boleh disaring"

    # "Bunyi Pasal 9 PMK 231/2019 setelah diubah PMK 59/2022" -- DUA peraturan.
    # Yang memuat bunyi barunya cuma 59/2022; kalau daftarnya menyusut jadi satu
    # di sini, saringan mengunci ke dokumen yang isinya justru bunyi lama.
    r = turunkan({"tipe": "perubahan", "peraturan": ["PMK 231/2019", "PMK 59/2022"],
                  "pasal": ["9", "12"], "temporal_mode": "diff"})
    assert r["saringan"]["peraturan"] == ["PMK 231/2019", "PMK 59/2022"]
    assert r["saringan"]["pasal"] == ["9", "12"], "pasal kedua jangan hilang"

    # tak ada peraturan disebut
    assert turunkan({"tipe": "tarif", "peraturan": [], "pasal": [],
                     "temporal_mode": "latest"})["saringan"]["peraturan"] == []

    # cakupan -> berapa blok. Yang tidak dikenal jatuh ke "sedang", bukan error:
    # planner meleset satu field tidak boleh mematikan seluruh pencarian.
    assert turunkan({"tipe": "bernomor", "cakupan": "sempit"})["k"] == 3
    assert turunkan({"tipe": "prosedur", "cakupan": "luas"})["k"] == 9
    assert turunkan({"tipe": "prosedur", "cakupan": "ngawur"})["k"] == 6
    assert turunkan({"tipe": "prosedur"})["k"] == 6, "field hilang -> sedang"


# --- yang di bawah menyentuh retrieval.py, yang mengimpor embed & qdrant-client.
#     Dilewati kalau paketnya belum terpasang -- itu bukan kegagalan logika.
try:
    from app.services import retrieval as _r
except Exception:                                    # noqa: BLE001
    _r = None

butuh_retrieval = pytest.mark.skipif(_r is None, reason="qdrant-client/embed belum ada")


@butuh_retrieval
def test_baca_sebutan():
    """Murni teks -- tidak menyentuh Qdrant."""
    b = _r.baca_sebutan
    assert b("PMK 81/2024") == ("pmk", "81", "2024")
    assert b("PP 55 tahun 2022") == ("pp", "55", "2022")
    assert b("PER-57/PJ/2010") == ("perdjp", "57", "2010")
    assert b("KEP-220/PJ/2002") == ("kepdjp", "220", "2002")
    assert b("10/PMK.03/2013") == ("pmk", "10", "2013")
    assert b("Undang-Undang Nomor 6 Tahun 1983") == ("uu", "6", "1983")
    assert b("Pasal 57 PP 55 tahun 2022") == ("pp", "55", "2022"), "nomor pasal jangan ikut"
    assert b("PERPU 1/2013") == ("perpu", "1", "2013"), "Perpu bukan PP"
    assert b("UU KUP") is None, "julukan bukan urusan sini -- biar kamus"
    assert b("PMK 81") is None, "tanpa tahun jangan ditebak"


@butuh_retrieval
def test_bikin_filter_dan_tingkat_saringan():
    """Label yang dirakit, bukan hasil pencarian -- tidak menyentuh Qdrant."""
    f = _r.bikin_filter(["dok-a"], ["57", "keempat belas"])
    lab = next(k.match.any for k in f.must if k.key == "label")
    assert "Pasal 57" in lab
    assert "diktum KEEMPAT BELAS" in lab, "dokumen penetapan pakai diktum, bukan Pasal"
    assert _r.bikin_filter([], []) is None
    assert all(k.key != "label" for k in _r.bikin_filter(["dok-a"], []).must)

    # tidak ada tingkat kembar, dan selalu ada jaring terakhir
    def n(*a):
        return [x for x, _ in _r.tingkat_saringan(*a)]

    assert n(["a", "b"], ["9", "12"]) == ["dokumen+pasal", "dokumen saja", "tanpa saringan"]
    assert n(["a"], []) == ["dokumen saja", "tanpa saringan"]
    assert n([], ["9"]) == ["pasal saja", "tanpa saringan"]
    assert n([], []) == ["tanpa saringan"]


@butuh_retrieval
def test_pilih_versi():
    """Murni tanggal -- tidak menyentuh Qdrant sama sekali."""
    pv = _r.pilih_versi
    v = [("2010-01-01", "asli"), ("2013-04-01", "r1"), ("2015-08-01", "r2")]
    assert pv(v, "latest") == (v[-1:], "")
    assert pv(v, "all") == (v, "")
    assert pv(v, "riwayat") == ([], ""), "riwayat tidak mengirim teks pasal"
    assert pv(v, "diff") == (v[-2:], "")
    assert pv(v, "as_of", "2014") == (v[1:2], ""), "2014 -> revisi 2013"
    # tahun saja + ada pergantian di tahun itu -> jangan diam-diam pilih satu
    assert pv(v, "as_of", "2013") == (v[:2], "ganti-di-tahun-itu")
    # tanggal disebut -> pilihannya tunggal, dan HARUS ikut bulannya
    assert pv(v, "as_of", "1 Maret 2013") == (v[:1], ""), "Maret masih bunyi lama"
    assert pv(v, "as_of", "1 Mei 2013") == (v[1:2], ""), "Mei sudah bunyi baru"
    assert pv(v, "as_of", "2013-04-01") == (v[1:2], ""), "tepat di tanggal berlaku"
    assert pv(v, "as_of", "2013-03-31") == (v[:1], ""), "sehari sebelum -> bunyi lama"
    assert pv(v, "as_of", "2026") == (v[-1:], "")
    assert pv(v, "as_of", "2009") == (v[:1], "sebelum-versi-awal")
    assert pv(v, "as_of", "") == (v[-1:], ""), "as_of tanpa tahun -> jangan menebak"
    assert pv(v, "as_of", "1 Januari 2014") == (v[1:2], "")
    assert pv(v, "as_of", "tahun lalu") == (v[-1:], ""), "tanpa angka tahun -> latest"
    assert pv(v, "ngawur") == (v[-1:], ""), "mode asing -> latest, bukan error"
    assert pv([("", "x"), ("2015", "y")], "as_of", "2014")[1] == "tanggal-tidak-lengkap"
    assert pv([], "all") == ([], "")


@butuh_retrieval
def test_rrf_pakai_peringkat_bukan_skor():
    """Yang dijumlah 1/(60+rank), bukan skor -- dense dan sparse beda skala."""
    class P:
        def __init__(self, i):
            self.id = i

    a, b, c = P("a"), P("b"), P("c")
    # b nomor 2 di DUA daftar mengalahkan a & c yang cuma nomor 1 di satu daftar
    urut = [h.id for h in _r.rrf([[a, b], [c, b]])]
    assert urut[0] == "b", urut


@butuh_retrieval
def test_gabung_buang_kepala_terulang():
    """Tiap pecahan mengulang preamble & batang ayat supaya bisa dibaca sendiri.
    Disambung mentah, LLM membaca satu daftar terpotong sebagai beberapa aturan."""
    class U:
        def __init__(self, bagian, teks):
            self.payload = {"bagian": bagian, "teks": teks}

    unit = [U("1/3", "Ketentuan Pasal 1 diubah:\n(1) Pemungut pajak adalah:\na. bank;"),
            U("2/3", "Ketentuan Pasal 1 diubah:\n(1) Pemungut pajak adalah:\nb. bendahara;"),
            # potongan 3 pindah ke ayat lain -- kepalanya dibanding potongan 2,
            # bukan potongan 1, jadi baris "(1) ..." memang sudah hilang di sini
            U("3/3", "Ketentuan Pasal 1 diubah:\n(2) Dikecualikan:\na. kedutaan;")]
    hasil = _r.gabung(unit)
    assert hasil.count("Ketentuan Pasal 1 diubah:") == 1, hasil
    assert hasil.count("(1) Pemungut pajak adalah:") == 1, hasil
    assert "b. bendahara;" in hasil and "a. kedutaan;" in hasil
    assert "(2) Dikecualikan:" in hasil, "ayat baru jangan ikut terbuang"


@butuh_retrieval
def test_sebut_tidak_mengulang_tahun():
    """`nomor` dari kolom DB hampir selalu SUDAH memuat tahun -- ditempel lagi
    jadi 'Nomor 7 Tahun 2021 Tahun 2021', dan penjawab menyalinnya apa adanya."""
    s = _r._sebut
    assert s({"jenis_peraturan": "PMK", "nomor": "7 Tahun 2021", "tahun": "2021"}) \
        == "PMK Nomor 7 Tahun 2021"
    assert s({"jenis_peraturan": "PP", "nomor": "55", "tahun": "2022"}) \
        == "PP Nomor 55 Tahun 2022"
    assert s(None) == "(?)"


@butuh_retrieval
def test_nomor_sumber_sama_dengan_bahan():
    """Nomor di sumber_blok() HARUS sama dengan "[n]" yang benar-benar dicetak
    bahan(). Kalau melenceng, sitasi "[3]" di jawaban menunjuk unit yang salah --
    dan itu tidak memunculkan error apa pun, cuma tautan yang diam-diam keliru.
    """
    import re

    from app.services.rag import bahan, sumber_blok

    class H:
        def __init__(self, i):
            self.payload = {
                "unit_id": f"u{i}", "document_id": f"d{i}", "label": f"Pasal {i}",
                "alamat": f"Pasal {i}", "tentang": "PAJAK PENGHASILAN",
                "jenis_peraturan": "PERATURAN PEMERINTAH", "nomor": "55",
                "tahun": "2022", "teks": f"isi pasal {i}",
            }

    hasil = [H(i) for i in (1, 2, 3)]
    dicetak = [int(n) for n in re.findall(r"^\[(\d+)\]", bahan(hasil, []), re.M)]
    didaftar = [s["blok"] for s in sumber_blok(hasil, [])]
    assert dicetak == didaftar == [1, 2, 3], (dicetak, didaftar)

    # tiap blok bawa unit asalnya, bukan cuma nomornya
    s = sumber_blok(hasil, [])
    assert [x["unit_id"] for x in s] == ["u1", "u2", "u3"]
    assert s[0]["sebutan"] == "PERATURAN PEMERINTAH Nomor 55 Tahun 2022"
    assert s[0]["perubahan"] is False

    # blok rantai versi menyambung dari N+1, bukan mulai dari 1 lagi
    rantai = [{"pasal": "Pasal 9", "akar": "d-akar", "versi": [("2013-04-01", [H(9)]),
                                                              ("2015-08-01", [H(10)])]}]
    lanjut = sumber_blok(hasil, rantai)
    assert [x["blok"] for x in lanjut] == [1, 2, 3, 4, 5], [x["blok"] for x in lanjut]
    assert lanjut[3]["berlaku"] == "2013-04-01"
    assert lanjut[3]["akar_document_id"] == "d-akar"


def test_hanya_terpakai_saring_dan_nomori_ulang():
    """Sitasi dan kartunya harus bergeser BARENGAN. Kalau daftar disaring tapi
    teksnya tidak ikut ditulis ulang, "[3]" di kalimat menunjuk kartu yang
    salah -- dan itu salah kutip peraturan, bukan cuma tampilan meleset."""
    from app.services.rag import hanya_terpakai

    sumber = [{"blok": n, "unit_id": f"u{n}"} for n in range(1, 6)]
    teks, dipakai = hanya_terpakai(
        "Wajib dipotong [3]. Tarifnya begini [2], lihat juga [3] dan [5].", sumber)

    # urut kemunculan: 3->1, 2->2, 5->3
    assert teks == "Wajib dipotong [1]. Tarifnya begini [2], lihat juga [1] dan [3]."
    assert [s["blok"] for s in dipakai] == [1, 2, 3]
    assert [s["unit_id"] for s in dipakai] == ["u3", "u2", "u5"]

    # sitiran ke blok yang tidak ada di BAHAN dibuang, tidak bikin kartu hantu
    teks, dipakai = hanya_terpakai("ada [2] dan ngarang [9].", sumber)
    assert teks == "ada [1] dan ngarang."
    assert [s["unit_id"] for s in dipakai] == ["u2"]

    # tidak menyitir sama sekali -> tidak ada kartu
    assert hanya_terpakai("tidak ada di bahan.", sumber) == ("tidak ada di bahan.", [])


def test_dasar_hukum_urut_nomor():
    """Nomor sitasi ikut urutan sitiran di badan jawaban, sedangkan penjawab
    menulis daftar dasar hukumnya menurut urutan BAHAN -- tanpa ini daftarnya
    terbaca [2] [1] [3] dan kelihatan acak."""
    from app.services.rag import hanya_terpakai

    sumber = [{"blok": n, "unit_id": f"u{n}"} for n in range(1, 6)]
    teks, _ = hanya_terpakai(
        "Pemberi kerja wajib memotong [5], dengan tarif tertentu [2].\n"
        "\n"
        "**Dasar hukum:**\n"
        "\n"
        "- PMK 28 Tahun 2024, Pasal 123 [3]\n"
        "- UU 7 Tahun 1983, Pasal 21 [2]\n"
        "  sebagaimana diubah terakhir dengan UU 36 Tahun 2008\n"
        "- PMK 40/PMK.03/2017, Pasal 2 [5]\n"
        "\n"
        "**Catatan:** tidak ada.\n", sumber)

    baris = teks.split("\n")
    assert [b for b in baris if b.startswith("- ")] == [
        "- PMK 40/PMK.03/2017, Pasal 2 [1]",
        "- UU 7 Tahun 1983, Pasal 21 [2]",
        "- PMK 28 Tahun 2024, Pasal 123 [3]",
    ]
    # baris sambungan ikut pindah bersama butirnya, tidak tertinggal
    assert baris[baris.index("- UU 7 Tahun 1983, Pasal 21 [2]") + 1].strip().startswith("sebagaimana")
    # yang di luar daftar tidak digeser
    assert baris[0] == "Pemberi kerja wajib memotong [1], dengan tarif tertentu [2]."
    assert baris[-2] == "**Catatan:** tidak ada."


def test_daftar_lain_tidak_diurutkan():
    """Membalik urutan daftar syarat itu mengubah isi jawaban, bukan merapikan."""
    from app.services.rag import urut_dasar_hukum

    asli = "Syaratnya:\n\n- menanggung PPh [3]\n- mempekerjakan 2.000 orang [1]\n"
    assert urut_dasar_hukum(asli) == asli


# (jenis, nomor) -> slug. SEMUA baris di bawah dibuka satu per satu ke
# perpajakan.ddtc.co.id dan menghasilkan halaman, bukan 404 -- jadi ini catatan
# hasil pengamatan, bukan tebakan yang kebetulan lolos test buatan sendiri.
SLUG = [
    ("Keputusan Presiden", "2 Tahun 2025", "keputusan-presiden-2-tahun-2025"),
    ("Peraturan Pemerintah", "1 Tahun 2024", "peraturan-pemerintah-1-tahun-2024"),
    # "/" hilang, "." jadi "-" -- ini bentuk 9.000-an dokumen di korpus.
    ("Peraturan Menteri Keuangan", "48/PMK.03/2021", "peraturan-menteri-keuangan-48pmk-032021"),
    ("Keputusan Menteri Keuangan", "01/KM.10/2016", "keputusan-menteri-keuangan-01km-102016"),
    ("Keputusan Direktur Jenderal Bea dan Cukai", "19/BC.1/UP.9/2009",
     "keputusan-direktur-jenderal-bea-dan-cukai-19bc-1up-92009"),
    # Tanpa titik: tidak ada "-" sama sekali di bagian nomor.
    ("Peraturan Direktur Jenderal Pajak", "08/PJ/2009", "peraturan-direktur-jenderal-pajak-08pj2009"),
    # Awalan "PER-"/"KEP-": tanda hubungnya BERTAHAN, cuma "/" yang hilang.
    # ~1.500 dokumen memakai bentuk ini.
    ("Peraturan Direktur Jenderal Pajak", "PER-32/PJ/2011", "peraturan-direktur-jenderal-pajak-per-32pj2011"),
    ("Keputusan Direktur Jenderal Pajak", "KEP-01/PJ/1995", "keputusan-direktur-jenderal-pajak-kep-01pj1995"),
    # Keterangan dalam kurung memang isi kolom `nomor`, dan ikut ke slug.
    ("Undang-Undang", "11 Tahun 2020 (Klaster Kemudahan Berusaha, Bidang Perpajakan)",
     "undang-undang-11-tahun-2020-klaster-kemudahan-berusaha-bidang-perpajakan"),
    # Satu-satunya jenis yang disingkat DDTC.
    ("Peraturan Pemerintah Pengganti Undang-Undang", "1 Tahun 2017", "perpu-1-tahun-2017"),
    # "/" di JENIS jadi "-", kebalikan dari "/" di nomor. Dua-duanya 404 kalau
    # aturannya tertukar.
    ("Peraturan Menteri Investasi/Kepala Badan Koordinasi Penanaman Modal", "1 Tahun 2022",
     "peraturan-menteri-investasi-kepala-badan-koordinasi-penanaman-modal-1-tahun-2022"),
    ("Peraturan Menteri Pendayagunaan Aparatur Negara - Reformasi Birokrasi", "11 Tahun 2018",
     "peraturan-menteri-pendayagunaan-aparatur-negara-reformasi-birokrasi-11-tahun-2018"),
]


@pytest.mark.parametrize("jenis,nomor,slug", SLUG)
def test_tautan(jenis, nomor, slug):
    """Slug yang meleset satu huruf = kartu sumber menautkan ke 404, dan itu
    tidak kelihatan dari sisi API -- responsnya tetap 200 dengan url berisi."""
    assert tautan({"jenis_peraturan": jenis, "nomor": nomor}) ==         "https://perpajakan.ddtc.co.id/id/sumber-hukum/peraturan-pusat/" + slug


def test_tautan_tanpa_identitas():
    """Dokumen yang jenis/nomornya kosong -> "" , bukan URL buntung. Frontend
    memakai string kosong ini buat memutuskan menautkan atau tidak."""
    assert tautan(None) == ""
    assert tautan({"jenis_peraturan": "Peraturan Pemerintah", "nomor": ""}) == ""
    assert tautan({"jenis_peraturan": "", "nomor": "1 Tahun 2024"}) == ""


# Kepala yang DIULANG di tiap pecahan `bagian` -- itu yang bikin gabung() harus
# membuang baris kembar di depan, bukan menyambung mentah.
KEPALA_17 = "Ketentuan Pasal 17 diubah sehingga berbunyi sebagai berikut:\nPasal 17\n"


class _Unit:
    """Point Qdrant seadanya -- sumber_blok() cuma menyentuh .payload."""

    def __init__(self, **p):
        self.payload = p


def _pasal17(bagian: str, isi: str) -> _Unit:
    # Tiap pecahan MENGULANG kepala yang sama, persis seperti di korpus:
    # itu yang bikin gabung() harus membuang baris kembar di depan.
    return _Unit(unit_id=f"per32::pasal-17#b{bagian[0]}", document_id="per32",
                 label="Pasal 17", alamat="Pasal I > 12. > Pasal 17",
                 bagian=bagian, role="target", kelas="pasal",
                 jenis_peraturan="Peraturan Direktur Jenderal Pajak",
                 nomor="PER-32/PJ/2011", tahun="2011", tentang="harga transfer",
                 tanggal_berlaku="2011-11-11", target_document_id="per43",
                 teks=KEPALA_17 + isi)


def test_kartu_rantai_merakit_semua_bagian():
    """Kartu sumber blok rantai harus memuat pasal UTUH, bukan satu pecahan.

    Pernah tidak: `unit[0].payload` dipakai apa adanya, jadi kartu Pasal 17
    PER-32/PJ/2011 (terpecah 3) cuma menampilkan ayat (8)-(9) sementara penjawab
    membaca ayat (1)-(9). Pecahan mana yang nongol tergantung urutan scroll
    Qdrant, jadi bug begini tidak muncul tiap kali dijalankan.
    """
    # Sengaja TIDAK urut: _di() mengembalikan urutan scroll Qdrant, dan itu
    # tidak menjanjikan apa-apa. Bagian 3/3 ditaruh duluan persis seperti yang
    # kejadian di layar.
    unit = [_pasal17("3/3", "  (8) Transaksi pengalihan ...\n  (9) Dalam melakukan ..."),
            _pasal17("1/3", "  (1) Prinsip Kewajaran ...\n  (2) Harta Tak Berwujud ..."),
            _pasal17("2/3", "  (6) Merek Dagang ...\n  (7) Transaksi pemanfaatan ...")]
    rantai = [{"akar": "per43", "versi": [("2011-11-11", unit)]}]
    kartu = sumber_blok([], rantai)
    assert len(kartu) == 1
    teks = kartu[0]["teks"]
    for ayat in ("(1)", "(2)", "(6)", "(7)", "(8)", "(9)"):
        assert ayat in teks, f"ayat {ayat} hilang dari kartu"
    # Kepala cuma sekali, walau ketiga pecahan membawanya.
    assert teks.count("Ketentuan Pasal 17 diubah") == 1
    # Yang dikirim ke LLM dan yang dipajang di kartu HARUS teks yang sama.
    assert teks == gabung(unit)
    # Wakilnya pecahan PERTAMA, bukan yang kebetulan di urutan awal daftar.
    assert kartu[0]["unit_id"].endswith("#b1")

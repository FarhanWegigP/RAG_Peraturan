"""Orkestrator: pertanyaan -> jawaban. Satu-satunya tempat tahap-tahap disambung."""
import logging
import re
import time
from collections.abc import Iterator

from app.core.config import settings, sumber
from app.core.prompts import SISTEM_JAWAB, SISTEM_SARAN
from app.services.llm import _bersih, alir_llm, panggil_llm
from app.services.planner import obj, planner
from app.services.retrieval import (PEMISAH, _dok, _sebut, _urut_bagian,
                                    blok_versi, ekspansi, gabung, konteks,
                                    penanda_lama, rapikan, retrieve, tautan)

log = logging.getLogger(__name__)


def bahan(hasil: list, rantai: list) -> str:
    """Konteks pencarian + riwayat versi, penomoran blok menyambung.

    Dipisah dari jawab() supaya eval bisa memeriksa bahannya tanpa membakar
    satu panggilan LLM.
    """
    teks = konteks(hasil, tanda=penanda_lama(rantai))
    if rantai:
        # PEMISAH cuma kalau ada yang dipisah: mode all/diff sering menyisakan
        # 0 blok pencarian (semuanya sudah ikut rantai), dan garis pemisah di
        # baris pertama membuat BAHAN seolah diawali satu blok kosong.
        teks += (PEMISAH if teks else "") + blok_versi(rantai, mulai=len(hasil) + 1)
    return teks


def sumber_blok(hasil: list, rantai: list) -> list[dict]:
    """Nomor blok -> unit asalnya. Penomorannya WAJIB sama persis dengan bahan().

    Penjawab menyitir pakai nomor blok ("[3]"), dan nomor itu satu-satunya
    jembatan antara kalimat di jawaban dan potongan peraturan yang jadi
    sumbernya. Tanpa daftar ini, "[3]" tidak bisa di-link ke apa pun di
    frontend -- angkanya benar, tapi tidak menunjuk ke mana-mana.

    Urutannya menempel ke bahan(): blok pencarian dulu 1..N, lalu blok rantai
    versi menyambung dari N+1. Kalau salah satu digeser, yang satunya harus
    ikut -- test_nomor_sumber_sama_dengan_bahan menjaganya.
    """
    keluar = []
    for i, h in enumerate(hasil, 1):
        p = h.payload
        keluar.append(_satu(i, p, skor=p.get("_rerank")))
    n = len(hasil) + 1
    for r in rantai:
        for waktu, unit in r["versi"]:
            # Teksnya WAJIB lewat gabung(), persis seperti blok_versi() merakitnya
            # untuk BAHAN. Dulu di sini `unit[0].payload` dipakai apa adanya, dan
            # itu menaruh SATU pecahan `bagian` di kartu sementara penjawab
            # membaca pasal utuh -- Pasal 17 PER-32/PJ/2011 terpecah 3, kartunya
            # kebagian ayat (8)-(9) tanpa ayat (1)-(7). Pecahan mana yang muncul
            # pun tergantung urutan scroll Qdrant, jadi tidak selalu kelihatan.
            # ponytail: kalau FE perlu menyorot per-potongan, ganti jadi daftar
            # unit_id; yang dipakai buat nge-link tetap document_id + alamat.
            wakil = min(unit, key=_urut_bagian)
            keluar.append(_satu(n, dict(wakil.payload, teks=gabung(unit)),
                                versi=waktu, akar=r["akar"]))
            n += 1
    return keluar


def _satu(n: int, p: dict, skor: float | None = None,
          versi: str | None = None, akar: str | None = None) -> dict:
    return {
        "blok": n,
        "unit_id": p["unit_id"],
        "document_id": p["document_id"],
        "label": p.get("label") or "",
        "alamat": p.get("alamat") or "",
        "sebutan": _sebut(p),          # bentuk resmi, sama dengan yang dibaca LLM
        # Halaman peraturannya di perpajakan.ddtc.co.id. Dirakit DI SINI, bukan
        # di frontend: dua tempat yang punya aturan slug sendiri pasti berbeda
        # suatu hari, dan yang satu akan diam-diam menautkan ke 404.
        "url": tautan(p),
        "tentang": p.get("tentang") or "",
        "kelas": p.get("kelas") or "",
        # Teks yang PERSIS dibaca penjawab -- sudah lewat rapikan(), jadi pasal
        # yang terpecah `bagian` sudah dirakit utuh. Ini yang bikin kartu sumber
        # di frontend bisa menampilkan isinya tanpa mem-parsing `bahan`.
        # ponytail: dikirim penuh, tidak dipotong. Satu pasal panjang bisa
        # beberapa KB; kalau responsnya kegemukan, potong DI SINI dan tambahkan
        # endpoint ambil-teks-per-unit -- jangan dipotong di FE, yang di kartu
        # harus sama dengan yang disitir.
        "teks": p.get("teks") or "",
        # Blok yang isinya bunyi baru untuk peraturan LAIN. Tanpa ini FE
        # menautkannya ke peraturan pengubah, padahal normanya milik akarnya.
        "perubahan": p.get("role") == "target",
        "akar_document_id": akar or p.get("target_document_id"),
        "berlaku": versi or p.get("tanggal_berlaku") or "",
        "skor_rerank": skor,
    }


# Spasi di depan ikut ditangkap: sitiran yang dibuang menyisakan "ngarang ."
SITIR = re.compile(r"(\s*)\[(\d+)\]")


def hanya_terpakai(teks: str, sumber: list[dict]) -> tuple[str, list[dict]]:
    """Buang blok yang tidak disitir, lalu nomori ulang 1..N urut kemunculan.

    BAHAN dikirim 10 blok, jawaban cuma menyitir 4 -- enam sisanya kartu sumber
    yang tidak menopang kalimat apa pun. Penomoran BAHAN juga acak dari sudut
    pembaca ([3], [2], [5]), karena urutannya urutan skor rerank, bukan urutan
    bacaan.

    Teks jawaban ikut ditulis ulang: nomor di dalam kurung siku itu satu-satunya
    tautan ke kartunya, jadi menyaring daftar tanpa menyentuh teksnya bikin
    sitasi menunjuk ke blok yang salah.

    Sitiran ke blok yang tidak ada di BAHAN (penjawab salah tulis) dihapus dari
    teks -- kartunya memang tidak ada, dan superskrip mati lebih membingungkan
    daripada tidak ada superskrip.
    """
    ada = {s["blok"] for s in sumber}
    peta: dict[int, int] = {}
    for n in (int(x) for _, x in SITIR.findall(teks)):
        if n in ada:
            peta.setdefault(n, len(peta) + 1)
    teks = SITIR.sub(lambda m: f"{m.group(1)}[{peta[int(m.group(2))]}]"
                     if int(m.group(2)) in peta else "", teks)
    dipakai = [dict(s, blok=peta[s["blok"]]) for s in sumber if s["blok"] in peta]
    return urut_dasar_hukum(teks), sorted(dipakai, key=lambda s: s["blok"])


KEPALA_DASAR = re.compile(r"^[#*_\s]*dasar hukum\b", re.I)
BUTIR = re.compile(r"^\s*[-*+]\s+")


def urut_dasar_hukum(teks: str) -> str:
    """Butir di bawah "Dasar hukum" diurutkan menurut nomor sitasinya.

    Nomornya mengikuti urutan sitiran pertama di badan jawaban, sedangkan
    penjawab menulis daftar dasar hukumnya menurut urutan BAHAN -- jadi
    daftarnya kerap terbaca [2] [1] [3] [4] dan tampak acak padahal tidak.

    HANYA daftar ini yang diurutkan. Daftar lain di jawaban (syarat, tata cara,
    langkah) urutannya bermakna: membalik urutannya bukan merapikan tampilan,
    tapi mengubah isi jawabannya.
    """
    baris = teks.split("\n")
    for i, b in enumerate(baris):
        if not KEPALA_DASAR.match(b):
            continue
        j = i + 1
        while j < len(baris) and not baris[j].strip():
            j += 1
        mulai, butir = j, []
        while j < len(baris):
            if BUTIR.match(baris[j]):
                butir.append([baris[j]])            # butir baru
            elif butir and baris[j].strip():
                butir[-1].append(baris[j])          # sambungan butir sebelumnya
            else:
                break
            j += 1
        if len(butir) < 2:
            continue
        # Butir tanpa sitasi ditaruh di belakang, urutannya tetap -- sort()
        # Python stabil, jadi yang sederajat tidak saling menyalip.
        def nomor(satu: list[str]) -> int:
            m = SITIR.search("\n".join(satu))
            return int(m.group(2)) if m else 10 ** 6

        baris[mulai:j] = [x for satu in sorted(butir, key=nomor) for x in satu]
        break
    return "\n".join(baris)


SKEMA_SARAN = {
    "type": "json_schema",
    "json_schema": {
        "name": "saran_lanjutan", "strict": True,
        "schema": obj({"saran": {"type": "array", "items": {"type": "string"},
                                 "minItems": 3, "maxItems": 3}}),
    },
}


def saran(pertanyaan: str, jawaban: str, sumber: list[dict],
          penyedia: str | None = None) -> list[str]:
    """3 pertanyaan lanjutan. -> daftar kosong kalau gagal.

    Sengaja TIDAK melempar: ini hiasan di bawah jawaban. Satu panggilan LLM yang
    ngambek tidak boleh menghanguskan jawaban yang sudah jadi -- pengguna
    kehilangan tiga tombol, bukan seluruh isi layar.

    Dipakaikan penyedia PLANNER, bukan penjawab: tugasnya pendek dan patuh
    skema, persis seperti planner. Model mahal di sini cuma buang uang.
    """
    dipakai = "\n".join(f"- {s['sebutan']} {s['label']}" for s in sumber[:6])
    isi = (f"# PERTANYAAN AWAL\n{pertanyaan}\n\n"
           f"# JAWABAN YANG SUDAH DIBERIKAN\n{jawaban}\n\n"
           f"# PERATURAN YANG TERPAKAI\n{dipakai}")
    try:
        hasil, _, _ = panggil_llm(SISTEM_SARAN, SKEMA_SARAN, isi, penyedia=penyedia)
        return [str(x) for x in hasil.get("saran", [])][:3]
    except Exception:                                   # noqa: BLE001
        log.warning("saran lanjutan gagal -- dilewati", exc_info=True)
        return []


def jawab_alir(pertanyaan: str, k: int | None = None,
               perencana: str | None = None, penjawab: str | None = None,
               pakai_saran: bool = True) -> Iterator[tuple[str, object]]:
    """Pipeline utuh, mengalir. yield ("potong", str) berkali-kali, lalu
    ("selesai", dict) berisi semua tahapnya.

    SATU-SATUNYA jalur pipeline -- jawab() cuma membungkusnya jadi panggilan
    biasa. Dipisah dua salinan berarti dua tempat yang harus ikut berubah tiap
    kali urutan tahapnya digeser, dan yang tidak dipakai eval akan diam-diam
    membusuk.

    Teks di "potong" masih bernomor blok BAHAN, sedangkan yang di "selesai"
    sudah lewat hanya_terpakai(). Penomoran ulang memang tidak bisa lebih awal:
    baru setelah kalimat terakhir kita tahu blok mana saja yang disitir. Yang
    menerima aliran ini harus MENGGANTI teksnya waktu "selesai" tiba, bukan
    menyambungnya.

    Dua penyedia dipisah dengan sengaja. Tugasnya berbeda jauh: planner harus
    patuh pada skema JSON dan boleh singkat; penjawab harus menulis panjang,
    rapi, dan setia pada kutipan.
    """
    perencana = settings.model_planner if perencana is None else perencana
    penjawab = settings.model_penjawab if penjawab is None else penjawab

    paham = planner(pertanyaan, penyedia=perencana)
    hasil = retrieve(paham, pertanyaan, k=k)
    rantai = ekspansi(hasil, paham)
    for x in rantai:
        # Rantai bersebab "diganti" tidak mengirim satu blok pun -- teksnya
        # memang tidak ada yang perlu dikirim. Tapi NAMA penggantinya harus
        # ikut keluar: tanpa itu pemanggil cuma dapat kata "diganti" dan tidak
        # bisa menunjukkan aturan mana yang benar sekarang.
        x["_penerus"] = [{"document_id": d, "sebutan": _sebut(q := _dok(d)),
                          "url": tautan(q)}
                         for d in (x.get("penerus") or [])]
    bersih = rapikan(hasil, rantai)
    # BAHAN dulu, PERTANYAAN paling bawah: bahan bisa 9 blok penuh teks pasal,
    # dan pertanyaan yang terkubur di atas tumpukan itu lebih mudah terlupa
    # daripada yang menempel di titik model mulai menulis.
    isi = bahan(bersih, rantai)
    isi = f"# BAHAN\n\n{isi}\n\n{PEMISAH}\n# PERTANYAAN\n\n{pertanyaan}"
    nama = sumber(penjawab)[1]
    t0 = time.perf_counter()
    potongan = []
    for potong in alir_llm(SISTEM_JAWAB, isi, penyedia=penjawab):
        potongan.append(potong)
        yield "potong", potong
    detik = time.perf_counter() - t0
    teks = _bersih("".join(potongan))
    # Menyaring SEBELUM saran(): saran berpijak pada peraturan yang terpakai,
    # dan yang tidak disitir memang tidak terpakai.
    teks, terpakai = hanya_terpakai(teks, sumber_blok(bersih, rantai))

    lanjutan = saran(pertanyaan, teks, terpakai,
                     penyedia=perencana) if pakai_saran else []

    log.info("Q: %s | planner %ss (%s) | jawab %ss (%s) | %d blok + %d rantai",
             pertanyaan, paham["_detik"], paham["_model"], round(detik, 2), nama,
             len(bersih), len(rantai))
    for x in rantai:
        # Dokumennya ikut disebut: satu pertanyaan sering memunculkan beberapa
        # "Pasal 1" dari dokumen BERBEDA, dan tanpa ini jejaknya terbaca seperti
        # baris kembar.
        log.info("riwayat %s @ %s: %d versi (%s, %s)",
                 x["pasal"], x["akar"], len(x["semua"]), x["sebab"], x["mode"])

    yield "selesai", {
        "pertanyaan": pertanyaan, "jawaban": teks, "paham": paham,
        "hasil": bersih, "rantai": rantai, "bahan": isi,
        "sumber": terpakai, "saran": lanjutan,
        "_model_planner": paham["_model"], "_model_jawab": nama,
        "_detik_planner": paham["_detik"], "_detik_jawab": round(detik, 2)}


def jawab(pertanyaan: str, k: int | None = None,
          perencana: str | None = None, penjawab: str | None = None,
          pakai_saran: bool = True) -> dict:
    """jawab_alir() ditelan sampai habis. -> dict tahap-tahapnya.

    Dipakai eval, test, dan endpoint yang tidak mengalir -- ketiganya cuma
    butuh hasil akhirnya.
    """
    for jenis, isi in jawab_alir(pertanyaan, k=k, perencana=perencana,
                                 penjawab=penjawab, pakai_saran=pakai_saran):
        if jenis == "selesai":
            return isi                                  # type: ignore[return-value]
    raise RuntimeError("pipeline berhenti tanpa jawaban")

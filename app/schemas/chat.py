"""Bentuk masuk-keluar endpoint chat."""
from pydantic import BaseModel, Field


class ChatRequest(BaseModel):
    pertanyaan: str = Field(..., min_length=3, max_length=2000,
                            description="Pertanyaan pengguna, bahasa bebas.")
    # Dua penyedia dipisah karena tugasnya beda: planner patuh skema JSON,
    # penjawab menulis panjang. Kosong -> ikut .env.
    penjawab: str | None = Field(
        None, description='Nama penyedia LLM penjawab ("Groq", "OpenAI"). '
                          'Kosong -> MODEL_PENJAWAB di .env.')
    perencana: str | None = Field(
        None, description='Penyedia LLM planner. "" = lokal, kosong -> .env.')
    k: int | None = Field(None, ge=1, le=30,
                          description="Paksa jumlah blok. Kosong -> ikut cakupan planner.")
    saran: bool = Field(True, description="Bikin 3 pertanyaan lanjutan. "
                                          "Satu panggilan LLM pendek lagi -- matikan "
                                          "kalau lagi mengukur waktu pipeline.")


class Penerus(BaseModel):
    """Peraturan yang menggantikan seluruh isi peraturan ini."""
    document_id: str
    sebutan: str
    url: str = ""


class Rantai(BaseModel):
    """Ringkasan satu rantai versi. Teks pasalnya sudah masuk `bahan`, yang di
    sini cuma keterangannya -- supaya pemanggil bisa menampilkan jejak versi
    tanpa mem-parsing BAHAN."""
    pasal: str
    akar: str
    sebab: str
    mode: str
    jumlah_versi: int
    # Diisi kalau sebab == "diganti". Tanpa ini pemanggil cuma tahu peraturannya
    # mati, tidak tahu harus menunjuk ke mana sebagai gantinya.
    penerus: list[Penerus] = []


class Sumber(BaseModel):
    """Satu blok BAHAN, beserta unit asalnya.

    `blok` inilah yang disitir penjawab di dalam teks jawaban ("[3]"), jadi ini
    jembatan dari kalimat ke potongan peraturannya -- yang dipakai frontend
    untuk menautkan sitasi.
    """
    blok: int = Field(..., description='Nomor yang disitir di `jawaban`, mis. "[3]".')
    unit_id: str
    document_id: str
    label: str = Field("", description='Mis. "Pasal 57" atau "diktum KESATU".')
    alamat: str = Field("", description='Alamat lengkap, mis. "Pasal 1 > angka 12".')
    sebutan: str = Field("", description="Sebutan resmi dokumen, siap dipakai jadi teks tautan.")
    url: str = Field("", description="Halaman peraturan di perpajakan.ddtc.co.id. "
                                     "Kosong kalau jenis/nomornya tidak lengkap.")
    tentang: str = ""
    kelas: str = Field("", description='Jenis unit, mis. "definisi", "norma".')
    teks: str = Field("", description="Bunyi unit, persis seperti yang dibaca "
                                      "penjawab. Isi kartu sumber di frontend.")
    perubahan: bool = Field(False, description="True kalau blok ini bunyi baru "
                                               "yang dipasang ke peraturan LAIN.")
    akar_document_id: str | None = Field(None, description="Dokumen yang normanya "
                                                           "diubah, kalau `perubahan`.")
    berlaku: str = ""
    skor_rerank: float | None = None


class ChatResponse(BaseModel):
    pertanyaan: str
    jawaban: str
    detik_proses: float
    detik_planner: float
    detik_jawab: float
    model_planner: str
    model_jawab: str
    jumlah_blok: int
    sumber: list[Sumber] = []
    saran: list[str] = Field([], description="3 pertanyaan lanjutan. Kosong "
                                             "kalau dimatikan atau LLM-nya gagal.")
    rantai: list[Rantai] = []
    # Dimatikan secara default: satu BAHAN bisa sembilan pasal penuh. Dipakai
    # waktu menelusuri jawaban yang mencurigakan.
    bahan: str | None = None

    model_config = {"protected_namespaces": ()}   # field memang bernama model_*

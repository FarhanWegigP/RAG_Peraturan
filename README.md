# RAG Regulasi Pajak

Tanya-jawab regulasi pajak Indonesia di atas korpus **6.582 peraturan** (206.946 unit
pasal/ayat) yang diindeks di Qdrant. Jawaban menyitir nomor blok, dan tiap blok punya
kartu sumber berisi bunyi pasalnya utuh beserta tautan ke halamannya di
perpajakan.ddtc.co.id.

FastAPI + Next.js. BGE-M3 (dense + sparse) untuk pencarian, bge-reranker-v2-m3 untuk
penyaringan akhir.

---

## Alur RAG

```
pertanyaan
    │
    ├─ 1. PLANNER ──────────── LLM, keluaran JSON berskema ketat
    │      tipe · peraturan · pasal · temporal_mode · tahun · cakupan
    │      rewritten_query · sub_queries (2-4)
    │
    ├─ 2. TURUNKAN ─────────── tanpa LLM, murni aturan
    │      tipe      -> pemeringkat (lexical / dense)
    │      cakupan   -> k blok (sempit 3, sedang 6, luas 9)
    │      peraturan -> saringan dokumen (dikosongkan untuk tipe tertentu)
    │
    ├─ 3. RETRIEVE ─────────── Qdrant
    │      a. resolusi dokumen : kamus julukan -> tabel (jenis,nomor,tahun) -> vektor
    │      b. cari per sub_query, dense + sparse, PROBE calon masing-masing
    │      c. RRF menggabung semua daftar jadi satu peringkat
    │      d. saringan berjenjang: dokumen+pasal -> dokumen saja -> tanpa saringan
    │         (turun satu tingkat hanya kalau hasilnya nol)
    │      e. rerank cross-encoder -> ambil k teratas
    │
    ├─ 4. EKSPANSI VERSI ───── Qdrant, tanpa vektor (scroll saja)
    │      naik ke pasal AKAR -> ambil semua versinya -> pilih menurut
    │      temporal_mode: latest | as_of | diff | riwayat | all
    │
    ├─ 5. RAPIKAN ──────────── buang dobel, rakit pasal yang terpecah `bagian`
    │
    ├─ 6. BAHAN ────────────── blok pencarian [1..N] + blok rantai versi [N+1..]
    │      dikirim ke LLM penjawab, dialirkan lewat SSE
    │
    └─ 7. PASCA ────────────── buang blok yang tidak disitir, nomori ulang 1..N,
           urutkan daftar dasar hukum, bikin 3 pertanyaan lanjutan
```

Dua LLM dipisah dengan sengaja: **planner** harus patuh skema JSON dan boleh singkat,
**penjawab** harus menulis panjang dan setia pada kutipan. Keduanya diatur terpisah
di `.env`.

### Kenapa ada langkah 4

Peraturan pajak berlapis: satu pasal bisa diubah beberapa kali oleh peraturan lain.
Pencarian biasa mengembalikan bunyi mana saja yang kebetulan cocok — bisa yang sudah
mati. Ekspansi versi menelusuri dari pasal akar ke seluruh perubahannya, lalu memilih
yang benar menurut maksud waktu di pertanyaan. Versi lama yang ikut terjaring diberi
penanda supaya penjawab tidak mengutipnya sebagai aturan yang berlaku.

---

## Menjalankan

Butuh **Qdrant berisi korpus** (koleksi `peraturan_unit_c` + `peraturan_dokumen`) dan
GPU untuk BGE-M3 + reranker (~5 GB VRAM).

```bash
python -m venv .venv && .venv/Scripts/activate
pip install -r requirements.txt
cp .env.example .env            # isi LLM_KEY_*
uvicorn app.main:app --port 8000
```

Model dimuat sekali di lifespan (~1 menit), jadi **jalankan satu worker saja** — tiap
worker memuat salinannya sendiri.

```bash
cd web && npm install && npm run dev      # http://localhost:3000
```

| endpoint | isi |
|---|---|
| `POST /api/v1/ask` | jawaban utuh sekali kirim |
| `POST /api/v1/ask/stream` | SSE: `potong` berkali-kali, lalu `selesai` |
| `GET /health` | `memuat` sampai model siap |

Teks di peristiwa `potong` masih bernomor blok BAHAN. Yang di `selesai` sudah
dinomori ulang — penerima harus **mengganti** teksnya, bukan menyambung.

---

## Setelan

`.env` (backend):

| | |
|---|---|
| `MODEL_PLANNER` / `MODEL_PENJAWAB` | nama penyedia LLM; kosong = lokal |
| `VARIAN` | varian indeks unit (`c` = judul lengkap + alamat ikut ke vektor) |
| `PROBE` | calon per sub_query sebelum RRF |
| `K` | cadangan jumlah blok kalau planner tidak menyebut cakupan |
| `DALAM_RERANK` | calon teratas RRF yang diadu di reranker. **Tombol waktu terbesar**; `0` = semua |
| `PERANGKAT` | `cuda` / `cpu` — hanya untuk reranker, encoder dipatok `cuda:0` |
| `AKAR_PROYEK` | opsional, lihat di bawah |

`web/.env` (frontend): `NEXT_PUBLIC_API_URL`, dan `NEXT_PUBLIC_STREAM_MS` (jeda per
kata; 40 = 25 kata/detik, 12 = 83 kata/detik).

---

## Hubungan dengan repo pengindeksan

Pengindeksan (Postgres → parse → chunk → etl → upsert Qdrant) **tidak ada di repo ini**.
Yang dipakai waktu melayani cuma encoder pertanyaan, dan itu sudah disalin ke
[`app/services/encoder.py`](app/services/encoder.py) supaya repo ini berdiri sendiri.

Salinan itu wajib identik dengan `embed.py` di repo pengindeksan — vektor pertanyaan
harus dibuat persis seperti vektor korpus, dan bedanya tidak memunculkan error apa pun,
cuma hasil pencarian yang pelan-pelan meleset. Penjaganya `test_encoder_sama_dengan_embed`:
isi `AKAR_PROYEK` ke repo itu, test mengadu tiap konstanta dan fungsi lewat AST. Kalau
tidak terjangkau, test-nya melewat.

---

## Tes

```bash
pytest -q
```

37 tes logika murni — tanpa Qdrant, tanpa GPU, tanpa LLM. Yang butuh indeks hidup
sengaja tidak ikut: kalau gagal di sini, yang salah kodenya.

"use client";

import { useCallback, useEffect, useRef, useState, type CSSProperties } from "react";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import { visit } from "unist-util-visit";
import Thinking from "./thinking";

const API = process.env.NEXT_PUBLIC_API_URL || "http://localhost:8000";

// Jeda per kata waktu jawaban dialirkan. Kecepatan baca bukan kecepatan model:
// OpenAI mengirim jauh lebih cepat daripada enak dibaca, jadi laju tampilnya
// diatur di sini, bukan diikutkan laju datangnya potongan.
const STREAM_MS = Number(process.env.NEXT_PUBLIC_STREAM_MS) || 40;

// Penyedia penjawab -- namanya harus sama dengan akhiran LLM_*_<nama> di .env backend.
const MODELS = [
  { key: "OpenAI", name: "OpenAI", tag: "Unggulan" },
  { key: "Groq", name: "Groq", tag: "Cepat" },
  { key: "", name: "Lokal", tag: "Offline" },
];

type Sumber = {
  blok: number;
  unit_id: string;
  document_id: string;
  label: string;
  alamat: string;
  sebutan: string;
  tentang: string;
  kelas: string;
  teks: string;
  perubahan: boolean;
  berlaku: string;
  url: string;
};

type ChatResponse = {
  pertanyaan: string;
  jawaban: string;
  detik_proses: number;
  detik_planner: number;
  detik_jawab: number;
  model_planner: string;
  model_jawab: string;
  jumlah_blok: number;
  sumber: Sumber[];
  saran: string[];
};

type Msg =
  | { id: string; role: "user"; text: string }
  // `stream` itu teks separuh jadi yang masih dialirkan; nomor sitasinya masih
  // penomoran BAHAN. Begitu `data` datang ia DIGANTI, bukan disambung -- backend
  // menomori ulang 1..N setelah tahu blok mana saja yang benar-benar disitir.
  | { id: string; role: "assistant"; loading: boolean; startedAt: number; error?: string; stream?: string; data?: ChatResponse };

type Chat = { id: string; title: string; messages: Msg[] };

let seq = 0;
const uid = () => "m" + ++seq;

// Semua sumber berasal dari perpajakan.ddtc.co.id, jadi badge per-dokumen tidak
// punya apa pun untuk dibedakan -- singkatan 3 huruf dari `sebutan` dulu cuma
// mengarang perbedaan yang tidak ada ("PMK" vs "UND" tidak memberi tahu apa-apa
// yang tidak sudah tertulis lengkap di sebelahnya).
// Aset yang SAMA dengan avatar asisten di gelembung jawaban -- satu berkas,
// satu bentuk. Rasio 0,57 menyamai avatar itu (17px di lingkaran 30px).
const IkonDDTC = ({ size }: { size: number }) => (
  <img src="/assets/mark.png" alt="" style={{ width: Math.round(size * 0.57), height: "auto", objectFit: "contain" }} />
);

// Nama peraturan jadi tautan ke halamannya di DDTC. `url` dirakit di backend
// (tautan() di retrieval.py) -- di sini cuma dipasang. Kosong berarti jenis
// atau nomornya tidak lengkap, dan teks biasa lebih baik daripada tautan mati.
const Tautan = ({ url, teks }: { url?: string; teks: string }) =>
  url ? (
    <a href={url} target="_blank" rel="noopener noreferrer"
       onClick={(e) => e.stopPropagation()}
       style={{ color: "inherit", textDecoration: "underline", textDecorationColor: "#c3ced9", textUnderlineOffset: 2 }}>
      {teks}
    </a>
  ) : (
    <>{teks}</>
  );

export default function Page() {
  const [chats, setChats] = useState<Chat[]>([{ id: "c1", title: "Obrolan baru", messages: [] }]);
  const [activeChat, setActiveChat] = useState<string | null>("c1");
  const [draft, setDraft] = useState("");
  const [model, setModel] = useState(MODELS[0]);
  const [modelOpen, setModelOpen] = useState(false);
  const [sidebarOpen, setSidebarOpen] = useState(true);
  const [openSourcesId, setOpenSourcesId] = useState<string | null>(null);
  const [openChunks, setOpenChunks] = useState<Record<string, boolean>>({});
  const [listening, setListening] = useState(false);

  const scrollEl = useRef<HTMLDivElement | null>(null);
  const isiEl = useRef<HTMLDivElement | null>(null);
  const inputEl = useRef<HTMLTextAreaElement | null>(null);
  const autoScroll = useRef(true);
  const recognition = useRef<any>(null);

  const chat = chats.find((c) => c.id === activeChat) ?? null;
  const messages = chat?.messages ?? [];

  useEffect(() => {
    if (autoScroll.current && scrollEl.current) scrollEl.current.scrollTop = scrollEl.current.scrollHeight;
  }, [messages]);

  // Kata yang menetes satu per satu tidak mengubah `messages`, jadi efek di
  // atas tidak terpicu -- tanpa ini jawaban panjang tumbuh di bawah lipatan
  // layar dan pembaca cuma melihat paragraf pertama sampai semuanya selesai.
  useEffect(() => {
    const luar = scrollEl.current;
    const dalam = isiEl.current;
    if (!luar || !dalam) return;
    const ro = new ResizeObserver(() => {
      if (autoScroll.current) luar.scrollTop = luar.scrollHeight;
    });
    ro.observe(dalam);
    return () => ro.disconnect();
  }, []);

  const patch = (chatId: string, fn: (m: Msg[]) => Msg[]) =>
    setChats((cs) => cs.map((c) => (c.id === chatId ? { ...c, messages: fn(c.messages) } : c)));

  async function send(text: string) {
    text = text.trim();
    if (!text || text.length < 3) return;
    autoScroll.current = true;

    let chatId = activeChat;
    if (!chatId) {
      chatId = "c" + Date.now();
      setChats((cs) => [{ id: chatId!, title: text.slice(0, 44), messages: [] }, ...cs]);
      setActiveChat(chatId);
    } else {
      setChats((cs) =>
        cs.map((c) => (c.id === chatId && c.messages.length === 0 ? { ...c, title: text.slice(0, 44) } : c)),
      );
    }

    const aId = uid();
    patch(chatId, (m) => [
      ...m,
      { id: uid(), role: "user", text },
      { id: aId, role: "assistant", loading: true, startedAt: Date.now() },
    ]);
    setDraft("");
    setOpenSourcesId(null);
    if (inputEl.current) inputEl.current.style.height = "auto";

    try {
      const r = await fetch(`${API}/api/v1/ask/stream`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ pertanyaan: text, penjawab: model.key, saran: true }),
      });
      // Gagal SEBELUM potongan pertama masih HTTP biasa; yang gagal di tengah
      // datang sebagai peristiwa "galat", karena statusnya sudah terlanjur 200.
      if (!r.ok || !r.body) throw new Error((await r.json().catch(() => null))?.detail || `HTTP ${r.status}`);

      const pembaca = r.body.getReader();
      const dekoder = new TextDecoder();
      let sisa = "";        // potongan peristiwa yang belum utuh
      let teks = "";
      let dilukis = 0;      // kapan terakhir dilempar ke state

      for (;;) {
        const { done, value } = await pembaca.read();
        if (done) break;
        sisa += dekoder.decode(value, { stream: true });
        // Baris kosong memisahkan peristiwa. Yang terakhir disisakan: paket TCP
        // boleh terpotong di tengah peristiwa, dan mem-parse separuhnya berarti
        // JSON.parse yang gagal di tengah aliran yang sebenarnya sehat.
        const bagian = sisa.split("\n\n");
        sisa = bagian.pop() ?? "";
        for (const b of bagian) {
          const peristiwa = b.match(/^event: (.+)$/m)?.[1];
          const muatan = b.match(/^data: (.*)$/m)?.[1];
          if (!peristiwa || muatan === undefined) continue;
          const isi = JSON.parse(muatan);
          if (peristiwa === "galat") throw new Error(isi.pesan);
          if (peristiwa === "potong") {
            teks += isi.teks;
            // Tiap potongan = satu parse markdown ulang. Jawaban panjang bisa
            // ribuan potongan, dan melukis semuanya bikin tersendat tanpa
            // menambah satu pun kata yang terbaca. 60ms cukup terasa mengalir.
            if (Date.now() - dilukis > 60) {
              dilukis = Date.now();
              const kini = teks;
              patch(chatId, (m) => m.map((x) => (x.id === aId ? { ...x, stream: kini } : x)));
            }
          } else if (peristiwa === "selesai") {
            patch(chatId, (m) =>
              m.map((x) => (x.id === aId ? { ...x, loading: false, stream: undefined, data: isi as ChatResponse } : x)),
            );
          }
        }
      }
    } catch (e: any) {
      console.error("[ask] gagal:", `${API}/api/v1/ask/stream`, e);
      patch(chatId, (m) =>
        m.map((x) => (x.id === aId ? { ...x, loading: false, error: e?.message || "gagal menghubungi server" } : x)),
      );
    }
  }

  function newChat() {
    autoScroll.current = false;
    const id = "c" + Date.now();
    setChats((cs) => [{ id, title: "Obrolan baru", messages: [] }, ...cs]);
    setActiveChat(id);
    setDraft("");
    setOpenSourcesId(null);
    inputEl.current?.focus();
  }

  function openCite(msgId: string, blok: number) {
    autoScroll.current = false;
    setOpenChunks((s) => ({ ...s, [msgId + "#" + blok]: true }));
    requestAnimationFrame(() => {
      const el = document.getElementById(`chunk-${msgId}-${blok}`);
      if (el && scrollEl.current) {
        const top =
          scrollEl.current.scrollTop +
          (el.getBoundingClientRect().top - scrollEl.current.getBoundingClientRect().top) -
          16;
        scrollEl.current.scrollTo({ top, behavior: "smooth" });
      }
    });
  }

  function toggleDictation() {
    // ponytail: Web Speech API bawaan browser (Chrome/Edge), tanpa dependensi.
    const SR = (window as any).SpeechRecognition || (window as any).webkitSpeechRecognition;
    if (!SR) return alert("Dikte tidak didukung browser ini.");
    if (listening) {
      recognition.current?.stop();
      return;
    }
    const rec = new SR();
    rec.lang = "id-ID";
    rec.interimResults = false;
    rec.onresult = (e: any) => {
      const t = e.results[0][0].transcript;
      setDraft((d) => (d ? d.trim() + " " + t : t));
    };
    rec.onend = () => setListening(false);
    rec.onerror = () => setListening(false);
    recognition.current = rec;
    rec.start();
    setListening(true);
  }

  const canSend = draft.trim().length >= 3;

  return (
    <div style={{ display: "flex", flexDirection: "row", height: "100vh", background: "#fff", color: "#112030", overflow: "hidden" }}>
      {sidebarOpen && (
        <aside style={{ flex: "0 0 264px", width: 264, height: "100%", borderRight: "1px solid #e4eaf0", background: "#f5f7fa", display: "flex", flexDirection: "column", padding: 10 }}>
          <div style={{ display: "flex", alignItems: "center", gap: 10, padding: "6px 6px 10px" }}>
            <img src="/assets/brand.png" alt="Perpajakan DDTC" style={{ flex: 1, minWidth: 0, height: 26, width: "auto", objectFit: "contain", objectPosition: "left center" }} />
            <button onClick={() => setSidebarOpen(false)} title="Sembunyikan" className="hov-icon" style={{ ...iconBtn, color: "#98a5b3" }}>
              <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"><path d="M11 17l-5-5 5-5M18 17l-5-5 5-5" /></svg>
            </button>
          </div>

          <div style={{ display: "flex", flexDirection: "column", gap: 1, marginBottom: 14 }}>
            <button onClick={newChat} className="hov" style={sideBtn}>
              <svg width="17" height="17" viewBox="0 0 24 24" fill="none" stroke="#364c63" strokeWidth="1.9" strokeLinecap="round" strokeLinejoin="round"><path d="M12 5v14M5 12h14" /></svg>
              <span style={{ flex: 1, fontSize: 13, color: "#364c63" }}>Obrolan baru</span>
            </button>
          </div>

          <div style={{ flex: 1, minHeight: 0, overflowY: "auto", display: "flex", flexDirection: "column" }}>
            <div style={{ padding: "2px 10px 6px", fontSize: 11, color: "#98a5b3" }}>Obrolan</div>
            <div style={{ display: "flex", flexDirection: "column", gap: 1 }}>
              {chats.map((c) => {
                const on = c.id === activeChat;
                return (
                  <button
                    key={c.id}
                    onClick={() => { autoScroll.current = false; setActiveChat(c.id); }}
                    className={on ? undefined : "hov"}
                    style={{ display: "flex", width: "100%", padding: "8px 10px", border: "none", background: on ? "#fef5ec" : "transparent", borderRadius: 7, cursor: "pointer", textAlign: "left" }}
                  >
                    <span style={{ minWidth: 0, flex: 1, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap", fontSize: 13, fontWeight: on ? 550 : 400, color: on ? "#f77b04" : "#364c63" }}>{c.title}</span>
                  </button>
                );
              })}
            </div>
          </div>

          <div style={{ display: "flex", alignItems: "center", gap: 10, marginTop: 8, padding: "8px 6px 2px", borderTop: "1px solid #e4eaf0" }}>
            <span style={{ flex: "0 0 auto", position: "relative", width: 32, height: 32 }}>
              <img src="/assets/avatar.png" alt="" style={{ width: 32, height: 32, borderRadius: 9999, objectFit: "cover", display: "block" }} />
              {/* Mahkota Lucide di dalam lingkaran oranye, menumpang pembungkus
                  yang memang sudah `position: relative`. Cincin putih 1,5px
                  memisahkannya dari foto. strokeWidth dinaikkan ke 2,6: bawaan
                  Lucide 2 dirancang untuk 24px, dan di 10px garisnya tipis
                  sampai mahkotanya hilang jadi noda oranye. */}
              <span style={{ position: "absolute", top: -3, right: -3, display: "flex", alignItems: "center", justifyContent: "center", width: 13, height: 13, borderRadius: 9999, background: "#f77b04", boxShadow: "0 0 0 1.5px #fff" }}>
                <svg width="8" height="8" viewBox="0 0 24 24" fill="none" stroke="#fff"
                     strokeWidth="2.8" strokeLinecap="round" strokeLinejoin="round" aria-hidden>
                  <path d="M11.562 3.266a.5.5 0 0 1 .876 0L15.39 8.87a1 1 0 0 0 1.516.294L21.183 5.5a.5.5 0 0 1 .798.519l-2.834 10.246a1 1 0 0 1-.956.734H5.81a1 1 0 0 1-.957-.734L2.02 6.02a.5.5 0 0 1 .798-.519l4.276 3.664a1 1 0 0 0 1.516-.294z" />
                  <path d="M5 21h14" />
                </svg>
              </span>
            </span>
            <span style={{ minWidth: 0, flex: 1 }}>
              <span style={{ display: "block", fontSize: 12.5, fontWeight: 550 }}>Tio</span>
              <span style={{ display: "block", fontSize: 11, color: "#98a5b3", overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>tio.ddtc@gmail.co.id</span>
            </span>
          </div>
        </aside>
      )}

      <div style={{ display: "flex", flexDirection: "column", flex: "1 1 auto", minWidth: 0, position: "relative" }}>
        {!sidebarOpen && (
          <button onClick={() => setSidebarOpen(true)} title="Tampilkan sidebar" style={{ position: "absolute", top: 14, left: 14, zIndex: 15, display: "flex", alignItems: "center", justifyContent: "center", width: 34, height: 34, border: "1px solid #e4eaf0", background: "#fff", borderRadius: 8, cursor: "pointer", color: "#364c63", boxShadow: "rgba(11,24,38,0.05) 1px 2px 10px 0px" }}>
            <svg width="17" height="17" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"><path d="M4 6h16M4 12h16M4 18h16" /></svg>
          </button>
        )}

        <div
          ref={scrollEl}
          // Menempel ke dasar selama pembaca memang sedang di dasar. Begitu ia
          // menggulir naik untuk membaca ulang, penempelan berhenti -- jawaban
          // yang tumbuh 20 detik tidak boleh menyeretnya balik ke bawah.
          onScroll={(e) => {
            const el = e.currentTarget;
            autoScroll.current = el.scrollHeight - el.scrollTop - el.clientHeight < 80;
          }}
          style={{ flex: "1 1 auto", overflowY: "auto", background: "#fff" }}
        >
          <div ref={isiEl} style={{ maxWidth: 780, margin: "0 auto", padding: "26px 24px 12px", display: "flex", flexDirection: "column", gap: 26 }}>
            {messages.length === 0 && (
              <div style={{ display: "flex", flexDirection: "column", alignItems: "center", justifyContent: "center", textAlign: "center", padding: "80px 0", gap: 14 }}>
                <img src="/assets/mark.png" alt="" style={{ height: 46, width: "auto", objectFit: "contain" }} />
                <div style={{ fontSize: 18, fontWeight: 550 }}>Ada yang bisa dibantu seputar perpajakan?</div>
                <div style={{ fontSize: 13, color: "#98a5b3", maxWidth: 360 }}>Tanyakan peraturan, tarif, atau ketentuan pajak.</div>
              </div>
            )}

            {messages.map((m) =>
              m.role === "user" ? (
                <div key={m.id} style={{ display: "flex", flexDirection: "column", alignItems: "flex-end", gap: 6 }}>
                  <div style={{ maxWidth: "82%", background: "#f5f7fa", border: "1px solid #e4eaf0", borderRadius: "14px 14px 4px 14px", padding: "10px 14px", fontSize: 13.5, lineHeight: 1.5 }}>{m.text}</div>
                </div>
              ) : (
                <Assistant
                  key={m.id}
                  msg={m}
                  sourcesOpen={openSourcesId === m.id}
                  toggleSources={() => setOpenSourcesId((s) => (s === m.id ? null : m.id))}
                  openChunks={openChunks}
                  toggleChunk={(blok) => setOpenChunks((s) => ({ ...s, [m.id + "#" + blok]: !(s[m.id + "#" + blok] ?? false) }))}
                  onCite={(blok) => openCite(m.id, blok)}
                  onFollowup={send}
                />
              ),
            )}
          </div>
        </div>

        <div style={{ flex: "0 0 auto", background: "#fff", padding: "4px 24px 18px" }}>
          <div style={{ maxWidth: 780, margin: "0 auto", position: "relative" }}>
            {modelOpen && (
              <div style={{ position: "absolute", left: 12, bottom: "100%", marginBottom: 8, width: 196, background: "#fff", border: "1px solid #e4eaf0", borderRadius: 10, boxShadow: "0 10px 34px rgba(11,24,38,0.12)", padding: 4, animation: "pop-in 160ms cubic-bezier(0.23,1,0.32,1) both", transformOrigin: "bottom left", zIndex: 30 }}>
                {MODELS.map((mo) => (
                  <button key={mo.name} onClick={() => { setModel(mo); setModelOpen(false); }} className="hov-row" style={{ display: "flex", alignItems: "center", gap: 8, width: "100%", height: 32, padding: "0 8px", border: "none", background: "transparent", borderRadius: 6, cursor: "pointer", textAlign: "left" }}>
                    <span style={{ flex: 1, fontSize: 12.5 }}>{mo.name}</span>
                    <span style={{ fontSize: 11, color: "#98a5b3" }}>{mo.tag}</span>
                    {mo.key === model.key ? (
                      <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="#f77b04" strokeWidth="2.6" strokeLinecap="round" strokeLinejoin="round"><path d="M20 6L9 17l-5-5" /></svg>
                    ) : (
                      <span style={{ width: 13 }} />
                    )}
                  </button>
                ))}
              </div>
            )}

            <div style={{ border: "1px solid #e4eaf0", background: "#fff", borderRadius: 14, boxShadow: "rgba(11,24,38,0.05) 1px 2px 10px 0px", padding: "8px 10px" }}>
              <textarea
                ref={inputEl}
                rows={1}
                value={draft}
                placeholder={listening ? "Mendengarkan…" : "Tanyakan tentang peraturan pajak"}
                onChange={(e) => {
                  e.target.style.height = "auto";
                  e.target.style.height = Math.min(e.target.scrollHeight, 120) + "px";
                  setDraft(e.target.value);
                }}
                onKeyDown={(e) => {
                  if (e.key === "Enter" && !e.shiftKey) {
                    e.preventDefault();
                    send(draft);
                  }
                }}
                style={{ display: "block", width: "100%", resize: "none", border: "none", outline: "none", background: "transparent", padding: "5px 4px", minHeight: 30, maxHeight: 120, fontSize: 13.5, lineHeight: "20px", color: "#112030" }}
              />

              <div style={{ display: "flex", alignItems: "center", gap: 6, marginTop: 4 }}>
                <button onClick={() => setModelOpen((v) => !v)} className="hov-row" style={{ display: "flex", alignItems: "center", gap: 5, height: 30, padding: "0 10px", border: "none", background: "transparent", borderRadius: 8, cursor: "pointer", fontSize: 12, color: "#364c63" }}>
                  {model.name}
                  <svg width="11" height="11" viewBox="0 0 24 24" fill="none" stroke="#98a5b3" strokeWidth="2.4" strokeLinecap="round" strokeLinejoin="round"><path d="M6 9l6 6 6-6" /></svg>
                </button>
                <div style={{ flex: 1 }} />
                <button onClick={toggleDictation} title="Dikte" style={{ display: "flex", alignItems: "center", justifyContent: "center", width: 30, height: 30, border: "none", borderRadius: 8, cursor: "pointer", background: listening ? "#fef5ec" : "transparent", color: listening ? "#f77b04" : "#364c63" }}>
                  {listening ? (
                    <span style={{ display: "flex", alignItems: "center", gap: 2.5, height: 14 }}>
                      {[0, 150, 300].map((d) => (
                        <span key={d} style={{ width: 2.5, height: "100%", borderRadius: 2, background: "currentColor", animation: `eq 900ms ease-in-out ${d}ms infinite` }} />
                      ))}
                    </span>
                  ) : (
                    <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"><path d="M12 2a3 3 0 0 0-3 3v7a3 3 0 0 0 6 0V5a3 3 0 0 0-3-3z" /><path d="M19 10v2a7 7 0 0 1-14 0v-2M12 19v3" /></svg>
                  )}
                </button>
                <button onClick={() => send(draft)} title="Kirim" style={{ display: "flex", alignItems: "center", justifyContent: "center", width: 30, height: 30, border: "none", borderRadius: 8, transition: "background 0.15s", background: canSend ? "#f77b04" : "#e4eaf0", color: canSend ? "#fff" : "#98a5b3", cursor: canSend ? "pointer" : "default" }}>
                  <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.4" strokeLinecap="round" strokeLinejoin="round"><path d="M12 19V5M5 12l7-7 7 7" /></svg>
                </button>
              </div>
            </div>
            <div style={{ textAlign: "center", fontSize: 11, color: "#98a5b3", marginTop: 8 }}>
              Asisten Pajak dapat keliru. Verifikasi ketentuan pada sumber resmi.
            </div>
          </div>
        </div>
      </div>
    </div>
  );
}

function Assistant({
  msg,
  sourcesOpen,
  toggleSources,
  openChunks,
  toggleChunk,
  onCite,
  onFollowup,
}: {
  msg: Extract<Msg, { role: "assistant" }>;
  sourcesOpen: boolean;
  toggleSources: () => void;
  openChunks: Record<string, boolean>;
  toggleChunk: (blok: number) => void;
  onCite: (blok: number) => void;
  onFollowup: (t: string) => void;
}) {
  const d = msg.data;
  // Jawaban sudah tampil sampai kata terakhir -- bukan sekadar respons sudah
  // sampai. Dua-duanya beda waktu: teksnya masih menetes setelah data lengkap.
  const [ditulis, setDitulis] = useState(false);
  const tuntas = useCallback(() => setDitulis(true), []);
  // satu kartu sumber per dokumen; blok tetap dipakai untuk sitasi.
  const docs = d
    ? Array.from(new Map(d.sumber.map((s) => [s.document_id, s])).values())
    : [];

  return (
    <div style={{ display: "flex", gap: 12, alignItems: "flex-start", animation: "fade-up 400ms cubic-bezier(0.23,1,0.32,1) both" }}>
      <span style={{ flex: "0 0 auto", display: "flex", alignItems: "center", justifyContent: "center", width: 30, height: 30, borderRadius: 9999, background: "#fff", border: "1px solid #e4eaf0" }}>
        <img src="/assets/mark.png" alt="" style={{ width: 17, height: "auto", objectFit: "contain" }} />
      </span>

      <div style={{ flex: 1, minWidth: 0, display: "flex", flexDirection: "column", gap: 14 }}>
        {!msg.error && <Thinking startedAt={msg.startedAt} data={d} />}

        {msg.error && (
          <div style={{ fontSize: 13, color: "#f77979", background: "#fff5f5", border: "1px solid #ffdede", borderRadius: 8, padding: "8px 12px" }}>{msg.error}</div>
        )}

        {/* Selagi mengalir, sitasinya belum bisa diklik: nomornya masih penomoran
            BAHAN dan kartunya belum ada. Setelah `d` datang barulah tersambung. */}
        {(d || msg.stream) && (
          <Jawaban
            teks={d ? d.jawaban : msg.stream!}
            selesai={!!d}
            onCite={d ? onCite : undefined}
            onTuntas={tuntas}
          />
        )}

        {/* Tombol, kartu sumber, dan pertanyaan lanjutan menunggu kalimat
            terakhir selesai tampil. Muncul di tengah jawaban yang masih ditulis
            bikin halamannya melompat persis waktu orang sedang membaca. */}
        {d && ditulis && (
          <>
            <div style={{ display: "flex", alignItems: "center", gap: 2, ...muncul(0) }}>
              <button title="Salin" onClick={() => navigator.clipboard.writeText(d.jawaban)} className="hov-row" style={{ ...iconBtn, color: "#98a5b3" }}>
                <svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round"><rect x="9" y="9" width="12" height="12" rx="2.5" /><path d="M5 15H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v1" /></svg>
              </button>
              <button onClick={toggleSources} className="hov-row" style={{ display: "flex", alignItems: "center", gap: 8, marginLeft: 8, padding: "4px 8px", border: "none", background: "transparent", borderRadius: 6, cursor: "pointer" }}>
                <span style={{ display: "flex" }}>
                  {docs.slice(0, 4).map((s) => (
                    <span key={s.document_id} style={{ display: "flex", alignItems: "center", justifyContent: "center", width: 16, height: 16, marginLeft: -4, borderRadius: 9999, background: "#fff", border: "1px solid #e4eaf0", boxShadow: "0 0 0 1.5px #fff" }}><IkonDDTC size={16} /></span>
                  ))}
                </span>
                <span style={{ fontSize: 12, color: "#364c63" }}>{docs.length} sumber</span>
              </button>
            </div>

            {sourcesOpen && docs.length > 0 && (
              <div style={{ display: "flex", flexDirection: "column", border: "1px solid #e4eaf0", borderRadius: 10, background: "#f5f7fa", padding: 4, animation: "fade-up 260ms cubic-bezier(0.23,1,0.32,1) both" }}>
                {docs.map((s) => (
                  <div key={s.document_id} style={{ display: "flex", alignItems: "center", gap: 9, padding: "6px 8px", borderRadius: 6, fontSize: 12.5, color: "#364c63" }}>
                    <span style={{ display: "flex", alignItems: "center", justifyContent: "center", width: 18, height: 18, borderRadius: 9999, background: "#fff", border: "1px solid #e4eaf0" }}><IkonDDTC size={18} /></span>
                    <span><Tautan url={s.url} teks={s.sebutan} /></span>
                    <span style={{ marginLeft: "auto", fontSize: 11, color: "#98a5b3" }}>{s.document_id}</span>
                  </div>
                ))}
              </div>
            )}

            {d.sumber.length > 0 && (
              <div style={{ display: "flex", flexDirection: "column", gap: 8, marginTop: 2 }}>
                <div style={{ display: "flex", alignItems: "center", gap: 8, fontSize: 12, fontWeight: 550, color: "#364c63", ...muncul(1) }}>
                  <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.2" strokeLinecap="round"><path d="M4 6h16M4 12h16M4 18h10" /></svg>
                  <span>Sumber yang diambil</span>
                  <span style={{ display: "inline-flex", alignItems: "center", height: 18, padding: "0 7px", borderRadius: 6, background: "#f5f7fa", border: "1px solid #e4eaf0", fontSize: 11, color: "#364c63" }}>{d.sumber.length}</span>
                </div>

                {d.sumber.map((c, i) => {
                  // Tertutup dulu. Sepuluh kartu yang terbuka sekaligus mengubur
                  // jawabannya sendiri; yang mau dibaca dibuka sendiri, atau lewat
                  // klik nomor sitasinya.
                  const open = openChunks[msg.id + "#" + c.blok] ?? false;
                  return (
                    <div key={c.unit_id} id={`chunk-${msg.id}-${c.blok}`} style={{ overflow: "hidden", border: "1px solid #e4eaf0", borderRadius: 8, background: "#fff", boxShadow: "rgba(11,24,38,0.05) 1px 2px 10px 0px", ...muncul(2 + i) }}>
                      <button onClick={() => toggleChunk(c.blok)} className="hov-row" style={{ display: "flex", alignItems: "center", gap: 10, width: "100%", padding: "8px 12px", border: "none", background: "transparent", cursor: "pointer", textAlign: "left" }}>
                        <span style={{ minWidth: 0, display: "flex", alignItems: "center", gap: 8, fontSize: 13, fontWeight: 550, color: "#112030" }}>
                          <span style={{ flex: "0 0 auto", display: "flex", alignItems: "center", justifyContent: "center", minWidth: 18, height: 18, borderRadius: 9999, background: "#fef5ec", color: "#f77b04", fontSize: 11 }}>{c.blok}</span>
                          {/* Peraturannya dulu, pasalnya belakangan. "Pasal 21" saja
                              tidak menunjuk apa pun: tiap peraturan punya Pasal 21. */}
                          <span style={{ overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>
                            {[c.sebutan, c.label || c.alamat].filter(Boolean).join(", ")}
                          </span>
                        </span>
                        <span style={{ marginLeft: "auto", flex: "0 0 auto", fontSize: 12, color: "#98a5b3" }}>{c.teks.length} karakter</span>
                        <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="#98a5b3" strokeWidth="2.4" strokeLinecap="round" strokeLinejoin="round" style={{ flex: "0 0 auto" }}>
                          <path d={open ? "M18 15l-6-6-6 6" : "M6 9l6 6 6-6"} />
                        </svg>
                      </button>
                      {open && (
                        <div style={{ borderTop: "1px solid #e4eaf0" }}>
                          <p style={{ margin: 0, padding: "8px 12px 4px", fontSize: 12.5, lineHeight: 1.55, color: "#364c63", whiteSpace: "pre-wrap" }}>{c.teks}</p>
                          <div style={{ padding: "0 12px 12px" }}>
                            <span style={{ display: "inline-flex", alignItems: "center", gap: 7, height: 24, padding: "0 9px", borderRadius: 9999, background: "#f5f7fa", border: "1px solid #e4eaf0", fontSize: 12, color: "#364c63" }}>
                              <span style={{ display: "flex", alignItems: "center", justifyContent: "center", minWidth: 15, height: 15, padding: "0 3px", borderRadius: 4, background: c.perubahan ? "#f77979" : "#0f9978", color: "#fff", fontSize: 7 }}>
                                {(c.kelas || "unit").slice(0, 3).toUpperCase()}
                              </span>
                              <Tautan url={c.url} teks={c.sebutan} />
                              {c.berlaku && <span style={{ color: "#98a5b3", fontSize: 11 }}>{c.berlaku}</span>}
                            </span>
                          </div>
                        </div>
                      )}
                    </div>
                  );
                })}
              </div>
            )}

            {d.saran.length > 0 && (
              <div>
                {/* Menyambung deret kartu sumber, bukan mulai dari nol -- kalau tidak,
                    dua kolom muncul bebarengan dan berebut perhatian. */}
                <div style={{ fontSize: 12, fontWeight: 550, color: "#364c63", marginBottom: 4, ...muncul(2 + d.sumber.length) }}>Pertanyaan lanjutan</div>
                <div style={{ display: "flex", flexDirection: "column" }}>
                  {d.saran.map((f, i) => (
                    <button key={f} onClick={() => onFollowup(f)} className="hov-row" style={{ display: "flex", alignItems: "center", gap: 10, padding: "9px 6px", border: "none", borderBottom: "1px solid #e4eaf0", background: "transparent", cursor: "pointer", textAlign: "left", fontSize: 12.5, color: "#112030", ...muncul(3 + d.sumber.length + i) }}>
                      <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="#98a5b3" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" style={{ flex: "0 0 auto" }}><path d="M9 10l-5 5 5 5" /><path d="M20 4v7a4 4 0 0 1-4 4H4" /></svg>
                      {f}
                    </button>
                  ))}
                </div>
              </div>
            )}
          </>
        )}
      </div>
    </div>
  );
}

// Bagian di bawah jawaban muncul berurutan, bukan serentak. Semuanya sudah ada
// di DOM begitu jawaban selesai; yang diatur cuma kapan masing-masing terlihat,
// jadi tinggi halamannya tetap dan tidak ada yang bergeser.
const TAHAP_MS = 90;
const muncul = (n: number): CSSProperties => ({
  animation: `fade-up 350ms cubic-bezier(0.23,1,0.32,1) ${n * TAHAP_MS}ms both`,
});

const citeBtn: CSSProperties = {
  display: "inline-flex",
  alignItems: "center",
  justifyContent: "center",
  minWidth: 15,
  height: 15,
  margin: "0 1px 0 2px",
  padding: "0 3px",
  border: "none",
  borderRadius: 9999,
  background: "#fef5ec",
  color: "#f77b04",
  fontSize: 9,
  verticalAlign: "middle",
  transform: "translateY(-3px)",
};

/** Bungkus tiap kata jadi <span class="w"> supaya animasinya per kata.
 *  React memakai ulang span di posisi yang sama, jadi kata lama tidak ikut
 *  beranimasi ulang tiap potongan baru datang -- yang beranimasi cuma yang
 *  memang baru muncul. */
// Opsinya dibungkus objek, bukan boolean telanjang: unified membaca `true`
// sebagai "nyalakan plugin ini" dan tidak meneruskannya sebagai argumen.
function rehypeKata({ mengalir }: { mengalir: boolean }) {
  return (pohon: any) => {
    const kata: any[] = [];
    visit(pohon, "text", (simpul: any, i: number | undefined, induk: any) => {
      if (i === undefined || !induk || induk.type !== "element") return;
      if (induk.tagName === "code" || induk.tagName === "a") return;   // sitasi & kode utuh
      const bagian = simpul.value.split(/(\s+)/).filter(Boolean);
      if (!bagian.length) return;
      const baru = bagian.map((t: string) =>
        /^\s+$/.test(t)
          ? { type: "text", value: t }
          : { type: "element", tagName: "span", properties: { className: ["w"] }, children: [{ type: "text", value: t }] },
      );
      baru.forEach((n: any) => n.tagName === "span" && kata.push(n));
      induk.children.splice(i, 1, ...baru);
      return i + baru.length;
    });
    if (mengalir && kata.length) kata[kata.length - 1].properties.className.push("akhir");
  };
}

/** Potong `teks` tepat di ujung kata ke-`kata`. */
function potong(teks: string, kata: number) {
  if (kata <= 0) return "";
  const re = /\S+/g;
  let m: RegExpExecArray | null;
  let n = 0;
  let akhir = 0;
  while ((m = re.exec(teks))) {
    n++;
    akhir = m.index + m[0].length;
    if (n >= kata) return teks.slice(0, akhir);
  }
  return teks;
}

const jumlahKata = (teks: string) => (teks.match(/\S+/g) ?? []).length;

function Jawaban({
  teks,
  selesai,
  onCite,
  onTuntas,
}: {
  teks: string;
  selesai: boolean;
  onCite?: (blok: number) => void;
  onTuntas?: () => void;
}) {
  // Pesan lama langsung utuh: yang dipasang ulang waktu pindah obrolan tidak
  // boleh mengetik ulang dirinya sendiri dari nol.
  const [tampil, setTampil] = useState(() => (selesai ? jumlahKata(teks) : 0));
  const total = jumlahKata(teks);
  const tuntas = tampil >= total;

  useEffect(() => {
    if (tuntas) return;
    // ponytail: satu kata per denyut, tanpa mengejar ketertinggalan. Jawaban
    // 400 kata butuh 400 x STREAM_MS untuk tampil penuh sekalipun modelnya
    // sudah selesai duluan. Itu memang maunya -- kalau kepanjangan, kecilkan
    // NEXT_PUBLIC_STREAM_MS, jangan tambahkan logika kejar-kejaran.
    const t = setTimeout(() => setTampil((n) => n + 1), STREAM_MS);
    return () => clearTimeout(t);
  }, [tampil, tuntas]);

  useEffect(() => {
    if (selesai && tuntas) onTuntas?.();
  }, [selesai, tuntas, onTuntas]);

  return <Markdown teks={potong(teks, tampil)} mengalir={!tuntas || !selesai} onCite={onCite} />;
}

function Markdown({
  teks,
  mengalir,
  onCite,
}: {
  teks: string;
  mengalir: boolean;
  onCite?: (blok: number) => void;
}) {
  // Sitasi "[3]" ditulis ulang jadi tautan markdown "[3](#blok-3)", lalu
  // ditangkap kembali lewat komponen `a`. Kelihatan memutar, tapi artinya
  // parser markdown yang mengurus DI MANA sitasi boleh muncul -- di sel tabel,
  // di dalam butir daftar, di tengah teks tebal. Memotong string sendiri
  // (cara lama) cuma jalan selama jawabannya satu paragraf polos.
  // ponytail: penulisan ulangnya membabi buta, termasuk "[3]" yang kebetulan
  // ada di dalam blok kode. Jawaban regulasi tidak memuat kode; kalau nanti
  // memuat, saring lewat plugin rehype -- bukan dengan regex yang lebih pintar.
  const md = teks.replace(/\[(\d+)\]/g, (_, n) => `[${n}](#blok-${n})`);
  return (
    <div className="md">
      <ReactMarkdown
        remarkPlugins={[remarkGfm]}
        rehypePlugins={[[rehypeKata, { mengalir }]]}
        components={{
          a({ href, children, ...sisa }) {
            const blok = href?.startsWith("#blok-") ? Number(href.slice(6)) : null;
            if (blok === null)
              return (
                <a href={href} target="_blank" rel="noreferrer" {...sisa}>
                  {children}
                </a>
              );
            return (
              <button
                type="button"
                disabled={!onCite}
                onClick={() => onCite?.(blok)}
                title={onCite ? "Lihat sumber" : undefined}
                style={{ ...citeBtn, cursor: onCite ? "pointer" : "default" }}
              >
                {blok}
              </button>
            );
          },
        }}
      >
        {md}
      </ReactMarkdown>
    </div>
  );
}

const iconBtn: CSSProperties = {
  display: "flex",
  alignItems: "center",
  justifyContent: "center",
  width: 28,
  height: 28,
  border: "none",
  background: "transparent",
  borderRadius: 7,
  cursor: "pointer",
  flex: "0 0 auto",
};

const sideBtn: CSSProperties = {
  display: "flex",
  alignItems: "center",
  gap: 10,
  width: "100%",
  padding: "8px 10px",
  border: "none",
  background: "transparent",
  borderRadius: 7,
  cursor: "pointer",
  textAlign: "left",
};

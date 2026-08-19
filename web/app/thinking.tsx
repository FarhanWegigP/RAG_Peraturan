"use client";

import { useEffect, useState } from "react";

/* Jejak proses satu jawaban. Selama menunggu: penghitung waktu berjalan.
 * Setelah selesai: rincian tahap dari respons -- planner, retrieval, penjawab.
 * ponytail: /ask/stream cuma mengalirkan TEKS jawaban -- rincian tahap tetap
 * ikut peristiwa "selesai", jadi barisnya masih muncul sekaligus di akhir.
 * Kalau mau muncul satu per satu, backend harus mengirim peristiwa per tahap
 * (rag.jawab_alir sudah berbentuk generator, tinggal menambah yield). */

export type Timing = {
  detik_proses: number;
  detik_planner: number;
  detik_jawab: number;
  model_planner: string;
  model_jawab: string;
  jumlah_blok: number;
};

const detik = (n: number) => n.toFixed(1).replace(".", ",") + "d";

export default function Thinking({ startedAt, data }: { startedAt: number; data?: Timing }) {
  const [now, setNow] = useState(startedAt);
  const [open, setOpen] = useState(false);

  useEffect(() => {
    if (data) return;
    const t = setInterval(() => setNow(Date.now()), 100);
    return () => clearInterval(t);
  }, [data]);

  const working = !data;
  const elapsed = (now - startedAt) / 1000;

  // sisa waktu di luar planner & penjawab: retrieval + rerank + saran.
  const sisa = data ? Math.max(0, data.detik_proses - data.detik_planner - data.detik_jawab) : 0;
  const rows = data
    ? [
        { primary: "Menyusun rencana", secondary: data.model_planner, t: data.detik_planner },
        { primary: "Mengambil bahan", secondary: `${data.jumlah_blok} blok`, t: sisa },
        { primary: "Menulis jawaban", secondary: data.model_jawab, t: data.detik_jawab },
      ]
    : [];

  return (
    <div style={{ display: "flex", flexDirection: "column" }}>
      <button
        type="button"
        aria-expanded={open}
        onClick={() => !working && setOpen((v) => !v)}
        className={working ? undefined : "hov-row"}
        style={{
          display: "flex",
          width: "fit-content",
          alignItems: "center",
          gap: 8,
          margin: "0 0 0 -6px",
          padding: "4px 6px",
          border: "none",
          background: "transparent",
          borderRadius: 7,
          cursor: working ? "default" : "pointer",
        }}
      >
        <svg width="16" height="16" viewBox="0 0 24 24" fill={working ? "#364c63" : "#98a5b3"}>
          <path d="M12 2l2.4 7.2L22 12l-7.6 2.8L12 22l-2.4-7.2L2 12l7.6-2.8z" />
        </svg>
        {working ? (
          <span
            role="status"
            style={{
              fontSize: 13,
              fontWeight: 500,
              whiteSpace: "nowrap",
              color: "transparent",
              backgroundClip: "text",
              WebkitBackgroundClip: "text",
              backgroundImage: "linear-gradient(90deg, #98a5b3 35%, #112030 50%, #98a5b3 65%)",
              backgroundSize: "200% 100%",
              animation: "shimmer-text 1.4s linear infinite",
            }}
          >
            Menyusun jawaban {detik(elapsed)}
          </span>
        ) : (
          <>
            <span style={{ fontSize: 13, fontWeight: 500, whiteSpace: "nowrap", color: "#364c63", animation: "fade-in 350ms ease-out both" }}>
              Berpikir {data!.detik_proses.toFixed(1).replace(".", ",")} detik
            </span>
            <svg
              width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="#98a5b3" strokeWidth="2.2" strokeLinecap="round" strokeLinejoin="round"
              style={{ transition: "transform 300ms", transform: open ? "rotate(180deg)" : "rotate(0)" }}
            >
              <path d="M6 9l6 6 6-6" />
            </svg>
          </>
        )}
      </button>

      <div
        style={{
          display: "grid",
          gridTemplateRows: open ? "1fr" : "0fr",
          opacity: open ? 1 : 0,
          transition: "grid-template-rows 400ms cubic-bezier(0.23,1,0.32,1), opacity 400ms cubic-bezier(0.23,1,0.32,1)",
        }}
      >
        <div style={{ overflow: "hidden" }}>
          <div style={{ marginTop: 2, marginLeft: 7, paddingLeft: 15, borderLeft: "1px solid #e4eaf0", display: "flex", flexDirection: "column", gap: 2, paddingTop: 4, paddingBottom: 4 }}>
            {rows.map((row, i) => (
              <div
                key={row.primary}
                style={{ display: "flex", minHeight: 26, alignItems: "center", gap: 8, padding: "2px 6px", animation: open ? `fade-up 320ms cubic-bezier(0.23,1,0.32,1) ${i * 90}ms both` : undefined }}
              >
                <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="#98a5b3" strokeWidth="2.5" strokeLinecap="round" strokeLinejoin="round" style={{ flex: "0 0 auto" }}>
                  <path d="M20 6L9 17l-5-5" />
                </svg>
                <span style={{ minWidth: 0, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap", fontSize: 12.5, fontWeight: 500, color: "#112030" }}>{row.primary}</span>
                <span style={{ flex: "0 0 auto", fontSize: 11.5, color: "#98a5b3" }}>{row.secondary || "lokal"}</span>
                <span style={{ marginLeft: "auto", flex: "0 0 auto", fontSize: 11.5, color: "#364c63", fontVariantNumeric: "tabular-nums" }}>{detik(row.t)}</span>
              </div>
            ))}
          </div>
        </div>
      </div>
    </div>
  );
}

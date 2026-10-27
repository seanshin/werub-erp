"use client";
/** 재고 워크스테이션 — 독립 전체화면(전용 단말/키오스크). 일일 PIN(알바) 입장 또는 ERP 세션. */
import { useEffect, useState } from "react";
import { ApiError, api, auth } from "@/lib/api";
import { APP_VERSION } from "@/lib/app-info";
import { InventoryWorkstation } from "@/components/inventory-workstation";

export default function WsStandalone() {
  const [authed, setAuthed] = useState<boolean | null>(null);  // null=확인중
  const [pin, setPin] = useState("");
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState("");

  useEffect(() => { auth.bootstrap().then((ok) => setAuthed(ok)); }, []);

  const enter = async () => {
    setErr(""); setBusy(true);
    try {
      await api("/inventory/workstation/passes/login", { method: "POST",
        body: JSON.stringify({ pin }) });
      window.location.reload();   // 쿠키 발급됨 → 새로고침 후 워크스테이션 진입
    } catch (e) {
      setErr(e instanceof ApiError ? "PIN이 올바르지 않거나 만료되었습니다" : "입장 실패");
      setBusy(false);
    }
  };

  if (authed === null) return null;

  if (!authed) {
    return (
      <main className="flex min-h-screen items-center justify-center bg-gradient-to-br from-slate-50 to-blue-50/40 p-4">
        <div className="w-full max-w-xs">
          <div className="mb-6 flex flex-col items-center gap-2">
            <span className="flex h-12 w-12 items-center justify-center rounded-2xl bg-blue-600 text-xl font-bold text-white shadow-lg">H</span>
            <h1 className="text-lg font-bold text-slate-900">재고 워크스테이션</h1>
            <p className="text-[12px] text-slate-400">담당자에게 받은 일일 PIN을 입력하세요</p>
          </div>
          <div className="rounded-2xl border border-slate-100 bg-white p-6 shadow-xl">
            <input autoFocus type="password" inputMode="numeric" maxLength={6} value={pin}
              onChange={(e) => setPin(e.target.value.replace(/\D/g, ""))}
              onKeyDown={(e) => { if (e.key === "Enter" && pin.length === 6) enter(); }}
              placeholder="● ● ● ● ● ●"
              className="w-full rounded-xl border-2 border-blue-200 bg-blue-50/40 px-4 py-3 text-center text-xl tracking-[0.5em] outline-none focus:border-blue-400" />
            {err && <p className="mt-2 text-center text-xs text-red-600">{err}</p>}
            <button disabled={pin.length !== 6 || busy} onClick={enter}
              className="mt-3 w-full rounded-xl bg-blue-600 py-3 text-sm font-semibold text-white disabled:bg-slate-300">
              {busy ? "입장 중…" : "입장"}
            </button>
          </div>
          <p className="mt-4 text-center text-[11px] text-slate-400">병원 ERP v{APP_VERSION}</p>
        </div>
      </main>
    );
  }

  return (
    <div className="min-h-screen bg-slate-50">
      <header className="flex items-center justify-between border-b border-slate-200 bg-white px-5 py-3">
        <span className="flex items-center gap-2 font-bold text-slate-900">
          <span className="flex h-7 w-7 items-center justify-center rounded-lg bg-blue-600 text-sm font-bold text-white">H</span>
          재고 워크스테이션
        </span>
        <span className="flex items-center gap-4 text-xs text-slate-400">
          <button onClick={() => auth.logout()} className="font-medium text-slate-500 hover:text-red-600">종료</button>
          <span>v{APP_VERSION}</span>
        </span>
      </header>
      <main className="p-4"><InventoryWorkstation /></main>
    </div>
  );
}

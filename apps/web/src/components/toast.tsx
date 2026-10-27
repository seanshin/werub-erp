"use client";
/** 전역 토스트 — 성공/실패 피드백 표준(전체검토 X1-1). 과거 화면별 setMsg + 파란 <p>(성공·실패
 * 동일 색)를 대체. 긴 페이지·모달 뒤에 묻히던 메시지를 화면 우상단 고정 스택으로 자동소멸 표시. */
import { createContext, useCallback, useContext, useState } from "react";

type Tone = "ok" | "err" | "info";
interface Toast { id: number; text: string; tone: Tone }

const ToastCtx = createContext<((text: string, tone: Tone) => void) | null>(null);

let _seq = 0;
const TONE_CLS: Record<Tone, string> = {
  ok: "bg-emerald-600 text-white",
  err: "bg-red-600 text-white",
  info: "bg-slate-700 text-white",
};

export function ToastProvider({ children }: { children: React.ReactNode }) {
  const [toasts, setToasts] = useState<Toast[]>([]);
  const push = useCallback((text: string, tone: Tone) => {
    const id = ++_seq;
    setToasts((t) => [...t, { id, text, tone }]);
    setTimeout(() => setToasts((t) => t.filter((x) => x.id !== id)),
              tone === "err" ? 6000 : 3500);   // 오류는 더 오래
  }, []);
  return (
    <ToastCtx.Provider value={push}>
      {children}
      <div className="pointer-events-none fixed right-4 top-4 z-[100] flex max-w-sm flex-col gap-2"
        aria-live="polite">
        {toasts.map((t) => (
          <div key={t.id} role="status"
            className={`pointer-events-auto cursor-pointer rounded-md px-4 py-2.5 text-sm shadow-lg ${TONE_CLS[t.tone]}`}
            onClick={() => setToasts((ts) => ts.filter((x) => x.id !== t.id))}>
            {t.text}
          </div>
        ))}
      </div>
    </ToastCtx.Provider>
  );
}

/** 토스트 발신 — toast.ok(성공)/toast.err(실패)/toast.info. 프로바이더 밖이면 no-op(안전). */
export function useToast() {
  const push = useContext(ToastCtx);
  return {
    ok: (m: string) => push?.(m, "ok"),
    err: (m: string) => push?.(m, "err"),
    info: (m: string) => push?.(m, "info"),
  };
}

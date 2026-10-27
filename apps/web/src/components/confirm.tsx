"use client";
/** 전역 확인 다이얼로그 — window.confirm 및 무확인 파괴/권한 액션 대체(전체검토 X1-2).
 * useConfirm()로 Promise<boolean> 반환. 접근성(포커스트랩·ESC)은 Modal 재사용. */
import { createContext, useCallback, useContext, useRef, useState } from "react";

import { Button, Modal } from "@/components/ui";

interface ConfirmOpts {
  title: string;
  body?: string;
  confirmLabel?: string;
  danger?: boolean;
}

const ConfirmCtx = createContext<((o: ConfirmOpts) => Promise<boolean>) | null>(null);

export function ConfirmProvider({ children }: { children: React.ReactNode }) {
  const [opts, setOpts] = useState<ConfirmOpts | null>(null);
  const resolver = useRef<((v: boolean) => void) | null>(null);

  const confirm = useCallback((o: ConfirmOpts) => {
    setOpts(o);
    return new Promise<boolean>((res) => { resolver.current = res; });
  }, []);

  const close = (v: boolean) => { setOpts(null); resolver.current?.(v); resolver.current = null; };

  return (
    <ConfirmCtx.Provider value={confirm}>
      {children}
      {opts && (
        <Modal title={opts.title} onClose={() => close(false)}>
          {opts.body && <p className="whitespace-pre-line text-sm text-slate-600">{opts.body}</p>}
          <div className="mt-4 flex justify-end gap-2">
            <button onClick={() => close(false)}
              className="rounded-md px-3 py-1.5 text-sm text-slate-500 hover:bg-slate-100">취소</button>
            <Button variant={opts.danger ? "danger" : "primary"} onClick={() => close(true)}>
              {opts.confirmLabel ?? "확인"}
            </Button>
          </div>
        </Modal>
      )}
    </ConfirmCtx.Provider>
  );
}

/** confirm({title, body, danger}) → Promise<boolean>. 프로바이더 밖이면 즉시 true(폴백). */
export function useConfirm() {
  const c = useContext(ConfirmCtx);
  return c ?? (async () => true);
}

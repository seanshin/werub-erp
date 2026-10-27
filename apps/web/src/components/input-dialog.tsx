"use client";
/** 재사용 입력 다이얼로그 — window.prompt 대체(검증 포함). ask()는 Promise로 값/취소(null) 반환. */
import { useCallback, useId, useRef, useState } from "react";

import { Button, Modal } from "@/components/ui";

type AskOpts = {
  title: string;
  label?: string;
  hint?: string;
  defaultValue?: string;
  placeholder?: string;
  kind?: "number" | "text" | "date";   // 기본 text
  min?: number;
  max?: number;
  required?: boolean;
  confirmLabel?: string;
  validate?: (value: string) => string | null;   // 커스텀 검증 — 오류 메시지 또는 null
};

export function useInputDialog() {
  const [opts, setOpts] = useState<AskOpts | null>(null);
  const [val, setVal] = useState("");
  const [err, setErr] = useState("");
  const resolver = useRef<((v: string | number | null) => void) | null>(null);
  const inputId = useId();   // 라벨-입력 연결(스크린리더·라벨 클릭 포커스)

  const ask = useCallback((o: AskOpts) => {
    setOpts(o); setVal(o.defaultValue ?? ""); setErr("");
    return new Promise<string | number | null>((res) => { resolver.current = res; });
  }, []);

  const close = (result: string | number | null) => {
    setOpts(null); resolver.current?.(result); resolver.current = null;
  };

  const submit = () => {
    if (!opts) return;
    if (opts.kind === "number") {
      const n = Number(val);
      if (val.trim() === "" || Number.isNaN(n)) { setErr("숫자를 입력하세요"); return; }
      if (opts.min != null && n < opts.min) { setErr(`${opts.min.toLocaleString()} 이상이어야 합니다`); return; }
      if (opts.max != null && n > opts.max) { setErr(`${opts.max.toLocaleString()} 이하여야 합니다`); return; }
      close(n);
    } else {
      const v = val.trim();
      if ((opts.required || opts.kind === "date") && !v) { setErr("필수 입력입니다"); return; }
      const custom = opts.validate?.(v);
      if (custom) { setErr(custom); return; }
      close(v);
    }
  };

  const inputType = opts?.kind === "number" ? "number" : opts?.kind === "date" ? "date" : "text";

  const dialog = opts ? (
    <Modal title={opts.title} onClose={() => close(null)}>
      {opts.label && <label htmlFor={inputId} className="text-sm text-slate-600">{opts.label}</label>}
      <input id={inputId} autoFocus type={inputType}
        value={val} placeholder={opts.placeholder}
        onChange={(e) => { setVal(e.target.value); setErr(""); }}
        onKeyDown={(e) => { if (e.key === "Enter") submit(); }}
        className="mt-1 w-full rounded-md border border-slate-300 px-3 py-2 text-sm" />
      {opts.hint && <p className="mt-1 text-xs text-slate-400">{opts.hint}</p>}
      {err && <p className="mt-1 text-xs text-red-600">{err}</p>}
      <div className="mt-3 flex justify-end gap-2">
        <button onClick={() => close(null)}
          className="rounded-md px-3 py-1.5 text-sm text-slate-500 hover:bg-slate-100">취소</button>
        <Button onClick={submit}>{opts.confirmLabel ?? "확인"}</Button>
      </div>
    </Modal>
  ) : null;

  return { ask, dialog };
}

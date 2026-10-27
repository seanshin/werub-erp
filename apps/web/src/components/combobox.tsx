"use client";
/** 공용 검색 콤보박스(X2-5) — 원시 ID/코드 텍스트 입력을 검색·선택으로 대체.
 * 옵션 전량 로드 후 클라 필터(유한 목록: 계정·품목·직원). EmployeePicker 패턴 일반화. */
import { useState } from "react";

export function Combobox<T>({ value, onPick, options, getKey, getLabel, getSearch,
                             placeholder = "검색", width = "w-64", disabled = false }: {
  value: T | null;
  onPick: (v: T | null) => void;
  options: T[];
  getKey: (o: T) => string | number;
  getLabel: (o: T) => string;      // 선택/옵션 표시 텍스트
  getSearch: (o: T) => string;     // 필터 대상 텍스트
  placeholder?: string;
  width?: string;
  disabled?: boolean;
}) {
  const [q, setQ] = useState("");
  const [open, setOpen] = useState(false);
  const sel = value != null ? options.find((o) => getKey(o) === getKey(value)) : undefined;
  const kw = q.trim();
  const filtered = (kw ? options.filter((o) => getSearch(o).includes(kw)) : options).slice(0, 60);
  return (
    <div className={`relative ${width}`}>
      <input disabled={disabled}
        value={open ? q : (sel ? getLabel(sel) : q)}
        onChange={(e) => { setQ(e.target.value); setOpen(true); if (value) onPick(null); }}
        onFocus={() => { if (sel) setQ(getLabel(sel)); setOpen(true); }}
        onBlur={() => setTimeout(() => { setOpen(false); if (!value) setQ(""); }, 150)}
        placeholder={placeholder}
        className="w-full rounded-md border border-slate-300 px-3 py-1.5 text-sm focus:border-blue-500 focus:outline-none disabled:bg-slate-100" />
      {open && !disabled && (
        <div className="absolute z-20 mt-1 max-h-60 w-full overflow-auto rounded-md border border-slate-200 bg-white shadow-lg">
          {filtered.length === 0
            ? <p className="px-3 py-2 text-xs text-slate-400">일치 없음</p>
            : filtered.map((o) => (
              <button key={getKey(o)} type="button"
                onMouseDown={() => { onPick(o); setQ(""); setOpen(false); }}
                className="block w-full px-3 py-1.5 text-left text-sm hover:bg-blue-50">
                {getLabel(o)}
              </button>))}
        </div>
      )}
    </div>
  );
}

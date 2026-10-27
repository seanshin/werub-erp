"use client";
/** 직원 콤보박스(공용) — 이름·사번·부서로 검색·선택.
 *
 * - EmployeePicker: ITAM 전체 목록(/itam/employees) 클라이언트 필터. 자산·SW 좌석 배정용.
 * - HrEmployeePicker: 인사 검색 EP(/hr/employees?q=) 서버 검색. 인사 화면의 "직원 ID 직접입력"
 *   대체용(기획 §2 피커) — hr 스코프로 호출하므로 ITAM 권한 없는 인사 담당자도 사용 가능.
 */
import { useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { api } from "@/lib/api";

export interface EmpOpt { id: number; employee_no: string; name: string; department: string | null }

export function EmployeePicker({ value, onPick }: {
  value: number | null; onPick: (id: number | null) => void }) {
  const [q, setQ] = useState("");
  const [open, setOpen] = useState(false);
  const emps = useQuery({ queryKey: ["itam-employees"],
    queryFn: () => api<{ items: EmpOpt[] }>("/itam/employees") });
  const list = emps.data?.items ?? [];
  const sel = list.find((e) => e.id === value);
  const kw = q.trim();
  const filtered = (kw
    ? list.filter((e) => e.name.includes(kw) || e.employee_no.includes(kw) || (e.department ?? "").includes(kw))
    : list).slice(0, 60);
  return (
    <div className="relative w-64">
      <input
        value={open ? q : (sel ? `${sel.name} (${sel.employee_no})` : q)}
        onChange={(e) => { setQ(e.target.value); setOpen(true); if (value) onPick(null); }}
        onFocus={() => { if (sel) setQ(sel.name); setOpen(true); }}   // 기존 선택을 검색어로 유지(표시 증발 방지)
        onBlur={() => setTimeout(() => { setOpen(false); if (!value) setQ(""); }, 150)}
        placeholder="이름·사번·부서 검색"
        className="w-full rounded-md border border-slate-300 px-3 py-1.5 text-sm focus:border-blue-500 focus:outline-none" />
      {open && (
        <div className="absolute z-20 mt-1 max-h-60 w-full overflow-auto rounded-md border border-slate-200 bg-white shadow-lg">
          {filtered.length === 0
            ? <p className="px-3 py-2 text-xs text-slate-400">일치 없음</p>
            : filtered.map((e) => (
              <button key={e.id} type="button"
                onMouseDown={() => { onPick(e.id); setQ(""); setOpen(false); }}
                className="block w-full px-3 py-1.5 text-left text-sm hover:bg-blue-50">
                <span className="font-medium">{e.name}</span>
                <span className="text-xs text-slate-400"> · {e.employee_no}{e.department ? ` · ${e.department}` : ""}</span>
              </button>))}
        </div>
      )}
    </div>
  );
}

/** 인사 화면용 직원 선택 — 값은 문자열 id(폼 상태 형태 유지). 빈 문자열 = 미선택. */
export function HrEmployeePicker({ value, onPick, placeholder = "이름·사번 검색", width = "w-56" }: {
  value: string; onPick: (id: string) => void; placeholder?: string; width?: string }) {
  const [q, setQ] = useState("");
  const [open, setOpen] = useState(false);
  const [label, setLabel] = useState("");
  const kw = q.trim();
  const emps = useQuery({
    queryKey: ["hr-emp-pick", kw], enabled: open && kw.length >= 1,
    queryFn: () => api<{ items: EmpOpt[] }>(
      `/hr/employees?q=${encodeURIComponent(kw)}&size=10`) });
  const list = emps.data?.items ?? [];
  return (
    <div className={`relative ${width}`}>
      <input
        value={open ? q : (value ? label : "")}
        onChange={(e) => { setQ(e.target.value); setOpen(true); if (value) { onPick(""); setLabel(""); } }}
        onFocus={() => { setQ(value ? label : ""); setOpen(true); }}
        onBlur={() => setTimeout(() => setOpen(false), 150)}
        placeholder={placeholder}
        className="w-full rounded-md border border-slate-300 px-3 py-1.5 text-sm focus:border-blue-500 focus:outline-none" />
      {open && kw.length >= 1 && (
        <div className="absolute z-20 mt-1 max-h-60 w-full overflow-auto rounded-md border border-slate-200 bg-white shadow-lg">
          {list.length === 0
            ? <p className="px-3 py-2 text-xs text-slate-400">{emps.isFetching ? "검색 중…" : "일치 없음"}</p>
            : list.map((e) => (
              <button key={e.id} type="button"
                onMouseDown={() => { onPick(String(e.id)); setLabel(`${e.name} (${e.employee_no})`);
                  setQ(""); setOpen(false); }}
                className="block w-full px-3 py-1.5 text-left text-sm hover:bg-blue-50">
                <span className="font-medium">{e.name}</span>
                <span className="text-xs text-slate-400"> · {e.employee_no}{e.department ? ` · ${e.department}` : ""}</span>
              </button>))}
        </div>
      )}
    </div>
  );
}

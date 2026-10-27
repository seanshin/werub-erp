"use client";
/** 듀티 편성 그리드 (FR-06) + 노동규제 위반 경고 패널 (FR-32 — 저장 비차단). */
import { memo, useCallback, useMemo, useState } from "react";
import { useMutation, useQuery } from "@tanstack/react-query";
import { ApiError, api, auth } from "@/lib/api";
import { Badge, Button, Card } from "@/components/ui";

interface Employee { id: number; name: string; job_group: string }
interface Violation { rule: string; employee_id: number; detail: string }

const SHIFTS = ["", "D", "E", "N", "OFF"] as const;
const SHIFT_COLOR: Record<string, string> = {
  D: "bg-sky-50", E: "bg-amber-50", N: "bg-indig100 bg-indigo-50", OFF: "bg-slate-100",
};

export default function DutiesPage() {
  const now = new Date();
  const [yyyymm, setYyyymm] = useState(
    `${now.getFullYear()}-${String(now.getMonth() + 1).padStart(2, "0")}`);
  // 로컬 편집분만 보관하고, 렌더 시 서버 저장분 위에 겹친다(effect로 setState 하지 않기 위해).
  const [edits, setEdits] = useState<Record<string, string>>({}); // "empId:day" → shift
  const [violations, setViolations] = useState<Violation[]>([]);
  const [message, setMessage] = useState<string | null>(null);
  const deptId = auth.claims()?.dept ?? null;

  const employees = useQuery({
    queryKey: ["employees-duty"],
    queryFn: () => api<{ items: Employee[] }>("/hr/employees?size=50"),
  });

  const days = useMemo(() => {
    const [y, m] = yyyymm.split("-").map(Number);
    return Array.from({ length: new Date(y, m, 0).getDate() }, (_, i) => i + 1);
  }, [yyyymm]);

  // 저장된 근무표 복원 — 조회 EP가 없어 화면이 늘 빈 그리드로 시작했고, PUT이
  // delete-then-insert라 그대로 저장하면 기존 배정이 전량 삭제됐다(데이터 손실).
  const saved = useQuery({
    queryKey: ["duties", yyyymm, deptId],
    enabled: deptId != null,
    queryFn: () => api<{ schedule_id: number | null; status: string | null;
      cells: { employee_id: number; duty_on: string; shift: string }[] }>(
      `/hr/duties/${yyyymm}?department_id=${deptId}`),
  });
  const serverCells = useMemo(() => {
    const next: Record<string, string> = {};
    for (const c of saved.data?.cells ?? []) {
      next[`${c.employee_id}:${Number(c.duty_on.slice(8, 10))}`] = c.shift;
    }
    return next;
  }, [saved.data]);
  const cells = useMemo(() => ({ ...serverCells, ...edits }), [serverCells, edits]);

  const save = useMutation({
    mutationFn: () => {
      const body = {
        department_id: deptId ?? 0,
        cells: Object.entries(cells)
          .filter(([, s]) => s)
          .map(([key, shift]) => {
            const [emp, day] = key.split(":");
            return { employee_id: Number(emp), shift,
                     duty_on: `${yyyymm}-${String(day).padStart(2, "0")}` };
          }),
      };
      return api<{ schedule_id: number; violations: Violation[] }>(
        `/hr/duties/${yyyymm}`, { method: "PUT", body: JSON.stringify(body) });
    },
    onSuccess: (data) => {
      setViolations(data.violations);
      setEdits({});             // 서버 확정 상태로 재동기화(로컬 편집 해제 후 재조회)
      saved.refetch();
      setMessage(data.violations.length
        ? `저장 완료 — 위반 경고 ${data.violations.length}건 (저장은 유지됨)`
        : "저장 완료 — 위반 없음");
    },
    onError: (e) => setMessage(e instanceof ApiError ? e.message : "저장 실패"),
  });

  // 경고-1 — 셀 단위 메모이즈: 930셀 전체 리렌더 방지
  const updateCell = useCallback((key: string, value: string) =>
    setEdits((prev) => ({ ...prev, [key]: value })), []);

  const empName = (id: number) =>
    employees.data?.items.find((e) => e.id === id)?.name ?? `#${id}`;

  return (
    <div className="space-y-4">
      <div className="flex items-center gap-3">
        <h1 className="text-lg font-bold">인사 — 듀티 편성</h1>
        <input type="month" value={yyyymm} onChange={(e) => { setYyyymm(e.target.value); setEdits({}); }}
          className="rounded-md border border-slate-300 px-2 py-1 text-sm" />
        <Button onClick={() => save.mutate()}
          disabled={save.isPending || saved.isFetching}>저장 + 룰 검사</Button>
        {saved.isFetching && <span className="text-xs text-slate-400">기존 근무표 불러오는 중…</span>}
        {saved.data?.status && <Badge tone={saved.data.status === "PUBLISHED" ? "green" : "gray"}>
          {saved.data.status === "PUBLISHED" ? "확정" : "작성중"}</Badge>}
      </div>
      {message && <p className="text-sm text-blue-600">{message}</p>}

      {violations.length > 0 && (
        <Card title="⚠️ 노동규제 위반 경고 (G3 초안 룰 — 근로기준법 주52h·전공의법 80h·연속근무)">
          <ul className="space-y-1 text-sm">
            {violations.map((v, i) => (
              <li key={i}>
                <Badge tone="red">{v.rule}</Badge>{" "}
                <span className="font-medium">{empName(v.employee_id)}</span>{" "}
                <span className="text-slate-500">{v.detail}</span>
              </li>
            ))}
          </ul>
        </Card>
      )}

      <Card>
        <div className="overflow-x-auto">
          <table className="text-xs">
            <thead>
              <tr>
                <th className="sticky left-0 bg-white px-2 py-1 text-left">직원</th>
                {days.map((d) => <th key={d} className="px-1 py-1 font-normal text-slate-400">{d}</th>)}
              </tr>
            </thead>
            <tbody>
              {(employees.data?.items ?? []).map((emp) => (
                <tr key={emp.id} className="border-t border-slate-100">
                  <td className="sticky left-0 whitespace-nowrap bg-white px-2 py-0.5">
                    {emp.name}
                    {emp.job_group === "RESIDENT" && <Badge tone="amber">전공의</Badge>}
                  </td>
                  {days.map((d) => {
                    const key = `${emp.id}:${d}`;
                    return <GridCell key={d} cellKey={key} value={cells[key] ?? ""}
                                     onChange={updateCell} />;
                  })}
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </Card>
    </div>
  );
}

const GridCell = memo(function GridCell({ cellKey, value, onChange }: {
  cellKey: string; value: string; onChange: (key: string, value: string) => void;
}) {
  return (
    <td className={`p-0 ${SHIFT_COLOR[value] ?? ""}`}>
      <select value={value} onChange={(e) => onChange(cellKey, e.target.value)}
        className="w-11 border-0 bg-transparent py-0.5 text-center text-xs">
        {SHIFTS.map((s) => <option key={s} value={s}>{s || "·"}</option>)}
      </select>
    </td>
  );
});

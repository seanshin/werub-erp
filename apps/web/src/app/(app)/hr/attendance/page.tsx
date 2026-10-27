"use client";
/** 근태 집계 조회 (FR-29) — 그룹웨어 수신 기반 일배치 산출물. KPI·시각화. */
import { useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { api } from "@/lib/api";
import { Card, Table } from "@/components/ui";

interface Row { employee_id: number; employee_name: string | null; employee_no: string | null; work_days: number; late_count: number;
                early_leave_count: number; night_shift_count: number; total_work_minutes: number }

function Kpi({ label, value, tone }: { label: string; value: string; tone?: string }) {
  return (
    <div className="rounded-lg border border-slate-200 bg-white p-3">
      <p className="text-xs text-slate-500">{label}</p>
      <p className={`text-xl font-bold ${tone ?? "text-slate-800"}`}>{value}</p>
    </div>
  );
}

export default function AttendancePage() {
  const now = new Date();
  const [yyyymm, setYyyymm] = useState(
    `${now.getFullYear()}-${String(now.getMonth() + 1).padStart(2, "0")}`);
  const { data, isError } = useQuery({
    queryKey: ["attendance", yyyymm],
    queryFn: () => api<{ items: Row[] }>(`/hr/attendance?yyyymm=${yyyymm}`),
  });

  const items = data?.items ?? [];
  // 이름·사번(문자열)이 추가돼 keyof Row가 더는 전부 숫자가 아니다 — 숫자 키만 허용
  type NumKey = { [K in keyof Row]: Row[K] extends number ? K : never }[keyof Row];
  const sum = (k: NumKey) => items.reduce((a, r) => a + (r[k] || 0), 0);
  const maxDays = Math.max(1, ...items.map((r) => r.work_days));
  const h = (n: number) => (n / 60).toFixed(1);

  return (
    <div className="space-y-4">
      <div className="flex items-center gap-3">
        <h1 className="text-lg font-bold">인사 — 근태 집계</h1>
        <input type="month" value={yyyymm} onChange={(e) => setYyyymm(e.target.value)}
          className="rounded-md border border-slate-300 px-2 py-1 text-sm" />
      </div>
      {isError && <p className="text-sm text-red-600">근태 데이터를 불러오지 못했습니다</p>}

      <div className="grid gap-2 sm:grid-cols-5">
        <Kpi label="대상 인원" value={`${items.length}명`} />
        <Kpi label="총 근무일" value={`${sum("work_days")}일`} />
        <Kpi label="지각" value={`${sum("late_count")}회`} tone="text-amber-600" />
        <Kpi label="조퇴" value={`${sum("early_leave_count")}회`} tone="text-amber-600" />
        <Kpi label="야간" value={`${sum("night_shift_count")}회`} tone="text-blue-600" />
      </div>

      <Card title={`직원별 근태 (${yyyymm})`}>
        <Table headers={["직원", "근무일", "지각", "조퇴", "야간", "총 근무(h)"]}
          rows={items.map((r) => [
            // 내부 PK만 찍혀 지각·조퇴 대상자를 특정할 수 없었다
            r.employee_name ? `${r.employee_name}(${r.employee_no})` : `#${r.employee_id}`,
            <div key="d" className="flex items-center gap-2">
              <span className="inline-block h-2 rounded bg-blue-400"
                style={{ width: `${(r.work_days / maxDays) * 60}px` }} />
              {r.work_days}
            </div>,
            r.late_count || "—", r.early_leave_count || "—", r.night_shift_count || "—",
            h(r.total_work_minutes),
          ])} empty="집계 데이터 없음 — 그룹웨어 연계(M0a) 활성화 후 일배치(04:00) 산출" />
      </Card>
      <p className="text-xs text-slate-400">출퇴근 원천은 그룹웨어 — 본 시스템은 수신·집계만 (Plan v0.2)</p>
    </div>
  );
}

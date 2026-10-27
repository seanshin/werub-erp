"use client";
/** 인사 — 휴직/복직·퇴사 처리 (P1-①): 발령 연동, 급여 일괄생성 제외·납부예외 식별. */
import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { ApiError, api } from "@/lib/api";
import { Card, Table } from "@/components/ui";
import { useConfirm } from "@/components/confirm";
import { useToast } from "@/components/toast";
import { useInputDialog } from "@/components/input-dialog";
import type { EmployeeRef } from "@/types";
import { won } from "@/lib/format";
import { HrEmployeePicker } from "@/components/employee-picker";

interface Absence { id: number; employee_id: number; employee_no: string; name: string;
  kind: string; start_on: string; expected_return_on: string | null;
  return_on: string | null; insurance_exempt: boolean; note: string | null }
interface Offboarding { employee_id: number; employee_no: string; name: string;
  resigned_on: string; avg_monthly_wage: number; leave_remaining_days: number;
  leave_allowance: number; severance: { 근속일수: number; 지급대상: boolean; 퇴직금: number };
  provision_cumulative: number; already_settled: boolean; total_payout: number;
  voucher_id?: number | null }

const KIND: Record<string, string> = {
  MATERNITY: "출산휴가", PARENTAL: "육아휴직", SICK: "병가",
  INDUSTRIAL: "산재", PERSONAL: "일반휴직",
};

export default function AbsencesPage() {
  const qc = useQueryClient();
  const toast = useToast();
  const confirmDialog = useConfirm();
  const { ask, dialog } = useInputDialog();
  const [q, setQ] = useState("");
  const [form, setForm] = useState({ employee_id: "", kind: "PARENTAL", start_on: "",
    expected_return_on: "", insurance_exempt: true });
  const [resign, setResign] = useState({ employee_id: "", resigned_on: "" });

  const absences = useQuery({ queryKey: ["absences"], queryFn: () =>
    api<{ count: number; items: Absence[] }>("/hr/absences") });
  const emps = useQuery({ queryKey: ["emps", q], enabled: q.length >= 1, queryFn: () =>
    api<{ items: EmployeeRef[] }>(`/hr/employees?q=${encodeURIComponent(q)}&size=10`) });

  const ok = (m: string) => { toast.ok(m); qc.invalidateQueries({ queryKey: ["absences"] }); };
  const fail = (e: unknown) => toast.err(e instanceof ApiError ? e.error.message : "처리 실패");

  const start = useMutation({
    mutationFn: () => api(`/hr/absences`, { method: "POST", body: JSON.stringify({
      employee_id: Number(form.employee_id), kind: form.kind, start_on: form.start_on,
      expected_return_on: form.expected_return_on || null,
      insurance_exempt: form.insurance_exempt }) }),
    onSuccess: () => ok("휴직 등록 완료 (발령 LEAVE 기록)"), onError: fail });

  const ret = useMutation({
    mutationFn: (p: { id: number; on: string }) =>
      api(`/hr/absences/${p.id}/return?return_on=${p.on}`, { method: "POST" }),
    onSuccess: () => ok("복직 처리 완료 (발령 REINSTATE 기록)"), onError: fail });

  const [ob, setOb] = useState<Offboarding | null>(null);
  const [obId, setObId] = useState("");
  const loadOb = async () => {
    try { setOb(await api<Offboarding>(`/hr/employees/${obId}/offboarding`)); }
    catch (e) { setOb(null); fail(e); }
  };
  const settle = useMutation({
    mutationFn: () => api<Offboarding>(`/hr/employees/${obId}/offboarding`, { method: "POST" }),
    onSuccess: (d) => { setOb(d);
      ok(`퇴직 정산 완료 — 총 지급액 ${won(d.total_payout)}${d.voucher_id ? ` (전표 #${d.voucher_id})` : ""}`); },
    onError: fail });

  const doResign = useMutation({
    mutationFn: () => api(`/hr/employees/${resign.employee_id}/resign`, {
      method: "POST", body: JSON.stringify({ resigned_on: resign.resigned_on }) }),
    onSuccess: () => ok("퇴사 처리 완료 (발령 RESIGN 기록)"), onError: fail });

  const input = "rounded-md border border-slate-300 px-3 py-1.5 text-sm";
  const btn = "rounded-md bg-blue-600 px-3 py-1.5 text-sm font-medium text-white hover:bg-blue-700 disabled:opacity-50";

  return (
    <div className="space-y-4">
      {dialog}
      <h1 className="text-lg font-bold">인사 — 휴직·복직·퇴사</h1>

      <Card title="직원 검색" action={<span className="text-[11px] text-slate-400">각 폼의 검색창에서 직접 선택할 수도 있습니다</span>}>
        <input value={q} onChange={(e) => setQ(e.target.value)} placeholder="이름 또는 사번"
          className={`${input} w-64`} />
        {(emps.data?.items ?? []).length > 0 && (
          <p className="mt-2 text-sm text-slate-600">
            {emps.data!.items.map((e) => `#${e.id} ${e.name}(${e.employee_no})`).join(" · ")}
          </p>
        )}
      </Card>

      <Card title="휴직 등록">
        <div className="flex flex-wrap items-end gap-2">
          <HrEmployeePicker value={form.employee_id}
            onPick={(id) => setForm({ ...form, employee_id: id })} />
          <select value={form.kind} onChange={(e) => setForm({ ...form, kind: e.target.value })}
            className={input}>
            {Object.entries(KIND).map(([k, v]) => <option key={k} value={k}>{v}</option>)}
          </select>
          <input type="date" value={form.start_on}
            onChange={(e) => setForm({ ...form, start_on: e.target.value })} className={input} />
          <input type="date" value={form.expected_return_on} title="복직 예정일(선택)"
            onChange={(e) => setForm({ ...form, expected_return_on: e.target.value })} className={input} />
          <label className="flex items-center gap-1 text-sm text-slate-600">
            <input type="checkbox" checked={form.insurance_exempt}
              onChange={(e) => setForm({ ...form, insurance_exempt: e.target.checked })} />
            4대보험 납부예외
          </label>
          <button disabled={!form.employee_id || !form.start_on || start.isPending}
            onClick={() => start.mutate()} className={btn}>휴직 등록</button>
        </div>
      </Card>

      <Card title="휴직 현황" action={<span className="text-[11px] text-slate-400">복직일 미정 = 휴직 중 — 급여 일괄생성 자동 제외</span>}>
        <Table headers={["사번", "이름", "구분", "휴직일", "복직예정", "복직일", "납부예외", ""]}
          rows={(absences.data?.items ?? []).map((a) => [
            a.employee_no, a.name, KIND[a.kind] ?? a.kind, a.start_on,
            a.expected_return_on ?? "-",
            a.return_on ?? <span key="s" className="text-amber-600">휴직 중</span>,
            a.insurance_exempt ? "○" : "-",
            a.return_on ? null :
              <button key="r" className="rounded-md bg-emerald-600 px-2.5 py-1 text-xs font-medium text-white hover:bg-emerald-700"
                onClick={async () => { const on = await ask({ title: "복직 처리",
                  label: "복직일", kind: "date", confirmLabel: "복직" });
                  if (on) ret.mutate({ id: a.id, on: String(on) }); }}>복직</button>,
          ])} />
      </Card>

      <Card title="퇴사 처리" action={<span className="text-[11px] text-rose-400">HIS 관리 직원(의료진·정규직)은 HIS에서 처리</span>}>
        <div className="flex items-end gap-2">
          <HrEmployeePicker value={resign.employee_id}
            onPick={(id) => setResign({ ...resign, employee_id: id })} />
          <input type="date" value={resign.resigned_on}
            onChange={(e) => setResign({ ...resign, resigned_on: e.target.value })} className={input} />
          <button disabled={!resign.employee_id || !resign.resigned_on || doResign.isPending}
            onClick={async () => { if (await confirmDialog({
              title: "퇴사 처리", body: "퇴사 처리하시겠습니까? 되돌릴 수 없습니다.",
              danger: true, confirmLabel: "퇴사 처리" })) doResign.mutate(); }}
            className="rounded-md bg-rose-600 px-3 py-1.5 text-sm font-medium text-white hover:bg-rose-700 disabled:opacity-50">
            퇴사 처리</button>
        </div>
      </Card>

      <Card title="퇴직 정산 (오프보딩)" action={
        <span className="text-[11px] text-slate-400">연차 미사용 수당 + 퇴직금 + 충당 상계 전표 — 직원당 1회</span>}>
        <div className="flex items-end gap-2">
          <HrEmployeePicker value={obId} onPick={setObId} />
          <button disabled={!obId} onClick={loadOb} className={btn}>미리보기</button>
          {ob && !ob.already_settled && (
            <button disabled={settle.isPending}
              onClick={async () => { if (await confirmDialog({
                title: "퇴직 정산 확정", danger: true, confirmLabel: "정산 확정",
                body: `총 ${won(ob.total_payout)} 정산을 확정하시겠습니까?` })) settle.mutate(); }}
              className="rounded-md bg-emerald-600 px-3 py-1.5 text-sm font-medium text-white hover:bg-emerald-700 disabled:opacity-50">
              정산 확정</button>
          )}
        </div>
        {ob && (
          <div className="mt-3 grid max-w-2xl grid-cols-2 gap-x-8 gap-y-1 text-sm text-slate-700 tabular-nums">
            <span>대상: {ob.name}({ob.employee_no}) · 퇴사일 {ob.resigned_on}
              {ob.already_settled && <em className="ml-2 text-emerald-600">정산 완료</em>}</span>
            <span>평균임금: {won(ob.avg_monthly_wage)}</span>
            <span>연차 미사용 {ob.leave_remaining_days}일 → 수당 {won(ob.leave_allowance)}</span>
            <span>퇴직금: {won(ob.severance.퇴직금)} (근속 {ob.severance.근속일수}일{ob.severance.지급대상 ? "" : " · 1년 미만 미지급"})</span>
            <span>충당부채 누적: {won(ob.provision_cumulative)} (상계)</span>
            <span className="font-semibold">총 지급액: {won(ob.total_payout)}</span>
          </div>
        )}
      </Card>
    </div>
  );
}

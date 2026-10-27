"use client";
/** 인사 — 직급·호봉 체계 + 징계·상벌 (Tier3·P3-8). 연도별 호봉표 마스터 + 상벌 인사기록.
 *  임금·인사 민감 → 관리자(hr.payroll). 지표는 각 화면, 본 화면은 인사 기준·기록. */
import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api, ApiError } from "@/lib/api";
import { Badge, Button, Card, Input, Modal, Table } from "@/components/ui";
import { QueryErrorBanner } from "@/components/query-guard";
import { won } from "@/lib/format";
import { todayLocal } from "@/lib/dates";
import { useToast } from "@/components/toast";
import type { Employee } from "@/types";

const THIS_YEAR = new Date().getFullYear();
const ATYPE: Record<string, { kr: string; tone: "red" | "green" }> = {
  DISCIPLINE: { kr: "징계", tone: "red" }, REWARD: { kr: "포상", tone: "green" } };

interface PayGrade { id: number; grade: string; step: number; base_pay: number;
  effective_year: number; note: string | null }
interface Disc { id: number; employee_id: number; action_type: string; category: string;
  reason: string; decided_on: string; effective_from: string | null;
  effective_to: string | null; note: string | null }

export default function PersonnelRecordsPage() {
  const qc = useQueryClient();
  const toast = useToast();
  const fail = (e: unknown) => toast.err(e instanceof ApiError ? e.error.message : "처리 실패");
  const [year, setYear] = useState(THIS_YEAR);
  const [addGrade, setAddGrade] = useState(false);
  const [q, setQ] = useState("");
  const [emp, setEmp] = useState<Employee | null>(null);
  const [recording, setRecording] = useState(false);

  const grades = useQuery({ queryKey: ["paygrades", year], queryFn: () =>
    api<{ items: PayGrade[] }>(`/hr/pay-grades?effective_year=${year}`) });
  const search = useQuery({ enabled: q.length > 0, queryKey: ["pr-emp", q], queryFn: () =>
    api<{ items: Employee[] }>(`/hr/employees?size=10&q=${encodeURIComponent(q)}`) });
  const discList = useQuery({ enabled: emp !== null, queryKey: ["disc", emp?.id], queryFn: () =>
    api<{ items: Disc[] }>(`/hr/disciplinary-actions?employee_id=${emp!.id}`) });

  return (
    <div className="space-y-4">
      <h1 className="text-lg font-bold">인사 — 직급·호봉 / 징계·상벌</h1>
      <p className="text-xs text-slate-500">
        연도별 호봉표(직급×호봉→기본급 마스터·급여 산정 참조)와 징계·포상 인사기록입니다.
        임금·인사 민감 정보로 관리자 전용입니다.</p>

      <QueryErrorBanner queries={[grades]} />

      <Card title={`호봉표 (${year})`} action={
        <span className="flex items-center gap-2">
          <input type="number" value={year} onChange={(e) => setYear(Number(e.target.value))}
            className="w-20 rounded border border-slate-300 px-1 py-0.5 text-xs" />
          <Button size="sm" onClick={() => setAddGrade(true)}>호봉 추가/수정</Button>
        </span>}>
        <Table headers={["직급", "호봉", "월 기본급", "비고"]}
          rows={(grades.data?.items ?? []).map((g) => [
            g.grade, `${g.step}호봉`, won(g.base_pay), g.note ?? "—"])}
          empty={grades.isFetching ? "불러오는 중…" : "등록된 호봉표 없음"} />
      </Card>

      <Card title="징계·상벌 인사기록">
        <input value={q} onChange={(e) => setQ(e.target.value)} placeholder="직원 이름·사번 검색"
          className="w-64 rounded-md border border-slate-300 px-3 py-1.5 text-sm" />
        {search.data && q && (
          <div className="mt-2 flex flex-wrap gap-1">
            {search.data.items.map((e) => (
              <button key={e.id} onClick={() => { setEmp(e); setQ(""); }}
                className="rounded-md border border-slate-200 px-2 py-1 text-xs hover:bg-slate-50">
                {e.name} ({e.employee_no})</button>))}
          </div>
        )}
        {emp && (
          <div className="mt-3">
            <div className="mb-2 flex items-center justify-between">
              <p className="text-sm">대상: <b>{emp.name}</b> ({emp.employee_no})</p>
              <Button size="sm" onClick={() => setRecording(true)}>상벌 등록</Button>
            </div>
            <Table headers={["의결일", "구분", "종류", "사유", "효력기간"]}
              rows={(discList.data?.items ?? []).map((d) => [
                d.decided_on,
                <Badge key="t" tone={(ATYPE[d.action_type] ?? { tone: "green" as const }).tone}>
                  {(ATYPE[d.action_type] ?? { kr: d.action_type }).kr}</Badge>,
                d.category, <span key="r" className="text-xs">{d.reason}</span>,
                d.effective_from ? `${d.effective_from} ~ ${d.effective_to ?? ""}` : "—",
              ])} empty={discList.isFetching ? "불러오는 중…" : "상벌 기록 없음"} />
          </div>
        )}
      </Card>

      {addGrade && <GradeModal year={year} onDone={() => { setAddGrade(false);
        qc.invalidateQueries({ queryKey: ["paygrades", year] }); }} onError={fail}
        onClose={() => setAddGrade(false)} />}
      {recording && emp && <DiscModal employee={emp} onDone={() => { setRecording(false);
        qc.invalidateQueries({ queryKey: ["disc", emp.id] }); }} onError={fail}
        onClose={() => setRecording(false)} />}
    </div>
  );
}

function GradeModal({ year, onDone, onError, onClose }: {
  year: number; onDone: () => void; onError: (e: unknown) => void; onClose: () => void;
}) {
  const toast = useToast();
  const [f, setF] = useState({ grade: "", step: "1", base_pay: "", note: "" });
  const m = useMutation({
    mutationFn: () => api("/hr/pay-grades", { method: "POST", body: JSON.stringify({
      grade: f.grade, step: Number(f.step), base_pay: Number(f.base_pay),
      effective_year: year, note: f.note || null }) }),
    onSuccess: () => { toast.ok("호봉표 저장"); onDone(); }, onError });
  return (
    <Modal title={`호봉표 항목 (${year})`} dirty onClose={onClose}>
      <div className="space-y-2">
        <Input label="직급" value={f.grade} onChange={(e) => setF({ ...f, grade: e.target.value })} />
        <Input label="호봉" value={f.step} inputMode="numeric"
          onChange={(e) => setF({ ...f, step: e.target.value.replace(/[^0-9]/g, "") })} />
        <Input label="월 기본급" value={f.base_pay} inputMode="numeric"
          onChange={(e) => setF({ ...f, base_pay: e.target.value.replace(/[^0-9]/g, "") })} />
        <Input label="비고(선택)" value={f.note} onChange={(e) => setF({ ...f, note: e.target.value })} />
        <Button disabled={m.isPending || !f.grade.trim() || !(Number(f.step) > 0) || !(Number(f.base_pay) > 0)}
          onClick={() => m.mutate()}>저장</Button>
      </div>
    </Modal>
  );
}

function DiscModal({ employee, onDone, onError, onClose }: {
  employee: Employee; onDone: () => void; onError: (e: unknown) => void; onClose: () => void;
}) {
  const toast = useToast();
  const [f, setF] = useState({ action_type: "DISCIPLINE", category: "", reason: "",
    decided_on: todayLocal(), effective_from: "", effective_to: "", note: "" });
  const m = useMutation({
    mutationFn: () => api("/hr/disciplinary-actions", { method: "POST", body: JSON.stringify({
      employee_id: employee.id, action_type: f.action_type, category: f.category,
      reason: f.reason, decided_on: f.decided_on, effective_from: f.effective_from || null,
      effective_to: f.effective_to || null, note: f.note || null }) }),
    onSuccess: () => { toast.ok("상벌 등록"); onDone(); }, onError });
  return (
    <Modal title={`징계·상벌 등록 — ${employee.name}`} dirty onClose={onClose}>
      <div className="space-y-2">
        <label className="block text-sm text-slate-600">구분
          <select value={f.action_type} onChange={(e) => setF({ ...f, action_type: e.target.value })}
            className="w-full rounded-md border border-slate-300 px-2 py-1.5 text-sm">
            <option value="DISCIPLINE">징계</option><option value="REWARD">포상</option>
          </select>
        </label>
        <Input label={f.action_type === "DISCIPLINE" ? "종류(견책·감봉·정직·해임 등)" : "종류(표창·포상 등)"}
          value={f.category} onChange={(e) => setF({ ...f, category: e.target.value })} />
        <Input label="사유" value={f.reason} onChange={(e) => setF({ ...f, reason: e.target.value })} />
        <Input label="의결일" type="date" value={f.decided_on}
          onChange={(e) => setF({ ...f, decided_on: e.target.value })} />
        <div className="grid grid-cols-2 gap-2">
          <Input label="효력 시작(선택)" type="date" value={f.effective_from}
            onChange={(e) => setF({ ...f, effective_from: e.target.value })} />
          <Input label="효력 종료(선택)" type="date" value={f.effective_to}
            onChange={(e) => setF({ ...f, effective_to: e.target.value })} />
        </div>
        <Button disabled={m.isPending || !f.category.trim() || !f.reason.trim()}
          onClick={() => m.mutate()}>등록</Button>
      </div>
    </Modal>
  );
}

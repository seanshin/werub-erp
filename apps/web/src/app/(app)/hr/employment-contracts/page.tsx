"use client";
/** 인사 — 일반직 근로계약 (근기법 §17·P3-5). 비의료진 근로조건 명시(임금·소정근로시간·근무일·
 *  취업장소·업무)·이력. DRAFT→발효(ACTIVE·기존 승계)→종료/갱신. 의료진은 별도(의료계약). write=ADMIN. */
import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api, ApiError } from "@/lib/api";
import { Badge, Button, Card, Input, Modal, Table } from "@/components/ui";
import { QueryErrorBanner } from "@/components/query-guard";
import { won } from "@/lib/format";
import { todayLocal } from "@/lib/dates";
import { useConfirm } from "@/components/confirm";
import { useInputDialog } from "@/components/input-dialog";
import { useToast } from "@/components/toast";
import type { Employee } from "@/types";

const TYPE: Record<string, string> = { REGULAR: "정규직", FIXED_TERM: "계약직", PART_TIME: "단시간" };
const ST: Record<string, { kr: string; tone: "gray" | "green" | "red" }> = {
  DRAFT: { kr: "작성중", tone: "gray" }, ACTIVE: { kr: "재직(유효)", tone: "green" },
  ENDED: { kr: "종료", tone: "red" } };

interface Contract {
  id: number; employee_id: number; contract_type: string; valid_from: string;
  valid_to: string | null; status: string; base_pay: number; weekly_hours: number | null;
  work_schedule: string | null; work_location: string | null; job_duties: string | null;
  note: string | null; ended_on: string | null;
}
interface Expiring extends Contract { d_day: number;
  employee_name: string | null; employee_no: string | null }

export default function EmploymentContractsPage() {
  const qc = useQueryClient();
  const toast = useToast();
  const confirm = useConfirm();
  const { ask, dialog } = useInputDialog();
  const fail = (e: unknown) => toast.err(e instanceof ApiError ? e.error.message : "처리 실패");
  const [q, setQ] = useState("");
  const [emp, setEmp] = useState<Employee | null>(null);
  const [creating, setCreating] = useState(false);

  const search = useQuery({ enabled: q.length > 0, queryKey: ["ec-emp", q], queryFn: () =>
    api<{ items: Employee[] }>(`/hr/employees?size=10&q=${encodeURIComponent(q)}`) });
  const contracts = useQuery({ enabled: emp !== null, queryKey: ["ec-list", emp?.id], queryFn: () =>
    api<{ items: Contract[] }>(`/hr/employment-contracts?employee_id=${emp!.id}`) });
  const expiring = useQuery({ queryKey: ["ec-expiring"], queryFn: () =>
    api<{ items: Expiring[] }>("/hr/employment-contracts/expiring?within_days=60") });
  const refresh = () => { qc.invalidateQueries({ queryKey: ["ec-list", emp?.id] });
    qc.invalidateQueries({ queryKey: ["ec-expiring"] }); };

  const activate = useMutation({
    mutationFn: (id: number) => api(`/hr/employment-contracts/${id}/activate`, { method: "POST" }),
    onSuccess: () => { toast.ok("발효(재직)"); refresh(); }, onError: fail });
  const end = useMutation({
    mutationFn: (v: { id: number; on: string }) => api(`/hr/employment-contracts/${v.id}/end`,
      { method: "POST", body: JSON.stringify({ end_on: v.on }) }),
    onSuccess: () => { toast.ok("계약 종료"); refresh(); }, onError: fail });
  const renew = useMutation({
    mutationFn: (v: { id: number; on: string }) => api(`/hr/employment-contracts/${v.id}/renew`,
      { method: "POST", body: JSON.stringify({ new_valid_to: v.on }) }),
    onSuccess: () => { toast.ok("계약 갱신"); refresh(); }, onError: fail });

  const onActivate = async (c: Contract) => {
    if (await confirm({ title: "근로계약 발효", confirmLabel: "발효",
      body: "이 계약을 발효(ACTIVE)합니다. 기존 유효 계약은 자동 종료됩니다." })) activate.mutate(c.id);
  };
  const onEnd = async (c: Contract) => {
    const on = await ask({ title: "계약 종료", label: "종료일", kind: "date",
      defaultValue: todayLocal(), confirmLabel: "종료" });
    if (on) end.mutate({ id: c.id, on: String(on) });
  };
  const onRenew = async (c: Contract) => {
    const on = await ask({ title: "계약 갱신(만료일 연장)", label: "새 만료일", kind: "date",
      confirmLabel: "갱신" });
    if (on) renew.mutate({ id: c.id, on: String(on) });
  };

  return (
    <div className="space-y-4">
      {dialog}
      <h1 className="text-lg font-bold">인사 — 일반직 근로계약</h1>
      <p className="text-xs text-slate-500">
        비의료진 근로조건 명시(근기법 §17: 임금·소정근로시간·근무일·취업장소·업무)·이력 관리.
        의료진은 별도(의료진 개별 계약). 발효 시 직원당 1건 유지(기존 자동 종료).</p>

      <QueryErrorBanner queries={[expiring]} />

      {(expiring.data?.items?.length ?? 0) > 0 && (
        <Card title={`계약직 만기 임박 (60일 내·${expiring.data!.items.length}건)`}>
          <Table headers={["직원", "유형", "만기", "D-day", "월급여"]}
            rows={expiring.data!.items.map((c) => [
              // 이름 없이는 60일 내 만기자를 판단할 수 없어 카드가 사실상 무용이었다
              c.employee_name ? `${c.employee_name}(${c.employee_no})` : `#${c.employee_id}`,
              TYPE[c.contract_type] ?? c.contract_type, c.valid_to ?? "—",
              <Badge key="d" tone={c.d_day <= 14 ? "red" : "amber"}>D-{c.d_day}</Badge>,
              won(c.base_pay),
            ])} />
        </Card>
      )}

      <Card title="직원 선택">
        <input value={q} onChange={(e) => setQ(e.target.value)} placeholder="이름·사번 검색(일반직)"
          className="w-64 rounded-md border border-slate-300 px-3 py-1.5 text-sm" />
        {search.data && q && (
          <div className="mt-2 flex flex-wrap gap-1">
            {search.data.items.map((e) => (
              <button key={e.id} onClick={() => { setEmp(e); setQ(""); }}
                className="rounded-md border border-slate-200 px-2 py-1 text-xs hover:bg-slate-50">
                {e.name} ({e.employee_no}){e.staff_class === "MEDICAL" ? " · 의료진" : ""}</button>
            ))}
          </div>
        )}
        {emp && <p className="mt-2 text-sm">선택: <b>{emp.name}</b> ({emp.employee_no})
          {emp.staff_class === "MEDICAL" && <span className="ml-1 text-red-600">— 의료진(의료계약 대상)</span>}</p>}
      </Card>

      {emp && (
        <Card title="근로계약 이력" action={emp.staff_class !== "MEDICAL"
          ? <Button size="sm" onClick={() => setCreating(true)}>신규 계약</Button> : undefined}>
          <Table headers={["기간", "유형", "월기본급", "주소정", "근무", "상태", "생애주기"]}
            rows={(contracts.data?.items ?? []).map((c) => [
              `${c.valid_from} ~ ${c.valid_to ?? "무기한"}`,
              TYPE[c.contract_type] ?? c.contract_type, won(c.base_pay),
              c.weekly_hours != null ? `${c.weekly_hours}h` : "—",
              c.work_schedule ?? "—",
              <Badge key="s" tone={(ST[c.status] ?? { tone: "gray" as const }).tone}>
                {(ST[c.status] ?? { kr: c.status }).kr}</Badge>,
              <span key="a" className="flex flex-wrap gap-1">
                {c.status === "DRAFT" && (<>
                  <Button size="sm" onClick={() => onActivate(c)}>발효</Button>
                  <Button size="sm" variant="danger" onClick={() => onEnd(c)}>폐기</Button>
                </>)}
                {c.status === "ACTIVE" && (<>
                  {c.contract_type === "FIXED_TERM" &&
                    <Button size="sm" variant="secondary" onClick={() => onRenew(c)}>갱신</Button>}
                  <Button size="sm" variant="danger" onClick={() => onEnd(c)}>종료</Button>
                </>)}
              </span>,
            ])} empty="계약 없음 — 신규 계약으로 등록" />
        </Card>
      )}

      {creating && emp && <CreateModal employee={emp}
        onDone={() => { setCreating(false); refresh(); }} onError={fail}
        onClose={() => setCreating(false)} />}
    </div>
  );
}

function CreateModal({ employee, onDone, onError, onClose }: {
  employee: Employee; onDone: () => void; onError: (e: unknown) => void; onClose: () => void;
}) {
  const toast = useToast();
  const [f, setF] = useState({ contract_type: "REGULAR", valid_from: todayLocal(), valid_to: "",
    base_pay: "", weekly_hours: "40", work_schedule: "월~금 09:00-18:00", work_location: "본원",
    job_duties: "", note: "", activate: true });
  const m = useMutation({
    mutationFn: () => api("/hr/employment-contracts", { method: "POST", body: JSON.stringify({
      employee_id: employee.id, contract_type: f.contract_type, valid_from: f.valid_from,
      valid_to: f.valid_to || null, base_pay: Number(f.base_pay),
      weekly_hours: f.weekly_hours ? Number(f.weekly_hours) : null,
      work_schedule: f.work_schedule || null, work_location: f.work_location || null,
      job_duties: f.job_duties || null, note: f.note || null, activate: f.activate }) }),
    onSuccess: () => { toast.ok("근로계약 등록"); onDone(); }, onError });
  const ok = Number(f.base_pay) > 0 && f.valid_from
    && (f.contract_type !== "FIXED_TERM" || f.valid_to);
  return (
    <Modal title={`근로계약 등록 — ${employee.name}`} dirty size="lg" onClose={onClose}>
      <div className="grid gap-2 sm:grid-cols-2">
        <label className="text-sm text-slate-600">계약 유형
          <select value={f.contract_type} onChange={(e) => setF({ ...f, contract_type: e.target.value })}
            className="mt-1 w-full rounded-md border border-slate-300 px-2 py-1.5 text-sm">
            {Object.entries(TYPE).map(([k, v]) => <option key={k} value={k}>{v}</option>)}
          </select>
        </label>
        <Input label="월 기본급" value={f.base_pay} inputMode="numeric"
          onChange={(e) => setF({ ...f, base_pay: e.target.value.replace(/[^0-9]/g, "") })} />
        <Input label="시작일" type="date" value={f.valid_from}
          onChange={(e) => setF({ ...f, valid_from: e.target.value })} />
        <Input label={`만료일${f.contract_type === "FIXED_TERM" ? "(필수)" : "(무기한=공란)"}`}
          type="date" value={f.valid_to} onChange={(e) => setF({ ...f, valid_to: e.target.value })} />
        <Input label="주 소정근로시간" value={f.weekly_hours} inputMode="decimal"
          onChange={(e) => setF({ ...f, weekly_hours: e.target.value.replace(/[^0-9.]/g, "") })} />
        <Input label="취업장소" value={f.work_location}
          onChange={(e) => setF({ ...f, work_location: e.target.value })} />
        <Input label="근무일·시간" value={f.work_schedule}
          onChange={(e) => setF({ ...f, work_schedule: e.target.value })} />
        <Input label="업무내용" value={f.job_duties}
          onChange={(e) => setF({ ...f, job_duties: e.target.value })} />
        <label className="col-span-full flex items-center gap-2 text-sm text-slate-600">
          <input type="checkbox" checked={f.activate}
            onChange={(e) => setF({ ...f, activate: e.target.checked })} />
          즉시 발효(기존 유효 계약 자동 종료) — 미체크 시 작성중(DRAFT)
        </label>
        <div className="col-span-full">
          <Button disabled={m.isPending || !ok} onClick={() => m.mutate()}>등록</Button>
        </div>
      </div>
    </Modal>
  );
}

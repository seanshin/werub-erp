"use client";
/** 원무 — 환자 미수금 채권관리(0209): 미수 연령·독촉·분납·개별 대손상각·환불.
 *  회계통제(patient.closing) — 조회/처리는 ADMIN. 대손·환불은 GL 전기 동반. */
import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { ApiError, api } from "@/lib/api";
import { Badge, Button, Card, Input, Modal, Table } from "@/components/ui";
import { QueryErrorBanner } from "@/components/query-guard";
import { won } from "@/lib/format";
import { todayLocal, monthLocal } from "@/lib/dates";
import { useConfirm } from "@/components/confirm";
import { useToast } from "@/components/toast";

const DUN_METHOD: Record<string, string> = { SMS: "SMS", MAIL: "우편", PHONE: "전화", VISIT: "방문" };

interface Overdue {
  as_of: string; count: number; total_outstanding: number;
  items: {
    receivable_id: number; chart_no: string; description: string | null;
    amount: number; collected: number; outstanding: number; due_on: string;
    days_overdue: number; status: string;
    dunning: { level: number; sent_on: string } | null; has_active_plan: boolean;
  }[];
}
interface WriteOffResult { receivable_id: number; written_off: number; allowance_used: number;
  bad_debt_expense: number; voucher_id: number; written_off_on: string }
interface Aging { as_of: string; buckets: Record<string, number>; total: number }
const BUCKET_LABEL: Record<string, string> = {
  current: "정상(기한내)", "1-30": "1–30일", "31-60": "31–60일", "61-90": "61–90일", "90+": "90일+" };

export default function ReceivablesPage() {
  const qc = useQueryClient();
  const toast = useToast();
  const confirm = useConfirm();
  const fail = (e: unknown) => toast.err(e instanceof ApiError ? e.error.message : "처리 실패");
  const [detailId, setDetailId] = useState<number | null>(null);
  const [planFor, setPlanFor] = useState<number | null>(null);
  const [refunding, setRefunding] = useState(false);

  const overdue = useQuery({ queryKey: ["ar-overdue"], queryFn: () =>
    api<Overdue>("/patients/receivables/overdue") });
  const aging = useQuery({ queryKey: ["ar-aging"], queryFn: () =>
    api<Aging>("/patients/accrual/aging") });
  const refresh = () => { qc.invalidateQueries({ queryKey: ["ar-overdue"] });
    qc.invalidateQueries({ queryKey: ["ar-aging"] }); qc.invalidateQueries({ queryKey: ["ar-dunning"] }); };

  const dun = useMutation({
    mutationFn: (v: { id: number; method: string }) =>
      api(`/patients/receivables/${v.id}/dunning`, { method: "POST",
        body: JSON.stringify({ method: v.method }) }),
    onSuccess: () => { toast.ok("독촉 기록"); refresh(); }, onError: fail });
  const writeOff = useMutation({
    mutationFn: (id: number) => api<WriteOffResult>(`/patients/receivables/${id}/write-off`,
      { method: "POST", body: JSON.stringify({ reason: "회수불능 대손상각" }) }),
    // 서버가 충당금 상계액·비용 인식액·전표를 돌려주는데 고정 문구만 띄우고 버렸다 —
    // 회계 담당이 즉시 확인해야 하는 값이라 결과를 그대로 노출한다.
    onSuccess: (r) => {
      toast.ok(`대손상각 ${won(r.written_off)} — 충당금(1190) 상계 ${won(r.allowance_used)}`
        + ` · 대손상각비(5690) ${won(r.bad_debt_expense)} · 전표 #${r.voucher_id}`);
      refresh();
    },
    onError: fail });

  const onWriteOff = async (id: number, chart: string, amt: number) => {
    if (await confirm({ title: "대손상각", danger: true, confirmLabel: "상각",
      body: `${chart} 미수 ${won(amt)}을(를) 대손상각합니다. 충당금 우선 상계 후 부족분은 대손상각비로 전기됩니다.` }))
      writeOff.mutate(id);
  };

  return (
    <div className="space-y-4">
      <div className="flex items-center justify-between">
        <h1 className="text-lg font-bold">원무 — 환자 미수금 채권관리</h1>
        <Button variant="secondary" onClick={() => setRefunding(true)}>과오납 환불</Button>
      </div>
      <p className="text-xs text-slate-500">
        기한 경과 환자 본인부담 미수를 회수 관리합니다. 독촉(차수 자동)·분납 합의·개별 대손상각.
        대손·환불은 회계 전기가 동반됩니다(관리자 권한).</p>

      <QueryErrorBanner queries={[overdue, aging]} />

      {aging.data && (
        <Card title={`미수 연령분석 (기준 ${aging.data.as_of})`}>
          <div className="grid grid-cols-2 gap-2 sm:grid-cols-3 lg:grid-cols-6">
            {["current", "1-30", "31-60", "61-90", "90+"].map((k) => (
              <div key={k} className="rounded-lg border border-slate-200 p-2">
                <p className="text-xs text-slate-500">{BUCKET_LABEL[k]}</p>
                <p className="text-sm font-bold">{won(aging.data!.buckets[k] ?? 0)}</p>
              </div>))}
            <div className="rounded-lg border border-slate-300 bg-slate-50 p-2">
              <p className="text-xs text-slate-500">합계</p>
              <p className="text-sm font-bold text-slate-800">{won(aging.data.total)}</p>
            </div>
          </div>
        </Card>
      )}

      <Card title={`연체 미수 (${overdue.data?.count ?? 0}건 · 잔액 ${won(overdue.data?.total_outstanding ?? 0)})`}>
        <Table headers={["차트", "내역", "청구", "기수납", "미수잔액", "상태", "기한", "경과일",
          "독촉", "분납", ""]}
          rows={(overdue.data?.items ?? []).map((r) => [
            r.chart_no, r.description ?? "—", won(r.amount), won(r.collected), won(r.outstanding),
            // 부분회수(PARTIAL)와 무회수는 독촉 강도·분납 제안이 달라진다
            r.status === "PARTIAL"
              ? <Badge key="s" tone="blue">부분회수</Badge>
              : <Badge key="s" tone="gray">미회수</Badge>,
            r.due_on,        // 소멸시효·독촉 기산일 — 경과일만으론 역산이 안 된다
            <Badge key="d" tone={r.days_overdue > 90 ? "red" : r.days_overdue > 30 ? "amber" : "gray"}>
              {r.days_overdue}일</Badge>,
            r.dunning ? `${r.dunning.level}차 (${r.dunning.sent_on})` : "—",
            r.has_active_plan ? <Badge key="p" tone="blue">분납중</Badge> : "—",
            <span key="a" className="flex flex-wrap gap-1">
              <select defaultValue="" onChange={(e) => { if (e.target.value) {
                dun.mutate({ id: r.receivable_id, method: e.target.value }); e.target.value = ""; } }}
                disabled={dun.isPending}
                className="rounded border border-slate-300 px-1 py-0.5 text-xs">
                <option value="">독촉+</option>
                {Object.entries(DUN_METHOD).map(([k, v]) => <option key={k} value={k}>{v}</option>)}
              </select>
              <Button variant="secondary" size="sm" onClick={() => setDetailId(r.receivable_id)}>이력</Button>
              {!r.has_active_plan && <Button variant="secondary" size="sm"
                onClick={() => setPlanFor(r.receivable_id)}>분납</Button>}
              <Button variant="danger" size="sm" disabled={writeOff.isPending}
                onClick={() => onWriteOff(r.receivable_id, r.chart_no, r.outstanding)}>대손</Button>
            </span>,
          ])} empty={overdue.isFetching ? "불러오는 중…" : "연체 미수 없음"} />
      </Card>

      {detailId != null && (
        <DunningModal receivableId={detailId} onClose={() => setDetailId(null)} />
      )}
      {planFor != null && (
        <PlanModal receivableId={planFor} onDone={() => { setPlanFor(null); refresh(); }}
          onError={fail} onClose={() => setPlanFor(null)} />
      )}
      {refunding && (
        <RefundModal onDone={() => { setRefunding(false); refresh(); }} onError={fail}
          onClose={() => setRefunding(false)} />
      )}
    </div>
  );
}

function DunningModal({ receivableId, onClose }: { receivableId: number; onClose: () => void }) {
  const hist = useQuery({ queryKey: ["ar-dunning", receivableId], queryFn: () =>
    api<{ items: { id: number; level: number; method: string; outstanding: number;
      note: string | null; sent_on: string }[] }>(`/patients/receivables/${receivableId}/dunning`) });
  return (
    <Modal title="독촉 이력" onClose={onClose}>
      <Table headers={["차수", "방법", "잔액", "발송일", "비고"]}
        rows={(hist.data?.items ?? []).map((n) => [
          `${n.level}차`, DUN_METHOD[n.method] ?? n.method, won(n.outstanding), n.sent_on, n.note ?? "—",
        ])} empty={hist.isFetching ? "불러오는 중…" : "독촉 이력 없음"} />
    </Modal>
  );
}

function PlanModal({ receivableId, onDone, onError, onClose }: {
  receivableId: number; onDone: () => void; onError: (e: unknown) => void; onClose: () => void;
}) {
  const toast = useToast();
  const [months, setMonths] = useState("3");
  const [startMonth, setStartMonth] = useState(monthLocal());
  const create = useMutation({
    mutationFn: () => api<{ monthly_amount: number }>(
      `/patients/receivables/${receivableId}/installment-plan`, { method: "POST",
        body: JSON.stringify({ months: Number(months), start_month: startMonth }) }),
    onSuccess: (r) => { toast.ok(`분납 합의 — 월 ${won(r.monthly_amount)}`); onDone(); }, onError });
  return (
    <Modal title="분납(납부계획) 합의" onClose={onClose}>
      <div className="space-y-2">
        <Input label="분납 회차(개월)" value={months} inputMode="numeric"
          onChange={(e) => setMonths(e.target.value.replace(/[^0-9]/g, ""))} />
        <Input label="시작 월(YYYY-MM)" value={startMonth}
          onChange={(e) => setStartMonth(e.target.value)} />
        <Button disabled={create.isPending || !(Number(months) > 0) || !/^\d{4}-\d{2}$/.test(startMonth)}
          onClick={() => create.mutate()}>분납 등록</Button>
      </div>
    </Modal>
  );
}

function RefundModal({ onDone, onError, onClose }: {
  onDone: () => void; onError: (e: unknown) => void; onClose: () => void;
}) {
  const toast = useToast();
  const [f, setF] = useState({ chart_no: "", amount: "", reason: "", method: "CASH",
    refunded_on: todayLocal() });
  const refund = useMutation({
    mutationFn: () => api("/patients/refunds", { method: "POST", body: JSON.stringify({
      chart_no: f.chart_no, amount: Number(f.amount), reason: f.reason,
      refunded_on: f.refunded_on, method: f.method }) }),
    onSuccess: () => { toast.ok("환불 처리 — 역전표(현금 반환)"); onDone(); }, onError });
  return (
    <Modal title="과오납 환불" dirty onClose={onClose}>
      <div className="space-y-2">
        <Input label="차트번호" value={f.chart_no}
          onChange={(e) => setF({ ...f, chart_no: e.target.value })} />
        <Input label="환불 금액" value={f.amount} inputMode="numeric"
          onChange={(e) => setF({ ...f, amount: e.target.value.replace(/[^0-9]/g, "") })} />
        <label className="block text-sm text-slate-600">환불 수단
          <select value={f.method} onChange={(e) => setF({ ...f, method: e.target.value })}
            className="w-full rounded-md border border-slate-300 px-2 py-1.5 text-sm">
            <option value="CASH">현금</option><option value="TRANSFER">계좌이체</option>
            <option value="CARD">카드취소</option>
          </select>
        </label>
        <Input label="사유" value={f.reason} onChange={(e) => setF({ ...f, reason: e.target.value })} />
        <Input label="환불일" type="date" value={f.refunded_on}
          onChange={(e) => setF({ ...f, refunded_on: e.target.value })} />
        <Button disabled={refund.isPending || !f.chart_no.trim() || !(Number(f.amount) > 0) || !f.reason.trim()}
          onClick={() => refund.mutate()}>환불 처리</Button>
      </div>
    </Modal>
  );
}

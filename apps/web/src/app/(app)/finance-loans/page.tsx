"use client";
/** 재무 — 차입금 관리(자금관리 심화): 차입 실행→월 이자 인식→원금 상환. GL 자동 전기.
 *  차입 DR현금/CR차입금(2310단기·2510장기)·이자 DR5720/CR현금·상환 DR차입금/CR현금. write=ADMIN. */
import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { ApiError, api, auth } from "@/lib/api";
import { Badge, Button, Card, Input, Modal, Table } from "@/components/ui";
import { QueryErrorBanner } from "@/components/query-guard";
import { won } from "@/lib/format";
import { monthLocal, todayLocal } from "@/lib/dates";
import { useInputDialog } from "@/components/input-dialog";
import { useToast } from "@/components/toast";

interface Loan {
  id: number; lender: string; kind: string; principal: number;
  outstanding: number; annual_rate: number; status: string;
}
const KIND: Record<string, string> = { SHORT: "단기(2310)", LONG: "장기(2510)" };
const pct = (r: number) => `${(r * 100).toFixed(2)}%`;

export default function LoansPage() {
  const qc = useQueryClient();
  const toast = useToast();
  const { ask, dialog } = useInputDialog();
  const isAdmin = auth.claims()?.role === "ADMIN";
  const fail = (e: unknown) => toast.err(e instanceof ApiError ? e.error.message : "처리 실패");
  const [registering, setRegistering] = useState(false);
  const [repaying, setRepaying] = useState<Loan | null>(null);

  const loans = useQuery({ queryKey: ["loans"], queryFn: () =>
    api<{ items: Loan[] }>("/finance/loans") });
  const refresh = () => qc.invalidateQueries({ queryKey: ["loans"] });

  const accrue = useMutation({
    mutationFn: (p: { id: number; ym: string }) =>
      api<{ posted: boolean; interest?: number; reason?: string }>(
        `/finance/loans/${p.id}/accrue-interest?yyyymm=${p.ym}`, { method: "POST" }),
    onSuccess: (r) => { toast.ok(r.posted ? `이자 인식 ${won(r.interest ?? 0)} (DR 이자비용/CR 현금)`
      : `미전기 — ${r.reason ?? ""}`); refresh(); }, onError: fail });

  const onAccrue = async (l: Loan) => {
    const ym = await ask({ title: "월 이자 인식", label: "인식 월 (YYYY-MM)", placeholder: "YYYY-MM",
      required: true, defaultValue: monthLocal(),
      validate: (v) => /^\d{4}-\d{2}$/.test(v) ? null : "YYYY-MM 형식으로 입력하세요", confirmLabel: "인식" });
    if (ym) accrue.mutate({ id: l.id, ym: String(ym) });
  };

  const totalOut = (loans.data?.items ?? []).filter((l) => l.status === "ACTIVE")
    .reduce((a, l) => a + l.outstanding, 0);

  return (
    <div className="space-y-4">
      {dialog}
      <div className="flex items-center justify-between">
        <h1 className="text-lg font-bold">재무 — 차입금 관리</h1>
        {isAdmin && <Button onClick={() => setRegistering(true)}>차입 실행</Button>}
      </div>
      <p className="text-xs text-slate-500">
        차입 실행(DR 현금/CR 차입금)·월 이자 인식(잔여원금×연이율/12·멱등)·원금 상환(DR 차입금/CR 현금).
        전액 상환 시 REPAID. 이자·상환은 회계 전기가 동반됩니다(관리자).</p>

      <QueryErrorBanner queries={[loans]} />

      <div className="grid grid-cols-2 gap-2 sm:grid-cols-3">
        <div className="rounded-lg border border-slate-300 bg-slate-50 p-2">
          <p className="text-xs text-slate-500">활성 차입 잔여원금</p>
          <p className="text-sm font-bold">{won(totalOut)}</p></div>
      </div>

      <Card title="차입금 목록">
        <Table headers={["대주(차입처)", "구분", "원금", "잔여원금", "연이율", "상태", isAdmin ? "처리" : ""]}
          rows={(loans.data?.items ?? []).map((l) => [
            l.lender, KIND[l.kind] ?? l.kind, won(l.principal), won(l.outstanding), pct(l.annual_rate),
            <Badge key="s" tone={l.status === "REPAID" ? "green" : "blue"}>
              {l.status === "REPAID" ? "상환완료" : "차입중"}</Badge>,
            isAdmin
              ? (l.status === "ACTIVE"
                ? <span key="a" className="flex gap-1">
                    <Button size="sm" variant="secondary" disabled={accrue.isPending}
                      onClick={() => onAccrue(l)}>이자인식</Button>
                    <Button size="sm" onClick={() => setRepaying(l)}>상환</Button>
                  </span>
                : <span key="a" className="text-xs text-slate-400">—</span>)
              : "",
          ])} empty={loans.isFetching ? "불러오는 중…" : "차입금 없음"} />
      </Card>

      {registering && <RegisterModal onDone={() => { setRegistering(false); refresh(); }}
        onError={fail} onClose={() => setRegistering(false)} />}
      {repaying && <RepayModal loan={repaying} onDone={() => { setRepaying(null); refresh(); }}
        onError={fail} onClose={() => setRepaying(null)} />}
    </div>
  );
}

function RegisterModal({ onDone, onError, onClose }: {
  onDone: () => void; onError: (e: unknown) => void; onClose: () => void;
}) {
  const toast = useToast();
  const [f, setF] = useState({ lender: "", principal: "", annual_rate_pct: "",
    started_on: todayLocal(), term_months: "12", kind: "SHORT" });
  const m = useMutation({
    mutationFn: () => api("/finance/loans", { method: "POST", body: JSON.stringify({
      lender: f.lender, principal: Number(f.principal),
      annual_rate: Number(f.annual_rate_pct) / 100,   // % → 소수
      started_on: f.started_on, term_months: Number(f.term_months), kind: f.kind }) }),
    onSuccess: () => { toast.ok("차입 실행 (DR 현금/CR 차입금)"); onDone(); }, onError });
  const ok = f.lender.trim() && Number(f.principal) > 0 && Number(f.annual_rate_pct) >= 0
    && Number(f.term_months) > 0;
  return (
    <Modal title="차입 실행" dirty onClose={onClose}>
      <div className="space-y-2">
        <Input label="대주(차입처)" value={f.lender}
          onChange={(e) => setF({ ...f, lender: e.target.value })} />
        <Input label="원금" value={f.principal} inputMode="numeric"
          onChange={(e) => setF({ ...f, principal: e.target.value.replace(/[^0-9]/g, "") })} />
        <Input label="연이율(%)" value={f.annual_rate_pct} inputMode="decimal"
          onChange={(e) => setF({ ...f, annual_rate_pct: e.target.value.replace(/[^0-9.]/g, "") })} />
        <label className="block text-sm text-slate-600">구분
          <select value={f.kind} onChange={(e) => setF({ ...f, kind: e.target.value })}
            className="w-full rounded-md border border-slate-300 px-2 py-1.5 text-sm">
            <option value="SHORT">단기차입금(2310)</option>
            <option value="LONG">장기차입금(2510)</option>
          </select>
        </label>
        <Input label="차입일" type="date" value={f.started_on}
          onChange={(e) => setF({ ...f, started_on: e.target.value })} />
        <Input label="약정 기간(개월)" value={f.term_months} inputMode="numeric"
          onChange={(e) => setF({ ...f, term_months: e.target.value.replace(/[^0-9]/g, "") })} />
        <Button disabled={m.isPending || !ok} onClick={() => m.mutate()}>차입 실행</Button>
      </div>
    </Modal>
  );
}

function RepayModal({ loan, onDone, onError, onClose }: {
  loan: Loan; onDone: () => void; onError: (e: unknown) => void; onClose: () => void;
}) {
  const toast = useToast();
  const [amount, setAmount] = useState("");
  const [on, setOn] = useState(todayLocal());
  const m = useMutation({
    mutationFn: () => api<{ outstanding: number; status: string }>(
      `/finance/loans/${loan.id}/repay`, { method: "POST",
        body: JSON.stringify({ amount: Number(amount), on }) }),
    onSuccess: (r) => { toast.ok(r.status === "REPAID" ? "전액 상환 완료 (REPAID)"
      : `상환 — 잔여원금 ${won(r.outstanding)}`); onDone(); }, onError });
  const amt = Number(amount);
  return (
    <Modal title={`원금 상환 — ${loan.lender}`} dirty onClose={onClose}>
      <div className="space-y-2">
        <p className="text-xs text-slate-500">잔여원금 {won(loan.outstanding)}. 상환액만큼 차입금이
          감소합니다(DR 차입금/CR 현금). 전액 상환 시 상환완료 처리됩니다.</p>
        <Input label="상환액(원금)" value={amount} inputMode="numeric"
          onChange={(e) => setAmount(e.target.value.replace(/[^0-9]/g, ""))} />
        <Input label="상환일" type="date" value={on} onChange={(e) => setOn(e.target.value)} />
        <Button disabled={m.isPending || !(amt > 0) || amt > loan.outstanding}
          onClick={() => m.mutate()}>상환</Button>
      </div>
    </Modal>
  );
}

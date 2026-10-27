"use client";
/** 재무 — 리스 회계(K-IFRS 16): 사용권자산·리스부채 인식, 월별 이자·원금·상각. */
import { todayLocal, monthLocal } from "@/lib/dates";
import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { ApiError, api } from "@/lib/api";
import { won } from "@/lib/format";
import { Badge, Button, Card, Table } from "@/components/ui";
import { useToast } from "@/components/toast";

interface Lease { id: number; lease_no: string; asset_name: string; lessor: string | null;
  start_on: string; term_months: number; monthly_payment: number; rou_initial: number;
  rou_book: number; current_liability: number; periods_accrued: number; status: string }

const EMPTY = { lease_no: "", asset_name: "", start_on: todayLocal(),
  term_months: "36", monthly_payment: "", annual_rate: "0.05", lessor: "" };

export default function LeasesPage() {
  const qc = useQueryClient();
  const [f, setF] = useState(EMPTY);
  const [exempt, setExempt] = useState(false);
  const [ym, setYm] = useState(() => monthLocal());
  const toast = useToast();
  const leases = useQuery({ queryKey: ["leases"], queryFn: () => api<{ items: Lease[] }>("/finance/leases") });

  const create = useMutation({
    mutationFn: () => api<{ rou_initial: number }>("/finance/leases", { method: "POST",
      body: JSON.stringify({ lease_no: f.lease_no, asset_name: f.asset_name, start_on: f.start_on,
        term_months: Number(f.term_months), monthly_payment: Number(f.monthly_payment),
        annual_rate: Number(f.annual_rate), lessor: f.lessor, exempt }) }),
    onSuccess: (d) => { toast.ok(exempt ? "단기·소액 리스 등록(면제 — 직접 비용)" : `리스개시 — 사용권자산·리스부채 ${won(d.rou_initial)} 인식`); setF(EMPTY); setExempt(false);
      qc.invalidateQueries({ queryKey: ["leases"] }); },
    onError: (e) => toast.err(e instanceof ApiError ? e.error.message : "개시 실패"),
  });
  const remeasure = useMutation({
    mutationFn: (v: { id: number; pay: number; term: number }) =>
      api(`/finance/leases/${v.id}/remeasure`, { method: "POST",
        body: JSON.stringify({ new_monthly_payment: v.pay, new_term_months: v.term,
          on_date: todayLocal() }) }),
    onSuccess: () => { toast.ok("리스 재측정 완료"); qc.invalidateQueries({ queryKey: ["leases"] }); },
    onError: (e) => toast.err(e instanceof ApiError ? e.error.message : "재측정 실패"),
  });
  const onRemeasure = (l: Lease) => {
    const pay = window.prompt(`'${l.asset_name}' 신 월지급액`, String(l.monthly_payment));
    if (!pay) return;
    const term = window.prompt("신 총기간(개월)", String(l.term_months));
    if (term) remeasure.mutate({ id: l.id, pay: Number(pay), term: Number(term) });
  };
  const accrue = useMutation({
    mutationFn: (id: number) => api<{ interest: number; principal: number; amortization: number }>(
      `/finance/leases/${id}/accrue/${ym}`, { method: "POST" }),
    onSuccess: (d) => { toast.ok(`${ym} 리스 회계 — 이자 ${won(d.interest)}·원금 ${won(d.principal)}·상각 ${won(d.amortization)}`);
      qc.invalidateQueries({ queryKey: ["leases"] }); },
    onError: (e) => toast.err(e instanceof ApiError ? e.error.message : "회계 실패"),
  });

  const inp = (k: keyof typeof EMPTY, ph: string, w = "w-32") =>
    <input key={k} value={f[k]} onChange={(e) => setF({ ...f, [k]: e.target.value })} placeholder={ph}
      className={`${w} rounded-md border border-slate-300 px-3 py-1.5 text-sm`} />;

  return (
    <div className="space-y-4">
      <h1 className="text-lg font-bold">재무 — 리스 회계 (K-IFRS 16)</h1>

      <Card title="리스개시" action={<span className="text-[11px] text-slate-400">사용권자산=리스부채(지급액 현가)</span>}>
        <div className="flex flex-wrap items-center gap-2">
          {inp("lease_no", "리스번호", "w-28")}{inp("asset_name", "자산명")}{inp("lessor", "리스사", "w-28")}
          {inp("start_on", "개시일", "w-36")}{inp("term_months", "개월", "w-20")}
          {inp("monthly_payment", "월 지급액")}{inp("annual_rate", "연이율(0.05)", "w-24")}
          <label className="flex items-center gap-1 text-xs text-slate-600">
            <input type="checkbox" checked={exempt} onChange={(e) => setExempt(e.target.checked)} />단기·소액 면제</label>
          <Button disabled={create.isPending}
            onClick={() => f.lease_no && f.asset_name && f.monthly_payment ? create.mutate() : toast.err("필수 항목을 입력하세요")}>개시</Button>
        </div>
      </Card>

      <Card title="리스계약 현황" action={
        <span className="flex items-center gap-1 text-xs">월 회계
          <input value={ym} onChange={(e) => setYm(e.target.value)}
            className="w-24 rounded border border-slate-300 px-2 py-0.5" /></span>}>
        <Table headers={["번호", "자산", "월지급", "사용권자산(장부)", "리스부채", "경과", "상태", ""]}
          rows={(leases.data?.items ?? []).map((l) => [
            l.lease_no, l.asset_name, won(l.monthly_payment),
            won(l.rou_book), won(l.current_liability), `${l.periods_accrued}/${l.term_months}`,
            <Badge key="s" tone={l.status === "ACTIVE" ? "green" : "gray"}>{l.status === "ACTIVE" ? "진행" : "종료"}</Badge>,
            l.status === "ACTIVE"
              ? <span key="a" className="flex gap-1">
                  <Button size="sm" onClick={() => accrue.mutate(l.id)}>월 회계</Button>
                  <Button variant="neutral" size="sm" onClick={() => onRemeasure(l)}>재측정</Button>
                </span>
              : "",
          ])} empty={leases.isFetching ? "불러오는 중…" : "리스계약 없음"} />
      </Card>
    </div>
  );
}

"use client";
/** 의료보험 — 공단 지급 대사(X2-7). 청구·결정·지급 대사 요약 + 지급명세 입금 기록(ADMIN).
 * 백엔드(심사청구 회계 A~E)는 완비돼 있으나 화면이 없어 curl 수준이던 데드엔드를 해소. */
import { todayLocal, monthLocal } from "@/lib/dates";
import Link from "next/link";
import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { ApiError, api, auth } from "@/lib/api";
import { Badge, Button, Card, Input, Table } from "@/components/ui";
import { QueryError } from "@/components/query-guard";
import { useToast } from "@/components/toast";
import { useUrlState } from "@/lib/use-url-state";

interface Pay {
  id: number; payment_ref: string; care_month: string; paid_on: string;
  paid_amt: number; expected_amt: number | null; variance: number | null;
  voucher_ref: string | null; note: string | null;
}
interface Recon {
  care_month: string; claimed: number; decided: number; reduced: number;
  paid: number; outstanding: number; advance: number; status: string;
}
const STATUS_TONE: Record<string, "gray" | "green" | "amber"> = {
  DECIDED: "gray", PARTIALLY_PAID: "amber", PAID: "green",
};
const STATUS_KO: Record<string, string> = {
  DECIDED: "결정(미지급)", PARTIALLY_PAID: "부분지급", PAID: "완납",
};
const won = (n: number) => n.toLocaleString();
const vLink = (ref: string | null) => {
  const m = ref?.match(/voucher-(\d+)/);
  return m ? <Link href={`/finance?voucher=${m[1]}`} className="text-blue-600 hover:underline">{ref} ↗</Link>
    : (ref ?? "—");
};

export default function ClaimPaymentsPage() {
  const qc = useQueryClient();
  const toast = useToast();
  const isAdmin = auth.claims()?.role === "ADMIN";
  const [cm, setCm] = useUrlState("cm", monthLocal());   // 공유·유지(X2-1)

  const recon = useQuery({ queryKey: ["claim-recon", cm],
    queryFn: () => api<Recon>(`/claims/payments/reconciliation?care_month=${cm}`) });
  const list = useQuery({ queryKey: ["claim-payments", cm],
    queryFn: () => api<{ items: Pay[] }>(`/claims/payments?care_month=${cm}`) });

  const [ref, setRef] = useState("");
  const [paidOn, setPaidOn] = useState(todayLocal());
  const [amt, setAmt] = useState("");
  const [note, setNote] = useState("");
  const refresh = () => {
    qc.invalidateQueries({ queryKey: ["claim-recon"] });
    qc.invalidateQueries({ queryKey: ["claim-payments"] });
  };
  const record = useMutation({
    mutationFn: () => api<{ variance: number | null }>("/claims/payments", {
      method: "POST",
      body: JSON.stringify({ care_month: cm, payment_ref: ref.trim(),
        paid_on: paidOn, paid_amt: Number(amt), note: note.trim() || null }) }),
    onSuccess: (d) => {
      toast.ok(`지급 대사 기록 — 잔여 ${d.variance != null ? won(d.variance) + "원" : "—"}`);
      setRef(""); setAmt(""); setNote(""); refresh();
    },
    onError: (e) => toast.err(e instanceof ApiError ? e.error.message : "대사 실패"),
  });

  const r = recon.data;
  return (
    <div className="space-y-4">
      <div className="flex items-center gap-3">
        <h1 className="text-lg font-bold">의료보험 — 공단 지급 대사</h1>
        <input type="month" value={cm} onChange={(e) => setCm(e.target.value)}
          className="rounded-md border border-slate-300 px-2 py-1 text-sm" />
      </div>
      <p className="text-xs text-slate-500">
        진료월 청구·결정·지급 대사. 삭감=청구−결정 · 미지급=결정−지급 · 선수금=과다지급(2141). 지급 기록은 DR 1110/CR 1140 전기(ADMIN).</p>

      {recon.isError && <QueryError error={recon.error} onRetry={() => recon.refetch()} />}

      <Card title={`대사 요약 — ${cm}`}>
        <div className="grid gap-2 sm:grid-cols-4 lg:grid-cols-7">
          <Kpi label="청구액" value={r?.claimed} />
          <Kpi label="결정액" value={r?.decided} />
          <Kpi label="삭감(청구−결정)" value={r?.reduced} tone="text-red-600" />
          <Kpi label="지급액" value={r?.paid} tone="text-emerald-600" />
          <Kpi label="미지급(결정−지급)" value={r?.outstanding} tone="text-amber-600" />
          <Kpi label="과다지급 선수금" value={r?.advance} tone="text-blue-600" />
          <div className="rounded-lg border border-slate-200 bg-white p-3">
            <p className="text-xs text-slate-500">상태</p>
            <p className="mt-1">{r && <Badge tone={STATUS_TONE[r.status] ?? "gray"}>{STATUS_KO[r.status] ?? r.status}</Badge>}</p>
          </div>
        </div>
      </Card>

      {isAdmin && (
        <Card title="지급명세 입금 기록 (공단 실지급 대사 — 지급명세 단위 멱등)">
          <div className="flex flex-wrap items-end gap-2">
            <Input label="지급명세 식별(payment_ref)" value={ref}
              onChange={(e) => setRef(e.target.value)} placeholder="예: PAY-2026-07-01" />
            <Input label="입금일" type="date" value={paidOn} onChange={(e) => setPaidOn(e.target.value)} />
            <Input label="지급액" type="number" value={amt} onChange={(e) => setAmt(e.target.value)} />
            <Input label="비고(선택)" value={note} onChange={(e) => setNote(e.target.value)} />
            <Button disabled={!ref.trim() || !amt || record.isPending}
              onClick={() => record.mutate()}>대사 기록</Button>
          </div>
        </Card>
      )}

      <Card title="지급 대사 내역">
        {list.isError
          ? <QueryError error={list.error} onRetry={() => list.refetch()} />
          : <Table headers={["지급명세", "입금일", "지급액", "예상(결정)", "잔여", "전표", "비고"]}
              rows={(list.data?.items ?? []).map((p) => [
                p.payment_ref, p.paid_on, won(p.paid_amt),
                p.expected_amt != null ? won(p.expected_amt) : "—",
                p.variance != null
                  ? <span key="v" className={p.variance > 0 ? "text-amber-600" : ""}>{won(p.variance)}</span>
                  : "—",
                vLink(p.voucher_ref), p.note ?? "—",
              ])} empty="해당 진료월 지급 대사 내역이 없습니다" />}
      </Card>
    </div>
  );
}

function Kpi({ label, value, tone }: { label: string; value: number | undefined; tone?: string }) {
  return (
    <div className="rounded-lg border border-slate-200 bg-white p-3">
      <p className="text-xs text-slate-500">{label}</p>
      <p className={`mt-1 text-right text-base font-bold tabular-nums ${tone ?? "text-slate-800"}`}>
        {value != null ? won(value) : "—"}</p>
    </div>
  );
}

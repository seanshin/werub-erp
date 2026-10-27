"use client";
/** 재무 — 채권/채무·자금관리 (FR-09 확장): 미수금/미지급금·수금/지급·연령분석·자금수지. */
import { todayLocal } from "@/lib/dates";
import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api, ApiError } from "@/lib/api";
import { won } from "@/lib/format";
import { Badge, Card, Table } from "@/components/ui";
import { QueryErrorBanner } from "@/components/query-guard";
import { DonutChart } from "@/components/charts";
import { useInputDialog } from "@/components/input-dialog";
import { useToast } from "@/components/toast";

interface Open {
  id: number; party: string; amount: number; outstanding: number;
  due_on: string; status: string;
}
interface Aging { buckets: Record<string, number>; total: number }
// 서버는 출처별(patient 수납·claims 공단입금·scm 구매·hr 급여) 유입·유출 분해를 이미 준다 —
// 3숫자만 보면 자금이 왜 늘거나 줄었는지 화면에서 짚을 수 없다.
interface Cash { inflow: number; outflow: number; net: number;
  by_module: Record<string, { inflow: number; outflow: number }> }

// 자금 유입·유출의 출처(전표 source_module) — 코드 그대로면 어느 업무에서 온 돈인지 안 읽힌다
// prod 실측(2026-07-28) 기준 — 미등재 코드는 원문 그대로 표시된다.
const MODULE_KO: Record<string, string> = {
  patient: "원무 수납", claims: "공단 입금", scm: "자재 구매", hr: "급여",
  ar: "매출채권", ap: "매입채무", asset: "고정자산", lease: "리스",
  intangible: "무형자산", finance: "재무", manual: "수기 전표",
  "his.claim": "청구(HIS)", "his.billing": "진료비(HIS)", "his.voucher": "전표(HIS)",
  "closing-ifrs": "결산(IFRS)", "demo-sim": "데모 시뮬", "demo-ifrs": "데모 IFRS",
};

const STATUS: Record<string, "green" | "amber" | "blue"> = {
  OUTSTANDING: "amber", PARTIAL: "blue", COLLECTED: "green", PAID: "green",
};

export default function SettlementPage() {
  const qc = useQueryClient();
  const today = todayLocal();
  const y = new Date().getFullYear();
  const toast = useToast();
  const [arPage, setArPage] = useState(1);
  const [apPage, setApPage] = useState(1);
  const SIZE = 50;

  const ar = useQuery({ queryKey: ["ar", arPage], queryFn: () =>
    api<{ items: Open[]; count: number }>(`/finance/receivables?page=${arPage}&size=${SIZE}`) });
  const ap = useQuery({ queryKey: ["ap", apPage], queryFn: () =>
    api<{ items: Open[]; count: number }>(`/finance/payables?page=${apPage}&size=${SIZE}`) });
  const arAge = useQuery({ queryKey: ["ar-age"], queryFn: () => api<Aging>(`/finance/aging?as_of=${today}&kind=AR`) });
  const apAge = useQuery({ queryKey: ["ap-age"], queryFn: () => api<Aging>(`/finance/aging?as_of=${today}&kind=AP`) });
  const cash = useQuery({ queryKey: ["cash"], queryFn: () =>
    api<Cash>(`/finance/cash-flow?start=${y}-01-01&end=${y}-12-31`) });

  const refresh = () => { ["ar", "ap", "ar-age", "ap-age", "cash"].forEach((k) =>
    qc.invalidateQueries({ queryKey: [k] })); };

  const act = useMutation({
    mutationFn: (v: { url: string; body?: object }) =>
      api(v.url, { method: "POST", body: v.body ? JSON.stringify(v.body) : undefined }),
    onSuccess: () => { toast.ok("처리 완료"); refresh(); },
    onError: (e) => toast.err(e instanceof ApiError ? e.error.message : "처리 실패"),
  });

  const { ask, dialog } = useInputDialog();
  const settle = async (kind: "collect" | "pay", item: Open) => {
    const amt = await ask({
      title: kind === "collect" ? "수금 입력" : "지급 입력",
      label: `금액 (잔액 ${won(item.outstanding)})`, kind: "number",
      defaultValue: String(item.outstanding), min: 1, max: item.outstanding,
      confirmLabel: kind === "collect" ? "수금" : "지급" });
    if (amt == null) return;
    const base = kind === "collect" ? "receivables" : "payables";
    act.mutate({ url: `/finance/${base}/${item.id}/${kind}?amount=${amt}&on=${today}` });
  };

  // #ID 노출 — 전자세금계산서 대사(미지급금 ID 지정)에서 참조 가능하도록(전체검토 X0-4).
  const openRow = (kind: "collect" | "pay") => (o: Open) => [
    <span key="id" className="font-mono text-xs text-slate-500">#{o.id}</span>,
    o.party, won(o.amount), won(o.outstanding), o.due_on,
    <Badge key="s" tone={STATUS[o.status] ?? "gray"}>{o.status}</Badge>,
    o.status !== "COLLECTED" && o.status !== "PAID"
      ? <button key="b" disabled={act.isPending} onClick={() => settle(kind, o)}
          className="rounded bg-blue-600 px-2 py-0.5 text-xs text-white disabled:opacity-50">
          {kind === "collect" ? "수금" : "지급"}</button>
      : "—",
  ];

  const pager = (cur: number, total: number, set: (f: (p: number) => number) => void) => {
    if (total <= SIZE) return null;
    const last = Math.max(1, Math.ceil(total / SIZE));
    return (
      <div className="mt-3 flex items-center justify-center gap-3 text-sm">
        <button onClick={() => set((p) => Math.max(1, p - 1))} disabled={cur <= 1}
          className="rounded-md border border-slate-200 px-3 py-1 disabled:opacity-40 hover:bg-slate-50">이전</button>
        <span className="text-slate-500">{cur} / {last}</span>
        <button onClick={() => set((p) => Math.min(last, p + 1))} disabled={cur >= last}
          className="rounded-md border border-slate-200 px-3 py-1 disabled:opacity-40 hover:bg-slate-50">다음</button>
      </div>
    );
  };

  const AGE_COLOR: Record<string, string> = {
    current: "#10b981", "1-30": "#3b82f6", "31-60": "#f59e0b", "61-90": "#f97316", "90+": "#ef4444" };
  const ageData = (a?: Aging) => ["current", "1-30", "31-60", "61-90", "90+"]
    .map((k) => ({ label: k, value: a?.buckets?.[k] ?? 0, color: AGE_COLOR[k] }))
    .filter((d) => d.value > 0);

  return (
    <div className="space-y-4">
      {dialog}
      <h1 className="text-lg font-bold">재무 — 채권/채무·자금관리</h1>
      <QueryErrorBanner queries={[ar, ap, arAge, apAge, cash]} />

      <div className="grid gap-4 lg:grid-cols-3">
        <Card title="자금수지 (당해년)">
          <p className="text-sm">유입 <b className="text-blue-600">{won(cash.data?.inflow ?? 0)}</b></p>
          <p className="text-sm">유출 <b className="text-red-600">{won(cash.data?.outflow ?? 0)}</b></p>
          <p className="mt-1 text-base font-bold">순현금 {won(cash.data?.net ?? 0)}</p>
          {Object.keys(cash.data?.by_module ?? {}).length > 0 && (
            <div className="mt-2 border-t border-slate-100 pt-2">
              <p className="mb-1 text-[11px] font-semibold text-slate-500">출처별</p>
              <Table headers={["출처", "유입", "유출"]}
                rows={Object.entries(cash.data!.by_module)
                  .sort((a, b) => (b[1].inflow + b[1].outflow) - (a[1].inflow + a[1].outflow))
                  .map(([mod, v]) => [
                    MODULE_KO[mod] ?? mod,
                    <span key="i" className="text-blue-600">{won(v.inflow)}</span>,
                    <span key="o" className="text-red-600">{won(v.outflow)}</span>,
                  ])} />
            </div>
          )}
        </Card>
        <Card title="채권 연령분석">
          <DonutChart data={ageData(arAge.data)} />
          <p className="mt-1 text-right text-sm font-bold">계 {won(arAge.data?.total ?? 0)}</p>
        </Card>
        <Card title="채무 연령분석">
          <DonutChart data={ageData(apAge.data)} />
          <p className="mt-1 text-right text-sm font-bold">계 {won(apAge.data?.total ?? 0)}</p>
        </Card>
      </div>

      <Card title="채권 (미수금)">
        <Table headers={["#", "거래처", "금액", "미수잔액", "만기", "상태", ""]}
          rows={(ar.data?.items ?? []).map(openRow("collect"))} />
        {pager(arPage, ar.data?.count ?? 0, setArPage)}
      </Card>
      <Card title="채무 (미지급금)">
        <Table headers={["#", "거래처", "금액", "미지급잔액", "만기", "상태", ""]}
          rows={(ap.data?.items ?? []).map(openRow("pay"))} />
        {pager(apPage, ap.data?.count ?? 0, setApPage)}
      </Card>
    </div>
  );
}

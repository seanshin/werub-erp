"use client";
/** 재고 — 재고자산 평가(FIFO 원가)·안전재고 미달·저가법(NRV, K-IFRS 1002) 리포트. */
import { todayLocal, monthLocal, daysAgoLocal } from "@/lib/dates";
import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { ApiError, api, auth } from "@/lib/api";
import { won } from "@/lib/format";
import { Badge, Button, Card, Table } from "@/components/ui";
import { useToast } from "@/components/toast";
import { QueryErrorBanner } from "@/components/query-guard";

interface NrvItem { item_id: number; code: string; name: string; qty: number;
  book_value: number; wavg_unit: number; nrv_unit: number; nrv_value: number; loss: number }
interface NrvPreview { yyyymm: string; items: NrvItem[]; required: number; current: number; delta: number }

interface Val { item_id: number; code: string; name: string; category: string;
  qty: number; value: number; cost_missing: boolean }
interface Valuation { items: Val[]; total_value: number;
  by_category: { category: string; value: number }[]; missing_cost_items: number }
// 저가법(NRV) 결산조정 = finance.periods read(DIRECTOR/ADMIN/FINANCE_MANAGER) — 재고 화면 메뉴는
// 부서장·물류관리자에게도 열리므로, 권한 없는 역할에는 NRV 카드를 아예 숨긴다(403 빈 카드 방지).
const NRV_ROLES = new Set(["DIRECTOR", "ADMIN", "FINANCE_MANAGER"]);

interface Short { code: string; name: string; category: string;
  on_hand: number; safety_stock: number; shortage: number }


export default function InventoryValuationPage() {
  const val = useQuery({ queryKey: ["inv-val"], queryFn: () =>
    api<Valuation>("/inventory/valuation") });
  const sh = useQuery({ queryKey: ["inv-short"], queryFn: () =>
    api<{ count: number; items: Short[] }>("/inventory/shortage") });
  const [until] = useState(() => todayLocal());
  const [since] = useState(() => daysAgoLocal(90));
  const to = useQuery({ queryKey: ["inv-turn", since], queryFn: () =>
    api<{ dead_stock_count: number; items: { code: string; name: string; out_qty: number;
      current_stock: number; turnover: number; turnover_days: number | null; dead_stock: boolean }[] }>(
      `/inventory/turnover?since=${since}&until=${until}`) });
  const cogs = useQuery({ queryKey: ["inv-cogs", since], queryFn: () =>
    api<{ total_cogs: number; items: { code: string; name: string; cost_method: string;
      out_qty: number; cogs: number }[] }>(`/inventory/cogs?since=${since}&until=${until}`) });

  // 저가법(NRV) 결산조정
  const qc = useQueryClient();
  const [nrvYm, setNrvYm] = useState(() => monthLocal());
  const toast = useToast();
  const canNrv = NRV_ROLES.has(auth.claims()?.role ?? "");
  const canPostNrv = auth.claims()?.role === "ADMIN";   // nrv/post = finance.periods write(ADMIN)
  const nrv = useQuery({ queryKey: ["inv-nrv", nrvYm], enabled: canNrv && /^\d{4}-\d{2}$/.test(nrvYm),
    queryFn: () => api<NrvPreview>(`/inventory/nrv/preview?yyyymm=${nrvYm}`) });
  const postNrv = useMutation({
    mutationFn: () => api(`/inventory/nrv/post?yyyymm=${nrvYm}`, { method: "POST" }),
    onSuccess: (d: unknown) => { const r = d as { posted: boolean; delta: number };
      toast.ok(r.posted ? `저가법 평가손실 전기(${won(r.delta)})` : "조정 없음");
      qc.invalidateQueries({ queryKey: ["inv-nrv"] }); },
    onError: (e) => toast.err(e instanceof ApiError ? e.error.message : "전기 실패"),
  });

  return (
    <div className="space-y-4">
      <h1 className="text-lg font-bold">재고 — 재고자산 평가·안전재고</h1>
      <QueryErrorBanner queries={[val, sh, to, cogs]} />

      <div className="grid gap-4 lg:grid-cols-3">
        <Card title="재고자산 총평가액">
          <p className="text-2xl font-bold">{won(val.data?.total_value ?? 0)}</p>
          {(val.data?.missing_cost_items ?? 0) > 0 &&
            <p className="mt-1 text-xs text-amber-600">단가 누락 품목 {val.data?.missing_cost_items}건(과거 입고)</p>}
        </Card>
        <Card title="카테고리 소계">
          <Table headers={["카테고리", "평가액"]}
            rows={(val.data?.by_category ?? []).map((c) => [c.category, won(c.value)])} />
        </Card>
        <Card title="안전재고 미달">
          <p className="text-2xl font-bold text-red-600">{sh.data?.count ?? 0}건</p>
          <p className="mt-1 text-xs text-slate-400">현재고 &lt; 안전재고</p>
        </Card>
      </div>

      <Card title="안전재고 미달 품목">
        <Table headers={["코드", "품목", "현재고", "안전재고", "부족"]}
          rows={(sh.data?.items ?? []).map((s) => [
            <code key="c" className="text-xs">{s.code}</code>, s.name,
            s.on_hand.toLocaleString(), s.safety_stock.toLocaleString(),
            <b key="x" className="text-red-600">{s.shortage.toLocaleString()}</b>,
          ])} empty={sh.isFetching ? "불러오는 중…" : "미달 품목 없음"} />
      </Card>

      <Card title="재고회전율 (최근 90일)" action={
        <span className="text-[11px] text-slate-400">장기 미사용 {to.data?.dead_stock_count ?? 0}건 · 회전율=출고/현재고</span>}>
        <Table headers={["코드", "품목", "출고량", "현재고", "회전율", "회전일수", ""]}
          rows={(to.data?.items ?? []).slice(0, 30).map((t) => [
            <code key="c" className="text-xs">{t.code}</code>, t.name,
            t.out_qty.toLocaleString(), t.current_stock.toLocaleString(),
            t.turnover.toFixed(2),
            t.turnover_days == null ? "—" : `${t.turnover_days}일`,
            t.dead_stock ? <Badge key="d" tone="red">장기재고</Badge> : "",
          ])} empty={to.isFetching ? "불러오는 중…" : "이동 없음"} />
      </Card>

      {canNrv && <Card title="저가법 평가 (NRV, K-IFRS 1002)" action={
        <span className="text-[11px] text-slate-400">평가손실=max(0, 장부가−NRV평가액) · 충당 차액만 결산분개</span>}>
        <div className="mb-3 flex flex-wrap items-center gap-2">
          <input value={nrvYm} onChange={(e) => setNrvYm(e.target.value)} placeholder="YYYY-MM"
            className="w-36 rounded-md border border-slate-300 px-3 py-1.5 text-sm" />
          <Button variant="neutral" onClick={() => nrv.refetch()}>미리보기</Button>
          <Button onClick={() => postNrv.mutate()} disabled={!nrv.data?.delta || postNrv.isPending || !canPostNrv}
            title={canPostNrv ? undefined : "전기는 관리자(기간마감 권한)만 가능합니다"}>평가손실 전기</Button>
          {nrv.data && <span className="text-sm text-slate-600">목표충당 <b>{won(nrv.data.required)}</b> · 차액 <b className={nrv.data.delta ? "text-blue-600" : "text-emerald-600"}>{won(nrv.data.delta)}</b></span>}
        </div>
        <Table headers={["코드", "품목", "잔량", "장부가", "가중평균단가", "NRV단가", "평가손실"]}
          rows={(nrv.data?.items ?? []).map((it) => [
            <code key="c" className="text-xs">{it.code}</code>, it.name, it.qty.toLocaleString(),
            won(it.book_value), it.wavg_unit.toLocaleString(), it.nrv_unit.toLocaleString(),
            <b key="l" className="text-red-600">{won(it.loss)}</b>,
          ])} empty={nrv.isFetching ? "불러오는 중…" : "평가손실 대상 없음(NRV단가 설정·원가>NRV 시)"} />
        <p className="mt-1 text-[11px] text-slate-400">NRV단가는 품목 마스터에 설정(미설정 품목은 원가 유지·제외).</p>
      </Card>}

      <Card title="매출원가 (출고원가, 최근 90일 — K-IFRS 1002)" action={
        <span className="text-[11px] text-slate-400">cost_method(FIFO=실제 lot 단가 / WAVG=이동평균) 적용 · 총 {won(cogs.data?.total_cogs ?? 0)}</span>}>
        <Table headers={["코드", "품목", "원가흐름", "출고량", "출고원가"]}
          rows={(cogs.data?.items ?? []).slice(0, 30).map((x) => [
            <code key="c" className="text-xs">{x.code}</code>, x.name,
            x.cost_method === "WAVG" ? "가중평균" : "FIFO",
            x.out_qty.toLocaleString(), <b key="g">{won(x.cogs)}</b>,
          ])} empty={cogs.isFetching ? "불러오는 중…" : "출고 없음"} />
      </Card>

      <Card title="재고자산 평가 명세">
        <Table headers={["코드", "품목", "카테고리", "잔량", "평가액", ""]}
          rows={(val.data?.items ?? []).map((v) => [
            <code key="c" className="text-xs">{v.code}</code>, v.name, v.category,
            v.qty.toLocaleString(), <b key="v">{won(v.value)}</b>,
            v.cost_missing ? <Badge key="m" tone="amber">단가누락</Badge> : "",
          ])} empty={val.isFetching ? "불러오는 중…" : "재고 없음"} />
      </Card>
    </div>
  );
}

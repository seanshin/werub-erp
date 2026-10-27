"use client";
/** IT 비용 점검(ITAM M2b) — 월 OPEX 집계 + 구독·클라우드 비용 변동 감지·미입력·책임자 확인. */
import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { ApiError, api } from "@/lib/api";
import { Badge, Button, Card, Input, Table } from "@/components/ui";
import { useToast } from "@/components/toast";
import { won, wonOrDash } from "@/lib/format";

interface Review {
  id: number; service_name: string; service_type: string; vendor: string | null;
  prev_amount: number | null; amount: number | null; delta_pct: number | null;
  reason: string | null; status: string; note: string | null;
}
interface Opex { period: string; total_krw: number; by_type: Record<string, number>;
  by_cost_center: Record<string, number>; by_vendor: Record<string, number> }

const TYPE_KO: Record<string, string> = {
  PERPETUAL_SW: "구매 SW", SAAS: "SaaS", AI_SUBSCRIPTION: "AI 구독",
  CLOUD: "클라우드", OPEN_SOURCE: "오픈소스" };
const REASON_KO: Record<string, string> = { SPIKE: "급증", DROP: "급감", MISSING: "미입력" };

interface ShadowRow { source: string; id: number; party: string | null; description: string | null;
  amount: number; on: string; source_module: string }
interface Shadow { months: number; known_vendor_count: number; managed_total: number;
  managed: ShadowRow[]; shadow_total: number; shadow: ShadowRow[] }

/** Shadow-IT 대사(R4) — 미지급금·세금계산서(매입) 중 ITAM 미등록 IT 벤더 지출 탐지. */
function ShadowItSection() {
  const [open, setOpen] = useState(false);
  const s = useQuery({ queryKey: ["itam-shadow-it"], enabled: open,
    queryFn: () => api<Shadow>("/itam/shadow-it?months=12") });
  const d = s.data;
  return (
    <Card title="Shadow-IT 대사 (최근 12개월 매입)">
      {!open ? (
        <button onClick={() => setOpen(true)}
          className="rounded-md border border-slate-300 px-4 py-1.5 text-sm text-slate-700 hover:bg-slate-50">
          미관리 IT 지출 점검 실행</button>
      ) : (
        <div className="space-y-3">
          <div className="flex flex-wrap items-baseline gap-4">
            <span className="text-sm text-slate-500">알려진 IT 벤더 {d?.known_vendor_count ?? 0}곳</span>
            <span className="text-sm text-emerald-600">관리 지출 {won(d?.managed_total ?? 0)}</span>
            <span className="text-sm font-semibold text-red-600">미관리 IT 후보 {won(d?.shadow_total ?? 0)} ({d?.shadow.length ?? 0}건)</span>
          </div>
          <p className="text-[11px] text-slate-400">
            IT성 키워드(SaaS·클라우드·라이선스·구독 등)에 걸리나 ITAM에 등록되지 않은 벤더로의 지출입니다.
            벤더 마스터가 없어 이름 기준 추정이므로 <b>수동 검토 후</b> 실제 IT 지출이면 서비스로 등록하세요.</p>
          <Table headers={["출처", "거래처", "내용", "금액", "일자"]}
            empty="미관리 IT 후보 지출이 없습니다"
            rows={(d?.shadow ?? []).map((r) => [
              <Badge key="s" tone={r.source === "AP" ? "amber" : "blue"}>{r.source === "AP" ? "미지급금" : "세금계산서"}</Badge>,
              r.party ?? "-", r.description ?? "-",
              `${r.amount.toLocaleString()} 원`, r.on])} />
        </div>
      )}
    </Card>
  );
}

interface Posting { period: string; posted: number; unposted: number; unposted_krw: number }

/** IT 비용 GL 전표 전기(R4·F-6) — DR 정보시스템운영비(5525) / CR 미지급금(2110). */
function PostingSection({ period }: { period: string }) {
  const qc = useQueryClient();
  const toast = useToast();
  const st = useQuery({ queryKey: ["itam-cost-posting", period],
    queryFn: () => api<Posting>(`/itam/cost-postings?period=${period}`) });
  const run = useMutation({
    mutationFn: () => api<{ posted: number; total_krw: number }>(
      `/itam/cost-postings/run?period=${period}`, { method: "POST" }),
    onSuccess: (r) => { toast.ok(`전기 완료 — ${r.posted}건 · ${won(r.total_krw)} (DR 정보시스템운영비/CR 미지급금)`);
      qc.invalidateQueries({ queryKey: ["itam-cost-posting", period] });
      qc.invalidateQueries({ queryKey: ["itam-service-costs"] }); },
    onError: (e) => toast.err(e instanceof ApiError ? e.message : "전기 실패"),
  });
  const d = st.data;
  return (
    <Card title={`GL 전표 전기 — ${period}`}>
      <div className="flex flex-wrap items-center gap-4">
        <div><p className="text-2xl font-bold text-slate-900">{d?.unposted ?? 0}</p>
          <p className="text-xs text-slate-500">미전기 ({(d?.unposted_krw ?? 0).toLocaleString()} 원)</p></div>
        <div><p className="text-2xl font-bold text-emerald-600">{d?.posted ?? 0}</p>
          <p className="text-xs text-slate-500">전기 완료</p></div>
        <Button isLoading={run.isPending} disabled={(d?.unposted ?? 0) === 0}
          onClick={() => run.mutate()}>전표 전기</Button>
      </div>
      <p className="mt-2 text-[11px] text-slate-400">
        운영중 서비스의 당월 비용을 건별 전표로 전기합니다 — <b>DR 정보시스템운영비(5525) / CR 미지급금(2110)</b>.
        의료기관 면세로 매입세액은 불공제(부가세 포함액 전액 비용). 이미 전기된 건은 제외(멱등).</p>
    </Card>
  );
}

interface VarRow { cost_center: string; budget_krw: number | null; actual_krw: number;
  variance_krw: number; over_pct: number | null; over: boolean; unbudgeted?: boolean }
interface Variance { period: string; total_budget_krw: number | null; total_actual_krw: number;
  total_over: boolean; rows: VarRow[] }

/** IT 예산 관리·예산 대비 실적(R4·F-6). */
function BudgetSection({ period }: { period: string }) {
  const qc = useQueryClient();
  const toast = useToast();
  const [cc, setCc] = useState("");
  const [amount, setAmount] = useState("");
  const v = useQuery({ queryKey: ["itam-budget-variance", period],
    queryFn: () => api<Variance>(`/itam/budget-variance?period=${period}`) });
  const save = useMutation({
    mutationFn: () => api("/itam/budgets", { method: "POST", body: JSON.stringify({
      period, cost_center: cc.trim() || null, amount_krw: Number(amount) }) }),
    onSuccess: () => { setCc(""); setAmount(""); toast.ok("예산 설정 완료");
      qc.invalidateQueries({ queryKey: ["itam-budget-variance", period] }); },
    onError: (e) => toast.err(e instanceof ApiError ? e.message : "예산 저장 실패") });
  const d = v.data;

  return (
    <Card title={`IT 예산 대비 실적 — ${period}`}>
      {d && d.total_budget_krw != null && (
        <div className="mb-3 flex flex-wrap items-baseline gap-3">
          <span className="text-sm text-slate-500">총예산 {wonOrDash(d.total_budget_krw)}</span>
          <span className="text-sm text-slate-500">실적 {wonOrDash(d.total_actual_krw)}</span>
          <span className={`text-sm font-semibold ${d.total_over ? "text-red-600" : "text-emerald-600"}`}>
            {d.total_over ? "초과" : "이내"} ({won(d.total_actual_krw - (d.total_budget_krw ?? 0))})</span>
        </div>
      )}
      <div className="mb-3 flex flex-wrap items-end gap-2">
        <label className="block"><span className="mb-1 block text-xs text-slate-600">코스트센터(비우면 전체)</span>
          <input value={cc} onChange={(e) => setCc(e.target.value)} placeholder="예: 영상의학 / 전체"
            className="rounded-md border border-slate-300 px-2 py-1.5 text-sm" /></label>
        <Input label="예산(원)" value={amount} onChange={(e) => setAmount(e.target.value)} />
        <Button size="sm" disabled={!amount || save.isPending} onClick={() => save.mutate()}>예산 설정</Button>
      </div>
      <Table headers={["코스트센터", "예산", "실적", "차이", "달성률"]}
        empty="설정된 예산·지출이 없습니다"
        rows={(d?.rows ?? []).map((r) => [
          <span key="c">{r.cost_center}{r.unbudgeted && <Badge tone="amber">미예산</Badge>}</span>,
          wonOrDash(r.budget_krw), wonOrDash(r.actual_krw),
          <span key="v" className={r.over ? "font-semibold text-red-600" : "text-slate-600"}>
            {r.variance_krw > 0 ? "+" : ""}{r.variance_krw.toLocaleString()}</span>,
          r.over_pct == null ? "-"
            : <span key="p" className={r.over ? "font-semibold text-red-600" : "text-slate-600"}>
                {(100 + r.over_pct).toFixed(0)}%{r.over && " 초과"}</span>])} />
    </Card>
  );
}

/** 집계 분해 카드(F-6a) — 부서·벤더별 지출 내림차순. */
function Breakdown({ title, data }: { title: string; data?: Record<string, number> }) {
  const rows = Object.entries(data ?? {}).sort((a, b) => b[1] - a[1]);
  const max = rows.length ? rows[0][1] : 0;
  return (
    <Card title={title}>
      {rows.length === 0 ? <p className="text-sm text-slate-400">해당 월 비용 입력 없음</p> : (
        <div className="space-y-1.5">
          {rows.map(([k, v]) => (
            <div key={k}>
              <div className="flex justify-between text-sm">
                <span className="text-slate-600">{k}</span>
                <span className="font-medium">{v.toLocaleString()} 원</span>
              </div>
              <div className="mt-0.5 h-1.5 rounded-full bg-slate-100">
                <div className="h-1.5 rounded-full bg-blue-400"
                  style={{ width: `${max ? (v / max) * 100 : 0}%` }} />
              </div>
            </div>
          ))}
        </div>
      )}
    </Card>
  );
}

function thisMonth(): string {
  // 서버 없이 브라우저 현재월 — 초기 기본값
  const d = new Date();
  return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}`;
}

export default function CostReviewPage() {
  const qc = useQueryClient();
  const toast = useToast();
  const [period, setPeriod] = useState(thisMonth());

  const opex = useQuery({ queryKey: ["itam-opex", period],
    queryFn: () => api<Opex>(`/itam/opex?period=${period}`) });
  const reviews = useQuery({ queryKey: ["itam-cost-reviews", period],
    queryFn: () => api<{ items: Review[] }>(`/itam/cost-reviews?period=${period}`) });

  const run = useMutation({
    mutationFn: () => api<{ flagged: number; missing: number; ok: number; total: number }>(
      `/itam/cost-reviews/run?period=${period}`, { method: "POST" }),
    onSuccess: (r) => { toast.ok(`점검 완료 — 변동 ${r.flagged} · 미입력 ${r.missing} · 정상 ${r.ok} / 총 ${r.total}`);
      qc.invalidateQueries({ queryKey: ["itam-cost-reviews", period] }); },
    onError: (e) => toast.err(e instanceof ApiError ? e.message : "점검 실패"),
  });
  const confirm = useMutation({
    mutationFn: (id: number) => api(`/itam/cost-reviews/${id}/confirm`, {
      method: "POST", body: JSON.stringify({}) }),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["itam-cost-reviews", period] }),
    onError: (e) => toast.err(e instanceof ApiError ? e.message : "확인 실패"),
  });

  const items = reviews.data?.items ?? [];
  const flagged = items.filter((r) => r.status === "FLAGGED");

  return (
    <div className="space-y-4">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <div>
          <h1 className="text-lg font-bold text-slate-900">IT 비용 점검</h1>
          <p className="text-xs text-slate-500">월 IT 지출(OPEX) 집계 + 구독·클라우드 비용 변동 감지·미입력 확인 (ITAM M2b)</p>
        </div>
        <div className="flex items-end gap-2">
          <input type="month" value={period} onChange={(e) => setPeriod(e.target.value)}
            className="rounded-md border border-slate-300 px-2 py-1.5 text-sm" />
          <Button isLoading={run.isPending} onClick={() => run.mutate()}>점검 실행</Button>
        </div>
      </div>
      {(opex.isError || reviews.isError) && <p className="text-sm text-red-600">조회 실패 — 권한(itam.assets)을 확인하세요</p>}

      <div className="grid gap-4 md:grid-cols-2">
        <Card title={`월 IT 지출(OPEX) — ${period}`}>
          <p className="text-2xl font-bold text-slate-900">
            {(opex.data?.total_krw ?? 0).toLocaleString()} <span className="text-sm text-slate-400">원</span></p>
          <div className="mt-3 space-y-1">
            {Object.entries(opex.data?.by_type ?? {}).map(([t, v]) => (
              <div key={t} className="flex justify-between text-sm">
                <span className="text-slate-600">{TYPE_KO[t] ?? t}</span>
                <span className="font-medium">{v.toLocaleString()} 원</span>
              </div>
            ))}
            {Object.keys(opex.data?.by_type ?? {}).length === 0 &&
              <p className="text-sm text-slate-400">해당 월 비용 입력 없음</p>}
          </div>
        </Card>
        <Card title="점검 요약">
          <div className="flex gap-4">
            <div><p className="text-2xl font-bold text-red-600">{flagged.length}</p>
              <p className="text-xs text-slate-500">확인 필요(변동·미입력)</p></div>
            <div><p className="text-2xl font-bold text-slate-700">{items.length}</p>
              <p className="text-xs text-slate-500">점검 대상</p></div>
          </div>
          <p className="mt-3 text-[11px] text-slate-400">
            매월 3일 배치가 전월 대비 ±30% 변동·미입력을 자동 감지해 관리자에게 알립니다. [점검 실행]으로 수동 재점검.</p>
        </Card>
      </div>

      <div className="grid gap-4 md:grid-cols-2">
        <Breakdown title="부서(코스트센터)별 지출" data={opex.data?.by_cost_center} />
        <Breakdown title="벤더별 지출" data={opex.data?.by_vendor} />
      </div>

      <PostingSection period={period} />

      <BudgetSection period={period} />

      <ShadowItSection />

      <Card title={`비용 점검 상세 — ${period}`}>
        <Table headers={["서비스", "유형", "전월(원)", "당월(원)", "증감", "상태", ""]}
          empty="점검 대상이 없습니다 — [점검 실행]을 눌러 주세요"
          rows={items.map((r) => [
            <span key="n">{r.service_name}{r.vendor && <span className="text-slate-400"> · {r.vendor}</span>}</span>,
            TYPE_KO[r.service_type] ?? r.service_type,
            r.prev_amount != null ? r.prev_amount.toLocaleString() : "-",
            r.amount != null ? r.amount.toLocaleString() : <span className="text-red-500">미입력</span>,
            r.delta_pct != null
              ? <span key="d" className={Math.abs(r.delta_pct) >= 30 ? "font-semibold text-red-600" : "text-slate-600"}>
                  {r.delta_pct > 0 ? "+" : ""}{r.delta_pct}%</span>
              : "-",
            r.status === "CONFIRMED" ? <Badge key="s" tone="green">확인완료</Badge>
              : r.status === "FLAGGED" ? <Badge key="s" tone="red">{REASON_KO[r.reason ?? ""] ?? "확인필요"}</Badge>
              : <Badge key="s" tone="gray">정상</Badge>,
            r.status !== "CONFIRMED"
              ? <Button key="c" variant="neutral" size="sm" onClick={() => confirm.mutate(r.id)}>확인</Button>
              : "",
          ])} />
      </Card>
    </div>
  );
}

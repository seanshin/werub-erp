"use client";
/** 재무 — 회계 기간 마감(월 결산): 손익 스냅샷·마감 잠금·재개방. */
import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { ApiError, api } from "@/lib/api";
import { wonOrDash as won } from "@/lib/format";
import { Badge, Button, Card, Table } from "@/components/ui";
import { useToast } from "@/components/toast";
import { QueryErrorBanner } from "@/components/query-guard";

interface Period {
  yyyymm: string; fiscal_year: number; status: string;
  total_revenue: number | null; total_expense: number | null; net_income: number | null;
  voucher_count: number | null; closed_at: string | null; note: string | null;
}
interface AllowancePreview {
  yyyymm: string; as_of: string; required: number; current: number; delta: number;
  base_ecl: number; overlay: number;
  buckets: { bucket: string; balance: number; rate: number; allowance: number }[];
}
interface DeferredTaxPreview {
  year: number; rate: number; dta: number; dtl: number; delta_dta: number; delta_dtl: number;
  items: { account: string; name: string; nature: string; temp_diff: number; tax_effect: number }[];
}


export default function FinancePeriodsPage() {
  const qc = useQueryClient();
  const toast = useToast();
  const [ym, setYm] = useState("");
  const periods = useQuery({ queryKey: ["fiscal-periods"],
    queryFn: () => api<{ items: Period[] }>("/finance/periods") });

  // 마감 사전점검 — 미결전표·차대불일치·감가미계상·미배부원가·미대사 세금계산서 등 7종 경고를
  // 서버가 이미 반환하는데 화면이 호출하지 않아 **경고 없이 곧바로 잠갔다**.
  const [checked, setChecked] = useState<{ yyyymm: string; ok: boolean;
    warnings: { code: string; count: number; message: string }[] } | null>(null);
  const precheck = useMutation({
    mutationFn: (m: string) => api<{ yyyymm: string; ok: boolean;
      warnings: { code: string; count: number; message: string }[] }>(
      `/finance/periods/${m}/preclose-check`),
    onSuccess: (d) => { setChecked(d);
      if (d.ok) toast.ok(`${d.yyyymm} 사전점검 통과 — 경고 없음`);
      else toast.err(`${d.yyyymm} 사전점검 경고 ${d.warnings.length}건 — 확인 후 마감하세요`); },
    onError: (e) => toast.err(e instanceof ApiError ? e.error.message : "사전점검 실패"),
  });

  const act = useMutation({
    mutationFn: (v: { yyyymm: string; action: "close" | "reopen" }) =>
      api(`/finance/periods/${v.yyyymm}/${v.action}`, { method: "POST", body: JSON.stringify({ note: "" }) }),
    onSuccess: (_d, v) => { toast.ok(`${v.yyyymm} ${v.action === "close" ? "마감" : "재개방"} 완료`);
      setYm(""); setChecked(null);
      qc.invalidateQueries({ queryKey: ["fiscal-periods"] }); },
    onError: (e) => toast.err(e instanceof ApiError ? e.error.message : "처리 실패"),
  });

  const monthEnd = useMutation({
    mutationFn: (m: string) => api<{ depreciation_count: number; retirement_employees: number;
      retirement_increment: number; lease_accrued: number }>(`/finance/month-end/${m}/run`, { method: "POST" }),
    onSuccess: (d) => toast.ok(`월마감 — 감가 ${d.depreciation_count}건·퇴직충당 ${d.retirement_employees}명(${won(d.retirement_increment)})·리스 ${d.lease_accrued}건`),
    onError: (e) => toast.err(e instanceof ApiError ? e.error.message : "월마감 실패"),
  });

  // 대손충당금(K-IFRS 1109) 결산조정
  const [alwYm, setAlwYm] = useState("");
  const allowance = useQuery({
    queryKey: ["allowance-preview", alwYm], enabled: /^\d{4}-\d{2}$/.test(alwYm),
    queryFn: () => api<AllowancePreview>(`/finance/allowance/preview?yyyymm=${alwYm}`),
  });
  const setOverlay = useMutation({
    mutationFn: (factor: number) => api(`/finance/allowance/overlay?factor=${factor}`, { method: "POST" }),
    onSuccess: () => { toast.ok("ECL 오버레이 계수 갱신"); allowance.refetch(); },
    onError: (e) => toast.err(e instanceof ApiError ? e.error.message : "오버레이 설정 실패"),
  });
  const postAllowance = useMutation({
    mutationFn: () => api(`/finance/allowance/post?yyyymm=${alwYm}`, { method: "POST" }),
    onSuccess: (d: unknown) => { const r = d as { posted: boolean; delta: number };
      toast.ok(r.posted ? `대손충당 조정 전기(차액 ${won(r.delta)})` : "대손충당 조정 없음");
      allowance.refetch(); },
    onError: (e) => toast.err(e instanceof ApiError ? e.error.message : "대손충당 전기 실패"),
  });

  // 이연법인세(K-IFRS 1012) 연말 결산조정
  const [dtYear, setDtYear] = useState("");
  const deferredTax = useQuery({
    queryKey: ["deferred-tax-preview", dtYear], enabled: /^\d{4}$/.test(dtYear),
    queryFn: () => api<DeferredTaxPreview>(`/finance/deferred-tax/preview?year=${dtYear}`),
  });
  const postDeferredTax = useMutation({
    mutationFn: () => api(`/finance/deferred-tax/post?year=${dtYear}`, { method: "POST" }),
    onSuccess: (d: unknown) => { const r = d as { posted: boolean };
      toast.ok(r.posted ? "이연법인세 조정 전기 완료" : "이연법인세 조정 없음"); deferredTax.refetch(); },
    onError: (e) => toast.err(e instanceof ApiError ? e.error.message : "이연법인세 전기 실패"),
  });

  // 변동대가 보험삭감 추정(K-IFRS 1115)
  const [vcYm, setVcYm] = useState("");
  const varConsider = useQuery({ queryKey: ["var-consider", vcYm], enabled: /^\d{4}-\d{2}$/.test(vcYm),
    queryFn: () => api<{ expected_deduction: number; current: number; delta: number;
      items: { dept_code: string; claimed: number; rate: number; expected_deduction: number }[] }>(
      `/claims/variable-consideration/preview?yyyymm=${vcYm}`) });
  const postVc = useMutation({
    mutationFn: () => api(`/claims/variable-consideration/post?yyyymm=${vcYm}`, { method: "POST" }),
    onSuccess: (d: unknown) => { const r = d as { posted: boolean; delta: number };
      toast.ok(r.posted ? `변동대가(예상삭감) 전기 ${won(r.delta)}` : "조정 없음"); varConsider.refetch(); },
    onError: (e) => toast.err(e instanceof ApiError ? e.error.message : "변동대가 전기 실패"),
  });

  // 미청구수익 발생(K-IFRS 1115)
  const [ubYm, setUbYm] = useState("");
  const [ubAmt, setUbAmt] = useState("");
  const accrueUnbilled = useMutation({
    mutationFn: () => api(`/finance/unbilled-revenue/accrue?yyyymm=${ubYm}&amount=${Number(ubAmt)}`,
      { method: "POST" }),
    onSuccess: (d: unknown) => { const r = d as { amount: number };
      toast.ok(`미청구수익 발생 ${won(r.amount)}(차기 환입)`); },
    onError: (e) => toast.err(e instanceof ApiError ? e.error.message : "발생 실패"),
  });

  const [fy, setFy] = useState(String(new Date().getFullYear() - 1));
  const closeYear = useMutation({
    mutationFn: () => api<{ net_income: number; voucher_id: number; already_closed?: boolean }>(
      `/finance/closing/${fy}`, { method: "POST" }),
    onSuccess: (d) => toast.ok(d.already_closed ? `${fy} 이미 결산됨(전표 #${d.voucher_id})`
      : `${fy} 결산분개 완료 — 당기순이익 ${won(d.net_income)} → 이익잉여금`),
    onError: (e) => toast.err(e instanceof ApiError ? e.error.message : "결산 실패"),
  });

  // 원장 무결성 검증(G11) — voucher_lines 해시 체인 재계산 대조(변조 탐지·읽기)
  const verifyIntegrity = async () => {
    try {
      const r = await api<{ ok: boolean; checkpoints: number; broken_checkpoint_id?: number }>(
        `/finance/ledger-integrity/verify`);
      if (r.ok) toast.ok(`원장 무결성 정상 — 체크포인트 ${r.checkpoints}개 검증 통과`);
      else toast.err(`원장 무결성 경보 — 체크포인트 #${r.broken_checkpoint_id} 불일치(변조 의심)`);
    } catch (e) {
      toast.err(e instanceof ApiError ? e.error.message : "검증 실패");
    }
  };

  // 마감 무결성 대사(G6·S6) — 확정 스냅샷 대비 현재 재도출 재무제표 드리프트 조회(읽기)
  const reconcile = async (yyyymm: string) => {
    try {
      const r = await api<{ snapshot_present: boolean; drifted: boolean | null;
        drifts: { statement: string; field: string; delta: number }[] }>(
        `/finance/periods/${yyyymm}/reconcile`);
      if (!r.snapshot_present) toast.info(`${yyyymm} 확정 스냅샷 없음 — 대사 불가`);
      else if (!r.drifted) toast.ok(`${yyyymm} 마감 무결성 정상 — 드리프트 없음`);
      else toast.err(`${yyyymm} 드리프트 ${r.drifts.length}건: `
        + r.drifts.map((d) => `${d.field} ${d.delta.toLocaleString()}`).join(", "));
    } catch (e) {
      toast.err(e instanceof ApiError ? e.error.message : "대사 실패");
    }
  };

  return (
    <div className="space-y-4">
      <div className="flex items-center justify-between">
        <h1 className="text-lg font-bold">재무 — 회계 기간 마감 (월 결산)</h1>
        <Button variant="neutral" size="sm" onClick={verifyIntegrity}>원장 무결성 검증</Button>
      </div>
      <QueryErrorBanner queries={[periods]} />

      <Card title="기간 마감" action={
        <span className="text-[11px] text-slate-400">마감 후 해당 월 전표 생성 차단 · 정정은 역분개</span>}>
        <div className="flex flex-wrap items-center gap-2">
          <input value={ym} onChange={(e) => setYm(e.target.value)} placeholder="YYYY-MM (예: 2026-06)"
            className="w-44 rounded-md border border-slate-300 px-3 py-1.5 text-sm" />
          <Button variant="success" disabled={monthEnd.isPending}
            onClick={() => /^\d{4}-\d{2}$/.test(ym) ? monthEnd.mutate(ym) : toast.err("YYYY-MM 형식으로 입력하세요")}>월마감 일괄(감가·충당·리스)</Button>
          <Button variant="secondary" disabled={precheck.isPending}
            onClick={() => /^\d{4}-\d{2}$/.test(ym) ? precheck.mutate(ym) : toast.err("YYYY-MM 형식으로 입력하세요")}>사전 점검</Button>
          <Button disabled={act.isPending}
            onClick={() => /^\d{4}-\d{2}$/.test(ym) ? act.mutate({ yyyymm: ym, action: "close" }) : toast.err("YYYY-MM 형식으로 입력하세요")}>기간 마감(잠금)</Button>
        </div>
        {checked && checked.yyyymm === ym && (
          <div className="mt-3">
            {checked.ok
              ? <p className="text-sm text-emerald-700">✓ {checked.yyyymm} 사전점검 통과 — 경고 없음</p>
              : <>
                  <p className="mb-1 text-sm font-medium text-amber-700">
                    {checked.yyyymm} 사전점검 경고 {checked.warnings.length}건 — 마감 전 확인</p>
                  <ul className="space-y-1">
                    {checked.warnings.map((w) => (
                      <li key={w.code} className="flex items-center gap-2 text-sm">
                        <Badge tone="amber">{w.code}</Badge>
                        <span className="text-slate-700">{w.message}</span>
                      </li>
                    ))}
                  </ul>
                </>}
          </div>
        )}
      </Card>

      <Card title="연말 결산분개 (손익→이익잉여금)" action={
        <span className="text-[11px] text-slate-400">수익·비용을 이익잉여금으로 이전 · 연도별 1전표(멱등)</span>}>
        <div className="flex items-center gap-2">
          <input value={fy} onChange={(e) => setFy(e.target.value)} placeholder="회계연도 (예: 2025)"
            className="w-40 rounded-md border border-slate-300 px-3 py-1.5 text-sm" />
          <Button disabled={closeYear.isPending}
            onClick={() => /^\d{4}$/.test(fy) ? closeYear.mutate() : toast.err("YYYY 형식으로 입력하세요")}>연말 결산</Button>
        </div>
      </Card>

      <Card title="대손충당금 결산조정 (K-IFRS 1109)" action={
        <span className="text-[11px] text-slate-400">미수금 연령×요율 = 목표충당 · 차액만 결산분개(멱등)</span>}>
        <div className="flex flex-wrap items-center gap-2">
          <input value={alwYm} onChange={(e) => setAlwYm(e.target.value)} placeholder="YYYY-MM"
            className="w-44 rounded-md border border-slate-300 px-3 py-1.5 text-sm" />
          <Button variant="neutral" onClick={() => /^\d{4}-\d{2}$/.test(alwYm) ? allowance.refetch() : toast.err("YYYY-MM 형식으로 입력하세요")}>미리보기</Button>
          <Button onClick={() => postAllowance.mutate()} disabled={!allowance.data?.delta || postAllowance.isPending}>조정 전기</Button>
        </div>
        {allowance.data && (
          <div className="mt-3 text-sm">
            <Table headers={["연령", "미수잔액", "요율", "충당액"]}
              rows={allowance.data.buckets.map((b) => [b.bucket, won(b.balance),
                `${(b.rate * 100).toFixed(1)}%`, won(b.allowance)])} empty="" />
            <p className="mt-2 text-slate-600">기본 ECL {won(allowance.data.base_ecl)} × 오버레이 <b>{allowance.data.overlay}</b> =
              목표충당 <b>{won(allowance.data.required)}</b> · 현재 {won(allowance.data.current)} ·
              조정차액 <b className={allowance.data.delta ? "text-blue-600" : "text-emerald-600"}>{won(allowance.data.delta)}</b></p>
            <div className="mt-1 flex items-center gap-1 text-xs text-slate-500">forward-looking 오버레이(경기전망):
              {[0.9, 1.0, 1.1, 1.2].map((f) => (
                <button key={f} onClick={() => setOverlay.mutate(f)}
                  className={`rounded border px-2 py-0.5 ${allowance.data?.overlay === f ? "border-blue-500 bg-blue-50 text-blue-600" : "border-slate-300"}`}>{f}</button>))}
            </div>
          </div>
        )}
      </Card>

      <Card title="이연법인세 결산조정 (K-IFRS 1012)" action={
        <span className="text-[11px] text-slate-400">일시적차이×세율 = DTA/DTL · 연말(12/31) 측정 · 멱등</span>}>
        <div className="flex flex-wrap items-center gap-2">
          <input value={dtYear} onChange={(e) => setDtYear(e.target.value)} placeholder="회계연도 (예: 2026)"
            className="w-40 rounded-md border border-slate-300 px-3 py-1.5 text-sm" />
          <Button variant="neutral" onClick={() => /^\d{4}$/.test(dtYear) ? deferredTax.refetch() : toast.err("YYYY 형식으로 입력하세요")}>미리보기</Button>
          <Button onClick={() => postDeferredTax.mutate()}
            disabled={(!deferredTax.data?.delta_dta && !deferredTax.data?.delta_dtl) || postDeferredTax.isPending}>조정 전기</Button>
        </div>
        {deferredTax.data && (
          <div className="mt-3 text-sm">
            <Table headers={["원천", "성격", "일시적차이", "세효과"]}
              rows={deferredTax.data.items.map((it) => [it.name,
                it.nature === "DTA" ? "차감(DTA)" : "가산(DTL)", won(it.temp_diff), won(it.tax_effect)])}
              empty="일시적차이 없음" />
            <p className="mt-2 text-slate-600">세율 {(deferredTax.data.rate * 100).toFixed(0)}% ·
              이연법인세자산 <b>{won(deferredTax.data.dta)}</b> · 부채 <b>{won(deferredTax.data.dtl)}</b> ·
              조정차액 DTA <b className={deferredTax.data.delta_dta ? "text-blue-600" : ""}>{won(deferredTax.data.delta_dta)}</b>
              / DTL <b className={deferredTax.data.delta_dtl ? "text-blue-600" : ""}>{won(deferredTax.data.delta_dtl)}</b></p>
            <p className="mt-1 text-[11px] text-slate-400">단일 유효세율 단순화 · 당기법인세·이월결손은 세무조정(외부) — 세무 전문 검증 권장.</p>
          </div>
        )}
      </Card>

      <Card title="미청구수익 발생 (K-IFRS 1115)" action={
        <span className="text-[11px] text-slate-400">기간말 발생(계약자산/수익) + 차기 환입 · 발생주의</span>}>
        <div className="flex flex-wrap items-center gap-2">
          <input value={ubYm} onChange={(e) => setUbYm(e.target.value)} placeholder="YYYY-MM"
            className="w-32 rounded-md border border-slate-300 px-3 py-1.5 text-sm" />
          <input value={ubAmt} onChange={(e) => setUbAmt(e.target.value.replace(/[^0-9]/g, ""))}
            placeholder="발생액(원)" className="w-40 rounded-md border border-slate-300 px-3 py-1.5 text-sm" />
          <Button disabled={accrueUnbilled.isPending}
            onClick={() => /^\d{4}-\d{2}$/.test(ubYm) && Number(ubAmt) > 0 ? accrueUnbilled.mutate() : toast.err("기간(YYYY-MM)과 금액을 확인하세요")}>발생 전기</Button>
        </div>
        <p className="mt-1 text-[11px] text-slate-400">진행률 기반 자동 산정은 HIS 진행 데이터 연동(후속) — 현재 추정액 입력.</p>
      </Card>

      <Card title="변동대가 — 보험삭감 추정 (K-IFRS 1115)" action={
        <span className="text-[11px] text-slate-400">미결정 청구 × 진료과 삭감률 = 예상삭감(수익 차감/환불부채)</span>}>
        <div className="flex flex-wrap items-center gap-2">
          <input value={vcYm} onChange={(e) => setVcYm(e.target.value)} placeholder="YYYY-MM"
            className="w-32 rounded-md border border-slate-300 px-3 py-1.5 text-sm" />
          <Button variant="neutral" onClick={() => /^\d{4}-\d{2}$/.test(vcYm) ? varConsider.refetch() : toast.err("YYYY-MM 형식으로 입력하세요")}>미리보기</Button>
          <Button onClick={() => postVc.mutate()} disabled={!varConsider.data?.delta || postVc.isPending}>조정 전기</Button>
          {varConsider.data && <span className="text-sm text-slate-600">예상삭감 <b>{won(varConsider.data.expected_deduction)}</b> · 차액 <b className={varConsider.data.delta ? "text-blue-600" : "text-emerald-600"}>{won(varConsider.data.delta)}</b></span>}
        </div>
        {varConsider.data && varConsider.data.items.length > 0 && (
          <Table headers={["진료과", "미결정 청구", "삭감률", "예상삭감"]}
            rows={varConsider.data.items.map((it) => [it.dept_code, won(it.claimed),
              `${(it.rate * 100).toFixed(1)}%`, won(it.expected_deduction)])} empty="" />
        )}
      </Card>

      <Card title="마감 현황">
        <Table headers={["기간", "상태", "총수익", "총비용", "순이익", "전표", "마감일", ""]}
          rows={(periods.data?.items ?? []).map((p) => [
            <b key="y">{p.yyyymm}</b>,
            <Badge key="s" tone={p.status === "CLOSED" ? "green" : "gray"}>
              {p.status === "CLOSED" ? "마감" : "개방"}</Badge>,
            won(p.total_revenue), won(p.total_expense),
            <span key="n" className={p.net_income != null && p.net_income < 0 ? "text-red-600" : ""}>{won(p.net_income)}</span>,
            p.voucher_count ?? "-",
            <span key="d" className="text-xs text-slate-500">{p.closed_at?.slice(0, 10) ?? "-"}</span>,
            p.status === "CLOSED"
              ? <span key="r" className="flex gap-1">
                  <Button variant="neutral" size="sm" onClick={() => reconcile(p.yyyymm)}>대사</Button>
                  <Button variant="secondary" size="sm" disabled={act.isPending}
                    onClick={() => act.mutate({ yyyymm: p.yyyymm, action: "reopen" })}>재개방</Button>
                </span>
              : "",
          ])} empty={periods.isFetching ? "불러오는 중…" : "마감 이력 없음"} />
      </Card>
    </div>
  );
}

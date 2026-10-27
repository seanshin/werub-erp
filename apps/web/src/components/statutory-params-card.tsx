"use client";
/** 법정 파라미터(연도별) — 4대보험 근로자 요율 등록·소득세 전년 승계. 매년 고시값을 코드 배포 없이 등록.
 *  미등록 연도는 급여·원천징수 계산이 멈춘다(fail-safe) — 다음 해 준비 상태를 맨 위에 보인다. */
import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { ApiError, api, auth } from "@/lib/api";
import { Badge, Button, Card, Input, Table } from "@/components/ui";
import { useToast } from "@/components/toast";
import { useConfirm } from "@/components/confirm";

type Ins = Record<string, number>;
interface YearRow {
  year: number; insurance: Ins | null; insurance_source: "db" | "code" | null;
  insurance_note: string | null; code_insurance: Ins | null;
  tax_source: string | null; tax_note: string | null; tax_overrides?: string[] | null;
}
interface Registry { years: YearRow[]; next_year: number; keys: string[]; optional_keys?: string[];
  next_year_ready: { insurance_rates: boolean; tax_params: boolean } }

const LABEL: Record<string, string> = {
  pension: "국민연금(%)", pension_min: "연금 기준소득 하한(원)", pension_max: "연금 기준소득 상한(원)",
  health: "건강보험(%)", longterm_of_health: "장기요양(건보료 대비 %)", employment: "고용보험(%)",
  pension_min_h2: "연금 하한 — 7월분부터(원·선택)", pension_max_h2: "연금 상한 — 7월분부터(원·선택)",
  employer_ei: "사업주 고용보험(%·선택)", employer_wc: "산재보험(%·업종·선택)",
  min_wage_hourly: "최저임금 시급(원·선택 — 비우면 코드 고시값)",
};
const isPct = (k: string) => !k.startsWith("pension_") && k !== "min_wage_hourly";
// 쉼표·공백 허용("410,000") — Number("410,000")은 NaN이고 JSON에선 null이 돼 선택 항목이 조용히 빠졌다(검토 LOW)
const num = (v: string | undefined) => Number(String(v ?? "").replace(/[,\s]/g, ""));
const show = (k: string, v: number | undefined) =>
  v === undefined ? "—" : isPct(k) ? `${+(v * 100).toFixed(4)}%` : v.toLocaleString("ko-KR");

export function StatutoryParamsCard() {
  const toast = useToast();
  const qc = useQueryClient();
  const confirm = useConfirm();
  const q = useQuery({ queryKey: ["statutory-params"], queryFn: () => api<Registry>("/hr/statutory-params") });
  const [year, setYear] = useState<number | null>(null);
  const [vals, setVals] = useState<Record<string, string>>({});
  const [note, setNote] = useState("");
  const reg = q.data;
  const target = year || reg?.next_year || new Date().getFullYear() + 1;   // 빈칸(0)이면 내년(G15)
  const canWrite = auth.claims()?.role === "ADMIN";                       // 쓰기=ADMIN만(G12)
  const invalidate = () => qc.invalidateQueries({ queryKey: ["statutory-params"] });
  const onErr = (e: unknown) => toast.err(e instanceof ApiError ? e.error.message : "저장 실패");

  const [warn, setWarn] = useState<string | null>(null);
  const [amendOpen, setAmendOpen] = useState(false);
  const saveIns = useMutation({
    mutationFn: () => api(`/hr/statutory-params/${target}/insurance`, { method: "PUT",
      body: JSON.stringify({ source_note: note, values: Object.fromEntries([
        ...(reg?.keys ?? []).map((k) => [k, isPct(k) ? num(vals[k]) / 100 : num(vals[k])]),
        // 선택 항목은 입력했을 때만 보낸다(빈칸=연중 같은 상·하한)
        ...(reg?.optional_keys ?? []).filter((k) => vals[k]?.trim()).map((k) => [k, isPct(k) ? num(vals[k]) / 100 : num(vals[k])]),
      ]) }) }),
    onSuccess: (r) => {
      const n = (r as { unconfirmed_payrolls?: number }).unconfirmed_payrolls ?? 0;
      toast.ok(`${target}년 4대보험 요율을 등록했습니다`); setVals({}); setNote(""); invalidate();
      // 확정 시 사업주 부담금은 그때 등록된 요율로 계산된다 — 생성 후 미확정 급여는 재생성해야 맞는다(G6)
      setWarn(n > 0 ? `${target}년 미확정 급여 ${n}건은 이전 요율로 생성돼 있습니다. 확정 전에 다시 생성하세요 `
        + "(그대로 확정하면 근로자 공제와 사업주 부담금의 요율이 달라집니다)." : null);
    },
    onError: onErr,
  });
  const carry = useMutation({
    mutationFn: (from: number) => api(`/hr/statutory-params/${target}/tax-carry`, { method: "POST",
      body: JSON.stringify({ from_year: from, source_note: note }) }),
    onSuccess: () => { toast.ok(`${target}년 소득세 파라미터를 전년 승계로 확정했습니다`); setNote(""); invalidate(); },
    onError: onErr,
  });

  // 직전 연도 값으로 입력칸 채우기(고시가 바뀐 항목만 고치게)
  const prefill = () => {
    const prevRow = reg?.years.find((y) => y.year === target - 1);
    const prev = prevRow?.insurance;
    if (!prev) return;
    const next = Object.fromEntries(Object.entries(prev).map(([k, v]) => [k, isPct(k) ? String(+(v * 100).toFixed(4)) : String(v)]));
    // 새해 1~6월분 상·하한 = 전년 7월분부터의 값(공단은 7월에 바꾼다) — 7월분부터 칸은 새 고시로 채우게 비운다
    if (prev.pension_min_h2 && prev.pension_max_h2) {
      next.pension_min = String(prev.pension_min_h2); next.pension_max = String(prev.pension_max_h2);
    }
    delete next.pension_min_h2; delete next.pension_max_h2;
    // 사업주 고용·산재 요율은 매년 고시 확인 대상 — 전년 값을 복사하지 않는다(2026 요율이 전년 복사본이었다)
    delete next.employer_ei; delete next.employer_wc; delete next.min_wage_hourly;   // 최저임금도 해마다 새 고시
    setVals(next);
  };
  const onSaveIns = async () => {
    const all = [...(reg?.keys ?? []), ...(reg?.optional_keys ?? []).filter((k) => vals[k]?.trim())];
    const bad = all.filter((k) => !Number.isFinite(num(vals[k])));
    if (bad.length) { toast.err(`숫자가 아닌 값: ${bad.map((k) => LABEL[k] ?? k).join(", ")}`); return; }
    if (await confirm({ title: `${target}년 4대보험 요율 등록`, confirmLabel: "등록",
      body: "고시 원문과 한 번 더 대조했는지 확인하세요. 등록하면 그 연도의 이후 급여 생성·확정 시 사업주 부담금·"
        + "연말정산 추정·보험료 미리보기가 새 요율을 씁니다. 이미 생성된 급여 라인은 바뀌지 않으므로 미확정분은 다시 생성해야 합니다." }))
      saveIns.mutate();
  };
  const onCarry = async () => {
    if (await confirm({ title: `${target}년 소득세 = ${target - 1}년 승계`, confirmLabel: "승계 확정", danger: true,
      body: `${target}년 귀속 근로소득 세율·공제 구간에 개정이 없음을 확인했을 때만 확정하세요. 개정이 있으면 '개정 반영 등록'으로 바뀐 항목을 넣으세요.` }))
      carry.mutate(target - 1);
  };

  const ready = reg?.next_year_ready;
  return (
    <Card title="법정 파라미터(연도별)" action={reg && (
      ready?.insurance_rates && ready?.tax_params
        ? <Badge tone="green">{reg.next_year}년 준비됨</Badge>
        : <Badge tone="red">{reg.next_year}년 미등록 — 1월 1일 급여 계산 정지</Badge>)}>
      <Table headers={["연도", "4대보험 요율", "출처", "소득세"]}
        rows={(reg?.years ?? []).map((y) => [
          y.year,
          <span key="i" className="text-xs">{y.insurance
            ? [...reg!.keys, ...(reg!.optional_keys ?? []).filter((k) => y.insurance![k] != null)]
                .map((k) => `${LABEL[k].replace(/\(.*\)/, "")} ${show(k, y.insurance![k])}`).join(" · ")
            : <span className="text-rose-600">미등록</span>}</span>,
          <span key="s" className="text-xs text-slate-500">
            {y.insurance_source === "db" ? `관리화면 — ${y.insurance_note ?? ""}` : y.insurance_source === "code" ? "코드 상수" : "—"}</span>,
          <span key="t" className="text-xs">{y.tax_source === "code" ? "코드 상수"
            : y.tax_source?.startsWith("carry:") ? `${y.tax_source.slice(6)}년 승계 — ${y.tax_note ?? ""}`
            : y.tax_source?.startsWith("amend:") ? `${y.tax_source.slice(6)}년 + 개정(${(y.tax_overrides ?? [])
                .map((k) => TAX_LABEL[k] ?? k).join(", ")}) — ${y.tax_note ?? ""}`
            : <span className="text-rose-600">미등록</span>}</span>,
        ])} />
      <div className="mt-4 space-y-3 border-t border-slate-100 pt-3">
        <div className="flex items-end gap-2">
          <Input label="등록 연도" type="number" value={target} className="w-28"
            onChange={(e) => setYear(Number(e.target.value))} />
          <Button size="sm" variant="secondary" onClick={prefill}>직전 연도 값 불러오기</Button>
        </div>
        <div className="grid gap-2 sm:grid-cols-3">
          {[...(reg?.keys ?? []), ...(reg?.optional_keys ?? [])].map((k) => (
            <Input key={k} label={LABEL[k]} inputMode="decimal" value={vals[k] ?? ""}
              onChange={(e) => setVals({ ...vals, [k]: e.target.value })} />))}
        </div>
        <Input label="근거(고시 번호·출처) — 필수" value={note} onChange={(e) => setNote(e.target.value)}
          placeholder="예: 보건복지부 고시 제2026-○○호" />
        <div className="flex flex-wrap gap-2">
          <Button size="sm" isLoading={saveIns.isPending} onClick={onSaveIns}
            disabled={!canWrite || !note.trim() || (reg?.keys ?? []).some((k) => !vals[k])}>4대보험 요율 등록</Button>
          <Button size="sm" variant="secondary" isLoading={carry.isPending} onClick={onCarry}
            disabled={!canWrite || !note.trim()}>소득세: 전년 승계 확정</Button>
          <Button size="sm" variant="secondary" disabled={!canWrite} onClick={() => setAmendOpen((v) => !v)}>
            소득세: 개정 반영 등록</Button>
        </div>
        {amendOpen && <TaxAmendPanel target={target} note={note}
          registered={!!reg?.years.find((y) => y.year === target)?.tax_source}
          onDone={() => { setAmendOpen(false); setNote(""); invalidate(); }} />}
        {warn && <p className="rounded-md bg-amber-50 p-2 text-xs text-amber-800">{warn}</p>}
        {!canWrite && <p className="text-xs text-slate-500">등록·승계는 관리자(ADMIN)만 할 수 있습니다.</p>}
        <p className="text-xs text-slate-400">
          요율은 <b>근로자 부담분</b>을 %로 입력합니다(국민연금 4.75% → 4.75). 국민연금 기준소득월액 상·하한은 매년
          7월분부터 바뀝니다 — 1~6월분 값은 기본 칸, 7~12월분 값은 &lsquo;7월분부터&rsquo; 칸에 넣으세요(비우면 연중 동일).
          등록·정정은 감사 기록됩니다.</p>
      </div>
    </Card>
  );
}

const TAX_LABEL: Record<string, string> = {
  earned_deduction: "근로소득공제 구간", earned_deduction_cap: "근로소득공제 한도", brackets: "기본세율 구간",
  eitc_threshold: "근로소득세액공제 기준(산출세액)", eitc_base: "근로소득세액공제 기준액", eitc_rate_low: "세액공제율(기준 이하)",
  eitc_rate_high: "세액공제율(기준 초과)", eitc_limits: "근로소득세액공제 한도 구간", personal_deduction: "기본공제(1인)",
  child_first: "자녀세액공제 1명", child_second: "자녀세액공제 2명(누적)", child_extra: "자녀세액공제 3명째부터(인당)",
  daily_deduction: "일용 일 공제", daily_rate: "일용 세율", daily_credit: "일용 세액공제율", daily_min_tax: "소액부징수 기준",
  retire_service_deduction: "퇴직 근속연수공제 구간", retire_conv_deduction: "퇴직 환산급여공제 구간",
};

/** 세법 개정 반영(v1.300) — 원본 연도 값을 불러와 바뀐 항목만 고쳐 등록(코드 배포 없이). 구간표는 [상한, …] 행 목록·마지막 상한 null(무한대). */
function TaxAmendPanel({ target, note, registered, onDone }: {
  target: number; note: string; registered: boolean; onDone: () => void }) {
  const toast = useToast();
  const qc = useQueryClient();
  const confirm = useConfirm();
  const from = target - 1;
  const base = useQuery({ queryKey: ["tax-params", from], retry: 0,
    queryFn: () => api<{ params: Record<string, unknown> }>(`/hr/statutory-params/tax/${from}`) });
  // 대상 연도에 이미 등록된 값(승계·개정)이 있으면 그 값에서 시작 — 두 번째 개정이 첫 개정을 덮어 보이지 않게(서버도 합친다)
  const cur = useQuery({ queryKey: ["tax-params", target], retry: 0, enabled: registered,
    queryFn: () => api<{ params: Record<string, unknown> }>(`/hr/statutory-params/tax/${target}`) });
  const [edit, setEdit] = useState<Record<string, string>>({});
  const params = cur.data?.params ?? base.data?.params ?? {};
  const kept = Object.keys(params).filter((k) => base.data && JSON.stringify(params[k]) !== JSON.stringify(base.data.params[k]));
  const shown = (k: string) => edit[k] ?? (Array.isArray(params[k]) ? JSON.stringify(params[k]) : String(params[k]));
  const changed = (): Record<string, unknown> | string => {
    const out: Record<string, unknown> = {};
    for (const k of Object.keys(edit)) {
      let v: unknown;
      if (!edit[k].trim()) return `${TAX_LABEL[k] ?? k}: 비어 있습니다`;
      try { v = Array.isArray(params[k]) ? JSON.parse(edit[k]) : num(edit[k]); } catch { return `${TAX_LABEL[k] ?? k}: JSON 형식이 아닙니다`; }
      if (typeof v === "number" && !Number.isFinite(v)) return `${TAX_LABEL[k] ?? k}: 숫자가 아닙니다`;
      if (JSON.stringify(v) !== JSON.stringify(params[k])) out[k] = v;
    }
    return out;
  };
  const save = useMutation({
    mutationFn: (overrides: Record<string, unknown>) => api(`/hr/statutory-params/${target}/tax-amend`, { method: "POST",
      body: JSON.stringify({ from_year: from, overrides, source_note: note }) }),
    onSuccess: () => { toast.ok(`${target}년 소득세 파라미터 개정분을 등록했습니다`); qc.invalidateQueries({ queryKey: ["tax-params"] }); onDone(); },
    onError: (e) => toast.err(e instanceof ApiError ? e.error.message : "등록 실패"),
  });
  const onSave = async () => {
    const c = changed();
    if (typeof c === "string") { toast.err(c); return; }
    const keys = Object.keys(c);
    if (!keys.length) { toast.err("바뀐 항목이 없습니다 — 개정이 없으면 '전년 승계'를 쓰세요"); return; }
    if (await confirm({ title: `${target}년 소득세 = ${from}년 + 개정 ${keys.length}항목`, confirmLabel: "등록", danger: true,
      body: `이번 개정 항목: ${keys.map((k) => TAX_LABEL[k] ?? k).join(", ")}`
        + (kept.filter((k) => !keys.includes(k)).length ? `\n유지되는 기존 개정: ${kept.filter((k) => !keys.includes(k)).map((k) => TAX_LABEL[k] ?? k).join(", ")}` : "")
        + `\n개정 법령 원문과 대조했는지 확인하세요. 등록하면 ${target}년 급여·연말정산·퇴직소득 계산이 이 값을 씁니다.\n근거: ${note}` }))
      save.mutate(c);
  };
  return (
    <div className="space-y-2 rounded-md border border-slate-200 p-3 text-sm">
      <p className="font-medium">{target}년 소득세 파라미터 — {from}년 값에서 바뀐 항목만 고치세요</p>
      {kept.length > 0 && <p className="text-xs text-amber-700">이미 등록된 개정: {kept.map((k) => TAX_LABEL[k] ?? k).join(", ")}
        (원본 값으로 되돌리면 개정에서 빠집니다)</p>}
      {base.isError && <p className="text-xs text-rose-600">{base.error instanceof ApiError ? base.error.error.message : "불러오기 실패"}</p>}
      <div className="grid gap-2 sm:grid-cols-2">
        {Object.keys(params).map((k) => Array.isArray(params[k]) ? (
          <label key={k} className="block text-xs text-slate-600 sm:col-span-2">{TAX_LABEL[k] ?? k}
            <textarea value={shown(k)} rows={2} onChange={(e) => setEdit({ ...edit, [k]: e.target.value })}
              className={`w-full rounded-md border px-2 py-1 font-mono text-xs ${edit[k] !== undefined && edit[k] !== JSON.stringify(params[k]) ? "border-amber-500" : "border-slate-300"}`} />
          </label>) : (
          <Input key={k} label={TAX_LABEL[k] ?? k} inputMode="decimal" value={shown(k)}
            onChange={(e) => setEdit({ ...edit, [k]: e.target.value })} />))}
      </div>
      <p className="text-xs text-slate-400">구간표 행 = [구간 상한, …](마지막 상한은 null=무한대). 율은 소수(55% → 0.55). 근거는 위 &lsquo;근거&rsquo; 칸에 입력합니다.</p>
      <Button size="sm" disabled={save.isPending || !note.trim() || !base.data || (registered && cur.isLoading)} onClick={onSave}>개정 반영 등록</Button>
    </div>
  );
}

interface RateDiff {
  yyyymm: string; records: number; affected: number; skipped: number; employee_diff_total: number;
  totals: Record<string, { recorded: number; recalculated: number; diff: number }>;
  rows: { employee_no: string; base: number; diff: Record<string, number>; total: number;
    confirmed: boolean; paid_on: string | null }[];
  note: string;
}
const CODE_LABEL: Record<string, string> = { NP: "국민연금", HI: "건강보험", LTC: "장기요양", EI: "고용보험" };
const won = (n: number) => `${n > 0 ? "+" : ""}${n.toLocaleString("ko-KR")}원`;

/** 요율 정정 후 차액 미리보기 — 이미 생성·확정된 급여의 공제 ↔ 현재 등록 요율 재계산(읽기 전용).
 *  2026-10-10: 2026 코드 요율이 2025 복사본이라 2026-07 급여가 낮은 요율로 지급됐다. 소급 정산 여부는
 *  결정 대기함에서 정하되, 정하려면 직원별 차액이 보여야 한다. 급여·전표는 바뀌지 않는다. */
export function RateDiffCard() {
  const [ym, setYm] = useState("");
  const [asked, setAsked] = useState<string | null>(null);
  const q = useQuery({ queryKey: ["rate-diff", asked], enabled: !!asked,
    queryFn: () => api<RateDiff>(`/hr/statutory-params/rate-diff?yyyymm=${asked}`) });
  const d = q.data;
  return (
    <Card title="요율 차액 미리보기(생성·확정 급여)">
      <p className="mb-2 text-xs text-slate-500">
        이미 만든 급여의 4대보험 공제를 <b>현재 등록된 요율</b>로 다시 계산해 차액을 보여줍니다. 급여·전표는 바뀌지 않습니다 —
        처리는 아래 「요율 차액 정산」에서 방식을 골라 실행하세요.</p>
      <div className="flex items-end gap-2">
        <Input label="급여월(YYYY-MM)" value={ym} placeholder="2026-07" className="w-36"
          onChange={(e) => setYm(e.target.value.trim())} />
        <Button size="sm" disabled={!/^\d{4}-(0[1-9]|1[0-2])$/.test(ym)} onClick={() => setAsked(ym)}>조회</Button>
      </div>
      {q.isError && <p className="mt-2 text-xs text-rose-600">{q.error instanceof ApiError ? q.error.error.message : "조회 실패"}</p>}
      {d && (
        <div className="mt-3 space-y-2">
          <p className="text-sm">{d.yyyymm} 급여 {d.records}건 중 <b>{d.affected}건</b> 차액
            {d.skipped > 0 && <span className="text-slate-500"> (공제 없음 {d.skipped}건 제외)</span>} · 근로자 공제 차액 합계{" "}
            <b className={d.employee_diff_total > 0 ? "text-rose-700" : ""}>{won(d.employee_diff_total)}</b></p>
          <Table headers={["항목", "기록(공제)", "현재 요율 재계산", "차액"]}
            rows={Object.entries(d.totals).map(([c, t]) => [CODE_LABEL[c] ?? c, t.recorded.toLocaleString("ko-KR"),
              t.recalculated.toLocaleString("ko-KR"), won(t.diff)])} />
          {d.rows.length > 0 && (
            <Table headers={["사번", "보수월액", "항목별 차액", "합계", "상태"]}
              rows={d.rows.map((r) => [r.employee_no, r.base.toLocaleString("ko-KR"),
                <span key="d" className="text-xs">{Object.entries(r.diff).map(([c, v]) => `${CODE_LABEL[c] ?? c} ${won(v)}`).join(" · ")}</span>,
                won(r.total),
                r.confirmed ? <Badge key="s" tone="amber">{r.paid_on ? `지급 ${r.paid_on}` : "확정"}</Badge> : <Badge key="s">미확정</Badge>])} />)}
          <p className="text-xs text-slate-400">{d.note}</p>
        </div>)}
    </Card>
  );
}

/** 요율 차액 정산 — 결정 대기함의 '지급분 차액 처리'를 방식 선택=실행으로(v1.295.0).
 *  미리보기(직원별 처분·전표 금액) → 사유 → 확인 → 실행. 결정 대기함 기록도 같은 트랜잭션에서 남는다.
 *  공제가 확정되기 전까지는 취소(전표 역분개·공제 줄 제거·결정 재오픈) 가능. */
type Method = "NEXT_PAYROLL_ADJUST" | "EMPLOYER_ABSORB" | "NOT_PAYROLL_SOT";
const METHOD_LABEL: Record<Method, string> = {
  NEXT_PAYROLL_ADJUST: "다음 급여에서 차액 정산(직원 추가 공제)",
  EMPLOYER_ABSORB: "근로자분 차액을 기관이 부담(전표)",
  NOT_PAYROLL_SOT: "ERP 급여는 지급 원천 아님(기록만)",
};
const DISP_LABEL: Record<string, string> = { DEDUCT: "다음 급여 공제", ABSORB: "기관 부담", RECORD: "기록만" };
interface Settlement {
  id: number; source_yyyymm: string; method: Method; target_yyyymm: string | null; status: string;
  note: string; employee_total: number; employer_total: number; absorbed_total: number;
  voucher_ref: string | null; created_at: string; canceled_at: string | null; cancel_reason: string | null;
  items?: number; deduct?: number; applied?: number; pending?: number; target_month_ended?: boolean;
}
interface SettleOverview {
  source_yyyymm: string; settlements: Settlement[];
  decision: { key: string; title: string; status: string; decided_option: string | null;
    options: { key: string; label: string; recommended: boolean }[] } | null;
}
interface SettlePlan {
  method: Method; target_yyyymm: string | null; affected: number; employee_total: number; employer_total: number;
  absorbed_total: number; voucher_amount: number; employer_correction: number; employer_posted_records: number;
  deduct_count: number;
  absorb_count: number; blocked: string | null; active: Settlement | null;
  items: { employee_no: string; diff: Record<string, number>; total: number; disposition: string; resigned_on: string | null }[];
}
const YM_RE = /^\d{4}-(0[1-9]|1[0-2])$/;

export function RateSettlementCard() {
  const toast = useToast();
  const qc = useQueryClient();
  const confirm = useConfirm();
  const [src, setSrc] = useState("2026-07");
  const [asked, setAsked] = useState<string | null>(null);
  const [method, setMethod] = useState<Method>("NEXT_PAYROLL_ADJUST");
  const [target, setTarget] = useState("");
  const [note, setNote] = useState("");
  const [cancelReason, setCancelReason] = useState("");
  const ov = useQuery({ queryKey: ["rate-settlement", asked], enabled: !!asked,
    queryFn: () => api<SettleOverview>(`/hr/statutory-params/rate-settlement?yyyymm=${asked}`) });
  const needsTarget = method === "NEXT_PAYROLL_ADJUST";
  const planReady = !!asked && (!needsTarget || YM_RE.test(target));
  const plan = useQuery({ queryKey: ["rate-settlement-plan", asked, method, needsTarget ? target : ""], enabled: planReady,
    queryFn: () => api<SettlePlan>(`/hr/statutory-params/rate-settlement/preview?yyyymm=${asked}&method=${method}` +
      (needsTarget ? `&target_yyyymm=${target}` : "")) });
  const refresh = () => {
    qc.invalidateQueries({ queryKey: ["rate-settlement"] });
    qc.invalidateQueries({ queryKey: ["rate-settlement-plan"] });
    qc.invalidateQueries({ queryKey: ["decisions"] });
  };
  const onErr = (e: unknown) => toast.err(e instanceof ApiError ? e.error.message : "처리 실패");
  const exec = useMutation({
    mutationFn: () => api<Settlement & { deduct: number; absorb: number; voucher_amount: number }>(
      "/hr/statutory-params/rate-settlement", { method: "POST", body: JSON.stringify({
        source_yyyymm: asked, method, target_yyyymm: needsTarget ? target : null, note: note.trim() }) }),
    onSuccess: (r) => { toast.ok(`${r.source_yyyymm} 정산 실행 — 공제 ${r.deduct}명·기관 부담 ${r.absorb}명·전표 ${r.voucher_amount.toLocaleString("ko-KR")}원`);
      setNote(""); refresh(); },
    onError: onErr,
  });
  const cancel = useMutation({
    mutationFn: (id: number) => api(`/hr/statutory-params/rate-settlement/${id}/cancel`,
      { method: "POST", body: JSON.stringify({ reason: cancelReason.trim() }) }),
    onSuccess: () => { toast.ok("정산을 취소했습니다 — 결정은 대기 상태로 돌아갔습니다"); setCancelReason(""); refresh(); },
    onError: onErr,
  });
  const absorb = useMutation({
    mutationFn: (id: number) => api<{ employees: number; absorbed_now: number }>(
      `/hr/statutory-params/rate-settlement/${id}/absorb-remaining`,
      { method: "POST", body: JSON.stringify({ reason: cancelReason.trim() }) }),
    onSuccess: (r) => { toast.ok(`미공제분 ${r.employees}명 ${r.absorbed_now.toLocaleString("ko-KR")}원을 기관 부담 전표로 처리했습니다`);
      setCancelReason(""); refresh(); },
    onError: onErr,
  });
  const onAbsorb = async (s: Settlement) => {
    if (await confirm({ title: "남은 공제분 기관 부담", danger: true, confirmLabel: "처리",
      body: `#${s.id} 정산에서 ${s.target_yyyymm} 급여가 없어 공제되지 못한 직원분을 기관 부담 전표로 돌립니다.\n공제월 미확정 급여가 남은 직원은 제외됩니다.` })) absorb.mutate(s.id);
  };
  const o = ov.data;
  const active = o?.settlements.find((s) => s.status === "ACTIVE") ?? null;
  const p = plan.data;
  const onExec = async () => {
    if (!p || !asked) return;
    const lines = [`원 급여월 ${asked} · ${METHOD_LABEL[method]}`,
      needsTarget ? `공제 급여월 ${target}: ${p.deduct_count}명 · 퇴사로 기관 부담 ${p.absorb_count}명` : `대상 ${p.affected}명`,
      `근로자분 차액 ${p.employee_total.toLocaleString("ko-KR")}원 · 전표 ${p.voucher_amount.toLocaleString("ko-KR")}원`,
      o?.decision ? `결정·조치함 '${o.decision.title}'도 이 방식으로 기록됩니다` : ""].filter(Boolean);
    if (await confirm({ title: "요율 차액 정산 실행", body: lines.join("\n"), confirmLabel: "실행", danger: true })) exec.mutate();
  };
  const onCancel = async (s: Settlement) => {
    if (await confirm({ title: "정산 취소", danger: true, confirmLabel: "취소 실행",
      body: `#${s.id} 정산을 취소합니다.\n전표 역분개 · 미확정 급여의 소급정산 공제 줄 제거 · 결정 재오픈` })) cancel.mutate(s.id);
  };
  return (
    <Card title="요율 차액 정산(결정=실행)">
      <p className="mb-2 text-xs text-slate-500">
        낮은 요율로 확정·지급된 급여월의 4대보험 차액을 처리합니다. 방식을 고르고 미리보기를 확인한 뒤 실행하면 급여 공제 줄·전표·결정
        대기함 기록이 함께 바뀝니다. 공제가 확정되기 전까지는 취소할 수 있습니다.</p>
      <div className="flex items-end gap-2">
        <Input label="원 급여월(YYYY-MM)" value={src} className="w-36" onChange={(e) => setSrc(e.target.value.trim())} />
        <Button size="sm" disabled={!YM_RE.test(src)} onClick={() => setAsked(src)}>불러오기</Button>
      </div>
      {ov.isError && <p className="mt-2 text-xs text-rose-600">{ov.error instanceof ApiError ? ov.error.error.message : "조회 실패"}</p>}
      {o && (
        <div className="mt-3 space-y-3 text-sm">
          {o.decision && (
            <p>결정·조치함: <b>{o.decision.title}</b>{" "}
              <Badge tone={o.decision.status === "DECIDED" ? "green" : "amber"}>
                {o.decision.status === "DECIDED" ? `결정됨 — ${o.decision.options.find((x) => x.key === o.decision?.decided_option)?.label ?? o.decision.decided_option}` : "결정 대기"}
              </Badge></p>)}
          {active ? (
            <div className="space-y-2 rounded-md border border-emerald-200 bg-emerald-50 p-3">
              <p>실행됨 #{active.id}: <b>{METHOD_LABEL[active.method]}</b>{active.target_yyyymm && ` → ${active.target_yyyymm} 급여`}
                {" "}· 근로자분 {won(active.employee_total)} · 기관 부담 {active.absorbed_total.toLocaleString("ko-KR")}원
                {active.voucher_ref && ` · 전표 ${active.voucher_ref}`}</p>
              {active.method === "NEXT_PAYROLL_ADJUST" && (
                <div className="space-y-1">
                  <p className="text-xs text-slate-600">공제 대상 {active.deduct}명 중 확정 {active.applied}명
                    {!!active.pending && (active.target_month_ended
                      ? <> · <b className="text-amber-700">미적용 {active.pending}명</b></>
                      : <> · 확정 대기 {active.pending}명({active.target_yyyymm} 급여 생성·확정 시 공제)</>)}</p>
                  {!!active.pending && active.target_month_ended && (
                    <p className="text-xs text-slate-500">실행 뒤 퇴사·휴직 등으로 {active.target_yyyymm} 급여가 없는 직원은 공제가 붙지 않습니다 —
                      공제월 급여 확정 후 아래 사유를 적고 &lsquo;남은 공제분 기관 부담&rsquo;으로 처리하세요.</p>)}
                </div>)}
              <p className="text-xs text-slate-600">근거: {active.note}</p>
              <div className="flex items-end gap-2">
                <Input label="사유(취소·미공제분 처리)" value={cancelReason} className="w-72" onChange={(e) => setCancelReason(e.target.value)} />
                {active.method === "NEXT_PAYROLL_ADJUST" && !!active.pending && active.target_month_ended && (
                  <Button size="sm" variant="secondary" disabled={absorb.isPending || cancelReason.trim().length < 4}
                    onClick={() => onAbsorb(active)}>남은 공제분 기관 부담</Button>)}
                <Button size="sm" variant="danger" disabled={cancel.isPending || cancelReason.trim().length < 4}
                  onClick={() => onCancel(active)}>정산 취소</Button>
              </div>
            </div>
          ) : (
            <div className="space-y-2">
              <div className="space-y-1">
                {(Object.keys(METHOD_LABEL) as Method[]).map((m) => (
                  <label key={m} className={`flex cursor-pointer gap-2 rounded-md border px-3 py-1.5 ${method === m ? "border-blue-500 bg-blue-50" : "border-slate-200"}`}>
                    <input type="radio" name="settle-method" checked={method === m} onChange={() => setMethod(m)} />
                    <span>{METHOD_LABEL[m]}{o.decision?.options.find((x) => x.key === m)?.recommended && <> <Badge tone="green">권장</Badge></>}</span>
                  </label>))}
              </div>
              {needsTarget && (
                <Input label="공제할 급여월(YYYY-MM)" value={target} placeholder="2026-10" className="w-40"
                  onChange={(e) => setTarget(e.target.value.trim())} />)}
              {plan.isError && <p className="text-xs text-rose-600">{plan.error instanceof ApiError ? plan.error.error.message : "미리보기 실패"}</p>}
              {p && (
                <div className="space-y-2">
                  <p>대상 {p.affected}명 · 근로자분 차액 <b>{won(p.employee_total)}</b>
                    {needsTarget && <> · 다음 급여 공제 {p.deduct_count}명 · 퇴사로 기관 부담 {p.absorb_count}명</>}
                    {" "}· 전표 <b>{p.voucher_amount.toLocaleString("ko-KR")}원</b>
                    {p.voucher_amount > 0 && <span className="text-xs text-slate-500"> (DR 복리후생비 5330 / CR 미지급금 2110)</span>}</p>
                  {method !== "NOT_PAYROLL_SOT" && (
                    <p className="text-xs text-slate-500">사업주분 보정 {p.employer_correction.toLocaleString("ko-KR")}원 — {asked} 사업주 부담 전표가
                      실제로 있던 {p.employer_posted_records}명분만(전표가 없던 급여는 보정할 것이 없음)</p>)}
                  {method === "EMPLOYER_ABSORB" &&
                    <p className="text-xs text-amber-700">기관이 대신 내는 근로자분은 근로소득 과세 여부를 세무 담당과 확인하세요.</p>}
                  {p.blocked && <p className="text-xs text-rose-600">{p.blocked}</p>}
                  <Table headers={["사번", "항목별 차액", "합계", "처분"]}
                    rows={p.items.slice(0, 50).map((i) => [i.employee_no,
                      <span key="d" className="text-xs">{Object.entries(i.diff).map(([c, v]) => `${CODE_LABEL[c] ?? c} ${won(v)}`).join(" · ")}</span>,
                      won(i.total),
                      <Badge key="b" tone={i.disposition === "DEDUCT" ? "blue" : i.disposition === "ABSORB" ? "amber" : "gray"}>
                        {DISP_LABEL[i.disposition] ?? i.disposition}{i.resigned_on ? ` (퇴사 ${i.resigned_on})` : ""}</Badge>])} />
                  {p.items.length > 50 && <p className="text-xs text-slate-400">외 {p.items.length - 50}명</p>}
                  <div className="flex items-end gap-2">
                    <Input label="실행 근거(결정 기록에 남음)" value={note} className="w-96" placeholder="예: 인사위원회 10/12 — 10월 급여에서 정산·직원 안내 완료"
                      onChange={(e) => setNote(e.target.value)} />
                    <Button size="sm" disabled={exec.isPending || !!p.blocked || p.affected === 0 || note.trim().length < 4}
                      onClick={onExec}>실행</Button>
                  </div>
                </div>)}
            </div>)}
          {o.settlements.filter((s) => s.status === "CANCELED").length > 0 && (
            <p className="text-xs text-slate-400">취소 이력: {o.settlements.filter((s) => s.status === "CANCELED")
              .map((s) => `#${s.id} ${METHOD_LABEL[s.method]} (${s.cancel_reason ?? ""})`).join(" · ")}</p>)}
        </div>)}
    </Card>
  );
}

/** 근로소득 간이세액표(소득세법 시행령 별표 2) — 후보 표를 홈택스 원표와 대조해 활성화(v1.297.0).
 *  2026-10-10: 월 원천징수가 표가 아닌 근사 산식이라 표보다 더 떼일 수 있었다. 표는 법정 데이터라
 *  사람이 표본 칸을 원표와 맞춰 보고 근거를 적어 활성화한 것만 급여 계산에 쓴다(활성 전엔 '근사 산식'으로 표기). */
interface WTable {
  id: number; effective_from: string; status: string; title: string; source_note: string | null;
  row_count: number; checksum: string; activated_at: string | null; samples: number[][];
  child_deduction: { one: number; two: number; extra: number };
}
interface WImpact {
  yyyymm: string; records: number; diff_total: number; mean: number; min: number; max: number; note: string;
  rows: { employee_no: string; taxable: number; family: number; children: number; recorded: number; table: number; diff: number }[];
}
interface WRetax {
  table_id: number; targets: number; diff_total: number; skipped_count: number; note: string;
  rows: { employee_no: string; yyyymm: string; taxable: number; family: number; recorded_family: number | null;
    children: number; recorded: number; new: number; diff: number }[];
  skipped: { employee_no: string; yyyymm: string; reason: string }[];
}
const WT_TONE: Record<string, "green" | "amber" | "gray"> = { ACTIVE: "green", CANDIDATE: "amber", RETIRED: "gray" };
const WT_LABEL: Record<string, string> = { ACTIVE: "활성(계산 사용)", CANDIDATE: "후보(대조 전)", RETIRED: "폐기" };

export function WithholdingTableCard() {
  const toast = useToast();
  const qc = useQueryClient();
  const confirm = useConfirm();
  const [note, setNote] = useState("");
  const [ym, setYm] = useState("");
  const [impactFor, setImpactFor] = useState<{ id: number; ym: string } | null>(null);
  const q = useQuery({ queryKey: ["withholding-tables"], queryFn: () => api<{ items: WTable[] }>("/hr/withholding-tables") });
  const imp = useQuery({ queryKey: ["withholding-impact", impactFor], enabled: !!impactFor, retry: 0,
    queryFn: () => api<WImpact>(`/hr/withholding-tables/${impactFor!.id}/impact?yyyymm=${impactFor!.ym}`) });
  const act = useMutation({
    mutationFn: (id: number) => api(`/hr/withholding-tables/${id}/activate`, { method: "POST",
      body: JSON.stringify({ source_note: note.trim() }) }),
    onSuccess: () => { toast.ok("간이세액표를 활성화했습니다 — 이후 생성하는 급여부터 표로 계산합니다"); setNote("");
      qc.invalidateQueries({ queryKey: ["withholding-tables"] }); },
    onError: (e) => toast.err(e instanceof ApiError ? e.error.message : "활성화 실패"),
  });
  const drop = useMutation({
    mutationFn: (id: number) => api(`/hr/withholding-tables/${id}/discard`, { method: "POST",
      body: JSON.stringify({ reason: note.trim() }) }),
    onSuccess: () => { toast.ok("후보 표를 폐기했습니다 — 계산은 그대로입니다"); setNote("");
      qc.invalidateQueries({ queryKey: ["withholding-tables"] }); qc.invalidateQueries({ queryKey: ["decisions"] }); },
    onError: (e) => toast.err(e instanceof ApiError ? e.error.message : "폐기 실패"),
  });
  // 활성화 전에 만든 미확정 급여 — 소득세·지방소득세 줄만 표로 재계산(v1.299·결정·조치함 항목의 처리 화면)
  const [retaxFor, setRetaxFor] = useState<number | null>(null);
  const [retaxNote, setRetaxNote] = useState("");
  const rtx = useQuery({ queryKey: ["withholding-retax", retaxFor], enabled: retaxFor !== null, retry: 0,
    refetchOnWindowFocus: false,
    queryFn: () => api<WRetax>(`/hr/withholding-tables/${retaxFor}/retax-preview`) });
  const doRetax = useMutation({
    mutationFn: (id: number) => api<WRetax>(`/hr/withholding-tables/${id}/retax`, { method: "POST",
      body: JSON.stringify({ note: retaxNote.trim() }) }),
    onSuccess: (r) => { toast.ok(`미확정 급여 ${r.targets}건 소득세를 재계산했습니다`); setRetaxNote("");
      qc.invalidateQueries({ queryKey: ["withholding-retax"] }); qc.invalidateQueries({ queryKey: ["withholding-impact"] });
      qc.invalidateQueries({ queryKey: ["decisions"] }); },
    onError: (e) => toast.err(e instanceof ApiError ? e.error.message : "재계산 실패"),
  });
  const onRetax = async (id: number, p: WRetax) => {
    if (await confirm({ title: `미확정 급여 ${p.targets}건 소득세 재계산`, confirmLabel: "재계산", danger: true,
      body: `소득세·지방소득세 줄만 활성 간이세액표로 바꿉니다(지급·다른 공제 줄·확정분은 그대로).\n합계 차이: ${won(p.diff_total)}\n사유: ${retaxNote.trim()}` }))
      doRetax.mutate(id);
  };
  const items = q.data?.items ?? [];
  const active = items.find((t) => t.status === "ACTIVE");
  const onDiscard = async (t: WTable) => {
    if (await confirm({ title: `간이세액표 ${t.effective_from} 후보 폐기`, confirmLabel: "폐기", danger: true,
      body: `원표와 맞지 않는 후보를 폐기합니다(되돌릴 수 없음 — 올바른 표는 다음 릴리즈로 다시 들어옵니다. 그때까지 근사 산식).\n사유: ${note.trim()}` }))
      drop.mutate(t.id);
  };
  const onActivate = async (t: WTable) => {
    if (await confirm({ title: `간이세액표 ${t.effective_from} 시행분 활성화`, confirmLabel: "활성화", danger: true,
      body: `표본 칸을 홈택스 원표와 대조했는지 확인하세요.\n활성화하면 ${t.effective_from} 이후 급여월부터 생성하는 급여의 소득세가 이 표로 계산됩니다.\n대조 근거: ${note.trim()}` }))
      act.mutate(t.id);
  };
  return (
    <Card title="근로소득 간이세액표(월 원천징수)">
      <p className="mb-2 text-xs text-slate-500">
        월 소득세는 국세청 간이세액표(소득세법 시행령 별표 2)로 원천징수합니다. 아래 후보 표의 <b>표본 칸을 홈택스 원표와 대조</b>한 뒤
        근거를 적어 활성화하세요. 활성 표가 없으면 종전 근사 산식으로 계산하며 명세서에 &lsquo;근사 산식&rsquo;으로 표시됩니다.</p>
      {!active && <p className="mb-2 text-xs font-medium text-rose-700">활성 간이세액표 없음 — 현재 근사 산식으로 계산 중</p>}
      {q.isError && <p className="text-xs text-rose-600">{q.error instanceof ApiError ? q.error.error.message : "조회 실패"}</p>}
      <div className="space-y-4">
        {items.map((t) => (
          <div key={t.id} className="space-y-2 rounded-md border border-slate-200 p-3 text-sm">
            <p><Badge tone={WT_TONE[t.status] ?? "gray"}>{WT_LABEL[t.status] ?? t.status}</Badge>{" "}
              <b>{t.effective_from} 시행</b> · {t.title} · {t.row_count}행 · 체크섬 {t.checksum}</p>
            <p className="text-xs text-slate-500">자녀(8~20세) 차감: 1명 {t.child_deduction.one.toLocaleString("ko-KR")}원 · 2명{" "}
              {t.child_deduction.two.toLocaleString("ko-KR")}원 · 3명 이상 +{t.child_deduction.extra.toLocaleString("ko-KR")}원/명
              {t.source_note && <> · 대조 근거: {t.source_note}</>}</p>
            <Table headers={["월급여(천원) 이상~미만", "가족 1명", "2명", "3명", "4명", "5명"]}
              rows={t.samples.map((r) => [r[0] === r[1] ? `${r[0].toLocaleString("ko-KR")} (정확값)` :
                `${r[0].toLocaleString("ko-KR")} ~ ${r[1].toLocaleString("ko-KR")}`,
                ...r.slice(2, 7).map((v) => v.toLocaleString("ko-KR"))])} />
            <div className="flex flex-wrap items-end gap-2">
              <Input label="영향 미리보기 급여월" value={ym} placeholder="2026-07"
                className="w-32" onChange={(e) => setYm(e.target.value.trim())} />
              <Button size="sm" variant="secondary" disabled={!/^\d{4}-(0[1-9]|1[0-2])$/.test(ym)}
                onClick={() => setImpactFor({ id: t.id, ym })}>미리보기</Button>
              {t.status === "CANDIDATE" && (<>
                <Input label="대조 근거 · 폐기 사유(필수)" value={note} className="w-80" placeholder="예: 홈택스 2026.3.1 시행 원표 표본 10칸 대조 일치"
                  onChange={(e) => setNote(e.target.value)} />
                <Button size="sm" disabled={act.isPending || note.trim().length < 8} onClick={() => onActivate(t)}>활성화</Button>
                <Button size="sm" variant="danger" disabled={drop.isPending || note.trim().length < 8}
                  onClick={() => onDiscard(t)}>후보 폐기</Button>
              </>)}
            </div>
            {impactFor?.id === t.id && imp.isError && (
              <p className="text-xs text-rose-600">{imp.error instanceof ApiError ? imp.error.error.message : "미리보기 실패"}</p>)}
            {impactFor?.id === t.id && imp.data && (
              <div className="space-y-1">
                <p>{imp.data.yyyymm} 급여 {imp.data.records}건 · 기록 소득세 대비 표 세액 차이 합계{" "}
                  <b className={imp.data.diff_total < 0 ? "text-rose-700" : ""}>{won(imp.data.diff_total)}</b>
                  {" "}(1인 평균 {won(imp.data.mean)} · {won(imp.data.min)} ~ {won(imp.data.max)})</p>
                <Table headers={["사번", "월급여(과세)", "가족", "자녀", "기록", "표", "차이"]}
                  rows={imp.data.rows.slice(0, 30).map((r) => [r.employee_no, r.taxable.toLocaleString("ko-KR"), r.family, r.children,
                    r.recorded.toLocaleString("ko-KR"), r.table.toLocaleString("ko-KR"), won(r.diff)])} />
                {imp.data.rows.length > 30 && <p className="text-xs text-slate-400">외 {imp.data.rows.length - 30}명</p>}
                <p className="text-xs text-slate-400">{imp.data.note}</p>
              </div>)}
            {t.status === "ACTIVE" && (
              <div className="space-y-1 border-t border-slate-100 pt-2">
                <div className="flex flex-wrap items-end gap-2">
                  <Button size="sm" variant="secondary" onClick={() => (retaxFor === t.id ? void rtx.refetch() : setRetaxFor(t.id))}>
                    미확정 급여 세액 재계산 미리보기</Button>
                  <span className="text-xs text-slate-500">활성화 전에 만든 미확정 급여는 근사 산식 소득세로 남아 있습니다.</span>
                </div>
                {retaxFor === t.id && rtx.isError && (
                  <p className="text-xs text-rose-600">{rtx.error instanceof ApiError ? rtx.error.error.message : "미리보기 실패"}</p>)}
                {retaxFor === t.id && rtx.data && (rtx.data.targets === 0 && rtx.data.skipped_count === 0 ? (
                  <p className="text-xs text-emerald-700">재계산할 미확정 급여가 없습니다 — 모두 표 기준입니다.</p>) : (
                  <div className="space-y-1">
                    <p>대상 {rtx.data.targets}건 · 소득세+지방소득세 차이 합계{" "}
                      <b className={rtx.data.diff_total < 0 ? "text-rose-700" : ""}>{won(rtx.data.diff_total)}</b></p>
                    {rtx.data.rows.length > 0 && (
                      <Table headers={["사번", "급여월", "월급여(과세)", "가족", "자녀", "기록", "표", "차이"]}
                        rows={rtx.data.rows.slice(0, 30).map((r) => [r.employee_no, r.yyyymm, r.taxable.toLocaleString("ko-KR"),
                          r.recorded_family != null && r.recorded_family !== r.family ? `${r.family} (기록 ${r.recorded_family})` : r.family,
                          r.children, r.recorded.toLocaleString("ko-KR"), r.new.toLocaleString("ko-KR"), won(r.diff)])} />)}
                    {rtx.data.rows.length > 30 && <p className="text-xs text-slate-400">외 {rtx.data.rows.length - 30}건</p>}
                    {rtx.data.skipped_count > 0 && (
                      <p className="text-xs text-amber-700">제외 {rtx.data.skipped_count}건(사유대로 개별 처리 — 확정하면 목록에서 빠짐):{" "}
                        {rtx.data.skipped.slice(0, 10).map((x) => `${x.employee_no} ${x.yyyymm} — ${x.reason}`).join(" · ")}</p>)}
                    <p className="text-xs text-slate-400">{rtx.data.note}</p>
                    {rtx.data.targets > 0 && (
                      <div className="flex flex-wrap items-end gap-2">
                        <Input label="재계산 사유(필수)" value={retaxNote} className="w-80"
                          placeholder="예: 2026-03-01 표 활성화 후 미확정분 반영" onChange={(e) => setRetaxNote(e.target.value)} />
                        <Button size="sm" disabled={doRetax.isPending || retaxNote.trim().length < 4}
                          onClick={() => onRetax(t.id, rtx.data!)}>재계산</Button>
                      </div>)}
                  </div>))}
              </div>)}
          </div>))}
      </div>
    </Card>
  );
}

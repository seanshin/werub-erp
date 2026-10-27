"use client";
/** 인사 — 급여 법정처리 (FR-07 확장): 4대보험·퇴직금·연말정산. */
import { localISO } from "@/lib/dates";
import { useState } from "react";
import { useQuery } from "@tanstack/react-query";

interface Recon {
  yyyymm: string; employee_id: number;
  payslip_earning: number; gl_salary_exp: number;
  payslip_deduction: number; gl_withholding: number;
  earning_matched: boolean; deduction_matched: boolean; balanced: boolean;
}
import { ApiError, api, apiDownload } from "@/lib/api";
import { Badge, Card, Table } from "@/components/ui";
import { QueryErrorBanner } from "@/components/query-guard";
import type { EmployeeRef } from "@/types";
import { won as fmtWon } from "@/lib/format";

export default function StatutoryPage() {
  const now = new Date();
  const [empId, setEmpId] = useState<number | null>(null);
  const [yyyymm, setYyyymm] = useState(localISO(now).slice(0, 7));
  // 연말정산은 이듬해 1~2월에 하므로 1~3월엔 기본 귀속연도=전년(1월에 열면 올해=미확정·미등록 연도라 오류)
  const [year, setYear] = useState(now.getMonth() < 3 ? now.getFullYear() - 1 : now.getFullYear());
  // null = 서버가 부양가족 등록(8~20세)에서 셈(준비 화면과 같은 값) — 숫자를 넣으면 그 값으로(가족−1 이하)
  const [children, setChildren] = useState<number | null>(null);

  const emps = useQuery({ queryKey: ["emps-stat"],
    queryFn: () => api<{ items: EmployeeRef[] }>("/hr/employees?size=200") });
  const ins = useQuery({ enabled: !!empId, queryKey: ["ins", empId, yyyymm],
    queryFn: () => api<Record<string, number>>(`/hr/payrolls/${yyyymm}/insurance?employee_id=${empId}`) });
  // 명세↔GL 대사 — 서비스 docstring이 "불일치=발행 게이트 경고 근거"라 명시했는데 화면 호출이 없었다.
  // 명세를 확정·교부하기 전 전표 정합을 확인할 유일한 수단이다.
  const recon = useQuery({ enabled: !!empId, retry: 0,
    queryKey: ["pay-recon", empId, yyyymm],
    queryFn: () => api<Recon>(`/hr/payrolls/${yyyymm}/reconcile?employee_id=${empId}`) });
  const sev = useQuery({ enabled: !!empId, queryKey: ["sev", empId],
    queryFn: () => api<Record<string, number | boolean>>(
      `/hr/employees/${empId}/severance?as_of=${localISO(now)}`) });
  const ye = useQuery({ enabled: !!empId, queryKey: ["ye", empId, year, children], retry: 0,
    queryFn: () => api<Record<string, number | boolean | string>>(
      `/hr/employees/${empId}/year-end?year=${year}${children == null ? "" : `&children=${children}`}`) });
  // 일용근로 원천징수(분리과세) — 서버 EP는 있었으나 화면 호출이 없어 일용직 세액을 손으로
  // 계산하고 있었다. 일급 미보유 직원은 서버가 422로 거르므로 조건 렌더 대신 오류 문구로 처리.
  const [dailyDays, setDailyDays] = useState(1);
  const daily = useQuery({ enabled: !!empId && dailyDays > 0, retry: 0,
    queryKey: ["daily-wh", empId, dailyDays],
    queryFn: () => api<Record<string, number | string>>(
      `/hr/employees/${empId}/daily-withholding?days=${dailyDays}`) });

  const won = (n: unknown) => fmtWon(Number(n ?? 0));

  return (
    <div className="space-y-4">
      <h1 className="text-lg font-bold">인사 — 급여 법정처리 (4대보험·퇴직금·연말정산)</h1>
      <QueryErrorBanner queries={[emps, ins, sev, ye]} />

      <MinWageCard />
      <YearEndReadinessCard />

      <Card title="직원 선택">
        <select value={empId ?? ""} onChange={(e) => setEmpId(Number(e.target.value) || null)}
          className="w-full max-w-md rounded border border-slate-300 px-2 py-1 text-sm">
          <option value="">— 직원 선택 —</option>
          {(emps.data?.items ?? []).map((e) =>
            <option key={e.id} value={e.id}>{e.employee_no} {e.name}</option>)}
        </select>
      </Card>

      {empId && (
        <div className="grid gap-4 lg:grid-cols-2">
          <Card title="4대보험 (근로자 부담)" action={
            <input type="month" value={yyyymm} onChange={(e) => setYyyymm(e.target.value)}
              className="rounded border border-slate-300 px-1 text-xs" />}>
            <Table headers={["항목", "근로자 부담"]}
              rows={["국민연금", "건강보험", "장기요양", "고용보험", "합계"].map((k) => [
                k, won(ins.data?.[k])])} />
            <p className="mt-1 text-xs text-slate-500">보수월액 {won(ins.data?.["보수월액"])} 기준</p>
          </Card>

          {/* 명세↔GL 대사 — 불일치는 명세 발행 게이트 경고 근거(employee_service.reconcile) */}
          <Card title={`명세 ↔ GL 대사 (${yyyymm})`} action={recon.data
            ? (recon.data.balanced
                ? <Badge tone="green">정합</Badge>
                : <Badge tone="red">불일치 — 발행 전 확인</Badge>)
            : null}>
            {recon.isError
              ? <p className="text-sm text-slate-400">해당 월 확정 명세가 없습니다</p>
              : <Table headers={["구분", "명세", "GL 전표", "일치"]}
                  rows={[
                    ["지급 합계(5310 급여비용)", won(recon.data?.payslip_earning),
                     won(recon.data?.gl_salary_exp),
                     recon.data ? (recon.data.earning_matched ? "✓" : "✗") : "—"],
                    ["공제 합계(2150 예수금)", won(recon.data?.payslip_deduction),
                     won(recon.data?.gl_withholding),
                     recon.data ? (recon.data.deduction_matched ? "✓" : "✗") : "—"],
                  ]} />}
          </Card>

          <Card title="퇴직금 추정 (현재 기준)">
            {sev.data?.["지급대상"]
              ? <p className="text-2xl font-bold text-slate-800">{won(sev.data?.["퇴직금"])}</p>
              : <Badge tone="amber">1년 미만 — 지급대상 아님</Badge>}
            <p className="mt-1 text-xs text-slate-500">
              근속 {String(sev.data?.["근속일수"] ?? "-")}일 · 평균임금 {won(sev.data?.["avg_monthly_wage"])}</p>
          </Card>

          <Card title="일용근로 원천징수 (분리과세)" action={
            <span className="flex items-center gap-1 text-xs">근로일수
              <input type="number" min={1} value={dailyDays}
                onChange={(e) => setDailyDays(Math.max(1, Number(e.target.value)))}
                className="w-16 rounded border border-slate-300 px-1 text-xs" /></span>}>
            {daily.isError
              ? <p className="text-xs text-slate-400">
                  일급(daily_wage)이 등록된 일용직만 산출됩니다 — 이 직원은 해당 없음</p>
              : <Table headers={["항목", "값"]}
                  rows={[["고용형태", String(daily.data?.["employment_type"] ?? "—")],
                         ...Object.entries(daily.data ?? {})
                           .filter(([k]) => !["employee_id", "employment_type"].includes(k))
                           .map(([k, v]) => [k, typeof v === "number" ? won(v) : String(v)])]} />}
          </Card>

          <Card title="연말정산 (간이)" action={
            <span className="flex items-center gap-1 text-xs text-slate-500">귀속
              <input type="number" value={year} onChange={(e) => setYear(Number(e.target.value))}
                className="w-20 rounded border border-slate-300 px-1 text-xs" />
              자녀(8~20세)
              <input type="number" min={0} max={10} value={children ?? ""}
                placeholder={ye.data ? String(ye.data.children) : "자동"}
                onChange={(e) => setChildren(e.target.value === "" ? null : Math.max(0, Math.min(10, Number(e.target.value) || 0)))}
                className="w-12 rounded border border-slate-300 px-1 text-xs" title="비우면 부양가족 등록에서 자동" />
            </span>}>
            {ye.isError && <p className="mb-2 text-xs text-rose-600">{ye.error instanceof Error ? ye.error.message : "조회 실패"}</p>}
            {ye.data && (
              <p className="mb-2 text-xs text-slate-500">부양가족 {String(ye.data.dependents)}명({String(ye.data.dependents_source)}) ·
                확정 급여만 합산{Number(ye.data.unconfirmed_months) > 0 && <b className="text-amber-700"> · 미확정 {String(ye.data.unconfirmed_months)}개월 제외</b>}</p>)}
            <Table headers={["항목", "금액"]}
              rows={["총급여", "근로소득공제", "과세표준", "산출세액", "결정세액", "기납부세액"].map((k) => [
                k, won(ye.data?.[k])])} />
            <p className="mt-2 text-right text-base font-bold">
              {(Number(ye.data?.["정산액"] ?? 0)) >= 0 ? "환급 " : "추가납부 "}
              <span className={(Number(ye.data?.["정산액"] ?? 0)) >= 0 ? "text-blue-600" : "text-red-600"}>
                {won(Math.abs(Number(ye.data?.["정산액"] ?? 0)))}</span>
            </p>
            {ye.data?.basis_note && <p className="mt-1 text-xs text-slate-400">{String(ye.data.basis_note)}</p>}
          </Card>
        </div>
      )}
    </div>
  );
}


interface MinWage {
  yyyymm: string; hourly_min: number | null; source: string; checked: number; skipped: number; note: string;
  skipped_by_reason: Record<string, number>; unclassified_items: string[];
  included_items: string[]; excluded_items: string[];
  violations: { employee_no: string; name: string; employment_type: string; confirmed: boolean; base: number | null;
    hourly: number; hourly_min: number; hours?: number; unpaid_days?: number; kind: string;
    included: number; threshold: number; shortfall: number }[];
}
const ITEM_LABEL: Record<string, string> = {
  BASE: "기본급", BONUS: "정기상여금", QUAL: "자격수당", SHIFT: "교대수당", WATCH: "감시단속수당", MEAL: "식대",
  UNPAID_LEAVE: "무급휴가 공제", OT: "연장", HOLIDAY: "휴일근로", NIGHT: "야간(시간 비례)", NIGHT_PREMIUM: "야간가산",
  DUTY: "당직", INCENTIVE: "성과급",
};

/** 최저임금 점검(v1.303) — 그 달 급여 중 미달 건. 막지 않고 드러낸다(수습 감액·단시간 등 예외는 사람이 판단). */
function MinWageCard() {
  const [ym, setYm] = useState(() => localISO(new Date()).slice(0, 7));
  const q = useQuery({ queryKey: ["min-wage", ym], retry: 0,
    queryFn: () => api<MinWage>(`/hr/payrolls/${ym}/min-wage`) });
  const d = q.data;
  // 전체 급여 권한이 없는 사용자(부서장 등)는 카드를 숨긴다 — 403을 오류 배너로 보이지 않게
  if (q.error instanceof ApiError && q.error.status === 403) return null;
  return (
    <Card title="최저임금 점검" action={
      <input type="month" value={ym} aria-label="점검 급여월" onChange={(e) => e.target.value && setYm(e.target.value)}
        className="rounded border border-slate-300 px-1 text-xs" />}>
      <QueryErrorBanner queries={[q]} />
      {d && (d.hourly_min == null
        ? <p className="text-sm text-rose-700">{d.yyyymm.slice(0, 4)}년 최저임금이 등록돼 있지 않아 점검하지 못했습니다 — 급여 설정 「법정 파라미터」에서 등록하세요.</p>
        : <div className="space-y-2 text-sm">
            <p>기준 시급 <b>{d.hourly_min.toLocaleString("ko-KR")}원</b>({d.source}) · 점검 {d.checked}건
              {d.skipped > 0 && <> · 판단 보류 {d.skipped}건({Object.entries(d.skipped_by_reason).map(([k, n]) => `${k} ${n}`).join(", ")})</>} ·{" "}
              {d.violations.length ? <Badge tone="red">미달 {d.violations.length}건</Badge> : <Badge tone="green">미달 없음</Badge>}</p>
            {d.violations.length > 0 && (
              <Table headers={["사번", "이름", "산입 임금", "기준", "부족", "시급 환산", "확정"]}
                rows={d.violations.map((v) => [v.employee_no, v.name, v.included.toLocaleString("ko-KR"),
                  `${v.threshold.toLocaleString("ko-KR")}${v.hours ? ` (${v.hours}h${v.unpaid_days ? `·무급 ${v.unpaid_days}일 제외` : ""})` : ` (${v.kind})`}`,
                  <b key="s" className="text-rose-700">{v.shortfall.toLocaleString("ko-KR")}</b>,
                  v.hourly.toLocaleString("ko-KR"), v.confirmed ? "확정" : "미확정"])} />)}
            <p className="text-xs text-slate-500">산입: {d.included_items.map((k) => ITEM_LABEL[k] ?? k).join("·")} ·
              제외: {d.excluded_items.map((k) => ITEM_LABEL[k] ?? k).join("·")} · 월 209h(주 40h·주휴 포함) × 단시간 비율 × 재직 일수 비율.</p>
            {d.unclassified_items.length > 0 && <p className="text-xs text-amber-700">
              분류되지 않은 지급 항목은 산입으로 봤습니다: {d.unclassified_items.join(", ")} — 연장·당직처럼 소정근로 밖 대가면 알려 주세요(제외 목록 추가).</p>}
            <p className="text-xs text-slate-400">{d.note}</p>
          </div>)}
    </Card>
  );
}


interface Readiness {
  year: number; tax_params_registered: boolean; note: string;
  summary: { people: number; gross: number; taxable: number; income_tax: number; determined: number; refund: number;
    additional: number; approx_people: number; unconfirmed_people: number; no_dependents_people: number };
  rows: { employee_id: number; employee_no: string; name: string; taxable: number; nontax: number; income_tax: number;
    pension: number; insurance: number; months: number; family: number; children: number;
    determined: number | null; settlement: number | null; flags: string[] }[];
}

/** 연말정산 준비(v1.304) — 귀속 연도 전 직원 연간 집계·간이 정산 미리보기·확인할 점. 결정 대기 '연말정산 범위'의 판단 자료이자
 *  외부 정산에 넘길 엑셀. 전체 급여 권한 사용자만(403이면 카드 숨김). */
function YearEndReadinessCard() {
  const now = new Date();
  const [year, setYear] = useState(now.getMonth() < 3 ? now.getFullYear() - 1 : now.getFullYear());
  const [onlyFlags, setOnlyFlags] = useState(false);
  const q = useQuery({ queryKey: ["year-end-readiness", year], retry: 0, enabled: year >= 2020 && year <= 2100,
    queryFn: () => api<Readiness>(`/hr/year-end/${year}/readiness`) });
  if (q.error instanceof ApiError && q.error.status === 403) return null;
  const d = q.data;
  const rows = (d?.rows ?? []).filter((r) => !onlyFlags || r.flags.length);
  const w = (n: number | null | undefined) => (n == null ? "—" : n.toLocaleString("ko-KR"));
  return (
    <Card title="연말정산 준비" action={
      <div className="flex items-center gap-2">
        <input type="number" value={year} aria-label="귀속 연도" onChange={(e) => setYear(Number(e.target.value))}
          className="w-20 rounded border border-slate-300 px-1 text-xs" />
        <button type="button" className="text-xs text-blue-600 underline"
          onClick={() => apiDownload(`/hr/year-end/${year}/readiness.xlsx`, `year-end-${year}.xlsx`)}>엑셀(외부 정산용)</button>
      </div>}>
      <QueryErrorBanner queries={[q]} />
      {d && (<div className="space-y-2 text-sm">
        <p>{d.year}년 귀속 {d.summary.people}명 · 총급여 {w(d.summary.taxable)}원 · 기납부 소득세 {w(d.summary.income_tax)}원
          {d.tax_params_registered
            ? <> · 간이 정산 예상 환급 <b className="text-emerald-700">{w(d.summary.refund)}원</b> · 추가 납부 <b className="text-rose-700">{w(d.summary.additional)}원</b></>
            : <span className="text-rose-700"> · {d.year}년 소득세 파라미터 미등록 — 정산 미리보기 생략</span>}</p>
        <div className="flex flex-wrap gap-2 text-xs">
          {d.summary.approx_people > 0 && <Badge tone="amber">근사 산식으로 원천징수된 직원 {d.summary.approx_people}명</Badge>}
          {d.summary.unconfirmed_people > 0 && <Badge tone="amber">미확정 급여가 있는 직원 {d.summary.unconfirmed_people}명</Badge>}
          {d.summary.no_dependents_people > 0 && <Badge tone="gray">부양가족 미등록 {d.summary.no_dependents_people}명</Badge>}
          <label className="flex items-center gap-1"><input type="checkbox" checked={onlyFlags} onChange={(e) => setOnlyFlags(e.target.checked)} />
            확인할 점 있는 직원만</label>
        </div>
        <Table headers={["사번", "이름", "총급여", "기납부", "보험료(연금·건강 등)", "가족·자녀", "간이 결정세액", "정산(+환급)", "확인할 점"]}
          rows={rows.slice(0, 50).map((r) => [r.employee_no, r.name, w(r.taxable), w(r.income_tax), `${w(r.pension)} · ${w(r.insurance)}`,
            `${r.family}·${r.children}`, w(r.determined),
            <span key="s" className={(r.settlement ?? 0) >= 0 ? "text-emerald-700" : "text-rose-700"}>{w(r.settlement)}</span>,
            <span key="f" className="text-xs text-amber-700">{r.flags.join(" · ")}</span>])}
          empty={q.isFetching ? "불러오는 중…" : "해당 연도 급여 없음"} />
        {rows.length > 50 && <p className="text-xs text-slate-400">외 {rows.length - 50}명 — 전체는 엑셀로</p>}
        <p className="text-xs text-slate-400">{d.note}</p>
      </div>)}
    </Card>
  );
}

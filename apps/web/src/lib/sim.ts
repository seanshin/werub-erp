/** 재무 시뮬레이션 공용 모델 — 시뮬레이션 화면과 경영진 대시보드가 동일 투영식을 공유. */

export interface Baseline {
  yyyymm: string; months_window: number;
  monthly: { revenue: number; variable: number; fixed: number; dep: number; other: number; net: number };
  variable_ratio: number; suggested_growth: number; revenue_cv: number;
  cash: number; ar: number; ap: number; total_liabilities: number; equity: number;
  seasonality: number[]; start_month: number;
}
export interface Drivers {
  growth: number; varRatio: number; fixedDelta: number; taxRate: number;
  capex: number; loan: number; seasonal: boolean;
}
export interface ProjRow { label: string; 수익: number; 순이익: number; 현금잔액: number }

/** 기준선 추천 드라이버 — 최근 추세(성장률·변동비율), 조정 없음. */
export const baseDrivers = (b: Baseline): Drivers =>
  ({ growth: b.suggested_growth, varRatio: b.variable_ratio, fixedDelta: 0,
     taxRate: 0, capex: 0, loan: 0, seasonal: false });

/** 향후 12개월 손익·현금 투영 — 추세(성장률 복리)와 계절지수를 분리(이중적용 방지). */
export function project(b: Baseline, d: Drivers): ProjRow[] {
  const rows: ProjRow[] = [];
  let trendRev = b.monthly.revenue;
  let prevRev = b.monthly.revenue;
  let cash = b.cash;
  for (let m = 1; m <= 12; m++) {
    trendRev = trendRev * (1 + d.growth);
    const calMonth = ((b.start_month - 1) + (m - 1)) % 12;
    const factor = d.seasonal ? (b.seasonality?.[calMonth] ?? 1) : 1;
    const rev = trendRev * factor;
    const variable = rev * d.varRatio;
    const fixed = b.monthly.fixed * (1 + d.fixedDelta);
    const other = b.monthly.other;
    const op = rev - variable - fixed - other;
    const tax = op > 0 ? op * d.taxRate : 0;
    const net = op - tax;
    const wcDrag = rev - prevRev;
    const opCash = net + b.monthly.dep - wcDrag;
    const oneOff = m === 1 ? d.loan - d.capex : 0;
    cash = cash + opCash + oneOff;
    prevRev = rev;
    rows.push({ label: `${calMonth + 1}월`, 수익: Math.round(rev), 순이익: Math.round(net), 현금잔액: Math.round(cash) });
  }
  return rows;
}

/** 기준선 기준 12개월 예측 요약 — 경영진 대시보드 KPI용(추천 드라이버 적용). */
export function forecastSummary(b: Baseline) {
  const d = baseDrivers(b);
  const rows = project(b, d);
  const annualNet = rows.reduce((a, r) => a + r.순이익, 0);
  const annualRev = rows.reduce((a, r) => a + r.수익, 0);
  const margin = annualRev ? annualNet / annualRev * 100 : 0;
  const fixedM = b.monthly.fixed * (1 + d.fixedDelta) + b.monthly.other;
  const bep = d.varRatio < 1 ? fixedM / (1 - d.varRatio) : 0;
  const curRev = rows[0]?.수익 ?? 0;
  const safety = curRev ? (curRev - bep) / curRev * 100 : 0;
  const minCash = rows.length ? Math.min(...rows.map((r) => r.현금잔액)) : 0;
  const shortage = rows.find((r) => r.현금잔액 < 0)?.label ?? null;
  return { annualNet, annualRev, margin, bep, safety, minCash, shortage, curRev, growth: d.growth };
}

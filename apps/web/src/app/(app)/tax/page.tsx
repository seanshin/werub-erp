"use client";
/** 세무 — 원천징수이행상황·4대보험 공단신고 집계 (FR-07 확장). */
import { monthLocal } from "@/lib/dates";
import { useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { api, apiDownload } from "@/lib/api";
import { won } from "@/lib/format";
import { Card, Table } from "@/components/ui";
import { QueryError } from "@/components/query-guard";

interface Withholding { 소득구분: string; 인원: number; 총지급액: number; 원천징수세액: number }
interface Filing {
  인원: number; 근로자부담: Record<string, number>;
  공단납부_추정_노사합산: Record<string, number>; 주: string;
}
interface BizStatus {
  revenue: { total: number; items: { month: string; account: string; amount: number }[] };
  purchases: { months: { month: string; invoice_type: string; supply_amount: number;
    vat_amount: number }[]; unmatched: { count: number; total_amount: number } };
  payroll_total: number; 주: string;
}

export default function TaxPage() {
  const [yyyymm, setYyyymm] = useState(monthLocal());
  const year = Number(yyyymm.slice(0, 4));

  const wh = useQuery({ queryKey: ["wh", yyyymm],
    queryFn: () => api<Withholding>(`/tax/withholding?yyyymm=${yyyymm}`) });
  const fl = useQuery({ queryKey: ["fl", yyyymm],
    queryFn: () => api<Filing>(`/tax/insurance-filing?yyyymm=${yyyymm}`) });
  const bs = useQuery({ queryKey: ["bizstat", year],
    queryFn: () => api<BizStatus>(`/tax/business-status?year=${year}`) });
  const vat = useQuery({ queryKey: ["vat", year],
    queryFn: () => api<{ sales: { taxable: { supply: number; vat: number };
      exempt: { supply: number } }; purchases: { taxable: { supply: number; vat: number } };
      output_vat: number; input_vat: number; payable_estimate: number; 주: string }>(
      `/tax/vat-summary?start=${year}-01-01&end=${year}-12-31`) });

  const w = wh.data; const f = fl.data;
  const items = ["국민연금", "건강보험", "장기요양", "고용보험"];

  return (
    <div className="space-y-4">
      <div className="flex items-center justify-between gap-2">
        <h1 className="text-lg font-bold">세무 — 원천징수·4대보험 신고</h1>
        {[wh, fl, bs, vat].some((x) => x.isError) && (
          <QueryError error={[wh, fl, bs, vat].find((x) => x.isError)?.error} />
        )}
        <div className="flex items-center gap-2">
          <input type="month" value={yyyymm} onChange={(e) => setYyyymm(e.target.value)}
            className="rounded border border-slate-300 px-2 py-1 text-sm" />
          <button onClick={() => apiDownload(`/tax/report.xlsx?yyyymm=${yyyymm}`, `세무신고-${yyyymm}.xlsx`)}
            className="rounded-md bg-emerald-600 px-3 py-1.5 text-sm font-medium text-white hover:bg-emerald-700">
            신고서식 Excel
          </button>
        </div>
      </div>

      <Card title="원천징수이행상황 (근로소득)">
        <Table headers={["소득구분", "인원", "총지급액", "원천징수세액"]}
          rows={[[w?.소득구분 ?? "-", `${w?.인원 ?? 0}명`,
                  won(w?.총지급액 ?? 0), won(w?.원천징수세액 ?? 0)]]} />
      </Card>

      <Card title={`4대보험 공단신고 집계 (대상 ${f?.인원 ?? 0}명)`}>
        <Table headers={["항목", "근로자 부담", "공단납부(노사합산 추정)"]}
          rows={[
            // ⚠ 서버는 고용보험 공단납부를 **의도적으로 제외**한다(사업주 요율이 사업장별로 달라
            //    노사 50:50 추정이 성립하지 않음·tax/service.py). 없을 때 근로자부담값으로 폴백하면
            //    "고용보험 공단납부 = 근로자부담"이라는 **틀린 금액**이 표시된다(Excel 산출물은 올바름).
            ...items.map((k) => [k, won(f?.근로자부담?.[k] ?? 0),
              f?.공단납부_추정_노사합산?.[k] != null
                ? won(f.공단납부_추정_노사합산[k])
                : "사업주 요율 별도"]),
            ["합계", won(f?.근로자부담?.["합계"] ?? 0), "—"],
          ]} />
        <p className="mt-2 text-xs text-slate-500">{f?.주}</p>
      </Card>

      <Card title={`사업장현황신고 집계 (${year}년 — 면세사업자, 익년 2/10)`} action={
        <button onClick={() => apiDownload(`/patients/medical-expenses.xlsx?year=${year}`,
          `의료비-${year}.xlsx`)}
          className="rounded-md bg-slate-600 px-3 py-1 text-xs font-medium text-white hover:bg-slate-700">
          의료비 자료 Excel</button>}>
        <div className="mb-2 grid max-w-2xl grid-cols-3 gap-2 text-sm tabular-nums">
          <span>수입금액: <b>{won(bs.data?.revenue.total ?? 0)}</b></span>
          <span>인건비: <b>{won(bs.data?.payroll_total ?? 0)}</b></span>
          <span className={bs.data?.purchases.unmatched.count ? "text-amber-600" : ""}>
            매입 미대사: {bs.data?.purchases.unmatched.count ?? 0}건</span>
        </div>
        <Table headers={["월", "구분", "금액"]}
          empty="집계 데이터 없음"
          rows={[
            ...(bs.data?.revenue.items ?? []).map((r) =>
              [r.month, `수입 — ${r.account}`, won(r.amount)]),
            ...(bs.data?.purchases.months ?? []).map((m) =>
              [m.month, `매입 — ${m.invoice_type === "TAX" ? "과세" : "면세"}`,
               won(m.supply_amount)]),
          ]} />
        <p className="mt-2 text-xs text-slate-500">{bs.data?.주}</p>
      </Card>

      <Card title={`부가세 집계 보조 (${year}년 — 세금계산서 raw, 안분 전)`}>
        <Table headers={["구분", "공급가액", "세액"]}
          rows={[
            ["과세 매출", won(vat.data?.sales.taxable.supply ?? 0), won(vat.data?.sales.taxable.vat ?? 0)],
            ["면세 매출 (의료용역)", won(vat.data?.sales.exempt.supply ?? 0), "—"],
            ["과세 매입", won(vat.data?.purchases.taxable.supply ?? 0), won(vat.data?.purchases.taxable.vat ?? 0)],
          ]} />
        <p className="mt-2 text-right text-sm">
          매출세액 {won(vat.data?.output_vat ?? 0)} − 매입세액 {won(vat.data?.input_vat ?? 0)} ={" "}
          <b>납부추정 {won(vat.data?.payable_estimate ?? 0)}</b>
        </p>
        <p className="text-[11px] text-slate-400">{vat.data?.주}</p>
      </Card>
    </div>
  );
}

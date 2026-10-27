"use client";
/** IFRS 정합 현황 — K-IFRS 기준별 처리·적용화면·상태 + 당해 실제 처리 활동(전표 집계). */
import { useState } from "react";
import Link from "next/link";
import { useQuery } from "@tanstack/react-query";
import { api } from "@/lib/api";
import { Badge, Card, Table } from "@/components/ui";
import { won } from "@/lib/format";

interface Item {
  category: string; code: string; name: string; treatment: string; status: string;
  screen: string; activity: { amount: number; count: number } | null;
}
interface Conformance {
  year: string; summary: { conformance: string; standards: number; measured: number; note: string };
  items: Item[]; principles: string[];
}

const CATS = ["측정", "표시", "공시", "수익"];

export default function FinanceIfrsPage() {
  const [year, setYear] = useState(() => new Date().getFullYear());
  const q = useQuery({
    queryKey: ["ifrs-conformance", year],
    queryFn: () => api<Conformance>(`/finance/ifrs-conformance?year=${year}`),
  });
  const data = q.data;
  const items = data?.items ?? [];

  return (
    <div className="space-y-4">
      <div className="flex items-center justify-between">
        <h1 className="text-lg font-bold">IFRS(K-IFRS) 정합 현황</h1>
        <select value={year} onChange={(e) => setYear(Number(e.target.value))}
          className="rounded border border-slate-300 px-2 py-1 text-sm">
          {[0, 1, 2].map((d) => new Date().getFullYear() - d).map((y) => (
            <option key={y} value={y}>{y}년</option>))}
        </select>
      </div>

      {data && (
        <div className="grid gap-3 sm:grid-cols-4">
          <Card title="부합도"><p className="text-2xl font-bold text-blue-700">{data.summary.conformance}</p>
            <p className="mt-1 text-xs text-slate-500">기말 측정·표시·공시 반영</p></Card>
          <Card title="적용 기준"><p className="text-2xl font-bold text-slate-800">{data.summary.standards}개</p>
            <p className="mt-1 text-xs text-slate-500">충족 {data.summary.measured} · 부분 {data.summary.standards - data.summary.measured}</p></Card>
          <Card title={`${year} 처리 건수`}>
            <p className="text-2xl font-bold text-emerald-700">
              {items.reduce((a, i) => a + (i.activity?.count ?? 0), 0)}건</p>
            <p className="mt-1 text-xs text-slate-500">당해 측정·조정 전표</p></Card>
          <Card title={`${year} 처리 금액`}>
            <p className="text-xl font-bold text-emerald-700">
              {won(items.reduce((a, i) => a + (i.activity?.amount ?? 0), 0))}원</p>
            <p className="mt-1 text-xs text-slate-500">시그니처 계정 순변동 합</p></Card>
        </div>
      )}

      {q.isError && <p className="text-sm text-red-600">정합 현황을 불러오지 못했습니다 (재무 열람 권한 필요)</p>}

      {CATS.map((cat) => {
        const rows = items.filter((i) => i.category === cat);
        if (!rows.length) return null;
        return (
          <Card key={cat} title={`${cat} 기준`}>
            <Table headers={["기준", "명칭", "처리 내용", "상태", `${year} 처리(금액·건수)`, "적용 화면"]}
              rows={rows.map((i) => [
                <span key="c" className="rounded bg-blue-600 px-1.5 py-0.5 text-[10px] font-bold text-white">
                  KIFRS {i.code}</span>,
                i.name,
                <span key="t" className="text-xs text-slate-500">{i.treatment}</span>,
                <Badge key="s" tone={i.status === "✅" ? "green" : "amber"}>
                  {i.status === "✅" ? "충족" : "부분"}</Badge>,
                i.activity
                  ? <span key="a" className="text-xs font-medium text-emerald-700">
                      {won(i.activity.amount)}원 · {i.activity.count}건</span>
                  : <span key="a" className="text-xs text-slate-300">—</span>,
                <Link key="l" href={i.screen}
                  className="text-xs font-medium text-blue-600 hover:underline">바로가기</Link>,
              ])} />
          </Card>
        );
      })}

      {data && (
        <Card title="정합 원칙·한계">
          <ul className="list-disc space-y-1 pl-5 text-sm text-slate-600">
            {data.principles.map((p, i) => <li key={i}>{p}</li>)}
          </ul>
          <p className="mt-3 rounded bg-amber-50 p-2 text-xs text-amber-700">{data.summary.note}</p>
          <p className="mt-2 text-xs text-slate-400">
            인증 증빙: 항목별 회계정책·측정방법론·통제기술·준수체크리스트·테스트증빙 (docs/06-compliance/ifrs).
            모든 조정은 결산분개(전표)로 귀결되어 감사추적과 통합됩니다.
          </p>
        </Card>
      )}
    </div>
  );
}

"use client";
/** 수가 마스터 시점 조회 (FR-35) — temporal 단가 확인 (시점 선택기). */
import { todayLocal } from "@/lib/dates";
import { useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { ApiError, api } from "@/lib/api";
import { Button, Card, Input } from "@/components/ui";
import { won } from "@/lib/format";

interface Fee { code: string; code_type: string; name: string; unit_price: string;
                price_cap: string; valid_from: string; valid_to: string | null }

export default function FeesPage() {
  const [code, setCode] = useState("");
  const [codeType, setCodeType] = useState("TARIFF");
  const [at, setAt] = useState(todayLocal());
  const [params, setParams] = useState<string | null>(null);

  const fee = useQuery({
    queryKey: ["fee", params],
    queryFn: () => api<Fee>(`/regulatory/fees?${params}`),
    enabled: !!params,
    retry: 0,
  });

  return (
    <div className="space-y-4">
      <h1 className="text-lg font-bold">규제 — 수가 시점 조회</h1>
      <Card title="시점 단가 조회 (시행일 당일 = 신 버전 적용)">
        <div className="flex flex-wrap items-end gap-2">
          <Input label="코드" value={code} onChange={(e) => setCode(e.target.value)} />
          <label className="block">
            <span className="mb-1 block text-xs font-medium text-slate-600">유형</span>
            <select value={codeType} onChange={(e) => setCodeType(e.target.value)}
              className="rounded-md border border-slate-300 px-3 py-1.5 text-sm">
              {/* NONCOVERED(비급여) 포함 — 백엔드 CHECK 4종과 일치(계산검토 FE-1) */}
              {["TARIFF", "DRUG", "MATERIAL", "NONCOVERED"].map((t) => <option key={t}>{t}</option>)}
            </select>
          </label>
          <Input label="기준 시점" type="date" value={at} onChange={(e) => setAt(e.target.value)} />
          <Button onClick={() => setParams(`code=${code}&code_type=${codeType}&at=${at}`)}>조회</Button>
        </div>

        {fee.data && (
          <div className="mt-4 rounded-md bg-slate-50 p-4 text-sm">
            <p className="font-medium">{fee.data.name} ({fee.data.code})</p>
            <p className="mt-1 text-2xl font-bold">{won(Number(fee.data.unit_price))}</p>
            <p className="mt-1 text-xs text-slate-500">
              적용기간: {fee.data.valid_from} ~ {fee.data.valid_to ?? "현행"}
              {fee.data.price_cap && ` · 상한가 ${won(Number(fee.data.price_cap))}`}
            </p>
          </div>
        )}
        {fee.error && (
          <p className="mt-3 text-sm text-red-600">
            {fee.error instanceof ApiError ? fee.error.message : "조회 실패"}
          </p>
        )}
      </Card>
      <p className="text-xs text-slate-400">
        과거 시점을 선택하면 당시 고시 단가가 그대로 재현됩니다 (temporal 마스터 — 정산 재현 보장)
      </p>
    </div>
  );
}

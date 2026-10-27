"use client";
/** 규제 — 코드 마스터(약가·수가·치료재료·비급여). 카드 클릭 → 하단 탭으로 종류별 목록·검색. */
import { useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { api } from "@/lib/api";
import { Badge, Card, Table } from "@/components/ui";
import { QueryErrorBanner } from "@/components/query-guard";
import { won } from "@/lib/format";

interface FeeRow {
  code: string; code_type: string; name: string; unit_price: number;
  price_cap: number | null; criteria: string | null; valid_from: string;
  benefit_type: string | null; reward_rate: number | null;
}
interface Stat { code_type: string; label: string; count: number }

const TYPE_LABEL: Record<string, string> = {
  DRUG: "약가", TARIFF: "수가", MATERIAL: "치료재료", NONCOVERED: "비급여",
};
const TYPE_TONE: Record<string, "blue" | "green" | "amber" | "red"> =
  { DRUG: "blue", TARIFF: "green", MATERIAL: "amber", NONCOVERED: "red" };

function parts(f: FeeRow): string[] {
  return (f.criteria ?? "").split("|").map((p) => p.trim()).filter(Boolean);
}
function chipTone(p: string): string {
  if (p === "급여") return "bg-emerald-50 text-emerald-700";
  if (p === "비급여") return "bg-amber-50 text-amber-700";
  if (/^0?\.\d+$/.test(p) || /^\d+(\.\d+)?$/.test(p)) return "bg-blue-50 text-blue-700";
  if (p.startsWith("분류")) return "bg-slate-100 text-slate-500";
  return "bg-slate-100 text-slate-600";
}

export default function CodesPage() {
  // type: null = 미선택(하단 숨김), "" = 전체, "DRUG"... = 종류
  const [type, setType] = useState<string | null>(null);
  const [q, setQ] = useState("");
  const [query, setQuery] = useState("");

  const stats = useQuery({ queryKey: ["fee-stats"],
    queryFn: () => api<{ items: Stat[] }>("/regulatory/fees/stats") });
  // 카드/탭 선택(type)만으로도 목록 로드 — 검색어는 보조 필터
  const search = useQuery({
    enabled: type !== null,
    queryKey: ["fee-list", type, query],
    queryFn: () => {
      const p = new URLSearchParams();
      if (query) p.set("q", query);
      if (type) p.set("code_type", type);
      p.set("limit", "100");
      return api<{ count: number; items: FeeRow[] }>(`/regulatory/fees/search?${p}`);
    },
  });

  const items = (stats.data?.items ?? []).slice().sort((a, b) => b.count - a.count);
  const total = items.reduce((s, i) => s + i.count, 0);
  const select = (t: string) => { setType(t); setQ(""); setQuery(""); };

  // 카드 = 전체 + 종류별 (총 5개: 전체·약가·수가·치료재료·비급여)
  const cards = [{ code_type: "", label: "전체", count: total }, ...items];

  return (
    <div className="space-y-4">
      <h1 className="text-lg font-bold">규제 — 코드 마스터 (약가·수가·치료재료·비급여)</h1>
      <p className="text-xs text-slate-400">카드를 누르면 하단에서 종류별 목록을 확인·검색할 수 있습니다.</p>
      <QueryErrorBanner queries={[stats, search]} />

      <div className="grid gap-3 sm:grid-cols-3 lg:grid-cols-5">
        {cards.map((c) => {
          const active = type === c.code_type;
          return (
            <button key={c.code_type || "ALL"} onClick={() => select(c.code_type)}
              className={`rounded-xl border bg-white p-4 text-left shadow-sm transition
                ${active ? "border-blue-500 ring-2 ring-blue-200" : "border-slate-200 hover:border-blue-300"}`}>
              <div className="flex items-center justify-between">
                <span className="text-sm font-medium text-slate-600">{c.label}</span>
                {c.code_type && <Badge tone={TYPE_TONE[c.code_type] ?? "gray"}>
                  {TYPE_LABEL[c.code_type] ?? c.code_type}</Badge>}
              </div>
              <p className="mt-1 text-2xl font-bold text-slate-800">{c.count.toLocaleString()}</p>
              <p className="text-[11px] text-slate-400">{active ? "보는 중 ▾" : "클릭하여 보기"}</p>
            </button>
          );
        })}
      </div>

      {type !== null && (
        <Card>
          {/* 하단 탭 — 카드와 동기화 */}
          <div className="mb-3 flex flex-wrap gap-1 border-b border-slate-200">
            {cards.map((c) => (
              <button key={c.code_type || "ALL"} onClick={() => select(c.code_type)}
                className={`px-4 py-2 text-sm font-medium -mb-px border-b-2 ${
                  type === c.code_type ? "border-blue-600 text-blue-700"
                    : "border-transparent text-slate-500 hover:text-slate-700"}`}>
                {c.label}
                <span className="ml-1 text-[11px] text-slate-400">{c.count.toLocaleString()}</span>
              </button>
            ))}
          </div>

          <div className="flex items-center gap-2">
            <input value={q} onChange={(e) => setQ(e.target.value)}
              onKeyDown={(e) => e.key === "Enter" && setQuery(q)}
              placeholder={`${TYPE_LABEL[type] ?? "전체"} 내 검색 (제품명·코드·주성분·명칭)`}
              className="w-96 rounded-md border border-slate-300 px-3 py-1.5 text-sm" />
            <button onClick={() => setQuery(q)}
              className="rounded-md bg-slate-700 px-4 py-1.5 text-sm text-white">검색</button>
            {q && <button onClick={() => { setQ(""); setQuery(""); }}
              className="text-xs text-slate-400 hover:text-slate-600">초기화</button>}
            {search.data && <span className="text-xs text-slate-400">{search.data.count}건 표시(최대 100)</span>}
          </div>

          <div className="mt-3 overflow-x-auto">
            <Table sortable pageSize={30} stickyHeader maxHeight={600}
              headers={["코드", "종류", "명칭", "단가/상한", "세부 정보", "시행"]}
              empty={search.isFetching ? "불러오는 중…" : "코드 없음"}
              rows={(search.data?.items ?? []).map((f) => [
                <code key="c" className="whitespace-nowrap text-xs">{f.code}</code>,
                <Badge key="t" tone={TYPE_TONE[f.code_type] ?? "gray"}>
                  {TYPE_LABEL[f.code_type] ?? f.code_type}</Badge>,
                <span key="n" className="font-medium text-slate-800">{f.name}</span>,
                <span key="p" className="whitespace-nowrap text-xs">
                  {f.unit_price > 0
                    ? <b className="text-sm">{won(f.unit_price)}</b>
                    : (f.benefit_type || f.reward_rate != null)
                      ? <span>
                          {f.benefit_type && <Badge tone={f.benefit_type === "급여" ? "green" : "amber"}>{f.benefit_type}</Badge>}
                          {f.reward_rate != null && <span className="ml-1 text-blue-700">보상 {(f.reward_rate * 100).toFixed(0)}%</span>}
                        </span>
                      : <span className="text-slate-400">-</span>}</span>,
                <div key="cr" className="flex max-w-2xl flex-wrap gap-1">
                  {parts(f).map((p, i) => (
                    <span key={i} className={`rounded px-1.5 py-0.5 text-[11px] ${chipTone(p)}`}>{p}</span>
                  ))}
                  {parts(f).length === 0 && <span className="text-slate-300">-</span>}
                </div>,
                <span key="v" className="whitespace-nowrap text-xs">{f.valid_from}</span>,
              ])} />
          </div>
        </Card>
      )}

      <p className="text-xs text-slate-400">
        출처: 보건복지부/심평원 공식 공개자료(약제급여목록·수가반영내역·치료재료·비급여 목록). 사전심사 코드검증·단가조회에 사용.
      </p>
    </div>
  );
}

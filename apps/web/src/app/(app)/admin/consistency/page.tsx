"use client";
/** 운영 정합성 점검 — 도메인 경계 불변식 상시 검사(관리자). */
import { useQuery } from "@tanstack/react-query";
import { api } from "@/lib/api";
import { Badge, Button, Card, Table } from "@/components/ui";
import { QueryErrorBanner } from "@/components/query-guard";

interface Item {
  key: string; title: string; domain: string;
  status: "OK" | "WARN" | "FAIL" | "SKIP";
  summary: string; detail: unknown; action: string | null;
}
interface Result { items: Item[]; counts: Record<string, number>; overall: string }

const TONE: Record<Item["status"], "green" | "amber" | "red" | "gray"> = {
  OK: "green", WARN: "amber", FAIL: "red", SKIP: "gray" };
const LABEL: Record<Item["status"], string> = {
  OK: "정상", WARN: "주의", FAIL: "이상", SKIP: "해당없음" };
const DOMAIN_KO: Record<string, string> = {
  finance: "재무·회계", patient: "원무·환자", hr: "인사", scm: "재고·조달" };

export default function ConsistencyPage() {
  const q = useQuery({ queryKey: ["consistency"],
    queryFn: () => api<Result>("/admin/consistency") });

  const items = q.data?.items ?? [];
  const bad = items.filter((i) => i.status === "FAIL" || i.status === "WARN");

  return (
    <div className="space-y-4">
      <div className="flex items-center justify-between">
        <h1 className="text-lg font-bold">운영 정합성 점검</h1>
        <Button variant="secondary" isLoading={q.isFetching}
          onClick={() => q.refetch()}>다시 검사</Button>
      </div>
      <p className="text-xs text-slate-500">
        도메인 경계를 가로지르는 불변식을 검사합니다 — 각 항목은 <b>정상 상태에서 반드시
        &lsquo;정상&rsquo;</b>이 되도록 설계돼 있어, 하나라도 &lsquo;이상&rsquo;이면 실제로
        조치가 필요합니다. 전제가 없는 검사는 &lsquo;해당없음&rsquo;으로 건너뜁니다.
      </p>
      <QueryErrorBanner queries={[q]} />

      <div className="flex flex-wrap gap-2">
        {(["FAIL", "WARN", "OK", "SKIP"] as const).map((st) => (
          <Card key={st} title={LABEL[st]}>
            <p className={`text-2xl font-bold ${st === "FAIL" ? "text-red-600"
              : st === "WARN" ? "text-amber-600" : "text-slate-700"}`}>
              {q.data?.counts?.[st] ?? 0}</p>
          </Card>
        ))}
      </div>

      {bad.length > 0 && (
        <Card title={`조치 필요 ${bad.length}건`}>
          <div className="space-y-2">
            {bad.map((i) => (
              <div key={i.key} className="rounded border border-slate-200 p-2">
                <div className="flex items-center gap-2">
                  <Badge tone={TONE[i.status]}>{LABEL[i.status]}</Badge>
                  <span className="text-sm font-medium">{i.title}</span>
                  <span className="text-[11px] text-slate-400">
                    {DOMAIN_KO[i.domain] ?? i.domain}</span>
                </div>
                <p className="mt-1 text-xs text-slate-700">{i.summary}</p>
                {i.action && (
                  <p className="mt-0.5 text-xs text-blue-700">→ {i.action}</p>)}
                {i.detail != null && (
                  <pre className="mt-1 overflow-x-auto rounded bg-slate-50 p-1.5 text-[11px] text-slate-600">
                    {JSON.stringify(i.detail, null, 1)}</pre>)}
              </div>
            ))}
          </div>
        </Card>
      )}

      <Card title={`전체 검사 (${items.length}건)`}>
        <Table headers={["상태", "영역", "검사", "결과"]}
          empty={q.isFetching ? "검사 중…" : "검사 없음"}
          rows={items.map((i) => [
            <Badge key="s" tone={TONE[i.status]}>{LABEL[i.status]}</Badge>,
            <span key="d" className="whitespace-nowrap text-xs text-slate-500">
              {DOMAIN_KO[i.domain] ?? i.domain}</span>,
            i.title,
            <span key="r" className="text-xs text-slate-600">{i.summary}</span>,
          ])} />
      </Card>
    </div>
  );
}

"use client";
/** 컴플라이언스 만기·한도 통합 — 면허·법정교육/검진·4대보험·방사선 장비검사·재고 유효기간 만기 +
 *  방사선 피폭 한도·환자안전 미검토. 흩어진 법정 기한을 긴급도(경과/D-7/D-30/D-90)로 한 화면에. */
import { useQuery } from "@tanstack/react-query";
import { api } from "@/lib/api";
import { Badge, Card, Table } from "@/components/ui";
import { QueryError } from "@/components/query-guard";
import { useUrlState } from "@/lib/use-url-state";

interface Item {
  category: string; title: string; subject: string | null;
  due_on: string; days_remaining: number; band: string; status: string;
}
interface RadRow { employee: string | null; ytd_msv: number; limit_msv: number }
interface Deadlines {
  as_of: string; horizon_days: number;
  summary: {
    total: number; actionable: number; overdue: number; d7: number; d30: number;
    d90: number; long_overdue: number; by_category: Record<string, number>;
  };
  items: Item[];
  alerts: {
    radiation_over_limit: RadRow[]; radiation_near_limit: RadRow[];
    safety_unreviewed: number; drug_recall_open: number;
  };
}

const BAND_TONE: Record<string, "red" | "amber" | "blue" | "gray"> = {
  overdue: "red", d7: "red", d30: "amber", d90: "blue", long_overdue: "gray",
};
const BAND_LABEL: Record<string, string> = {
  overdue: "경과", d7: "D-7", d30: "D-30", d90: "D-90", long_overdue: "장기경과",
};
const dayLabel = (n: number) => (n < 0 ? `${-n}일 경과` : `D-${n}`);

function Kpi({ label, value, tone }: { label: string; value: number; tone?: string }) {
  return (
    <div className="rounded-lg border border-slate-200 bg-white p-3">
      <p className="text-xs text-slate-500">{label}</p>
      <p className={`text-lg font-bold tabular-nums text-right ${tone ?? "text-slate-800"}`}>{value}</p>
    </div>
  );
}

export default function CompliancePage() {
  const [horizonStr, setHorizon] = useUrlState("horizon", "90");   // 공유·새로고침 유지(X2-1)
  const horizon = Number(horizonStr) || 90;
  const q = useQuery({
    queryKey: ["compliance", horizon],
    queryFn: () => api<Deadlines>(`/compliance/deadlines?horizon_days=${horizon}`),
  });
  const d = q.data;

  return (
    <div className="space-y-4">
      <div className="flex items-center justify-between">
        <h1 className="text-lg font-bold">컴플라이언스 만기·한도</h1>
        <select value={horizon} onChange={(e) => setHorizon(e.target.value)}
          className="rounded border border-slate-300 px-2 py-1 text-sm">
          <option value={30}>향후 30일</option>
          <option value={60}>향후 60일</option>
          <option value={90}>향후 90일</option>
          <option value={180}>향후 180일</option>
        </select>
      </div>
      {q.isError && <QueryError error={q.error} onRetry={() => q.refetch()} />}

      <Card title="긴급도 요약">
        <div className="grid gap-2 sm:grid-cols-6">
          <Kpi label="실행 대상" value={d?.summary.actionable ?? 0} />
          <Kpi label="경과(초과)" value={d?.summary.overdue ?? 0} tone="text-red-600" />
          <Kpi label="D-7 이내" value={d?.summary.d7 ?? 0} tone="text-red-600" />
          <Kpi label="D-30 이내" value={d?.summary.d30 ?? 0} tone="text-amber-600" />
          <Kpi label="D-90 이내" value={d?.summary.d90 ?? 0} tone="text-blue-600" />
          <Kpi label="장기경과(180일+)" value={d?.summary.long_overdue ?? 0} tone="text-slate-400" />
        </div>
        {d && Object.keys(d.summary.by_category).length > 0 && (
          <div className="mt-3 flex flex-wrap gap-2">
            {Object.entries(d.summary.by_category).map(([c, n]) => (
              <Badge key={c} tone="gray">{c} {n}</Badge>
            ))}
          </div>
        )}
      </Card>

      {d && (d.alerts.radiation_over_limit.length > 0 || d.alerts.radiation_near_limit.length > 0
        || d.alerts.safety_unreviewed > 0 || d.alerts.drug_recall_open > 0) && (
        <Card title="한도·미검토 경보">
          <div className="flex flex-wrap gap-2">
            {d.alerts.radiation_over_limit.map((r, i) => (
              <Badge key={`o${i}`} tone="red">
                방사선 초과: {r.employee ?? "미상"} {r.ytd_msv}/{r.limit_msv}mSv
              </Badge>
            ))}
            {d.alerts.radiation_near_limit.map((r, i) => (
              <Badge key={`n${i}`} tone="amber">
                방사선 근접: {r.employee ?? "미상"} {r.ytd_msv}/{r.limit_msv}mSv
              </Badge>
            ))}
            {d.alerts.safety_unreviewed > 0 && (
              <Badge tone="amber">환자안전 미검토 {d.alerts.safety_unreviewed}건</Badge>
            )}
            {d.alerts.drug_recall_open > 0 && (
              <Badge tone="red">의약품 회수 미조치 {d.alerts.drug_recall_open}건</Badge>
            )}
          </div>
        </Card>
      )}

      <Card title={`만기 임박·경과 (as_of ${d?.as_of ?? "-"})`}>
        <Table headers={["긴급도", "카테고리", "항목", "대상", "기한", "잔여(일)", "상태"]}
          rows={(d?.items ?? []).map((it) => [
            <Badge key="b" tone={BAND_TONE[it.band] ?? "gray"}>{BAND_LABEL[it.band] ?? it.band}</Badge>,
            it.category, it.title, it.subject ?? "-", it.due_on,
            dayLabel(it.days_remaining), it.status,
          ])}
          empty="해당 기간 내 만기 임박 항목이 없습니다" />
      </Card>
    </div>
  );
}

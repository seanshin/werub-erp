"use client";
/** 사전심사 룰 관리 — DUR·진단연계·묶음·종별가산율 master 현황(적재 EP로 관리, 화면 조회). */
import { useQuery } from "@tanstack/react-query";
import { api } from "@/lib/api";
import { Badge, Card, Table } from "@/components/ui";

interface DurRow { id: number; dur_type: string; ingredient_code: string | null;
  ingredient_code_b: string | null; min_age: number | null; max_age: number | null;
  reason: string | null; notice_no: string | null; severity: string; source: string; active: boolean }
interface DxRow { id: number; code: string; require_dx_prefixes: string; name: string | null;
  reason: string | null; severity: string; active: boolean }
interface BundleRow { id: number; parent_code: string; child_codes: string; rule_type: string;
  name: string | null; reason: string | null; severity: string; active: boolean }
interface SurchargeRow { grade: string; rate: number; applies_to: string; active: boolean }

const sev = (s: string) => <Badge tone={s === "BLOCK" ? "red" : "amber"}>{s}</Badge>;

export default function ReviewRulesPage() {
  const dur = useQuery({ queryKey: ["dur-rules"],
    queryFn: () => api<{ items: DurRow[] }>("/claims/pre-review/dur/rules") });
  const dx = useQuery({ queryKey: ["dx-rules"],
    queryFn: () => api<{ items: DxRow[] }>("/claims/pre-review/dx-rules") });
  const bundle = useQuery({ queryKey: ["bundle-rules"],
    queryFn: () => api<{ items: BundleRow[] }>("/claims/pre-review/bundling-rules") });
  const sur = useQuery({ queryKey: ["surcharge-rates"],
    queryFn: () => api<{ items: SurchargeRow[] }>("/claims/pre-review/surcharge-rates") });

  const n = (x?: { length: number }) => x?.length ?? 0;

  return (
    <div className="space-y-4">
      <div>
        <h1 className="text-lg font-bold">심사 룰 관리</h1>
        <p className="mt-1 text-sm text-slate-500">
          DUR·진단연계·묶음청구·종별가산율 룰 현황. 적재는 관리자 API(데이터 적재 런북 참조) — 빈 상태에선 무영향.
        </p>
      </div>

      <Card title={`약제 DUR (병용금기·동일성분중복·연령·임부금기) — ${n(dur.data?.items)}건`}>
        <Table headers={["유형", "성분A", "성분B", "연령(min/max)", "사유", "공고", "심각도", "출처", "활성"]}
          rows={(dur.data?.items ?? []).map((r) => [
            r.dur_type, r.ingredient_code ?? "-", r.ingredient_code_b ?? "-",
            `${r.min_age ?? "-"}/${r.max_age ?? "-"}`, r.reason ?? "-", r.notice_no ?? "-",
            sev(r.severity), r.source, r.active ? "✓" : "—"])}
          empty="DUR 룰 미적재 (식약처/공공데이터 적재 시 작동)" />
      </Card>

      <Card title={`진단(KCD)-수가 연계 — ${n(dx.data?.items)}건`}>
        <Table headers={["수가코드", "필수 상병 prefix", "명칭", "사유", "심각도", "활성"]}
          rows={(dx.data?.items ?? []).map((r) => [
            r.code, r.require_dx_prefixes, r.name ?? "-", r.reason ?? "-", sev(r.severity),
            r.active ? "✓" : "—"])}
          empty="진단-수가 룰 미적재" />
      </Card>

      <Card title={`묶음청구 (번들·재료 별도산정) — ${n(bundle.data?.items)}건`}>
        <Table headers={["패키지", "포함 세부/재료", "유형", "명칭", "사유", "심각도", "활성"]}
          rows={(bundle.data?.items ?? []).map((r) => [
            r.parent_code, r.child_codes, r.rule_type, r.name ?? "-", r.reason ?? "-",
            sev(r.severity), r.active ? "✓" : "—"])}
          empty="묶음 룰 미적재" />
      </Card>

      <Card title={`종별가산율 — ${n(sur.data?.items)}건`}>
        <Table headers={["종별", "가산율", "적용대상", "활성"]}
          rows={(sur.data?.items ?? []).map((r) => [
            r.grade, `${(r.rate * 100).toFixed(1)}%`, r.applies_to, r.active ? "✓" : "—"])}
          empty="종별가산율 미적재 (HIRA 기준 적재 시 행위료에 가산)" />
      </Card>
    </div>
  );
}

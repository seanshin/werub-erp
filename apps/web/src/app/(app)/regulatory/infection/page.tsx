"use client";
/** 규제 — 감염관리 지표(의료기관 인증평가·P3-1): HAI(요로·혈류·수술부위·인공호흡기폐렴)·손위생
 *  수행률·항생제·다제내성균. 월별 분자/분모→비율·목표대비 경보. 입력=감염관리실/관리자. */
import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { ApiError, api } from "@/lib/api";
import { Badge, Button, Card, Input, Modal, Table } from "@/components/ui";
import { QueryErrorBanner } from "@/components/query-guard";
import { monthLocal } from "@/lib/dates";
import { useToast } from "@/components/toast";

const TYPE: Record<string, { kr: string; mult: number; unit: string }> = {
  HAI_UTI: { kr: "요로감염(UTI)", mult: 1000, unit: "‰(재원일)" },
  HAI_BSI: { kr: "혈류감염(BSI)", mult: 1000, unit: "‰(재원일)" },
  HAI_SSI: { kr: "수술부위감염(SSI)", mult: 100, unit: "%(수술)" },
  HAI_VAP: { kr: "인공호흡기폐렴(VAP)", mult: 1000, unit: "‰(기구일)" },
  HAND_HYGIENE: { kr: "손위생 수행률", mult: 100, unit: "%" },
  ANTIBIOTIC: { kr: "항생제 사용률", mult: 100, unit: "%" },
  MDRO: { kr: "다제내성균 분리율", mult: 1000, unit: "‰(재원일)" },
};
const ST: Record<string, { kr: string; tone: "green" | "amber" | "red" | "gray" }> = {
  GOOD: { kr: "양호", tone: "green" }, WARN: { kr: "주의", tone: "amber" },
  ALERT: { kr: "경보", tone: "red" }, NONE: { kr: "목표미설정", tone: "gray" } };

interface Indicator {
  id: number; period: string; indicator_type: string; department_id: number | null;
  numerator: number; denominator: number; multiplier: number; rate: number | null;
  target: number | null; note: string | null; status: string;
}
interface Dashboard {
  period: string; indicators: Indicator[]; alert_count: number;
  alerts: { indicator_type: string; rate: number | null; target: number | null }[];
}

export default function InfectionPage() {
  const qc = useQueryClient();
  const toast = useToast();
  const fail = (e: unknown) => toast.err(e instanceof ApiError ? e.error.message : "처리 실패");
  const [period, setPeriod] = useState(monthLocal());
  const [typeFilter, setTypeFilter] = useState("");
  const [entering, setEntering] = useState(false);

  const dash = useQuery({ queryKey: ["inf-dash", period], queryFn: () =>
    api<Dashboard>(`/regulatory/infection/dashboard?period=${period}`) });
  const list = useQuery({ queryKey: ["inf-list", typeFilter], queryFn: () =>
    api<{ items: Indicator[] }>(`/regulatory/infection/indicators${typeFilter ? `?indicator_type=${typeFilter}` : ""}`) });
  const refresh = () => { qc.invalidateQueries({ queryKey: ["inf-dash"] });
    qc.invalidateQueries({ queryKey: ["inf-list"] }); };

  const fmtRate = (r: number | null, t: string) => r == null ? "—" : `${r}${TYPE[t]?.unit.startsWith("%") ? "%" : ""}`;

  return (
    <div className="space-y-4">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <h1 className="text-lg font-bold">규제 — 감염관리 지표</h1>
        <div className="flex items-center gap-2">
          <label className="text-sm text-slate-600">기준월
            <input value={period} onChange={(e) => setPeriod(e.target.value)}
              placeholder="YYYY-MM"
              className="ml-1 w-24 rounded-md border border-slate-300 px-2 py-1 text-sm" /></label>
          <Button size="sm" onClick={() => setEntering(true)}>지표 입력</Button>
        </div>
      </div>
      <p className="text-xs text-slate-500">
        의료관련감염(HAI)·손위생·항생제·다제내성균 지표를 월별로 집계해 목표 대비 추적합니다(인증평가
        근거). 임상 판정·미생물 결과는 진단검사(HIS) 소관, 본 화면은 지표 집계·추세·목표관리입니다.</p>

      <QueryErrorBanner queries={[dash, list]} />

      {dash.data && dash.data.alert_count > 0 && (
        <p className="rounded-md bg-red-50 px-3 py-1.5 text-sm text-red-700">
          목표 초과(경보) {dash.data.alert_count}건 — {dash.data.alerts.map((a) =>
            `${TYPE[a.indicator_type]?.kr ?? a.indicator_type}(${a.rate}/목표 ${a.target})`).join(", ")}
        </p>
      )}

      <Card title={`당월 지표 대시보드 (${period})`}>
        <div className="grid grid-cols-1 gap-2 sm:grid-cols-2 lg:grid-cols-3">
          {(dash.data?.indicators ?? []).map((i) => {
            const st = ST[i.status] ?? ST.NONE;
            return (
              <div key={i.id} className="rounded-lg border border-slate-200 p-3">
                <div className="flex items-center justify-between">
                  <p className="text-sm font-medium">{TYPE[i.indicator_type]?.kr ?? i.indicator_type}</p>
                  <Badge tone={st.tone}>{st.kr}</Badge>
                </div>
                <p className="mt-1 text-xl font-bold">{fmtRate(i.rate, i.indicator_type)}
                  <span className="ml-1 text-xs font-normal text-slate-400">{TYPE[i.indicator_type]?.unit}</span></p>
                <p className="text-xs text-slate-500">
                  {i.numerator}/{i.denominator}{i.target != null ? ` · 목표 ${i.target}` : ""}</p>
              </div>);
          })}
          {(dash.data?.indicators?.length ?? 0) === 0 && (
            <p className="text-sm text-slate-400">{dash.isFetching ? "불러오는 중…" : "당월 입력된 지표가 없습니다."}</p>)}
        </div>
      </Card>

      <Card title="지표 이력" action={
        <select value={typeFilter} onChange={(e) => setTypeFilter(e.target.value)}
          className="rounded border border-slate-300 px-2 py-1 text-xs">
          <option value="">전체 유형</option>
          {Object.entries(TYPE).map(([k, v]) => <option key={k} value={k}>{v.kr}</option>)}
        </select>}>
        <Table headers={["기간", "지표", "분자/분모", "비율", "목표", "부서", "상태"]}
          rows={(list.data?.items ?? []).map((i) => [
            i.period, TYPE[i.indicator_type]?.kr ?? i.indicator_type,
            `${i.numerator}/${i.denominator}`, fmtRate(i.rate, i.indicator_type),
            i.target != null ? String(i.target) : "—",
            i.department_id != null ? `#${i.department_id}` : "전체",
            <Badge key="s" tone={(ST[i.status] ?? ST.NONE).tone}>{(ST[i.status] ?? ST.NONE).kr}</Badge>,
          ])} empty={list.isFetching ? "불러오는 중…" : "지표 없음"} />
      </Card>

      {entering && <EntryModal defaultPeriod={period}
        onDone={() => { setEntering(false); refresh(); }} onError={fail}
        onClose={() => setEntering(false)} />}
    </div>
  );
}

function EntryModal({ defaultPeriod, onDone, onError, onClose }: {
  defaultPeriod: string; onDone: () => void; onError: (e: unknown) => void; onClose: () => void;
}) {
  const toast = useToast();
  const [f, setF] = useState({ period: defaultPeriod, indicator_type: "HAI_UTI",
    numerator: "", denominator: "", target: "", department_id: "", note: "" });
  const mult = TYPE[f.indicator_type]?.mult ?? 1000;
  const m = useMutation({
    mutationFn: () => api("/regulatory/infection/indicators", { method: "POST", body: JSON.stringify({
      period: f.period, indicator_type: f.indicator_type, numerator: Number(f.numerator),
      denominator: Number(f.denominator), multiplier: mult,
      department_id: f.department_id ? Number(f.department_id) : null,
      target: f.target ? Number(f.target) : null, note: f.note || null }) }),
    onSuccess: () => { toast.ok("지표 입력"); onDone(); }, onError });
  const ok = /^\d{4}-\d{2}$/.test(f.period) && Number(f.numerator) >= 0 && Number(f.denominator) > 0;
  return (
    <Modal title="감염관리 지표 입력" dirty onClose={onClose}>
      <div className="space-y-2">
        <Input label="기간(YYYY-MM)" value={f.period}
          onChange={(e) => setF({ ...f, period: e.target.value })} />
        <label className="block text-sm text-slate-600">지표 유형
          <select value={f.indicator_type} onChange={(e) => setF({ ...f, indicator_type: e.target.value })}
            className="w-full rounded-md border border-slate-300 px-2 py-1.5 text-sm">
            {Object.entries(TYPE).map(([k, v]) => <option key={k} value={k}>{v.kr} — {v.unit}</option>)}
          </select>
        </label>
        <div className="grid grid-cols-2 gap-2">
          <Input label="분자(건수)" value={f.numerator} inputMode="decimal"
            onChange={(e) => setF({ ...f, numerator: e.target.value.replace(/[^0-9.]/g, "") })} />
          <Input label="분모(재원일·기회 등)" value={f.denominator} inputMode="decimal"
            onChange={(e) => setF({ ...f, denominator: e.target.value.replace(/[^0-9.]/g, "") })} />
        </div>
        <p className="text-xs text-slate-400">비율 = 분자/분모 × {mult}
          {mult === 100 ? " (%)" : " (‰ 감염밀도)"}</p>
        <div className="grid grid-cols-2 gap-2">
          <Input label="목표치(선택)" value={f.target} inputMode="decimal"
            onChange={(e) => setF({ ...f, target: e.target.value.replace(/[^0-9.]/g, "") })} />
          <Input label="부서 ID(선택·미입력=전체)" value={f.department_id} inputMode="numeric"
            onChange={(e) => setF({ ...f, department_id: e.target.value.replace(/[^0-9]/g, "") })} />
        </div>
        <Input label="비고" value={f.note} onChange={(e) => setF({ ...f, note: e.target.value })} />
        <Button disabled={m.isPending || !ok} onClick={() => m.mutate()}>입력</Button>
      </div>
    </Modal>
  );
}

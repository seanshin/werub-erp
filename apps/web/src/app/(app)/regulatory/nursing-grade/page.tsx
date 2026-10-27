"use client";
/** 규제 — 간호등급(간호관리료 차등제): 기관 규제 프로필·분기 재원환자수 설정 + 모니터링·충원 시뮬레이션.
 * 조회 STAFF↑ · 설정 저장 ADMIN. 입원료 가산 직결. */
import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api, ApiError } from "@/lib/api";
import { Badge, Button, Card, Input, Table } from "@/components/ui";
import { QueryErrorBanner } from "@/components/query-guard";
import { useToast } from "@/components/toast";
import { useConfirm } from "@/components/confirm";

interface Monitor {
  period: { start: string; end: string };
  hospital_type: string; patient_days: number; patient_days_source: string;
  patient_days_estimate: number;
  nurse: { nurse_days: number; headcount: number;
    nurses: { employee_id: number; employee_no: string; name: string; days: number;
      excluded_absence_days: number; weekly_hours: number | null; fte: number }[];
    excluded_from_count: { employee_no: string; name: string; reason: string }[] };
  avg_patients: number; avg_nurses: number; ratio: number; grade: string | null;
  simulation: { upgrade_nurses_needed: number | null; grade_if_one_leaves: string | null };
  주: string;
}
interface Profile { hospital_type: string; licensed_beds: number | null; source: string }
interface Census { id: number; period_start: string; period_end: string;
  patient_days: number; note: string | null }

const TYPES: [string, string][] = [
  ["TERTIARY", "상급종합"], ["GENERAL", "종합병원"], ["HOSPITAL", "병원"],
  ["CLINIC_BED_BASIS", "의원(병상기준)"]];
const TYPE_LABEL = Object.fromEntries(TYPES);
const SOURCE_LABEL: Record<string, string> = {
  MANUAL: "수동 입력", SAVED_CENSUS: "저장 재원환자수", HIS_CENSUS: "HIS 0시 census",
  BILLING_LINES_ESTIMATE: "입원 라인 추정",
};

export default function NursingGradePage() {
  const qc = useQueryClient();
  const toast = useToast();
  const confirm = useConfirm();

  const prof = useQuery({ queryKey: ["nursing-profile"],
    queryFn: () => api<Profile>("/regulatory/institution-profile") });
  const mon = useQuery({ queryKey: ["nursing-monitor"],
    queryFn: () => api<Monitor>("/regulatory/nursing-grade/monitor") });
  const census = useQuery({ queryKey: ["nursing-census"],
    queryFn: () => api<{ items: Census[] }>("/regulatory/nursing-census") });

  // 프로필 편집 드래프트
  const [pDraft, setPDraft] = useState<{ hospital_type: string; licensed_beds: string } | null>(null);
  const pView = pDraft ?? { hospital_type: prof.data?.hospital_type ?? "HOSPITAL",
    licensed_beds: prof.data?.licensed_beds != null ? String(prof.data.licensed_beds) : "" };
  const saveProfile = useMutation({
    mutationFn: () => api("/regulatory/institution-profile", { method: "PUT",
      body: JSON.stringify({ hospital_type: pView.hospital_type,
        licensed_beds: pView.licensed_beds ? Number(pView.licensed_beds) : null }) }),
    onSuccess: () => {
      toast.ok("기관 규제 프로필 저장 — 간호등급 산정 종별 반영");
      setPDraft(null);
      qc.invalidateQueries({ queryKey: ["nursing-profile"] });
      qc.invalidateQueries({ queryKey: ["nursing-monitor"] });
    },
    onError: (e: unknown) => toast.err(e instanceof ApiError ? e.error.message : "저장 실패 (ADMIN 전용)"),
  });

  // 분기 재원환자수 입력
  const [cInput, setCInput] = useState("");
  const [cNote, setCNote] = useState("");
  const saveCensus = useMutation({
    mutationFn: () => api("/regulatory/nursing-census", { method: "POST",
      body: JSON.stringify({ patient_days: Number(cInput), note: cNote || null }) }),
    onSuccess: () => {
      toast.ok("분기 재원환자수 저장 — 산정 재원일수에 우선 반영");
      setCInput(""); setCNote("");
      qc.invalidateQueries({ queryKey: ["nursing-census"] });
      qc.invalidateQueries({ queryKey: ["nursing-monitor"] });
    },
    onError: (e: unknown) => toast.err(e instanceof ApiError ? e.error.message : "저장 실패 (ADMIN 전용)"),
  });
  const delCensus = useMutation({
    mutationFn: (id: number) => api(`/regulatory/nursing-census/${id}`, { method: "DELETE" }),
    onSuccess: () => {
      toast.ok("삭제됨");
      qc.invalidateQueries({ queryKey: ["nursing-census"] });
      qc.invalidateQueries({ queryKey: ["nursing-monitor"] });
    },
    onError: (e: unknown) => toast.err(e instanceof ApiError ? e.error.message : "삭제 실패"),
  });

  // 간호사 FTE·산정 제외 인라인 편집
  const [staff, setStaff] = useState<Record<number, { wh: string; ex: string }>>({});
  const saveNurse = useMutation({
    mutationFn: (v: { id: number; wh: string; ex: string }) =>
      api(`/regulatory/nursing-staff/${v.id}`, { method: "POST",
        body: JSON.stringify({ weekly_hours: v.wh ? Number(v.wh) : null,
          nursing_exclude_reason: v.ex || null }) }),
    onSuccess: () => {
      toast.ok("간호사 산정 설정 저장");
      qc.invalidateQueries({ queryKey: ["nursing-monitor"] });
    },
    onError: (e: unknown) => toast.err(e instanceof ApiError ? e.error.message : "저장 실패 (ADMIN 전용)"),
  });

  const d = mon.data;
  const input = "rounded-md border border-slate-300 px-2 py-1 text-sm";

  return (
    <div className="space-y-4">
      <h1 className="text-lg font-bold">규제 — 간호등급 모니터링 (간호관리료 차등제)</h1>
      <QueryErrorBanner queries={[prof, mon, census]} />

      <Card title="기관 규제 프로필 (종별·허가병상)"
        action={<Button size="sm" isLoading={saveProfile.isPending}
          disabled={pDraft === null} onClick={() => saveProfile.mutate()}>저장</Button>}>
        <div className="flex flex-wrap items-end gap-3">
          <label className="text-sm">종별
            <select value={pView.hospital_type} className={`${input} ml-2`}
              onChange={(e) => setPDraft({ ...pView, hospital_type: e.target.value })}>
              {TYPES.map(([k, v]) => <option key={k} value={k}>{v}</option>)}
            </select>
          </label>
          <Input label="허가병상수 (의원급 산정용)" value={pView.licensed_beds} inputMode="numeric"
            onChange={(e) => setPDraft({ ...pView, licensed_beds: e.target.value })} />
          <span className="text-xs text-slate-400">
            {prof.data?.source === "db" ? "설정됨" : "미설정 — 기본 병원(HOSPITAL) 가정"}</span>
        </div>
        <p className="mt-2 text-xs text-slate-500">
          종별은 등급 구간을 결정합니다. 대시보드 간호등급 위젯·모니터링이 이 값을 사용합니다.</p>
      </Card>

      <Card title="분기 재원환자수 (실측 — HIS 0시 census 도착 전 수동)">
        <div className="flex flex-wrap items-end gap-2">
          <Input label={`재원일수 합 (당 분기 · 추정 ${d?.patient_days_estimate ?? 0})`}
            value={cInput} inputMode="numeric" onChange={(e) => setCInput(e.target.value)} />
          <Input label="비고" value={cNote} onChange={(e) => setCNote(e.target.value)} />
          <Button isLoading={saveCensus.isPending} disabled={!cInput}
            onClick={() => saveCensus.mutate()}>당 분기 저장</Button>
        </div>
        <Table headers={["산정기간", "재원일수", "비고", ""]}
          rows={(census.data?.items ?? []).map((c) => [
            `${c.period_start} ~ ${c.period_end}`, c.patient_days.toLocaleString(), c.note ?? "-",
            <Button key="d" size="sm" variant="warning" isLoading={delCensus.isPending}
              onClick={async () => {
                if (await confirm({ title: "재원환자수 삭제", danger: true, confirmLabel: "삭제",
                  body: `${c.period_start}~${c.period_end} 항목을 삭제할까요?` }))
                  delCensus.mutate(c.id);
              }}>삭제</Button>,
          ])} empty="저장된 분기 재원환자수 없음 — 미저장 시 추정치 사용" />
      </Card>

      {d && (
        <Card title="현재 등급">
          <div className="flex flex-wrap items-center gap-6 text-sm tabular-nums">
            <span className="text-3xl font-bold">{d.grade ? `${d.grade}등급` : "—"}</span>
            <span>종별 {TYPE_LABEL[d.hospital_type] ?? d.hospital_type}</span>
            <span>평균 환자 {d.avg_patients} / 평균 간호사 {d.avg_nurses}
              → 비율 <b>{d.ratio}</b></span>
            <span>간호사 {d.nurse.headcount}명 · 재직일수 합 {d.nurse.nurse_days.toLocaleString()}</span>
          </div>
          <div className="mt-3 flex flex-wrap gap-3 text-sm">
            {d.simulation.upgrade_nurses_needed !== null && (
              <Badge tone="blue">상위 등급까지 간호사 +{d.simulation.upgrade_nurses_needed}명</Badge>)}
            {d.simulation.grade_if_one_leaves && d.simulation.grade_if_one_leaves !== d.grade && (
              <Badge tone="red">1명 이탈 시 {d.simulation.grade_if_one_leaves}등급 하락 위험</Badge>)}
            {d.simulation.grade_if_one_leaves === d.grade && (
              <Badge tone="green">1명 이탈에도 등급 유지</Badge>)}
          </div>
          <p className="mt-2 text-xs text-slate-400">
            산정기간 {d.period.start} ~ {d.period.end} · 재원 출처 {
              SOURCE_LABEL[d.patient_days_source] ?? d.patient_days_source}</p>
          <p className="mt-1 text-xs text-slate-500">{d.주}</p>
        </Card>
      )}

      {d && (
        <Card title={`간호사 재직일수 (${d.nurse.headcount}명 — 휴직 16일+ 자동 제외 · 주근로시간 FTE 환산)`}>
          <Table headers={["사번", "이름", "산정 재직일", "휴직제외", "주근로시간", "FTE", "제외사유", ""]}
            rows={d.nurse.nurses.map((n) => {
              const dr = staff[n.employee_id] ?? {
                wh: n.weekly_hours != null ? String(n.weekly_hours) : "",
                ex: "" };
              const set = (patch: Partial<{ wh: string; ex: string }>) =>
                setStaff((s) => ({ ...s, [n.employee_id]: { ...dr, ...patch } }));
              return [
                n.employee_no, n.name, n.days,
                n.excluded_absence_days ? `${n.excluded_absence_days}일` : "-",
                <input key="wh" value={dr.wh} inputMode="numeric" placeholder="40"
                  onChange={(e) => set({ wh: e.target.value })} className={`${input} w-16`} />,
                <Badge key="fte" tone={n.fte < 1 ? "amber" : "gray"}>{n.fte}</Badge>,
                <input key="ex" value={dr.ex} placeholder="예: 간호감독"
                  onChange={(e) => set({ ex: e.target.value })} className={`${input} w-28`} />,
                <Button key="s" size="sm" isLoading={saveNurse.isPending}
                  onClick={() => saveNurse.mutate({ id: n.employee_id, wh: dr.wh, ex: dr.ex })}>
                  저장</Button>,
              ];
            })} />
          <p className="mt-2 text-xs text-slate-500">
            주근로시간 미입력=전일제(1.0)·16~40h=0.4~0.9 환산. 제외사유 입력 시 산정 인원에서 제외.</p>
        </Card>
      )}

      {d && (d.nurse.excluded_from_count?.length ?? 0) > 0 && (
        <Card title="산정 제외 간호사 (사유 지정)">
          <Table headers={["사번", "이름", "제외 사유"]}
            rows={(d.nurse.excluded_from_count ?? []).map((n) => [n.employee_no, n.name, n.reason])} />
        </Card>
      )}
    </div>
  );
}

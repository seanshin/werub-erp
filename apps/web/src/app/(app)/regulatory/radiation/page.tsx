"use client";
/** 규제 — 방사선 안전관리(진단용방사선 안전관리규칙). 종사자 피폭선량 연한도(50mSv) 추적·
 *  장비 정기검사 만기. 기록·등록은 관리자(regulatory.master write=ADMIN), 조회는 규제 열람권. */
import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { ApiError, api, auth } from "@/lib/api";
import { Badge, Button, Card, Input, Modal, Table } from "@/components/ui";
import { QueryErrorBanner } from "@/components/query-guard";
import { EmployeePicker } from "@/components/employee-picker";
import { todayLocal } from "@/lib/dates";
import { useToast } from "@/components/toast";

const THIS_YEAR = new Date().getFullYear();

interface DoseEntry { period: string; dose_msv: number; measured_on: string | null; instrument: string | null }
interface DoseEmp { employee_id: number; ytd_msv: number; over_limit: boolean; entries: DoseEntry[] }
interface Doses { year: string; annual_limit: number; items: DoseEmp[] }
interface Equip {
  id: number; equipment_no: string; name: string; last_inspected_on: string | null;
  next_inspection_on: string; overdue: boolean;
}
interface EmpOpt { id: number; employee_no: string; name: string; department: string | null }

export default function RadiationPage() {
  const qc = useQueryClient();
  const toast = useToast();
  const isAdmin = auth.claims()?.role === "ADMIN";   // 기록·등록(DoseModal의 ITAM 피커 포함)=ADMIN 전용
  const fail = (e: unknown) => toast.err(e instanceof ApiError ? e.error.message : "처리 실패");
  const [year, setYear] = useState(THIS_YEAR);
  const [modal, setModal] = useState<null | "dose" | "equip">(null);

  const doses = useQuery({ queryKey: ["rad-doses", year], queryFn: () =>
    api<Doses>(`/regulatory/radiation/doses?year=${year}`) });
  const equip = useQuery({ queryKey: ["rad-equip"], queryFn: () =>
    api<{ items: Equip[] }>("/regulatory/radiation/equipment") });
  // 직원 표시명(실패·범위 밖은 id 폴백). /itam/employees는 itam.assets read(DIRECTOR/ADMIN/IT관리자)
  // 전용이라 이 화면의 부서장(MANAGER)에게는 403이 났다 → 그 외 역할은 인사 EP(hr.employees read·
  // MANAGER=본부서 행스코프)로 이름을 해소한다. 타 부서 종사자는 #id로 남는다(권한 확대 없음).
  const role = auth.claims()?.role ?? "";
  const itamNames = ["DIRECTOR", "ADMIN", "IT_MANAGER"].includes(role);
  const emps = useQuery({ queryKey: ["rad-emp-names", itamNames], retry: false, queryFn: () =>
    api<{ items: EmpOpt[] }>(itamNames ? "/itam/employees" : "/hr/employees?size=200") });
  const empName = (id: number) => {
    const e = emps.data?.items.find((x) => x.id === id);
    return e ? `${e.name} (${e.employee_no})` : `#${id}`;
  };
  const refresh = () => { qc.invalidateQueries({ queryKey: ["rad-doses"] });
    qc.invalidateQueries({ queryKey: ["rad-equip"] }); };

  return (
    <div className="space-y-4">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <h1 className="text-lg font-bold">규제 — 방사선 안전관리</h1>
        {isAdmin && (
          <div className="flex gap-2">
            <Button size="sm" onClick={() => setModal("dose")}>피폭선량 기록</Button>
            <Button size="sm" variant="secondary" onClick={() => setModal("equip")}>장비 등록</Button>
          </div>)}
      </div>
      <p className="text-xs text-slate-500">
        종사자 연간 피폭선량 한도 50mSv(법정) 누적 추적·장비 정기검사 만기 관리.
        기록·등록은 안전관리자(관리자 권한), 초과·만기를 색으로 경고합니다.</p>

      <QueryErrorBanner queries={[doses, equip]} />

      <Card title="종사자 피폭선량" action={
        <label className="text-xs text-slate-600">연도
          <input type="number" value={year} onChange={(e) => setYear(Number(e.target.value))}
            className="ml-1 w-20 rounded border border-slate-300 px-1 py-0.5 text-xs" />
        </label>}>
        <Table headers={["종사자", "연 누적(YTD)", "한도", "상태", "측정 이력"]}
          rows={(doses.data?.items ?? []).map((d) => [
            empName(d.employee_id),
            `${d.ytd_msv} mSv`,
            `${doses.data!.annual_limit} mSv`,
            <Badge key="s" tone={d.over_limit ? "red" : d.ytd_msv >= doses.data!.annual_limit * 0.8 ? "amber" : "green"}>
              {d.over_limit ? "한도 초과" : d.ytd_msv >= doses.data!.annual_limit * 0.8 ? "주의(80%+)" : "정상"}</Badge>,
            <span key="e" className="text-xs text-slate-500">
              {d.entries.map((en) => `${en.period} ${en.dose_msv}mSv`).join(" · ")}</span>,
          ])} empty={doses.isFetching ? "불러오는 중…" : "기록 없음"} />
      </Card>

      <Card title="방사선 장비 정기검사">
        <Table headers={["장비번호", "명칭", "최근검사", "차기검사", "상태"]}
          rows={(equip.data?.items ?? []).map((e) => [
            e.equipment_no, e.name, e.last_inspected_on ?? "—", e.next_inspection_on,
            <Badge key="s" tone={e.overdue ? "red" : "green"}>{e.overdue ? "만기 경과" : "유효"}</Badge>,
          ])} empty={equip.isFetching ? "불러오는 중…" : "등록 장비 없음"} />
      </Card>

      {modal === "dose" && <DoseModal onDone={() => { setModal(null); refresh(); }}
        onError={fail} onClose={() => setModal(null)} />}
      {modal === "equip" && <EquipModal onDone={() => { setModal(null); refresh(); }}
        onError={fail} onClose={() => setModal(null)} />}
    </div>
  );
}

function DoseModal({ onDone, onError, onClose }: {
  onDone: () => void; onError: (e: unknown) => void; onClose: () => void;
}) {
  const toast = useToast();
  const [empId, setEmpId] = useState<number | null>(null);
  const [f, setF] = useState({ period: `${THIS_YEAR}-Q1`, dose_msv: "", measured_on: todayLocal(),
    instrument: "" });
  const m = useMutation({
    mutationFn: () => api<{ ytd_msv: number; over_limit: boolean }>(
      "/regulatory/radiation/doses", { method: "POST", body: JSON.stringify({
        employee_id: empId, period: f.period, dose_msv: Number(f.dose_msv),
        measured_on: f.measured_on, instrument: f.instrument || null }) }),
    onSuccess: (r) => { toast.ok(r.over_limit ? `기록 — YTD ${r.ytd_msv}mSv ⚠한도 초과!`
      : `기록 — YTD ${r.ytd_msv}mSv`); onDone(); }, onError });
  return (
    <Modal title="피폭선량 기록" dirty onClose={onClose}>
      <div className="space-y-2">
        <label className="block text-sm text-slate-600">종사자
          <div className="mt-1"><EmployeePicker value={empId} onPick={setEmpId} /></div>
        </label>
        <Input label="측정 주기(yyyy-Qn 또는 yyyy-mm)" value={f.period}
          onChange={(e) => setF({ ...f, period: e.target.value })} />
        <Input label="피폭선량(mSv)" value={f.dose_msv} inputMode="decimal"
          onChange={(e) => setF({ ...f, dose_msv: e.target.value.replace(/[^0-9.]/g, "") })} />
        <Input label="측정일" type="date" value={f.measured_on}
          onChange={(e) => setF({ ...f, measured_on: e.target.value })} />
        <Input label="측정기(선택)" value={f.instrument}
          onChange={(e) => setF({ ...f, instrument: e.target.value })} />
        <Button disabled={m.isPending || !empId || !(Number(f.dose_msv) >= 0) || !f.dose_msv || !f.period.trim()}
          onClick={() => m.mutate()}>기록</Button>
      </div>
    </Modal>
  );
}

function EquipModal({ onDone, onError, onClose }: {
  onDone: () => void; onError: (e: unknown) => void; onClose: () => void;
}) {
  const toast = useToast();
  const [f, setF] = useState({ equipment_no: "", name: "", next_inspection_on: "", last_inspected_on: "" });
  const m = useMutation({
    mutationFn: () => api("/regulatory/radiation/equipment", { method: "POST", body: JSON.stringify({
      equipment_no: f.equipment_no, name: f.name, next_inspection_on: f.next_inspection_on,
      last_inspected_on: f.last_inspected_on || null }) }),
    onSuccess: () => { toast.ok("장비 등록"); onDone(); }, onError });
  return (
    <Modal title="방사선 장비 등록" dirty onClose={onClose}>
      <div className="space-y-2">
        <Input label="장비번호" value={f.equipment_no}
          onChange={(e) => setF({ ...f, equipment_no: e.target.value })} />
        <Input label="명칭" value={f.name} onChange={(e) => setF({ ...f, name: e.target.value })} />
        <Input label="최근 검사일(선택)" type="date" value={f.last_inspected_on}
          onChange={(e) => setF({ ...f, last_inspected_on: e.target.value })} />
        <Input label="차기 검사 만기일" type="date" value={f.next_inspection_on}
          onChange={(e) => setF({ ...f, next_inspection_on: e.target.value })} />
        <Button disabled={m.isPending || !f.equipment_no.trim() || !f.name.trim() || !f.next_inspection_on}
          onClick={() => m.mutate()}>등록</Button>
      </div>
    </Modal>
  );
}

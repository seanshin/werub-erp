"use client";
/** 인사 — 원내 교육이수 (Tier3·0228): 과정 마스터·회차 편성·참석자 이수·현황 매트릭스.
 *  법정의무교육(/hr/compliance)과 별개로 임의 사내 과정(감염관리·CPR·직무·신규자 등) 관리. */
import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { ApiError, api } from "@/lib/api";
import { Badge, Button, Card, Input, Modal, Table } from "@/components/ui";
import { QueryErrorBanner } from "@/components/query-guard";
import { EmployeePicker } from "@/components/employee-picker";
import { todayLocal } from "@/lib/dates";
import { useToast } from "@/components/toast";

const CATEGORY: Record<string, string> = {
  STATUTORY: "법정", JOB: "직무", SAFETY: "안전", INFECTION: "감염관리",
  ONBOARDING: "신규자", QUALITY: "질관리", OTHER: "기타",
};
const METHOD: Record<string, string> = { OFFLINE: "집합", ONLINE: "온라인", EXTERNAL: "외부" };
const STATE: Record<string, { label: string; tone: "green" | "amber" | "red" | "gray" }> = {
  OK: { label: "이수", tone: "green" }, DUE_SOON: { label: "임박", tone: "amber" },
  OVERDUE: { label: "만료", tone: "red" }, NONE: { label: "미이수", tone: "gray" },
  "N/A": { label: "—", tone: "gray" },
};

interface Course {
  id: number; code: string; title: string; category: string; cycle_months: number;
  target: string; target_value: string | null; mandatory: boolean; active: boolean;
  note: string | null; sessions: number;
}
interface Session {
  id: number; held_on: string; hours: number; method: string;
  instructor: string | null; location: string | null; note: string | null; completions: number;
}
interface Cell { state: string; completed_on?: string; valid_until?: string }
interface Status {
  as_of: string;
  courses: { id: number; code: string; title: string; category: string; cycle_months: number }[];
  items: { employee_id: number; employee_no: string; name: string;
    job_category: string | null; cells: Record<string, Cell> }[];
  summary: { course_id: number; code: string; title: string; required: number;
    compliant: number; rate: number | null }[];
}

export default function TrainingPage() {
  const qc = useQueryClient();
  const toast = useToast();
  const fail = (e: unknown) => toast.err(e instanceof ApiError ? e.error.message : "처리 실패");
  const [tab, setTab] = useState<"courses" | "status">("courses");
  const [creating, setCreating] = useState(false);
  const [sessionsOf, setSessionsOf] = useState<Course | null>(null);

  const courses = useQuery({ queryKey: ["tcourses"], queryFn: () =>
    api<{ items: Course[] }>("/hr/training/courses") });
  const status = useQuery({ enabled: tab === "status", queryKey: ["tstatus"], queryFn: () =>
    api<Status>("/hr/training/status") });
  const refresh = () => { qc.invalidateQueries({ queryKey: ["tcourses"] });
    qc.invalidateQueries({ queryKey: ["tstatus"] }); };

  const create = useMutation({
    mutationFn: (b: unknown) => api("/hr/training/courses", { method: "POST", body: JSON.stringify(b) }),
    onSuccess: () => { toast.ok("과정 등록"); setCreating(false); refresh(); }, onError: fail });

  return (
    <div className="space-y-4">
      <div className="flex items-center gap-2">
        <h1 className="text-lg font-bold">인사 — 원내 교육이수</h1>
        <div className="ml-auto flex gap-1">
          <Button variant={tab === "courses" ? "primary" : "secondary"} size="sm"
            onClick={() => setTab("courses")}>과정·회차</Button>
          <Button variant={tab === "status" ? "primary" : "secondary"} size="sm"
            onClick={() => setTab("status")}>이수 현황</Button>
        </div>
      </div>
      <p className="text-xs text-slate-500">
        감염관리·심폐소생술·직무·신규자 오리엔테이션 등 사내 교육 과정과 회차·이수를 관리합니다
        (법정의무교육은 별도 화면). 필수 과정은 대상 재직자 이수 현황이 매트릭스로 집계됩니다.</p>

      {tab === "courses" ? (
        <>
          <QueryErrorBanner queries={[courses]} />
          <Card title="교육 과정" action={<Button size="sm" onClick={() => setCreating(true)}>과정 등록</Button>}>
            <Table headers={["코드", "과정명", "분류", "주기", "대상", "필수", "회차", ""]}
              rows={(courses.data?.items ?? []).map((c) => [
                c.code, c.title, CATEGORY[c.category] ?? c.category,
                c.cycle_months > 0 ? `${c.cycle_months}개월` : "일회성",
                c.target === "ALL" ? "전체" : `${c.target}:${c.target_value ?? ""}`,
                c.mandatory ? <Badge key="m" tone="blue">필수</Badge> : "선택",
                c.sessions,
                <Button key="s" variant="secondary" size="sm" onClick={() => setSessionsOf(c)}>회차·이수</Button>,
              ])} empty={courses.isFetching ? "불러오는 중…" : "등록된 과정 없음"} />
          </Card>
        </>
      ) : (
        <>
          <QueryErrorBanner queries={[status]} />
          {status.data && (
            <Card title={`이수율 요약 (기준 ${status.data.as_of})`}>
              <Table headers={["과정", "대상", "이수", "이수율"]}
                rows={status.data.summary.map((s) => [
                  `${s.title} (${s.code})`, s.required, s.compliant,
                  s.rate != null ? `${(s.rate * 100).toFixed(1)}%` : "—",
                ])} empty="필수 과정 없음" />
            </Card>
          )}
          <Card title="재직자 × 필수과정 매트릭스">
            <div className="overflow-x-auto">
              <Table headers={["사번", "이름", "직종", ...(status.data?.courses ?? []).map((c) => c.code)]}
                rows={(status.data?.items ?? []).map((i) => [
                  i.employee_no, i.name, i.job_category ?? "—",
                  ...(status.data?.courses ?? []).map((c) => {
                    const st = STATE[i.cells[c.code]?.state ?? "NONE"] ?? STATE.NONE;
                    return <Badge key={c.code} tone={st.tone}>{st.label}</Badge>;
                  }),
                ])} empty={status.isFetching ? "불러오는 중…" : "재직자 없음"} />
            </div>
          </Card>
        </>
      )}

      {creating && (
        <Modal title="교육 과정 등록" dirty onClose={() => setCreating(false)}>
          <CourseForm onSubmit={(b) => create.mutate(b)} isLoading={create.isPending}
            onCancel={() => setCreating(false)} />
        </Modal>
      )}
      {sessionsOf && (
        <SessionsModal course={sessionsOf} onClose={() => setSessionsOf(null)} onChanged={refresh} onError={fail} />
      )}
    </div>
  );
}

function CourseForm({ onSubmit, isLoading, onCancel }: {
  onSubmit: (b: unknown) => void; isLoading: boolean; onCancel: () => void;
}) {
  const [f, setF] = useState({ code: "", title: "", category: "JOB", cycle_months: "0",
    target: "ALL", target_value: "", mandatory: true });
  return (
    <div className="space-y-2">
      <div className="grid grid-cols-2 gap-2">
        <Input label="코드" value={f.code} onChange={(e) => setF({ ...f, code: e.target.value })}
          placeholder="예: CPR" />
        <Input label="과정명" value={f.title} onChange={(e) => setF({ ...f, title: e.target.value })} />
      </div>
      <div className="grid grid-cols-2 gap-2">
        <label className="block text-sm text-slate-600">분류
          <select value={f.category} onChange={(e) => setF({ ...f, category: e.target.value })}
            className="w-full rounded-md border border-slate-300 px-2 py-1.5 text-sm">
            {Object.entries(CATEGORY).map(([k, v]) => <option key={k} value={k}>{v}</option>)}
          </select>
        </label>
        <Input label="주기(개월·0=일회성)" value={f.cycle_months} inputMode="numeric"
          onChange={(e) => setF({ ...f, cycle_months: e.target.value.replace(/[^0-9]/g, "") })} />
      </div>
      <div className="grid grid-cols-2 gap-2">
        <label className="block text-sm text-slate-600">대상
          <select value={f.target} onChange={(e) => setF({ ...f, target: e.target.value })}
            className="w-full rounded-md border border-slate-300 px-2 py-1.5 text-sm">
            <option value="ALL">전체</option>
            <option value="JOB_CATEGORY">직종</option>
            <option value="DEPT">부서</option>
          </select>
        </label>
        {f.target !== "ALL" && (
          <Input label={f.target === "JOB_CATEGORY" ? "직종코드" : "부서ID"} value={f.target_value}
            onChange={(e) => setF({ ...f, target_value: e.target.value })} />)}
      </div>
      <label className="flex items-center gap-1 text-sm text-slate-600">
        <input type="checkbox" checked={f.mandatory}
          onChange={(e) => setF({ ...f, mandatory: e.target.checked })} /> 필수 과정(이수 현황 집계 대상)
      </label>
      <div className="flex gap-2">
        <Button disabled={isLoading || !f.code.trim() || !f.title.trim()}
          onClick={() => onSubmit({ code: f.code, title: f.title, category: f.category,
            cycle_months: Number(f.cycle_months), target: f.target,
            target_value: f.target === "ALL" ? null : f.target_value, mandatory: f.mandatory })}>
          등록</Button>
        <Button variant="secondary" onClick={onCancel}>닫기</Button>
      </div>
    </div>
  );
}

function SessionsModal({ course, onClose, onChanged, onError }: {
  course: Course; onClose: () => void; onChanged: () => void; onError: (e: unknown) => void;
}) {
  const toast = useToast();
  const qc = useQueryClient();
  const [newS, setNewS] = useState({ held_on: todayLocal(), hours: "1", method: "OFFLINE",
    instructor: "", location: "" });
  const [attendOf, setAttendOf] = useState<number | null>(null);
  const [empId, setEmpId] = useState<number | null>(null);
  const sessions = useQuery({ queryKey: ["tsessions", course.id], queryFn: () =>
    api<{ items: Session[] }>(`/hr/training/courses/${course.id}/sessions`) });
  const refresh = () => { qc.invalidateQueries({ queryKey: ["tsessions", course.id] }); onChanged(); };

  const addSession = useMutation({
    mutationFn: () => api(`/hr/training/courses/${course.id}/sessions`, { method: "POST",
      body: JSON.stringify({ held_on: newS.held_on, hours: Number(newS.hours),
        method: newS.method, instructor: newS.instructor || null,
        location: newS.location || null }) }),
    onSuccess: () => { toast.ok("회차 추가"); refresh(); }, onError });
  const addCompletion = useMutation({
    mutationFn: () => api(`/hr/training/sessions/${attendOf}/completions`, { method: "POST",
      body: JSON.stringify({ employee_id: empId }) }),
    onSuccess: () => { toast.ok("이수 등록"); setEmpId(null); refresh(); }, onError });

  return (
    <Modal title={`${course.title} — 회차·이수`} size="lg" onClose={onClose}>
      <div className="space-y-3">
        <div className="rounded-md border border-slate-200 p-2">
          <p className="mb-1 text-sm font-medium text-slate-600">회차 추가</p>
          <div className="grid grid-cols-2 gap-2 sm:grid-cols-3">
            <Input label="개최일" type="date" value={newS.held_on}
              onChange={(e) => setNewS({ ...newS, held_on: e.target.value })} />
            <Input label="시간" value={newS.hours} inputMode="decimal"
              onChange={(e) => setNewS({ ...newS, hours: e.target.value.replace(/[^0-9.]/g, "") })} />
            <label className="block text-sm text-slate-600">방식
              <select value={newS.method} onChange={(e) => setNewS({ ...newS, method: e.target.value })}
                className="w-full rounded-md border border-slate-300 px-2 py-1.5 text-sm">
                {Object.entries(METHOD).map(([k, v]) => <option key={k} value={k}>{v}</option>)}
              </select>
            </label>
            <Input label="강사" value={newS.instructor}
              onChange={(e) => setNewS({ ...newS, instructor: e.target.value })} />
            <Input label="장소" value={newS.location}
              onChange={(e) => setNewS({ ...newS, location: e.target.value })} />
          </div>
          <Button size="sm" className="mt-2" disabled={addSession.isPending || !newS.held_on}
            onClick={() => addSession.mutate()}>회차 추가</Button>
        </div>
        <Table headers={["개최일", "방식", "시간", "강사", "이수자", ""]}
          rows={(sessions.data?.items ?? []).map((s) => [
            s.held_on, METHOD[s.method] ?? s.method, s.hours, s.instructor ?? "—", s.completions,
            <Button key="a" variant="secondary" size="sm"
              onClick={() => setAttendOf(attendOf === s.id ? null : s.id)}>
              {attendOf === s.id ? "닫기" : "이수 등록"}</Button>,
          ])} empty={sessions.isFetching ? "불러오는 중…" : "회차 없음"} />
        {attendOf != null && (
          <div className="flex items-end gap-2 rounded-md border border-slate-200 p-2">
            <div className="flex-1"><EmployeePicker value={empId} onPick={setEmpId} /></div>
            <Button size="sm" disabled={addCompletion.isPending || empId == null}
              onClick={() => addCompletion.mutate()}>이수 등록</Button>
          </div>
        )}
      </div>
    </Modal>
  );
}

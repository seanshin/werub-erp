"use client";
/** 인사 — 채용 ATS (P3-9·Tier3): 공고·지원자·전형 퍼널. 지원자 개인정보로 관리자/부서장 한정. */
import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { ApiError, api } from "@/lib/api";
import { Badge, Button, Card, Input, Modal, Table } from "@/components/ui";
import { QueryErrorBanner } from "@/components/query-guard";
import { todayLocal } from "@/lib/dates";
import { useToast } from "@/components/toast";

const PSTATUS: Record<string, { kr: string; tone: "green" | "gray" | "blue" }> = {
  OPEN: { kr: "모집중", tone: "green" }, CLOSED: { kr: "마감", tone: "gray" },
  FILLED: { kr: "충원완료", tone: "blue" } };
const STAGE: Record<string, { kr: string; tone: "gray" | "blue" | "amber" | "green" | "red" }> = {
  APPLIED: { kr: "지원", tone: "gray" }, SCREENING: { kr: "서류", tone: "blue" },
  INTERVIEW: { kr: "면접", tone: "amber" }, OFFER: { kr: "합격통보", tone: "blue" },
  HIRED: { kr: "채용", tone: "green" }, REJECTED: { kr: "탈락", tone: "red" } };
const NEXT: Record<string, string[]> = {
  APPLIED: ["SCREENING", "REJECTED"], SCREENING: ["INTERVIEW", "REJECTED"],
  INTERVIEW: ["OFFER", "REJECTED"], OFFER: ["HIRED", "REJECTED"], HIRED: [], REJECTED: [] };

interface Posting { id: number; title: string; department_id: number | null;
  employment_type: string; headcount: number; status: string; posted_on: string;
  close_on: string | null; note: string | null; applicant_count: number }
interface Applicant { id: number; name: string; contact: string | null; source: string | null;
  stage: string; applied_on: string; note: string | null }
interface Funnel { by_stage: Record<string, number>; total: number; hired: number }

export default function RecruitmentPage() {
  const qc = useQueryClient();
  const toast = useToast();
  const fail = (e: unknown) => toast.err(e instanceof ApiError ? e.error.message : "처리 실패");
  const [creating, setCreating] = useState(false);
  const [sel, setSel] = useState<Posting | null>(null);
  const [adding, setAdding] = useState(false);

  const postings = useQuery({ queryKey: ["postings"], queryFn: () =>
    api<{ items: Posting[] }>("/hr/job-postings") });
  const applicants = useQuery({ enabled: sel !== null, queryKey: ["applicants", sel?.id], queryFn: () =>
    api<{ items: Applicant[] }>(`/hr/applicants?posting_id=${sel!.id}`) });
  const funnel = useQuery({ enabled: sel !== null, queryKey: ["funnel", sel?.id], queryFn: () =>
    api<Funnel>(`/hr/job-postings/${sel!.id}/funnel`) });
  const refresh = () => { qc.invalidateQueries({ queryKey: ["postings"] });
    if (sel) { qc.invalidateQueries({ queryKey: ["applicants", sel.id] });
      qc.invalidateQueries({ queryKey: ["funnel", sel.id] }); } };

  const setStatus = useMutation({
    mutationFn: (v: { id: number; status: string }) =>
      api(`/hr/job-postings/${v.id}/status`, { method: "POST", body: JSON.stringify({ status: v.status }) }),
    onSuccess: () => { toast.ok("공고 상태 변경"); refresh(); }, onError: fail });
  const advance = useMutation({
    mutationFn: (v: { id: number; stage: string }) =>
      api(`/hr/applicants/${v.id}/stage`, { method: "POST", body: JSON.stringify({ stage: v.stage }) }),
    onSuccess: () => { toast.ok("전형 진행"); refresh(); }, onError: fail });

  return (
    <div className="space-y-4">
      <div className="flex items-center justify-between">
        <h1 className="text-lg font-bold">인사 — 채용 관리(ATS)</h1>
        <Button onClick={() => setCreating(true)}>공고 등록</Button>
      </div>
      <p className="text-xs text-slate-500">
        채용 공고·지원자·전형 단계 관리. 지원자 개인정보(이름·연락처)를 담아 관리자/부서장 전용입니다.</p>

      <QueryErrorBanner queries={[postings]} />

      <div className="grid gap-4 lg:grid-cols-2">
        <Card title="채용 공고">
          <Table headers={["제목", "유형", "인원", "지원", "상태", ""]}
            rows={(postings.data?.items ?? []).map((p) => [
              <button key="t" className="text-left text-blue-600 hover:underline"
                onClick={() => setSel(p)}>{p.title}</button>,
              p.employment_type, `${p.headcount}명`, `${p.applicant_count}명`,
              <Badge key="s" tone={(PSTATUS[p.status] ?? { tone: "gray" as const }).tone}>
                {(PSTATUS[p.status] ?? { kr: p.status }).kr}</Badge>,
              <span key="a" className="flex gap-1">
                {p.status === "OPEN" && <>
                  <Button size="sm" variant="secondary" disabled={setStatus.isPending}
                    onClick={() => setStatus.mutate({ id: p.id, status: "FILLED" })}>충원</Button>
                  <Button size="sm" variant="secondary" disabled={setStatus.isPending}
                    onClick={() => setStatus.mutate({ id: p.id, status: "CLOSED" })}>마감</Button>
                </>}
              </span>,
            ])} empty={postings.isFetching ? "불러오는 중…" : "공고 없음"} />
        </Card>

        <Card title={sel ? `지원자 — ${sel.title}` : "지원자"} action={
          sel ? <Button size="sm" onClick={() => setAdding(true)}>지원자 추가</Button> : undefined}>
          {!sel ? <p className="text-sm text-slate-400">좌측 공고를 선택하세요.</p> : (
            <div className="space-y-3">
              {funnel.data && (
                <div className="flex flex-wrap gap-1 text-xs">
                  {["APPLIED", "SCREENING", "INTERVIEW", "OFFER", "HIRED", "REJECTED"].map((s) => (
                    <span key={s} className="rounded border border-slate-200 px-1.5 py-0.5">
                      {STAGE[s].kr} {funnel.data!.by_stage[s] ?? 0}</span>))}
                </div>
              )}
              <Table headers={["이름", "연락처", "경로", "지원일", "단계", "진행"]}
                rows={(applicants.data?.items ?? []).map((a) => [
                  a.name, a.contact ?? "—", a.source ?? "—", a.applied_on,
                  <Badge key="s" tone={(STAGE[a.stage] ?? { tone: "gray" as const }).tone}>
                    {(STAGE[a.stage] ?? { kr: a.stage }).kr}</Badge>,
                  <span key="n" className="flex flex-wrap gap-1">
                    {(NEXT[a.stage] ?? []).map((ns) => (
                      <Button key={ns} size="sm" variant={ns === "REJECTED" ? "danger" : "secondary"}
                        disabled={advance.isPending}
                        onClick={() => advance.mutate({ id: a.id, stage: ns })}>{STAGE[ns].kr}</Button>))}
                    {(NEXT[a.stage] ?? []).length === 0 && <span className="text-xs text-slate-400">종결</span>}
                  </span>,
                ])} empty={applicants.isFetching ? "불러오는 중…" : "지원자 없음"} />
            </div>
          )}
        </Card>
      </div>

      {creating && <PostingModal onDone={() => { setCreating(false); refresh(); }}
        onError={fail} onClose={() => setCreating(false)} />}
      {adding && sel && <ApplicantModal postingId={sel.id}
        onDone={() => { setAdding(false); refresh(); }} onError={fail}
        onClose={() => setAdding(false)} />}
    </div>
  );
}

function PostingModal({ onDone, onError, onClose }: {
  onDone: () => void; onError: (e: unknown) => void; onClose: () => void;
}) {
  const toast = useToast();
  const [f, setF] = useState({ title: "", employment_type: "REGULAR", headcount: "1",
    posted_on: todayLocal(), close_on: "", note: "" });
  const m = useMutation({
    mutationFn: () => api("/hr/job-postings", { method: "POST", body: JSON.stringify({
      title: f.title, employment_type: f.employment_type, headcount: Number(f.headcount),
      posted_on: f.posted_on, close_on: f.close_on || null, note: f.note || null }) }),
    onSuccess: () => { toast.ok("공고 등록"); onDone(); }, onError });
  return (
    <Modal title="채용 공고 등록" dirty onClose={onClose}>
      <div className="space-y-2">
        <Input label="제목" value={f.title} onChange={(e) => setF({ ...f, title: e.target.value })} />
        <label className="block text-sm text-slate-600">고용형태
          <select value={f.employment_type} onChange={(e) => setF({ ...f, employment_type: e.target.value })}
            className="w-full rounded-md border border-slate-300 px-2 py-1.5 text-sm">
            <option value="REGULAR">정규직</option><option value="FIXED_TERM">계약직</option>
            <option value="PART_TIME">단시간</option>
          </select>
        </label>
        <Input label="채용 인원" value={f.headcount} inputMode="numeric"
          onChange={(e) => setF({ ...f, headcount: e.target.value.replace(/[^0-9]/g, "") })} />
        <Input label="공고일" type="date" value={f.posted_on}
          onChange={(e) => setF({ ...f, posted_on: e.target.value })} />
        <Input label="마감일(선택)" type="date" value={f.close_on}
          onChange={(e) => setF({ ...f, close_on: e.target.value })} />
        <Input label="비고" value={f.note} onChange={(e) => setF({ ...f, note: e.target.value })} />
        <Button disabled={m.isPending || !f.title.trim() || !(Number(f.headcount) > 0)}
          onClick={() => m.mutate()}>등록</Button>
      </div>
    </Modal>
  );
}

function ApplicantModal({ postingId, onDone, onError, onClose }: {
  postingId: number; onDone: () => void; onError: (e: unknown) => void; onClose: () => void;
}) {
  const toast = useToast();
  const [f, setF] = useState({ name: "", contact: "", source: "", applied_on: todayLocal(), note: "" });
  const m = useMutation({
    mutationFn: () => api("/hr/applicants", { method: "POST", body: JSON.stringify({
      posting_id: postingId, name: f.name, contact: f.contact || null, source: f.source || null,
      applied_on: f.applied_on, note: f.note || null }) }),
    onSuccess: () => { toast.ok("지원자 접수"); onDone(); }, onError });
  return (
    <Modal title="지원자 접수" dirty onClose={onClose}>
      <div className="space-y-2">
        <Input label="이름" value={f.name} onChange={(e) => setF({ ...f, name: e.target.value })} />
        <Input label="연락처/이메일(선택)" value={f.contact}
          onChange={(e) => setF({ ...f, contact: e.target.value })} />
        <Input label="지원 경로(선택)" value={f.source}
          onChange={(e) => setF({ ...f, source: e.target.value })} />
        <Input label="지원일" type="date" value={f.applied_on}
          onChange={(e) => setF({ ...f, applied_on: e.target.value })} />
        <Input label="비고" value={f.note} onChange={(e) => setF({ ...f, note: e.target.value })} />
        <Button disabled={m.isPending || !f.name.trim()} onClick={() => m.mutate()}>접수</Button>
      </div>
    </Modal>
  );
}

"use client";
/** 자금결재 — 결재선 관리(0212): 개인 결재선 CRUD + 공용(ORG) 결재선 신설/변경/폐기 요청 + 관리자 검토.
 *  공용은 요청→ADMIN 승인 시에만 프로비저닝(직무분리 — 요청자 본인 검토 불가·서버 강제). */
import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { ApiError, api, auth } from "@/lib/api";
import { Badge, Button, Card, Input, Modal, Table } from "@/components/ui";
import { QueryErrorBanner } from "@/components/query-guard";
import { Combobox } from "@/components/combobox";
import { useConfirm } from "@/components/confirm";
import { useToast } from "@/components/toast";

const ROLE_LABEL: Record<string, string> = {
  DIRECTOR: "원장", ADMIN: "관리자", MANAGER: "부서장", STAFF: "직원",
  IT_MANAGER: "IT관리자", FINANCE_MANAGER: "회계관리자", LOGISTICS_MANAGER: "물류관리자",
};
const DOC_LABEL: Record<string, string> = {
  ANY: "전체", GENERAL_DRAFT: "기안문서", EXPENSE: "경비", PO: "발주", VOUCHER: "전표",
  OUTSOURCING_CONTRACT: "위수탁계약", MEDICAL_CONTRACT: "의료진계약", ITAM_DISPOSAL: "IT자산폐기",
};
const REQ_TONE: Record<string, "gray" | "amber" | "green" | "red"> = {
  REQUESTED: "amber", APPROVED: "green", REJECTED: "red", CANCELED: "gray",
};
const REQ_TYPE_LABEL: Record<string, string> = { NEW: "신설", MODIFY: "변경", RETIRE: "폐기" };

interface Step { type: string; value: string | null }
interface Tpl {
  id: number; name: string; scope: string; owner_user_id: number | null;
  doc_types: string[]; steps: Step[]; active: boolean;
}
interface LineReq {
  id: number; req_no: string; request_type: string; target_template_id: number | null;
  proposed_name: string | null; proposed_steps: Step[]; doc_types: string[];
  reason: string; status: string; requested_by: number; reviewed_by: number | null;
  review_note: string | null;
}
interface Person { id: number; name: string; department_id: number | null }   // v1.203 — role 미노출(최소필요)

function stepText(s: Step, people: Person[]): string {
  if (s.type === "DEPT_HEAD") return "부서장(기안부서)";
  if (s.type === "ROLE") return ROLE_LABEL[s.value ?? ""] ?? s.value ?? "?";
  const p = people.find((x) => String(x.id) === s.value);
  return p ? `${p.name}(#${p.id})` : `사용자#${s.value}`;
}

export default function ApprovalLinesPage() {
  const qc = useQueryClient();
  const toast = useToast();
  const confirm = useConfirm();
  const fail = (e: unknown) => toast.err(e instanceof ApiError ? e.error.message : "처리 실패");
  const myId = Number(auth.claims()?.sub ?? 0);
  const isAdmin = auth.claims()?.role === "ADMIN";

  const [editing, setEditing] = useState<Tpl | "new" | null>(null);     // 개인 결재선
  const [requesting, setRequesting] = useState<{ type: string; target?: Tpl } | null>(null);
  const [reviewing, setReviewing] = useState<LineReq | null>(null);

  const templates = useQuery({ queryKey: ["appr-lines"], queryFn: () =>
    api<{ items: Tpl[] }>("/approvals/lines") });
  const directory = useQuery({ queryKey: ["appr-directory"], queryFn: () =>
    api<{ items: Person[] }>("/approvals/lines/directory") });
  const myReqs = useQuery({ queryKey: ["line-reqs", "mine"], queryFn: () =>
    api<{ items: LineReq[] }>("/approvals/lines/requests?mine=true") });
  const inbox = useQuery({ enabled: isAdmin, queryKey: ["line-reqs", "inbox"], queryFn: () =>
    api<{ items: LineReq[] }>("/approvals/lines/requests?status=REQUESTED") });
  const people = directory.data?.items ?? [];
  const refresh = () => { qc.invalidateQueries({ queryKey: ["appr-lines"] });
    qc.invalidateQueries({ queryKey: ["line-reqs"] }); };

  const removePersonal = useMutation({
    mutationFn: (id: number) => api(`/approvals/lines/personal/${id}`, { method: "DELETE" }),
    onSuccess: () => { toast.ok("개인 결재선 삭제"); refresh(); }, onError: fail,
  });
  const cancelReq = useMutation({
    mutationFn: (id: number) => api(`/approvals/lines/requests/${id}/cancel`, { method: "POST" }),
    onSuccess: () => { toast.ok("요청 회수"); refresh(); }, onError: fail,
  });

  const onDelete = async (t: Tpl) => {
    if (await confirm({ title: "개인 결재선 삭제", body: `"${t.name}"을(를) 삭제할까요?`,
      danger: true, confirmLabel: "삭제" })) removePersonal.mutate(t.id);
  };
  const onCancelReq = async (r: LineReq) => {
    if (await confirm({ title: "요청 회수", body: `${r.req_no}을(를) 회수할까요?`,
      confirmLabel: "회수" })) cancelReq.mutate(r.id);
  };

  const orgTpls = (templates.data?.items ?? []).filter((t) => t.scope === "ORG");

  return (
    <div className="space-y-4">
      <div className="flex items-center justify-between">
        <h1 className="text-lg font-bold">자금결재선 관리</h1>
        <span className="flex gap-2">
          <Button variant="secondary" onClick={() => setRequesting({ type: "NEW" })}>공용 결재선 요청</Button>
          <Button onClick={() => setEditing("new")}>개인 결재선 만들기</Button>
        </span>
      </div>
      <p className="text-xs text-slate-500">
        상신 시 선택하는 결재선 템플릿을 관리합니다. <b>개인</b> 결재선은 본인 전용으로 자유롭게
        만들고, <b>공용(ORG)</b> 결재선은 신설·변경·폐기를 요청하면 관리자 검토·승인 후 반영됩니다
        (요청자 본인은 검토 불가 — 직무분리).</p>

      <QueryErrorBanner queries={isAdmin ? [templates, myReqs, inbox] : [templates, myReqs]} />
      <Card title="결재선 템플릿 (공용 + 내 개인)">
        <Table headers={["이름", "구분", "적용 문서", "결재 단계", ""]}
          rows={(templates.data?.items ?? []).map((t) => [
            t.name,
            <Badge key="s" tone={t.scope === "ORG" ? "blue" : "gray"}>
              {t.scope === "ORG" ? "공용" : "개인"}</Badge>,
            t.doc_types.map((d) => DOC_LABEL[d] ?? d).join("·"),
            t.steps.map((s, i) => `${i + 1}.${stepText(s, people)}`).join(" → "),
            <span key="a" className="flex gap-1">
              {t.scope === "PERSONAL" && t.owner_user_id === myId && <>
                <Button variant="secondary" size="sm" onClick={() => setEditing(t)}>수정</Button>
                <Button variant="danger" size="sm" disabled={removePersonal.isPending}
                  onClick={() => onDelete(t)}>삭제</Button></>}
              {t.scope === "ORG" && <>
                <Button variant="secondary" size="sm"
                  onClick={() => setRequesting({ type: "MODIFY", target: t })}>변경요청</Button>
                <Button variant="secondary" size="sm"
                  onClick={() => setRequesting({ type: "RETIRE", target: t })}>폐기요청</Button></>}
            </span>,
          ])} empty={templates.isFetching ? "불러오는 중…" : "결재선이 없습니다"} />
      </Card>

      <Card title="내 결재선 요청">
        <Table headers={["요청번호", "유형", "제안/대상", "사유", "상태", "검토의견", ""]}
          rows={(myReqs.data?.items ?? []).map((r) => [
            r.req_no, REQ_TYPE_LABEL[r.request_type] ?? r.request_type,
            r.proposed_name ?? (r.target_template_id ? `템플릿#${r.target_template_id}` : "—"),
            r.reason,
            <Badge key="s" tone={REQ_TONE[r.status] ?? "gray"}>{r.status}</Badge>,
            r.review_note ?? "—",
            r.status === "REQUESTED"
              ? <Button key="c" variant="secondary" size="sm" disabled={cancelReq.isPending}
                  onClick={() => onCancelReq(r)}>회수</Button>
              : null,
          ])} empty={myReqs.isFetching ? "불러오는 중…" : "요청 내역 없음"} />
      </Card>

      {isAdmin && (
        <Card title="요청 검토함 (관리자)">
          <Table headers={["요청번호", "유형", "제안/대상", "제안 단계", "요청자", "사유", ""]}
            rows={(inbox.data?.items ?? []).map((r) => [
              r.req_no, REQ_TYPE_LABEL[r.request_type] ?? r.request_type,
              r.proposed_name ?? (r.target_template_id ? `템플릿#${r.target_template_id}` : "—"),
              (r.proposed_steps ?? []).map((s, i) => `${i + 1}.${stepText(s, people)}`).join(" → ") || "—",
              stepText({ type: "USER", value: String(r.requested_by) }, people),
              r.reason,
              <Button key="v" size="sm" onClick={() => setReviewing(r)}>검토</Button>,
            ])} empty={inbox.isFetching ? "불러오는 중…" : "대기 중 요청 없음"} />
        </Card>
      )}

      {editing != null && (
        <Modal title={editing === "new" ? "개인 결재선 만들기" : "개인 결재선 수정"} size="lg"
          onClose={() => setEditing(null)}>
          <PersonalForm tpl={editing === "new" ? null : editing} people={people}
            onDone={() => { setEditing(null); refresh(); }} onError={fail} />
        </Modal>
      )}
      {requesting != null && (
        <Modal title={`공용 결재선 ${REQ_TYPE_LABEL[requesting.type]} 요청`} size="lg"
          onClose={() => setRequesting(null)}>
          <RequestForm type={requesting.type} target={requesting.target ?? null} orgTpls={orgTpls}
            people={people} onDone={() => { setRequesting(null); refresh(); }} onError={fail} />
        </Modal>
      )}
      {reviewing != null && (
        <ReviewModal req={reviewing} people={people}
          onDone={() => { setReviewing(null); refresh(); }} onError={fail}
          onClose={() => setReviewing(null)} />
      )}
    </div>
  );
}

/* ── 결재 단계 편집기 — ROLE/부서장/USER·최대 8단계·순서 이동 ── */
function StepsEditor({ steps, onChange, people }: {
  steps: Step[]; onChange: (s: Step[]) => void; people: Person[];
}) {
  const set = (i: number, s: Step) => onChange(steps.map((x, j) => (j === i ? s : x)));
  const move = (i: number, d: number) => {
    const next = [...steps];
    const [row] = next.splice(i, 1);
    next.splice(i + d, 0, row);
    onChange(next);
  };
  return (
    <div className="space-y-1.5">
      <p className="text-sm text-slate-600">결재 단계 (1~8단계·순서대로 승인)</p>
      {steps.map((s, i) => (
        // 내용 기반 키 — 삭제/이동 시 Combobox 내부 검색상태가 옆 행으로 잔존하는 것 방지(v1.203)
        <div key={`${i}:${s.type}:${s.value ?? ""}`} className="flex items-center gap-2">
          <span className="w-5 text-right text-xs text-slate-400">{i + 1}</span>
          <select value={s.type}
            onChange={(e) => set(i, { type: e.target.value,
              value: e.target.value === "ROLE" ? "MANAGER" : null })}
            className="rounded-md border border-slate-300 px-2 py-1.5 text-sm">
            <option value="DEPT_HEAD">부서장(기안부서)</option>
            <option value="ROLE">역할</option>
            <option value="USER">특정 사용자</option>
          </select>
          {s.type === "ROLE" && (
            <select value={s.value ?? "MANAGER"} onChange={(e) => set(i, { ...s, value: e.target.value })}
              className="rounded-md border border-slate-300 px-2 py-1.5 text-sm">
              {Object.entries(ROLE_LABEL).map(([k, v]) => <option key={k} value={k}>{v}</option>)}
            </select>
          )}
          {s.type === "USER" && (
            <Combobox<Person>
              value={people.find((p) => String(p.id) === s.value) ?? null}
              onPick={(p) => set(i, { ...s, value: p ? String(p.id) : null })}
              options={people} getKey={(p) => p.id}
              getLabel={(p) => `${p.name}${p.department_id != null ? ` (부서#${p.department_id})` : ""}`}
              getSearch={(p) => `${p.name} ${p.id}`} placeholder="결재자 검색" width="w-56" />
          )}
          <span className="ml-auto flex gap-1">
            <Button variant="secondary" size="sm" disabled={i === 0}
              onClick={() => move(i, -1)}>↑</Button>
            <Button variant="secondary" size="sm" disabled={i === steps.length - 1}
              onClick={() => move(i, 1)}>↓</Button>
            <Button variant="danger" size="sm" disabled={steps.length <= 1}
              onClick={() => onChange(steps.filter((_, j) => j !== i))}>삭제</Button>
          </span>
        </div>
      ))}
      <Button variant="secondary" size="sm" disabled={steps.length >= 8}
        onClick={() => onChange([...steps, { type: "DEPT_HEAD", value: null }])}>+ 단계 추가</Button>
    </div>
  );
}

function DocTypesPicker({ value, onChange }: { value: string[]; onChange: (v: string[]) => void }) {
  const opts = Object.keys(DOC_LABEL).filter((k) => k !== "ANY");
  return (
    <div>
      <p className="text-sm text-slate-600">적용 문서 (미선택 = 전체)</p>
      <div className="flex flex-wrap gap-x-3 gap-y-1">
        {opts.map((k) => (
          <label key={k} className="flex items-center gap-1 text-sm">
            <input type="checkbox" checked={value.includes(k)}
              onChange={(e) => onChange(e.target.checked
                ? [...value, k] : value.filter((x) => x !== k))} />
            {DOC_LABEL[k]}
          </label>
        ))}
      </div>
    </div>
  );
}

const stepsValid = (steps: Step[]) =>
  steps.every((s) => s.type === "DEPT_HEAD" || (s.value != null && s.value !== ""));

function PersonalForm({ tpl, people, onDone, onError }: {
  tpl: Tpl | null; people: Person[]; onDone: () => void; onError: (e: unknown) => void;
}) {
  const toast = useToast();
  const [name, setName] = useState(tpl?.name ?? "");
  const [steps, setSteps] = useState<Step[]>(tpl?.steps ?? [{ type: "DEPT_HEAD", value: null }]);
  const [docTypes, setDocTypes] = useState<string[]>(
    (tpl?.doc_types ?? []).filter((d) => d !== "ANY"));
  const save = useMutation({
    mutationFn: () => api(tpl ? `/approvals/lines/personal/${tpl.id}` : "/approvals/lines/personal",
      { method: tpl ? "PATCH" : "POST", body: JSON.stringify({
        // v1.203 — 항상 배열 전송: 수정에서 null은 "변경 안 함"이라 전체(ANY) 되돌리기가
        // 무음 no-op 되던 결함(빈 배열은 서버 _norm_doc_types가 ANY로 정규화)
        name, steps, doc_types: docTypes }) }),
    onSuccess: () => { toast.ok(tpl ? "개인 결재선 수정" : "개인 결재선 생성"); onDone(); },
    onError,
  });
  return (
    <div className="space-y-3">
      <Input label="이름" value={name} onChange={(e) => setName(e.target.value)}
        placeholder="예: 내 품의선(팀장→원장)" />
      <DocTypesPicker value={docTypes} onChange={setDocTypes} />
      <StepsEditor steps={steps} onChange={setSteps} people={people} />
      <Button disabled={save.isPending || !name.trim() || !stepsValid(steps)}
        onClick={() => save.mutate()}>{tpl ? "수정 저장" : "생성"}</Button>
    </div>
  );
}

function RequestForm({ type, target, orgTpls, people, onDone, onError }: {
  type: string; target: Tpl | null; orgTpls: Tpl[]; people: Person[];
  onDone: () => void; onError: (e: unknown) => void;
}) {
  const toast = useToast();
  const [targetId, setTargetId] = useState<number | null>(target?.id ?? null);
  const [name, setName] = useState("");
  const [reason, setReason] = useState("");
  const [steps, setSteps] = useState<Step[]>(
    target?.steps ?? [{ type: "DEPT_HEAD", value: null }]);
  const [docTypes, setDocTypes] = useState<string[]>(
    (target?.doc_types ?? []).filter((d) => d !== "ANY"));
  const needsSteps = type !== "RETIRE";
  const submit = useMutation({
    mutationFn: () => api<{ req_no: string }>("/approvals/lines/requests", { method: "POST",
      body: JSON.stringify({
        request_type: type, reason, proposed_name: name || null,
        proposed_steps: needsSteps ? steps : null,
        doc_types: docTypes.length ? docTypes : null,
        target_template_id: targetId }) }),
    onSuccess: (r) => { toast.ok(`요청 접수 — ${r.req_no}`); onDone(); },
    onError,
  });
  const ok = reason.trim() && (type !== "NEW" || name.trim())
    && (type === "NEW" || targetId != null) && (!needsSteps || stepsValid(steps));
  return (
    <div className="space-y-3">
      {type !== "NEW" && (
        <label className="block text-sm text-slate-600">대상 공용 결재선
          <select value={targetId ?? ""} onChange={(e) => {
            const id = e.target.value ? Number(e.target.value) : null;
            setTargetId(id);
            const t = orgTpls.find((x) => x.id === id);
            if (t) { setSteps(t.steps); setDocTypes(t.doc_types.filter((d) => d !== "ANY")); }
          }} className="w-full rounded-md border border-slate-300 px-2 py-1.5 text-sm">
            <option value="">선택하세요</option>
            {orgTpls.map((t) => <option key={t.id} value={t.id}>{t.name}</option>)}
          </select>
        </label>
      )}
      {type === "NEW" && (
        <Input label="신설 결재선 이름" value={name} onChange={(e) => setName(e.target.value)}
          placeholder="예: 지출품의 표준 결재선" />
      )}
      {needsSteps && <>
        <DocTypesPicker value={docTypes} onChange={setDocTypes} />
        <StepsEditor steps={steps} onChange={setSteps} people={people} />
      </>}
      <label className="block text-sm text-slate-600">요청 사유
        <textarea value={reason} onChange={(e) => setReason(e.target.value)} rows={3}
          className="w-full rounded-md border border-slate-300 px-2 py-1.5 text-sm"
          placeholder="필수 — 관리자 검토 근거가 됩니다" />
      </label>
      <Button disabled={submit.isPending || !ok} onClick={() => submit.mutate()}>요청 제출</Button>
    </div>
  );
}

function ReviewModal({ req, people, onDone, onError, onClose }: {
  req: LineReq; people: Person[]; onDone: () => void; onError: (e: unknown) => void;
  onClose: () => void;
}) {
  const toast = useToast();
  const [note, setNote] = useState("");
  const review = useMutation({
    mutationFn: (approve: boolean) =>
      api(`/approvals/lines/requests/${req.id}/review`, { method: "POST",
        body: JSON.stringify({ approve, note: note || null }) }),
    onSuccess: (_r, approve) => { toast.ok(approve ? "승인 — 템플릿 반영" : "반려"); onDone(); },
    onError,
  });
  return (
    <Modal title={`요청 검토 — ${req.req_no}`} onClose={onClose}>
      <div className="space-y-3">
        <div className="space-y-1 rounded-md bg-slate-50 p-3 text-sm text-slate-700">
          <p><b>{REQ_TYPE_LABEL[req.request_type] ?? req.request_type}</b>
            {" · "}{req.proposed_name ?? (req.target_template_id ? `템플릿#${req.target_template_id}` : "")}</p>
          {(req.proposed_steps ?? []).length > 0 && (
            <p>{req.proposed_steps.map((s, i) => `${i + 1}.${stepText(s, people)}`).join(" → ")}</p>)}
          <p>적용: {(req.doc_types ?? []).map((d) => DOC_LABEL[d] ?? d).join("·")}</p>
          <p className="text-slate-500">사유: {req.reason}</p>
        </div>
        <Input label="검토 의견(선택)" value={note} onChange={(e) => setNote(e.target.value)} />
        <div className="flex gap-2">
          <Button variant="success" disabled={review.isPending}
            onClick={() => review.mutate(true)}>승인</Button>
          <Button variant="danger" disabled={review.isPending}
            onClick={() => review.mutate(false)}>반려</Button>
        </div>
      </div>
    </Modal>
  );
}

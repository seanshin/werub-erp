"use client";
/** 자금결재 — 기안문서: 기안(DRAFT)→상신(결재선 템플릿 선택)→N단계 승인/반려. 회수·공람.
 *  ERP 내부 결재(자금 관련 품의) — 그룹웨어 전자결재와 별개 명칭(자금결재).
 *  회계 없음(품의≠집행). 상신 시 본문 봉인(결재 원장 해시체인). */
import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { ApiError, api } from "@/lib/api";
import { Badge, Button, Card, Input, Modal, Table } from "@/components/ui";
import { QueryErrorBanner } from "@/components/query-guard";
import { won } from "@/lib/format";
import { useConfirm } from "@/components/confirm";
import { useToast } from "@/components/toast";

const STATUS_TONE: Record<string, "gray" | "amber" | "green" | "red" | "blue"> = {
  DRAFT: "gray", PENDING: "amber", APPROVED: "green", REJECTED: "red", CANCELED: "gray",
};
const STEP_TONE: Record<string, "gray" | "amber" | "green" | "red"> = {
  PENDING: "amber", APPROVED: "green", REJECTED: "red", SKIPPED: "gray",
};
// v1.203 — 서버 _CATEGORIES와 정합(기존 5종 중 4종이 서버 ValidationError로 거부되던 결함)
const CATEGORIES: [string, string][] = [
  ["GENERAL", "일반"], ["BUDGET", "예산"], ["POLICY", "규정·정책"], ["COOPERATION", "협조"],
];

interface Draft {
  id: number; doc_no: string | null; title: string; category: string;
  status: string; created_by: number; decided_at: string | null;
}
interface Step {
  step_no: number; approver_type: string; approver_value: string | null;
  status: string; acted_by: number | null; acted_at: string | null; note: string | null;
}
interface Detail extends Draft {
  body: string | null; ref_amount: number | null;
  approval: { status: string; current_step: number; steps: Step[] } | null;
  viewers: { user_id: number; read_at: string | null }[];
}
interface Tpl { id: number; name: string; scope: string; steps: { approver_type: string; approver_value: string | null }[] }

export default function DraftsPage() {
  const qc = useQueryClient();
  const toast = useToast();
  const confirm = useConfirm();
  const fail = (e: unknown) => toast.err(e instanceof ApiError ? e.error.message : "처리 실패");
  const [status, setStatus] = useState("");
  const [creating, setCreating] = useState(false);
  const [detailId, setDetailId] = useState<number | null>(null);
  const [submitId, setSubmitId] = useState<number | null>(null);

  const drafts = useQuery({ queryKey: ["drafts", status], queryFn: () =>
    api<{ items: Draft[] }>(`/approvals/drafts${status ? `?status=${status}` : ""}`) });
  const detail = useQuery({ enabled: detailId != null, queryKey: ["draft", detailId], queryFn: () =>
    api<Detail>(`/approvals/drafts/${detailId}`) });
  const templates = useQuery({ enabled: submitId != null, queryKey: ["appr-lines"], queryFn: () =>
    api<{ items: Tpl[] }>("/approvals/lines") });
  const refresh = () => { qc.invalidateQueries({ queryKey: ["drafts"] });
    qc.invalidateQueries({ queryKey: ["draft"] }); };

  const create = useMutation({
    mutationFn: (b: unknown) => api<{ id: number }>("/approvals/drafts", { method: "POST", body: JSON.stringify(b) }),
    onSuccess: () => { toast.ok("기안문서 생성(DRAFT)"); setCreating(false); refresh(); },
    onError: fail,
  });
  const submit = useMutation({
    mutationFn: (v: { id: number; template_id: number }) =>
      api<{ doc_no: string }>(`/approvals/drafts/${v.id}/submit`, { method: "POST",
        body: JSON.stringify({ template_id: v.template_id }) }),
    onSuccess: (r) => { toast.ok(`상신 완료 — 문서번호 ${r.doc_no}`); setSubmitId(null); refresh(); },
    onError: fail,
  });
  const act = useMutation({
    mutationFn: (v: { id: number; action: string; note?: string }) =>
      api(`/approvals/drafts/${v.id}/${v.action}`, { method: "POST",
        body: JSON.stringify({ note: v.note ?? "" }) }),
    onSuccess: (_r, v) => { toast.ok(`${ACT_LABEL[v.action]} 완료`); refresh(); },
    onError: fail,
  });
  const ACT_LABEL: Record<string, string> = { approve: "승인", reject: "반려", cancel: "회수", read: "열람확인" };

  const onAct = async (id: number, action: string) => {
    const msg: Record<string, string> = { reject: "이 기안을 반려할까요?", cancel: "상신을 회수할까요? (미결재 시만 가능)" };
    if (action === "approve" || !msg[action]) { act.mutate({ id, action }); return; }
    if (await confirm({ title: ACT_LABEL[action], body: msg[action], danger: action === "reject",
      confirmLabel: ACT_LABEL[action] })) act.mutate({ id, action });
  };

  const d = detail.data;

  return (
    <div className="space-y-4">
      <div className="flex items-center justify-between">
        <h1 className="text-lg font-bold">자금결재 — 기안문서</h1>
        <Button onClick={() => setCreating(true)}>기안 작성</Button>
      </div>
      <p className="text-xs text-slate-500">
        지출·계약 등 자금 관련 품의의 <b>ERP 내부 결재</b>입니다(그룹웨어 전자결재와 별개). 기안 후
        결재선(공용/개인 템플릿)을 골라 상신하면 N단계 승인이 진행됩니다. 회계 전기는 없습니다
        (품의≠집행). 상신 시 본문이 봉인(결재 원장 해시체인)됩니다.</p>

      <QueryErrorBanner queries={[drafts]} />
      <Card title="기안문서" action={
        <select value={status} onChange={(e) => setStatus(e.target.value)}
          className="rounded-md border border-slate-300 px-2 py-1 text-xs">
          <option value="">전체</option>
          {["DRAFT", "PENDING", "APPROVED", "REJECTED", "CANCELED"].map((s) => <option key={s} value={s}>{s}</option>)}
        </select>}>
        <Table headers={["문서번호", "제목", "분류", "상태", "결정일", ""]}
          rows={(drafts.data?.items ?? []).map((dr) => [
            dr.doc_no ?? "—", dr.title, dr.category,
            <Badge key="s" tone={STATUS_TONE[dr.status] ?? "gray"}>{dr.status}</Badge>,
            dr.decided_at?.slice(0, 10) ?? "—",
            <span key="e" className="flex gap-1">
              <Button variant="secondary" size="sm" onClick={() => setDetailId(dr.id)}>상세</Button>
              {dr.status === "DRAFT" && <Button size="sm" onClick={() => setSubmitId(dr.id)}>상신</Button>}
            </span>,
          ])} empty={drafts.isFetching ? "불러오는 중…" : "기안문서 없음"} />
      </Card>

      {creating && (
        <Modal title="기안문서 작성" size="lg" dirty onClose={() => setCreating(false)}>
          <DraftForm isLoading={create.isPending} onSubmit={(b) => create.mutate(b)}
            onCancel={() => setCreating(false)} />
        </Modal>
      )}
      {submitId != null && (
        <Modal title="상신 — 결재선 선택" onClose={() => setSubmitId(null)}>
          <div className="space-y-2">
            {templates.isFetching && <p className="text-sm text-slate-500">결재선 불러오는 중…</p>}
            {(templates.data?.items ?? []).map((t) => (
              <button key={t.id} onClick={() => submit.mutate({ id: submitId, template_id: t.id })}
                disabled={submit.isPending}
                className="flex w-full items-center justify-between rounded-md border border-slate-200 px-3 py-2 text-left text-sm hover:bg-slate-50 disabled:opacity-50">
                <span><b>{t.name}</b> <Badge tone={t.scope === "ORG" ? "blue" : "gray"}>{t.scope}</Badge></span>
                <span className="text-xs text-slate-500">{t.steps.length}단계</span>
              </button>))}
            {templates.data && templates.data.items.length === 0 && (
              <p className="text-sm text-amber-700">선택 가능한 결재선이 없습니다. 자금결재선 관리에서 요청/생성하세요.</p>)}
          </div>
        </Modal>
      )}
      {detailId != null && d && (
        <Modal title={`${d.doc_no ?? "기안"} — ${d.title}`} size="lg" onClose={() => setDetailId(null)}>
          <div className="space-y-3">
            <div className="flex flex-wrap items-center gap-2 text-sm">
              <Badge tone={STATUS_TONE[d.status] ?? "gray"}>{d.status}</Badge>
              <span className="text-slate-500">{d.category}</span>
              {d.ref_amount != null && <span>관련금액 <b>{won(d.ref_amount)}</b></span>}
            </div>
            <div className="whitespace-pre-wrap rounded-md bg-slate-50 p-3 text-sm text-slate-700">
              {d.body || "(본문 없음)"}</div>
            {d.approval && (
              <div>
                <p className="mb-1 text-sm font-medium text-slate-600">결재선 (현재 {d.approval.current_step}단계)</p>
                <Table headers={["단계", "결재자", "상태", "처리일", "의견"]}
                  rows={d.approval.steps.map((s) => [
                    `${s.step_no}`,
                    // 지정이 ROLE인 단계는 '실제 처리자'가 없으면 누가 승인했는지 안 남는다
                    <span key="a" className="text-xs">
                      {s.approver_type}{s.approver_value ? `:${s.approver_value}` : ""}
                      {s.acted_by && <span className="text-slate-500"> → 처리 #{s.acted_by}</span>}
                    </span>,
                    <Badge key="ss" tone={STEP_TONE[s.status] ?? "gray"}>{s.status}</Badge>,
                    s.acted_at?.slice(0, 10) ?? "—", s.note ?? "—",
                  ])} empty="단계 없음" />
              </div>
            )}
            {/* 문서별 불변 원장 — 전역 체인 무결성만 보이고 "이 문서의 결재 이력이 위조되지
                않았다"는 개별 증거는 화면에서 도달할 수 없었다(EP 완비·호출처 0건). */}
            <LedgerPanel docId={d.id} />
            {/* 공람 지정·열람 여부가 화면에 없어 "누가 아직 안 읽었는지"를 알 수 없었고,
                본인이 공람 대상인지도 모른 채 열람확인 버튼을 눌렀다. */}
            <div className="border-t border-slate-100 pt-2">
              <p className="mb-1 text-xs font-semibold text-slate-600">
                공람 {d.viewers.length}명 · 열람 {d.viewers.filter((v) => v.read_at).length}명</p>
              {d.viewers.length > 0 && (
                <ul className="flex flex-wrap gap-1.5">
                  {d.viewers.map((v) => (
                    <li key={v.user_id}>
                      <Badge tone={v.read_at ? "green" : "gray"}>
                        #{v.user_id}{v.read_at ? ` 열람 ${v.read_at.slice(5, 10)}` : " 미열람"}</Badge>
                    </li>
                  ))}
                </ul>
              )}
            </div>
            <div className="flex flex-wrap gap-1.5 border-t border-slate-100 pt-2">
              {d.status === "DRAFT" && <Button size="sm" onClick={() => { setSubmitId(d.id); setDetailId(null); }}>상신</Button>}
              {d.status === "PENDING" && <>
                <Button variant="success" size="sm" disabled={act.isPending} onClick={() => onAct(d.id, "approve")}>승인</Button>
                <Button variant="danger" size="sm" disabled={act.isPending} onClick={() => onAct(d.id, "reject")}>반려</Button>
                <Button variant="secondary" size="sm" disabled={act.isPending} onClick={() => onAct(d.id, "cancel")}>회수</Button></>}
              <Button variant="secondary" size="sm" disabled={act.isPending} onClick={() => onAct(d.id, "read")}>열람확인</Button>
            </div>
          </div>
        </Modal>
      )}
    </div>
  );
}

function DraftForm({ onSubmit, isLoading, onCancel }: {
  onSubmit: (b: unknown) => void; isLoading: boolean; onCancel: () => void;
}) {
  const [title, setTitle] = useState("");
  const [body, setBody] = useState("");
  const [category, setCategory] = useState("GENERAL");
  const [refAmount, setRefAmount] = useState("");
  return (
    <div className="space-y-2">
      <Input label="제목" value={title} onChange={(e) => setTitle(e.target.value)}
        placeholder="예: 부서 워크숍 개최 품의" />
      <div className="grid grid-cols-2 gap-2">
        <label className="block text-sm text-slate-600">분류
          <select value={category} onChange={(e) => setCategory(e.target.value)}
            className="w-full rounded-md border border-slate-300 px-2 py-1.5 text-sm">
            {CATEGORIES.map(([c, ko]) => <option key={c} value={c}>{ko}</option>)}
          </select>
        </label>
        <Input label="관련 금액(선택)" value={refAmount} inputMode="numeric"
          onChange={(e) => setRefAmount(e.target.value.replace(/[^0-9]/g, ""))} />
      </div>
      <label className="block text-sm text-slate-600">본문
        <textarea value={body} onChange={(e) => setBody(e.target.value)} rows={6}
          className="w-full rounded-md border border-slate-300 px-2 py-1.5 text-sm"
          placeholder="기안 내용을 입력하세요. 상신 시 봉인됩니다." />
      </label>
      <div className="flex gap-2">
        <Button disabled={isLoading || !title} onClick={() => onSubmit({
          title, body: body || null, category, ref_amount: refAmount ? Number(refAmount) : null })}>
          기안 생성</Button>
        <Button variant="secondary" onClick={onCancel}>닫기</Button>
      </div>
    </div>
  );
}

/** 기안문서 결재 원장(불변 해시체인) — 조회는 접속기록에 남는다(서버가 기록). */
function LedgerPanel({ docId }: { docId: number }) {
  const [open, setOpen] = useState(false);
  const q = useQuery({
    queryKey: ["draft-ledger", docId], enabled: open, retry: 0,
    queryFn: () => api<{ items: { seq_no: number; action: string; step_no: number | null;
      actor: number; note: string | null; acted_at: string | null;
      row_hash: string; prev_hash: string }[] }>(`/approvals/GENERAL_DRAFT/${docId}/ledger`),
  });
  return (
    <div className="border-t border-slate-100 pt-2">
      <button type="button" onClick={() => setOpen(!open)}
        className="text-xs font-medium text-blue-600 hover:underline">
        {open ? "원장 이력 닫기" : "원장 이력 (불변 해시체인)"}
      </button>
      {open && (q.isError
        ? <p className="mt-1 text-xs text-slate-400">원장 이력을 불러오지 못했습니다</p>
        : <Table headers={["seq", "동작", "단계", "처리자", "시각", "해시"]}
            rows={(q.data?.items ?? []).map((r) => [
              r.seq_no, r.action, r.step_no ?? "—", `#${r.actor}`,
              r.acted_at?.slice(0, 16).replace("T", " ") ?? "—",
              <code key="h" className="text-[10px] text-slate-400"
                title={`row=${r.row_hash}\nprev=${r.prev_hash}`}>
                {r.row_hash.slice(0, 12)}…</code>,
            ])} empty={q.isFetching ? "불러오는 중…" : "원장 항목 없음"} />)}
    </div>
  );
}

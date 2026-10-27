"use client";
/** 원무 — 환자 민원·CS(VOC·0223): 접수→분류→배정→회신→종결·재오픈. SLA 14일(due D-day).
 *  read 행스코프(SELF 본인접수/DEPT 귀속부서)·배정/회신/종결은 관리자·부서장(서버 게이트). */
import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { ApiError, api, auth } from "@/lib/api";
import { Badge, Button, Card, Input, Modal, Table } from "@/components/ui";
import { QueryErrorBanner } from "@/components/query-guard";
import { Combobox } from "@/components/combobox";
import { monthLocal } from "@/lib/dates";
import { useConfirm } from "@/components/confirm";
import { useToast } from "@/components/toast";

const STATUS_TONE: Record<string, "gray" | "amber" | "green" | "red" | "blue"> = {
  RECEIVED: "amber", IN_PROGRESS: "blue", ANSWERED: "green", CLOSED: "gray",
};
const STATUS_LABEL: Record<string, string> = {
  RECEIVED: "접수", IN_PROGRESS: "처리중", ANSWERED: "회신완료", CLOSED: "종결",
};
const VTYPE: Record<string, string> = {
  COMPLAINT: "불만", GRIEVANCE: "고충", SUGGESTION: "제안", PRAISE: "칭찬", INQUIRY: "상담",
};
const CATEGORY: Record<string, string> = {
  CARE: "진료", ADMIN: "원무행정", FACILITY: "시설환경", STAFF: "직원응대",
  COST: "비용", PRIVACY: "개인정보", ETC: "기타",
};
const CHANNEL: Record<string, string> = {
  DESK: "창구", PHONE: "전화", LETTER: "서면", WEB: "홈페이지", ETC: "기타",
};
const SEVERITY_TONE: Record<string, "gray" | "amber" | "red"> = {
  LOW: "gray", MEDIUM: "amber", HIGH: "red",
};
const MANAGE_ROLES = new Set(["DIRECTOR", "ADMIN", "MANAGER"]);

interface Voc {
  id: number; case_no: string; channel: string; vtype: string; category: string;
  severity: string; patient_id: number | null; title: string; department_id: number | null;
  assignee_user_id: number | null; status: string; received_on: string; due_on: string;
  answered_at: string | null; closed_at: string | null; reopen_count: number; overdue: boolean;
  created_by: number;
}
interface VocDetail extends Voc {
  body: string | null; answer_body: string | null; contact?: string;
  actions: { action: string; actor: number; note: string | null; at: string }[];
}
interface Person { id: number; name: string; department_id: number | null }
interface Stats {
  total: number; open: number; overdue: number; avg_close_days: number | null;
  by_vtype: Record<string, number>; by_category: Record<string, number>;
}

export default function VocPage() {
  const qc = useQueryClient();
  const toast = useToast();
  const fail = (e: unknown) => toast.err(e instanceof ApiError ? e.error.message : "처리 실패");
  const canManage = MANAGE_ROLES.has(auth.claims()?.role ?? "");

  const [status, setStatus] = useState("");
  const [overdueOnly, setOverdueOnly] = useState(false);
  const [creating, setCreating] = useState(false);
  const [detailId, setDetailId] = useState<number | null>(null);

  const list = useQuery({ queryKey: ["voc", status, overdueOnly], queryFn: () =>
    api<{ items: Voc[] }>(`/patients/voc?${new URLSearchParams({
      ...(status ? { status } : {}), ...(overdueOnly ? { overdue: "true" } : {}) })}`) });
  const month = monthLocal();
  const stats = useQuery({ enabled: canManage, queryKey: ["voc-stats", month], queryFn: () =>
    api<Stats>(`/patients/voc/stats?since=${month}-01&until=${month}-31`) });
  const refresh = () => { qc.invalidateQueries({ queryKey: ["voc"] });
    qc.invalidateQueries({ queryKey: ["voc-stats"] }); qc.invalidateQueries({ queryKey: ["voc-detail"] }); };

  return (
    <div className="space-y-4">
      <div className="flex items-center justify-between">
        <h1 className="text-lg font-bold">원무 — 환자 민원·CS(VOC)</h1>
        <Button onClick={() => setCreating(true)}>민원 접수</Button>
      </div>
      <p className="text-xs text-slate-500">
        접수 후 14일(내부 SLA) 내 처리·회신을 원칙으로 합니다. 배정·회신·종결은 관리자/부서장(귀속부서)
        권한이며, 모든 처리 이력이 기록됩니다(의료기관 인증평가 — 민원·고충처리 체계 자료 소스).</p>

      <QueryErrorBanner queries={canManage ? [list, stats] : [list]} />

      {canManage && stats.data && (
        <div className="grid grid-cols-2 gap-3 sm:grid-cols-4">
          <Card title="이번달 접수"><p className="text-xl font-bold">{stats.data.total}건</p></Card>
          <Card title="처리중"><p className="text-xl font-bold">{stats.data.open}건</p></Card>
          <Card title="기한초과"><p className={`text-xl font-bold ${stats.data.overdue ? "text-red-600" : ""}`}>
            {stats.data.overdue}건</p></Card>
          <Card title="평균 처리일수"><p className="text-xl font-bold">
            {stats.data.avg_close_days != null ? `${stats.data.avg_close_days}일` : "—"}</p></Card>
        </div>
      )}

      <Card title="민원 목록" action={
        <span className="flex items-center gap-2">
          <label className="flex items-center gap-1 text-xs text-slate-500">
            <input type="checkbox" checked={overdueOnly}
              onChange={(e) => setOverdueOnly(e.target.checked)} /> 기한초과만
          </label>
          <select value={status} onChange={(e) => setStatus(e.target.value)}
            className="rounded-md border border-slate-300 px-2 py-1 text-xs">
            <option value="">전체</option>
            {Object.keys(STATUS_LABEL).map((s) =>
              <option key={s} value={s}>{STATUS_LABEL[s]}</option>)}
          </select>
        </span>}>
        <Table headers={["민원번호", "제목", "유형", "분류", "심각도", "상태", "접수일", "기한", ""]}
          rows={(list.data?.items ?? []).map((v) => [
            v.case_no, v.title, VTYPE[v.vtype] ?? v.vtype, CATEGORY[v.category] ?? v.category,
            <Badge key="sv" tone={SEVERITY_TONE[v.severity] ?? "gray"}>{v.severity}</Badge>,
            <Badge key="st" tone={STATUS_TONE[v.status] ?? "gray"}>
              {STATUS_LABEL[v.status] ?? v.status}{v.reopen_count > 0 ? ` (재${v.reopen_count})` : ""}</Badge>,
            v.received_on,
            v.overdue ? <Badge key="d" tone="red">기한초과</Badge> : v.due_on,
            <Button key="a" variant="secondary" size="sm" onClick={() => setDetailId(v.id)}>상세</Button>,
          ])} empty={list.isFetching ? "불러오는 중…" : "민원 없음"} />
      </Card>

      {creating && (
        <Modal title="민원 접수" size="lg" dirty onClose={() => setCreating(false)}>
          <ReceiveForm onDone={() => { setCreating(false); refresh(); }} onError={fail}
            onCancel={() => setCreating(false)} />
        </Modal>
      )}
      {detailId != null && (
        <DetailModal id={detailId} canManage={canManage} onClose={() => setDetailId(null)}
          onChanged={refresh} onError={fail} />
      )}
    </div>
  );
}

function Sel({ label, value, onChange, options }: {
  label: string; value: string; onChange: (v: string) => void; options: Record<string, string>;
}) {
  return (
    <label className="block text-sm text-slate-600">{label}
      <select value={value} onChange={(e) => onChange(e.target.value)}
        className="w-full rounded-md border border-slate-300 px-2 py-1.5 text-sm">
        {Object.entries(options).map(([k, v]) => <option key={k} value={k}>{v}</option>)}
      </select>
    </label>
  );
}

function ReceiveForm({ onDone, onError, onCancel }: {
  onDone: () => void; onError: (e: unknown) => void; onCancel: () => void;
}) {
  const toast = useToast();
  const [title, setTitle] = useState("");
  const [body, setBody] = useState("");
  const [channel, setChannel] = useState("DESK");
  const [vtype, setVtype] = useState("COMPLAINT");
  const [category, setCategory] = useState("ETC");
  const [severity, setSeverity] = useState("LOW");
  const [chartNo, setChartNo] = useState("");
  const [patientId, setPatientId] = useState<number | null>(null);
  const [patientName, setPatientName] = useState("");
  const [contact, setContact] = useState("");
  const [searchErr, setSearchErr] = useState(false);

  const lookup = async () => {
    setSearchErr(false);
    try {
      const r = await api<{ items: { id: number; name: string; chart_no: string }[] }>(
        "/patients/search", { method: "POST", body: JSON.stringify({ chart_no: chartNo }) });
      const p = r.items[0];
      if (p) { setPatientId(p.id); setPatientName(`${p.name} (${p.chart_no})`); }
      else { setPatientId(null); setPatientName(""); toast.err("해당 차트번호 환자 없음"); }
    } catch { setSearchErr(true); setPatientId(null); setPatientName(""); }
  };
  const create = useMutation({
    mutationFn: () => api<{ case_no: string; due_on: string }>("/patients/voc", { method: "POST",
      body: JSON.stringify({ title, body: body || null, channel, vtype, category, severity,
        patient_id: patientId, contact: patientId == null && contact ? contact : null }) }),
    onSuccess: (r) => { toast.ok(`접수 완료 — ${r.case_no} (기한 ${r.due_on})`); onDone(); },
    onError,
  });

  return (
    <div className="space-y-2">
      <Input label="제목" value={title} onChange={(e) => setTitle(e.target.value)}
        placeholder="예: 외래 대기시간 불만" />
      <div className="grid grid-cols-2 gap-2 sm:grid-cols-4">
        <Sel label="채널" value={channel} onChange={setChannel} options={CHANNEL} />
        <Sel label="유형" value={vtype} onChange={setVtype} options={VTYPE} />
        <Sel label="분류" value={category} onChange={setCategory} options={CATEGORY} />
        <Sel label="심각도" value={severity} onChange={setSeverity}
          options={{ LOW: "낮음", MEDIUM: "중간", HIGH: "높음(경영진 보고)" }} />
      </div>
      <label className="block text-sm text-slate-600">본문
        <textarea value={body} onChange={(e) => setBody(e.target.value)} rows={4}
          className="w-full rounded-md border border-slate-300 px-2 py-1.5 text-sm"
          placeholder="민원 내용 요지" />
      </label>
      <div className="flex items-end gap-2">
        <Input label="환자 연결(선택 — 차트번호)" value={chartNo}
          onChange={(e) => setChartNo(e.target.value)} placeholder="차트번호 검색" />
        <Button variant="secondary" size="sm" disabled={!chartNo.trim()} onClick={lookup}>검색</Button>
        {patientName && <Badge tone="blue">{patientName}</Badge>}
      </div>
      {searchErr && <p className="text-xs text-amber-700">
        환자 검색 권한이 없습니다 — 아래 회신 연락처를 입력하세요.</p>}
      {patientId == null && (
        <Input label="회신 연락처(선택 — 환자 미연결 시·암호화 저장)" value={contact}
          onChange={(e) => setContact(e.target.value)} placeholder="010-…" />)}
      <div className="flex gap-2">
        <Button disabled={create.isPending || !title.trim()} onClick={() => create.mutate()}>접수</Button>
        <Button variant="secondary" onClick={onCancel}>닫기</Button>
      </div>
    </div>
  );
}

function DetailModal({ id, canManage, onClose, onChanged, onError }: {
  id: number; canManage: boolean; onClose: () => void; onChanged: () => void;
  onError: (e: unknown) => void;
}) {
  const toast = useToast();
  const confirm = useConfirm();
  const [assigning, setAssigning] = useState(false);
  const [answering, setAnswering] = useState(false);
  const [note, setNote] = useState("");
  const detail = useQuery({ queryKey: ["voc-detail", id], queryFn: () =>
    api<VocDetail>(`/patients/voc/${id}`) });
  const act = useMutation({
    mutationFn: (v: { action: string; body: unknown }) =>
      api(`/patients/voc/${id}/${v.action}`, { method: "POST", body: JSON.stringify(v.body) }),
    onSuccess: (_r, v) => { toast.ok({ assign: "배정", comment: "의견 기록", answer: "회신 기록",
      close: "종결", reopen: "재오픈" }[v.action] ?? "완료"); onChanged(); setNote("");
      setAssigning(false); setAnswering(false); },
    onError,
  });
  const d = detail.data;

  const onClose_ = async () => {
    if (await confirm({ title: "민원 종결", body: "회신 내용 확인 후 종결합니다.", confirmLabel: "종결" }))
      act.mutate({ action: "close", body: { note: note || null } });
  };
  const onReopen = async () => {
    if (!note.trim()) { toast.err("재오픈 사유를 하단 입력란에 적어주세요"); return; }
    if (await confirm({ title: "재오픈", body: "종결된 민원을 다시 엽니다.", danger: true,
      confirmLabel: "재오픈" })) act.mutate({ action: "reopen", body: { note } });
  };

  return (
    <Modal title={d ? `${d.case_no} — ${d.title}` : "민원 상세"} size="lg" onClose={onClose}>
      {!d ? <p className="text-sm text-slate-500">불러오는 중…</p> : (
        <div className="space-y-3">
          <div className="flex flex-wrap items-center gap-2 text-sm">
            <Badge tone={STATUS_TONE[d.status] ?? "gray"}>{STATUS_LABEL[d.status] ?? d.status}</Badge>
            <Badge tone={SEVERITY_TONE[d.severity] ?? "gray"}>{d.severity}</Badge>
            <span className="text-slate-500">{VTYPE[d.vtype]}·{CATEGORY[d.category]}·{CHANNEL[d.channel]}</span>
            <span>접수 {d.received_on} · 기한 {d.overdue
              ? <b className="text-red-600">{d.due_on} 초과</b> : d.due_on}</span>
            {d.patient_id != null && <span className="text-slate-500">환자#{d.patient_id}</span>}
            {d.contact && <span className="text-slate-500">회신처 {d.contact}</span>}
          </div>
          <div className="whitespace-pre-wrap rounded-md bg-slate-50 p-3 text-sm text-slate-700">
            {d.body || "(본문 없음)"}</div>
          {d.answer_body && (
            <div>
              <p className="mb-1 text-sm font-medium text-slate-600">회신 내용</p>
              <div className="whitespace-pre-wrap rounded-md bg-emerald-50 p-3 text-sm text-slate-700">
                {d.answer_body}</div>
            </div>)}
          <div>
            <p className="mb-1 text-sm font-medium text-slate-600">처리 이력</p>
            <Table headers={["일시", "처리", "처리자", "내용"]}
              rows={d.actions.map((a) => [
                a.at.slice(0, 16).replace("T", " "),
                { ASSIGN: "배정", COMMENT: "의견", ANSWER: "회신", CLOSE: "종결", REOPEN: "재오픈" }[a.action] ?? a.action,
                `user#${a.actor}`, a.note ?? "—",
              ])} empty="이력 없음" />
          </div>
          <Input label="의견/사유" value={note} onChange={(e) => setNote(e.target.value)}
            placeholder="의견 기록 또는 종결/재오픈 사유" />
          <div className="flex flex-wrap gap-1.5 border-t border-slate-100 pt-2">
            <Button variant="secondary" size="sm" disabled={act.isPending || !note.trim()}
              onClick={() => act.mutate({ action: "comment", body: { note } })}>의견 기록</Button>
            {canManage && d.status !== "CLOSED" && <>
              <Button size="sm" disabled={act.isPending} onClick={() => setAssigning(true)}>배정</Button>
              {(d.status === "RECEIVED" || d.status === "IN_PROGRESS") &&
                <Button variant="success" size="sm" disabled={act.isPending}
                  onClick={() => setAnswering(true)}>회신</Button>}
              {d.status === "ANSWERED" &&
                <Button size="sm" disabled={act.isPending} onClick={onClose_}>종결</Button>}</>}
            {canManage && d.status === "CLOSED" &&
              <Button variant="danger" size="sm" disabled={act.isPending} onClick={onReopen}>재오픈</Button>}
          </div>
          {assigning && (
            <AssignForm isLoading={act.isPending}
              onSubmit={(uid) => act.mutate({ action: "assign",
                body: { assignee_user_id: uid, note: note || null } })}
              onCancel={() => setAssigning(false)} />)}
          {answering && (
            <AnswerForm isLoading={act.isPending}
              onSubmit={(t) => act.mutate({ action: "answer", body: { answer_body: t } })}
              onCancel={() => setAnswering(false)} />)}
        </div>
      )}
    </Modal>
  );
}

function AssignForm({ onSubmit, onCancel, isLoading }: {
  onSubmit: (userId: number) => void; onCancel: () => void; isLoading: boolean;
}) {
  const [person, setPerson] = useState<Person | null>(null);
  const directory = useQuery({ queryKey: ["appr-directory"], queryFn: () =>
    api<{ items: Person[] }>("/approvals/lines/directory") });
  return (
    <div className="flex items-end gap-2 rounded-md border border-slate-200 p-2">
      <label className="block text-sm text-slate-600">담당자
        <Combobox<Person> value={person} onPick={setPerson}
          options={directory.data?.items ?? []} getKey={(p) => p.id}
          getLabel={(p) => `${p.name}${p.department_id != null ? ` (부서#${p.department_id})` : ""}`}
          getSearch={(p) => `${p.name} ${p.id}`} placeholder="담당자 검색" width="w-56" />
      </label>
      <Button size="sm" disabled={isLoading || person == null}
        onClick={() => onSubmit(person!.id)}>배정 확정</Button>
      <Button variant="secondary" size="sm" onClick={onCancel}>취소</Button>
    </div>
  );
}

function AnswerForm({ onSubmit, onCancel, isLoading }: {
  onSubmit: (text: string) => void; onCancel: () => void; isLoading: boolean;
}) {
  const [text, setText] = useState("");
  return (
    <div className="space-y-2 rounded-md border border-slate-200 p-2">
      <label className="block text-sm text-slate-600">회신 내용(민원인 안내 요지)
        <textarea value={text} onChange={(e) => setText(e.target.value)} rows={3}
          className="w-full rounded-md border border-slate-300 px-2 py-1.5 text-sm" />
      </label>
      <div className="flex gap-2">
        <Button size="sm" disabled={isLoading || !text.trim()} onClick={() => onSubmit(text)}>회신 기록</Button>
        <Button variant="secondary" size="sm" onClick={onCancel}>취소</Button>
      </div>
    </div>
  );
}

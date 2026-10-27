"use client";
/** 규제 — 의료기관 인증평가 조사항목 관리(상시준비·P3-2): 조사기준별 준비상태·근거·담당 등록/갱신
 *  ·영역별 준비율. 지표(환자안전·감염관리·방사선·VOC)는 각 화면 연계. write=품질팀/관리자. */
import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { ApiError, api } from "@/lib/api";
import { Badge, Button, Card, Input, Modal, Table } from "@/components/ui";
import { QueryErrorBanner } from "@/components/query-guard";
import { useToast } from "@/components/toast";

const DOMAIN: Record<string, string> = { BASIC: "기본가치체계", CARE: "환자진료체계",
  ADMIN: "행정관리체계", PERFORMANCE: "성과관리체계" };
const STATUS: Record<string, { kr: string; tone: "green" | "amber" | "red" | "gray" }> = {
  READY: { kr: "준비완료", tone: "green" }, PARTIAL: { kr: "보완필요", tone: "amber" },
  GAP: { kr: "미비", tone: "red" }, NA: { kr: "해당없음", tone: "gray" } };
const STATUS_OPTS = ["READY", "PARTIAL", "GAP", "NA"];

interface Item {
  id: number; standard: string; domain: string; chapter: string | null; item_code: string;
  title: string; requirement: string | null; evidence_note: string | null; status: string;
  owner_dept_id: number | null; owner_dept_name: string | null;
  last_reviewed_on: string | null; note: string | null;
}
interface DomSummary {
  domain: string; READY: number; PARTIAL: number; GAP: number; NA: number;
  total: number; ready_rate: number | null;
}
interface Summary { standard: string; domains: DomSummary[]; overall_ready_rate: number | null; total_items: number }

export default function AccreditationPage() {
  const qc = useQueryClient();
  const toast = useToast();
  const fail = (e: unknown) => toast.err(e instanceof ApiError ? e.error.message : "처리 실패");
  const [domainF, setDomainF] = useState("");
  const [statusF, setStatusF] = useState("");
  const [creating, setCreating] = useState(false);
  const [editing, setEditing] = useState<Item | null>(null);

  const summary = useQuery({ queryKey: ["acc-summary"], queryFn: () =>
    api<Summary>("/regulatory/accreditation/summary") });
  const list = useQuery({ queryKey: ["acc-list", domainF, statusF], queryFn: () =>
    api<{ items: Item[] }>(`/regulatory/accreditation/items?${domainF ? `domain=${domainF}&` : ""}${statusF ? `status=${statusF}` : ""}`) });
  const refresh = () => { qc.invalidateQueries({ queryKey: ["acc-summary"] });
    qc.invalidateQueries({ queryKey: ["acc-list"] }); };

  return (
    <div className="space-y-4">
      <div className="flex items-center justify-between">
        <h1 className="text-lg font-bold">규제 — 의료기관 인증평가 준비</h1>
        <Button onClick={() => setCreating(true)}>조사항목 추가</Button>
      </div>
      <p className="text-xs text-slate-500">
        인증 조사기준별 준비상태·근거·담당을 상시 관리합니다. 지표(환자안전·감염관리·방사선·민원)는
        각 도메인 화면에서, 본 화면은 조사항목 준비 등록부입니다.</p>

      <QueryErrorBanner queries={[summary, list]} />

      {summary.data && (
        <Card title={`준비 요약 (전체 준비율 ${summary.data.overall_ready_rate ?? "—"}% · ${summary.data.total_items}항목)`}>
          <div className="grid grid-cols-1 gap-2 sm:grid-cols-2 lg:grid-cols-4">
            {summary.data.domains.map((d) => (
              <div key={d.domain} className="rounded-lg border border-slate-200 p-2">
                <div className="flex items-center justify-between">
                  <p className="text-sm font-medium">{DOMAIN[d.domain] ?? d.domain}</p>
                  <Badge tone={d.ready_rate == null ? "gray" : d.ready_rate >= 90 ? "green"
                    : d.ready_rate >= 70 ? "amber" : "red"}>{d.ready_rate ?? "—"}%</Badge>
                </div>
                <p className="mt-1 text-xs text-slate-500">
                  완료 {d.READY} · 보완 {d.PARTIAL} · 미비 {d.GAP}{d.NA ? ` · N/A ${d.NA}` : ""}</p>
              </div>))}
          </div>
        </Card>
      )}

      <Card title="조사항목" action={
        <span className="flex gap-1">
          <select value={domainF} onChange={(e) => setDomainF(e.target.value)}
            className="rounded border border-slate-300 px-2 py-1 text-xs">
            <option value="">전체 영역</option>
            {Object.entries(DOMAIN).map(([k, v]) => <option key={k} value={k}>{v}</option>)}
          </select>
          <select value={statusF} onChange={(e) => setStatusF(e.target.value)}
            className="rounded border border-slate-300 px-2 py-1 text-xs">
            <option value="">전체 상태</option>
            {STATUS_OPTS.map((k) => <option key={k} value={k}>{STATUS[k].kr}</option>)}
          </select>
        </span>}>
        <Table headers={["영역", "항목", "제목", "조사 기준", "근거", "책임부서", "상태", "점검일", ""]}
          rows={(list.data?.items ?? []).map((it) => [
            DOMAIN[it.domain] ?? it.domain,
            <code key="c" className="text-xs">{it.item_code}</code>, it.title,
            // 조사 기준이 없으면 READY/GAP 판정의 근거를 화면에서 확인할 수 없다
            <span key="r" className="block max-w-sm whitespace-pre-wrap text-xs text-slate-600">
              {it.requirement ?? "—"}</span>,
            <span key="e" className="text-xs text-slate-500">{it.evidence_note ?? "—"}</span>,
            <span key="o" className="whitespace-nowrap text-xs text-slate-500">
              {it.owner_dept_name ?? (it.owner_dept_id ? `#${it.owner_dept_id}` : "—")}</span>,
            <Badge key="s" tone={(STATUS[it.status] ?? STATUS.GAP).tone}>
              {(STATUS[it.status] ?? { kr: it.status }).kr}</Badge>,
            it.last_reviewed_on ?? "—",
            <Button key="b" size="sm" variant="secondary" onClick={() => setEditing(it)}>갱신</Button>,
          ])} empty={list.isFetching ? "불러오는 중…" : "조사항목 없음 — 추가하세요"} />
      </Card>

      {creating && <CreateModal onDone={() => { setCreating(false); refresh(); }}
        onError={fail} onClose={() => setCreating(false)} />}
      {editing && <EditModal item={editing} onDone={() => { setEditing(null); refresh(); }}
        onError={fail} onClose={() => setEditing(null)} />}
    </div>
  );
}

function CreateModal({ onDone, onError, onClose }: {
  onDone: () => void; onError: (e: unknown) => void; onClose: () => void;
}) {
  const toast = useToast();
  const [f, setF] = useState({ domain: "CARE", item_code: "", title: "", chapter: "",
    requirement: "", evidence_note: "", status: "GAP" });
  const m = useMutation({
    mutationFn: () => api("/regulatory/accreditation/items", { method: "POST", body: JSON.stringify({
      domain: f.domain, item_code: f.item_code, title: f.title, chapter: f.chapter || null,
      requirement: f.requirement || null, evidence_note: f.evidence_note || null,
      status: f.status }) }),
    onSuccess: () => { toast.ok("조사항목 등록"); onDone(); }, onError });
  return (
    <Modal title="인증 조사항목 추가" dirty onClose={onClose}>
      <div className="space-y-2">
        <label className="block text-sm text-slate-600">영역
          <select value={f.domain} onChange={(e) => setF({ ...f, domain: e.target.value })}
            className="w-full rounded-md border border-slate-300 px-2 py-1.5 text-sm">
            {Object.entries(DOMAIN).map(([k, v]) => <option key={k} value={k}>{v}</option>)}
          </select>
        </label>
        <Input label="조사항목 번호" value={f.item_code}
          onChange={(e) => setF({ ...f, item_code: e.target.value })} />
        <Input label="제목" value={f.title} onChange={(e) => setF({ ...f, title: e.target.value })} />
        <Input label="장(章·선택)" value={f.chapter}
          onChange={(e) => setF({ ...f, chapter: e.target.value })} />
        <label className="block text-sm text-slate-600">기준·조사방법
          <textarea value={f.requirement} rows={2}
            onChange={(e) => setF({ ...f, requirement: e.target.value })}
            className="w-full rounded-md border border-slate-300 px-2 py-1.5 text-sm" /></label>
        <Input label="근거·비치자료" value={f.evidence_note}
          onChange={(e) => setF({ ...f, evidence_note: e.target.value })} />
        <label className="block text-sm text-slate-600">상태
          <select value={f.status} onChange={(e) => setF({ ...f, status: e.target.value })}
            className="w-full rounded-md border border-slate-300 px-2 py-1.5 text-sm">
            {STATUS_OPTS.map((k) => <option key={k} value={k}>{STATUS[k].kr}</option>)}
          </select>
        </label>
        <Button disabled={m.isPending || !f.item_code.trim() || !f.title.trim()}
          onClick={() => m.mutate()}>등록</Button>
      </div>
    </Modal>
  );
}

function EditModal({ item, onDone, onError, onClose }: {
  item: Item; onDone: () => void; onError: (e: unknown) => void; onClose: () => void;
}) {
  const toast = useToast();
  const [f, setF] = useState({ status: item.status, evidence_note: item.evidence_note ?? "",
    note: item.note ?? "" });
  const m = useMutation({
    mutationFn: () => api(`/regulatory/accreditation/items/${item.id}`, { method: "PATCH",
      body: JSON.stringify({ status: f.status, evidence_note: f.evidence_note,
        note: f.note || null, reviewed: true }) }),
    onSuccess: () => { toast.ok("갱신 (점검일 기록)"); onDone(); }, onError });
  return (
    <Modal title={`조사항목 갱신 — ${item.item_code}`} dirty onClose={onClose}>
      <div className="space-y-2">
        <p className="text-xs text-slate-500">{item.title}</p>
        <label className="block text-sm text-slate-600">상태
          <select value={f.status} onChange={(e) => setF({ ...f, status: e.target.value })}
            className="w-full rounded-md border border-slate-300 px-2 py-1.5 text-sm">
            {STATUS_OPTS.map((k) => <option key={k} value={k}>{STATUS[k].kr}</option>)}
          </select>
        </label>
        <Input label="근거·비치자료" value={f.evidence_note}
          onChange={(e) => setF({ ...f, evidence_note: e.target.value })} />
        <Input label="비고" value={f.note} onChange={(e) => setF({ ...f, note: e.target.value })} />
        <Button disabled={m.isPending} onClick={() => m.mutate()}>갱신</Button>
      </div>
    </Modal>
  );
}

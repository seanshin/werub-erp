"use client";
/** 인사 — 복리후생 (Tier3·0230): 제도·연 부여·신청/승인/지급·잔액.
 *  신청은 전 직원(본인), 제도·부여·승인·지급은 관리자(서버 게이트). */
import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { ApiError, api, auth } from "@/lib/api";
import { Badge, Button, Card, Input, Modal, Table } from "@/components/ui";
import { QueryErrorBanner } from "@/components/query-guard";
import { won } from "@/lib/format";
import { todayLocal } from "@/lib/dates";
import { useConfirm } from "@/components/confirm";
import { useToast } from "@/components/toast";

const TYPE: Record<string, string> = {
  POINT: "복지포인트", SUBSIDY: "지원금", CONGRATS: "경조사", EVENT: "행사", OTHER: "기타",
};
const CLAIM_STATUS: Record<string, { label: string; tone: "amber" | "green" | "blue" | "red" }> = {
  REQUESTED: { label: "신청", tone: "amber" }, APPROVED: { label: "승인", tone: "green" },
  PAID: { label: "지급", tone: "blue" }, REJECTED: { label: "반려", tone: "red" },
};
const CUR_YEAR = Number(todayLocal().slice(0, 4));
const MANAGE = new Set(["DIRECTOR", "ADMIN"]);

interface Program {
  id: number; code: string; title: string; benefit_type: string; unit: string;
  annual_limit: number; target: string; target_value: string | null;
  requires_receipt: boolean; active: boolean;
}
interface Balance { program_id: number; code: string; title: string; unit: string;
  limit: number; used: number; remaining: number }
interface Claim {
  id: number; program_id: number; program_title: string; employee_id: number;
  employee_no: string; name: string; amount: number; claim_on: string; reason: string | null;
  status: string; paid_on: string | null; voucher_ref: string | null;
}

export default function WelfarePage() {
  const qc = useQueryClient();
  const toast = useToast();
  const confirm = useConfirm();
  const fail = (e: unknown) => toast.err(e instanceof ApiError ? e.error.message : "처리 실패");
  const canManage = MANAGE.has(auth.claims()?.role ?? "");
  const [tab, setTab] = useState<"me" | "admin">("me");
  const [claiming, setClaiming] = useState(false);

  const balance = useQuery({ queryKey: ["welfare-bal"], queryFn: () =>
    api<{ items: Balance[] }>(`/hr/welfare/balance?year=${CUR_YEAR}`) });
  const myClaims = useQuery({ queryKey: ["welfare-claims", "me"], queryFn: () =>
    api<{ items: Claim[] }>(`/hr/welfare/claims?year=${CUR_YEAR}`) });
  const refresh = () => { qc.invalidateQueries({ queryKey: ["welfare-bal"] });
    qc.invalidateQueries({ queryKey: ["welfare-claims"] }); };

  return (
    <div className="space-y-4">
      <div className="flex items-center gap-2">
        <h1 className="text-lg font-bold">인사 — 복리후생</h1>
        <div className="ml-auto flex gap-1">
          <Button variant={tab === "me" ? "primary" : "secondary"} size="sm"
            onClick={() => setTab("me")}>내 복지</Button>
          {canManage && <Button variant={tab === "admin" ? "primary" : "secondary"} size="sm"
            onClick={() => setTab("admin")}>제도·승인</Button>}
        </div>
      </div>

      {tab === "me" ? (
        <>
          <QueryErrorBanner queries={[balance, myClaims]} />
          <div className="flex items-center justify-between">
            <p className="text-xs text-slate-500">{CUR_YEAR}년 복리후생 잔액과 신청 내역입니다. 영수증이
              필요한 제도는 승인 시 확인됩니다.</p>
            <Button onClick={() => setClaiming(true)}>복지 신청</Button>
          </div>
          <div className="grid grid-cols-1 gap-3 sm:grid-cols-2 lg:grid-cols-3">
            {(balance.data?.items ?? []).map((b) => (
              <Card key={b.program_id} title={b.title}>
                <p className="text-sm text-slate-500">한도 {won(b.limit)} · 사용 {won(b.used)}</p>
                <p className="text-xl font-bold text-emerald-700">잔액 {won(b.remaining)}</p>
              </Card>))}
            {balance.data && balance.data.items.length === 0 &&
              <p className="text-sm text-slate-400">대상 복리후생 제도가 없습니다.</p>}
          </div>
          <Card title="내 신청 내역">
            <Table headers={["제도", "금액", "신청일", "사유", "상태", "지급일"]}
              rows={(myClaims.data?.items ?? []).map((c) => [
                c.program_title, won(c.amount), c.claim_on, c.reason ?? "—",
                <Badge key="s" tone={CLAIM_STATUS[c.status]?.tone ?? "amber"}>
                  {CLAIM_STATUS[c.status]?.label ?? c.status}</Badge>,
                c.paid_on ?? "—",
              ])} empty={myClaims.isFetching ? "불러오는 중…" : "신청 내역 없음"} />
          </Card>
        </>
      ) : (
        <AdminPanel confirm={confirm} onError={fail} />
      )}

      {claiming && (
        <Modal title="복지 신청" dirty onClose={() => setClaiming(false)}>
          <ClaimForm programs={(balance.data?.items ?? [])}
            onDone={() => { setClaiming(false); refresh(); }} onError={fail}
            onCancel={() => setClaiming(false)} />
        </Modal>
      )}
    </div>
  );
}

function ClaimForm({ programs, onDone, onError, onCancel }: {
  programs: Balance[]; onDone: () => void; onError: (e: unknown) => void; onCancel: () => void;
}) {
  const toast = useToast();
  const [f, setF] = useState({ program_id: "", amount: "", claim_on: todayLocal(), reason: "" });
  const sel = programs.find((p) => String(p.program_id) === f.program_id);
  const create = useMutation({
    mutationFn: () => api("/hr/welfare/claims", { method: "POST", body: JSON.stringify({
      program_id: Number(f.program_id), amount: Number(f.amount),
      claim_on: f.claim_on, reason: f.reason || null }) }),
    onSuccess: () => { toast.ok("복지 신청 접수"); onDone(); }, onError });
  const over = sel != null && Number(f.amount) > sel.remaining;
  return (
    <div className="space-y-2">
      <label className="block text-sm text-slate-600">제도
        <select value={f.program_id} onChange={(e) => setF({ ...f, program_id: e.target.value })}
          className="w-full rounded-md border border-slate-300 px-2 py-1.5 text-sm">
          <option value="">선택하세요</option>
          {programs.map((p) => <option key={p.program_id} value={p.program_id}>
            {p.title} (잔액 {won(p.remaining)})</option>)}
        </select>
      </label>
      <Input label="금액" value={f.amount} inputMode="numeric"
        onChange={(e) => setF({ ...f, amount: e.target.value.replace(/[^0-9]/g, "") })} />
      {over && <p className="text-xs text-red-600">잔액({won(sel!.remaining)})을 초과했습니다.</p>}
      <Input label="신청일" type="date" value={f.claim_on}
        onChange={(e) => setF({ ...f, claim_on: e.target.value })} />
      <Input label="사유(선택)" value={f.reason} onChange={(e) => setF({ ...f, reason: e.target.value })} />
      <div className="flex gap-2">
        <Button disabled={create.isPending || !f.program_id || !(Number(f.amount) > 0) || over}
          onClick={() => create.mutate()}>신청</Button>
        <Button variant="secondary" onClick={onCancel}>닫기</Button>
      </div>
    </div>
  );
}

function AdminPanel({ confirm, onError }: {
  confirm: ReturnType<typeof useConfirm>; onError: (e: unknown) => void;
}) {
  const qc = useQueryClient();
  const toast = useToast();
  const [creating, setCreating] = useState(false);
  const [statusFilter, setStatusFilter] = useState("REQUESTED");
  const programs = useQuery({ queryKey: ["welfare-programs"], queryFn: () =>
    api<{ items: Program[] }>("/hr/welfare/programs") });
  const claims = useQuery({ queryKey: ["welfare-claims", "admin", statusFilter], queryFn: () =>
    api<{ items: Claim[] }>(`/hr/welfare/claims?year=${CUR_YEAR}${statusFilter ? `&status=${statusFilter}` : ""}`) });
  const refresh = () => { qc.invalidateQueries({ queryKey: ["welfare-programs"] });
    qc.invalidateQueries({ queryKey: ["welfare-claims"] }); };

  const create = useMutation({
    mutationFn: (b: unknown) => api("/hr/welfare/programs", { method: "POST", body: JSON.stringify(b) }),
    onSuccess: () => { toast.ok("제도 등록"); setCreating(false); refresh(); }, onError });
  const decide = useMutation({
    mutationFn: (v: { id: number; approve: boolean }) =>
      api(`/hr/welfare/claims/${v.id}/decide`, { method: "POST",
        body: JSON.stringify({ approve: v.approve }) }),
    onSuccess: (_r, v) => { toast.ok(v.approve ? "승인" : "반려"); refresh(); }, onError });
  const pay = useMutation({
    mutationFn: (id: number) => api(`/hr/welfare/claims/${id}/pay`, { method: "POST",
      body: JSON.stringify({}) }),
    onSuccess: () => { toast.ok("지급 완료 (DR 5330/CR 1110)"); refresh(); }, onError });

  const onDecide = async (c: Claim, approve: boolean) => {
    if (await confirm({ title: approve ? "복지 승인" : "복지 반려",
      body: `${c.name} · ${won(c.amount)}을(를) ${approve ? "승인" : "반려"}할까요?`,
      danger: !approve, confirmLabel: approve ? "승인" : "반려" }))
      decide.mutate({ id: c.id, approve });
  };
  const onPay = async (c: Claim) => {
    if (await confirm({ title: "복지 지급", body: `${won(c.amount)} 지급 전표(DR 5330/CR 1110)를 생성합니다.`,
      confirmLabel: "지급" })) pay.mutate(c.id);
  };

  return (
    <div className="space-y-4">
      <QueryErrorBanner queries={[programs, claims]} />
      <Card title="복리후생 제도" action={<Button size="sm" onClick={() => setCreating(true)}>제도 등록</Button>}>
        <Table headers={["코드", "제도", "유형", "연 한도", "대상", "영수증", "상태"]}
          rows={(programs.data?.items ?? []).map((p) => [
            p.code, p.title, TYPE[p.benefit_type] ?? p.benefit_type, won(p.annual_limit),
            p.target === "ALL" ? "전체" : `${p.target}:${p.target_value ?? ""}`,
            p.requires_receipt ? "필요" : "불요",
            p.active ? <Badge key="a" tone="green">활성</Badge> : <Badge key="a" tone="gray">비활성</Badge>,
          ])} empty={programs.isFetching ? "불러오는 중…" : "제도 없음"} />
      </Card>
      <Card title="신청 처리" action={
        <select value={statusFilter} onChange={(e) => setStatusFilter(e.target.value)}
          className="rounded-md border border-slate-300 px-2 py-1 text-xs">
          <option value="">전체</option>
          {Object.keys(CLAIM_STATUS).map((s) => <option key={s} value={s}>{CLAIM_STATUS[s].label}</option>)}
        </select>}>
        <Table headers={["직원", "제도", "금액", "사유", "신청일", "상태", "지급일", ""]}
          rows={(claims.data?.items ?? []).map((c) => [
            `${c.name}(${c.employee_no})`, c.program_title, won(c.amount),
            // 승인/반려의 유일한 근거인데 관리자 표에 사유 열이 없어 근거 없이 결재했다
            <span key="r" className="text-xs text-slate-600" title={c.reason ?? ""}>
              {c.reason ?? "—"}</span>,
            c.claim_on,
            <Badge key="s" tone={CLAIM_STATUS[c.status]?.tone ?? "amber"}>
              {CLAIM_STATUS[c.status]?.label ?? c.status}</Badge>,
            c.paid_on ?? "—",
            <span key="a" className="flex gap-1">
              {c.status === "REQUESTED" && <>
                <Button variant="success" size="sm" disabled={decide.isPending}
                  onClick={() => onDecide(c, true)}>승인</Button>
                <Button variant="danger" size="sm" disabled={decide.isPending}
                  onClick={() => onDecide(c, false)}>반려</Button></>}
              {c.status === "APPROVED" && <Button size="sm" disabled={pay.isPending}
                onClick={() => onPay(c)}>지급</Button>}
            </span>,
          ])} empty={claims.isFetching ? "불러오는 중…" : "신청 없음"} />
      </Card>
      {creating && (
        <Modal title="복리후생 제도 등록" onClose={() => setCreating(false)}>
          <ProgramForm onSubmit={(b) => create.mutate(b)} isLoading={create.isPending}
            onCancel={() => setCreating(false)} />
        </Modal>
      )}
    </div>
  );
}

function ProgramForm({ onSubmit, isLoading, onCancel }: {
  onSubmit: (b: unknown) => void; isLoading: boolean; onCancel: () => void;
}) {
  const [f, setF] = useState({ code: "", title: "", benefit_type: "POINT",
    annual_limit: "", target: "ALL", target_value: "", requires_receipt: true });
  return (
    <div className="space-y-2">
      <div className="grid grid-cols-2 gap-2">
        <Input label="코드" value={f.code} onChange={(e) => setF({ ...f, code: e.target.value })}
          placeholder="예: POINT" />
        <Input label="제도명" value={f.title} onChange={(e) => setF({ ...f, title: e.target.value })} />
      </div>
      <div className="grid grid-cols-2 gap-2">
        <label className="block text-sm text-slate-600">유형
          <select value={f.benefit_type} onChange={(e) => setF({ ...f, benefit_type: e.target.value })}
            className="w-full rounded-md border border-slate-300 px-2 py-1.5 text-sm">
            {Object.entries(TYPE).map(([k, v]) => <option key={k} value={k}>{v}</option>)}
          </select>
        </label>
        <Input label="연 한도(원)" value={f.annual_limit} inputMode="numeric"
          onChange={(e) => setF({ ...f, annual_limit: e.target.value.replace(/[^0-9]/g, "") })} />
      </div>
      <div className="grid grid-cols-2 gap-2">
        <label className="block text-sm text-slate-600">대상
          <select value={f.target} onChange={(e) => setF({ ...f, target: e.target.value })}
            className="w-full rounded-md border border-slate-300 px-2 py-1.5 text-sm">
            <option value="ALL">전체</option><option value="JOB_CATEGORY">직종</option>
            <option value="DEPT">부서</option>
          </select>
        </label>
        {f.target !== "ALL" && (
          <Input label={f.target === "JOB_CATEGORY" ? "직종코드" : "부서ID"} value={f.target_value}
            onChange={(e) => setF({ ...f, target_value: e.target.value })} />)}
      </div>
      <label className="flex items-center gap-1 text-sm text-slate-600">
        <input type="checkbox" checked={f.requires_receipt}
          onChange={(e) => setF({ ...f, requires_receipt: e.target.checked })} /> 영수증 필요
      </label>
      <div className="flex gap-2">
        <Button disabled={isLoading || !f.code.trim() || !f.title.trim()}
          onClick={() => onSubmit({ code: f.code, title: f.title, benefit_type: f.benefit_type,
            annual_limit: Number(f.annual_limit || 0), target: f.target,
            target_value: f.target === "ALL" ? null : f.target_value,
            requires_receipt: f.requires_receipt })}>등록</Button>
        <Button variant="secondary" onClick={onCancel}>닫기</Button>
      </div>
    </div>
  );
}

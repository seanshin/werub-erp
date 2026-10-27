"use client";
/** 자재 — 위수탁 계약 관리(CLM 생애주기): 등록→상신→승인/반려→발효→(월비용·변경·갱신)→해지.
 *  급식·세탁·폐기물·보안 등 기간·금액. 직무분리(작성자≠승인자·서버 강제)·월 위탁료 미지급 발생. */
import { monthLocal, todayLocal } from "@/lib/dates";
import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { ApiError, api, apiDownload } from "@/lib/api";
import { won } from "@/lib/format";
import { Badge, Button, Card, Input, Modal, Table } from "@/components/ui";
import { useToast } from "@/components/toast";
import { useConfirm } from "@/components/confirm";
import { useInputDialog } from "@/components/input-dialog";

interface Item { id: number; vendor_name: string; category: string; title: string;
  monthly_amount: number; start_on: string; end_on: string; d_day: number;
  auto_renew: boolean; status: string; expiring_soon: boolean; billing_type: string;
  contract_type: string; effective_on: string | null; sign_status: string | null;
  policy_no: string | null; coverage_limit: number | null; premium_cycle: string | null }
interface ListOut { items: Item[]; monthly_total: number;
  by_category: { category: string; monthly: number }[]; expiring_soon: Item[] }

const CAT: Record<string, string> = { MEAL: "급식", LAUNDRY: "세탁", WASTE: "의료폐기물",
  SECURITY: "보안", CLEANING: "미화", FACILITY: "시설", IT: "IT",
  INSURANCE: "보험", OTHER: "기타" };   // INSURANCE 누락으로 원문 코드가 노출되던 것 보완
// 화면 제목이 "위수탁"이라 전 계약이 위수탁으로 읽히던 문제 — 유형을 명시한다
const CTYPE: Record<string, string> = { OUTSOURCING: "위수탁", PURCHASE: "구매", SERVICE: "용역",
  LEASE: "임대", INSURANCE: "보험", OTHER: "기타" };
// ⚠ ONE_TIME 계약의 monthly_amount는 **총액**이다 — "월 금액" 열에 그대로 찍으면
//    일회성 5억 계약이 월 5억으로 읽힌다(금액 오독).
const BILLING: Record<string, string> = { MONTHLY: "월", ONE_TIME: "일시", ANNUAL: "연", USAGE: "사용량" };
const ST: Record<string, { kr: string; tone: "green" | "red" | "amber" | "blue" | "gray" }> = {
  DRAFT: { kr: "작성중", tone: "gray" }, PENDING_APPROVAL: { kr: "결재중", tone: "blue" },
  APPROVED: { kr: "승인완료", tone: "blue" }, ACTIVE: { kr: "활성", tone: "green" },
  EXPIRING: { kr: "만기임박", tone: "amber" }, EXPIRED: { kr: "만료", tone: "red" },
  TERMINATED: { kr: "해지", tone: "gray" }, REJECTED: { kr: "반려", tone: "red" } };

export default function ContractsPage() {
  const qc = useQueryClient();
  const confirmDialog = useConfirm();
  const { ask, dialog } = useInputDialog();
  const toast = useToast();
  const [form, setForm] = useState({ vendor_name: "", category: "MEAL", title: "",
    monthly_amount: "", start_on: "", end_on: "" });
  const [amending, setAmending] = useState<Item | null>(null);
  const fail = (e: unknown) => toast.err(e instanceof ApiError ? e.error.message : "처리 실패");
  const refresh = () => qc.invalidateQueries({ queryKey: ["contracts"] });

  const list = useQuery({ queryKey: ["contracts"], queryFn: () =>
    api<ListOut>("/inventory/contracts") });

  const register = useMutation({
    mutationFn: () => api("/inventory/contracts", { method: "POST", body: JSON.stringify({
      ...form, monthly_amount: Number(form.monthly_amount) }) }),
    onSuccess: () => { toast.ok("계약 등록 완료"); refresh(); }, onError: fail });
  const submit = useMutation({
    mutationFn: (id: number) => api(`/inventory/contracts/${id}/submit`, { method: "POST" }),
    onSuccess: () => { toast.ok("결재 상신"); refresh(); }, onError: fail });
  const activate = useMutation({
    mutationFn: (id: number) => api(`/inventory/contracts/${id}/activate`, { method: "POST" }),
    onSuccess: () => { toast.ok("발효(ACTIVE)"); refresh(); }, onError: fail });
  const approve = useMutation({
    mutationFn: (id: number) => api<{ pending_next_step: boolean }>(
      `/inventory/contracts/${id}/approve`, { method: "POST" }),
    onSuccess: (r) => { toast.ok(r.pending_next_step ? "승인 — 다음 결재 단계 대기" : "승인 완료"); refresh(); },
    onError: fail });
  const reject = useMutation({
    mutationFn: (p: { id: number; reason: string }) => api(`/inventory/contracts/${p.id}/reject`,
      { method: "POST", body: JSON.stringify({ reason: p.reason }) }),
    onSuccess: () => { toast.ok("반려"); refresh(); }, onError: fail });
  const renew = useMutation({
    mutationFn: (p: { id: number; on: string }) =>
      api(`/inventory/contracts/${p.id}/renew?new_end_on=${p.on}`, { method: "POST" }),
    onSuccess: () => { toast.ok("갱신 완료"); refresh(); }, onError: fail });
  const terminate = useMutation({
    mutationFn: (p: { id: number; reason: string }) => api(`/inventory/contracts/${p.id}/terminate`,
      { method: "POST", body: JSON.stringify({ reason: p.reason }) }),
    onSuccess: () => { toast.ok("해지 완료"); refresh(); }, onError: fail });
  const accrue = useMutation({
    mutationFn: (p: { id: number; ym: string }) =>
      api<{ duplicate: boolean; amount?: number }>(
        `/inventory/contracts/${p.id}/accrue/${p.ym}`, { method: "POST" }),
    onSuccess: (r) => { toast.ok(r.duplicate ? "이미 발생된 월입니다(멱등)"
      : `미지급 발생 ${won(r.amount ?? 0)} — 재무·정산에서 지급/세금계산서 대사`); refresh(); },
    onError: fail });

  const onRenew = async (c: Item) => {
    const on = await ask({ title: "계약 갱신", label: "갱신 종료일", kind: "date", confirmLabel: "갱신" });
    if (on) renew.mutate({ id: c.id, on: String(on) });
  };
  const onAccrue = async (c: Item) => {
    const ym = await ask({ title: "위탁료 발생", label: "발생 월 (YYYY-MM)", placeholder: "YYYY-MM",
      required: true, defaultValue: monthLocal(),
      validate: (v) => /^\d{4}-\d{2}$/.test(v) ? null : "YYYY-MM 형식으로 입력하세요", confirmLabel: "발생" });
    if (ym) accrue.mutate({ id: c.id, ym: String(ym) });
  };
  const onTerminate = async (c: Item) => {
    const reason = await ask({ title: "위수탁 계약 해지", label: "해지 사유", confirmLabel: "해지" });
    if (reason !== null) terminate.mutate({ id: c.id, reason: String(reason) });
  };
  const onReject = async (c: Item) => {
    const reason = await ask({ title: "계약 반려", label: "반려 사유", confirmLabel: "반려" });
    if (reason !== null) reject.mutate({ id: c.id, reason: String(reason) });
  };
  const onActivate = async (c: Item) => {
    if (await confirmDialog({ title: "계약 발효", body: "이 계약을 발효(ACTIVE)합니다.",
      confirmLabel: "발효" })) activate.mutate(c.id);
  };

  const d = list.data;
  const input = "rounded-md border border-slate-300 px-2 py-1 text-sm";
  const busy = submit.isPending || approve.isPending || reject.isPending || activate.isPending
    || renew.isPending || terminate.isPending;

  return (
    <div className="space-y-4">
      {dialog}
      <h1 className="text-lg font-bold">자재 — 위수탁 계약 관리</h1>
      <p className="text-xs text-slate-500">
        CLM 생애주기: 등록(작성중) → 상신 → 승인/반려 → 발효 → 월비용·변경·갱신 → 해지.
        승인은 직무분리(작성자≠승인자·서버 강제), 결재선 룰이 있으면 다단계입니다.</p>
      {(d?.expiring_soon.length ?? 0) > 0 && (
        <p className="rounded-md bg-amber-50 px-3 py-1.5 text-sm text-amber-700">
          만기 임박(60일 내) {d!.expiring_soon.length}건 — {
            d!.expiring_soon.map((i) => `${i.vendor_name}(D-${i.d_day})`).join(", ")}
        </p>
      )}

      <Card title="등록">
        <div className="flex flex-wrap items-end gap-2">
          <input placeholder="업체명" value={form.vendor_name} className={`${input} w-36`}
            onChange={(e) => setForm({ ...form, vendor_name: e.target.value })} />
          <select value={form.category} className={input}
            onChange={(e) => setForm({ ...form, category: e.target.value })}>
            {Object.entries(CAT).map(([k, v]) => <option key={k} value={k}>{v}</option>)}
          </select>
          <input placeholder="계약명" value={form.title} className={`${input} w-56`}
            onChange={(e) => setForm({ ...form, title: e.target.value })} />
          <input placeholder="월 금액" inputMode="numeric" value={form.monthly_amount}
            className={`${input} w-28`}
            onChange={(e) => setForm({ ...form, monthly_amount: e.target.value })} />
          <input type="date" value={form.start_on} className={input}
            onChange={(e) => setForm({ ...form, start_on: e.target.value })} />
          <input type="date" value={form.end_on} className={input}
            onChange={(e) => setForm({ ...form, end_on: e.target.value })} />
          <button disabled={!form.vendor_name || !form.title || !form.start_on || !form.end_on || register.isPending}
            onClick={() => register.mutate()}
            className="rounded-md bg-blue-600 px-4 py-1.5 text-sm font-medium text-white disabled:opacity-50">
            등록</button>
        </div>
      </Card>

      <Card title={`계약 목록 — 활성 월비용 ${won(d?.monthly_total ?? 0)}`} action={
        <span className="text-[11px] text-slate-400">
          {(d?.by_category ?? []).map((b) => `${CAT[b.category] ?? b.category} ${won(b.monthly)}`).join(" · ")}</span>}>
        <Table headers={["업체", "유형", "구분", "계약명", "금액", "기간", "D-day", "상태",
          "서명", "생애주기"]}
          rows={(d?.items ?? []).map((c) => {
            const st = ST[c.status] ?? { kr: c.status, tone: "gray" as const };
            const active = c.status === "ACTIVE" || c.status === "EXPIRING";
            return [
              c.vendor_name,
              <Badge key="t" tone="gray">{CTYPE[c.contract_type] ?? c.contract_type}</Badge>,
              CAT[c.category] ?? c.category, c.title,
              // 청구유형을 금액에 붙여 총액/월액 오독을 막는다
              `${won(c.monthly_amount)} / ${BILLING[c.billing_type] ?? c.billing_type}`,
              `${c.start_on} ~ ${c.end_on}`,
              active ? <span key="d" className="flex items-center gap-1">
                <span className={c.expiring_soon ? "font-semibold text-amber-600" : ""}>D-{c.d_day}</span>
                {/* 자동갱신은 만기 전 해지통보를 놓치면 1년 연장된다 — 만기 D-day와 함께 봐야 한다 */}
                {c.auto_renew && <Badge tone="blue">자동갱신</Badge>}</span> : "-",
              <Badge key="s" tone={st.tone}>{st.kr}</Badge>,
              c.sign_status ? <Badge key="sg" tone={c.sign_status === "SIGNED" ? "green" : "amber"}>
                {c.sign_status === "SIGNED" ? "서명완료" : c.sign_status}</Badge> : "—",
              <span key="b" className="flex flex-wrap gap-1">
                {c.status === "DRAFT" && (<>
                  <Button size="sm" disabled={busy} onClick={() => submit.mutate(c.id)}>상신</Button>
                  <Button size="sm" variant="secondary" disabled={busy} onClick={() => onActivate(c)}>발효</Button>
                </>)}
                {c.status === "PENDING_APPROVAL" && (<>
                  <Button size="sm" disabled={busy} onClick={() => approve.mutate(c.id)}>승인</Button>
                  <Button size="sm" variant="danger" disabled={busy} onClick={() => onReject(c)}>반려</Button>
                </>)}
                {c.status === "APPROVED" && (
                  <Button size="sm" disabled={busy} onClick={() => onActivate(c)}>발효</Button>)}
                {active && (<>
                  <Button size="sm" variant="secondary" disabled={accrue.isPending}
                    onClick={() => onAccrue(c)}>월비용</Button>
                  <Button size="sm" variant="secondary" onClick={() => setAmending(c)}>금액변경</Button>
                  <Button size="sm" variant="secondary" disabled={busy} onClick={() => onRenew(c)}>갱신</Button>
                  <Button size="sm" variant="danger" disabled={busy} onClick={() => onTerminate(c)}>해지</Button>
                </>)}
                {c.status === "EXPIRED" && (
                  <Button size="sm" variant="secondary" disabled={busy} onClick={() => onRenew(c)}>갱신</Button>)}
                {c.status !== "TERMINATED" && (
                  <Button size="sm" variant="neutral"
                    onClick={() => apiDownload(`/inventory/contracts/${c.id}/pdf`, `contract-${c.id}.pdf`)}>PDF</Button>)}
              </span>,
            ];
          })} empty={list.isFetching ? "불러오는 중…" : "계약 없음"} />
      </Card>

      {amending && <AmendModal contract={amending}
        onDone={() => { setAmending(null); refresh(); }} onError={fail}
        onClose={() => setAmending(null)} />}
    </div>
  );
}

function AmendModal({ contract, onDone, onError, onClose }: {
  contract: Item; onDone: () => void; onError: (e: unknown) => void; onClose: () => void;
}) {
  const toast = useToast();
  const [f, setF] = useState({ new_monthly: String(contract.monthly_amount),
    effective_on: todayLocal(), reason: "" });
  const m = useMutation({
    mutationFn: () => api(`/inventory/contracts/${contract.id}/amend-amount`, { method: "POST",
      body: JSON.stringify({ new_monthly: Number(f.new_monthly), effective_on: f.effective_on,
        reason: f.reason }) }),
    onSuccess: () => { toast.ok("월 위탁료 변경 (변경이력 기록)"); onDone(); }, onError });
  return (
    <Modal title={`월 위탁료 변경 — ${contract.vendor_name}`} dirty onClose={onClose}>
      <div className="space-y-2">
        <p className="text-xs text-slate-500">현재 {won(contract.monthly_amount)} → 변경 후 금액 입력.
          변경일 이후 accrue부터 새 금액이 적용됩니다.</p>
        <Input label="변경 후 월 금액" value={f.new_monthly} inputMode="numeric"
          onChange={(e) => setF({ ...f, new_monthly: e.target.value.replace(/[^0-9]/g, "") })} />
        <Input label="적용일" type="date" value={f.effective_on}
          onChange={(e) => setF({ ...f, effective_on: e.target.value })} />
        <Input label="사유" value={f.reason} onChange={(e) => setF({ ...f, reason: e.target.value })} />
        <Button disabled={m.isPending || !(Number(f.new_monthly) > 0)} onClick={() => m.mutate()}>
          변경</Button>
      </div>
    </Modal>
  );
}

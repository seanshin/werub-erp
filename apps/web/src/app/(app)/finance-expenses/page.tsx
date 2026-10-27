"use client";
/** 재무 — 경비청구·법인카드: 경비 기안(DRAFT)→상신(PENDING)→승인(APPROVED)→지급(PAID). 반려(REJECTED).
 *  법인카드 등록·사용내역·월정산(DR2112/CR1110). 승인/지급 권한은 백엔드 게이트(재무 승인권·ADMIN 지급). */
import { todayLocal } from "@/lib/dates";
import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { ApiError, api } from "@/lib/api";
import { Badge, Button, Card, Input, Modal, Table } from "@/components/ui";
import { QueryErrorBanner } from "@/components/query-guard";
import { won } from "@/lib/format";
import { useConfirm } from "@/components/confirm";
import { useToast } from "@/components/toast";

const STATUS_TONE: Record<string, "gray" | "amber" | "green" | "red" | "blue"> = {
  DRAFT: "gray", PENDING: "amber", APPROVED: "green", PAID: "blue", REJECTED: "red",
};
const ACCOUNTS = [
  ["5110", "여비교통비"], ["5120", "접대비"], ["5130", "복리후생비"], ["5140", "소모품비"],
  ["5150", "교육훈련비"], ["5160", "회의비"], ["5190", "잡비"],
];

interface Claim {
  id: number; employee_id: number; title: string; claim_on: string;
  total_amount: number; status: string; paid_on: string | null;
}
interface Line {
  id?: number; expense_on: string; account_code: string; description: string;
  amount: number; merchant: string | null; card_tx_id?: number | null;
}
interface ClaimDetail extends Claim {
  note: string | null; voucher_ref: string | null; payment_voucher_ref: string | null; lines: Line[];
}
interface Cardt { id: number; alias: string; card_last4: string; is_active: boolean }
interface CardTx {
  id: number; tx_id: string; tx_on: string; merchant: string | null; amount: number;
  status: string; claim_id: number | null; settled_on: string | null;
}

export default function ExpensesPage() {
  const qc = useQueryClient();
  const toast = useToast();
  const confirm = useConfirm();
  const fail = (e: unknown) => toast.err(e instanceof ApiError ? e.error.message : "처리 실패");
  const [tab, setTab] = useState<"claims" | "cards">("claims");

  return (
    <div className="space-y-4">
      <div className="flex items-center gap-2">
        <h1 className="text-lg font-bold">재무 — 경비청구·법인카드</h1>
        <div className="ml-auto flex gap-1">
          <Button variant={tab === "claims" ? "primary" : "secondary"} size="sm"
            onClick={() => setTab("claims")}>경비청구</Button>
          <Button variant={tab === "cards" ? "primary" : "secondary"} size="sm"
            onClick={() => setTab("cards")}>법인카드</Button>
        </div>
      </div>
      {tab === "claims"
        ? <Claims qc={qc} toast={toast} confirm={confirm} fail={fail} />
        : <Cards qc={qc} toast={toast} fail={fail} />}
    </div>
  );
}

/* eslint-disable @typescript-eslint/no-explicit-any */
function Claims({ qc, toast, confirm, fail }: any) {
  const [status, setStatus] = useState("");
  const [creating, setCreating] = useState(false);
  const [detailId, setDetailId] = useState<number | null>(null);

  const claims = useQuery({ queryKey: ["expenses", status], queryFn: () =>
    api<{ items: Claim[] }>(`/finance/expenses${status ? `?status=${status}` : ""}`) });
  const detail = useQuery({ enabled: detailId != null, queryKey: ["expense", detailId], queryFn: () =>
    api<ClaimDetail>(`/finance/expenses/${detailId}`) });
  const refresh = () => { qc.invalidateQueries({ queryKey: ["expenses"] });
    qc.invalidateQueries({ queryKey: ["expense"] }); };

  const act = useMutation({
    mutationFn: (v: { id: number; action: string; body?: unknown }) =>
      api(`/finance/expenses/${v.id}/${v.action}`, { method: "POST",
        body: v.body ? JSON.stringify(v.body) : undefined }),
    onSuccess: (_r, v) => { toast.ok(`${LABEL[v.action]} 완료`); refresh(); },
    onError: fail,
  });
  const create = useMutation({
    mutationFn: (b: unknown) => api<{ id: number }>("/finance/expenses", { method: "POST",
      body: JSON.stringify(b) }),
    onSuccess: () => { toast.ok("경비 기안 생성(DRAFT)"); setCreating(false); refresh(); },
    onError: fail,
  });
  const LABEL: Record<string, string> = { submit: "상신", approve: "승인", reject: "반려", pay: "지급" };

  const onReject = async (id: number) => {
    if (await confirm({ title: "경비 반려", body: "이 경비 청구를 반려할까요?", danger: true, confirmLabel: "반려" }))
      act.mutate({ id, action: "reject", body: { reason: "반려" } });
  };
  const onApprove = async (id: number) => {   // v1.203 — 승인=회계 전기 발생 액션이라 확인 경유
    if (await confirm({ title: "경비 승인", body: "승인 시 비용/미지급(2110·2112) 전표가 전기됩니다.", confirmLabel: "승인" }))
      act.mutate({ id, action: "approve" });
  };
  const onPay = async (id: number) => {
    if (await confirm({ title: "경비 지급", body: "개인분 현금 지급(전기)을 진행할까요? (ADMIN)", confirmLabel: "지급" }))
      act.mutate({ id, action: "pay", body: { paid_on: todayLocal() } });
  };

  const rowActions = (c: Claim) => (
    <span className="flex gap-1">
      <Button variant="secondary" size="sm" onClick={() => setDetailId(c.id)}>상세</Button>
      {c.status === "DRAFT" && <Button size="sm" disabled={act.isPending}
        onClick={() => act.mutate({ id: c.id, action: "submit" })}>상신</Button>}
      {c.status === "PENDING" && <>
        <Button variant="success" size="sm" disabled={act.isPending} onClick={() => onApprove(c.id)}>승인</Button>
        <Button variant="danger" size="sm" disabled={act.isPending} onClick={() => onReject(c.id)}>반려</Button></>}
      {c.status === "APPROVED" && <Button size="sm" disabled={act.isPending} onClick={() => onPay(c.id)}>지급</Button>}
    </span>
  );

  return (
    <>
      <div className="flex items-center justify-between">
        <p className="text-xs text-slate-500">
          경비 기안→상신→승인(재무 승인권·다단계 결재선)→지급(ADMIN). 승인 시 비용/미지급(개인분 2110·
          카드분 2112) 전기, 지급 시 개인분 현금 전기.</p>
        <Button onClick={() => setCreating(true)}>경비 기안</Button>
      </div>
      <QueryErrorBanner queries={[claims]} />
      <Card title="경비 청구" action={
        <select value={status} onChange={(e) => setStatus(e.target.value)}
          className="rounded-md border border-slate-300 px-2 py-1 text-xs">
          <option value="">전체</option>
          {["DRAFT", "PENDING", "APPROVED", "PAID", "REJECTED"].map((s) => <option key={s} value={s}>{s}</option>)}
        </select>}>
        <Table headers={["청구#", "제목", "청구일", "금액", "상태", ""]}
          rows={(claims.data?.items ?? []).map((c: Claim) => [
            `#${c.id}`, c.title, c.claim_on, won(c.total_amount),
            <Badge key="s" tone={STATUS_TONE[c.status] ?? "gray"}>{c.status}</Badge>,
            rowActions(c),
          ])} empty={claims.isFetching ? "불러오는 중…" : "경비 청구 없음"} />
      </Card>

      {creating && (
        <Modal title="경비 기안" size="lg" dirty onClose={() => setCreating(false)}>
          <ClaimForm isLoading={create.isPending} onSubmit={(b) => create.mutate(b)}
            onCancel={() => setCreating(false)} />
        </Modal>
      )}
      {detailId != null && detail.data && (
        <Modal title={`경비 청구 #${detail.data.id} — ${detail.data.title}`} size="lg"
          onClose={() => setDetailId(null)}>
          <div className="space-y-2">
            <div className="flex flex-wrap gap-3 text-sm">
              <Badge tone={STATUS_TONE[detail.data.status] ?? "gray"}>{detail.data.status}</Badge>
              <span>합계 <b>{won(detail.data.total_amount)}</b></span>
              {detail.data.voucher_ref && <span className="text-slate-500">전표 {detail.data.voucher_ref}</span>}
              {detail.data.paid_on && <span className="text-blue-600">지급일 {detail.data.paid_on}</span>}
            </div>
            <Table headers={["사용일", "계정", "내역", "가맹점", "금액", "카드"]}
              rows={detail.data.lines.map((ln) => [
                ln.expense_on, ln.account_code, ln.description, ln.merchant ?? "—",
                won(ln.amount), ln.card_tx_id ? `#${ln.card_tx_id}` : "—",
              ])} empty="라인 없음" />
          </div>
        </Modal>
      )}
    </>
  );
}

function ClaimForm({ onSubmit, isLoading, onCancel }: {
  onSubmit: (b: unknown) => void; isLoading: boolean; onCancel: () => void;
}) {
  const [title, setTitle] = useState("");
  const [claimOn, setClaimOn] = useState(todayLocal());
  const [lines, setLines] = useState<Line[]>([
    { expense_on: todayLocal(), account_code: "5110", description: "", amount: 0, merchant: "" }]);
  // v1.203 — 법인카드 사용분 연결(카드 선택 → UNMATCHED 내역을 라인에 귀속). 미연결이면 개인 경비.
  const [cardId, setCardId] = useState<number | null>(null);
  const cards = useQuery({ queryKey: ["cards"], queryFn: () =>
    api<{ items: Cardt[] }>("/finance/corporate-cards") });
  const unmatched = useQuery({ enabled: cardId != null, queryKey: ["card-tx", cardId, "UNMATCHED"],
    queryFn: () => api<{ items: CardTx[] }>(
      `/finance/corporate-cards/${cardId}/transactions?status=UNMATCHED`) });
  const setLine = (i: number, p: Partial<Line>) =>
    setLines(lines.map((l, j) => (j === i ? { ...l, ...p } : l)));
  const pickTx = (i: number, txId: number | null) => {
    const tx = unmatched.data?.items.find((t) => t.id === txId);
    setLine(i, tx
      ? { card_tx_id: tx.id, amount: tx.amount, merchant: tx.merchant ?? "", expense_on: tx.tx_on }
      : { card_tx_id: null });
  };
  const addLine = () => setLines([...lines,
    { expense_on: todayLocal(), account_code: "5110", description: "", amount: 0, merchant: "" }]);
  const rmLine = (i: number) => setLines(lines.filter((_, j) => j !== i));
  const total = lines.reduce((s, l) => s + (Number(l.amount) || 0), 0);
  const valid = title && lines.length > 0 && lines.every((l) => l.description && Number(l.amount) > 0);
  const usedTx = new Set(lines.map((l) => l.card_tx_id).filter(Boolean));

  return (
    <div className="space-y-2">
      <div className="grid grid-cols-2 gap-2">
        <Input label="제목" value={title} onChange={(e) => setTitle(e.target.value)}
          placeholder="예: 6월 출장 경비" />
        <Input label="청구일" type="date" value={claimOn} onChange={(e) => setClaimOn(e.target.value)} />
      </div>
      <label className="block text-sm text-slate-600">법인카드 사용분 연결(선택)
        <select value={cardId ?? ""} onChange={(e) => setCardId(e.target.value ? Number(e.target.value) : null)}
          className="w-full rounded-md border border-slate-300 px-2 py-1.5 text-sm">
          <option value="">연결 안 함(개인 경비)</option>
          {(cards.data?.items ?? []).filter((c: Cardt) => c.is_active).map((c: Cardt) =>
            <option key={c.id} value={c.id}>{c.alias} (****-{c.card_last4})</option>)}
        </select>
      </label>
      <div className="space-y-1.5">
        <div className="flex items-center justify-between">
          <span className="text-sm font-medium text-slate-600">경비 내역</span>
          <Button variant="secondary" size="sm" onClick={addLine}>+ 줄 추가</Button>
        </div>
        {lines.map((l, i) => (
          <div key={i} className="grid grid-cols-12 items-center gap-1">
            <input type="date" value={l.expense_on} onChange={(e) => setLine(i, { expense_on: e.target.value })}
              className="col-span-2 rounded border border-slate-300 px-1.5 py-1 text-xs" />
            <select value={l.account_code} onChange={(e) => setLine(i, { account_code: e.target.value })}
              className="col-span-2 rounded border border-slate-300 px-1 py-1 text-xs">
              {ACCOUNTS.map(([code, name]) => <option key={code} value={code}>{name}</option>)}
            </select>
            <input value={l.description} onChange={(e) => setLine(i, { description: e.target.value })}
              placeholder="내역" className="col-span-3 rounded border border-slate-300 px-1.5 py-1 text-xs" />
            {cardId != null ? (
              <select value={l.card_tx_id ?? ""} onChange={(e) => pickTx(i, e.target.value ? Number(e.target.value) : null)}
                className="col-span-3 rounded border border-slate-300 px-1 py-1 text-xs">
                <option value="">카드내역 선택(개인분)</option>
                {(unmatched.data?.items ?? [])
                  .filter((t: CardTx) => t.id === l.card_tx_id || !usedTx.has(t.id))
                  .map((t: CardTx) =>
                    <option key={t.id} value={t.id}>{t.tx_on} {t.merchant ?? t.tx_id} {won(t.amount)}</option>)}
              </select>
            ) : (
              <input value={l.merchant ?? ""} onChange={(e) => setLine(i, { merchant: e.target.value })}
                placeholder="가맹점" className="col-span-3 rounded border border-slate-300 px-1.5 py-1 text-xs" />
            )}
            <input inputMode="numeric" value={l.amount || ""} disabled={l.card_tx_id != null}
              onChange={(e) => setLine(i, { amount: Number(e.target.value.replace(/[^0-9]/g, "")) })}
              placeholder="금액" className="col-span-1 rounded border border-slate-300 px-1 py-1 text-right text-xs disabled:bg-slate-100" />
            <button onClick={() => rmLine(i)} disabled={lines.length === 1}
              className="col-span-1 text-xs text-red-500 disabled:text-slate-300">✕</button>
          </div>
        ))}
      </div>
      <p className="text-right text-sm">합계 <b>{won(total)}</b></p>
      <div className="flex gap-2">
        <Button disabled={isLoading || !valid} onClick={() => onSubmit({
          title, claim_on: claimOn,
          lines: lines.map((l) => ({ expense_on: l.expense_on, account_code: l.account_code,
            description: l.description, amount: Number(l.amount), merchant: l.merchant || null,
            card_tx_id: l.card_tx_id ?? null })) })}>
          기안 생성</Button>
        <Button variant="secondary" onClick={onCancel}>닫기</Button>
      </div>
    </div>
  );
}

function Cards({ qc, toast, fail }: any) {
  const [txCard, setTxCard] = useState<Cardt | null>(null);
  const [settleCard, setSettleCard] = useState<Cardt | null>(null);
  const cards = useQuery({ queryKey: ["cards"], queryFn: () =>
    api<{ items: Cardt[] }>("/finance/corporate-cards") });
  const txns = useQuery({ enabled: txCard != null, queryKey: ["card-tx", txCard?.id], queryFn: () =>
    api<{ items: CardTx[] }>(`/finance/corporate-cards/${txCard!.id}/transactions`) });
  const settle = useMutation({
    mutationFn: (v: { id: number; yyyymm: string; paid_on: string }) =>
      api<{ settled?: number; amount?: number }>(`/finance/corporate-cards/${v.id}/settle`,
        { method: "POST", body: JSON.stringify({ yyyymm: v.yyyymm, paid_on: v.paid_on }) }),
    onSuccess: (r) => {   // v1.203 — 대상 0건인데 "정산 완료"로 오인하던 결함
      if (!r.settled) toast.err("정산 대상 없음 — 해당 월에 승인 청구에 귀속된 카드 내역이 없습니다");
      else toast.ok(`카드 월정산 완료 — ${won(r.settled)} (DR 2112/CR 1110)`);
      setSettleCard(null);
      qc.invalidateQueries({ queryKey: ["card-tx"] }); },
    onError: fail,
  });

  return (
    <>
      <p className="text-xs text-slate-500">
        법인카드는 마지막 4자리만 저장(PCI 최소화). 사용내역은 승인번호(tx_id) 멱등 적재, 승인된 경비
        청구에 귀속된 내역을 카드사 월정산으로 미지급(2112) 상계.</p>
      <Card title="법인카드">
        <Table headers={["ID", "별칭", "카드번호", "상태", ""]}
          rows={(cards.data?.items ?? []).map((c: Cardt) => [
            `#${c.id}`, c.alias, `****-${c.card_last4}`,
            c.is_active ? <Badge key="a" tone="green">활성</Badge> : <Badge key="a" tone="gray">비활성</Badge>,
            <span key="e" className="flex gap-1">
              <Button variant="secondary" size="sm" onClick={() => setTxCard(c)}>사용내역</Button>
              <Button variant="secondary" size="sm" onClick={() => setSettleCard(c)}>월정산</Button>
            </span>,
          ])} empty={cards.isFetching ? "불러오는 중…" : "등록된 법인카드 없음(ADMIN 등록)"} />
      </Card>

      {txCard && (
        <Modal title={`${txCard.alias} 사용내역`} size="lg" onClose={() => setTxCard(null)}>
          <Table headers={["승인번호", "사용일", "가맹점", "금액", "상태", "청구", "정산일"]}
            rows={(txns.data?.items ?? []).map((t: CardTx) => [
              t.tx_id, t.tx_on, t.merchant ?? "—", won(t.amount),
              <Badge key="s" tone={t.status === "SETTLED" ? "blue" : t.claim_id ? "green" : "gray"}>{t.status}</Badge>,
              t.claim_id ? `#${t.claim_id}` : "—", t.settled_on ?? "—",
            ])} empty={txns.isFetching ? "불러오는 중…" : "사용내역 없음"} />
        </Modal>
      )}
      {settleCard && (
        <Modal title={`${settleCard.alias} 월정산`} onClose={() => setSettleCard(null)}>
          <SettleForm isLoading={settle.isPending}
            onSubmit={(v) => settle.mutate({ id: settleCard.id, ...v })} />
        </Modal>
      )}
    </>
  );
}

function SettleForm({ onSubmit, isLoading }: {
  onSubmit: (v: { yyyymm: string; paid_on: string }) => void; isLoading: boolean;
}) {
  const [yyyymm, setYyyymm] = useState(todayLocal().slice(0, 7));
  const [paidOn, setPaidOn] = useState(todayLocal());
  return (
    <div className="space-y-2">
      <Input label="정산 월 (YYYY-MM)" value={yyyymm}
        onChange={(e) => setYyyymm(e.target.value)} placeholder="2026-07" />
      <Input label="결제일" type="date" value={paidOn} onChange={(e) => setPaidOn(e.target.value)} />
      <p className="text-xs text-slate-500">승인된 경비 청구에 귀속된 카드 사용분 합계를 DR 2112/CR 1110로 정산합니다.</p>
      <Button disabled={isLoading || !/^\d{4}-\d{2}$/.test(yyyymm)}
        onClick={() => onSubmit({ yyyymm, paid_on: paidOn })}>월정산</Button>
    </div>
  );
}

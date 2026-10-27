"use client";
/** 원무 — 수납·일마감(FR-18)·마감 후 정정(FR-54). */
import { todayLocal } from "@/lib/dates";
import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import Link from "next/link";
import { ApiError, api, auth } from "@/lib/api";
import { Badge, Button, Card, Input, Table } from "@/components/ui";
import { useConfirm } from "@/components/confirm";
import { useUrlState } from "@/lib/use-url-state";
import { won } from "@/lib/format";

/** "voucher-42" → /finance?voucher=42 링크(X2-2·마감→GL 전표). */
function voucherLink(ref: string | null) {
  const m = ref?.match(/^voucher-(\d+)$/);
  return m ? <Link href={`/finance?voucher=${m[1]}`}
    className="text-blue-600 hover:underline">{ref} ↗</Link> : (ref ?? "—");
}

interface ReceptionRow { id: number; chart_no: string; name: string; dept_code: string; paid: boolean }
interface Preview { closing_on: string; total: number; count: number; already_closed: boolean;
  self_closing: boolean; by_method: { method: string; amount: number; count: number }[];
  refund_total: number; refund_count: number; net_total: number; net_cash: number }
interface ClosingRow { id: number; closing_on: string; seq: number; total_amount: number;
  payment_count: number; status: string; voucher_ref: string | null; closed_at: string | null;
  refund_total: number; refund_count: number; closed_by_name: string | null }
interface TellerRow { teller_id: number | null; teller: string; total: number; count: number;
  cash: number; card: number; refund: number; refund_count: number; net_total: number; net_cash: number }
interface Tellers { closing_on: string; tellers: TellerRow[]; total: number; net_cash: number }
interface PaymentRow { id: number; amount: number; method: string; chart_no: string; name: string }
interface Correction { cancel: boolean; newAmount: string }
const METHOD_KO: Record<string, string> = { CARD: "카드", CASH: "현금", TRANSFER: "이체" };

const today = () => todayLocal();

export default function ClosingPage() {
  const qc = useQueryClient();
  const ask = useConfirm();
  const [date, setDate] = useUrlState("date", today());        // 공유·새로고침 유지(X2-1)
  const [month, setMonth] = useUrlState("month", today().slice(0, 7));
  const [message, setMessage] = useState<string | null>(null);
  const [amendReason, setAmendReason] = useState("");
  const [corrections, setCorrections] = useState<Record<number, Correction>>({});
  const isAdmin = auth.claims()?.role === "ADMIN";

  const receptions = useQuery({
    queryKey: ["receptions", date],
    queryFn: () => api<{ items: ReceptionRow[] }>(`/patients/receptions?on=${date}`),
  });

  const preview = useQuery({
    queryKey: ["closing-preview", date],
    queryFn: () => api<Preview>(`/patients/closings/preview?on=${date}`),
  });

  const history = useQuery({
    queryKey: ["closings", month],
    queryFn: () => api<{ items: ClosingRow[] }>(`/patients/closings?yyyymm=${month}`),
  });

  // 수납원별 시재 — 서버는 제공하는데 화면이 소비하지 않던 값. 현금 과부족 시 책임 창구 특정용.
  const tellers = useQuery({
    queryKey: ["closing-tellers", date],
    queryFn: () => api<Tellers>(`/patients/closings/by-teller?on=${date}`),
  });

  // 그날 정상 수납 목록 — 마감 정정(취소·정정) 대상 선택용. 마감된 일자에서만 의미(ADMIN 전용).
  const payments = useQuery({
    queryKey: ["day-payments", date],
    queryFn: () => api<{ items: PaymentRow[] }>(`/patients/payments?on=${date}`),
    enabled: isAdmin && (preview.data?.already_closed ?? false),
  });

  const refresh = () => {
    qc.invalidateQueries({ queryKey: ["receptions"] });
    qc.invalidateQueries({ queryKey: ["closing-preview"] });
    qc.invalidateQueries({ queryKey: ["closings"] });
    qc.invalidateQueries({ queryKey: ["day-payments"] });
  };

  const pay = useMutation({
    mutationFn: ({ reception_id, amount, method }:
                 { reception_id: number; amount: number; method: string }) =>
      api("/patients/payments", { method: "POST",
        body: JSON.stringify({ reception_id, amount, method, paid_on: date }) }),
    onSuccess: () => { setMessage("수납 완료"); refresh(); },
    onError: (e) => setMessage(e instanceof ApiError ? `${e.error.code}: ${e.message}` : "수납 실패"),
  });

  const close = useMutation<{ seq: number; total: number; voucher_ref: string | null }, Error, boolean>({
    mutationFn: (force: boolean) => api<{ seq: number; total: number; voucher_ref: string | null }>(
      `/patients/closings/${date}${force ? "?force=true" : ""}`, { method: "POST" }),
    onSuccess: (d) => {
      setMessage(`일마감 완료 (#${d.seq}) — 합계 ${won(d.total)}, 전표 ${d.voucher_ref ?? "없음"}`);
      refresh();
    },
    onError: (e) => setMessage(e instanceof ApiError ? e.message : "마감 실패"),
  });

  // 정정 라인 구성 — 취소 체크된 수납만: {cancel_payment_id, new_amount?}(정정 금액 입력 시 대체 수납)
  const correctionPayments = () =>
    (payments.data?.items ?? [])
      .filter((p) => corrections[p.id]?.cancel)
      .map((p) => {
        const na = corrections[p.id]?.newAmount;
        return na ? { cancel_payment_id: p.id, new_amount: Number(na) }
                  : { cancel_payment_id: p.id };
      });

  const amend = useMutation({
    mutationFn: () => api<{ seq: number; total: number }>(
      `/patients/closings/${date}/amend`, {
        method: "POST",
        body: JSON.stringify({ reason: amendReason,
                               correction_payments: correctionPayments() }) }),
    onSuccess: (d) => {
      setMessage(`재마감 완료 (#${d.seq}) — 원 마감 보존·역분개 처리, 합계 ${won(d.total)}`);
      setCorrections({}); setAmendReason("");
      refresh();
    },
    onError: (e) => setMessage(e instanceof ApiError ? e.message : "정정 실패"),
  });

  return (
    <div className="space-y-4">
      <div className="flex items-center gap-3">
        <h1 className="text-lg font-bold">원무 — 수납·일마감</h1>
        <input type="date" value={date} onChange={(e) => setDate(e.target.value)}
          className="rounded-md border border-slate-300 px-2 py-1 text-sm" />
        <Button onClick={async () => {
            if (preview.data?.self_closing && !(await ask({
                title: "직무분리 경고 — 강제 마감",
                body: "본인이 입력한 수납만 있습니다. 강제 마감하시겠습니까? (감사 기록됨)",
                danger: true, confirmLabel: "강제 마감" })))
              return;
            close.mutate(preview.data?.self_closing ?? false);
          }}
          disabled={close.isPending || preview.data?.already_closed || (preview.data?.count ?? 0) === 0}>
          {preview.data?.already_closed ? "마감 완료됨"
            : preview.data?.self_closing ? "강제 일마감" : "일마감 (전표 자동)"}</Button>
      </div>
      {message && <p className="text-sm text-blue-600">{message}</p>}

      <Card title={`일계표 미리보기 — ${date}`}>
        {preview.isLoading ? <p className="text-sm text-slate-400">집계 중…</p> : (
          <div className="space-y-2">
            <div className="flex flex-wrap items-center gap-4 text-sm">
              <span>수납 <b className="text-base">{won(preview.data?.total)}</b></span>
              <span>건수 {preview.data?.count ?? 0}</span>
              {/* 환불은 자기 전표(refund-N)로 전기되고 마감 전표엔 미포함 — 병기해야 합계 오해가 없다 */}
              <span className="text-rose-600">
                환불 {preview.data?.refund_count ?? 0}건 −{won(preview.data?.refund_total)}</span>
              <span className="text-slate-500">순수납 {won(preview.data?.net_total)}</span>
              {preview.data?.already_closed
                ? <Badge tone="green">마감됨</Badge> : <Badge tone="amber">미마감</Badge>}
              {preview.data?.self_closing && !preview.data?.already_closed
                && <Badge tone="red">직무분리 경고 — 본인 단독 입력</Badge>}
            </div>
            {/* 물리 시재와 맞춰야 하는 유일한 값 — 카드 포함 합계로 대사하면 항상 어긋난다 */}
            <div className="rounded-md border border-blue-200 bg-blue-50 px-3 py-2 text-sm">
              현금 시재 대사액 <b className="text-base text-blue-800">{won(preview.data?.net_cash)}</b>
              <span className="ml-2 text-xs text-slate-500">현금성 수납 − 현금성 환불 (카드 제외)</span>
            </div>
            <Table headers={["수단", "금액", "건수"]}
              rows={(preview.data?.by_method ?? []).map((m) => [
                METHOD_KO[m.method] ?? m.method, won(m.amount), m.count])} />
          </div>
        )}
      </Card>

      {/* 수납원별 시재 — 서버(/closings/by-teller)는 제공하는데 소비처가 없어 현금 과부족 시
          어느 창구인지 특정할 수 없었다. 권한은 일계표와 동일(patient.closing:read). */}
      <Card title={`수납원별 시재 — ${date}`}>
        {tellers.isLoading ? <p className="text-sm text-slate-400">집계 중…</p> : (
          <Table headers={["수납원", "건수", "수납", "현금", "카드", "환불", "순수납", "현금 시재"]}
            rows={(tellers.data?.tellers ?? []).map((t) => [
              t.teller, t.count, won(t.total), won(t.cash), won(t.card),
              t.refund_count ? `${t.refund_count}건 −${won(t.refund)}` : "—",
              won(t.net_total), won(t.net_cash),
            ])} empty="해당 일자 수납 없음" />
        )}
      </Card>

      <Card title="접수 목록 — 수납 처리">
        <Table headers={["접수#", "차트", "이름", "진료과", "수납", ""]}
          rows={(receptions.data?.items ?? []).map((r) => [
            r.id, r.chart_no, r.name, r.dept_code,
            r.paid ? <Badge key="p" tone="green">수납됨</Badge> : <Badge key="p" tone="amber">미수납</Badge>,
            !r.paid && <PayCell key="c" pending={pay.isPending}
              onPay={(amt, method) => pay.mutate({ reception_id: r.id, amount: amt, method })} />,
          ])} />
      </Card>

      <Card title="마감 이력">
        <div className="mb-2 flex items-center gap-2">
          <span className="text-sm text-slate-500">조회 월</span>
          <input type="month" value={month} onChange={(e) => setMonth(e.target.value)}
            className="rounded-md border border-slate-300 px-2 py-1 text-sm" />
        </div>
        <Table headers={["일자", "차수", "합계", "건수", "환불", "상태", "전표", "마감자", "마감시각"]}
          rows={(history.data?.items ?? []).map((c) => [
            c.closing_on, `#${c.seq}`, won(c.total_amount), c.payment_count,
            c.refund_count ? `${c.refund_count}건 −${won(c.refund_total)}` : "—",
            c.status === "CLOSED" ? <Badge key="s" tone="green">마감</Badge>
              : <Badge key="s" tone="gray">정정됨</Badge>,
            voucherLink(c.voucher_ref),
            c.closed_by_name ?? "—",
            c.closed_at ? c.closed_at.slice(0, 16).replace("T", " ") : "—",
          ])} />
      </Card>

      {isAdmin && (
        <Card title="마감 후 정정 (FR-54 — 관리자 전용: 역분개 + 재마감, 원 마감 보존)">
          {!preview.data?.already_closed ? (
            <p className="text-sm text-slate-500">마감된 일자에서만 정정할 수 있습니다.</p>
          ) : (
            <div className="space-y-3">
              <p className="text-xs text-slate-500">
                취소할 수납을 선택하세요. 정정 금액을 입력하면 취소 후 해당 금액으로 대체 수납됩니다
                (빈칸이면 취소만). 선택 없이 실행하면 역분개 후 현재 상태로 재마감만 합니다.
              </p>
              <Table headers={["취소", "차트", "이름", "수단", "원 금액", "정정 금액"]}
                rows={(payments.data?.items ?? []).map((p) => {
                  const c = corrections[p.id] ?? { cancel: false, newAmount: "" };
                  const set = (patch: Partial<Correction>) =>
                    setCorrections((prev) => ({ ...prev, [p.id]: { ...c, ...patch } }));
                  return [
                    <input key="ck" type="checkbox" checked={c.cancel}
                      onChange={(e) => set({ cancel: e.target.checked })} />,
                    p.chart_no, p.name, METHOD_KO[p.method] ?? p.method,
                    p.amount.toLocaleString(),
                    <input key="na" value={c.newAmount} disabled={!c.cancel} placeholder="(취소만)"
                      onChange={(e) => set({ newAmount: e.target.value })}
                      className="w-24 rounded border border-slate-300 px-1 py-0.5 text-xs disabled:bg-slate-100" />,
                  ];
                })} />
              <div className="flex items-end gap-2">
                <Input label="정정 사유 (필수)" value={amendReason}
                  onChange={(e) => setAmendReason(e.target.value)} className="min-w-64" />
                <Button variant="danger" disabled={!amendReason || amend.isPending}
                  onClick={() => amend.mutate()}>재마감 실행</Button>
              </div>
              <p className="text-xs text-slate-400">
                마감된 일자의 수납은 차단됩니다 — 정정은 이 경로만 허용 (감사 기록)
              </p>
            </div>
          )}
        </Card>
      )}
    </div>
  );
}

function PayCell({ onPay, pending }: { onPay: (amount: number, method: string) => void; pending?: boolean }) {
  const [amt, setAmt] = useState("");
  const [method, setMethod] = useState("CARD");   // 하드코딩 제거 — 현금/이체 수납 가능(일계표 수단별 정합)
  return (
    <span className="flex items-center gap-1">
      <select value={method} onChange={(e) => setMethod(e.target.value)}
        className="rounded border border-slate-300 px-1 py-0.5 text-xs">
        <option value="CARD">카드</option>
        <option value="CASH">현금</option>
        <option value="TRANSFER">이체</option>
      </select>
      <input value={amt} onChange={(e) => setAmt(e.target.value)} placeholder="금액"
        className="w-20 rounded border border-slate-300 px-1 py-0.5 text-xs" />
      <Button variant="secondary" disabled={!amt || pending}
        onClick={() => amt && onPay(Number(amt), method)}>수납</Button>
    </span>
  );
}

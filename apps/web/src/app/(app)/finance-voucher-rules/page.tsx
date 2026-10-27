"use client";
/** 재무 — 검진권 전기규칙 관리. lineType[+딜러유형]별 차·대 계정·반전·무전표를 관리자가 설정.
 *  수수료 방향(비용/수익)·정산(AP/AR) 등 기관·계약별로 달라 코드 아닌 규칙으로 운영. */
import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { ApiError, api } from "@/lib/api";
import { Badge, Button, Card, Input, Modal, Table } from "@/components/ui";
import { useToast } from "@/components/toast";

interface Rule {
  id: number;
  line_type: string;
  dealer_type: string | null;
  debit_account: string | null;
  credit_account: string | null;
  amount_source: string | null;
  reverse_on_negative: boolean;
  skip: boolean;
  active: boolean;
  priority: number;
  description: string | null;
}
type Draft = Omit<Rule, "id">;

const LINE_TYPES = ["purchase", "sub_purchase", "resale", "redeem", "topup",
  "refund", "commission", "settlement"];
const DEALER_TYPES = ["", "DOMESTIC", "OVERSEAS"];        // ""=전체
const BLANK: Draft = { line_type: "commission", dealer_type: null, debit_account: "",
  credit_account: "", amount_source: "", reverse_on_negative: false, skip: false,
  active: true, priority: 100, description: "" };

export default function VoucherRulesPage() {
  const qc = useQueryClient();
  const toast = useToast();
  const [edit, setEdit] = useState<Rule | "new" | null>(null);
  const fail = (e: unknown) => toast.err(e instanceof ApiError ? e.error.message : "처리 실패");

  const rules = useQuery({
    queryKey: ["voucher-rules"],
    queryFn: () => api<{ items: Rule[] }>("/finance/voucher/posting-rules"),
  });
  const refresh = () => qc.invalidateQueries({ queryKey: ["voucher-rules"] });

  const save = useMutation({
    mutationFn: (r: Draft & { id?: number }) =>
      r.id
        ? api(`/finance/voucher/posting-rules/${r.id}`, { method: "PUT", body: JSON.stringify(r) })
        : api("/finance/voucher/posting-rules", { method: "POST", body: JSON.stringify(r) }),
    onSuccess: () => { toast.ok("규칙 저장 완료 — 이후 수신분부터 적용"); setEdit(null); refresh(); },
    onError: fail,
  });

  return (
    <div className="space-y-4">
      <div className="flex items-center justify-between">
        <h1 className="text-lg font-bold">재무 — 검진권 전기규칙</h1>
        <Button onClick={() => setEdit("new")}>규칙 추가</Button>
      </div>
      <p className="text-xs text-slate-500">
        검진권 거래/정산의 차·대 계정을 lineType별로 설정합니다. 수수료 방향(비용 vs 수익)·정산
        AP/AR 등 기관·계약별 차이를 여기서 조정(ADMIN). 무전표=skip, clawback=음수 반전.</p>
      {rules.isError && <p className="text-sm text-red-600">규칙 조회 권한이 없습니다 (DIRECTOR/ADMIN)</p>}

      <Card title={`전기규칙 (${rules.data?.items.length ?? 0}개)`}>
        <Table headers={["lineType", "딜러유형", "차변", "대변", "금액필드", "옵션", "활성", ""]}
          rows={(rules.data?.items ?? []).map((r) => [
            <span key="l" className="font-medium">{r.line_type}</span>,
            r.dealer_type ?? <span key="d" className="text-slate-400">전체</span>,
            r.skip ? "—" : (r.debit_account ?? "—"),
            r.skip ? "—" : (r.credit_account ?? "—"),
            <code key="a" className="text-xs text-slate-500">{r.amount_source ?? "—"}</code>,
            <span key="o" className="text-xs">
              {r.skip && <Badge tone="gray">무전표</Badge>}
              {r.reverse_on_negative && <Badge tone="amber">음수반전</Badge>}</span>,
            r.active ? <Badge key="ac" tone="green">활성</Badge>
              : <Badge key="ac" tone="gray">비활성</Badge>,
            <Button key="e" variant="secondary" onClick={() => setEdit(r)}>편집</Button>,
          ])} empty="규칙 없음" />
      </Card>

      {edit && (
        <Modal title={edit === "new" ? "전기규칙 추가" : `전기규칙 편집 #${edit.id}`}
          onClose={() => setEdit(null)}>
          <RuleForm initial={edit === "new" ? BLANK : edit}
            isLoading={save.isPending}
            onSubmit={(d) => save.mutate(edit === "new" ? d : { ...d, id: edit.id })} />
        </Modal>
      )}
    </div>
  );
}

function RuleForm({ initial, onSubmit, isLoading }: {
  initial: Draft; onSubmit: (d: Draft) => void; isLoading: boolean;
}) {
  const [f, setF] = useState<Draft>(initial);
  const set = (p: Partial<Draft>) => setF({ ...f, ...p });
  return (
    <div className="space-y-2">
      <label className="block text-sm text-slate-600">lineType
        <select value={f.line_type} onChange={(e) => set({ line_type: e.target.value })}
          className="w-full rounded-md border border-slate-300 px-2 py-1.5 text-sm">
          {LINE_TYPES.map((t) => <option key={t} value={t}>{t}</option>)}
        </select>
      </label>
      <label className="block text-sm text-slate-600">딜러유형 (전체=빈값)
        <select value={f.dealer_type ?? ""}
          onChange={(e) => set({ dealer_type: e.target.value || null })}
          className="w-full rounded-md border border-slate-300 px-2 py-1.5 text-sm">
          {DEALER_TYPES.map((t) => <option key={t} value={t}>{t || "전체"}</option>)}
        </select>
      </label>
      <div className="grid grid-cols-2 gap-2">
        <Input label="차변 계정" value={f.debit_account ?? ""}
          onChange={(e) => set({ debit_account: e.target.value || null })} placeholder="2110" />
        <Input label="대변 계정" value={f.credit_account ?? ""}
          onChange={(e) => set({ credit_account: e.target.value || null })} placeholder="1110" />
      </div>
      <Input label="금액필드 (payload, 콤마 우선순위)" value={f.amount_source ?? ""}
        onChange={(e) => set({ amount_source: e.target.value || null })}
        placeholder="netPayableKrw,netPayable" />
      <Input label="우선순위 (낮을수록 우선)" type="number" value={f.priority}
        onChange={(e) => set({ priority: Number(e.target.value) })} />
      <Input label="설명" value={f.description ?? ""}
        onChange={(e) => set({ description: e.target.value || null })} />
      <div className="flex flex-wrap gap-4 pt-1">
        <label className="flex items-center gap-1.5 text-sm text-slate-600">
          <input type="checkbox" checked={f.skip}
            onChange={(e) => set({ skip: e.target.checked })} /> 무전표(skip)</label>
        <label className="flex items-center gap-1.5 text-sm text-slate-600">
          <input type="checkbox" checked={f.reverse_on_negative}
            onChange={(e) => set({ reverse_on_negative: e.target.checked })} /> 음수 차대반전</label>
        <label className="flex items-center gap-1.5 text-sm text-slate-600">
          <input type="checkbox" checked={f.active}
            onChange={(e) => set({ active: e.target.checked })} /> 활성</label>
      </div>
      <Button disabled={isLoading || (!f.skip && (!f.debit_account || !f.credit_account))}
        onClick={() => onSubmit(f)}>저장</Button>
    </div>
  );
}

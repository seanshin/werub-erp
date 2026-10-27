"use client";
/** 재무 — 원천징수 요율 (시점 버전관리). 세법 정기 개정 시 새 시행일 구간을 추가하면
 *  정산이 그 시점 요율로 재계산된다. HIS 동봉 withholdingTax가 있으면 그 값 우선. */
import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { ApiError, api } from "@/lib/api";
import { Badge, Button, Card, Input, Modal, Table } from "@/components/ui";
import { useToast } from "@/components/toast";

interface Rate {
  id: number;
  dealer_type: string | null;
  rate_pct: number;
  effective_from: string;
  effective_to: string | null;
  withholding_account: string;
  tax_invoice_by: string | null;
  active: boolean;
  note: string | null;
}
type Draft = Omit<Rate, "id">;

const DEALER_TYPES = ["", "DOMESTIC", "OVERSEAS"];
const BLANK: Draft = { dealer_type: "DOMESTIC", rate_pct: 3.3, effective_from: "",
  effective_to: null, withholding_account: "2150", tax_invoice_by: "", active: true, note: "" };

export default function WithholdingPage() {
  const qc = useQueryClient();
  const toast = useToast();
  const [edit, setEdit] = useState<Rate | "new" | null>(null);
  const fail = (e: unknown) => toast.err(e instanceof ApiError ? e.error.message : "처리 실패");

  const rates = useQuery({
    queryKey: ["withholding-rates"],
    queryFn: () => api<{ items: Rate[] }>("/finance/withholding-rates"),
  });
  const refresh = () => qc.invalidateQueries({ queryKey: ["withholding-rates"] });

  const save = useMutation({
    mutationFn: (r: Draft & { id?: number }) =>
      r.id
        ? api(`/finance/withholding-rates/${r.id}`, { method: "PUT", body: JSON.stringify(r) })
        : api("/finance/withholding-rates", { method: "POST", body: JSON.stringify(r) }),
    onSuccess: () => { toast.ok("요율 저장 완료 — 이후 정산부터 시점 요율 재계산"); setEdit(null); refresh(); },
    onError: fail,
  });

  return (
    <div className="space-y-4">
      <div className="flex items-center justify-between">
        <h1 className="text-lg font-bold">재무 — 원천징수 요율</h1>
        <Button onClick={() => setEdit("new")}>요율 추가</Button>
      </div>
      <p className="text-xs text-slate-500">
        검진권 딜러 정산 원천징수 요율을 시행일 구간으로 관리합니다(세법 개정 시 새 구간 추가 →
        정산이 시점 요율로 재계산). HIS가 withholdingTax를 동봉하면 그 값이 우선. 종료일 빈값=현행.</p>
      {rates.isError && <p className="text-sm text-red-600">요율 조회 권한이 없습니다 (DIRECTOR/ADMIN)</p>}

      <Card title={`요율 이력 (${rates.data?.items.length ?? 0}건)`}>
        <Table headers={["딜러유형", "요율%", "시행일", "종료일", "예수금계정", "세금계산서", "활성", ""]}
          rows={(rates.data?.items ?? []).map((r) => [
            r.dealer_type ?? <span key="d" className="text-slate-400">전체</span>,
            <span key="r" className="font-medium">{r.rate_pct}%</span>,
            r.effective_from,
            r.effective_to ?? <Badge key="t" tone="green">현행</Badge>,
            r.withholding_account, r.tax_invoice_by ?? "—",
            r.active ? <Badge key="a" tone="green">활성</Badge>
              : <Badge key="a" tone="gray">비활성</Badge>,
            <Button key="e" variant="secondary" onClick={() => setEdit(r)}>편집</Button>,
          ])} empty="요율 없음 — 추가 전까지 무원천징수(2-line) 유지" />
      </Card>

      {edit && (
        <Modal title={edit === "new" ? "원천징수 요율 추가" : `요율 편집 #${edit.id}`}
          onClose={() => setEdit(null)}>
          <RateForm initial={edit === "new" ? BLANK : edit}
            isLoading={save.isPending}
            onSubmit={(d) => save.mutate(edit === "new" ? d : { ...d, id: edit.id })} />
        </Modal>
      )}
    </div>
  );
}

function RateForm({ initial, onSubmit, isLoading }: {
  initial: Draft; onSubmit: (d: Draft) => void; isLoading: boolean;
}) {
  const [f, setF] = useState<Draft>(initial);
  const set = (p: Partial<Draft>) => setF({ ...f, ...p });
  return (
    <div className="space-y-2">
      <label className="block text-sm text-slate-600">딜러유형 (전체=빈값)
        <select value={f.dealer_type ?? ""}
          onChange={(e) => set({ dealer_type: e.target.value || null })}
          className="w-full rounded-md border border-slate-300 px-2 py-1.5 text-sm">
          {DEALER_TYPES.map((t) => <option key={t} value={t}>{t || "전체"}</option>)}
        </select>
      </label>
      <Input label="요율 (%)" type="number" step="0.001" value={f.rate_pct}
        onChange={(e) => set({ rate_pct: Number(e.target.value) })} />
      <div className="grid grid-cols-2 gap-2">
        <Input label="시행일" type="date" value={f.effective_from}
          onChange={(e) => set({ effective_from: e.target.value })} />
        <Input label="종료일 (현행=빈값)" type="date" value={f.effective_to ?? ""}
          onChange={(e) => set({ effective_to: e.target.value || null })} />
      </div>
      <div className="grid grid-cols-2 gap-2">
        <Input label="예수금 계정" value={f.withholding_account}
          onChange={(e) => set({ withholding_account: e.target.value })} placeholder="2150" />
        <Input label="세금계산서 주체" value={f.tax_invoice_by ?? ""}
          onChange={(e) => set({ tax_invoice_by: e.target.value || null })} placeholder="병원역발행" />
      </div>
      <Input label="비고" value={f.note ?? ""}
        onChange={(e) => set({ note: e.target.value || null })} />
      <label className="flex items-center gap-1.5 text-sm text-slate-600">
        <input type="checkbox" checked={f.active}
          onChange={(e) => set({ active: e.target.checked })} /> 활성</label>
      <Button disabled={isLoading || !f.effective_from || !f.rate_pct}
        onClick={() => onSubmit(f)}>저장</Button>
    </div>
  );
}

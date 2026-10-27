"use client";
/** 재고 워크스테이션(인앱) — 공유 컴포넌트 + 별도 전체화면 창 + 알바 일일 PIN 발급. */
import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { ApiError, api } from "@/lib/api";
import { dt } from "@/lib/format";
import { Badge, Button, Card, Input } from "@/components/ui";
import { QueryErrorBanner } from "@/components/query-guard";
import { useToast } from "@/components/toast";
import { InventoryWorkstation } from "@/components/inventory-workstation";

interface Pass { id: number; label: string | null; valid_until: string; created_at: string }

function DayPassPanel() {
  const qc = useQueryClient();
  const toast = useToast();
  const [label, setLabel] = useState("");
  const [issued, setIssued] = useState<{ pin: string; valid_until: string } | null>(null);
  const wsUrl = typeof window !== "undefined" ? `${window.location.origin}/erp/ws` : "/erp/ws";

  const list = useQuery({ queryKey: ["ws-passes"],
    queryFn: () => api<{ items: Pass[] }>("/inventory/workstation/passes") });
  const create = useMutation({
    mutationFn: () => api<{ id: number; pin: string; valid_until: string }>(
      "/inventory/workstation/passes", { method: "POST", body: JSON.stringify({ label: label || null }) }),
    onSuccess: (d) => { setIssued({ pin: d.pin, valid_until: d.valid_until }); setLabel("");
      qc.invalidateQueries({ queryKey: ["ws-passes"] }); },
    onError: (e) => toast.err(e instanceof ApiError ? e.message : "발급 실패(권한 확인)"),
  });
  const revoke = useMutation({
    mutationFn: (id: number) => api(`/inventory/workstation/passes/${id}/revoke`, { method: "POST" }),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["ws-passes"] }),
  });
  const copyDeliver = () => {
    if (!issued) return;
    const text = `[재고 워크스테이션 접속]\n링크: ${wsUrl}\nPIN: ${issued.pin}\n` +
      `유효: ${dt(issued.valid_until)}`;
    navigator.clipboard?.writeText(text); toast.ok("링크+PIN 복사됨 — 알바에게 전달하세요");
  };

  return (
    <Card title="알바 일일 PIN 발급" action={<span className="text-[11px] text-slate-400">당일 23:59 만료 · 재고 작업만</span>}>
      <div className="flex flex-wrap items-end gap-2">
        <Input label="용도/담당(선택)" value={label} placeholder="예: 김알바 오전"
          onChange={(e) => setLabel(e.target.value)} />
        <Button isLoading={create.isPending} onClick={() => create.mutate()}>PIN 발급</Button>
      </div>
      {issued && (
        <div className="mt-3 rounded-xl border border-blue-100 bg-blue-50/50 p-4">
          <div className="flex flex-wrap items-center gap-x-6 gap-y-1">
            <span className="text-sm text-slate-500">PIN</span>
            <span className="text-2xl font-bold tracking-[0.3em] text-blue-700">{issued.pin}</span>
            <span className="text-xs text-slate-400">유효 {dt(issued.valid_until)}</span>
            <button onClick={copyDeliver}
              className="ml-auto rounded bg-blue-600 px-3 py-1.5 text-xs font-medium text-white">링크+PIN 복사</button>
          </div>
          <p className="mt-2 break-all text-[12px] text-slate-500">링크: {wsUrl}</p>
          <p className="mt-1 text-[11px] text-amber-600">⚠️ PIN은 지금만 표시됩니다 — 전달 후 화면을 닫으세요.</p>
        </div>
      )}
      <div className="mt-3"><QueryErrorBanner queries={[list]} /></div>
      {(list.data?.items ?? []).length > 0 && (
        <div className="mt-3">
          <p className="mb-1 text-[11px] font-semibold uppercase text-slate-400">유효 출입증</p>
          {(list.data?.items ?? []).map((p) => (
            <div key={p.id} className="flex items-center justify-between border-b border-slate-100 py-1.5 text-sm">
              <span>{p.label ?? `출입증 #${p.id}`} <Badge tone="green">유효</Badge>
                <span className="ml-2 text-[11px] text-slate-400">~ {dt(p.valid_until)}</span></span>
              <button onClick={() => revoke.mutate(p.id)} className="text-xs text-red-500 hover:underline">폐기</button>
            </div>
          ))}
        </div>
      )}
    </Card>
  );
}

export default function WorkstationPage() {
  return (
    <div className="space-y-4">
      <div className="flex justify-end">
        <a href="/erp/ws" target="_blank" rel="noopener noreferrer"
          className="text-xs font-medium text-blue-600 hover:underline">전체화면(별도 창)으로 열기 ↗</a>
      </div>
      <DayPassPanel />
      <InventoryWorkstation />
    </div>
  );
}

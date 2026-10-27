"use client";
/** 인사 — 4대보험 취득·상실 신고 추적 (P1-③): 사유발생 자동적재·기한·처리. */
import { todayLocal } from "@/lib/dates";
import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { ApiError, api } from "@/lib/api";
import { Card, Table } from "@/components/ui";
import { useToast } from "@/components/toast";
import { useInputDialog } from "@/components/input-dialog";

interface Item { id: number; employee_no: string; name: string; event: string;
  basis_on: string; due_on: string; status: string; overdue: boolean;
  reported_on: string | null; note: string | null }

const EVENT: Record<string, string> = {
  ACQUIRE: "취득(입사)", LOSE: "상실(퇴사)", EXEMPT: "납부예외(휴직)", RESUME: "재개(복직)",
};

export default function InsuranceReportsPage() {
  const qc = useQueryClient();
  const { ask, dialog } = useInputDialog();
  const [tab, setTab] = useState<"PENDING" | "REPORTED">("PENDING");
  const toast = useToast();

  const list = useQuery({ queryKey: ["insrep", tab], queryFn: () =>
    api<{ count: number; items: Item[] }>(`/hr/insurance-reports?status=${tab}`) });

  const mark = useMutation({
    mutationFn: (p: { id: number; on: string }) =>
      api(`/hr/insurance-reports/${p.id}/reported?reported_on=${p.on}`, { method: "POST" }),
    onSuccess: () => { toast.ok("신고 처리 완료"); qc.invalidateQueries({ queryKey: ["insrep"] }); },
    onError: (e) => toast.err(e instanceof ApiError ? e.error.message : "처리 실패"),
  });

  return (
    <div className="space-y-4">
      {dialog}
      <h1 className="text-lg font-bold">인사 — 4대보험 취득·상실 신고</h1>
      <p className="text-sm text-slate-500">
        입사·퇴사·휴직·복직 시 자동 적재됩니다. 기한: 국민·건보 14일 이내, 고용·산재 다음달 15일.
      </p>

      <div className="flex gap-2">
        {(["PENDING", "REPORTED"] as const).map((t) => (
          <button key={t} onClick={() => setTab(t)}
            className={`rounded-md px-3 py-1.5 text-sm font-medium ${tab === t
              ? "bg-blue-600 text-white" : "bg-slate-100 text-slate-600 hover:bg-slate-200"}`}>
            {t === "PENDING" ? "신고 대기" : "처리 완료"}
          </button>
        ))}
      </div>

      <Card title={`${tab === "PENDING" ? "신고 대기" : "처리 완료"} (${list.data?.count ?? 0}건)`}>
        <Table headers={["사번", "이름", "이벤트", "사유발생일", "신고기한", "상태", "비고", ""]}
          empty="해당 상태의 신고 이벤트가 없습니다"
          rows={(list.data?.items ?? []).map((i) => [
            i.employee_no, i.name, EVENT[i.event] ?? i.event, i.basis_on,
            i.overdue ? <span key="d" className="font-semibold text-rose-600">{i.due_on} (기한경과)</span> : i.due_on,
            i.status === "REPORTED" ? `완료 (${i.reported_on})` : "대기",
            <span key="n" className="text-xs text-slate-400">{i.note ?? "-"}</span>,
            i.status === "PENDING" ?
              <button key="b" className="rounded-md bg-blue-600 px-2.5 py-1 text-xs font-medium text-white hover:bg-blue-700"
                onClick={async () => { const on = await ask({ title: "신고 처리", label: "신고일",
                  kind: "date", defaultValue: todayLocal(), confirmLabel: "처리" });
                  if (on) mark.mutate({ id: i.id, on: String(on) }); }}>처리</button> : null,
          ])} />
      </Card>
    </div>
  );
}

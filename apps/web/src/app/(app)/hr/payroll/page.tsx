"use client";
/** 급여 — 본인 명세 조회(FR-07b)·관리자 입력/확정(FR-07a). */
import { useState } from "react";
import { useMutation, useQuery } from "@tanstack/react-query";
import { ApiError, api, apiDownload, auth } from "@/lib/api";
import { Badge, Button, Card, Input, Table } from "@/components/ui";
import { QueryError } from "@/components/query-guard";
import { EmployeePicker } from "@/components/employee-picker";
import { useToast } from "@/components/toast";
import { won } from "@/lib/format";

// 서버가 카탈로그 한글명·계산방법·과세구분을 이미 붙여 보내는데 타입에 없어 원시코드가 노출됐다.
// 항목명·지급일·계산방법은 근기법 §27의2 법정 기재사항이다.
interface Line { item_code: string; item_kind: string; item_name: string; amount: number;
  taxable: boolean; formula: string | null }

export default function PayrollPage() {
  const now = new Date();
  const [yyyymm, setYyyymm] = useState(
    `${now.getFullYear()}-${String(now.getMonth() + 1).padStart(2, "0")}`);
  const toast = useToast();
  const isAdmin = auth.claims()?.role === "ADMIN";

  const mine = useQuery({
    queryKey: ["payroll-me", yyyymm],
    queryFn: () => api<{ confirmed: boolean; paid_on: string | null; lines: Line[] }>(
      `/hr/payrolls/me/${yyyymm}`),
    retry: 0,
  });
  const earn = (mine.data?.lines ?? []).filter((l) => l.item_kind === "EARNING")
    .reduce((s, l) => s + l.amount, 0);
  const deduct = (mine.data?.lines ?? []).filter((l) => l.item_kind === "DEDUCTION")
    .reduce((s, l) => s + l.amount, 0);

  // 관리자 입력 (간이 — 직원 선택 + 항목 3개)
  const [empId, setEmpId] = useState<number | null>(null);
  const [base, setBase] = useState("");
  const [tax, setTax] = useState("");
  const upsert = useMutation({
    mutationFn: () => api(`/hr/payrolls/${yyyymm}`, {
      method: "PUT",
      body: JSON.stringify({
        employee_id: empId,
        lines: [
          { item_code: "BASE", item_kind: "EARNING", amount: Number(base) },
          { item_code: "TAX", item_kind: "DEDUCTION", amount: Number(tax) },
        ],
      }),
    }),
    onSuccess: () => toast.ok("저장 완료 — 확정 전까지 수정 가능"),
    onError: (e) => toast.err(e instanceof ApiError ? e.message : "저장 실패"),
  });
  const confirmPay = useMutation({
    mutationFn: () => api(`/hr/payrolls/${yyyymm}/confirm?employee_id=${empId}`, { method: "POST" }),
    onSuccess: () => toast.ok("확정 완료 — 직원 조회 가능 (이후 수정은 감사 기록)"),
    onError: (e) => toast.err(e instanceof ApiError ? e.message : "확정 실패"),
  });

  return (
    <div className="space-y-4">
      <div className="flex items-center gap-3">
        <h1 className="text-lg font-bold">인사 — 급여</h1>
        {mine.isError && <QueryError error={mine.error} onRetry={() => mine.refetch()} />}
        <input type="month" value={yyyymm} onChange={(e) => setYyyymm(e.target.value)}
          className="rounded-md border border-slate-300 px-2 py-1 text-sm" />
      </div>

      <Card title={`내 급여명세 (${yyyymm}) — 조회는 접속기록에 남습니다`}
        action={mine.data?.confirmed
          // 명세서 교부(근기법 §48②) 수단인 PDF EP가 완비인데 화면에 진입점이 없었다
          ? <Button variant="secondary" size="sm"
              onClick={() => apiDownload(`/hr/payrolls/me/${yyyymm}/pdf`,
                `payslip-${yyyymm}.pdf`)}>명세서 PDF</Button>
          : undefined}>
        {mine.data?.confirmed ? (
          <>
            {mine.data.paid_on && <p className="mb-2 text-sm text-slate-600">
              지급일 <b>{mine.data.paid_on}</b></p>}
            <Table headers={["항목", "구분", "금액", "계산방법"]}
              rows={(mine.data.lines).map((l) => [
                // 코드(BASE·TAX)가 아니라 카탈로그 한글명 — 근로자가 읽을 수 있어야 한다
                <span key="n" className="flex items-center gap-1">
                  {l.item_name}
                  {l.item_kind === "EARNING" && !l.taxable && <Badge tone="blue">비과세</Badge>}
                </span>,
                <Badge key="k" tone={l.item_kind === "EARNING" ? "green" : "red"}>
                  {l.item_kind === "EARNING" ? "지급" : "공제"}</Badge>,
                won(l.amount),
                <span key="f" className="text-xs text-slate-500">{l.formula ?? "-"}</span>,
              ])} />
            <p className="mt-2 text-sm font-medium">
              실수령액: {won(earn - deduct)}
            </p>
          </>
        ) : <p className="text-sm text-slate-400">확정된 명세가 없습니다</p>}
      </Card>

      {isAdmin && (
        <Card title="급여 입력·확정 (관리자 — 수동 입력, 자동 계산 없음)">
          <div className="flex flex-wrap items-end gap-2">
            <label className="block">
              <span className="mb-1 block text-xs font-medium text-slate-600">직원 (이름·사번 검색)</span>
              <EmployeePicker value={empId} onPick={setEmpId} />
            </label>
            <Input label="기본급" value={base} onChange={(e) => setBase(e.target.value)} />
            <Input label="공제(세금 등)" value={tax} onChange={(e) => setTax(e.target.value)} />
            <Button variant="secondary" disabled={!empId || upsert.isPending}
              onClick={() => upsert.mutate()}>저장</Button>
            <Button disabled={!empId || confirmPay.isPending || upsert.isPending}
              onClick={() => confirmPay.mutate()}>확정</Button>
          </div>
        </Card>
      )}
    </div>
  );
}

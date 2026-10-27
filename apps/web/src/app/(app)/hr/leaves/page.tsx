"use client";
/** 휴가 — 본인 신청(FR-05)·잔여 조회·승인함 테이블 (W3). */
import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useForm } from "react-hook-form";
import { zodResolver } from "@hookform/resolvers/zod";
import { z } from "zod";
import { ApiError, api, auth } from "@/lib/api";
import { Badge, type Banner, Button, Card, Input, MessageBanner, Table } from "@/components/ui";
import { useConfirm } from "@/components/confirm";
import type { LeaveRequestRow } from "@/types";

const leaveSchema = z.object({
  start_on: z.string().min(1, "시작일 필수"),
  end_on: z.string().min(1, "종료일 필수"),
  days: z.string().regex(/^\d+(\.5)?$/, "0.5 단위 숫자"),
  reason: z.string().optional(),
});
type LeaveForm = z.infer<typeof leaveSchema>;

const STATUS_TONE: Record<string, "blue" | "green" | "red" | "gray"> = {
  REQUESTED: "blue", APPROVED: "green", REJECTED: "red", CANCELED: "gray",
};

export default function LeavesPage() {
  const qc = useQueryClient();
  const confirm = useConfirm();
  const onDecide = async (id: number, ok: boolean) => {
    if (await confirm({ title: ok ? "휴가 승인" : "휴가 반려",
      body: `신청 #${id}을(를) ${ok ? "승인" : "반려"}할까요?`, danger: !ok,
      confirmLabel: ok ? "승인" : "반려" })) decide.mutate({ id, ok });
  };
  const year = new Date().getFullYear();
  const role = auth.claims()?.role ?? "STAFF";
  const isApprover = ["DIRECTOR", "ADMIN", "MANAGER"].includes(role);
  const [banner, setBanner] = useState<Banner>(null);

  const remaining = useQuery({
    queryKey: ["leave-remaining", year],
    queryFn: () => api<{ remaining: number }>(`/hr/leaves/remaining?year=${year}`),
    retry: 0,
  });
  const requests = useQuery({
    queryKey: ["leave-requests"],
    queryFn: () => api<{ items: LeaveRequestRow[] }>("/hr/leaves/requests?size=100"),
  });

  const form = useForm<LeaveForm>({ resolver: zodResolver(leaveSchema) });
  const request = useMutation({
    mutationFn: (f: LeaveForm) => api("/hr/leaves/requests", {
      method: "POST",
      body: JSON.stringify({ ...f, days: Number(f.days), leave_type: "ANNUAL" }) }),
    onSuccess: () => {
      setBanner({ text: "신청 완료 — 부서장 승인 대기", tone: "ok" });
      qc.invalidateQueries({ queryKey: ["leave-requests"] });
    },
    onError: (e) => setBanner({
      text: e instanceof ApiError ? e.message : "신청 실패", tone: "error" }),
  });
  const decide = useMutation({
    mutationFn: ({ id, ok }: { id: number; ok: boolean }) =>
      api(`/hr/leaves/requests/${id}/${ok ? "approve" : "reject"}`, { method: "PATCH" }),
    onSuccess: (_, v) => {
      setBanner({ text: `#${v.id} ${v.ok ? "승인" : "반려"} 완료`, tone: "ok" });
      qc.invalidateQueries({ queryKey: ["leave-requests"] });
      qc.invalidateQueries({ queryKey: ["leave-remaining"] });
    },
    onError: (e) => setBanner({
      text: e instanceof ApiError ? e.message : "처리 실패 (본부서 신청만 가능)", tone: "error" }),
  });

  return (
    <div className="space-y-4">
      <h1 className="text-lg font-bold">인사 — 휴가</h1>
      <MessageBanner banner={banner} />

      <div className="grid gap-4 lg:grid-cols-2">
        <Card title={`연차 잔여 (${year})`}>
          <p className="text-2xl font-bold">
            {remaining.data ? `${remaining.data.remaining}일` : "—"}
            <span className="ml-2 text-xs font-normal text-slate-400">근로기준법 §60 자동 산정</span>
          </p>
        </Card>

        <Card title="연차 신청">
          <form className="flex flex-wrap items-end gap-2"
            onSubmit={form.handleSubmit((f) => request.mutate(f))}>
            <Input label="시작일" type="date" {...form.register("start_on")} />
            <Input label="종료일" type="date" {...form.register("end_on")} />
            <Input label="일수" {...form.register("days")} className="w-20" />
            <Input label="사유 (선택)" {...form.register("reason")} />
            <Button type="submit" isLoading={request.isPending}>신청</Button>
          </form>
          {Object.values(form.formState.errors).map((e, i) =>
            <p key={i} className="mt-1 text-xs text-red-600">{e?.message as string}</p>)}
        </Card>
      </div>

      <Card title={isApprover ? "신청 목록·승인함 (본부서)" : "내 신청 내역"}>
        {requests.isError && (
          <p className="text-sm text-red-600">목록을 불러오지 못했습니다</p>)}
        <Table headers={["#", "신청자", "기간", "일수", "사유", "상태", ""]}
          rows={(requests.data?.items ?? []).map((r) => [
            r.id, r.employee_name, `${r.start_on} ~ ${r.end_on}`, r.days,
            r.reason ?? "-",
            <Badge key="s" tone={STATUS_TONE[r.status] ?? "gray"}>{r.status}</Badge>,
            isApprover && r.status === "REQUESTED" && (
              <span key="a" className="flex gap-1">
                <Button variant="secondary" isLoading={decide.isPending}
                  onClick={() => onDecide(r.id, true)}>승인</Button>
                <Button variant="danger" isLoading={decide.isPending}
                  onClick={() => onDecide(r.id, false)}>반려</Button>
              </span>
            ),
          ])} empty="신청 내역 없음" />
      </Card>
    </div>
  );
}

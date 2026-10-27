"use client";
/** 예약 캘린더 (FR-17) — 일자별 목록·등록 (동일 의사·시간 중복은 서버 차단). */
import { todayLocal } from "@/lib/dates";
import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useForm } from "react-hook-form";
import { zodResolver } from "@hookform/resolvers/zod";
import { z } from "zod";
import { ApiError, api } from "@/lib/api";
import { useToast } from "@/components/toast";
import { Badge, Button, Card, Input, Table } from "@/components/ui";
import { QueryErrorBanner } from "@/components/query-guard";

const schema = z.object({
  patient_id: z.string().regex(/^\d+$/, "환자 ID는 숫자입니다"),
  dept_code: z.string().min(1, "진료과 필수"),
  doctor_id: z.string().regex(/^\d+$/, "의사 ID는 숫자입니다"),
  reserved_at: z.string().min(1, "예약 일시 필수"),
});
type Form = z.infer<typeof schema>;

interface Row { id: number; chart_no: string; name: string; dept_code: string;
                doctor_id: number; reserved_at: string; status: string }

export default function ReservationsPage() {
  const qc = useQueryClient();
  const toast = useToast();
  const [date, setDate] = useState(todayLocal());

  const list = useQuery({
    queryKey: ["reservations", date],
    queryFn: () => api<{ items: Row[] }>(`/patients/reservations?on=${date}`),
  });

  const { register, handleSubmit, formState: { errors } } = useForm<Form>({
    resolver: zodResolver(schema),
  });
  const create = useMutation({
    mutationFn: (f: Form) => api("/patients/reservations", {
      method: "POST",
      body: JSON.stringify({ ...f, patient_id: Number(f.patient_id),
                             doctor_id: Number(f.doctor_id) }) }),
    onSuccess: () => { toast.ok("예약 완료"); qc.invalidateQueries({ queryKey: ["reservations"] }); },
    onError: (e) => toast.err(e instanceof ApiError ? e.message : "예약 실패 (중복 슬롯?)"),
  });

  return (
    <div className="space-y-4">
      <QueryErrorBanner queries={[list]} />
      <div className="flex items-center gap-3">
        <h1 className="text-lg font-bold">원무 — 예약</h1>
        <input type="date" value={date} onChange={(e) => setDate(e.target.value)}
          className="rounded-md border border-slate-300 px-2 py-1 text-sm" />
      </div>

      <Card title="예약 등록">
        <form className="flex flex-wrap items-end gap-2"
          onSubmit={handleSubmit((f) => create.mutate(f))}>
          <Input label="환자 ID" {...register("patient_id")} />
          <Input label="진료과" {...register("dept_code")} />
          <Input label="의사 ID" {...register("doctor_id")} />
          <Input label="예약 일시" type="datetime-local" {...register("reserved_at")} />
          <Button type="submit" disabled={create.isPending}>예약</Button>
        </form>
        {Object.values(errors).map((e, i) =>
          <p key={i} className="mt-1 text-xs text-red-600">{e?.message as string}</p>)}
      </Card>

      <Card title={`${date} 예약 목록`}>
        <Table headers={["시각", "차트", "이름", "진료과", "의사", "상태"]}
          rows={(list.data?.items ?? []).map((r) => [
            new Date(r.reserved_at).toLocaleTimeString("ko-KR", { hour: "2-digit", minute: "2-digit" }),
            r.chart_no, r.name, r.dept_code, r.doctor_id,
            <Badge key="s" tone={r.status === "BOOKED" ? "blue" : "gray"}>{r.status}</Badge>,
          ])} />
      </Card>
    </div>
  );
}

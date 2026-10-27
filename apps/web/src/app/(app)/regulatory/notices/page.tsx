"use client";
/** 공시 관리 (FR-51) + AI 발신 모니터·차단 검토 큐 (FR-52/53) — design §7.1. */
import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useForm } from "react-hook-form";
import { ApiError, api } from "@/lib/api";
import { Badge, Button, Card, Input, Table } from "@/components/ui";

interface NoticeRow {
  erp_ref_id: string; notice_type: string; revision_no: number;
  title: string; status: string; effective_from: string;
}
interface DispatchRow {
  id: number; event_type: string; revision_no: number; urgency: string;
  filter_status: string; dispatch_status: string; retry_count: number;
  filter_detail: { findings?: { type: string; masked: string }[]; reason?: string } | null;
}

const TYPES = ["TARIFF", "DRUG_MATERIAL", "REVIEW_CRITERIA", "MEDICAL_LAW",
               "ADMIN", "NARCOTIC", "LABOR", "INTERNAL"];

export default function NoticesPage() {
  const qc = useQueryClient();
  const [selected, setSelected] = useState<string | null>(null);
  const [message, setMessage] = useState<string | null>(null);

  const notices = useQuery({
    queryKey: ["notices"],
    queryFn: () => api<{ items: NoticeRow[] }>("/regulatory/notices"),
  });
  const dispatch = useQuery({
    queryKey: ["dispatch", selected],
    queryFn: () => api<{ items: DispatchRow[] }>(`/regulatory/notices/${selected}/dispatch-status`),
    enabled: !!selected,
    // 진행 중(QUEUED/SENT)인 발신이 있을 때만 30초 폴링 — 모두 종료되면 중단(ai_dispatch 부하 절감)
    refetchInterval: (query) =>
      (query.state.data?.items ?? []).some(
        (d) => d.dispatch_status === "QUEUED" || d.dispatch_status === "SENT")
        ? 30_000 : false,
  });

  const form = useForm<{ notice_type: string; title: string; body_text: string;
                         effective_from: string; urgent_flag: boolean }>({
    defaultValues: { notice_type: "ADMIN", urgent_flag: false },
  });
  const create = useMutation({
    mutationFn: (body: object) =>
      api("/regulatory/notices", { method: "POST", body: JSON.stringify(body) }),
    onSuccess: () => {
      setMessage("공시 등록 완료 — AI 색인 큐에 적재됨");
      form.reset({ notice_type: "ADMIN", urgent_flag: false, title: "", body_text: "" });
      qc.invalidateQueries({ queryKey: ["notices"] });
    },
    onError: (e) => setMessage(e instanceof ApiError ? `${e.error.code}: ${e.message}` : "등록 실패"),
  });
  const approveDispatch = useMutation({
    mutationFn: (queueId: number) =>
      api(`/regulatory/dispatch/${queueId}/approve`, { method: "POST" }),
    onSuccess: () => {
      setMessage("승인 완료 — 현재 본문 hash에 바인딩되어 재발신 큐 적재 (본문 변경 시 자동 재필터)");
      qc.invalidateQueries({ queryKey: ["dispatch"] });
    },
    onError: (e) => setMessage(e instanceof ApiError ? e.message : "승인 실패"),
  });
  const revoke = useMutation({
    mutationFn: (id: string) =>
      api(`/regulatory/notices/${id}/revoke`, { method: "POST" }),
    onSuccess: () => { setMessage("폐지 완료 — revoke 발신 큐 적재"); qc.invalidateQueries({ queryKey: ["notices"] }); },
  });

  const filterTone = (s: string) =>
    s === "BLOCKED" ? "red" : s === "PASSED" || s === "APPROVED" ? "green" : "gray";
  const dispatchTone = (s: string) =>
    s === "DONE" ? "green" : s === "DLQ" || s === "FAILED" ? "red" : s === "SENT" ? "blue" : "gray";

  return (
    <div className="space-y-4">
      <h1 className="text-lg font-bold">규제 — 공시 관리·AI 색인</h1>
      {message && <p className="text-sm text-blue-600">{message}</p>}

      <Card title="공시 등록 (시행일 필수 — 미입력 차단)">
        <form className="space-y-2" onSubmit={form.handleSubmit((f) => create.mutate(f))}>
          <div className="flex flex-wrap items-end gap-2">
            <label className="block">
              <span className="mb-1 block text-xs font-medium text-slate-600">분류</span>
              <select {...form.register("notice_type")}
                className="rounded-md border border-slate-300 px-3 py-1.5 text-sm">
                {TYPES.map((t) => <option key={t}>{t}</option>)}
              </select>
            </label>
            <Input label="제목" {...form.register("title", { required: true })} className="min-w-64" />
            <Input label="시행일" type="date" {...form.register("effective_from", { required: true })} />
            <label className="flex items-center gap-1 pb-2 text-xs text-slate-600">
              <input type="checkbox" {...form.register("urgent_flag")} /> 긴급(immediate)
            </label>
          </div>
          <textarea {...form.register("body_text", { required: true })}
            placeholder="본문 (1MB 한도 — 별표·서식은 첨부 분리)" rows={4}
            className="w-full rounded-md border border-slate-300 px-3 py-2 text-sm" />
          <Button type="submit" disabled={create.isPending}>등록</Button>
        </form>
      </Card>

      <Card title="공시 목록">
        <Table headers={["분류", "제목", "rev", "상태", "시행일", ""]}
          rows={(notices.data?.items ?? []).map((n) => [
            <Badge key="t" tone={n.notice_type === "NARCOTIC" ? "red" : "gray"}>{n.notice_type}</Badge>,
            <button key="b" className="text-blue-600 hover:underline"
              onClick={() => setSelected(n.erp_ref_id)}>{n.title}</button>,
            n.revision_no,
            <Badge key="s" tone={n.status === "ACTIVE" ? "green" : n.status === "REPEALED" ? "red" : "amber"}>
              {n.status}</Badge>,
            n.effective_from,
            n.status !== "REPEALED" &&
              <Button key="r" variant="danger" onClick={() => revoke.mutate(n.erp_ref_id)}>폐지</Button>,
          ])} />
      </Card>

      {selected && (
        <Card title="AI 발신 상태 (30초 갱신 — 차단 건은 filter_detail 확인)">
          <Table headers={["이벤트", "rev", "긴급", "필터", "발신", "재시도", "차단 상세", ""]}
            rows={(dispatch.data?.items ?? []).map((d) => [
              d.event_type, d.revision_no,
              <Badge key="u" tone={d.urgency === "IMMEDIATE" ? "amber" : "gray"}>{d.urgency}</Badge>,
              <Badge key="f" tone={filterTone(d.filter_status)}>{d.filter_status}</Badge>,
              <Badge key="d" tone={dispatchTone(d.dispatch_status)}>{d.dispatch_status}</Badge>,
              d.retry_count,
              d.filter_detail?.findings?.map((f) => `${f.type}(${f.masked})`).join(", ")
                ?? d.filter_detail?.reason ?? "-",
              d.filter_status === "BLOCKED" &&
                <Button key="a" variant="secondary" disabled={approveDispatch.isPending}
                  onClick={() => approveDispatch.mutate(d.id)}>검토 승인</Button>,
            ])} />
        </Card>
      )}
    </div>
  );
}

"use client";
/** 의료보험 — 통보서 업로드(FR-38 fallback)·이의신청 보드(FR-40)·삭감 통계(FR-48). */
import { useRef, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { ApiError, api, apiUpload } from "@/lib/api";
import { Badge, Button, Card, Input, Modal, Table } from "@/components/ui";
import { QueryErrorBanner } from "@/components/query-guard";
import type { AppealRow } from "@/types";

// 이의신청(90일)과 심사청구는 절차·기한 성격이 달라 표에서 구분돼야 한다
const APPEAL_TYPE: Record<string, string> = {
  REVIEW_REQUEST: "심사청구", OBJECTION: "이의신청" };
import { won } from "@/lib/format";

interface BatchRow { id: number; receipt_no: string; care_month: string;
                     claimed_amt: number; decided_amt: number }
interface StatementRow {
  id: number; stmt_serial: string; chart_no: string | null; dept_code: string | null;
  claimed_amt: number; adjusted_amt: number; decided_amt: number;
  adjustments: { id: number; adj_code: string | null; adj_amt: number;
                 adj_reason: string | null; map_status: string }[];
}

interface Resub {
  id: number; original_ref: string; care_month: string | null; returned_on: string | null;
  return_code: string | null; return_reason: string | null; amount: number | null;
  resubmit_ref: string | null; resubmitted_on: string | null; status: string;
}
interface Recon {
  care_month: string; submitted: number; reviewed_claimed: number; variance: number;
  matched: boolean; submission_count: number; statement_count: number;
}
const RESUB_TONE: Record<string, "red" | "green" | "amber"> = {
  RETURNED: "red", RESUBMITTED: "amber", ACCEPTED: "green" };

export default function ClaimsPage() {
  const qc = useQueryClient();
  const fileRef = useRef<HTMLInputElement>(null);
  const [message, setMessage] = useState<string | null>(null);
  const [transitionId, setTransitionId] = useState("");

  const [batchId, setBatchId] = useState<number | null>(null);
  // 반송·재청구 / 제출액 대사 — 백엔드 EP가 완비인데 화면 호출이 0건이었다.
  // 미재청구 = 수익 유실인데 가시화되지 않아 놓치는 구조였다(모델 docstring이 명시한 목적).
  const [reconMonth, setReconMonth] = useState(() => new Date().toISOString().slice(0, 7));
  const resub = useQuery({
    queryKey: ["resubmissions"],
    queryFn: () => api<{ items: Resub[]; pending_amount: number }>("/claims/resubmissions"),
  });
  const recon = useQuery({
    queryKey: ["submission-recon", reconMonth],
    enabled: /^\d{4}-\d{2}$/.test(reconMonth),
    queryFn: () => api<Recon>(`/claims/submission-reconciliation?care_month=${reconMonth}`),
    retry: 0,
  });
  const appeals = useQuery({
    queryKey: ["appeals"],
    queryFn: () => api<{ items: AppealRow[] }>("/claims/appeals?size=100"),
  });
  const batches = useQuery({
    queryKey: ["batches"],
    queryFn: () => api<{ items: BatchRow[] }>("/claims/batches?size=50"),
  });
  const statements = useQuery({
    queryKey: ["statements", batchId],
    queryFn: () => api<{ items: StatementRow[] }>(`/claims/statements?batch_id=${batchId}`),
    enabled: batchId !== null,
  });
  const selectedReceipt = batches.data?.items.find((b) => b.id === batchId)?.receipt_no;
  const [statsBy, setStatsBy] = useState<"dept" | "doctor" | "reason">("dept");
  const stats = useQuery({
    queryKey: ["claim-stats", statsBy],
    queryFn: () => api<{ items: { dim: string; count: number; amount: number }[];
                         automation: { rate: number; total: number } }>(
      `/claims/stats?by=${statsBy}`),
  });

  const upload = useMutation({
    // 검토 버그-2 — api.ts와 동일 refresh 경로 사용 (직접 fetch·sessionStorage 접근 제거)
    mutationFn: async (file: File) => {
      const fd = new FormData();
      fd.append("file", file);
      return apiUpload<{ staging_id: number; status: string; note: string }>(
        "/claims/upload", fd);
    },
    onSuccess: (d) => setMessage(`업로드 완료 (staging #${d.staging_id}) — ${d.note}`),
    onError: (e) => setMessage(e instanceof ApiError ? e.message : "업로드 실패"),
  });

  const transition = useMutation({
    mutationFn: ({ id, status }: { id: string; status: string }) =>
      api(`/claims/appeals/${id}/status`, { method: "PATCH", body: JSON.stringify({ status }) }),
    onSuccess: (_, v) => {
      setMessage(`#${v.id} → ${v.status} ${v.status === "REJECTED" ? "(손실 전표 자동 생성)" : ""}`);
      qc.invalidateQueries({ queryKey: ["appeals"] });
    },
    onError: (e) => setMessage(e instanceof ApiError ? e.message : "전이 실패"),
  });

  return (
    <div className="space-y-4">
      <h1 className="text-lg font-bold">의료보험 — 삭감·이의신청</h1>
      <QueryErrorBanner queries={[appeals, batches, statements, stats]} />
      {message && <p className="text-sm text-blue-600">{message}</p>}

      <div className="grid gap-4 lg:grid-cols-2">
        <Card title="심사결과통보서 업로드 (50MB · PDF/XLSX/HWP/전문)">
          <div className="flex items-center gap-2">
            <input ref={fileRef} type="file" className="text-sm" />
            <Button onClick={() => fileRef.current?.files?.[0] && upload.mutate(fileRef.current.files[0])}
              disabled={upload.isPending}>업로드</Button>
          </div>
          <p className="mt-2 text-xs text-slate-400">
            파싱·삭감 매핑은 레이아웃 규격 확보(M0c) 후 활성화 — 현재는 스테이징 보관
          </p>
        </Card>

        <Card title="삭감 통계"
          action={
            <select value={statsBy}
              onChange={(e) => setStatsBy(e.target.value as "dept" | "doctor" | "reason")}
              className="rounded border border-slate-300 px-2 py-1 text-xs">
              <option value="dept">진료과별</option>
              <option value="doctor">의사별</option>
              <option value="reason">사유별</option>
            </select>}>
          <Table headers={[statsBy === "dept" ? "진료과" : statsBy === "doctor" ? "의사" : "조정 사유", "건수", "삭감액"]}
            rows={(stats.data?.items ?? []).map((r) => [
              r.dim, r.count, won(r.amount)])} />
          <p className="mt-2 text-xs text-slate-500">
            자동 매핑율: <Badge tone="blue">{stats.data?.automation.rate ?? 0}%</Badge>
            {" "}(목표 90% — Plan §4.3)
          </p>
        </Card>
      </div>

      <Card title="통보서 드릴다운 (접수번호 → 명세서 → 조정 내역)">
        <Table headers={["접수번호", "진료월", "청구액", "결정액", ""]}
          rows={(batches.data?.items ?? []).map((b) => [
            b.receipt_no, b.care_month,
            b.claimed_amt.toLocaleString(), b.decided_amt.toLocaleString(),
            <Button key="d" variant={batchId === b.id ? "primary" : "secondary"}
              onClick={() => setBatchId(b.id)}>명세 보기</Button>,
          ])} empty="수신된 통보서 없음 (G1 파서 활성화 또는 수동 등록 후 표시)" />
      </Card>

      {batchId !== null && (
        <Modal size="lg" title={`명세서 — 접수 #${batchId} (${selectedReceipt ?? ""})`}
          onClose={() => setBatchId(null)}>
          {statements.isFetching && <p className="py-6 text-center text-sm text-slate-400">명세 불러오는 중…</p>}
          {statements.isError && <p className="py-6 text-center text-sm text-red-600">명세 조회 실패 — 권한 또는 네트워크 확인</p>}
          {!statements.isFetching && !statements.isError
            && (statements.data?.items?.length ?? 0) === 0
            && <p className="py-6 text-center text-sm text-slate-400">이 통보서에 명세서가 없습니다.</p>}
          {(statements.data?.items ?? []).length > 0 && (
            <Table headers={["명일련", "차트", "진료과", "청구액", "조정액", "결정액"]}
              rows={(statements.data?.items ?? []).map((s) => [
                s.stmt_serial, s.chart_no ?? "-", s.dept_code ?? "-",
                won(s.claimed_amt ?? 0),
                won(s.adjusted_amt ?? 0),
                won(s.decided_amt ?? 0),
              ])} />
          )}
          {(statements.data?.items ?? []).flatMap((s) =>
            s.adjustments.map((a) => ({ ...a, serial: s.stmt_serial }))).length > 0 && (
            <div className="mt-4">
              <p className="mb-1 text-xs font-semibold uppercase tracking-wide text-slate-500">조정 내역</p>
              <Table headers={["명일련", "코드", "금액", "사유", "매핑"]}
                rows={(statements.data?.items ?? []).flatMap((s) =>
                  s.adjustments.map((a) => [
                    s.stmt_serial, a.adj_code ?? "?", won(a.adj_amt ?? 0),
                    a.adj_reason ?? "-",
                    <Badge key="m" tone={a.map_status === "AUTO" ? "green"
                      : a.map_status === "MANUAL" ? "blue" : "amber"}>{a.map_status}</Badge>,
                  ]))} />
            </div>
          )}
        </Modal>
      )}

      {/* 반송·재청구 — 심사 전 반려 트랙(이의신청과 별개). 미재청구는 곧 수익 유실이다. */}
      <Card title="반송·재청구" action={
        <span className={`text-sm font-semibold ${(resub.data?.pending_amount ?? 0) > 0
          ? "text-rose-600" : "text-slate-400"}`}>
          미재청구 {won(resub.data?.pending_amount ?? 0)}</span>}>
        <Table headers={["원 청구", "진료월", "반송일", "코드", "사유", "금액", "상태", "재청구"]}
          rows={(resub.data?.items ?? []).map((r) => [
            <span key="o" className="font-mono text-xs">{r.original_ref}</span>,
            r.care_month ?? "—", r.returned_on ?? "—", r.return_code ?? "—",
            <span key="rs" className="text-xs text-slate-600" title={r.return_reason ?? ""}>
              {r.return_reason ?? "—"}</span>,
            r.amount != null ? won(r.amount) : "—",
            <Badge key="s" tone={RESUB_TONE[r.status] ?? "gray"}>{r.status}</Badge>,
            r.resubmitted_on ? `${r.resubmit_ref ?? ""} (${r.resubmitted_on})` : "—",
          ])} empty={resub.isFetching ? "불러오는 중…" : "반송 건 없음"} />
      </Card>

      {/* 제출액 대사 — 제출 청구액(HIS)과 심사대상 청구액(MEDRLT) 불일치는 청구 누락 신호다. */}
      <Card title="청구액 대사 (제출 vs 심사대상)" action={
        <input type="month" value={reconMonth} onChange={(e) => setReconMonth(e.target.value)}
          className="rounded-md border border-slate-300 px-2 py-1 text-sm" />}>
        {recon.isError ? <p className="text-sm text-slate-400">해당 진료월 대사 데이터 없음</p> : (
          <div className="flex flex-wrap items-center gap-6 text-sm">
            <span>제출액 <b>{won(recon.data?.submitted)}</b>
              <span className="ml-1 text-xs text-slate-400">({recon.data?.submission_count ?? 0}건)</span></span>
            <span>심사대상 <b>{won(recon.data?.reviewed_claimed)}</b>
              <span className="ml-1 text-xs text-slate-400">({recon.data?.statement_count ?? 0}건)</span></span>
            <span>차이 <b className={recon.data?.matched ? "" : "text-rose-600"}>
              {won(recon.data?.variance)}</b></span>
            {recon.data && (recon.data.matched
              ? <Badge tone="green">일치</Badge>
              // 심사대상이 0이면 "누락"이 아니라 통보서 미도착(제출 후 심사 진행 중)이다 —
              // 정상 진행 상태를 적색 누락 경보로 띄우면 진짜 누락과 구분되지 않는다.
              : recon.data.statement_count === 0 && recon.data.submission_count > 0
                ? <Badge tone="amber">통보서 미도착 — 심사 진행 중</Badge>
                : <Badge tone="red">불일치 — 청구 누락 확인 필요</Badge>)}
          </div>
        )}
      </Card>

      <Card title="이의신청 보드 (90일 기한 — D-14/7/3 알림)">
        <Table headers={["#", "대상 삭감", "유형", "상태", "도달일", "법정 기한", "D-day",
          "확정손실", "전표"]}
          rows={(appeals.data?.items ?? []).map((a) => [
            a.id,
            `#${a.adjustment_id}`,
            <Badge key="t" tone="gray">
              {APPEAL_TYPE[a.appeal_type] ?? a.appeal_type}</Badge>,
            <Badge key="s" tone={a.status === "ACCEPTED" ? "green"
              : a.status === "REJECTED" ? "red"
              : a.status === "SUPPLEMENT" ? "amber" : "blue"}>{a.status}</Badge>,
            // 90일 기한의 기산일 — 도달일이 잘못 입력된 건은 D-day만 봐선 영영 못 잡는다
            a.notice_arrived_on,
            a.legal_deadline,
            // 도과 건은 d_day가 음수라 "D--5"로 찍히던 표기 정정
            a.d_day < 0
              ? <Badge key="d" tone="red">{-a.d_day}일 도과</Badge>
              : <Badge key="d" tone={a.d_day <= 3 ? "red" : a.d_day <= 14 ? "amber" : "gray"}>
                  D-{a.d_day}</Badge>,
            a.final_loss_amt != null ? won(a.final_loss_amt) : "-",
            a.voucher_ref ?? "-",
          ])} />
        <div className="mt-3 flex items-end gap-2 border-t border-slate-100 pt-3">
          <Input label="신청 #" value={transitionId}
            onChange={(e) => setTransitionId(e.target.value)} />
          <Button variant="secondary" disabled={!transitionId || transition.isPending}
            onClick={() => transition.mutate({ id: transitionId, status: "SUPPLEMENT" })}>보완</Button>
          <Button disabled={!transitionId || transition.isPending}
            onClick={() => transition.mutate({ id: transitionId, status: "ACCEPTED" })}>인정</Button>
          <Button variant="danger" disabled={!transitionId || transition.isPending}
            onClick={() => transition.mutate({ id: transitionId, status: "REJECTED" })}>기각 (손실 확정)</Button>
        </div>
      </Card>
    </div>
  );
}

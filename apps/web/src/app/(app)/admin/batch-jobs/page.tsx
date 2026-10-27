"use client";
/** 시스템 — 배치 작업(v1.302): worker-batch 잡의 주기·다음 실행·마지막 결과·이력 + 지금 실행·일시정지·재개(사유·감사).
 *  조작은 Redis 명령 큐 → 스케줄러 리더가 30초 안에 반영(결과는 '최근 조작'). 보기=관리자·원장, 조작=관리자. */
import { useState } from "react";
import Link from "next/link";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { ApiError, api, auth } from "@/lib/api";
import { Badge, Button, Card, Modal, Table } from "@/components/ui";
import { QueryErrorBanner } from "@/components/query-guard";
import { useInputDialog } from "@/components/input-dialog";
import { useToast } from "@/components/toast";

interface Job {
  id: string; label: string; description: string; can_pause: boolean; can_run: boolean;
  registered?: boolean; trigger?: string | null; next_run?: number | null; paused?: boolean;
  paused_info?: { by: number; reason: string; at: number } | null; pause_mismatch?: boolean;
  last_run?: number | null; last_status?: string | null; last_error?: string | null;
}
interface Overview {
  redis_ok: boolean; error?: string; now?: number; heartbeats: Record<string, number | null>;
  heartbeat_limits?: Record<string, number>;
  pending_commands?: { req: string; op: string; job: string; at: number; reason: string }[];
  schedule_published: boolean; jobs: Job[];
  recent_commands: { req: string; op: string; job: string; ok: boolean; msg: string; at: number }[];
}
interface Hist { ts: number; status: string; err: string; manual_by: string | null }

const OP_LABEL: Record<string, string> = { run: "지금 실행", pause: "일시정지", resume: "재개" };
const fmt = (ts?: number | null) => ts ? new Date(ts * 1000).toLocaleString("ko-KR", { timeZone: "Asia/Seoul",
  month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit" }) : "—";
const ago = (now: number, ts?: number | null) => {
  if (!ts || !now) return "";
  const s = now - ts;
  return s < 120 ? `${s}초 전` : s < 7200 ? `${Math.round(s / 60)}분 전` : s < 172800 ? `${Math.round(s / 3600)}시간 전` : `${Math.round(s / 86400)}일 전`;
};

export default function BatchJobsPage() {
  const qc = useQueryClient();
  const toast = useToast();
  const { ask, dialog } = useInputDialog();
  const canWrite = auth.claims()?.role === "ADMIN";
  const [hist, setHist] = useState<Job | null>(null);
  const q = useQuery({ queryKey: ["batch-jobs"], queryFn: () => api<Overview>("/admin/batch-jobs"), refetchInterval: 15_000 });
  const h = useQuery({ queryKey: ["batch-job-history", hist?.id], enabled: !!hist,
    queryFn: () => api<{ items: Hist[] }>(`/admin/batch-jobs/${hist!.id}/history`) });
  const cmd = useMutation({
    mutationFn: (v: { job: string; op: string; reason: string }) =>
      api(`/admin/batch-jobs/${v.job}/${v.op}`, { method: "POST", body: JSON.stringify({ reason: v.reason }) }),
    onSuccess: (_d, v) => { toast.ok(`${OP_LABEL[v.op]} 요청 — 30초 안에 반영됩니다`); qc.invalidateQueries({ queryKey: ["batch-jobs"] }); },
    onError: (e) => toast.err(e instanceof ApiError ? e.error.message : "요청 실패"),
  });
  const onCmd = async (j: Job, op: string) => {
    const reason = await ask({ title: `${j.label} — ${OP_LABEL[op]}`, required: true, label: "사유(필수)",
      placeholder: op === "pause" ? "예: 스토리지 점검 동안 백업 중지" : "예: 원인 수정 후 확인",
      hint: op === "pause" ? "멈춘 동안 이 작업은 돌지 않습니다. 결정·조치함에 '일시정지 중'으로 남아 잊히지 않습니다. 재시작해도 멈춰 있습니다."
          + (j.id === "recurring" ? " 정기업무 실행을 멈추면 상태 점검(/health/deep)이 '정기업무 멈춤'으로 바뀝니다 — 의도한 정지입니다." : "")
        : op === "run" ? "다음 실행 시각을 지금으로 당깁니다(주기는 그대로)." : "다음 예정 시각부터 다시 돕니다.",
      validate: (v) => (v.trim().length < 4 ? "4자 이상 입력하세요" : v.length > 200 ? "200자 이하" : null), confirmLabel: OP_LABEL[op] });
    if (typeof reason === "string" && reason) cmd.mutate({ job: j.id, op, reason });
  };

  const d = q.data;
  const now = d?.now ?? 0;          // 서버 시각 기준(렌더 중 Date.now 금지 — 순수성)
  const hb = d?.heartbeats ?? {};
  const limit = (k: string) => d?.heartbeat_limits?.[k] ?? 180;   // 연동 실행기는 설계상 최대 600초 비움(기준 900초)
  const beat = (k: string) => hb[k] == null ? <Badge tone="red">신호 없음</Badge>
    : hb[k]! > limit(k) ? <Badge tone="red">{hb[k]}초 전(멈춤 의심)</Badge> : <Badge tone="green">정상 · {hb[k]}초 전</Badge>;
  const jobs = (d?.jobs ?? []).filter((j) => j.registered !== false || !d?.schedule_published);
  return (
    <div className="space-y-4">
      <h1 className="text-lg font-bold">시스템 — 배치 작업</h1>
      <p className="text-xs text-slate-500">
        배치 실행기(worker-batch)가 주기적으로 돌리는 작업입니다. 일정·업무 단위 정기업무는{" "}
        <Link href="/admin/recurring-tasks" className="text-blue-600 underline">정기업무</Link>(매시 20분 &lsquo;정기업무 실행&rsquo;이 돌림),
        한 달 일정은 <Link href="/admin/schedule" className="text-blue-600 underline">일정 캘린더</Link>에서 봅니다.</p>
      <QueryErrorBanner queries={[q]} />
      {d && !d.redis_ok && <p className="rounded-md bg-rose-50 p-2 text-xs text-rose-700">상태 저장소(Redis)에 닿지 않습니다 — {d.error}</p>}
      {d?.redis_ok && !d.schedule_published && (
        <p className="rounded-md bg-amber-50 p-2 text-xs text-amber-800">실행기가 아직 일정을 게시하지 않았습니다(배포 직후·구버전 실행기). 30초 뒤 다시 보세요.</p>)}
      <Card title="실행기">
        <div className="flex flex-wrap gap-4 text-sm">
          <span>배치 실행기 {beat("worker-batch")}</span>
          <span>연동 실행기 {beat("worker-integration")}</span>
        </div>
      </Card>
      <Card title={`작업 ${jobs.length}개`}>
        <Table headers={["작업", "주기", "다음 실행", "마지막 실행", ""]}
          rows={jobs.map((j) => [
            <span key="n"><b>{j.label}</b>{j.paused && <> <Badge tone="amber">일시정지</Badge></>}
              {j.pause_mismatch && <> <Badge tone="red">기록·실제 다름(30초 안에 맞춤)</Badge></>}
              <span className="block text-xs text-slate-500">{j.description}</span>
              {j.paused_info && <span className="block text-xs text-amber-700">사유: {j.paused_info.reason} · {fmt(j.paused_info.at)}</span>}</span>,
            <span key="t" className="text-xs">{j.trigger ?? "—"}</span>,
            <span key="x" className="text-xs">{j.paused ? "—" : fmt(j.next_run)}</span>,
            <span key="l" className="text-xs">
              {j.last_status && <Badge tone={j.last_status === "ok" ? "green" : j.last_status === "skipped" ? "amber" : "red"}>{j.last_status === "ok" ? "정상" : j.last_status === "missed" ? "놓침" : j.last_status === "skipped" ? "건너뜀" : "실패"}</Badge>}
              {" "}{fmt(j.last_run)} <span className="text-slate-400">{ago(now, j.last_run)}</span>
              {j.last_error && <span className="block text-rose-600">{j.last_error}</span>}</span>,
            <div key="a" className="flex flex-wrap justify-end gap-1">
              <Button size="sm" variant="secondary" onClick={() => setHist(j)}>이력</Button>
              {j.can_run && <Button size="sm" variant="secondary" disabled={!canWrite || j.paused || cmd.isPending}
                onClick={() => onCmd(j, "run")}>지금 실행</Button>}
              {j.can_pause && (j.paused
                ? <Button size="sm" disabled={!canWrite || cmd.isPending} onClick={() => onCmd(j, "resume")}>재개</Button>
                : <Button size="sm" variant="warning" disabled={!canWrite || cmd.isPending} onClick={() => onCmd(j, "pause")}>일시정지</Button>)}
              {!j.can_pause && !j.can_run && <span className="text-xs text-slate-400">조작 불가(필수)</span>}
            </div>,
          ])} empty={q.isFetching ? "불러오는 중…" : "작업 없음"} />
        {!canWrite && <p className="mt-2 text-xs text-slate-500">지금 실행·일시정지·재개는 관리자(ADMIN)만 할 수 있습니다.</p>}
      </Card>
      {(d?.pending_commands ?? []).length > 0 && (
        <Card title={`반영 대기 ${d!.pending_commands!.length}건`}>
          <p className="mb-1 text-xs text-slate-500">실행기가 30초마다 가져갑니다. 10분 넘게 남은 명령은 &lsquo;만료&rsquo;로 처리됩니다.</p>
          <Table headers={["요청", "작업", "조작", "사유"]}
            rows={d!.pending_commands!.map((c) => [fmt(c.at), jobs.find((j) => j.id === c.job)?.label ?? c.job, OP_LABEL[c.op] ?? c.op, c.reason])} />
        </Card>)}
      {(d?.recent_commands ?? []).length > 0 && (
        <Card title="최근 조작">
          <Table headers={["시각", "작업", "조작", "결과"]}
            rows={d!.recent_commands.map((c) => [fmt(c.at), jobs.find((j) => j.id === c.job)?.label ?? c.job, OP_LABEL[c.op] ?? c.op,
              <Badge key="r" tone={c.ok ? "green" : "red"}>{c.msg}</Badge>])} />
        </Card>)}
      {dialog}
      {hist && (
        <Modal title={`${hist.label} — 실행 이력`} size="lg" onClose={() => setHist(null)}>
          <Table headers={["시각", "결과", "수동", "오류"]}
            rows={(h.data?.items ?? []).map((x) => [fmt(x.ts),
              <Badge key="s" tone={x.status === "ok" ? "green" : "red"}>{x.status}</Badge>,
              x.manual_by ? `요청자 #${x.manual_by}` : "", x.err || ""])}
            empty={h.isFetching ? "불러오는 중…" : "기록 없음(이 기능 배포 이후 실행부터 남습니다)"} />
        </Modal>)}
    </div>
  );
}

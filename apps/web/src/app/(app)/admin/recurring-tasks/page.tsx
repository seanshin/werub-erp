"use client";
/** 시스템 — 정기업무(주/월/분기 고정·반복업무) 관리: 정의 조회·활성화·실행·완료·이력.
 *  AUTO=자동 실행, AI_ASSISTED=AI 초안→확정, MANUAL=수기 체크리스트. read/write=관리자·원장
 *  (admin.recurring). 기획서 docs/01-plan/erp-recurring-operations-plan-2026-07-25.md. */
import { useState } from "react";
import Link from "next/link";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { ApiError, api } from "@/lib/api";
import { Badge, Button, Card, Modal, Table } from "@/components/ui";
import { QueryErrorBanner } from "@/components/query-guard";
import { useConfirm } from "@/components/confirm";
import { useInputDialog } from "@/components/input-dialog";
import { useToast } from "@/components/toast";

const CADENCE_LABEL: Record<string, string> = {
  DAILY: "일", WEEKLY: "주", MONTHLY: "월", QUARTERLY: "분기", YEARLY: "연",
};
const MODE_LABEL: Record<string, string> = {
  AUTO: "자동", AI_ASSISTED: "AI보조", MANUAL: "수기",
};
const MODE_TONE: Record<string, "blue" | "amber" | "gray"> = {
  AUTO: "blue", AI_ASSISTED: "amber", MANUAL: "gray",
};
const STATUS_LABEL: Record<string, string> = {
  DONE: "완료", FAILED: "실패", RUNNING: "실행중", DUE: "대기", SCHEDULED: "예정",
  SKIPPED: "생략", WAITING_APPROVAL: "승인대기", NOT_APPLICABLE: "해당 없음(등록 전 기한)",
};
const STATUS_TONE: Record<string, "green" | "red" | "blue" | "amber" | "gray"> = {
  DONE: "green", FAILED: "red", RUNNING: "blue", DUE: "amber",
  SCHEDULED: "gray", SKIPPED: "gray", WAITING_APPROVAL: "amber", NOT_APPLICABLE: "gray",
};

interface Task {
  code: string; name: string; domain: string; category: string; cadence: string;
  execution_mode: string; handler_ref: string | null; owner_role: string | null;
  owner_recipients: number;
  requires_approval: boolean; sla_days: number; enabled: boolean; note: string | null;
  has_handler: boolean;
}
// 담당 역할 후보 — /admin/menu와 동일 어휘(역할 배정 자체는 /admin/users)
const ROLE_KO: Record<string, string> = {
  DIRECTOR: "원장", ADMIN: "관리자", MANAGER: "부서장", STAFF: "직원",
  IT_MANAGER: "IT관리자", FINANCE_MANAGER: "회계관리자", LOGISTICS_MANAGER: "물류관리자" };
interface Due {
  code: string; period_key: string; scheduled_for: string; status: string;
  overdue: boolean; ai_runnable: boolean;
}
interface ComplianceItem {
  category: string; title: string; subject: string | null;
  due_on: string; days_remaining: number; band: string; status: string;
}
const BAND_TONE: Record<string, "red" | "amber" | "gray"> = {
  long_overdue: "red", overdue: "red", d7: "amber", d30: "amber", d90: "gray",
};
interface InfraJob {
  id: string; status: string; last_run_at: string; age_s: number; error: string | null;
  expected_interval_s: number | null; stale: boolean;
}
interface InfraPanel {
  redis_ok: boolean; worker_heartbeat_age_s: number | null; jobs: InfraJob[];
  signal: { ok: boolean; failed_recent: string[]; stuck_due: string[];
    stuck_running: string[]; engine_stale: boolean };
}
const INFRA_TONE: Record<string, "green" | "red" | "amber"> = {
  ok: "green", error: "red", missed: "amber",
};
const fmtAge = (s: number) =>
  s < 90 ? `${s}초 전` : s < 5400 ? `${Math.round(s / 60)}분 전` : `${Math.round(s / 3600)}시간 전`;

interface Run {
  period_key: string; status: string; scheduled_for: string | null; started_at: string | null;
  finished_at: string | null; triggered_by: string | null; actor: number | null;
  result: Record<string, unknown> | null; error: string | null; duration_ms: number | null;
}

export default function RecurringTasksPage() {
  const qc = useQueryClient();
  const toast = useToast();
  const confirm = useConfirm();
  const { ask, dialog } = useInputDialog();
  const fail = (e: unknown) => toast.err(e instanceof ApiError ? e.error.message : "처리 실패");
  const [historyOf, setHistoryOf] = useState<Task | null>(null);

  const tasks = useQuery({ queryKey: ["rectasks"], queryFn: () =>
    api<{ tasks: Task[] }>("/admin/recurring-tasks") });
  const due = useQuery({ queryKey: ["rectasks-due"], queryFn: () =>
    api<{ due: Due[] }>("/admin/recurring-tasks/due") });
  const compliance = useQuery({ queryKey: ["rectasks-compliance"], queryFn: () =>
    api<{ deadlines: ComplianceItem[] }>("/admin/recurring-tasks/compliance-context") });
  const infra = useQuery({ queryKey: ["rectasks-infra"], queryFn: () =>
    api<InfraPanel>("/admin/recurring-tasks/infra-jobs"), refetchInterval: 60_000 });
  const refresh = () => {
    qc.invalidateQueries({ queryKey: ["rectasks"] });
    qc.invalidateQueries({ queryKey: ["rectasks-due"] });
  };

  const run = useMutation({
    mutationFn: (code: string) =>
      api<{ status: string; error: string | null }>(`/admin/recurring-tasks/${code}/run`,
        { method: "POST" }),
    onSuccess: (r) => {
      if (r.status === "DONE") toast.ok("실행 완료"); else toast.err(r.error ?? "실패");
      refresh();
    },
    onError: fail,
  });
  const complete = useMutation({
    mutationFn: (v: { code: string; note: string }) =>
      api(`/admin/recurring-tasks/${v.code}/complete`, { method: "POST",
        body: JSON.stringify({ note: v.note }) }),
    onSuccess: () => { toast.ok("완료 처리"); refresh(); }, onError: fail,
  });
  const toggle = useMutation({
    mutationFn: (v: { code: string; enabled: boolean }) =>
      api(`/admin/recurring-tasks/${v.code}`, { method: "PUT",
        body: JSON.stringify({ enabled: v.enabled }) }),
    onSuccess: () => { toast.ok("반영됨"); refresh(); }, onError: fail,
  });
  // 담당 역할 변경 — 보유자 0명이면 알림이 관리자 폴백으로만 가므로 그 자리에서 고칠 수 있게 한다
  const setOwner = useMutation({
    mutationFn: (v: { code: string; owner_role: string }) =>
      api(`/admin/recurring-tasks/${v.code}`, { method: "PUT",
        body: JSON.stringify({ owner_role: v.owner_role }) }),
    onSuccess: () => { toast.ok("담당 역할 변경됨"); refresh(); }, onError: fail,
  });
  const runDue = useMutation({
    mutationFn: () => api<Record<string, number>>("/admin/recurring-tasks/run-due",
      { method: "POST" }),
    onSuccess: (s) => { toast.ok(`실행 ${s.executed ?? 0} · 완료 ${s.done ?? 0} · 실패 ${s.failed ?? 0}`); refresh(); },
    onError: fail,
  });
  const prepare = useMutation({
    mutationFn: (code: string) =>
      api<{ status: string; error: string | null }>(
        `/admin/recurring-tasks/${code}/prepare`, { method: "POST" }),
    // 서버가 준 status를 봐야 한다 — 초안 생성이 실패해도(FAILED) 200이라, 무조건 성공 토스트를
    // 띄우면 아무 일도 없었는데 "승인하세요"라고 안내하게 된다.
    onSuccess: (r) => {
      if (r.status === "WAITING_APPROVAL") toast.ok("초안 준비 — 결정·조치함에서 승인하세요");
      else toast.err(r.error ?? `초안 준비 실패 (${r.status})`);
      refresh();
    },
    onError: fail,
  });

  const dueMap = new Map((due.data?.due ?? []).map((d) => [d.code, d]));
  const rows = tasks.data?.tasks ?? [];
  const overdueCount = (due.data?.due ?? []).filter((d) => d.overdue).length;

  const onRun = async (t: Task) => {
    if (await confirm({ title: "지금 실행", body: `"${t.name}"을(를) 이번 기간 기준으로 실행합니다.`,
      confirmLabel: "실행" })) run.mutate(t.code);
  };
  const onComplete = async (t: Task) => {
    const note = await ask({ title: "완료 처리", label: `"${t.name}" 완료 비고(선택)`,
      placeholder: "예: 분기 재고실사 이상 없음" });
    if (note !== null) complete.mutate({ code: t.code, note: String(note) });
  };
  const onOwner = async (t: Task) => {
    const roles = Object.keys(ROLE_KO);
    const v = await ask({
      title: "담당 역할 변경",
      label: `"${t.name}"의 담당 역할`,
      hint: `${roles.join(" / ")} — 보유자 0명이면 알림이 관리자에게만 갑니다. 역할 배정은 시스템 > 사용자.`,
      defaultValue: t.owner_role ?? "", required: true,
      validate: (s) => (roles.includes(s.trim().toUpperCase()) ? null : "위 역할 중 하나를 입력하세요"),
    });
    if (v !== null) setOwner.mutate({ code: t.code, owner_role: String(v).trim().toUpperCase() });
  };

  const busy = run.isPending || complete.isPending || toggle.isPending || setOwner.isPending;

  return (
    <div className="space-y-4">
      <div className="flex items-center justify-between">
        <h1 className="text-lg font-bold">시스템 — 정기업무</h1>
        <div className="flex items-center gap-3 text-sm">
          {overdueCount > 0 && <span>지연 <b className="text-red-600">{overdueCount}</b>건</span>}
          <Button size="sm" isLoading={runDue.isPending} onClick={() => runDue.mutate()}>
            자동 일괄 실행</Button>
        </div>
      </div>
      <p className="text-xs text-slate-500">
        주·월·분기 고정/반복업무를 정의·추적합니다. <b>자동</b>은 배치로 멱등 실행되고, <b>AI보조</b>는
        AI 초안 후 사람이 확정하며, <b>수기</b>는 담당자가 완료 처리하는 체크리스트입니다. 지연(SLA 초과)
        항목은 붉게 표시됩니다.</p>

      <QueryErrorBanner queries={[tasks, due]} />
      <Card title="정기업무">
        <Table headers={["주기", "업무", "모드", "다음 예정", "상태", "담당", ""]}
          rows={rows.map((t) => {
            const d = dueMap.get(t.code);
            const status = t.enabled ? (d?.status ?? "—") : "비활성";
            const canRun = t.execution_mode !== "MANUAL" && t.has_handler && !t.requires_approval;
            return [
              <Badge key="c" tone="gray">{CADENCE_LABEL[t.cadence] ?? t.cadence}</Badge>,
              <span key="n">{t.name}{t.requires_approval &&
                <Badge tone="amber">승인필요</Badge>}</span>,
              <Badge key="m" tone={MODE_TONE[t.execution_mode] ?? "gray"}>
                {MODE_LABEL[t.execution_mode] ?? t.execution_mode}</Badge>,
              d ? `${d.scheduled_for.slice(0, 16).replace("T", " ")} (${d.period_key})` : "—",
              t.enabled
                ? <Badge key="s" tone={d?.overdue ? "red" : (STATUS_TONE[status] ?? "gray")}>
                    {d?.overdue ? "지연" : (STATUS_LABEL[status] ?? status)}</Badge>
                : <Badge key="s" tone="gray">비활성</Badge>,
              // 담당 역할 + 보유자 0명 경고 — 알림이 담당에게 못 가고 관리자 폴백으로만 간다
              <button key="o" type="button" disabled={busy} onClick={() => onOwner(t)}
                className="text-left hover:underline disabled:no-underline">
                {t.owner_role ? (ROLE_KO[t.owner_role] ?? t.owner_role) : "미지정"}
                {t.owner_role && t.owner_recipients === 0 &&
                  <Badge tone="red">수신자 0명</Badge>}
              </button>,
              <span key="a" className="flex justify-end gap-1.5">
                {t.enabled && canRun &&
                  <Button variant="secondary" size="sm" disabled={busy}
                    onClick={() => onRun(t)}>실행</Button>}
                {t.enabled && t.execution_mode === "MANUAL" &&
                  <Button variant="secondary" size="sm" disabled={busy}
                    onClick={() => onComplete(t)}>완료</Button>}
                {/* 승인대기면 남은 일은 결정 대기함에서의 승인이다 — 그 화면으로 가는 경로가
                    없어서 상태만 보고 어디로 가야 하는지 알 수 없었다(초안 준비 버튼은 이미
                    준비된 건에 대해 멱등 no-op). */}
                {t.enabled && t.execution_mode === "AI_ASSISTED" && d?.status === "WAITING_APPROVAL"
                  ? <Link key="dec" href="/admin/decisions"
                      className="text-sm text-sky-700 underline dark:text-sky-400">결정·조치함</Link>
                  : t.enabled && t.execution_mode === "AI_ASSISTED" && t.has_handler &&
                    <Button variant="secondary" size="sm" disabled={busy || prepare.isPending}
                      onClick={() => prepare.mutate(t.code)}>초안 준비</Button>}
                <Button variant="neutral" size="sm" onClick={() => setHistoryOf(t)}>이력</Button>
                <Button variant="neutral" size="sm" disabled={busy}
                  onClick={() => toggle.mutate({ code: t.code, enabled: !t.enabled })}>
                  {t.enabled ? "비활성" : "활성"}</Button>
              </span>,
            ];
          })} empty={tasks.isFetching ? "불러오는 중…" : "등록된 정기업무 없음"} />
      </Card>

      <RegistrySyncCard onSynced={refresh} />

      <Card title="실행 인프라 상태 (worker-batch)" action={<Link href="/admin/batch-jobs" className="text-xs text-blue-600 underline">배치 작업 관리(이력·지금 실행·일시정지) ›</Link>}>
        <p className="mb-2 text-xs text-slate-500">
          정기업무 자동실행 경로의 이상 신호와, 레지스트리 밖 APScheduler 인프라·연동 잡의 마지막 실행
          상태입니다(1분 자동 갱신).</p>
        <QueryErrorBanner queries={[infra]} />
        {infra.data && (
          <div className="mb-2 flex flex-wrap items-center gap-2 text-sm">
            {infra.data.signal.ok
              ? <Badge tone="green">정기업무 정상</Badge>
              : <>
                  {infra.data.signal.failed_recent.map((c) =>
                    <Badge key={`f${c}`} tone="red">실패 {c}</Badge>)}
                  {infra.data.signal.stuck_due.map((c) =>
                    <Badge key={`d${c}`} tone="amber">정체 {c}</Badge>)}
                  {infra.data.signal.stuck_running.map((c) =>
                    <Badge key={`r${c}`} tone="amber">중단의심 {c}</Badge>)}
                  {infra.data.signal.engine_stale && <Badge tone="red">엔진 무실행 26h+</Badge>}
                </>}
            <span className="text-xs text-slate-500">
              워커 하트비트 {infra.data.worker_heartbeat_age_s != null
                ? fmtAge(infra.data.worker_heartbeat_age_s) : "없음"}
              {!infra.data.redis_ok && " · 상태저장소(Redis) 조회 불가 — 잡 목록 생략"}</span>
          </div>
        )}
        <Table headers={["잡", "마지막 실행", "상태", "오류"]}
          rows={(infra.data?.jobs ?? []).map((j) => [
            j.id,
            `${j.last_run_at.slice(0, 16)} (${fmtAge(j.age_s)})`,
            // 기대 주기 대비 지연이면 상태값(ok)보다 지연을 우선 표시 — 죽은 잡의 초록 오독 방지
            j.stale
              ? <Badge key="s" tone="red">지연 · {j.status}</Badge>
              : <Badge key="s" tone={INFRA_TONE[j.status] ?? "gray"}>{j.status}</Badge>,
            <span key="e" className="text-xs text-slate-500">{j.error ?? "—"}</span>,
          ])} empty={infra.isFetching ? "불러오는 중…" : "기록된 잡 상태 없음(워커 기동 직후)"} />
      </Card>

      <Card title="관련 법정기한 (참고)">
        <p className="mb-2 text-xs text-slate-500">
          컴플라이언스 원천에서 가져온 임박·경과 법정기한입니다(정기업무와 별개·읽기전용).
          상세·처리는 <b>컴플라이언스</b> 화면에서.</p>
        <QueryErrorBanner queries={[compliance]} />
        <Table headers={["분류", "항목", "대상", "기한", "잔여"]}
          rows={(compliance.data?.deadlines ?? []).map((c) => [
            c.category,
            c.title,
            c.subject ?? "—",
            <Badge key="b" tone={BAND_TONE[c.band] ?? "gray"}>{c.due_on}</Badge>,
            c.days_remaining < 0 ? `${-c.days_remaining}일 경과` : `D-${c.days_remaining}`,
          ])} empty={compliance.isFetching ? "불러오는 중…" : "임박한 법정기한 없음"} />
      </Card>

      {historyOf && <HistoryModal task={historyOf} onClose={() => setHistoryOf(null)} />}
      {dialog}
    </div>
  );
}

function HistoryModal({ task, onClose }: { task: Task; onClose: () => void }) {
  const runs = useQuery({ queryKey: ["rectask-runs", task.code], queryFn: () =>
    api<{ runs: Run[] }>(`/admin/recurring-tasks/${task.code}/runs`) });
  return (
    <Modal title={`실행 이력 — ${task.name}`} size="lg" onClose={onClose}>
      <QueryErrorBanner queries={[runs]} />
      <Table headers={["기간", "상태", "실행", "소요", "결과·오류"]}
        rows={(runs.data?.runs ?? []).map((r) => [
          r.period_key,
          <Badge key="s" tone={STATUS_TONE[r.status] ?? "gray"}>
            {STATUS_LABEL[r.status] ?? r.status}</Badge>,
          r.triggered_by ?? "—",
          r.duration_ms != null ? `${r.duration_ms}ms` : "—",
          <span key="e" className="text-xs text-slate-500">
            {r.error ?? (r.result ? JSON.stringify(r.result) : "—")}</span>,
        ])} empty={runs.isFetching ? "불러오는 중…" : "실행 이력 없음"} />
    </Modal>
  );
}

const FIELD_LABEL: Record<string, string> = {
  name: "이름", domain: "도메인", cadence: "주기", category: "분류", execution_mode: "실행모드",
  handler_ref: "핸들러", owner_role: "담당 역할", sla_days: "SLA(일)", anchor: "기준일",
};
interface Drift {
  missing: { code: string; name: string }[];
  differs: { code: string; name: string; fields: { field: string; registry: unknown; code: unknown }[] }[];
  extra: { code: string; name: string; enabled: boolean }[];
  kept: string[];
  in_sync: boolean;
}
const show = (v: unknown) => (v == null ? "—" : typeof v === "object" ? JSON.stringify(v) : String(v));

/** 정의 동기화 — 코드가 정의한 정기업무 ↔ 레지스트리 차이. 2026-10-10: 배포 시드가 기존 행을 덮어써 관리자 편집
 *  (담당 역할·SLA 등)이 조용히 되돌아갈 수 있었다 → 시드는 누락분 추가만, 값 차이는 여기서 관리자가 골라 반영. */
function RegistrySyncCard({ onSynced }: { onSynced: () => void }) {
  const qc = useQueryClient();
  const toast = useToast();
  const confirm = useConfirm();
  const drift = useQuery({ queryKey: ["rectasks-drift"], queryFn: () =>
    api<Drift>("/admin/recurring-tasks/registry/drift") });
  const sync = useMutation({
    mutationFn: (codes: string[]) => api<{ applied: string[] }>("/admin/recurring-tasks/registry/sync",
      { method: "POST", body: JSON.stringify({ codes }) }),
    onSuccess: (r) => {
      toast.ok(`${r.applied.length}건 코드 정의로 반영됨`);
      qc.invalidateQueries({ queryKey: ["rectasks-drift"] }); onSynced();
    },
    onError: (e: unknown) => toast.err(e instanceof ApiError ? e.error.message : "처리 실패"),
  });
  const { ask, dialog } = useInputDialog();
  const keep = useMutation({
    mutationFn: (v: { code: string; reason: string }) => api(`/admin/recurring-tasks/registry/${v.code}/keep`,
      { method: "POST", body: JSON.stringify({ reason: v.reason }) }),
    onSuccess: () => { toast.ok("현재 값 유지로 확인됨"); qc.invalidateQueries({ queryKey: ["rectasks-drift"] }); },
    onError: (e: unknown) => toast.err(e instanceof ApiError ? e.error.message : "처리 실패"),
  });
  const d = drift.data;
  const onKeep = async (code: string) => {
    const reason = await ask({ title: `현재 값 유지 — ${code}`, required: true, label: "유지 이유(필수)",
      placeholder: "예: 재무팀 담당으로 이관(운영회의 결정)",
      hint: "이 차이는 목록에서 빠집니다. 코드 정의가 나중에 다시 바뀌면 다시 표시됩니다.",
      validate: (v) => (v.length > 300 ? "300자 이하로 입력하세요" : null), confirmLabel: "유지" });
    if (typeof reason === "string" && reason) keep.mutate({ code, reason });
  };
  const onAdd = async () => {
    if (!d) return;
    const codes = d.missing.map((m) => m.code);
    if (await confirm({ title: "누락 업무 등록",
      body: `코드에 정의됐지만 레지스트리에 없는 ${codes.length}건(${codes.join(", ")})을 등록합니다. 자동 업무는 등록 즉시 활성입니다.`,
      confirmLabel: "등록" })) sync.mutate(codes);
  };
  const onApply = async (code: string, fields: string[]) => {
    if (await confirm({ title: `코드 정의로 맞추기 — ${code}`,
      body: `${fields.join(", ")} 값을 코드 정의로 바꿉니다. 관리자가 의도해 바꾼 값이면 그대로 두세요(활성 여부는 유지·감사 기록).`,
      danger: true, confirmLabel: "코드값 반영" })) sync.mutate([code]);
  };
  return (
    <Card title="정의 동기화 (코드 ↔ 레지스트리)">
      <QueryErrorBanner queries={[drift]} />
      {!d ? <p className="text-sm text-slate-500">불러오는 중…</p> : d.in_sync ? (
        <p className="text-sm text-emerald-700">코드 정의와 레지스트리가 일치합니다.
          {d.extra.length > 0 && <span className="text-slate-500"> (화면에서만 등록된 업무 {d.extra.length}건)</span>}
          {d.kept.length > 0 && <span className="text-slate-500"> (현재 값 유지로 확인한 업무 {d.kept.length}건)</span>}</p>
      ) : (
        <div className="space-y-3">
          <p className="text-xs text-slate-500">
            배포로 코드 정의가 바뀌었거나 관리자가 화면에서 값을 바꾼 경우 차이가 생깁니다. 어느 쪽이 맞는지는 시스템이
            판단할 수 없으니 항목별로 고르세요 — 그대로 두면 현재 레지스트리 값으로 계속 실행됩니다.</p>
          {d.missing.length > 0 && (
            <div className="flex items-center justify-between rounded bg-amber-50 p-2 text-sm">
              <span>레지스트리에 없는 업무 {d.missing.length}건: {d.missing.map((m) => m.name).join(", ")}</span>
              <Button size="sm" disabled={sync.isPending} onClick={onAdd}>등록</Button>
            </div>)}
          {d.differs.length > 0 && (
            <Table headers={["업무", "항목", "현재(레지스트리)", "코드 정의", ""]}
              rows={d.differs.map((x) => [
                <span key="n"><b>{x.name}</b> <span className="font-mono text-xs text-slate-500">{x.code}</span></span>,
                x.fields.map((f) => FIELD_LABEL[f.field] ?? f.field).join(", "),
                <span key="r" className="text-xs">{x.fields.map((f) => show(f.registry)).join(" · ")}</span>,
                <span key="c" className="text-xs">{x.fields.map((f) => show(f.code)).join(" · ")}</span>,
                <div key="a" className="flex gap-1">
                  <Button size="sm" variant="secondary" disabled={keep.isPending}
                    onClick={() => onKeep(x.code)}>현재 값 유지</Button>
                  <Button size="sm" variant="warning" disabled={sync.isPending}
                    onClick={() => onApply(x.code, x.fields.map((f) => FIELD_LABEL[f.field] ?? f.field))}>
                    코드값 반영</Button>
                </div>,
              ])} />)}
        </div>)}
      {dialog}
    </Card>
  );
}

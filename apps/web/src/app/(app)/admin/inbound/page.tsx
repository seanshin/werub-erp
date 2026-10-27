"use client";
/** 시스템 — 연동 인박스(HIS 웹훅 his_inbound_events) 정체건 조치.
 *  2026-10-09 시나리오 3회차: 재심사 추론 정정 가드·정정 충돌로 DEAD가 된 이벤트는 재처리해도 같은 가드에
 *  다시 걸린다 — 원장 대조를 마친 관리자가 사유를 남기고 '처리 완료'로 종결하는 출구(RESOLVED·감사 기록).
 *  2026-10-09 시나리오 5회차: 삭감이 줄어든 재심사(claim.reviewed·revision 없음)는 ERP 이의 인정 복원과 겹칠 수
 *  있어 자동 반영하지 않고 DEAD로 보류된다 — 이의 결과와 대조해 미반영이면 '확인 후 반영'(사유·감사).
 *  2026-10-10 prod DEAD 13건: 참조·오류만으로는 판단 근거가 없었다 — 조치 안내(서버 hint)·상세(마스킹 payload)·
 *  외화 라인 '환율 지정 후 반영'(근거·감사)·테스트 이벤트 여러 건 한 번에 종결.
 *  조회=admin.staging read, 조치=admin.staging write(관리자). */
import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { ApiError, api } from "@/lib/api";
import { Badge, Button, Card, Modal, Table } from "@/components/ui";
import { QueryErrorBanner } from "@/components/query-guard";
import { useConfirm } from "@/components/confirm";
import { useInputDialog } from "@/components/input-dialog";
import { useToast } from "@/components/toast";

const TABS = [
  { key: "FAILED", label: "재시도 중(FAILED)", tone: "amber" as const },
  { key: "DEAD", label: "처리 실패(DEAD)", tone: "red" as const },
  { key: "RESOLVED", label: "처리 완료(RESOLVED)", tone: "green" as const },
];

type HintKind = "RESTORE_HOLD" | "FX_HOLD" | "TEST" | "ERP_SETUP" | "STAFF_UNKNOWN" | "UNSUPPORTED"
  | "SENDER_FIX" | "OTHER";
const HINT_LABEL: Record<HintKind, { label: string; tone: "red" | "amber" | "gray" | "blue" | "green" }> = {
  RESTORE_HOLD: { label: "심사팀 대조", tone: "amber" },
  FX_HOLD: { label: "환율 필요", tone: "amber" },
  TEST: { label: "테스트 추정", tone: "gray" },
  ERP_SETUP: { label: "ERP 설정", tone: "blue" },
  STAFF_UNKNOWN: { label: "직원 미상", tone: "blue" },
  UNSUPPORTED: { label: "미지원 유형", tone: "gray" },
  SENDER_FIX: { label: "HIS 보완", tone: "red" },
  OTHER: { label: "확인 필요", tone: "red" },
};

interface InboundEvent {
  id: number; domain: string; event_type: string; source_ref: string; status: string;
  retry_count: number; next_retry_at: string | null; error: string | null; received_at: string;
  resolved_by: number | null; resolved_at: string | null; resolve_reason: string | null;
  hint: { kind: HintKind; text: string } | null;
}
interface InboundDetail extends InboundEvent { payload: Record<string, unknown> }

const ts = (s: string | null) => (s ? s.slice(0, 16).replace("T", " ") : "—");
const reasonOk = (v: string) => (v.length > 300 ? "300자 이하로 입력하세요" : null);

export default function InboundPage() {
  const qc = useQueryClient();
  const toast = useToast();
  const confirm = useConfirm();
  const { ask, dialog } = useInputDialog();
  const fail = (e: unknown) => toast.err(e instanceof ApiError ? e.error.message : "처리 실패");
  const [tab, setTab] = useState("DEAD");
  const [picked, setPicked] = useState<number[]>([]);
  const [detailId, setDetailId] = useState<number | null>(null);

  const list = useQuery({ queryKey: ["inbound", tab], queryFn: () =>
    api<{ count: number; items: InboundEvent[] }>(`/integrations/his/inbound?status=${tab}&limit=200`) });
  const detail = useQuery({ queryKey: ["inbound-detail", detailId], enabled: detailId != null,
    queryFn: () => api<InboundDetail>(`/integrations/his/inbound/${detailId}`) });
  const refresh = () => { setPicked([]); qc.invalidateQueries({ queryKey: ["inbound"] }); };

  const post = (path: string, body?: unknown) =>
    api(`/integrations/his/inbound/${path}`, { method: "POST", body: body ? JSON.stringify(body) : undefined });
  const requeue = useMutation({
    mutationFn: (id: number) => post(`${id}/requeue`),
    onSuccess: () => { toast.ok("재처리 요청됨 — 잠시 후 결과를 확인하세요"); refresh(); },
    onError: fail,
  });
  const resolve = useMutation({
    mutationFn: (v: { id: number; reason: string }) => post(`${v.id}/resolve`, { reason: v.reason }),
    onSuccess: () => { toast.ok("처리 완료로 종결됨"); refresh(); },
    onError: fail,
  });
  const resolveBulk = useMutation({
    mutationFn: (v: { ids: number[]; reason: string }) => post("resolve-bulk", v),
    onSuccess: (_d, v) => { toast.ok(`${v.ids.length}건 처리 완료로 종결됨`); refresh(); },
    onError: fail,
  });
  const applyConfirmed = useMutation({
    mutationFn: (v: { id: number; reason: string }) => post(`${v.id}/apply-confirmed`, { reason: v.reason }),
    onSuccess: () => { toast.ok("확인 후 반영 요청됨 — 잠시 후 '처리 실패' 목록에서 사라졌는지 확인하세요"); refresh(); },
    onError: fail,
  });
  const applyFx = useMutation({
    mutationFn: (v: { id: number; fx_rate: number; reason: string }) =>
      post(`${v.id}/apply-fx`, { fx_rate: v.fx_rate, reason: v.reason }),
    onSuccess: () => { toast.ok("환율 지정 후 반영 요청됨 — 잠시 후 '처리 실패' 목록에서 사라졌는지 확인하세요"); refresh(); },
    onError: fail,
  });

  const onRequeue = async (e: InboundEvent) => {
    if (await confirm({ title: "재처리",
      body: `#${e.id} ${e.event_type}를 다시 처리합니다. 사유가 해소되지 않았으면 같은 오류로 다시 실패합니다.`,
      confirmLabel: "재처리" })) requeue.mutate(e.id);
  };
  const askReason = (title: string, placeholder: string, hint: string, label = "처리 사유(필수)") =>
    ask({ title, required: true, label, placeholder, hint, validate: reasonOk, confirmLabel: "다음" });

  const onResolve = async (e: InboundEvent) => {
    const reason = await askReason(`처리 완료 — #${e.id} ${e.event_type}`,
      "예: 심사팀 대조 — 이의 인정분이 이미 반영됨(원장 확인)",
      "전표·도메인 데이터는 바뀌지 않습니다. 원장 대조(필요하면 역분개)를 먼저 마치세요.");
    if (typeof reason !== "string" || !reason) return;
    if (await confirm({ title: "처리 완료로 종결",
      body: `#${e.id}를 RESOLVED로 종결합니다(오류 내용은 보존·감사 기록). 종결 후에는 재처리할 수 없습니다.`,
      danger: true, confirmLabel: "처리 완료" })) resolve.mutate({ id: e.id, reason });
  };
  const onResolveBulk = async () => {
    const chosen = items.filter((e) => e.status === "DEAD" && picked.includes(e.id));
    const ids = chosen.map((e) => e.id);
    if (ids.length === 0) return;
    const reason = await askReason(`선택한 ${ids.length}건 처리 완료`,
      "예: 연동 점검용 테스트 이벤트 — 실제 거래 아님(HIS 확인)",
      "선택한 건 모두 같은 사유로 종결합니다(건별 감사 기록). 하나라도 DEAD가 아니면 아무것도 종결하지 않습니다.");
    if (typeof reason !== "string" || !reason) return;
    if (await confirm({ title: `${ids.length}건 처리 완료로 종결`,
      body: `다음 ${ids.length}건을 RESOLVED로 종결합니다. 종결 후에는 재처리할 수 없습니다.\n`
        + chosen.map((e) => `#${e.id} ${e.event_type} · ${e.source_ref}`).join("\n"),
      danger: true, confirmLabel: "일괄 종결" })) resolveBulk.mutate({ ids, reason });
  };
  const onApplyConfirmed = async (e: InboundEvent) => {
    const reason = await askReason(`확인 후 반영 — #${e.id} ${e.event_type}`,
      "예: 이의신청 결과와 대조 — 이 청구의 복원 전표 없음(미반영 확인)",
      "이의신청 인정으로 이미 복원됐다면 반영하지 말고 '처리 완료'로 종결하세요(이중 복원 방지).",
      "대조 결과(필수)");
    if (typeof reason !== "string" || !reason) return;
    if (await confirm({ title: "확인 후 반영",
      body: `#${e.id} 재심사의 삭감 감소(복원)를 회계에 반영합니다(삭감손실 역분개·감사 기록). 이의신청 인정분과 겹치지 않는지 확인했을 때만 진행하세요.`,
      danger: true, confirmLabel: "확인 후 반영" })) applyConfirmed.mutate({ id: e.id, reason });
  };
  const onApplyFx = async (e: InboundEvent) => {
    const cur = /외화\(([A-Z]{3})\)/.exec(e.error ?? "")?.[1] ?? "외화";
    const rate = await ask({ title: `환율 지정 — #${e.id} ${e.event_type}`, kind: "number", min: 0.0001,
      max: 100000, label: `1 ${cur}당 원화(KRW) 환율`, placeholder: "예: 1385.20",
      hint: "거래일(전표일) 기준 고시 환율을 넣으세요. 금액 × 환율로 원화 전표가 만들어지고, 외화 자금은 통화별 법인계좌로 갑니다.",
      confirmLabel: "다음" });
    if (typeof rate !== "number") return;
    const reason = await askReason(`환율 근거 — #${e.id}`, "예: 서울외국환중개 2026-06-18 매매기준율",
      "고시 기관·일자를 적어 주세요(감사 기록·전표 메모에 남습니다).", "환율 근거(필수)");
    if (typeof reason !== "string" || !reason) return;
    if (await confirm({ title: "환율 지정 후 반영",
      body: `#${e.id}를 1 ${cur} = ${rate.toLocaleString()}원으로 환산해 전기합니다(근거: ${reason}). 전기된 전표를 바꾸려면 역분개가 필요합니다.`,
      danger: true, confirmLabel: "반영" })) applyFx.mutate({ id: e.id, fx_rate: rate, reason });
  };

  const busy = requeue.isPending || resolve.isPending || resolveBulk.isPending
    || applyConfirmed.isPending || applyFx.isPending;
  const items = list.data?.items ?? [];
  const deadIds = items.filter((e) => e.status === "DEAD").map((e) => e.id);
  // 백그라운드 재조회로 목록에서 빠진 건은 선택에서도 뺀다(개수 표시·일괄 요청이 어긋나지 않게)
  const pickedLive = picked.filter((id) => deadIds.includes(id));
  const toggle = (id: number) =>
    setPicked((p) => (p.includes(id) ? p.filter((x) => x !== id) : [...p, id]));
  const pickTests = () => setPicked(items.filter((e) => e.status === "DEAD" && e.hint?.kind === "TEST")
    .map((e) => e.id));

  return (
    <div className="space-y-4">
      <h1 className="text-lg font-bold">시스템 — 연동 인박스</h1>
      <p className="text-xs text-slate-500">
        HIS에서 받은 이벤트 중 처리되지 못한 건입니다. <b>재시도 중</b>은 자동으로 다시 시도됩니다.
        <b> 처리 실패</b>는 <b>조치</b> 열의 안내를 따르세요 — 원인이 풀렸으면 <b>재처리</b>, 외화 라인은 근거 환율로
        <b> 환율 지정 후 반영</b>, 삭감이 줄어든 재심사는 이의신청 결과와 대조해 <b>확인 후 반영</b>, 재처리로 풀 수 없거나
        실제 거래가 아닌 건은 사유를 남기고 <b>처리 완료</b>로 종결합니다. 판단이 어려우면 <b>상세</b>에서 내용을 확인하세요.</p>

      <div className="flex gap-2">
        {TABS.map((t) => (
          <Button key={t.key} size="sm" variant={tab === t.key ? "primary" : "secondary"}
            onClick={() => { setTab(t.key); setPicked([]); }}>{t.label}</Button>
        ))}
      </div>

      <QueryErrorBanner queries={[list]} />
      <Card title={`${TABS.find((t) => t.key === tab)?.label ?? tab} ${items.length}건`}
        action={tab === "DEAD" && deadIds.length > 0 ? (
          <div className="flex gap-1">
            <Button size="sm" variant="secondary" disabled={busy} onClick={pickTests}>테스트 추정 선택</Button>
            <Button size="sm" variant="warning" disabled={busy || pickedLive.length === 0}
              onClick={onResolveBulk}>선택 {pickedLive.length}건 처리 완료</Button>
          </div>) : undefined}>
        <Table
          headers={tab === "RESOLVED"
            ? ["ID", "이벤트", "참조", "오류(보존)", "수신", "종결", "사유"]
            : ["", "ID", "이벤트", "참조", "재시도", "오류", "조치", "수신", ""]}
          rows={items.map((e) => tab === "RESOLVED" ? [
            e.id,
            <span key="t" className="font-mono text-xs">{e.event_type}</span>,
            <span key="r" className="break-all font-mono text-xs">{e.source_ref}</span>,
            <span key="e" className="text-xs text-slate-600">{e.error ?? "—"}</span>,
            ts(e.received_at),
            <span key="d" className="text-xs">{ts(e.resolved_at)}{e.resolved_by ? ` · #${e.resolved_by}` : ""}</span>,
            <span key="s" className="text-xs">{e.resolve_reason ?? "—"}</span>,
          ] : [
            e.status === "DEAD" ? (
              <input key="p" type="checkbox" aria-label={`#${e.id} 선택`} checked={picked.includes(e.id)}
                onChange={() => toggle(e.id)} />) : "",
            e.id,
            <span key="t" className="font-mono text-xs">{e.event_type}</span>,
            <span key="r" className="break-all font-mono text-xs">{e.source_ref}</span>,
            <Badge key="c" tone={TABS.find((t) => t.key === e.status)?.tone ?? "gray"}>{e.retry_count}</Badge>,
            <span key="e" className="text-xs text-slate-600">{e.error ?? "—"}</span>,
            e.hint ? (
              <div key="h" className="space-y-1">
                <Badge tone={HINT_LABEL[e.hint.kind]?.tone ?? "gray"}>{HINT_LABEL[e.hint.kind]?.label ?? e.hint.kind}</Badge>
                <div className="text-xs text-slate-500">{e.hint.text}</div>
              </div>) : "—",
            ts(e.received_at),
            <div key="a" className="flex flex-wrap gap-1">
              <Button size="sm" variant="secondary" onClick={() => setDetailId(e.id)}>상세</Button>
              <Button size="sm" variant="secondary" disabled={busy}
                onClick={() => onRequeue(e)}>재처리</Button>
              {e.status === "DEAD" && e.hint?.kind === "RESTORE_HOLD" && (
                <Button size="sm" variant="primary" disabled={busy}
                  onClick={() => onApplyConfirmed(e)}>확인 후 반영</Button>)}
              {e.status === "DEAD" && e.hint?.kind === "FX_HOLD" && (
                <Button size="sm" variant="primary" disabled={busy}
                  onClick={() => onApplyFx(e)}>환율 지정 후 반영</Button>)}
              {e.status === "DEAD" && (
                <Button size="sm" variant="warning" disabled={busy}
                  onClick={() => onResolve(e)}>처리 완료</Button>)}
            </div>,
          ])}
          empty={list.isFetching ? "불러오는 중…" : "해당 상태의 이벤트 없음"} />
      </Card>

      {detailId != null && (
        <Modal title={`이벤트 상세 — #${detailId}`} size="lg" onClose={() => setDetailId(null)}>
          <QueryErrorBanner queries={[detail]} />
          {detail.data ? (
            <div className="space-y-2 text-sm">
              <div><span className="text-slate-500">유형</span> <span className="font-mono">{detail.data.event_type}</span>
                {" · "}<span className="text-slate-500">참조</span> <span className="break-all font-mono text-xs">{detail.data.source_ref}</span></div>
              <div><span className="text-slate-500">수신</span> {ts(detail.data.received_at)}
                {" · "}<span className="text-slate-500">상태</span> {detail.data.status}</div>
              {detail.data.error && <div className="text-xs text-red-700">{detail.data.error}</div>}
              {detail.data.hint && <div className="text-xs text-slate-600">조치: {detail.data.hint.text}</div>}
              <div className="text-xs text-slate-500">내용 — 판단에 필요한 값(금액·통화·유형·상태·일자·업무 참조)만 보이고, 나머지 값은 길이만 표시합니다(개인정보 보호)</div>
              <pre className="max-h-96 overflow-auto rounded bg-slate-50 p-2 text-xs">
                {JSON.stringify(detail.data.payload, null, 2)}</pre>
            </div>) : <div className="text-sm text-slate-500">불러오는 중…</div>}
        </Modal>)}
      {dialog}
    </div>
  );
}

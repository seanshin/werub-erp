"use client";
/** 시스템 — 결정·조치함: 사람 몫의 일을 분류별로 한곳에(2026-10-11).
 *  ① 결정(0226) — 선택지·권고안 → 결정 기록(감사추적)·보류·재오픈. 선택=실행인 결정은 실행 화면으로.
 *  ② 화면 조치 — 결정이 아니라 다른 화면에서 처리할 일(간이세액표 활성화·공휴일·수신함 DEAD 등). 서버가 현재 데이터로
 *     계산하므로 처리하면 사라진다. read/write=관리자·원장(admin.decisions). */
import { useState } from "react";
import Link from "next/link";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { ApiError, api } from "@/lib/api";
import { Badge, Button, Card, Input, Modal, Table } from "@/components/ui";
import { QueryErrorBanner } from "@/components/query-guard";
import { useConfirm } from "@/components/confirm";
import { useInputDialog } from "@/components/input-dialog";
import { useToast } from "@/components/toast";

const STATUS_TONE: Record<string, "amber" | "green" | "gray"> = {
  PENDING: "amber", DECIDED: "green", DEFERRED: "gray",
};
const STATUS_LABEL: Record<string, string> = {
  PENDING: "결정 대기", DECIDED: "결정됨", DEFERRED: "보류",
};
const VIEW_LABEL: Record<string, string> = { OPEN: "미결(대기·보류)", ALL: "전체", DECIDED: "결정됨" };
// 정기업무 AI 초안(rectask:<code>:<기간>) — 사람의 정책 결정과 섞이면 진짜 결정이 묻힌다(2026-10-10 prod 37건 중 12건)
const isDraft = (d: { key: string }) => d.key.startsWith("rectask:");
const draftTask = (key: string) => key.split(":")[1] ?? key;

interface Option { key: string; label: string; description?: string; recommended?: boolean }
interface Decision {
  id: number; key: string; category: string; title: string; question: string;
  options: Option[]; context_ref: string | null; status: string;
  decided_option: string | null; decided_note: string | null;
  decided_by: number | null; decided_at: string | null; created_at: string;
  execute_screen?: string | null;   // 선택=실행인 결정 — 여기서 결정하지 않고 실행 화면으로
}
interface Category { key: string; label: string }
interface ScreenAction {
  key: string; category: string; title: string; detail: string;
  screen: string; screen_label: string; count: number | null; resolver_role?: string;
}
interface ActionsResp { categories: Category[]; actions: ScreenAction[]; errors: { probe: string; error: string }[] }

export default function DecisionsPage() {
  const qc = useQueryClient();
  const toast = useToast();
  const confirm = useConfirm();
  const fail = (e: unknown) => toast.err(e instanceof ApiError ? e.error.message : "처리 실패");
  const [view, setView] = useState("OPEN");
  const [cat, setCat] = useState("");          // "" = 전체 분류
  const [detail, setDetail] = useState<Decision | null>(null);

  const list = useQuery({ queryKey: ["decisions"], queryFn: () =>
    api<{ items: Decision[] }>("/admin/decisions") });
  const acts = useQuery({ queryKey: ["decisions", "actions"], queryFn: () =>
    api<ActionsResp>("/admin/decisions/actions") });
  const refresh = () => qc.invalidateQueries({ queryKey: ["decisions"] });

  const act = useMutation({
    mutationFn: (v: { id: number; action: string; body: unknown }) =>
      api<Decision>(`/admin/decisions/${v.id}/${v.action}`, { method: "POST",
        body: JSON.stringify(v.body) }),
    onSuccess: (d, v) => {
      toast.ok({ decide: "결정 완료", defer: "보류", reopen: "재오픈", category: "분류 변경" }[v.action] ?? "완료");
      setDetail(d); refresh();
    },
    onError: fail,
  });

  // 갱신(PATCH) — 상황이 변하면 결정문도 낡는다. 결정된 항목은 서버가 거부(결정 근거 불변).
  const edit = useMutation({
    mutationFn: (v: { id: number; body: unknown }) =>
      api<Decision>(`/admin/decisions/${v.id}`, { method: "PATCH",
        body: JSON.stringify(v.body) }),
    onSuccess: (d) => { toast.ok("결정문 갱신됨"); setDetail(d); refresh(); },
    onError: fail,
  });

  const items = list.data?.items ?? [];
  const decisions = items.filter((d) => !isDraft(d) || d.status !== "PENDING");   // 결정된 초안은 이력으로
  const drafts = items.filter((d) => isDraft(d) && d.status === "PENDING");
  const pending = decisions.filter((d) => d.status === "PENDING").length;
  const actions = acts.data?.actions ?? [];
  const probeErrors = acts.data?.errors ?? [];

  // 분류 — 서버 단일 목록 순서 + 목록에 없는 옛 분류는 뒤에(원문 표기)
  const known = acts.data?.categories ?? [];
  const extra = [...new Set(decisions.map((d) => d.category))].filter((k) => !known.some((c) => c.key === k))
    .map((k) => ({ key: k, label: k }));
  const categories: Category[] = [...known, ...extra];
  const catLabel = (k: string) => categories.find((c) => c.key === k)?.label ?? k;
  const inView = (d: Decision) => view === "ALL" || (view === "DECIDED" ? d.status === "DECIDED" : d.status !== "DECIDED");
  const shown = decisions.filter(inView);
  // 칩·합계는 보기(상태) 필터와 무관하게 — 보기를 바꿔도 분류가 사라지거나 미결 수가 바뀌지 않는다
  const groups = categories.map((c) => ({
    ...c,
    actions: actions.filter((a) => a.category === c.key),
    decisions: shown.filter((d) => d.category === c.key),
    open: decisions.filter((d) => d.category === c.key && d.status !== "DECIDED").length
      + actions.filter((a) => a.category === c.key).length,
    total: decisions.filter((d) => d.category === c.key).length + actions.filter((a) => a.category === c.key).length,
  })).filter((g) => g.total > 0);
  // 고른 분류가 비면(분류 변경으로 마지막 항목이 옮겨 감 등) 전체로 — 강조된 칩 없이 빈 화면이 되지 않게
  const curCat = groups.some((g) => g.key === cat) ? cat : "";
  const visible = groups.filter((g) => (!curCat || g.key === curCat) && g.actions.length + g.decisions.length > 0);
  const shownDrafts = drafts.filter((d) => !curCat || d.category === curCat);

  const { ask, dialog } = useInputDialog();
  const [picked, setPicked] = useState<number[]>([]);
  const pickedLive = picked.filter((id) => shownDrafts.some((d) => d.id === id));
  // 업무별 최신 기간 초안만 남기고 이전 기간 선택 — 키의 기간 문자열은 같은 업무 안에서 사전순=시간순(2026-W31 < 2026-W41)
  const pickOlder = () => {
    const latest = new Map<string, string>();
    for (const d of drafts) {
      const t = draftTask(d.key);
      if (!latest.has(t) || d.key > latest.get(t)!) latest.set(t, d.key);
    }
    // 서버 한 번 요청 상한(100) — 넘으면 앞 100건만(나머지는 다시 선택)
    setPicked(shownDrafts.filter((d) => latest.get(draftTask(d.key)) !== d.key).map((d) => d.id).slice(0, 100));
  };
  const bulkSkip = useMutation({
    mutationFn: (v: { ids: number[]; note: string }) =>
      api<{ skipped: string[] }>("/admin/decisions/bulk-skip", { method: "POST", body: JSON.stringify(v) }),
    onSuccess: (r) => { toast.ok(`${r.skipped.length}건 생략(반려) 처리됨`); setPicked([]); refresh(); },
    onError: fail,
  });
  const onBulkSkip = async () => {
    const chosen = shownDrafts.filter((d) => pickedLive.includes(d.id));
    if (chosen.length > 100) { toast.err("한 번에 100건까지 생략할 수 있습니다 — 나눠서 처리하세요"); return; }
    const note = await ask({ title: `AI 초안 ${chosen.length}건 생략`, required: true, label: "생략 사유(필수)",
      placeholder: "예: 이후 주차 초안으로 대체 — 시점 점검이라 이전 주차 검토 불요",
      hint: "반려(생략)로 기록됩니다. 승인은 검토 완료를 뜻하므로 건별로만 합니다.",
      validate: (v) => (v.length > 500 ? "500자 이하로 입력하세요" : null), confirmLabel: "다음" });
    if (typeof note !== "string" || !note) return;
    if (await confirm({ title: `${chosen.length}건 생략`, danger: true, confirmLabel: "생략",
      body: "다음 초안을 반려(생략)로 결정합니다. 재오픈 전까지 바꿀 수 없습니다.\n"
        + chosen.map((d) => `· ${d.title}`).join("\n") })) bulkSkip.mutate({ ids: chosen.map((d) => d.id), note });
  };

  const chip = (key: string, label: string, n: number) => (
    <button key={key || "all"} type="button" onClick={() => setCat(key)}
      className={`rounded-full border px-3 py-1 text-xs ${curCat === key
        ? "border-blue-500 bg-blue-50 text-blue-700" : "border-slate-300 text-slate-600"}`}>
      {label} {n > 0 && <b className="text-amber-600">{n}</b>}
    </button>);

  return (
    <div className="space-y-4">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <h1 className="text-lg font-bold">시스템 — 결정·조치함</h1>
        <span className="text-sm text-slate-500">
          결정 대기 <b className="text-amber-600">{pending}</b>건 · 화면 조치 <b className="text-amber-600">{actions.length}</b>건
          {drafts.length > 0 && <> · AI 초안 <b className="text-amber-600">{drafts.length}</b>건</>}</span>
      </div>
      <p className="text-xs text-slate-500">
        사람이 정하거나 처리해야 하는 일을 분류별로 모았습니다. <b>화면 조치</b>는 해당 화면에서 처리하면 저절로 사라지고,
        <b> 결정</b>은 선택지와 권고안을 검토해 기록하면 후속 구현의 근거가 되고 감사추적에 남습니다.</p>

      <QueryErrorBanner queries={[list, acts]} />
      {probeErrors.length > 0 && (
        <div className="rounded-md border border-red-200 bg-red-50 p-3 text-xs text-red-700">
          일부 점검을 하지 못했습니다 — 아래 목록에 빠진 조치가 있을 수 있습니다(시스템 관리자 확인).
          {probeErrors.map((e) => <p key={e.probe}>· {e.probe}: {e.error}</p>)}
        </div>)}

      <div className="flex flex-wrap items-center gap-1.5">
        {chip("", "전체", groups.reduce((n, g) => n + g.open, 0))}
        {groups.map((g) => chip(g.key, g.label, g.open))}
        <select value={view} onChange={(e) => setView(e.target.value)} aria-label="결정 상태"
          className="ml-auto rounded-md border border-slate-300 px-2 py-1 text-xs">
          {Object.entries(VIEW_LABEL).map(([k, v]) => <option key={k} value={k}>{v}</option>)}
        </select>
      </div>

      {visible.length === 0 && (
        <Card><p className="text-sm text-slate-500">
          {list.isFetching || acts.isFetching ? "불러오는 중…"
            : groups.length ? "이 보기에 해당하는 결정·조치가 없습니다" : "처리할 결정·조치가 없습니다"}</p></Card>)}
      {visible.map((g) => (
        <Card key={g.key} title={`${g.label}${g.open ? ` — 미결 ${g.open}건` : ""}`}>
          {g.actions.length > 0 && (
            <div className="mb-3 space-y-1.5">
              <p className="text-xs font-medium text-slate-500">화면에서 처리</p>
              {g.actions.map((a) => (
                <div key={a.key} className="flex flex-wrap items-start justify-between gap-2 rounded-md border border-amber-200 bg-amber-50 px-3 py-2">
                  <div className="min-w-0 flex-1 basis-56 text-sm [overflow-wrap:anywhere]">
                    <b>{a.title}</b>{a.resolver_role === "ADMIN" && <> <Badge tone="gray">관리자 처리</Badge></>}
                    <p className="text-xs text-slate-600">{a.detail}</p>
                  </div>
                  <Link href={a.screen}><Button size="sm" variant="secondary">{a.screen_label}</Button></Link>
                </div>))}
            </div>)}
          {g.decisions.length > 0 && (<>
            <p className="mb-1 text-xs font-medium text-slate-500">결정</p>
            <Table headers={["제목", "상태", "결정", "결정일", ""]}
              rows={g.decisions.map((d) => [
                <span key="t">{d.title}{d.execute_screen && d.status !== "DECIDED" &&
                  <> <Badge tone="blue">실행 화면</Badge></>}</span>,
                <Badge key="s" tone={STATUS_TONE[d.status] ?? "gray"}>{STATUS_LABEL[d.status] ?? d.status}</Badge>,
                d.decided_option
                  ? (d.options.find((o) => o.key === d.decided_option)?.label ?? d.decided_option)
                  : "—",
                d.decided_at?.slice(0, 10) ?? "—",
                <Button key="a" variant="secondary" size="sm" onClick={() => setDetail(d)}>보기</Button>,
              ])} />
          </>)}
        </Card>))}

      {shownDrafts.length > 0 && (
        <Card title={`정기업무 AI 초안 검토 대기 ${shownDrafts.length}건`} action={
          <div className="flex gap-1">
            <Button size="sm" variant="secondary" onClick={pickOlder}>업무별 이전 기간 선택</Button>
            <Button size="sm" variant="warning" disabled={bulkSkip.isPending || pickedLive.length === 0}
              onClick={onBulkSkip}>선택 {pickedLive.length}건 생략</Button>
          </div>}>
          <p className="mb-2 text-xs text-slate-500">
            정기업무가 만든 초안입니다(사람의 정책 결정과 분리). 내용을 보고 <b>승인</b>(검토 완료)은 건별로, 다음 기간 초안으로
            대체된 시점 점검은 골라서 <b>생략</b>하세요. 결정은 다음 정기업무 실행 때 업무 이력에 반영됩니다.</p>
          <Table headers={["", "초안", "생성", ""]}
            rows={shownDrafts.map((d) => [
              <input key="p" type="checkbox" aria-label={`${d.title} 선택`} checked={picked.includes(d.id)}
                onChange={() => setPicked((p) => (p.includes(d.id) ? p.filter((x) => x !== d.id) : [...p, d.id]))} />,
              d.title,
              d.created_at?.slice(0, 10) ?? "—",
              <Button key="a" variant="secondary" size="sm" onClick={() => setDetail(d)}>보기</Button>,
            ])} />
        </Card>)}
      {dialog}

      {detail && (
        <DecisionModal d={detail} isLoading={act.isPending} confirm={confirm}
          categories={categories} catLabel={catLabel}
          onAct={(action, body) => act.mutate({ id: detail.id, action, body })}
          onEdit={(body) => edit.mutate({ id: detail.id, body })}
          onClose={() => setDetail(null)} />
      )}
    </div>
  );
}

function DecisionModal({ d, isLoading, confirm, categories, catLabel, onAct, onEdit, onClose }: {
  d: Decision; isLoading: boolean; categories: Category[]; catLabel: (k: string) => string;
  confirm: ReturnType<typeof useConfirm>;
  onAct: (action: string, body: unknown) => void;
  onEdit: (body: unknown) => void; onClose: () => void;
}) {
  const [picked, setPicked] = useState<string | null>(d.decided_option);
  const [note, setNote] = useState("");
  const [editing, setEditing] = useState(false);
  const [q, setQ] = useState(d.question);
  const [optJson, setOptJson] = useState(() => JSON.stringify(d.options, null, 2));
  const [optErr, setOptErr] = useState("");

  const onDecide = async () => {
    if (!picked) return;
    const opt = d.options.find((o) => o.key === picked);
    if (await confirm({ title: "결정 확정",
      body: `"${opt?.label}"(으)로 결정합니다. 재오픈 전까지 변경할 수 없습니다.`,
      confirmLabel: "결정" })) onAct("decide", { option: picked, note: note || null });
  };
  const onReopen = async () => {
    if (!note.trim()) return;
    if (await confirm({ title: "재오픈", body: "결정을 취소하고 대기 상태로 되돌립니다.",
      danger: true, confirmLabel: "재오픈" })) onAct("reopen", { note });
  };
  const onSaveEdit = () => {
    let options: Option[];
    try {
      options = JSON.parse(optJson);
    } catch {
      setOptErr("JSON 형식이 아닙니다"); return;
    }
    if (!Array.isArray(options) || !options.length || !options.every((o) => o.key && o.label)) {
      setOptErr("[{key,label,description?,recommended?}] 1개 이상이어야 합니다"); return;
    }
    setOptErr("");
    onEdit({ question: q, options });
    setEditing(false);
  };

  return (
    <Modal title={d.title} size="lg" onClose={onClose}>
      <div className="space-y-3">
        <div className="flex flex-wrap items-center gap-2 text-sm">
          <Badge tone={STATUS_TONE[d.status] ?? "gray"}>{STATUS_LABEL[d.status] ?? d.status}</Badge>
          {/* 분류 변경 — 정리 목적이라 결정된 항목도 가능(질문·선택지·결정은 그대로) */}
          <select value={d.category} aria-label="분류" disabled={isLoading}
            onChange={(e) => onAct("category", { category: e.target.value })}
            className="rounded-md border border-slate-300 px-2 py-0.5 text-xs">
            {categories.map((c) => <option key={c.key} value={c.key}>{c.label}</option>)}
            {!categories.some((c) => c.key === d.category) && <option value={d.category}>{catLabel(d.category)}</option>}
          </select>
          {d.context_ref && <span className="text-xs text-slate-400">{d.context_ref}</span>}
        </div>
        {editing ? (
          /* 상황이 변해 결정문이 낡았을 때 갱신 — 결정된 항목은 서버가 거부한다(결정 근거 불변) */
          <div className="space-y-2 rounded-md border border-amber-300 bg-amber-50 p-3">
            <p className="text-xs text-amber-800">
              구현·정책 변화로 낡은 질문·선택지를 고칩니다. 갱신 전/후는 감사로그에 남습니다.</p>
            <label className="block text-sm text-slate-600">질문
              <textarea value={q} onChange={(e) => setQ(e.target.value)} rows={5}
                className="w-full rounded-md border border-slate-300 px-2 py-1.5 text-sm" />
            </label>
            <label className="block text-sm text-slate-600">선택지(JSON)
              <textarea value={optJson} onChange={(e) => setOptJson(e.target.value)} rows={8}
                className="w-full rounded-md border border-slate-300 px-2 py-1.5 font-mono text-xs" />
            </label>
            {optErr && <p className="text-xs text-red-600">{optErr}</p>}
            <div className="flex gap-2">
              <Button size="sm" disabled={isLoading || !q.trim()} onClick={onSaveEdit}>갱신</Button>
              <Button variant="secondary" size="sm" onClick={() => {
                setEditing(false); setQ(d.question);
                setOptJson(JSON.stringify(d.options, null, 2)); setOptErr("");
              }}>취소</Button>
            </div>
          </div>
        ) : (
          <p className="whitespace-pre-wrap rounded-md bg-slate-50 p-3 text-sm text-slate-700">{d.question}</p>
        )}

        <div className="space-y-1.5">
          <div className="flex items-center justify-between">
            <p className="text-sm font-medium text-slate-600">선택지</p>
            {d.status !== "DECIDED" && !editing &&
              <Button variant="neutral" size="sm" onClick={() => setEditing(true)}>
                질문·선택지 갱신</Button>}
          </div>
          {d.options.map((o) => {
            const isDecided = d.status === "DECIDED";
            const chosen = d.decided_option === o.key;
            return (
              <label key={o.key}
                className={`flex cursor-pointer gap-2 rounded-md border px-3 py-2 text-sm ${
                  picked === o.key ? "border-blue-500 bg-blue-50" : "border-slate-200"} ${
                  isDecided && !chosen ? "opacity-50" : ""}`}>
                <input type="radio" name="opt" className="mt-1" checked={picked === o.key}
                  disabled={isDecided} onChange={() => setPicked(o.key)} />
                <span>
                  <b>{o.label}</b>
                  {o.recommended && <Badge tone="green">권장</Badge>}
                  {chosen && <Badge tone="blue">선택됨</Badge>}
                  {o.description && <span className="block text-xs text-slate-500">{o.description}</span>}
                </span>
              </label>
            );
          })}
        </div>

        {d.status === "DECIDED" ? (
          <div className="rounded-md bg-emerald-50 p-3 text-sm">
            <p>결정: <b>{d.options.find((o) => o.key === d.decided_option)?.label}</b>
              {d.decided_at && <span className="text-slate-500"> · {d.decided_at.slice(0, 10)}</span>}</p>
            {d.decided_note && <p className="mt-1 text-slate-600">{d.decided_note}</p>}
            {d.execute_screen ? (
              <p className="mt-2 text-xs text-slate-600">실행과 함께 기록된 결정입니다 — 바꾸려면{" "}
                <Link href={d.execute_screen} className="text-blue-600 underline">실행 화면</Link>에서 취소하세요(취소하면 재오픈).</p>
            ) : (<>
            <Input label="재오픈 사유" value={note} onChange={(e) => setNote(e.target.value)}
              placeholder="조건 변경 등 — 재오픈 시 필수" />
            <Button variant="danger" size="sm" disabled={isLoading || !note.trim()}
              onClick={onReopen}>재오픈</Button>
            </>)}
          </div>
        ) : d.execute_screen ? (
          /* 선택=실행: 결정만 기록되고 실행이 안 되는 상태를 막는다(서버도 decide를 거부) */
          <div className="space-y-2 border-t border-slate-100 pt-2 text-sm">
            <p className="text-slate-600">이 결정은 방식을 고르면 <b>실행과 함께</b> 기록됩니다(미리보기 → 실행).</p>
            <div className="flex gap-2">
              <Link href={d.execute_screen}><Button>실행 화면으로</Button></Link>
              {d.status !== "DEFERRED" && (
                <Button variant="secondary" disabled={isLoading}
                  onClick={() => onAct("defer", { note: note || null })}>보류</Button>)}
            </div>
          </div>
        ) : (
          <div className="space-y-2 border-t border-slate-100 pt-2">
            <Input label="결정 메모(선택)" value={note} onChange={(e) => setNote(e.target.value)}
              placeholder="판단 근거·조건" />
            <div className="flex gap-2">
              <Button disabled={isLoading || !picked} onClick={onDecide}>결정 확정</Button>
              {d.status !== "DEFERRED" && (
                <Button variant="secondary" disabled={isLoading}
                  onClick={() => onAct("defer", { note: note || null })}>보류</Button>)}
            </div>
          </div>
        )}
      </div>
    </Modal>
  );
}

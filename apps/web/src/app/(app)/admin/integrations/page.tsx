"use client";
/** 시스템 — 연동 관리/모니터 (관리자). 설정 여부·상신 on/off·웹훅 인박스 (시크릿 비노출). */
import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { ApiError, api, auth } from "@/lib/api";
import { Badge, Button, Card, Table } from "@/components/ui";
import { useToast } from "@/components/toast";
import { useConfirm } from "@/components/confirm";

interface P0Status { total_mapped: number; pending_unresolved: number; key_configured: boolean;
  cutover_enabled: boolean; cutover_env: boolean; cutover_runtime: boolean }
interface P0Pending { id: number; patient_ref: string; first_event: string | null;
  event_count: number; first_seen: string | null; last_seen: string | null }

interface Integ { name: string; env: string; configured: boolean; enabled?: boolean; detail?: string }
interface Inbox { domain: string; event_type: string; status: string; count: number;
  last_received_at: string | null; expected_hours: number | null; stale: boolean }
interface InboxError { domain: string; status: string; error: string; received_at: string | null }
interface Status { integrations: Integ[]; inbox: Inbox[]; inbox_errors: InboxError[];
  eapproval_linked_leaves: number; egress?: Record<string, string[]> }

interface Tenant { usename: string; connections: number; states: Record<string, number>;
                   max_idle_seconds: number; state_visible: boolean }
interface SharedPg { max_connections: number; total: number; usage_pct: number | null;
                     warning: boolean; by_tenant: Tenant[]; note: string }
interface DbConn { erp_pool: { checked_out: number | null; overflow: number | null;
                               configured: string }; shared_pg: SharedPg | null }

interface SsoPolicy { token_allowed: boolean; token_mode: "allow" | "block" | null;
  enforce_audience: boolean | null; audience: string | null; audience_source: "env" | "db" | null;
  require_jti: boolean; undecided: string[] }

/** HIS SSO 정책 결정 — 코드 기본값으로 정하지 않고 관리자가 결정한다.
 *  공개판: 미결정이면 안전한 쪽(token 차단·aud 강제)으로 동작하고 경고를 띄운다. */
function SsoPolicyCard() {
  const toast = useToast();
  const qc = useQueryClient();
  const confirm = useConfirm();
  const q = useQuery({ queryKey: ["sso-policy"], queryFn: () =>
    api<SsoPolicy>("/admin/integrations/sso-policy") });
  const save = useMutation({
    mutationFn: (body: Partial<Pick<SsoPolicy, "token_mode" | "enforce_audience">>) =>
      api<SsoPolicy>("/admin/integrations/sso-policy", { method: "PUT", body: JSON.stringify(body) }),
    onSuccess: () => { toast.ok("SSO 정책을 저장했습니다");
      qc.invalidateQueries({ queryKey: ["sso-policy"] });
      qc.invalidateQueries({ queryKey: ["integration-status"] }); },
    onError: (e) => toast.err(e instanceof ApiError ? e.error.message : "저장 실패"),
  });
  const p = q.data;
  // 쓰기는 ADMIN만(admin.org write) — DIRECTOR는 열람만. 확인 창 뒤 403을 보지 않게 버튼을 끈다(G12).
  const canWrite = auth.claims()?.role === "ADMIN";
  const onToken = async (mode: "allow" | "block") => {
    const ok = await confirm(mode === "block" ? {
      title: "token 방식 SSO 차단", danger: true, confirmLabel: "차단",
      body: "HIS가 token 방식으로 넘기고 있다면 ERP 버튼 로그인이 즉시 실패합니다. HIS가 2026-09-22 이후 "
        + "버전이면 aud 강제만으로 충분하니 차단할 필요가 없습니다. 구버전 HIS에서 aud 강제를 쓸 수 없을 때만 차단하세요.",
    } : { title: "token 방식 SSO 허용", confirmLabel: "허용",
      body: "token 방식은 HIS 버전에 따라 다릅니다. 2026-09-22 이전 HIS는 메인 로그인 토큰(수 시간 유효·"
        + "반복 수락·허용 역할 게이트 우회)을 보내고, 이후 HIS는 ERP 전용 60초 토큰을 보냅니다. aud 강제를 함께 켜면 "
        + "구버전 메인 토큰은 거부됩니다." });
    if (ok) save.mutate({ token_mode: mode });
  };
  const onAud = async (on: boolean) => {
    const ok = await confirm(on ? {
      title: "aud(erp-sso) 강제", danger: true, confirmLabel: "강제",
      body: "ERP 전용 핸드오프 토큰(aud erp-sso·1회용 jti)만 받습니다. HIS가 2026-09-22 이후 버전이면 cookie·token "
        + "방식 모두 통과하고, 구버전 HIS의 token 방식(메인 토큰)은 거부됩니다 — HIS 버전을 먼저 확인하세요.",
    } : { title: "aud 강제 해제", confirmLabel: "해제",
      body: "HIS 메인 토큰 등 aud가 다른 토큰도 서명만 맞으면 받습니다." });
    if (ok) save.mutate({ enforce_audience: on });
  };
  const undecided = (k: string) => p?.undecided.includes(k);
  return (
    <Card title="HIS SSO 정책" action={p && (p.undecided.length > 0
      ? <Badge tone="amber">결정 필요 {p.undecided.length}</Badge>
      : <Badge tone="green">결정됨</Badge>)}>
      <p className="text-xs text-slate-500">
        HIS 핸드오프는 cookie·token 두 방식입니다. 2026-09-22 이후 HIS는 두 방식 모두 ERP 전용 60초·1회용 토큰을
        보내므로 <b>aud 강제</b>만 켜면 됩니다. 그 이전 HIS의 token 방식은 HIS 메인 로그인 토큰(수 시간 유효·반복 수락·
        역할 게이트 우회)을 URL로 보내므로, aud 강제를 켤 수 없으면 <b>token 차단</b>을 고려하세요.</p>
      <div className="mt-3 grid gap-2 sm:grid-cols-2">
        <div className="rounded-lg border border-slate-200 p-3">
          <p className="text-xs text-slate-500">token 방식</p>
          <p className="text-sm font-bold">
            {p ? (p.token_allowed ? "허용" : "차단") : "—"}
            {undecided("token_mode") && <span className="ml-1 text-xs font-normal text-amber-600">(미결정·기본 차단)</span>}</p>
          <div className="mt-2 flex gap-2">
            <Button size="sm" variant="danger" isLoading={save.isPending}
              disabled={!canWrite || p?.token_mode === "block"} onClick={() => onToken("block")}>차단</Button>
            <Button size="sm" variant="secondary" isLoading={save.isPending}
              disabled={!canWrite || p?.token_mode === "allow"} onClick={() => onToken("allow")}>허용</Button>
          </div>
        </div>
        <div className="rounded-lg border border-slate-200 p-3">
          <p className="text-xs text-slate-500">aud 강제(+jti 필수)</p>
          <p className="text-sm font-bold">
            {p ? (p.audience ? `강제 (${p.audience})` : "미검증") : "—"}
            {p?.audience_source === "env" && <span className="ml-1 text-xs font-normal text-slate-500">env 고정</span>}
            {undecided("enforce_audience") && <span className="ml-1 text-xs font-normal text-amber-600">(미결정·기본 강제)</span>}</p>
          {p?.audience_source === "env" ? (
            <p className="mt-2 text-xs text-slate-400">서버 HIS_JWT_AUDIENCE로 고정 — 해제는 서버 .env에서</p>
          ) : (
            <div className="mt-2 flex gap-2">
              <Button size="sm" variant="danger" isLoading={save.isPending}
                disabled={!canWrite || p?.enforce_audience === true} onClick={() => onAud(true)}>강제</Button>
              <Button size="sm" variant="secondary" isLoading={save.isPending}
                disabled={!canWrite || p?.enforce_audience === false} onClick={() => onAud(false)}>해제</Button>
            </div>
          )}
        </div>
      </div>
      <p className="mt-2 text-xs text-slate-400">
        HIS에서 다른 사용자로 바꿔 ERP 버튼을 누르면 ERP 세션도 그 사용자로 바뀝니다. 핸드오프가 실패하면
        기존 세션을 지우고 로그인 화면으로 보냅니다(공용 PC 보호). 변경은 즉시 적용되며 감사 기록됩니다.
        {!canWrite && " 변경은 관리자(ADMIN)만 할 수 있습니다."}</p>
    </Card>
  );
}

interface MapResult { candidates?: number; matched?: number; mapped?: number;
  failed?: number; unmatched_no_directory?: number; dry_run?: boolean; skipped?: string }

export default function IntegrationsPage() {
  const toast = useToast();
  const qc = useQueryClient();
  const confirm = useConfirm();
  // Phase0 가명화 컷오버 관리 — 대사현황·보류큐·게이트 토글(무재시작)
  const p0 = useQuery({ queryKey: ["phase0-status"], queryFn: () =>
    api<P0Status>("/admin/integrations/patient-ref/status"), refetchInterval: 60_000 });
  const p0pending = useQuery({ queryKey: ["phase0-pending"], queryFn: () =>
    api<{ items: P0Pending[] }>("/admin/integrations/patient-ref/pending") });
  const cutover = useMutation({
    mutationFn: (enabled: boolean) => api<{ cutover_runtime: boolean }>(
      "/admin/integrations/patient-ref/cutover", { method: "POST",
        body: JSON.stringify({ enabled }) }),
    onSuccess: (r) => { toast.ok(r.cutover_runtime ? "컷오버 ON — patientRef 역해소 시작"
      : "컷오버 OFF — 원 chartNo 통과(롤백)");
      qc.invalidateQueries({ queryKey: ["phase0-status"] }); },
    onError: (e) => toast.err(e instanceof ApiError ? e.error.message : "토글 실패") });
  const onCutover = async (enabled: boolean) => {
    const ok = await confirm(enabled ? {
      title: "Phase0 컷오버 ON", danger: true, confirmLabel: "컷오버 실행",
      body: "⚠ 보안·법무 재식별위험 확인이 선행됐는지 확인하세요. ON 시 수신 이벤트의 patientRef를 "
        + "원 chart_no로 역해소하기 시작합니다(HIS 플립과 합의 일시 동시). 미등록 토큰은 보류 큐로.",
    } : { title: "Phase0 컷오버 OFF(롤백)", confirmLabel: "롤백",
      body: "게이트를 내려 원 chartNo를 그대로 통과시킵니다(즉시 롤백)." });
    if (ok) cutover.mutate(enabled);
  };
  const [mapOut, setMapOut] = useState<MapResult | null>(null);
  const empMap = useMutation({
    mutationFn: (dry: boolean) => api<MapResult>(
      `/admin/integrations/esign-emp-map?dry_run=${dry}&signatories_only=true`, { method: "POST" }),
    onSuccess: (r, dry) => { setMapOut(r);
      toast.ok(dry ? `조인 미리보기 — 매칭 ${r.matched ?? 0}건` : `백필 완료 — ${r.mapped ?? 0}건`); },
    onError: (e) => toast.err(e instanceof ApiError ? e.error.message : "실행 실패"),
  });
  const { data, isError } = useQuery({
    queryKey: ["integration-status"],
    queryFn: () => api<Status>("/admin/integrations/status"),
    refetchInterval: 60_000,
  });
  const db = useQuery({
    queryKey: ["db-connections"],
    queryFn: () => api<DbConn>("/admin/diagnostics/db-connections"),
    refetchInterval: 60_000,
  });

  // DEAD=조치 필요(빨강) · RESOLVED=관리자가 사유와 함께 종결(회색) · NEW=대기(주황)
  const inboxTone = (s: string) => s === "FAILED" || s === "DEAD" ? "red"
    : s === "APPLIED" ? "green" : s === "RESOLVED" ? "gray" : "amber";

  return (
    <div className="space-y-4">
      <h1 className="text-lg font-bold">시스템 — 연동 관리</h1>
      {isError && <p className="text-sm text-red-600">연동 상태를 불러오지 못했습니다 (관리자 권한 필요)</p>}

      <Card title="Phase0 가명화 컷오버 관리" action={
        p0.data && (
          <Badge tone={p0.data.cutover_enabled ? "green" : "gray"}>
            게이트 {p0.data.cutover_enabled ? "ON" : "OFF"}</Badge>)}>
        <div className="grid grid-cols-2 gap-2 sm:grid-cols-4">
          <div className="rounded-lg border border-slate-200 p-2">
            <p className="text-xs text-slate-500">대사표 이관</p>
            <p className="text-sm font-bold">{p0.data?.total_mapped ?? "—"}건</p></div>
          <div className="rounded-lg border border-slate-200 p-2">
            <p className="text-xs text-slate-500">미등록 보류</p>
            <p className={`text-sm font-bold ${(p0.data?.pending_unresolved ?? 0) > 0 ? "text-amber-600" : ""}`}>
              {p0.data?.pending_unresolved ?? "—"}건</p></div>
          <div className="rounded-lg border border-slate-200 p-2">
            <p className="text-xs text-slate-500">가명화 키</p>
            <p className="text-sm font-bold">{p0.data?.key_configured ? "주입됨" : "미설정"}</p></div>
          <div className="rounded-lg border border-slate-200 p-2">
            <p className="text-xs text-slate-500">게이트 출처</p>
            <p className="text-xs">{p0.data?.cutover_env ? "env(상시)" : p0.data?.cutover_runtime ? "런타임(UI)" : "OFF"}</p></div>
        </div>
        <p className="mt-2 text-xs text-slate-500">
          ⚠ 컷오버는 <b>보안·법무 재식별위험 확인</b>이 선행되어야 하며, HIS pseudonymizeChartNo 플립과
          합의 일시에 동시 수행합니다. ON=수신 patientRef를 원 chart_no로 역해소(원 MRN egress 제거),
          OFF=즉시 롤백. env(HIS_PSEUDONYM_ENABLED)가 ON이면 UI 토글로 끌 수 없습니다(서버 설정).</p>
        <div className="mt-2 flex gap-2">
          {p0.data?.cutover_env ? (
            <span className="text-xs text-slate-400">env 상시 ON — 롤백은 서버 .env에서</span>
          ) : p0.data?.cutover_runtime ? (
            <Button size="sm" variant="danger" isLoading={cutover.isPending}
              onClick={() => onCutover(false)}>컷오버 OFF(롤백)</Button>
          ) : (
            <Button size="sm" isLoading={cutover.isPending}
              onClick={() => onCutover(true)}>컷오버 ON</Button>
          )}
        </div>
        {(p0pending.data?.items?.length ?? 0) > 0 && (
          <div className="mt-3">
            <p className="mb-1 text-xs font-semibold text-slate-500">미등록 보류 큐(신규환자·증분 대사표 대기)</p>
            <Table headers={["patientRef", "최초 이벤트", "수신횟수", "최초", "최근"]}
              rows={p0pending.data!.items.map((p) => [
                <code key="r" className="text-xs">{p.patient_ref}</code>, p.first_event ?? "—",
                `${p.event_count}회`, (p.first_seen ?? "").slice(0, 10), (p.last_seen ?? "").slice(0, 10)])} />
          </div>
        )}
      </Card>

      <SsoPolicyCard />

      <Card title="연동 상태">
        <Table headers={["연동", "환경변수", "설정", "상신/상세"]}
          rows={(data?.integrations ?? []).map((i) => [
            i.name, <code key="e" className="text-xs text-slate-500">{i.env}</code>,
            i.configured ? <Badge key="c" tone="green">설정됨</Badge>
                         : <Badge key="c" tone="gray">미설정</Badge>,
            <span key="d" className="text-xs text-slate-500">
              {i.enabled !== undefined && (
                <Badge tone={i.enabled ? "blue" : "amber"}>
                  상신 {i.enabled ? "ON" : "OFF"}</Badge>)}
              {i.detail ? ` ${i.detail}` : ""}
            </span>,
          ])} />
        <p className="mt-2 text-xs text-slate-400">
          시크릿 값은 표시하지 않습니다(설정 여부·타입 프리픽스만). 설정은 서버 <code>.env</code>(600).
        </p>
        {data?.egress && (
          <div className="mt-3 border-t border-slate-100 pt-2">
            <p className="mb-1 text-xs font-semibold text-slate-500">아웃바운드 허용 호스트</p>
            <ul className="space-y-0.5 text-xs text-slate-600">
              {Object.entries(data.egress).map(([k, hosts]) => (
                <li key={k}><span className="inline-block w-12 font-semibold uppercase">{k}</span>
                  <code>{hosts.join(", ")}</code></li>))}
            </ul>
            <p className="mt-1 text-xs text-slate-400">
              연동 주소(<code>*_BASE</code>)를 바꿨다면 해당 호스트를 서버 설정 <code>HIS_ALLOWED_HOSTS</code>
              {" "}· <code>CLINIC_ALLOWED_HOSTS</code> · <code>AI_ALLOWED_HOSTS</code>에도 추가해야 호출이 나갑니다
              (주소만 바뀌어 데이터가 새는 것을 막기 위한 이중 확인).</p>
          </div>
        )}
      </Card>

      <div className="grid gap-4 lg:grid-cols-2">
        <Card title="HIS 웹훅 인박스 (his_inbound_events)">
          {/* 건수만 보면 연동이 멎어도 APPLIED 숫자가 그대로라 정상으로 보인다 — 마지막 수신
              시각이 정지를 드러내는 유일한 신호. 단 대부분의 이벤트는 **업무 활동이 있을 때만**
              발생하므로(HIS 확인 2026-07-28) 고정 임계 경보는 오탐이다 → 기대 주기가 계약으로
              정해진 이벤트만 적색 처리하고, 나머지는 시각만 보여 사람이 판단하게 한다. */}
          <Table headers={["도메인", "이벤트", "상태", "건수", "최근 수신"]}
            rows={(data?.inbox ?? []).map((x) => [
              x.domain,
              <span key="e" className="font-mono text-xs">{x.event_type}</span>,
              <Badge key="s" tone={inboxTone(x.status)}>{x.status}</Badge>, x.count,
              x.last_received_at
                ? <span key="t" className={x.stale ? "font-semibold text-rose-600" : ""}>
                    {x.last_received_at.slice(5, 16).replace("T", " ")}
                    {x.stale && ` (${x.expected_hours}h+ 무수신)`}</span>
                : "—",
            ])}
            empty="수신 이벤트 없음 (Wave 연동 전)" />
          {(data?.inbox_errors ?? []).length > 0 && (
            <div className="mt-3 border-t border-slate-100 pt-2">
              <p className="mb-1 text-xs font-semibold text-rose-700">최근 실패 사유</p>
              {/* 수신 일자를 함께 보여준다 — 오래된 잔재(예: 검증 발송)가 목록을 채우면 신규 실패를
                  가리므로, 날짜 없이는 "지금 문제인지"를 판단할 수 없다. */}
              <ul className="space-y-1">
                {(data?.inbox_errors ?? []).map((e, i) => (
                  <li key={i} className="text-xs text-slate-600">
                    <Badge tone="red">{e.domain}</Badge>{" "}
                    <span className="text-slate-400">
                      {e.received_at ? e.received_at.slice(0, 10) : "—"}</span>{" "}
                    <span className="font-mono">{e.error || "(사유 없음)"}</span>
                  </li>
                ))}
              </ul>
            </div>
          )}
        </Card>
        <Card title="전자결재 연동">
          <p className="text-sm text-slate-600">결재 연결된 휴가 신청</p>
          <p className="text-2xl font-bold text-slate-800">{data?.eapproval_linked_leaves ?? 0}건</p>
          <p className="mt-1 text-xs text-slate-400">
            상신 성공 시 approval_id가 부여됩니다. Clinic W.Sign 준비 후 누적.
          </p>
        </Card>
      </div>

      <Card title="e-sign 사번 매핑 백필 (HIS erpEmployeeCode)">
        <p className="text-xs text-slate-500">
          HIS 디렉터리(employeeNumber+clinicUserId)를 ERP 직원(external_user_id)에 조인해, 서명자
          (ACTIVE 의료계약) 우선으로 HIS map EP에 ERP 사번을 백필합니다. 먼저 미리보기(조인만)로 확인 후
          실행하세요. HIS resolver는 양키(OR) 해소라 HIS 신규 작업이 없습니다.</p>
        <div className="mt-2 flex gap-2">
          <Button variant="secondary" size="sm" isLoading={empMap.isPending}
            onClick={() => empMap.mutate(true)}>미리보기(조인)</Button>
          <Button size="sm" isLoading={empMap.isPending}
            onClick={() => empMap.mutate(false)}>백필 실행</Button>
        </div>
        {mapOut && (
          <p className="mt-2 text-xs text-slate-600">
            {mapOut.skipped
              ? `건너뜀 — ${mapOut.skipped}`
              : `대상 ${mapOut.candidates ?? 0} · 매칭 ${mapOut.matched ?? 0} · ${
                  mapOut.dry_run ? "(미리보기)" : `백필 ${mapOut.mapped ?? 0}`}`
                + ` · 실패 ${mapOut.failed ?? 0} · 디렉터리 미발견 ${mapOut.unmatched_no_directory ?? 0}`}
          </p>
        )}
      </Card>

      <Card title="DB 연결 슬롯 (공유 PG — 슬롯 고갈 조기경보)" action={
        db.data?.shared_pg
          ? <Badge tone={db.data.shared_pg.warning ? "red" : "green"}>
              {db.data.shared_pg.total}/{db.data.shared_pg.max_connections}
              {db.data.shared_pg.usage_pct !== null ? ` (${db.data.shared_pg.usage_pct}%)` : ""}</Badge>
          : undefined}>
        <p className="mb-3 text-xs text-slate-500">
          ERP 풀: 점유 {db.data?.erp_pool.checked_out ?? "–"} · 오버플로 {db.data?.erp_pool.overflow ?? "–"}
          <span className="text-slate-400"> ({db.data?.erp_pool.configured ?? "pool 10 + overflow 5"})</span>
        </p>
        {db.data?.shared_pg ? (
          <>
            <Table headers={["테넌트", "연결수", "상태 / 유휴"]}
              rows={db.data.shared_pg.by_tenant.map((t) => [
                t.usename, t.connections,
                t.state_visible
                  ? <span key="s" className="text-xs text-slate-500">
                      {Object.entries(t.states).map(([k, v]) => `${k} ${v}`).join(" · ")}
                      {t.max_idle_seconds ? ` · 유휴 ${t.max_idle_seconds}s` : ""}</span>
                  : <span key="s" className="text-xs text-slate-400">가려짐 (비특권)</span>,
              ])} />
            <p className="mt-2 text-xs text-slate-400">{db.data.shared_pg.note}</p>
          </>
        ) : (
          <p className="text-sm text-slate-500">
            공유 PG 연결 진단은 운영(PostgreSQL)에서만 표시됩니다. 회복 절차는 ops-runbook §9.1.</p>
        )}
      </Card>
    </div>
  );
}

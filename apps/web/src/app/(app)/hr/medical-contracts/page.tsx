"use client";
/** 인사 — 의료진 개별 계약 (P4-1). 의료진 선택 → 계약 이력 + 신규 등록(성과급 산식). 보수=민감. */
import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api, ApiError } from "@/lib/api";
import { Badge, Button, Card, Input, Modal, Table } from "@/components/ui";
import { useToast } from "@/components/toast";
import { useConfirm } from "@/components/confirm";
import { useInputDialog } from "@/components/input-dialog";
import { todayLocal } from "@/lib/dates";
import { AttachmentCell } from "@/components/attachment-cell";
import type { Employee } from "@/types";
import { won } from "@/lib/format";

// CLM 생애주기 상태 — 위수탁계약과 일원화(common.contract_lifecycle)
const MC_ST: Record<string, { kr: string; tone: "green" | "red" | "amber" | "blue" | "gray" }> = {
  DRAFT: { kr: "작성중", tone: "gray" }, PENDING_APPROVAL: { kr: "결재중", tone: "blue" },
  APPROVED: { kr: "승인완료", tone: "blue" }, ACTIVE: { kr: "활성", tone: "green" },
  EXPIRING: { kr: "만기임박", tone: "amber" }, EXPIRED: { kr: "만료", tone: "red" },
  TERMINATED: { kr: "종료", tone: "gray" }, REJECTED: { kr: "반려", tone: "red" } };

interface Contract {
  id: number; valid_from: string; valid_to: string | null; base_pay: number;
  duty_pay: number | null; incentive_type: string; incentive_rate: number | null;
  incentive_params: Record<string, unknown>; status: string; note: string | null;
  sign_status: string | null; signed_at: string | null; sign_cert_serial: string | null;
  sign_doc_ref: string | null; sign_doc_attachment_id: number | null;
}
// 발의 응답 — 서명자별 포털 deep-link(다자) / 단일 portal_url (sign §2.5b). 발송은 ERP 책임(D3).
interface PortalLink {
  portal_url: string; sign_order?: number; channel?: string;
  display_name?: string | null; external_signer_ref?: string;
}
const INCENTIVE = ["NONE", "NET_COLLECTION", "DEPT_SHARE", "TARGET_EXCESS", "RVU"];
const INC_LABEL: Record<string, string> = { NONE: "없음", NET_COLLECTION: "순수입 배분",
  DEPT_SHARE: "진료과 배분", TARGET_EXCESS: "목표 초과", RVU: "RVU" };
// 전자서명(e-sign #6, HIS 경유) — sign_status 표시
const SIGN_LABEL: Record<string, string> = { SENT: "서명 대기", COMPLETED: "서명 완료" };
const SIGN_TONE: Record<string, "gray" | "blue" | "green"> = { SENT: "blue", COMPLETED: "green" };
// 서명 채널 — HIS는 DIRECT|PORTAL 2종(his-reply-esign6-signer-clarify-2026-06-29).
// PORTAL=이메일 deep-link(외부·신규입사) · STAFF/DIRECT=직원 SSO(→HIS DIRECT, certSerial은 HIS 해소).
const SIGN_CHANNEL: Record<string, string> = { PORTAL: "이메일 링크(PORTAL)", STAFF: "직원 SSO(STAFF)", DIRECT: "직원 SSO(DIRECT)" };

export default function MedicalContractsPage() {
  const qc = useQueryClient();
  const toast = useToast();
  const [q, setQ] = useState("");
  const [emp, setEmp] = useState<Employee | null>(null);
  const [form, setForm] = useState({ valid_from: "", base_pay: "", duty_pay: "",
    incentive_type: "NONE", incentive_rate: "", note: "" });

  const search = useQuery({
    enabled: q.length > 0,
    queryKey: ["mc-emp", q],
    queryFn: () => api<{ items: Employee[] }>(
      `/hr/employees?staff_class=MEDICAL&size=10&q=${encodeURIComponent(q)}`),
  });
  const contracts = useQuery({
    enabled: emp !== null,
    queryKey: ["mc-list", emp?.id],
    queryFn: () => api<{ items: Contract[] }>(`/hr/medical-contracts?employee_id=${emp!.id}`),
  });

  const create = useMutation({
    mutationFn: () => api(`/hr/medical-contracts`, { method: "POST", body: JSON.stringify({
      employee_id: emp!.id, valid_from: form.valid_from, base_pay: Number(form.base_pay),
      duty_pay: form.duty_pay ? Number(form.duty_pay) : null,
      incentive_type: form.incentive_type,
      incentive_rate: form.incentive_rate ? Number(form.incentive_rate) : null,
      note: form.note || null }) }),
    onSuccess: () => { toast.ok("계약 등록 완료 (기존 계약은 자동 종료)");
      qc.invalidateQueries({ queryKey: ["mc-list", emp?.id] }); },
    onError: (e) => toast.err(e instanceof ApiError ? e.error.message : "등록 실패"),
  });

  // ── CLM 생애주기 (위수탁과 일원화) ──
  const confirmDialog = useConfirm();
  const { ask, dialog } = useInputDialog();
  const [amending, setAmending] = useState<Contract | null>(null);
  const mcFail = (e: unknown) => toast.err(e instanceof ApiError ? e.error.message : "처리 실패");
  const refreshMc = () => qc.invalidateQueries({ queryKey: ["mc-list", emp?.id] });
  const submit = useMutation({
    mutationFn: (id: number) => api(`/hr/medical-contracts/${id}/submit`, { method: "POST" }),
    onSuccess: () => { toast.ok("결재 상신"); refreshMc(); }, onError: mcFail });
  const activate = useMutation({
    mutationFn: (id: number) => api(`/hr/medical-contracts/${id}/activate`, { method: "POST" }),
    onSuccess: () => { toast.ok("발효(ACTIVE)"); refreshMc(); }, onError: mcFail });
  const approve = useMutation({
    mutationFn: (id: number) => api<{ pending_next_step: boolean }>(
      `/hr/medical-contracts/${id}/approve`, { method: "POST" }),
    onSuccess: (r) => { toast.ok(r.pending_next_step ? "승인 — 다음 결재 단계 대기" : "승인 완료"); refreshMc(); },
    onError: mcFail });
  const reject = useMutation({
    mutationFn: (p: { id: number; reason: string }) => api(`/hr/medical-contracts/${p.id}/reject`,
      { method: "POST", body: JSON.stringify({ reason: p.reason }) }),
    onSuccess: () => { toast.ok("반려"); refreshMc(); }, onError: mcFail });
  const renew = useMutation({
    mutationFn: (p: { id: number; on: string }) => api(`/hr/medical-contracts/${p.id}/renew`,
      { method: "POST", body: JSON.stringify({ new_valid_to: p.on }) }),
    onSuccess: () => { toast.ok("갱신 완료"); refreshMc(); }, onError: mcFail });
  const terminate = useMutation({
    mutationFn: (p: { id: number; reason: string }) => api(`/hr/medical-contracts/${p.id}/terminate`,
      { method: "POST", body: JSON.stringify({ reason: p.reason }) }),
    onSuccess: () => { toast.ok("종료 완료"); refreshMc(); }, onError: mcFail });
  const mcBusy = submit.isPending || approve.isPending || reject.isPending || activate.isPending
    || renew.isPending || terminate.isPending;

  const onMcRenew = async (c: Contract) => {
    const on = await ask({ title: "계약 갱신", label: "갱신 종료일", kind: "date", confirmLabel: "갱신" });
    if (on) renew.mutate({ id: c.id, on: String(on) });
  };
  const onMcReject = async (c: Contract) => {
    const reason = await ask({ title: "계약 반려", label: "반려 사유", confirmLabel: "반려" });
    if (reason !== null) reject.mutate({ id: c.id, reason: String(reason) });
  };
  const onMcTerminate = async (c: Contract) => {
    const reason = await ask({ title: "계약 종료", label: "종료 사유", confirmLabel: "종료" });
    if (reason !== null) terminate.mutate({ id: c.id, reason: String(reason) });
  };
  const onMcActivate = async (c: Contract) => {
    if (await confirmDialog({ title: "계약 발효", body: "이 계약을 발효(ACTIVE)합니다.",
      confirmLabel: "발효" })) activate.mutate(c.id);
  };
  const lifecycle = (c: Contract) => {
    const active = c.status === "ACTIVE" || c.status === "EXPIRING";
    return (
      <span className="flex flex-wrap gap-1">
        {c.status === "DRAFT" && (<>
          <Button size="sm" disabled={mcBusy} onClick={() => submit.mutate(c.id)}>상신</Button>
          <Button size="sm" variant="secondary" disabled={mcBusy} onClick={() => onMcActivate(c)}>발효</Button>
        </>)}
        {c.status === "PENDING_APPROVAL" && (<>
          <Button size="sm" disabled={mcBusy} onClick={() => approve.mutate(c.id)}>승인</Button>
          <Button size="sm" variant="danger" disabled={mcBusy} onClick={() => onMcReject(c)}>반려</Button>
        </>)}
        {c.status === "APPROVED" && (
          <Button size="sm" disabled={mcBusy} onClick={() => onMcActivate(c)}>발효</Button>)}
        {active && (<>
          <Button size="sm" variant="secondary" onClick={() => setAmending(c)}>급여변경</Button>
          <Button size="sm" variant="secondary" disabled={mcBusy} onClick={() => onMcRenew(c)}>갱신</Button>
          <Button size="sm" variant="danger" disabled={mcBusy} onClick={() => onMcTerminate(c)}>종료</Button>
        </>)}
        {c.status === "EXPIRED" && (
          <Button size="sm" variant="secondary" disabled={mcBusy} onClick={() => onMcRenew(c)}>갱신</Button>)}
        {!["DRAFT", "PENDING_APPROVAL", "APPROVED", "ACTIVE", "EXPIRING", "EXPIRED"].includes(c.status)
          && <span className="text-xs text-slate-400">—</span>}
      </span>
    );
  };

  // 전자서명 발의 (HIS 경유) — ACTIVE 계약 snapshot → HIS 렌더·sign 제출. 완료는 sign.completed 미러.
  const [portalLinks, setPortalLinks] = useState<PortalLink[]>([]);  // 발의 응답의 서명자별 포털 링크
  const [channel, setChannel] = useState("PORTAL");
  // 공동 서명자(병원장 등) — 지정 시 순차 서명(다자 봉투). 비우면 단일(HIS 기본 라우팅).
  const [coSigners, setCoSigners] = useState<{ name: string; channel: string }[]>([]);
  // 채널→signers[] 속성(sign §2.5b). STAFF 서명자는 signerType/requiredIdentityLevel=STAFF_SSO →
  // HIS prepareSigners(werubyHIS PR#117)가 externalSignerRef(erp:staff:<empId>→erpEmployeeCode)로
  // certSerial 내부 주입. ERP는 certSerial 미공급. 봉투 채널의 STAFF→DIRECT 매핑은 백엔드 수행.
  const signerAttrs = (ch: string) => ch === "STAFF"
    ? { signerType: "STAFF", requiredIdentityLevel: "STAFF_SSO" }
    : { signerType: "WITNESS_EXTERNAL", requiredIdentityLevel: "NONE" };  // PORTAL/DIRECT
  // 다자 봉투 signers[] — 의료진(order1, 주 채널)+공동서명자(채널별 PORTAL/STAFF). 비면 null(단일).
  const buildSigners = () => {
    const named = coSigners.map((s) => ({ name: s.name.trim(), channel: s.channel }))
      .filter((s) => s.name);
    if (named.length === 0 || !emp) return null;
    return [
      { displayName: emp.name, channel, signOrder: 1, ...signerAttrs(channel),
        externalSignerRef: `erp:staff:${emp.employee_no}` },  // HIS Staff.erpEmployeeCode(=사번) 역해소
      ...named.map((s, i) => ({ displayName: s.name, channel: s.channel, signOrder: i + 2,
        ...signerAttrs(s.channel), externalSignerRef: `erp:cosigner:${i + 1}` })),
    ];
  };
  const initiateSign = useMutation({
    mutationFn: ({ id, force }: { id: number; force: boolean }) =>
      api<{ issue_no: string | null; signature_request_id: string | null;
      portal_url: string | null; portal_links?: PortalLink[];
      status: string; already_sent?: boolean }>(
      `/integrations/his-sign/medical-contracts/${id}/request`,
      { method: "POST", body: JSON.stringify({ channel, signers: buildSigners(), force }) }),
    onSuccess: (d) => {
      // 발송은 ERP 책임(D3) — 단일 portal_url 또는 다자 portal_links를 운영자가 각 서명자에게 전달.
      const links: PortalLink[] = (d.portal_links?.length ? d.portal_links
        : d.portal_url ? [{ portal_url: d.portal_url }] : []);
      setPortalLinks(links);
      toast.ok(d.already_sent
        ? "이미 발의됨(서명 대기) — 새 봉투가 필요하면 ‘재발의’를 사용하세요."
        : `전자서명 발의 완료 — 발급번호 ${d.issue_no ?? "(대기)"}${links.length ? ` · 포털 링크 ${links.length}건` : ""}`);
      qc.invalidateQueries({ queryKey: ["mc-list", emp?.id] }); },
    onError: (e) => toast.err(e instanceof ApiError ? e.error.message : "발의 실패"),
  });
  const activeContract = (contracts.data?.items ?? []).find((c) => c.status === "ACTIVE");

  return (
    <div className="space-y-4">
      {dialog}
      <h1 className="text-lg font-bold">인사 — 의료진 개별 계약</h1>

      <Card title="의료진 선택">
        <input value={q} onChange={(e) => setQ(e.target.value)} placeholder="의료진 이름·사번 검색"
          className="w-64 rounded-md border border-slate-300 px-3 py-1.5 text-sm" />
        {search.data && q && (
          <div className="mt-2 flex flex-wrap gap-1">
            {search.data.items.map((e) => (
              <button key={e.id} onClick={() => { setEmp(e); setQ(""); }}
                className="rounded-md border border-slate-200 px-2 py-1 text-xs hover:bg-slate-50">
                {e.name} ({e.employee_no}) · {e.job_category}</button>
            ))}
          </div>
        )}
        {emp && <p className="mt-2 text-sm">선택: <b>{emp.name}</b> ({emp.employee_no}) · {emp.job_category}</p>}
      </Card>

      {emp && (
        <>
          <Card title="계약 이력">
            <Table headers={["시작", "종료", "기본급", "당직단가", "성과급", "배분율", "상태", "생애주기", "전자서명", "서류"]}
              rows={(contracts.data?.items ?? []).map((c) => [
                c.valid_from, c.valid_to ?? "—", c.base_pay.toLocaleString(),
                c.duty_pay?.toLocaleString() ?? "-",
                <Badge key="i" tone={c.incentive_type === "NONE" ? "gray" : "blue"}>
                  {INC_LABEL[c.incentive_type]}</Badge>,
                c.incentive_rate != null ? `${c.incentive_rate}%` : "-",
                <Badge key="s" tone={(MC_ST[c.status] ?? { tone: "gray" as const }).tone}>
                  {(MC_ST[c.status] ?? { kr: c.status }).kr}</Badge>,
                lifecycle(c),
                c.sign_status
                  ? <Badge key="sg" tone={SIGN_TONE[c.sign_status] ?? "gray"}>
                      {SIGN_LABEL[c.sign_status] ?? c.sign_status}</Badge>
                  : <span key="sg" className="text-xs text-slate-400">미발송</span>,
                <AttachmentCell key="a" entityType="contract" entityId={c.id} />,
              ])} empty="계약 없음 — 아래에서 등록" />
          </Card>

          <Card title="전자서명 (HIS 경유)">
            {portalLinks.length > 0 && (
              <div className="mb-2 rounded-md border border-blue-200 bg-blue-50 p-2 text-xs">
                <p className="font-medium text-blue-800">포털 서명 링크 — 각 서명자에게 전달하세요(48h 만료, 순차 서명)</p>
                <ul className="mt-1 space-y-1">
                  {portalLinks.map((l, i) => (
                    <li key={i} className="flex items-center gap-2">
                      <span className="text-slate-500 w-32 shrink-0">
                        {l.sign_order ? `순서 ${l.sign_order} · ` : ""}{l.display_name ?? l.channel ?? "서명자"}</span>
                      <input readOnly value={l.portal_url}
                        className="flex-1 rounded border border-slate-300 bg-white px-1.5 py-0.5 font-mono text-[11px]" />
                      <button type="button" onClick={() => navigator.clipboard?.writeText(l.portal_url)}
                        className="rounded border border-slate-200 px-2 py-0.5 hover:bg-white">복사</button>
                    </li>
                  ))}
                </ul>
              </div>
            )}
            {!activeContract ? (
              <p className="text-sm text-slate-500">ACTIVE 계약이 없습니다 — 먼저 계약을 등록하세요.</p>
            ) : activeContract.sign_status === "COMPLETED" ? (
              <p className="text-sm">
                <Badge tone="green">서명 완료</Badge>{" "}
                {activeContract.signed_at && <span className="text-slate-600">{activeContract.signed_at} 완료</span>}
                {activeContract.sign_cert_serial && <span className="text-slate-400"> · 인증서 {activeContract.sign_cert_serial}</span>}
                <span className="block mt-1 text-xs text-slate-400">완료본은 위 “서류” 열에서 확인(자동 회수·보관).</span>
              </p>
            ) : (
              <div className="space-y-2">
                <p className="text-sm text-slate-600">
                  ACTIVE 계약(시작 {activeContract.valid_from})을 HIS 경유로 발의합니다. ERP는 계약 snapshot만
                  전송(원본 PDF는 HIS 렌더), 완료 시 결과·완료본이 자동 미러됩니다.
                  {activeContract.sign_status === "SENT" &&
                    <span className="ml-1 text-blue-600">현재 서명 대기 중 — 재발의 시 갱신됩니다.</span>}
                </p>
                <div className="flex items-center gap-2">
                  <label className="text-sm">{coSigners.length > 0 ? "의료진 채널" : "채널"}
                    <select value={channel} onChange={(e) => setChannel(e.target.value)}
                      className="ml-1 rounded-md border border-slate-300 px-2 py-1.5 text-sm">
                      {Object.entries(SIGN_CHANNEL).map(([v, l]) => <option key={v} value={v}>{l}</option>)}
                    </select>
                  </label>
                  <Button onClick={() => initiateSign.mutate({ id: activeContract.id, force: false })}
                    disabled={initiateSign.isPending}>
                    {activeContract.sign_status === "SENT" ? "전자서명 (대기 확인)" : "전자서명 개시"}</Button>
                  {activeContract.sign_status === "SENT" &&
                    <Button onClick={() => initiateSign.mutate({ id: activeContract.id, force: true })}
                      disabled={initiateSign.isPending}>재발의(새 봉투)</Button>}
                </div>

                {/* 공동 서명자(병원장 등) — 다자 봉투. 채널별 순차 서명 */}
                <div className="rounded-md border border-slate-200 p-2">
                  <div className="flex items-center justify-between">
                    <span className="text-sm font-medium">공동 서명자 (선택 · 병원장 등)</span>
                    <button type="button" onClick={() => setCoSigners([...coSigners, { name: "", channel: "PORTAL" }])}
                      className="rounded-md border border-slate-200 px-2 py-1 text-xs hover:bg-slate-50">+ 추가</button>
                  </div>
                  {coSigners.map((s, i) => (
                    <div key={i} className="mt-1 flex items-center gap-1">
                      <span className="text-xs text-slate-400 w-12">순서 {i + 2}</span>
                      <input value={s.name} placeholder="서명자 이름"
                        onChange={(e) => setCoSigners(coSigners.map((v, j) => j === i ? { ...v, name: e.target.value } : v))}
                        className="flex-1 rounded-md border border-slate-300 px-2 py-1 text-sm" />
                      <select value={s.channel}
                        onChange={(e) => setCoSigners(coSigners.map((v, j) => j === i ? { ...v, channel: e.target.value } : v))}
                        className="rounded-md border border-slate-300 px-1 py-1 text-xs">
                        <option value="PORTAL">PORTAL</option>
                        <option value="STAFF">STAFF</option>
                        <option value="DIRECT">DIRECT</option>{/* 주 서명과 옵션 일치(FE-3) */}
                      </select>
                      <button type="button" onClick={() => setCoSigners(coSigners.filter((_, j) => j !== i))}
                        className="rounded-md border border-slate-200 px-2 py-1 text-xs hover:bg-slate-50">제거</button>
                    </div>
                  ))}
                  {coSigners.length > 0 &&
                    <p className="mt-1 text-xs text-amber-600">
                      다자 봉투: 의료진(순서 1) → 공동서명자 순차 서명. STAFF는 HIS SSO 서명(certSerial은 HIS 해소).</p>}
                </div>
                <p className="text-xs text-slate-400">
                  PORTAL=이메일 deep-link(외부·신규입사, 신원 자동발급) · STAFF/DIRECT=직원 SSO(HIS DIRECT, certSerial HIS 해소).
                </p>
              </div>
            )}
          </Card>

          <Card title="신규 계약 등록 (기존 ACTIVE 계약 자동 종료)">
            <div className="grid gap-2 sm:grid-cols-2 lg:grid-cols-3">
              <label className="text-sm">시작일
                <input type="date" value={form.valid_from}
                  onChange={(e) => setForm({ ...form, valid_from: e.target.value })}
                  className="mt-1 w-full rounded-md border border-slate-300 px-2 py-1.5 text-sm" /></label>
              <label className="text-sm">기본급(월)
                <input type="number" value={form.base_pay}
                  onChange={(e) => setForm({ ...form, base_pay: e.target.value })}
                  className="mt-1 w-full rounded-md border border-slate-300 px-2 py-1.5 text-sm" /></label>
              <label className="text-sm">당직 단가
                <input type="number" value={form.duty_pay}
                  onChange={(e) => setForm({ ...form, duty_pay: e.target.value })}
                  className="mt-1 w-full rounded-md border border-slate-300 px-2 py-1.5 text-sm" /></label>
              <label className="text-sm">성과급 유형
                <select value={form.incentive_type}
                  onChange={(e) => setForm({ ...form, incentive_type: e.target.value })}
                  className="mt-1 w-full rounded-md border border-slate-300 px-2 py-1.5 text-sm">
                  {INCENTIVE.map((t) => <option key={t} value={t}>{INC_LABEL[t]}</option>)}
                </select></label>
              <label className="text-sm">배분율(%)
                <input type="number" value={form.incentive_rate}
                  onChange={(e) => setForm({ ...form, incentive_rate: e.target.value })}
                  disabled={form.incentive_type === "NONE" || form.incentive_type === "RVU"}
                  className="mt-1 w-full rounded-md border border-slate-300 px-2 py-1.5 text-sm disabled:bg-slate-100" /></label>
              <label className="text-sm">비고
                <input value={form.note} onChange={(e) => setForm({ ...form, note: e.target.value })}
                  className="mt-1 w-full rounded-md border border-slate-300 px-2 py-1.5 text-sm" /></label>
            </div>
            <Button className="mt-3" onClick={() => create.mutate()}
              disabled={!form.valid_from || !form.base_pay || create.isPending}>계약 등록</Button>
            <p className="mt-2 text-xs text-slate-400">
              성과급 실제 산정(진료실적×산식)은 P4-2/3에서 계약 조건을 사용합니다. 보수정보는 접속기록에 남습니다.
            </p>
          </Card>
        </>
      )}

      {amending && <AmendPayModal contract={amending}
        onDone={() => { setAmending(null); refreshMc(); }} onError={mcFail}
        onClose={() => setAmending(null)} />}
    </div>
  );
}

function AmendPayModal({ contract, onDone, onError, onClose }: {
  contract: Contract; onDone: () => void; onError: (e: unknown) => void; onClose: () => void;
}) {
  const toast = useToast();
  const [f, setF] = useState({ new_base_pay: String(contract.base_pay),
    effective_on: todayLocal(), reason: "" });
  const m = useMutation({
    mutationFn: () => api(`/hr/medical-contracts/${contract.id}/amend-pay`, { method: "POST",
      body: JSON.stringify({ new_base_pay: Number(f.new_base_pay), effective_on: f.effective_on,
        reason: f.reason }) }),
    onSuccess: () => { toast.ok("기본급 변경 (변경이력 기록)"); onDone(); }, onError });
  return (
    <Modal title="기본급 변경" dirty onClose={onClose}>
      <div className="space-y-2">
        <p className="text-xs text-slate-500">현재 {won(contract.base_pay)} → 변경 후 금액.
          변경일 이후 급여 산정부터 적용됩니다.</p>
        <Input label="변경 후 기본급(월)" value={f.new_base_pay} inputMode="numeric"
          onChange={(e) => setF({ ...f, new_base_pay: e.target.value.replace(/[^0-9]/g, "") })} />
        <Input label="적용일" type="date" value={f.effective_on}
          onChange={(e) => setF({ ...f, effective_on: e.target.value })} />
        <Input label="사유" value={f.reason} onChange={(e) => setF({ ...f, reason: e.target.value })} />
        <Button disabled={m.isPending || !(Number(f.new_base_pay) > 0)} onClick={() => m.mutate()}>
          변경</Button>
      </div>
    </Modal>
  );
}

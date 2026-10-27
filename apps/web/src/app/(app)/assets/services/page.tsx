"use client";
/** SW·클라우드·구독 관리(ITAM M2) — 4유형 마스터·좌석 준수·갱신·라이선스 증서. */
import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { ApiError, api, apiDownload } from "@/lib/api";
import { Badge, Button, Card, Input, Modal, Table } from "@/components/ui";
import { QueryErrorBanner } from "@/components/query-guard";
import { useConfirm } from "@/components/confirm";
import { useToast } from "@/components/toast";
import { AttachmentCell } from "@/components/attachment-cell";
import { EmployeePicker } from "@/components/employee-picker";

interface Compliance { seats: number | null; used: number; over: boolean; unused: number | null; seat_based: boolean }
interface Service {
  id: number; name: string; vendor: string | null; edition: string | null;
  service_type: string; billing_model: string; billing_cycle: string | null;
  seats: number | null; currency: string; recurring_cost: number | null;
  expires_on: string | null; auto_renew: boolean; cancel_notice_days: number | null;
  registered_on: string | null; started_on: string | null; first_billing_on: string | null;
  payment_terms: string | null; status: string; cancelled_on: string | null;
  account_ref: string | null; portal_url: string | null; intangible_asset_id: number | null;
  domain: string | null; homepage_url: string | null; tech_info: string | null;
  owner_employee_id: number | null; sales_contact: string | null; cost_center: string | null;
  cost_period: string | null; vat_included: boolean | null; usage_unit: string | null;
  key_masked: string | null; token_masked: string | null; note: string | null;
  contract_id: number | null; compliance?: Compliance;
}
interface HistItem { id: number; action: string; actor: number | null; at: string;
  before: Record<string, unknown> | null; after: Record<string, unknown> | null }
interface IntangibleOpt { id: number; asset_no: string; name: string }
const PAYMENT_KO: Record<string, string> = {
  PREPAID: "선불", POSTPAID: "후불", FREE_TRIAL: "무료기간" };
interface Alloc { id: number; employee_id: number | null; employee_name?: string | null;
  it_asset_id: number | null; allocated_on: string; released_on: string | null }
interface Contract { id: number; vendor: string; title: string; kind: string }

const TYPES = [
  { k: "", label: "전체" }, { k: "PERPETUAL_SW", label: "구매 SW" }, { k: "SAAS", label: "SaaS" },
  { k: "AI_SUBSCRIPTION", label: "AI 구독" }, { k: "CLOUD", label: "클라우드" },
  { k: "OPEN_SOURCE", label: "오픈소스" }];
const TYPE_KO: Record<string, string> = Object.fromEntries(TYPES.map((t) => [t.k, t.label]));
const BILLING = ["ONE_TIME", "SEAT_RECURRING", "USAGE_METERED"];

export default function ServicesPage() {
  const qc = useQueryClient();
  const [type, setType] = useState("");
  const [q, setQ] = useState("");
  const [showNew, setShowNew] = useState(false);
  const [detailId, setDetailId] = useState<number | null>(null);

  const [showCancelled, setShowCancelled] = useState(false);
  const list = useQuery({
    queryKey: ["itam-services", type, q, showCancelled],
    queryFn: () => {
      const p = new URLSearchParams();
      if (type) p.set("service_type", type);
      if (!showCancelled) p.set("status", "ACTIVE");   // 기본: 운영중만
      if (q.trim()) p.set("q", q.trim());
      return api<{ items: Service[] }>(`/itam/services?${p}`);
    },
  });

  return (
    <div className="space-y-4">
      <QueryErrorBanner queries={[list]} />
      <div className="flex flex-wrap items-center justify-between gap-2">
        <div>
          <h1 className="text-lg font-bold text-slate-900">SW·클라우드·구독 관리</h1>
          <p className="text-xs text-slate-500">구매 SW·SaaS·AI·클라우드 4유형·좌석 준수·갱신·라이선스 (ITAM M2)</p>
        </div>
        <div className="flex gap-2">
          <button onClick={() => apiDownload("/itam/cost-ledger.csv", "it-costs.csv")}
            className="rounded-md border border-slate-300 px-4 py-1.5 text-sm text-slate-700 hover:bg-slate-50">비용 원장 CSV</button>
          <Button onClick={() => setShowNew(true)}>+ 서비스 등록</Button>
        </div>
      </div>

      <div className="flex flex-wrap gap-2 text-xs">
        {TYPES.map((t) => (
          <button key={t.k} onClick={() => setType(t.k)}
            className={`rounded-full px-3 py-1 ${type === t.k ? "bg-blue-600 text-white" : "bg-slate-100 text-slate-600"}`}>
            {t.label}</button>
        ))}
      </div>

      <Card>
        <div className="mb-3 flex items-end gap-3">
          <Input label="검색(이름·벤더)" value={q} placeholder="검색어" onChange={(e) => setQ(e.target.value)} />
          <label className="flex items-center gap-1.5 pb-1.5 text-sm text-slate-600">
            <input type="checkbox" checked={showCancelled}
              onChange={(e) => setShowCancelled(e.target.checked)} /> 해지 포함</label>
        </div>
        <Table headers={["서비스", "유형", "벤더", "좌석 준수", "정기료", "만기", ""]}
          empty="등록된 서비스가 없습니다"
          rows={(list.data?.items ?? []).map((s) => [
            <span key="n">{s.name}{s.status === "CANCELLED" && <Badge tone="red">해지</Badge>}</span>,
            TYPE_KO[s.service_type] ?? s.service_type, s.vendor ?? "-",
            s.compliance?.seat_based
              ? <span key="c" className={s.compliance.over ? "font-semibold text-red-600" : "text-slate-700"}>
                  {s.compliance.used}/{s.compliance.seats ?? "-"}
                  {s.compliance.over && " 초과"}
                  {!s.compliance.over && (s.compliance.unused ?? 0) > 0 && ` (미사용 ${s.compliance.unused})`}
                </span>
              : <span key="c" className="text-slate-400">사용량형</span>,
            s.recurring_cost != null ? `${s.recurring_cost.toLocaleString()} ${s.currency}` : "-",
            s.expires_on
              ? <span key="e">{s.expires_on}{s.auto_renew && <Badge tone="amber">자동갱신</Badge>}</span>
              : "-",
            <button key="d" onClick={() => setDetailId(s.id)}
              className="rounded bg-slate-100 px-2 py-0.5 text-xs text-slate-700 hover:bg-slate-200">상세</button>,
          ])} />
      </Card>

      {showNew && <NewServiceModal onClose={() => setShowNew(false)}
        onDone={(id) => { setShowNew(false); qc.invalidateQueries({ queryKey: ["itam-services"] }); setDetailId(id); }} />}
      {detailId != null && <ServiceDetail serviceId={detailId} onClose={() => setDetailId(null)}
        onChanged={() => qc.invalidateQueries({ queryKey: ["itam-services"] })} />}
    </div>
  );
}

function NewServiceModal({ onClose, onDone }: { onClose: () => void; onDone: (id: number) => void }) {
  const [f, setF] = useState({ name: "", service_type: "SAAS", billing_model: "SEAT_RECURRING",
    vendor: "", seats: "", recurring_cost: "", currency: "KRW", expires_on: "",
    auto_renew: false, cancel_notice_days: "", license_key: "", contract_id: "",
    registered_on: "", started_on: "", first_billing_on: "", payment_terms: "",
    domain: "", homepage_url: "", tech_info: "", owner_employee_id: "", sales_contact: "",
    cost_center: "", cost_period: "", vat_included: false, usage_unit: "", api_token: "", note: "" });
  const [err, setErr] = useState("");
  const contracts = useQuery({ queryKey: ["itam-contracts"],
    queryFn: () => api<{ items: Contract[] }>("/itam/contracts") });
  const create = useMutation({
    mutationFn: () => api<Service>("/itam/services", { method: "POST", body: JSON.stringify({
      name: f.name, service_type: f.service_type, billing_model: f.billing_model,
      vendor: f.vendor || null, seats: f.seats ? Number(f.seats) : null,
      recurring_cost: f.recurring_cost ? Number(f.recurring_cost) : null, currency: f.currency,
      expires_on: f.expires_on || null, auto_renew: f.auto_renew,
      cancel_notice_days: f.cancel_notice_days ? Number(f.cancel_notice_days) : null,
      registered_on: f.registered_on || null, started_on: f.started_on || null,
      first_billing_on: f.first_billing_on || null, payment_terms: f.payment_terms || null,
      license_key: f.license_key || null, contract_id: f.contract_id ? Number(f.contract_id) : null,
      domain: f.domain || null, homepage_url: f.homepage_url || null, tech_info: f.tech_info || null,
      owner_employee_id: f.owner_employee_id ? Number(f.owner_employee_id) : null,
      sales_contact: f.sales_contact || null, cost_center: f.cost_center || null,
      cost_period: f.cost_period || null, vat_included: f.vat_included,
      usage_unit: f.usage_unit || null, api_token: f.api_token || null, note: f.note || null }) }),
    onSuccess: (s) => onDone(s.id),
    onError: (e) => setErr(e instanceof ApiError ? e.message : "등록 실패"),
  });
  const set = (k: string, v: string | boolean) => setF({ ...f, [k]: v });

  return (
    <Modal title="SW·서비스 등록" onClose={onClose} size="lg">
      <div className="space-y-3">
        <div className="grid grid-cols-2 gap-3">
          <Input label="서비스명*" value={f.name} onChange={(e) => set("name", e.target.value)} />
          <Input label="벤더" value={f.vendor} onChange={(e) => set("vendor", e.target.value)} />
          <label className="block"><span className="mb-1 block text-xs font-medium text-slate-600">유형</span>
            <select value={f.service_type} onChange={(e) => set("service_type", e.target.value)}
              className="w-full rounded-md border border-slate-300 px-2 py-1.5 text-sm">
              {TYPES.filter((t) => t.k).map((t) => <option key={t.k} value={t.k}>{t.label}</option>)}
            </select></label>
          <label className="block"><span className="mb-1 block text-xs font-medium text-slate-600">과금</span>
            <select value={f.billing_model} onChange={(e) => set("billing_model", e.target.value)}
              className="w-full rounded-md border border-slate-300 px-2 py-1.5 text-sm">
              {BILLING.map((b) => <option key={b} value={b}>{b}</option>)}
            </select></label>
          <Input label="보유 좌석(좌석형)" value={f.seats} onChange={(e) => set("seats", e.target.value)} />
          <div className="grid grid-cols-2 gap-2">
            <Input label="정기료" value={f.recurring_cost} onChange={(e) => set("recurring_cost", e.target.value)} />
            <label className="block"><span className="mb-1 block text-xs font-medium text-slate-600">통화</span>
              <select value={f.currency} onChange={(e) => set("currency", e.target.value)}
                className="w-full rounded-md border border-slate-300 px-2 py-1.5 text-sm">
                {["KRW", "USD", "EUR", "JPY"].map((c) => <option key={c} value={c}>{c}</option>)}
              </select></label>
          </div>
          <Input label="등록일자" type="date" value={f.registered_on}
            onChange={(e) => set("registered_on", e.target.value)} />
          <Input label="개시일자(사용 개시)" type="date" value={f.started_on}
            onChange={(e) => set("started_on", e.target.value)} />
          <Input label="유료 첫 결제일" type="date" value={f.first_billing_on}
            onChange={(e) => set("first_billing_on", e.target.value)} />
          <label className="block"><span className="mb-1 block text-xs font-medium text-slate-600">결제방식</span>
            <select value={f.payment_terms} onChange={(e) => set("payment_terms", e.target.value)}
              className="w-full rounded-md border border-slate-300 px-2 py-1.5 text-sm">
              <option value="">미지정</option>
              <option value="PREPAID">선불</option>
              <option value="POSTPAID">후불</option>
              <option value="FREE_TRIAL">무료기간</option>
            </select></label>
          <Input label="만기" type="date" value={f.expires_on} onChange={(e) => set("expires_on", e.target.value)} />
          <Input label="해지 통보일수" value={f.cancel_notice_days}
            onChange={(e) => set("cancel_notice_days", e.target.value)} />
          <label className="block"><span className="mb-1 block text-xs font-medium text-slate-600">예상비용 단위</span>
            <select value={f.cost_period} onChange={(e) => set("cost_period", e.target.value)}
              className="w-full rounded-md border border-slate-300 px-2 py-1.5 text-sm">
              <option value="">미지정</option><option value="MONTHLY">월</option><option value="ANNUAL">년</option>
            </select></label>
          <label className="block"><span className="mb-1 block text-xs font-medium text-slate-600">결제 단위(사용량형)</span>
            <input value={f.usage_unit} onChange={(e) => set("usage_unit", e.target.value)}
              list="usage-units" placeholder="토큰 / vCPU-h / GB 등"
              className="w-full rounded-md border border-slate-300 px-2 py-1.5 text-sm" />
            <datalist id="usage-units"><option value="토큰" /><option value="vCPU-h" /><option value="GB" /><option value="요청" /></datalist>
          </label>
          <label className="flex items-end gap-2 pb-1 text-sm text-slate-700">
            <input type="checkbox" checked={f.vat_included}
              onChange={(e) => set("vat_included", e.target.checked)} /> 부가세(VAT) 포함</label>
          <Input label="도메인" value={f.domain} onChange={(e) => set("domain", e.target.value)} />
          <Input label="홈페이지·관련 링크" value={f.homepage_url} onChange={(e) => set("homepage_url", e.target.value)} />
          <div className="block"><span className="mb-1 block text-xs font-medium text-slate-600">서비스 담당자</span>
            <EmployeePicker value={f.owner_employee_id ? Number(f.owner_employee_id) : null}
              onPick={(id) => set("owner_employee_id", id ? String(id) : "")} /></div>
          <Input label="영업 담당자" value={f.sales_contact} onChange={(e) => set("sales_contact", e.target.value)} />
          <Input label="코스트센터(부서)" value={f.cost_center} onChange={(e) => set("cost_center", e.target.value)} />
          <Input label="라이선스 키(암호화 저장)" value={f.license_key}
            onChange={(e) => set("license_key", e.target.value)} />
          <Input label="API 토큰(암호화 저장)" value={f.api_token}
            onChange={(e) => set("api_token", e.target.value)} />
          <label className="block"><span className="mb-1 block text-xs font-medium text-slate-600">벤더 계약</span>
            <select value={f.contract_id} onChange={(e) => set("contract_id", e.target.value)}
              className="w-full rounded-md border border-slate-300 px-2 py-1.5 text-sm">
              <option value="">없음</option>
              {(contracts.data?.items ?? []).map((c) => (
                <option key={c.id} value={c.id}>{c.vendor} · {c.title}</option>))}
            </select></label>
          <label className="flex items-end gap-2 pb-1 text-sm text-slate-700">
            <input type="checkbox" checked={f.auto_renew}
              onChange={(e) => set("auto_renew", e.target.checked)} /> 자동갱신</label>
          <label className="col-span-2 block"><span className="mb-1 block text-xs font-medium text-slate-600">IT 정보(리전·티어·리소스 등)</span>
            <textarea value={f.tech_info} onChange={(e) => set("tech_info", e.target.value)} rows={2}
              className="w-full rounded-md border border-slate-300 px-2 py-1.5 text-sm" /></label>
          <label className="col-span-2 block"><span className="mb-1 block text-xs font-medium text-slate-600">비고·메모</span>
            <textarea value={f.note} onChange={(e) => set("note", e.target.value)} rows={2}
              className="w-full rounded-md border border-slate-300 px-2 py-1.5 text-sm" /></label>
        </div>
        <p className="text-[11px] text-amber-600">⚠️ 라이선스 키·토큰은 암호화 저장되며 마스킹만 표시됩니다. 계정 비밀번호는 별도 볼트로 관리하세요.</p>
        <p className="text-[11px] text-slate-400">📷 이미지·증빙은 등록 후 상세 화면에서 첨부합니다.</p>
        {err && <p className="text-xs text-red-600">{err}</p>}
        <div className="flex gap-2">
          <Button isLoading={create.isPending} onClick={() => { setErr(""); create.mutate(); }}>등록</Button>
          <button onClick={onClose} className="rounded-md border border-slate-300 px-4 py-1.5 text-sm text-slate-600">취소</button>
        </div>
      </div>
    </Modal>
  );
}

function ServiceDetail({ serviceId, onClose, onChanged }: {
  serviceId: number; onClose: () => void; onChanged: () => void }) {
  const qc = useQueryClient();
  const ask = useConfirm();
  const toast = useToast();
  const [empId, setEmpId] = useState("");
  const detail = useQuery({ queryKey: ["itam-service", serviceId],
    queryFn: () => api<Service & { compliance: Compliance; allocations: Alloc[] }>(`/itam/services/${serviceId}`) });
  const refresh = () => { qc.invalidateQueries({ queryKey: ["itam-service", serviceId] }); onChanged(); };
  const fail = (e: unknown) => toast.err(e instanceof ApiError ? e.message : "요청 실패");
  const alloc = useMutation({
    mutationFn: () => api(`/itam/services/${serviceId}/allocate`, { method: "POST",
      body: JSON.stringify({ employee_id: Number(empId) }) }),
    onSuccess: () => { setEmpId(""); refresh(); }, onError: fail });
  const release = useMutation({
    mutationFn: (id: number) => api(`/itam/service-allocations/${id}/release`, { method: "POST" }),
    onSuccess: () => { refresh(); }, onError: fail });
  const [editing, setEditing] = useState(false);
  const [showHist, setShowHist] = useState(false);
  const cancel = useMutation({
    mutationFn: (reactivate: boolean) => api(
      `/itam/services/${serviceId}/cancel?reactivate=${reactivate}`, { method: "POST" }),
    onSuccess: () => { refresh(); }, onError: fail });

  const s = detail.data;
  return (
    <Modal title={s ? s.name : "서비스 상세"} onClose={onClose} size="lg">
      {!s ? <p className="text-sm text-slate-400">불러오는 중…</p> : (
        <div className="space-y-4">
          <div className="flex items-center justify-between">
            <span>{s.status === "CANCELLED"
              ? <Badge tone="red">해지됨{s.cancelled_on ? ` (${s.cancelled_on})` : ""}</Badge>
              : <Badge tone="green">운영중</Badge>}</span>
            <div className="flex gap-2">
              <button onClick={() => setEditing((v) => !v)}
                className="rounded border border-slate-300 px-3 py-1 text-xs text-slate-700 hover:bg-slate-50">
                {editing ? "수정 닫기" : "수정"}</button>
              <button onClick={() => setShowHist((v) => !v)}
                className="rounded border border-slate-300 px-3 py-1 text-xs text-slate-700 hover:bg-slate-50">
                {showHist ? "이력 닫기" : "갱신 이력"}</button>
              {s.status === "CANCELLED"
                ? <Button variant="success" size="sm" onClick={() => cancel.mutate(true)}>재개</Button>
                : <Button variant="danger" size="sm" onClick={async () => { if (await ask({ title: "서비스 해지",
                    body: "이 서비스를 해지 처리할까요?", danger: true, confirmLabel: "해지" })) cancel.mutate(false); }}>해지</Button>}
            </div>
          </div>
          {editing && <ServiceEditForm s={s} onSaved={() => { setEditing(false); refresh(); }} />}
          {showHist && <ServiceHistoryPanel serviceId={serviceId} />}

          <div className="text-sm text-slate-700">
            <p><b>유형</b> {TYPE_KO[s.service_type] ?? s.service_type} · <b>과금</b> {s.billing_model}
              {s.usage_unit ? ` (${s.usage_unit} 단위)` : ""}</p>
            <p><b>공급처(벤더)</b> {s.vendor ?? "-"}
              · <b>정기료</b> {s.recurring_cost != null ? `${s.recurring_cost.toLocaleString()} ${s.currency}` : "-"}
              {s.cost_period ? ` /${s.cost_period === "ANNUAL" ? "년" : "월"}` : ""}
              {s.vat_included != null && <> · <Badge tone="gray">VAT {s.vat_included ? "포함" : "별도"}</Badge></>}</p>
            <p><b>서비스 담당자</b> {s.owner_employee_id ? `#${s.owner_employee_id}` : "-"}
              · <b>영업 담당자</b> {s.sales_contact ?? "-"} · <b>코스트센터</b> {s.cost_center ?? "-"}</p>
            {(s.domain || s.homepage_url) && <p><b>도메인</b> {s.domain ?? "-"}
              {s.homepage_url && <> · <a href={s.homepage_url} target="_blank" rel="noreferrer" className="text-blue-600 underline">링크</a></>}</p>}
            <p><b>등록일</b> {s.registered_on ?? "-"} · <b>개시일</b> {s.started_on ?? "-"}</p>
            <p><b>유료 첫 결제일</b> {s.first_billing_on ?? "-"}
              {s.payment_terms && <> · <Badge tone="gray">{PAYMENT_KO[s.payment_terms] ?? s.payment_terms}</Badge></>}</p>
            <p><b>만기</b> {s.expires_on ?? "-"} {s.auto_renew && <Badge tone="amber">자동갱신</Badge>}
              {s.cancel_notice_days ? ` · 해지통보 ${s.cancel_notice_days}일 전` : ""}</p>
            <p><b>라이선스 키</b> {s.key_masked ?? "-"} · <b>API 토큰</b> {s.token_masked ?? "-"}</p>
            {s.tech_info && <p><b>IT 정보</b> {s.tech_info}</p>}
            {s.note && <p><b>비고</b> {s.note}</p>}
          </div>

          {s.compliance.seat_based && (
            <div className={`rounded-lg border p-3 ${s.compliance.over ? "border-red-200 bg-red-50/50" : "border-slate-100"}`}>
              <p className="text-sm font-medium text-slate-700">좌석 준수
                <span className={s.compliance.over ? "ml-2 font-semibold text-red-600" : "ml-2"}>
                  사용 {s.compliance.used} / 보유 {s.compliance.seats ?? "-"}
                  {s.compliance.over && " — 초과(위반)"}
                  {!s.compliance.over && (s.compliance.unused ?? 0) > 0 && ` — 미사용 ${s.compliance.unused}(절감 대상)`}</span></p>
              <div className="mt-2 flex items-end gap-2">
                <div><span className="mb-1 block text-xs font-medium text-slate-600">직원 좌석 배정</span>
                  <EmployeePicker value={empId ? Number(empId) : null}
                    onPick={(id) => setEmpId(id ? String(id) : "")} /></div>
                <Button size="sm" disabled={!empId || alloc.isPending} onClick={() => alloc.mutate()}>좌석 배정</Button>
              </div>
              <Table headers={["직원/자산", "배정일", "해제", ""]} empty="배정 없음"
                rows={s.allocations.map((a) => [
                  a.employee_id ? (a.employee_name ?? `직원#${a.employee_id}`) : a.it_asset_id ? `자산#${a.it_asset_id}` : "-",
                  a.allocated_on, a.released_on ?? "사용중",
                  a.released_on ? "" :
                    <Button key="r" variant="neutral" size="sm" onClick={() => release.mutate(a.id)}>해제</Button>,
                ])} />
            </div>
          )}

          <CostSection serviceId={serviceId} />

          <div>
            <p className="mb-1 text-sm font-medium text-slate-700">📷 이미지·라이선스 증서·구독 증빙</p>
            <AttachmentCell entityType="software_service" entityId={serviceId} gallery />
          </div>
        </div>
      )}
    </Modal>
  );
}

function ServiceEditForm({ s, onSaved }: { s: Service; onSaved: () => void }) {
  const intangibles = useQuery({ queryKey: ["finance-intangibles"],
    queryFn: () => api<{ items: IntangibleOpt[] }>("/finance/intangibles") });
  const [f, setF] = useState({
    name: s.name, vendor: s.vendor ?? "", seats: s.seats?.toString() ?? "",
    recurring_cost: s.recurring_cost?.toString() ?? "", currency: s.currency,
    expires_on: s.expires_on ?? "", auto_renew: s.auto_renew,
    cancel_notice_days: s.cancel_notice_days?.toString() ?? "",
    started_on: s.started_on ?? "", first_billing_on: s.first_billing_on ?? "",
    payment_terms: s.payment_terms ?? "", license_key: "", api_token: "",
    account_ref: s.account_ref ?? "", portal_url: s.portal_url ?? "",
    domain: s.domain ?? "", homepage_url: s.homepage_url ?? "", tech_info: s.tech_info ?? "",
    owner_employee_id: s.owner_employee_id ? String(s.owner_employee_id) : "",
    sales_contact: s.sales_contact ?? "", cost_center: s.cost_center ?? "",
    cost_period: s.cost_period ?? "", vat_included: s.vat_included ?? false,
    usage_unit: s.usage_unit ?? "", note: s.note ?? "",
    intangible_asset_id: s.intangible_asset_id ? String(s.intangible_asset_id) : "" });
  const [err, setErr] = useState("");
  const save = useMutation({
    mutationFn: () => api(`/itam/services/${s.id}`, { method: "PATCH", body: JSON.stringify({
      name: f.name, vendor: f.vendor || null, seats: f.seats ? Number(f.seats) : null,
      recurring_cost: f.recurring_cost ? Number(f.recurring_cost) : null, currency: f.currency,
      expires_on: f.expires_on || null, auto_renew: f.auto_renew,
      cancel_notice_days: f.cancel_notice_days ? Number(f.cancel_notice_days) : null,
      started_on: f.started_on || null, first_billing_on: f.first_billing_on || null,
      payment_terms: f.payment_terms || null, license_key: f.license_key || null,
      api_token: f.api_token || null, account_ref: f.account_ref || null, portal_url: f.portal_url || null,
      domain: f.domain || null, homepage_url: f.homepage_url || null, tech_info: f.tech_info || null,
      owner_employee_id: f.owner_employee_id ? Number(f.owner_employee_id) : null,
      sales_contact: f.sales_contact || null, cost_center: f.cost_center || null,
      cost_period: f.cost_period || null, vat_included: f.vat_included,
      usage_unit: f.usage_unit || null, note: f.note || null,
      intangible_asset_id: f.intangible_asset_id ? Number(f.intangible_asset_id) : null }) }),
    onSuccess: onSaved,
    onError: (e) => setErr(e instanceof ApiError ? e.message : "저장 실패"),
  });
  const set = (k: string, v: string | boolean) => setF({ ...f, [k]: v });
  return (
    <div className="rounded-lg border border-blue-100 bg-blue-50/30 p-3">
      <p className="mb-2 text-sm font-medium text-slate-700">서비스 정보 수정 (갱신·좌석·해지통보 등)</p>
      <div className="grid grid-cols-2 gap-2">
        <Input label="서비스명" value={f.name} onChange={(e) => set("name", e.target.value)} />
        <Input label="벤더" value={f.vendor} onChange={(e) => set("vendor", e.target.value)} />
        <Input label="보유 좌석" value={f.seats} onChange={(e) => set("seats", e.target.value)} />
        <Input label="정기료" value={f.recurring_cost} onChange={(e) => set("recurring_cost", e.target.value)} />
        <Input label="만기(갱신 시 연장)" type="date" value={f.expires_on}
          onChange={(e) => set("expires_on", e.target.value)} />
        <Input label="해지 통보일수" value={f.cancel_notice_days}
          onChange={(e) => set("cancel_notice_days", e.target.value)} />
        <Input label="개시일" type="date" value={f.started_on} onChange={(e) => set("started_on", e.target.value)} />
        <Input label="유료 첫 결제일" type="date" value={f.first_billing_on}
          onChange={(e) => set("first_billing_on", e.target.value)} />
        <label className="block"><span className="mb-1 block text-xs font-medium text-slate-600">예상비용 단위</span>
          <select value={f.cost_period} onChange={(e) => set("cost_period", e.target.value)}
            className="w-full rounded-md border border-slate-300 px-2 py-1.5 text-sm">
            <option value="">미지정</option><option value="MONTHLY">월</option><option value="ANNUAL">년</option>
          </select></label>
        <Input label="결제 단위(토큰·vCPU-h 등)" value={f.usage_unit} onChange={(e) => set("usage_unit", e.target.value)} />
        <label className="flex items-end gap-2 pb-1 text-sm text-slate-700">
          <input type="checkbox" checked={f.vat_included}
            onChange={(e) => set("vat_included", e.target.checked)} /> 부가세(VAT) 포함</label>
        <Input label="도메인" value={f.domain} onChange={(e) => set("domain", e.target.value)} />
        <Input label="홈페이지·관련 링크" value={f.homepage_url} onChange={(e) => set("homepage_url", e.target.value)} />
        <div className="block"><span className="mb-1 block text-xs font-medium text-slate-600">서비스 담당자</span>
          <EmployeePicker value={f.owner_employee_id ? Number(f.owner_employee_id) : null}
            onPick={(id) => set("owner_employee_id", id ? String(id) : "")} /></div>
        <Input label="영업 담당자" value={f.sales_contact} onChange={(e) => set("sales_contact", e.target.value)} />
        <Input label="코스트센터(부서)" value={f.cost_center} onChange={(e) => set("cost_center", e.target.value)} />
        <Input label="라이선스 키(변경 시 입력)" value={f.license_key}
          onChange={(e) => set("license_key", e.target.value)} />
        <Input label="API 토큰(변경 시 입력·암호화)" value={f.api_token}
          onChange={(e) => set("api_token", e.target.value)} />
        <label className="flex items-end gap-2 pb-1 text-sm text-slate-700">
          <input type="checkbox" checked={f.auto_renew}
            onChange={(e) => set("auto_renew", e.target.checked)} /> 자동갱신</label>
        <Input label="벤더 계정/테넌트 ID" value={f.account_ref}
          onChange={(e) => set("account_ref", e.target.value)} />
        <Input label="관리 콘솔 URL" value={f.portal_url}
          onChange={(e) => set("portal_url", e.target.value)} />
        <label className="block"><span className="mb-1 block text-xs font-medium text-slate-600">감가상각 연결(무형자산)</span>
          <select value={f.intangible_asset_id} onChange={(e) => set("intangible_asset_id", e.target.value)}
            className="w-full rounded-md border border-slate-300 px-2 py-1.5 text-sm">
            <option value="">연결 없음</option>
            {(intangibles.data?.items ?? []).map((i) => (
              <option key={i.id} value={i.id}>{i.asset_no} · {i.name}</option>))}
          </select></label>
        <label className="col-span-2 block"><span className="mb-1 block text-xs font-medium text-slate-600">IT 정보(리전·티어·리소스 등)</span>
          <textarea value={f.tech_info} onChange={(e) => set("tech_info", e.target.value)} rows={2}
            className="w-full rounded-md border border-slate-300 px-2 py-1.5 text-sm" /></label>
        <label className="col-span-2 block"><span className="mb-1 block text-xs font-medium text-slate-600">비고·메모</span>
          <textarea value={f.note} onChange={(e) => set("note", e.target.value)} rows={2}
            className="w-full rounded-md border border-slate-300 px-2 py-1.5 text-sm" /></label>
      </div>
      <p className="mt-1 text-[11px] text-amber-600">⚠️ 라이선스 키·API 토큰은 <b>암호화 저장·마스킹만 표시</b>됩니다. 계정/콘솔 필드(평문)에 <b>비밀번호는 입력 금지</b> — 비밀은 별도 볼트로 관리.</p>
      {err && <p className="mt-1 text-xs text-red-600">{err}</p>}
      <Button isLoading={save.isPending} onClick={() => { setErr(""); save.mutate(); }}>저장</Button>
    </div>
  );
}

/** 갱신 이력(F-8) — 감사 기반. 만기·단가 변경 추적. */
function ServiceHistoryPanel({ serviceId }: { serviceId: number }) {
  const hist = useQuery({ queryKey: ["itam-service-history", serviceId],
    queryFn: () => api<{ items: HistItem[] }>(`/itam/services/${serviceId}/history`) });
  const fmt = (o: Record<string, unknown> | null) =>
    o ? Object.entries(o).map(([k, v]) => `${k}:${v ?? "-"}`).join(", ") : "";
  return (
    <div className="rounded-lg border border-slate-100 p-3">
      <p className="mb-2 text-sm font-medium text-slate-700">갱신·변경 이력</p>
      <Table headers={["시각", "동작", "이전", "이후"]} empty="이력 없음"
        rows={(hist.data?.items ?? []).map((h) => [
          <span key="t" className="text-xs">{h.at?.slice(0, 19).replace("T", " ")}</span>,
          h.action,
          <span key="b" className="text-[11px] text-slate-500">{fmt(h.before)}</span>,
          <span key="a" className="text-[11px] text-slate-700">{fmt(h.after)}</span>])} />
    </div>
  );
}

interface Cost { period: string; amount: number; currency: string; amount_krw: number;
  usage_note: string | null; source: string; posted: boolean }

function CostSection({ serviceId }: { serviceId: number }) {
  const qc = useQueryClient();
  const [f, setF] = useState({ period: "", amount: "", currency: "KRW", fx_rate: "", usage_note: "" });
  const [cerr, setCerr] = useState("");
  const costs = useQuery({ queryKey: ["itam-service-costs", serviceId],
    queryFn: () => api<{ items: Cost[] }>(`/itam/services/${serviceId}/costs`) });
  const rec = useMutation({
    mutationFn: () => api(`/itam/services/${serviceId}/costs`, { method: "POST", body: JSON.stringify({
      period: f.period, amount: Number(f.amount), currency: f.currency,
      fx_rate: f.fx_rate ? Number(f.fx_rate) : null, usage_note: f.usage_note || null }) }),
    onSuccess: () => { setF({ period: "", amount: "", currency: "KRW", fx_rate: "", usage_note: "" });
      setCerr(""); qc.invalidateQueries({ queryKey: ["itam-service-costs", serviceId] }); },
    onError: (e) => setCerr(e instanceof ApiError ? e.message : "입력 실패"),
  });
  const set = (k: string, v: string) => setF({ ...f, [k]: v });

  return (
    <div className="rounded-lg border border-slate-100 p-3">
      <p className="mb-2 text-sm font-medium text-slate-700">월 비용 (구독료·클라우드 사용량 — KRW 환산 저장)</p>
      <div className="flex flex-wrap items-end gap-2">
        <label className="block"><span className="mb-1 block text-xs text-slate-600">월</span>
          <input type="month" value={f.period} onChange={(e) => set("period", e.target.value)}
            className="rounded-md border border-slate-300 px-2 py-1.5 text-sm" /></label>
        <Input label="금액" value={f.amount} onChange={(e) => set("amount", e.target.value)} />
        <label className="block"><span className="mb-1 block text-xs text-slate-600">통화</span>
          <select value={f.currency} onChange={(e) => set("currency", e.target.value)}
            className="rounded-md border border-slate-300 px-2 py-1.5 text-sm">
            {["KRW", "USD", "EUR", "JPY"].map((c) => <option key={c} value={c}>{c}</option>)}
          </select></label>
        {f.currency !== "KRW" &&
          <Input label="환율(→KRW)" value={f.fx_rate} onChange={(e) => set("fx_rate", e.target.value)} />}
        <Input label="사용량 메모" value={f.usage_note} onChange={(e) => set("usage_note", e.target.value)} />
        <Button size="sm" disabled={!f.period || !f.amount || rec.isPending} onClick={() => rec.mutate()}>입력</Button>
      </div>
      {cerr && <p className="mt-1 text-xs text-red-600">{cerr}</p>}
      <Table headers={["월", "금액", "KRW 환산", "사용량", "출처", "전기"]} empty="비용 입력 없음"
        rows={(costs.data?.items ?? []).map((c) => [
          c.period, `${c.amount.toLocaleString()} ${c.currency}`,
          `${c.amount_krw.toLocaleString()} 원`, c.usage_note ?? "-", c.source,
          c.posted ? <Badge key="p" tone="green">전기됨</Badge> : <span key="p" className="text-slate-400">미전기</span>])} />
    </div>
  );
}

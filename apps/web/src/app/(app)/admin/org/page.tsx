"use client";
/** 기관 설정 — 공식 문서(증명서·계약서·급여명세) 발급기관 표기의 SoT. 조회 DIRECTOR/ADMIN·저장 ADMIN. */
import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { ApiError, api } from "@/lib/api";
import { Button, Card, Input } from "@/components/ui";
import { useToast } from "@/components/toast";
import { useConfirm } from "@/components/confirm";
import { AttachmentCell } from "@/components/attachment-cell";

interface ExtraItem { key: string; value: string }
interface OrgSettings {
  configured: boolean;
  source: "db" | "env" | "unset";
  name: string | null;
  representative: string | null;
  biz_reg_no: string | null;
  care_org_no: string | null;
  address: string | null;
  tel: string | null;
  domain: string | null;
  fiscal_year_start_month: number;
  extra: ExtraItem[] | null;
  updated_at: string | null;
}
type Draft = {
  name: string; representative: string; biz_reg_no: string;
  care_org_no: string; address: string; tel: string; domain: string;
  fiscal_year_start_month: number;
};

const FIELDS: { key: keyof Draft; label: string; placeholder: string }[] = [
  { key: "name", label: "기관 정식명칭 (필수)", placeholder: "예: OO의료재단 OO병원" },
  { key: "representative", label: "대표자(병원장)", placeholder: "예: 홍길동" },
  { key: "biz_reg_no", label: "사업자등록번호", placeholder: "예: 123-45-67890" },
  { key: "care_org_no", label: "요양기관기호", placeholder: "예: 11100001" },
  { key: "domain", label: "도메인 주소", placeholder: "예: hospital.example.org" },
  { key: "address", label: "주소", placeholder: "예: 서울특별시 ..." },
  { key: "tel", label: "대표 전화", placeholder: "예: 02-0000-0000" },
];

const SOURCE_LABEL: Record<OrgSettings["source"], string> = {
  db: "DB 설정 적용 중",
  env: "환경변수(ORG_NAME) 폴백 적용 중 — 저장 시 DB 설정이 우선",
  unset: "미설정 — 문서에 \"본 의료기관\" 폴백 표기 중",
};

export default function OrgSettingsPage() {
  const qc = useQueryClient();
  const toast = useToast();
  const [draft, setDraft] = useState<Draft | null>(null);

  const q = useQuery({
    queryKey: ["org-settings"],
    queryFn: () => api<OrgSettings>("/admin/org-settings"),
  });

  const cur = q.data;
  const form: Draft = draft ?? {
    name: cur?.name ?? "", representative: cur?.representative ?? "",
    biz_reg_no: cur?.biz_reg_no ?? "", care_org_no: cur?.care_org_no ?? "",
    address: cur?.address ?? "", tel: cur?.tel ?? "", domain: cur?.domain ?? "",
    fiscal_year_start_month: cur?.fiscal_year_start_month ?? 1,
  };
  const set = (k: keyof Draft, v: string | number) => setDraft({ ...form, [k]: v });

  const [extraDraft, setExtraDraft] = useState<ExtraItem[] | null>(null);
  const extra: ExtraItem[] = extraDraft ?? (cur?.extra ?? []);
  const setExtra = (next: ExtraItem[]) => setExtraDraft(next);

  const save = useMutation({
    mutationFn: () => api("/admin/org-settings", { method: "PUT",
      body: JSON.stringify({ ...form, extra: extra.filter((e) => e.key.trim()) }) }),
    onSuccess: () => {
      toast.ok("저장 완료 — 증명서·계약서·급여명세 PDF에 즉시 반영");
      setDraft(null); setExtraDraft(null);
      qc.invalidateQueries({ queryKey: ["org-settings"] });
    },
    onError: (e: unknown) => toast.err(e instanceof ApiError ? e.error.message : "저장 실패 (ADMIN 전용)"),
  });
  const dirty = draft !== null || extraDraft !== null;

  return (
    <div className="space-y-4">
      <div className="flex items-center justify-between">
        <h1 className="text-lg font-bold">기관 설정</h1>
        <Button isLoading={save.isPending} disabled={!dirty || !form.name.trim()}
                onClick={() => save.mutate()}>저장</Button>
      </div>
      <p className="text-xs text-slate-500">
        재직/경력증명서·계약서(안)·급여명세서 PDF의 발급기관 표기에 사용됩니다.
        증명서 하단은 기관장 명의 관행에 따라 &ldquo;{form.name.trim() || "본 의료기관"}장&rdquo;으로 표기됩니다.</p>
      {q.isError && <p className="text-sm text-red-600">기관 설정 조회 권한이 없습니다 (DIRECTOR/ADMIN)</p>}

      <Card title="발급기관 정보">
        {cur && (
          <p className="mb-3 text-xs text-slate-500">
            상태: {SOURCE_LABEL[cur.source]}
            {cur.updated_at && ` · 최근 수정 ${cur.updated_at.slice(0, 16).replace("T", " ")}`}
          </p>
        )}
        <div className="grid max-w-2xl grid-cols-1 gap-3 sm:grid-cols-2">
          {FIELDS.map((f) => (
            <div key={f.key} className={f.key === "name" || f.key === "address" ? "sm:col-span-2" : ""}>
              <Input label={f.label} value={form[f.key]} placeholder={f.placeholder}
                     onChange={(e) => set(f.key, e.target.value)} />
            </div>
          ))}
          <div>
            <label className="block">
              <span className="mb-1 block text-xs font-medium text-slate-600">회계연도 시작월</span>
              <select value={form.fiscal_year_start_month}
                      onChange={(e) => set("fiscal_year_start_month", Number(e.target.value))}
                      className="w-full rounded-md border border-slate-300 px-3 py-1.5 text-sm">
                {Array.from({ length: 12 }, (_, i) => i + 1).map((m) => (
                  <option key={m} value={m}>{m}월{m === 1 ? " (역년 1/1~12/31 — 기본)" : ""}</option>
                ))}
              </select>
            </label>
            <p className="mt-1 text-[11px] text-slate-400">
              FY N = N년 시작월 1일~+12개월−1일. 재무제표(연간)·연말결산·공시·이연법인세에 적용 —
              세무(부가세)·연말정산·4대보험은 법정 달력연도 유지.</p>
          </div>
        </div>
        <p className="mt-3 text-xs text-slate-400">
          요양기관기호는 문서 표기용입니다 — HIS/그룹웨어 연동 식별자(hospital_code)는 서버 환경설정으로 별도 관리됩니다.</p>
      </Card>

      <Card title="로고·이미지">
        <p className="mb-2 text-xs text-slate-500">기관 로고·인장 등 이미지를 등록합니다(PNG·JPG·WEBP·PDF).</p>
        <AttachmentCell entityType="org" entityId={1} gallery />
      </Card>

      <Card title="기타 항목 (커스텀)">
        <p className="mb-2 text-xs text-slate-500">문서·표기에 활용할 기타 항목(항목명·값)을 자유롭게 등록합니다.</p>
        <div className="max-w-2xl space-y-2">
          {extra.map((it, i) => (
            <div key={i} className="flex items-center gap-2">
              <input value={it.key} placeholder="항목명 (예: 설립일)"
                onChange={(e) => setExtra(extra.map((x, j) => j === i ? { ...x, key: e.target.value } : x))}
                className="w-40 rounded-md border border-slate-300 px-2 py-1.5 text-sm" />
              <input value={it.value} placeholder="값"
                onChange={(e) => setExtra(extra.map((x, j) => j === i ? { ...x, value: e.target.value } : x))}
                className="flex-1 rounded-md border border-slate-300 px-2 py-1.5 text-sm" />
              <button onClick={() => setExtra(extra.filter((_, j) => j !== i))}
                className="rounded bg-slate-100 px-2 py-1 text-xs text-slate-600 hover:bg-red-100">삭제</button>
            </div>
          ))}
          <button onClick={() => setExtra([...extra, { key: "", value: "" }])}
            className="rounded-md border border-dashed border-slate-300 px-3 py-1.5 text-sm text-slate-600 hover:bg-slate-50">
            + 항목 추가</button>
        </div>
      </Card>

      <HolidaysCard />
    </div>
  );
}

interface Holiday { id: number; on_date: string; name: string; kind: string }
interface HolidayCandidates {
  year: number; source: string;
  items: { on_date: string; name: string; kind: string; note: string; disputed: boolean; registered: boolean }[];
  review: { registered: string[]; excluded: string[]; source_note: string; reviewed_at: string } | null;
}

/** 공휴일 후보 검토(2026-10-11) — 해마다 바뀌는 공휴일(음력·대체·선거일·2026 신설 노동절·제헌절)을 원문과 대조해 고른 것만 등록.
 *  검토 기록(등록·뺀 날짜·근거)이 남으면 결정·조치함의 그 해 항목이 닫힌다. */
function HolidayCandidatesPanel({ year, onDone }: { year: number; onDone: () => void }) {
  const qc = useQueryClient();
  const toast = useToast();
  const confirm = useConfirm();
  const q = useQuery({ queryKey: ["holiday-candidates", year],
    queryFn: () => api<HolidayCandidates>(`/holidays/candidates?year=${year}`) });
  const [picked, setPicked] = useState<string[] | null>(null);
  const [note, setNote] = useState("");
  const items = q.data?.items ?? [];
  // 기본 선택 = 이미 등록된 것 + 이견 없는 후보
  const sel = picked ?? items.filter((c) => c.registered || !c.disputed).map((c) => c.on_date);
  const review = useMutation({
    mutationFn: () => api(`/holidays/candidates/${year}/review`, { method: "POST",
      body: JSON.stringify({ dates: sel, source_note: note.trim() }) }),
    onSuccess: () => { toast.ok("공휴일 후보 검토를 기록했습니다"); setNote(""); setPicked(null);
      qc.invalidateQueries({ queryKey: ["holiday-candidates"] }); qc.invalidateQueries({ queryKey: ["decisions"] }); onDone(); },
    onError: (e) => toast.err(e instanceof ApiError ? e.error.message : "검토 실패"),
  });
  if (!items.length) return null;
  const onReview = async () => {
    const out = items.filter((c) => !sel.includes(c.on_date)).map((c) => `${c.on_date} ${c.name}`);
    if (await confirm({ title: `${year}년 공휴일 후보 검토`, confirmLabel: "확정",
      body: `등록 ${sel.length}건${out.length ? `\n뺀 날짜: ${out.join(", ")}` : ""}\n대조 근거: ${note.trim()}` })) review.mutate();
  };
  return (
    <div className="mb-3 space-y-2 rounded-md border border-amber-200 bg-amber-50 p-3 text-sm">
      <p className="font-medium">{year}년 공휴일 후보 — 원문과 대조 후 등록할 날짜를 고르세요</p>
      <p className="text-xs text-slate-600">{q.data?.source}</p>
      {q.data?.review && (
        <p className="text-xs text-emerald-700">검토 완료 {String(q.data.review.reviewed_at).slice(0, 10)} · 근거: {q.data.review.source_note}
          {q.data.review.excluded.length > 0 && <> · 뺀 날짜: {q.data.review.excluded.join(", ")}</>}</p>)}
      <div className="space-y-1">
        {items.map((c) => (
          <label key={c.on_date} className="flex items-start gap-2">
            <input type="checkbox" className="mt-1" checked={sel.includes(c.on_date)} disabled={c.registered}
              onChange={() => setPicked(sel.includes(c.on_date) ? sel.filter((d) => d !== c.on_date) : [...sel, c.on_date])} />
            <span><b className="font-mono">{c.on_date}</b> {c.name}
              {c.registered && <span className="ml-1 text-xs text-emerald-700">등록됨</span>}
              {c.disputed && <span className="ml-1 text-xs text-rose-700">이견</span>}
              {c.note && <span className="block text-xs text-slate-500">{c.note}</span>}</span>
          </label>))}
      </div>
      <div className="flex flex-wrap items-end gap-2">
        <Input label="대조 근거(필수)" value={note} className="w-80" placeholder="예: 우주항공청 2027 월력요항 원문 대조"
          onChange={(e) => setNote(e.target.value)} />
        <Button size="sm" disabled={review.isPending || note.trim().length < 8} onClick={onReview}>
          {q.data?.review ? "다시 검토" : "검토 확정"}</Button>
      </div>
    </div>
  );
}

function HolidaysCard() {
  const qc = useQueryClient();
  const toast = useToast();
  const [year, setYear] = useState(new Date().getFullYear());
  const [form, setForm] = useState({ on_date: "", name: "", kind: "PUBLIC" });
  const q = useQuery({ queryKey: ["holidays", year],
    queryFn: () => api<{ items: Holiday[] }>(`/holidays?year=${year}`) });
  const refresh = () => qc.invalidateQueries({ queryKey: ["holidays"] });
  const fail = (e: unknown) => toast.err(e instanceof ApiError ? e.error.message : "처리 실패");
  const add = useMutation({
    mutationFn: () => api("/holidays", { method: "POST", body: JSON.stringify(form) }),
    onSuccess: () => { toast.ok("공휴일 등록"); setForm({ on_date: "", name: "", kind: "PUBLIC" }); refresh(); },
    onError: fail });
  const del = useMutation({
    mutationFn: (id: number) => api(`/holidays/${id}`, { method: "DELETE" }),
    onSuccess: () => { toast.ok("삭제"); refresh(); }, onError: fail });
  return (
    <Card title="공휴일 캘린더 (변동 공휴일 — 음력·대체)" action={
      <input type="number" value={year} onChange={(e) => setYear(Number(e.target.value))}
        className="w-24 rounded-md border border-slate-300 px-2 py-1 text-sm" />}>
      <p className="mb-2 text-xs text-slate-500">
        고정 양력 공휴일·주말은 자동 처리됩니다. 매년 발표되는 <b>음력 명절·대체공휴일·선거일</b>(2026부터 노동절·제헌절 포함)만 등록하세요
        (법정 기한·근태 계산에 반영). 미등록 시 해당일이 평일로 처리됩니다.</p>
      <div className="mb-3 flex flex-wrap items-end gap-2">
        <Input label="일자" type="date" value={form.on_date}
          onChange={(e) => setForm({ ...form, on_date: e.target.value })} />
        <Input label="명칭" value={form.name} placeholder="예: 추석"
          onChange={(e) => setForm({ ...form, name: e.target.value })} />
        <label className="block"><span className="mb-1 block text-xs font-medium text-slate-600">종류</span>
          <select value={form.kind} onChange={(e) => setForm({ ...form, kind: e.target.value })}
            className="rounded-md border border-slate-300 px-2 py-1.5 text-sm">
            <option value="PUBLIC">공휴일</option><option value="LUNAR">음력명절</option>
            <option value="SUBSTITUTE">대체공휴일</option><option value="TEMP">임시공휴일</option>
          </select></label>
        <Button disabled={!form.on_date || !form.name || add.isPending} onClick={() => add.mutate()}>등록</Button>
      </div>
      <HolidayCandidatesPanel key={year} year={year} onDone={refresh} />
      <div className="divide-y divide-slate-100">
        {(q.data?.items ?? []).map((h) => (
          <div key={h.id} className="flex items-center justify-between py-1.5 text-sm">
            <span><b className="font-mono">{h.on_date}</b> · {h.name}
              <span className="ml-1 text-xs text-slate-400">{h.kind}</span></span>
            <Button variant="secondary" size="sm" onClick={() => del.mutate(h.id)}>삭제</Button>
          </div>
        ))}
        {(q.data?.items ?? []).length === 0 && (
          <p className="py-3 text-sm text-slate-400">{q.isFetching ? "불러오는 중…" : `${year}년 등록된 변동 공휴일 없음`}</p>)}
      </div>
    </Card>
  );
}

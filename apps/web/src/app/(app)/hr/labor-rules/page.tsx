"use client";
/** 노동규제 룰 관리 (FR-32) — 시프트 카탈로그·임계값 조회/수정.
 *  당직(OC 24h)·온콜 등 장시간 시프트를 등록하면 전공의 4주 80h 룰이 실효화되고,
 *  임계값은 법 개정·기관 정책에 맞춰 코드 수정 없이 조정된다. 듀티 편성 경고에 즉시 반영. */
import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { ApiError, api } from "@/lib/api";
import { Badge, Button, Card, Modal, Table } from "@/components/ui";
import { useToast } from "@/components/toast";

interface ShiftRow { code: string; label: string; work_hours: number;
  is_night: boolean; counts_as_workday: boolean; active: boolean;
  start_time: string | null; end_time: string | null;
  source: string; overridden: boolean }
interface RuleRow { rule_key: string; label: string; threshold: number;
  notice_ref: string | null; default: number | null; active: boolean;
  source: string; overridden: boolean }

type ShiftForm = { code: string; label: string; work_hours: string;
  is_night: boolean; counts_as_workday: boolean; active: boolean;
  start_time: string; end_time: string; isNew: boolean };
type RuleForm = { rule_key: string; label: string; threshold: string;
  notice_ref: string; active: boolean };

const src = (s: string, ov: boolean) =>
  <Badge tone={ov ? "blue" : "gray"}>{s}</Badge>;
const flag = (b: boolean) => b ? "✓" : <span className="text-slate-400">—</span>;
const input = "w-full rounded-md border border-slate-300 px-3 py-1.5 text-sm";

export default function LaborRulesPage() {
  const qc = useQueryClient();
  const toast = useToast();
  const [shift, setShift] = useState<ShiftForm | null>(null);
  const [rule, setRule] = useState<RuleForm | null>(null);

  const shifts = useQuery({ queryKey: ["shift-types"],
    queryFn: () => api<{ items: ShiftRow[] }>("/hr/shift-types") });
  const rules = useQuery({ queryKey: ["labor-rules"],
    queryFn: () => api<{ items: RuleRow[] }>("/hr/labor-rules") });

  const saveShift = useMutation({
    mutationFn: (f: ShiftForm) => api(`/hr/shift-types/${encodeURIComponent(f.code)}`, {
      method: "PUT", body: JSON.stringify({
        label: f.label, work_hours: Number(f.work_hours), is_night: f.is_night,
        counts_as_workday: f.counts_as_workday, active: f.active,
        start_time: f.start_time || null, end_time: f.end_time || null }) }),
    onSuccess: () => { toast.ok("시프트 저장 완료 (감사기록)"); setShift(null);
      qc.invalidateQueries({ queryKey: ["shift-types"] }); },
    onError: (e) => toast.err(e instanceof ApiError ? e.error.message : "시프트 저장 실패"),
  });
  const saveRule = useMutation({
    mutationFn: (f: RuleForm) => api(`/hr/labor-rules/${encodeURIComponent(f.rule_key)}`, {
      method: "PUT", body: JSON.stringify({
        threshold: Number(f.threshold), notice_ref: f.notice_ref || null,
        active: f.active }) }),
    onSuccess: () => { toast.ok("임계값 저장 완료 (감사기록)"); setRule(null);
      qc.invalidateQueries({ queryKey: ["labor-rules"] }); },
    onError: (e) => toast.err(e instanceof ApiError ? e.error.message : "임계값 저장 실패"),
  });

  const editShift = (r: ShiftRow) => setShift({ code: r.code, label: r.label,
    work_hours: String(r.work_hours), is_night: r.is_night,
    counts_as_workday: r.counts_as_workday, active: r.active,
    start_time: r.start_time ?? "", end_time: r.end_time ?? "", isNew: false });
  const newShift = () => setShift({ code: "", label: "", work_hours: "8",
    is_night: false, counts_as_workday: true, active: true,
    start_time: "", end_time: "", isNew: true });
  const editRule = (r: RuleRow) => setRule({ rule_key: r.rule_key, label: r.label,
    threshold: String(r.threshold), notice_ref: r.notice_ref ?? "", active: r.active });

  return (
    <div className="space-y-4">
      <div>
        <h1 className="text-lg font-bold">노동규제 룰 관리</h1>
        <p className="mt-1 text-sm text-slate-500">
          근무 시프트별 시간·플래그와 근로시간 임계값을 관리합니다. 당직(예 OC 24h)·온콜 등
          장시간 시프트를 등록하면 전공의 4주 평균 80h 룰이 실효화됩니다. 미등록 항목은
          코드 기본값(법 조문 초안)이 유효하며, 변경은 듀티 편성 위반 경고에 즉시 반영됩니다.
        </p>
      </div>

      <Card title={`시프트 카탈로그 (${shifts.data?.items.length ?? 0}종)`}
        action={<Button onClick={newShift}>시프트 추가</Button>}>
        <Table headers={["코드", "명칭", "근무시간", "근무시각", "야간", "근무일 산입", "활성", "출처", ""]}
          empty="시프트 미등록 (코드 기본값 D/E/N/OFF 유효)"
          rows={(shifts.data?.items ?? []).map((r) => [
            <code key="c" className="text-xs font-semibold">{r.code}</code>,
            r.label,
            `${r.work_hours}시간`,
            r.start_time && r.end_time ? `${r.start_time}~${r.end_time}` : "-",
            flag(r.is_night), flag(r.counts_as_workday), flag(r.active),
            src(r.source, r.overridden),
            <Button key="e" variant="neutral" size="sm" onClick={() => editShift(r)}>{r.overridden ? "수정" : "재정의"}</Button>,
          ])} />
      </Card>

      <Card title={`노동규제 임계값 (${rules.data?.items.length ?? 0}종)`}
        action={<span className="text-[11px] text-slate-400">변경 시 감사기록 · 듀티 경고 즉시 반영</span>}>
        <Table headers={["룰", "임계값", "기본값", "근거 조항", "활성", "출처", ""]}
          empty="임계값 미등록 (법 조문 기본값 유효)"
          rows={(rules.data?.items ?? []).map((r) => [
            <span key="l" className="text-sm">{r.label}<br />
              <code className="text-[11px] text-slate-400">{r.rule_key}</code></span>,
            <span key="t" className={r.overridden ? "font-semibold text-blue-700" : ""}>
              {r.threshold}</span>,
            r.default !== null ? r.default : "-",
            r.notice_ref ?? "-",
            flag(r.active),
            src(r.source, r.overridden),
            <Button key="e" variant="neutral" size="sm" onClick={() => editRule(r)}>{r.overridden ? "변경" : "재정의"}</Button>,
          ])} />
      </Card>

      {shift && (
        <Modal title={shift.isNew ? "시프트 추가" : `시프트 ${shift.code} 수정`}
          onClose={() => setShift(null)}>
          <div className="space-y-3">
            <label className="block text-sm text-slate-600">코드 (≤4자, 예 OC=당직)
              <input value={shift.code} disabled={!shift.isNew} maxLength={4}
                className={`${input} ${!shift.isNew ? "bg-slate-100" : ""} uppercase`}
                onChange={(e) => setShift({ ...shift, code: e.target.value.toUpperCase() })} />
            </label>
            <label className="block text-sm text-slate-600">명칭
              <input value={shift.label} className={input}
                onChange={(e) => setShift({ ...shift, label: e.target.value })} />
            </label>
            <label className="block text-sm text-slate-600">근무시간 (0~24)
              <input value={shift.work_hours} inputMode="decimal" className={input}
                onChange={(e) => setShift({ ...shift, work_hours: e.target.value })} />
            </label>
            <div className="grid grid-cols-2 gap-3">
              <label className="block text-sm text-slate-600">시작 시각 (근태 지각판정)
                <input type="time" value={shift.start_time} className={input}
                  onChange={(e) => setShift({ ...shift, start_time: e.target.value })} />
              </label>
              <label className="block text-sm text-slate-600">종료 시각 (조퇴판정)
                <input type="time" value={shift.end_time} className={input}
                  onChange={(e) => setShift({ ...shift, end_time: e.target.value })} />
              </label>
            </div>
            <label className="flex items-center gap-2 text-sm text-slate-600">
              <input type="checkbox" checked={shift.is_night}
                onChange={(e) => setShift({ ...shift, is_night: e.target.checked })} />
              야간 (연속야간 룰 대상)
            </label>
            <label className="flex items-center gap-2 text-sm text-slate-600">
              <input type="checkbox" checked={shift.counts_as_workday}
                onChange={(e) => setShift({ ...shift, counts_as_workday: e.target.checked })} />
              근무일 산입 (연속근무일 룰 대상 · OFF는 해제)
            </label>
            <label className="flex items-center gap-2 text-sm text-slate-600">
              <input type="checkbox" checked={shift.active}
                onChange={(e) => setShift({ ...shift, active: e.target.checked })} />
              활성
            </label>
            <div className="flex justify-end gap-2 pt-1">
              <Button variant="secondary" onClick={() => setShift(null)}>취소</Button>
              <Button isLoading={saveShift.isPending}
                onClick={() => {
                  if (!shift.code.trim()) return toast.err("코드를 입력하세요");
                  if (!shift.label.trim()) return toast.err("명칭을 입력하세요");
                  const h = Number(shift.work_hours);
                  if (!Number.isFinite(h) || h < 0 || h > 24) return toast.err("근무시간은 0~24");
                  saveShift.mutate(shift);
                }}>저장</Button>
            </div>
          </div>
        </Modal>
      )}

      {rule && (
        <Modal title={`임계값 ${rule.rule_key} 변경`} onClose={() => setRule(null)}>
          <div className="space-y-3">
            <p className="text-sm font-medium text-slate-700">{rule.label}
              <code className="ml-2 text-[11px] text-slate-400">{rule.rule_key}</code></p>
            <label className="block text-sm text-slate-600">임계값 (시간/일수)
              <input value={rule.threshold} inputMode="decimal" className={input}
                onChange={(e) => setRule({ ...rule, threshold: e.target.value })} />
            </label>
            <label className="block text-sm text-slate-600">근거 조항
              <input value={rule.notice_ref} placeholder="예 전공의법 §7①" className={input}
                onChange={(e) => setRule({ ...rule, notice_ref: e.target.value })} />
            </label>
            <label className="flex items-center gap-2 text-sm text-slate-600">
              <input type="checkbox" checked={rule.active}
                onChange={(e) => setRule({ ...rule, active: e.target.checked })} />
              활성 (해제 시 코드 기본값으로 폴백)
            </label>
            <div className="flex justify-end gap-2 pt-1">
              <Button variant="secondary" onClick={() => setRule(null)}>취소</Button>
              <Button isLoading={saveRule.isPending}
                onClick={() => {
                  const t = Number(rule.threshold);
                  if (!Number.isFinite(t) || t <= 0) return toast.err("임계값은 0보다 커야 합니다");
                  saveRule.mutate(rule);
                }}>저장</Button>
            </div>
          </div>
        </Modal>
      )}
    </div>
  );
}

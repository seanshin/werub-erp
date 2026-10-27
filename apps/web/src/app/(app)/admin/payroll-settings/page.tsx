"use client";
/** 급여 정책 설정 — 성과급 원천징수 방식·사업주 4대보험 부담금. 세무 정책이라 관리자(회계)가 설정. */
import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { ApiError, api } from "@/lib/api";
import { Button, Card, Input } from "@/components/ui";
import { useToast } from "@/components/toast";
import { RateDiffCard, RateSettlementCard, StatutoryParamsCard, WithholdingTableCard } from "@/components/statutory-params-card";

type Method = "NONE" | "FLAT" | "COMBINED";
interface PayrollSettings {
  configured: boolean;
  incentive_withholding_method: Method;
  incentive_withholding_rate: number;
  combined_months: number;
  employer_burden_posting: boolean;
  employer_ei_rate: number;
  employer_wc_rate: number;
  night_pay_mode: NightMode;
  incentive_grade_link: boolean;
  updated_at: string | null;
}
type NightMode = "FLAT" | "STATUTORY" | "MAX";
type Draft = {
  method: Method;
  flatRate: string;       // % 입력
  months: string;
  employerOn: boolean;
  eiRate: string;         // %
  wcRate: string;         // %
  nightMode: NightMode;   // 야간수당 산정(M-11)
  gradeLink: boolean;     // 인사평가 등급→성과급 배율(P1-1)
};

const NIGHT_HELP: Record<NightMode, string> = {
  FLAT: "정액 단가(수당 단가 NIGHT_HOURLY, 기본 12,000원/h)로 지급합니다(현행).",
  STATUTORY: "통상시급(기본급÷209h)×0.5 야간가산(근기법 §56③)으로 지급합니다.",
  MAX: "정액과 법정가산 중 큰 금액으로 지급합니다(법정 미달 방지).",
};

const METHOD_HELP: Record<Method, string> = {
  NONE: "성과급에서 원천징수하지 않고 연말정산에서 정산합니다(현행).",
  FLAT: "성과급 지급액에 아래 정률을 소득세로 원천징수합니다(간이).",
  COMBINED: "성과급을 지급대상기간으로 나눠 당월 급여에 합산한 간이세액표 세액을 원천징수합니다(지방소득세 포함).",
};

const pct = (v: number) => String(+(v * 100).toFixed(3));

export default function PayrollSettingsPage() {
  const qc = useQueryClient();
  const toast = useToast();
  const [draft, setDraft] = useState<Draft | null>(null);

  const q = useQuery({
    queryKey: ["payroll-settings"],
    queryFn: () => api<PayrollSettings>("/admin/payroll-settings"),
  });
  const cur = q.data;
  const form: Draft = draft ?? {
    method: cur?.incentive_withholding_method ?? "NONE",
    flatRate: cur ? pct(cur.incentive_withholding_rate) : "",
    months: String(cur?.combined_months ?? 1),
    employerOn: cur?.employer_burden_posting ?? false,
    eiRate: cur ? pct(cur.employer_ei_rate) : "1.15",
    wcRate: cur ? pct(cur.employer_wc_rate) : "0.7",
    nightMode: cur?.night_pay_mode ?? "FLAT",
    gradeLink: cur?.incentive_grade_link ?? false,
  };
  const set = (patch: Partial<Draft>) => setDraft({ ...form, ...patch });

  const flatNum = parseFloat(form.flatRate);
  const monthsNum = parseInt(form.months, 10);
  const eiNum = parseFloat(form.eiRate);
  const wcNum = parseFloat(form.wcRate);
  const flatBad = form.method === "FLAT" && !(flatNum > 0 && flatNum < 100);
  const monthsBad = form.method === "COMBINED" && !(monthsNum >= 1 && monthsNum <= 12);
  const eiBad = form.employerOn && !(eiNum >= 0 && eiNum < 100);
  const wcBad = form.employerOn && !(wcNum >= 0 && wcNum < 100);
  const invalid = flatBad || monthsBad || eiBad || wcBad;

  const save = useMutation({
    mutationFn: () => api("/admin/payroll-settings", {
      method: "PUT",
      body: JSON.stringify({
        incentive_withholding_method: form.method,
        incentive_withholding_rate: form.method === "FLAT" ? flatNum / 100 : 0,
        combined_months: form.method === "COMBINED" ? monthsNum : 1,
        employer_burden_posting: form.employerOn,
        employer_ei_rate: (eiNum || 0) / 100,
        employer_wc_rate: (wcNum || 0) / 100,
        night_pay_mode: form.nightMode,
        incentive_grade_link: form.gradeLink,
      }),
    }),
    onSuccess: () => {
      toast.ok("저장 완료 — 성과급은 이후 승인분부터, 사업주부담은 이후 급여 확정분부터 적용");
      setDraft(null);
      qc.invalidateQueries({ queryKey: ["payroll-settings"] });
    },
    onError: (e: unknown) => toast.err(e instanceof ApiError ? e.error.message : "저장 실패 (hr.payroll 권한)"),
  });
  const dirty = draft !== null;

  return (
    <div className="space-y-4">
      <div className="flex items-center justify-between">
        <h1 className="text-lg font-bold">급여 정책 설정</h1>
        <Button isLoading={save.isPending} disabled={!dirty || invalid}
                onClick={() => save.mutate()}>저장</Button>
      </div>
      <p className="text-xs text-slate-500">
        성과급 원천징수·사업주 4대보험 부담금 등 세무·회계 정책을 설정합니다. 기본값은 <b>미적용</b>이며,
        변경은 소급되지 않습니다(성과급=이후 승인분·사업주부담=이후 확정분).</p>
      {q.isError && <p className="text-sm text-red-600">급여 정책 조회 권한이 없습니다 (hr.payroll)</p>}

      <Card title="성과급(인센티브) 원천징수">
        {cur?.updated_at && (
          <p className="mb-3 text-xs text-slate-500">
            최근 수정 {cur.updated_at.slice(0, 16).replace("T", " ")}</p>
        )}
        <div className="max-w-lg space-y-3">
          <label className="block">
            <span className="mb-1 block text-sm text-slate-600">원천징수 방식</span>
            <select className="w-full rounded-lg border border-slate-300 px-3 py-2 text-sm"
                    value={form.method}
                    onChange={(e) => set({ method: e.target.value as Method })}>
              <option value="NONE">미적용 — 연말정산에서 정산(현행)</option>
              <option value="FLAT">정률(FLAT) — 성과급 × 정률</option>
              <option value="COMBINED">상여 합산(COMBINED) — 간이세액표 근사</option>
            </select>
          </label>
          <p className="rounded-md bg-slate-50 px-3 py-2 text-xs text-slate-500">{METHOD_HELP[form.method]}</p>
          {form.method === "FLAT" && (
            <Input label="원천징수 정률 (%)" type="number" step="0.1" value={form.flatRate}
                   placeholder="예: 3.3" onChange={(e) => set({ flatRate: e.target.value })} />
          )}
          {flatBad && <p className="text-xs text-red-600">정률은 0 초과 100 미만이어야 합니다.</p>}
          {form.method === "COMBINED" && (
            <Input label="상여 지급대상기간 (개월, 1~12)" type="number" step="1" value={form.months}
                   placeholder="예: 1" onChange={(e) => set({ months: e.target.value })} />
          )}
          {monthsBad && <p className="text-xs text-red-600">지급대상기간은 1~12개월이어야 합니다.</p>}
        </div>
        <p className="mt-3 text-xs text-slate-400">
          FLAT/COMBINED 선택 시 성과급 승인마다 <b>성과급 소득세</b> 공제 라인과 예수금 전기가 함께 반영됩니다.
          정식 상여 세액표는 세무 검토 후 확장 예정입니다.</p>
      </Card>

      <Card title="사업주 4대보험 부담금 (법정부담금)">
        <label className="flex items-center gap-2 text-sm">
          <input type="checkbox" checked={form.employerOn}
                 onChange={(e) => set({ employerOn: e.target.checked })} />
          급여 확정 시 사업주부담금을 GL 전기(DR 복리후생비 / CR 미지급금)
        </label>
        {form.employerOn && (
          <div className="mt-3 grid max-w-lg grid-cols-2 gap-3">
            <Input label="고용보험 사업주 요율 (%)" type="number" step="0.01" value={form.eiRate}
                   placeholder="예: 1.15" onChange={(e) => set({ eiRate: e.target.value })} />
            <Input label="산재 요율 (%, 업종별)" type="number" step="0.01" value={form.wcRate}
                   placeholder="예: 0.7" onChange={(e) => set({ wcRate: e.target.value })} />
          </div>
        )}
        {(eiBad || wcBad) && <p className="mt-1 text-xs text-red-600">요율은 0 이상 100 미만이어야 합니다.</p>}
        <p className="mt-3 text-xs text-slate-400">
          국민연금·건강보험·장기요양은 근로자와 동률(자동), 고용보험 사업주·산재는 위 요율을 사용합니다.
          산재 요율은 업종·사업장별로 다르니 공단 고지 요율을 입력하세요.</p>
      </Card>

      <Card title="야간수당 산정 방식 (간호사 — 근기법 §56③)">
        <div className="max-w-lg space-y-3">
          <label className="block">
            <span className="mb-1 block text-sm text-slate-600">산정 방식</span>
            <select className="w-full rounded-lg border border-slate-300 px-3 py-2 text-sm"
                    value={form.nightMode}
                    onChange={(e) => set({ nightMode: e.target.value as NightMode })}>
              <option value="FLAT">FLAT — 정액 단가(현행)</option>
              <option value="STATUTORY">STATUTORY — 통상시급×0.5 법정가산</option>
              <option value="MAX">MAX — 정액·법정 중 큰 값(미달 방지)</option>
            </select>
          </label>
          <p className="text-xs text-slate-500">{NIGHT_HELP[form.nightMode]}</p>
          <p className="text-xs text-slate-400">
            변경은 이후 급여 생성분부터 적용됩니다. 정액 단가 자체는 급여 기초정보 화면의
            수당 단가(NIGHT_HOURLY·발효일 이력)에서 조정합니다.</p>
        </div>
      </Card>

      <StatutoryParamsCard />
      <RateDiffCard />
      <RateSettlementCard />
      <WithholdingTableCard />

      <Card title="인사평가 등급 → 성과급 연계">
        <div className="max-w-lg space-y-2">
          <label className="flex items-center gap-2 text-sm">
            <input type="checkbox" checked={form.gradeLink}
              onChange={(e) => set({ gradeLink: e.target.checked })} />
            해당 연도 확정 인사평가 등급을 성과급 산정에 배율로 반영
          </label>
          <p className="text-xs text-slate-500">
            배율 — S 1.2 · A 1.1 · B 1.0 · C 0.9 · D 0.8. 확정 평가가 없으면 배율 없음(현행과 동일).
            이후 산정분부터 적용됩니다.</p>
        </div>
      </Card>
    </div>
  );
}

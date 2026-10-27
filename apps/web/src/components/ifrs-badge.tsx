"use client";
/** 화면별 K-IFRS 처리 안내 플로팅 마크 — 우측 하단. 클릭 시 적용 기준·처리 상세 + 당해 실제 처리 금액. */
import { usePathname } from "next/navigation";
import { useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { api } from "@/lib/api";
import { won } from "@/lib/format";

interface Std { code: string; name: string; treat: string }
interface Entry { prefix: string; screen: string; items: Std[] }

// 라우트 접두 → 해당 화면에서 처리되는 K-IFRS 기준·회계처리(가장 긴 접두 우선 매칭)
const MAP: Entry[] = [
  { prefix: "/finance-statements", screen: "재무제표·공시", items: [
    { code: "1001", name: "재무제표 표시", treat: "포괄손익(당기+OCI)·기능별 비용·전기비교" },
    { code: "1007", name: "현금흐름표", treat: "영업/투자/재무 + 직접법 병기" },
    { code: "1034", name: "중간재무보고", treat: "분기 누적·전분기 비교" },
    { code: "1108", name: "영업부문", treat: "진료과별 수익·손익·자산, 보고임계 10%" },
    { code: "1024", name: "특수관계자", treat: "거래·잔액 공시 + 적요 자동탐지(보조)" },
    { code: "1037", name: "충당부채·우발부채", treat: "우발 등록 + 충당부채 전환(5680/2130)" },
    { code: "1010", name: "보고기간후사건", treat: "수정/비수정 구분" },
    { code: "1113", name: "공정가치 측정", treat: "공정가치 서열(Level 1~3) 주석" },
  ] },
  { prefix: "/finance-periods", screen: "결산·기말 측정", items: [
    { code: "1109", name: "대손충당금(ECL)", treat: "연령 매트릭스·forward-looking 오버레이·결산분개" },
    { code: "1012", name: "법인세(이연)", treat: "일시적차이 DTA/DTL·유효세율" },
    { code: "1115", name: "수익인식", treat: "미청구수익 발생주의(1135/4110)·차기 환입" },
    { code: "1008", name: "회계정책·추정변경·오류", treat: "소급/전진 등록·공시" },
  ] },
  { prefix: "/assets", screen: "유형·무형자산", items: [
    { code: "1016", name: "유형자산", treat: "원가·감가상각·재평가모형(OCI)" },
    { code: "1036", name: "자산손상·환입", treat: "회수가능액 비교·손상/환입(재평가잉여금 우선)" },
    { code: "1038", name: "무형자산", treat: "정액상각·손상/환입·처분(생애주기)" },
    { code: "1023", name: "차입원가", treat: "적격자산 건설기간 차입원가 자본화" },
  ] },
  { prefix: "/inventory-valuation", screen: "재고 평가", items: [
    { code: "1002", name: "재고자산", treat: "저가법(NRV)·원가흐름(FIFO/WAVG)·매출원가(COGS)" },
  ] },
  { prefix: "/finance-leases", screen: "리스", items: [
    { code: "1116", name: "리스", treat: "사용권자산/리스부채·단기소액 면제·재측정·만기분석" },
  ] },
  { prefix: "/finance-corporate-accounts", screen: "법인 자금·외화", items: [
    { code: "1021", name: "환율변동효과", treat: "외화잔액 기말환율 환산·환산차손익(4650/5670)" },
  ] },
  { prefix: "/finance-accounts", screen: "계정과목", items: [
    { code: "1001", name: "비용의 기능별 표시", treat: "매출원가/판관비(cost_function) 분류" },
  ] },
  { prefix: "/hr", screen: "인사·급여", items: [
    { code: "1019", name: "종업원급여", treat: "퇴직급여 보험수리 DBO·재측정 OCI(3410)" },
  ] },
  { prefix: "/claims", screen: "보험·청구", items: [
    { code: "1115", name: "변동대가", treat: "보험삭감 추정·수익차감/환불부채(2155)" },
  ] },
  { prefix: "/finance-voucher", screen: "전표", items: [
    { code: "1001", name: "복식부기 기반", treat: "차대 균형·멱등·Maker-Checker·기간잠금" },
  ] },
  { prefix: "/finance-tax-invoices", screen: "세금계산서", items: [
    { code: "1115", name: "수익인식", treat: "재화·용역 이전 시점 수익 인식 근거" },
  ] },
];

function lookup(pathname: string): Entry | null {
  const hits = MAP.filter((e) => pathname === e.prefix || pathname.startsWith(e.prefix));
  if (!hits.length) return null;
  return hits.sort((a, b) => b.prefix.length - a.prefix.length)[0];
}

export function IFRSBadge() {
  const pathname = usePathname() ?? "";
  const [open, setOpen] = useState(false);
  const entry = lookup(pathname);
  const active = !!entry;
  const year = new Date().getFullYear();
  // 당해 IFRS 처리 활동(기준별 금액·건수) — 패널 열 때만 조회, 권한 없으면 조용히 무시
  const activity = useQuery({
    queryKey: ["ifrs-activity", year],
    queryFn: () => api<{ activity: Record<string, { amount: number; count: number }> }>(
      `/finance/ifrs-activity?year=${year}`),
    enabled: open, retry: false, staleTime: 300_000,
  });
  const act = activity.data?.activity ?? {};

  return (
    <div className="fixed bottom-4 right-4 z-40">
      {open && (
        <>
          <div className="fixed inset-0 z-0" onClick={() => setOpen(false)} aria-hidden />
          <div className="absolute bottom-12 right-0 z-10 w-80 max-w-[88vw] rounded-xl border border-slate-200 bg-white shadow-xl">
            <div className="flex items-center justify-between border-b border-slate-100 px-4 py-2.5">
              <p className="text-sm font-semibold text-slate-800">
                K-IFRS 처리 {entry && <span className="text-slate-400">· {entry.screen}</span>}
              </p>
              <button onClick={() => setOpen(false)}
                className="text-xs text-slate-400 hover:text-slate-600">닫기 ✕</button>
            </div>
            <div className="max-h-[60vh] overflow-y-auto p-3">
              {entry ? (
                <ul className="space-y-2">
                  {entry.items.map((s) => (
                    <li key={s.code + s.name} className="rounded-lg bg-slate-50 p-2.5">
                      <p className="flex items-center justify-between text-[13px] font-semibold text-slate-800">
                        <span>
                          <span className="mr-1.5 rounded bg-blue-600 px-1.5 py-0.5 text-[10px] font-bold text-white">
                            KIFRS {s.code}</span>
                          {s.name}
                        </span>
                        {act[s.code] && (
                          <span className="ml-2 shrink-0 rounded bg-emerald-50 px-1.5 py-0.5 text-[10px] font-semibold text-emerald-700">
                            {year} {won(act[s.code].amount)}·{act[s.code].count}건
                          </span>
                        )}
                      </p>
                      <p className="mt-1 text-xs leading-relaxed text-slate-500">{s.treat}</p>
                    </li>
                  ))}
                </ul>
              ) : (
                <p className="px-1 py-3 text-xs leading-relaxed text-slate-500">
                  이 화면에는 직접적인 K-IFRS 측정·표시 처리가 없습니다. 회계 측정·결산·공시는
                  <span className="font-medium text-slate-700"> 재무(결산·재무제표·자산·재고·리스)</span> 메뉴에서 처리됩니다.
                </p>
              )}
              <p className="mt-3 border-t border-slate-100 pt-2 text-[10px] text-slate-400">
                모든 회계 조정은 결산분개(전표)로 귀결·역분개 가능. 부합도 ~95%+ (보험계리·세무조정·외부감사 제외).
              </p>
            </div>
          </div>
        </>
      )}
      <button onClick={() => setOpen((v) => !v)}
        title="이 화면의 K-IFRS 처리 보기"
        className={`flex items-center gap-1.5 rounded-full px-3 py-2 text-xs font-bold shadow-lg ring-1 transition ${
          active
            ? "bg-blue-600 text-white ring-blue-700 hover:bg-blue-700"
            : "bg-white text-slate-400 ring-slate-200 hover:text-slate-600"}`}>
        <span className="flex h-4 w-4 items-center justify-center rounded-full bg-white/20 text-[9px]">✓</span>
        IFRS
        {active && (
          <span className="flex h-4 min-w-4 items-center justify-center rounded-full bg-white px-1 text-[10px] font-bold text-blue-700">
            {entry.items.length}
          </span>
        )}
      </button>
    </div>
  );
}

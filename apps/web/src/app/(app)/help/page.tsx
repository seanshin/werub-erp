"use client";
/** 사용 설명서 — 화면별 세부 기능 + 작업 순서 상세 매뉴얼(HTML). 메뉴 /help. */
import { useState } from "react";

type Screen = {
  name: string; menu?: string; purpose: string;
  features?: string[]; steps?: string[]; note?: string; warn?: string;
};
type Area = { id: string; title: string; intro?: string; screens: Screen[] };

const AREAS: Area[] = [
  {
    id: "login", title: "1. 로그인 · 접속",
    intro: "ERP는 세 가지 방법으로 접속합니다. 최초 1회는 HIS SSO로 들어와 2FA를 등록한 뒤, 이후 PIN+생체로 직접 로그인할 수 있습니다.",
    screens: [
      {
        name: "HIS SSO 접속", purpose: "HIS 포털 로그인 상태에서 ERP로 무인증 진입.",
        steps: ["HIS 포털에 로그인한다.", "사이드바/상단의 ERP 버튼을 누른다.",
          "자동으로 ERP 대시보드로 이동(이메일·비밀번호 입력 없음)."],
        note: "HIS 계정 권한이 ERP 접근을 결정하고, ERP 내부 역할은 별도(3번 참고).",
      },
      {
        name: "PIN + 생체인증 로그인", menu: "/erp/login",
        purpose: "스마트폰 지문/Face ID + PIN으로 ERP에 직접 로그인(2FA).",
        steps: ["로그인 화면에서 이메일과 PIN(숫자 4~8자리)을 입력한다.",
          "‘생체인증으로 로그인’을 누른다.",
          "기기의 지문/Face ID 창이 뜨면 인증한다.",
          "인증 성공 시 대시보드로 이동."],
        warn: "사전에 ‘내 보안(2FA)’에서 PIN과 패스키를 등록해야 합니다(2번). 생체창 취소 시 다시 시도.",
      },
      {
        name: "비밀번호 로그인(폴백)", purpose: "생체 미지원 환경용 대체 수단.",
        steps: ["로그인 화면 하단 ‘비밀번호로 로그인’을 누른다.",
          "이메일·비밀번호 입력 후 로그인."],
        note: "비밀번호 5회 실패 시 15분 잠금.",
      },
    ],
  },
  {
    id: "security", title: "2. 내 보안 (2FA 등록)",
    intro: "‘시스템 → 내 보안(2FA)’(/erp/security)에서 본인이 직접 PIN과 스마트폰 생체인증(패스키)을 등록·관리합니다.",
    screens: [
      {
        name: "① PIN 설정",
        purpose: "로그인 1단계(지식 요소)로 쓰는 숫자 PIN 등록.",
        steps: ["‘내 보안(2FA)’ 화면을 연다.", "PIN(숫자 4~8자리)을 입력하고 ‘PIN 확인’에 동일하게 재입력.",
          "‘PIN 저장’을 누른다."],
        note: "PIN은 변경 시 같은 화면에서 다시 저장하면 갱신됩니다.",
      },
      {
        name: "② 생체인증(패스키) 등록",
        purpose: "로그인 2단계(소유·생체 요소)로 쓰는 패스키 등록.",
        steps: ["기기 별칭(예: ‘내 아이폰’)을 입력(선택).",
          "‘이 기기에 생체인증 등록’을 누른다.",
          "기기의 지문/Face ID로 승인한다.",
          "등록된 패스키가 목록에 나타난다(여러 기기 등록 가능, 삭제 가능)."],
        warn: "생체인증은 HTTPS + 지원 기기에서만 동작하며, 등록한 그 기기/브라우저에서 사용됩니다.",
      },
    ],
  },
  {
    id: "admin", title: "3. 권한 · 메뉴 관리 (관리자)",
    intro: "HIS는 ERP 접근만 부여하고, ERP 내부 역할·메뉴 노출은 ERP 관리자가 별도 관리합니다.",
    screens: [
      {
        name: "사용자 권한", menu: "/erp/admin/users",
        purpose: "ERP 사용자의 역할(원장·관리자·부서장·직원)과 계정 활성 상태 관리.",
        features: ["역할별/활성별 사용자 목록", "역할 변경(드롭다운)", "활성/비활성 토글"],
        steps: ["‘시스템 → 사용자 권한’을 연다.",
          "대상 사용자 행의 역할 드롭다운에서 새 역할을 선택(즉시 반영).",
          "필요 시 ‘비활성화/활성화’로 계정을 막거나 푼다."],
        note: "역할/활성 변경 시 해당 사용자는 강제 재인증됩니다. 본인 역할·마지막 ADMIN 강등은 차단(잠금 방지).",
      },
      {
        name: "메뉴 권한", menu: "/erp/admin/menu",
        purpose: "역할별로 사이드바에 보이는 메뉴를 설정.",
        steps: ["‘시스템 → 메뉴 권한’을 연다.",
          "메뉴×역할 매트릭스에서 체크박스로 노출을 켜고 끈다.",
          "우측 상단 ‘저장’을 누른다.",
          "사용자는 새로고침/재로그인 시 반영된다."],
        warn: "메뉴 노출과 실제 접근 권한은 별개입니다. 메뉴를 숨겨도 데이터 접근은 서버 정책이 차단하고, 권한이 없으면 메뉴를 열어도 거부됩니다.",
      },
    ],
  },
  {
    id: "patient", title: "4. 원무 (수납 · 예약 · 환자)",
    screens: [
      {
        name: "수납 · 일마감", menu: "/erp/patients/closing",
        purpose: "진료비 수납 처리와 일별 마감.",
        steps: ["접수/진료 건의 수납 금액을 확인한다.",
          "결제수단(현금/카드 등)으로 수납을 등록한다.",
          "영업 종료 시 ‘일마감’으로 당일 수납을 확정한다."],
        warn: "일마감 후에는 직접 정정이 막힙니다. 정정은 재무의 역분개 경로로 처리합니다.",
      },
      { name: "예약", menu: "/erp/patients/reservations", purpose: "예약 등록·조회.",
        steps: ["환자·일시·진료과를 선택해 예약을 생성한다.", "목록에서 예약 상태를 관리한다."] },
      { name: "환자 · 접수", menu: "/erp/patients", purpose: "환자 등록과 접수.",
        steps: ["신규 환자 등록 또는 기존 환자 검색.", "진료과·구분을 지정해 접수한다."],
        note: "HIS 청구가 발생하면 진료 라인이 자동 수신되어 진료비 책정·사전심사로 이어집니다." },
    ],
  },
  {
    id: "hr", title: "5. 인사 · 급여",
    intro: "직원 마스터부터 급여 산정·법정처리·증명서까지. 부서장 이상 권한이 필요한 화면이 많습니다.",
    screens: [
      { name: "직원", menu: "/erp/hr/employees", purpose: "직원 인사 마스터(직군·고용형태·부서).",
        steps: ["직원을 검색/선택한다.", "인사 정보를 확인·수정한다."],
        note: "직원 다수는 HIS/그룹웨어에서 동기화됩니다." },
      { name: "급여 기초정보", menu: "/erp/hr/onboarding", purpose: "보수 프로필·수당 기초 설정.",
        steps: ["대상 직원의 보수 프로필을 입력한다.", "급여 생성의 기준이 됩니다."] },
      { name: "급여", menu: "/erp/hr/payroll", purpose: "월 급여 생성·확인.",
        steps: ["급여월을 선택한다.", "일괄 생성(generate-bulk)으로 급여를 산정한다.",
          "항목(기본급·수당·공제)을 확인한다."],
        note: "근태·듀티(야간·당직)·OT가 자동 반영됩니다." },
      { name: "근태 / 듀티", menu: "/erp/hr/attendance · /erp/hr/duties", purpose: "근태 집계와 듀티 편성.",
        steps: ["듀티 화면에서 직원별 근무(주간/야간/오프)를 배정한다.",
          "저장 시 노동규제(주52h·연속 야간/근무일) 위반은 경고로 표시된다(저장은 가능)."],
        warn: "전공의 80h 등 일부 규제는 인사팀 임계값 확정 후 활성화됩니다." },
      { name: "4대보험·연말정산 / 4대보험 신고", menu: "/erp/hr/statutory · /erp/hr/insurance-reports",
        purpose: "법정 계산과 공단 신고 집계.",
        steps: ["연/월을 선택한다.", "4대보험·퇴직금·연말정산 계산 결과를 확인한다.",
          "신고 집계를 Excel로 내려받는다."] },
      { name: "증명서", menu: "/erp/hr/certificates", purpose: "재직·경력 등 증명서 발급.",
        steps: ["직원·증명서 종류를 선택한다.", "발급(PDF)하고 발급 이력을 관리한다."] },
      { name: "기타", menu: "의료진 계약·성과급 · 휴직/퇴직 처리 · 퇴직급여충당 · 법정교육/검진",
        purpose: "의료진 개별 계약·인센티브, 휴직·복직·퇴사, 퇴직충당부채, 법정의무교육·건강검진 추적." },
    ],
  },
  {
    id: "claims", title: "6. 의료보험 (사전심사 · 삭감)",
    intro: "HIS 청구를 자동 사전심사하고, 심사결과통보서 기반 삭감·이의신청을 관리합니다.",
    screens: [
      {
        name: "사전심사", menu: "/erp/claims/pre-review",
        purpose: "청구 전 삭감 위험을 룰엔진(+AI 보조)으로 적발하고 차트별 케이스로 관리.",
        features: ["워크리스트(차트 케이스, 진료월 탭·상태 필터)", "실행(룰/AI 평가)", "통계", "룰", "질의", "자동(자동 처리 내역)"],
        steps: ["‘워크리스트’ 탭에서 진료월·상태로 케이스를 필터한다.",
          "케이스를 열어 적발 내역(룰·코드·사유·금액)을 확인한다.",
          "각 적발에 결정을 기록한다: 수용(ACCEPT, 실제 삭감 가능)·기각(DISMISS, 오탐).",
          "의료진 확인이 필요하면 ‘질의’로 회신을 요청한다.",
          "필요 시 ‘AI 종합검토’로 미해소 적발을 일괄 진단(참고용·결정은 심사자)."],
        note: "🤖 자동 배지 = 시스템 자동 판정(무적발 자동 이상없음·확정 오탐 자동 기각). 모두 사람이 되돌릴 수 있습니다. 청구가 들어오면 수 분 내 자동 적발됩니다.",
      },
      { name: "삭감 · 이의신청", menu: "/erp/claims", purpose: "통보서 삭감 관리와 이의신청.",
        steps: ["수신된 삭감 내역을 확인한다.", "이의신청 대상을 선정·접수한다.",
          "기한(D-14/7/3) 알림에 따라 처리한다."] },
    ],
  },
  {
    id: "scm", title: "7. 재고 · 구매",
    screens: [
      {
        name: "발주 보드 (요청→승인→입고)", menu: "/erp/inventory",
        purpose: "발주 생명주기 관리. 입고 시 재고 증가·미지급금 발생(procure-to-pay).",
        steps: ["발주 보드에서 발주를 요청한다(공급처·품목·수량).",
          "관리자가 ‘승인’한다(상태 APPROVED).",
          "물품 도착 시 로트·유효기간과 함께 ‘입고’한다(상태 RECEIVED).",
          "발주 행의 📎로 거래명세서·납품서를 첨부한다."],
        note: "입고분만큼 미지급금이 자동 생성되어 세금계산서 대사·지급으로 이어집니다.",
      },
      { name: "품목 · 재고 · 마약류", menu: "/erp/inventory", purpose: "품목·재고 현황과 출고(FIFO)·마약류 수불부.",
        steps: ["품목 목록에서 재고·안전재고를 확인한다.", "출고는 FIFO로 처리된다.",
          "마약류는 수불부(append-only)로 기록하며 정정은 별도 규칙."] },
      { name: "재고 평가 · 실사 · 위수탁", menu: "/erp/inventory-valuation · -stocktake · -contracts",
        purpose: "FIFO 재고 평가, 실사 조정(감모손실), 위수탁(급식·세탁 등) 월 정산.",
        steps: ["평가 화면에서 재고자산 가액을 확인한다.",
          "실사 화면에서 실사 수량을 입력해 차이를 조정한다.",
          "위수탁 계약의 월 위탁료를 미지급금으로 계상한다."] },
    ],
  },
  {
    id: "finance", title: "8. 재무 · 회계",
    intro: "모든 거래는 전표(복식부기)로 기록됩니다. 자동 전표는 즉시 반영, 수동 전표는 승인 후 반영됩니다.",
    screens: [
      {
        name: "전표 · 예산", menu: "/erp/finance",
        purpose: "수동 전표 작성·승인, 예산 집행 현황, 월별 정산.",
        features: ["예산 통제(예산/집행/약정/가용/상태)", "월별 정산", "수동 전표 작성(분개행 차/대·균형)", "전표 목록(상태·증빙·조치)"],
        steps: ["‘수동 전표 작성’에서 전표일자·적요를 입력한다.",
          "분개 행을 추가해 계정코드·차/대·금액을 입력한다(차변합=대변합 ‘균형’ 확인).",
          "‘초안 생성’ → 전표가 DRAFT로 만들어진다(아직 장부 미반영).",
          "전표 목록에서 ‘상신’(→PENDING).",
          "다른 권한자가 ‘승인’하면 POSTED로 장부에 반영된다(작성자≠승인자).",
          "반려 시 사유를 입력한다. 전표 행 📎로 영수증 등 증빙을 첨부한다."],
        warn: "DRAFT/PENDING 전표는 재무제표·예산·마감 집계에 포함되지 않습니다(승인 시 반영).",
      },
      {
        name: "결산 · 재무제표", menu: "/erp/finance-statements",
        purpose: "기간별 재무제표 일괄 조회·Excel 출력.",
        features: ["시산표", "손익계산서(+전년 동기 비교)", "재무상태표(대차 정합 배지)",
          "기본금변동계산서", "현금흐름표(간접법·정합 배지)", "재무비율·경영 KPI"],
        steps: ["상단에서 조회 기간(시작~종료)을 선택한다.",
          "각 카드에서 재무제표를 확인한다(대차/현금 정합 배지로 검증).",
          "필요 시 시산표·기본금변동계산서 등을 Excel로 내려받는다."],
        note: "현금흐름표는 ‘기초현금+순증감=기말현금’이, 재무상태표는 ‘자산=부채+자본’이 자동 검증됩니다.",
      },
      {
        name: "기간 마감 (월결산)", menu: "/erp/finance-periods",
        purpose: "월 단위 장부 마감과 재무제표 확정 스냅샷 보존.",
        steps: ["마감할 월을 선택한다.",
          "‘마감 전 점검’으로 미결 전표·감가 미계상·미배부 원가·미대사 세금계산서·차대 불일치를 확인한다.",
          "경고를 해소한 뒤 ‘마감’한다(필요 시 점검 강제).",
          "마감 시 재무제표 4종이 확정 스냅샷으로 보존된다."],
        warn: "마감 기간에는 전표가 생성되지 않습니다. 정정은 ‘재개방’ 또는 역분개로.",
      },
      { name: "채권·채무·자금 / 자금수지 예측", menu: "/erp/finance-settlement · -cash-forecast",
        purpose: "미수금·미지급금 수금/지급·aging, 향후 자금 포지션 예측.",
        steps: ["채권/채무 목록에서 수금·지급을 등록한다(전표 자동).",
          "aging으로 연체를 관리한다.", "자금수지 예측으로 월별 현금 포지션을 본다."] },
      { name: "고정자산 / 리스 / 원가", menu: "/erp/assets · /erp/finance-leases · /erp/costing*",
        purpose: "고정자산 취득·감가·처분·손상·재평가, 리스(K-IFRS16), ABC 원가·진료과 손익.",
        steps: ["자산 등록 후 월 감가상각을 실행한다(전표 자동).",
          "원가 화면에서 부서·진료과별 배부 결과와 손익을 확인한다."] },
    ],
  },
  {
    id: "tax", title: "9. 세무",
    screens: [
      { name: "원천징수 · 신고", menu: "/erp/tax", purpose: "원천징수이행상황·4대보험 신고·사업장현황·부가세 집계 보조.",
        steps: ["귀속 월/연을 선택한다.", "원천징수·4대보험 집계와 신고서식(Excel)을 확인·출력한다.",
          "부가세 집계(과세 매출·매입세액·납부추정)를 참고한다."],
        note: "의료기관은 의료용역 면세 — 부가세는 과세분만 신고 대상, 공통매입 안분·서식은 세무사 확정." },
      {
        name: "전자세금계산서", menu: "/erp/finance-tax-invoices",
        purpose: "매입 세금계산서 수취·대사(미지급금 연결)와 증빙 보관.",
        steps: ["수기 등록: 승인번호·작성일·공급자(사업자번호/상호)·공급가액·세액·종류 입력 후 ‘등록’.",
          "또는 홈택스 CSV를 ‘CSV 업로드’로 일괄 등록.",
          "수취 목록에서 미대사 건의 ‘대사’를 눌러 해당 미지급금과 연결한다.",
          "각 행의 📎로 세금계산서 스캔본을 첨부·보관한다."],
        warn: "지급 전 증빙 대사를 완료하세요(미대사 경고 표시).",
      },
    ],
  },
  {
    id: "attach", title: "10. 증빙 문서 첨부 · 보관",
    intro: "세금계산서·전표·입고(발주) 행에서 증빙 파일을 올리고 내려받습니다.",
    screens: [
      { name: "첨부 / 다운로드 / 삭제", menu: "각 목록의 📎 버튼",
        purpose: "거래 증빙(스캔본·영수증·거래명세서)을 레코드에 보관.",
        steps: ["행의 ‘+’(📎) 버튼을 누른다.", "파일을 선택하면 업로드된다.",
          "올라간 파일(📎)을 누르면 다운로드된다.", "삭제는 첨부 관리에서 처리."],
        note: "지원: PDF·이미지(PNG/JPG/WebP)·XLSX·CSV, 파일당 20MB. 동일 파일은 자동 중복 제거." },
    ],
  },
  {
    id: "etc", title: "11. 규제 · 경영분석",
    screens: [
      { name: "규제", menu: "/erp/regulatory/*", purpose: "공시·AI 색인, 코드 마스터(약가·수가·재료), 약품 코드 매핑, 수가 조회, 간호등급 모니터.",
        steps: ["코드 마스터에서 약가/수가/재료 코드를 조회·관리한다.",
          "약품 코드 매핑에서 HIS 비표준코드↔보험코드를 확인·승인한다."] },
      { name: "경영분석(BI) · 경영진 대시보드", menu: "/erp/analytics · /erp/executive",
        purpose: "재무·인사·진료·보험 KPI, 부서 손익, 추세 분석.",
        steps: ["기간/연도를 선택해 KPI·차트를 본다."] },
    ],
  },
  {
    id: "common", title: "12. 공통 사용법",
    screens: [
      { name: "상태 배지", purpose: "색으로 상태를 표시: 초록=정상/완료, 황색=주의/대기, 빨강=오류/초과, 🤖=자동 처리." },
      { name: "알림", menu: "우상단 🔔", purpose: "면허 만료·이의신청 기한·승인 요청 등 알림. 숫자 배지는 미읽음 수." },
      { name: "Excel·PDF 출력", purpose: "다수 화면에 Excel/PDF 내려받기 버튼이 있습니다(신고서식·시산표·증명서 등)." },
      { name: "도움말·버전", menu: "사이드바 하단", purpose: "본 사용 설명서 링크와 버전·제작사 표시." },
    ],
  },
];

const BODY = "[&_ul]:list-disc [&_ul]:pl-5 [&_ol]:list-decimal [&_ol]:pl-5 [&_li]:my-1 " +
  "[&_code]:rounded [&_code]:bg-slate-100 [&_code]:px-1.5 [&_code]:py-0.5 [&_code]:text-[0.82em] [&_code]:text-blue-600";

function ScreenBlock({ s }: { s: Screen }) {
  return (
    <div className="rounded-xl border border-slate-100 bg-slate-50/40 p-4">
      <div className="flex flex-wrap items-baseline gap-x-2 gap-y-0.5">
        <h3 className="text-[15px] font-bold text-slate-900">{s.name}</h3>
        {s.menu && <span className="font-mono text-[11px] text-slate-400">{s.menu}</span>}
      </div>
      <p className="mt-1 text-[13px] text-slate-600">{s.purpose}</p>
      {s.features && (
        <p className="mt-2 text-[13px] text-slate-600">
          <span className="font-semibold text-slate-700">주요 기능 · </span>{s.features.join(" / ")}</p>
      )}
      {s.steps && (
        <div className="mt-2">
          <p className="mb-1 text-[12px] font-semibold uppercase tracking-wide text-slate-400">작업 순서</p>
          <ol className="list-decimal space-y-1 pl-5 text-[13px] text-slate-700">
            {s.steps.map((t, i) => <li key={i}>{t}</li>)}
          </ol>
        </div>
      )}
      {s.note && <p className="mt-2 rounded-lg bg-blue-50 px-3 py-2 text-[12.5px] text-blue-700">💡 {s.note}</p>}
      {s.warn && <p className="mt-2 rounded-lg bg-amber-50 px-3 py-2 text-[12.5px] text-amber-700">⚠️ {s.warn}</p>}
    </div>
  );
}

export default function HelpPage() {
  const [active, setActive] = useState(AREAS[0].id);
  return (
    <div className="mx-auto max-w-5xl">
      <div className="mb-6">
        <h1 className="text-2xl font-bold tracking-tight text-slate-900">병원 ERP 사용 설명서</h1>
        <p className="mt-1 text-sm text-slate-500">화면별 세부 기능과 작업 순서를 안내합니다. 좌측 목차에서 이동하세요.</p>
      </div>
      <div className="flex gap-8">
        <nav className="sticky top-20 hidden h-fit w-56 shrink-0 lg:block">
          <ul className="space-y-0.5 border-l border-slate-200">
            {AREAS.map((a) => (
              <li key={a.id}>
                <a href={`#${a.id}`} onClick={() => setActive(a.id)}
                  className={`-ml-px block border-l-2 py-1.5 pl-3 text-[13px] transition ${
                    active === a.id ? "border-blue-600 font-semibold text-blue-700"
                      : "border-transparent text-slate-500 hover:border-slate-300 hover:text-slate-800"}`}>
                  {a.title}
                </a>
              </li>
            ))}
          </ul>
        </nav>
        <article className={`min-w-0 flex-1 space-y-8 ${BODY}`}>
          {AREAS.map((a) => (
            <section key={a.id} id={a.id} className="scroll-mt-20 rounded-2xl border border-slate-100 bg-white p-6 shadow-sm">
              <h2 className="text-lg font-bold text-slate-900">{a.title}</h2>
              {a.intro && <p className="mt-1 mb-4 text-[13.5px] leading-relaxed text-slate-600">{a.intro}</p>}
              <div className="space-y-3">{a.screens.map((s, i) => <ScreenBlock key={i} s={s} />)}</div>
            </section>
          ))}
          <p className="pb-8 text-center text-xs text-slate-400">
            본 설명서는 ERP 기능 변경에 따라 갱신됩니다 · 문의는 시스템 관리자에게</p>
        </article>
      </div>
    </div>
  );
}

/** 공용 UI 프리미티브 — Tailwind 기반 (shadcn 전환 가능한 동일 인터페이스). */
"use client";
import { type ReactElement, type ReactNode, useEffect, useMemo, useRef, useState } from "react";

export function Card({ title, children, action }: {
  title?: string; children: ReactNode; action?: ReactNode;
}) {
  return (
    <div className="rounded-xl border border-slate-200/70 bg-white shadow-[0_1px_3px_rgba(15,23,42,0.05)]">
      {(title || action) && (
        <div className="flex items-center justify-between gap-2 border-b border-slate-100 px-5 py-3.5">
          <h3 className="text-sm font-semibold text-slate-800">{title}</h3>
          {action}
        </div>
      )}
      <div className="p-5">{children}</div>
    </div>
  );
}

// 시맨틱 버튼 변형(디자인 토큰·R5) — 화면마다 하드코딩하던 bg-<color>-600 원시 버튼을 대체.
// primary=주요/저장, success=전기·확정, neutral=조회·미리보기, danger=삭제·해지, warning=주의, secondary=보조(외곽선).
export type ButtonVariant = "primary" | "secondary" | "danger" | "success" | "neutral" | "warning";
export function Button({ children, variant = "primary", size = "md", isLoading = false, ...props }: {
  children: ReactNode; variant?: ButtonVariant; size?: "sm" | "md"; isLoading?: boolean;
} & React.ButtonHTMLAttributes<HTMLButtonElement>) {
  const styles = {
    primary: "bg-blue-600 text-white hover:bg-blue-700 disabled:bg-blue-300",
    secondary: "border border-slate-300 text-slate-700 hover:bg-slate-50 disabled:opacity-50",
    danger: "bg-red-600 text-white hover:bg-red-700 disabled:bg-red-300",
    success: "bg-emerald-600 text-white hover:bg-emerald-700 disabled:bg-emerald-300",
    neutral: "bg-slate-600 text-white hover:bg-slate-700 disabled:bg-slate-300",
    warning: "bg-amber-600 text-white hover:bg-amber-700 disabled:bg-amber-300",
  }[variant];
  const sizeCls = size === "sm" ? "px-2.5 py-1 text-xs" : "px-3 py-1.5 text-sm";
  return (
    <button {...props} disabled={props.disabled || isLoading}
      className={`rounded-md font-medium transition disabled:cursor-not-allowed ${sizeCls} ${styles} ${props.className ?? ""}`}>
      {isLoading ? "처리 중…" : children}
    </button>
  );
}

/** 성공/에러 분리 메시지 배너 (W3 — 검토 개선-1). */
export type Banner = { text: string; tone: "ok" | "error" } | null;

export function MessageBanner({ banner }: { banner: Banner }) {
  if (!banner) return null;
  return (
    <p className={`rounded-md px-3 py-2 text-sm ${
      banner.tone === "ok"
        ? "bg-emerald-50 text-emerald-700"
        : "bg-red-50 text-red-700"}`}>
      {banner.text}
    </p>
  );
}

export function Input(props: React.InputHTMLAttributes<HTMLInputElement> & { label?: string }) {
  const { label, ...rest } = props;
  return (
    <label className="block">
      {label && <span className="mb-1 block text-xs font-medium text-slate-600">{label}</span>}
      <input {...rest}
        className={`w-full rounded-md border border-slate-300 px-3 py-1.5 text-sm
          focus:border-blue-500 focus:outline-none ${rest.className ?? ""}`} />
    </label>
  );
}

// 숫자·금액 셀 자동 감지(우측정렬). 단일(12,000원·100%·-1,200) + 복합(5,000원 → 6,000원·9,000원 / 0원·1~3).
// 코드·날짜(2026-06)·식별자(#173)·텍스트는 제외.
const _NUM = String.raw`[₩$]?[-+]?[\d,]+(?:\.\d+)?\s*(?:억원|만원|천원|원|%|건|개|명|일|회|시간|점|년|월|배|주|호|kg|g|ea|EA)?`;
const NUMERIC_RE = new RegExp(`^${_NUM}(?:\\s*(?:→|->|/|~)\\s*${_NUM})*$`);

// 셀(문자/숫자/배열/단순 래퍼 요소)의 텍스트 추출. 컴포넌트·인터랙티브 요소는 null(비숫자 취급).
function cellText(c: ReactNode): string | null {
  if (c == null || typeof c === "boolean") return "";
  if (typeof c === "number") return String(c);
  if (typeof c === "string") return c;
  if (Array.isArray(c)) {
    let out = "";
    for (const p of c) { const t = cellText(p as ReactNode); if (t === null) return null; out += t; }
    return out;
  }
  if (typeof c === "object" && "props" in c) {
    const el = c as ReactElement<{ children?: ReactNode }>;
    if (typeof el.type !== "string") return null;          // Badge·Button 등 컴포넌트
    if (["button", "a", "input", "select", "textarea", "label"].includes(el.type)) return null;
    return cellText(el.props?.children);
  }
  return null;
}
function isNumericCell(c: ReactNode): boolean {
  const t = cellText(c);
  return t != null && t.trim() !== "" && NUMERIC_RE.test(t.trim());
}
function isNeutralCell(c: ReactNode): boolean {
  const t = cellText(c);
  return t == null ? false : (t.trim() === "" || t.trim() === "-" || t.trim() === "—");
}

// 정렬 키 추출 — 셀 텍스트(cellText). 숫자 컬럼은 수치 파싱(콤마·단위 제거), 그 외 문자열.
function sortKey(c: ReactNode, numeric: boolean): number | string {
  const t = (cellText(c) ?? "").trim();
  if (numeric) { const n = parseFloat(t.replace(/[^0-9.-]/g, "")); return isNaN(n) ? -Infinity : n; }
  return t;
}

export function Table({ headers, rows, empty = "데이터가 없습니다",
  sortable = false, pageSize, stickyHeader = false, maxHeight }: {
  headers: string[]; rows: ReactNode[][]; empty?: string;
  sortable?: boolean;       // 헤더 클릭 정렬(cellText 기준·숫자컬럼은 수치)
  pageSize?: number;        // 지정 시 클라 페이지네이션(prev/next)
  stickyHeader?: boolean;   // 헤더 고정(스크롤 시)
  maxHeight?: number;       // 지정 시 세로 스크롤 컨테이너(px)
}) {
  const [sort, setSort] = useState<{ col: number; dir: 1 | -1 } | null>(null);
  const [page, setPage] = useState(1);
  // 컬럼별 우측정렬 여부 — 데이터 셀(중립 제외)이 모두 숫자형이면 헤더·셀 우측정렬
  const rightCols = useMemo(() => headers.map((_, j) => {
    const cells = rows.map((r) => r[j]).filter((c) => !isNeutralCell(c));
    return cells.length > 0 && cells.every(isNumericCell);
  }), [headers, rows]);
  const colCls = (j: number) => rightCols[j] ? "text-right" : "text-left";

  const sorted = useMemo(() => {
    if (!sortable || !sort) return rows;
    const { col, dir } = sort, numeric = rightCols[col];
    return [...rows].sort((a, b) => {
      const ka = sortKey(a[col], numeric), kb = sortKey(b[col], numeric);
      return ka < kb ? -dir : ka > kb ? dir : 0;
    });
  }, [rows, sort, sortable, rightCols]);

  const pageCount = pageSize ? Math.max(1, Math.ceil(sorted.length / pageSize)) : 1;
  const safePage = Math.min(page, pageCount);
  const view = pageSize ? sorted.slice((safePage - 1) * pageSize, safePage * pageSize) : sorted;

  const onSort = (j: number) => {
    if (!sortable) return;
    setPage(1);
    setSort((s) => s?.col === j ? (s.dir === 1 ? { col: j, dir: -1 } : null) : { col: j, dir: 1 });
  };
  const arrow = (j: number) => sort?.col === j ? (sort.dir === 1 ? " ▲" : " ▼") : "";

  return (
    <div className={maxHeight ? "overflow-auto" : "overflow-x-auto"}
      style={maxHeight ? { maxHeight } : undefined}>
      <table className="w-full text-sm">
        <thead className={stickyHeader ? "sticky top-0 z-10 bg-white" : ""}>
          <tr className="border-b border-slate-200 text-[11px] uppercase tracking-wide text-slate-500">
            {headers.map((h, j) => (
              <th key={h || j}
                onClick={() => onSort(j)}
                className={`px-4 py-2.5 font-semibold ${colCls(j)} ${
                  sortable ? "cursor-pointer select-none hover:text-slate-700" : ""}`}>
                {h}{arrow(j)}
              </th>
            ))}
          </tr>
        </thead>
        <tbody className="tabular-nums">
          {view.length === 0 ? (
            <tr><td colSpan={headers.length} className="px-4 py-10 text-center text-slate-400">{empty}</td></tr>
          ) : view.map((cells, i) => (
            <tr key={i} className="border-b border-slate-100 last:border-0 transition-colors hover:bg-blue-50/40">
              {cells.map((c, j) => (
                <td key={j} className={`px-4 py-2.5 text-slate-700 ${colCls(j)}`}>{c}</td>
              ))}
            </tr>
          ))}
        </tbody>
      </table>
      {pageSize && sorted.length > pageSize && (
        <div className="flex items-center justify-end gap-3 border-t border-slate-100 px-4 py-2 text-xs text-slate-500">
          <span>{sorted.length}건 · {safePage}/{pageCount}</span>
          <button onClick={() => setPage((p) => Math.max(1, p - 1))} disabled={safePage <= 1}
            className="rounded border border-slate-200 px-2 py-0.5 hover:bg-slate-50 disabled:opacity-40">이전</button>
          <button onClick={() => setPage((p) => Math.min(pageCount, p + 1))} disabled={safePage >= pageCount}
            className="rounded border border-slate-200 px-2 py-0.5 hover:bg-slate-50 disabled:opacity-40">다음</button>
        </div>
      )}
    </div>
  );
}

export function Badge({ children, tone = "gray" }: {
  children: ReactNode; tone?: "gray" | "green" | "red" | "amber" | "blue";
}) {
  const tones = {
    gray: "bg-slate-50 text-slate-600 ring-slate-200", green: "bg-emerald-50 text-emerald-700 ring-emerald-200",
    red: "bg-red-50 text-red-700 ring-red-200", amber: "bg-amber-50 text-amber-700 ring-amber-200",
    blue: "bg-blue-50 text-blue-700 ring-blue-200",
  }[tone];
  return <span className={`inline-flex items-center whitespace-nowrap rounded-full px-2 py-0.5 text-[11px] font-medium ring-1 ring-inset ${tones}`}>{children}</span>;
}

export function Spinner({ label = "불러오는 중…" }: { label?: string }) {
  return (
    <div className="flex items-center justify-center gap-2 py-8 text-sm text-slate-400">
      <span className="h-4 w-4 animate-spin rounded-full border-2 border-slate-300 border-t-blue-500" />
      {label}
    </div>
  );
}

/** 접근성 모달 — 포커스 트랩(Tab 순환)·ESC 닫기·오버레이 클릭·열림 시 첫 포커스·닫힘 시 복원. */
export function Modal({ title, onClose, children, size = "sm", bare = false, dirty = false }: {
  title: string; onClose: () => void; children: ReactNode;
  size?: "sm" | "lg"; bare?: boolean;   // lg=넓은 패널(명세 등), bare=Card 래퍼 생략(자체 Card 콘텐츠)
  dirty?: boolean;   // 입력 변경 시 true — 오버레이/ESC 우발 닫기로 인한 데이터 손실 차단(X1-2)
}) {
  const boxRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    const opener = document.activeElement as HTMLElement | null;
    const box = boxRef.current;
    const focusables = () => Array.from(
      box?.querySelectorAll<HTMLElement>(
        'button, [href], input, select, textarea, [tabindex]:not([tabindex="-1"])',
      ) ?? []).filter((el) => !el.hasAttribute("disabled"));
    focusables()[0]?.focus();

    const handler = (e: KeyboardEvent) => {
      if (e.key === "Escape") { if (!dirty) onClose(); return; }   // dirty면 ESC 닫기 무시
      if (e.key !== "Tab") return;
      const els = focusables();
      if (!els.length) return;
      const first = els[0], last = els[els.length - 1];
      if (e.shiftKey && document.activeElement === first) { e.preventDefault(); last.focus(); }
      else if (!e.shiftKey && document.activeElement === last) { e.preventDefault(); first.focus(); }
    };
    window.addEventListener("keydown", handler);
    return () => { window.removeEventListener("keydown", handler); opener?.focus(); };
  }, [onClose, dirty]);

  return (
    <div className={`fixed inset-0 z-50 flex justify-center overflow-y-auto bg-black/30 p-4 ${
      size === "lg" ? "items-start" : "items-center"}`}
      onClick={dirty ? undefined : onClose}>   {/* dirty면 오버레이 우발 닫기 무시 */}
      <div ref={boxRef} role="dialog" aria-modal="true" aria-label={title}
        onClick={(e) => e.stopPropagation()}
        className={size === "lg" ? "my-6 w-[min(96vw,1100px)]" : "w-96"}>
        {bare ? children : <Card title={title}>{children}</Card>}
      </div>
    </div>
  );
}

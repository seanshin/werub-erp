"use client";
/** 인증 영역 레이아웃 — 사이드바 메뉴는 서버(/menu, DB 역할별 노출)에서 수신. 권한 판정은 서버. */
import Link from "next/link";
import { usePathname } from "next/navigation";
import { useEffect, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { api, auth } from "@/lib/api";
import { APP_MAKER, APP_VERSION } from "@/lib/app-info";
import { IFRSBadge } from "@/components/ifrs-badge";

type Group = { group: string | null; items: { href: string; label: string }[] };

export default function AppLayout({ children }: { children: React.ReactNode }) {
  const pathname = usePathname();
  const [ready, setReady] = useState(false);
  useEffect(() => {
    auth.bootstrap().then((ok) => {
      if (ok) setReady(true);
      else window.location.href = "/erp/login";   // 미인증 → 로그인(무한루프 방지·P0-7)
    });
    // bfcache 복원(로그아웃 후 브라우저 뒤로가기) 재검증 — mount effect가 재발화 안 하고 react-query도
    // refetch 안 해 이전 화면(환자/급여/청구 PHI)이 세션 없이 그대로 남던 갭(적대검토 MED·키오스크 위험).
    // persisted 복원 시 전체 리로드 → 인증 게이트 재실행(무세션이면 로그인 리다이렉트).
    const onShow = (e: PageTransitionEvent) => { if (e.persisted) window.location.reload(); };
    window.addEventListener("pageshow", onShow);
    return () => window.removeEventListener("pageshow", onShow);
  }, []);

  const { data: notif } = useQuery({
    queryKey: ["notifications"],
    queryFn: () => api<{ items: { read_at: string | null }[] }>("/notifications?size=50"),
    refetchInterval: 60_000,
    enabled: ready,
  });
  // 사이드바 메뉴 — 서버가 역할별 노출(DB 오버라이드 반영) 필터해 반환
  const { data: menu } = useQuery({
    queryKey: ["menu"],
    queryFn: () => api<{ groups: Group[] }>("/menu"),
    enabled: ready,
  });
  const unread = notif?.items.filter((n) => !n.read_at).length ?? 0;
  const role = auth.claims()?.role ?? "STAFF";
  const groups = menu?.groups ?? [];
  // 가장 구체적인(가장 긴) 일치 1개만 활성 — /patients 가 /patients/closing 을 가로채지 않도록
  const activeHref = groups.flatMap((g) => g.items.map((i) => i.href))
    .filter((h) => pathname === h || pathname.startsWith(h + "/"))
    .sort((a, b) => b.length - a.length)[0];
  const isActive = (href: string) => href === activeHref;
  // 브레드크럼 — 활성 메뉴(그룹>항목)에서 유도(기획서 §2·전 페이지 일괄). 메뉴 밖 서브경로는 홈만.
  const crumb = (() => {
    if (activeHref === "/dashboard") return null;   // 홈=대시보드 — "홈/대시보드" 중복 표시 방지
    for (const g of groups) for (const it of g.items) {
      if (it.href === activeHref) return { group: g.group, label: it.label };
    }
    return null;
  })();

  // 휴대폰 폭(md 미만) — 메뉴는 서랍(종전: 240px 사이드바가 늘 떠 있어 390px 폭에서 본문이 150px로 눌리고 가로로 밀렸다·2026-10-11).
  // 경로가 바뀌면 닫는다(렌더 중 이전 값 비교 — 뒤로 가기 포함). 서랍 안 링크를 누르면 같은 경로여도 닫는다.
  const [navOpen, setNavOpen] = useState(false);
  const [navPath, setNavPath] = useState(pathname);
  if (navPath !== pathname) { setNavPath(pathname); setNavOpen(false); }
  useEffect(() => {
    if (!navOpen) return;
    const onKey = (e: KeyboardEvent) => { if (e.key === "Escape") setNavOpen(false); };
    window.addEventListener("keydown", onKey);
    // 열리면 서랍 첫 링크로 포커스(키보드·스크린리더가 바로 메뉴에서 시작) · 배경 스크롤 잠금 — 닫히면 햄버거로 포커스 복귀
    const prevOverflow = document.body.style.overflow;
    document.body.style.overflow = "hidden";
    const first = document.querySelector<HTMLElement>("#app-nav a, #app-nav button");
    const t = window.setTimeout(() => first?.focus(), 50);     // invisible → visible 전환 뒤
    return () => {
      window.removeEventListener("keydown", onKey);
      window.clearTimeout(t);
      document.body.style.overflow = prevOverflow;
      document.querySelector<HTMLElement>("[aria-controls='app-nav']")?.focus();
    };
  }, [navOpen]);

  if (!ready) return null;
  return (
    <div className="flex min-h-screen bg-slate-50 text-slate-800">
      {navOpen && <button type="button" aria-label="메뉴 바깥(닫기)" tabIndex={-1} onClick={() => setNavOpen(false)}
        className="fixed inset-0 z-[41] bg-slate-900/30 md:hidden" />}
      {/* 닫힌 서랍은 invisible(키보드·스크린리더가 숨은 메뉴를 돌지 않게) · 위치 지정(inset)은 휴대폰 폭만(데스크톱 sticky가 가로로 고정되지 않게) ·
          z=배지(40) 위·모달(50) 아래 */}
      <aside id="app-nav" onClickCapture={(e) => { if ((e.target as HTMLElement).closest("a")) setNavOpen(false); }}
        className={`flex h-screen w-60 shrink-0 flex-col border-r border-slate-200 bg-white
        max-md:fixed max-md:inset-y-0 max-md:left-0 max-md:z-[45] max-md:h-dvh max-md:transition-[transform,visibility]
        md:sticky md:top-0 ${navOpen ? "max-md:translate-x-0" : "max-md:invisible max-md:-translate-x-full"}`}>
        <div className="flex items-center gap-2 px-5 py-4">
          <span className="flex h-7 w-7 items-center justify-center rounded-lg bg-blue-600 text-sm font-bold text-white">H</span>
          <span className="text-[15px] font-bold tracking-tight text-slate-900">병원 ERP</span>
        </div>
        <nav className="flex-1 overflow-y-auto px-2.5 pb-6">
          {groups.map((g, gi) => (
            <div key={g.group ?? `g${gi}`} className="mb-1">
              {g.group && (
                <p className="px-2.5 pb-1 pt-4 text-[11px] font-semibold uppercase tracking-wider text-slate-400">
                  {g.group}
                </p>
              )}
              {g.items.map((m) => {
                const active = isActive(m.href);
                return (
                  <Link key={m.href} href={m.href}
                    className={`relative block rounded-md px-2.5 py-[7px] text-[13px] transition-colors ${
                      active
                        ? "bg-blue-50 font-semibold text-blue-700"
                        : "text-slate-600 hover:bg-slate-100 hover:text-slate-900"}`}>
                    {active && <span className="absolute inset-y-1 left-0 w-0.5 rounded-full bg-blue-600" />}
                    {m.label}
                  </Link>
                );
              })}
            </div>
          ))}
          {/* 경영 BI(Grafana) — 외부 링크(/erp-bi/, basePath 밖·새 탭)라 DB 메뉴 대신 정적 노출.
              서버 BI_ROLES(ADMIN·DIRECTOR·MANAGER·FINANCE_MANAGER)와 일치 — /auth/bi-check가 실제 게이트(X0-8). */}
          {["ADMIN", "MANAGER", "DIRECTOR", "FINANCE_MANAGER"].includes(role) && (
            <div className="mb-1">
              <p className="px-2.5 pb-1 pt-4 text-[11px] font-semibold uppercase tracking-wider text-slate-400">
                경영 BI
              </p>
              <a href="/erp/bi/" target="_blank" rel="noopener noreferrer"
                className="block rounded-md px-2.5 py-[7px] text-[13px] text-slate-600 transition-colors hover:bg-slate-100 hover:text-slate-900">
                경영 BI 대시보드 ↗
              </a>
              {/* 셀프서비스 BI(Metabase) — SQL 없이 질문/대시보드. ERP 세션으로 자동 로그인(SSO 브리지)→/erp/mb/. */}
              <a href="/erp/api/v1/integrations/bi/metabase-login" target="_blank" rel="noopener noreferrer"
                className="block rounded-md px-2.5 py-[7px] text-[13px] text-slate-600 transition-colors hover:bg-slate-100 hover:text-slate-900">
                셀프서비스 BI ↗
              </a>
            </div>
          )}
        </nav>
        <div className="border-t border-slate-100 px-4 py-3 text-[11px] text-slate-400">
          <Link href="/help" className="font-medium text-slate-500 transition hover:text-blue-600">
            도움말(사용 설명서)
          </Link>
          <p className="mt-1.5">병원 ERP <span className="font-medium text-slate-500">v{APP_VERSION}</span></p>
          <p>© {APP_MAKER}</p>
        </div>
      </aside>

      <div className="flex min-w-0 flex-1 flex-col">
        <header className="sticky top-0 z-10 flex h-14 items-center justify-between gap-2 border-b border-slate-200 bg-white/80 px-4 backdrop-blur md:gap-4 md:px-6">
          <button type="button" aria-label={navOpen ? "메뉴 닫기" : "메뉴 열기"} aria-controls="app-nav" aria-expanded={navOpen}
            onClick={() => setNavOpen((v) => !v)}
            className="-ml-1 shrink-0 rounded-md p-1 text-slate-600 hover:bg-slate-100 md:hidden">
            <svg className="h-5 w-5" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={1.8}>
              <path strokeLinecap="round" d="M4 6h16M4 12h16M4 18h16" />
            </svg>
          </button>
          <nav aria-label="breadcrumb" className="flex min-w-0 flex-1 items-center gap-1.5 text-[13px] text-slate-500">
            <Link href="/dashboard" className="shrink-0 transition-colors hover:text-slate-900">홈</Link>
            {crumb?.group && (<>
              <span className="hidden text-slate-300 sm:inline">/</span>
              <span className="hidden shrink-0 text-slate-400 sm:inline">{crumb.group}</span>
            </>)}
            {crumb && (<>
              <span className="text-slate-300">/</span>
              <span className="truncate font-medium text-slate-800">{crumb.label}</span>
            </>)}
          </nav>
          <div className="flex shrink-0 items-center gap-3 md:gap-4">
          <Link href="/notifications" className="relative text-slate-500 transition-colors hover:text-slate-900">
            <svg className="h-5 w-5" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={1.7}>
              <path strokeLinecap="round" strokeLinejoin="round"
                d="M14.857 17.082a23.8 23.8 0 0 0 5.454-1.31A8.97 8.97 0 0 1 18 9.75V9A6 6 0 0 0 6 9v.75a8.97 8.97 0 0 1-2.312 6.022c1.733.64 3.56 1.085 5.455 1.31m5.714 0a24 24 0 0 1-5.714 0m5.714 0a3 3 0 1 1-5.714 0" />
            </svg>
            {unread > 0 && (
              <span className="absolute -right-1.5 -top-1 flex h-4 min-w-4 items-center justify-center rounded-full bg-red-500 px-1 text-[10px] font-bold text-white">
                {unread}
              </span>
            )}
          </Link>
          <span className="hidden rounded-full bg-slate-100 px-2.5 py-1 text-[11px] font-semibold text-slate-600 sm:inline">{role}</span>
          <button onClick={() => auth.logout()}
            className="text-[13px] font-medium text-slate-500 transition-colors hover:text-red-600">로그아웃</button>
          </div>
        </header>
        <main className="min-w-0 flex-1 p-4 md:p-6">{children}</main>
      </div>
      <IFRSBadge />
    </div>
  );
}

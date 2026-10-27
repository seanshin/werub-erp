"use client";
/** 시스템 — 일정 캘린더(v1.302): 한 달의 정기업무·법정 기한·공휴일·이의신청 기한을 한 화면에. 저장 없음 — 서버가 기존 소스를 모은다.
 *  각 항목은 처리 화면으로 이어진다. 휴대폰 폭은 날짜별 목록. 권한 없는 소스는 서버가 빼고 hidden_kinds로 알린다. */
import { useState } from "react";
import Link from "next/link";
import { useQuery } from "@tanstack/react-query";
import { api } from "@/lib/api";
import { Badge, Button, Card } from "@/components/ui";
import { QueryErrorBanner } from "@/components/query-guard";

interface Ev { date: string; kind: string; title: string; status: string | null; time: string | null; detail: string | null; href: string | null }
interface Month { yyyymm: string; first: string; last: string; today: string; kinds: Record<string, string>;
  hidden_kinds: string[]; events: Ev[]; errors: { kind: string; error: string }[] }

const KIND_TONE: Record<string, string> = {
  recurring: "bg-blue-50 text-blue-800 border-blue-200", deadline: "bg-amber-50 text-amber-800 border-amber-200",
  holiday: "bg-rose-50 text-rose-700 border-rose-200", appeal: "bg-violet-50 text-violet-800 border-violet-200",
};
const BAD = new Set(["실패", "지연", "미생성", "경과", "장기 경과", "기한 경과"]);   // '기록 없음'(등록 전·먼 과거)은 제외
// "YYYY-MM-DD" → 요일(현지 날짜로 — new Date("YYYY-MM-DD")는 UTC 자정이라 서쪽 시간대에서 하루 밀린다)
const dow = (k: string) => { const [a, b, c] = k.split("-").map(Number); return new Date(a, b - 1, c).getDay(); };
const WEEK = ["일", "월", "화", "수", "목", "금", "토"];
const shift = (ym: string, n: number) => {
  const [y, m] = ym.split("-").map(Number);
  const d = new Date(y, m - 1 + n, 1);
  return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}`;
};
const thisMonth = () => {
  const d = new Date();
  return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}`;
};

export default function SchedulePage() {
  const [ym, setYm] = useState(thisMonth);
  const [off, setOff] = useState<string[]>([]);
  const [day, setDay] = useState<string | null>(null);
  const q = useQuery({ queryKey: ["schedule", ym], queryFn: () => api<Month>(`/admin/schedule?yyyymm=${ym}`) });
  const d = q.data;
  const events = (d?.events ?? []).filter((e) => !off.includes(e.kind));
  const byDay = new Map<string, Ev[]>();
  for (const e of events) byDay.set(e.date, [...(byDay.get(e.date) ?? []), e]);
  const holidays = new Set((d?.events ?? []).filter((e) => e.kind === "holiday").map((e) => e.date));
  const [y, m] = ym.split("-").map(Number);
  const days = new Date(y, m, 0).getDate();
  const lead = new Date(y, m - 1, 1).getDay();
  const iso = (n: number) => `${ym}-${String(n).padStart(2, "0")}`;
  const counts = Object.keys(d?.kinds ?? {}).map((k) => [k, (d?.events ?? []).filter((e) => e.kind === k).length] as const);
  const bad = events.filter((e) => e.status && BAD.has(e.status));
  const selected = day ? byDay.get(day) ?? [] : [];

  const item = (e: Ev, i: number, compact = false) => {
    const body = (
      <span className={`block truncate rounded border px-1 py-0.5 text-[11px] ${KIND_TONE[e.kind] ?? ""} ${e.status && BAD.has(e.status) ? "ring-1 ring-rose-400" : ""}`}
        title={`${e.title}${e.status ? ` · ${e.status}` : ""}${e.detail ? ` · ${e.detail}` : ""}`}>
        {e.time && <span className="mr-0.5 font-mono">{e.time}</span>}{e.title}{!compact && e.status && ` · ${e.status}`}
      </span>);
    const key = `${i}-${e.kind}-${e.title}`;      // 같은 날 같은 제목(직원별 법정교육 등)도 키 충돌 없이
    return e.href ? <Link key={key} href={e.href}>{body}</Link> : <span key={key}>{body}</span>;
  };

  return (
    <div className="space-y-4">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <h1 className="text-lg font-bold">시스템 — 일정 캘린더</h1>
        <div className="flex items-center gap-1">
          <Button size="sm" variant="secondary" onClick={() => { setYm(shift(ym, -1)); setDay(null); }}>‹ 이전 달</Button>
          <input type="month" value={ym} aria-label="월" onChange={(e) => { if (e.target.value) { setYm(e.target.value); setDay(null); } }}
            className="rounded-md border border-slate-300 px-2 py-1 text-sm" />
          <Button size="sm" variant="secondary" onClick={() => { setYm(shift(ym, 1)); setDay(null); }}>다음 달 ›</Button>
          <Button size="sm" variant="secondary" onClick={() => { setYm(thisMonth()); setDay(null); }}>이번 달</Button>
        </div>
      </div>
      <p className="text-xs text-slate-500">
        정기업무(주간 이상 — 매일 자동 작업은 <Link href="/admin/batch-jobs" className="text-blue-600 underline">배치 작업</Link>)·법정 기한·
        공휴일(고정·코드 표·캘린더 등록)·이의신청 기한을 모았습니다. 항목을 누르면 처리 화면으로 갑니다.</p>
      <QueryErrorBanner queries={[q]} />
      {(d?.errors ?? []).length > 0 && (
        <p className="rounded-md bg-rose-50 p-2 text-xs text-rose-700">일부 일정을 불러오지 못했습니다 —{" "}
          {d!.errors.map((x) => `${d!.kinds[x.kind] ?? x.kind}: ${x.error}`).join(" · ")}</p>)}
      <div className="flex flex-wrap items-center gap-1.5">
        {counts.map(([k, n]) => (
          <button key={k} type="button" aria-pressed={!off.includes(k)}
            onClick={() => setOff(off.includes(k) ? off.filter((x) => x !== k) : [...off, k])}
            className={`rounded-full border px-3 py-1 text-xs ${off.includes(k) ? "border-slate-200 text-slate-400 line-through" : KIND_TONE[k]}`}>
            {d!.kinds[k]} {n}</button>))}
        {(d?.hidden_kinds ?? []).length > 0 && <span className="text-xs text-slate-400">
          권한 밖이라 뺀 것: {d!.hidden_kinds.map((k) => d!.kinds[k] ?? k).join(", ")}</span>}
        {bad.length > 0 && <Badge tone="red">확인 필요 {bad.length}건</Badge>}
      </div>

      {/* 월 격자 — md 이상 */}
      <div className="hidden md:block">
        <div className="grid grid-cols-7 gap-px overflow-hidden rounded-lg border border-slate-200 bg-slate-200 text-sm">
          {WEEK.map((w, i) => <div key={w} className={`bg-slate-50 px-2 py-1 text-center text-xs font-medium ${i === 0 ? "text-rose-600" : i === 6 ? "text-blue-600" : "text-slate-500"}`}>{w}</div>)}
          {Array.from({ length: lead }).map((_, i) => <div key={`b${i}`} className="min-h-24 bg-white/60" />)}
          {Array.from({ length: days }).map((_, i) => {
            const n = i + 1, key = iso(n), dow = (lead + i) % 7, list = byDay.get(key) ?? [];
            const red = dow === 0 || holidays.has(key);
            return (
              <button key={key} type="button" onClick={() => setDay(day === key ? null : key)}
                className={`min-h-24 min-w-0 bg-white p-1 text-left align-top hover:bg-slate-50 ${day === key ? "ring-2 ring-inset ring-blue-500" : ""}`}>
                <span className={`mb-0.5 inline-block rounded px-1 text-xs font-semibold ${key === d?.today ? "bg-blue-600 text-white" : red ? "text-rose-600" : dow === 6 ? "text-blue-600" : "text-slate-600"}`}>{n}</span>
                <span className="block space-y-0.5">
                  {list.slice(0, 3).map((e, i) => <span key={`${i}-${e.kind}`} className={`block truncate rounded border px-1 text-[11px] ${KIND_TONE[e.kind] ?? ""} ${e.status && BAD.has(e.status) ? "ring-1 ring-rose-400" : ""}`}>
                    {e.time && <span className="mr-0.5 font-mono">{e.time}</span>}{e.title}</span>)}
                  {list.length > 3 && <span className="block text-[11px] text-slate-500">+{list.length - 3}건</span>}
                </span>
              </button>);
          })}
        </div>
      </div>
      {day && (
        <Card title={`${day} 일정 ${selected.length}건`} action={<Button size="sm" variant="secondary" onClick={() => setDay(null)}>닫기</Button>}>
          <div className="space-y-1">{selected.map((e, i) => item(e, i))}{!selected.length && <p className="text-sm text-slate-400">일정 없음</p>}</div>
        </Card>)}

      {/* 날짜별 목록 — 휴대폰 폭(md 미만) */}
      <div className="space-y-2 md:hidden">
        {[...byDay.entries()].map(([k, list]) => (
          <div key={k} className="rounded-md border border-slate-200 bg-white p-2">
            <p className={`mb-1 text-xs font-semibold ${k === d?.today ? "text-blue-700" : holidays.has(k) ? "text-rose-600" : "text-slate-600"}`}>
              {k} ({WEEK[dow(k)]}){k === d?.today && " · 오늘"}</p>
            <div className="space-y-1">{list.map((e, i) => item(e, i))}</div>
          </div>))}
        {!byDay.size && <p className="text-sm text-slate-400">{q.isFetching ? "불러오는 중…" : "이 달 일정 없음"}</p>}
      </div>
    </div>
  );
}

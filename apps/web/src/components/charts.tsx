/** 경량 SVG 차트 — 외부 의존 없음. 막대·도넛·추세선. */
"use client";
import { type ReactNode } from "react";

const won = (n: number) => n.toLocaleString();

/** 세로 막대 차트 — 양수/음수 지원. */
export function BarChart({ data, height = 160, format = won }: {
  data: { label: string; value: number; color?: string }[];
  height?: number; format?: (n: number) => string;
}) {
  if (!data.length) return <p className="py-6 text-center text-sm text-slate-400">데이터 없음</p>;
  const max = Math.max(1, ...data.map((d) => Math.abs(d.value)));
  return (
    <div className="flex items-end gap-2 overflow-x-auto pt-4" style={{ height: height + 36 }}>
      {data.map((d, i) => {
        const h = (Math.abs(d.value) / max) * height;
        const pos = d.value >= 0;
        return (
          <div key={i} className="flex min-w-8 flex-1 flex-col items-center justify-end gap-1">
            <span className="text-[10px] text-slate-500">{format(d.value)}</span>
            <div className="w-full rounded-t" style={{
              height: Math.max(2, h),
              background: d.color ?? (pos ? "#3b82f6" : "#ef4444") }} />
            <span className="truncate text-[10px] text-slate-500" title={d.label}>{d.label}</span>
          </div>
        );
      })}
    </div>
  );
}

/** 그룹 막대 — 한 라벨에 여러 막대(예: Benford 기대 vs 실측). */
export function GroupedBarChart({ data, height = 160, format = won }: {
  data: { label: string; bars: { value: number; color: string }[] }[];
  height?: number; format?: (n: number) => string;
}) {
  if (!data.length) return <p className="py-6 text-center text-sm text-slate-400">데이터 없음</p>;
  const max = Math.max(1, ...data.flatMap((d) => d.bars.map((b) => Math.abs(b.value))));
  return (
    <div className="flex items-end gap-2 overflow-x-auto pt-4" style={{ height: height + 28 }}>
      {data.map((d, i) => (
        <div key={i} className="flex min-w-8 flex-1 flex-col items-center justify-end gap-1">
          <div className="flex h-full w-full items-end justify-center gap-0.5">
            {d.bars.map((b, j) => (
              <div key={j} title={format(b.value)} className="w-2.5 rounded-t" style={{
                height: Math.max(2, (Math.abs(b.value) / max) * height), background: b.color }} />
            ))}
          </div>
          <span className="text-[10px] text-slate-500">{d.label}</span>
        </div>
      ))}
    </div>
  );
}

/** 도넛 차트 — 구성비. */
export function DonutChart({ data, size = 150 }: {
  data: { label: string; value: number; color: string }[]; size?: number;
}) {
  const total = data.reduce((a, d) => a + Math.max(0, d.value), 0);
  if (total <= 0) return <p className="py-6 text-center text-sm text-slate-400">데이터 없음</p>;
  const r = size / 2 - 12;
  const cx = size / 2;
  const circ = 2 * Math.PI * r;
  // 누적 오프셋을 사전계산(렌더 중 변수 재할당 회피).
  const dashes = data.map((d) => (Math.max(0, d.value) / total) * circ);
  const offsets = dashes.map((_, i) => dashes.slice(0, i).reduce((a, x) => a + x, 0));
  return (
    <div className="flex items-center gap-4">
      <svg width={size} height={size} viewBox={`0 0 ${size} ${size}`}>
        <g transform={`rotate(-90 ${cx} ${cx})`}>
          {data.map((d, i) => (
            <circle key={i} cx={cx} cy={cx} r={r} fill="none" stroke={d.color} strokeWidth={20}
              strokeDasharray={`${dashes[i]} ${circ - dashes[i]}`} strokeDashoffset={-offsets[i]} />
          ))}
        </g>
      </svg>
      <ul className="space-y-1 text-xs">
        {data.map((d, i) => (
          <li key={i} className="flex items-center gap-1.5">
            <span className="inline-block h-2.5 w-2.5 rounded-sm" style={{ background: d.color }} />
            <span className="text-slate-600">{d.label}</span>
            <span className="font-medium text-slate-800">{won(d.value)}</span>
          </li>
        ))}
      </ul>
    </div>
  );
}

/** 카드 섹션 래퍼 (차트 제목용 보조). */
export function ChartFrame({ title, children }: { title: string; children: ReactNode }) {
  return (
    <div>
      <p className="mb-1 text-xs font-medium text-slate-500">{title}</p>
      {children}
    </div>
  );
}

"use client";
/** 재고 워크스테이션(공유) — 조회·입고·출고·실사·검수·작업목록 + 바코드/GS1·라벨·출력. */
import { todayLocal } from "@/lib/dates";
import { useRef, useState } from "react";
import { useMutation, useQuery } from "@tanstack/react-query";
import JsBarcode from "jsbarcode";
import { ApiError, api } from "@/lib/api";
import { Badge, Button, Card, Input } from "@/components/ui";
import { CameraScanButton } from "@/components/camera-scan";

interface Lot { lot_no: string; expiry_on: string; remaining: number }
interface LookupRes {
  item: { id: number; code: string; barcode: string | null; name: string;
    unit: string; is_narcotic: boolean; safety_stock: number };
  stock: number; below_safety: boolean; lots: Lot[];
}
interface Po { id: number; supplier_id: number; status: string; requested_at: string;
  lines: { item_id: number; qty: number }[] }
type Mode = "lookup" | "receive" | "issue" | "stocktake" | "inspect" | "list";
const MODE_LABEL: Record<Mode, string> = {
  lookup: "조회", receive: "입고", issue: "출고", stocktake: "실사",
  inspect: "발주 검수", list: "작업 목록" };

function parseScan(raw: string): { key: string; lot?: string; expiry?: string } {
  const s = raw.replace(/\x1d/g, "|").trim();
  if (/^01\d{14}/.test(s)) {
    const gtin = s.slice(2, 16);
    let rest = s.slice(16); let lot: string | undefined; let expiry: string | undefined;
    while (rest.length >= 2) {
      const ai = rest.slice(0, 2);
      if (ai === "17") {
        const d = rest.slice(2, 8);
        expiry = `20${d.slice(0, 2)}-${d.slice(2, 4)}-${d.slice(4, 6)}`;
        rest = rest.slice(8).replace(/^\|/, "");
      } else if (ai === "10") {
        const v = rest.slice(2); const gi = v.indexOf("|");
        lot = gi >= 0 ? v.slice(0, gi) : v; rest = gi >= 0 ? v.slice(gi + 1) : "";
      } else break;
    }
    return { key: gtin, lot, expiry };
  }
  if (s.includes("|")) { const [code, lot, expiry] = s.split("|");
    return { key: code, lot: lot || undefined, expiry: expiry || undefined }; }
  return { key: s };
}

function printDoc(title: string, html: string) {
  const w = window.open("", "_blank", "width=760,height=600");
  if (!w) return;
  w.document.write(`<html><head><title>${title}</title>` +
    `<style>body{font-family:sans-serif;margin:20px}h2{font-size:16px}` +
    `table{width:100%;border-collapse:collapse;font-size:13px;margin-top:8px}` +
    `th,td{border:1px solid #ddd;padding:5px 8px;text-align:left}th{background:#f1f5f9}` +
    `.r{text-align:right}</style></head><body>${html}` +
    `<p style="margin-top:14px;font-size:11px;color:#888">출력: ${new Date().toLocaleString("ko-KR")}</p>` +
    `</body></html>`);
  w.document.close(); w.focus(); w.print();
}
function printLabel(value: string, lines: string[]) {
  const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
  try { JsBarcode(svg, value, { format: "CODE128", width: 2, height: 48, fontSize: 13 }); }
  catch { return; }
  printDoc("라벨", lines.map((l) => `<div style="text-align:center;font-size:13px">${l}</div>`).join("") +
    `<div style="text-align:center">${svg.outerHTML}</div>`);
}
function printTable(title: string, headers: string[], rows: (string | number)[][]) {
  printDoc(title, `<h2>${title}</h2><table><thead><tr>${headers.map((h) => `<th>${h}</th>`).join("")}` +
    `</tr></thead><tbody>${rows.map((r) => `<tr>${r.map((c) => `<td>${c}</td>`).join("")}</tr>`).join("")}` +
    `</tbody></table>`);
}

export function InventoryWorkstation() {
  const [mode, setMode] = useState<Mode>("lookup");
  const [scan, setScan] = useState("");
  const [data, setData] = useState<LookupRes | null>(null);
  const [msg, setMsg] = useState<{ t: string; ok: boolean } | null>(null);
  const [lotNo, setLotNo] = useState("");
  const [expiry, setExpiry] = useState("");
  const [qty, setQty] = useState("");
  const [narcType, setNarcType] = useState("IN");
  const [take, setTake] = useState<{ id: number; book: number; diff: number } | null>(null);
  const scanRef = useRef<HTMLInputElement>(null);
  const [soonMs] = useState(() => Date.now() + 90 * 864e5);
  const note = (t: string, ok = true) => setMsg({ t, ok });
  const focusScan = () => setTimeout(() => scanRef.current?.focus(), 50);
  const today = () => todayLocal();
  const expSoon = (d: string) => new Date(d).getTime() < soonMs;
  const tabs = <ModeTabs mode={mode} set={(m) => { setMode(m); setTake(null); focusScan(); }} />;

  const lookup = useMutation({
    mutationFn: (q: string) => api<LookupRes>(`/inventory/items/lookup?q=${encodeURIComponent(q)}`),
    onSuccess: (d) => { setData(d); setMsg(null); setTake(null); },
    onError: (e) => { setData(null); note(e instanceof ApiError ? e.message : "조회 실패", false); focusScan(); },
  });
  const reload = () => data && lookup.mutate(data.item.barcode || data.item.code);
  const receive = useMutation({
    mutationFn: () => api("/inventory/movements/receive", { method: "POST",
      body: JSON.stringify({ item_id: data!.item.id, lot_no: lotNo, expiry_on: expiry, qty: Number(qty), moved_on: today() }) }),
    onSuccess: () => { note(`입고 완료 — ${data!.item.name} / ${lotNo} ×${qty}`);
      if (confirm("로트 라벨을 인쇄할까요?"))
        printLabel(`${data!.item.code}|${lotNo}|${expiry}`, [data!.item.name, `LOT ${lotNo} · 유효 ${expiry}`]);
      setLotNo(""); setExpiry(""); setQty(""); reload(); focusScan(); },
    onError: (e) => note(e instanceof ApiError ? e.message : "입고 실패", false),
  });
  const issue = useMutation({
    mutationFn: () => api<{ movements: unknown[] }>("/inventory/movements/issue", { method: "POST",
      body: JSON.stringify({ item_id: data!.item.id, qty: Number(qty), moved_on: today() }) }),
    onSuccess: (r) => { note(`출고 완료 — ${data!.item.name} ×${qty} (로트 ${r.movements.length}건 FIFO)`);
      setQty(""); reload(); focusScan(); },
    onError: (e) => note(e instanceof ApiError ? e.message : "출고 실패(재고·유효 확인)", false),
  });
  const narc = useMutation({
    mutationFn: () => api<{ balance: number; seq_no: number }>("/inventory/narcotics/entries", { method: "POST",
      body: JSON.stringify({ item_id: data!.item.id, entry_type: narcType, qty: Number(qty) }) }),
    onSuccess: (r) => { note(`마약류 수불 — ${narcType} ×${qty} (No.${r.seq_no}, 잔량 ${r.balance})`);
      setQty(""); reload(); focusScan(); },
    onError: (e) => note(e instanceof ApiError ? e.message : "마약류 수불 실패", false),
  });
  const record = useMutation({
    mutationFn: () => api<{ id: number; book_qty: number; diff_qty: number }>("/inventory/stock-takes", { method: "POST",
      body: JSON.stringify({ item_id: data!.item.id, physical_qty: Number(qty) }) }),
    onSuccess: (r) => { setTake({ id: r.id, book: r.book_qty, diff: r.diff_qty });
      note(`실사 기록 — 장부 ${r.book_qty} / 실사 ${qty} / 차이 ${r.diff_qty}`); },
    onError: (e) => note(e instanceof ApiError ? e.message : "실사 기록 실패", false),
  });
  const applyTake = useMutation({
    mutationFn: () => api(`/inventory/stock-takes/${take!.id}/apply`, { method: "POST" }),
    onSuccess: () => { note("실사 조정 반영 완료"); setTake(null); setQty(""); reload(); focusScan(); },
    onError: (e) => note(e instanceof ApiError ? e.message : "반영 실패", false),
  });

  const submitScan = (raw: string) => {
    const v = raw.trim();
    if (!v) return;
    const p = parseScan(v); lookup.mutate(p.key);
    if (mode === "receive") { if (p.lot) setLotNo(p.lot); if (p.expiry) setExpiry(p.expiry); }
    setScan(""); focusScan();
  };
  const onScan = (e: React.KeyboardEvent) => {
    if ((e.key === "Enter" || e.key === "Tab") && scan.trim()) {
      e.preventDefault();
      submitScan(scan);
    }
  };

  if (mode === "inspect") return <InspectMode tabs={tabs} />;
  if (mode === "list") return <ListMode tabs={tabs} expSoon={expSoon} />;

  const isNarc = data?.item.is_narcotic;
  return (
    <div className="mx-auto max-w-3xl space-y-4">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <h1 className="text-lg font-bold">재고 워크스테이션</h1>{tabs}
      </div>
      <Card title="바코드 스캔 / 코드 입력">
        <div className="flex items-stretch gap-2">
          <input ref={scanRef} autoFocus value={scan} onChange={(e) => setScan(e.target.value)}
            onKeyDown={onScan} placeholder="스캐너로 스캔하거나 코드 입력 후 Enter"
            inputMode="text" enterKeyHint="search"
            className="min-w-0 flex-1 rounded-xl border-2 border-blue-200 bg-blue-50/40 px-4 py-3 text-base outline-none focus:border-blue-400" />
          <CameraScanButton onScan={submitScan} />
        </div>
        <p className="mt-1.5 text-[11px] text-slate-400">GS1-128(제조사) 스캔 시 로트·유효기간 자동 입력 · 연속 스캔 · 📷 폰 카메라 스캔(지원 기기)</p>
      </Card>
      {msg && <p className={`rounded-lg px-3 py-2 text-sm ${msg.ok
        ? "bg-emerald-50 text-emerald-700" : "bg-red-50 text-red-700"}`}>{msg.t}</p>}
      {data && (
        <Card title={`${data.item.name} (${data.item.code})`} action={isNarc ? <Badge tone="red">마약류</Badge> : null}>
          <div className="flex flex-wrap items-center gap-x-6 gap-y-1 text-sm">
            <span>현재고 <b className={data.below_safety ? "text-red-600" : "text-slate-900"}>
              {data.stock.toLocaleString()} {data.item.unit}</b></span>
            <span className="text-slate-500">안전재고 {data.item.safety_stock.toLocaleString()}</span>
            {data.below_safety && <Badge tone="amber">안전재고 미달</Badge>}
            <button onClick={() => printLabel(data.item.barcode || data.item.code, [data.item.name, data.item.code])}
              className="ml-auto rounded bg-slate-600 px-2.5 py-1 text-xs text-white">품목 라벨</button>
          </div>
          {isNarc && (mode === "receive" || mode === "issue") && (
            <div className="mt-4 border-t border-slate-100 pt-4">
              <p className="mb-2 text-[12px] font-semibold text-red-600">마약류 수불부 (append-only · 감사)</p>
              <div className="grid grid-cols-1 gap-2 sm:grid-cols-3">
                <label className="block"><span className="mb-1 block text-xs font-medium text-slate-600">구분</span>
                  <select value={narcType} onChange={(e) => setNarcType(e.target.value)}
                    className="w-full rounded-md border border-slate-300 px-2 py-1.5 text-sm">
                    <option value="IN">입고</option><option value="OUT">출고</option>
                    <option value="DISPOSE">폐기</option><option value="ADJUST">조정</option></select></label>
                <Input label="수량" type="number" value={qty} onChange={(e) => setQty(e.target.value)} />
                <div className="flex items-end"><Button className="w-full" variant="danger" isLoading={narc.isPending}
                  disabled={!(Number(qty) > 0)} onClick={() => narc.mutate()}>수불 기록</Button></div>
              </div>
            </div>
          )}
          {!isNarc && mode === "receive" && (
            <div className="mt-4 grid grid-cols-1 gap-2 border-t border-slate-100 pt-4 sm:grid-cols-4">
              <Input label="로트번호" value={lotNo} onChange={(e) => setLotNo(e.target.value)} />
              <Input label="유효기간" type="date" value={expiry} onChange={(e) => setExpiry(e.target.value)} />
              <Input label="수량" type="number" value={qty} onChange={(e) => setQty(e.target.value)} />
              <div className="flex items-end"><Button className="w-full" isLoading={receive.isPending}
                disabled={!lotNo || !expiry || !(Number(qty) > 0)} onClick={() => receive.mutate()}>입고</Button></div>
            </div>
          )}
          {!isNarc && mode === "issue" && (
            <div className="mt-4 grid grid-cols-1 gap-2 border-t border-slate-100 pt-4 sm:grid-cols-3">
              <Input label="출고 수량" type="number" value={qty} onChange={(e) => setQty(e.target.value)} />
              <div className="flex items-end"><Button className="w-full" isLoading={issue.isPending}
                disabled={!(Number(qty) > 0)} onClick={() => issue.mutate()}>출고 (FIFO)</Button></div>
              <p className="flex items-end text-[11px] text-slate-400">유효임박 로트부터 출고됩니다.</p>
            </div>
          )}
          {mode === "stocktake" && (
            <div className="mt-4 border-t border-slate-100 pt-4">
              <div className="grid grid-cols-1 gap-2 sm:grid-cols-3">
                <Input label="실사 수량" type="number" value={qty} onChange={(e) => setQty(e.target.value)} />
                <div className="flex items-end"><Button className="w-full" isLoading={record.isPending}
                  disabled={qty === ""} onClick={() => record.mutate()}>차이 확인</Button></div>
              </div>
              {take && (
                <div className="mt-3 flex flex-wrap items-center gap-3 rounded-lg bg-slate-50 px-3 py-2 text-sm">
                  <span>장부 <b>{take.book.toLocaleString()}</b></span><span>실사 <b>{Number(qty).toLocaleString()}</b></span>
                  <span>차이 <b className={take.diff !== 0 ? "text-red-600" : "text-emerald-600"}>
                    {take.diff > 0 ? "+" : ""}{take.diff.toLocaleString()}</b></span>
                  <Button className="ml-auto" variant={take.diff !== 0 ? "danger" : "secondary"}
                    isLoading={applyTake.isPending} onClick={() => applyTake.mutate()}>조정 반영</Button>
                </div>
              )}
            </div>
          )}
          <div className="mt-4">
            <p className="mb-1 text-[12px] font-semibold uppercase text-slate-400">로트 (유효기간순)</p>
            <table className="w-full text-sm">
              <thead><tr className="text-left text-[11px] uppercase text-slate-400">
                <th className="py-1">로트</th><th>유효기간</th><th className="text-right">잔량</th><th></th></tr></thead>
              <tbody>
                {data.lots.length === 0 ? (<tr><td colSpan={4} className="py-3 text-center text-slate-400">재고 로트 없음</td></tr>)
                  : data.lots.map((l) => (
                  <tr key={l.lot_no} className="border-t border-slate-100">
                    <td className="py-1.5">{l.lot_no}</td>
                    <td className={expSoon(l.expiry_on) ? "font-medium text-red-600" : ""}>{l.expiry_on}{expSoon(l.expiry_on) && " ⚠"}</td>
                    <td className="text-right">{l.remaining.toLocaleString()}</td>
                    <td className="text-right"><button onClick={() => printLabel(`${data.item.code}|${l.lot_no}|${l.expiry_on}`,
                      [data.item.name, `LOT ${l.lot_no} · 유효 ${l.expiry_on}`])}
                      className="text-xs text-blue-600 hover:underline">라벨</button></td>
                  </tr>))}
              </tbody>
            </table>
          </div>
        </Card>
      )}
    </div>
  );
}

function ModeTabs({ mode, set }: { mode: Mode; set: (m: Mode) => void }) {
  return (
    <div className="flex flex-wrap gap-1 rounded-lg bg-slate-100 p-1">
      {(["lookup", "receive", "issue", "stocktake", "inspect", "list"] as Mode[]).map((m) => (
        <button key={m} onClick={() => set(m)}
          className={`rounded-md px-3.5 py-2 text-sm font-medium transition active:scale-95 ${
            mode === m ? "bg-white text-blue-700 shadow" : "text-slate-500"}`}>{MODE_LABEL[m]}</button>
      ))}
    </div>
  );
}

/** 작업 목록 — 유효임박·안전재고 미달·검수 대기(승인 발주) + 인쇄. */
function ListMode({ tabs, expSoon }: { tabs: React.ReactNode; expSoon: (d: string) => boolean }) {
  const exp = useQuery({ queryKey: ["ws-expiring"],
    queryFn: () => api<{ items: { name: string; lot_no: string; expiry_on: string; remaining: number }[] }>("/inventory/expiring?days=90") });
  const sh = useQuery({ queryKey: ["ws-shortage"],
    queryFn: () => api<{ items: { code: string; name: string; on_hand: number; safety_stock: number; shortage: number }[] }>("/inventory/shortage") });
  const pos = useQuery({ queryKey: ["ws-pos2"],
    queryFn: () => api<{ items: Po[] }>("/inventory/purchase-orders") });
  const approved = (pos.data?.items ?? []).filter((p) => p.status === "APPROVED");
  const won = (n: number) => n.toLocaleString();

  return (
    <div className="mx-auto max-w-3xl space-y-4">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <h1 className="text-lg font-bold">재고 워크스테이션 — 작업 목록</h1>{tabs}
      </div>
      <Card title={`유효기간 임박 (90일, ${exp.data?.items.length ?? 0}건)`} action={
        <button onClick={() => printTable("유효기간 임박 목록", ["품목", "로트", "유효기간", "잔량"],
          (exp.data?.items ?? []).map((e) => [e.name, e.lot_no, e.expiry_on, won(e.remaining)]))}
          className="rounded bg-slate-600 px-2.5 py-1 text-xs text-white">목록 인쇄</button>}>
        <table className="w-full text-sm"><thead><tr className="text-left text-[11px] uppercase text-slate-400">
          <th className="py-1">품목</th><th>로트</th><th>유효기간</th><th className="text-right">잔량</th></tr></thead>
          <tbody>{(exp.data?.items ?? []).length === 0 ? (<tr><td colSpan={4} className="py-3 text-center text-slate-400">임박 로트 없음</td></tr>)
            : (exp.data?.items ?? []).map((e, i) => (<tr key={i} className="border-t border-slate-100">
              <td className="py-1.5">{e.name}</td><td>{e.lot_no}</td>
              <td className={expSoon(e.expiry_on) ? "font-medium text-red-600" : ""}>{e.expiry_on}</td>
              <td className="text-right">{won(e.remaining)}</td></tr>))}</tbody></table>
      </Card>
      <Card title={`안전재고 미달 (${sh.data?.items.length ?? 0}건)`} action={
        <button onClick={() => printTable("안전재고 미달 목록", ["코드", "품목", "현재고", "안전재고", "부족"],
          (sh.data?.items ?? []).map((s) => [s.code, s.name, won(s.on_hand), won(s.safety_stock), won(s.shortage)]))}
          className="rounded bg-slate-600 px-2.5 py-1 text-xs text-white">목록 인쇄</button>}>
        <table className="w-full text-sm"><thead><tr className="text-left text-[11px] uppercase text-slate-400">
          <th className="py-1">품목</th><th className="text-right">현재고</th><th className="text-right">안전재고</th><th className="text-right">부족</th></tr></thead>
          <tbody>{(sh.data?.items ?? []).length === 0 ? (<tr><td colSpan={4} className="py-3 text-center text-slate-400">없음</td></tr>)
            : (sh.data?.items ?? []).map((s, i) => (<tr key={i} className="border-t border-slate-100">
              <td className="py-1.5">{s.name} <span className="text-[11px] text-slate-400">{s.code}</span></td>
              <td className="text-right">{won(s.on_hand)}</td><td className="text-right">{won(s.safety_stock)}</td>
              <td className="text-right font-medium text-red-600">{won(s.shortage)}</td></tr>))}</tbody></table>
      </Card>
      <Card title={`검수 대기 — 승인 발주 (${approved.length}건)`} action={
        <button onClick={() => printTable("검수 대기 발주", ["발주#", "공급처", "품목", "요청일"],
          approved.map((p) => [p.id, p.supplier_id, p.lines.map((l) => `#${l.item_id}×${l.qty}`).join(", "),
            new Date(p.requested_at).toLocaleDateString("ko-KR")]))}
          className="rounded bg-slate-600 px-2.5 py-1 text-xs text-white">목록 인쇄</button>}>
        <table className="w-full text-sm"><thead><tr className="text-left text-[11px] uppercase text-slate-400">
          <th className="py-1">#</th><th>공급처</th><th>품목</th><th>요청일</th></tr></thead>
          <tbody>{approved.length === 0 ? (<tr><td colSpan={4} className="py-3 text-center text-slate-400">없음</td></tr>)
            : approved.map((p) => (<tr key={p.id} className="border-t border-slate-100">
              <td className="py-1.5">{p.id}</td><td>{p.supplier_id}</td>
              <td>{p.lines.map((l) => `#${l.item_id}×${l.qty}`).join(", ")}</td>
              <td>{new Date(p.requested_at).toLocaleDateString("ko-KR")}</td></tr>))}</tbody></table>
      </Card>
    </div>
  );
}

/** 발주 검수 — 승인 발주 선택 → 라인별 입고 + 발주수량 대조. */
function InspectMode({ tabs }: { tabs: React.ReactNode }) {
  const [poId, setPoId] = useState<number | null>(null);
  const [rows, setRows] = useState<Record<number, { lot: string; exp: string; qty: string }>>({});
  const [msg, setMsg] = useState<{ t: string; ok: boolean } | null>(null);
  const pos = useQuery({ queryKey: ["ws-pos"], queryFn: () => api<{ items: Po[] }>("/inventory/purchase-orders") });
  const items = useQuery({ queryKey: ["ws-items"],
    queryFn: () => api<{ items: { id: number; code: string; name: string }[] }>("/inventory/items?size=200") });
  const nameOf = (id: number) => items.data?.items.find((i) => i.id === id);
  const approved = (pos.data?.items ?? []).filter((p) => p.status === "APPROVED");
  const po = approved.find((p) => p.id === poId);
  const setRow = (id: number, p: Partial<{ lot: string; exp: string; qty: string }>) =>
    setRows((r) => ({ ...r, [id]: { ...(r[id] ?? { lot: "", exp: "", qty: "" }), ...p } }));
  const receive = useMutation({
    mutationFn: () => api(`/inventory/purchase-orders/${poId}/receive`, { method: "POST",
      body: JSON.stringify({ receipts: (po?.lines ?? []).map((l) => ({ item_id: l.item_id,
        lot_no: rows[l.item_id]?.lot || "", expiry_on: rows[l.item_id]?.exp || "",
        qty: Number(rows[l.item_id]?.qty || 0) })) }) }),
    onSuccess: () => { setMsg({ t: "검수 입고 완료 — 발주 RECEIVED", ok: true }); setPoId(null); pos.refetch(); },
    onError: (e) => setMsg({ t: e instanceof ApiError ? e.message : "검수 입고 실패", ok: false }),
  });
  return (
    <div className="mx-auto max-w-3xl space-y-4">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <h1 className="text-lg font-bold">재고 워크스테이션 — 발주 검수</h1>{tabs}</div>
      {msg && <p className={`rounded-lg px-3 py-2 text-sm ${msg.ok ? "bg-emerald-50 text-emerald-700" : "bg-red-50 text-red-700"}`}>{msg.t}</p>}
      {!po ? (
        <Card title={`승인 발주 (${approved.length}건)`}>
          <table className="w-full text-sm"><thead><tr className="text-left text-[11px] uppercase text-slate-400">
            <th className="py-1">#</th><th>공급처</th><th>품목</th><th></th></tr></thead>
            <tbody>{approved.length === 0 ? (<tr><td colSpan={4} className="py-4 text-center text-slate-400">승인 발주 없음</td></tr>)
              : approved.map((p) => (<tr key={p.id} className="border-t border-slate-100">
                <td className="py-1.5">{p.id}</td><td>{p.supplier_id}</td>
                <td>{p.lines.map((l) => `#${l.item_id}×${l.qty}`).join(", ")}</td>
                <td className="text-right"><button onClick={() => { setPoId(p.id); setRows({}); }}
                  className="rounded bg-blue-600 px-2.5 py-1 text-xs text-white">검수</button></td></tr>))}</tbody></table>
        </Card>
      ) : (
        <Card title={`발주 #${po.id} 검수`} action={<button onClick={() => setPoId(null)} className="text-xs text-slate-500">← 목록</button>}>
          <div className="space-y-3">
            {po.lines.map((l) => { const it = nameOf(l.item_id); const r = rows[l.item_id] ?? { lot: "", exp: "", qty: "" };
              const mismatch = r.qty !== "" && Number(r.qty) !== l.qty;
              return (<div key={l.item_id} className="rounded-lg border border-slate-100 p-3">
                <p className="mb-1 text-sm font-medium">{it ? `${it.name} (${it.code})` : `품목 #${l.item_id}`}
                  <span className="ml-2 text-xs text-slate-400">발주 {l.qty}</span>
                  {mismatch && <span className="ml-2 text-xs font-semibold text-red-600">수량 불일치</span>}</p>
                <div className="grid grid-cols-1 gap-2 sm:grid-cols-3">
                  <Input label="로트" value={r.lot} onChange={(e) => setRow(l.item_id, { lot: e.target.value })} />
                  <Input label="유효기간" type="date" value={r.exp} onChange={(e) => setRow(l.item_id, { exp: e.target.value })} />
                  <Input label="입고수량" type="number" value={r.qty} placeholder={String(l.qty)}
                    onChange={(e) => setRow(l.item_id, { qty: e.target.value })} /></div></div>); })}
            <Button isLoading={receive.isPending}
              disabled={po.lines.some((l) => !rows[l.item_id]?.lot || !rows[l.item_id]?.exp || !(Number(rows[l.item_id]?.qty) > 0))}
              onClick={() => receive.mutate()}>검수 입고 (발주 입고 처리)</Button>
          </div>
        </Card>
      )}
    </div>
  );
}

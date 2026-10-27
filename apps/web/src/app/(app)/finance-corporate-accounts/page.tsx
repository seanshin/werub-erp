"use client";
/** 재무 — 법인 자금계좌 관리(외화 포함). 통화별 별도 계좌·기본계좌·GL 매핑. 외화는 원화로
 *  섞지 않고 통화별 분리 관리. 계좌번호는 기본 마스킹(전체보기 토글). */
import { monthLocal, todayLocal } from "@/lib/dates";
import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { ApiError, api } from "@/lib/api";
import { Badge, Button, Card, Input, Modal, Table } from "@/components/ui";
import { useToast } from "@/components/toast";
import { won } from "@/lib/format";

interface Acct {
  id: number; alias: string; bank_name: string; account_no: string; currency: string;
  gl_account_code: string; is_default: boolean; is_active: boolean;
  holder: string | null; swift: string | null; note: string | null;
}
type Draft = Omit<Acct, "id">;

const CURRENCIES = ["KRW", "USD", "EUR", "JPY", "CNY", "GBP"];
const BLANK: Draft = { alias: "", bank_name: "", account_no: "", currency: "KRW",
  gl_account_code: "1110", is_default: false, is_active: true, holder: "", swift: "", note: "" };

export default function CorporateAccountsPage() {
  const qc = useQueryClient();
  const toast = useToast();
  const [reveal, setReveal] = useState(false);
  const [edit, setEdit] = useState<Acct | "new" | null>(null);
  const fail = (e: unknown) => toast.err(e instanceof ApiError ? e.error.message : "처리 실패");

  const accts = useQuery({
    queryKey: ["corp-accounts", reveal],
    queryFn: () => api<{ items: Acct[] }>(`/finance/corporate-accounts?reveal=${reveal}`),
  });
  const refresh = () => qc.invalidateQueries({ queryKey: ["corp-accounts"] });

  const save = useMutation({
    mutationFn: (r: Draft & { id?: number }) => {
      // 편집(PUT) 시 계좌번호가 마스킹값(reveal=false로 조회된 "****7890")이면 페이로드에서 제외 —
      // 서버 setattr 무가드 덮어쓰기로 실계좌번호가 마스킹값으로 파손되던 것 방지(적대검토·온보딩 벌크 동일 규약).
      if (r.id) {
        const body: Partial<Draft> & { id?: number } = { ...r };
        if (typeof body.account_no === "string" && body.account_no.includes("*"))
          delete body.account_no;   // 변경 시에만 실계좌 전송(마스킹값은 기존 유지)
        return api(`/finance/corporate-accounts/${r.id}`, { method: "PUT", body: JSON.stringify(body) });
      }
      return api("/finance/corporate-accounts", { method: "POST", body: JSON.stringify(r) });
    },
    onSuccess: () => { toast.ok("계좌 저장 완료"); setEdit(null); refresh(); },
    onError: fail,
  });
  // ⚠ 종전에는 window.prompt 3연타로 곧바로 POST /forex/post를 호출했다 — 얼마가 전기될지
  //   못 보고 GL을 확정하는 구조였고, 서버가 주는 경고(통화 혼입 차단·동일 통화 다계좌 합계
  //   안내)를 볼 방법도 없었다. preview로 먼저 보여주고 확인 후 전기하도록 바꾼다.
  const [fx, setFx] = useState<Acct | null>(null);       // 기말환산 대상
  const [mv, setMv] = useState<Acct | null>(null);       // 입출금 대상

  return (
    <div className="space-y-4">
      <div className="flex items-center justify-between">
        <h1 className="text-lg font-bold">재무 — 법인 자금계좌</h1>
        <div className="flex items-center gap-2">
          <label className="flex items-center gap-1 text-xs text-slate-500">
            <input type="checkbox" checked={reveal} onChange={(e) => setReveal(e.target.checked)} />
            계좌번호 전체보기</label>
          <Button onClick={() => setEdit("new")}>계좌 추가</Button>
        </div>
      </div>
      <p className="text-xs text-slate-500">
        통화별 법인 자금계좌를 관리합니다. 외화는 원화로 섞지 않고 통화별 별도 계좌·GL(외화예금)로
        분리합니다. 통화별 기본계좌(★)는 외화 거래 부킹 시 사용됩니다. 계좌번호는 기본 마스킹.</p>
      {accts.isError && <p className="text-sm text-red-600">계좌 조회 권한이 없습니다 (DIRECTOR/ADMIN)</p>}

      <Card title={`법인 계좌 (${accts.data?.items.length ?? 0})`}>
        <Table headers={["통화", "별칭", "은행", "계좌번호", "GL", "예금주", "기본", "활성", ""]}
          rows={(accts.data?.items ?? []).map((a) => [
            <Badge key="c" tone={a.currency === "KRW" ? "gray" : "blue"}>{a.currency}</Badge>,
            a.alias, a.bank_name,
            <span key="n" className="font-mono text-xs">{a.account_no}</span>,
            a.gl_account_code, a.holder ?? "—",
            a.is_default ? <Badge key="d" tone="green">★ 기본</Badge> : "",
            a.is_active ? <Badge key="a" tone="green">활성</Badge>
              : <Badge key="a" tone="gray">비활성</Badge>,
            <span key="e" className="flex gap-1">
              <Button variant="secondary" onClick={() => setEdit(a)}>편집</Button>
              {a.currency !== "KRW" && (<>
                <Button variant="secondary" onClick={() => setMv(a)}>입출금</Button>
                <Button variant="secondary" onClick={() => setFx(a)}>기말환산</Button>
              </>)}
            </span>,
          ])} empty="등록된 법인 계좌 없음" />
      </Card>

      {fx && (
        <Modal title={`기말환산 — ${fx.alias} (${fx.currency})`} onClose={() => setFx(null)}>
          <ForexTranslateForm acct={fx} onDone={() => setFx(null)} onFail={fail}
            onOk={(m) => { toast.ok(m); setFx(null); }} />
        </Modal>
      )}

      {mv && (
        <Modal title={`외화 입출금 — ${mv.alias} (${mv.currency})`} onClose={() => setMv(null)}>
          <ForexMovementForm acct={mv} onFail={fail}
            onOk={(m) => { toast.ok(m); setMv(null); }} />
        </Modal>
      )}

      {edit && (
        <Modal title={edit === "new" ? "법인 계좌 추가" : `계좌 편집 #${edit.id}`}
          onClose={() => setEdit(null)}>
          <AcctForm initial={edit === "new" ? BLANK : edit}
            isLoading={save.isPending}
            onSubmit={(d) => save.mutate(edit === "new" ? d : { ...d, id: edit.id })} />
        </Modal>
      )}
    </div>
  );
}

function AcctForm({ initial, onSubmit, isLoading }: {
  initial: Draft; onSubmit: (d: Draft) => void; isLoading: boolean;
}) {
  const [f, setF] = useState<Draft>(initial);
  const set = (p: Partial<Draft>) => setF({ ...f, ...p });
  return (
    <div className="space-y-2">
      <Input label="별칭" value={f.alias} onChange={(e) => set({ alias: e.target.value })}
        placeholder="예: 신한 USD 외화" />
      <div className="grid grid-cols-2 gap-2">
        <Input label="은행" value={f.bank_name} onChange={(e) => set({ bank_name: e.target.value })} />
        <Input label="계좌번호" value={f.account_no}
          onChange={(e) => set({ account_no: e.target.value })} />
      </div>
      <div className="grid grid-cols-2 gap-2">
        <label className="block text-sm text-slate-600">통화
          <select value={f.currency} onChange={(e) => set({ currency: e.target.value })}
            className="w-full rounded-md border border-slate-300 px-2 py-1.5 text-sm">
            {CURRENCIES.map((c) => <option key={c} value={c}>{c}</option>)}
          </select>
        </label>
        <Input label="GL 계정코드" value={f.gl_account_code}
          onChange={(e) => set({ gl_account_code: e.target.value })}
          placeholder="KRW=1110 / 외화=1115" />
      </div>
      <div className="grid grid-cols-2 gap-2">
        <Input label="예금주" value={f.holder ?? ""}
          onChange={(e) => set({ holder: e.target.value || null })} />
        <Input label="SWIFT/BIC (외화)" value={f.swift ?? ""}
          onChange={(e) => set({ swift: e.target.value || null })} />
      </div>
      <Input label="비고" value={f.note ?? ""}
        onChange={(e) => set({ note: e.target.value || null })} />
      <div className="flex gap-4 pt-1">
        <label className="flex items-center gap-1.5 text-sm text-slate-600">
          <input type="checkbox" checked={f.is_default}
            onChange={(e) => set({ is_default: e.target.checked })} /> 통화별 기본계좌</label>
        <label className="flex items-center gap-1.5 text-sm text-slate-600">
          <input type="checkbox" checked={f.is_active}
            onChange={(e) => set({ is_active: e.target.checked })} /> 활성</label>
      </div>
      <Button disabled={isLoading || !f.alias || !f.bank_name || !f.account_no || !f.gl_account_code}
        onClick={() => onSubmit(f)}>저장</Button>
    </div>
  );
}


interface Preview {
  account_id: number; currency: string; gl: string; fc_balance: number; rate: number;
  translated_krw: number; book_value: number; delta: number;
  shared_gl_accounts?: number[]; warning?: string;
}

/** 기말환산 — **미리보기 후 확인**해야 전기된다(K-IFRS 1021).
 *  종전 prompt 방식은 얼마가 전기될지 모른 채 GL을 확정했고 서버 경고도 보이지 않았다. */
function ForexTranslateForm({ acct, onOk, onFail, onDone }: {
  acct: Acct; onOk: (m: string) => void; onFail: (e: unknown) => void; onDone: () => void;
}) {
  const [ym, setYm] = useState(monthLocal());
  const [bal, setBal] = useState("");
  const [rate, setRate] = useState("");
  const [pv, setPv] = useState<Preview | null>(null);

  const qs = () => `account_id=${acct.id}&fc_balance=${Number(bal)}`
    + `&rate=${Number(rate)}&yyyymm=${ym}`;

  const preview = useMutation({
    mutationFn: () => api<Preview>(`/finance/forex/preview?${qs()}`),
    onSuccess: setPv,
    onError: (e) => { setPv(null); onFail(e); },
  });
  const post = useMutation({
    mutationFn: () => api<{ posted: boolean; delta: number }>(
      `/finance/forex/post?${qs()}`, { method: "POST" }),
    onSuccess: (r) => { onOk(r.posted
      ? `외화환산 ${r.delta >= 0 ? "이익" : "손실"} ${won(Math.abs(r.delta))} 전기`
      : "환산차 없음 — 전기하지 않음"); onDone(); },
    onError: onFail,
  });

  const ready = ym && Number(bal) > 0 && Number(rate) > 0;
  return (
    <div className="space-y-2">
      <Input label="환산 기간 (YYYY-MM)" value={ym}
        onChange={(e) => { setYm(e.target.value); setPv(null); }} />
      <Input label={`외화 잔액 (${acct.currency})`} value={bal}
        onChange={(e) => { setBal(e.target.value); setPv(null); }} />
      <Input label="기말 환율 (1통화당 원)" value={rate}
        onChange={(e) => { setRate(e.target.value); setPv(null); }} />

      {!pv ? (
        <Button disabled={!ready} isLoading={preview.isPending}
          onClick={() => preview.mutate()}>미리보기</Button>
      ) : (
        <div className="space-y-2">
          <div className="rounded border border-slate-200 bg-slate-50 p-2 text-xs">
            <div>환산액 {won(pv.translated_krw)} = {Number(bal).toLocaleString()} {pv.currency} × {pv.rate}</div>
            <div>장부가 {won(pv.book_value)} (GL {pv.gl})</div>
            <div className="mt-1 font-semibold">
              환산차 <span className={pv.delta >= 0 ? "text-blue-600" : "text-red-600"}>
                {pv.delta >= 0 ? "이익 " : "손실 "}{won(Math.abs(pv.delta))}</span>
              {pv.delta === 0 && " — 전기 대상 없음"}
            </div>
          </div>
          {pv.warning && (
            <p className="rounded border border-amber-300 bg-amber-50 p-2 text-xs text-amber-800">
              ⚠ {pv.warning}</p>)}
          <div className="flex gap-2">
            <Button variant="secondary" onClick={() => setPv(null)}>수정</Button>
            <Button disabled={pv.delta === 0} isLoading={post.isPending}
              onClick={() => post.mutate()}>이대로 전기</Button>
          </div>
          <p className="text-[11px] text-slate-400">
            전기하면 외화환산이익(4650)/손실(5670)과 {pv.gl} 계정에 전표가 생성됩니다.
          </p>
        </div>
      )}
    </div>
  );
}

/** 외화 입출금 — 취득환율로 통화별 계좌 GL에 부킹(Stage2). 백엔드는 있었으나 화면이 없었다. */
function ForexMovementForm({ acct, onOk, onFail }: {
  acct: Acct; onOk: (m: string) => void; onFail: (e: unknown) => void;
}) {
  const [f, setF] = useState({ direction: "IN", fc_amount: "", rate: "", on: todayLocal() });
  const krw = Number(f.fc_amount) * Number(f.rate);
  const save = useMutation({
    mutationFn: () => api<{ krw: number; voucher_id: number }>("/finance/forex/movements", {
      method: "POST",
      body: JSON.stringify({ account_id: acct.id, direction: f.direction,
        fc_amount: Number(f.fc_amount), rate: Number(f.rate), on: f.on }) }),
    onSuccess: (r) => onOk(`${f.direction === "IN" ? "입금" : "출금"} ${won(r.krw)} 전기 (#${r.voucher_id})`),
    onError: onFail,
  });
  const ready = Number(f.fc_amount) > 0 && Number(f.rate) > 0 && f.on;
  return (
    <div className="space-y-2">
      <label className="block text-xs font-medium text-slate-600">구분
        <select value={f.direction} onChange={(e) => setF({ ...f, direction: e.target.value })}
          className="mt-0.5 w-full rounded border border-slate-300 px-2 py-1 text-sm">
          <option value="IN">입금 (DR 외화예금 / CR 현금)</option>
          <option value="OUT">출금 (DR 현금 / CR 외화예금)</option>
        </select>
      </label>
      <Input label={`외화 금액 (${acct.currency})`} value={f.fc_amount}
        onChange={(e) => setF({ ...f, fc_amount: e.target.value })} />
      <Input label="거래 환율 (취득원가)" value={f.rate}
        onChange={(e) => setF({ ...f, rate: e.target.value })} />
      <Input label="거래일" type="date" value={f.on}
        onChange={(e) => setF({ ...f, on: e.target.value })} />
      {krw > 0 && (
        <p className="rounded bg-slate-50 p-2 text-xs text-slate-600">
          원화 환산 <b>{won(Math.round(krw))}</b> — 이 금액으로 GL {acct.gl_account_code}에 부킹됩니다.
        </p>)}
      <Button disabled={!ready} isLoading={save.isPending} onClick={() => save.mutate()}>전기</Button>
    </div>
  );
}

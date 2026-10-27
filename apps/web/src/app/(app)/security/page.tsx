"use client";
/** 내 보안 (2FA) — PIN 설정 + 스마트폰 생체(패스키) 등록·관리. */
import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { startRegistration } from "@simplewebauthn/browser";
import { ApiError, api } from "@/lib/api";
import { Button, Card, Input } from "@/components/ui";
import { useToast } from "@/components/toast";
import { useConfirm } from "@/components/confirm";

interface Passkey { id: number; name: string | null; created_at: string;
  last_used_at: string | null }

export default function SecurityPage() {
  const qc = useQueryClient();
  const [pin, setPin] = useState("");
  const [pin2, setPin2] = useState("");
  const [name, setName] = useState("");
  const toast = useToast();
  const confirm = useConfirm();
  const note = (t: string, ok = true) => (ok ? toast.ok(t) : toast.err(t));
  const fail = (e: unknown, f: string) => note(e instanceof ApiError ? e.message : f, false);

  const passkeys = useQuery({ queryKey: ["my-passkeys"],
    queryFn: () => api<{ items: Passkey[] }>("/auth/passkeys") });

  const setPinM = useMutation({
    mutationFn: () => api("/auth/pin", { method: "POST", body: JSON.stringify({ pin }) }),
    onSuccess: () => { note("PIN이 설정되었습니다"); setPin(""); setPin2(""); },
    onError: (e) => fail(e, "PIN 설정 실패"),
  });

  const registerPasskey = useMutation({
    mutationFn: async () => {
      const begin = await api<{ options: object; token: string }>(
        "/auth/passkeys/register/begin", { method: "POST" });
      const credential = await startRegistration({ optionsJSON: begin.options as never });
      return api("/auth/passkeys/register/complete", { method: "POST",
        body: JSON.stringify({ token: begin.token, credential, name: name || "내 기기" }) });
    },
    onSuccess: () => { note("생체인증(패스키)이 등록되었습니다"); setName("");
      qc.invalidateQueries({ queryKey: ["my-passkeys"] }); },
    onError: (e) => {
      if (e && typeof e === "object" && "name" in e &&
          (e as { name: string }).name === "NotAllowedError") note("등록이 취소되었습니다", false);
      else fail(e, "패스키 등록 실패 (HTTPS·지원 기기 필요)");
    },
  });

  const del = useMutation({
    mutationFn: (id: number) => api(`/auth/passkeys/${id}`, { method: "DELETE" }),
    onSuccess: () => { note("패스키 삭제됨"); qc.invalidateQueries({ queryKey: ["my-passkeys"] }); },
    onError: (e) => fail(e, "패스키 삭제 실패"),
  });
  const onDelete = async (id: number, name: string | null) => {
    if (await confirm({ title: "패스키 삭제", danger: true, confirmLabel: "삭제",
      body: `"${name ?? "기기"}" 패스키를 삭제할까요? 이 기기로는 생체 로그인이 불가해집니다.` }))
      del.mutate(id);
  };

  const pinValid = /^\d{4,8}$/.test(pin) && pin === pin2;

  return (
    <div className="mx-auto max-w-xl space-y-4">
      <h1 className="text-lg font-bold">내 보안 (2FA 로그인)</h1>
      <p className="text-xs text-slate-500">
        PIN과 스마트폰 생체인증(패스키)을 등록하면 HIS SSO 없이도 직접 로그인할 수 있습니다.</p>

      <Card title="① PIN 설정">
        <div className="space-y-3">
          <Input label="PIN (숫자 4~8자리)" type="password" inputMode="numeric" maxLength={8}
            value={pin} onChange={(e) => setPin(e.target.value.replace(/\D/g, ""))} />
          <Input label="PIN 확인" type="password" inputMode="numeric" maxLength={8}
            value={pin2} onChange={(e) => setPin2(e.target.value.replace(/\D/g, ""))} />
          {pin && pin2 && pin !== pin2 && (
            <p className="text-xs text-red-600">PIN이 일치하지 않습니다</p>)}
          <Button disabled={!pinValid} isLoading={setPinM.isPending}
            onClick={() => setPinM.mutate()}>PIN 저장</Button>
        </div>
      </Card>

      <Card title="② 스마트폰 생체인증(패스키) 등록" action={
        <span className="text-[11px] text-slate-400">지문 · Face ID</span>}>
        <div className="space-y-3">
          <Input label="기기 별칭(선택)" value={name} placeholder="예: 내 아이폰"
            onChange={(e) => setName(e.target.value)} />
          <Button isLoading={registerPasskey.isPending} onClick={() => registerPasskey.mutate()}>
            이 기기에 생체인증 등록</Button>
          <div className="pt-2">
            <p className="mb-1 text-xs font-medium text-slate-500">등록된 패스키</p>
            {(passkeys.data?.items ?? []).length === 0 ? (
              <p className="text-xs text-slate-400">아직 없음 — 위에서 등록하세요</p>
            ) : (passkeys.data?.items ?? []).map((k) => (
              <div key={k.id} className="flex items-center justify-between border-b border-slate-100 py-1.5 text-sm">
                <span>{k.name ?? "기기"} <span className="text-[11px] text-slate-400">
                  · 등록 {new Date(k.created_at).toLocaleDateString("ko-KR")}</span>
                  {/* 마지막 사용 시각이 없으면 어느 기기를 정리해도 되는지 판단할 수 없다 */}
                  {k.last_used_at
                    ? <span className="ml-1 text-[11px] text-slate-400">
                        · 최근 사용 {new Date(k.last_used_at).toLocaleDateString("ko-KR")}</span>
                    : <span className="ml-1 text-[11px] text-amber-600">· 사용 이력 없음</span>}
                </span>
                <button onClick={() => onDelete(k.id, k.name)} disabled={del.isPending}
                  className="text-xs text-red-500 hover:underline disabled:text-slate-300">삭제</button>
              </div>
            ))}
          </div>
        </div>
      </Card>
    </div>
  );
}

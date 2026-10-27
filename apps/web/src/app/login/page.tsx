"use client";
/** ERP 로그인 — PIN + 스마트폰 생체(패스키) 2FA. 비밀번호 폴백·HIS SSO 안내. */
import { useRouter } from "next/navigation";
import { useState } from "react";
import { startAuthentication } from "@simplewebauthn/browser";
import { ApiError, api, auth } from "@/lib/api";
import { APP_MAKER, APP_VERSION } from "@/lib/app-info";

export default function LoginPage() {
  const router = useRouter();
  const [email, setEmail] = useState("");
  const [pin, setPin] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [pwMode, setPwMode] = useState(false);
  const [password, setPassword] = useState("");

  const fail = (e: unknown, fallback: string) =>
    setError(e instanceof ApiError ? e.message : fallback);

  // PIN + 생체(패스키) 2FA
  const loginBiometric = async () => {
    setError(null); setBusy(true);
    try {
      const begin = await api<{ options: object; token: string }>("/auth/2fa/begin", {
        method: "POST", body: JSON.stringify({ email, pin }) });
      const assertion = await startAuthentication({ optionsJSON: begin.options as never });
      await api("/auth/2fa/complete", { method: "POST",
        body: JSON.stringify({ token: begin.token, credential: assertion }) });
      router.replace("/dashboard");
    } catch (e) {
      if (e && typeof e === "object" && "name" in e &&
          (e as { name: string }).name === "NotAllowedError")
        setError("생체인증이 취소되었습니다");
      else fail(e, "로그인에 실패했습니다 (PIN·생체인증 확인)");
    } finally { setBusy(false); }
  };

  const loginPassword = async () => {
    setError(null); setBusy(true);
    try { await auth.login(email, password); router.replace("/dashboard"); }
    catch (e) { fail(e, "로그인에 실패했습니다"); }
    finally { setBusy(false); }
  };

  const pinReady = /.+@.+/.test(email) && pin.length >= 4;
  const inputCls = "w-full rounded-xl border border-slate-200 bg-slate-50/60 px-4 py-3 text-sm " +
    "outline-none transition focus:border-blue-400 focus:bg-white focus:ring-2 focus:ring-blue-100";

  return (
    <main className="flex min-h-screen items-center justify-center bg-gradient-to-br from-slate-50 via-blue-50/40 to-emerald-50/30 p-4">
      <div className="w-full max-w-sm">
        <div className="mb-8 flex flex-col items-center gap-3">
          <span className="flex h-14 w-14 items-center justify-center rounded-2xl bg-gradient-to-br from-blue-600 to-blue-500 text-2xl font-bold text-white shadow-lg shadow-blue-200">H</span>
          <div className="text-center">
            <h1 className="text-xl font-bold tracking-tight text-slate-900">병원 ERP</h1>
            <p className="mt-0.5 text-[13px] text-slate-400">PIN과 생체인증으로 안전하게 로그인</p>
          </div>
        </div>

        <div className="rounded-2xl border border-slate-100 bg-white/90 p-6 shadow-xl shadow-slate-200/50 backdrop-blur">
          {!pwMode ? (
            <div className="space-y-3">
              <input className={inputCls} type="email" placeholder="이메일" autoComplete="username"
                value={email} onChange={(e) => setEmail(e.target.value)} />
              <input className={`${inputCls} tracking-[0.4em]`} type="password" inputMode="numeric"
                placeholder="PIN (숫자 4~8자리)" autoComplete="off" maxLength={8}
                value={pin} onChange={(e) => setPin(e.target.value.replace(/\D/g, ""))} />
              <button disabled={!pinReady || busy} onClick={loginBiometric}
                className="flex w-full items-center justify-center gap-2 rounded-xl bg-blue-600 py-3 text-sm font-semibold text-white shadow-lg shadow-blue-200 transition hover:bg-blue-700 disabled:bg-slate-300 disabled:shadow-none">
                <svg className="h-5 w-5" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={1.8}>
                  <path strokeLinecap="round" strokeLinejoin="round" d="M7.864 4.243A7.5 7.5 0 0 1 19.5 10.5c0 2.92-.556 5.709-1.568 8.268M5.742 6.364A7.465 7.465 0 0 0 4.5 10.5a7.464 7.464 0 0 1-1.15 3.993m1.989 3.559A11.2 11.2 0 0 0 8.25 10.5a3.75 3.75 0 1 1 7.5 0c0 .527-.021 1.049-.064 1.565M12 10.5a14.94 14.94 0 0 1-3.6 9.75m6.633-4.596a18.6 18.6 0 0 1-.722 3.846" />
                </svg>
                {busy ? "인증 중…" : "생체인증으로 로그인"}
              </button>
              <p className="text-center text-[11px] text-slate-400">
                PIN 입력 후 스마트폰 지문/Face ID로 인증합니다</p>
            </div>
          ) : (
            <div className="space-y-3">
              <input className={inputCls} type="email" placeholder="이메일" autoComplete="username"
                value={email} onChange={(e) => setEmail(e.target.value)} />
              <input className={inputCls} type="password" placeholder="비밀번호" autoComplete="current-password"
                value={password} onChange={(e) => setPassword(e.target.value)} />
              <button disabled={busy} onClick={loginPassword}
                className="w-full rounded-xl bg-slate-800 py-3 text-sm font-semibold text-white transition hover:bg-slate-900 disabled:bg-slate-300">
                {busy ? "로그인 중…" : "비밀번호로 로그인"}
              </button>
            </div>
          )}

          {error && <p className="mt-3 rounded-lg bg-red-50 px-3 py-2 text-center text-xs text-red-600">{error}</p>}

          <div className="mt-5 border-t border-slate-100 pt-4 text-center">
            <button onClick={() => { setPwMode(!pwMode); setError(null); }}
              className="text-xs font-medium text-slate-500 transition hover:text-blue-600">
              {pwMode ? "← PIN·생체인증으로 로그인" : "비밀번호로 로그인"}
            </button>
          </div>
        </div>

        <p className="mt-6 text-center text-[11px] text-slate-400">
          HIS에서 ERP 버튼으로도 접속할 수 있습니다 · 계정 잠금 시 15분 후 재시도</p>
        <p className="mt-1.5 text-center text-[11px] text-slate-300">
          병원 ERP v{APP_VERSION} · © {APP_MAKER}</p>
      </div>
    </main>
  );
}

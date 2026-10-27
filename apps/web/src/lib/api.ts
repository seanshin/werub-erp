/** API 클라이언트 — design §7.2. HIS SSO httpOnly 쿠키 세션(credentials:include).
 *
 * 토큰은 httpOnly 쿠키(JS 미접근) — Authorization 헤더·sessionStorage 미사용.
 * 역할 등 표시값은 /auth/me를 1회 조회해 캐시(권한 판정은 항상 서버).
 */

const BASE = process.env.NEXT_PUBLIC_API_URL ?? "/erp/api/v1";
// 미인증 복귀지 — /erp/login((app) 밖 독립 페이지: PIN+패스키+비밀번호+HIS SSO 안내).
// 과거 "/erp/"는 root page가 /dashboard로 재리다이렉트→(app) 부트스트랩 실패→"/erp/" 무한루프(P0-7).
const LANDING = "/erp/login";

/** 첨부·이미지 등 동일출처 리소스의 절대 경로(쿠키 인증 <img src> 등에 사용). */
export function apiUrl(path: string): string {
  return `${BASE}${path}`;
}

export interface ErpError {
  code: string;
  message: string;
  details?: Record<string, unknown>;
}

export class ApiError extends Error {
  constructor(public error: ErpError, public status: number) {
    super(error.message);
  }
}

// 세션 사용자 캐시 — bootstrap()이 /auth/me로 채움. 표시 전용.
let me: { sub?: string; role?: string; dept?: number | null; email?: string } | null = null;

let refreshing: Promise<boolean> | null = null;
let lastRefreshAt = 0;

async function tryRefresh(): Promise<boolean> {
  if (Date.now() - lastRefreshAt < 2_000) return true; // 직전 갱신 직후 — 재시도만
  refreshing ??= (async () => {
    // refresh_token 쿠키로 회전 → 백엔드가 새 세션 쿠키 갱신 (본문 없음)
    const res = await fetch(`${BASE}/auth/refresh`, { method: "POST", credentials: "include" });
    if (!res.ok) return false;
    lastRefreshAt = Date.now();
    return true;
  })().finally(() => { refreshing = null; });
  return refreshing;
}

/** CSRF double-submit — csrf_token 쿠키(non-httpOnly)를 읽어 X-CSRF-Token 헤더로 재제출 */
function csrfHeader(): Record<string, string> {
  if (typeof document === "undefined") return {};
  const m = document.cookie.match(/(?:^|;\s*)csrf_token=([^;]+)/);
  return m ? { "X-CSRF-Token": decodeURIComponent(m[1]) } : {};
}

export async function api<T = unknown>(
  path: string,
  init: RequestInit & { retry?: boolean } = {},
): Promise<T> {
  const { retry = true, ...rest } = init;
  const res = await fetch(`${BASE}${path}`, {
    ...rest,
    credentials: "include",
    headers: { "Content-Type": "application/json", ...csrfHeader(), ...rest.headers },
  });

  if (res.status === 401 && retry && (await tryRefresh())) {
    return api<T>(path, { ...init, retry: false });
  }
  if (!res.ok) {
    const body = await res.json().catch(() => null);
    const error: ErpError = body?.error ?? {
      code: `HTTP-${res.status}`, message: "요청 처리에 실패했습니다",
    };
    if (res.status === 401) {
      me = null;
      if (typeof window !== "undefined") window.location.href = LANDING;
    }
    throw new ApiError(error, res.status);
  }
  return res.status === 204 ? (undefined as T) : res.json();
}

/** multipart 업로드 — Content-Type은 브라우저가 설정, 쿠키 자동 전송 */
export async function apiUpload<T = unknown>(path: string, form: FormData,
                                             retry = true): Promise<T> {
  const res = await fetch(`${BASE}${path}`, {
    method: "POST", body: form, credentials: "include", headers: csrfHeader() });
  if (res.status === 401 && retry && (await tryRefresh())) {
    return apiUpload<T>(path, form, false);
  }
  if (!res.ok) {
    const body = await res.json().catch(() => null);
    throw new ApiError(body?.error ?? { code: `HTTP-${res.status}`, message: "업로드 실패" },
                       res.status);
  }
  return res.json();
}

/** 인증 다운로드 — fetch+blob (쿠키 자동 전송, <a href>로는 못 받음) */
export async function apiDownload(path: string, filename: string,
                                  retry = true): Promise<void> {
  const res = await fetch(`${BASE}${path}`, { credentials: "include" });
  if (res.status === 401 && retry && (await tryRefresh())) {
    return apiDownload(path, filename, false);
  }
  if (!res.ok) {
    const body = await res.json().catch(() => null);
    throw new ApiError(body?.error ?? { code: `HTTP-${res.status}`, message: "다운로드 실패" },
                       res.status);
  }
  const blob = await res.blob();
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url;
  a.download = filename;
  a.click();
  URL.revokeObjectURL(url);
}

export const auth = {
  /** 비밀번호 로그인(폴백) — 백엔드가 세션 쿠키 설정. 이후 세션 캐시 적재 */
  async login(email: string, password: string) {
    await api("/auth/login",
      { method: "POST", body: JSON.stringify({ email, password }), retry: false });
    await auth.bootstrap();
  },
  async logout() {
    await api("/auth/logout", { method: "POST" }).catch(() => undefined);
    me = null;
    if (typeof window !== "undefined") window.location.href = LANDING;
  },
  /** /auth/me 조회 → 세션 캐시(표시용). 인증 여부 반환. 401이면 refresh 1회 시도 */
  async bootstrap(): Promise<boolean> {
    try {
      let res = await fetch(`${BASE}/auth/me`, { credentials: "include" });
      if (res.status === 401 && (await tryRefresh())) {
        res = await fetch(`${BASE}/auth/me`, { credentials: "include" });
      }
      if (!res.ok) { me = null; return false; }
      const u = await res.json();
      me = { sub: String(u.user_id), role: u.role, dept: u.dept_id, email: u.email };
      return true;
    } catch { me = null; return false; }
  },
  isAuthenticated: () => me != null,
  /** 세션 사용자(role 등) — 표시용. 권한 판정은 항상 서버가 수행 */
  claims(): { sub?: string; role?: string; dept?: number | null; email?: string } | null {
    return me;
  },
};

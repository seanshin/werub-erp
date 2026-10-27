/** e-sign #6 트랙1 다자(STAFF+PORTAL 혼합) relay E2E — ERP측 검증 스크립트.
 *
 * sign 측 `test/erp-multisig-mixed.e2e.mjs`(PORTAL+STAFF 혼합 통과)의 **ERP 대응본**.
 * 합동 go-live(런북 §2)에서 트랙1 발의→HIS relay→ERP 응답까지를 ERP가 단독으로 구동·검증한다.
 * 검증 대상은 ERP 소유 불변식(채널 매핑·portal_links 추출·STAFF 비노출·순차)이며, HIS §6 per-signer
 * portalUrl relay 반영 여부는 soft-report(미반영=HIS 대기, 서명 비파괴).
 *
 * 모드
 *   1) self-check (네트워크 無 — ERP 단독, 오늘 green):
 *        node scripts/esign-relay-e2e.mjs --self-check
 *      → 검증 함수(assertRelayInvariants)를 정상/비정상 EP-출력 픽스처로 구동해 검증기 자체를 검증.
 *
 *   2) live (합동 세션 — 실 HIS relay 구동):
 *        ERP_TOKEN=<access_token> node scripts/esign-relay-e2e.mjs --contract 7 \
 *          [--base https://his.example.org] [--staff-ref BF-2026-0001] [--employee 12] \
 *          [--force] [--poll]
 *      → ACTIVE 의료계약에 STAFF+PORTAL 혼합 signers[]로 발의(POST .../request) → 응답 불변식 검증 +
 *        HIS §6 relay 반영 soft-report. --poll 시 실서명 후 미러 COMPLETED·완료본 회수까지 관찰.
 *      토큰 발급(운영 컨테이너): python /tmp/mktoken.py (security.create_access_token)
 *
 * 관련: docs/05-operations/esign-golive-runbook.md §2,
 *       docs/05-reference/erp-request-his-esign6-portalurl-relay-2026-06-29.md,
 *       services/core/.../integrations/his_sign_origination.py (initiate_medical_contract_signing).
 */

const args = process.argv.slice(2);
const has = (f) => args.includes(f);
const val = (f, d) => { const i = args.indexOf(f); return i >= 0 && args[i + 1] ? args[i + 1] : d; };

const BASE = val("--base", "https://his.example.org").replace(/\/$/, "");
const CONTRACT = val("--contract", null);
const STAFF_REF = val("--staff-ref", "BF-2026-0001");  // 실 HIS 의료진 사번(employee_no) — go-live 시 교체
const EMPLOYEE = val("--employee", null);              // --poll 미러 조회용(계약 직원 id)
const FORCE = has("--force");
const POLL = has("--poll");
const TOKEN = process.env.ERP_TOKEN;

const C = { ok: "\x1b[32m", bad: "\x1b[31m", warn: "\x1b[33m", dim: "\x1b[2m", off: "\x1b[0m" };
const ok = (m) => console.log(`${C.ok}✅${C.off} ${m}`);
const bad = (m) => console.log(`${C.bad}❌${C.off} ${m}`);
const warn = (m) => console.log(`${C.warn}⚠️ ${C.off} ${m}`);
const dim = (m) => console.log(`${C.dim}   ${m}${C.off}`);

/** 혼합 봉투 signers[] — 1=STAFF(직원 SSO·certSerial HIS 내부주입), 2·3=PORTAL(deep-link 발송 D3). */
function mixedSigners() {
  return [
    { displayName: "주치의(STAFF)", channel: "STAFF", signOrder: 1,
      signerType: "STAFF", requiredIdentityLevel: "STAFF_SSO",
      externalSignerRef: `erp:staff:${STAFF_REF}` },
    { displayName: "병원장(PORTAL)", channel: "PORTAL", signOrder: 2,
      externalSignerRef: "erp:cosigner:1" },
    { displayName: "거래처대표(PORTAL)", channel: "PORTAL", signOrder: 3,
      externalSignerRef: "erp:vendor:1" },
  ];
}

const isStaff = (s) =>
  (s.channel || "").toUpperCase() === "STAFF" || (s.signerType || "").toUpperCase() === "STAFF";

/** EP 응답(initiate_medical_contract_signing 출력)에 대한 ERP 소유 relay 불변식 검증.
 * 위반 시 throw. relayedPortal 수(HIS §6 반영분)를 반환해 호출부가 soft-report 한다. */
function assertRelayInvariants(out, requested) {
  if (out.status !== "SENT") throw new Error(`status=SENT 기대, 실제=${out.status}`);
  if (!out.signature_request_id) throw new Error("signature_request_id 비어있음");

  const links = out.portal_links;
  if (!Array.isArray(links)) throw new Error("portal_links 배열 아님");

  const reqStaffRefs = new Set(requested.filter(isStaff).map((s) => s.externalSignerRef));
  const reqPortalOrders = requested.filter((s) => !isStaff(s)).map((s) => s.signOrder);

  let prevOrder = -Infinity;
  for (const l of links) {
    if (!l.portal_url) throw new Error(`portal_links에 portal_url 없는 항목: ${JSON.stringify(l)}`);
    // STAFF(SSO)는 deep-link가 없어야 함 — 노출 시 신원경계 위반.
    if ((l.channel || "").toUpperCase() === "STAFF" || reqStaffRefs.has(l.external_signer_ref))
      throw new Error(`STAFF 서명자가 portal_links에 노출됨: ${JSON.stringify(l)}`);
    // 순차(signOrder 증가) — sign 순차 게이팅과 정합.
    if (l.sign_order != null) {
      if (l.sign_order <= prevOrder) throw new Error(`portal_links sign_order 비증가: ${l.sign_order}`);
      prevOrder = l.sign_order;
      if (!reqPortalOrders.includes(l.sign_order))
        throw new Error(`요청 PORTAL signOrder(${reqPortalOrders})에 없는 link order=${l.sign_order}`);
    }
  }
  return { relayedPortal: links.length, requestedPortal: reqPortalOrders.length };
}

// ─────────────────────────────────────────────── self-check (네트워크 無)
function selfCheck() {
  console.log("self-check — relay 불변식 검증기 자체 검증(네트워크 無)\n");
  const requested = mixedSigners();
  let pass = 0, fail = 0;
  const expectPass = (name, out, extra) => {
    try { const r = assertRelayInvariants(out, requested); ok(`${name} — relayed ${r.relayedPortal}/${r.requestedPortal} PORTAL`); pass++; if (extra) extra(r); }
    catch (e) { bad(`${name} — 통과 기대했으나 throw: ${e.message}`); fail++; }
  };
  const expectThrow = (name, out, match) => {
    try { assertRelayInvariants(out, requested); bad(`${name} — throw 기대했으나 통과`); fail++; }
    catch (e) { if (match && !e.message.includes(match)) { bad(`${name} — 메시지 불일치: ${e.message}`); fail++; }
                else { ok(`${name} — 거부: ${e.message}`); pass++; } }
  };

  // (정상) HIS §6 relay 반영 — PORTAL 2건만 link, STAFF 제외, 순차.
  expectPass("HIS §6 relay 반영(정상)", {
    status: "SENT", signature_request_id: "SR-OK",
    portal_links: [
      { sign_order: 2, channel: "PORTAL", display_name: "병원장", external_signer_ref: "erp:cosigner:1", portal_url: "https://sign.example.org/portal/t2" },
      { sign_order: 3, channel: "PORTAL", display_name: "거래처대표", external_signer_ref: "erp:vendor:1", portal_url: "https://sign.example.org/portal/t3" },
    ],
  });
  // (정상) HIS §6 미반영 — portal_links 빈 배열(HIS 대기, 비파괴). 불변식은 통과, soft-report로 분기.
  expectPass("HIS §6 미반영(빈 links 허용)", { status: "SENT", signature_request_id: "SR-OK2", portal_links: [] });

  // (거부) STAFF가 deep-link 노출 — 신원경계 위반.
  expectThrow("STAFF deep-link 노출", {
    status: "SENT", signature_request_id: "SR-X",
    portal_links: [{ sign_order: 1, channel: "STAFF", external_signer_ref: `erp:staff:${STAFF_REF}`, portal_url: "https://x/staff" }],
  }, "STAFF");
  // (거부) status SENT 아님.
  expectThrow("status≠SENT", { status: "ERROR", signature_request_id: "SR-X", portal_links: [] }, "status");
  // (거부) portal_url 없는 link.
  expectThrow("portal_url 누락 link", { status: "SENT", signature_request_id: "SR-X", portal_links: [{ sign_order: 2, channel: "PORTAL" }] }, "portal_url");
  // (거부) signOrder 비증가(순차 위반).
  expectThrow("signOrder 비증가", {
    status: "SENT", signature_request_id: "SR-X",
    portal_links: [
      { sign_order: 3, channel: "PORTAL", external_signer_ref: "erp:vendor:1", portal_url: "https://x/3" },
      { sign_order: 2, channel: "PORTAL", external_signer_ref: "erp:cosigner:1", portal_url: "https://x/2" },
    ],
  }, "비증가");

  console.log(`\n${fail ? C.bad : C.ok}${pass} pass · ${fail} fail${C.off}`);
  process.exit(fail ? 1 : 0);
}

// ─────────────────────────────────────────────── live (실 HIS relay)
async function api(method, path, body) {
  const r = await fetch(BASE + path, {
    method,
    headers: { "Content-Type": "application/json", Cookie: `access_token=${TOKEN}; csrf_token=e2e`, "X-CSRF-Token": "e2e" },
    body: body ? JSON.stringify(body) : undefined,
  });
  const text = await r.text();
  let json; try { json = JSON.parse(text); } catch { json = null; }
  if (!r.ok) throw new Error(`${method} ${path} → ${r.status} ${text.slice(0, 300)}`);
  return json;
}

async function live() {
  if (!TOKEN) { bad("ERP_TOKEN 필요 (또는 --self-check)"); process.exit(2); }
  if (!CONTRACT) { bad("--contract <id> 필요"); process.exit(2); }
  console.log(`live relay E2E — base=${BASE} contract=${CONTRACT} staff-ref=${STAFF_REF}${FORCE ? " (force)" : ""}\n`);

  const requested = mixedSigners();
  dim(`signers: ${requested.map((s) => `#${s.signOrder} ${s.channel}`).join(" · ")}`);

  // 1) 발의 — 혼합 봉투(채널 STAFF→DIRECT 매핑은 백엔드 _to_his_channel 소관).
  let out;
  try {
    out = await api("POST", `/api/v1/integrations/his-sign/medical-contracts/${CONTRACT}/request`,
                    { channel: "STAFF", signers: requested, force: FORCE });
  } catch (e) { bad(`발의 실패: ${e.message}`); process.exit(1); }
  ok(`발의 OK — issue_no=${out.issue_no} req_id=${out.signature_request_id} channel=${out.channel}`);
  if (out.already_sent) warn("already_sent=true (기존 SENT 봉투 반환) — 새 봉투 필요 시 --force");

  // 2) ERP 소유 불변식 검증.
  let rep;
  try { rep = assertRelayInvariants(out, requested); ok("relay 불변식 통과 (status·STAFF 비노출·순차)"); }
  catch (e) { bad(`relay 불변식 위반: ${e.message}`); dim(JSON.stringify(out.portal_links)); process.exit(1); }

  // 3) HIS §6 per-signer portalUrl relay soft-report.
  if (rep.relayedPortal === rep.requestedPortal && rep.requestedPortal > 0)
    ok(`HIS §6 relay 반영 — PORTAL ${rep.relayedPortal}건 deep-link 수신`);
  else if (rep.relayedPortal === 0)
    warn(`HIS §6 relay 미반영 — portal_links 0건(HIS 대기). 단일 portal_url=${out.portal_url ?? "없음"} 폴백. 비파괴.`);
  else
    warn(`HIS §6 부분 relay — ${rep.relayedPortal}/${rep.requestedPortal} PORTAL만 deep-link. HIS 확인 필요.`);
  for (const l of out.portal_links || []) dim(`#${l.sign_order} ${l.display_name}: ${l.portal_url}`);

  // 4) (선택) 실서명 후 미러 COMPLETED·완료본 회수 관찰.
  if (POLL) {
    if (!EMPLOYEE) { warn("--poll엔 --employee <계약직원 id> 필요 — 미러 조회 생략"); }
    else await pollMirror();
  }
  console.log(`\n${C.ok}트랙1 다자 발의·relay 검증 PASS${C.off}${POLL ? "" : "  (실서명→COMPLETED는 --poll 또는 런북 §2 SQL로 관찰)"}`);
}

async function pollMirror() {
  console.log("\n미러 관찰 — 실서명(PORTAL deep-link + STAFF SSO) 후 COMPLETED 대기 (최대 10분, 15초 간격)");
  const deadline = Date.now() + 10 * 60 * 1000;
  let last = null;
  while (Date.now() < deadline) {
    let res; try { res = await api("GET", `/api/v1/hr/medical-contracts?employee_id=${EMPLOYEE}`); }
    catch (e) { warn(`조회 실패: ${e.message}`); break; }
    const c = (res.items || []).find((x) => String(x.id) === String(CONTRACT));
    if (!c) { warn(`계약 ${CONTRACT} 미발견(employee_id=${EMPLOYEE} 확인)`); break; }
    if (c.sign_status !== last) { dim(`sign_status=${c.sign_status}`); last = c.sign_status; }
    if (c.sign_status === "COMPLETED") {
      ok(`COMPLETED — signed_at=${c.signed_at} cert=${c.sign_cert_serial} attachment=${c.sign_doc_attachment_id}`);
      if (!c.sign_doc_attachment_id) warn("완료본 Attachment 미회수 — sign.completed 회수 경로 확인");
      return;
    }
    await new Promise((r) => setTimeout(r, 15000));
  }
  warn("타임아웃/중단 — COMPLETED 미관찰. reconcile: sign GET /v1/requests/<id>/webhooks");
}

// ─────────────────────────────────────────────── entry
if (has("--self-check") || (!TOKEN && !CONTRACT)) selfCheck();
else await live();

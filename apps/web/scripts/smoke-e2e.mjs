/** 운영 헤드리스 스모크 — 신규/핵심 화면 로드+콘솔/HTTP 오류 캡처.
 *
 * 사용: ERP_TOKEN=<access_token> node scripts/smoke-e2e.mjs [base]
 * 토큰 발급(운영 컨테이너): python /tmp/mktoken.py (security.create_access_token)
 */
import { chromium } from "playwright";

const BASE = process.argv[2] ?? "https://his.example.org";
const TOKEN = process.env.ERP_TOKEN;
if (!TOKEN) { console.error("ERP_TOKEN 필요"); process.exit(1); }
const HOST = new URL(BASE).hostname;
const PAGES = [
  "/erp/dashboard", "/erp/claims/pre-review", "/erp/hr/onboarding", "/erp/hr/absences",
  "/erp/hr/insurance-reports", "/erp/hr/compliance", "/erp/finance-tax-invoices",
  "/erp/regulatory/nursing-grade", "/erp/inventory-contracts", "/erp/tools/spreadsheet",
  "/erp/inventory",
];

const browser = await chromium.launch();
const ctx = await browser.newContext({ ignoreHTTPSErrors: true });
await ctx.addCookies([
  { name: "access_token", value: TOKEN, domain: HOST, path: "/", secure: true, httpOnly: true },
  { name: "csrf_token", value: "smoke", domain: HOST, path: "/", secure: true },
]);

let failed = 0;
for (const path of PAGES) {
  const page = await ctx.newPage();
  const errs = [];
  page.on("console", (m) => m.type() === "error" && errs.push("console: " + m.text().slice(0, 200)));
  page.on("pageerror", (e) => errs.push("pageerror: " + String(e).slice(0, 200)));
  page.on("response", (r) => { if (r.status() >= 400)
    errs.push("http " + r.status() + " " + r.url().replace(BASE, "").slice(0, 100)); });
  try {
    await page.goto(BASE + path, { waitUntil: "networkidle", timeout: 40000 });
    await page.waitForTimeout(1200);
  } catch (e) { errs.push("NAV FAIL: " + String(e).slice(0, 150)); }
  if (errs.length) failed++;
  console.log((errs.length ? "❌" : "✅") + " " + path
    + (errs.length ? "\n   " + errs.join("\n   ") : ""));
  await page.close();
}
await browser.close();
process.exit(failed ? 1 : 0);

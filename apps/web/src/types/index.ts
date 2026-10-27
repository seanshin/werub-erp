/** 도메인 타입 중앙화 (W3 — 검토 개선-2: 페이지별 재선언·드리프트 방지). */

// common
export interface Notification {
  id: number; type: string; title: string; body: string | null;
  href: string | null; read_at: string | null; created_at: string;
}

// hr
export interface Employee {
  id: number; employee_no: string; name: string; department_id: number | null;
  position: string | null; job_group: string; hired_on: string; phone: string | null;
  source?: "HIS" | "ERP"; staff_class?: "MEDICAL" | "SUPPORT" | null;
  job_category?: string | null; employment_type?: string;
}

// 직원 최소 참조(목록·검색용 공통 투영)
export interface EmployeeRef { id: number; employee_no: string; name: string }

export interface LeaveRequestRow {
  id: number; employee_name: string; department_id: number | null;
  leave_type: string; start_on: string; end_on: string; days: number;
  status: string; reason: string | null;
}

// claims
export interface AppealRow {
  id: number; status: string; legal_deadline: string; d_day: number;
  // 모델엔 있는데 응답이 4필드만 내보내 기한 기산일·유형·확정손실·전표를 볼 수 없었다
  appeal_type: string; notice_arrived_on: string; adjustment_id: number;
  final_loss_amt: number | null; voucher_ref: string | null; alert_stage: number | null;
}

export interface ClaimStatRow { dim: string; count: number; amount: number }

// scm
export interface Item {
  id: number; code: string; name: string; is_narcotic: boolean;
  stock: number; safety_stock: number;
}

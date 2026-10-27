# Hospital-ERP PDCA Changelog

All notable changes to hospital-erp project are documented here, organized by PDCA cycle completion.

---

## [2026-06-07] - Rendering Engine Completion (Phase 1, Feature 1)

### Added
- Excel export module with openpyxl write_only mode (5000-row sync checkpoint)
- PDF rendering engine (WeasyPrint) with secure url_fetcher (allow-list only)
- Certificate and payslip PDF templates with Jinja2 autoescape enabled
- Comprehensive test suite: 8 tests covering rendering engines, security, RBAC
- Audit logging infrastructure for EXPORT and PRINT actions
- Department-level RBAC row filtering in export orchestration

### Changed
- Updated hospital-erp.design.md §11.0 with payslip endpoint specification
- Clarified openpyxl write_only status in design document (line 345)

### Fixed
- SSRF/LFI prevention: URL fetcher now uses allow-list strategy (templates/, assets/ only)
- XSS injection prevention: Jinja2 autoescape enabled on all PDF templates
- Memory efficiency: write_only mode for large Excel exports (100K+ rows ready)

### Deferred to Operations Phase
- Async export infrastructure for >5000 rows (Celery/Redis framework)
- Async download endpoint with IDOR protection and 24-hour expiry
- Real hospital data performance profiling (target: 500ms for <1000 rows)

**PDCA Match Rate**: 96% ✅ (exceeds 90% gate)  
**Test Coverage**: 100% of public APIs  
**Status**: Ready for production handoff to operations  
**Completion Report**: [hospital-erp-rendering-engine.report.md](hospital-erp-rendering-engine.report.md)

---

## Upcoming Milestones

- **2026-07-01**: Operations phase entry — async export infrastructure
- **2026-08-01**: Phase 2 feature planning

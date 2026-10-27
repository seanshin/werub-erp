"""근거검색 다수결 안정화 — 위험도 다수결·보수적 동률·근거 과반 보류·부분실패 degraded."""
from erp.regulatory.router import _SAFE_REP_KEYS, _aggregate_diagnoses


def _r(risk, *, cites=1, score=0.0, **extra):
    return {"has_evidence": cites > 0, "risk_level": risk, "risk_score": score,
            "recommendation": "적정" if risk == "LOW" else "재검토",
            "evidence_citations": [{"title": f"고시{i}"} for i in range(cites)], **extra}


def test_empty_returns_error():
    out = _aggregate_diagnoses([], 3)
    assert out["available"] is False and out["attempted"] == 3 and out["samples"] == 0


def test_majority_risk_level():
    out = _aggregate_diagnoses([_r("LOW"), _r("LOW"), _r("HIGH")], 3)
    assert out["held"] is False and out["risk_level"] == "LOW"
    assert out["risk_votes"] == {"LOW": 2, "HIGH": 1} and out["samples"] == 3
    assert out["attempted"] == 3 and out["degraded"] is False


def test_tie_breaks_conservative_high():
    # 동률(1:1) → 보수적으로 높은 위험(HIGH) 채택 — 삭감 과소표시 방지
    out = _aggregate_diagnoses([_r("LOW"), _r("HIGH")], 2)
    assert out["risk_level"] == "HIGH"


def test_held_only_when_majority_no_evidence():
    # 3표본 중 2건 근거 없음(과반) → 보류
    assert _aggregate_diagnoses([_r("LOW"), _r("LOW", cites=0), _r("HIGH", cites=0)], 3)["held"] is True
    # 1건만 근거 없음(소수) → 폐기, 근거 표본으로 집계
    out = _aggregate_diagnoses([_r("LOW"), _r("LOW"), _r("HIGH", cites=0)], 3)
    assert out["held"] is False and out["risk_level"] == "LOW"
    assert out["evidence_samples"] == 2     # votes 모집단은 근거 보유 표본


def test_score_averaged_over_mode_and_rep_richest():
    # 대표 표본 = 인용 가장 많은 것(세부내역 풍부), score는 mode 평균
    out = _aggregate_diagnoses([_r("HIGH", cites=1, score=0.8), _r("HIGH", cites=3, score=0.6)], 2)
    assert out["risk_level"] == "HIGH" and out["risk_score"] == 0.7
    assert len(out["evidence_citations"]) == 3   # 인용 풍부한 대표 채택


def test_degraded_when_partial_failure():
    # 3회 시도 중 1건만 성공 → 다수결 신뢰 저하(degraded), 사실은 비가시화하지 않음
    out = _aggregate_diagnoses([_r("HIGH")], 3)
    assert out["degraded"] is True and out["samples"] == 1 and out["attempted"] == 3
    # 2/3 성공은 과반 → degraded 아님
    assert _aggregate_diagnoses([_r("LOW"), _r("LOW")], 3)["degraded"] is False


def test_has_evidence_flag_overrides_citations():
    # 모순 응답(has_evidence=False인데 인용 존재) → 명시 플래그를 신뢰해 '근거 없음' 처리.
    # 3표본 중 2건이 플래그 False면 인용이 있어도 과반 보류.
    out = _aggregate_diagnoses(
        [_r("LOW"), _r("HIGH", cites=2, has_evidence=False), _r("HIGH", cites=2, has_evidence=False)], 3)
    assert out["held"] is True


def test_rep_fields_allowlisted_no_passthrough():
    # weve 원본의 내부/디버그 필드는 응답에 새지 않음(allowlist만 통과)
    out = _aggregate_diagnoses([_r("LOW", _debug="x", internal_url="http://weve", has_evidence=True)], 1)
    assert "_debug" not in out and "internal_url" not in out and "has_evidence" not in out
    assert set(out) <= {"available", "held", "risk_level", "risk_score", "attempted",
                        "samples", "evidence_samples", "risk_votes", "degraded", *_SAFE_REP_KEYS}

"""ERP→HIS 수가/재료 push 클라이언트 — SSRF·data 봉투·헤더."""
import httpx
import pytest

from erp.integrations.his_fee_push import HISFeePushClient


def test_ssrf_blocks_non_his():
    with pytest.raises(ValueError, match="허용되지"):
        HISFeePushClient("https://evil.example.com", "k")


def test_push_unwraps_data_and_auth():
    seen = {}

    def handler(request):
        seen["key"] = request.headers.get("X-Integration-Key")
        seen["path"] = request.url.path
        return httpx.Response(200, json={"data": {"total": 2, "created": 1, "updated": 1, "failed": 0}})

    client = HISFeePushClient("https://his.example.org", "hiserp_x",
                              transport=httpx.MockTransport(handler))
    out = client.push_procedure_codes([{"code": "A", "nameKo": "n", "unitPrice": 100,
                                        "validFrom": "2026-06-01"}])
    assert out["created"] == 1 and out["updated"] == 1
    assert seen["key"] == "hiserp_x"
    assert seen["path"] == "/api/v1/integration/fee/procedure-codes"

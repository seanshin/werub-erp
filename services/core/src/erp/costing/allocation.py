"""ABC 원가 배부엔진 (FR-42/50 정밀화) — 원가풀을 원가동인으로 원가대상(진료과)에 배부.

순수 배부 로직(DB 비의존) — 서비스가 원가풀 금액·동인값을 주입. 동인 정의·값은 원가팀
협의(D42)로 확정하되, 엔진은 무수정. 인건비=인원/시간, 재료=사용량, 간접비=면적/건수 등.
"""


class AllocationEngine:
    def allocate(self, *, pools: list[dict], driver_values: dict) -> dict:
        """pools=[{name, amount, driver}], driver_values={driver: {obj_id: weight}}.

        각 풀을 해당 동인의 가중치 비율로 원가대상에 배부 → 대상별 풀별 배부액·합계."""
        by_obj: dict = {}
        for pool in pools:
            dv = driver_values.get(pool["driver"], {})
            total_w = sum(dv.values())
            if total_w <= 0:
                continue
            for obj, w in dv.items():
                share = float(pool["amount"]) * (w / total_w)
                slot = by_obj.setdefault(obj, {"pools": {}, "total": 0.0})
                slot["pools"][pool["name"]] = slot["pools"].get(pool["name"], 0.0) + share
                slot["total"] += share
        objects = [{"object_id": obj, "pools": {k: round(v) for k, v in d["pools"].items()},
                    "total": round(d["total"])}
                   for obj, d in sorted(by_obj.items(), key=lambda x: -x[1]["total"])]
        return {"objects": objects, "grand_total": round(sum(o["total"] for o in objects)),
                "pools": [{"name": p["name"], "amount": float(p["amount"]),
                           "driver": p["driver"]} for p in pools]}

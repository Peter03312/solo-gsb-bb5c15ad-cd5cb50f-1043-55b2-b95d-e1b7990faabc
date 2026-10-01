"""清洁更正预演（只读）测试。

覆盖：
- 预演逐批推演：残留/实际过敏原/未声明项/RELEASE|HOLD 的当前与预演结论及变化原因；
- 从拟更正批起后续有效批次即使结论相同仍属于版本影响范围（版本 +1）；
- 已确认核验单：将因裁决版本追加而转待复核（草稿/待复核不列、结论相同也列）；
- 跳过作废批次：作废批退出残留链，其前有效批残留会继续传递；
- 待检锁定不参与过敏原推演、不预测资格版本递增；
- 只读性：预演不更改清洁记录、裁决/资格版本、核验单状态或历史；
- 预演结论与随后正式更正落库结果一致；
- 拒绝：未知批次 404、已作废批次 409、非法过敏原 400；
- 多产线隔离。
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.main import create_app


@pytest.fixture()
def client(tmp_path):
    app = create_app(db_path=str(tmp_path / "test.db"), seed_demo=False)
    with TestClient(app) as c:
        yield c


def add_lot(client, lot_no, material="原料", allergens=None):
    resp = client.post(
        "/api/material-lots",
        json={"lot_no": lot_no, "material": material, "allergens": allergens or []},
    )
    assert resp.status_code == 201, resp.text
    return resp.json()


def add_batch(client, batch_no, line_id="L1", lots=None, declared=None, cleaned=None):
    resp = client.post(
        "/api/batches",
        json={
            "batch_no": batch_no,
            "line_id": line_id,
            "material_lots": lots or [],
            "declared_allergens": declared or [],
            "cleaned_allergens": cleaned or [],
        },
    )
    assert resp.status_code == 201, resp.text
    return resp.json()


def preview(client, batch_no, cleaned):
    return client.post(
        f"/api/batches/{batch_no}/cleaning/preview",
        json={"cleaned_allergens": cleaned},
    )


def by_no(batches):
    return {b["batch_no"]: b for b in batches}


def setup_line(client):
    """L1：B1(peanut 声明 peanut，RELEASE，残留 peanut) → B2(无料无清洁，HOLD 继承 peanut)
    → B3(无料无清洁，HOLD 继续继承 peanut)。"""
    add_lot(client, "RM-P", "花生酱", ["peanut"])
    add_batch(client, "B1", lots=["RM-P"], declared=["peanut"])
    add_batch(client, "B2", lots=[], declared=[])
    add_batch(client, "B3", lots=[], declared=[])


# ---------------------------------------------------------------- 基本推演


def test_preview_simulates_hold_to_release_chain(client):
    setup_line(client)
    resp = preview(client, "B2", ["peanut"])
    assert resp.status_code == 200, resp.text
    body = resp.json()

    # 元数据：只读预演、从 B2(seq=2) 起、同线 L1
    assert body["simulation"] == "CLEANING_CORRECTION_PREVIEW"
    assert body["read_only"] is True
    assert body["line_id"] == "L1"
    assert body["start_seq"] == 2
    assert body["correction_target"] == {
        "batch_no": "B2",
        "line_id": "L1",
        "seq": 2,
        "current_cleaned_allergens": [],
        "proposed_cleaned_allergens": ["peanut"],
    }

    entries = by_no(body["batches"])
    # B1 在更正点之前，不纳入版本影响范围
    assert "B1" not in entries

    b2 = entries["B2"]
    assert b2["is_correction_target"] is True
    assert b2["current"]["disposition"] == "HOLD"
    assert b2["current"]["carried_in_allergens"] == ["peanut"]
    assert b2["current"]["residual_allergens"] == ["peanut"]
    assert b2["preview"]["cleaned_allergens"] == ["peanut"]
    assert b2["preview"]["residual_allergens"] == []
    assert b2["preview"]["actual_allergens"] == []
    assert b2["preview"]["undeclared_allergens"] == []
    assert b2["preview"]["disposition"] == "RELEASE"
    assert b2["preview"]["version"] == b2["current"]["version"] + 1
    assert b2["changed"] is True
    assert {r["code"] for r in b2["change_reasons"]} >= {
        "CLEANING_SET_CHANGED",
        "RESIDUAL_CHANGED",
        "ACTUAL_ALLERGENS_CHANGED",
        "UNDECLARED_ALLERGENS_CHANGED",
        "DISPOSITION_CHANGED",
    }

    # B3 连带放行：清洁更正点之后批次同样推演
    b3 = entries["B3"]
    assert b3["is_correction_target"] is False
    assert b3["current"]["disposition"] == "HOLD"
    assert b3["preview"]["carried_in_allergens"] == []
    assert b3["preview"]["disposition"] == "RELEASE"
    assert b3["changed"] is True


def test_preview_identical_conclusion_still_in_version_scope(client):
    setup_line(client)
    # 把 B2 清洁更正为 []（与当前相同）：裁决内容不变，但正式更正仍会追加版本
    resp = preview(client, "B2", [])
    assert resp.status_code == 200
    entries = by_no(resp.json()["batches"])
    assert set(entries) == {"B2", "B3"}
    for bn, entry in entries.items():
        assert entry["in_version_scope"] is True
        assert entry["changed"] is False
        assert entry["preview"]["version"] == entry["current"]["version"] + 1
        assert entry["preview"]["disposition"] == entry["current"]["disposition"] == "HOLD"
        codes = [r["code"] for r in entry["change_reasons"]]
        assert codes == ["NO_ADJUDICATION_CONTENT_CHANGE"]
    # 更正目标批本身清洁集合也未变
    assert entries["B2"]["change_reasons"][0]["code"] == "NO_ADJUDICATION_CONTENT_CHANGE"


def test_preview_adding_cleaning_can_hold_later_batches(client):
    # 反向场景：当前 B2 清洁了 peanut（链上 RELEASE）；预演改为不清洁 -> B2/B3 HOLD
    add_lot(client, "RM-P", "花生酱", ["peanut"])
    add_batch(client, "B1", lots=["RM-P"], declared=["peanut"])
    add_batch(client, "B2", lots=[], declared=[], cleaned=["peanut"])
    add_batch(client, "B3", lots=[], declared=[])
    resp = preview(client, "B2", [])
    entries = by_no(resp.json()["batches"])
    assert entries["B2"]["current"]["disposition"] == "RELEASE"
    assert entries["B2"]["preview"]["residual_allergens"] == ["peanut"]
    assert entries["B2"]["preview"]["disposition"] == "HOLD"
    assert entries["B3"]["preview"]["disposition"] == "HOLD"


def test_preview_cleans_extra_but_materials_reintroduce(client):
    # 清洁清掉残留，但本批原料再次引入的过敏原仍计入实际
    add_lot(client, "RM-M", "奶粉", ["milk"])
    add_batch(client, "B1", lots=["RM-M"], declared=["milk"])  # 残留 milk
    add_batch(client, "B2", lots=["RM-M"], declared=[])        # 原料再次引入 milk
    resp = preview(client, "B2", ["milk"])
    entry = resp.json()["batches"][0]
    assert entry["preview"]["residual_allergens"] == []
    assert entry["preview"]["from_material_allergens"] == ["milk"]
    assert entry["preview"]["actual_allergens"] == ["milk"]
    assert entry["preview"]["disposition"] == "HOLD"


def test_preview_dedupes_allergen_codes(client):
    setup_line(client)
    resp = preview(client, "B2", ["peanut", "peanut"])
    assert resp.status_code == 200
    assert resp.json()["correction_target"]["proposed_cleaned_allergens"] == ["peanut"]


# ---------------------------------------------------------------- 已确认核验单


def test_preview_confirmed_verification_flips_even_when_disposition_same(client):
    setup_line(client)
    # 核验单 [B3]：当前 B3 HOLD 不能确认；先清洁 B2 使其全 RELEASE 再确认
    client.put("/api/batches/B2/cleaning", json={"cleaned_allergens": ["peanut"]})
    create = client.post("/api/verifications", json={"verification_no": "V1", "batch_nos": ["B3"]})
    assert create.status_code == 201
    assert client.post("/api/verifications/V1/confirm").status_code == 200

    # 预演：把 B2 清洁改为 []（与现在的 peanut 不同），B3 预演为 HOLD；V1 必转待复核
    resp = preview(client, "B2", [])
    affected = {v["verification_no"]: v for v in resp.json()["affected_verifications"]}
    assert "V1" in affected
    v1 = affected["V1"]
    assert v1["current_status"] == "CONFIRMED"
    assert v1["status_if_applied"] == "PENDING_REVIEW"
    assert v1["batches"][0]["batch_no"] == "B3"
    assert v1["batches"][0]["preview_version"] == v1["batches"][0]["current_version"] + 1
    assert v1["batches"][0]["preview_disposition"] == "HOLD"
    assert v1["batches"][0]["disposition_changed"] is True

    # 结论不变的预演（清洁集合不变）也必须列出已确认单据
    resp2 = preview(client, "B2", ["peanut"])
    affected2 = {v["verification_no"]: v for v in resp2.json()["affected_verifications"]}
    assert "V1" in affected2
    assert affected2["V1"]["batches"][0]["disposition_changed"] is False


def test_preview_draft_and_pending_review_verifications_not_listed(client):
    setup_line(client)
    client.put("/api/batches/B2/cleaning", json={"cleaned_allergens": ["peanut"]})
    client.post("/api/verifications", json={"verification_no": "DRAFT1", "batch_nos": ["B3"]})
    # 待复核单：先确认再制造版本漂移
    client.post("/api/verifications", json={"verification_no": "PR1", "batch_nos": ["B3"]})
    client.post("/api/verifications/PR1/confirm")
    client.put("/api/batches/B3/cleaning", json={"cleaned_allergens": []})
    statuses = {
        v["verification_no"]: v["status"]
        for v in client.get("/api/verifications").json()["verifications"]
    }
    assert statuses["PR1"] == "PENDING_REVIEW"

    resp = preview(client, "B2", ["peanut"])
    listed = {v["verification_no"] for v in resp.json()["affected_verifications"]}
    assert listed == set()  # 草稿/待复核均不列入“将转待复核”的已确认单据


def test_preview_verification_batches_only_in_scope(client):
    setup_line(client)
    client.put("/api/batches/B2/cleaning", json={"cleaned_allergens": ["peanut"]})
    # V1 关联更正点之前的 B1 与之后的 B3：正式更正只重算 B2/B3，V1 因 B3 仍须转待复核
    client.post("/api/verifications", json={"verification_no": "V1", "batch_nos": ["B1", "B3"]})
    client.post("/api/verifications/V1/confirm")
    resp = preview(client, "B2", ["peanut"])
    v1 = {v["verification_no"]: v for v in resp.json()["affected_verifications"]}["V1"]
    assert [b["batch_no"] for b in v1["batches"]] == ["B3"]  # 按单据内顺序，只列影响范围内批次


def test_preview_verification_on_unaffected_line_not_listed(client):
    setup_line(client)
    client.put("/api/batches/B2/cleaning", json={"cleaned_allergens": ["peanut"]})
    add_batch(client, "X1", line_id="L2", lots=[], declared=[])
    client.post("/api/verifications", json={"verification_no": "V2", "batch_nos": ["X1"]})
    client.post("/api/verifications/V2/confirm")
    resp = preview(client, "B2", ["peanut"])
    listed = {v["verification_no"] for v in resp.json()["affected_verifications"]}
    assert "V2" not in listed


# ---------------------------------------------------------------- 作废批次


def test_preview_skips_voided_batches_in_chain(client):
    add_lot(client, "RM-P", "花生酱", ["peanut"])
    add_batch(client, "B1", lots=["RM-P"], declared=["peanut"])  # 残留 peanut
    add_batch(client, "B2", lots=[], declared=[])                # 继承 peanut -> HOLD
    add_batch(client, "B3", lots=[], declared=[], cleaned=["peanut"])  # 清洁 peanut -> RELEASE
    add_batch(client, "B4", lots=[], declared=[])                # 当前 RELEASE
    # B3 作废：其清洁视为未发生 -> B4 重算后从 B2 继承 peanut（HOLD）
    client.post("/api/batches/B3/void", json={"reason": "误登记"})
    assert client.get("/api/batches/B4").json()["adjudication"]["disposition"] == "HOLD"

    # 预演更正 B2 的清洁为 [peanut]：从 B2 起逐批推演，跳过已作废的 B3，
    # B4 的上一有效批变为已清洁干净的 B2 -> RELEASE
    resp = preview(client, "B2", ["peanut"])
    assert resp.status_code == 200
    body = resp.json()
    entries = by_no(body["batches"])
    assert "B3" not in entries
    assert set(entries) == {"B2", "B4"}
    assert entries["B4"]["current"]["carried_in_allergens"] == ["peanut"]
    assert entries["B4"]["current"]["disposition"] == "HOLD"
    assert entries["B4"]["preview"]["carried_in_allergens"] == []
    assert entries["B4"]["preview"]["disposition"] == "RELEASE"

    skipped = {s["batch_no"]: s for s in body["skipped_voided_batches"]}
    assert set(skipped) == {"B3"}
    assert skipped["B3"]["status"] == "VOIDED"
    assert skipped["B3"]["voided_reason"] == "误登记"
    assert "跳过" in skipped["B3"]["reason"]


def test_preview_voided_target_rejected(client):
    setup_line(client)
    client.post("/api/batches/B2/void", json={"reason": "误登记"})
    resp = preview(client, "B2", ["peanut"])
    assert resp.status_code == 409
    assert resp.json()["error"]["code"] == "BATCH_VOIDED"


def test_preview_voided_before_target_not_listed_but_chain_bypasses_it(client):
    add_lot(client, "RM-P", "花生酱", ["peanut"])
    add_batch(client, "B1", lots=["RM-P"], declared=["peanut"])
    # B2 是“清洁 peanut”的有效批，先被作废（其清洁视为未发生）
    add_batch(client, "B2", lots=[], declared=[], cleaned=["peanut"])
    client.post("/api/batches/B2/void", json={"reason": "误登记"})
    add_batch(client, "B3", lots=[], declared=[])  # 绕过 B2 继承 B1 的 peanut -> HOLD

    # 从 B3 起预演清洁 peanut：B2 在更正点之前，不列入 skipped，但推演链必须跳过它
    resp = preview(client, "B3", ["peanut"])
    assert resp.status_code == 200
    body = resp.json()
    assert body["skipped_voided_batches"] == []
    entry = body["batches"][0]
    assert entry["batch_no"] == "B3"
    assert entry["current"]["carried_in_allergens"] == ["peanut"]
    assert entry["preview"]["residual_allergens"] == []
    assert entry["preview"]["disposition"] == "RELEASE"


def test_preview_locked_direct_lot_eligibility_echoed(client):
    add_lot(client, "RM-P", "花生酱", ["peanut"])
    add_batch(client, "B1", lots=["RM-P"], declared=["peanut"])
    client.post("/api/material-lots/RM-P/lock", json={"reason": "待检风险"})
    resp = preview(client, "B1", ["peanut"])
    assert resp.status_code == 200
    entry = resp.json()["batches"][0]
    ship = entry["shipment_eligibility"]
    # 直接使用锁定批号：当前资格 BLOCKED 被原样回显；但清洁更正不改变直接用料，不预测资格递增
    assert ship["would_advance_if_applied"] is False
    assert ship["current"]["status"] == "BLOCKED"
    assert ship["current"]["locked_lots"] == ["RM-P"]
    # 过敏原推演与锁定无关：本批原料引入 peanut（已声明），清洁 peanut 也不影响原料再引入
    assert entry["preview"]["actual_allergens"] == ["peanut"]
    assert entry["preview"]["disposition"] == "RELEASE"


# ---------------------------------------------------------------- 待检锁定


def test_preview_locked_lot_not_part_of_allergen_simulation(client):
    setup_line(client)
    # 锁定不改变裁决：B2/B3 仍 HOLD（peanut）
    client.post("/api/material-lots/RM-P/lock", json={"reason": "待检风险"})
    adj_before = client.get("/api/batches/B2").json()["adjudication"]
    resp = preview(client, "B2", ["peanut"])
    entry = by_no(resp.json()["batches"])["B2"]
    # 过敏原推演不受锁定影响：清洁 peanut 后 RELEASE
    assert entry["preview"]["disposition"] == "RELEASE"
    # 出货资格现状被回显，但预演明确不递增资格版本
    ship = entry["shipment_eligibility"]
    assert ship["would_advance_if_applied"] is False
    assert ship["current"]["version"] == client.get(
        "/api/batches/B2/eligibility"
    ).json()["eligibility"]["version"]
    # 仅 B1 直接使用 RM-P；B2 无直接用料，资格仍 SHIPPABLE
    assert ship["current"]["status"] == "SHIPPABLE"
    # 裁决当前版本未被预演改动
    assert client.get("/api/batches/B2").json()["adjudication"]["version"] == adj_before["version"]


# ---------------------------------------------------------------- 只读性 / 与正式更正一致


def test_preview_does_not_mutate_any_state(client):
    setup_line(client)
    snapshot = {
        "batches": client.get("/api/batches").json(),
        "hist_b2": client.get("/api/batches/B2/history").json(),
        "hist_b3": client.get("/api/batches/B3/history").json(),
        "verifications": client.get("/api/verifications").json(),
        "b2_cleaned": client.get("/api/batches/B2").json()["cleaned_allergens"],
    }
    resp = preview(client, "B2", ["peanut"])
    assert resp.status_code == 200
    after = {
        "batches": client.get("/api/batches").json(),
        "hist_b2": client.get("/api/batches/B2/history").json(),
        "hist_b3": client.get("/api/batches/B3/history").json(),
        "verifications": client.get("/api/verifications").json(),
        "b2_cleaned": client.get("/api/batches/B2").json()["cleaned_allergens"],
    }
    assert after == snapshot


def test_preview_matches_formal_correction_result(client):
    setup_line(client)
    resp = preview(client, "B2", ["peanut"])
    expected = {
        b["batch_no"]: b["preview"] for b in resp.json()["batches"]
    }
    formal = client.put(
        "/api/batches/B2/cleaning", json={"cleaned_allergens": ["peanut"]}
    )
    assert formal.status_code == 200
    for s in formal.json()["recomputed"]:
        exp = expected[s["batch_no"]]
        assert s["actual"] == exp["actual_allergens"]
        assert s["undeclared"] == exp["undeclared_allergens"]
        assert s["disposition"] == exp["disposition"]
        assert s["version"] == exp["version"]


def test_preview_repeated_calls_are_stable(client):
    setup_line(client)
    r1 = preview(client, "B2", ["peanut"]).json()
    r2 = preview(client, "B2", ["peanut"]).json()
    # 版本号预测不随调用次数漂移
    assert [b["preview"]["version"] for b in r1["batches"]] == [
        b["preview"]["version"] for b in r2["batches"]
    ]


# ---------------------------------------------------------------- 拒绝


def test_preview_unknown_batch_404(client):
    resp = preview(client, "NOPE", ["milk"])
    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "CORRECTION_TARGET_NOT_FOUND"


def test_preview_invalid_allergen_rejected(client):
    setup_line(client)
    resp = preview(client, "B2", ["shellfish"])
    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == "INVALID_ALLERGEN"
    # 非法代码整体报错：未改动任何状态
    assert client.get("/api/batches/B2").json()["cleaned_allergens"] == []


def test_preview_non_list_payload_422(client):
    setup_line(client)
    resp = client.post(
        "/api/batches/B2/cleaning/preview",
        json={"cleaned_allergens": "peanut"},
    )
    assert resp.status_code == 422

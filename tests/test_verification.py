"""出货核验单测试。

覆盖：
- 创建：唯一单号、非空/无重复批次、未知批次与重复单号拒绝且不留部分单据；
- 草稿重新核验刷新逐批快照；
- 确认：原子校验快照版本仍当前且全部 RELEASE；不符批次随 422 明细返回、单据保持未确认；
  HOLD 批次始终不能确认；
- 已确认单据所含批次更正重算出新版本（即使仍 RELEASE）立即转待复核；
- 历次确认快照保留；待复核单据重新核验后才能再次确认；
- 查询：单据状态、逐批记录版本/当前版本/变化原因；
- 一个批次可被多张核验单关联；更正响应附带受影响核验单。
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


def create_verification(client, verification_no, batch_nos):
    return client.post(
        "/api/verifications",
        json={"verification_no": verification_no, "batch_nos": batch_nos},
    )


# ---------------------------------------------------------------- 创建与拒绝


def test_create_verification_saves_per_batch_snapshot(client):
    add_lot(client, "RM-M", "奶粉", ["milk"])
    b1 = add_batch(client, "B1", lots=["RM-M"], declared=["milk"])
    add_batch(client, "B2", lots=[], declared=[], cleaned=["milk"])

    resp = create_verification(client, "V1", ["B2", "B1"])  # 提交顺序须保留
    assert resp.status_code == 201
    body = resp.json()
    assert body["status"] == "DRAFT"
    assert body["confirmed_at"] is None
    assert [b["batch_no"] for b in body["batches"]] == ["B2", "B1"]
    assert [b["position"] for b in body["batches"]] == [1, 2]
    snap0 = body["batches"][0]["snapshot"]
    assert snap0["version"] == b1["adjudication"]["version"] == 1
    assert snap0["disposition"] == "RELEASE"
    assert body["batches"][0]["changed"] is False
    assert body["batches"][0]["change_reasons"] == []
    assert body["confirmations"] == []


def test_duplicate_verification_no_rejected(client):
    add_batch(client, "B1")
    assert create_verification(client, "V1", ["B1"]).status_code == 201
    resp = create_verification(client, "V1", ["B1"])
    assert resp.status_code == 409
    assert resp.json()["error"]["code"] == "DUPLICATE_VERIFICATION"
    # 拒绝不留下重复/部分单据
    assert len(client.get("/api/verifications").json()["verifications"]) == 1


def test_unknown_batch_rejected_without_partial_document(client):
    add_batch(client, "B1")
    resp = create_verification(client, "V1", ["B1", "NOPE"])
    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == "UNKNOWN_BATCH"
    assert client.get("/api/verifications/V1").status_code == 404
    assert client.get("/api/verifications").json()["verifications"] == []


def test_duplicate_batch_in_list_rejected(client):
    add_batch(client, "B1")
    resp = create_verification(client, "V1", ["B1", "B1"])
    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == "DUPLICATE_BATCH_IN_VERIFICATION"
    assert client.get("/api/verifications/V1").status_code == 404


def test_empty_batch_list_rejected(client):
    # pydantic 非空校验 -> 422；单号空白 -> 400 INVALID_PAYLOAD
    resp = client.post(
        "/api/verifications", json={"verification_no": "V1", "batch_nos": []}
    )
    assert resp.status_code == 422
    resp = client.post(
        "/api/verifications", json={"verification_no": "  ", "batch_nos": ["B1"]}
    )
    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == "INVALID_PAYLOAD"


def test_batch_can_belong_to_multiple_verifications(client):
    add_batch(client, "B1")
    assert create_verification(client, "V1", ["B1"]).status_code == 201
    assert create_verification(client, "V2", ["B1"]).status_code == 201


# ---------------------------------------------------------------- 确认


def test_confirm_when_all_release_and_current(client):
    add_lot(client, "RM-M", "奶粉", ["milk"])
    add_batch(client, "B1", lots=["RM-M"], declared=["milk"])
    create_verification(client, "V1", ["B1"])

    resp = client.post("/api/verifications/V1/confirm")
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "CONFIRMED"
    assert body["confirmed_at"] is not None
    assert len(body["confirmations"]) == 1
    assert body["confirmations"][0]["batches"][0]["version"] == 1

    # 已确认单据再次确认 -> 409
    resp = client.post("/api/verifications/V1/confirm")
    assert resp.status_code == 409
    assert resp.json()["error"]["code"] == "VERIFICATION_ALREADY_CONFIRMED"


def test_confirm_rejected_on_stale_snapshot(client):
    add_lot(client, "RM-M", "奶粉", ["milk"])
    add_batch(client, "B1", lots=["RM-M"], declared=[])  # HOLD（milk 未声明）
    add_batch(client, "B2", lots=[], declared=[])        # 继承 milk，HOLD
    create_verification(client, "V1", ["B2"])

    # 更正 B2 的声明使其放行：B2 产生 v2（RELEASE），草稿不转待复核
    resp = client.put("/api/batches/B2", json={"declared_allergens": ["milk"]})
    assert resp.status_code == 200

    resp = client.post("/api/verifications/V1/confirm")
    assert resp.status_code == 422
    err = resp.json()["error"]
    assert err["code"] == "VERIFICATION_NOT_CONFIRMABLE"
    assert err["details"][0]["batch_no"] == "B2"
    assert err["details"][0]["reason_code"] == "STALE_SNAPSHOT"
    assert err["details"][0]["snapshot_version"] == 1
    assert err["details"][0]["current_version"] == 2
    assert err["details"][0]["change_reasons"][0]["trigger"] == "CORRECT_BATCH"
    # 单据保持未确认草稿，且未写入确认记录
    doc = client.get("/api/verifications/V1").json()
    assert doc["status"] == "DRAFT"
    assert doc["confirmations"] == []

    # 重新核验刷新快照（回到 DRAFT）后可以确认
    resp = client.post("/api/verifications/V1/reverify")
    assert resp.status_code == 200
    assert resp.json()["batches"][0]["snapshot"]["version"] == 2
    resp = client.post("/api/verifications/V1/confirm")
    assert resp.status_code == 200
    assert resp.json()["status"] == "CONFIRMED"


def test_hold_batch_can_never_confirm_until_resolved(client):
    add_lot(client, "RM-P", "花生酱", ["peanut"])
    add_batch(client, "B1", lots=["RM-P"], declared=[])  # HOLD
    create_verification(client, "V1", ["B1"])

    resp = client.post("/api/verifications/V1/confirm")
    assert resp.status_code == 422
    details = resp.json()["error"]["details"]
    assert len(details) == 1
    assert details[0]["reason_code"] == "BATCH_ON_HOLD"
    assert details[0]["disposition"] == "HOLD"
    assert details[0]["undeclared_allergens"] == ["peanut"]
    assert client.get("/api/verifications/V1").json()["status"] == "DRAFT"

    # 即使重新核验，HOLD 仍不能确认
    assert client.post("/api/verifications/V1/reverify").status_code == 200
    assert client.post("/api/verifications/V1/confirm").status_code == 422

    # 更正为放行后，重新核验再确认才成功
    client.put("/api/batches/B1", json={"declared_allergens": ["peanut"]})
    client.post("/api/verifications/V1/reverify")
    resp = client.post("/api/verifications/V1/confirm")
    assert resp.status_code == 200
    assert resp.json()["status"] == "CONFIRMED"


def test_confirm_stale_and_hold_returns_all_mismatches_atomically(client):
    add_lot(client, "RM-M", "奶粉", ["milk"])
    add_batch(client, "B1", lots=["RM-M"], declared=[])  # HOLD
    add_batch(client, "B2", lots=["RM-M"], declared=["milk"])  # RELEASE
    create_verification(client, "V1", ["B1", "B2"])

    # B2 声明被清空 -> 重算后 B2 v2 HOLD：快照陈旧 + 当前 HOLD
    client.put("/api/batches/B2", json={"declared_allergens": []})

    resp = client.post("/api/verifications/V1/confirm")
    assert resp.status_code == 422
    details = resp.json()["error"]["details"]
    by_batch: dict[str, list[str]] = {}
    for d in details:
        by_batch.setdefault(d["batch_no"], []).append(d["reason_code"])
    # B1：快照即 HOLD；B2：快照陈旧（v1→v2）且当前 HOLD
    assert by_batch["B1"] == ["BATCH_ON_HOLD"]
    assert by_batch["B2"] == ["STALE_SNAPSHOT", "BATCH_ON_HOLD"]
    assert client.get("/api/verifications/V1").json()["status"] == "DRAFT"


# ---------------------------------------------------------------- 待复核与历史确认


def test_confirmed_document_goes_pending_review_on_new_version_even_if_release(client):
    add_lot(client, "RM-P", "花生酱", ["peanut"])
    add_batch(client, "B1", lots=["RM-P"], declared=["peanut"])  # RELEASE
    create_verification(client, "V1", ["B1"])
    assert client.post("/api/verifications/V1/confirm").status_code == 200

    # 更正原料批号过敏原再更正回来：B1 产生新版本，但结论仍为 RELEASE
    client.put("/api/material-lots/RM-P", json={"allergens": ["peanut", "sesame"]})
    resp = client.put("/api/material-lots/RM-P", json={"allergens": ["peanut"]})
    assert resp.status_code == 200
    assert resp.json()["recomputed"][0]["disposition"] == "RELEASE"
    assert resp.json()["recomputed"][0]["version"] == 3

    doc = client.get("/api/verifications/V1").json()
    assert doc["status"] == "PENDING_REVIEW"
    entry = doc["batches"][0]
    assert entry["snapshot"]["version"] == 1
    assert entry["current"]["version"] == 3
    assert entry["changed"] is True
    assert [r["to_version"] for r in entry["change_reasons"]] == [2, 3]
    assert entry["change_reasons"][0]["trigger"] == "CORRECT_LOT"

    # 待复核单据不能直接确认：必须先重新核验
    resp = client.post("/api/verifications/V1/confirm")
    assert resp.status_code == 422
    assert resp.json()["error"]["details"][0]["reason_code"] == "STALE_SNAPSHOT"

    # 重新核验 -> DRAFT，快照 v3；再次确认成功，且历次确认快照保留
    assert client.post("/api/verifications/V1/reverify").json()["status"] == "DRAFT"
    resp = client.post("/api/verifications/V1/confirm")
    assert resp.status_code == 200
    doc = resp.json()
    assert len(doc["confirmations"]) == 2
    assert doc["confirmations"][0]["batches"][0]["version"] == 1
    assert doc["confirmations"][1]["batches"][0]["version"] == 3


def test_correction_response_lists_affected_verifications(client):
    add_lot(client, "RM-P", "花生酱", ["peanut"])
    add_batch(client, "B1", lots=["RM-P"], declared=["peanut"])
    create_verification(client, "VD", ["B1"])  # 草稿
    create_verification(client, "VC", ["B1"])
    client.post("/api/verifications/VC/confirm")

    resp = client.put(
        "/api/batches/B1", json={"declared_allergens": ["peanut", "milk"]}
    )
    affected = {v["verification_no"]: v["status"] for v in resp.json()["affected_verifications"]}
    # 已确认 -> 待复核；草稿保持草稿
    assert affected == {"VC": "PENDING_REVIEW", "VD": "DRAFT"}


def test_reverify_rules(client):
    add_batch(client, "B1")
    create_verification(client, "V1", ["B1"])
    client.post("/api/verifications/V1/confirm")

    # 已确认单据不能重新核验
    resp = client.post("/api/verifications/V1/reverify")
    assert resp.status_code == 409
    assert resp.json()["error"]["code"] == "VERIFICATION_ALREADY_CONFIRMED"

    # 不存在
    assert client.post("/api/verifications/NOPE/reverify").status_code == 404
    assert client.post("/api/verifications/NOPE/confirm").status_code == 404
    assert client.get("/api/verifications/NOPE").status_code == 404


def test_lot_correction_across_lines_flips_confirmed_to_pending(client):
    # 两条产线各有一个引用同一原料批号的已放行批次，分别归属不同已确认核验单
    add_lot(client, "RM-M", "奶粉", ["milk"])
    add_batch(client, "X1", line_id="LX", lots=["RM-M"], declared=["milk"])
    add_batch(client, "Y1", line_id="LY", lots=["RM-M"], declared=["milk"])
    create_verification(client, "VX", ["X1"])
    create_verification(client, "VY", ["Y1"])
    client.post("/api/verifications/VX/confirm")
    client.post("/api/verifications/VY/confirm")

    resp = client.put("/api/material-lots/RM-M", json={"allergens": ["milk", "egg"]})
    assert resp.status_code == 200
    affected = {v["verification_no"]: v["status"] for v in resp.json()["affected_verifications"]}
    assert affected == {"VX": "PENDING_REVIEW", "VY": "PENDING_REVIEW"}
    for no in ("VX", "VY"):
        assert client.get(f"/api/verifications/{no}").json()["status"] == "PENDING_REVIEW"


def test_list_verifications_and_status_filter(client):
    add_lot(client, "RM-P", "花生酱", ["peanut"])
    add_batch(client, "B1", lots=["RM-P"], declared=["peanut"])
    create_verification(client, "V1", ["B1"])
    create_verification(client, "V2", ["B1"])
    client.post("/api/verifications/V2/confirm")

    assert {v["verification_no"] for v in client.get("/api/verifications").json()["verifications"]} == {"V1", "V2"}
    drafts = client.get("/api/verifications", params={"status": "DRAFT"}).json()["verifications"]
    assert [v["verification_no"] for v in drafts] == ["V1"]
    confirmed = client.get("/api/verifications", params={"status": "CONFIRMED"}).json()["verifications"]
    assert [v["verification_no"] for v in confirmed] == ["V2"]
    assert client.get("/api/verifications", params={"status": "BOGUS"}).status_code == 400

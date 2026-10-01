"""批次误归产线更正（换线）测试。

覆盖：
- 基础换线：批次号不变、移至目标线末尾获得新 seq、原线 seq 留空不复用；
  原线从移出位置起、目标线从移入批次起重算过敏原残留与裁决（trigger=MOVE_BATCH），
  每个被重算批次追加历史版本，即使结论不变也追加；
- 历史版本中的原产线与顺序号不改：旧版本保留旧 line_id/seq，新版本记录新归属；
- 原线顺序空洞：其余批次 seq 不重排，后续新登记顺延、空洞不被复用；
- 直接用料未变：出货资格版本不递增、资格历史不追加（含锁定批号迁移后仍 BLOCKED）；
- 核验单：含被重算批次的已确认单据转 PENDING_REVIEW，草稿不转态，
  历次确认快照保持原样；待复核单据重新核验后新快照带新归属；
- 拒绝：未知批次 404、未知目标线 404、已作废批次 409、目标线与原线相同 409、
  空目标线 400；失败原子回滚，不留下部分归属或裁决版本；
- 查询、更正、核验接口约定保持可用：换线后批次更正/作废/历史/资格接口正常。
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


def add_line(client, line_id):
    # 产线随首个批次自动建档；直接登记一个批次即可，这里用空批次占位
    resp = client.post(
        "/api/batches",
        json={
            "batch_no": f"SEED-{line_id}",
            "line_id": line_id,
            "material_lots": [],
            "declared_allergens": [],
            "cleaned_allergens": [],
        },
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


def reassign(client, batch_no, target_line_id):
    return client.post(
        f"/api/batches/{batch_no}/reassign",
        json={"target_line_id": target_line_id},
    )


def create_verification(client, verification_no, batch_nos):
    return client.post(
        "/api/verifications",
        json={"verification_no": verification_no, "batch_nos": batch_nos},
    )


def confirm(client, verification_no):
    return client.post(f"/api/verifications/{verification_no}/confirm")


def adj(batch_json):
    return batch_json["adjudication"]


# ---------------------------------------------------------------- 基础归属与顺序号


def test_reassign_moves_batch_to_target_line_tail_keeps_batch_no(client):
    add_line(client, "L2")
    b1 = add_batch(client, "B1", "L1")
    b2 = add_batch(client, "B2", "L1")

    resp = reassign(client, "B1", "L2")
    assert resp.status_code == 200, resp.text
    body = resp.json()

    # 新旧归属
    assert body["assignment"] == {
        "batch_no": "B1",
        "from_line_id": "L1",
        "from_seq": 1,
        "to_line_id": "L2",
        "to_seq": 2,  # L2 已有占位批 SEED-L2(seq1)，移入批顺延为 2
    }
    moved = body["moved"]
    assert moved["batch_no"] == "B1"
    assert moved["line_id"] == "L2"
    assert moved["seq"] == 2
    # 直接用料登记内容不变
    assert moved["material_lots"] == b1["material_lots"]
    assert moved["declared_allergens"] == b1["declared_allergens"]
    assert moved["cleaned_allergens"] == b2["cleaned_allergens"] == []

    # 原线：B2 仍在 L1、seq 仍为 2（不重排），seq 1 留空
    b2_after = client.get("/api/batches/B2").json()
    assert b2_after["line_id"] == "L1" and b2_after["seq"] == 2
    l1 = {b["batch_no"]: b for b in client.get("/api/batches?line_id=L1").json()["batches"]}
    assert set(l1) == {"B2"}
    l2 = {b["batch_no"]: b for b in client.get("/api/batches?line_id=L2").json()["batches"]}
    assert set(l2) == {"SEED-L2", "B1"}


def test_original_seq_is_never_reused(client):
    add_line(client, "L2")
    add_batch(client, "B1", "L1")
    add_batch(client, "B2", "L1")
    assert reassign(client, "B1", "L2").status_code == 200

    # L1 再登记新批：顺延为 seq 3，留空的 seq 1 不复用
    b3 = add_batch(client, "B3", "L1")
    assert b3["seq"] == 3
    seqs = sorted(b["seq"] for b in client.get("/api/batches?line_id=L1").json()["batches"])
    assert seqs == [2, 3]


# ---------------------------------------------------------------- 两线重算与历史版本


def test_source_and_target_lines_recomputed_with_move_trigger(client):
    # L1: R1 peanut 残留（声明 peanut，RELEASE）
    #     R2 无原料、未清洁 -> 继承 peanut 未声明 -> HOLD（误归产线，待移出）
    # L2: T1 milk 残留（声明 milk，RELEASE）
    add_lot(client, "RM-P", "花生酱", ["peanut"])
    add_lot(client, "RM-M", "奶粉", ["milk"])
    r1 = add_batch(client, "R1", "L1", lots=["RM-P"], declared=["peanut"])
    r2 = add_batch(client, "R2", "L1", lots=[], declared=[])
    assert adj(r2)["disposition"] == "HOLD"
    assert adj(r2)["actual_allergens"] == ["peanut"]
    t1 = add_batch(client, "T1", "L2", lots=["RM-M"], declared=["milk"])
    assert adj(t1)["actual_allergens"] == ["milk"]

    resp = reassign(client, "R2", "L2")
    assert resp.status_code == 200, resp.text
    body = resp.json()

    # 原线重算：移出位置 seq2 起，仅 R2 之后……R2 是最后一批，故原线无批次重算
    assert body["source_line"]["line_id"] == "L1"
    assert body["source_line"]["vacated_seq"] == 2
    assert body["source_line"]["recomputed"] == []
    # 目标线重算：从移入批 seq2 起，仅移入批 R2 本身
    rec = body["target_line"]["recomputed"]
    assert [s["batch_no"] for s in rec] == ["R2"]
    assert rec[0]["line_id"] == "L2" and rec[0]["seq"] == 2
    assert rec[0]["version"] == 2
    # L2 上一批 T1 残留 milk：R2 无原料、清洁为空，继承 milk 而非 peanut
    assert rec[0]["actual"] == ["milk"]
    assert rec[0]["undeclared"] == ["milk"]
    assert rec[0]["disposition"] == "HOLD"

    # R1 不受影响：仍是 v1，实际 peanut
    r1_after = client.get("/api/batches/R1").json()
    assert adj(r1_after)["version"] == 1
    assert adj(r1_after)["actual_allergens"] == ["peanut"]
    # T1（移入点之前）不重算，仍 v1
    t1_after = client.get("/api/batches/T1").json()
    assert adj(t1_after)["version"] == 1

    # 合并重算列表
    assert [s["batch_no"] for s in body["recomputed"]] == ["R2"]


def test_source_line_recomputes_successors_from_vacated_position(client):
    # L1: A1 peanut 残留；A2 peanut 误归（待移出）；A3 无原料继承 peanut -> HOLD
    # 移出 A2 后：A3 的上一有效批变成 A1（仍残留 peanut），结果不变但仍追加版本
    add_lot(client, "RM-P", "花生酱", ["peanut"])
    a1 = add_batch(client, "A1", "L1", lots=["RM-P"], declared=["peanut"])
    a2 = add_batch(client, "A2", "L1", lots=["RM-P"], declared=["peanut"])
    a3 = add_batch(client, "A3", "L1", lots=[], declared=[])
    assert adj(a3)["disposition"] == "HOLD"
    add_line(client, "L2")

    resp = reassign(client, "A2", "L2")
    body = resp.json()
    src_rec = body["source_line"]["recomputed"]
    assert [s["batch_no"] for s in src_rec] == ["A3"]  # 从 vacated seq2 起，A2 已不在 L1
    assert src_rec[0]["version"] == 2
    assert src_rec[0]["actual"] == ["peanut"]  # 结论不变也追加版本

    a3_after = client.get("/api/batches/A3").json()
    assert adj(a3_after)["version"] == 2
    assert adj(a3_after)["disposition"] == "HOLD"
    # A1 在移出点之前，不重算
    assert adj(client.get("/api/batches/A1").json())["version"] == 1


def test_source_line_residue_changes_after_removal(client):
    # L1: C1 peanut 残留、C2 清洁 peanut（误归，待移出）、C3 无原料
    # 移出前 C3 无残留（C2 清洁了 peanut）-> RELEASE；
    # 移出 C2 后其清洁视为不在该线发生，C3 直接继承 C1 的 peanut -> HOLD
    add_lot(client, "RM-P", "花生酱", ["peanut"])
    add_batch(client, "C1", "L1", lots=["RM-P"], declared=["peanut"])
    add_batch(client, "C2", "L1", lots=[], declared=[], cleaned=["peanut"])
    c3 = add_batch(client, "C3", "L1", lots=[], declared=[])
    assert adj(c3)["disposition"] == "RELEASE"
    assert adj(c3)["actual_allergens"] == []
    add_line(client, "L2")

    resp = reassign(client, "C2", "L2")
    src_rec = resp.json()["source_line"]["recomputed"]
    assert [s["batch_no"] for s in src_rec] == ["C3"]
    assert src_rec[0]["disposition"] == "HOLD"
    assert src_rec[0]["actual"] == ["peanut"]


def test_history_keeps_original_line_and_seq_unchanged(client):
    add_lot(client, "RM-P", "花生酱", ["peanut"])
    add_batch(client, "M1", "L1", lots=["RM-P"], declared=["peanut"])
    add_batch(client, "T1", "L2", lots=[], declared=[])

    resp = reassign(client, "M1", "L2")
    assert resp.status_code == 200
    new_seq = resp.json()["assignment"]["to_seq"]

    hist = client.get("/api/batches/M1/history").json()
    assert hist["line_id"] == "L2" and hist["seq"] == new_seq  # 当前归属
    versions = hist["history"]
    assert [v["version"] for v in versions] == [1, 2]
    # 旧版本保留原产线与顺序号，不改写
    assert versions[0]["line_id"] == "L1" and versions[0]["seq"] == 1
    assert versions[0]["trigger"] == "REGISTER"
    # 新版本记录目标线与新顺序号
    assert versions[1]["line_id"] == "L2" and versions[1]["seq"] == new_seq
    assert versions[1]["trigger"] == "MOVE_BATCH"


def test_version_appended_even_when_disposition_unchanged(client):
    # 空产线间移动：移入前后都无残留、无原料，结论恒 RELEASE，仍追加版本
    add_line(client, "L2")
    add_batch(client, "E1", "L1")
    before = adj(client.get("/api/batches/E1").json())["version"]

    resp = reassign(client, "E1", "L2")
    rec = resp.json()["target_line"]["recomputed"]
    assert rec[0]["version"] == before + 1
    assert rec[0]["disposition"] == "RELEASE"
    after = client.get("/api/batches/E1").json()
    assert adj(after)["version"] == before + 1
    assert len(client.get("/api/batches/E1/history").json()["history"]) == before + 1


def test_moved_batch_residual_comes_from_target_line(client):
    # L1 残留 peanut，L2 残留 milk；移动批无原料、无清洁，换线后继承 milk
    add_lot(client, "RM-P", "花生酱", ["peanut"])
    add_lot(client, "RM-M", "奶粉", ["milk"])
    add_batch(client, "P1", "L1", lots=["RM-P"], declared=["peanut"])
    x = add_batch(client, "X1", "L1", lots=[], declared=[])
    assert adj(x)["actual_allergens"] == ["peanut"]
    add_batch(client, "M1", "L2", lots=["RM-M"], declared=["milk"])

    resp = reassign(client, "X1", "L2")
    rec = resp.json()["target_line"]["recomputed"][0]
    assert rec["actual"] == ["milk"]
    assert rec["undeclared"] == ["milk"]


# ---------------------------------------------------------------- 出货资格不因换线递增


def test_shipment_eligibility_version_not_incremented(client):
    add_line(client, "L2")
    add_batch(client, "S1", "L1")
    before = client.get("/api/batches/S1/eligibility").json()

    assert reassign(client, "S1", "L2").status_code == 200

    after = client.get("/api/batches/S1/eligibility").json()
    assert after["line_id"] == "L2"  # 当前归属更新
    assert after["eligibility"]["version"] == before["eligibility"]["version"] == 1
    assert after["eligibility"]["status"] == "SHIPPABLE"
    # 资格历史不追加换线记录
    triggers = [v["trigger"] for v in after["history"]]
    assert triggers == ["REGISTER"]


def test_shipment_blocked_state_moves_with_batch_without_new_version(client):
    add_lot(client, "RM-P", "花生酱", ["peanut"])
    add_line(client, "L2")
    add_batch(client, "S2", "L1", lots=["RM-P"], declared=["peanut"])
    # 锁定批号：S2 出货资格 v2 BLOCKED
    lock = client.post(
        "/api/material-lots/RM-P/lock", json={"reason": "待检风险"}
    )
    assert lock.status_code == 200
    before = client.get("/api/batches/S2/eligibility").json()
    assert before["eligibility"]["version"] == 2
    assert before["eligibility"]["status"] == "BLOCKED"

    assert reassign(client, "S2", "L2").status_code == 200

    after = client.get("/api/batches/S2/eligibility").json()
    assert after["line_id"] == "L2"
    # 直接用料未变：版本不递增，仍是 v2 BLOCKED，锁定批号保留
    assert after["eligibility"]["version"] == 2
    assert after["eligibility"]["status"] == "BLOCKED"
    assert after["eligibility"]["locked_lots"] == ["RM-P"]
    assert [v["trigger"] for v in after["history"]] == ["REGISTER", "LOT_LOCK"]


# ---------------------------------------------------------------- 核验单联动


def test_confirmed_verification_flips_to_pending_review(client):
    add_lot(client, "RM-P", "花生酱", ["peanut"])
    add_line(client, "L2")
    add_batch(client, "V1", "L1", lots=["RM-P"], declared=["peanut"])

    assert create_verification(client, "SH-1", ["V1"]).status_code == 201
    assert confirm(client, "SH-1").status_code == 200

    resp = reassign(client, "V1", "L2")
    affected = {v["verification_no"]: v for v in resp.json()["affected_verifications"]}
    assert affected["SH-1"]["status"] == "PENDING_REVIEW"

    doc = client.get("/api/verifications/SH-1").json()
    assert doc["status"] == "PENDING_REVIEW"
    batch_rec = doc["batches"][0]
    assert batch_rec["line_id"] == "L2"  # 逐批记录反映新归属
    assert batch_rec["changed"] is True
    assert batch_rec["change_reasons"][-1]["trigger"] == "MOVE_BATCH"
    # 出货资格版本未变
    assert batch_rec["shipment_changed"] is False
    # 历次确认快照原样保留：仍是换线前版本与旧归属无关字段
    assert len(doc["confirmations"]) == 1
    snap = doc["confirmations"][0]["batches"][0]
    assert snap["version"] == 1  # 确认时冻结 v1，不因换线改写


def test_draft_verification_not_flipped_but_listed_as_affected(client):
    add_line(client, "L2")
    add_batch(client, "D1", "L1")
    assert create_verification(client, "SH-D", ["D1"]).status_code == 201

    resp = reassign(client, "D1", "L2")
    affected = {v["verification_no"]: v for v in resp.json()["affected_verifications"]}
    assert affected["SH-D"]["status"] == "DRAFT"
    assert client.get("/api/verifications/SH-D").json()["status"] == "DRAFT"


def test_reverify_after_move_refreshes_snapshot_and_can_reconfirm(client):
    add_lot(client, "RM-M", "奶粉", ["milk"])
    add_batch(client, "W1", "L1", lots=["RM-M"], declared=["milk"])
    # L2 上一批同样只残留 milk 且 W1 声明 milk：换线后仍 RELEASE
    add_batch(client, "W0", "L2", lots=["RM-M"], declared=["milk"])
    assert create_verification(client, "SH-W", ["W1"]).status_code == 201
    assert confirm(client, "SH-W").status_code == 200

    assert reassign(client, "W1", "L2").status_code == 200
    assert client.get("/api/verifications/SH-W").json()["status"] == "PENDING_REVIEW"

    assert client.post("/api/verifications/SH-W/reverify").status_code == 200
    doc = client.get("/api/verifications/SH-W").json()
    assert doc["status"] == "DRAFT"
    rec = doc["batches"][0]
    assert rec["line_id"] == "L2"
    assert rec["changed"] is False
    assert rec["snapshot"]["version"] == rec["current"]["version"] == 2
    assert rec["shipment_changed"] is False
    # 重新确认成功；历史确认保留为两次
    assert confirm(client, "SH-W").status_code == 200
    doc2 = client.get("/api/verifications/SH-W").json()
    assert doc2["status"] == "CONFIRMED"
    assert len(doc2["confirmations"]) == 2


def test_verification_with_recomputed_successor_flips(client):
    # 核验单关联原线后续批次 A3（非移动批本身）：A3 被重算 -> 列入受影响；
    # 草稿单不转态。移出点之前的 A1 不重算，其已确认单据不受影响。
    add_lot(client, "RM-P", "花生酱", ["peanut"])
    add_batch(client, "A1", "L1", lots=["RM-P"], declared=["peanut"])
    add_batch(client, "A2", "L1", lots=["RM-P"], declared=["peanut"])
    add_batch(client, "A3", "L1", lots=[], declared=[])
    add_line(client, "L2")
    assert create_verification(client, "SH-A", ["A3"]).status_code == 201  # 草稿
    assert create_verification(client, "SH-A1", ["A1"]).status_code == 201
    assert confirm(client, "SH-A1").status_code == 200

    body = reassign(client, "A2", "L2").json()
    affected = {v["verification_no"] for v in body["affected_verifications"]}
    assert "SH-A" in affected          # A3 被重算
    assert "SH-A1" not in affected     # A1 在移出点之前未重算
    # 草稿保持草稿；已确认的 SH-A1 不被转态
    assert client.get("/api/verifications/SH-A").json()["status"] == "DRAFT"
    assert client.get("/api/verifications/SH-A1").json()["status"] == "CONFIRMED"


def test_confirmed_doc_on_recomputed_successor_flips(client):
    add_lot(client, "RM-P", "花生酱", ["peanut"])
    add_batch(client, "K1", "L1", lots=["RM-P"], declared=["peanut"])
    add_batch(client, "K2", "L1", lots=[], declared=["peanut"])  # peanut 已声明 RELEASE
    add_line(client, "L2")
    assert create_verification(client, "SH-K", ["K2"]).status_code == 201
    assert confirm(client, "SH-K").status_code == 200

    # 移出 K1 后 K2 从移出点重算（v2），已确认单据转待复核
    body = reassign(client, "K1", "L2").json()
    assert {s["batch_no"] for s in body["source_line"]["recomputed"]} == {"K2"}
    assert client.get("/api/verifications/SH-K").json()["status"] == "PENDING_REVIEW"


# ---------------------------------------------------------------- 拒绝与原子性


def test_reject_unknown_batch(client):
    add_line(client, "L2")
    resp = reassign(client, "NOPE", "L2")
    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "BATCH_NOT_FOUND"


def test_reject_unknown_target_line(client):
    add_batch(client, "B1", "L1")
    resp = reassign(client, "B1", "NO-LINE")
    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "LINE_NOT_FOUND"
    # 批次归属不变
    b = client.get("/api/batches/B1").json()
    assert b["line_id"] == "L1" and b["seq"] == 1


def test_reject_voided_batch(client):
    add_line(client, "L2")
    add_batch(client, "B1", "L1")
    assert client.post("/api/batches/B1/void", json={"reason": "误登记"}).status_code == 200

    resp = reassign(client, "B1", "L2")
    assert resp.status_code == 409
    assert resp.json()["error"]["code"] == "BATCH_VOIDED"


def test_reject_same_source_and_target_line(client):
    add_batch(client, "B1", "L1")
    resp = reassign(client, "B1", "L1")
    assert resp.status_code == 409
    body = resp.json()
    assert body["error"]["code"] == "SAME_SOURCE_TARGET_LINE"
    assert body["error"]["details"][0]["line_id"] == "L1"
    # 归属与版本均不变
    b = client.get("/api/batches/B1").json()
    assert b["line_id"] == "L1" and b["seq"] == 1
    assert adj(b)["version"] == 1


def test_reject_blank_target_line(client):
    add_batch(client, "B1", "L1")
    resp = client.post("/api/batches/B1/reassign", json={"target_line_id": "   "})
    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == "INVALID_PAYLOAD"


def test_missing_target_line_field_is_422(client):
    add_batch(client, "B1", "L1")
    resp = client.post("/api/batches/B1/reassign", json={})
    assert resp.status_code == 422


def test_failure_is_atomic_no_partial_assignment_or_versions(client):
    add_lot(client, "RM-P", "花生酱", ["peanut"])
    add_batch(client, "B1", "L1", lots=["RM-P"], declared=["peanut"])
    add_batch(client, "B2", "L1", lots=[], declared=[])
    v_before = adj(client.get("/api/batches/B2").json())["version"]

    # 目标线不存在：归属更新与两线重算都不得落库
    resp = reassign(client, "B1", "GHOST")
    assert resp.status_code == 404
    b1 = client.get("/api/batches/B1").json()
    assert b1["line_id"] == "L1" and b1["seq"] == 1
    b2 = client.get("/api/batches/B2").json()
    assert b2["line_id"] == "L1" and b2["seq"] == 2
    assert adj(b2)["version"] == v_before
    assert len(client.get("/api/batches/B1/history").json()["history"]) == 1
    assert len(client.get("/api/batches/B2/history").json()["history"]) == v_before


# ---------------------------------------------------------------- 既有接口约定保持可用


def test_existing_apis_still_work_after_move(client):
    add_lot(client, "RM-P", "花生酱", ["peanut"])
    add_line(client, "L2")
    add_batch(client, "Q1", "L1", lots=["RM-P"], declared=["peanut"])

    assert reassign(client, "Q1", "L2").status_code == 200

    # 批次更正仍可用（按新线顺序重算）
    resp = client.put(
        "/api/batches/Q1",
        json={"declared_allergens": ["peanut", "milk"]},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["corrected"]["line_id"] == "L2"

    # 清洁更正专用入口可用
    resp2 = client.put(
        "/api/batches/Q1/cleaning", json={"cleaned_allergens": ["peanut"]}
    )
    assert resp2.status_code == 200

    # 作废仍可用，且作废后保留新归属记录
    assert client.post("/api/batches/Q1/void", json={"reason": "误登记"}).status_code == 200
    q1 = client.get("/api/batches/Q1").json()
    assert q1["status"] == "VOIDED" and q1["line_id"] == "L2"

    # 产线列表统计正常
    lines = {l["line_id"]: l for l in client.get("/api/lines").json()["lines"]}
    assert lines["L1"]["batch_count"] == 0
    assert lines["L2"]["batch_count"] == 2  # 占位批 + 移入批


def test_new_batch_registration_on_both_lines_after_move(client):
    add_line(client, "L2")
    add_batch(client, "N1", "L1")
    add_batch(client, "N2", "L1")
    assert reassign(client, "N1", "L2").status_code == 200

    n3 = add_batch(client, "N3", "L1")  # seq 顺延：N2=2, 空洞 1 -> 3
    n4 = add_batch(client, "N4", "L2")  # L2: SEED=1, N1=2 -> N4=3
    assert n3["seq"] == 3
    assert n4["seq"] == 3
    # 残留链按新结构：N4 前一有效批为 N1（空批，无残留）-> RELEASE
    assert adj(n4)["disposition"] == "RELEASE"


def test_batch_no_unchanged_and_duplicate_still_rejected(client):
    add_line(client, "L2")
    add_batch(client, "Z1", "L1")
    assert reassign(client, "Z1", "L2").status_code == 200

    # 批次号不可被新登记复用（无论登记到哪条线）
    for line in ("L1", "L2"):
        resp = client.post(
            "/api/batches",
            json={
                "batch_no": "Z1", "line_id": line,
                "material_lots": [], "declared_allergens": [], "cleaned_allergens": [],
            },
        )
        assert resp.status_code == 409
        assert resp.json()["error"]["code"] == "DUPLICATE_BATCH"


def test_move_then_move_again_to_third_line(client):
    add_line(client, "L2")
    add_line(client, "L3")
    add_batch(client, "Z9", "L1")

    assert reassign(client, "Z9", "L2").status_code == 200
    resp = reassign(client, "Z9", "L3")
    assert resp.status_code == 200
    body = resp.json()
    assert body["assignment"]["from_line_id"] == "L2"
    assert body["assignment"]["to_line_id"] == "L3"
    hist = client.get("/api/batches/Z9/history").json()["history"]
    assert [v["trigger"] for v in hist[-2:]] == ["MOVE_BATCH", "MOVE_BATCH"]
    assert [(v["line_id"], v["seq"]) for v in hist] == [("L1", 1), ("L2", 2), ("L3", 2)]

"""原料批号待检锁定与出货资格测试。

覆盖：
- 锁定/解除：未知批号与空原因拒绝；重复提交相同状态幂等不产生新变更；
  当前状态与历次变更可查询；
- 出货资格：仅按“直接使用”的批号锁定状态计算，不沿同线残留传递；
  新登记批次按当前锁定状态确定资格；
- 版本：批号状态每次实际变化使直接使用批次出货资格版本递增；
  更正用料改变资格也递增（不改变则不递增）；过敏原裁决与历史版本不变；
- 出货核验单：逐批快照记录出货资格版本；确认同时核对裁决版本、出货资格版本与
  当前锁定状态，任一不符返回逐批原因且整单不确认；已确认单据在资格版本变化后
  转待复核，重新核验后方可再确认；历次确认快照保留。
"""
from __future__ import annotations

import sqlite3

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


def lock(client, lot_no, reason):
    return client.post(f"/api/material-lots/{lot_no}/lock", json={"reason": reason})


def unlock(client, lot_no, reason):
    return client.post(f"/api/material-lots/{lot_no}/unlock", json={"reason": reason})


def create_verification(client, verification_no, batch_nos):
    return client.post(
        "/api/verifications",
        json={"verification_no": verification_no, "batch_nos": batch_nos},
    )


# ---------------------------------------------------------------- 锁定/解除


def test_default_lock_state_unlocked(client):
    add_lot(client, "RM-M", "奶粉", ["milk"])
    body = client.get("/api/material-lots/RM-M/lock").json()
    assert body["lock"]["locked"] is False
    assert body["lock"]["status"] == "UNLOCKED"
    assert body["lock"]["version"] == 0
    assert body["history"] == []
    # 登记响应也带当前锁定状态
    lot = client.get("/api/material-lots/RM-M").json()
    assert lot["lock"]["locked"] is False


def test_lock_unlock_records_history(client):
    add_lot(client, "RM-M", "奶粉", ["milk"])

    resp = lock(client, "RM-M", "复检待判定")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["changed"] is True
    assert body["locked"] is True
    assert body["lock_version"] == 1

    body = client.get("/api/material-lots/RM-M/lock").json()
    assert body["lock"]["locked"] is True
    assert body["lock"]["reason"] == "复检待判定"
    assert [h["action"] for h in body["history"]] == ["LOCK"]

    resp = unlock(client, "RM-M", "复检合格解除")
    assert resp.status_code == 200
    assert resp.json()["changed"] is True
    assert resp.json()["lock_version"] == 2

    body = client.get("/api/material-lots/RM-M/lock").json()
    assert body["lock"]["locked"] is False
    assert body["lock"]["status"] == "UNLOCKED"
    assert [h["action"] for h in body["history"]] == ["LOCK", "UNLOCK"]
    assert [h["reason"] for h in body["history"]] == ["复检待判定", "复检合格解除"]
    assert [h["version"] for h in body["history"]] == [1, 2]


def test_duplicate_same_state_is_idempotent(client):
    add_lot(client, "RM-M", "奶粉")
    assert lock(client, "RM-M", "首次锁定").json()["changed"] is True

    resp = lock(client, "RM-M", "再次锁定")
    assert resp.status_code == 200
    body = resp.json()
    assert body["changed"] is False
    assert body["lock_version"] == 1
    assert body["reason"] == "首次锁定"  # 保留首次实际变化的原因
    assert body["affected_batches"] == []

    hist = client.get("/api/material-lots/RM-M/lock").json()["history"]
    assert len(hist) == 1  # 不产生新变更


def test_unlock_never_locked_lot_is_idempotent(client):
    add_lot(client, "RM-M", "奶粉")
    resp = unlock(client, "RM-M", "本就未锁定")
    assert resp.status_code == 200
    body = resp.json()
    assert body["changed"] is False
    assert body["locked"] is False
    assert body["lock_version"] == 0
    assert client.get("/api/material-lots/RM-M/lock").json()["history"] == []


def test_lock_rejects_unknown_lot_and_empty_reason(client):
    add_lot(client, "RM-M", "奶粉")

    resp = client.post("/api/material-lots/NOPE/lock", json={"reason": "x"})
    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "MATERIAL_LOT_NOT_FOUND"
    assert client.post("/api/material-lots/NOPE/unlock", json={"reason": "x"}).status_code == 404
    assert client.get("/api/material-lots/NOPE/lock").status_code == 404

    for bad in ("", "   "):
        resp = client.post("/api/material-lots/RM-M/lock", json={"reason": bad})
        assert resp.status_code == 400
        assert resp.json()["error"]["code"] == "INVALID_PAYLOAD"


# ---------------------------------------------------------------- 出货资格计算


def test_direct_use_blocked_but_line_carryover_not_affected(client):
    # L1：B1 直接使用 RM-P（声明 peanut，RELEASE）；
    #     B2 同线后续、不直接使用任何批号（仅残留承接）；
    # L2：C1 直接使用 RM-P
    add_lot(client, "RM-P", "花生酱", ["peanut"])
    b1 = add_batch(client, "B1", "L1", lots=["RM-P"], declared=["peanut"])
    b2 = add_batch(client, "B2", "L1", lots=[], declared=[], cleaned=["peanut"])
    c1 = add_batch(client, "C1", "L2", lots=["RM-P"], declared=["peanut"])

    assert b1["shipment_eligibility"]["status"] == "SHIPPABLE"
    assert b1["shipment_eligibility"]["version"] == 1
    assert b2["shipment_eligibility"]["status"] == "SHIPPABLE"

    resp = lock(client, "RM-P", "黄曲霉毒素待检")
    assert resp.status_code == 200
    affected = {a["batch_no"]: a for a in resp.json()["affected_batches"]}
    assert set(affected) == {"B1", "C1"}  # B2 未直接使用，不连带
    assert affected["B1"]["status"] == "BLOCKED"
    assert affected["B1"]["locked_lots"] == ["RM-P"
                                             ]

    # 直接使用的批次：裁决仍 RELEASE，但出货资格 BLOCKED、版本递增
    b1now = client.get("/api/batches/B1").json()
    assert b1now["adjudication"]["disposition"] == "RELEASE"
    assert b1now["adjudication"]["version"] == 1  # 过敏原裁决版本不变
    assert b1now["shipment_eligibility"]["status"] == "BLOCKED"
    assert b1now["shipment_eligibility"]["version"] == 2

    # 同线后续未直接使用该批号的批次：不连带锁定，资格版本保持 1
    b2now = client.get("/api/batches/B2").json()
    assert b2now["shipment_eligibility"]["status"] == "SHIPPABLE"
    assert b2now["shipment_eligibility"]["version"] == 1
    assert b2now["adjudication"]["version"] == 1  # 裁决历史不变

    c1now = client.get("/api/batches/C1").json()
    assert c1now["shipment_eligibility"]["status"] == "BLOCKED"
    assert c1now["shipment_eligibility"]["version"] == 2


def test_lock_bumps_even_when_still_blocked_by_another_lot(client):
    # B1 同时使用两个批号；锁定/解除其中一个时资格版本每次实际变化都递增，
    # 即使 B1 因另一批号仍处于 BLOCKED。
    add_lot(client, "RM-A", "原料A")
    add_lot(client, "RM-B", "原料B")
    add_batch(client, "B1", lots=["RM-A", "RM-B"], declared=[])

    lock(client, "RM-A", "A 风险")
    assert client.get("/api/batches/B1").json()["shipment_eligibility"]["version"] == 2
    lock(client, "RM-B", "B 风险")
    body = client.get("/api/batches/B1").json()["shipment_eligibility"]
    assert body["version"] == 3
    assert body["status"] == "BLOCKED"
    assert body["locked_lots"] == ["RM-A", "RM-B"]

    # 解除 A：仍因 B 而 BLOCKED，但版本继续递增
    unlock(client, "RM-A", "A 解除")
    body = client.get("/api/batches/B1").json()["shipment_eligibility"]
    assert body["version"] == 4
    assert body["status"] == "BLOCKED"
    assert body["locked_lots"] == ["RM-B"]

    unlock(client, "RM-B", "B 解除")
    body = client.get("/api/batches/B1").json()["shipment_eligibility"]
    assert body["version"] == 5
    assert body["status"] == "SHIPPABLE"
    assert body["locked_lots"] == []

    # 重复解除不产生新版本
    assert unlock(client, "RM-B", "重复").json()["changed"] is False
    assert client.get("/api/batches/B1").json()["shipment_eligibility"]["version"] == 5

    hist = client.get("/api/batches/B1/eligibility").json()["history"]
    assert [v["version"] for v in hist] == [1, 2, 3, 4, 5]
    assert [v["trigger"] for v in hist] == [
        "REGISTER", "LOT_LOCK", "LOT_LOCK", "LOT_UNLOCK", "LOT_UNLOCK",
    ]


def test_new_batch_takes_current_lock_state(client):
    add_lot(client, "RM-P", "花生酱", ["peanut"])
    add_batch(client, "B1", lots=["RM-P"], declared=["peanut"])
    lock(client, "RM-P", "待检")

    # 锁定期间新登记、直接使用该批号：初始资格即 BLOCKED（v1）
    b2 = add_batch(client, "B2", lots=["RM-P"], declared=["peanut"])
    assert b2["shipment_eligibility"]["version"] == 1
    assert b2["shipment_eligibility"]["status"] == "BLOCKED"

    # 未直接使用的新批：SHIPPABLE
    b3 = add_batch(client, "B3", lots=[], declared=[])
    assert b3["shipment_eligibility"]["status"] == "SHIPPABLE"

    unlock(client, "RM-P", "解除")
    # 只有直接使用的 B1/B2 递增；B3 不递增
    assert client.get("/api/batches/B1").json()["shipment_eligibility"]["version"] == 3
    assert client.get("/api/batches/B2").json()["shipment_eligibility"]["version"] == 2
    assert client.get("/api/batches/B3").json()["shipment_eligibility"]["version"] == 1


def test_eligibility_query_endpoints_and_filters(client):
    add_lot(client, "RM-P", "花生酱", ["peanut"])
    add_batch(client, "B1", "L1", lots=["RM-P"], declared=["peanut"])
    add_batch(client, "B2", "L1", lots=[], declared=[])
    add_batch(client, "C1", "L2", lots=["RM-P"], declared=["peanut"])
    lock(client, "RM-P", "待检")

    body = client.get("/api/batches/B1/eligibility").json()
    assert body["batch_no"] == "B1"
    assert body["eligibility"]["status"] == "BLOCKED"
    assert [v["version"] for v in body["history"]] == [1, 2]

    blocked = client.get("/api/eligibilities", params={"status": "BLOCKED"}).json()
    assert [e["batch_no"] for e in blocked["eligibilities"]] == ["B1", "C1"]

    line = client.get("/api/eligibilities", params={"line_id": "L1"}).json()
    assert [e["batch_no"] for e in line["eligibilities"]] == ["B1", "B2"]

    shippable = client.get("/api/eligibilities", params={"status": "SHIPPABLE"}).json()
    assert [e["batch_no"] for e in shippable["eligibilities"]] == ["B2"]

    assert client.get("/api/eligibilities", params={"status": "BOGUS"}).status_code == 400
    assert client.get("/api/batches/NOPE/eligibility").status_code == 404


def test_lock_does_not_change_allergen_adjudication_history(client):
    add_lot(client, "RM-P", "花生酱", ["peanut"])
    add_batch(client, "B1", lots=["RM-P"], declared=["peanut"])
    add_batch(client, "B2", lots=[], declared=[])  # 无残留（B1 清洁 peanut 由下批决定）
    lock(client, "RM-P", "待检")
    unlock(client, "RM-P", "解除")

    # 裁决历史始终只有登记一条；出货资格历史独立递增
    hist = client.get("/api/batches/B1/history").json()["history"]
    assert [v["version"] for v in hist] == [1]
    assert hist[0]["trigger"] == "REGISTER"


# ---------------------------------------------------------------- 更正用料


def test_correct_materials_changing_eligibility_bumps_version(client):
    add_lot(client, "RM-A", "原料A")
    add_lot(client, "RM-B", "原料B")
    add_batch(client, "B1", lots=["RM-A"], declared=[])
    lock(client, "RM-B", "B 待检")

    # 更正用料加入被锁定的 RM-B：资格 SHIPPABLE->BLOCKED，版本递增
    resp = client.put("/api/batches/B1", json={"material_lots": ["RM-A", "RM-B"]})
    assert resp.status_code == 200
    body = resp.json()
    assert body["shipment_eligibility"]["changed"] is True
    assert body["shipment_eligibility"]["status"] == "BLOCKED"
    assert body["shipment_eligibility"]["version"] == 2
    se = client.get("/api/batches/B1").json()["shipment_eligibility"]
    assert se["version"] == 2
    assert se["locked_lots"] == ["RM-B"]

    # 再次更正但资格不变（仍是 RM-A+RM-B）：出货资格不递增
    resp = client.put("/api/batches/B1", json={"declared_allergens": ["milk"]})
    assert resp.status_code == 200
    assert resp.json()["shipment_eligibility"]["changed"] is False
    assert client.get("/api/batches/B1").json()["shipment_eligibility"]["version"] == 2

    # 移除锁定批号：BLOCKED->SHIPPABLE，再次递增
    resp = client.put("/api/batches/B1", json={"material_lots": ["RM-A"]})
    assert resp.json()["shipment_eligibility"]["changed"] is True
    assert client.get("/api/batches/B1").json()["shipment_eligibility"]["version"] == 3


def test_correct_materials_with_unknown_lot_rejected(client):
    add_lot(client, "RM-A", "原料A")
    add_batch(client, "B1", lots=["RM-A"], declared=[])
    resp = client.put("/api/batches/B1", json={"material_lots": ["NOPE"]})
    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == "UNKNOWN_MATERIAL_LOT"


# ---------------------------------------------------------------- 出货核验单


def test_verification_snapshot_includes_shipment_eligibility(client):
    add_lot(client, "RM-P", "花生酱", ["peanut"])
    add_batch(client, "B1", lots=["RM-P"], declared=["peanut"])
    create_verification(client, "V1", ["B1"])

    doc = client.get("/api/verifications/V1").json()
    entry = doc["batches"][0]
    assert entry["shipment_snapshot"]["version"] == 1
    assert entry["shipment_snapshot"]["status"] == "SHIPPABLE"
    assert entry["shipment_current"]["version"] == 1
    assert entry["shipment_changed"] is False
    assert entry["shipment_change_reasons"] == []


def test_confirm_blocked_when_lot_locked_even_if_release(client):
    add_lot(client, "RM-P", "花生酱", ["peanut"])
    add_batch(client, "B1", lots=["RM-P"], declared=["peanut"])  # 裁决 RELEASE
    create_verification(client, "V1", ["B1"])

    lock(client, "RM-P", "待检")
    resp = client.post("/api/verifications/V1/confirm")
    assert resp.status_code == 422
    details = resp.json()["error"]["details"]
    codes = {d["reason_code"] for d in details}
    assert codes == {"STALE_ELIGIBILITY", "MATERIAL_LOT_LOCKED"}
    stale = next(d for d in details if d["reason_code"] == "STALE_ELIGIBILITY")
    assert stale["snapshot_version"] == 1
    assert stale["current_version"] == 2
    assert stale["change_reasons"][0]["trigger"] == "LOT_LOCK"
    locked = next(d for d in details if d["reason_code"] == "MATERIAL_LOT_LOCKED")
    assert locked["locked_lots"] == ["RM-P"]
    # 整单不确认、无确认记录
    doc = client.get("/api/verifications/V1").json()
    assert doc["status"] == "DRAFT"
    assert doc["confirmations"] == []

    # 重新核验后快照为当前（BLOCKED）：版本不再陈旧，但仍因当前锁定被拒
    assert client.post("/api/verifications/V1/reverify").status_code == 200
    resp = client.post("/api/verifications/V1/confirm")
    details = resp.json()["error"]["details"]
    assert [d["reason_code"] for d in details] == ["MATERIAL_LOT_LOCKED"]

    # 解除后仍须重新核验（快照资格版本陈旧），再确认成功
    unlock(client, "RM-P", "解除")
    resp = client.post("/api/verifications/V1/confirm")
    assert resp.status_code == 422
    assert resp.json()["error"]["details"][0]["reason_code"] == "STALE_ELIGIBILITY"
    client.post("/api/verifications/V1/reverify")
    resp = client.post("/api/verifications/V1/confirm")
    assert resp.status_code == 200
    assert resp.json()["status"] == "CONFIRMED"


def test_confirmed_verification_pending_review_on_eligibility_change(client):
    add_lot(client, "RM-P", "花生酱", ["peanut"])
    add_batch(client, "B1", lots=["RM-P"], declared=["peanut"])
    add_batch(client, "B2", lots=[], declared=[], cleaned=["peanut"])  # 同线不直接使用，已清残留
    create_verification(client, "V1", ["B1", "B2"])
    assert client.post("/api/verifications/V1/confirm").status_code == 200

    # 锁定 RM-P：B1 出货资格版本递增 -> 已确认单据转待复核；
    # B2 未直接使用，不递增、不连带，但整单仍因 B1 待复核
    resp = lock(client, "RM-P", "待检")
    affected = {v["verification_no"]: v["status"] for v in resp.json()["affected_verifications"]}
    assert affected == {"V1": "PENDING_REVIEW"}
    assert client.get("/api/verifications/V1").json()["status"] == "PENDING_REVIEW"

    # 待复核直接确认被拒，逐批原因：B1 陈旧+锁定；B2 无问题（不在 details）
    resp = client.post("/api/verifications/V1/confirm")
    assert resp.status_code == 422
    details = resp.json()["error"]["details"]
    assert {d["batch_no"] for d in details} == {"B1"}

    # 解除、重新核验后再确认；历次确认快照保留
    unlock(client, "RM-P", "解除")
    client.post("/api/verifications/V1/reverify")
    resp = client.post("/api/verifications/V1/confirm")
    assert resp.status_code == 200
    doc = resp.json()
    assert len(doc["confirmations"]) == 2
    first_batches = {b["batch_no"]: b for b in doc["confirmations"][0]["batches"]}
    assert first_batches["B1"]["shipment_eligibility"]["version"] == 1
    second_batches = {b["batch_no"]: b for b in doc["confirmations"][1]["batches"]}
    assert second_batches["B1"]["shipment_eligibility"]["version"] == 3


def test_confirm_checks_three_dimensions_and_returns_all(client):
    add_lot(client, "RM-P", "花生酱", ["peanut"])
    add_batch(client, "B1", lots=["RM-P"], declared=["peanut"])   # RELEASE
    add_batch(client, "B2", lots=["RM-P"], declared=[])           # HOLD
    create_verification(client, "V1", ["B1", "B2"])

    lock(client, "RM-P", "待检")
    # 同时让 B2 的声明变化产生新裁决版本（快照进一步陈旧）
    client.put("/api/batches/B2", json={"declared_allergens": ["peanut"]})

    resp = client.post("/api/verifications/V1/confirm")
    assert resp.status_code == 422
    by_batch: dict[str, list[str]] = {}
    for d in resp.json()["error"]["details"]:
        by_batch.setdefault(d["batch_no"], []).append(d["reason_code"])
    # B1：出货资格陈旧 + 当前锁定
    assert by_batch["B1"] == ["STALE_ELIGIBILITY", "MATERIAL_LOT_LOCKED"]
    # B2：裁决版本陈旧（v1->v2）
    assert by_batch["B2"] == ["STALE_SNAPSHOT", "STALE_ELIGIBILITY", "MATERIAL_LOT_LOCKED"]
    assert client.get("/api/verifications/V1").json()["status"] == "DRAFT"


def test_draft_verification_not_flipped_by_lock(client):
    add_lot(client, "RM-P", "花生酱", ["peanut"])
    add_batch(client, "B1", lots=["RM-P"], declared=["peanut"])
    create_verification(client, "VD", ["B1"])   # 草稿
    create_verification(client, "VC", ["B1"])
    client.post("/api/verifications/VC/confirm")

    resp = lock(client, "RM-P", "待检")
    affected = {v["verification_no"]: v["status"] for v in resp.json()["affected_verifications"]}
    assert affected == {"VD": "DRAFT", "VC": "PENDING_REVIEW"}


# ---------------------------------------------------------------- 旧库升级


def test_database_upgrade_backfills_eligibility(tmp_path):
    """旧版本库（无锁定表/无出货资格列）启动后自动升级并补齐资格。"""
    db_path = tmp_path / "old.db"
    conn = sqlite3.connect(db_path)
    conn.executescript(
        """
        CREATE TABLE allergen (code TEXT PRIMARY KEY, name_zh TEXT NOT NULL);
        CREATE TABLE material_lot (lot_no TEXT PRIMARY KEY, material TEXT NOT NULL,
            allergens TEXT NOT NULL DEFAULT '[]', updated_at TEXT NOT NULL);
        CREATE TABLE line (line_id TEXT PRIMARY KEY, created_at TEXT NOT NULL);
        CREATE TABLE batch (batch_no TEXT PRIMARY KEY, line_id TEXT NOT NULL REFERENCES line(line_id),
            seq INTEGER NOT NULL, material_lots TEXT NOT NULL DEFAULT '[]',
            declared TEXT NOT NULL DEFAULT '[]', cleaned TEXT NOT NULL DEFAULT '[]',
            created_at TEXT NOT NULL, UNIQUE(line_id, seq));
        CREATE TABLE batch_material (batch_no TEXT NOT NULL, lot_no TEXT NOT NULL,
            PRIMARY KEY (batch_no, lot_no));
        CREATE TABLE adjudication (batch_no TEXT PRIMARY KEY, version INTEGER NOT NULL DEFAULT 0,
            actual TEXT NOT NULL DEFAULT '[]', undeclared TEXT NOT NULL DEFAULT '[]',
            disposition TEXT NOT NULL, reasons TEXT NOT NULL DEFAULT '[]',
            sources TEXT NOT NULL DEFAULT '{}', decided_at TEXT NOT NULL);
        CREATE TABLE adjudication_version (id INTEGER PRIMARY KEY AUTOINCREMENT,
            batch_no TEXT NOT NULL, line_id TEXT NOT NULL, seq INTEGER NOT NULL,
            version INTEGER NOT NULL, actual TEXT NOT NULL DEFAULT '[]',
            undeclared TEXT NOT NULL DEFAULT '[]', disposition TEXT NOT NULL,
            reasons TEXT NOT NULL DEFAULT '[]', sources TEXT NOT NULL DEFAULT '{}',
            trigger TEXT NOT NULL, decided_at TEXT NOT NULL);
        CREATE TABLE verification (verification_no TEXT PRIMARY KEY, status TEXT NOT NULL,
            created_at TEXT NOT NULL, updated_at TEXT NOT NULL, confirmed_at TEXT);
        CREATE TABLE verification_batch (verification_no TEXT NOT NULL, batch_no TEXT NOT NULL,
            position INTEGER NOT NULL, snapshot_version INTEGER NOT NULL,
            snapshot_disposition TEXT NOT NULL, snapshot_actual TEXT NOT NULL DEFAULT '[]',
            snapshot_undeclared TEXT NOT NULL DEFAULT '[]',
            snapshot_reasons TEXT NOT NULL DEFAULT '[]',
            PRIMARY KEY (verification_no, batch_no));
        CREATE TABLE verification_confirmation (id INTEGER PRIMARY KEY AUTOINCREMENT,
            verification_no TEXT NOT NULL, confirmed_at TEXT NOT NULL);
        CREATE TABLE verification_confirmation_batch (confirmation_id INTEGER NOT NULL,
            verification_no TEXT NOT NULL, position INTEGER NOT NULL, batch_no TEXT NOT NULL,
            snapshot_version INTEGER NOT NULL, snapshot_disposition TEXT NOT NULL,
            snapshot_actual TEXT NOT NULL DEFAULT '[]', snapshot_undeclared TEXT NOT NULL DEFAULT '[]',
            snapshot_reasons TEXT NOT NULL DEFAULT '[]');
        INSERT INTO allergen(code, name_zh) VALUES ('milk', '乳');
        INSERT INTO material_lot VALUES ('RM-M', '奶粉', '[]', 't');
        INSERT INTO line VALUES ('L1', 't');
        INSERT INTO batch VALUES ('B1', 'L1', 1, '["RM-M"]', '[]', '[]', 't');
        INSERT INTO batch_material VALUES ('B1', 'RM-M');
        INSERT INTO adjudication VALUES ('B1', 1, '[]', '[]', 'RELEASE', '[]', '{}', 't');
        INSERT INTO verification VALUES ('V1', 'CONFIRMED', 't', 't', 't');
        INSERT INTO verification_batch VALUES ('V1', 'B1', 1, 1, 'RELEASE', '[]', '[]', '[]');
        INSERT INTO verification_confirmation VALUES (1, 'V1', 't');
        INSERT INTO verification_confirmation_batch VALUES (1, 'V1', 1, 'B1', 1, 'RELEASE', '[]', '[]', '[]');
        """
    )
    conn.commit()
    conn.close()

    app = create_app(db_path=str(db_path), seed_demo=False)
    with TestClient(app) as c:
        # 既有批次补齐 v1 出货资格（SHIPPABLE：无锁定记录）
        se = c.get("/api/batches/B1").json()["shipment_eligibility"]
        assert se["version"] == 1
        assert se["status"] == "SHIPPABLE"
        hist = c.get("/api/batches/B1/eligibility").json()["history"]
        assert hist[0]["trigger"] == "UPGRADE_INIT"

        # 旧确认快照的出货资格版本为 NULL：确认要求先重新核验
        resp = c.post("/api/verifications/V1/confirm")
        assert resp.status_code == 409  # 仍是 CONFIRMED（升级不改变单据状态）
        # 锁定使已确认单据转待复核
        c.post("/api/material-lots/RM-M/lock", json={"reason": "待检"})
        assert c.get("/api/verifications/V1").json()["status"] == "PENDING_REVIEW"
        resp = c.post("/api/verifications/V1/confirm")
        assert resp.status_code == 422
        codes = {d["reason_code"] for d in resp.json()["error"]["details"]}
        assert "STALE_ELIGIBILITY" in codes
        assert "MATERIAL_LOT_LOCKED" in codes

        c.post("/api/material-lots/RM-M/unlock", json={"reason": "解除"})
        c.post("/api/verifications/V1/reverify")
        assert c.post("/api/verifications/V1/confirm").status_code == 200
        # 历次确认快照保留（旧快照 shipment_eligibility 为 null）
        doc = c.get("/api/verifications/V1").json()
        assert len(doc["confirmations"]) == 2
        assert doc["confirmations"][0]["batches"][0]["shipment_eligibility"] is None

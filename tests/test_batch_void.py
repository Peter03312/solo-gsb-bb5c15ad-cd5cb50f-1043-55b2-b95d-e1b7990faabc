"""批次作废测试。

覆盖：
- 作废：未知批次 404、空原因 400 且不留部分版本；作废保留原登记/历史/资格与原因可查；
- 批次号不可复用；作废批次不得更正（批次/清洁两种入口）；
- 作废批次退出同线残留链：其后有效批次按原顺序重算追加版本（版本即使结论不变也追加），
  作废批次本身不产生新版本；新登记批次 seq 顺延且残留来自最近有效批次；
- 出货资格：作废批次不再参与当前资格统计（/api/eligibilities）与批号锁定联动，
  但既有资格记录冻结可查（单批 /eligibility 可查）；
- 核验单：新核验单含作废批次拒绝；涉及作废批次或被重算批次的已确认单据转待复核，
  历次确认快照不改；含作废批次的单据可查但重新核验与确认均拒绝（409）；
- 幂等：重复作废返回既有结果、changed=false、不重算；
- 原料批号更正的重算只覆盖有效批次，作废批次历史冻结；
- 查询：批次列表/详情/历史、核验单列表/详情均标明作废状态；有效批次接口保持兼容。
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


def void(client, batch_no, reason):
    return client.post(f"/api/batches/{batch_no}/void", json={"reason": reason})


def create_verification(client, verification_no, batch_nos):
    return client.post(
        "/api/verifications",
        json={"verification_no": verification_no, "batch_nos": batch_nos},
    )


# ---------------------------------------------------------------- 基础作废


def test_void_basic_marks_batch_and_keeps_data(client):
    add_lot(client, "RM-P", "花生酱", ["peanut"])
    add_batch(client, "B1", lots=["RM-P"], declared=["peanut"])

    resp = void(client, "B1", "误登记，该批实际未投产")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["changed"] is True
    v = body["voided"]
    assert v["batch_no"] == "B1"
    assert v["status"] == "VOIDED"
    assert v["voided"] is True
    assert v["voided_reason"] == "误登记，该批实际未投产"
    assert v["voided_at"]
    # 原登记与裁决保留
    assert v["material_lots"] == ["RM-P"]
    assert v["adjudication"]["disposition"] == "RELEASE"
    assert v["shipment_eligibility"]["version"] == 1
    assert body["recomputed"] == []  # 最后一批：无后续可重算

    # 详情与列表均标明作废
    detail = client.get("/api/batches/B1").json()
    assert detail["voided"] is True and detail["status"] == "VOIDED"
    listing = {b["batch_no"]: b for b in client.get("/api/batches").json()["batches"]}
    assert listing["B1"]["voided"] is True


def test_void_rejects_unknown_batch_and_empty_reason(client):
    add_batch(client, "B1")
    for bad in ("", "   "):
        resp = void(client, "B1", bad)
        assert resp.status_code == 400
        assert resp.json()["error"]["code"] == "INVALID_PAYLOAD"
    resp = void(client, "NOPE", "原因")
    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "BATCH_NOT_FOUND"
    # 失败后批次仍有效
    assert client.get("/api/batches/B1").json()["status"] == "ACTIVE"


def test_failed_void_is_atomic_no_partial_versions(client):
    add_lot(client, "RM-P", "花生酱", ["peanut"])
    add_batch(client, "B1", lots=["RM-P"], declared=["peanut"])
    add_batch(client, "B2", lots=[], declared=[])  # 继承 peanut，HOLD

    v_before = client.get("/api/batches/B2").json()["adjudication"]["version"]
    # 空原因：作废标记与重算都不得落库
    resp = void(client, "B2", " ")
    assert resp.status_code == 400
    b2 = client.get("/api/batches/B2").json()
    assert b2["status"] == "ACTIVE"
    assert b2["adjudication"]["version"] == v_before
    assert len(client.get("/api/batches/B2/history").json()["history"]) == v_before


def test_voided_batch_number_cannot_be_reused(client):
    add_batch(client, "B1")
    assert void(client, "B1", "误登记").status_code == 200
    resp = client.post(
        "/api/batches",
        json={
            "batch_no": "B1", "line_id": "L1",
            "material_lots": [], "declared_allergens": [], "cleaned_allergens": [],
        },
    )
    assert resp.status_code == 409
    assert resp.json()["error"]["code"] == "DUPLICATE_BATCH"


def test_voided_batch_cannot_be_corrected(client):
    add_lot(client, "RM-M", "奶粉", ["milk"])
    add_batch(client, "B1", lots=["RM-M"], declared=["milk"])
    assert void(client, "B1", "误登记").status_code == 200

    for method, path, payload in [
        ("PUT", "/api/batches/B1", {"declared_allergens": []}),
        ("PUT", "/api/batches/B1/cleaning", {"cleaned_allergens": ["milk"]}),
    ]:
        resp = client.request(method, path, json=payload)
        assert resp.status_code == 409, resp.text
        assert resp.json()["error"]["code"] == "BATCH_VOIDED"

    # 原登记未被修改
    b1 = client.get("/api/batches/B1").json()
    assert b1["declared_allergens"] == ["milk"]


def test_repeated_void_is_idempotent_without_recompute(client):
    add_lot(client, "RM-P", "花生酱", ["peanut"])
    add_batch(client, "B1", lots=["RM-P"], declared=["peanut"])
    add_batch(client, "B2", lots=[], declared=[], cleaned=["peanut"])
    add_batch(client, "B3", lots=[], declared=[])

    # 作废最后一批 B3：无后续重算
    r1 = void(client, "B3", "首次作废原因")
    assert r1.status_code == 200 and r1.json()["changed"] is True
    assert r1.json()["recomputed"] == []

    r2 = void(client, "B3", "重复作废的另一个原因")
    assert r2.status_code == 200
    body = r2.json()
    assert body["changed"] is False
    assert body["reason"] == "首次作废原因"  # 保留首次原因
    assert body["recomputed"] == []          # 不重算
    assert body["voided"]["voided_reason"] == "首次作废原因"

    # 不产生任何新版本：B1/B2 裁决版本保持 1，B3 历史仍只有 1 条
    assert client.get("/api/batches/B1").json()["adjudication"]["version"] == 1
    assert client.get("/api/batches/B2").json()["adjudication"]["version"] == 1
    assert len(client.get("/api/batches/B3/history").json()["history"]) == 1


# ---------------------------------------------------------------- 残留传递与重算


def test_void_removes_batch_from_carryover_and_recomputes_following(client):
    add_lot(client, "RM-P", "花生酱", ["peanut"])
    # B1 声明 peanut：RELEASE，但产线残留 peanut
    add_batch(client, "B1", lots=["RM-P"], declared=["peanut"])
    # B2 开工前清洁 peanut、无原料：RELEASE（产线被清洁干净）
    add_batch(client, "B2", lots=[], declared=[], cleaned=["peanut"])
    # B3/B4 无原料无清洁：无残留继承，RELEASE
    add_batch(client, "B3", lots=[], declared=[])
    add_batch(client, "B4", lots=[], declared=[])

    # 作废 B2（其清洁不再发生）：B3 重新从 B1 继承 peanut -> HOLD；B4 连带 HOLD
    resp = void(client, "B2", "误登记未投产")
    rec = {s["batch_no"]: s for s in resp.json()["recomputed"]}
    assert list(rec) == ["B3", "B4"]
    assert rec["B3"]["actual"] == ["peanut"]
    assert rec["B3"]["disposition"] == "HOLD"
    assert rec["B3"]["version"] == 2
    assert rec["B4"]["disposition"] == "HOLD" and rec["B4"]["version"] == 2
    assert all(s["reasons"] for s in rec.values())

    # 作废批次本身不产生新裁决版本
    b2 = client.get("/api/batches/B2").json()
    assert b2["adjudication"]["version"] == 1
    hist = client.get("/api/batches/B2/history").json()
    assert hist["voided"] is True and [v["version"] for v in hist["history"]] == [1]
    # 后续批次历史追加 VOID_BATCH 触发的版本
    h3 = client.get("/api/batches/B3/history").json()["history"]
    assert [v["trigger"] for v in h3] == ["REGISTER", "VOID_BATCH"]


def test_void_appends_version_even_when_disposition_unchanged(client):
    add_lot(client, "RM-P", "花生酱", ["peanut"])
    add_batch(client, "B1", lots=["RM-P"], declared=["peanut"])  # RELEASE，残留 peanut
    add_batch(client, "B2", lots=[], declared=[])                # 继承 peanut，HOLD
    add_batch(client, "B3", lots=[], declared=[])                # HOLD

    # 作废中间的 HOLD 批 B2：B3 仍从 B1 继承 peanut，结论仍 HOLD，但版本必须追加
    resp = void(client, "B2", "误登记")
    rec = {s["batch_no"]: s for s in resp.json()["recomputed"]}
    assert list(rec) == ["B3"]
    assert rec["B3"]["disposition"] == "HOLD"
    assert rec["B3"]["version"] == 2


def test_new_batch_after_void_skips_voided_for_carryover_and_seq(client):
    add_lot(client, "RM-P", "花生酱", ["peanut"])
    add_batch(client, "B1", lots=["RM-P"], declared=["peanut"])  # 残留 peanut
    add_batch(client, "B2", lots=[], declared=[])                # HOLD
    void(client, "B2", "误登记")

    # 新批 seq 顺延（B2 的 seq=2 不复用），残留直接看最近有效批 B1
    b3 = add_batch(client, "B3", lots=[], declared=[])
    assert b3["seq"] == 3
    assert b3["adjudication"]["actual_allergens"] == ["peanut"]
    assert b3["adjudication"]["disposition"] == "HOLD"


def test_void_does_not_affect_other_lines(client):
    add_lot(client, "RM-P", "花生酱", ["peanut"])
    add_batch(client, "L1a", line_id="L1", lots=["RM-P"], declared=["peanut"])
    add_batch(client, "L1b", line_id="L1", lots=[], declared=[])
    add_batch(client, "L2a", line_id="L2", lots=[], declared=[])

    resp = void(client, "L1a", "误登记")
    rec = {s["batch_no"] for s in resp.json()["recomputed"]}
    assert rec == {"L1b"}
    # L2 完全不受影响，无新版本
    l2 = client.get("/api/batches/L2a").json()
    assert l2["adjudication"]["version"] == 1
    assert l2["adjudication"]["disposition"] == "RELEASE"


# ---------------------------------------------------------------- 出货资格


def test_voided_batch_excluded_from_eligibility_stats_but_queryable(client):
    add_lot(client, "RM-P", "花生酱", ["peanut"])
    add_batch(client, "B1", lots=["RM-P"], declared=["peanut"])
    add_batch(client, "B2", lots=[], declared=[])
    void(client, "B1", "误登记")

    # 当前资格统计不含作废批次
    rows = client.get("/api/eligibilities").json()["eligibilities"]
    assert [e["batch_no"] for e in rows] == ["B2"]
    # 过滤依然有效
    assert client.get("/api/eligibilities", params={"status": "SHIPPABLE"}).json()["eligibilities"]

    # 单批资格仍可查，记录冻结且标明作废
    e1 = client.get("/api/batches/B1/eligibility").json()
    assert e1["voided"] is True and e1["status"] == "VOIDED"
    assert e1["eligibility"]["version"] == 1
    assert [v["version"] for v in e1["history"]] == [1]


def test_lot_lock_does_not_advance_voided_batch_eligibility(client):
    add_lot(client, "RM-P", "花生酱", ["peanut"])
    add_batch(client, "B1", lots=["RM-P"], declared=["peanut"])
    add_batch(client, "B2", lots=["RM-P"], declared=["peanut"])
    void(client, "B1", "误登记")

    resp = client.post("/api/material-lots/RM-P/lock", json={"reason": "待检风险"})
    affected = {a["batch_no"] for a in resp.json()["affected_batches"]}
    assert affected == {"B2"}  # 作废的 B1 不联动
    assert client.get("/api/batches/B1").json()["shipment_eligibility"]["version"] == 1
    assert client.get("/api/batches/B2").json()["shipment_eligibility"]["version"] == 2


# ---------------------------------------------------------------- 核验单


def test_new_verification_rejects_voided_batch(client):
    add_batch(client, "B1")
    void(client, "B1", "误登记")

    resp = create_verification(client, "V1", ["B1"])
    assert resp.status_code == 400
    err = resp.json()["error"]
    assert err["code"] == "BATCH_VOIDED"
    assert err["details"][0]["batch_no"] == "B1"
    assert client.get("/api/verifications/V1").status_code == 404

    # 与未知批次同时出现：未知批次优先报错（整体拒绝，不留单据）
    resp = create_verification(client, "V2", ["B1", "NOPE"])
    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == "UNKNOWN_BATCH"


def test_confirmed_verification_flips_on_void_and_keeps_snapshots(client):
    add_lot(client, "RM-P", "花生酱", ["peanut"])
    add_batch(client, "B1", lots=["RM-P"], declared=["peanut"])
    add_batch(client, "B2", lots=[], declared=[], cleaned=["peanut"])
    create_verification(client, "V1", ["B1", "B2"])
    assert client.post("/api/verifications/V1/confirm").status_code == 200

    # 作废 B1：含作废批次的已确认单据转待复核；作废响应列出受影响单据
    resp = void(client, "B1", "误登记未投产")
    affected = {v["verification_no"]: v["status"] for v in resp.json()["affected_verifications"]}
    assert affected == {"V1": "PENDING_REVIEW"}

    doc = client.get("/api/verifications/V1").json()
    assert doc["status"] == "PENDING_REVIEW"
    assert doc["contains_voided_batches"] is True
    assert doc["voided_batches"] == ["B1"]
    entry = {b["batch_no"]: b for b in doc["batches"]}
    assert entry["B1"]["voided"] is True
    assert entry["B1"]["voided_reason"] == "误登记未投产"
    assert entry["B2"]["voided"] is False
    # 历次确认快照不改
    assert len(doc["confirmations"]) == 1
    snap = {b["batch_no"]: b for b in doc["confirmations"][0]["batches"]}
    assert snap["B1"]["version"] == 1


def test_voided_batch_carries_recompute_flips_confirmed_doc(client):
    add_lot(client, "RM-P", "花生酱", ["peanut"])
    add_batch(client, "B1", lots=["RM-P"], declared=["peanut"])  # 残留 peanut
    add_batch(client, "B2", lots=[], declared=[], cleaned=["peanut"])  # 清干净，RELEASE
    create_verification(client, "V1", ["B2"])
    assert client.post("/api/verifications/V1/confirm").status_code == 200

    # 作废 B2：其后无重算批次，但单据所含的 B2 自身作废 -> 转待复核
    assert void(client, "B2", "误登记").status_code == 200
    assert client.get("/api/verifications/V1").json()["status"] == "PENDING_REVIEW"


def test_voided_batch_recompute_flips_doc_through_downstream_batch(client):
    add_lot(client, "RM-P", "花生酱", ["peanut"])
    add_batch(client, "B1", lots=["RM-P"], declared=["peanut"])
    add_batch(client, "B2", lots=[], declared=[], cleaned=["peanut"])
    add_batch(client, "B3", lots=[], declared=[])  # 产线干净，RELEASE
    create_verification(client, "V1", ["B3"])
    assert client.post("/api/verifications/V1/confirm").status_code == 200

    # 作废 B2：B3 重算追加版本（变为 HOLD）-> 已确认单据转待复核
    resp = void(client, "B2", "误登记")
    affected = {v["verification_no"]: v["status"] for v in resp.json()["affected_verifications"]}
    assert affected == {"V1": "PENDING_REVIEW"}
    doc = client.get("/api/verifications/V1").json()
    # 单据不含作废批次，标记为 false；逐批变化原因可见 VOID_BATCH
    assert doc["contains_voided_batches"] is False
    entry = doc["batches"][0]
    assert entry["current"]["version"] == 2
    assert entry["change_reasons"][0]["trigger"] == "VOID_BATCH"


def test_voided_batch_document_reverify_and_confirm_rejected(client):
    add_lot(client, "RM-P", "花生酱", ["peanut"])
    add_batch(client, "B1", lots=["RM-P"], declared=["peanut"])
    create_verification(client, "VD", ["B1"])   # 草稿含作废批次
    create_verification(client, "VC", ["B1"])
    client.post("/api/verifications/VC/confirm")
    void(client, "B1", "误登记")
    # VC 转待复核；VD 保持草稿
    assert client.get("/api/verifications/VD").json()["status"] == "DRAFT"
    assert client.get("/api/verifications/VC").json()["status"] == "PENDING_REVIEW"

    for no in ("VD", "VC"):
        for action in ("reverify", "confirm"):
            resp = client.post(f"/api/verifications/{no}/{action}")
            assert resp.status_code == 409, (no, action, resp.text)
            assert resp.json()["error"]["code"] == "VERIFICATION_CONTAINS_VOIDED_BATCH"
            assert resp.json()["error"]["details"][0]["batch_no"] == "B1"

    # 单据仍可查询；状态不被失败操作改变
    assert client.get("/api/verifications/VD").json()["status"] == "DRAFT"
    assert client.get("/api/verifications/VC").json()["status"] == "PENDING_REVIEW"
    # 草稿单也不含新确认记录
    assert client.get("/api/verifications/VD").json()["confirmations"] == []


def test_draft_doc_not_flipped_but_marked_when_batch_voided(client):
    add_batch(client, "B1")
    create_verification(client, "VD", ["B1"])  # 草稿
    void(client, "B1", "误登记")
    doc = client.get("/api/verifications/VD").json()
    assert doc["status"] == "DRAFT"  # 草稿不转态
    assert doc["contains_voided_batches"] is True


def test_void_flips_all_confirmed_docs_involved(client):
    add_lot(client, "RM-P", "花生酱", ["peanut"])
    add_batch(client, "B1", lots=["RM-P"], declared=["peanut"])
    add_batch(client, "B2", lots=[], declared=[], cleaned=["peanut"])
    add_batch(client, "B3", lots=[], declared=[])
    # 一张单据含作废批次本身，一张单据只含被重算的下游批次
    create_verification(client, "VSELF", ["B2"])
    create_verification(client, "VDOWN", ["B3"])
    client.post("/api/verifications/VSELF/confirm")
    client.post("/api/verifications/VDOWN/confirm")

    resp = void(client, "B2", "误登记")
    affected = {v["verification_no"]: v["status"] for v in resp.json()["affected_verifications"]}
    assert affected == {"VSELF": "PENDING_REVIEW", "VDOWN": "PENDING_REVIEW"}


def test_void_freezes_blocked_eligibility_when_lot_locked(client):
    add_lot(client, "RM-P", "花生酱", ["peanut"])
    add_batch(client, "B1", lots=["RM-P"], declared=["peanut"])
    # 先锁定：B1 出货资格 BLOCKED v2
    client.post("/api/material-lots/RM-P/lock", json={"reason": "待检"})
    assert client.get("/api/batches/B1").json()["shipment_eligibility"]["version"] == 2

    # 锁定状态作废：资格记录冻结保留，不随作废变化
    assert void(client, "B1", "误登记").status_code == 200
    se = client.get("/api/batches/B1").json()["shipment_eligibility"]
    assert se["version"] == 2 and se["status"] == "BLOCKED"
    # 解除锁定也不推进作废批次
    client.post("/api/material-lots/RM-P/unlock", json={"reason": "解除"})
    assert client.get("/api/batches/B1").json()["shipment_eligibility"]["version"] == 2
    assert client.get("/api/batches/B1").json()["voided"] is True


# ---------------------------------------------------------------- 原料批号更正


def test_material_lot_correction_skips_voided_batches(client):
    add_lot(client, "RM-P", "花生酱", ["peanut"])
    add_batch(client, "B1", lots=["RM-P"], declared=["peanut"])
    add_batch(client, "B2", lots=["RM-P"], declared=["peanut"])
    void(client, "B1", "误登记")

    resp = client.put("/api/material-lots/RM-P", json={"allergens": ["peanut", "sesame"]})
    rec = resp.json()["recomputed"]
    # 仅有效批次 B2 重算；作废的 B1 历史冻结
    assert [s["batch_no"] for s in rec] == ["B2"]
    b1 = client.get("/api/batches/B1").json()
    assert b1["adjudication"]["version"] == 1
    assert [v["trigger"] for v in client.get("/api/batches/B1/history").json()["history"]] == ["REGISTER"]
    # 作废记录中的实际过敏原仍是登记时的快照（可查）
    assert b1["adjudication"]["actual_allergens"] == ["peanut"]


# ---------------------------------------------------------------- 兼容性


def test_legacy_database_upgrades_with_void_columns(tmp_path):
    """旧版本库（无作废列）启动后自动升级：既有批次为 ACTIVE，作废功能可用。"""
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
        INSERT INTO allergen(code, name_zh) VALUES ('milk', '乳');
        INSERT INTO material_lot VALUES ('RM-M', '奶粉', '["milk"]', 't');
        INSERT INTO line VALUES ('L1', 't');
        INSERT INTO batch VALUES ('B1', 'L1', 1, '["RM-M"]', '["milk"]', '[]', 't');
        INSERT INTO batch_material VALUES ('B1', 'RM-M');
        INSERT INTO adjudication VALUES ('B1', 1, '["milk"]', '[]', 'RELEASE', '[]', '{}', 't');
        """
    )
    conn.commit()
    conn.close()

    app = create_app(db_path=str(db_path), seed_demo=False)
    with TestClient(app) as c:
        b1 = c.get("/api/batches/B1").json()
        assert b1["status"] == "ACTIVE" and b1["voided"] is False
        # 升级后作废功能立即可用
        resp = c.post("/api/batches/B1/void", json={"reason": "旧库批次作废"})
        assert resp.status_code == 200
        assert resp.json()["voided"]["voided"] is True
        assert c.get("/api/batches/B1").json()["voided_reason"] == "旧库批次作废"


def test_active_batch_payloads_remain_compatible(client):
    add_lot(client, "RM-M", "奶粉", ["milk"])
    body = add_batch(client, "B1", lots=["RM-M"], declared=["milk"])
    # 既有字段不变，仅新增作废标记
    assert body["adjudication"]["disposition"] == "RELEASE"
    assert body["shipment_eligibility"]["status"] == "SHIPPABLE"
    assert body["status"] == "ACTIVE" and body["voided"] is False
    assert body["voided_reason"] is None and body["voided_at"] is None

    h = client.get("/api/batches/B1/history").json()
    assert h["status"] == "ACTIVE" and h["voided"] is False

    create_verification(client, "V1", ["B1"])
    doc = client.get("/api/verifications/V1").json()
    assert doc["contains_voided_batches"] is False and doc["voided_batches"] == []
    assert doc["batches"][0]["status"] == "ACTIVE" and doc["batches"][0]["voided"] is False

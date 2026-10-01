"""出货核验单批次清单修订（revise）测试。

覆盖：
- 修订号：创建=1；重新核验/确认等单据快照写入均推进修订号，详情/列表回显；
- 草稿/待复核可凭所见修订号提交完整、有序的新批次清单：同一事务内重取每批
  当前裁决与出货资格快照，单据回到草稿并推进修订号；
- 允许借修订移除旧清单中的已作废批次；新清单批次必须全部有效；
- 历次确认及其当时的逐批快照始终保留，即使旧批次已不在现行清单；
- 已确认单据不可直接修订；
- 整体拒绝：空清单、重复批次、未知批次、含已作废批次、过期修订号——
  当前成员与快照不变；
- 并发的批次更正/批号锁定不得使单据混入不同时间的快照（同一写事务内重取）；
- 旧库升级：verification.revision 列幂等补齐，既有单据按 v1 继续工作。
"""
from __future__ import annotations

import sqlite3
import threading
from contextlib import contextmanager

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


def revise(client, verification_no, expected_revision, batch_nos):
    return client.post(
        f"/api/verifications/{verification_no}/revise",
        json={"expected_revision": expected_revision, "batch_nos": batch_nos},
    )


def batch_nos(doc) -> list[str]:
    return [b["batch_no"] for b in doc["batches"]]


# ---------------------------------------------------------------- 修订号基础


def test_created_document_starts_at_revision_one(client):
    add_batch(client, "B1")
    body = create_verification(client, "V1", ["B1"]).json()
    assert body["revision"] == 1
    listed = client.get("/api/verifications").json()["verifications"][0]
    assert listed["revision"] == 1


def test_reverify_advances_revision(client):
    add_batch(client, "B1")
    create_verification(client, "V1", ["B1"])
    resp = client.post("/api/verifications/V1/reverify")
    assert resp.status_code == 200
    assert resp.json()["revision"] == 2


def test_confirm_advances_revision(client):
    add_lot(client, "RM-M", "奶粉", ["milk"])
    add_batch(client, "B1", lots=["RM-M"], declared=["milk"])
    create_verification(client, "V1", ["B1"])
    resp = client.post("/api/verifications/V1/confirm")
    assert resp.status_code == 200
    assert resp.json()["revision"] == 2
    # 确认后自动转待复核，再重新核验 + 再次确认继续递增
    client.put("/api/material-lots/RM-M", json={"allergens": ["milk", "egg"]})
    client.put("/api/material-lots/RM-M", json={"allergens": ["milk"]})
    assert client.get("/api/verifications/V1").json()["status"] == "PENDING_REVIEW"
    assert client.post("/api/verifications/V1/reverify").json()["revision"] == 3
    assert client.post("/api/verifications/V1/confirm").json()["revision"] == 4


# ---------------------------------------------------------------- 修订成功路径


def test_revise_draft_replaces_members_in_order_and_refreshes_snapshots(client):
    add_batch(client, "B1")
    add_batch(client, "B2")
    add_batch(client, "B3")
    create_verification(client, "V1", ["B1", "B2"])

    # 更正 B2 产生 v2（草稿不转态），使旧快照落后
    client.put("/api/batches/B2", json={"declared_allergens": []})
    doc = client.get("/api/verifications/V1").json()
    assert doc["batches"][1]["snapshot"]["version"] == 1
    assert doc["batches"][1]["current"]["version"] == 2

    resp = revise(client, "V1", 1, ["B3", "B1", "B2"])  # 完整新清单、保序
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "DRAFT"
    assert body["revision"] == 2
    assert batch_nos(body) == ["B3", "B1", "B2"]
    assert [b["position"] for b in body["batches"]] == [1, 2, 3]
    # 同一事务重取当前裁决：B2 快照直接是 v2，全部不再 changed
    by_no = {b["batch_no"]: b for b in body["batches"]}
    assert by_no["B2"]["snapshot"]["version"] == 2
    assert all(b["changed"] is False for b in body["batches"])
    assert all(b["shipment_changed"] is False for b in body["batches"])


def test_revise_pending_review_returns_to_draft_with_current_snapshots(client):
    add_lot(client, "RM-P", "花生酱", ["peanut"])
    add_batch(client, "B1", lots=["RM-P"], declared=["peanut"])
    create_verification(client, "V1", ["B1"])
    client.post("/api/verifications/V1/confirm")
    # 新版本使单据转待复核（确认已把修订号推进到 2）
    client.put("/api/material-lots/RM-P", json={"allergens": ["peanut", "sesame"]})
    client.put("/api/material-lots/RM-P", json={"allergens": ["peanut"]})
    assert client.get("/api/verifications/V1").json()["status"] == "PENDING_REVIEW"

    resp = revise(client, "V1", 2, ["B1"])
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "DRAFT"
    assert body["revision"] == 3
    assert body["batches"][0]["snapshot"]["version"] == 3
    assert body["batches"][0]["changed"] is False


def test_revise_can_remove_voided_batch_from_old_list(client):
    add_batch(client, "B1")
    add_batch(client, "B2")
    create_verification(client, "V1", ["B1", "B2"])
    # 作废 B2：草稿不转态；单据含作废批次，reverify/confirm 都会被拒
    resp = client.post("/api/batches/B2/void", json={"reason": "误登记未投产"})
    assert resp.status_code == 200
    doc = client.get("/api/verifications/V1").json()
    assert doc["contains_voided_batches"] is True
    assert doc["voided_batches"] == ["B2"]
    assert client.post("/api/verifications/V1/reverify").status_code == 409

    # 借修订移除已作废批次，单据恢复可用
    resp = revise(client, "V1", 1, ["B1"])
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert batch_nos(body) == ["B1"]
    assert body["contains_voided_batches"] is False
    assert body["voided_batches"] == []
    assert client.post("/api/verifications/V1/confirm").status_code == 200


def test_prior_confirmations_remain_even_if_batches_left_list(client):
    add_lot(client, "RM-P", "花生酱", ["peanut"])
    add_batch(client, "B1", lots=["RM-P"], declared=["peanut"])
    add_batch(client, "B2", lots=["RM-P"], declared=["peanut"])
    create_verification(client, "V1", ["B1", "B2"])
    client.post("/api/verifications/V1/confirm")
    doc = client.get("/api/verifications/V1").json()
    assert len(doc["confirmations"]) == 1
    assert [b["batch_no"] for b in doc["confirmations"][0]["batches"]] == ["B1", "B2"]

    # 新版本使单据转待复核（确认已把修订号推进到 2）：先引入 sesame 再更正回来，
    # B1/B2 到 v3 且结论仍为 RELEASE
    client.put("/api/material-lots/RM-P", json={"allergens": ["peanut", "sesame"]})
    client.put("/api/material-lots/RM-P", json={"allergens": ["peanut"]})
    assert client.get("/api/verifications/V1").json()["status"] == "PENDING_REVIEW"

    # 转待复核后修订：移除 B2。历次确认及其逐批快照原样保留
    resp = revise(client, "V1", 2, ["B1"])
    assert resp.status_code == 200
    body = resp.json()
    assert batch_nos(body) == ["B1"]
    assert len(body["confirmations"]) == 1
    confirmed_batches = body["confirmations"][0]["batches"]
    assert [b["batch_no"] for b in confirmed_batches] == ["B1", "B2"]
    assert confirmed_batches[0]["version"] == 1
    # 新清单只有 B1（修订已在同一事务刷新为 v3 快照），可再次确认；
    # 第二次确认只含 B1，第一次确认仍保留
    resp = client.post("/api/verifications/V1/confirm")
    assert resp.status_code == 200, resp.text
    doc = client.get("/api/verifications/V1").json()
    assert len(doc["confirmations"]) == 2
    assert [b["batch_no"] for b in doc["confirmations"][0]["batches"]] == ["B1", "B2"]
    assert [b["batch_no"] for b in doc["confirmations"][1]["batches"]] == ["B1"]


# ---------------------------------------------------------------- 整体拒绝


def test_revise_unknown_document_404(client):
    add_batch(client, "B1")
    assert revise(client, "NOPE", 1, ["B1"]).status_code == 404


def test_revise_confirmed_document_rejected(client):
    add_batch(client, "B1")
    create_verification(client, "V1", ["B1"])
    client.post("/api/verifications/V1/confirm")
    resp = revise(client, "V1", 2, ["B1"])
    assert resp.status_code == 409
    assert resp.json()["error"]["code"] == "VERIFICATION_ALREADY_CONFIRMED"
    # 成员不变
    assert batch_nos(client.get("/api/verifications/V1").json()) == ["B1"]


def test_revise_stale_revision_rejected_and_leaves_document_unchanged(client):
    add_batch(client, "B1")
    add_batch(client, "B2")
    create_verification(client, "V1", ["B1"])
    # 重新核验把修订号推进到 2
    client.post("/api/verifications/V1/reverify")

    resp = revise(client, "V1", 1, ["B1", "B2"])
    assert resp.status_code == 409
    err = resp.json()["error"]
    assert err["code"] == "VERIFICATION_REVISION_CONFLICT"
    assert err["details"][0]["submitted_revision"] == 1
    assert err["details"][0]["current_revision"] == 2

    doc = client.get("/api/verifications/V1").json()
    assert doc["revision"] == 2
    assert batch_nos(doc) == ["B1"]  # 新清单未落库

    # 使用当前修订号重新提交即成功
    resp = revise(client, "V1", 2, ["B1", "B2"])
    assert resp.status_code == 200
    assert batch_nos(resp.json()) == ["B1", "B2"]
    assert resp.json()["revision"] == 3


def test_revise_empty_list_rejected(client):
    add_batch(client, "B1")
    create_verification(client, "V1", ["B1"])
    # pydantic min_length -> 422
    resp = revise(client, "V1", 1, [])
    assert resp.status_code == 422


def test_revise_missing_revision_field_rejected(client):
    add_batch(client, "B1")
    create_verification(client, "V1", ["B1"])
    resp = client.post(
        "/api/verifications/V1/revise", json={"batch_nos": ["B1"]}
    )
    assert resp.status_code == 422
    resp = client.post(
        "/api/verifications/V1/revise",
        json={"expected_revision": 0, "batch_nos": ["B1"]},
    )
    assert resp.status_code == 422
    # 文档未受影响
    assert client.get("/api/verifications/V1").json()["revision"] == 1


def test_revise_duplicate_batch_rejected(client):
    add_batch(client, "B1")
    create_verification(client, "V1", ["B1"])
    resp = revise(client, "V1", 1, ["B1", "B1"])
    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == "DUPLICATE_BATCH_IN_VERIFICATION"
    assert batch_nos(client.get("/api/verifications/V1").json()) == ["B1"]


def test_revise_unknown_batch_rejected(client):
    add_batch(client, "B1")
    create_verification(client, "V1", ["B1"])
    resp = revise(client, "V1", 1, ["B1", "GHOST"])
    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == "UNKNOWN_BATCH"
    doc = client.get("/api/verifications/V1").json()
    assert doc["revision"] == 1
    assert batch_nos(doc) == ["B1"]


def test_revise_voided_batch_rejected(client):
    add_batch(client, "B1")
    add_batch(client, "B2")
    client.post("/api/batches/B2/void", json={"reason": "误登记未投产"})
    create_verification(client, "V1", ["B1"])
    resp = revise(client, "V1", 1, ["B1", "B2"])
    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == "BATCH_VOIDED"
    doc = client.get("/api/verifications/V1").json()
    assert batch_nos(doc) == ["B1"]
    assert doc["revision"] == 1


def test_revise_rejections_do_not_partially_write_snapshots(client):
    # 新清单里混入一个会产生新版本的有效批次 + 一个作废批次：
    # 作废校验失败时，有效批次的快照也不得被写入
    add_lot(client, "RM-M", "奶粉", ["milk"])
    b1 = add_batch(client, "B1", lots=["RM-M"], declared=["milk"])
    add_batch(client, "B2")
    client.post("/api/batches/B2/void", json={"reason": "误登记未投产"})
    create_verification(client, "V1", ["B1"])
    client.put("/api/batches/B1", json={"declared_allergens": ["milk"]})  # B1 v2

    assert revise(client, "V1", 1, ["B1", "B2"]).status_code == 400

    doc = client.get("/api/verifications/V1").json()
    assert doc["revision"] == 1
    assert batch_nos(doc) == ["B1"]
    # 快照仍停留在创建时 v1（修订失败未刷新成 v2）
    assert doc["batches"][0]["snapshot"]["version"] == b1["adjudication"]["version"]
    assert doc["batches"][0]["snapshot"]["version"] == 1
    assert doc["batches"][0]["current"]["version"] == 2
    assert doc["batches"][0]["changed"] is True


# ---------------------------------------------------------------- 并发一致性


def test_concurrent_correction_mixes_no_cross_time_snapshots(client, monkeypatch):
    """修订事务的“逐批重取”必须全部发生在同一写事务内。

    确定性交织：修订进入写事务后先暂停（挂起点），在它尚未读取任何批次时，
    另一线程尝试更正批次——因写事务串行化而阻塞等待；放行后更正先提交、
    修订随后在同一事务内继续逐批读取。若实现把校验与快照重取拆到多个事务，
    就会读到一部分更正前、一部分更正后（清单内出现 changed=true 的跨时间混合）。
    """
    add_lot(client, "RM-M", "奶粉", ["milk"])
    add_batch(client, "B1", lots=["RM-M"], declared=["milk"])
    add_batch(client, "B2", lots=["RM-M"], declared=["milk"])
    create_verification(client, "V1", ["B1"])

    service = client.app.state.service
    store = client.app.state.store
    entered = threading.Event()
    release_revise = threading.Event()
    rlock = store._write_lock
    conn = store._conn

    @contextmanager
    def instrumented_write():
        # 先取进程写锁（更正线程因此串行排队）
        with rlock:
            if instrumented_write.pause:
                # 仅在修订事务挂起一次：持锁但尚未 BEGIN/读取任何数据
                instrumented_write.pause = False
                entered.set()
                release_revise.wait(timeout=10)
            # 与 Store.write 相同的立即事务：放行后在同一临界区内
            # BEGIN IMMEDIATE -> 逐批读取 -> 提交
            conn.execute("BEGIN IMMEDIATE")
            try:
                yield conn
                conn.execute("COMMIT")
            except Exception:
                try:
                    conn.execute("ROLLBACK")
                except sqlite3.OperationalError:
                    pass
                raise

    instrumented_write.pause = True
    monkeypatch.setattr(store, "write", instrumented_write)

    result: dict = {}

    def revise_paused_inside_transaction():
        result["resp"] = service.revise_verification("V1", 1, ["B1", "B2"])

    worker = threading.Thread(target=revise_paused_inside_transaction)
    worker.start()
    entered.wait(timeout=5)  # 修订已进入写事务、尚未读取批次

    # 修订持锁期间发起两次更正（另一线程）：阻塞等待修订提交
    corrections: dict = {}

    def pending_corrections():
        corrections["c1"] = client.put(
            "/api/batches/B1", json={"declared_allergens": ["milk", "egg"]}
        ).status_code
        corrections["c2"] = client.put(
            "/api/batches/B2", json={"declared_allergens": ["milk", "egg"]}
        ).status_code

    corrector = threading.Thread(target=pending_corrections)
    corrector.start()
    corrector.join(timeout=0.5)
    assert corrector.is_alive()  # 更正串行化等待中

    # 放行修订：它在同一事务内逐批重取——此时其事务快照早已建立，
    # 读到的仍是更正前版本（一致的旧快照）；随后更正才提交。
    release_revise.set()
    worker.join(timeout=10)
    corrector.join(timeout=10)
    assert not worker.is_alive()
    assert not corrector.is_alive()
    assert corrections["c1"] == corrections["c2"] == 200

    body = result["resp"]
    # 清单内一致：修订事务快照建立于更正之前，两批快照同为 v1、且彼此一致
    # （若分批跨事务读取，B1/B2 会出现一旧一新、changed 不一致的混合）
    versions = {b["batch_no"]: b["snapshot"]["version"] for b in body["batches"]}
    assert versions == {"B1": 1, "B2": 1}

    # 更正提交后单据转为“快照落后”，再修订一次即在同一事务取到一致的新快照
    resp = revise(client, "V1", body["revision"], ["B1", "B2"])
    assert resp.status_code == 200
    body = resp.json()
    for entry in body["batches"]:
        assert entry["snapshot"]["version"] == entry["current"]["version"]
        assert entry["changed"] is False
        assert entry["shipment_changed"] is False


def test_concurrent_lot_lock_then_revise_fetches_current_eligibility(client):
    add_lot(client, "RM-M", "奶粉", ["milk"])
    add_batch(client, "B1", lots=["RM-M"], declared=["milk"])
    create_verification(client, "V1", ["B1"])

    # 锁定批号：B1 出货资格 v2 BLOCKED（草稿不转态）
    resp = client.post("/api/material-lots/RM-M/lock", json={"reason": "待检风险"})
    assert resp.status_code == 200

    # 修订重取出货资格快照：直接取到 v2 BLOCKED，而不是创建时的 v1 SHIPPABLE
    resp = revise(client, "V1", 1, ["B1"])
    assert resp.status_code == 200
    entry = resp.json()["batches"][0]
    assert entry["shipment_snapshot"]["version"] == 2
    assert entry["shipment_snapshot"]["status"] == "BLOCKED"
    assert entry["shipment_changed"] is False
    # 锁定批次仍不能确认（修订只刷新快照，不绕过资格规则）
    resp = client.post("/api/verifications/V1/confirm")
    assert resp.status_code == 422
    assert resp.json()["error"]["details"][0]["reason_code"] == "MATERIAL_LOT_LOCKED"


# ---------------------------------------------------------------- 旧库升级


def test_legacy_database_upgrades_revision_column(tmp_path):
    """无 revision 列的旧库启动后幂等补齐，既有单据按 v1 继续工作。"""
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
            status TEXT NOT NULL DEFAULT 'ACTIVE', voided_reason TEXT, voided_at TEXT,
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
        CREATE TABLE shipment_eligibility (batch_no TEXT PRIMARY KEY,
            version INTEGER NOT NULL DEFAULT 1, status TEXT NOT NULL,
            locked_lots TEXT NOT NULL DEFAULT '[]', reasons TEXT NOT NULL DEFAULT '[]',
            updated_at TEXT NOT NULL);
        CREATE TABLE shipment_eligibility_version (id INTEGER PRIMARY KEY AUTOINCREMENT,
            batch_no TEXT NOT NULL, version INTEGER NOT NULL, status TEXT NOT NULL,
            locked_lots TEXT NOT NULL DEFAULT '[]', reasons TEXT NOT NULL DEFAULT '[]',
            trigger TEXT NOT NULL, changed_at TEXT NOT NULL);
        CREATE TABLE material_lot_lock (lot_no TEXT PRIMARY KEY, locked INTEGER NOT NULL,
            reason TEXT NOT NULL, version INTEGER NOT NULL DEFAULT 0, updated_at TEXT NOT NULL);
        CREATE TABLE material_lot_lock_version (id INTEGER PRIMARY KEY AUTOINCREMENT,
            lot_no TEXT NOT NULL, version INTEGER NOT NULL, locked INTEGER NOT NULL,
            action TEXT NOT NULL, reason TEXT NOT NULL, changed_at TEXT NOT NULL);
        CREATE TABLE verification (verification_no TEXT PRIMARY KEY, status TEXT NOT NULL,
            created_at TEXT NOT NULL, updated_at TEXT NOT NULL, confirmed_at TEXT);
        CREATE TABLE verification_batch (verification_no TEXT NOT NULL, batch_no TEXT NOT NULL,
            position INTEGER NOT NULL, snapshot_version INTEGER NOT NULL,
            snapshot_disposition TEXT NOT NULL, snapshot_actual TEXT NOT NULL DEFAULT '[]',
            snapshot_undeclared TEXT NOT NULL DEFAULT '[]',
            snapshot_reasons TEXT NOT NULL DEFAULT '[]',
            snapshot_ship_version INTEGER, snapshot_ship_status TEXT,
            snapshot_locked_lots TEXT NOT NULL DEFAULT '[]',
            snapshot_ship_reasons TEXT NOT NULL DEFAULT '[]',
            PRIMARY KEY (verification_no, batch_no));
        CREATE TABLE verification_confirmation (id INTEGER PRIMARY KEY AUTOINCREMENT,
            verification_no TEXT NOT NULL, confirmed_at TEXT NOT NULL);
        CREATE TABLE verification_confirmation_batch (confirmation_id INTEGER NOT NULL,
            verification_no TEXT NOT NULL, position INTEGER NOT NULL, batch_no TEXT NOT NULL,
            snapshot_version INTEGER NOT NULL, snapshot_disposition TEXT NOT NULL,
            snapshot_actual TEXT NOT NULL DEFAULT '[]', snapshot_undeclared TEXT NOT NULL DEFAULT '[]',
            snapshot_reasons TEXT NOT NULL DEFAULT '[]',
            snapshot_ship_version INTEGER, snapshot_ship_status TEXT,
            snapshot_locked_lots TEXT NOT NULL DEFAULT '[]',
            snapshot_ship_reasons TEXT NOT NULL DEFAULT '[]');
        INSERT INTO allergen(code, name_zh) VALUES ('milk', '乳');
        INSERT INTO material_lot VALUES ('RM-M', '奶粉', '["milk"]', 't');
        INSERT INTO line VALUES ('L1', 't');
        INSERT INTO batch VALUES ('B1', 'L1', 1, '["RM-M"]', '["milk"]', '[]',
            'ACTIVE', NULL, NULL, 't');
        INSERT INTO batch_material VALUES ('B1', 'RM-M');
        INSERT INTO adjudication VALUES ('B1', 1, '["milk"]', '[]', 'RELEASE', '[]', '{}', 't');
        INSERT INTO adjudication_version VALUES
            (1, 'B1', 'L1', 1, 1, '["milk"]', '[]', 'RELEASE', '[]', '{}', 'REGISTER', 't');
        INSERT INTO shipment_eligibility VALUES ('B1', 1, 'SHIPPABLE', '[]', '[]', 't');
        INSERT INTO shipment_eligibility_version VALUES
            (1, 'B1', 1, 'SHIPPABLE', '[]', '[]', 'REGISTER', 't');
        INSERT INTO verification VALUES ('V1', 'DRAFT', 't', 't', NULL);
        INSERT INTO verification_batch VALUES
            ('V1', 'B1', 1, 1, 'RELEASE', '["milk"]', '[]', '[]', 1, 'SHIPPABLE', '[]', '[]');
        """
    )
    conn.commit()
    conn.close()

    app = create_app(db_path=str(db_path), seed_demo=False)
    with TestClient(app) as c:
        doc = c.get("/api/verifications/V1").json()
        assert doc["revision"] == 1  # 既有单据按 v1
        # 以 v1 为基线修订成功并推进到 v2
        resp = c.post(
            "/api/verifications/V1/revise",
            json={"expected_revision": 1, "batch_nos": ["B1"]},
        )
        assert resp.status_code == 200
        assert resp.json()["revision"] == 2
        # 过期修订号被拒
        resp = c.post(
            "/api/verifications/V1/revise",
            json={"expected_revision": 1, "batch_nos": ["B1"]},
        )
        assert resp.status_code == 409
        assert resp.json()["error"]["code"] == "VERIFICATION_REVISION_CONFLICT"

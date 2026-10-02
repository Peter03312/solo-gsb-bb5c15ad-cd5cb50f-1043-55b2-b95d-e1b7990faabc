"""装车交接（整单发运）测试。

覆盖：
- 成功路径：凭唯一交接号、已确认核验单号、读取时修订号登记整单发运；
  单事务占用全单批次，冻结单号、修订号、逐批确认快照与交接时间；
  交接不改写核验单（状态/修订号保持原样）；
- 查询：交接详情/列表（可按核验单过滤）、逐批交接归属（未发运 handover=null）；
- 只追加不可变：交接后的更正/待复核/清单修订均不改写交接记录，仍可追溯每批归属；
- 整体拒绝：重复交接号、未知单据、未确认单据、修订号冲突、
  任一批次已在其他交接记录中发运（含批次交叉的两张单据）——不留下部分占用；
- 纵深防御（直接构造不一致状态）：确认后批次作废、裁决版本漂移、出货资格版本漂移、
  资格结论变为 BLOCKED，整单拒绝；
- 同事务一致性：并发批次更正被写锁串行化，交接不混入跨时间状态；
- 旧库升级：旧库启动自动建立交接表，交接流程可用。
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


def add_batch(client, batch_no, line_id="LHO", lots=None, declared=None, cleaned=None):
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


def create_and_confirm(client, verification_no, batch_nos):
    resp = client.post(
        "/api/verifications",
        json={"verification_no": verification_no, "batch_nos": batch_nos},
    )
    assert resp.status_code == 201, resp.text
    resp = client.post(f"/api/verifications/{verification_no}/confirm")
    assert resp.status_code == 200, resp.text
    return resp.json()


def handover(client, handover_no, verification_no, expected_revision):
    return client.post(
        "/api/handovers",
        json={
            "handover_no": handover_no,
            "verification_no": verification_no,
            "expected_revision": expected_revision,
        },
    )


def db(client) -> sqlite3.Connection:
    return client.app.state.store._conn


# ---------------------------------------------------------------- 成功路径


def test_handover_ships_confirmed_document_and_freezes_snapshot(client):
    add_lot(client, "RM-M", "奶粉", ["milk"])
    add_batch(client, "B1", lots=["RM-M"], declared=["milk"])
    add_batch(client, "B2", cleaned=["milk"])
    doc = create_and_confirm(client, "V1", ["B1", "B2"])

    resp = handover(client, "H1", "V1", doc["revision"])
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["handover_no"] == "H1"
    assert body["verification_no"] == "V1"
    assert body["confirmation_id"] == doc["confirmations"][0]["confirmation_id"]
    assert body["verification_revision"] == doc["revision"]
    assert body["handed_over_at"]
    assert [b["batch_no"] for b in body["batches"]] == ["B1", "B2"]
    assert [b["position"] for b in body["batches"]] == [1, 2]
    for b in body["batches"]:
        assert b["snapshot"]["disposition"] == "RELEASE"
        assert b["shipment_snapshot"]["status"] == "SHIPPABLE"
        assert b["status"] == "ACTIVE"


def test_handover_does_not_modify_verification(client):
    add_batch(client, "B1")
    doc = create_and_confirm(client, "V1", ["B1"])
    assert doc["status"] == "CONFIRMED"
    assert doc["revision"] == 2

    assert handover(client, "H1", "V1", 2).status_code == 201

    after = client.get("/api/verifications/V1").json()
    assert after["status"] == "CONFIRMED"
    assert after["revision"] == 2  # 交接不推进单据修订号
    assert len(after["confirmations"]) == 1


def test_handover_detail_list_and_batch_attribution(client):
    add_batch(client, "B1")
    add_batch(client, "B2")
    create_and_confirm(client, "V1", ["B1"])
    create_and_confirm(client, "V2", ["B2"])
    handover(client, "H1", "V1", 2)
    handover(client, "H2", "V2", 2)

    detail = client.get("/api/handovers/H1").json()
    assert [b["batch_no"] for b in detail["batches"]] == ["B1"]

    listed = [h["handover_no"] for h in client.get("/api/handovers").json()["handovers"]]
    assert listed == ["H1", "H2"]
    # 列表项不重复展开逐批快照
    assert "batches" not in client.get("/api/handovers").json()["handovers"][0]

    filtered = [
        h["handover_no"]
        for h in client.get("/api/handovers?verification_no=V2").json()["handovers"]
    ]
    assert filtered == ["H2"]

    assert (
        client.get("/api/batches/B1/handover").json()["handover"]["handover_no"] == "H1"
    )
    # 另造一批：未发运时归属为 null
    add_batch(client, "B3")
    assert client.get("/api/batches/B3/handover").json()["handover"] is None
    assert (
        client.get("/api/batches/B2/handover").json()["handover"]["verification_no"]
        == "V2"
    )


def test_handover_freezes_latest_confirmation_snapshot(client):
    """单据可经历 确认 -> 转待复核 -> 重新核验 -> 再次确认；交接冻结最近一次确认。"""
    add_lot(client, "RM-M", "奶粉", ["milk"])
    add_batch(client, "B1", lots=["RM-M"], declared=["milk"])
    create_and_confirm(client, "V1", ["B1"])  # 第一次确认：裁决 v1、修订号 2
    # 引入新版本再改回：v3、RELEASE；单据转待复核
    client.put("/api/material-lots/RM-M", json={"allergens": ["milk", "egg"]})
    client.put("/api/material-lots/RM-M", json={"allergens": ["milk"]})
    assert client.get("/api/verifications/V1").json()["status"] == "PENDING_REVIEW"
    client.post("/api/verifications/V1/reverify")
    doc = client.post("/api/verifications/V1/confirm").json()
    assert doc["revision"] == 4

    body = handover(client, "H1", "V1", 4).json()
    assert body["confirmation_id"] == 2
    assert body["verification_revision"] == 4
    assert body["batches"][0]["snapshot"]["version"] == 3  # 最近一次确认快照，不是 v1


def test_handover_record_immutable_after_correction_and_pending_review(client):
    add_lot(client, "RM-M", "奶粉", ["milk"])
    add_batch(client, "B1", lots=["RM-M"], declared=["milk"])
    create_and_confirm(client, "V1", ["B1"])
    handover(client, "H1", "V1", 2)

    # 交接后更正（内容不变也追加裁决版本）：单据转待复核，但交接记录原样
    client.put("/api/batches/B1", json={"declared_allergens": ["milk"]})
    assert client.get("/api/verifications/V1").json()["status"] == "PENDING_REVIEW"

    frozen = client.get("/api/handovers/H1").json()
    assert frozen["verification_revision"] == 2
    assert frozen["batches"][0]["snapshot"]["version"] == 1
    # 交接记录反映“交接时事实”，不回显单据当前状态
    assert "status" not in frozen
    # 每批交接归属仍可追溯
    assert client.get("/api/batches/B1/handover").json()["handover"]["handover_no"] == "H1"


def test_handover_record_immutable_after_revise(client):
    add_batch(client, "B1")
    add_batch(client, "B2")
    create_and_confirm(client, "V1", ["B1"])
    handover(client, "H1", "V1", 2)

    # 单据转待复核后修订清单（加入 B2）：交接记录仍只冻结 B1
    client.put("/api/batches/B1", json={"declared_allergens": []})
    client.post("/api/verifications/V1/reverify")
    resp = client.post(
        "/api/verifications/V1/revise",
        json={"expected_revision": 3, "batch_nos": ["B1", "B2"]},
    )
    assert resp.status_code == 200, resp.text

    frozen = client.get("/api/handovers/H1").json()
    assert [b["batch_no"] for b in frozen["batches"]] == ["B1"]
    assert client.get("/api/batches/B2/handover").json()["handover"] is None


# ---------------------------------------------------------------- 整体拒绝


def test_handover_duplicate_handover_no_rejected(client):
    add_batch(client, "B1")
    create_and_confirm(client, "V1", ["B1"])
    assert handover(client, "H1", "V1", 2).status_code == 201
    resp = handover(client, "H1", "V1", 2)
    assert resp.status_code == 409
    assert resp.json()["error"]["code"] == "DUPLICATE_HANDOVER"


def test_handover_unknown_verification_404(client):
    resp = handover(client, "H1", "NOPE", 1)
    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "VERIFICATION_NOT_FOUND"
    assert client.get("/api/handovers").json()["handovers"] == []


def test_handover_draft_and_pending_review_rejected(client):
    add_batch(client, "B1")
    client.post("/api/verifications", json={"verification_no": "V1", "batch_nos": ["B1"]})
    resp = handover(client, "H1", "V1", 1)
    assert resp.status_code == 409
    err = resp.json()["error"]
    assert err["code"] == "VERIFICATION_NOT_CONFIRMED"
    assert err["details"][0]["current_status"] == "DRAFT"

    # 确认后因版本变化转待复核，同样拒绝交接
    client.post("/api/verifications/V1/confirm")
    client.put("/api/batches/B1", json={"declared_allergens": []})
    assert client.get("/api/verifications/V1").json()["status"] == "PENDING_REVIEW"
    resp = handover(client, "H1", "V1", 2)
    assert resp.status_code == 409
    assert resp.json()["error"]["details"][0]["current_status"] == "PENDING_REVIEW"


def test_handover_stale_revision_rejected_without_occupying(client):
    add_batch(client, "B1")
    create_and_confirm(client, "V1", ["B1"])  # 确认后修订号为 2
    resp = handover(client, "H1", "V1", 1)    # 用旧修订号
    assert resp.status_code == 409
    err = resp.json()["error"]
    assert err["code"] == "VERIFICATION_REVISION_CONFLICT"
    assert err["details"][0]["submitted_revision"] == 1
    assert err["details"][0]["current_revision"] == 2
    # 未占用：用当前修订号立即成功
    assert handover(client, "H1", "V1", 2).status_code == 201


def test_handover_same_verification_twice_rejected_as_already_shipped(client):
    add_batch(client, "B1")
    add_batch(client, "B2")
    create_and_confirm(client, "V1", ["B1", "B2"])
    assert handover(client, "H1", "V1", 2).status_code == 201

    resp = handover(client, "H2", "V1", 2)
    assert resp.status_code == 409
    err = resp.json()["error"]
    assert err["code"] == "HANDOVER_BATCH_NOT_READY"
    codes = {d["reason_code"] for d in err["details"]}
    assert codes == {"BATCH_ALREADY_SHIPPED"}
    assert {d["shipped_in_handover"] for d in err["details"]} == {"H1"}


def test_handover_overlapping_documents_whole_order_rejected(client):
    """两张已确认单据批次交叉：第二张因 B1 已发运整单拒绝，B2 不被占用。"""
    add_batch(client, "B1")
    add_batch(client, "B2")
    create_and_confirm(client, "V1", ["B1"])
    create_and_confirm(client, "V2", ["B1", "B2"])

    assert handover(client, "H1", "V1", 2).status_code == 201
    resp = handover(client, "H2", "V2", 2)
    assert resp.status_code == 409
    detail_codes = {d["batch_no"]: d["reason_code"] for d in resp.json()["error"]["details"]}
    assert detail_codes == {"B1": "BATCH_ALREADY_SHIPPED"}
    # 整单拒绝：B2 未被部分占用；把 B1 移出后 B2 所在单据可走修订重新确认再交接的恢复路径
    assert client.get("/api/batches/B2/handover").json()["handover"] is None
    # H2 记录不存在
    assert client.get("/api/handovers/H2").status_code == 404


def test_handover_disjoint_documents_both_ship(client):
    add_batch(client, "B1")
    add_batch(client, "B2")
    create_and_confirm(client, "V1", ["B1"])
    create_and_confirm(client, "V2", ["B2"])
    assert handover(client, "H1", "V1", 2).status_code == 201
    assert handover(client, "H2", "V2", 2).status_code == 201


def test_handover_blank_fields_and_bad_payload(client):
    add_batch(client, "B1")
    create_and_confirm(client, "V1", ["B1"])
    assert (
        client.post(
            "/api/handovers",
            json={"handover_no": "  ", "verification_no": "V1", "expected_revision": 2},
        ).status_code
        == 400
    )
    # 缺字段 / 修订号 < 1 -> 422 模型校验
    assert (
        client.post(
            "/api/handovers",
            json={"verification_no": "V1", "expected_revision": 2},
        ).status_code
        == 422
    )
    assert (
        client.post(
            "/api/handovers",
            json={"handover_no": "H1", "verification_no": "V1", "expected_revision": 0},
        ).status_code
        == 422
    )
    assert client.get("/api/handovers").json()["handovers"] == []


# ---------------------------------------------------------------- 不一致状态（纵深防御）


def test_handover_rejects_when_became_voided_after_confirm(client):
    add_batch(client, "B1")
    add_batch(client, "B2")
    create_and_confirm(client, "V1", ["B1", "B2"])
    # 直接构造“已确认但批次随后作废”的不一致状态（正常流程会先转待复核）
    db(client).execute("UPDATE batch SET status='VOIDED', voided_reason='x', voided_at='t' WHERE batch_no='B1'")
    db(client).commit()

    resp = handover(client, "H1", "V1", 2)
    assert resp.status_code == 409
    details = {d["batch_no"]: d["reason_code"] for d in resp.json()["error"]["details"]}
    assert details == {"B1": "BATCH_VOIDED"}
    assert client.get("/api/batches/B2/handover").json()["handover"] is None


def test_handover_rejects_when_adjudication_version_drifted(client):
    add_batch(client, "B1")
    create_and_confirm(client, "V1", ["B1"])
    db(client).execute("UPDATE adjudication SET version=version+1 WHERE batch_no='B1'")
    db(client).commit()

    resp = handover(client, "H1", "V1", 2)
    assert resp.status_code == 409
    detail = resp.json()["error"]["details"][0]
    assert detail["reason_code"] == "STALE_SNAPSHOT"
    assert detail["snapshot_version"] == 1
    assert detail["current_version"] == 2


def test_handover_rejects_when_eligibility_version_drifted(client):
    add_batch(client, "B1")
    create_and_confirm(client, "V1", ["B1"])
    db(client).execute(
        "UPDATE shipment_eligibility SET version=version+1 WHERE batch_no='B1'"
    )
    db(client).commit()

    resp = handover(client, "H1", "V1", 2)
    assert resp.status_code == 409
    detail = resp.json()["error"]["details"][0]
    assert detail["reason_code"] == "STALE_ELIGIBILITY"
    assert detail["snapshot_version"] == 1
    assert detail["current_version"] == 2


def test_handover_rejects_when_became_blocked(client):
    add_lot(client, "RM-M", "奶粉", ["milk"])
    add_batch(client, "B1", lots=["RM-M"], declared=["milk"])
    create_and_confirm(client, "V1", ["B1"])
    # 直接构造“已确认但当前 BLOCKED”（正常流程锁定会先把单据转待复核）
    db(client).execute(
        "UPDATE shipment_eligibility SET version=version+1, status='BLOCKED',"
        " locked_lots='[\"RM-M\"]' WHERE batch_no='B1'"
    )
    db(client).commit()

    resp = handover(client, "H1", "V1", 2)
    assert resp.status_code == 409
    by_code = {d["reason_code"]: d for d in resp.json()["error"]["details"]}
    # 版本漂移与资格 BLOCKED 同时上报（与确认接口的逐批明细风格一致）
    assert by_code["STALE_ELIGIBILITY"]["current_version"] == 2
    assert by_code["MATERIAL_LOT_LOCKED"]["locked_lots"] == ["RM-M"]


def test_handover_rejects_when_became_hold(client):
    add_batch(client, "B1")
    create_and_confirm(client, "V1", ["B1"])
    db(client).execute(
        "UPDATE adjudication SET disposition='HOLD', undeclared='[\"egg\"]' WHERE batch_no='B1'"
    )
    db(client).commit()

    resp = handover(client, "H1", "V1", 2)
    assert resp.status_code == 409
    detail = resp.json()["error"]["details"][0]
    assert detail["reason_code"] == "BATCH_ON_HOLD"
    assert detail["undeclared_allergens"] == ["egg"]


def test_handover_collects_all_unqualified_batches(client):
    add_lot(client, "RM-M", "奶粉", ["milk"])
    add_batch(client, "B1", lots=["RM-M"], declared=["milk"])
    add_batch(client, "B2", lots=["RM-M"], declared=["milk"])
    create_and_confirm(client, "V1", ["B1", "B2"])
    conn = db(client)
    conn.execute("UPDATE adjudication SET disposition='HOLD' WHERE batch_no='B1'")
    conn.execute("UPDATE shipment_eligibility SET version=version+1 WHERE batch_no='B2'")
    conn.commit()

    resp = handover(client, "H1", "V1", 2)
    assert resp.status_code == 409
    by_batch = {}
    for d in resp.json()["error"]["details"]:
        by_batch.setdefault(d["batch_no"], []).append(d["reason_code"])
    assert by_batch == {"B1": ["BATCH_ON_HOLD"], "B2": ["STALE_ELIGIBILITY"]}
    assert client.get("/api/handovers").json()["handovers"] == []


# ---------------------------------------------------------------- 并发一致性


def test_concurrent_handover_occupies_each_batch_once(client, monkeypatch):
    """两个线程同时交接批次交叉的两张已确认单据：恰好一个成功，另一个整单拒绝。

    写锁（BEGIN IMMEDIATE）串行化两个交接事务；UNIQUE(batch_no) 在插入阶段
    兜底并发占用，失败事务整体回滚，不留下部分占用。
    """
    add_batch(client, "B1")
    add_batch(client, "B2")
    create_and_confirm(client, "V1", ["B1", "B2"])
    create_and_confirm(client, "V2", ["B1"])

    service = client.app.state.service
    barrier = threading.Barrier(2)

    results: dict[str, object] = {}

    def worker(name, handover_no, verification_no):
        barrier.wait(timeout=10)
        try:
            results[name] = service.create_handover(handover_no, verification_no, 2)
        except Exception as exc:  # noqa: BLE001 - 断言在主线程检查 code
            results[name] = exc

    t1 = threading.Thread(target=worker, args=("a", "H1", "V1"))
    t2 = threading.Thread(target=worker, args=("b", "H2", "V2"))
    t1.start()
    t2.start()
    t1.join(timeout=20)
    t2.join(timeout=20)
    assert not t1.is_alive() and not t2.is_alive()

    from app.errors import APIError

    statuses = []
    for name, res in results.items():
        if isinstance(res, APIError):
            statuses.append((name, res.code))
        else:
            statuses.append((name, "OK"))
    oks = [s for s in statuses if s[1] == "OK"]
    rejected = [s for s in statuses if s[1] == "HANDOVER_BATCH_NOT_READY"]
    assert len(oks) == 1, statuses
    assert len(rejected) == 1, statuses

    # 恰好一张交接成功（线程竞态决定哪张），每批恰好一个交接归属，无部分占用
    handovers = [h["handover_no"] for h in client.get("/api/handovers").json()["handovers"]]
    assert len(handovers) == 1
    winner = handovers[0]
    detail = client.get(f"/api/handovers/{winner}").json()
    shipped_batches = {b["batch_no"] for b in detail["batches"]}
    assert shipped_batches == {"V1": {"B1", "B2"}, "V2": {"B1"}}[
        client.get(f"/api/handovers/{winner}").json()["verification_no"]
    ]
    for bn in {"B1", "B2"}:
        attr = client.get(f"/api/batches/{bn}/handover").json()["handover"]
        if bn in shipped_batches:
            assert attr["handover_no"] == winner
        else:
            assert attr is None
    # 失败的交接号无任何记录
    loser = "H2" if winner == "H1" else "H1"
    assert client.get(f"/api/handovers/{loser}").status_code == 404


def test_concurrent_correction_serialized_with_handover(client, monkeypatch):
    """交接事务持锁期间，并发批次更正排队；交接在同一事务快照内判定，不混入跨时间状态。

    交接事务先取得写锁时，更正尚未提交，交接看到 RELEASE/版本一致 → 成功；
    随后更正提交，单据转待复核，但交接记录已冻结。
    """
    add_lot(client, "RM-M", "奶粉", ["milk"])
    add_batch(client, "B1", lots=["RM-M"], declared=["milk"])
    create_and_confirm(client, "V1", ["B1"])

    store = client.app.state.store
    service = client.app.state.service
    rlock = store._write_lock
    conn = store._conn
    entered = threading.Event()
    release_handover = threading.Event()

    @contextmanager
    def instrumented_write():
        with rlock:
            if instrumented_write.pause:
                instrumented_write.pause = False
                entered.set()
                release_handover.wait(timeout=10)
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

    def do_handover():
        result["res"] = service.create_handover("H1", "V1", 2)

    worker = threading.Thread(target=do_handover)
    worker.start()
    entered.wait(timeout=5)

    correction: dict = {}

    def do_correction():
        correction["status"] = client.put(
            "/api/batches/B1", json={"declared_allergens": ["milk"]}
        ).status_code

    corrector = threading.Thread(target=do_correction)
    corrector.start()
    corrector.join(timeout=0.5)
    assert corrector.is_alive()  # 更正被写锁串行化阻塞

    release_handover.set()
    worker.join(timeout=10)
    corrector.join(timeout=10)
    assert not worker.is_alive() and not corrector.is_alive()
    assert correction["status"] == 200

    # 交接基于更正前的一致快照成功，冻结 v1
    assert result["res"]["verification_revision"] == 2
    assert result["res"]["batches"][0]["snapshot"]["version"] == 1
    # 更正提交后单据转待复核，但交接记录不变
    assert client.get("/api/verifications/V1").json()["status"] == "PENDING_REVIEW"
    assert client.get("/api/handovers/H1").json()["batches"][0]["snapshot"]["version"] == 1


# ---------------------------------------------------------------- 旧库升级


def test_legacy_database_creates_handover_tables(tmp_path):
    """早于交接功能的旧库启动后自动建表，交接流程可用。"""
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
            revision INTEGER NOT NULL DEFAULT 1,
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
        INSERT INTO verification VALUES ('V1', 'CONFIRMED', 2, 't', 't', 't');
        INSERT INTO verification_batch VALUES
            ('V1', 'B1', 1, 1, 'RELEASE', '["milk"]', '[]', '[]', 1, 'SHIPPABLE', '[]', '[]');
        INSERT INTO verification_confirmation VALUES (1, 'V1', 't');
        INSERT INTO verification_confirmation_batch VALUES
            (1, 'V1', 1, 'B1', 1, 'RELEASE', '["milk"]', '[]', '[]', 1, 'SHIPPABLE', '[]', '[]');
        """
    )
    conn.commit()
    conn.close()

    app = create_app(db_path=str(db_path), seed_demo=False)
    with TestClient(app) as c:
        resp = c.post(
            "/api/handovers",
            json={"handover_no": "H1", "verification_no": "V1", "expected_revision": 2},
        )
        assert resp.status_code == 201, resp.text
        body = resp.json()
        assert body["verification_revision"] == 2
        assert body["batches"][0]["batch_no"] == "B1"
        assert (
            c.get("/api/batches/B1/handover").json()["handover"]["handover_no"] == "H1"
        )
        # 重启再次初始化仍幂等（表已存在），记录不丢
    app2 = create_app(db_path=str(db_path), seed_demo=False)
    with TestClient(app2) as c:
        assert c.get("/api/handovers/H1").status_code == 200

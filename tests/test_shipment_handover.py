"""装车交接（整单发运）测试。

覆盖：
- 成功交接：凭唯一交接号 + 已确认核验单号 + 读取时修订号，整单发运；
  冻结交接时的单号、修订号、最近一次确认引用、交接时间与逐批确认快照（保序）；
- 拒绝：重复交接号 409、未知单据 404、非已确认单据 409、修订号不匹配 409；
- 逐批版本/结论门禁（防御性）：当前裁决/出货资格版本与最近一次确认快照不一致，
  或结论非 RELEASE / 非 SHIPPABLE，或批次已作废 -> 422 HANDOVER_NOT_ALLOWED，
  details 逐批说明，整体回滚不占用；
- 任一批次已在其他交接记录中发运 -> 409 BATCH_ALREADY_SHIPPED（details 指向已有交接），
  同一核验单/多张核验单场景均整单拒绝，且不留下部分占用；
- 交接记录不可变：交接后的更正（单据转待复核）、重新核验、清单修订不改写交接记录；
- 追溯：单批交接归属（未发运 shipped=false、未知批次 404）、交接列表/详情/按核验单过滤；
- 原子性：并发两单共享同一批次时恰有一单成功，失败方不留下部分占用；
- 旧库启动幂等补齐交接表，既有单据仍可确认交接。
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


def confirm(client, verification_no):
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


def raw_conn(client):
    """另开一个到同一 SQLite 文件的连接，用于构造正常 API 无法到达的防御性数据状态。

    应用使用 WAL；独立连接在其读事务内可看到应用已提交的数据，独立连接的写入
    经自身事务提交后同样对应用可见。
    """
    conn = sqlite3.connect(client.app.state.store.path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def release_batches(client, *batch_nos, line_id="L1", lot="RM-M"):
    """登记一个含 milk 的批号与若干全部 RELEASE/SHIPPABLE 的批次（逐批声明 milk）。"""
    add_lot(client, lot, "奶粉", ["milk"])
    for bn in batch_nos:
        add_batch(client, bn, line_id=line_id, lots=[lot], declared=["milk"])


# ---------------------------------------------------------------- 成功交接


def test_handover_registers_whole_order_with_frozen_snapshot(client):
    release_batches(client, "B1", "B2")
    create_verification(client, "V1", ["B2", "B1"])  # 顺序须保留
    doc = confirm(client, "V1")
    assert doc["revision"] == 2  # 创建=1，确认 +1

    resp = handover(client, "H1", "V1", doc["revision"])
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["handover_no"] == "H1"
    assert body["verification_no"] == "V1"
    assert body["verification_revision"] == 2  # 冻结交接时修订号
    assert body["batch_count"] == 2
    assert body["handed_over_at"] is not None
    assert body["confirmed_at"] == doc["confirmed_at"]
    assert body["confirmation_id"] == doc["confirmations"][-1]["confirmation_id"]

    # 逐批确认快照按确认清单顺序冻结
    assert [b["batch_no"] for b in body["batches"]] == ["B2", "B1"]
    assert [b["position"] for b in body["batches"]] == [1, 2]
    for b in body["batches"]:
        assert b["adjudication"]["version"] == 1
        assert b["adjudication"]["disposition"] == "RELEASE"
        assert b["shipment_eligibility"]["version"] == 1
        assert b["shipment_eligibility"]["status"] == "SHIPPABLE"

    # 单批可追溯到本次交接
    for bn, position in (("B2", 1), ("B1", 2)):
        trace = client.get(f"/api/batches/{bn}/handover").json()
        assert trace["shipped"] is True
        assert trace["handover"]["handover_no"] == "H1"
        assert trace["handover"]["verification_no"] == "V1"
        assert trace["handover"]["verification_revision"] == 2
        assert trace["handover"]["position"] == position


def test_handover_does_not_change_verification_status_or_revision(client):
    release_batches(client, "B1")
    create_verification(client, "V1", ["B1"])
    doc = confirm(client, "V1")
    rev_before = doc["revision"]

    assert handover(client, "H1", "V1", rev_before).status_code == 201

    doc_after = client.get("/api/verifications/V1").json()
    # 交接不改写单据：仍为已确认、修订号不变
    assert doc_after["status"] == "CONFIRMED"
    assert doc_after["revision"] == rev_before


# ---------------------------------------------------------------- 重复交接号


def test_duplicate_handover_no_rejected_without_partial_occupation(client):
    release_batches(client, "B1", "B2")
    create_verification(client, "V1", ["B1"])
    create_verification(client, "V2", ["B2"])
    confirm(client, "V1")
    d2 = confirm(client, "V2")

    assert handover(client, "H1", "V1", 2).status_code == 201
    resp = handover(client, "H1", "V2", d2["revision"])
    assert resp.status_code == 409
    assert resp.json()["error"]["code"] == "DUPLICATE_HANDOVER"

    # 重复交接号拒绝不占用 V2 的任何批次
    trace = client.get("/api/batches/B2/handover").json()
    assert trace["shipped"] is False
    # V2 仍可换一个交接号完成交接
    assert handover(client, "H2", "V2", d2["revision"]).status_code == 201


# ---------------------------------------------------------------- 未知/状态/修订号


def test_handover_unknown_verification_404(client):
    resp = handover(client, "H1", "NOPE", 1)
    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "VERIFICATION_NOT_FOUND"
    assert client.get("/api/handovers/H1").status_code == 404


def test_handover_draft_and_pending_review_rejected(client):
    release_batches(client, "B1")
    create_verification(client, "VD", ["B1"])
    resp = handover(client, "HD", "VD", 1)
    assert resp.status_code == 409
    assert resp.json()["error"]["code"] == "VERIFICATION_NOT_CONFIRMED"

    # 确认后更正批次 -> 待复核
    confirm(client, "VD")
    r = client.put("/api/batches/B1", json={"declared_allergens": ["milk", "egg"]})
    assert r.status_code == 200
    doc = client.get("/api/verifications/VD").json()
    assert doc["status"] == "PENDING_REVIEW"
    resp = handover(client, "HP", "VD", doc["revision"])
    assert resp.status_code == 409
    assert resp.json()["error"]["code"] == "VERIFICATION_NOT_CONFIRMED"


def test_handover_revision_mismatch_409(client):
    release_batches(client, "B1")
    create_verification(client, "V1", ["B1"])
    confirm(client, "V1")

    # 已确认单据当前修订号为 2：提交 1（过期）整体拒绝
    resp = handover(client, "H1", "V1", 1)
    assert resp.status_code == 409
    err = resp.json()["error"]
    assert err["code"] == "VERIFICATION_REVISION_CONFLICT"
    assert err["details"][0]["submitted_revision"] == 1
    assert err["details"][0]["current_revision"] == 2

    # 用当前修订号即成功
    assert handover(client, "H1", "V1", 2).status_code == 201


def test_handover_empty_fields_400(client):
    release_batches(client, "B1")
    create_verification(client, "V1", ["B1"])
    confirm(client, "V1")
    for payload in [
        {"handover_no": "  ", "verification_no": "V1", "expected_revision": 2},
        {"handover_no": "H1", "verification_no": " ", "expected_revision": 2},
    ]:
        resp = client.post("/api/handovers", json=payload)
        assert resp.status_code == 400, resp.text
        assert resp.json()["error"]["code"] == "INVALID_PAYLOAD"


def test_handover_missing_or_invalid_revision_422(client):
    release_batches(client, "B1")
    create_verification(client, "V1", ["B1"])
    confirm(client, "V1")
    # 缺字段 / < 1 -> pydantic 422
    assert client.post(
        "/api/handovers", json={"handover_no": "H1", "verification_no": "V1"}
    ).status_code == 422
    assert client.post(
        "/api/handovers",
        json={"handover_no": "H1", "verification_no": "V1", "expected_revision": 0},
    ).status_code == 422


# ------------------------------------------------ 逐批版本/结论门禁（防御性）


def test_handover_rejects_stale_adjudication_snapshot(client):
    """当前裁决版本领先最近一次确认快照（单据仍 CONFIRMED 的防御性状态）-> 422。"""
    release_batches(client, "B1")
    create_verification(client, "V1", ["B1"])
    confirm(client, "V1")

    conn = raw_conn(client)
    # 直接为 B1 追加裁决 v2 并推进当前裁决版本，但不触碰核验单状态（绕过自动转待复核）
    conn.execute(
        "UPDATE adjudication SET version=2, decided_at=? WHERE batch_no='B1'",
        ("2026-10-02T00:00:00+00:00",),
    )
    conn.execute(
        """INSERT INTO adjudication_version
           (batch_no, line_id, seq, version, actual, undeclared, disposition, reasons,
            sources, trigger, decided_at)
           SELECT a.batch_no, b.line_id, b.seq, 2, a.actual, a.undeclared, a.disposition,
                  a.reasons, a.sources, 'CORRECT_BATCH', ?
           FROM adjudication a JOIN batch b ON b.batch_no=a.batch_no
           WHERE a.batch_no='B1'""",
        ("2026-10-02T00:00:00+00:00",),
    )
    conn.commit()
    conn.close()

    resp = handover(client, "H1", "V1", 2)
    assert resp.status_code == 422
    err = resp.json()["error"]
    assert err["code"] == "HANDOVER_NOT_ALLOWED"
    assert err["details"][0]["batch_no"] == "B1"
    assert err["details"][0]["reason_code"] == "STALE_SNAPSHOT"
    assert err["details"][0]["snapshot_version"] == 1
    assert err["details"][0]["current_version"] == 2
    _assert_no_handover_rows(client, "H1")


def test_handover_rejects_hold_disposition_even_if_version_matches(client):
    """当前裁决结论非 RELEASE（版本仍与确认快照相同）-> 422 BATCH_ON_HOLD。"""
    release_batches(client, "B1")
    create_verification(client, "V1", ["B1"])
    confirm(client, "V1")

    conn = raw_conn(client)
    # 仅把当前裁决改为 HOLD（版本号保持 1 与确认快照一致）
    conn.execute(
        "UPDATE adjudication SET disposition='HOLD', undeclared=? WHERE batch_no='B1'",
        ('["egg"]',),
    )
    conn.commit()
    conn.close()

    resp = handover(client, "H1", "V1", 2)
    assert resp.status_code == 422
    detail = resp.json()["error"]["details"][0]
    assert detail["reason_code"] == "BATCH_ON_HOLD"
    assert detail["disposition"] == "HOLD"
    assert detail["undeclared_allergens"] == ["egg"]
    _assert_no_handover_rows(client, "H1")


def test_handover_rejects_stale_eligibility_snapshot(client):
    """当前出货资格版本领先最近一次确认快照 -> 422 STALE_ELIGIBILITY。"""
    release_batches(client, "B1")
    create_verification(client, "V1", ["B1"])
    confirm(client, "V1")

    conn = raw_conn(client)
    conn.execute(
        "UPDATE shipment_eligibility SET version=2, updated_at=? WHERE batch_no='B1'",
        ("2026-10-02T00:00:00+00:00",),
    )
    conn.commit()
    conn.close()

    resp = handover(client, "H1", "V1", 2)
    assert resp.status_code == 422
    detail = resp.json()["error"]["details"][0]
    assert detail["reason_code"] == "STALE_ELIGIBILITY"
    assert detail["snapshot_version"] == 1
    assert detail["current_version"] == 2
    _assert_no_handover_rows(client, "H1")


def test_handover_rejects_blocked_eligibility_even_if_version_matches(client):
    """当前出货资格非 SHIPPABLE（版本仍匹配）-> 422 MATERIAL_LOT_LOCKED。"""
    release_batches(client, "B1")
    create_verification(client, "V1", ["B1"])
    confirm(client, "V1")

    conn = raw_conn(client)
    conn.execute(
        "UPDATE shipment_eligibility SET status='BLOCKED', locked_lots=? WHERE batch_no='B1'",
        ('["RM-M"]',),
    )
    conn.commit()
    conn.close()

    resp = handover(client, "H1", "V1", 2)
    assert resp.status_code == 422
    detail = resp.json()["error"]["details"][0]
    assert detail["reason_code"] == "MATERIAL_LOT_LOCKED"
    assert detail["locked_lots"] == ["RM-M"]
    _assert_no_handover_rows(client, "H1")


def test_handover_rejects_voided_batch(client):
    """确认清单中的批次被作废（单据仍 CONFIRMED 的防御性状态）-> 422 BATCH_VOIDED。"""
    release_batches(client, "B1", "B2")
    create_verification(client, "V1", ["B1", "B2"])
    confirm(client, "V1")

    conn = raw_conn(client)
    # 直接作废 B1 但不推进任何版本、不联动核验单（绕过正常作废路径）
    conn.execute(
        "UPDATE batch SET status='VOIDED', voided_reason='x', voided_at=? WHERE batch_no='B1'",
        ("2026-10-02T00:00:00+00:00",),
    )
    conn.commit()
    conn.close()

    resp = handover(client, "H1", "V1", 2)
    assert resp.status_code == 422
    codes = {d["reason_code"] for d in resp.json()["error"]["details"]}
    assert "BATCH_VOIDED" in codes
    _assert_no_handover_rows(client, "H1")
    # 合格批 B2 也未被部分占用
    assert client.get("/api/batches/B2/handover").json()["shipped"] is False


def test_handover_collects_all_ineligible_batches_atomically(client):
    """多批各自不合格时一次返回全部不符批次，且不留下任何占用。"""
    release_batches(client, "B1", "B2", "B3")
    create_verification(client, "V1", ["B1", "B2", "B3"])
    confirm(client, "V1")

    conn = raw_conn(client)
    conn.execute("UPDATE adjudication SET version=2 WHERE batch_no='B1'")
    conn.execute(
        "UPDATE shipment_eligibility SET status='BLOCKED', locked_lots=? WHERE batch_no='B2'",
        ('["RM-M"]',),
    )
    conn.commit()
    conn.close()

    resp = handover(client, "H1", "V1", 2)
    assert resp.status_code == 422
    by_batch: dict[str, list[str]] = {}
    for d in resp.json()["error"]["details"]:
        by_batch.setdefault(d["batch_no"], []).append(d["reason_code"])
    assert "STALE_SNAPSHOT" in by_batch["B1"]
    assert "MATERIAL_LOT_LOCKED" in by_batch["B2"]
    for bn in ("B1", "B2", "B3"):
        assert client.get(f"/api/batches/{bn}/handover").json()["shipped"] is False
    _assert_no_handover_rows(client, "H1")


def _assert_no_handover_rows(client, handover_no):
    """失败交接不得写入主记录/逐批快照/占用中的任何一项。"""
    conn = client.app.state.store
    with conn.read() as db:
        assert db.execute(
            "SELECT COUNT(*) AS n FROM shipment_handover WHERE handover_no=?",
            (handover_no,),
        ).fetchone()["n"] == 0
        assert db.execute(
            "SELECT COUNT(*) AS n FROM shipment_handover_batch WHERE handover_no=?",
            (handover_no,),
        ).fetchone()["n"] == 0
        assert db.execute(
            "SELECT COUNT(*) AS n FROM shipment_handover_occupancy WHERE handover_no=?",
            (handover_no,),
        ).fetchone()["n"] == 0


# ------------------------------------------------ 已在其他交接发运 -> 整单拒绝


def test_second_handover_of_same_verification_rejected(client):
    """同一已确认单据完成交接后，再次交接（新交接号）被占用规则拦截。"""
    release_batches(client, "B1")
    create_verification(client, "V1", ["B1"])
    confirm(client, "V1")
    assert handover(client, "H1", "V1", 2).status_code == 201

    resp = handover(client, "H2", "V1", 2)
    assert resp.status_code == 409
    err = resp.json()["error"]
    assert err["code"] == "BATCH_ALREADY_SHIPPED"
    assert err["details"][0]["batch_no"] == "B1"
    assert err["details"][0]["handover_no"] == "H1"


def test_shared_batch_across_verifications_blocks_whole_order(client):
    """两张核验单共享 B1：一单交接后，另一单（含全新批次 B3）仍整单拒绝、不部分占用。"""
    release_batches(client, "B1", "B2")
    add_batch(client, "B3", line_id="L2", lots=["RM-M"], declared=["milk"])

    create_verification(client, "V1", ["B1", "B2"])
    create_verification(client, "V2", ["B1", "B3"])
    confirm(client, "V1")
    d2 = confirm(client, "V2")

    assert handover(client, "H1", "V1", 2).status_code == 201
    resp = handover(client, "H2", "V2", d2["revision"])
    assert resp.status_code == 409
    err = resp.json()["error"]
    assert err["code"] == "BATCH_ALREADY_SHIPPED"
    assert [d["batch_no"] for d in err["details"]] == ["B1"]
    assert err["details"][0]["handover_no"] == "H1"

    # V2 的全新批次 B3 不得被部分占用；H2 不得留任何记录
    assert client.get("/api/batches/B3/handover").json()["shipped"] is False
    _assert_no_handover_rows(client, "H2")


def test_concurrent_handovers_sharing_batch_exactly_one_succeeds(client, monkeypatch):
    """并发两单共享 B1：写事务串行化，恰有一单成功，另一单 409 且无部分占用。"""
    release_batches(client, "B1")
    add_batch(client, "B2", line_id="L2", lots=["RM-M"], declared=["milk"])
    create_verification(client, "V1", ["B1"])
    create_verification(client, "V2", ["B1", "B2"])
    confirm(client, "V1")
    confirm(client, "V2")

    service = client.app.state.service
    store = client.app.state.store
    rlock = store._write_lock
    raw = store._conn
    inside = threading.Event()
    release = threading.Event()
    hold = {"on": True}

    @contextmanager
    def instrumented_write():
        with rlock:
            if hold["on"]:
                hold["on"] = False
                inside.set()
                release.wait(timeout=10)
            raw.execute("BEGIN IMMEDIATE")
            try:
                yield raw
                raw.execute("COMMIT")
            except Exception:
                try:
                    raw.execute("ROLLBACK")
                except sqlite3.OperationalError:
                    pass
                raise

    monkeypatch.setattr(store, "write", instrumented_write)

    results: dict[str, tuple] = {}

    def run(key, hno, vno):
        try:
            payload = service.create_handover(hno, vno, 2)
            results[key] = (201, payload["handover_no"])
        except Exception as exc:  # APIError
            results[key] = (exc.status_code, exc.code)

    t1 = threading.Thread(target=run, args=("a", "H1", "V1"))
    t1.start()
    inside.wait(timeout=5)  # 第一个交接进入临界区并暂停
    t2 = threading.Thread(target=run, args=("b", "H2", "V2"))
    t2.start()
    t2.join(timeout=0.5)
    assert t2.is_alive()  # 串行等待写锁
    release.set()
    t1.join(timeout=10)
    t2.join(timeout=10)
    assert not t1.is_alive() and not t2.is_alive()

    statuses = {k: v[0] for k, v in results.items()}
    assert sorted(statuses.values()) == [201, 409], results
    failed_key = "a" if statuses["a"] == 409 else "b"
    assert results[failed_key][1] == "BATCH_ALREADY_SHIPPED"

    # 全局只有一次成功交接；B2 属于失败单 V2，不得被占用
    occupied = {}
    with store.read() as db:
        for r in db.execute(
            "SELECT batch_no, handover_no FROM shipment_handover_occupancy"
        ).fetchall():
            occupied[r["batch_no"]] = r["handover_no"]
    assert occupied == {"B1": "H1"}
    assert client.get("/api/batches/B2/handover").json()["shipped"] is False


# ---------------------------------------------------------------- 不可变与追溯


def test_post_handover_correction_does_not_rewrite_handover(client):
    release_batches(client, "B1")
    create_verification(client, "V1", ["B1"])
    confirm(client, "V1")
    assert handover(client, "H1", "V1", 2).status_code == 201

    # 交接后更正批次 -> 已确认单据转待复核，但交接记录冻结不改
    r = client.put("/api/batches/B1", json={"declared_allergens": ["milk", "egg"]})
    assert r.status_code == 200
    assert client.get("/api/verifications/V1").json()["status"] == "PENDING_REVIEW"

    h = client.get("/api/handovers/H1").json()
    assert h["verification_revision"] == 2
    assert h["batches"][0]["adjudication"]["version"] == 1
    assert h["batches"][0]["adjudication"]["disposition"] == "RELEASE"
    # 归属追溯仍指向 H1
    assert client.get("/api/batches/B1/handover").json()["handover"]["handover_no"] == "H1"

    # 重新核验 + 清单修订（清单元素不变）也不改写交接记录
    client.post("/api/verifications/V1/reverify")
    doc = client.get("/api/verifications/V1").json()
    r = client.post(
        "/api/verifications/V1/revise",
        json={"expected_revision": doc["revision"], "batch_nos": ["B1"]},
    )
    assert r.status_code == 200
    h = client.get("/api/handovers/H1").json()
    assert h["verification_revision"] == 2
    assert h["batches"][0]["adjudication"]["version"] == 1


def test_list_and_filter_and_trace_queries(client):
    release_batches(client, "B1", "B2")
    create_verification(client, "V1", ["B1"])
    create_verification(client, "V2", ["B2"])
    confirm(client, "V1")
    confirm(client, "V2")
    assert handover(client, "H1", "V1", 2).status_code == 201
    assert handover(client, "H2", "V2", 2).status_code == 201

    # 列表按交接时间排序，摘要含成员顺序/批号，不含快照详情
    body = client.get("/api/handovers").json()["handovers"]
    assert [h["handover_no"] for h in body] == ["H1", "H2"]
    assert body[0]["batches"] == [{"position": 1, "batch_no": "B1"}]
    assert body[0]["batch_count"] == 1

    # 按核验单过滤
    body = client.get("/api/handovers?verification_no=V2").json()["handovers"]
    assert [h["handover_no"] for h in body] == ["H2"]

    # 不存在的交接 / 批次
    assert client.get("/api/handovers/NOPE").status_code == 404
    r = client.get("/api/batches/GHOST/handover")
    assert r.status_code == 404 and r.json()["error"]["code"] == "BATCH_NOT_FOUND"


# ---------------------------------------------------------------- 旧库升级


def test_legacy_database_without_handover_tables_upgrades_and_works(tmp_path):
    """无交接表的旧库启动后幂等建表，既有单据可确认并交接。"""
    db_path = tmp_path / "old.db"
    conn = sqlite3.connect(db_path)
    conn.executescript(
        """
        CREATE TABLE allergen (code TEXT PRIMARY KEY, name_zh TEXT NOT NULL);
        CREATE TABLE material_lot (lot_no TEXT PRIMARY KEY, material TEXT NOT NULL,
            allergens TEXT NOT NULL DEFAULT '[]', updated_at TEXT NOT NULL);
        CREATE TABLE line (line_id TEXT PRIMARY KEY, created_at TEXT NOT NULL);
        CREATE TABLE batch (batch_no TEXT PRIMARY KEY, line_id TEXT NOT NULL,
            seq INTEGER NOT NULL, material_lots TEXT NOT NULL DEFAULT '[]',
            declared TEXT NOT NULL DEFAULT '[]', cleaned TEXT NOT NULL DEFAULT '[]',
            created_at TEXT NOT NULL);
        CREATE TABLE batch_material (batch_no TEXT NOT NULL, lot_no TEXT NOT NULL,
            PRIMARY KEY (batch_no, lot_no));
        CREATE TABLE material_lot_lock (lot_no TEXT PRIMARY KEY, locked INTEGER NOT NULL,
            reason TEXT NOT NULL, version INTEGER NOT NULL DEFAULT 0, updated_at TEXT NOT NULL);
        CREATE TABLE material_lot_lock_version (id INTEGER PRIMARY KEY AUTOINCREMENT,
            lot_no TEXT NOT NULL, version INTEGER NOT NULL, locked INTEGER NOT NULL,
            action TEXT NOT NULL, reason TEXT NOT NULL, changed_at TEXT NOT NULL);
        CREATE TABLE shipment_eligibility (batch_no TEXT PRIMARY KEY, version INTEGER NOT NULL DEFAULT 1,
            status TEXT NOT NULL, locked_lots TEXT NOT NULL DEFAULT '[]',
            reasons TEXT NOT NULL DEFAULT '[]', updated_at TEXT NOT NULL);
        CREATE TABLE shipment_eligibility_version (id INTEGER PRIMARY KEY AUTOINCREMENT,
            batch_no TEXT NOT NULL, version INTEGER NOT NULL, status TEXT NOT NULL,
            locked_lots TEXT NOT NULL DEFAULT '[]', reasons TEXT NOT NULL DEFAULT '[]',
            trigger TEXT NOT NULL, changed_at TEXT NOT NULL);
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
            revision INTEGER NOT NULL DEFAULT 1, created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL, confirmed_at TEXT);
        CREATE TABLE verification_batch (verification_no TEXT NOT NULL, batch_no TEXT NOT NULL,
            position INTEGER NOT NULL, snapshot_version INTEGER NOT NULL,
            snapshot_disposition TEXT NOT NULL, snapshot_actual TEXT NOT NULL DEFAULT '[]',
            snapshot_undeclared TEXT NOT NULL DEFAULT '[]', snapshot_reasons TEXT NOT NULL DEFAULT '[]',
            PRIMARY KEY (verification_no, batch_no));
        CREATE TABLE verification_confirmation (id INTEGER PRIMARY KEY AUTOINCREMENT,
            verification_no TEXT NOT NULL, confirmed_at TEXT NOT NULL);
        CREATE TABLE verification_confirmation_batch (confirmation_id INTEGER NOT NULL,
            verification_no TEXT NOT NULL, position INTEGER NOT NULL, batch_no TEXT NOT NULL,
            snapshot_version INTEGER NOT NULL, snapshot_disposition TEXT NOT NULL,
            snapshot_actual TEXT NOT NULL DEFAULT '[]', snapshot_undeclared TEXT NOT NULL DEFAULT '[]',
            snapshot_reasons TEXT NOT NULL DEFAULT '[]');
        INSERT INTO allergen(code,name_zh) VALUES ('milk','乳');
        """
    )
    conn.commit()
    conn.close()

    app = create_app(db_path=str(db_path), seed_demo=False)
    with TestClient(app) as c:
        # 建表幂等：交接表已就位
        store = app.state.store
        with store.read() as db:
            names = {
                r["name"]
                for r in db.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                ).fetchall()
            }
        assert {
            "shipment_handover",
            "shipment_handover_batch",
            "shipment_handover_occupancy",
        } <= names

        add_lot(c, "RM-M", "奶粉", ["milk"])
        add_batch(c, "B1", lots=["RM-M"], declared=["milk"])
        create_verification(c, "V1", ["B1"])
        confirm(c, "V1")
        resp = handover(c, "H1", "V1", 2)
        assert resp.status_code == 201, resp.text
        assert resp.json()["verification_revision"] == 2
        assert c.get("/api/batches/B1/handover").json()["shipped"] is True

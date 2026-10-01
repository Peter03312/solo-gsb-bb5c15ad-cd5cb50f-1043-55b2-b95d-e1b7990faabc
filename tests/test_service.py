"""换线放行服务测试。

覆盖：放行、未声明扣留、清洁仅覆盖确认项、上一批（含被扣留）残留传递、
原料批号/批次更正重算、历史裁决版本、各类拒绝与原子性、多产线隔离。
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.main import create_app


@pytest.fixture()
def client(tmp_path):
    db_file = tmp_path / "test.db"
    app = create_app(db_path=str(db_file), seed_demo=False)
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
    return client.post(
        "/api/batches",
        json={
            "batch_no": batch_no,
            "line_id": line_id,
            "material_lots": lots or [],
            "declared_allergens": declared or [],
            "cleaned_allergens": cleaned or [],
        },
    )


# ---------------------------------------------------------------- 基础裁决


def test_allergen_dictionary_seeded(client):
    resp = client.get("/api/allergens")
    codes = {a["code"] for a in resp.json()["allergens"]}
    assert {"milk", "peanut", "gluten", "sesame"} <= codes


def test_release_when_declared_complete(client):
    add_lot(client, "RM-M", "奶粉", ["milk"])
    resp = add_batch(client, "B1", lots=["RM-M"], declared=["milk"])
    assert resp.status_code == 201
    body = resp.json()
    adj = body["adjudication"]
    assert adj["disposition"] == "RELEASE"
    assert adj["actual_allergens"] == ["milk"]
    assert adj["undeclared_allergens"] == []
    assert adj["version"] == 1


def test_hold_when_undeclared(client):
    add_lot(client, "RM-P", "花生酱", ["peanut"])
    resp = add_batch(client, "B1", lots=["RM-P"], declared=[])
    adj = resp.json()["adjudication"]
    assert adj["disposition"] == "HOLD"
    assert adj["undeclared_allergens"] == ["peanut"]
    assert any(r["code"] == "UNDECLARED_ALLERGEN" for r in adj["reasons"])


def test_cleaning_only_removes_confirmed_and_hold_still_carries(client):
    # B1：milk + peanut，只声明 milk -> peanut 未声明，扣留
    add_lot(client, "RM-M", "奶粉", ["milk"])
    add_lot(client, "RM-P", "花生酱", ["peanut"])
    add_batch(client, "B1", lots=["RM-M", "RM-P"], declared=["milk"])

    # B2：无过敏原原料；本次清洁只确认清除 milk
    resp = add_batch(client, "B2", lots=[], declared=[], cleaned=["milk"])
    adj = resp.json()["adjudication"]
    # milk 被清洁清除；peanut 未被清洁覆盖，从上一批（被扣留）继续传递
    assert adj["actual_allergens"] == ["peanut"]
    assert adj["disposition"] == "HOLD"
    assert "上一批残留" in adj["sources"]["peanut"][0]


def test_confirmed_cleaning_allows_release_next_batch(client):
    add_lot(client, "RM-P", "花生酱", ["peanut"])
    add_batch(client, "B1", lots=["RM-P"], declared=["peanut"])  # 放行，但产线残留 peanut
    resp = add_batch(client, "B2", lots=[], declared=[], cleaned=["peanut"])
    adj = resp.json()["adjudication"]
    assert adj["actual_allergens"] == []
    assert adj["disposition"] == "RELEASE"


def test_material_reintroduction_after_cleaning(client):
    # 清洁清掉了 milk，但本批原料再次引入 milk，仍计入实际
    add_lot(client, "RM-M", "奶粉", ["milk"])
    add_batch(client, "B1", lots=["RM-M"], declared=["milk"])
    resp = add_batch(
        client, "B2", lots=["RM-M"], declared=[], cleaned=["milk"]
    )
    adj = resp.json()["adjudication"]
    assert adj["actual_allergens"] == ["milk"]
    assert adj["disposition"] == "HOLD"
    assert "原料" in adj["sources"]["milk"][0]


# ---------------------------------------------------------------- 更正重算


def test_cleaning_correction_recomputes_current_and_following(client):
    add_lot(client, "RM-P", "花生酱", ["peanut"])
    add_batch(client, "B1", lots=["RM-P"], declared=[])            # 扣留，peanut 残留
    add_batch(client, "B2", lots=[], declared=[])                  # 继承 peanut，扣留
    add_batch(client, "B3", lots=[], declared=[])                  # 继续继承，扣留

    # 更正 B2 开工前的清洁核验：确认清除上一批 B1 留下的 peanut
    resp = client.put(
        "/api/batches/B2/cleaning", json={"cleaned_allergens": ["peanut"]}
    )
    assert resp.status_code == 200
    recomputed = {r["batch_no"]: r for r in resp.json()["recomputed"]}
    # B1 未在更正点之前，保持原状（仍扣留）
    assert "B1" not in recomputed
    # B2：peanut 被本次清洁覆盖，且本批无原料 -> 放行
    assert recomputed["B2"]["disposition"] == "RELEASE"
    assert recomputed["B2"]["actual"] == []
    # B3：B2 已无残留可传递，自动连带变为放行
    assert recomputed["B3"]["disposition"] == "RELEASE"
    assert recomputed["B3"]["actual"] == []


def test_material_lot_correction_recomputes_across_batches(client):
    add_lot(client, "RM-P", "花生酱", ["peanut"])
    add_batch(client, "B1", lots=["RM-P"], declared=["peanut"])
    add_batch(client, "B2", lots=["RM-P"], declared=["peanut"])

    # 原料批号过敏原更正为 [peanut, sesame]：两批都未声明 sesame，应全部扣留
    resp = client.put("/api/material-lots/RM-P", json={"allergens": ["peanut", "sesame"]})
    assert resp.status_code == 200
    recomputed = resp.json()["recomputed"]
    assert [r["batch_no"] for r in recomputed] == ["B1", "B2"]  # 从最早引用点按序
    assert all(r["disposition"] == "HOLD" for r in recomputed)
    assert all("sesame" in r["undeclared"] for r in recomputed)

    # 更正回仅 peanut，两批恢复放行
    resp = client.put("/api/material-lots/RM-P", json={"allergens": ["peanut"]})
    assert all(r["disposition"] == "RELEASE" for r in resp.json()["recomputed"])


def test_batch_correction_change_materials_and_declared(client):
    add_lot(client, "RM-M", "奶粉", ["milk"])
    add_lot(client, "RM-E", "蛋液", ["egg"])
    add_batch(client, "B1", lots=["RM-M"], declared=["milk"])
    add_batch(client, "B2", lots=[], declared=[], cleaned=["milk"])

    # B1 更正：原料换成蛋液（声明 egg），并在开工前清洁掉旧的 milk 残留
    resp = client.put(
        "/api/batches/B1",
        json={
            "material_lots": ["RM-E"],
            "declared_allergens": ["egg"],
            "cleaned_allergens": ["milk"],
        },
    )
    assert resp.status_code == 200
    recomputed = {r["batch_no"]: r for r in resp.json()["recomputed"]}
    assert recomputed["B1"]["disposition"] == "RELEASE"
    # B2 开工前虽清洁了 milk，但 B1 现在留下的是 egg（清洁清单未覆盖 egg）-> egg 残留
    assert recomputed["B2"]["actual"] == ["egg"]
    assert recomputed["B2"]["disposition"] == "HOLD"

    # 再更正 B2 的清洁核验，覆盖 egg -> B2 放行
    resp = client.put("/api/batches/B2/cleaning", json={"cleaned_allergens": ["egg"]})
    recomputed = {r["batch_no"]: r for r in resp.json()["recomputed"]}
    assert recomputed["B2"]["actual"] == []
    assert recomputed["B2"]["disposition"] == "RELEASE"


def test_history_keeps_all_versions(client):
    add_lot(client, "RM-P", "花生酱", ["peanut"])
    add_batch(client, "B1", lots=["RM-P"], declared=[])
    client.put("/api/batches/B1", json={"declared_allergens": ["peanut"]})
    client.put("/api/batches/B1/cleaning", json={"cleaned_allergens": ["peanut"]})

    resp = client.get("/api/batches/B1/history")
    history = resp.json()["history"]
    assert [v["version"] for v in history] == [1, 2, 3]
    assert [v["trigger"] for v in history] == ["REGISTER", "CORRECT_BATCH", "CORRECT_BATCH"]
    assert history[0]["disposition"] == "HOLD"
    assert history[1]["disposition"] == "RELEASE"


def test_name_only_lot_correction_does_not_recompute(client):
    add_lot(client, "RM-P", "旧名称", ["peanut"])
    add_batch(client, "B1", lots=["RM-P"], declared=["peanut"])

    resp = client.put("/api/material-lots/RM-P", json={"material": "新名称"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["corrected"]["allergens"] == ["peanut"]
    assert body["recomputed"] == []  # 过敏原未变，不产生新裁决版本
    assert client.get("/api/material-lots/RM-P").json()["material"] == "新名称"
    assert client.get("/api/batches/B1").json()["adjudication"]["version"] == 1


def test_material_lot_correction_bumps_downstream_versions(client):
    add_lot(client, "RM-P", "花生酱", ["peanut"])
    add_batch(client, "B1", lots=["RM-P"], declared=["peanut"])
    add_batch(client, "B2", lots=[], declared=[], cleaned=["peanut"])
    client.put("/api/material-lots/RM-P", json={"allergens": ["peanut", "sesame"]})

    h1 = client.get("/api/batches/B1/history").json()["history"]
    h2 = client.get("/api/batches/B2/history").json()["history"]
    assert h1[-1]["trigger"] == "CORRECT_LOT"
    assert h2[-1]["trigger"] == "CORRECT_LOT"
    assert h1[-1]["version"] == 2 and h2[-1]["version"] == 2


# ---------------------------------------------------------------- 拒绝与原子性


def test_duplicate_batch_rejected(client):
    add_lot(client, "RM-M", "奶粉", ["milk"])
    add_batch(client, "B1", lots=["RM-M"], declared=["milk"])
    resp = add_batch(client, "B1", lots=["RM-M"], declared=["milk"])
    assert resp.status_code == 409
    assert resp.json()["error"]["code"] == "DUPLICATE_BATCH"


def test_duplicate_material_lot_rejected(client):
    add_lot(client, "RM-M", "奶粉", ["milk"])
    resp = client.post(
        "/api/material-lots",
        json={"lot_no": "RM-M", "material": "重复", "allergens": []},
    )
    assert resp.status_code == 409


def test_unknown_material_lot_rejected_without_partial_write(client):
    before = len(client.get("/api/batches").json()["batches"])
    resp = add_batch(client, "B1", lots=["RM-404"], declared=[], cleaned=[])
    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == "UNKNOWN_MATERIAL_LOT"
    # 失败后不得留下批次、产线计数也不得变化
    after = len(client.get("/api/batches").json()["batches"])
    assert before == after
    assert client.get("/api/batches/B1").status_code == 404


def test_invalid_allergen_rejected(client):
    resp = client.post(
        "/api/material-lots",
        json={"lot_no": "RM-X", "material": "x", "allergens": ["shellfish"]},
    )
    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == "INVALID_ALLERGEN"

    add_lot(client, "RM-M", "奶粉", ["milk"])
    resp = add_batch(client, "B1", lots=["RM-M"], declared=["nuts"])
    assert resp.status_code == 400

    # 原料更正中的非法过敏原不得改动原记录
    client.put("/api/material-lots/RM-M", json={"allergens": ["bogus"]})
    assert client.get("/api/material-lots/RM-M").json()["allergens"] == ["milk"]


def test_correction_target_not_found(client):
    resp = client.put(
        "/api/batches/NOPE/cleaning", json={"cleaned_allergens": ["milk"]}
    )
    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "CORRECTION_TARGET_NOT_FOUND"

    resp = client.put("/api/material-lots/NOPE", json={"allergens": ["milk"]})
    assert resp.status_code == 404


def test_failed_lot_correction_is_atomic(client):
    add_lot(client, "RM-P", "花生酱", ["peanut"])
    add_batch(client, "B1", lots=["RM-P"], declared=["peanut"])
    v_before = client.get("/api/batches/B1").json()["adjudication"]["version"]

    # 非法过敏原 -> 整笔更正回滚：名称不变、不产生新版本
    resp = client.put(
        "/api/material-lots/RM-P",
        json={"material": "不应生效的名称", "allergens": ["nope"]},
    )
    assert resp.status_code == 400
    assert client.get("/api/material-lots/RM-P").json()["material"] == "花生酱"
    assert client.get("/api/batches/B1").json()["adjudication"]["version"] == v_before
    assert len(client.get("/api/batches/B1/history").json()["history"]) == v_before


# ---------------------------------------------------------------- 顺序与隔离


def test_batches_must_follow_line_order(client):
    add_lot(client, "RM-M", "奶粉", ["milk"])
    add_batch(client, "B1", lots=["RM-M"], declared=["milk"])
    resp = add_batch(client, "B2", lots=["RM-M"], declared=["milk"])
    assert resp.status_code == 201  # 自动 seq=2
    assert resp.json()["seq"] == 2
    lines = {l["line_id"]: l for l in client.get("/api/lines").json()["lines"]}
    assert lines["L1"]["batch_count"] == 2


def test_lines_are_isolated(client):
    add_lot(client, "RM-P", "花生酱", ["peanut"])
    # L1 留下 peanut 残留
    add_batch(client, "A1", line_id="L1", lots=["RM-P"], declared=["peanut"])
    # L2 首产线无任何带入
    resp = add_batch(client, "C1", line_id="L2", lots=[], declared=[])
    assert resp.json()["adjudication"]["actual_allergens"] == []
    assert resp.json()["adjudication"]["disposition"] == "RELEASE"

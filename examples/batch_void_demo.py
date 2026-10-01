#!/usr/bin/env python3
"""食品换线放行服务 —— 批次作废端到端调用示例（仅使用 Python 标准库）。

演示流程（独立产线 LVOID，避免与其他示例的批次冲突）：
  1. 登记原料批号 VB-RM-P（peanut）与三个批次：
       OB1 使用 VB-RM-P、声明 peanut -> RELEASE，peanut 残留在产线
       OB2 开工前清洁 peanut、无原料 -> RELEASE（产线被清洁干净）
       OB3 无原料无清洁 -> RELEASE（无残留可继承）
  2. 创建并确认出货核验单 OV-<时间戳>-1：[OB3]
  3. 拒绝场景：未知批次作废 404、空原因 400
  4. 作废 OB2（误登记、实际未投产，填写原因）：
       作废保留原登记/历史裁决/出货资格与作废原因可查，批次号不可复用；
       OB2 退出同线残留链（其清洁不再发生），OB3 按原顺序重算追加 v2，
       重新从 OB1 继承 peanut -> HOLD；OB1 不受影响；
       已确认核验单 OV 转 PENDING_REVIEW（OB3 产生新版本）。
  5. 作废批次不得更正（409 BATCH_VOIDED）、批次号不可复用（409 DUPLICATE_BATCH）。
  6. 重复作废：changed=false，返回既有原因、不重算。
  7. 作废 OB1：已确认核验单因含作废批次（且关联作废批次本身）继续为待复核，
       含作废批次的单据可查询，但重新核验与确认均拒绝（409）；
       新核验单含作废批次也被拒绝（400 BATCH_VOIDED）。
  8. 查询：批次详情/列表/历史标明作废状态；当前出货资格统计排除作废批次，
       单批资格冻结可查；原料批号锁定不联动作废批次。

用法：
    python examples/batch_void_demo.py
    BASE_URL=http://192.168.1.10:8080 python examples/batch_void_demo.py
"""
from __future__ import annotations

import json
import os
import sys
import time
import urllib.error
import urllib.request

BASE_URL = os.environ.get("BASE_URL", "http://127.0.0.1:8080")
LINE = "LVOID"


def call(method: str, path: str, payload: dict | None = None) -> tuple[int, dict]:
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8") if payload is not None else None
    req = urllib.request.Request(
        BASE_URL + path,
        data=data,
        method=method,
        headers={"Content-Type": "application/json; charset=utf-8"},
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read().decode("utf-8"))


def section(title: str) -> None:
    print("\n" + "=" * 72)
    print(title)
    print("=" * 72)


def show(label: str, status: int, body: dict) -> None:
    print(f"[{status}] {label}")
    print(json.dumps(body, ensure_ascii=False, indent=2))


def ensure_lot(lot_no: str, material: str, allergens: list[str]) -> None:
    status, body = call("POST", "/api/material-lots", {
        "lot_no": lot_no, "material": material, "allergens": allergens,
    })
    if status == 201:
        print(f"  已登记原料批号 {lot_no}（{material}，{allergens}）")
    elif status == 409:
        print(f"  原料批号 {lot_no} 已存在，继续")
    else:
        raise RuntimeError(f"登记原料批号失败: {body}")


def ensure_batch(batch_no: str, payload: dict) -> None:
    status, body = call("POST", "/api/batches", payload)
    if status == 201:
        adj = body["adjudication"]
        print(f"  已登记批次 {batch_no} => {adj['disposition']}（v{adj['version']}）")
    elif status == 409:
        print(f"  批次 {batch_no} 已存在，继续")
    else:
        raise RuntimeError(f"登记批次失败: {body}")


def main() -> int:
    print(f"目标服务：{BASE_URL}")
    status, _ = call("GET", "/health")
    if status != 200:
        print(f"服务不可用（/health 返回 {status}），请先启动服务。", file=sys.stderr)
        return 1
    print("健康检查通过")

    suffix = str(int(time.time()))
    lot = "VB-RM-P"

    section("1. 准备数据：OB1（声明 peanut，RELEASE 但残留）、OB2（清洁 peanut）、OB3")
    ensure_lot(lot, "作废示例花生酱", ["peanut"])
    ensure_batch("OB1", {
        "batch_no": "OB1", "line_id": LINE,
        "material_lots": [lot],
        "declared_allergens": ["peanut"], "cleaned_allergens": [],
    })
    ensure_batch("OB2", {
        "batch_no": "OB2", "line_id": LINE,
        "material_lots": [],
        "declared_allergens": [], "cleaned_allergens": ["peanut"],
    })
    ensure_batch("OB3", {
        "batch_no": "OB3", "line_id": LINE,
        "material_lots": [],
        "declared_allergens": [], "cleaned_allergens": [],
    })

    doc_no = f"OV-{suffix}-1"
    section(f"2. 创建并确认核验单 {doc_no}：[OB3]")
    status, doc = call("POST", "/api/verifications", {
        "verification_no": doc_no, "batch_nos": ["OB3"],
    })
    assert status == 201, doc
    status, doc = call("POST", f"/api/verifications/{doc_no}/confirm")
    show("确认（预期 200 CONFIRMED）", status, doc)
    assert status == 200 and doc["status"] == "CONFIRMED", doc

    section("3. 拒绝场景：未知批次 404 / 空原因 400")
    status, body = call("POST", "/api/batches/NO-SUCH-BATCH/void", {"reason": "x"})
    print(f"[{status}] 作废未知批次: {body['error']['code']}")
    assert status == 404 and body["error"]["code"] == "BATCH_NOT_FOUND", body
    status, body = call("POST", "/api/batches/OB1/void", {"reason": "   "})
    print(f"[{status}] 空原因作废: {body['error']['code']}")
    assert status == 400 and body["error"]["code"] == "INVALID_PAYLOAD", body
    # 失败不留部分版本
    assert call("GET", "/api/batches/OB1")[1]["status"] == "ACTIVE"

    section("4. 作废 OB2（误登记、实际未投产）：其后 OB3 按序重算")
    status, voided = call("POST", "/api/batches/OB2/void", {
        "reason": "计划误登记，该批实际未投产",
    })
    show("作废结果（OB3 重算追加 v2、已确认单据转待复核）", status, voided)
    assert status == 200 and voided["changed"] is True, voided
    assert voided["voided"]["status"] == "VOIDED"
    rec = {s["batch_no"]: s for s in voided["recomputed"]}
    assert list(rec) == ["OB3"], rec
    # OB2 的清洁不再发生 -> OB3 重新继承 OB1 的 peanut 残留 -> HOLD
    assert rec["OB3"]["disposition"] == "HOLD"
    assert rec["OB3"]["actual"] == ["peanut"]
    assert rec["OB3"]["version"] == 2
    # 作废批次自身不产生新裁决版本
    assert call("GET", "/api/batches/OB2")[1]["adjudication"]["version"] == 1
    # 已确认单据转待复核
    affected = {v["verification_no"]: v["status"] for v in voided["affected_verifications"]}
    assert affected == {doc_no: "PENDING_REVIEW"}, affected
    assert call("GET", f"/api/verifications/{doc_no}")[1]["status"] == "PENDING_REVIEW"

    section("5. 作废批次不得更正、批次号不可复用")
    status, body = call("PUT", "/api/batches/OB2", {"declared_allergens": ["peanut"]})
    print(f"[{status}] 更正作废批次: {body['error']['code']}")
    assert status == 409 and body["error"]["code"] == "BATCH_VOIDED", body
    status, body = call("PUT", "/api/batches/OB2/cleaning", {"cleaned_allergens": ["peanut"]})
    print(f"[{status}] 清洁更正作废批次: {body['error']['code']}")
    assert status == 409 and body["error"]["code"] == "BATCH_VOIDED", body
    status, body = call("POST", "/api/batches", {
        "batch_no": "OB2", "line_id": LINE,
        "material_lots": [], "declared_allergens": [], "cleaned_allergens": [],
    })
    print(f"[{status}] 复用作废批次号: {body['error']['code']}")
    assert status == 409 and body["error"]["code"] == "DUPLICATE_BATCH", body

    section("6. 重复作废：幂等返回既有结果，不重算")
    status, again = call("POST", "/api/batches/OB2/void", {"reason": "再次作废"})
    show("重复作废（预期 changed=false、原因保留、recomputed 为空）", status, again)
    assert status == 200, again
    assert again["changed"] is False
    assert again["reason"] == "计划误登记，该批实际未投产"
    assert again["recomputed"] == []
    assert call("GET", "/api/batches/OB3")[1]["adjudication"]["version"] == 2

    section("7. 作废 OB1：含作废批次的单据重新核验/确认均拒绝")
    status, voided = call("POST", "/api/batches/OB1/void", {"reason": "重复登记，未投产"})
    assert status == 200 and voided["changed"] is True, voided

    # 新核验单含作废批次 -> 400
    status, body = call("POST", "/api/verifications", {
        "verification_no": f"OV-{suffix}-NEW", "batch_nos": ["OB1"],
    })
    show("新核验单含作废批次（预期 400 BATCH_VOIDED）", status, body)
    assert status == 400 and body["error"]["code"] == "BATCH_VOIDED", body

    # 已有单据：可查询，但重新核验与确认均拒绝
    status, doc = call("GET", f"/api/verifications/{doc_no}")
    print(f"[{status}] 单据仍可查询：status={doc['status']}，"
          f"contains_voided_batches={doc['contains_voided_batches']}，"
          f"voided_batches={doc['voided_batches']}")
    # OB3 未作废；OB1 不在该单据内 —— 该单据不含作废批次，但因 OB3 新版本待复核
    assert doc["contains_voided_batches"] is False

    # 再建一张含作废批次的历史单据场景：直接对 OB3 作废
    status, body = call("POST", "/api/batches/OB3/void", {"reason": "未投产"})
    assert status == 200, body
    status, body = call("POST", f"/api/verifications/{doc_no}/reverify")
    show("含作废批次单据重新核验（预期 409）", status, body)
    assert status == 409 and body["error"]["code"] == "VERIFICATION_CONTAINS_VOIDED_BATCH", body
    assert body["error"]["details"][0]["batch_no"] == "OB3"
    status, body = call("POST", f"/api/verifications/{doc_no}/confirm")
    show("含作废批次单据确认（预期 409）", status, body)
    assert status == 409 and body["error"]["code"] == "VERIFICATION_CONTAINS_VOIDED_BATCH", body
    # 历次确认快照不改
    doc = call("GET", f"/api/verifications/{doc_no}")[1]
    assert len(doc["confirmations"]) == 1
    assert doc["confirmations"][0]["batches"][0]["version"] == 1

    section("8. 查询：作废状态标明；资格统计排除作废批次；锁定不联动作废批次")
    status, batches = call("GET", f"/api/batches?line_id={LINE}")
    for b in batches["batches"]:
        print(f"  {b['batch_no']}: status={b['status']} voided={b['voided']} "
              f"裁决v{b['adjudication']['version'] if b['adjudication'] else None} "
              f"原因={b['voided_reason']}")
    assert all(b["voided"] for b in batches["batches"])  # OB1/OB2/OB3 均已作废

    # 当前出货资格统计：全部批次作废 -> 空
    status, elig = call("GET", f"/api/eligibilities?line_id={LINE}")
    show("当前出货资格统计（作废批次不参与，预期为空）", status, elig)
    assert elig["eligibilities"] == []
    # 单批资格冻结可查
    e1 = call("GET", "/api/batches/OB1/eligibility")[1]
    assert e1["voided"] is True and e1["eligibility"]["version"] == 1

    # 批号锁定：直接使用的 OB1 已作废，不产生资格联动
    status, locked = call("POST", f"/api/material-lots/{lot}/lock", {"reason": "待检"})
    show("锁定期间作废批次不联动（affected_batches 预期为空）", status, locked)
    assert status == 200 and locked["affected_batches"] == [], locked
    # 作废批次历史仍可查
    h = call("GET", "/api/batches/OB1/history")[1]
    assert h["voided"] is True and [v["trigger"] for v in h["history"]] == ["REGISTER"]

    section("完成")
    print(f"作废批次 OB1/OB2/OB3 保留可查（登记、历史裁决、资格与作废原因），批次号不可复用。")
    print(f"核验单 {doc_no} 可查询但不可重新核验/确认；历次确认快照未改动。")
    print("可继续在交互式文档中探索： %s/docs" % BASE_URL)
    return 0


if __name__ == "__main__":
    sys.exit(main())

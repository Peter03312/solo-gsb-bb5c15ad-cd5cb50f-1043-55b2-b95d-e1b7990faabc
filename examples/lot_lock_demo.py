#!/usr/bin/env python3
"""食品换线放行服务 —— 原料批号待检锁定端到端调用示例（仅使用 Python 标准库）。

演示流程（独立产线 LLOCK，避免与其他示例的批次冲突）：
  1. 登记原料批号 LK-RM-P（peanut）与三个批次：
       LB1 产线 L1：直接使用 LK-RM-P，声明 peanut -> 过敏原裁决 RELEASE
       LB2 同线后续：不使用任何批号（清洁 peanut），用于验证残留不连带锁定
       LC1 另一条产线 L2：直接使用 LK-RM-P
  2. 创建并确认出货核验单 LV-<时间戳>-1：[LB1, LB2]
  3. 拒绝场景：未知批号锁定 404、空原因 400
  4. 锁定 LK-RM-P（填写原因）：
       直接使用的 LB1/LC1 出货资格 BLOCKED、资格版本递增（过敏原裁决保持 RELEASE）；
       LB2 未直接使用，不连带锁定、版本不变；
       已确认核验单自动转 PENDING_REVIEW。
  5. 重复提交相同锁定状态：changed=false，不产生新变更。
  6. 待复核核验单确认被拒（STALE_ELIGIBILITY + MATERIAL_LOT_LOCKED 逐批原因）。
  7. 解除锁定：直接使用批次资格版本继续递增、恢复 SHIPPABLE；
     重新核验刷新快照后再次确认成功，历次确认快照保留。
  8. 查询：批号锁定状态与历次变更、逐批出货资格及历史版本。

用法：
    python examples/lot_lock_demo.py
    BASE_URL=http://192.168.1.10:8080 python examples/lot_lock_demo.py
"""
from __future__ import annotations

import json
import os
import sys
import time
import urllib.error
import urllib.request

BASE_URL = os.environ.get("BASE_URL", "http://127.0.0.1:8080")


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


def ensure_batch(batch_no: str, line_id: str, payload_extra: dict) -> None:
    payload = {"batch_no": batch_no, "line_id": line_id, **payload_extra}
    status, body = call("POST", "/api/batches", payload)
    if status == 201:
        adj = body["adjudication"]
        ship = body["shipment_eligibility"]
        print(
            f"  已登记批次 {batch_no}（{line_id}）=> 裁决 {adj['disposition']} v{adj['version']}；"
            f"出货资格 {ship['status']} v{ship['version']}"
        )
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
    lot = "LK-RM-P"

    section("1. 准备数据：原料批号 + 直接使用批次 LB1/LC1 与同线不直接使用批次 LB2")
    ensure_lot(lot, "锁定示例花生酱", ["peanut"])
    ensure_batch("LB1", "LLOCK-1", {
        "material_lots": [lot],
        "declared_allergens": ["peanut"],
        "cleaned_allergens": [],
    })
    ensure_batch("LB2", "LLOCK-1", {
        # 清洁掉 LB1 留下的 peanut 残留；本批不直接使用任何原料批号
        "material_lots": [],
        "declared_allergens": [],
        "cleaned_allergens": ["peanut"],
    })
    ensure_batch("LC1", "LLOCK-2", {
        "material_lots": [lot],
        "declared_allergens": ["peanut"],
        "cleaned_allergens": [],
    })

    doc_no = f"LV-{suffix}-1"
    section(f"2. 创建并确认核验单 {doc_no}：[LB1, LB2]（锁定前全部具备出货资格）")
    status, doc = call("POST", "/api/verifications", {
        "verification_no": doc_no, "batch_nos": ["LB1", "LB2"],
    })
    assert status == 201, body_assert(doc)
    status, doc = call("POST", f"/api/verifications/{doc_no}/confirm")
    show("确认（预期 200 CONFIRMED）", status, doc)
    assert status == 200 and doc["status"] == "CONFIRMED", doc

    section("3. 拒绝场景：未知批号 404 / 空原因 400")
    status, body = call("POST", "/api/material-lots/NO-SUCH-LOT/lock", {"reason": "x"})
    print(f"[{status}] 锁定未知批号: {body['error']['code']}")
    assert status == 404 and body["error"]["code"] == "MATERIAL_LOT_NOT_FOUND", body
    status, body = call("POST", f"/api/material-lots/{lot}/lock", {"reason": "   "})
    print(f"[{status}] 空原因锁定: {body['error']['code']}")
    assert status == 400 and body["error"]["code"] == "INVALID_PAYLOAD", body

    section("4. 锁定原料批号（待检风险，填写原因）")
    status, locked = call("POST", f"/api/material-lots/{lot}/lock", {
        "reason": "供应商通报黄曲霉毒素风险，待复检",
    })
    show("锁定结果（直接使用批次资格版本递增、已确认单据转待复核）", status, locked)
    assert status == 200 and locked["changed"] is True, locked
    assert {a["batch_no"] for a in locked["affected_batches"]} == {"LB1", "LC1"}, locked
    affected = {v["verification_no"]: v["status"] for v in locked["affected_verifications"]}
    assert affected == {doc_no: "PENDING_REVIEW"}, affected

    section("5. 验证：过敏原裁决不变；同线未直接使用批次不连带锁定")
    for bn in ("LB1", "LB2", "LC1"):
        status, b = call("GET", f"/api/batches/{bn}")
        adj, ship = b["adjudication"], b["shipment_eligibility"]
        print(
            f"  {bn}: 裁决 {adj['disposition']} v{adj['version']}；"
            f"出货资格 {ship['status']} v{ship['version']}，锁定批号 {ship['locked_lots']}"
        )
    lb1 = call("GET", "/api/batches/LB1")[1]
    lb2 = call("GET", "/api/batches/LB2")[1]
    assert lb1["adjudication"]["disposition"] == "RELEASE"
    assert lb1["adjudication"]["version"] == 1           # 裁决版本不变
    assert lb1["shipment_eligibility"]["status"] == "BLOCKED"
    assert lb1["shipment_eligibility"]["version"] == 2
    assert lb2["shipment_eligibility"]["status"] == "SHIPPABLE"
    assert lb2["shipment_eligibility"]["version"] == 1   # 残留不连带

    section("6. 重复提交相同状态：幂等，不产生新变更")
    status, again = call("POST", f"/api/material-lots/{lot}/lock", {"reason": "再次锁定"})
    show("重复锁定（预期 changed=false，版本不递增）", status, again)
    assert status == 200 and again["changed"] is False and again["lock_version"] == 1, again

    section("7. 待复核核验单确认被拒：逐批返回全部原因，整单不确认")
    status, body = call("POST", f"/api/verifications/{doc_no}/confirm")
    show("确认（预期 422：STALE_ELIGIBILITY + MATERIAL_LOT_LOCKED）", status, body)
    assert status == 422, body
    codes = {(d["batch_no"], d["reason_code"]) for d in body["error"]["details"]}
    assert ("LB1", "STALE_ELIGIBILITY") in codes
    assert ("LB1", "MATERIAL_LOT_LOCKED") in codes
    assert not any(d["batch_no"] == "LB2" for d in body["error"]["details"]), body
    # 确认失败不写入新确认记录：历史仍只有锁定前的那一次确认
    assert len(call("GET", f"/api/verifications/{doc_no}")[1]["confirmations"]) == 1

    section("8. 解除锁定：直接使用批次资格恢复且版本递增；重新核验后再次确认")
    status, unlocked = call("POST", f"/api/material-lots/{lot}/unlock", {
        "reason": "复检合格，解除待检",
    })
    show("解除结果", status, unlocked)
    assert status == 200 and unlocked["changed"] is True, unlocked

    # 版本陈旧仍不能直接确认
    status, body = call("POST", f"/api/verifications/{doc_no}/confirm")
    print(f"[{status}] 未重新核验直接确认: {body['error']['code']}")
    assert status == 422 and body["error"]["details"][0]["reason_code"] == "STALE_ELIGIBILITY"

    status, body = call("POST", f"/api/verifications/{doc_no}/reverify")
    assert status == 200 and body["status"] == "DRAFT", body
    status, body = call("POST", f"/api/verifications/{doc_no}/confirm")
    show("重新核验后再次确认（预期 200，历次确认快照保留）", status, body)
    assert status == 200 and len(body["confirmations"]) == 2, body

    section("9. 查询批号锁定历史与逐批出货资格")
    status, lock_info = call("GET", f"/api/material-lots/{lot}/lock")
    show(f"{lot} 当前状态与历次变更", status, lock_info)
    assert [h["action"] for h in lock_info["history"]] == ["LOCK", "UNLOCK"], lock_info

    status, elig = call("GET", "/api/eligibilities?line_id=LLOCK-1")
    show("LLOCK-1 逐批出货资格", status, elig)
    rows = {e["batch_no"]: e["eligibility"] for e in elig["eligibilities"]}
    assert rows["LB1"]["status"] == "SHIPPABLE" and rows["LB1"]["version"] == 3
    assert rows["LB2"]["status"] == "SHIPPABLE" and rows["LB2"]["version"] == 1

    section("完成")
    print(f"核验单 {doc_no} 已二次确认；批号锁定/解除与出货资格历史均可查询。")
    print("可继续在交互式文档中探索： %s/docs" % BASE_URL)
    return 0


def body_assert(body: dict) -> None:
    """create 返回非 201 时让 assert 信息可读。"""
    if "verification_no" not in body:
        raise AssertionError(body)


if __name__ == "__main__":
    sys.exit(main())

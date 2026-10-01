#!/usr/bin/env python3
"""食品换线放行服务 —— 出货核验单批次清单修订（revise）端到端调用示例（仅标准库）。

演示流程（独立产线 LREVISE，核验单号带时间戳，可重复运行）：
  1. 登记原料批号与 3 个批次：RV001（RELEASE）、RV002（RELEASE）、RV003（RELEASE）
  2. 创建核验单 [RV001, RV002]，记录修订号 v1
  3. 装运批次变化：凭所见修订号提交完整新清单 [RV002, RV003, RV001]
       -> 同一事务重取当前裁决/出货资格快照、回到草稿、修订号 v2、顺序保留
  4. 并发的批次更正先落地：旧清单中 RV001 追加裁决版本（草稿不转态）
       -> 用过期修订号 v2 再修订被整体拒绝（409 VERIFICATION_REVISION_CONFLICT）
       -> 重新查询拿到 v3 与最新清单后，用当前修订号修订成功
  5. 整体拒绝演示：未知批次（400 UNKNOWN_BATCH）、重复批次（400
       DUPLICATE_BATCH_IN_VERIFICATION）、空清单（422）
  6. 旧清单批次被作废：单据含作废批次时重新核验被拒；借修订移除该作废批次，
       单据恢复可确认；首次确认的逐批快照始终保留
  7. 已确认单据不可直接修订（409 VERIFICATION_ALREADY_CONFIRMED）

用法：
    python examples/verification_revise_demo.py
    BASE_URL=http://192.168.1.10:8080 python examples/verification_revise_demo.py
"""
from __future__ import annotations

import json
import os
import sys
import time
import urllib.error
import urllib.request

BASE_URL = os.environ.get("BASE_URL", "http://127.0.0.1:8080")
LINE = "LREVISE"


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


def revise(verification_no: str, expected_revision: int, batch_nos: list[str]):
    return call(
        "POST",
        f"/api/verifications/{verification_no}/revise",
        {"expected_revision": expected_revision, "batch_nos": batch_nos},
    )


def members(doc: dict) -> list[str]:
    return [b["batch_no"] for b in doc["batches"]]


def main() -> int:
    print(f"目标服务：{BASE_URL}")
    status, _ = call("GET", "/health")
    if status != 200:
        print(f"服务不可用（/health 返回 {status}），请先启动服务。", file=sys.stderr)
        return 1
    print("健康检查通过")

    suffix = str(int(time.time()))
    vno = f"RV-{suffix}"

    section("1. 准备数据：3 个 RELEASE 批次 RV001/RV002/RV003")
    ensure_lot("RV-RM-MILK", "修订示例奶粉", ["milk"])
    for bn in ("RV001", "RV002", "RV003"):
        ensure_batch(bn, {
            "batch_no": bn, "line_id": LINE,
            "material_lots": ["RV-RM-MILK"],
            "declared_allergens": ["milk"], "cleaned_allergens": [],
        })

    section(f"2. 创建核验单 {vno}：[RV001, RV002]，创建即修订号 v1")
    status, body = call("POST", "/api/verifications", {
        "verification_no": vno, "batch_nos": ["RV001", "RV002"],
    })
    show("创建核验单", status, body)
    assert status == 201 and body["revision"] == 1 and members(body) == ["RV001", "RV002"]

    section("3. 装运批次变化：凭 v1 提交完整新清单 [RV002, RV003, RV001]")
    status, body = revise(vno, 1, ["RV002", "RV003", "RV001"])
    show("修订批次清单（保序、回到草稿、推进修订号）", status, body)
    assert status == 200
    assert body["status"] == "DRAFT" and body["revision"] == 2
    assert members(body) == ["RV002", "RV003", "RV001"]
    assert all(not b["changed"] and not b["shipment_changed"] for b in body["batches"])

    section("4. 并发的单据快照写入先落地 -> 过期修订号被整体拒绝，重新查询后再修订成功")
    # 另一现场先做了“重新核验”（单据快照写入之一）：刷新逐批快照并把修订号 v2->v3
    status, body = call("POST", f"/api/verifications/{vno}/reverify")
    assert status == 200 and body["revision"] == 3
    print("  并发重新核验已完成：逐批快照刷新，修订号 v2 -> v3")

    # 与此同时，清单批次的更正/批号锁定只改逐批版本、不推进单据修订号
    status, corrected = call("PUT", "/api/batches/RV001", {"declared_allergens": ["milk"]})
    assert status == 200
    print("  RV001 追加了裁决版本（草稿不转态；单据修订号仍为 v3）")

    # 仍手持旧修订号 v2 提交修订 -> 409，当前成员与快照不变
    status, body = revise(vno, 2, ["RV001", "RV002"])
    show("用过期修订号 v2 修订（应为 409，当前成员与快照不变）", status, body)
    assert status == 409 and body["error"]["code"] == "VERIFICATION_REVISION_CONFLICT"
    assert body["error"]["details"][0]["submitted_revision"] == 2
    assert body["error"]["details"][0]["current_revision"] == 3

    # 重新查询单据拿到当前修订号（并发修改可能已推进它）
    status, doc = call("GET", f"/api/verifications/{vno}")
    current_revision = doc["revision"]
    print(f"  重新查询：当前修订号 v{current_revision}，当前成员 {members(doc)}")
    status, body = revise(vno, current_revision, ["RV001", "RV002"])
    show("用当前修订号修订成功（同一事务重取 RV001 最新裁决/出货资格快照）", status, body)
    assert status == 200 and members(body) == ["RV001", "RV002"]
    by_no = {b["batch_no"]: b for b in body["batches"]}
    assert by_no["RV001"]["snapshot"]["version"] == by_no["RV001"]["current"]["version"]
    assert all(not b["changed"] for b in body["batches"])
    revision_after_correct = body["revision"]

    section("5. 整体拒绝：未知批次 / 重复批次 / 空清单")
    status, body = revise(vno, revision_after_correct, ["RV001", "RV-GHOST"])
    show("未知批次 -> 400 UNKNOWN_BATCH", status, body)
    assert status == 400 and body["error"]["code"] == "UNKNOWN_BATCH"

    status, body = revise(vno, revision_after_correct, ["RV001", "RV001"])
    show("重复批次 -> 400 DUPLICATE_BATCH_IN_VERIFICATION", status, body)
    assert status == 400 and body["error"]["code"] == "DUPLICATE_BATCH_IN_VERIFICATION"

    status, body = revise(vno, revision_after_correct, [])
    show("空清单 -> 422 模型校验", status, body)
    assert status == 422

    # 被拒绝后成员、快照、修订号都不变
    status, doc = call("GET", f"/api/verifications/{vno}")
    assert doc["revision"] == revision_after_correct and members(doc) == ["RV001", "RV002"]
    print(f"  拒绝后单据保持：revision=v{doc['revision']}，成员 {members(doc)}")

    section("6. 旧清单批次作废：重新核验被拒；借修订移除作废批次；历史确认保留")
    status, body = call("POST", "/api/batches/RV002/void", {"reason": "误登记，该批实际未投产"})
    assert status == 200
    print("  RV002 已作废（保留可查，批次号不可复用）")

    status, body = call("POST", f"/api/verifications/{vno}/reverify")
    show("含作废批次重新核验 -> 409", status, body)
    assert status == 409 and body["error"]["code"] == "VERIFICATION_CONTAINS_VOIDED_BATCH"

    # 含作废批次也不能确认，必须先修订移除
    status, body = call("POST", f"/api/verifications/{vno}/confirm")
    assert status == 409 and body["error"]["code"] == "VERIFICATION_CONTAINS_VOIDED_BATCH"

    # 把作废批次再放进新清单同样被拒
    status, body = revise(vno, revision_after_correct, ["RV001", "RV002"])
    show("新清单含已作废批次 -> 400 BATCH_VOIDED", status, body)
    assert status == 400 and body["error"]["code"] == "BATCH_VOIDED"

    status, body = revise(vno, revision_after_correct, ["RV003", "RV001"])
    show("借修订移除作废批次 RV002，单据恢复干净", status, body)
    assert status == 200
    assert members(body) == ["RV003", "RV001"]
    assert body["contains_voided_batches"] is False
    revision_clean = body["revision"]

    status, body = call("POST", f"/api/verifications/{vno}/confirm")
    show("确认成功（写入第一份确认快照，仍含当时的逐批成员）", status, body)
    assert status == 200 and body["status"] == "CONFIRMED"
    assert len(body["confirmations"]) == 1

    section("7. 已确认单据不可直接修订")
    status, body = revise(vno, body["revision"], ["RV001"])
    show("修订已确认单据 -> 409 VERIFICATION_ALREADY_CONFIRMED", status, body)
    assert status == 409 and body["error"]["code"] == "VERIFICATION_ALREADY_CONFIRMED"

    section("8. 历次确认始终保留（即使旧批次已不在现行清单）")
    # 让单据转待复核后再修订/确认，演示确认历史只追加
    status, body = call("PUT", "/api/material-lots/RV-RM-MILK", {"allergens": ["milk", "egg"]})
    assert status == 200
    status, body = call("PUT", "/api/material-lots/RV-RM-MILK", {"allergens": ["milk"]})
    assert status == 200
    status, doc = call("GET", f"/api/verifications/{vno}")
    print(f"  原料批号更正后单据状态：{doc['status']}")
    assert doc["status"] == "PENDING_REVIEW"
    status, body = revise(vno, doc["revision"], ["RV003"])
    assert status == 200 and members(body) == ["RV003"]
    status, body = call("POST", f"/api/verifications/{vno}/confirm")
    assert status == 200
    show("二次确认完成；第一份确认快照仍保留", status, {
        "revision": body["revision"],
        "current_batches": members(body),
        "confirmation_members": [[b["batch_no"] for b in c["batches"]] for c in body["confirmations"]],
    })
    assert len(body["confirmations"]) == 2
    assert [b["batch_no"] for b in body["confirmations"][0]["batches"]] == ["RV003", "RV001"]
    assert [b["batch_no"] for b in body["confirmations"][1]["batches"]] == ["RV003"]

    print("\n全部修订流程演示完成。")
    return 0


if __name__ == "__main__":
    sys.exit(main())

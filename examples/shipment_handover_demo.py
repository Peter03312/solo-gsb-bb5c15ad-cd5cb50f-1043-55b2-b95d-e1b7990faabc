#!/usr/bin/env python3
"""食品换线放行服务 —— 装车交接（整单发运）端到端调用示例（仅标准库）。

演示流程（独立产线 LHO，交接号与核验单号带时间戳，可重复运行）：
  1. 登记原料批号与 3 个全部 RELEASE/SHIPPABLE 的批次 HO001/HO002/HO003
  2. 创建核验单 [HO002, HO001] 并确认（确认后修订号 v2）
  3. 凭唯一交接号 + 已确认核验单号 + 读取时修订号登记整单发运
       -> 冻结单号/修订号/最近一次确认引用/交接时间与逐批确认快照
       -> 三个批次被全局占用，可逐批追溯交接归属
  4. 拒绝场景（整体拒绝、不留部分占用）：
       重复交接号(409)、未知单据(404)、非已确认单据(409)、修订号不匹配(409)
  5. 任一批次已在其他交接记录中发运 -> 整单 409（另一单含全新批次 HO003 也不部分占用）
  6. 交接后更正：单据转待复核，但交接记录冻结不改；逐批归属仍可追溯
  7. 查询：交接列表（按核验单过滤）、交接详情（逐批冻结快照）、单批交接归属

用法：
    python examples/shipment_handover_demo.py
    BASE_URL=http://192.168.1.10:8080 python examples/shipment_handover_demo.py
"""
from __future__ import annotations

import json
import os
import sys
import time
import urllib.error
import urllib.request

BASE_URL = os.environ.get("BASE_URL", "http://127.0.0.1:8080")
LINE = "LHO"


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
        ship = body["shipment_eligibility"]
        print(f"  已登记批次 {batch_no} => 裁决 {adj['disposition']}（v{adj['version']}），"
              f"出货资格 {ship['status']}（v{ship['version']}）")
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
    v1 = f"HOV-{suffix}"
    v2 = f"HOV2-{suffix}"
    h1 = f"HO-{suffix}"

    section("1. 准备数据：3 个全部 RELEASE/SHIPPABLE 的批次 HO001/HO002/HO003")
    ensure_lot("HO-RM-MILK", "交接示例奶粉", ["milk"])
    for bn in ("HO001", "HO002", "HO003"):
        ensure_batch(bn, {
            "batch_no": bn, "line_id": LINE,
            "material_lots": ["HO-RM-MILK"],
            "declared_allergens": ["milk"], "cleaned_allergens": [],
        })

    section(f"2. 创建并确认核验单 {v1}：[HO002, HO001]（顺序保留）")
    status, body = call("POST", "/api/verifications", {
        "verification_no": v1, "batch_nos": ["HO002", "HO001"],
    })
    assert status == 201, body
    status, doc = call("POST", f"/api/verifications/{v1}/confirm")
    assert status == 200 and doc["status"] == "CONFIRMED"
    revision = doc["revision"]  # 创建=1，确认 +1 -> 2
    print(f"  已确认，读取时修订号 revision=v{revision}；成员 [HO002, HO001]")

    section(f"3. 装车交接：交接号 {h1}，整单发运并冻结单号/修订号/逐批确认快照")
    status, body = call("POST", "/api/handovers", {
        "handover_no": h1, "verification_no": v1, "expected_revision": revision,
    })
    show("交接成功（201，逐批为最近一次确认快照的不可变副本）", status, body)
    assert status == 201
    assert [b["batch_no"] for b in body["batches"]] == ["HO002", "HO001"]
    assert body["verification_revision"] == revision
    assert all(b["adjudication"]["disposition"] == "RELEASE" for b in body["batches"])
    assert all(b["shipment_eligibility"]["status"] == "SHIPPABLE" for b in body["batches"])

    status, trace = call("GET", "/api/batches/HO001/handover")
    print(f"  逐批追溯 HO001 -> 已发运于交接 {trace['handover']['handover_no']}，"
          f"单号 {trace['handover']['verification_no']}，"
          f"冻结修订号 v{trace['handover']['verification_revision']}")

    section("4. 整体拒绝：重复交接号 / 未知单据 / 非已确认单据 / 修订号不匹配")
    status, body = call("POST", "/api/handovers", {
        "handover_no": h1, "verification_no": v1, "expected_revision": revision,
    })
    print(f"[{status}] 重复交接号 -> {body['error']['code']}")
    assert status == 409 and body["error"]["code"] == "DUPLICATE_HANDOVER"

    status, body = call("POST", "/api/handovers", {
        "handover_no": f"HO-X-{suffix}", "verification_no": "NO-SUCH-VERIFICATION",
        "expected_revision": 1,
    })
    print(f"[{status}] 未知单据 -> {body['error']['code']}")
    assert status == 404 and body["error"]["code"] == "VERIFICATION_NOT_FOUND"

    # 一张草稿单：非已确认不能交接
    call("POST", "/api/verifications", {"verification_no": v2, "batch_nos": ["HO003"]})
    status, body = call("POST", "/api/handovers", {
        "handover_no": f"HO-D-{suffix}", "verification_no": v2, "expected_revision": 1,
    })
    print(f"[{status}] 草稿单据 -> {body['error']['code']}")
    assert status == 409 and body["error"]["code"] == "VERIFICATION_NOT_CONFIRMED"

    status, body = call("POST", "/api/handovers", {
        "handover_no": f"HO-R-{suffix}", "verification_no": v1,
        "expected_revision": revision + 99,
    })
    print(f"[{status}] 修订号不匹配 -> {body['error']['code']} "
          f"({body['error']['details'][0]['submitted_revision']} vs "
          f"{body['error']['details'][0]['current_revision']})")
    assert status == 409 and body["error"]["code"] == "VERIFICATION_REVISION_CONFLICT"

    section("5. 任一批次已在其他交接发运 -> 整单 409，不留下部分占用")
    # v2 在第 4 步以草稿 [HO003] 创建；这里把已发运的 HO001 也加入清单（草稿可修订），
    # 再确认：确认后含 [HO003(全新), HO001(已随 h1 发运)]，交接整单拒绝，HO003 不被占用
    status, body = call("POST", f"/api/verifications/{v2}/revise", {
        "expected_revision": 1, "batch_nos": ["HO003", "HO001"],
    })
    assert status == 200, body
    status, doc2 = call("POST", f"/api/verifications/{v2}/confirm")
    assert status == 200
    status, body = call("POST", "/api/handovers", {
        "handover_no": f"HO2-{suffix}", "verification_no": v2,
        "expected_revision": doc2["revision"],
    })
    show("含已发运批次 HO001（409 BATCH_ALREADY_SHIPPED，全新批次 HO003 不部分占用）",
         status, body)
    assert status == 409 and body["error"]["code"] == "BATCH_ALREADY_SHIPPED"
    assert body["error"]["details"][0]["batch_no"] == "HO001"
    assert body["error"]["details"][0]["handover_no"] == h1
    status, trace = call("GET", "/api/batches/HO003/handover")
    assert trace["shipped"] is False
    print("  HO003 仍未发运（shipped=false）：失败交接未占用任何批次")

    section("6. 交接后更正：单据转待复核，但交接记录冻结不改")
    status, body = call("PUT", "/api/batches/HO001",
                        {"declared_allergens": ["milk", "egg"]})
    assert status == 200
    status, doc = call("GET", f"/api/verifications/{v1}")
    print(f"  交接后更正 HO001：核验单 {v1} 状态 -> {doc['status']}")
    assert doc["status"] == "PENDING_REVIEW"
    status, ho = call("GET", f"/api/handovers/{h1}")
    print(f"  交接记录 {h1} 冻结：revision=v{ho['verification_revision']}，"
          f"HO001 快照裁决仍为 v{ho['batches'][1]['adjudication']['version']} "
          f"{ho['batches'][1]['adjudication']['disposition']}")
    assert ho["verification_revision"] == revision
    assert ho["batches"][1]["adjudication"]["version"] == 1
    # 待复核单据不能再交接
    status, body = call("POST", "/api/handovers", {
        "handover_no": f"HO3-{suffix}", "verification_no": v1,
        "expected_revision": revision,
    })
    assert status == 409 and body["error"]["code"] == "VERIFICATION_NOT_CONFIRMED"
    print(f"[{status}] 待复核单据再次交接 -> {body['error']['code']}")

    section("7. 查询：交接列表 / 按核验单过滤 / 详情 / 单批归属")
    status, body = call("GET", "/api/handovers")
    print(f"[{status}] 全部交接：{[x['handover_no'] for x in body['handovers']]}")
    status, body = call("GET", f"/api/handovers?verification_no={v1}")
    print(f"[{status}] 按核验单 {v1} 过滤：{[x['handover_no'] for x in body['handovers']]}")
    status, body = call("GET", f"/api/handovers/{h1}")
    print(f"[{status}] 交接详情 {h1}：{body['batch_count']} 批，"
          f"交接时间 {body['handed_over_at']}")
    for bn in ("HO001", "HO002", "HO003"):
        status, trace = call("GET", f"/api/batches/{bn}/handover")
        if trace["shipped"]:
            print(f"  {bn}: 已发运 -> {trace['handover']['handover_no']} "
                  f"(position {trace['handover']['position']})")
        else:
            print(f"  {bn}: 未发运（shipped=false）")

    print("\n全部装车交接流程演示完成。")
    return 0


if __name__ == "__main__":
    sys.exit(main())

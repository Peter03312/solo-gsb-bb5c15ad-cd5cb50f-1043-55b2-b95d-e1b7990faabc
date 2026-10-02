#!/usr/bin/env python3
"""食品换线放行服务 —— 仓库装车交接（整单发运）端到端调用示例（仅标准库）。

演示流程（独立产线 LHAND，批号带时间戳后缀，可重复运行）：
  1. 登记原料批号与两个 RELEASE/SHIPPABLE 批次 HB001/HB002
  2. 创建核验单并确认（CONFIRMED，确认推进修订号到 2）
  3. 先 GET 单据拿“读取时修订号”，再凭唯一交接号登记整单发运
     —— 冻结核验单号、修订号、逐批确认快照（RELEASE / SHIPPABLE）与交接时间
  4. 查询交接详情、交接列表、每批交接归属
  5. 交接后更正批次：单据自动转待复核，但交接记录冻结不改、归属仍可追溯
  6. 拒绝场景：重复交接号、未知单据、草稿单据、过期修订号、
     批次已在其他交接记录中发运（含批次交叉的两张单据，整单拒绝不留部分占用）

用法：
    python examples/handover_demo.py
    BASE_URL=http://192.168.1.10:8080 python examples/handover_demo.py
"""
from __future__ import annotations

import json
import os
import sys
import time
import urllib.error
import urllib.request

BASE_URL = os.environ.get("BASE_URL", "http://127.0.0.1:8080")
LINE = "LHAND"


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
        print(
            f"  已登记批次 {batch_no} => 裁决 {adj['disposition']}（v{adj['version']}），"
            f"出货资格 {ship['status']}（v{ship['version']}）"
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
    lot_milk = f"H-RM-MILK-{suffix}"
    b1, b2 = f"HB-{suffix}-1", f"HB-{suffix}-2"
    doc1, doc2 = f"HV-{suffix}-1", f"HV-{suffix}-2"
    hand1, hand2, hand3 = f"HH-{suffix}-1", f"HH-{suffix}-2", f"HH-{suffix}-3"

    section("1. 准备数据：两个 RELEASE / SHIPPABLE 批次")
    ensure_lot(lot_milk, "交接示例奶粉", ["milk"])
    ensure_batch(b1, {
        "batch_no": b1, "line_id": LINE,
        "material_lots": [lot_milk],
        "declared_allergens": ["milk"], "cleaned_allergens": [],
    })
    # 第二批清洁掉上一批 milk 残留，本身无过敏原原料 => RELEASE
    ensure_batch(b2, {
        "batch_no": b2, "line_id": LINE,
        "material_lots": [],
        "declared_allergens": [], "cleaned_allergens": ["milk"],
    })

    section(f"2. 创建核验单 {doc1}（{b1},{b2}）并确认 -> CONFIRMED")
    status, _ = call("POST", "/api/verifications", {
        "verification_no": doc1, "batch_nos": [b1, b2],
    })
    assert status == 201
    status, confirmed = call("POST", f"/api/verifications/{doc1}/confirm")
    assert status == 200, confirmed
    print(f"  单据状态 {confirmed['status']}，修订号 revision={confirmed['revision']}")
    print("  逐批确认快照：")
    for b in confirmed["batches"]:
        print(
            f"    {b['batch_no']}: 裁决 {b['snapshot']['disposition']} "
            f"v{b['snapshot']['version']} / 资格 {b['shipment_snapshot']['status']} "
            f"v{b['shipment_snapshot']['version']}"
        )

    section("3. 装车交接：凭唯一交接号 + 已确认单号 + 读取时修订号登记整单发运")
    # 现场先查单据（上一步返回也可），以“所见修订号”发起交接
    status, seen = call("GET", f"/api/verifications/{doc1}")
    assert status == 200
    expected_revision = seen["revision"]
    status, ho = call("POST", "/api/handovers", {
        "handover_no": hand1,
        "verification_no": doc1,
        "expected_revision": expected_revision,
    })
    show(f"交接 {hand1}（预期 201）", status, ho)
    assert status == 201, ho
    assert ho["verification_revision"] == expected_revision
    assert [x["batch_no"] for x in ho["batches"]] == [b1, b2]
    assert all(x["snapshot"]["disposition"] == "RELEASE" for x in ho["batches"])
    assert all(x["shipment_snapshot"]["status"] == "SHIPPABLE" for x in ho["batches"])

    section("4. 查询：交接详情 / 列表 / 每批交接归属")
    status, detail = call("GET", f"/api/handovers/{hand1}")
    print(f"[{status}] GET /api/handovers/{hand1}: {len(detail['batches'])} 个批次，"
          f"交接时间 {detail['handed_over_at']}")
    status, listed = call("GET", "/api/handovers", )
    print(f"[{status}] 全部交接记录：{[h['handover_no'] for h in listed['handovers']]}")
    status, attr = call("GET", f"/api/batches/{b1}/handover")
    print(f"[{status}] 批次 {b1} 的交接归属：{attr['handover']['handover_no']} "
          f"（核验单 {attr['handover']['verification_no']}）")

    section("5. 交接后更正批次：单据转待复核，但交接记录冻结、归属仍可追溯")
    status, corrected = call("PUT", f"/api/batches/{b1}", {"declared_allergens": ["milk"]})
    assert status == 200, corrected
    status, doc_now = call("GET", f"/api/verifications/{doc1}")
    print(f"  更正后单据状态：{doc_now['status']}（修订号仍为 {doc_now['revision']}）")
    assert doc_now["status"] == "PENDING_REVIEW"
    status, frozen = call("GET", f"/api/handovers/{hand1}")
    print(f"[{status}] 交接记录未改写：冻结修订号 {frozen['verification_revision']}，"
          f"逐批快照版本 {[b['snapshot']['version'] for b in frozen['batches']]}")
    assert frozen["verification_revision"] == expected_revision

    section("6. 拒绝场景（全部整体拒绝，不留下部分占用）")
    # 6.1 重复交接号
    status, body = call("POST", "/api/handovers", {
        "handover_no": hand1, "verification_no": doc1, "expected_revision": expected_revision,
    })
    print(f"[{status}] 重复交接号: {body['error']['code']}")
    assert status == 409 and body["error"]["code"] == "DUPLICATE_HANDOVER"

    # 6.2 未知单据
    status, body = call("POST", "/api/handovers", {
        "handover_no": hand2, "verification_no": "NO-SUCH", "expected_revision": 1,
    })
    print(f"[{status}] 未知单据: {body['error']['code']}")
    assert status == 404 and body["error"]["code"] == "VERIFICATION_NOT_FOUND"

    # 6.3 草稿/待复核单据不能交接（doc1 已因更正转待复核）
    status, body = call("POST", "/api/handovers", {
        "handover_no": hand2, "verification_no": doc1, "expected_revision": expected_revision,
    })
    print(f"[{status}] 单据非 CONFIRMED: {body['error']['code']} "
          f"(current_status={body['error']['details'][0]['current_status']})")
    assert status == 409 and body["error"]["code"] == "VERIFICATION_NOT_CONFIRMED"

    # 6.4 过期修订号（先准备另一张已确认单据 doc2）
    call("POST", "/api/verifications", {"verification_no": doc2, "batch_nos": [b2]})
    status, conf2 = call("POST", f"/api/verifications/{doc2}/confirm")
    assert status == 200, conf2
    status, body = call("POST", "/api/handovers", {
        "handover_no": hand2, "verification_no": doc2, "expected_revision": 1,  # 过期
    })
    print(f"[{status}] 过期修订号: {body['error']['code']} "
          f"(submitted={body['error']['details'][0]['submitted_revision']}, "
          f"current={body['error']['details'][0]['current_revision']})")
    assert status == 409 and body["error"]["code"] == "VERIFICATION_REVISION_CONFLICT"

    # 6.5 批次已在其他交接记录中发运：doc2 含 b2（已随 hand1 发运）-> 整单拒绝
    status, body = call("POST", "/api/handovers", {
        "handover_no": hand2,
        "verification_no": doc2,
        "expected_revision": conf2["revision"],
    })
    print(f"[{status}] 含已发运批次: {body['error']['code']}")
    for d in body["error"]["details"]:
        print(f"    - {d['batch_no']}: {d['reason_code']}（已在 {d['shipped_in_handover']} 发运）")
    assert status == 409 and body["error"]["code"] == "HANDOVER_BATCH_NOT_READY"
    assert [d["reason_code"] for d in body["error"]["details"]] == ["BATCH_ALREADY_SHIPPED"]

    # 整单拒绝：hand2 无记录，且不存在“部分占用”
    status, _ = call("GET", f"/api/handovers/{hand2}")
    print(f"[{status}] 失败交接号 {hand2} 无任何记录（GET 详情 404）")
    assert status == 404

    section("完成")
    print(f"交接 {hand1} 已登记整单发运：核验单 {doc1} 修订号 v{expected_revision}，"
          f"批次 {b1}/{b2} 全部 RELEASE / SHIPPABLE。")
    print("交接记录只追加、不可改：其后更正/待复核/清单修订均不改写，"
          "可通过 GET /api/batches/{batch_no}/handover 追溯每批交接归属。")
    return 0


if __name__ == "__main__":
    sys.exit(main())

#!/usr/bin/env python3
"""食品换线放行服务 —— 出货核验单端到端调用示例（仅使用 Python 标准库）。

演示流程（独立产线 LVERIFY，避免与 run_demo.py 的批次冲突）：
  1. 登记原料批号与两个批次：
       VB001 花生黑巧声明 peanut -> RELEASE
       VB002 牛奶黑巧未声明 milk  -> HOLD
  2. 创建核验单 V-<时间戳>-1：[VB001, VB002]，逐批保存当前裁决版本
       -> 草稿快照：VB001 RELEASE / VB002 HOLD
  3. 确认尝试：VB002 为 HOLD -> 422 返回不符批次，单据保持 DRAFT，无确认记录
  4. 创建只含放行批次的核验单并确认成功 -> CONFIRMED，保留确认快照
  5. 更正已确认单据所含批次（即使结论仍为 RELEASE）-> 单据立即转 PENDING_REVIEW
  6. 待复核单据直接确认被拒（快照版本陈旧）；重新核验刷新快照后再次确认成功
  7. 查询核验单：状态、逐批记录版本/当前版本/变化原因、历次确认快照
  8. 拒绝场景：重复单号、未知批次、批次重复

用法：
    python examples/verification_demo.py
    BASE_URL=http://192.168.1.10:8080 python examples/verification_demo.py
"""
from __future__ import annotations

import json
import os
import sys
import time
import urllib.error
import urllib.request

BASE_URL = os.environ.get("BASE_URL", "http://127.0.0.1:8080")
LINE = "LVERIFY"


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

    section("1. 准备数据：1 个 RELEASE 批次 VB001、1 个 HOLD 批次 VB002")
    ensure_lot("V-RM-PEANUT", "核验示例花生酱", ["peanut"])
    ensure_lot("V-RM-MILK", "核验示例奶粉", ["milk"])
    ensure_batch("VB001", {
        "batch_no": "VB001", "line_id": LINE,
        "material_lots": ["V-RM-PEANUT"],
        "declared_allergens": ["peanut"], "cleaned_allergens": [],
    })
    ensure_batch("VB002", {
        "batch_no": "VB002", "line_id": LINE,
        "material_lots": ["V-RM-MILK"],
        "declared_allergens": [], "cleaned_allergens": ["peanut"],
    })

    hold_doc = f"V-{suffix}-1"
    section(f"2. 创建核验单 {hold_doc}：[VB001, VB002]（草稿，逐批快照）")
    status, doc = call("POST", "/api/verifications", {
        "verification_no": hold_doc, "batch_nos": ["VB001", "VB002"],
    })
    show("创建核验单", status, doc)

    section("3. 尝试确认：VB002 为 HOLD -> 422 不符批次，单据保持 DRAFT")
    status, body = call("POST", f"/api/verifications/{hold_doc}/confirm")
    show("确认（预期 422）", status, body)
    assert status == 422 and body["error"]["details"], body

    ok_doc = f"V-{suffix}-2"
    section(f"4. 创建只含放行批次的核验单 {ok_doc} 并确认")
    status, doc = call("POST", "/api/verifications", {
        "verification_no": ok_doc, "batch_nos": ["VB001"],
    })
    show("创建核验单", status, doc)
    status, confirmed = call("POST", f"/api/verifications/{ok_doc}/confirm")
    show("确认（预期 200 CONFIRMED）", status, confirmed)
    assert status == 200 and confirmed["status"] == "CONFIRMED", confirmed

    section("5. 更正 VB001 声明后再更正回来：产生新版本但结论仍为 RELEASE")
    status, c1 = call("PUT", "/api/batches/VB001", {"declared_allergens": ["peanut"]})
    show("更正（声明全集不变，仍产生新版本）", status, c1)
    assert status == 200
    affected = {v["verification_no"]: v["status"] for v in c1["affected_verifications"]}
    print(f"  受影响核验单：{affected}")

    status, doc = call("GET", f"/api/verifications/{ok_doc}")
    show(f"{ok_doc} 查询：已确认单据立即转 PENDING_REVIEW", status, doc)
    assert doc["status"] == "PENDING_REVIEW", doc
    entry = doc["batches"][0]
    print(f"  快照版本 v{entry['snapshot']['version']} -> 当前版本 v{entry['current']['version']}")
    print(f"  变化原因：{[r['message'] for r in entry['change_reasons']]}")

    section("6. 待复核单据直接确认被拒；重新核验后再次确认（历次确认快照保留）")
    status, body = call("POST", f"/api/verifications/{ok_doc}/confirm")
    show("未重新核验直接确认（预期 422 STALE_SNAPSHOT）", status, body)
    assert status == 422
    assert body["error"]["details"][0]["reason_code"] == "STALE_SNAPSHOT", body

    status, body = call("POST", f"/api/verifications/{ok_doc}/reverify")
    show("重新核验（快照刷新，回到 DRAFT）", status, body)
    assert status == 200 and body["status"] == "DRAFT", body

    status, body = call("POST", f"/api/verifications/{ok_doc}/confirm")
    show("再次确认（预期 200，confirmations 保留两次）", status, body)
    assert status == 200 and len(body["confirmations"]) == 2, body

    section("7. 列表与过滤查询")
    status, body = call("GET", "/api/verifications?status=CONFIRMED")
    confirmed_nos = [v["verification_no"] for v in body["verifications"]]
    print(f"[{status}] CONFIRMED 单据（含本次 {ok_doc}）：{confirmed_nos}")
    assert ok_doc in confirmed_nos

    section("8. 拒绝场景：重复单号 / 未知批次 / 批次重复")
    for label, payload in [
        ("重复单号", {"verification_no": ok_doc, "batch_nos": ["VB001"]}),
        ("未知批次", {"verification_no": f"V-{suffix}-X", "batch_nos": ["NO-SUCH"]}),
        ("批次重复", {"verification_no": f"V-{suffix}-Y", "batch_nos": ["VB001", "VB001"]}),
        ("空批次列表", {"verification_no": f"V-{suffix}-Z", "batch_nos": []}),
    ]:
        status, body = call("POST", "/api/verifications", payload)
        code = body.get("error", {}).get("code", "(FastAPI 校验)")
        print(f"[{status}] {label}: {code}")

    section("完成")
    print(f"核验单 {hold_doc} 仍为 DRAFT（含 HOLD 批次，处理后重新核验再确认）。")
    print(f"核验单 {ok_doc} 已二次确认，全部历史确认快照均可在 GET 详情中查看。")
    print("可继续在交互式文档中探索： %s/docs" % BASE_URL)
    return 0


if __name__ == "__main__":
    sys.exit(main())

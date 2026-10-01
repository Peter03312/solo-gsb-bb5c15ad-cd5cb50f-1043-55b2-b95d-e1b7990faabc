#!/usr/bin/env python3
"""食品换线放行服务 —— 批次误归产线更正（换线）端到端调用示例（仅用标准库）。

演示流程（独立产线 LMOVE-SRC / LMOVE-DST，批号 MV-RM-*，避免与其他示例冲突）：
  1. 准备：
       原料批号 MV-RM-P（peanut）、MV-RM-M（milk）
       原线 LMOVE-SRC：MS1 用 peanut 声明 peanut（RELEASE，peanut 残留）
                       MS2 开工前清洁 peanut、无原料（误归产线，待移出，RELEASE）
                       MS3 无原料无清洁（继承被 MS2 清洁干净的产线 -> RELEASE）
       目标线 LMOVE-DST：MD1 用 milk 声明 milk（RELEASE，milk 残留）
  2. 创建并确认核验单 MV-<时间戳>-1：[MS2]（确认时 RELEASE/SHIPPABLE）。
  3. 拒绝场景：未知批次 404、未知目标线 404、目标线与原线相同 409（失败不留部分改动）。
  4. 换线 MS2 -> LMOVE-DST：
       批次号不变，目标线末尾获得新顺序号 2；原线顺序号 2 留空不复用；
       原线从移出位置起重算：MS3 直接继承 MS1 的 peanut（MS2 的清洁随批移出）-> HOLD v2；
       目标线从移入批次起重算：MS2 继承 MD1 的 milk 且未声明 -> HOLD v2；
       历史版本 v1 保留原产线/顺序号不改，v2 记录目标线/新序号（trigger=MOVE_BATCH）；
       直接用料未变，出货资格版本不递增；已确认核验单转 PENDING_REVIEW，确认快照原样。
  5. 已确认单据详情：变化原因含 MOVE_BATCH；出货资格快照未漂移；历次确认快照仍为 v1。
  6. 原线顺序号留空：再登记 MS4 顺延 seq 4（不复用 2）。
  7. 重新核验后二次确认：MS2 当前 HOLD，确认被 422 拦截（BATCH_ON_HOLD），
       更正其声明加上 milk 后重新核验、再次确认成功；历史确认保留两次。

用法：
    python examples/batch_reassign_demo.py
    BASE_URL=http://192.168.1.10:8080 python examples/batch_reassign_demo.py
"""
from __future__ import annotations

import json
import os
import sys
import time
import urllib.error
import urllib.request

BASE_URL = os.environ.get("BASE_URL", "http://127.0.0.1:8080")
SOURCE_LINE = "LMOVE-SRC"
TARGET_LINE = "LMOVE-DST"


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


def ensure_batch(batch_no: str, payload: dict) -> dict:
    status, body = call("POST", "/api/batches", payload)
    if status == 201:
        adj = body["adjudication"]
        print(f"  已登记批次 {batch_no}（{body['line_id']} seq={body['seq']}）"
              f" => {adj['disposition']}（v{adj['version']}，实际={adj['actual_allergens']}）")
        return body
    if status == 409:
        print(f"  批次 {batch_no} 已存在，继续")
        return call("GET", f"/api/batches/{batch_no}")[1]
    raise RuntimeError(f"登记批次失败: {body}")


def main() -> int:
    print(f"目标服务：{BASE_URL}")
    status, _ = call("GET", "/health")
    if status != 200:
        print(f"服务不可用（/health 返回 {status}），请先启动服务。", file=sys.stderr)
        return 1
    print("健康检查通过")

    suffix = str(int(time.time()))

    section("1. 准备：原料批号；原线 MS1(peanut 残留)/MS2(清洁 peanut，误归)/MS3；目标线 MD1(milk)")
    ensure_lot("MV-RM-P", "换线示例花生酱", ["peanut"])
    ensure_lot("MV-RM-M", "换线示例奶粉", ["milk"])
    ms1 = ensure_batch("MS1", {
        "batch_no": "MS1", "line_id": SOURCE_LINE,
        "material_lots": ["MV-RM-P"],
        "declared_allergens": ["peanut"], "cleaned_allergens": [],
    })
    ms2 = ensure_batch("MS2", {
        "batch_no": "MS2", "line_id": SOURCE_LINE,
        "material_lots": [],
        "declared_allergens": [], "cleaned_allergens": ["peanut"],
    })
    ms3 = ensure_batch("MS3", {
        "batch_no": "MS3", "line_id": SOURCE_LINE,
        "material_lots": [],
        "declared_allergens": [], "cleaned_allergens": [],
    })
    assert ms3["adjudication"]["disposition"] == "RELEASE", ms3  # MS2 已清洁 peanut
    ensure_batch("MD1", {
        "batch_no": "MD1", "line_id": TARGET_LINE,
        "material_lots": ["MV-RM-M"],
        "declared_allergens": ["milk"], "cleaned_allergens": [],
    })

    doc_no = f"MV-{suffix}-1"
    section(f"2. 创建并确认核验单 {doc_no}：[MS2]")
    status, body = call("POST", "/api/verifications", {
        "verification_no": doc_no, "batch_nos": ["MS2"],
    })
    assert status == 201, body
    status, doc = call("POST", f"/api/verifications/{doc_no}/confirm")
    show("确认（预期 200 CONFIRMED）", status, doc)
    assert status == 200 and doc["status"] == "CONFIRMED", doc

    section("3. 拒绝场景：未知批次 / 未知目标线 / 目标线与原线相同")
    status, body = call("POST", "/api/batches/NO-SUCH-BATCH/reassign",
                        {"target_line_id": TARGET_LINE})
    print(f"[{status}] 更正未知批次: {body['error']['code']}")
    assert status == 404 and body["error"]["code"] == "BATCH_NOT_FOUND", body

    status, body = call("POST", "/api/batches/MS2/reassign",
                        {"target_line_id": "NO-SUCH-LINE"})
    print(f"[{status}] 目标线未登记: {body['error']['code']}")
    assert status == 404 and body["error"]["code"] == "LINE_NOT_FOUND", body
    # 失败不留部分归属：MS2 仍在原线 seq2
    still = call("GET", "/api/batches/MS2")[1]
    assert still["line_id"] == SOURCE_LINE and still["seq"] == 2, still

    status, body = call("POST", "/api/batches/MS2/reassign",
                        {"target_line_id": SOURCE_LINE})
    print(f"[{status}] 目标线与原线相同: {body['error']['code']}")
    assert status == 409 and body["error"]["code"] == "SAME_SOURCE_TARGET_LINE", body

    section("4. 换线更正：MS2 移至目标线末尾")
    status, moved = call("POST", "/api/batches/MS2/reassign",
                         {"target_line_id": TARGET_LINE})
    show("换线结果（新旧归属、两线重算、受影响核验单）", status, moved)
    assert status == 200, moved

    # 新旧归属
    assert moved["assignment"] == {
        "batch_no": "MS2",
        "from_line_id": SOURCE_LINE,
        "from_seq": 2,
        "to_line_id": TARGET_LINE,
        "to_seq": 2,
    }, moved["assignment"]
    assert moved["moved"]["batch_no"] == "MS2"
    assert moved["moved"]["line_id"] == TARGET_LINE

    # 原线重算：MS2 移出后其清洁不再在原线发生，MS3 直接继承 MS1 的 peanut -> HOLD v2
    src = {s["batch_no"]: s for s in moved["source_line"]["recomputed"]}
    assert list(src) == ["MS3"], src
    assert src["MS3"]["version"] == 2
    assert src["MS3"]["disposition"] == "HOLD"
    assert src["MS3"]["actual"] == ["peanut"]
    # 目标线重算：MS2 继承 MD1 的 milk 未声明 -> HOLD v2
    dst = {s["batch_no"]: s for s in moved["target_line"]["recomputed"]}
    assert list(dst) == ["MS2"], dst
    assert dst["MS2"]["version"] == 2 and dst["MS2"]["seq"] == 2
    assert dst["MS2"]["actual"] == ["milk"] and dst["MS2"]["undeclared"] == ["milk"]
    # 移出点之前的批次不重算
    assert call("GET", "/api/batches/MS1")[1]["adjudication"]["version"] == 1
    assert call("GET", "/api/batches/MD1")[1]["adjudication"]["version"] == 1

    # 历史版本：旧版本的原产线/顺序号不改，新版本记录新归属
    hist = call("GET", "/api/batches/MS2/history")[1]
    print("MS2 历史裁决版本（v1 留原线 seq，v2 为换线后）：")
    for v in hist["history"]:
        print(f"  v{v['version']} line={v['line_id']} seq={v['seq']} "
              f"trigger={v['trigger']} => {v['disposition']}")
    assert (hist["history"][0]["line_id"], hist["history"][0]["seq"]) == (SOURCE_LINE, 2)
    assert (hist["history"][1]["line_id"], hist["history"][1]["seq"]) == (TARGET_LINE, 2)
    assert hist["history"][1]["trigger"] == "MOVE_BATCH"

    # 直接用料未变：出货资格版本不因换线递增
    elig = call("GET", "/api/batches/MS2/eligibility")[1]
    print(f"MS2 出货资格：v{elig['eligibility']['version']} {elig['eligibility']['status']}，"
          f"历史触发={[v['trigger'] for v in elig['history']]}")
    assert elig["eligibility"]["version"] == 1
    assert [v["trigger"] for v in elig["history"]] == ["REGISTER"]

    section("5. 已确认核验单转待复核；历次确认快照保持原样")
    affected = {v["verification_no"]: v["status"] for v in moved["affected_verifications"]}
    print(f"受影响核验单：{affected}")
    assert affected == {doc_no: "PENDING_REVIEW"}, affected

    doc = call("GET", f"/api/verifications/{doc_no}")[1]
    rec = doc["batches"][0]
    print(f"单据状态={doc['status']}；逐批 line_id={rec['line_id']} changed={rec['changed']} "
          f"shipment_changed={rec['shipment_changed']}")
    assert doc["status"] == "PENDING_REVIEW"
    assert rec["line_id"] == TARGET_LINE and rec["changed"] is True
    assert rec["change_reasons"][-1]["trigger"] == "MOVE_BATCH"
    assert rec["shipment_changed"] is False  # 出货资格版本未变
    # 历次确认快照只追加不改：仍冻结换线前 v1
    assert len(doc["confirmations"]) == 1
    assert doc["confirmations"][0]["batches"][0]["version"] == 1

    section("6. 原线顺序号留空不复用：MS4 顺延 seq 4")
    ms4 = ensure_batch("MS4", {
        "batch_no": "MS4", "line_id": SOURCE_LINE,
        "material_lots": [],
        "declared_allergens": [], "cleaned_allergens": [],
    })
    assert ms4["seq"] == 4, ms4  # MS1=1, 留空 2(MS2 移出), MS3=3 -> MS4=4
    print(f"MS4 顺序号={ms4['seq']}（原 seq 2 留空）；继承 MS1/MS3 的 peanut -> "
          f"{ms4['adjudication']['disposition']}")

    section("7. 重新核验后：MS2 当前 HOLD，确认拦截；补声明 milk 后二次确认成功")
    status, body = call("POST", f"/api/verifications/{doc_no}/reverify")
    assert status == 200, body
    status, body = call("POST", f"/api/verifications/{doc_no}/confirm")
    show("HOLD 批次确认（预期 422 VERIFICATION_NOT_CONFIRMABLE）", status, body)
    assert status == 422 and body["error"]["code"] == "VERIFICATION_NOT_CONFIRMABLE", body
    codes = {d["reason_code"] for d in body["error"]["details"]}
    assert "BATCH_ON_HOLD" in codes, codes

    # 更正 MS2 声明（换线后更正接口照常可用）：补声明 milk -> RELEASE
    status, corr = call("PUT", "/api/batches/MS2",
                        {"declared_allergens": ["milk"]})
    assert status == 200, corr
    assert corr["corrected"]["line_id"] == TARGET_LINE
    assert corr["corrected"]["adjudication"]["disposition"] == "RELEASE"

    status, body = call("POST", f"/api/verifications/{doc_no}/reverify")
    assert status == 200, body
    status, doc = call("POST", f"/api/verifications/{doc_no}/confirm")
    show("重新核验后二次确认（预期 200 CONFIRMED）", status, doc)
    assert status == 200 and doc["status"] == "CONFIRMED", doc
    # 历次确认快照保留两次（v1 原线确认、v3 换线更正后确认）
    versions = [c["batches"][0]["version"] for c in doc["confirmations"]]
    print(f"历次确认快照版本：{versions}（原样保留，不覆盖）")
    assert versions == [1, doc["batches"][0]["current"]["version"]]

    section("完成")
    print(f"MS2 已由 {SOURCE_LINE}#2 更正归属至 {TARGET_LINE}#2，批次号不变；原线 #2 留空。")
    print("两线重算、历史版本（旧版原产线/序号不改）、核验单转待复核与快照保留均符合预期。")
    print("可继续在交互式文档中探索： %s/docs" % BASE_URL)
    return 0


if __name__ == "__main__":
    sys.exit(main())

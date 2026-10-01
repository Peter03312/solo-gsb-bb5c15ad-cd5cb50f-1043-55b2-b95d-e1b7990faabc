#!/usr/bin/env python3
"""食品换线放行服务 —— 开工前清洁更正“预演”端到端调用示例（仅使用 Python 标准库）。

预演是只读操作：生产主管在正式更正某批开工前清洁记录前，先用同一次当前数据快照
推演对同线后续放行及已确认出货核验单的影响；预演不更改清洁记录、裁决/资格版本、
核验单状态或历史。

演示流程（独立产线 LCPV，批次号带时间戳，可重复运行）：
  1. 登记批号 CP-RM-P（peanut），逐批登记：
       CP1 使用花生原料、声明 peanut -> RELEASE，peanut 残留在产线
       CP2 无原料、开工前清洁 peanut -> 清除残留 -> RELEASE
       CP3 无原料、无清洁 -> 上一有效批已无残留 -> RELEASE
       CP4 无原料、无清洁 -> RELEASE
  2. 创建并确认出货核验单 CPV-...：[CP3, CP4]（全部 RELEASE，可确认）。
  3. 预演 A：把 CP2 的清洁集合改为 []（不再清洁 peanut）
       -> CP2/CP3/CP4 逐批 HOLD（peanut 重新沿链传递），给出每批当前/预演结论
          与变化原因；已确认核验单将因裁决版本追加而转待复核（预演中状态不变）。
  4. 预演 B：拟更正集合与当前相同（[peanut]）
       -> 裁决内容不变，但 CP2/CP3/CP4 仍全部属于版本影响范围（版本 +1）。
  5. 只读性核对：预演后核验单仍为 CONFIRMED、清洁记录与裁决版本均未变化。
  6. 拒绝场景：未知批次 404、非法过敏原代码 400。
  7. 原有正式更正入口保持可用（PUT .../cleaning，此次才真正落库并使核验单转待复核）。

用法：
    python examples/cleaning_preview_demo.py
    BASE_URL=http://192.168.1.10:8080 python examples/cleaning_preview_demo.py
"""
from __future__ import annotations

import json
import os
import sys
import time
import urllib.error
import urllib.request

BASE_URL = os.environ.get("BASE_URL", "http://127.0.0.1:8080")
LINE = "LCPV"


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


def main() -> int:
    print(f"目标服务：{BASE_URL}")
    status, _ = call("GET", "/health")
    if status != 200:
        print(f"服务不可用（/health 返回 {status}），请先启动服务。", file=sys.stderr)
        return 1
    print("健康检查通过")

    suffix = str(int(time.time()))
    lot = "CP-RM-P"
    b1, b2, b3, b4 = [f"CP{x}-{suffix}" for x in (1, 2, 3, 4)]

    section("1. 准备数据：CP1(RELEASE，残留 peanut) → CP2(清洁 peanut，RELEASE) → CP3/CP4(RELEASE)")
    ensure_lot(lot, "预演示例花生酱", ["peanut"])
    for bn, lots, declared, cleaned in (
        (b1, [lot], ["peanut"], []),
        (b2, [], [], ["peanut"]),
        (b3, [], [], []),
        (b4, [], [], []),
    ):
        st, body = call("POST", "/api/batches", {
            "batch_no": bn, "line_id": LINE,
            "material_lots": lots,
            "declared_allergens": declared,
            "cleaned_allergens": cleaned,
        })
        assert st == 201, body
        print(f"  已登记 {bn} => {body['adjudication']['disposition']}"
              f"（v{body['adjudication']['version']}，实际 {body['adjudication']['actual_allergens']}）")

    doc_no = f"CPV-{suffix}-1"
    st, body = call("POST", "/api/verifications", {
        "verification_no": doc_no, "batch_nos": [b3, b4],
    })
    assert st == 201, body
    st, body = call("POST", f"/api/verifications/{doc_no}/confirm")
    assert st == 200, body
    print(f"\n  核验单 {doc_no}（[{b3}, {b4}]）已确认：status={body['status']}")

    section("2. 预演 A：拟把 CP2 的开工前清洁集合由 [peanut] 更正为 []（只读，不落库）")
    st, preview_a = call("POST", f"/api/batches/{b2}/cleaning/preview", {
        "cleaned_allergens": [],
    })
    assert st == 200, preview_a
    for entry in preview_a["batches"]:
        cur, prev = entry["current"], entry["preview"]
        print(
            f"  {entry['batch_no']}（seq={entry['seq']}，目标批={entry['is_correction_target']}）: "
            f"{cur['disposition']}(v{cur['version']}) -> {prev['disposition']}(v{prev['version']})；"
            f"残留 {cur['residual_allergens']} -> {prev['residual_allergens']}；"
            f"未声明 {cur['undeclared_allergens']} -> {prev['undeclared_allergens']}；"
            f"changed={entry['changed']}，属于版本影响范围={entry['in_version_scope']}"
        )
        for reason in entry["change_reasons"]:
            print(f"      · [{reason['code']}] {reason['message']}")
    print("  将转待复核的已确认核验单：")
    for v in preview_a["affected_verifications"]:
        print(f"      · {v['verification_no']}：{v['current_status']} -> "
              f"{v['status_if_applied']}；关联批次 "
              + ", ".join(f"{x['batch_no']}(v{x['current_version']}->{x['preview_version']}, "
                          f"{x['current_disposition']}->{x['preview_disposition']})"
                          for x in v["batches"]))

    section("3. 预演 B：拟更正集合与当前相同（[peanut]）——结论相同仍属于版本影响范围")
    st, preview_b = call("POST", f"/api/batches/{b2}/cleaning/preview", {
        "cleaned_allergens": ["peanut"],
    })
    assert st == 200, preview_b
    for entry in preview_b["batches"]:
        assert entry["changed"] is False and entry["in_version_scope"] is True
        print(f"  {entry['batch_no']}: 结论保持 {entry['current']['disposition']}，"
              f"但正式更正仍将追加 v{entry['preview']['version']}；"
              f"原因：{entry['change_reasons'][0]['code']}")
    affected = {v["verification_no"] for v in preview_b["affected_verifications"]}
    print(f"  已确认核验单 {doc_no} 仍在影响清单中：{doc_no in affected}")

    section("4. 只读性核对：预演后清洁记录、裁决版本与核验单状态均未变化")
    _, vdoc = call("GET", f"/api/verifications/{doc_no}")
    _, b2_now = call("GET", f"/api/batches/{b2}")
    print(f"  核验单状态仍为：{vdoc['status']}（预演不应转待复核）")
    print(f"  {b2} 当前清洁记录：{b2_now['cleaned_allergens']}；"
          f"裁决版本 v{b2_now['adjudication']['version']}，结论 "
          f"{b2_now['adjudication']['disposition']}（与预演前一致）")
    assert vdoc["status"] == "CONFIRMED"
    assert b2_now["cleaned_allergens"] == ["peanut"]

    section("5. 拒绝场景：未知批次 404 / 非法过敏原代码 400（整体报错、不落库）")
    st, body = call("POST", "/api/batches/NO-SUCH-BATCH/cleaning/preview", {
        "cleaned_allergens": ["peanut"],
    })
    print(f"  未知批次：[{st}] {body['error']['code']}")
    assert st == 404 and body["error"]["code"] == "CORRECTION_TARGET_NOT_FOUND"

    st, body = call("POST", f"/api/batches/{b2}/cleaning/preview", {
        "cleaned_allergens": ["shellfish"],
    })
    print(f"  非法过敏原：[{st}] {body['error']['code']} {body['error']['message']}")
    assert st == 400 and body["error"]["code"] == "INVALID_ALLERGEN"

    section("6. 原有正式更正入口保持兼容（PUT cleaning；此处才真正落库）")
    st, body = call("PUT", f"/api/batches/{b2}/cleaning", {
        "cleaned_allergens": [],
    })
    assert st == 200, body
    print("  正式更正已落库，重算："
          + ", ".join(f"{r['batch_no']}={r['disposition']}(v{r['version']})"
                      for r in body["recomputed"]))
    _, vdoc = call("GET", f"/api/verifications/{doc_no}")
    print(f"  核验单 {doc_no} 现状态：{vdoc['status']}（正式更正后已自动转待复核）")
    assert vdoc["status"] == "PENDING_REVIEW"

    print("\n预演端到端示例完成。")
    return 0


if __name__ == "__main__":
    sys.exit(main())

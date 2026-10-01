#!/usr/bin/env python3
"""食品换线放行服务 —— 端到端调用示例（仅使用 Python 标准库）。

演示流程（共用产线 L1，顺序登记）：
  1. 登记两个原料批号（奶粉含 milk、花生酱含 peanut）
  2. B2026001：生产牛奶巧克力，声明 milk，无清洁 -> 放行，milk 留在产线
  3. B2026002：生产花生黑巧克力，开工前仅清洁 milk、成品只声明 milk
       -> milk 被清除；原料 peanut 未声明 -> 扣留，且 peanut 残留在产线
  4. B2026003：生产纯黑巧克力（无过敏原原料），本次未清洁
       -> 上一批 peanut 继续传递且未声明 -> 扣留（上一批即使被扣留仍传递）
  5. B2026004：继续生产纯黑巧克力，未清洁 -> peanut 继续传递 -> 扣留
  6. 更正 B2026003 开工前的清洁核验：确认清除上一批留下的 peanut
       -> B2026003 与后续 B2026004 按序重算，均变为放行
  7. 查询 B2026003 的历史裁决版本（v1 扣留 -> v2 放行）

用法：
    python examples/run_demo.py            # 默认 http://127.0.0.1:8080
    BASE_URL=http://192.168.1.10:8080 python examples/run_demo.py
"""
from __future__ import annotations

import json
import os
import sys
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
    verdict = ""
    adj = body.get("adjudication")
    if adj:
        verdict = f"  => {adj['disposition']}（v{adj['version']}）"
    print(f"[{status}] {label}{verdict}")
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
    status, body = call("GET", "/health")
    if status != 200:
        print(f"服务不可用（/health 返回 {status}），请先启动服务。", file=sys.stderr)
        return 1
    print("健康检查通过")

    section("1. 登记原料批号")
    ensure_lot("RM-DEMO-MILK", "示例全脂奶粉", ["milk"])
    ensure_lot("RM-DEMO-PEANUT", "示例花生酱", ["peanut"])

    section("2. 批次 B2026001：牛奶巧克力（声明 milk，产线起始无清洁）")
    status, b1 = call("POST", "/api/batches", {
        "batch_no": "B2026001",
        "line_id": "L1",
        "material_lots": ["RM-DEMO-MILK"],
        "declared_allergens": ["milk"],
        "cleaned_allergens": [],
    })
    show("登记 B2026001", status, b1)

    section("3. 批次 B2026002：花生黑巧（仅清洁 milk；只声明 milk）")
    status, b2 = call("POST", "/api/batches", {
        "batch_no": "B2026002",
        "line_id": "L1",
        "material_lots": ["RM-DEMO-PEANUT"],
        "declared_allergens": ["milk"],
        "cleaned_allergens": ["milk"],
    })
    show("登记 B2026002", status, b2)

    section("4. 批次 B2026003：纯黑巧（无过敏原原料，本次未清洁）")
    status, b3 = call("POST", "/api/batches", {
        "batch_no": "B2026003",
        "line_id": "L1",
        "material_lots": [],
        "declared_allergens": [],
        "cleaned_allergens": [],
    })
    show("登记 B2026003", status, b3)
    print("  预期：B2026002 被扣留，但 peanut 仍向下传递，B2026003 扣留。")

    section("5. 批次 B2026004：继续纯黑巧（未清洁，验证长链传递）")
    status, b4 = call("POST", "/api/batches", {
        "batch_no": "B2026004",
        "line_id": "L1",
        "material_lots": [],
        "declared_allergens": [],
        "cleaned_allergens": [],
    })
    show("登记 B2026004", status, b4)
    print("  预期：peanut 继续传递，B2026004 扣留。")

    section("6. 更正 B2026003 开工前的清洁核验：确认清除上一批留下的 peanut")
    status, corrected = call("PUT", "/api/batches/B2026003/cleaning", {
        "cleaned_allergens": ["peanut"],
    })
    show("清洁更正（重算 B2026003 与 B2026004）", status, corrected)
    print("  预期：B2026003 清洁覆盖 peanut 后放行；B2026004 无残留可继承，连带放行。")

    section("7. 查询 B2026003 的历史裁决版本")
    status, history = call("GET", "/api/batches/B2026003/history")
    show("B2026003 历史版本", status, history)
    print("  预期：v1=HOLD（REGISTER），v2=RELEASE（CORRECT_BATCH）。")

    section("8. 错误处理示例：重复批次号 / 无效过敏原 / 未知原料 / 更正不存在批次")
    for label, method, path, payload in [
        ("重复批次号", "POST", "/api/batches", {
            "batch_no": "B2026001", "line_id": "L1",
            "material_lots": [], "declared_allergens": [], "cleaned_allergens": [],
        }),
        ("无效过敏原标识", "POST", "/api/material-lots", {
            "lot_no": "RM-BAD", "material": "坏数据", "allergens": ["shellfish"],
        }),
        ("未知原料批号", "POST", "/api/batches", {
            "batch_no": "B-UNKNOWN", "line_id": "L1",
            "material_lots": ["RM-NOT-EXIST"],
            "declared_allergens": [], "cleaned_allergens": [],
        }),
        ("更正目标不存在", "PUT", "/api/batches/NO-SUCH-BATCH/cleaning", {
            "cleaned_allergens": ["milk"],
        }),
    ]:
        status, body = call(method, path, payload)
        print(f"[{status}] {label}: {body['error']['code']} - {body['error']['message']}")

    section("完成")
    print("可继续在交互式文档中探索： %s/docs" % BASE_URL)
    return 0


if __name__ == "__main__":
    sys.exit(main())

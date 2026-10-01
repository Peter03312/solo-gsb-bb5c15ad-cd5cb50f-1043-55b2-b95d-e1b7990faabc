"""过敏原受控字典。

初始化时写入数据库，接口只接受这里登记的过敏原标识。
标识采用欧盟 14 类法定过敏原的稳定英文代码（全部小写），
中文名称仅用于展示，调用方传入/返回一律使用代码。
"""
from __future__ import annotations

# code -> 中文名称
ALLERGENS: dict[str, str] = {
    "gluten": "含麸质谷物",
    "crustaceans": "甲壳类动物",
    "egg": "蛋",
    "fish": "鱼",
    "peanut": "花生",
    "soy": "大豆",
    "milk": "乳",
    "tree_nuts": "坚果",
    "celery": "芹菜",
    "mustard": "芥末",
    "sesame": "芝麻",
    "sulphites": "二氧化硫/亚硫酸盐",
    "lupin": "羽扇豆",
    "molluscs": "软体动物",
}


def is_valid(code: str) -> bool:
    return code in ALLERGENS


def invalid_codes(codes) -> list[str]:
    """返回输入中不在受控字典内的标识。"""
    return [c for c in codes if c not in ALLERGENS]

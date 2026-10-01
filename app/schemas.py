"""HTTP 请求模型。"""
from __future__ import annotations

from pydantic import BaseModel, Field


class MaterialLotIn(BaseModel):
    lot_no: str = Field(..., description="原料批号（唯一）")
    material: str = Field(..., description="原料名称")
    allergens: list[str] = Field(default_factory=list, description="该批号原料含有的过敏原代码")


class MaterialLotCorrection(BaseModel):
    material: str | None = Field(None, description="更正后的原料名称；不传表示不变")
    allergens: list[str] | None = Field(None, description="更正后的过敏原代码全集；不传表示不变")


class BatchIn(BaseModel):
    batch_no: str = Field(..., description="成品生产批次号（全局唯一）")
    line_id: str = Field(..., description="共用产线标识；首次出现时自动建档")
    material_lots: list[str] = Field(default_factory=list, description="本批所用原料批号")
    declared_allergens: list[str] = Field(default_factory=list, description="成品标签声明的过敏原代码")
    cleaned_allergens: list[str] = Field(default_factory=list, description="批次开始前清洁核验确认清除的过敏原代码")


class BatchCorrection(BaseModel):
    material_lots: list[str] | None = Field(None, description="更正后的原料批号全集；不传表示不变")
    declared_allergens: list[str] | None = Field(None, description="更正后的声明过敏原全集；不传表示不变")
    cleaned_allergens: list[str] | None = Field(None, description="更正后的清洁确认过敏原全集；不传表示不变")


class CleaningCorrection(BaseModel):
    cleaned_allergens: list[str] = Field(..., description="重新核验后确认已清除的过敏原代码全集")


class LotLockIn(BaseModel):
    reason: str = Field(..., description="锁定/解除原因（必填，不允许空白）；重复提交相同状态不产生新变更")


class BatchVoidIn(BaseModel):
    reason: str = Field(..., description="作废原因（必填，不允许空白）；作废保留登记与历史可查，批次号不可复用")


class BatchReassignIn(BaseModel):
    target_line_id: str = Field(
        ...,
        description="更正归属的目标产线编号（必须为已登记产线，且与批次当前产线不同）",
    )


class VerificationIn(BaseModel):
    verification_no: str = Field(..., description="出货核验单号（唯一）")
    batch_nos: list[str] = Field(..., min_length=1, description="关联的已登记生产批次号列表；非空、不可重复，按提交顺序保存")


class VerificationReviseIn(BaseModel):
    expected_revision: int = Field(
        ...,
        ge=1,
        description="工作人员所见单据修订号（乐观并发）；须与当前修订号一致，否则整体拒绝（VERIFICATION_REVISION_CONFLICT）",
    )
    batch_nos: list[str] = Field(
        ...,
        min_length=1,
        description="修订后的完整批次号新清单：非空、不可重复、批次须全部存在且有效（ACTIVE），按提交顺序保存",
    )

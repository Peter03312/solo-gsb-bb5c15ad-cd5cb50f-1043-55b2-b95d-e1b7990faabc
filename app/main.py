"""FastAPI 应用与路由：食品换线放行 HTTP API。"""
from __future__ import annotations

from fastapi import Depends, FastAPI, Request
from fastapi.responses import JSONResponse

from . import config
from .db import Store, initialize
from .errors import APIError
from .schemas import (
    BatchCorrection,
    BatchIn,
    BatchReassignIn,
    BatchVoidIn,
    CleaningCorrection,
    HandoverIn,
    LotLockIn,
    MaterialLotCorrection,
    MaterialLotIn,
    VerificationIn,
    VerificationReviseIn,
)
from .service import Service


def create_app(db_path: str | None = None, seed_demo: bool | None = None) -> FastAPI:
    app = FastAPI(
        title="食品换线放行服务",
        description=(
            "面向共用产线的生产批次过敏原换线放行：登记原料批号与批次、"
            "计算残留传递与未声明过敏原，给出放行/扣留裁决；支持更正重算与历史版本查询。"
        ),
        version="1.0.0",
    )

    store = Store(db_path or config.DB_PATH)
    store.connect()
    initialize(store, config.SEED_DEMO_DATA if seed_demo is None else seed_demo)
    app.state.store = store
    app.state.service = Service(store)

    @app.exception_handler(APIError)
    async def handle_api_error(_: Request, exc: APIError) -> JSONResponse:
        error: dict = {"code": exc.code, "message": exc.message}
        if exc.details is not None:
            error["details"] = exc.details
        return JSONResponse(
            status_code=exc.status_code,
            content={"error": error},
        )

    def svc() -> Service:
        return app.state.service

    @app.get("/health", tags=["系统"])
    def health() -> dict[str, str]:
        return {"status": "ok"}

    # ---------------- 过敏原字典 ----------------

    @app.get("/api/allergens", tags=["过敏原字典"])
    def list_allergens(service: Service = Depends(svc)):
        return {"allergens": service.list_allergens()}

    # ---------------- 原料批号 ----------------

    @app.post("/api/material-lots", tags=["原料批号"], status_code=201)
    def create_material_lot(body: MaterialLotIn, service: Service = Depends(svc)):
        return service.create_material_lot(body.lot_no, body.material, body.allergens)

    @app.get("/api/material-lots", tags=["原料批号"])
    def list_material_lots(service: Service = Depends(svc)):
        return {"material_lots": service.list_material_lots()}

    @app.get("/api/material-lots/{lot_no}", tags=["原料批号"])
    def get_material_lot(lot_no: str, service: Service = Depends(svc)):
        return service.get_material_lot(lot_no)

    @app.put("/api/material-lots/{lot_no}", tags=["原料批号"])
    def correct_material_lot(lot_no: str, body: MaterialLotCorrection, service: Service = Depends(svc)):
        """更正原料批号（名称/过敏原），重算受影响批次及后续批次。"""
        return service.correct_material_lot(lot_no, body.material, body.allergens)

    @app.post("/api/material-lots/{lot_no}/lock", tags=["原料批号待检锁定"])
    def lock_material_lot(lot_no: str, body: LotLockIn, service: Service = Depends(svc)):
        """原料批号出现待检风险时按批号锁定并填写原因。

        未知批号 404、空原因 400；重复提交相同状态幂等无变化；
        状态实际变化使所有直接使用该批号的批次出货资格版本递增。
        """
        return service.set_material_lot_lock(lot_no, True, body.reason)

    @app.post("/api/material-lots/{lot_no}/unlock", tags=["原料批号待检锁定"])
    def unlock_material_lot(lot_no: str, body: LotLockIn, service: Service = Depends(svc)):
        """解除批号待检锁定并填写原因（同锁定的幂等与版本规则）。"""
        return service.set_material_lot_lock(lot_no, False, body.reason)

    @app.get("/api/material-lots/{lot_no}/lock", tags=["原料批号待检锁定"])
    def get_material_lot_lock(lot_no: str, service: Service = Depends(svc)):
        """查询批号当前锁定状态与历次锁定/解除变更。"""
        return service.get_material_lot_lock(lot_no)

    # ---------------- 产线 ----------------

    @app.get("/api/lines", tags=["产线"])
    def list_lines(service: Service = Depends(svc)):
        return {"lines": service.list_lines()}

    # ---------------- 批次与裁决 ----------------

    @app.post("/api/batches", tags=["批次"], status_code=201)
    def register_batch(body: BatchIn, service: Service = Depends(svc)):
        """按产线顺序登记一个生产批次，立即返回裁决。"""
        return service.register_batch(
            batch_no=body.batch_no,
            line_id=body.line_id,
            material_lots=body.material_lots,
            declared_allergens=body.declared_allergens,
            cleaned_allergens=body.cleaned_allergens,
        )

    @app.get("/api/batches", tags=["批次"])
    def list_batches(line_id: str | None = None, service: Service = Depends(svc)):
        return {"batches": service.list_batches(line_id)}

    @app.get("/api/batches/{batch_no}", tags=["批次"])
    def get_batch(batch_no: str, service: Service = Depends(svc)):
        return service.get_batch(batch_no)

    @app.put("/api/batches/{batch_no}", tags=["批次"])
    def correct_batch(batch_no: str, body: BatchCorrection, service: Service = Depends(svc)):
        """更正批次的原料批号/声明/清洁核验，按产线顺序重算本批及后续批次。"""
        return service.correct_batch(
            batch_no,
            body.material_lots,
            body.declared_allergens,
            body.cleaned_allergens,
        )

    @app.put("/api/batches/{batch_no}/cleaning", tags=["批次"])
    def correct_cleaning(batch_no: str, body: CleaningCorrection, service: Service = Depends(svc)):
        """专用：更正“批次开始前清洁核验确认清除的过敏原”。"""
        return service.correct_cleaning(batch_no, body.cleaned_allergens)

    @app.post("/api/batches/{batch_no}/cleaning/preview", tags=["清洁更正预演"])
    def preview_cleaning_correction(batch_no: str, body: CleaningCorrection, service: Service = Depends(svc)):
        """只读预演：正式更正开工前清洁记录前，推演同线该批及后续有效批次的
        残留/实际过敏原/未声明项与 RELEASE/HOLD 变化，并列出将因裁决版本追加
        而转待复核的已确认出货核验单。

        预演以同一次当前数据快照计算，不更改清洁记录、裁决/资格版本、核验单状态或历史；
        从该批起跳过作废批次逐批推演，后续有效批次即使结论相同仍属于版本影响范围；
        待检锁定仅影响出货资格，不参与过敏原推演。
        未知/已作废批次、非法过敏原代码整体报错（错误码与正式更正入口一致）。
        """
        return service.preview_cleaning_correction(batch_no, body.cleaned_allergens)

    @app.post("/api/batches/{batch_no}/void", tags=["批次作废"])
    def void_batch(batch_no: str, body: BatchVoidIn, service: Service = Depends(svc)):
        """作废误登记、实际未投产的批次（必须填写非空原因）。

        作废保留原登记、历史裁决与资格及原因供查询，批次号不可复用；
        作废批次退出同线残留链，其后有效批次按序重算并追加版本；
        涉及的已确认核验单转待复核，历次确认快照不改；重复作废幂等不重算。
        未知批次 404、空原因 400，失败不留下部分版本。
        """
        return service.void_batch(batch_no, body.reason)

    @app.post("/api/batches/{batch_no}/reassign", tags=["批次换线更正"])
    def reassign_batch(batch_no: str, body: BatchReassignIn, service: Service = Depends(svc)):
        """更正批次误归产线：凭有效批次号与另一条已登记产线编号移至目标线末尾。

        批次号不变，获得目标线新顺序号，原线顺序号留空不复用；原线从移出位置起、
        目标线从移入批次起按有效批次顺序重算过敏原残留与裁决，各追加历史裁决版本
        （历史版本中的原产线与顺序号不改）；含这些批次的已确认核验单转待复核，
        历次确认快照保持原样；直接用料未变，出货资格版本不递增。
        未知批次/目标线、已作废批次、目标线与原线相同均整体拒绝，失败不留部分改动。
        """
        return service.reassign_batch(batch_no, body.target_line_id)

    @app.get("/api/batches/{batch_no}/history", tags=["批次"])
    def batch_history(batch_no: str, service: Service = Depends(svc)):
        """查询某批次历次裁决版本（可审计的历史裁决）。"""
        return service.batch_history(batch_no)

    # ---------------- 出货资格（原料批号待检锁定） ----------------

    @app.get("/api/batches/{batch_no}/eligibility", tags=["出货资格"])
    def get_batch_eligibility(batch_no: str, service: Service = Depends(svc)):
        """查询单批当前出货资格及历次版本（批号锁定状态决定）。"""
        return service.get_shipment_eligibility(batch_no)

    @app.get("/api/eligibilities", tags=["出货资格"])
    def list_eligibilities(
        line_id: str | None = None,
        status: str | None = None,
        service: Service = Depends(svc),
    ):
        """逐批列出出货资格，可按产线与 SHIPPABLE / BLOCKED 过滤。"""
        return service.list_shipment_eligibility(line_id, status)

    # ---------------- 出货核验单 ----------------

    @app.post("/api/verifications", tags=["出货核验单"], status_code=201)
    def create_verification(body: VerificationIn, service: Service = Depends(svc)):
        """创建出货核验单（草稿）：唯一单号 + 非空、无重复的已登记批次号列表。

        逐批保存创建时的当前裁决版本与结论；未知批次、重复单号/批次均整体拒绝。
        """
        return service.create_verification(body.verification_no, body.batch_nos)

    @app.get("/api/verifications", tags=["出货核验单"])
    def list_verifications(status: str | None = None, service: Service = Depends(svc)):
        """列出出货核验单（可按 DRAFT / PENDING_REVIEW / CONFIRMED 过滤）。"""
        return service.list_verifications(status)

    @app.get("/api/verifications/{verification_no}", tags=["出货核验单"])
    def get_verification(verification_no: str, service: Service = Depends(svc)):
        """查询核验单状态、逐批记录（快照版本/当前版本/变化原因）与历次确认快照。"""
        return service.get_verification(verification_no)

    @app.post("/api/verifications/{verification_no}/reverify", tags=["出货核验单"])
    def reverify_verification(verification_no: str, service: Service = Depends(svc)):
        """重新核验：刷新逐批快照并回到草稿（草稿/待复核可用；已确认拒绝）。"""
        return service.reverify_verification(verification_no)

    @app.post("/api/verifications/{verification_no}/revise", tags=["出货核验单"])
    def revise_verification(verification_no: str, body: VerificationReviseIn, service: Service = Depends(svc)):
        """修订出货前批次清单：凭所见修订号提交完整、有序的新批次清单（仅草稿/待复核）。

        同一事务内逐批重取当前裁决与出货资格快照，单据回到草稿并推进修订号；
        允许移除旧清单中的已作废批次，新清单批次必须全部有效。
        空清单/重复/未知批次、含已作废批次、过期修订号、已确认单据均整体拒绝，
        当前成员与快照不变；历次确认快照始终保留。
        """
        return service.revise_verification(
            verification_no, body.expected_revision, body.batch_nos
        )

    @app.post("/api/verifications/{verification_no}/confirm", tags=["出货核验单"])
    def confirm_verification(verification_no: str, service: Service = Depends(svc)):
        """确认：原子校验逐批快照仍为当前版本且全部 RELEASE。

        不符时返回 422 与不符批次明细（STALE_SNAPSHOT / BATCH_ON_HOLD），单据保持未确认。
        """
        return service.confirm_verification(verification_no)

    # ---------------- 装车交接（整单发运） ----------------

    @app.post("/api/handovers", tags=["装车交接"], status_code=201)
    def create_handover(body: HandoverIn, service: Service = Depends(svc)):
        """装车交接：凭唯一交接号、已确认核验单号及读取时修订号登记整单发运。

        只接受仍为 CONFIRMED 且修订号匹配的单据；逐批校验有效、当前裁决/出货资格
        版本与最近一次确认快照一致且结论分别为 RELEASE / SHIPPABLE；任一批次已在其他
        交接记录中发运即整单拒绝。一次事务占用全单批次并冻结单号、修订号、逐批确认
        快照与交接时间；后续更正、待复核或清单修订不改写交接记录。重复交接号、未知
        单据、状态/版本冲突、任一不合格批次均整体拒绝，不留下部分占用。
        """
        return service.create_handover(
            body.handover_no, body.verification_no, body.expected_revision
        )

    @app.get("/api/handovers", tags=["装车交接"])
    def list_handovers(
        verification_no: str | None = None, service: Service = Depends(svc)
    ):
        """列出装车交接记录（可按核验单号过滤）。"""
        return service.list_handovers(verification_no)

    @app.get("/api/handovers/{handover_no}", tags=["装车交接"])
    def get_handover(handover_no: str, service: Service = Depends(svc)):
        """查询单次交接：冻结的单号/修订号/确认引用/交接时间与逐批确认快照。"""
        return service.get_handover(handover_no)

    @app.get("/api/batches/{batch_no}/handover", tags=["装车交接"])
    def get_batch_handover(batch_no: str, service: Service = Depends(svc)):
        """追溯单批的交接归属（未发运返回 shipped=false；未知批次 404）。"""
        return service.get_batch_handover(batch_no)

    return app


app = create_app()

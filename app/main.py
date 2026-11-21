"""FastAPI 应用：纸样厚度计数核对与客户凭证明号核验。

* ``POST /inspections``                       登记一批纸样的总片数
* ``GET  /inspections``                       列出全部登记
* ``GET  /inspections/{id}``                  查看登记及已接收的分次补报
* ``PATCH /inspections/{id}``                 更正总片数/备注（相容性校验 + 同事务重估证明）
* ``POST /inspections/{id}/reports``          分次提交厚度区间计数（整份原子接收或拒收）
* ``GET  /inspections/{id}/reports``          查看补报历史
* ``GET  /inspections/{id}/bounds``           给定合格区间，查询合格片数最少/最多
* ``POST /inspections/{id}/certificates``     按承诺片数开具客户证明
* ``GET  /inspections/{id}/certificates``     按质检号列出全部证明（含已撤销）
* ``GET  /certificates/{certificate_no}``     客户凭证明号核验
* ``GET  /health``                            健康检查

所有区间均为整数微米，含起点不含终点，即 ``[lo, hi)``，且 ``hi > lo``。
"""

from __future__ import annotations

import os
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, HTTPException, Query, status
from sqlite3 import Connection

from . import db
from .schemas import (
    BoundsOut,
    CertificateCreate,
    CertificateOut,
    CountReportIn,
    CountReportOut,
    InspectionCreate,
    InspectionDetail,
    InspectionSummary,
    InspectionUpdate,
)
from .solver import InconsistentCounts, check_consistent, compute_bounds

DEFAULT_DB_PATH = os.environ.get("DB_PATH", "./data/inspection.db")


def create_app(db_path: str | None = None) -> FastAPI:
    path = db_path or DEFAULT_DB_PATH

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        db.init(path)
        yield

    api = FastAPI(
        title="纸样厚度计数核对与客户凭证核验",
        description=(
            "登记总片数，分次补报互可重叠的厚度区间计数；"
            "按全部计数的交集计算合格片数的可达到上下界，不逐片展开。"
            "质检员按质检号给定合格区间与承诺片数开具客户证明；总片数更正生效时，"
            "按新下界撤销不再满足承诺的证明，撤销为终态，其余证明照常有效。"
        ),
        version="1.1.0",
        lifespan=lifespan,
    )

    def get_conn():
        connection = db.connect(path)
        try:
            yield connection
        finally:
            connection.close()

    def _require_inspection(connection: Connection, inspection_id: int):
        row = db.get_inspection(connection, inspection_id)
        if row is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"登记 {inspection_id} 不存在",
            )
        return row

    def _summary(row) -> InspectionSummary:
        return InspectionSummary(
            id=row["id"],
            total_pieces=row["total_pieces"],
            note=row["note"],
            created_at=row["created_at"],
            report_count=row["report_count"],
            segment_count=row["segment_count"],
            active_certificate_count=row["active_certificate_count"],
            revoked_certificate_count=row["revoked_certificate_count"],
        )

    def _certificate_out(
        connection: Connection, row, constraints_cache: dict[int, list] | None = None
    ) -> CertificateOut:
        """把证明行转成响应；有效证明实时计算当前上下界，已撤销证明返回撤销快照。"""
        inspection_id = int(row["inspection_id"])
        base = dict(
            certificate_no=db.format_certificate_no(int(row["id"])),
            inspection_id=inspection_id,
            status=row["status"],
            qualified_lo=int(row["qualified_lo"]),
            qualified_hi=int(row["qualified_hi"]),
            committed_count=int(row["committed_count"]),
            issued_total_pieces=int(row["issued_total_pieces"]),
            issued_min_count=int(row["issued_min_count"]),
            issued_at=row["issued_at"],
            revoked_at=row["revoked_at"],
            revoked_reason=row["revoked_reason"],
            revoked_total_pieces=(
                None
                if row["revoked_total_pieces"] is None
                else int(row["revoked_total_pieces"])
            ),
            revoked_min_count=(
                None
                if row["revoked_min_count"] is None
                else int(row["revoked_min_count"])
            ),
        )
        if row["status"] == "revoked":
            # 已撤销：冻结在撤销时的总片数与下界，后续更正不再改写、不恢复效力。
            base["current_total_pieces"] = int(row["revoked_total_pieces"])
            base["current_min_count"] = None
            base["current_max_count"] = None
            return CertificateOut(**base)

        inspection = db.get_inspection(connection, inspection_id)
        if constraints_cache is not None and inspection_id in constraints_cache:
            constraints = constraints_cache[inspection_id]
        else:
            constraints = db.load_constraints(connection, inspection_id)
            if constraints_cache is not None:
                constraints_cache[inspection_id] = constraints
        try:
            min_count, max_count = compute_bounds(
                int(inspection["total_pieces"]),
                constraints,
                int(row["qualified_lo"]),
                int(row["qualified_hi"]),
            )
        except InconsistentCounts as exc:  # 防御：写入路径已保证相容
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="已存计数互相矛盾，无法计算；请检查数据",
            ) from exc
        base["current_total_pieces"] = int(inspection["total_pieces"])
        base["current_min_count"] = min_count
        base["current_max_count"] = max_count
        return CertificateOut(**base)

    @api.get("/health", tags=["运维"], summary="健康检查")
    def health() -> dict:
        return {"status": "ok"}

    @api.post(
        "/inspections",
        response_model=InspectionSummary,
        status_code=status.HTTP_201_CREATED,
        tags=["登记"],
        summary="登记一批纸样的总片数",
    )
    def create_inspection(
        payload: InspectionCreate, connection: Connection = Depends(get_conn)
    ) -> InspectionSummary:
        inspection_id = db.create_inspection(
            connection, payload.total_pieces, payload.note
        )
        row = _require_inspection(connection, inspection_id)
        return _summary(row)

    @api.get(
        "/inspections",
        response_model=list[InspectionSummary],
        tags=["登记"],
        summary="列出全部登记",
    )
    def list_inspections(
        connection: Connection = Depends(get_conn),
    ) -> list[InspectionSummary]:
        return [_summary(row) for row in db.list_inspections(connection)]

    @api.get(
        "/inspections/{inspection_id}",
        response_model=InspectionDetail,
        tags=["登记"],
        summary="查看登记及已接收的补报",
    )
    def get_inspection(
        inspection_id: int, connection: Connection = Depends(get_conn)
    ) -> InspectionDetail:
        row = _require_inspection(connection, inspection_id)
        summary = _summary(row)
        reports = [CountReportOut(**r) for r in db.get_reports(connection, inspection_id)]
        return InspectionDetail(**summary.model_dump(), reports=reports)

    @api.patch(
        "/inspections/{inspection_id}",
        response_model=InspectionSummary,
        tags=["登记"],
        summary="更正总片数或备注",
        description=(
            "新总片数仍须通过原有相容性校验；不相容则整次更正拒收，原登记、"
            "原证明状态全部保持不变、仍可查询。更正生效的同一事务内，按新总片数"
            "纳入此前已提交的全部补报与证明，重算各有效证明合格区间的下界："
            "下界不再达到原承诺片数的证明一并撤销（终态），其余有效证明照常可用。"
            "试算期间同批的补报与开证可继续提交，均会在更正生效时被纳入核算。"
        ),
    )
    def update_inspection(
        inspection_id: int,
        payload: InspectionUpdate,
        connection: Connection = Depends(get_conn),
    ) -> InspectionSummary:
        connection.execute("BEGIN IMMEDIATE")
        try:
            row = _require_inspection(connection, inspection_id)
            total_changes = "total_pieces" in payload.model_fields_set
            new_total = (
                payload.total_pieces
                if total_changes
                else int(row["total_pieces"])
            )
            new_note = payload.note if "note" in payload.model_fields_set else row["note"]
            if total_changes:
                # 原有相容性校验：与全部已接收补报不相容则整次更正回滚。
                constraints = db.load_constraints(connection, inspection_id)
                try:
                    check_consistent(new_total, constraints)
                except InconsistentCounts:
                    raise HTTPException(
                        status_code=status.HTTP_409_CONFLICT,
                        detail=(
                            f"新总片数 {new_total} 与已接收的计数不相容，"
                            "本次更正已拒收，原登记与证明状态保持不变"
                        ),
                    )
            db.update_inspection(connection, inspection_id, new_total, new_note)

            if total_changes and new_total != int(row["total_pieces"]):
                # 更正生效：同事务纳入此前全部补报，按新下界重估有效证明。
                constraints = db.load_constraints(connection, inspection_id)
                for certificate in db.list_active_certificates(
                    connection, inspection_id
                ):
                    min_count, _ = compute_bounds(
                        new_total,
                        constraints,
                        int(certificate["qualified_lo"]),
                        int(certificate["qualified_hi"]),
                    )
                    if min_count < int(certificate["committed_count"]):
                        db.revoke_certificate(
                            connection,
                            int(certificate["id"]),
                            revoked_total_pieces=new_total,
                            revoked_min_count=min_count,
                            reason=(
                                f"总片数更正为 {new_total} 后，合格区间"
                                f"[{certificate['qualified_lo']},"
                                f"{certificate['qualified_hi']}) 的合格片数下界"
                                f"降为 {min_count}，低于承诺片数 "
                                f"{int(certificate['committed_count'])}"
                            ),
                        )
            connection.execute("COMMIT")
        except HTTPException:
            connection.execute("ROLLBACK")
            raise
        except BaseException:
            connection.execute("ROLLBACK")
            raise
        row = _require_inspection(connection, inspection_id)
        return _summary(row)

    @api.post(
        "/inspections/{inspection_id}/reports",
        response_model=CountReportOut,
        status_code=status.HTTP_201_CREATED,
        tags=["补报"],
        summary="提交一批厚度区间计数",
        description=(
            "区间可以重叠。整份报告要么全部接收，要么在与已有计数"
            "（含本报告内其他区间）不相容时整份拒收；拒收不改变已有结果。"
        ),
    )
    def submit_report(
        inspection_id: int,
        payload: CountReportIn,
        connection: Connection = Depends(get_conn),
    ) -> CountReportOut:
        segments = [(s.lo, s.hi, s.count) for s in payload.segments]

        connection.execute("BEGIN IMMEDIATE")
        try:
            row = _require_inspection(connection, inspection_id)
            existing = db.load_constraints(connection, inspection_id)
            try:
                check_consistent(int(row["total_pieces"]), existing + segments)
            except InconsistentCounts as exc:
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail=(
                        "补报与已有计数（或本报告内部）不相容，整份拒收；"
                        "已有结果保持不变"
                    ),
                ) from exc
            report_id = db.insert_report(connection, inspection_id, segments)
            connection.execute("COMMIT")
        except HTTPException:
            connection.execute("ROLLBACK")
            raise
        except BaseException:
            connection.execute("ROLLBACK")
            raise

        reports = db.get_reports(connection, inspection_id)
        for report in reports:
            if report["id"] == report_id:
                return CountReportOut(**report)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="补报写入后未能读回",
        )

    @api.get(
        "/inspections/{inspection_id}/reports",
        response_model=list[CountReportOut],
        tags=["补报"],
        summary="查看补报历史",
    )
    def list_reports(
        inspection_id: int, connection: Connection = Depends(get_conn)
    ) -> list[CountReportOut]:
        _require_inspection(connection, inspection_id)
        return [CountReportOut(**r) for r in db.get_reports(connection, inspection_id)]

    @api.get(
        "/inspections/{inspection_id}/bounds",
        response_model=BoundsOut,
        tags=["查询"],
        summary="查询合格片数的最少/最多可达到值",
        description=(
            "合格区间为 [qualified_lo, qualified_hi)，整数微米，含起点不含终点。"
            "返回值是同时满足全部已接收计数时，合格片数可同时达到的最小值和最大值；"
            "不逐片展开，按端点划分的整数规划精确求解。"
        ),
    )
    def get_bounds(
        inspection_id: int,
        qualified_lo: int = Query(..., ge=0, description="合格区间起点，包含"),
        qualified_hi: int = Query(..., description="合格区间终点，不包含，须大于起点"),
        connection: Connection = Depends(get_conn),
    ) -> BoundsOut:
        if qualified_hi <= qualified_lo:
            raise HTTPException(
                status_code=422,
                detail="qualified_hi 必须大于 qualified_lo（区间含起点、不含终点）",
            )
        row = _require_inspection(connection, inspection_id)
        constraints = db.load_constraints(connection, inspection_id)
        try:
            min_count, max_count = compute_bounds(
                int(row["total_pieces"]), constraints, qualified_lo, qualified_hi
            )
        except InconsistentCounts as exc:
            # 正常写入路径会拒绝不相容数据；此处防御数据库被外部改写。
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="已存计数互相矛盾，无法计算；请检查数据",
            ) from exc
        return BoundsOut(
            inspection_id=inspection_id,
            total_pieces=int(row["total_pieces"]),
            qualified_lo=qualified_lo,
            qualified_hi=qualified_hi,
            min_count=min_count,
            max_count=max_count,
        )

    @api.post(
        "/inspections/{inspection_id}/certificates",
        response_model=CertificateOut,
        status_code=status.HTTP_201_CREATED,
        tags=["证明"],
        summary="按质检号开具客户证明",
        description=(
            "质检员按质检号提供合格区间与承诺片数。开证时按当时生效的总片数和"
            "全部已收补报核算合格片数下界；下界达到承诺片数才开证，否则 409 拒绝，"
            "不写入任何证明。试算期间同批仍可继续补报与开证。"
        ),
    )
    def issue_certificate(
        inspection_id: int,
        payload: CertificateCreate,
        connection: Connection = Depends(get_conn),
    ) -> CertificateOut:
        connection.execute("BEGIN IMMEDIATE")
        try:
            row = _require_inspection(connection, inspection_id)
            total_pieces = int(row["total_pieces"])
            constraints = db.load_constraints(connection, inspection_id)
            try:
                min_count, max_count = compute_bounds(
                    total_pieces,
                    constraints,
                    payload.qualified_lo,
                    payload.qualified_hi,
                )
            except InconsistentCounts as exc:  # 防御：写入路径已保证相容
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail="已存计数互相矛盾，无法核算；请检查数据",
                ) from exc
            if min_count < payload.committed_count:
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail=(
                        f"合格区间 [{payload.qualified_lo},{payload.qualified_hi}) "
                        f"的合格片数下界为 {min_count}，未达到承诺片数 "
                        f"{payload.committed_count}，不予开证；已有数据保持不变"
                    ),
                )
            certificate_id = db.insert_certificate(
                connection,
                inspection_id,
                qualified_lo=payload.qualified_lo,
                qualified_hi=payload.qualified_hi,
                committed_count=payload.committed_count,
                issued_total_pieces=total_pieces,
                issued_min_count=min_count,
            )
            connection.execute("COMMIT")
        except HTTPException:
            connection.execute("ROLLBACK")
            raise
        except BaseException:
            connection.execute("ROLLBACK")
            raise

        certificate = db.get_certificate(connection, certificate_id)
        if certificate is None:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="证明写入后未能读回",
            )
        return _certificate_out(connection, certificate)

    @api.get(
        "/inspections/{inspection_id}/certificates",
        response_model=list[CertificateOut],
        tags=["证明"],
        summary="按质检号列出全部证明（含已撤销）",
        description=(
            "返回该质检号下的全部证明。有效证明附当前总片数与实时下界/上界；"
            "已撤销证明保留原承诺、更正后的总片数与撤销时的下界快照。"
        ),
    )
    def list_certificates(
        inspection_id: int,
        connection: Connection = Depends(get_conn),
    ) -> list[CertificateOut]:
        _require_inspection(connection, inspection_id)
        rows = db.list_certificates(connection, inspection_id)
        cache: dict[int, list] = {}
        return [_certificate_out(connection, row, cache) for row in rows]

    @api.get(
        "/certificates/{certificate_no}",
        response_model=CertificateOut,
        tags=["证明"],
        summary="客户凭证明号核验",
        description=(
            "凭证明编号查询证明状态与承诺内容。已撤销的证明仍可查原承诺、"
            "更正后的总片数和撤销时的下界；撤销为终态，后续更正不会恢复其效力。"
        ),
    )
    def get_certificate(
        certificate_no: str,
        connection: Connection = Depends(get_conn),
    ) -> CertificateOut:
        certificate_id = db.parse_certificate_no(certificate_no)
        if certificate_id is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"证明编号 {certificate_no!r} 不存在",
            )
        certificate = db.get_certificate(connection, certificate_id)
        if certificate is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"证明编号 {certificate_no!r} 不存在",
            )
        return _certificate_out(connection, certificate)

    return api


app = create_app()

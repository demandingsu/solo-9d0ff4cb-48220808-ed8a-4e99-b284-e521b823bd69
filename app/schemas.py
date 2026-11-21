"""API 请求/响应模型。"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field, model_validator


class SegmentIn(BaseModel):
    lo: int = Field(..., ge=0, description="区间起点，包含，整数微米")
    hi: int = Field(..., description="区间终点，不包含，整数微米，必须大于 lo")
    count: int = Field(..., ge=0, description="该厚度区间内的片数")

    @model_validator(mode="after")
    def _validate_range(self) -> "SegmentIn":
        if self.hi <= self.lo:
            raise ValueError("hi 必须大于 lo（区间含起点、不含终点）")
        return self


class CountReportIn(BaseModel):
    segments: list[SegmentIn] = Field(
        ...,
        min_length=1,
        description="本次补报的全部厚度区间计数；同一报告内或与已有报告可以重叠。"
        "任一区间与已有计数不相容时，整份报告拒收。",
    )


class InspectionCreate(BaseModel):
    total_pieces: int = Field(..., ge=0, description="纸样总片数")
    note: str | None = Field(None, max_length=500, description="备注，可选")


class InspectionUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    total_pieces: int | None = Field(None, ge=0, description="新的总片数，可选")
    note: str | None = Field(None, max_length=500, description="新的备注；传 null 可清空")

    @model_validator(mode="after")
    def _validate_payload(self) -> "InspectionUpdate":
        if not self.model_fields_set:
            raise ValueError("至少提供 total_pieces 或 note 中的一项")
        return self


class SegmentOut(BaseModel):
    id: int
    lo: int
    hi: int
    count: int


class CountReportOut(BaseModel):
    id: int
    inspection_id: int
    created_at: str
    segments: list[SegmentOut]


class InspectionSummary(BaseModel):
    id: int
    total_pieces: int
    note: str | None
    created_at: str
    report_count: int = Field(..., description="已接收的补报批次数")
    segment_count: int = Field(..., description="已接收的厚度区间条数")
    active_certificate_count: int = Field(0, description="当前有效证明份数")
    revoked_certificate_count: int = Field(0, description="已撤销证明份数（终态）")


class InspectionDetail(InspectionSummary):
    reports: list[CountReportOut]


class BoundsOut(BaseModel):
    inspection_id: int
    total_pieces: int
    qualified_lo: int
    qualified_hi: int
    min_count: int = Field(..., description="同时满足全部计数时，合格片数可达到的最小值")
    max_count: int = Field(..., description="同时满足全部计数时，合格片数可达到的最大值")


class CertificateCreate(BaseModel):
    qualified_lo: int = Field(..., ge=0, description="质检员给定的合格区间起点，包含")
    qualified_hi: int = Field(..., description="质检员给定的合格区间终点，不包含，须大于起点")
    committed_count: int = Field(..., ge=0, description="承诺的合格片数")

    @model_validator(mode="after")
    def _validate_range(self) -> "CertificateCreate":
        if self.qualified_hi <= self.qualified_lo:
            raise ValueError("qualified_hi 必须大于 qualified_lo（区间含起点、不含终点）")
        return self


class CertificateOut(BaseModel):
    certificate_no: str = Field(..., description="证明编号，客户据此核验")
    inspection_id: int
    status: str = Field(
        ...,
        description="active=有效；revoked=已撤销（撤销为终态，后续更正不会恢复效力）",
    )
    qualified_lo: int = Field(..., description="开证时质检员给定的合格区间起点")
    qualified_hi: int = Field(..., description="开证时质检员给定的合格区间终点")
    committed_count: int = Field(..., description="原承诺合格片数，撤销后仍可查")
    issued_total_pieces: int = Field(..., description="开证时生效的总片数")
    issued_min_count: int = Field(..., description="开证时核算的合格片数下界")
    issued_at: str
    revoked_at: str | None = Field(None, description="撤销时间；有效证明为 null")
    revoked_reason: str | None = Field(None, description="撤销原因；有效证明为 null")
    revoked_total_pieces: int | None = Field(
        None, description="撤销时更正生效的总片数（即更正后的总片数）；有效证明为 null"
    )
    revoked_min_count: int | None = Field(
        None, description="撤销时按更正后数据重算的合格片数下界；有效证明为 null"
    )
    current_total_pieces: int | None = Field(
        None, description="当前生效的总片数；已撤销证明固定为撤销时的快照"
    )
    current_min_count: int | None = Field(
        None,
        description="当前数据下该证明合格区间的合格片数下界（仅有效证明实时计算）",
    )
    current_max_count: int | None = Field(
        None,
        description="当前数据下该证明合格区间的合格片数上界（仅有效证明实时计算）",
    )

"""Pydantic request/response models for the REST API."""
from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field


class MatchRequest(BaseModel):
    query: str = Field(..., min_length=1, description="待匹配的外部地址文本")
    save: bool = Field(True, description="是否把匹配结果写入待复核/日志表")


class BatchMatchRequest(BaseModel):
    queries: list[str] = Field(..., min_length=1)
    save: bool = True


class AddressIn(BaseModel):
    address: str | None = None
    full_text: str | None = None
    line1: str | None = None
    city: str | None = None
    state: str | None = None
    postal_code: str | None = None
    postcode: str | None = None
    zip: str | None = None
    country: str | None = None
    external_id: str | None = None
    id: str | None = None

    def lookup_fields(self) -> dict[str, Any]:
        return {
            "line1": self.line1,
            "city": self.city,
            "state": self.state,
            "postal_code": self.postal_code or self.postcode or self.zip,
            "country": self.country,
            "external_id": self.external_id or self.id,
        }


class AddressBatchIn(BaseModel):
    addresses: list[AddressIn]
    skip_duplicates: bool = True


class ReviewAction(BaseModel):
    action: str = Field(..., pattern="^(confirmed|overridden|rejected)$")
    selected_id: int | None = None


class GroupReviewAction(BaseModel):
    """整组复核请求：group_key 为组签名，match_ids 为客户端看到的成员集合。"""
    group_key: str = Field(..., min_length=1)
    match_ids: list[int] = Field(..., min_length=1)
    action: str = Field(..., pattern="^(confirmed|overridden|rejected)$")
    selected_id: int | None = None


# --------------------------------------------------------- A/B rule experiments


class RuleProfileIn(BaseModel):
    """命名规则档案：自动命中/拒绝/分差阈值与词元权重。

    字段全部可选（缺省回退到服务端当前阈值）；取值范围与档案间一致性由
    ``app.experiments.validate_request`` 逐项校验并集中报错。
    """

    model_config = {"extra": "ignore"}

    name: str | None = None
    auto_threshold: float | None = None
    reject_threshold: float | None = None
    margin_threshold: float | None = None
    token_weight: float | None = None


class ExperimentRequest(BaseModel):
    """规则 A/B 对照实验请求：2～4 个档案，1～100 条查询。"""

    model_config = {"extra": "ignore"}

    name: str | None = Field(default=None, max_length=80)
    profiles: list[RuleProfileIn] = Field(..., min_length=2, max_length=4)
    queries: list[str] = Field(..., min_length=1, max_length=100)

"""换向间隔决策。

换向间隔**按炉组单独计算**（定死，不做全厂统一间隔）：每个炉组各持有一份
:class:`ReversalIntervalAdvisor`，只吃本炉组的蓄热室温度回读，用本炉组配置
里的策略与上下限出结果。炉况好的炉组拉长间隔省煤气，炉况差的炉组加密换向，
互不影响；全厂统一间隔只会对健康的炉组浪费煤气、对差的炉组又换得太少。

决策规则（优先级从上到下，命中即返回）：

1. 回读缺失或超过 ``readback_timeout_s`` 未更新 → 部分失败，回落保守基线
   （``reversal_interval_s``），理由 ``readback_missing``；
2. 炉组策略为 ``"fixed"`` → 始终保守基线，理由 ``policy_fixed``；
3. 偏差比 ≤ ``temp_good_ratio`` → 炉况好，拉长到 ``reversal_interval_max_s``，
   理由 ``temp_comfortable``；
4. 偏差比 ≤ 1.0（带内）→ 保持基线，理由 ``temp_in_band``；
5. 偏差比 > 1.0（越带）→ 炉况差，加密到 ``reversal_interval_min_s``，理由
   ``temp_out_of_band``。

决策只产出 :class:`IntervalDecision` 一个对象：排程器拿它判定是否到期，平台
把同一个对象的 ``as_dict()`` 原样写审计、喂控制台，保证"间隔调整依据与审计
一致"，不会出现决策一套说法、审计另一套说法。
"""

from __future__ import annotations

from dataclasses import dataclass

from ..clock import Clock
from ..config import HeatingLimits

REASON_READBACK_MISSING = "readback_missing"
REASON_POLICY_FIXED = "policy_fixed"
REASON_TEMP_COMFORTABLE = "temp_comfortable"
REASON_TEMP_IN_BAND = "temp_in_band"
REASON_TEMP_OUT_OF_BAND = "temp_out_of_band"


@dataclass(frozen=True)
class IntervalDecision:
    """一次换向间隔决策：结果连同全部依据。"""

    interval_s: float
    reason: str
    policy: str
    readback_c: float | None
    readback_age_s: float | None
    offset_ratio: float | None
    partial_failure: bool

    def as_dict(self) -> dict:
        return {
            "interval_s": round(self.interval_s, 2),
            "reason": self.reason,
            "policy": self.policy,
            "readback_c": None if self.readback_c is None else round(self.readback_c, 2),
            "readback_age_s": None if self.readback_age_s is None else round(self.readback_age_s, 2),
            "offset_ratio": None if self.offset_ratio is None else round(self.offset_ratio, 4),
            "partial_failure": self.partial_failure,
        }


class ReversalIntervalAdvisor:
    """按蓄热室温度回读给出本炉组当前生效的换向间隔。"""

    def __init__(self, limits: HeatingLimits, clock: Clock) -> None:
        self._limits = limits
        self._clock = clock
        self._readback_c: float | None = None
        self._readback_ts: float | None = None
        self._missed = 0

    @property
    def limits(self) -> HeatingLimits:
        return self._limits

    def feed(self, temperature_c: float | None, ts: float | None = None) -> None:
        """喂入一次蓄热室温度回读；``None`` 表示本次回读缺失（部分失败）。

        缺失立即生效：当前有效回读作废，间隔当场回落保守基线，而不是沿用上一
        次好值拖到超时。回读彻底不来了（不再 feed）则由 ``readback_timeout_s``
        兜底，同样按缺失处理。
        """

        if temperature_c is None:
            self._missed += 1
            self._readback_c = None
            self._readback_ts = None
            return
        self._readback_c = float(temperature_c)
        self._readback_ts = self._clock.now() if ts is None else ts

    def missed(self) -> int:
        """返回累计回读缺失次数。"""

        return self._missed

    def _readback_state(self, moment: float) -> tuple[float | None, float | None]:
        """返回（有效回读温度, 回读年龄秒数）；缺失或过期时温度为 ``None``。"""

        if self._readback_c is None or self._readback_ts is None:
            return None, None
        age = moment - self._readback_ts
        if age > self._limits.readback_timeout_s:
            return None, age
        return self._readback_c, age

    def decide(self, now: float | None = None) -> IntervalDecision:
        """按当前最新回读给出间隔决策（规则见模块 docstring）。"""

        moment = self._clock.now() if now is None else now
        limits = self._limits
        readback_c, age_s = self._readback_state(moment)
        if readback_c is None:
            return IntervalDecision(
                interval_s=limits.reversal_interval_s,
                reason=REASON_READBACK_MISSING,
                policy=limits.reversal_policy,
                readback_c=None,
                readback_age_s=age_s,
                offset_ratio=None,
                partial_failure=True,
            )
        ratio = limits.regen_offset_ratio(readback_c)
        if limits.reversal_policy == "fixed":
            interval_s, reason = limits.reversal_interval_s, REASON_POLICY_FIXED
        elif ratio <= limits.temp_good_ratio:
            interval_s, reason = limits.reversal_interval_max_s, REASON_TEMP_COMFORTABLE
        elif ratio <= 1.0:
            interval_s, reason = limits.reversal_interval_s, REASON_TEMP_IN_BAND
        else:
            interval_s, reason = limits.reversal_interval_min_s, REASON_TEMP_OUT_OF_BAND
        return IntervalDecision(
            interval_s=interval_s,
            reason=reason,
            policy=limits.reversal_policy,
            readback_c=readback_c,
            readback_age_s=age_s,
            offset_ratio=ratio,
            partial_failure=False,
        )

"""换向间隔调节（按炉组单独生效）。

设计决策（定死，不再每期重议）：

- **按炉组单独算**：每个炉组一份调节器，只消费本炉组的蓄热室温度回读，
  全厂不设统一间隔；炉组模式来自 ``ReversalLimits.mode_for(group)``。
- **炉况好时的行为按炉组定**：``economy`` 模式在炉温有余量时逐步拉长间隔
  省煤气，``conservative`` 模式始终走配置的保守间隔。
- **回读缺失属于部分失败**：温度未喂入、非数值、超龄或采样时间戳在未来，
  都按缺失处理——间隔立即回落保守间隔（``heating.reversal_interval_s``，
  即几年前定死的固定间隔），决策记 ``readback_missing``，系统不闭锁。
- **换向时序不能乱**：调节器只决定"何时到期"，不触碰换向编排的阀序；间隔
  被 ``min_interval_s`` / ``max_interval_s`` 夹住，单次调整不超过
  ``adjust_step_s``，进行中的换向不受影响。
- **依据与审计同源**：每次决策返回完整的 :class:`IntervalDecision`（回读
  质量、温度、前后间隔、原因），平台把它作为一条事件发布，控制台展示与审
  计留存读同一条记录，两处口径一致。
- **重启回落保守间隔**：调节器状态不落盘，重启后从保守间隔起步，等新鲜回
  读再调整——恢复运行时不拿旧的外推间隔冒充新鲜炉温证据。
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from enum import Enum

from ..clock import Clock
from ..config import HeatingLimits, ReversalLimits


class ReadbackQuality(Enum):
    """蓄热室温度回读质量。"""

    GOOD = "good"
    MISSING = "missing"
    STALE = "stale"


class DecisionReason(Enum):
    """间隔决策原因（审计与控制台共用的稳定词表）。"""

    READBACK_MISSING = "readback_missing"  # 部分失败：回读缺失，回落保守间隔
    CONSERVATIVE_MODE = "conservative_mode"  # 炉组策略：固定间隔保守走
    EXTEND_ON_MARGIN = "extend_on_margin"  # 炉温有余量：拉长间隔省煤气
    HOLD_AT_MAX = "hold_at_max"  # 已到间隔上限，保持
    HOLD_IN_BAND = "hold_in_band"  # 炉温在确认带内，保持
    SHORTEN_ON_LOW_TEMP = "shorten_on_low_temp"  # 炉温守不住：加密换向
    HOLD_AT_MIN = "hold_at_min"  # 已到间隔下限，保持


def _is_number(value: object) -> bool:
    """是否为可用的有限数值（布尔与 NaN/inf 都不算）。"""

    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(float(value))


@dataclass(frozen=True)
class IntervalDecision:
    """一次换向间隔决策的完整依据（控制台展示与审计留存共用同一份）。"""

    seq: int
    group: str
    ts: float
    mode: str
    reason: str
    previous_interval_s: float
    interval_s: float
    readback_quality: str
    temperature_c: float | None

    def partial_failure(self) -> bool:
        """本次决策是否属于部分失败（回读缺失回落保守间隔）。"""

        return self.reason == DecisionReason.READBACK_MISSING.value

    def as_dict(self) -> dict:
        return {
            "seq": self.seq,
            "group": self.group,
            "decided_at": round(self.ts, 3),
            "mode": self.mode,
            "reason": self.reason,
            "previous_interval_s": round(self.previous_interval_s, 2),
            "interval_s": round(self.interval_s, 2),
            "readback_quality": self.readback_quality,
            "temperature_c": None if self.temperature_c is None else round(self.temperature_c, 2),
        }


class IntervalGovernor:
    """单炉组的换向间隔调节器。"""

    def __init__(self, group: str, heating: HeatingLimits, reversal: ReversalLimits, clock: Clock) -> None:
        self._group = group
        self._heating = heating
        self._reversal = reversal
        self._clock = clock
        self._mode = reversal.mode_for(group)
        # 起步即保守间隔：没有回读证据时不允许比固定间隔更激进。
        self._current_s = heating.reversal_interval_s
        self._seq = 0
        self._last: IntervalDecision | None = None
        self._last_readback: tuple[float, float] | None = None

    def mode(self) -> str:
        """返回本炉组的间隔模式（economy / conservative）。"""

        return self._mode

    def current_interval_s(self) -> float:
        """返回当前生效的换向间隔。"""

        return self._current_s

    def last_decision(self) -> IntervalDecision | None:
        """返回最近一次决策（尚无决策时返回 None）。"""

        return self._last

    def decide(self, temperature_c: float | None = None, sampled_at: float | None = None,
               now: float | None = None) -> IntervalDecision:
        """根据一次蓄热室温度回读决策新的换向间隔。"""

        moment = self._clock.now() if now is None else now
        quality = self._classify(temperature_c, sampled_at, moment)
        previous = self._current_s
        base = self._heating.reversal_interval_s
        temperature = float(temperature_c) if _is_number(temperature_c) else None
        if quality is not ReadbackQuality.GOOD:
            interval, reason = base, DecisionReason.READBACK_MISSING
        elif self._mode == "conservative":
            interval, reason = base, DecisionReason.CONSERVATIVE_MODE
        else:
            interval, reason = self._adjust(float(temperature_c), previous)
        interval = min(max(interval, self._reversal.min_interval_s), self._reversal.max_interval_s)
        self._current_s = interval
        if temperature is not None and _is_number(sampled_at):
            self._last_readback = (temperature, float(sampled_at))
        self._seq += 1
        self._last = IntervalDecision(
            seq=self._seq,
            group=self._group,
            ts=moment,
            mode=self._mode,
            reason=reason.value,
            previous_interval_s=previous,
            interval_s=interval,
            readback_quality=quality.value,
            temperature_c=temperature,
        )
        return self._last

    def refresh(self, now: float | None = None) -> IntervalDecision | None:
        """按当前时刻重估最近一次回读，结论变化时才返回新决策。

        回读还新鲜时结论不变（返回 None）；一旦超龄——等同缺失，属部分失败——
        且上次决策还不是回读缺失，就重新决策，让间隔及时回落保守间隔。
        """

        if self._last_readback is None:
            return None
        moment = self._clock.now() if now is None else now
        temperature_c, sampled_at = self._last_readback
        if self._classify(temperature_c, sampled_at, moment) is ReadbackQuality.GOOD:
            return None
        if self._last is not None and self._last.reason == DecisionReason.READBACK_MISSING.value:
            return None
        return self.decide(temperature_c, sampled_at, now=moment)

    def _classify(self, temperature_c: object, sampled_at: object, moment: float) -> ReadbackQuality:
        """判定回读质量：缺失（无值/非数值/时间戳在未来）或超龄都不算可用。"""

        if not _is_number(temperature_c) or not _is_number(sampled_at):
            return ReadbackQuality.MISSING
        sampled = float(sampled_at)
        if sampled > moment:
            return ReadbackQuality.MISSING
        if moment - sampled > self._reversal.readback_max_age_s:
            return ReadbackQuality.STALE
        return ReadbackQuality.GOOD

    def _adjust(self, temperature_c: float, previous: float) -> tuple[float, DecisionReason]:
        """经济模式的档位调整：炉温有余量拉长、守不住加密、带内保持。"""

        target = self._heating.temp_target_c
        band = self._heating.temp_band_c
        if temperature_c > target + band:
            interval = min(previous + self._reversal.adjust_step_s, self._reversal.max_interval_s)
            reason = DecisionReason.EXTEND_ON_MARGIN if interval > previous else DecisionReason.HOLD_AT_MAX
            return interval, reason
        if temperature_c < target - band:
            interval = max(previous - self._reversal.adjust_step_s, self._reversal.min_interval_s)
            reason = DecisionReason.SHORTEN_ON_LOW_TEMP if interval < previous else DecisionReason.HOLD_AT_MIN
            return interval, reason
        return previous, DecisionReason.HOLD_IN_BAND

"""换向排程。

按换向间隔判定是否到期；每次成功换向记录时间并累加计数。到期末打标记不会
自己换向，仍由编排在确认安全后调用，符合"申请 → 执行 → 确认"的真实节奏。

间隔来自 :class:`~coke.heating.advisor.ReversalIntervalAdvisor` 的实时决策
（蓄热室温度回读驱动，按炉组单独计算）；未接决策器时退化为配置里的固定基线
间隔。排程器缓存每次 ``due`` 判定实际使用的决策，平台审计时取同一份依据。
"""

from __future__ import annotations

from ..config import HeatingLimits
from ..clock import Clock
from .advisor import IntervalDecision, ReversalIntervalAdvisor


class HeatingReversal:
    """加热换向排程器。"""

    def __init__(
        self,
        limits: HeatingLimits,
        clock: Clock,
        advisor: ReversalIntervalAdvisor | None = None,
    ) -> None:
        self._limits = limits
        self._clock = clock
        self._advisor = advisor
        self._last_ts = clock.now()
        self._count = 0
        self._last_decision: IntervalDecision | None = None

    @property
    def limits(self) -> HeatingLimits:
        return self._limits

    def _interval_s(self, moment: float) -> float:
        """返回当前生效的换向间隔，并缓存本次用到的决策依据。"""

        if self._advisor is None:
            return self._limits.reversal_interval_s
        decision = self._advisor.decide(moment)
        self._last_decision = decision
        return decision.interval_s

    def last_decision(self) -> IntervalDecision | None:
        """返回最近一次 ``due`` 判定实际使用的间隔决策（未接决策器时为 None）。"""

        return self._last_decision

    def due(self, now: float | None = None) -> bool:
        """当前时刻是否已到换向间隔。"""

        moment = self._clock.now() if now is None else now
        return (moment - self._last_ts) >= self._interval_s(moment)

    def mark_reversed(self, now: float | None = None) -> int:
        """记录一次换向，返回累计换向次数。"""

        self._last_ts = self._clock.now() if now is None else now
        self._count += 1
        return self._count

    def count(self) -> int:
        """返回累计换向次数。"""

        return self._count

    def seconds_to_due(self, now: float | None = None) -> float:
        """返回距下次换向还有多少秒（已到期返回 0）。"""

        moment = self._clock.now() if now is None else now
        remaining = self._interval_s(moment) - (moment - self._last_ts)
        return max(0.0, remaining)

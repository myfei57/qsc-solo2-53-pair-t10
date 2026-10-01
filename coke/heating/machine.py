"""换向排程。

换向间隔由调节器按炉组动态给出（未接调节器时退回配置里的固定间隔）；排程
器只判定是否到期并记录换向时刻，间隔怎么算、阀序怎么走都不在这里。每次成
功换向记录时间并累加计数。到期不打标记不会自己换向，仍由编排在确认安全后
调用，符合"申请 → 执行 → 确认"的真实节奏。
"""

from __future__ import annotations

from ..config import HeatingLimits
from ..clock import Clock
from .governor import IntervalGovernor


class HeatingReversal:
    """加热换向排程器。"""

    def __init__(self, limits: HeatingLimits, clock: Clock, governor: IntervalGovernor | None = None) -> None:
        self._limits = limits
        self._clock = clock
        self._governor = governor
        self._last_ts = clock.now()
        self._count = 0

    @property
    def limits(self) -> HeatingLimits:
        return self._limits

    def _interval_s(self) -> float:
        """当前生效的换向间隔（调节器优先，否则配置固定值）。"""

        if self._governor is not None:
            return self._governor.current_interval_s()
        return self._limits.reversal_interval_s

    def due(self, now: float | None = None) -> bool:
        """当前时刻是否已到换向间隔。"""

        moment = self._clock.now() if now is None else now
        return (moment - self._last_ts) >= self._interval_s()

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
        remaining = self._interval_s() - (moment - self._last_ts)
        return max(0.0, remaining)

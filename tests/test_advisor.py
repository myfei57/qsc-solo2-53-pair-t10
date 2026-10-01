"""换向间隔自适应：蓄热室温度回读驱动的间隔决策与审计一致性。"""

from __future__ import annotations

import dataclasses

import pytest

from coke.app import build_platform
from coke.clock import ManualClock
from coke.config import PlatformConfig, config_to_mapping, default_config
from coke.console.api import Api
from coke.errors import ConfigError
from coke.heating.advisor import ReversalIntervalAdvisor
from coke.heating.machine import HeatingReversal


def _advisor(clock: ManualClock, policy: str = "adaptive") -> ReversalIntervalAdvisor:
    config = default_config()
    heating = dataclasses.replace(config.heating, reversal_policy=policy)
    return ReversalIntervalAdvisor(heating, clock)


def test_missing_readback_falls_back_to_conservative_interval():
    clock = ManualClock()
    advisor = _advisor(clock)
    decision = advisor.decide()
    assert decision.interval_s == advisor.limits.reversal_interval_s
    assert decision.reason == "readback_missing"
    assert decision.partial_failure is True


def test_explicit_miss_invalidates_readback_immediately():
    clock = ManualClock()
    advisor = _advisor(clock)
    advisor.feed(advisor.limits.regen_target_c)
    assert advisor.decide().reason == "temp_comfortable"
    advisor.feed(None)
    decision = advisor.decide()
    assert decision.reason == "readback_missing"
    assert decision.interval_s == advisor.limits.reversal_interval_s
    assert advisor.missed() == 1


def test_stale_readback_is_treated_as_missing():
    clock = ManualClock()
    advisor = _advisor(clock)
    advisor.feed(advisor.limits.regen_target_c)
    clock.advance(advisor.limits.readback_timeout_s + 1.0)
    decision = advisor.decide()
    assert decision.reason == "readback_missing"
    assert decision.partial_failure is True
    assert decision.readback_age_s is not None


def test_adaptive_tiers_follow_regenerator_offset():
    clock = ManualClock()
    advisor = _advisor(clock)
    limits = advisor.limits
    advisor.feed(limits.regen_target_c)
    comfortable = advisor.decide()
    assert comfortable.interval_s == limits.reversal_interval_max_s
    assert comfortable.reason == "temp_comfortable"

    advisor.feed(limits.regen_target_c + limits.regen_band_c * 0.8)
    in_band = advisor.decide()
    assert in_band.interval_s == limits.reversal_interval_s
    assert in_band.reason == "temp_in_band"

    advisor.feed(limits.regen_target_c + limits.regen_band_c * 1.2)
    out_of_band = advisor.decide()
    assert out_of_band.interval_s == limits.reversal_interval_min_s
    assert out_of_band.reason == "temp_out_of_band"


def test_adaptive_tier_boundaries_are_inclusive():
    clock = ManualClock()
    advisor = _advisor(clock)
    limits = advisor.limits
    advisor.feed(limits.regen_target_c + limits.regen_band_c * limits.temp_good_ratio)
    assert advisor.decide().reason == "temp_comfortable"
    advisor.feed(limits.regen_target_c + limits.regen_band_c)
    assert advisor.decide().reason == "temp_in_band"


def test_fixed_policy_keeps_conservative_interval_regardless_of_readback():
    clock = ManualClock()
    advisor = _advisor(clock, policy="fixed")
    limits = advisor.limits
    advisor.feed(limits.regen_target_c)
    decision = advisor.decide()
    assert decision.interval_s == limits.reversal_interval_s
    assert decision.reason == "policy_fixed"
    advisor.feed(limits.regen_target_c + limits.regen_band_c * 2.0)
    assert advisor.decide().interval_s == limits.reversal_interval_s
    advisor.feed(None)
    missing = advisor.decide()
    assert missing.reason == "readback_missing"
    assert missing.partial_failure is True


def test_scheduler_due_respects_stretched_interval():
    clock = ManualClock()
    config = default_config()
    advisor = ReversalIntervalAdvisor(config.heating, clock)
    reversal = HeatingReversal(config.heating, clock, advisor=advisor)
    advisor.feed(config.heating.regen_target_c)
    clock.advance(config.heating.reversal_interval_s + 1.0)
    advisor.feed(config.heating.regen_target_c)  # 现场回读持续刷新，间隔保持拉长
    assert reversal.due() is False
    clock.advance(config.heating.reversal_interval_max_s - config.heating.reversal_interval_s)
    advisor.feed(config.heating.regen_target_c)
    assert reversal.due() is True
    assert reversal.last_decision() is not None
    assert reversal.last_decision().reason == "temp_comfortable"


def test_scheduler_due_respects_shortened_interval():
    clock = ManualClock()
    config = default_config()
    advisor = ReversalIntervalAdvisor(config.heating, clock)
    reversal = HeatingReversal(config.heating, clock, advisor=advisor)
    advisor.feed(config.heating.regen_target_c + config.heating.regen_band_c * 2.0)
    clock.advance(config.heating.reversal_interval_min_s)
    advisor.feed(config.heating.regen_target_c + config.heating.regen_band_c * 2.0)
    assert reversal.due() is True
    assert reversal.seconds_to_due() == 0.0


def test_scheduler_without_advisor_keeps_fixed_interval():
    clock = ManualClock()
    config = default_config()
    reversal = HeatingReversal(config.heating, clock)
    clock.advance(config.heating.reversal_interval_s - 1.0)
    assert reversal.due() is False
    assert reversal.last_decision() is None
    clock.advance(1.0)
    assert reversal.due() is True


def test_platform_feed_and_snapshot_expose_decision(platform, clock):
    platform.start()
    decision = platform.feed_regenerator_readback(platform.config.heating.regen_target_c)
    assert decision["reason"] == "temp_comfortable"
    snapshot = platform.heating_snapshot().as_dict()
    assert snapshot["interval"]["interval_s"] == platform.config.heating.reversal_interval_max_s
    assert snapshot["readbacks_missed"] == 0
    status_heating = platform.status()["heating"]
    assert status_heating["interval"]["reason"] == "temp_comfortable"


def test_platform_missing_readback_is_partial_failure(platform):
    platform.start()
    platform.feed_regenerator_readback(platform.config.heating.regen_target_c)
    decision = platform.feed_regenerator_readback(None)
    assert decision["partial_failure"] is True
    assert decision["interval_s"] == platform.config.heating.reversal_interval_s
    warnings = [entry for entry in platform.audit.alarms() if entry["action"] == "regenerator_readback_missed"]
    assert warnings
    assert platform.heating_snapshot().readbacks_missed == 1


def test_reversal_interval_basis_matches_audit_record(platform, clock):
    platform.start()
    platform.feed_regenerator_readback(platform.config.heating.regen_target_c)
    clock.advance(platform.config.heating.reversal_interval_max_s)
    platform.feed_regenerator_readback(platform.config.heating.regen_target_c)  # 回读刷新，间隔保持拉长
    report = platform.reverse(oven="oven01")
    assert report["ok"] is True
    assert report["interval"]["reason"] == "temp_comfortable"
    basis = [
        entry
        for entry in platform.audit.entries(limit=50)
        if entry["action"] == "reversal_interval_basis"
    ]
    assert basis
    recorded = {key: basis[0]["payload"][key] for key in report["interval"]}
    assert recorded == report["interval"]


def test_reversal_under_missing_readback_audits_conservative_basis(platform, clock):
    platform.start()
    clock.advance(platform.config.heating.reversal_interval_s)
    report = platform.reverse(oven="oven01")
    assert report["ok"] is True
    assert report["interval"]["reason"] == "readback_missing"
    basis = [
        entry
        for entry in platform.audit.entries(limit=50)
        if entry["action"] == "reversal_interval_basis"
    ]
    assert basis[0]["severity"] == "warning"
    assert basis[0]["payload"]["interval_s"] == platform.config.heating.reversal_interval_s


def test_reversal_history_serves_console(platform, clock):
    platform.start()
    clock.advance(platform.config.heating.reversal_interval_s)
    platform.reverse(oven="oven01")
    history = platform.reversal_history()
    assert history
    assert any(entry["action"] == "reverse_complete" for entry in history)
    assert any(entry["action"] == "reversal_interval_basis" for entry in history)
    with pytest.raises(ValueError):
        platform.reversal_history(limit=0)


def test_cycle_records_interval_trend(platform):
    platform.start()
    platform.feed_regenerator_readback(platform.config.heating.regen_target_c)
    platform.cycle(dt=1.0)
    series = platform.trend.series("reversal_interval")
    assert series
    assert series[-1].value == platform.config.heating.reversal_interval_max_s


def test_console_exposes_heating_and_reversal_records(platform, clock):
    api = Api(platform)
    platform.start()
    status, payload = api.handle("POST", "/api/heating/readback", {}, {"temperature_c": 1240.0})
    assert status == 200
    assert payload["reason"] == "temp_comfortable"
    status, heating = api.handle("GET", "/api/heating", {}, {})
    assert status == 200
    assert heating["interval"]["interval_s"] == platform.config.heating.reversal_interval_max_s

    clock.advance(platform.config.heating.reversal_interval_max_s)
    status, _reversal = api.handle("POST", "/api/reverse", {}, {"oven": "oven01"})
    assert status == 200
    status, reversals = api.handle("GET", "/api/reversals", {}, {})
    assert status == 200
    assert any(record["action"] == "reverse_complete" for record in reversals["records"])


def test_console_readback_missing_is_partial_failure(platform):
    api = Api(platform)
    platform.start()
    status, payload = api.handle("POST", "/api/heating/readback", {}, {"temperature_c": None})
    assert status == 200
    assert payload["partial_failure"] is True
    assert payload["interval_s"] == platform.config.heating.reversal_interval_s


def test_config_rejects_invalid_reversal_policy():
    payload = config_to_mapping(default_config())
    payload["limits"]["heating"]["reversal_policy"] = "sometimes"
    with pytest.raises(ConfigError):
        PlatformConfig.from_mapping(payload)


def test_config_rejects_interval_bounds_outside_baseline():
    payload = config_to_mapping(default_config())
    payload["limits"]["heating"]["reversal_interval_min_s"] = 2000.0
    with pytest.raises(ConfigError):
        PlatformConfig.from_mapping(payload)
    payload = config_to_mapping(default_config())
    payload["limits"]["heating"]["reversal_interval_max_s"] = 1000.0
    with pytest.raises(ConfigError):
        PlatformConfig.from_mapping(payload)


def test_config_rejects_good_ratio_out_of_range():
    payload = config_to_mapping(default_config())
    payload["limits"]["heating"]["temp_good_ratio"] = 1.5
    with pytest.raises(ConfigError):
        PlatformConfig.from_mapping(payload)


def test_config_rejects_waste_lead_above_min_interval():
    payload = config_to_mapping(default_config())
    payload["limits"]["heating"]["waste_close_lead_s"] = 950.0
    with pytest.raises(ConfigError):
        PlatformConfig.from_mapping(payload)


def test_default_config_interval_bounds_are_ordered():
    heating = default_config().heating
    assert heating.reversal_interval_min_s < heating.reversal_interval_s < heating.reversal_interval_max_s
    assert heating.reversal_policy in ("adaptive", "fixed")

"""换向间隔自动调节：按炉组生效、回读缺失回落、依据与审计同源。"""

from __future__ import annotations

import dataclasses
import json

import pytest

from coke.app import build_platform
from coke.clock import ManualClock
from coke.config import PlatformConfig, config_to_mapping, default_config, load_config
from coke.console.api import Api
from coke.errors import ConfigError
from coke.heating.governor import IntervalGovernor


def _governor(mode: str, clock: ManualClock) -> IntervalGovernor:
    config = default_config()
    limits = dataclasses.replace(config.reversal, default_mode=mode, groups={})
    return IntervalGovernor("battery1", config.heating, limits, clock)


def _hot_c() -> float:
    heating = default_config().heating
    return heating.temp_target_c + heating.temp_band_c + 10.0


def _cold_c() -> float:
    heating = default_config().heating
    return heating.temp_target_c - heating.temp_band_c - 10.0


def _fresh_platform(tmp_path, clock):
    """回读保鲜期拉长的平台，便于跨整个换向间隔观察调节效果。"""

    config = dataclasses.replace(
        default_config(),
        reversal=dataclasses.replace(default_config().reversal, readback_max_age_s=7200.0),
    )
    return build_platform(config, clock=clock, storage_root=str(tmp_path / "state"))


def test_economy_extends_stepwise_and_caps_at_max():
    clock = ManualClock()
    governor = _governor("economy", clock)
    config = default_config()
    first = governor.decide(_hot_c(), clock.now())
    assert first.reason == "extend_on_margin"
    assert first.previous_interval_s == config.heating.reversal_interval_s
    assert first.interval_s == config.heating.reversal_interval_s + config.reversal.adjust_step_s
    second = governor.decide(_hot_c(), clock.now())
    assert second.interval_s == first.interval_s + config.reversal.adjust_step_s
    while governor.current_interval_s() < config.reversal.max_interval_s:
        governor.decide(_hot_c(), clock.now())
    assert governor.current_interval_s() == config.reversal.max_interval_s
    assert governor.decide(_hot_c(), clock.now()).reason == "hold_at_max"


def test_economy_shortens_on_low_temp_and_floors_at_min():
    clock = ManualClock()
    governor = _governor("economy", clock)
    config = default_config()
    first = governor.decide(_cold_c(), clock.now())
    assert first.reason == "shorten_on_low_temp"
    assert first.interval_s == config.heating.reversal_interval_s - config.reversal.adjust_step_s
    while governor.current_interval_s() > config.reversal.min_interval_s:
        governor.decide(_cold_c(), clock.now())
    assert governor.current_interval_s() == config.reversal.min_interval_s
    assert governor.decide(_cold_c(), clock.now()).reason == "hold_at_min"
    assert governor.current_interval_s() >= config.reversal.min_interval_s


def test_economy_holds_inside_band():
    clock = ManualClock()
    governor = _governor("economy", clock)
    config = default_config()
    inside = governor.decide(config.heating.temp_target_c + 10.0, clock.now())
    assert inside.reason == "hold_in_band"
    assert inside.interval_s == config.heating.reversal_interval_s
    edge = governor.decide(config.heating.temp_target_c + config.heating.temp_band_c, clock.now())
    assert edge.reason == "hold_in_band"


def test_conservative_mode_keeps_fixed_interval():
    clock = ManualClock()
    governor = _governor("conservative", clock)
    config = default_config()
    decision = governor.decide(_hot_c(), clock.now())
    assert decision.reason == "conservative_mode"
    assert decision.interval_s == config.heating.reversal_interval_s


def test_missing_readback_is_partial_failure_and_falls_back():
    clock = ManualClock()
    governor = _governor("economy", clock)
    config = default_config()
    governor.decide(_hot_c(), clock.now())
    assert governor.current_interval_s() > config.heating.reversal_interval_s
    fallback = governor.decide()
    assert fallback.reason == "readback_missing"
    assert fallback.readback_quality == "missing"
    assert fallback.partial_failure()
    assert governor.current_interval_s() == config.heating.reversal_interval_s


def test_stale_readback_falls_back():
    clock = ManualClock()
    governor = _governor("economy", clock)
    config = default_config()
    governor.decide(_hot_c(), clock.now())
    sampled = clock.now()
    clock.advance(config.reversal.readback_max_age_s + 1.0)
    stale = governor.decide(_hot_c(), sampled)
    assert stale.reason == "readback_missing"
    assert stale.readback_quality == "stale"
    assert stale.temperature_c == _hot_c()
    assert governor.current_interval_s() == config.heating.reversal_interval_s


def test_non_finite_and_future_readbacks_are_missing():
    clock = ManualClock()
    governor = _governor("economy", clock)
    config = default_config()
    assert governor.decide(float("nan"), clock.now()).readback_quality == "missing"
    assert governor.decide(float("inf"), clock.now()).readback_quality == "missing"
    assert governor.decide(1230.0, None).readback_quality == "missing"
    assert governor.decide(1230.0, clock.now() + 10.0).readback_quality == "missing"
    assert governor.decide("1230", clock.now()).readback_quality == "missing"
    assert governor.current_interval_s() == config.heating.reversal_interval_s


def test_refresh_reclassifies_aged_readback_once():
    clock = ManualClock()
    governor = _governor("economy", clock)
    config = default_config()
    governor.decide(_hot_c(), clock.now())
    assert governor.refresh() is None
    clock.advance(config.reversal.readback_max_age_s + 1.0)
    fallback = governor.refresh()
    assert fallback is not None
    assert fallback.reason == "readback_missing"
    assert governor.current_interval_s() == config.heating.reversal_interval_s
    assert governor.refresh() is None


def test_decision_seq_and_payload_are_complete():
    clock = ManualClock()
    governor = _governor("economy", clock)
    first = governor.decide(_hot_c(), clock.now())
    second = governor.decide()
    assert (first.seq, second.seq) == (1, 2)
    payload = first.as_dict()
    for key in (
        "seq",
        "group",
        "decided_at",
        "mode",
        "reason",
        "previous_interval_s",
        "interval_s",
        "readback_quality",
        "temperature_c",
    ):
        assert key in payload
    assert payload["group"] == "battery1"
    assert governor.last_decision() == second


def test_interval_governance_is_per_group(tmp_path, clock):
    config = default_config()
    economy = build_platform(config, clock=clock, storage_root=str(tmp_path / "eco"), unit="battery1")
    conservative = build_platform(config, clock=clock, storage_root=str(tmp_path / "con"), unit="battery2")
    assert economy.governor.mode() == "economy"
    assert conservative.governor.mode() == "conservative"
    extended = economy.feed_regenerator_temperature(_hot_c(), clock.now())
    held = conservative.feed_regenerator_temperature(_hot_c(), clock.now())
    assert extended["interval_s"] > config.heating.reversal_interval_s
    assert held["interval_s"] == config.heating.reversal_interval_s


def test_unlisted_group_uses_default_mode(tmp_path, clock):
    config = default_config()
    platform = build_platform(config, clock=clock, storage_root=str(tmp_path / "state"), unit="battery99")
    assert platform.governor.mode() == "conservative"
    decision = platform.feed_regenerator_temperature(_hot_c(), clock.now())
    assert decision["interval_s"] == config.heating.reversal_interval_s


def test_extended_interval_defers_reversal(tmp_path, clock):
    platform = _fresh_platform(tmp_path, clock)
    config = platform.config
    decision = platform.feed_regenerator_temperature(_hot_c(), clock.now())
    assert decision["interval_s"] == config.heating.reversal_interval_s + config.reversal.adjust_step_s
    clock.advance(config.heating.reversal_interval_s)
    # 间隔已拉长，原固定间隔时刻不再到期：炉况好时就省下这次换向。
    rejected = platform.reverse(oven="oven01")
    assert rejected["ok"] is False
    assert "reversal_not_due" in rejected["violations"]
    clock.advance(config.reversal.adjust_step_s)
    accepted = platform.reverse(oven="oven01", temperature_c=config.heating.temp_target_c)
    assert accepted["ok"] is True
    assert accepted["interval_s"] == decision["interval_s"]
    assert accepted["interval_reason"] == "extend_on_margin"
    completed = [
        entry
        for entry in platform.audit.entries(limit=20)
        if entry["action"] == "reverse_complete" and entry["payload"]["ok"]
    ]
    assert completed[0]["payload"]["interval_s"] == decision["interval_s"]
    assert completed[0]["payload"]["interval_reason"] == "extend_on_margin"
    assert completed[0]["payload"]["interval_decision_seq"] == decision["seq"]


def test_missing_readback_is_partial_failure_in_audit(platform):
    decision = platform.feed_regenerator_temperature()
    assert decision["reason"] == "readback_missing"
    assert decision["interval_s"] == platform.config.heating.reversal_interval_s
    assert platform.governor.last_decision().partial_failure()
    assert platform.governor.current_interval_s() == platform.config.heating.reversal_interval_s
    alarms = platform.audit.alarms()
    assert any(item["action"] == "interval_decision" and item["severity"] == "warning" for item in alarms)


def test_cycle_refreshes_stale_readback(platform):
    platform.feed_regenerator_temperature(_hot_c(), platform.clock.now())
    assert platform.governor.current_interval_s() > platform.config.heating.reversal_interval_s
    platform.clock.advance(platform.config.reversal.readback_max_age_s + 1.0)
    platform.cycle(dt=1.0)
    assert platform.governor.current_interval_s() == platform.config.heating.reversal_interval_s
    decisions = [entry for entry in platform.audit.entries(limit=20) if entry["action"] == "interval_decision"]
    assert decisions[0]["severity"] == "warning"
    assert decisions[0]["payload"]["reason"] == "readback_missing"


def test_reverse_falls_back_when_readback_stale(platform):
    platform.feed_regenerator_temperature(_hot_c(), platform.clock.now())
    assert platform.governor.current_interval_s() > platform.config.heating.reversal_interval_s
    platform.clock.advance(platform.config.heating.reversal_interval_s)
    report = platform.reverse(oven="oven01", temperature_c=platform.config.heating.temp_target_c)
    assert report["ok"] is True
    assert report["interval_reason"] == "readback_missing"
    assert report["interval_s"] == platform.config.heating.reversal_interval_s


def test_console_reversals_match_audit(tmp_path, clock):
    platform = _fresh_platform(tmp_path, clock)
    api = Api(platform)
    config = platform.config
    platform.feed_regenerator_temperature(_hot_c(), clock.now())
    clock.advance(config.heating.reversal_interval_s + config.reversal.adjust_step_s)
    platform.reverse(oven="oven01", temperature_c=config.heating.temp_target_c)
    status, payload = api.handle("GET", "/api/reversals", {}, {})
    assert status == 200
    assert payload["group"] == "battery1"
    assert payload["mode"] == "economy"
    assert payload["decisions"] and payload["reversals"]
    audit_decisions = [
        entry for entry in platform.audit.entries(limit=1024) if entry["action"] == "interval_decision"
    ]
    assert payload["decisions"] == audit_decisions[: len(payload["decisions"])]
    reversal = payload["reversals"][0]
    assert reversal["payload"]["interval_reason"] == payload["last_decision"]["reason"]
    assert reversal["payload"]["interval_decision_seq"] == payload["last_decision"]["seq"]


def test_console_heating_readback_endpoint(platform):
    api = Api(platform)
    status, payload = api.handle("POST", "/api/heating/readback", {}, {"temperature_c": 1230.0})
    assert status == 200
    assert payload["readback_quality"] == "good"
    assert payload["interval_s"] > platform.config.heating.reversal_interval_s
    status, missing = api.handle("POST", "/api/heating/readback", {}, {})
    assert status == 200
    assert missing["reason"] == "readback_missing"
    assert missing["interval_s"] == platform.config.heating.reversal_interval_s
    status, bad = api.handle("POST", "/api/heating/readback", {}, {"temperature_c": "hot"})
    assert status == 409


def test_config_round_trip_includes_reversal(tmp_path):
    payload = config_to_mapping(default_config())
    path = tmp_path / "coke.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    loaded = load_config(str(path))
    assert loaded.reversal == default_config().reversal


def test_config_rejects_bad_reversal_mode():
    payload = config_to_mapping(default_config())
    payload["limits"]["reversal"]["default_mode"] = "random"
    with pytest.raises(ConfigError):
        PlatformConfig.from_mapping(payload)


def test_config_rejects_inverted_reversal_bounds():
    payload = config_to_mapping(default_config())
    payload["limits"]["reversal"]["min_interval_s"] = 3000.0
    with pytest.raises(ConfigError):
        PlatformConfig.from_mapping(payload)


def test_config_rejects_base_interval_outside_bounds():
    payload = config_to_mapping(default_config())
    payload["limits"]["heating"]["reversal_interval_s"] = 5000.0
    with pytest.raises(ConfigError):
        PlatformConfig.from_mapping(payload)


def test_config_rejects_bad_group_mode():
    payload = config_to_mapping(default_config())
    payload["limits"]["reversal"]["groups"] = {"battery1": "random"}
    with pytest.raises(ConfigError):
        PlatformConfig.from_mapping(payload)

"""Warning light, alarm voice, person sensor and detection zones.

Values come from XM232 dual-lens cameras (AlarmOutUsedAsLed, VoiceTipType list,
7x7 motion grid, tracking sensitivity 5, motion level 8).
"""

from __future__ import annotations

from datetime import timedelta

import pytest
from pytest_homeassistant_custom_component.common import async_fire_time_changed

from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ServiceValidationError
from homeassistant.util import dt as dt_util

from custom_components.icsee_ptz.const import DOMAIN

MOTION = "binary_sensor.garten_motion_alarm"


async def test_warning_light_on_off(
    hass: HomeAssistant, setup_integration, mock_device
) -> None:
    entity = "siren.garten_warning_light"
    assert hass.states.get(entity).state == "off"  # AUTO
    await hass.services.async_call(
        "siren", "turn_on", {"entity_id": entity}, blocking=True
    )
    name, value = mock_device.writes[-1]
    # the whole list is written, like the camera expects
    assert name == "Alarm.AlarmOut"
    assert value == [{"AlarmOutStatus": "OPEN", "AlarmOutType": "MANUAL"}]
    assert hass.states.get(entity).state == "on"
    await hass.services.async_call(
        "siren", "turn_off", {"entity_id": entity}, blocking=True
    )
    # off hands the light back to the alarm events instead of disabling it
    assert mock_device.writes[-1][1][0]["AlarmOutType"] == "AUTO"
    assert hass.states.get(entity).state == "off"


async def test_warning_light_duration(
    hass: HomeAssistant, setup_integration, mock_device
) -> None:
    entity = "siren.garten_warning_light"
    await hass.services.async_call(
        "siren", "turn_on", {"entity_id": entity, "duration": 5}, blocking=True
    )
    assert hass.states.get(entity).state == "on"
    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=6))
    await hass.async_block_till_done()
    assert mock_device.writes[-1][1][0]["AlarmOutType"] == "AUTO"
    assert hass.states.get(entity).state == "off"


async def test_alarm_output_select(
    hass: HomeAssistant, setup_integration, mock_device
) -> None:
    entity = "select.garten_alarm_output"
    state = hass.states.get(entity)
    assert state.state == "auto"
    assert state.attributes["options"] == ["auto", "on", "off"]
    await hass.services.async_call(
        "select", "select_option", {"entity_id": entity, "option": "off"}, blocking=True
    )
    assert mock_device.writes[-1][1][0]["AlarmOutType"] == "CLOSE"


async def test_alarm_voice_entities(
    hass: HomeAssistant, setup_integration, mock_device
) -> None:
    assert hass.states.get("switch.garten_alarm_voice").state == "off"
    await hass.services.async_call(
        "switch", "turn_on", {"entity_id": "switch.garten_alarm_voice"}, blocking=True
    )
    name, value = mock_device.writes[-1]
    assert name == "Detect.MotionDetect.[0]"
    assert value["EventHandler"]["VoiceEnable"] is True

    voice = hass.states.get("select.garten_alarm_voice_sound")
    assert voice.state == "alarm"
    assert voice.attributes["options"] == ["alarm", "tracking_area", "custom"]
    await hass.services.async_call(
        "select",
        "select_option",
        {"entity_id": "select.garten_alarm_voice_sound", "option": "tracking_area"},
        blocking=True,
    )
    assert mock_device.writes[-1][1]["EventHandler"]["VoiceType"] == 523

    interval = hass.states.get("number.garten_alarm_voice_interval")
    assert float(interval.state) == 0


async def test_human_alarm_only_for_people(
    hass: HomeAssistant, setup_integration, mock_device
) -> None:
    entity = "binary_sensor.garten_person_detected"
    assert hass.states.get(entity).state == "off"
    mock_device.fire_alarm({"Channel": 0, "Status": "Start", "Event": "VideoMotion"})
    await hass.async_block_till_done()
    assert hass.states.get(entity).state == "off"
    mock_device.fire_alarm(
        {"Channel": 0, "Status": "Start", "Event": "appEventHumanDetectAlarm"}
    )
    await hass.async_block_till_done()
    state = hass.states.get(entity)
    assert state.state == "on"
    assert state.attributes["last_event"] == "appEventHumanDetectAlarm"
    assert len(state.attributes["motion_zone"]) == 7
    assert state.attributes["human_zone"]["x_min"] == pytest.approx(1.2)
    mock_device.fire_alarm(
        {"Channel": 0, "Status": "Stop", "Event": "appEventHumanDetectAlarm"}
    )
    await hass.async_block_till_done()
    assert hass.states.get(entity).state == "off"


async def test_set_human_zone(
    hass: HomeAssistant, setup_integration, mock_device
) -> None:
    await hass.services.async_call(
        DOMAIN,
        "set_human_zone",
        {"entity_id": MOTION, "x_min": 25, "y_min": 10, "x_max": 75, "y_max": 100},
        blocking=True,
    )
    name, value = mock_device.writes[-1]
    assert name == "Detect.HumanDetection.[0]"
    rule = value["PedRule"][0]
    assert rule["RuleType"] == 1 and rule["Enable"] is True
    assert rule["RuleRegion"]["Pts"] == [
        {"X": 2048, "Y": 819},
        {"X": 6143, "Y": 819},
        {"X": 6143, "Y": 8191},
        {"X": 2048, "Y": 8191},
    ]
    assert value["PedRule"][1]["Enable"] is False  # other rules untouched
    with pytest.raises(ServiceValidationError):
        await hass.services.async_call(
            DOMAIN,
            "set_human_zone",
            {"entity_id": MOTION, "x_min": 80, "y_min": 0, "x_max": 20, "y_max": 50},
            blocking=True,
        )


async def test_set_motion_zone(
    hass: HomeAssistant, setup_integration, mock_device
) -> None:
    rows = ["1110000"] * 7
    await hass.services.async_call(
        DOMAIN, "set_motion_zone", {"entity_id": MOTION, "rows": rows}, blocking=True
    )
    name, value = mock_device.writes[-1]
    assert name == "Detect.MotionDetect.[0]"
    assert value["Region"][:7] == ["0x00000007"] * 7
    state = hass.states.get("binary_sensor.garten_person_detected")
    assert state.attributes["motion_zone"] == rows
    writes = len(mock_device.writes)
    for bad in (["111"] * 7, ["1111111"] * 6, ["0000000"] * 7):
        with pytest.raises(ServiceValidationError):
            await hass.services.async_call(
                DOMAIN,
                "set_motion_zone",
                {"entity_id": MOTION, "rows": bad},
                blocking=True,
            )
    assert len(mock_device.writes) == writes  # nothing written


async def test_sensitivity_above_app_range(
    hass: HomeAssistant, mock_device, config_entry
) -> None:
    # XM232: the app stored motion level 7 and tracking sensitivity 5
    mock_device.configs["Detect.MotionDetect"][0]["Level"] = 8
    mock_device.configs["Detect.DetectTrack"]["Sensitivity"] = 5
    config_entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(config_entry.entry_id)
    await hass.async_block_till_done()

    motion = hass.states.get("number.garten_motion_sensitivity")
    assert float(motion.state) == 8 and motion.attributes["max"] == 8
    tracking = hass.states.get("number.garten_motion_tracking_sensitivity")
    assert float(tracking.state) == 5 and tracking.attributes["max"] == 5
    # the low/medium/high select would show "unknown" for 5, so it is not created
    assert hass.states.get("select.garten_motion_tracking_sensitivity") is None

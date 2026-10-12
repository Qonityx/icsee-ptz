"""Sirens: the manual alarm siren and the warning light (alarm output)."""

from __future__ import annotations

from datetime import timedelta
from typing import Any

from homeassistant.components.siren import (
    ATTR_DURATION,
    SirenEntity,
    SirenEntityFeature,
)
from homeassistant.core import CALLBACK_TYPE, HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.event import async_call_later

from .binary_sensor import _run
from .coordinator import ICSeeConfigEntry, ICSeeCoordinator
from .entity import ICSeeEntity

PARALLEL_UPDATES = 1

ABILITY = "SystemFunction.AlarmFunction.OneKeyManulAlarm"
# The alarm output drives a light instead of a relay (e.g. the red/blue "siren
# light" on top of XM232 dual-lens cameras). MANUAL turns it on, AUTO hands it
# back to the alarm events (see the Motion warning light switch).
LED_ABILITY = "SystemFunction.OtherFunction.AlarmOutUsedAsLed"
ALARM_OUT = "Alarm.AlarmOut"


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ICSeeConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    coordinator = entry.runtime_data.coordinator
    entities: list[SirenEntity] = []
    if coordinator.ability(ABILITY):
        entities.append(ICSeeSiren(coordinator))
    out = coordinator.channel_value(ALARM_OUT, 0)
    if (
        coordinator.ability(LED_ABILITY)
        and isinstance(out, dict)
        and "AlarmOutType" in out
    ):
        entities.append(ICSeeWarningLight(coordinator))
    async_add_entities(entities)


class ICSeeSiren(ICSeeEntity, SirenEntity):
    _attr_translation_key = "siren"
    _attr_supported_features = SirenEntityFeature.TURN_ON | SirenEntityFeature.TURN_OFF
    _attr_assumed_state = True  # the camera does not report the siren state

    def __init__(self, coordinator: ICSeeCoordinator) -> None:
        super().__init__(coordinator, "siren")
        self._attr_is_on = False

    @property
    def available(self) -> bool:
        return self.device.is_connected

    async def async_turn_on(self, **kwargs: Any) -> None:
        await _run(self.device.async_remote_ctrl("ManuIntelAlarm", "0x00000001"))
        self._attr_is_on = True
        self.async_write_ha_state()

    async def async_turn_off(self, **kwargs: Any) -> None:
        await _run(self.device.async_remote_ctrl("ManuIntelAlarm", "0x00000000"))
        self._attr_is_on = False
        self.async_write_ha_state()


class ICSeeWarningLight(ICSeeEntity, SirenEntity):
    """The alarm output light, switched by hand (optionally for some seconds)."""

    _attr_translation_key = "warning_light"
    _attr_supported_features = (
        SirenEntityFeature.TURN_ON
        | SirenEntityFeature.TURN_OFF
        | SirenEntityFeature.DURATION
    )

    def __init__(self, coordinator: ICSeeCoordinator) -> None:
        super().__init__(coordinator, "warning_light")
        self._cancel_off: CALLBACK_TYPE | None = None

    @property
    def is_on(self) -> bool | None:
        out = self.coordinator.channel_value(ALARM_OUT, 0)
        if not isinstance(out, dict) or out.get("AlarmOutType") is None:
            return None
        return out["AlarmOutType"] == "MANUAL"

    @property
    def available(self) -> bool:
        return super().available and self.is_on is not None

    async def _write(self, mode: str) -> None:
        def mutate(out: dict[str, Any]) -> None:
            out["AlarmOutType"] = mode

        await self.coordinator.async_update_config(ALARM_OUT, 0, mutate)

    def _cancel_timer(self) -> None:
        if self._cancel_off:
            self._cancel_off()
            self._cancel_off = None

    async def async_turn_on(self, **kwargs: Any) -> None:
        self._cancel_timer()
        await self._write("MANUAL")
        if duration := kwargs.get(ATTR_DURATION):

            @callback
            def _off(_now: Any) -> None:
                self._cancel_off = None
                self.hass.async_create_task(self._write("AUTO"))

            self._cancel_off = async_call_later(
                self.hass, timedelta(seconds=float(duration)), _off
            )

    async def async_turn_off(self, **kwargs: Any) -> None:
        # back to AUTO (alarm events), not CLOSE: turning the light off by hand
        # must not disable the "warning light on motion" automation
        self._cancel_timer()
        await self._write("AUTO")

    async def async_will_remove_from_hass(self) -> None:
        self._cancel_timer()
        await super().async_will_remove_from_hass()

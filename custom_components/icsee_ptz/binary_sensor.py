"""Motion alarm binary sensor. It is also the target of the PTZ actions."""

from __future__ import annotations

from typing import Any

from collections.abc import Callable
from dataclasses import dataclass
import time

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
    BinarySensorEntityDescription,
)
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .const import CONF_CHANNEL, CONF_CHANNEL_COUNT, CONF_PRESET, CONF_STEP, DOMAIN
from .coordinator import ICSeeConfigEntry, ICSeeCoordinator
from .device import CannotConnect, CommandFailed
from .entity import (
    ICSeeConfigEntity,
    ICSeeConfigEntityDescription,
    ICSeeEntity,
    config_entities,
)

PARALLEL_UPDATES = 0
PUSH_GRACE = 60  # seconds a pushed alarm wins over the polled state


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ICSeeConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    coordinator = entry.runtime_data.coordinator
    channels = range(entry.data.get(CONF_CHANNEL_COUNT, 1))
    async_add_entities(MotionAlarm(coordinator, channel) for channel in channels)
    if coordinator.ability("SystemFunction.AlarmFunction.HumanDection"):
        async_add_entities(HumanAlarm(coordinator, channel) for channel in channels)
    async_add_entities(
        config_entities(coordinator, CONFIG_SENSORS, ICSeeConfigBinarySensor)
    )


@dataclass(frozen=True, kw_only=True)
class ICSeeBinarySensorEntityDescription(
    ICSeeConfigEntityDescription, BinarySensorEntityDescription
):
    is_on_fn: Callable[[Any], bool]


def _any_set(*keys: str) -> Callable[[Any], bool]:
    def is_on(value: Any) -> bool:
        return any(bool(value.get(k)) for k in keys)

    return is_on


CONFIG_SENSORS: tuple[ICSeeBinarySensorEntityDescription, ...] = (
    ICSeeBinarySensorEntityDescription(
        # Security questions let anyone who knows the answers reset the password
        key="password_reset_questions",
        translation_key="password_reset_questions",
        config="General.PwdSafety",
        path=("PwdReset",),
        is_on_fn=lambda answers: any(a.get("QuestionAnswer") for a in answers),
        entity_category=EntityCategory.DIAGNOSTIC,
    ),
    ICSeeBinarySensorEntityDescription(
        key="telnet_open",
        translation_key="telnet_open",
        config="Ability.TelnetDebugMode",
        path=("Open",),
        is_on_fn=bool,
        entity_category=EntityCategory.DIAGNOSTIC,
    ),
    ICSeeBinarySensorEntityDescription(
        key="app_bound",
        translation_key="app_bound",
        config="General.AppBindFlag",
        path=("BeBinded",),
        is_on_fn=bool,
        entity_category=EntityCategory.DIAGNOSTIC,
    ),
    ICSeeBinarySensorEntityDescription(
        key="password_reset_contact",
        translation_key="password_reset_contact",
        config="General.PwdSafety",
        path=(),
        is_on_fn=_any_set("SecurityEmail", "SecurityPhone"),
        entity_category=EntityCategory.DIAGNOSTIC,
    ),
    ICSeeBinarySensorEntityDescription(
        # UPnP has opened ports on the router
        key="upnp_ports_open",
        translation_key="upnp_ports_open",
        config="NetWork.Upnp",
        path=("State",),
        is_on_fn=bool,
        entity_category=EntityCategory.DIAGNOSTIC,
    ),
)


class ICSeeConfigBinarySensor(ICSeeConfigEntity, BinarySensorEntity):
    entity_description: ICSeeBinarySensorEntityDescription

    @property
    def is_on(self) -> bool | None:
        value = self.config_value
        return None if value is None else self.entity_description.is_on_fn(value)


class MotionAlarm(ICSeeEntity, BinarySensorEntity):
    """Turns on while the camera reports an alarm (motion, human, ...)."""

    _attr_device_class = BinarySensorDeviceClass.MOTION
    _attr_translation_key = "motion_alarm"

    def __init__(self, coordinator: ICSeeCoordinator, channel: int) -> None:
        super().__init__(coordinator, f"alarm_{channel}", channel)  # legacy unique id
        self._last_push = 0.0
        # Some cameras never set VideoMotion in WorkState, even during an alarm.
        # Only trust a polled "off" once the camera has shown a polled "on".
        self._poll_reports_motion = False
        self._attr_is_on = self._polled()

    def _polled(self) -> bool | None:
        """Current alarm state from WorkState (one bit per channel)."""
        state = (self.coordinator.channel_value("WorkState", 0) or {}).get("AlarmState")
        if not isinstance(state, dict) or state.get("VideoMotion") is None:
            return None
        try:
            bits = int(state["VideoMotion"], 16)
        except (TypeError, ValueError):
            return None
        return bool(bits >> self.channel & 1)

    @callback
    def _handle_coordinator_update(self) -> None:
        polled = self._polled()
        if polled:
            self._poll_reports_motion = True
        # Use the polled state when an event was missed (e.g. the "motion ended"
        # event during a connection drop). A polled "off" only counts on cameras
        # that are known to report motion in WorkState.
        recent_push = time.monotonic() - self._last_push < PUSH_GRACE
        trusted = polled or self._poll_reports_motion
        if (
            polled is not None
            and polled != self._attr_is_on
            and not recent_push
            and trusted
        ):
            self._attr_is_on = polled
        super()._handle_coordinator_update()

    @property
    def available(self) -> bool:
        # Pushed by the camera; does not depend on the config polling
        return self.device.is_connected

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        self.async_on_remove(self.device.add_alarm_callback(self._on_alarm))

    @callback
    def _on_alarm(self, what: dict[str, Any]) -> None:
        if what.get("Channel") != self.channel:
            return
        self._last_push = time.monotonic()
        self._attr_is_on = what.get("Status") == "Start"
        self._attr_extra_state_attributes = what
        self.async_write_ha_state()

    # ---- entity actions (registered in __init__.async_setup) ---------------

    async def async_move(self, cmd: str, **kwargs: Any) -> None:
        options = self.coordinator.config_entry.options
        step = kwargs.get("step", options.get(CONF_STEP, 2))
        preset = kwargs.get("preset", options.get(CONF_PRESET, 0))
        channel = kwargs.get("channel", options.get(CONF_CHANNEL, self.channel))
        await _run(self.device.async_ptz(cmd, step, preset, channel))

    async def async_synchronize_clock(self) -> None:
        await _run(self.device.async_set_time())

    async def async_force_frame(self) -> None:
        await _run(self.device.async_force_frame())

    async def async_set_human_zone(
        self, x_min: float, y_min: float, x_max: float, y_max: float
    ) -> None:
        """Person detection area (first rule), in percent of the image."""
        if not (0 <= x_min < x_max <= 100 and 0 <= y_min < y_max <= 100):
            raise ServiceValidationError(
                translation_domain=DOMAIN, translation_key="invalid_zone"
            )
        x1, y1, x2, y2 = (_to_coord(v) for v in (x_min, y_min, x_max, y_max))
        pts = [
            {"X": x1, "Y": y1},
            {"X": x2, "Y": y1},
            {"X": x2, "Y": y2},
            {"X": x1, "Y": y2},
        ]

        def mutate(value: dict[str, Any]) -> None:
            rules = value.get("PedRule")
            if not rules or not isinstance(rules[0].get("RuleRegion"), dict):
                raise ServiceValidationError(
                    translation_domain=DOMAIN, translation_key="zone_not_supported"
                )
            rule = rules[0]
            rule["Enable"] = True
            rule["RuleType"] = 1  # area (0 = crossing line)
            rule["RuleRegion"]["Pts"] = pts
            rule["RuleRegion"]["PtsNum"] = 4

        await self.coordinator.async_update_config(
            "Detect.HumanDetection", self.channel, mutate
        )

    async def async_set_motion_zone(self, rows: list[str]) -> None:
        """Motion grid: one string of 1/0 per row, left column first."""
        n_rows, n_cols = motion_grid_size(self.coordinator)
        if (
            len(rows) != n_rows
            or any(len(r) != n_cols or set(r) - {"0", "1"} for r in rows)
            or "1" not in "".join(rows)
        ):
            raise ServiceValidationError(
                translation_domain=DOMAIN,
                translation_key="invalid_motion_zone",
                translation_placeholders={"rows": str(n_rows), "cols": str(n_cols)},
            )

        def mutate(value: dict[str, Any]) -> None:
            region = value.get("Region")
            if not isinstance(region, list) or len(region) < n_rows:
                raise ServiceValidationError(
                    translation_domain=DOMAIN, translation_key="zone_not_supported"
                )
            for r, row in enumerate(rows):
                mask = sum(1 << c for c, cell in enumerate(row) if cell == "1")
                region[r] = f"0x{mask:08X}"

        await self.coordinator.async_update_config(
            "Detect.MotionDetect", self.channel, mutate
        )


ZONE_MAX = 8191  # camera coordinates of detection rules (0..8191)
HUMAN_EVENTS = frozenset(
    {"appEventHumanDetectAlarm", "HumanDetect", "HumanDetection", "HumanAlarm"}
)


def _to_coord(percent: float) -> int:
    return round(percent / 100 * ZONE_MAX)


def motion_grid_size(coordinator: ICSeeCoordinator) -> tuple[int, int]:
    area = coordinator.abilities.get("MotionArea") or {}
    try:
        rows, cols = int(area["GridRow"]), int(area["GridColumn"])
    except (KeyError, TypeError, ValueError):
        return 18, 22  # the usual XM grid when the camera does not say
    return rows, cols


def zones_of(coordinator: ICSeeCoordinator, channel: int) -> dict[str, Any]:
    """Current detection zones, for the person sensor attributes."""
    attrs: dict[str, Any] = {}
    human = coordinator.channel_value("Detect.HumanDetection", channel) or {}
    rules = human.get("PedRule") or []
    if rules and isinstance(rules[0], dict):
        pts = (rules[0].get("RuleRegion") or {}).get("Pts") or []
        try:
            xs = [int(p["X"]) for p in pts]
            ys = [int(p["Y"]) for p in pts]
        except (KeyError, TypeError, ValueError):
            xs = ys = []
        if xs and ys:
            pct = lambda v: round(v / ZONE_MAX * 100, 1)  # noqa: E731
            attrs["human_zone"] = {
                "x_min": pct(min(xs)),
                "y_min": pct(min(ys)),
                "x_max": pct(max(xs)),
                "y_max": pct(max(ys)),
                "enabled": bool(rules[0].get("Enable")),
            }
    region = (coordinator.channel_value("Detect.MotionDetect", channel) or {}).get(
        "Region"
    )
    if isinstance(region, list):
        n_rows, n_cols = motion_grid_size(coordinator)
        out = []
        for value in region[:n_rows]:
            try:
                mask = int(value, 16) if isinstance(value, str) else int(value)
            except (TypeError, ValueError):
                mask = 0
            out.append("".join("1" if mask >> c & 1 else "0" for c in range(n_cols)))
        attrs["motion_zone"] = out
    return attrs


class HumanAlarm(ICSeeEntity, BinarySensorEntity):
    """On while the camera reports a person (its own AI), not plain motion."""

    _attr_device_class = BinarySensorDeviceClass.OCCUPANCY
    _attr_translation_key = "human_alarm"

    def __init__(self, coordinator: ICSeeCoordinator, channel: int) -> None:
        super().__init__(coordinator, f"human_alarm_{channel}", channel)
        self._attr_is_on = False
        self._last: dict[str, Any] = {}

    @property
    def available(self) -> bool:
        return self.device.is_connected

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        return {**zones_of(self.coordinator, self.channel), **self._last}

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        self.async_on_remove(self.device.add_alarm_callback(self._on_alarm))

    @callback
    def _on_alarm(self, what: dict[str, Any]) -> None:
        if what.get("Channel") != self.channel or what.get("Event") not in HUMAN_EVENTS:
            return
        self._attr_is_on = what.get("Status") == "Start"
        self._last = {
            "last_event": what.get("Event"),
            "start_time": what.get("StartTime"),
        }
        self.async_write_ha_state()


async def _run(coro: Any) -> None:
    try:
        await coro
    except CannotConnect as err:
        raise HomeAssistantError(
            translation_domain=DOMAIN, translation_key="cannot_connect"
        ) from err
    except CommandFailed as err:
        raise HomeAssistantError(
            translation_domain=DOMAIN,
            translation_key="command_failed",
            translation_placeholders={"config": err.name, "ret": str(err.ret)},
        ) from err

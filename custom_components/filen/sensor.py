"""Storage sensors for Filen."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.const import PERCENTAGE, EntityCategory, UnitOfInformation
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceEntryType, DeviceInfo
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from . import FilenConfigEntry
from .const import DOMAIN
from .coordinator import FilenStorageCoordinator, StorageInfo

PARALLEL_UPDATES = 0  # all sensors read from one coordinator


@dataclass(frozen=True, kw_only=True)
class FilenSensorDescription(SensorEntityDescription):
    """Describes a Filen storage sensor."""

    value_fn: Callable[[StorageInfo], float]


def _bytes_sensor(
    key: str,
    value_fn: Callable[[StorageInfo], float],
    entity_category: EntityCategory | None = None,
) -> FilenSensorDescription:
    return FilenSensorDescription(
        key=key,
        translation_key=key,
        device_class=SensorDeviceClass.DATA_SIZE,
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=UnitOfInformation.BYTES,
        suggested_unit_of_measurement=UnitOfInformation.GIGABYTES,
        suggested_display_precision=1,
        entity_category=entity_category,
        value_fn=value_fn,
    )


SENSORS: tuple[FilenSensorDescription, ...] = (
    _bytes_sensor("storage_used", lambda s: s.used),
    _bytes_sensor("storage_free", lambda s: s.free),
    _bytes_sensor("storage_total", lambda s: s.total, EntityCategory.DIAGNOSTIC),
    FilenSensorDescription(
        key="storage_used_percent",
        translation_key="storage_used_percent",
        native_unit_of_measurement=PERCENTAGE,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=1,
        value_fn=lambda s: round(s.used / s.total * 100, 2) if s.total else 0.0,
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: FilenConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up the storage sensors."""
    coordinator = entry.runtime_data.coordinator
    async_add_entities(
        FilenStorageSensor(coordinator, description) for description in SENSORS
    )


class FilenStorageSensor(CoordinatorEntity[FilenStorageCoordinator], SensorEntity):
    """One storage value of the Filen account."""

    _attr_has_entity_name = True
    entity_description: FilenSensorDescription

    def __init__(
        self,
        coordinator: FilenStorageCoordinator,
        description: FilenSensorDescription,
    ) -> None:
        super().__init__(coordinator)
        self.entity_description = description
        entry = coordinator.config_entry
        self._attr_unique_id = f"{entry.unique_id}_{description.key}"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, str(entry.unique_id))},
            name="Filen",
            manufacturer="Filen",
            model="Cloud",
            entry_type=DeviceEntryType.SERVICE,
            configuration_url="https://drive.filen.io",
        )

    @property
    def native_value(self) -> float | None:
        if self.coordinator.data is None:
            return None
        return self.entity_description.value_fn(self.coordinator.data)
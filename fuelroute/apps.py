"""App configuration, including start-up cache warming."""

from __future__ import annotations

import logging
import sys
import threading

from django.apps import AppConfig

logger = logging.getLogger(__name__)

# Management commands that must not trigger warming (the tables may not exist
# yet, and warming would just slow them down).
_COLD_COMMANDS = {
    "migrate", "makemigrations", "load_fuel_prices", "collectstatic",
    "shell", "dbshell", "createsuperuser", "test", "check", "flush",
}


class FuelrouteConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "fuelroute"
    verbose_name = "Fuel route planning"

    def ready(self) -> None:
        if _COLD_COMMANDS.intersection(sys.argv):
            return
        threading.Thread(target=self._warm, name="fuelroute-warm", daemon=True).start()

    @staticmethod
    def _warm() -> None:
        """Preload the gazetteer and station index so request #1 isn't the slow one."""
        try:
            from fuelroute.services.geocoding import _GAZETTEER
            from fuelroute.services.station_index import StationIndex

            _GAZETTEER.load()
            StationIndex.get()
            logger.info("Warm-up complete: gazetteer and station index ready.")
        except Exception:  # noqa: BLE001 - warming must never break start-up
            logger.warning("Warm-up skipped", exc_info=True)

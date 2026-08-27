from django.db import models


class FuelStation(models.Model):
    """A truck stop with a retail diesel price and a resolved location.

    Coordinates are not present in the source CSV; they are resolved offline by
    ``scripts/build_station_coordinates.py`` so that request handling never has
    to geocode anything.
    """

    opis_id = models.CharField(max_length=32, unique=True, db_index=True)
    name = models.CharField(max_length=255)
    address = models.CharField(max_length=255, blank=True)
    city = models.CharField(max_length=128)
    state = models.CharField(max_length=2, db_index=True)
    rack_id = models.CharField(max_length=32, blank=True)

    # Mean of every retail price observation for this truck stop in the CSV.
    retail_price = models.FloatField()
    price_observations = models.PositiveIntegerField(default=1)

    latitude = models.FloatField()
    longitude = models.FloatField()
    geocode_precision = models.CharField(
        max_length=16,
        default="city",
        help_text="How the coordinate was resolved (city centroid, nominatim, ...).",
    )

    class Meta:
        indexes = [
            models.Index(fields=["latitude", "longitude"]),
            models.Index(fields=["retail_price"]),
        ]
        ordering = ["opis_id"]

    def __str__(self) -> str:
        return f"{self.name} ({self.city}, {self.state}) ${self.retail_price:.3f}"

    @property
    def label(self) -> str:
        return f"{self.name}, {self.city}, {self.state}"

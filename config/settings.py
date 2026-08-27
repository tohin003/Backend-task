"""Django settings for the fuel-route API."""

import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent


def _env_bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    return default if raw is None else raw.strip().lower() in {"1", "true", "yes", "on"}


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.environ[name])
    except (KeyError, ValueError):
        return default


SECRET_KEY = os.environ.get(
    "DJANGO_SECRET_KEY", "django-insecure-dev-only-key-change-me-in-production"
)
DEBUG = _env_bool("DJANGO_DEBUG", True)
ALLOWED_HOSTS = [h for h in os.environ.get("DJANGO_ALLOWED_HOSTS", "*").split(",") if h]

INSTALLED_APPS = [
    "django.contrib.contenttypes",
    "django.contrib.staticfiles",
    "fuelroute",
]

MIDDLEWARE = [
    "django.middleware.common.CommonMiddleware",
    "django.middleware.gzip.GZipMiddleware",
]

ROOT_URLCONF = "config.urls"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [],
        "APP_DIRS": True,
        "OPTIONS": {"context_processors": []},
    },
]

WSGI_APPLICATION = "config.wsgi.application"
ASGI_APPLICATION = "config.asgi.application"

DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.sqlite3",
        "NAME": os.environ.get("DJANGO_DB_PATH", BASE_DIR / "db.sqlite3"),
    }
}

# In-process cache. Route plans are deterministic for a given input, so caching
# them means repeat requests make zero external API calls.
CACHES = {
    "default": {
        "BACKEND": "django.core.cache.backends.locmem.LocMemCache",
        "LOCATION": "fuelroute",
        "TIMEOUT": 60 * 60 * 6,
        "OPTIONS": {"MAX_ENTRIES": 2000, "CULL_FREQUENCY": 4},
    }
}

AUTH_PASSWORD_VALIDATORS = []
LANGUAGE_CODE = "en-us"
TIME_ZONE = "UTC"
USE_I18N = False
USE_TZ = True
STATIC_URL = "static/"
DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "formatters": {"simple": {"format": "%(levelname)s %(name)s: %(message)s"}},
    "handlers": {
        "console": {"class": "logging.StreamHandler", "formatter": "simple"},
    },
    "root": {"handlers": ["console"], "level": os.environ.get("LOG_LEVEL", "INFO")},
}

# --------------------------------------------------------------------------
# Application settings
# --------------------------------------------------------------------------

DATA_DIR = BASE_DIR / "data"
FUEL_PRICES_CSV = DATA_DIR / "fuel-prices-for-be-assessment.csv"
STATION_COORDINATES_CSV = DATA_DIR / "station_coordinates.csv"
US_PLACES_CSV = DATA_DIR / "us_places.csv"

# OSRM public demo server: free, keyless, OpenStreetMap-based.
# Override with a self-hosted instance for production traffic.
OSRM_BASE_URL = os.environ.get("OSRM_BASE_URL", "https://router.project-osrm.org")
OSRM_TIMEOUT_SECONDS = _env_float("OSRM_TIMEOUT_SECONDS", 20.0)

# Nominatim is only ever used as a fallback when a free-text location cannot be
# resolved from the bundled offline gazetteer.
NOMINATIM_BASE_URL = os.environ.get(
    "NOMINATIM_BASE_URL", "https://nominatim.openstreetmap.org"
)
NOMINATIM_USER_AGENT = os.environ.get(
    "NOMINATIM_USER_AGENT", "fuel-route-api/1.0 (backend assessment)"
)
NOMINATIM_TIMEOUT_SECONDS = _env_float("NOMINATIM_TIMEOUT_SECONDS", 10.0)
ENABLE_NOMINATIM_FALLBACK = _env_bool("ENABLE_NOMINATIM_FALLBACK", True)

# Vehicle profile (fixed by the assignment brief).
VEHICLE_MAX_RANGE_MILES = _env_float("VEHICLE_MAX_RANGE_MILES", 500.0)
VEHICLE_MPG = _env_float("VEHICLE_MPG", 10.0)

# How far off the route a station may sit and still count as "on the way".
DEFAULT_CORRIDOR_MILES = _env_float("DEFAULT_CORRIDOR_MILES", 15.0)
MAX_CORRIDOR_MILES = _env_float("MAX_CORRIDOR_MILES", 50.0)

ROUTE_CACHE_SECONDS = int(_env_float("ROUTE_CACHE_SECONDS", 60 * 60 * 6))

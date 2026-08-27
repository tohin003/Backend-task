"""Tests for resolving user-supplied locations."""

from __future__ import annotations

from django.test import SimpleTestCase, override_settings

from fuelroute.services.geocoding import (
    GeocodingError,
    normalise,
    resolve_location,
    split_state,
)


class SplitStateTests(SimpleTestCase):
    def test_comma_separated_abbreviation(self):
        self.assertEqual(split_state("Dallas, TX"), ("Dallas", "TX"))

    def test_space_separated_abbreviation(self):
        self.assertEqual(split_state("Dallas TX"), ("Dallas", "TX"))

    def test_full_state_name(self):
        self.assertEqual(split_state("Dallas, Texas"), ("Dallas", "TX"))

    def test_multiword_state_name(self):
        self.assertEqual(split_state("Charlotte, North Carolina"), ("Charlotte", "NC"))

    def test_trailing_country_is_stripped(self):
        self.assertEqual(split_state("Dallas, TX, USA"), ("Dallas", "TX"))

    def test_no_state_returns_none(self):
        self.assertEqual(split_state("Dallas"), ("Dallas", None))

    def test_multiword_city_keeps_its_words(self):
        self.assertEqual(split_state("Salt Lake City, UT"), ("Salt Lake City", "UT"))


class NormaliseTests(SimpleTestCase):
    def test_uppercases_and_strips_punctuation(self):
        self.assertEqual(normalise(" St. Louis "), "ST LOUIS")

    def test_expands_leading_direction(self):
        self.assertEqual(normalise("S Coffeyville"), "SOUTH COFFEYVILLE")

    def test_collapses_whitespace(self):
        self.assertEqual(normalise("Fort   Worth"), "FORT WORTH")


@override_settings(ENABLE_NOMINATIM_FALLBACK=False)
class ResolveLocationTests(SimpleTestCase):
    def test_resolves_a_known_city_from_the_bundled_gazetteer(self):
        located = resolve_location("Dallas, TX")
        self.assertEqual(located.source, "gazetteer")
        self.assertAlmostEqual(located.latitude, 32.78, delta=0.2)
        self.assertAlmostEqual(located.longitude, -96.80, delta=0.2)

    def test_resolves_new_york_via_an_alternate_name(self):
        located = resolve_location("New York, NY")
        self.assertEqual(located.source, "gazetteer")
        self.assertAlmostEqual(located.latitude, 40.71, delta=0.3)

    def test_parses_explicit_coordinates_without_any_lookup(self):
        located = resolve_location("32.7767,-96.7970")
        self.assertEqual(located.source, "coordinates")
        self.assertAlmostEqual(located.latitude, 32.7767)
        self.assertAlmostEqual(located.longitude, -96.7970)

    def test_rejects_coordinates_outside_the_usa(self):
        with self.assertRaises(GeocodingError) as ctx:
            resolve_location("48.8566,2.3522")  # Paris
        self.assertEqual(ctx.exception.code, "outside_usa")

    def test_rejects_out_of_range_coordinates(self):
        with self.assertRaises(GeocodingError) as ctx:
            resolve_location("999,-96.0")
        self.assertEqual(ctx.exception.code, "invalid_coordinates")

    def test_rejects_canadian_province_codes(self):
        for query in ("Toronto, ON", "Vancouver, BC", "Calgary, Alberta"):
            with self.assertRaises(GeocodingError) as ctx:
                resolve_location(query)
            self.assertEqual(ctx.exception.code, "outside_usa", query)

    def test_does_not_reject_us_cities_that_share_canadian_names(self):
        located = resolve_location("Vancouver, WA")
        self.assertEqual(located.source, "gazetteer")
        self.assertAlmostEqual(located.latitude, 45.63, delta=0.3)

    def test_blank_input_is_rejected(self):
        with self.assertRaises(GeocodingError) as ctx:
            resolve_location("   ", field="start")
        self.assertEqual(ctx.exception.code, "missing_parameter")

    def test_unknown_place_raises_not_found(self):
        with self.assertRaises(GeocodingError) as ctx:
            resolve_location("Zzzyxville, QQ")
        self.assertEqual(ctx.exception.code, "location_not_found")

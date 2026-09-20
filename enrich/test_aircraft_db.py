"""
Tests for aircraft_db.py.

Offline by design: fixtures below reproduce the *exact* on-disk shape that
download() writes -- which was reverse-engineered from the real, live
sources with curl (see aircraft_db.py's module docstring for what was
found: db/<PREFIX>.js is gzip bytes despite the ".js" name, the shard JSON
shape, the "children" metadata key, the plane-alert-db CSV header/no-dup
guarantee). Keeping the test suite offline means it's fast and
deterministic; the format itself was verified by hand against the live
GitHub raw content before this parser was written, not assumed.

download() itself is exercised against a mocked urllib.request.urlopen so
its retry/partial-failure/counts behavior is checked without hitting the
network on every test run.
"""

import csv
import gzip
import json
import os
import tempfile
import unittest
import urllib.error
from unittest import mock

import aircraft_db


def _write_shard(tar_dir, prefix, entries):
    with open(os.path.join(tar_dir, f"{prefix}.json"), "w", encoding="utf-8") as f:
        json.dump(entries, f)


def _write_index(tar_dir, prefixes):
    with open(os.path.join(tar_dir, "files.json"), "w", encoding="utf-8") as f:
        json.dump(prefixes, f)


def _write_plane_alert(dest_dir, rows):
    header = [
        "$ICAO", "$Registration", "$Operator", "$Type", "$ICAO Type",
        "#CMPG", "$Tag 1", "$#Tag 2", "$#Tag 3", "Category", "$#Link",
    ]
    path = os.path.join(dest_dir, "plane_alert.csv")
    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(header)
        writer.writerows(rows)


class LoadedFixtureTestCase(unittest.TestCase):
    """Builds a small on-disk dataset in the exact shape download() would
    have produced, then load()s it, mirroring real usage."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dest_dir = self.tmp.name
        tar_dir = os.path.join(self.dest_dir, "tar1090")
        os.makedirs(tar_dir)

        # Shard "AB": one real-looking entry, one placeholder/PIA entry
        # (all-null except flags -- must be dropped, not stored), and a
        # "children" metadata key (must be skipped, not treated as data).
        _write_shard(tar_dir, "AB", {
            "CDEF": ["N123AB", "C172", "00", "CESSNA 172 Skyhawk"],
            "0000": [None, None, "0010", None],
            # Exactly 4 elements on purpose: a shorter/longer list would
            # already be dropped by the entry-unpack guard regardless of
            # whether "children" is explicitly skipped, which would let
            # that guard silently mask a missing "children" check. Four
            # elements forces the two checks apart.
            "children": ["A0", "A1", "A2", "A3"],
        })
        # Shard "39": one entry with no long description, to check the
        # type-code fallback.
        _write_shard(tar_dir, "39", {
            "9999": ["D-EXYZ", "P28A", "00", None],
        })
        _write_index(tar_dir, ["AB", "39"])

        _write_plane_alert(self.dest_dir, [
            [
                "112233", "N911PD", "Some Police Dept", "Cessna 182",
                "C182", "Pol", "Patrol", "Law Enforcement", "Copper Chopper",
                "Police Forces", "https://example.invalid/x",
            ],
        ])

        result = aircraft_db.load(self.dest_dir)
        self.assertEqual(result, {"tar1090_entries": 2, "plane_alert_entries": 1})

    # --- lookup() ---------------------------------------------------

    def test_lookup_prefers_long_description(self):
        self.assertEqual(
            aircraft_db.lookup("ABCDEF"),
            {"registration": "N123AB", "type": "CESSNA 172 Skyhawk", "operator": None},
        )

    def test_lookup_falls_back_to_type_code_when_no_description(self):
        self.assertEqual(
            aircraft_db.lookup("399999"),
            {"registration": "D-EXYZ", "type": "P28A", "operator": None},
        )

    def test_lookup_case_insensitive_and_whitespace_tolerant(self):
        expected = {"registration": "N123AB", "type": "CESSNA 172 Skyhawk", "operator": None}
        self.assertEqual(aircraft_db.lookup("abcdef"), expected)
        self.assertEqual(aircraft_db.lookup("  ABCDEF  "), expected)
        self.assertEqual(aircraft_db.lookup("  abcdef\n"), expected)

    def test_lookup_placeholder_entry_not_stored(self):
        # AB0000 exists in the raw shard but every field is null -- it
        # must not surface as a "found" result.
        self.assertIsNone(aircraft_db.lookup("AB0000"))

    def test_lookup_children_key_not_treated_as_hex(self):
        # "children" must never be reachable as if it were a suffix.
        self.assertIsNone(aircraft_db.lookup("children"))

    def test_lookup_unknown_hex_returns_none(self):
        self.assertIsNone(aircraft_db.lookup("FFFFFF"))

    # --- notable() negative controls (required) ----------------------

    def test_notable_hex_not_in_curated_list_returns_none(self):
        # ABCDEF is a perfectly valid, present-in-registry hex -- just
        # not one of the curated notables. This is the negative control:
        # a real, well-formed miss must come back None, not some
        # default/empty-ish truthy value.
        self.assertIsNone(aircraft_db.notable("ABCDEF"))
        self.assertIsNotNone(aircraft_db.lookup("ABCDEF"))  # sanity: it *is* known

    def test_notable_found_returns_full_shape(self):
        self.assertEqual(
            aircraft_db.notable("112233"),
            {
                "registration": "N911PD",
                "operator": "Some Police Dept",
                "type": "Cessna 182",
                "category": "Police Forces",
                "tags": ["Patrol", "Law Enforcement", "Copper Chopper"],
                "mil_civ": "Pol",
                "link": "https://example.invalid/x",
            },
        )

    def test_notable_case_insensitive_and_whitespace_tolerant(self):
        result = aircraft_db.notable("  112233  ")
        self.assertIsNotNone(result)
        self.assertEqual(result["registration"], "N911PD")
        result_lower = aircraft_db.notable("112233".lower())
        self.assertIsNotNone(result_lower)

    def test_notable_result_is_a_defensive_copy(self):
        result = aircraft_db.notable("112233")
        result["registration"] = "TAMPERED"
        self.assertEqual(aircraft_db.notable("112233")["registration"], "N911PD")

    # --- malformed/empty hex negative controls (required) -------------

    def test_malformed_or_empty_hex_never_raises(self):
        bad_values = [
            "", "   ", None, 12345, "ZZZZZZ", "ABCDE", "ABCDEFG",
            "AB-CDEF", "AB CD", ["ABCDEF"], {}, b"ABCDEF",
        ]
        for bad in bad_values:
            with self.subTest(bad=bad):
                self.assertIsNone(aircraft_db.lookup(bad))
                self.assertIsNone(aircraft_db.notable(bad))


class MissingDataDirectoryTestCase(unittest.TestCase):
    """A missing/never-downloaded dest_dir must degrade to an empty,
    working state -- not raise, and not have a lookup silently succeed
    with stale data from a previous test."""

    def test_load_nonexistent_directory_does_not_raise(self):
        result = aircraft_db.load("/nonexistent/path/does/not/exist")
        self.assertEqual(result, {"tar1090_entries": 0, "plane_alert_entries": 0})
        self.assertIsNone(aircraft_db.lookup("ABCDEF"))
        self.assertIsNone(aircraft_db.notable("112233"))

    def test_load_directory_missing_only_plane_alert(self):
        with tempfile.TemporaryDirectory() as dest_dir:
            tar_dir = os.path.join(dest_dir, "tar1090")
            os.makedirs(tar_dir)
            _write_shard(tar_dir, "AB", {"CDEF": ["N123AB", "C172", "00", "Cessna 172"]})
            _write_index(tar_dir, ["AB"])
            # no plane_alert.csv written at all
            result = aircraft_db.load(dest_dir)
            self.assertEqual(result, {"tar1090_entries": 1, "plane_alert_entries": 0})
            self.assertIsNotNone(aircraft_db.lookup("ABCDEF"))
            self.assertIsNone(aircraft_db.notable("ABCDEF"))

    def test_load_directory_missing_only_tar1090(self):
        with tempfile.TemporaryDirectory() as dest_dir:
            _write_plane_alert(dest_dir, [[
                "112233", "N911PD", "Some Police Dept", "Cessna 182", "C182",
                "Pol", "Patrol", "Law Enforcement", "Copper Chopper",
                "Police Forces", "https://example.invalid/x",
            ]])
            # no tar1090/ dir at all
            result = aircraft_db.load(dest_dir)
            self.assertEqual(result, {"tar1090_entries": 0, "plane_alert_entries": 1})
            self.assertIsNone(aircraft_db.lookup("ABCDEF"))
            self.assertIsNotNone(aircraft_db.notable("112233"))

    def test_load_corrupt_shard_file_skipped_not_raised(self):
        with tempfile.TemporaryDirectory() as dest_dir:
            tar_dir = os.path.join(dest_dir, "tar1090")
            os.makedirs(tar_dir)
            with open(os.path.join(tar_dir, "AB.json"), "w", encoding="utf-8") as f:
                f.write("{not valid json")
            _write_index(tar_dir, ["AB"])
            result = aircraft_db.load(dest_dir)
            self.assertEqual(result, {"tar1090_entries": 0, "plane_alert_entries": 0})
            self.assertIsNone(aircraft_db.lookup("ABCDEF"))


class DownloadTestCase(unittest.TestCase):
    """Exercises download() against a mocked network so success/failure
    accounting is checked without depending on GitHub being reachable."""

    def _gzip_json(self, obj):
        return gzip.compress(json.dumps(obj).encode("utf-8"))

    def test_download_writes_files_and_reports_counts(self):
        files_js = self._gzip_json(["AB", "39"])
        ab_js = self._gzip_json({"CDEF": ["N123AB", "C172", "00", "Cessna 172"]})
        js_39 = self._gzip_json({"9999": ["D-EXYZ", "P28A", "00", None]})
        csv_text = (
            "$ICAO,$Registration,$Operator,$Type,$ICAO Type,#CMPG,"
            "$Tag 1,$#Tag 2,$#Tag 3,Category,$#Link\r\n"
            "112233,N911PD,Some Police Dept,Cessna 182,C182,Pol,"
            "Patrol,Law Enforcement,Copper Chopper,Police Forces,"
            "https://example.invalid/x\r\n"
        ).encode("utf-8")

        responses = {
            f"{aircraft_db.TAR1090_BASE}/files.js": files_js,
            f"{aircraft_db.TAR1090_BASE}/AB.js": ab_js,
            f"{aircraft_db.TAR1090_BASE}/39.js": js_39,
            aircraft_db.PLANE_ALERT_URL: csv_text,
        }

        def fake_fetch(url):
            return responses[url]

        with tempfile.TemporaryDirectory() as dest_dir:
            with mock.patch.object(aircraft_db, "_fetch", side_effect=fake_fetch):
                counts = aircraft_db.download(dest_dir)

            self.assertEqual(counts["tar1090_shards_total"], 2)
            self.assertEqual(counts["tar1090_shards_ok"], 2)
            self.assertEqual(counts["tar1090_shards_failed"], 0)
            self.assertTrue(counts["plane_alert_ok"])
            self.assertEqual(counts["plane_alert_rows"], 1)
            self.assertEqual(counts["errors"], [])

            # And the files it wrote are actually load()-able end to end.
            load_result = aircraft_db.load(dest_dir)
            self.assertEqual(load_result, {"tar1090_entries": 2, "plane_alert_entries": 1})
            self.assertIsNotNone(aircraft_db.lookup("ABCDEF"))
            self.assertIsNotNone(aircraft_db.notable("112233"))

    def test_download_partial_shard_failure_does_not_raise_or_abort(self):
        files_js = self._gzip_json(["AB", "BAD"])
        ab_js = self._gzip_json({"CDEF": ["N123AB", "C172", "00", "Cessna 172"]})

        def fake_fetch(url):
            if url.endswith("/BAD.js"):
                raise urllib.error.URLError("simulated network failure")
            responses = {
                f"{aircraft_db.TAR1090_BASE}/files.js": files_js,
                f"{aircraft_db.TAR1090_BASE}/AB.js": ab_js,
            }
            if url in responses:
                return responses[url]
            raise urllib.error.URLError("simulated network failure")

        with tempfile.TemporaryDirectory() as dest_dir:
            with mock.patch.object(aircraft_db, "_fetch", side_effect=fake_fetch):
                counts = aircraft_db.download(dest_dir)

        self.assertEqual(counts["tar1090_shards_total"], 2)
        self.assertEqual(counts["tar1090_shards_ok"], 1)
        self.assertEqual(counts["tar1090_shards_failed"], 1)
        self.assertFalse(counts["plane_alert_ok"])
        self.assertEqual(len(counts["errors"]), 2)  # BAD.js + plane-alert-db.csv


if __name__ == "__main__":
    unittest.main()

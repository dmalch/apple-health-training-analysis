#!/usr/bin/env python3
"""Tests for profile loading and for the device guard in sync_health.sh.

    python3 -m unittest test_profile -v

The guard test is the one that matters: it is the only thing standing between a
two-phone household and one person's export landing in the other person's
database. It builds a fake finished backup that claims a different device and
checks the script refuses before touching anything.

No real health data; no phone required.
"""

import os
import plistlib
import shutil
import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path

import athlete_profile

REPO = Path(__file__).resolve().parent
SYNC = REPO / ".claude" / "skills" / "health-sync" / "scripts" / "sync_health.sh"

MINIMAL = """
timezone = "Europe/Berlin"
max_hr = 190.0
strength = ["CoreTraining"]
hybrid = ["HighIntensityIntervalTraining"]
"""


class ProfileTest(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp(prefix="profiles-"))
        self.addCleanup(shutil.rmtree, self.dir, ignore_errors=True)

    def write(self, name, body):
        path = self.dir / f"{name}.toml"
        path.write_text(textwrap.dedent(body))
        return path

    def load(self, name, body):
        return athlete_profile.load(path=self.write(name, body))

    def test_a_minimal_profile_fills_in_the_rest(self):
        p = self.load("min", MINIMAL)
        self.assertEqual(p["name"], "min")
        self.assertEqual(p["max_hr"], 190.0)
        self.assertEqual(p["data_dir"], "data")
        self.assertEqual(p["db"], os.path.join("data", "health.duckdb"))
        self.assertEqual(p["duplicates"], [])
        self.assertEqual(p["cross_talk"], [])
        self.assertEqual(len(p["zones"]), 5)
        self.assertIn("Bluetooth Device", p["strap_sources"])

    def test_strap_means_derive_the_anchor_later(self):
        p = self.load("strap", MINIMAL.replace("max_hr = 190.0", 'max_hr = "strap"'))
        self.assertIsNone(p["max_hr"])

    def test_a_typo_in_a_field_name_is_refused_not_ignored(self):
        # A silently ignored key is a setting that never takes effect.
        with self.assertRaises(athlete_profile.ProfileError) as e:
            self.load("typo", MINIMAL + '\nduplicate = ["gym app"]\n')
        self.assertIn("duplicate", str(e.exception))

    def test_a_missing_required_field_names_itself(self):
        with self.assertRaises(athlete_profile.ProfileError) as e:
            self.load("bare", 'timezone = "UTC"\n')
        msg = str(e.exception)
        for field in ("max_hr", "strength", "hybrid"):
            self.assertIn(field, msg)

    def test_an_implausible_anchor_is_refused(self):
        # 19 instead of 190 would rescale every zone without any error at all.
        with self.assertRaises(athlete_profile.ProfileError) as e:
            self.load("typo2", MINIMAL.replace("190.0", "19.0"))
        self.assertIn("120-230", str(e.exception))

    def test_a_string_that_is_not_strap_is_refused(self):
        with self.assertRaises(athlete_profile.ProfileError):
            self.load("bad", MINIMAL.replace("190.0", '"about 190"'))

    def test_a_list_field_must_hold_strings(self):
        with self.assertRaises(athlete_profile.ProfileError) as e:
            self.load("nums", MINIMAL.replace('["CoreTraining"]', "[1, 2]"))
        self.assertIn("strength", str(e.exception))

    def test_zones_with_a_gap_are_refused(self):
        body = (
            MINIMAL
            + """
        [[zones]]
        name = "easy"
        lo = 0.0
        hi = 0.6
        [[zones]]
        name = "hard"
        lo = 0.7
        hi = 9.99
        """
        )
        with self.assertRaises(athlete_profile.ProfileError) as e:
            self.load("gap", body)
        self.assertIn("gap", str(e.exception))

    def test_a_missing_profile_lists_where_it_looked(self):
        with self.assertRaises(athlete_profile.ProfileError) as e:
            athlete_profile.load("nobody-has-this-name")
        self.assertIn("Looked in", str(e.exception))

    def test_field_tolerates_no_profile_at_all(self):
        self.assertIsNone(athlete_profile.field(None, "db"))
        self.assertIsNone(athlete_profile.field({}, "db"))


@unittest.skipUnless(
    sys.platform == "darwin" and shutil.which("plutil"),
    "the guard reads plists with plutil, which is macOS-only",
)
class DeviceGuardTest(unittest.TestCase):
    """sync_health.sh must refuse a backup that came from another phone."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="sync-guard-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.profiles = self.tmp / "profiles"
        self.profiles.mkdir()
        self.db = self.tmp / "data" / "health-live.duckdb"

    def make_backup(self, udid):
        d = self.tmp / "backups" / udid
        d.mkdir(parents=True)
        (d / "Manifest.db").write_bytes(b"")
        with open(d / "Status.plist", "wb") as fh:
            plistlib.dump({"SnapshotState": "finished"}, fh)
        with open(d / "Info.plist", "wb") as fh:
            plistlib.dump({"Target Identifier": udid}, fh)
        return d

    def make_profile(self, name, want_udid):
        (self.profiles / f"{name}.toml").write_text(
            MINIMAL
            + f'\nbackup_root = "{self.tmp / "backups"}"\n'
            + f'db = "{self.db}"\n'
            + f'device_udid = "{want_udid}"\n'
        )

    def run_sync(self, name):
        env = dict(os.environ, HEALTH_PROFILES=str(self.profiles), HEALTH_PROJECT=str(REPO))
        return subprocess.run(
            ["bash", str(SYNC), "--profile", name, "--skip-backup"],
            capture_output=True,
            text=True,
            env=env,
            timeout=120,
        )

    def test_a_backup_from_another_phone_is_refused(self):
        self.make_backup("PHONE-B-UDID")
        self.make_profile("owner", "PHONE-A-UDID")

        result = self.run_sync("owner")

        self.assertNotEqual(result.returncode, 0, "the guard let another phone through")
        combined = result.stdout + result.stderr
        self.assertIn("different phone", combined)
        self.assertIn("PHONE-A-UDID", combined)
        self.assertIn("PHONE-B-UDID", combined)
        # And, the whole point: it stopped before writing anything.
        self.assertFalse(self.db.exists(), "the database was created despite the mismatch")

    def test_a_profile_without_a_udid_warns_rather_than_guards(self):
        self.make_backup("SOME-UDID")
        (self.profiles / "loose.toml").write_text(
            MINIMAL + f'\nbackup_root = "{self.tmp / "backups"}"\n' + f'db = "{self.db}"\n'
        )

        result = self.run_sync("loose")

        self.assertIn("sets no device_udid", result.stdout + result.stderr)


if __name__ == "__main__":
    unittest.main()


class ZoneValidationTest(unittest.TestCase):
    """Every rejection here prevents minutes vanishing from a report in silence."""

    def setUp(self):
        self.dir = Path(tempfile.mkdtemp(prefix="zones-"))
        self.addCleanup(shutil.rmtree, self.dir, ignore_errors=True)

    def load(self, zones_toml):
        path = self.dir / "z.toml"
        path.write_text(textwrap.dedent(MINIMAL + zones_toml))
        return athlete_profile.load(path=path)

    TWO_GOOD = """
    [[zones]]
    name = "easy"
    lo = 0.0
    hi = 0.7
    [[zones]]
    name = "hard"
    lo = 0.7
    hi = 9.99
    """

    def test_two_touching_bands_are_accepted(self):
        p = self.load(self.TWO_GOOD)
        self.assertEqual([z[0] for z in p["zones"]], ["easy", "hard"])

    def test_a_band_written_backwards_is_refused(self):
        # This used to pass validation: the gap check compared hi=0.6 of the
        # inverted band against lo=0.6 of the next and saw no gap. Downstream,
        # `lo <= frac < hi` could never fire, so those minutes were counted
        # nowhere and the totals quietly came out short.
        body = """
        [[zones]]
        name = "backwards"
        lo = 0.9
        hi = 0.6
        [[zones]]
        name = "rest"
        lo = 0.6
        hi = 9.99
        """
        with self.assertRaises(athlete_profile.ProfileError) as e:
            self.load(body)
        self.assertIn("backwards", str(e.exception))

    def test_overlapping_bands_are_refused(self):
        body = """
        [[zones]]
        name = "a"
        lo = 0.0
        hi = 0.8
        [[zones]]
        name = "b"
        lo = 0.7
        hi = 9.99
        """
        with self.assertRaises(athlete_profile.ProfileError) as e:
            self.load(body)
        self.assertIn("twice", str(e.exception))

    def test_a_first_band_that_does_not_start_at_zero_is_refused(self):
        body = """
        [[zones]]
        name = "a"
        lo = 0.5
        hi = 0.7
        [[zones]]
        name = "b"
        lo = 0.7
        hi = 9.99
        """
        with self.assertRaises(athlete_profile.ProfileError) as e:
            self.load(body)
        self.assertIn("below that", str(e.exception))

    def test_a_zero_width_band_is_refused(self):
        body = """
        [[zones]]
        name = "flat"
        lo = 0.0
        hi = 0.0
        [[zones]]
        name = "rest"
        lo = 0.0
        hi = 9.99
        """
        with self.assertRaises(athlete_profile.ProfileError):
            self.load(body)

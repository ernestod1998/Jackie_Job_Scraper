#!/usr/bin/env python3
"""Secret-free regression tests for scraper filtering and retrieval policy."""

import json
import os
import re
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import scrape_jobs as sj

# A frozen fixture date becomes provably stale once real time passes it by
# MAX_POSTING_AGE_DAYS and the choke point starts rejecting every fixture
# row — so the default is always "yesterday".
FRESH_FIXTURE_DATE = (
    datetime.now(timezone.utc) - timedelta(days=1)
).strftime("%Y-%m-%dT%H:%M:%SZ")


def role(url="https://example.com/job/1", **overrides):
    job = {
        "company": "Acme", "title": "Account Manager",
        "location": "San Francisco, CA", "url": url,
        "date_posted": FRESH_FIXTURE_DATE, "ats": "Test",
    }
    job.update(overrides)
    return job


class RoleAndLocationPolicy(unittest.TestCase):
    def test_curated_company_matching_is_exact_not_substring_based(self):
        # The curated allowlist is empty for Jackie until Phase 2 (healthtech),
        # so exercise the matcher with a patched allowlist + cleared cache.
        omada = sj._normalize_company_name("Omada Health")
        with patch.object(sj, "HOLLYWOOD_COMPANY_ALLOWLIST", {omada}), \
             patch.dict(sj._HOLLYWOOD_UNION_CACHE, {}, clear=True):
            self.assertTrue(sj._is_hollywood_company("Omada Health, Inc."))
            self.assertFalse(sj._is_hollywood_company("Omada"))
            self.assertFalse(sj._is_hollywood_company("Meta"))
        sj._HOLLYWOOD_UNION_CACHE.clear()
        self.assertFalse(sj._is_hollywood_company("Omada Health"))

    def test_seniority_veto_is_word_bounded(self):
        for prefix in ("Principal", "Director", "Founding", "Distinguished"):
            self.assertFalse(sj.is_target_role(f"{prefix} Care Coordinator"), prefix)
        for title in (
            # bare senior / staff / lead / manager / supervisor are ALLOWED
            "Senior Care Coordinator", "Sr. Operations Specialist",
            "Staff Training Coordinator", "Operations Manager",
            "Support Team Lead", "Supervisor, Patient Services",
            "Clinical Operations Manager", "Customer Support Team Lead",
            "Account Manager", "Junior Project Manager",
            "Care Coordinator, Leadership Development", "Clinician Support Lead",
        ):
            self.assertTrue(sj.is_target_role(title), title)
        for title in (
            "Senior Manager, Operations", "Sr Manager, Customer Support",
            "General Manager, Client Services", "Regional Manager, Operations",
            "VP Operations", "Head of Support", "Director of Clinical Operations",
            "Associate Director, Care Operations", "Chief of Staff",
        ):
            self.assertFalse(sj.is_target_role(title), title)

    def test_domain_veto_drops_clinical_engineering_sales_industrial(self):
        for title in (
            "RN Case Manager", "Nurse Care Manager", "Pharmacist, Clinical Operations",
            "Engineering Program Manager", "Software Support Engineer",
            "Technical Program Manager", "Technical Project Manager", "Data Analyst, Operations",
            "Account Executive", "Sales Development Representative",
            "Warehouse Operations Manager", "Operations Manager - 1st Shift",
            "Hotel Operations Manager", "Branch Operations Manager",
        ):
            self.assertFalse(sj.is_target_role(title), title)
        for title in ("Care Manager", "Program Manager", "Account Manager",
                      "Clinician Support Lead", "Driver Support Team Lead"):
            self.assertTrue(sj.is_target_role(title), title)

    def test_watch_location_accepts_bay_area_and_us_remote(self):
        accepted = (
            "San Francisco, CA", "South San Francisco, CA", "Oakland, CA", "SF",
            "Palo Alto, CA", "Newark, CA", "Richmond, California", "San Jose, CA",
            "Fremont, CA", "Walnut Creek, CA", "San Francisco Bay Area",
            # US-remote
            "Remote", "Remote - USA", "Remote, US", "United States (Remote)",
            "Remote (United States)",
        )
        for location in accepted:
            self.assertTrue(sj.is_watch_location(location), location)

    def test_watch_location_rejects_namesakes_and_out_of_region(self):
        rejected = (
            # Bay namesakes
            "Dublin, Ireland", "Newark, DE", "Newark, NJ", "Richmond, VA",
            "Concord, NH", "Union City, NJ", "Danville, VA", "Brisbane, Australia",
            # PJ's metros are out of scope here
            "Los Angeles, CA", "Long Beach, CA", "Irvine, CA",
            "New York, NY", "Brooklyn, NY", "Jersey City, NJ",
            "New York City Metropolitan Area",
            "Atlanta, GA", "Chicago, IL",
            # Elsewhere / non-US remote
            "Riverside, CA", "San Diego, CA", "Sacramento, CA", "Denver, CO",
            "Boston, MA", "Remote - Canada", "Spain - Remote", "Remote, UK",
        )
        for location in rejected:
            self.assertFalse(sj.is_watch_location(location), location)

    def test_remote_us_flag_off_rejects_remote(self):
        with patch.object(sj, "INCLUDE_REMOTE_US", False):
            self.assertFalse(sj.is_watch_location("Remote - USA"))
            self.assertFalse(sj.is_watch_location("Remote"))
            self.assertTrue(sj.is_watch_location("Oakland, CA"))

    def test_filter_is_feed_aware_and_reports_stats(self):
        rows = [
            role("https://x/ok"),
            role("https://x/senior", title="Senior Manager, Client Services"),
            role("https://x/domain", title="RN Case Manager"),
            role("https://x/far", location="Boston, MA"),
            role("https://x/company", company="Jack & Jill"),
            role("https://x/old", date_posted="2024-09-04"),
        ]
        kept, rejected, stats = sj._filter_job_observations(rows, default_feed="general")
        self.assertEqual([j["url"] for j in kept], ["https://x/ok"])
        self.assertEqual(kept[0]["feeds"], ["general"])
        self.assertEqual(stats, {"company": 1, "seniority": 1, "domain": 1, "role": 0, "location": 1, "stale": 1})
        self.assertEqual({r["reason"] for r in rejected}, {"company", "seniority", "domain", "location", "stale"})
        bio, _, _ = sj._filter_job_observations(
            [role("https://x/bio", location="Berkeley, CA")], default_feed="hollywood")
        self.assertEqual(bio[0]["feeds"], ["hollywood"])

    def test_stale_policy_at_the_choke_point(self):
        now = datetime.now(timezone.utc)

        def days_ago(n):
            return (now - timedelta(days=n)).strftime("%Y-%m-%dT%H:%M:%SZ")

        rows = [
            role("https://x/fresh", date_posted=days_ago(13)),
            role("https://x/stale", date_posted=days_ago(15)),
            role("https://x/workday-old", date_posted="Posted 30+ Days Ago"),
            role("https://x/workday-fresh", date_posted="Posted 2 Days Ago"),
            role("https://x/undated", date_posted=""),
        ]
        kept, _, stats = sj._filter_job_observations(rows, default_feed="general")
        self.assertEqual(
            [j["url"] for j in kept],
            ["https://x/fresh", "https://x/workday-fresh", "https://x/undated"],
        )
        self.assertEqual(stats["stale"], 2)


class MasterPolicy(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.old_dir = sj.SCRIPT_DIR
        sj.SCRIPT_DIR = self.tmp.name

    def tearDown(self):
        sj.SCRIPT_DIR = self.old_dir
        self.tmp.cleanup()

    def read_master(self):
        with open(os.path.join(self.tmp.name, "all_jobs.json")) as f:
            return json.load(f)["jobs"]

    def test_canonical_identity_refreshes_and_preserves_first_seen(self):
        sj._merge_into_all_jobs([role("https://example.com/job/1?source=a", feeds=["general"])])
        first = self.read_master()[0]["first_seen"]
        sj._merge_into_all_jobs([role(
            "https://example.com/job/1?source=b", feeds=["hollywood"],
            title="Care Coordinator", salary="$100k",
        )])
        jobs = self.read_master()
        self.assertEqual(len(jobs), 1)
        self.assertEqual(jobs[0]["first_seen"], first)
        self.assertEqual(jobs[0]["title"], "Care Coordinator")
        self.assertEqual(jobs[0]["feeds"], ["general", "hollywood"])

    def test_rejection_removes_only_one_feed(self):
        sj._merge_into_all_jobs([role(feeds=["general", "hollywood"])])
        sj._merge_into_all_jobs([], [{
            "identity": sj._job_identity("https://example.com/job/1"),
            "feed": "general", "reason": "location",
        }])
        self.assertEqual(self.read_master()[0]["feeds"], ["hollywood"])
        sj._merge_into_all_jobs([], [{
            "identity": sj._job_identity("https://example.com/job/1"),
            "feed": "hollywood", "reason": "seniority",
        }])
        self.assertEqual(self.read_master(), [])

    def test_accepted_duplicate_wins_over_same_feed_rejection(self):
        sj._merge_into_all_jobs([role(feeds=["general"])])
        first = self.read_master()[0]["first_seen"]
        sj._merge_into_all_jobs([role(feeds=["general"], salary="$120k")], [{
            "identity": sj._job_identity("https://example.com/job/1"),
            "feed": "general", "reason": "location",
        }])
        self.assertEqual(self.read_master()[0]["first_seen"], first)
        self.assertEqual(self.read_master()[0]["salary"], "$120k")


class RetrievalPolicy(unittest.TestCase):
    @staticmethod
    def card(job_id, title="Care Coordinator"):
        return (
            f'<li><div data-entity-urn="urn:li:jobPosting:{job_id}">'
            f'<h3 class="base-search-card__title">{title}</h3>'
            '<h4 class="base-search-card__subtitle">Acme</h4>'
            '<span class="job-search-card__location">Los Angeles, CA</span>'
            '<time datetime="2026-08-05"></time></div></li>'
        )

    def test_linkedin_advances_by_raw_page_size_and_stops_repeat(self):
        page = "".join(self.card(str(i)) for i in range(10))
        urls = []

        def fake_fetch(url):
            urls.append(url)
            return page

        with patch.object(sj, "LINKEDIN_LOCATIONS", [("Los Angeles, CA", "1")]), \
             patch.object(sj, "fetch", side_effect=fake_fetch), \
             patch.object(sj.time, "sleep"):
            jobs, raw = sj._linkedin_search(["marketing coordinator"], 3600)
        self.assertEqual(len(jobs), 10)
        self.assertEqual(raw, 20)  # repeated page is counted as received raw data
        self.assertEqual([re.search(r"start=(\d+)", u).group(1) for u in urls], ["0", "10"])

    def test_jobspy_retries_only_on_exactly_fifty(self):
        calls = []

        def fake(**kwargs):
            calls.append(kwargs["results_wanted"])
            return list(range(kwargs["results_wanted"]))

        self.assertEqual(len(sj._jobspy_fetch_with_retry(fake, site_name=["indeed"])), 100)
        self.assertEqual(calls, [50, 100])
        calls.clear()

        def short(**kwargs):
            calls.append(kwargs["results_wanted"])
            return list(range(49))

        self.assertEqual(len(sj._jobspy_fetch_with_retry(short)), 49)
        self.assertEqual(calls, [50])

    def test_jobspy_metro_radii(self):
        self.assertEqual(sj.JOBSPY_LOCATIONS, [
            ("San Francisco, CA", 50), ("Remote", 50),
        ])


class RefilterCommand(unittest.TestCase):
    def test_preview_is_read_only_and_write_preserves_first_seen(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "all_jobs.json")
            payload = {"updated_at": "old", "jobs": [
                role("https://x/keep", first_seen="2026-08-01T00:00:00Z"),
                role("https://x/drop", title="Senior Manager, Client Services",
                     first_seen="2026-08-01T00:00:00Z"),
            ]}
            with open(path, "w") as f:
                json.dump(payload, f)
            with open(path) as f:
                before = f.read()
            with patch.object(sj, "SCRIPT_DIR", tmp):
                sj.refilter_existing_outputs(write=False)
                with open(path) as f:
                    self.assertEqual(f.read(), before)
                sj.refilter_existing_outputs(write=True)
            with open(path) as f:
                jobs = json.load(f)["jobs"]
            self.assertEqual(len(jobs), 1)
            self.assertEqual(jobs[0]["first_seen"], "2026-08-01T00:00:00Z")

    def test_master_migration_uses_hollywood_source_url_provenance(self):
        job = role("https://unknown-clinic.example/job/1", location="Emeryville, CA")
        kept, stats = sj._refilter_master_jobs(
            [job], {sj._job_identity(job["url"])})
        self.assertEqual(stats["location"], 0)
        self.assertEqual(kept[0]["feeds"], ["hollywood"])


class RegistrySaveIntegration(unittest.TestCase):
    def test_mixed_feeds_and_per_board_baseline_marker(self):
        rows = [
            role("https://registry/quiet", location="Oakland, CA", ats="Greenhouse",
                 feeds=["hollywood"], registry_notify_eligible=False),
            role("https://registry/loud", location="San Jose, CA", ats="Lever",
                 feeds=["general"], registry_notify_eligible=True),
        ]
        with tempfile.TemporaryDirectory() as tmp, \
             patch.object(sj, "SCRIPT_DIR", tmp), \
             patch("notify.notify_new_jobs") as mocked_notify:
            sj.save_jobs_output(
                rows, basename="registry_jobs", title="Registry", subtitle="Test",
                accent="#000", empty_message="Empty", window_label="test",
                default_feed="general",
            )
            with open(os.path.join(tmp, "registry_jobs.json")) as f:
                saved = json.load(f)["jobs"]
        self.assertEqual([j["feeds"] for j in saved], [["hollywood"], ["general"]])
        self.assertTrue(all("registry_notify_eligible" not in j for j in saved))
        notified = mocked_notify.call_args.args[0]
        self.assertEqual([j["url"] for j in notified], ["https://registry/loud"])


if __name__ == "__main__":
    unittest.main()

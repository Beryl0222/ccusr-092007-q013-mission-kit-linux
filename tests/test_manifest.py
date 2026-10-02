"""清单加载与 schema 迁移。"""

import json
import tempfile
import unittest
from pathlib import Path

from helpers import FIXTURE  # noqa: E402
from mission_kit import ManifestError, load_manifest, load_record


class ManifestTest(unittest.TestCase):
    def test_fixture_loads_with_cases_batches_devices(self):
        manifest = load_manifest(FIXTURE)
        self.assertEqual(manifest.mission_id, "EYE-2026-09-KGL")
        self.assertEqual(manifest.cases["CASE-01"].seal_id, "SEAL-9001")
        self.assertEqual(manifest.batches["BATCH-IOL-2605"].lot, "L2605")
        self.assertEqual(manifest.batches["BATCH-IOL-2605"].expiry, "2027-05-31")
        self.assertEqual(manifest.devices["DEV-TONO-01"].software_version, "3.2.1")
        self.assertEqual(manifest.handover_languages, ("zh", "en"))

    def test_v1_record_rejected_by_manifest_loader(self):
        v1 = {
            "schema_version": 1,
            "record_id": "sample-013",
            "domain": "mission_kit",
            "occurred_at": "2026-09-20T09:00:00+08:00",
            "revision": 1,
            "source": "业务样例",
        }
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "v1.json"
            path.write_text(json.dumps(v1, ensure_ascii=False), encoding="utf-8")
            with self.assertRaises(ManifestError):
                load_manifest(path)
            # 但登记头合同对 v1/v2 都可读
            record = load_record(path)
            self.assertEqual(record.domain, "mission_kit")

    def test_record_loader_ignores_v2_extension_fields(self):
        record = load_record(FIXTURE)
        self.assertEqual(record.record_id, "sample-013")
        self.assertEqual(record.occurred_at, "2026-09-20T09:00:00+08:00")


if __name__ == "__main__":
    unittest.main()

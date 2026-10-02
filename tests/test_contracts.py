import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[1] / "src"))
from mission_kit import CURRENT_SCHEMA_VERSION, load_record, migrate_envelope


class ContractTest(unittest.TestCase):
    def test_example_uses_current_contract(self):
        item = load_record(
            Path(__file__).parents[1] / "fixtures" / "equipment_handoff.json"
        )
        self.assertEqual(item.domain, "mission_kit")
        self.assertGreater(item.revision, 0)

    def test_v1_envelope_migrates_to_v2_without_touching_metadata(self):
        v1_path = Path(__file__).parents[1] / "fixtures" / "equipment_handoff.json"
        payload = json.loads(v1_path.read_text(encoding="utf-8"))
        migrated = migrate_envelope(payload)
        self.assertEqual(migrated["schema_version"], CURRENT_SCHEMA_VERSION)
        # 既有标识与时间含义逐字段保留。
        for key in ("record_id", "domain", "occurred_at", "revision", "source"):
            self.assertEqual(migrated[key], payload[key])
        self.assertEqual(migrated["events"], [])

    def test_migration_is_idempotent_at_v2(self):
        payload = {"schema_version": 2, "record_id": "x", "domain": "mission_kit",
                   "occurred_at": "2026-09-20T09:00:00+08:00", "revision": 2,
                   "source": "s", "events": [{"type": "x"}]}
        self.assertEqual(migrate_envelope(payload)["events"], [{"type": "x"}])


if __name__ == "__main__":
    unittest.main()

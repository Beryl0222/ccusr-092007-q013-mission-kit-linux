"""设备启用核查：校准、软件版本、操作者资质与按台隔离。"""

import unittest

from helpers import T, arrive_and_unpack, make_ledger


class ActivationTest(unittest.TestCase):
    def setUp(self):
        self.ledger = arrive_and_unpack(make_ledger())

    def test_activation_allowed_when_all_checks_pass(self):
        _, outcome = self.ledger.activate_device("DEV-TONO-01", "OP-01", T("21T11:00:00"))
        self.assertTrue(outcome["allowed"])
        self.assertEqual(outcome["reasons"], [])

    def test_expired_calibration_denies_and_isolates_only_that_device(self):
        # 裂隙灯校准 2026-09-15 到期，行动期间已失效
        _, outcome = self.ledger.activate_device("DEV-SLIT-01", "OP-01", T("21T11:00:00"))
        self.assertFalse(outcome["allowed"])
        self.assertIn("calibration_expired", outcome["reasons"])
        self.assertEqual(self.ledger.state.devices["DEV-SLIT-01"].state, "quarantined")
        # 只隔离这台设备：眼压计照常启用，耗材照常领用
        _, other = self.ledger.activate_device("DEV-TONO-01", "OP-01", T("21T11:05:00"))
        self.assertTrue(other["allowed"])
        result = self.ledger.issue("BATCH-SWAB-0422", 10, "OP-01", "诊室A", T("21T11:10:00"))
        self.assertFalse(result.rejected)

    def test_unqualified_operator_denied_without_quarantine(self):
        _, outcome = self.ledger.activate_device("DEV-TONO-01", "OP-02", T("21T11:00:00"))
        self.assertFalse(outcome["allowed"])
        self.assertEqual(outcome["reasons"], ["operator_not_qualified"])
        # 资质问题不怀疑设备本身，设备保持可用
        self.assertEqual(self.ledger.state.devices["DEV-TONO-01"].state, "warehouse")

    def test_unknown_operator_denied(self):
        _, outcome = self.ledger.activate_device("DEV-TONO-01", "OP-99", T("21T11:00:00"))
        self.assertFalse(outcome["allowed"])
        self.assertIn("operator_not_qualified", outcome["reasons"])

    def test_quarantined_device_reports_reason(self):
        self.ledger.quarantine(["DEV-TONO-01"], "跌落待检", T("21T10:30:00"))
        _, outcome = self.ledger.activate_device("DEV-TONO-01", "OP-01", T("21T11:00:00"))
        self.assertFalse(outcome["allowed"])
        self.assertIn("device_quarantined", outcome["reasons"])

    def test_device_room_rotation(self):
        le = self.ledger
        le.assign_device("DEV-TONO-01", "诊室A", "OP-01", T("21T11:00:00"))
        self.assertEqual(le.state.devices["DEV-TONO-01"].location, "room:诊室A")
        le.return_device("DEV-TONO-01", T("21T18:00:00"))
        le.assign_device("DEV-TONO-01", "诊室B", "OP-01", T("22T09:00:00"))
        status = le.state.devices["DEV-TONO-01"]
        self.assertEqual((status.state, status.location), ("room", "room:诊室B"))


if __name__ == "__main__":
    unittest.main()

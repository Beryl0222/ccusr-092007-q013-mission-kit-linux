import unittest

from _helpers import build_service, register_standard_box, register_second_box, ts
from mission_kit import ComplianceHold


class DeviceComplianceTest(unittest.TestCase):
    def setUp(self):
        self.svc = build_service()
        register_standard_box(self.svc)
        register_second_box(self.svc)
        self.svc.verify_seal(container_id="BOX-1", seal_id="SEAL-A001", intact=True,
                             actor="logistics", occurred_at=ts(1))
        self.svc.open_box(container_id="BOX-1", seal_id="SEAL-A001", room="诊室A",
                          actor="nurse-li", occurred_at=ts(2))

    def _register_operator(self, *, certs=(("OCT-CERT", "2027-01-01"),)):
        self.svc.register_operator(
            operator_id="OP-01", name="王医生", role="ophthalmologist",
            certifications=[{"cert_id": c, "expires": e} for c, e in certs],
            actor="admin", occurred_at=ts(3),
        )

    def test_enable_with_all_checks_passing(self):
        self._register_operator()
        event, _ = self.svc.enable_device(serial="DEV-SCAN-01", operator_id="OP-01",
                                          room="诊室A", actor="OP-01",
                                          occurred_at=ts(4))
        self.assertEqual(event.type, "device_session_started")
        self.assertEqual(self.svc.inventory.devices["DEV-SCAN-01"].state, "in_use")

    def test_expired_calibration_blocks_and_isolates_only_device(self):
        self._register_operator()
        self.svc.record_calibration(serial="DEV-SCAN-01", calibration_due="2026-08-01",
                                    certificate_ref="CAL-OLD", actor="biomed",
                                    occurred_at=ts(3))
        with self.assertRaises(ComplianceHold) as ctx:
            self.svc.enable_device(serial="DEV-SCAN-01", operator_id="OP-01",
                                   room="诊室A", actor="OP-01", occurred_at=ts(4))
        self.assertTrue(any("校准" in f for f in ctx.exception.failures))
        # 设备被隔离，且只连坐其登记附件 ITM-PROBE。
        self.assertEqual(self.svc.inventory.devices["DEV-SCAN-01"].state, "quarantined")
        self.assertEqual(self.svc.inventory.items["ITM-PROBE"].quarantined, 20)
        # 同诊室无关耗材不受影响，其他诊疗继续。
        self.assertEqual(self.svc.inventory.items["ITM-GAUZE"].quarantined, 0)
        self.svc.issue(items=[{"item_id": "ITM-GAUZE", "qty": 5}],
                       recipient="dr-wang", role="doctor", room="诊室A",
                       actor="nurse-li", occurred_at=ts(5))

    def test_wrong_software_blocks(self):
        self._register_operator()
        self.svc.verify_software(serial="DEV-SCAN-01", version="3.9.0",
                                 actor="biomed", occurred_at=ts(3))
        with self.assertRaises(ComplianceHold) as ctx:
            self.svc.enable_device(serial="DEV-SCAN-01", operator_id="OP-01",
                                   room="诊室A", actor="OP-01", occurred_at=ts(4))
        self.assertTrue(any("软件版本" in f for f in ctx.exception.failures))

    def test_missing_and_expired_operator_cert_blocks(self):
        self._register_operator(certs=(("OTHER-CERT", "2027-01-01"),))
        with self.assertRaises(ComplianceHold) as ctx:
            self.svc.enable_device(serial="DEV-SCAN-01", operator_id="OP-01",
                                   room="诊室A", actor="OP-01", occurred_at=ts(4))
        self.assertTrue(any("OCT-CERT" in f for f in ctx.exception.failures))

        self.svc.register_operator(
            operator_id="OP-02", name="实习生", role="resident",
            certifications=[{"cert_id": "OCT-CERT", "expires": "2026-01-01"}],
            actor="admin", occurred_at=ts(5),
        )
        with self.assertRaises(ComplianceHold) as ctx:
            self.svc.enable_device(serial="DEV-SCAN-01", operator_id="OP-02",
                                   room="诊室A", actor="OP-02", occurred_at=ts(6))
        self.assertTrue(any("到期" in f for f in ctx.exception.failures))

    def test_unknown_operator_blocked_but_no_session_written(self):
        before = len(self.svc.store.events)
        with self.assertRaises(ComplianceHold):
            self.svc.enable_device(serial="DEV-SCAN-01", operator_id="NOBODY",
                                   room="诊室A", actor="NOBODY", occurred_at=ts(4))
        types_after = [e.type for e in self.svc.store.events[before:]]
        self.assertNotIn("device_session_started", types_after)
        # 只新增了隔离事件。
        self.assertEqual(types_after, ["item_quarantined"])

    def test_fixed_calibration_allows_release_and_enable(self):
        self._register_operator()
        self.svc.record_calibration(serial="DEV-SCAN-01", calibration_due="2026-08-01",
                                    certificate_ref="CAL-OLD", actor="biomed",
                                    occurred_at=ts(3))
        with self.assertRaises(ComplianceHold):
            self.svc.enable_device(serial="DEV-SCAN-01", operator_id="OP-01",
                                   room="诊室A", actor="OP-01", occurred_at=ts(4))
        # 重新校准并解除隔离后可正常启用。
        self.svc.record_calibration(serial="DEV-SCAN-01", calibration_due="2027-08-01",
                                    certificate_ref="CAL-NEW", actor="biomed",
                                    occurred_at=ts(5))
        self.svc.release_hold(device_serials=["DEV-SCAN-01"], actor="qa",
                              occurred_at=ts(6))
        self.svc.enable_device(serial="DEV-SCAN-01", operator_id="OP-01",
                               room="诊室A", actor="OP-01", occurred_at=ts(7))
        self.assertEqual(self.svc.inventory.devices["DEV-SCAN-01"].state, "in_use")


if __name__ == "__main__":
    unittest.main()

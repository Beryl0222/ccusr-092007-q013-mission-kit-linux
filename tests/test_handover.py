"""留置交接：双语条款、培训、双方责任确认与凭证。"""

import unittest

from helpers import T, arrive_and_unpack, make_ledger

TERMS = {
    "zh": "设备与耗材留置当地，接收方负责后续维护、校准与使用。",
    "en": "Assets are donated; the recipient assumes maintenance, calibration and use.",
}


class HandoverTest(unittest.TestCase):
    def setUp(self):
        self.ledger = arrive_and_unpack(make_ledger())

    def _announce(self, handover_id="HO-01", items=("DEV-TONO-01", "BATCH-SWAB-0422")):
        return self.ledger.announce_handover(
            handover_id, list(items), "基加利市立医院", TERMS, T("25T09:00:00"))

    def test_handover_requires_all_languages(self):
        result = self.ledger.announce_handover(
            "HO-01", ["DEV-TONO-01"], "基加利市立医院",
            {"zh": "只有中文条款"}, T("25T09:00:00"))
        self.assertTrue(result.rejected)
        self.assertIn("missing_languages", result.reason)
        self.assertIn("en", result.reason)

    def test_full_handover_flow(self):
        le = self.ledger
        self.assertFalse(self._announce().rejected)
        # 培训之前不能完成
        early = le.complete_handover("HO-01", "CERT-H-01", "光明行眼科志愿队", T("25T10:00:00"))
        self.assertTrue(early.rejected)
        self.assertEqual(early.reason, "training_missing")
        le.log_training("HO-01", "当地技师A", "眼压计日常维护", T("25T10:30:00"))
        le.confirm_handover("HO-01", "donor", "陈医生", "领队", T("25T11:00:00"))
        # 双方确认缺一不可
        half = le.complete_handover("HO-01", "CERT-H-01", "光明行眼科志愿队", T("25T11:05:00"))
        self.assertTrue(half.rejected)
        self.assertIn("confirmation_missing", half.reason)
        le.confirm_handover("HO-01", "recipient", "M. Uwase", "设备科主任", T("25T11:10:00"))
        done = le.complete_handover("HO-01", "CERT-H-01", "光明行眼科志愿队", T("25T11:15:00"))
        self.assertFalse(done.rejected)

        self.assertEqual(le.state.devices["DEV-TONO-01"].state, "handed_over")
        self.assertEqual(le.state.terminal["BATCH-SWAB-0422"]["handed_over"], 200)
        cert = le.state.certificates["CERT-H-01"]
        self.assertEqual(cert.kind, "handover")
        self.assertEqual(cert.detail, "基加利市立医院")
        self.assertEqual(len(cert.digest), 64)
        for row in le.conservation():
            self.assertEqual(row["variance"], 0, row)

    def test_handover_blocked_while_items_outstanding(self):
        le = self.ledger
        le.issue("BATCH-SWAB-0422", 10, "OP-01", "诊室A", T("22T09:00:00"))
        self._announce("HO-02", ("BATCH-SWAB-0422",))
        le.log_training("HO-02", "当地技师A", "耗材管理", T("25T10:00:00"))
        le.confirm_handover("HO-02", "donor", "陈医生", "领队", T("25T11:00:00"))
        le.confirm_handover("HO-02", "recipient", "M. Uwase", "设备科主任", T("25T11:10:00"))
        # 仍有 10 片在领用人手中，须先清账（归还、消耗或报损）才能交接
        result = le.complete_handover("HO-02", "CERT-H-02", "光明行眼科志愿队", T("25T11:15:00"))
        self.assertTrue(result.rejected)
        self.assertEqual(result.reason, "items_outstanding")

    def test_ship_back_and_destroy_keep_certificates(self):
        le = self.ledger
        le.report_damage("BATCH-SWAB-0422", 5, "包装受潮", T("22T09:00:00"))
        back = le.ship_back(["BATCH-VISCO-0311"], "LEG-1", "CERT-R-01",
                            "光明行眼科志愿队", T("25T09:00:00"))
        self.assertFalse(back.rejected)
        gone = le.destroy(["BATCH-SWAB-0422"], "CERT-D-01", "持证销毁商",
                          T("25T10:00:00"), detail="高温焚烧")
        self.assertFalse(gone.rejected)
        self.assertEqual(le.state.terminal["BATCH-VISCO-0311"]["shipped_back"], 50)
        self.assertEqual(le.state.terminal["BATCH-SWAB-0422"]["destroyed"], 200)
        kinds = {c.kind for c in le.state.certificates.values()}
        self.assertEqual(kinds, {"return_shipment", "destruction"})
        for row in le.conservation():
            self.assertEqual(row["variance"], 0, row)


if __name__ == "__main__":
    unittest.main()

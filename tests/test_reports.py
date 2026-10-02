"""查询与结存复算：在哪里、能否用、谁接管；复算守恒且不暴露患者详情。"""

import json
import unittest

from helpers import T, arrive_and_unpack, make_ledger
from mission_kit import reports


class ReportsTest(unittest.TestCase):
    def setUp(self):
        le = arrive_and_unpack(make_ledger())
        le.split_batch("BATCH-IOL-2605", "BATCH-IOL-2605-A", 30, T("21T11:00:00"))
        le.issue("BATCH-IOL-2605", 40, "OP-01", "诊室A", T("21T13:00:00"))
        le.consume("BATCH-IOL-2605", 5, "OP-01", "诊室A", "u#7f3a", T("21T14:00:00"))
        le.issue("BATCH-IOL-2605-A", 30, "OP-02", "诊室B", T("22T09:00:00"))
        le.consume("BATCH-IOL-2605-A", 2, "OP-02", "诊室B", "u#51c0", T("22T10:00:00"))
        le.assign_device("DEV-TONO-01", "诊室A", "OP-01", T("22T09:00:00"))
        self.ledger = le

    def test_locate_answers_where(self):
        info = reports.locate(self.ledger, "BATCH-IOL-2605")
        self.assertEqual(info["kind"], "batch")
        spots = {(h["state"], h["location"], h["custodian"]): h["quantity"]
                 for h in info["holdings"]}
        self.assertEqual(spots[("stock", "warehouse", None)], 30)
        self.assertEqual(spots[("issued", "room:诊室A", "OP-01")], 35)
        self.assertEqual(info["terminal"], {"consumed": 5})
        dev = reports.locate(self.ledger, "DEV-TONO-01")
        self.assertEqual((dev["state"], dev["location"], dev["custodian"]),
                         ("room", "room:诊室A", "OP-01"))

    def test_usability_answers_whether_usable(self):
        ok = reports.usability(self.ledger, "BATCH-IOL-2605", T("22T12:00:00"))
        self.assertTrue(ok["usable"])
        self.assertEqual(ok["usable_quantity"], 65)  # 库房 30 + 诊室 35
        expired = reports.usability(self.ledger, "BATCH-IOL-2605",
                                    "2027-06-02T00:00:00+08:00")
        self.assertFalse(expired["usable"])
        self.assertIn("expired", expired["reasons"])
        dev = reports.usability(self.ledger, "DEV-SLIT-01", T("22T12:00:00"))
        self.assertFalse(dev["usable"])
        self.assertIn("calibration_expired", dev["reasons"])

    def test_custodian_answers_who_takes_over(self):
        who = reports.custodian_of(self.ledger, "BATCH-IOL-2605")
        self.assertEqual(who["custodians"], ["OP-01"])
        who_dev = reports.custodian_of(self.ledger, "DEV-TONO-01")
        self.assertEqual(who_dev["custodians"], ["OP-01"])

    def test_closing_balance_conserves_and_hides_patient_detail(self):
        le = self.ledger
        le.announce_handover("HO-01", ["DEV-TONO-01"], "基加利市立医院",
                             {"zh": "设备留置，接收方负责维护。",
                              "en": "Device donated; recipient maintains it."},
                             T("25T09:00:00"))
        le.log_training("HO-01", "当地技师A", "眼压计维护", T("25T10:00:00"))
        le.confirm_handover("HO-01", "donor", "陈医生", "领队", T("25T11:00:00"))
        le.confirm_handover("HO-01", "recipient", "M. Uwase", "设备科主任", T("25T11:10:00"))
        le.complete_handover("HO-01", "CERT-H-01", "光明行眼科志愿队", T("25T11:15:00"))
        le.ship_back(["BATCH-VISCO-0311"], "LEG-1", "CERT-R-01",
                     "光明行眼科志愿队", T("25T12:00:00"))

        balance = reports.closing_balance(le)
        self.assertTrue(balance["conservation_ok"])
        self.assertEqual(balance["discrepancies"], [])
        root = next(r for r in balance["lineages"] if r["lineage_root"] == "BATCH-IOL-2605")
        self.assertEqual(root["initial"], 100)
        self.assertEqual(root["live"] + root["terminal"], 100)
        handed = reports.custodian_of(le, "DEV-TONO-01")
        self.assertEqual(handed["terminal"]["state"], "handover")
        # 隐私：复算结果不含任何脱敏用途令牌或患者诊疗字段
        blob = json.dumps(balance, ensure_ascii=False)
        self.assertNotIn("u#7f3a", blob)
        self.assertNotIn("u#51c0", blob)
        self.assertNotIn("usage_ref", blob)


if __name__ == "__main__":
    unittest.main()

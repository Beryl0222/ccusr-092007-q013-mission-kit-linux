import json
import tempfile
import unittest
from pathlib import Path

from _helpers import register_second_box, register_standard_box, ts
from mission_kit import (
    ComplianceHold,
    DuplicateSubmission,
    EventStore,
    Inventory,
    MissionService,
)


class EndToEndMissionTest(unittest.TestCase):
    def test_full_mission_then_restart_and_answer_questions(self):
        with tempfile.TemporaryDirectory() as tmp:
            ledger_path = Path(tmp) / "mission.jsonl"

            # ---- 出境运输：两网关重复推送同一航段到达 -------------- #
            svc = MissionService(EventStore(ledger_path), Inventory())
            register_standard_box(svc)
            register_second_box(svc)
            notice = dict(
                kind="leg_arrived",
                fingerprint_parts=["leg-2", "BOX-1", "arrived"],
                payload={"location": "当地机场", "container_ids": ["BOX-1"]},
            )
            svc.transport_notice(actor="gw-a", occurred_at=ts(10), **notice)
            with self.assertRaises(DuplicateSubmission):
                svc.transport_notice(
                    actor="gw-b", occurred_at="2026-09-20T06:00:00+04:00", **notice
                )
            self.assertEqual(svc.inventory.where_is("BOX-1")["location"], "当地机场")

            # ---- 入库拆箱 ------------------------------------------ #
            svc.verify_seal(container_id="BOX-1", seal_id="SEAL-A001", intact=True,
                            actor="logistics", occurred_at=ts(20))
            svc.verify_seal(container_id="BOX-2", seal_id="SEAL-B002", intact=True,
                            actor="logistics", occurred_at=ts(20))
            svc.open_box(container_id="BOX-1", seal_id="SEAL-A001", room="诊室一",
                         actor="nurse-li", occurred_at=ts(21))
            svc.open_box(container_id="BOX-2", seal_id="SEAL-B002", room="设备间",
                         actor="nurse-li", occurred_at=ts(22))

            # ---- 扫码离线补传：同一领用不重复扣库存 ---------------- #
            issue_kw = dict(items=[{"item_id": "ITM-GAUZE", "qty": 8}],
                            recipient="王医生", role="ophthalmologist", room="诊室一",
                            actor="scanner-3")
            svc.issue(occurred_at=ts(30), client_event_id="ISS-1001", **issue_kw)
            with self.assertRaises(DuplicateSubmission):
                svc.issue(occurred_at=ts(95), client_event_id="ISS-1001", **issue_kw)
            svc.record_use(item_id="ITM-GAUZE", qty=8, actor="王医生",
                           encounter_ref="ENC-ZZ01", occurred_at=ts(31),
                           client_event_id="USE-1001")
            with self.assertRaises(DuplicateSubmission):
                svc.record_use(item_id="ITM-GAUZE", qty=8, actor="王医生",
                               encounter_ref="ENC-ZZ01", occurred_at=ts(96),
                               client_event_id="USE-1001")

            # ---- 设备首次启用校准过期：仅隔离设备与探头套 ---------- #
            svc.register_operator(
                operator_id="OP-W", name="王医生", role="ophthalmologist",
                certifications=[{"cert_id": "OCT-CERT", "expires": "2027-12-01"}],
                actor="admin", occurred_at=ts(25),
            )
            svc.record_calibration(serial="DEV-SCAN-01", calibration_due="2026-09-01",
                                   certificate_ref="CAL-OLD", actor="biomed",
                                   occurred_at=ts(26))
            with self.assertRaises(ComplianceHold):
                svc.enable_device(serial="DEV-SCAN-01", operator_id="OP-W",
                                  room="诊室一", actor="OP-W", occurred_at=ts(32))
            self.assertEqual(svc.inventory.items["ITM-PROBE"].quarantined, 20)
            # 纱布等无关诊疗未受影响（8 包已消耗，剩余 42 可用）。
            self.assertEqual(svc.inventory.items["ITM-GAUZE"].available, 42)

            # ---- 重新校准、解除隔离、正常使用 ---------------------- #
            svc.record_calibration(serial="DEV-SCAN-01", calibration_due="2027-09-01",
                                   certificate_ref="CAL-NEW", actor="biomed",
                                   occurred_at=ts(40))
            svc.release_hold(device_serials=["DEV-SCAN-01"], actor="qa",
                             occurred_at=ts(41))
            svc.release_hold(item_id="ITM-PROBE", actor="qa", occurred_at=ts(42))
            svc.enable_device(serial="DEV-SCAN-01", operator_id="OP-W",
                              room="诊室一", actor="OP-W", occurred_at=ts(43))
            svc.end_session(serial="DEV-SCAN-01", actor="OP-W", occurred_at=ts(60))

            # ---- 留置：双语文档 + 培训 + 责任确认 ------------------ #
            svc.record_handover_docs(serial="DEV-SCAN-01",
                                     documents={"zh": "DOC-ZH", "en": "DOC-EN"},
                                     actor="lead", occurred_at=ts(70))
            svc.complete_training(serial="DEV-SCAN-01", trainer="biomed-zhou",
                                  trainees=["dr-ahmed"], session_ref="TRN-1",
                                  actor="biomed-zhou", occurred_at=ts(71))
            svc.accept_responsibility(serial="DEV-SCAN-01", owner_name="Dr. Ahmed",
                                      owner_org="当地眼科医院", signoff_ref="SGN-1",
                                      actor="lead", occurred_at=ts(72))
            svc.retain_asset(serial="DEV-SCAN-01", location="当地眼科医院设备科",
                             actor="lead", occurred_at=ts(73))

            # ---- 返运剩余手套；销毁 2 包污染纱布 ------------------- #
            svc.write_off(item_id="ITM-GAUZE", qty=2, reason="污染",
                          witness="nurse-fatima", actor="nurse-li", occurred_at=ts(80))
            svc.ship_back(waybill="AWB-1", customs_declaration="CUS-OUT",
                          items=[{"item_id": "ITM-GLOVE", "qty": 70},
                                 {"item_id": "ITM-PROBE", "qty": 20}],
                          device_serials=[], actor="logistics", occurred_at=ts(81))
            svc.destroy(items=[{"item_id": "ITM-GAUZE", "qty": 40}],
                        device_serials=[], certificate_ref="DES-1",
                        witness="officer", actor="logistics", occurred_at=ts(82))

            report = svc.inventory.reconcile()
            self.assertTrue(report["balanced"], msg=json.dumps(report, ensure_ascii=False))
            self.assertEqual(report["registered_total"], 170)  # 100+50+20，无重复
            answers_before = {
                "scanner": svc.inventory.where_is("DEV-SCAN-01"),
                "scanner_owner": svc.inventory.custodian("DEV-SCAN-01"),
                "gauze": svc.inventory.usable("ITM-GAUZE", as_of="2026-09-20"),
            }
            events_on_disk = len(EventStore(ledger_path).events)
            self.assertEqual(events_on_disk, len(svc.store.events))

            # ---- 返程后换一台机器重放：回答必须一致 ---------------- #
            svc2 = MissionService.rebuild(EventStore(ledger_path))
            self.assertEqual(svc2.inventory.where_is("DEV-SCAN-01"),
                             answers_before["scanner"])
            self.assertEqual(
                svc2.inventory.custodian("DEV-SCAN-01")["responsibility"]["owner_name"],
                "Dr. Ahmed",
            )
            self.assertTrue(svc2.inventory.reconcile()["balanced"])
            gauze = svc2.inventory.items["ITM-GAUZE"]
            self.assertEqual((gauze.consumed, gauze.written_off, gauze.destroyed),
                             (8, 2, 40))
            # 结存序列化后不含任何就诊代号。
            self.assertNotIn("ENC-ZZ01",
                             json.dumps(svc2.inventory.reconcile(), ensure_ascii=False))


if __name__ == "__main__":
    unittest.main()

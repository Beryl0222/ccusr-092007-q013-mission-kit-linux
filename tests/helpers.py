"""测试共用的搭建助手：从样例清单建账并完成到港拆箱。"""

from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).parents[1] / "src"))

from mission_kit import Ledger, load_manifest

FIXTURE = Path(__file__).parents[1] / "fixtures" / "equipment_handoff.json"

T = lambda s: f"2026-09-{s}+08:00"  # 行动期间统一使用东八区


def make_ledger(store=None) -> Ledger:
    return Ledger(load_manifest(FIXTURE), store=store)


def arrive_and_unpack(ledger: Ledger) -> Ledger:
    """两个箱件经两个航段到达目的港并拆箱入库。"""

    for case_id in ("CASE-01", "CASE-02"):
        ledger.notify_leg_arrival("LEG-1", case_id, T("20T10:00:00"))
        ledger.notify_leg_arrival("LEG-2", case_id, T("21T08:00:00"))
        ledger.unpack(case_id, T("21T10:00:00"))
    return ledger

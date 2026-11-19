"""引擎规则单测：python -I -m unittest tests.test_engine -v"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from engine import Engine, EngineError, split_address  # noqa: E402


class SplitTest(unittest.TestCase):
    def test_split_ok(self):
        self.assertEqual(split_address("栖原市港区临港大道188号"),
                         ("栖原市港区临港大道", "188号"))
        self.assertEqual(split_address("梧桐路 12栋3单元502室"),
                         ("梧桐路", "12栋3单元502室"))

    def test_split_fail(self):
        self.assertIsNone(split_address("港区三号仓库门卫代收"))
        self.assertIsNone(split_address(""))


class EngineRuleTest(unittest.TestCase):
    def setUp(self):
        self.e = Engine()
        self.e.switch_mode("v2")
        r = self.e.create_order("栖原市港区临港大道188号", "rid-create")
        self.oid = r["body"]["order_id"]

    # 规则 1：并发 CAS —— 旧端迟到整段写（版本过期）不能覆盖新端已写
    def test_concurrent_cas(self):
        self.e.migrate_order(self.oid, "rid-mig")
        # 两端都读到 v1
        old_resp = self.e.write("old", self.oid, "栖原市港区临港大道999号", None, 1, "rid-old")
        self.assertEqual(old_resp["status"], 200)
        # 新端也基于 v1 写门牌 -> 版本冲突
        conflict = self.e.write("new", self.oid, "188号", "house", 1, "rid-new-stale")
        self.assertEqual(conflict["status"], 409)
        self.assertEqual(conflict["body"]["error"], "version_conflict")
        # 新端重读拿 v2 再写 -> 成功
        ok = self.e.write("new", self.oid, "188号", "house", 2, "rid-new-fresh")
        self.assertEqual(ok["status"], 200)
        self.assertTrue(ok["body"]["house_confirmed"])

    # 规则 2：已确认门牌，迟到旧写被 house_locked 拒绝
    def test_locked_house_rejects_late_legacy_write(self):
        self.e.migrate_order(self.oid, "rid-mig2")
        self.e.write("new", self.oid, "188号", "house", 1, "rid-confirm")
        late = self.e.write("old", self.oid, "栖原市港区临港大道1号", None, 2, "rid-late")
        self.assertEqual(late["status"], 409)
        self.assertEqual(late["body"]["error"], "house_locked")
        view = self.e._view(self.oid)
        self.assertEqual(view["house"], "188号")  # 确认值未被盖掉

    # 规则 3：幂等重试只生效一次（含响应丢失场景）
    def test_idempotent_retry_and_lost_response(self):
        self.e.migrate_order(self.oid, "rid-mig3")
        lost = self.e.write("new", self.oid, "188号", "house", 1,
                            "rid-lost", simulate_lost=True)
        self.assertEqual(lost["status"], 503)
        v_after_lost = self.e._view(self.oid)["version"]
        # 同一 request_id 重试：不重复生效，版本不再增长
        retry = self.e.write("new", self.oid, "188号", "house", 1, "rid-lost")
        self.assertTrue(retry["replayed"])
        self.assertEqual(self.e._view(self.oid)["version"], v_after_lost)
        # 再来一次仍是同一响应
        retry2 = self.e.write("new", self.oid, "188号", "house", 1, "rid-lost")
        self.assertTrue(retry2["replayed"])
        self.assertEqual(self.e._view(self.oid)["version"], v_after_lost)

    # 规则 4：无法拆分 -> 暂存（不拒收），影响面：接单保留、新端读冲突、可人工处置
    def test_unsplittable_stash_flow(self):
        bad = self.e.create_order("港区三号仓库门卫代收", "rid-bad")
        bid = bad["body"]["order_id"]
        self.assertEqual(bad["status"], 201)  # 接单不受影响
        mig = self.e.migrate_order(bid, "rid-mig-bad")
        self.assertEqual(mig["status"], 202)
        self.assertEqual(mig["body"]["state"], "stashed")
        # 新端主库读能看到暂存态与原文
        rd = self.e.read_primary(bid, "new")
        self.assertEqual(rd["body"]["state"], "stashed")
        self.assertEqual(rd["body"]["legacy_addr"], "港区三号仓库门卫代收")
        # 人工处置后恢复可读
        fixed = self.e.manual_resolve(bid, "港区仓库路", "3号", "rid-fix")
        self.assertEqual(fixed["body"]["state"], "split")
        self.assertTrue(fixed["body"]["house_confirmed"])

    # 规则 5：副本追上写入但未换格式 -> 新端读有明确去向（回主库）；换格式后可服务
    def test_replica_routing(self):
        self.e.migrate_order(self.oid, "rid-mig4")
        # 副本落后：订单尚不可见
        with self.assertRaises(EngineError) as c:
            self.e.read_replica(self.oid, "new")
        self.assertEqual(c.exception.code, "replica_lagging")
        # 追平写入，但仍是 legacy 格式
        self.e.replica_catchup()
        with self.assertRaises(EngineError) as c:
            self.e.read_replica(self.oid, "new")
        self.assertEqual(c.exception.code, "format_not_migrated")
        self.assertEqual(c.exception.extra["fallback"], "primary")
        # 旧端在副本上照常读整段
        old_rd = self.e.read_replica(self.oid, "old")
        self.assertEqual(old_rd["read_route"]["served_by"], "replica")
        # 换格式后新端可在副本读
        self.e.replica_switch_format("split")
        new_rd = self.e.read_replica(self.oid, "new")
        self.assertEqual(new_rd["body"]["house"], "188号")

    # 规则 6：回退 v1 旧版照常读写；再升级不丢记录，确认门牌保留
    def test_rollback_and_reupgrade_keeps_confirmed_house(self):
        self.e.migrate_order(self.oid, "rid-mig5")
        self.e.write("new", self.oid, "188号", "house", 1, "rid-c5")
        # 回退
        self.e.switch_mode("v1")
        old = self.e.read_primary(self.oid, "old")
        self.assertEqual(old["body"]["address"], "栖原市港区临港大道 188号")
        # v1 下新端写被拒
        blocked = self.e.write("new", self.oid, "1号", "house", 2, "rid-blocked")
        self.assertEqual(blocked["body"]["error"], "new_client_blocked_in_v1")
        # 旧端照常整段写（门牌未确认的另一个订单演示 dirty；本单已确认 -> house_locked）
        locked = self.e.write("old", self.oid, "别的地址8号", None, 2, "rid-old-v1")
        self.assertEqual(locked["body"]["error"], "house_locked")
        # 再升级：事件全在，确认门牌仍保留
        self.e.switch_mode("v2")
        view = self.e._view(self.oid)
        self.assertEqual(view["state"], "split")
        self.assertEqual(view["house"], "188号")
        self.assertTrue(view["house_confirmed"])

    # 规则 6b：回退期旧端改动（未确认门牌的订单）在再升级时被重新转换
    def test_reupgrade_replays_dirty_legacy_write(self):
        r = self.e.create_order("枫林街20号", "rid-o2")
        oid2 = r["body"]["order_id"]
        self.e.migrate_order(oid2, "rid-mig6")          # v2 split, v1, 未确认
        self.e.switch_mode("v1")
        w = self.e.write("old", oid2, "枫林街22号", None, 1, "rid-v1-write")
        self.assertEqual(w["status"], 200)
        self.e.switch_mode("v2")
        view = self.e._view(oid2)
        self.assertEqual(view["state"], "split")
        self.assertEqual(view["house"], "22号")
        self.assertFalse(view["dirty"])

    # 模式闸门：v1 下迁移被拒
    def test_migration_requires_v2(self):
        self.e.switch_mode("v1")
        resp = self.e.migrate_order(self.oid, "rid-x")
        self.assertEqual(resp["body"]["error"], "migration_requires_v2")


if __name__ == "__main__":
    unittest.main(verbosity=2)

"""端到端 API 测试（仅标准库）：python -I tests/e2e_api.py"""
import json
import os
import sys
import urllib.error
import urllib.request

BASE = os.environ.get("BASE_URL", "http://localhost:8080")
PASS, FAIL = 0, 0


def call(path, body=None):
    data = None if body is None else json.dumps(body).encode()
    req = urllib.request.Request(BASE + path, data=data,
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read())


def check(name, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  PASS {name}")
    else:
        FAIL += 1
        print(f"  FAIL {name} {detail}")


def main():
    # 干净起点
    call("/api/reset", {})
    print("== 1. 接单（含无法拆分地址）==")
    s, r = call("/api/orders", {"full_address": "栖原市港区临港大道188号",
                                "request_id": "e-c1"})
    oid1 = r["body"]["order_id"]
    check("正常地址接单 201", s == 201, r)
    s, r = call("/api/orders", {"full_address": "港区三号仓库门卫代收",
                                "request_id": "e-c2"})
    oid2 = r["body"]["order_id"]
    check("无法拆分地址同样接单（不拒收）", s == 201, r)

    print("== 2. 升级 v2 与转换 ==")
    check("升级 v2", call("/api/mode", {"mode": "v2"})[0] == 200)
    s, r = call("/api/migrate", {"order_id": oid1, "request_id": "e-m1"})
    check("正常订单拆分 split", s == 200 and r["body"]["state"] == "split", r)
    check("门牌待确认", r["body"]["house_confirmed"] is False)
    s, r = call("/api/migrate", {"order_id": oid2, "request_id": "e-m2"})
    check("拆不动 -> 202 暂存", s == 202 and r["body"]["state"] == "stashed", r)
    s, r = call("/api/state", )
    check("生成人工任务", any(t["order_id"] == oid2 and t["status"] == "open"
                          for t in r["tasks"]), r["tasks"])

    print("== 3. 并发 CAS + 门牌锁 + 幂等 ==")
    s, r = call("/api/write", {"client": "old", "order_id": oid1,
                               "value": "栖原市港区临港大道999号",
                               "expected_version": 1, "request_id": "e-w1"})
    check("旧端基于 v1 写成功", s == 200 and r["body"]["version"] == 2, r)
    s, r = call("/api/write", {"client": "new", "order_id": oid1, "value": "188号",
                               "field": "house", "expected_version": 1,
                               "request_id": "e-w2"})
    check("新端同版本 -> version_conflict", s == 409
          and r["error"] == "version_conflict", r)
    s, r = call("/api/write", {"client": "new", "order_id": oid1, "value": "188号",
                               "field": "house", "expected_version": 2,
                               "request_id": "e-w3"})
    check("新端按 v2 确认门牌", s == 200 and r["body"]["house_confirmed"], r)
    s, r = call("/api/write", {"client": "old", "order_id": oid1,
                               "value": "栖原市港区临港大道1号",
                               "expected_version": 3, "request_id": "e-w4"})
    check("迟到旧写 -> house_locked", s == 409 and r["error"] == "house_locked", r)
    s, r = call("/api/write", {"client": "new", "order_id": oid1, "value": "188号",
                               "field": "house", "expected_version": 2,
                               "request_id": "e-w3"})
    check("同 request_id 重试 -> 幂等重放",
          s == 200 and r["body"].get("idempotent_replay") is True, r)
    s, state = call("/api/state")
    check("重试未新增版本（仍 v3）",
          next(o for o in state["orders"] if o["id"] == oid1)["version"] == 3)
    check("确认门牌未被任何旧写盖掉",
          next(o for o in state["orders"] if o["id"] == oid1)["house"] == "188号")
    # 拒绝也幂等：重放同一拒绝 request_id
    s, r = call("/api/write", {"client": "old", "order_id": oid1,
                               "value": "x", "expected_version": 3,
                               "request_id": "e-w4"})
    check("拒绝请求重试仍返回同一错误码", s == 409 and r["error"] == "house_locked")

    print("== 4. 响应丢失后重试 ==")
    s, state0 = call("/api/state")
    seq_before = state0["seq"]
    s, r = call("/api/write", {"client": "new", "order_id": oid1,
                               "value": "188号", "field": "house",
                               "expected_version": 3, "request_id": "e-lost",
                               "simulate_lost": True})
    check("首次返回 503 response_lost", s == 503 and r["error"] == "response_lost", r)
    s, state1 = call("/api/state")
    ver_after_lost = next(o for o in state1["orders"]
                          if o["id"] == oid1)["version"]
    check("丢失的写入其实已落库（v3 -> v4）", ver_after_lost == 4, ver_after_lost)
    s, r = call("/api/write", {"client": "new", "order_id": oid1,
                               "value": "188号", "field": "house",
                               "expected_version": 3, "request_id": "e-lost"})
    check("重试幂等重放", s == 200 and r["body"].get("idempotent_replay"), r)
    s, state2 = call("/api/state")
    check("重试后版本不增长、事件位点不增加",
          next(o for o in state2["orders"] if o["id"] == oid1)["version"]
          == ver_after_lost and state2["seq"] == state1["seq"],
          (state2["seq"], state1["seq"]))

    print("== 5. 副本读路由 ==")
    s, r = call("/api/read", {"order_id": oid1, "client": "new",
                              "source": "replica"})
    check("落后副本 -> replica_lagging 并指明 primary",
          s == 409 and r["error"] == "replica_lagging"
          and r["extra"]["fallback"] == "primary", r)
    check("追平写入", call("/api/replica/catchup", {})[0] == 200)
    s, r = call("/api/read", {"order_id": oid1, "client": "new",
                              "source": "replica"})
    check("追平但 legacy 格式 -> format_not_migrated 回主库",
          s == 409 and r["error"] == "format_not_migrated"
          and r["extra"]["fallback"] == "primary", r)
    s, r = call("/api/read", {"order_id": oid1, "client": "old",
                              "source": "replica"})
    check("旧端在 legacy 副本照常读整段", s == 200
          and r["read_route"]["served_by"] == "replica", r)
    s, r = call("/api/replica/format", {"format": "split"})
    check("副本换 split 格式", s == 200)
    s, r = call("/api/read", {"order_id": oid1, "client": "new",
                              "source": "replica"})
    check("换格式后新端副本读成功", s == 200 and r["body"]["house"] == "188号", r)
    # 暂存单在副本上的明确去向
    s, r = call("/api/read", {"order_id": oid2, "client": "new",
                              "source": "replica"})
    check("暂存单副本读 -> stashed_on_replica 回主库",
          s == 409 and r["error"] == "stashed_on_replica", r)

    print("== 6. 人工处置 ==")
    s, r = call("/api/manual-resolve", {"order_id": oid2, "street": "港区仓库路",
                                        "house": "3号", "request_id": "e-fix"})
    check("人工处置恢复 split 且确认门牌",
          s == 200 and r["body"]["state"] == "split"
          and r["body"]["house_confirmed"], r)

    print("== 7. 回退 v1 与再升级 ==")
    check("回退 v1", call("/api/mode", {"mode": "v1"})[0] == 200)
    s, r = call("/api/write", {"client": "new", "order_id": oid1, "value": "1号",
                               "field": "house", "expected_version": 4,
                               "request_id": "e-v1block"})
    check("v1 下新端写被模式闸门拒绝",
          s == 409 and r["error"] == "new_client_blocked_in_v1", r)
    s, r = call("/api/read", {"order_id": oid1, "client": "old"})
    check("v1 旧端主库读整段正常", s == 200 and r["body"]["address"], r)
    # 再建一单未确认订单，验证回退期 dirty 重放
    s, r = call("/api/orders", {"full_address": "枫林街20号", "request_id": "e-o3"})
    oid3 = r["body"]["order_id"]
    call("/api/mode", {"mode": "v2"})
    call("/api/migrate", {"order_id": oid3, "request_id": "e-m3"})
    call("/api/mode", {"mode": "v1"})
    s, r = call("/api/write", {"client": "old", "order_id": oid3,
                               "value": "枫林街22号", "expected_version": 1,
                               "request_id": "e-v1w"})
    check("v1 旧端整段写照常", s == 200, r)
    s, r = call("/api/mode", {"mode": "v2"})
    check("再升级 v2", s == 200)
    s, state = call("/api/state")
    o1 = next(o for o in state["orders"] if o["id"] == oid1)
    o3 = next(o for o in state["orders"] if o["id"] == oid3)
    check("再升级后已确认门牌保留", o1["house"] == "188号"
          and o1["house_confirmed"], o1)
    check("dirty 订单被重新拆分（22号）", o3["house"] == "22号"
          and o3["state"] == "split", o3)
    check("全部事件仍在日志中", len(state["events"]) >= 20)
    check("人工任务已关闭", all(t["status"] == "resolved"
                            for t in state["tasks"] if t["order_id"] == oid2))

    print(f"\n结果：{PASS} 通过 / {FAIL} 失败")
    sys.exit(1 if FAIL else 0)


if __name__ == "__main__":
    main()

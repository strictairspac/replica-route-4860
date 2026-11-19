"""收货地址拆分演练 —— 事件溯源内核（零三方依赖，可独立单测）。

设计要点（对应演练需求）：
1. 所有写入都是追加事件，当前状态由事件投影而来 -> 回退再升级不丢记录。
2. 每次写带 expected_version 做乐观锁（CAS），旧整段写 / 新门牌写可并发、冲突可预期。
3. 门牌一旦经新端确认，旧端整段写被 house_locked 规则拒绝，迟到旧写无法盖写。
4. 每个写带 request_id，同一 request_id 重试只生效一次（幂等）。
5. 无法拆分的旧地址 -> 暂存（stashed），保留接单，生成人工处置任务；拒收方案见 README 对照。
6. 副本是事件日志在某个位点的投影：追上写入但未换格式时，新端读得到明确去向（回主库）。
7. 全局模式 v1/v2 也是事件，回退旧版后照常读写，重新升级重放全部事件。
"""
from __future__ import annotations

import json
import os
import re
import threading
import time
import uuid
from copy import deepcopy

# 拆分规则（演练用，明确可解释）：门牌 = 从首个阿拉伯数字到字符串末尾；
# 其前的部分为街道。街道至少 2 个字符、门牌必须以数字开头，否则判定无法拆分。
_HOUSE_RE = re.compile(r"^(?P<street>\D+?)\s*(?P<house>\d[\w\-号幢栋#]*)$")


def split_address(full: str):
    """把整段地址拆成 (街道, 门牌)；无法可靠拆分返回 None。"""
    if not full or not full.strip():
        return None
    full = full.strip()
    m = _HOUSE_RE.match(full)
    if not m:
        return None
    street = m.group("street").strip().rstrip("号#").strip()
    house = m.group("house").strip()
    if len(street) < 2 or len(house) < 1:
        return None
    return street, house


class EngineError(Exception):
    """业务拒绝：携带 HTTP 状态码与机器可读 code。"""

    def __init__(self, status: int, code: str, message: str, extra=None):
        super().__init__(message)
        self.status = status
        self.code = code
        self.extra = extra or {}


def _now_ms() -> int:
    return int(time.time() * 1000)


class Engine:
    def __init__(self, snapshot_path: str | None = None):
        self._lock = threading.RLock()
        self.snapshot_path = snapshot_path
        self.events: list[dict] = []          # 全局追加事件日志（含拒绝审计事件）
        self.seq = 0                          # 全局位点
        self.idempotency: dict[str, dict] = {}  # request_id -> 首次响应
        self.replica = {"applied_seq": 0, "format": "legacy", "online": True}
        self.tasks: list[dict] = []
        if snapshot_path and os.path.exists(snapshot_path):
            self._load()

    # ------------------------------------------------------------------ 基础工具
    def _next_seq(self) -> int:
        self.seq += 1
        return self.seq

    def _append(self, kind: str, data: dict, request_id: str | None = None) -> dict:
        evt = {
            "seq": self._next_seq(),
            "at_ms": _now_ms(),
            "kind": kind,
            "request_id": request_id,
            "data": data,
        }
        self.events.append(evt)
        return evt

    def _order(self, order_id: str) -> dict:
        for o in self._project()["orders"].values():
            if o["id"] == order_id:
                return o
        raise EngineError(404, "order_not_found", f"订单 {order_id} 不存在")

    def _project(self, upto_seq: int | None = None) -> dict:
        """把事件日志投影成当前状态（upto_seq 给副本用）。"""
        orders: dict[str, dict] = {}
        mode = "v1"
        mode_since = None
        upto = self.seq if upto_seq is None else min(upto_seq, self.seq)
        for evt in self.events:
            if evt["seq"] > upto:
                break
            kind, data = evt["kind"], evt["data"]
            if kind == "order_created":
                orders[data["order_id"]] = {
                    "id": data["order_id"],
                    "version": 0,
                    "legacy_addr": data["full_address"],
                    "street": None,
                    "house": None,
                    "house_confirmed": False,
                    "state": "legacy",
                    "dirty": False,
                    "updated_seq": evt["seq"],
                }
            elif kind in ("legacy_write", "house_write", "street_write",
                          "address_split", "address_stashed", "manual_resolve",
                          "rollback_touch", "reconvert_split", "reconvert_stash"):
                o = orders.get(data["order_id"])
                if o is None:
                    continue
                for k in ("version", "legacy_addr", "street", "house",
                          "house_confirmed", "state", "dirty"):
                    if k in data:
                        o[k] = data[k]
                o["updated_seq"] = evt["seq"]
            elif kind == "mode_switched":
                mode = data["mode"]
                mode_since = evt["seq"]
        return {"orders": orders, "mode": mode, "mode_since_seq": mode_since}

    def snapshot(self) -> dict:
        with self._lock:
            p = self._project()
            return {
                "mode": p["mode"],
                "seq": self.seq,
                "replica": deepcopy(self.replica),
                "orders": [self._view(o["id"]) for o in p["orders"].values()],
                "tasks": deepcopy(self.tasks),
                "events": deepcopy(self.events),
            }

    def _save(self):
        if not self.snapshot_path:
            return
        tmp = self.snapshot_path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({
                "events": self.events,
                "seq": self.seq,
                "idempotency": self.idempotency,
                "replica": self.replica,
                "tasks": self.tasks,
            }, f, ensure_ascii=False)
        os.replace(tmp, self.snapshot_path)

    def _load(self):
        with open(self.snapshot_path, encoding="utf-8") as f:
            blob = json.load(f)
        self.events = blob.get("events", [])
        self.seq = blob.get("seq", len(self.events))
        self.idempotency = blob.get("idempotency", {})
        self.replica = blob.get("replica", {"applied_seq": 0, "format": "legacy", "online": True})
        self.tasks = blob.get("tasks", [])

    # ------------------------------------------------------------------ 接单
    def create_order(self, full_address: str, request_id: str | None = None) -> dict:
        with self._lock:
            rid = request_id or f"req-{uuid.uuid4().hex[:10]}"
            if rid in self.idempotency:
                return self._replay(rid)
            # 订单号只按 order_created 计数，与审计等事件数无关，不跳号
            n = sum(1 for e in self.events if e["kind"] == "order_created") + 1
            oid = f"ORD-{n:03d}"
            self._append("order_created",
                         {"order_id": oid, "full_address": full_address}, rid)
            self._append("audit_accept",
                         {"order_id": oid, "note": "接单不受地址能否拆分影响，整段原文先入库"}, rid)
            resp = {"status": 201, "body": {"order_id": oid, "state": "legacy"},
                    "replayed": False}
            self.idempotency[rid] = resp
            self._save()
            return resp

    # ------------------------------------------------------------------ 读取
    def _view(self, order_id: str) -> dict:
        o = self._project()["orders"][order_id]
        mode = self._project()["mode"]
        view = deepcopy(o)
        # 给旧端合成整段地址：拆分态下 街道+门牌 反推
        if view["legacy_addr"] is None and view["street"]:
            view["legacy_addr"] = f"{view['street']} {view['house'] or ''}".strip()
        view["mode"] = mode
        return view

    def read_primary(self, order_id: str, client: str) -> dict:
        """client=old 读整段；client=new 优先读拆分结构。"""
        with self._lock:
            view = self._view(order_id)
            if client == "old":
                body = {"order_id": order_id, "address": view["legacy_addr"],
                        "state": view["state"]}
            else:
                body = {"order_id": order_id, "state": view["state"],
                        "street": view["street"], "house": view["house"],
                        "house_confirmed": view["house_confirmed"],
                        "legacy_addr": view["legacy_addr"]}
            return {"status": 200, "read_route": {
                "served_by": "primary", "reason": "primary_read",
                "replica_seq": self.replica["applied_seq"], "primary_seq": self.seq,
            }, "body": body}

    def read_replica(self, order_id: str, client: str) -> dict:
        """副本读：落后/未换格式/冲突都必须给新端明确去向。"""
        with self._lock:
            if not self.replica["online"]:
                raise EngineError(503, "replica_down", "副本离线，请回主库读",
                                  {"fallback": "primary"})
            applied = self.replica["applied_seq"]
            if order_id not in self._project(applied)["orders"]:
                raise EngineError(409, "replica_lagging",
                                  "副本尚未收到该订单（位点落后），路由回主库",
                                  {"fallback": "primary", "replica_seq": applied,
                                   "primary_seq": self.seq})
            o = self._project(applied)["orders"][order_id]
            if client == "new" and self.replica["format"] != "split":
                # 追上了写入但还没换格式：新端在副本上读不到 street/house
                raise EngineError(
                    409, "format_not_migrated",
                    "副本已追平写入但仍是 legacy 格式，新端改道主库读取",
                    {"fallback": "primary", "replica_seq": applied,
                     "replica_format": self.replica["format"], "primary_seq": self.seq})
            if client == "new" and o["state"] == "stashed":
                raise EngineError(
                    409, "stashed_on_replica",
                    "该订单在副本上为暂存冲突态，回主库走人工处置",
                    {"fallback": "primary", "replica_seq": applied})
            if client == "old":
                body = {"order_id": order_id, "address": o["legacy_addr"],
                        "state": o["state"]}
            else:
                body = {"order_id": order_id, "state": o["state"],
                        "street": o["street"], "house": o["house"],
                        "house_confirmed": o["house_confirmed"],
                        "legacy_addr": o["legacy_addr"]}
            return {"status": 200, "read_route": {
                "served_by": "replica", "reason": "replica_safe",
                "replica_seq": applied, "replica_format": self.replica["format"],
                "primary_seq": self.seq,
            }, "body": body}

    # ------------------------------------------------------------------ 写入
    def _replay(self, rid: str) -> dict:
        first = deepcopy(self.idempotency[rid])
        first["replayed"] = True
        first["body"] = dict(first["body"])
        first["body"]["idempotent_replay"] = True
        return first

    def _reject(self, rid: str, status: int, code: str, message: str,
                order_id: str | None, extra: dict | None = None) -> dict:
        self._append("audit_reject",
                     {"order_id": order_id, "code": code, "message": message,
                      "extra": extra or {}}, rid)
        resp = {"status": status, "body": {"error": code, "message": message,
                                           "extra": extra or {}}, "replayed": False}
        self.idempotency[rid] = resp
        self._save()
        return resp

    def write(self, client: str, order_id: str, value: str, field: str | None,
              expected_version: int, request_id: str,
              simulate_lost: bool = False) -> dict:
        """统一写入口。client=old 写整段；client=new 写 house/street。"""
        with self._lock:
            rid = request_id
            if rid in self.idempotency:
                return self._replay(rid)
            try:
                proj = self._project()
                mode = proj["mode"]
                o = proj["orders"].get(order_id)
                if o is None:
                    raise EngineError(404, "order_not_found", f"订单 {order_id} 不存在")

                # 1) 乐观锁：基于客户端读到的版本做 CAS
                if expected_version != o["version"]:
                    raise EngineError(
                        409, "version_conflict",
                        f"版本冲突：期望 v{expected_version}，当前已 v{o['version']}，"
                        "请重新读取后再写（并发写互不覆盖）",
                        {"current_version": o["version"]})

                # 2) 模式闸门
                if mode == "v1" and client == "new":
                    raise EngineError(409, "new_client_blocked_in_v1",
                                      "系统已回退到旧版 v1，新端写被拒绝；"
                                      "请升级回 v2 后再编辑门牌")

                new_version = o["version"] + 1

                if client == "old":
                    # 3) 门牌确认锁：迟到的旧整段写不能盖掉已确认门牌
                    if o["house_confirmed"]:
                        raise EngineError(
                            409, "house_locked",
                            "门牌已被新端确认，旧端整段写被拒绝（迟到旧写不盖确认门牌）；"
                            "请旧端重新拉取最新整段地址",
                            {"confirmed_house": o["house"]})
                    parts = split_address(value)
                    if mode == "v1":
                        # 回退期：旧版照常整段读写，记录 dirty 供再升级识别
                        self._append("legacy_write", {
                            "order_id": order_id, "version": new_version,
                            "legacy_addr": value, "street": o["street"],
                            "house": o["house"],
                            "house_confirmed": o["house_confirmed"],
                            "state": o["state"], "dirty": True,
                            "note": "v1 回退期旧端整段写",
                        }, rid)
                        self._append("rollback_touch", {
                            "order_id": order_id, "version": new_version,
                            "legacy_addr": value, "street": o["street"],
                            "house": o["house"],
                            "house_confirmed": o["house_confirmed"],
                            "state": o["state"], "dirty": True,
                        }, rid)
                    elif parts is None:
                        # v2 下旧端写来一段无法拆分的新地址 -> 暂存
                        self._stash(order_id, value, new_version, rid,
                                    reason="old_write_unsplittable",
                                    note="旧端整段写无法自动拆分，暂存待人工处理")
                        self.tasks.append({
                            "order_id": order_id, "reason": "old_write_unsplittable",
                            "raw": value, "status": "open", "at_ms": _now_ms()})
                        resp = {"status": 202, "body": {
                            "order_id": order_id, "version": new_version,
                            "state": "stashed",
                            "note": "已接单暂存：原文保留，等待人工拆分，不覆盖已确认门牌"},
                            "replayed": False}
                        self.idempotency[rid] = resp
                        self._save()
                        return resp
                    else:
                        street, house = parts
                        self._append("legacy_write", {
                            "order_id": order_id, "version": new_version,
                            "legacy_addr": value, "street": street, "house": house,
                            "house_confirmed": False, "state": "split", "dirty": False,
                            "note": "旧端整段写，自动拆分为街道+门牌（门牌待新端确认）",
                        }, rid)

                else:  # client == "new"（仅 v2 可达此分支）
                    if field == "house":
                        self._append("house_write", {
                            "order_id": order_id, "version": new_version,
                            "legacy_addr": f"{o['street'] or ''} {value}".strip(),
                            "street": o["street"], "house": value,
                            "house_confirmed": True, "state": "split",
                            "dirty": False, "note": "新端确认门牌",
                        }, rid)
                    elif field == "street":
                        self._append("street_write", {
                            "order_id": order_id, "version": new_version,
                            "legacy_addr": f"{value} {o['house'] or ''}".strip(),
                            "street": value, "house": o["house"],
                            "house_confirmed": o["house_confirmed"],
                            "state": "split", "dirty": False,
                        }, rid)
                    else:
                        raise EngineError(422, "bad_field", "新端写必须指定 field=house|street")

                # 模拟响应丢失：事件已生效，但客户端拿到 503，随后凭同一 request_id 重试
                if simulate_lost:
                    raise EngineError(503, "response_lost",
                                      "模拟：服务端已落库但响应丢失，请用同一 request_id 重试")

                view = self._view(order_id)
                resp = {"status": 200, "body": {
                    "order_id": order_id, "version": new_version,
                    "state": view["state"], "street": view["street"],
                    "house": view["house"],
                    "house_confirmed": view["house_confirmed"]},
                    "replayed": False}
                self.idempotency[rid] = resp
                self._save()
                return resp

            except EngineError as e:
                if e.code == "response_lost":
                    # 事件此前已生效：登记成功响应（重试时原样重放），本次只回 503
                    view = self._view(order_id)
                    self._append("audit_response_lost", {
                        "order_id": order_id,
                        "note": "响应丢失模拟：写已落库，客户端将凭同一 request_id 重放"}, rid)
                    self.idempotency[rid] = {
                        "status": 200, "replayed": False,
                        "body": {"order_id": order_id, "version": view["version"],
                                 "state": view["state"], "street": view["street"],
                                 "house": view["house"],
                                 "house_confirmed": view["house_confirmed"],
                                 "note": "上次响应丢失，事件此前已生效；本次为幂等重放"}}
                    self._save()
                    return {"status": 503,
                            "body": {"error": "response_lost", "message": str(e),
                                     "extra": e.extra}, "replayed": False}
                return self._reject(rid, e.status, e.code, str(e), order_id, e.extra)

    def _stash(self, order_id: str, raw: str, version: int, rid: str,
               reason: str, note: str):
        self._append("address_stashed", {
            "order_id": order_id, "version": version,
            "legacy_addr": raw, "street": None, "house": None,
            "house_confirmed": False, "state": "stashed", "dirty": False,
            "reason": reason, "note": note,
        }, rid)

    # ------------------------------------------------------------------ 迁移
    def migrate_order(self, order_id: str, request_id: str | None = None) -> dict:
        """把一笔 legacy 订单转换为 split；拆不动则暂存。"""
        with self._lock:
            rid = request_id or f"req-{uuid.uuid4().hex[:10]}"
            if rid in self.idempotency:
                return self._replay(rid)
            try:
                proj = self._project()
                o = proj["orders"].get(order_id)
                if o is None:
                    raise EngineError(404, "order_not_found", f"订单 {order_id} 不存在")
                if proj["mode"] != "v2":
                    raise EngineError(409, "migration_requires_v2",
                                      "仅 v2 模式执行格式转换")
                if o["state"] == "split":
                    raise EngineError(409, "already_split", "订单已是拆分格式，无需转换")
                new_version = o["version"] + 1
                parts = split_address(o["legacy_addr"] or "")
                if parts is None:
                    self._stash(order_id, o["legacy_addr"], new_version, rid,
                                reason="auto_split_failed",
                                note="自动拆分失败：暂存并生成人工处置任务（订单不拒收）")
                    self.tasks.append({
                        "order_id": order_id, "reason": "auto_split_failed",
                        "raw": o["legacy_addr"], "status": "open", "at_ms": _now_ms()})
                    resp = {"status": 202, "body": {
                        "order_id": order_id, "state": "stashed",
                        "version": new_version,
                        "note": "无法拆分，已暂存；接单保留，等待人工处置"},
                        "replayed": False}
                else:
                    street, house = parts
                    self._append("address_split", {
                        "order_id": order_id, "version": new_version,
                        "legacy_addr": o["legacy_addr"], "street": street,
                        "house": house, "house_confirmed": False,
                        "state": "split", "dirty": False,
                        "note": "自动拆分完成，门牌待新端确认",
                    }, rid)
                    resp = {"status": 200, "body": {
                        "order_id": order_id, "state": "split",
                        "version": new_version, "street": street, "house": house,
                        "house_confirmed": False}, "replayed": False}
                self.idempotency[rid] = resp
                self._save()
                return resp
            except EngineError as e:
                return self._reject(rid, e.status, e.code, str(e), order_id, e.extra)

    def manual_resolve(self, order_id: str, street: str, house: str,
                       request_id: str | None = None) -> dict:
        """人工处置暂存订单。"""
        with self._lock:
            rid = request_id or f"req-{uuid.uuid4().hex[:10]}"
            if rid in self.idempotency:
                return self._replay(rid)
            try:
                o = self._project()["orders"].get(order_id)
                if o is None:
                    raise EngineError(404, "order_not_found", f"订单 {order_id} 不存在")
                if o["state"] != "stashed":
                    raise EngineError(409, "not_stashed", "仅暂存订单可人工处置")
                new_version = o["version"] + 1
                self._append("manual_resolve", {
                    "order_id": order_id, "version": new_version,
                    "legacy_addr": f"{street} {house}", "street": street,
                    "house": house, "house_confirmed": True, "state": "split",
                    "dirty": False, "note": "人工拆分并确认门牌",
                }, rid)
                for t in reversed(self.tasks):
                    if t["order_id"] == order_id and t["status"] == "open":
                        t["status"] = "resolved"
                        t["resolved_at_ms"] = _now_ms()
                        break
                resp = {"status": 200, "body": {
                    "order_id": order_id, "state": "split",
                    "version": new_version, "street": street, "house": house,
                    "house_confirmed": True}, "replayed": False}
                self.idempotency[rid] = resp
                self._save()
                return resp
            except EngineError as e:
                return self._reject(rid, e.status, e.code, str(e), order_id, e.extra)

    # ------------------------------------------------------------------ 模式切换
    def switch_mode(self, mode: str) -> dict:
        with self._lock:
            if mode not in ("v1", "v2"):
                raise EngineError(422, "bad_mode", "mode 仅支持 v1/v2")
            proj = self._project()
            if proj["mode"] == mode:
                return {"status": 200, "body": {"mode": mode, "note": "模式未变化"}}
            self._append("mode_switched", {"mode": mode,
                                           "from": proj["mode"]})
            if mode == "v2":
                # 重新升级：对回退期被旧端改动过（dirty）的订单重新转换
                for oid, o in proj["orders"].items():
                    if not o.get("dirty"):
                        continue
                    new_version = o["version"] + 1
                    parts = split_address(o["legacy_addr"] or "")
                    if parts is None:
                        self._append("reconvert_stash", {
                            "order_id": oid, "version": new_version,
                            "legacy_addr": o["legacy_addr"], "street": None,
                            "house": None, "house_confirmed": False,
                            "state": "stashed", "dirty": False,
                            "note": "再升级：回退期改动无法重新拆分，转人工",
                        })
                        self.tasks.append({
                            "order_id": oid, "reason": "reconvert_failed",
                            "raw": o["legacy_addr"], "status": "open",
                            "at_ms": _now_ms()})
                    else:
                        street, house = parts
                        # 重拆结果为准；仅当回退期旧写没动过门牌（未确认且本就为空）
                        # 且历史上已确认过门牌时才保留确认态（house_locked 已保证
                        # 已确认订单在 v1 不会被旧写改动）
                        keep_confirmed = o["house_confirmed"] and o["house"]
                        self._append("reconvert_split", {
                            "order_id": oid, "version": new_version,
                            "legacy_addr": o["legacy_addr"], "street": street,
                            "house": o["house"] if keep_confirmed else house,
                            "house_confirmed": keep_confirmed,
                            "state": "split", "dirty": False,
                            "note": "再升级：重放拆分，保留已确认门牌编辑",
                        })
            self._save()
            return {"status": 200, "body": {"mode": mode,
                                            "primary_seq": self.seq}}

    # ------------------------------------------------------------------ 副本
    def replica_catchup(self) -> dict:
        """副本追日志位点（只追写入，不换格式）。"""
        with self._lock:
            before = self.replica["applied_seq"]
            self.replica["applied_seq"] = self.seq
            self._save()
            return {"status": 200, "body": {
                "applied_seq_before": before,
                "applied_seq": self.seq,
                "format": self.replica["format"],
                "note": "副本已追平写入位点，但格式仍为 legacy，新端读仍会回主库"}}

    def replica_switch_format(self, fmt: str) -> dict:
        with self._lock:
            if fmt not in ("legacy", "split"):
                raise EngineError(422, "bad_format", "format 仅支持 legacy/split")
            if fmt == "split" and self.replica["applied_seq"] < self.seq:
                raise EngineError(409, "replica_lagging",
                                  "副本位点未追平主库，禁止切换格式；先 catchup")
            self.replica["format"] = fmt
            self._save()
            return {"status": 200, "body": {"replica": deepcopy(self.replica)}}

    def replica_set_online(self, online: bool) -> dict:
        with self._lock:
            self.replica["online"] = online
            self._save()
            return {"status": 200, "body": {"replica": deepcopy(self.replica)}}

    def reset(self):
        with self._lock:
            self.events.clear()
            self.seq = 0
            self.idempotency.clear()
            self.tasks.clear()
            self.replica = {"applied_seq": 0, "format": "legacy", "online": True}
            self._save()

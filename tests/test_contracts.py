"""接口契约模块测试。

覆盖：
- 类型推断与契约校验（状态码 / 类型 / 必填 / 枚举）；
- 从用例断言自动提炼草稿（字段、类型、状态码、必填性）；
- 多用例证据矛盾时的四种取舍策略（majority / strict / latest / ignore）；
- ContractManager 的版本发布、用例绑定、影响分析、历史重判；
- 执行器 + 调度器联动：用例断言全过但响应违背契约时，结果为
  「contract_violated」这一新检查结果。
"""

from __future__ import annotations

import os
import shutil
import sys
import tempfile
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from engine import (CONTRACT_VIOLATED, ContractManager, NotificationManager,
                    Scheduler, TestExecutor, extract_drafts)
from engine.contracts import (diff_specs, infer_type, normalize_path,
                              validate_response)
from engine.environments import EnvironmentManager
from storage import BuildStoreRegistry, StoreRegistry


# ---------------------------------------------------------------------------
# 纯函数
# ---------------------------------------------------------------------------

class TestInferType(unittest.TestCase):
    def test_basic(self):
        self.assertEqual(infer_type(True), "boolean")
        self.assertEqual(infer_type(1), "integer")
        self.assertEqual(infer_type(1.5), "number")
        self.assertEqual(infer_type("x"), "string")
        self.assertEqual(infer_type([]), "array")
        self.assertEqual(infer_type({}), "object")
        self.assertEqual(infer_type(None), "null")

    def test_bool_is_not_integer(self):
        self.assertNotEqual(infer_type(False), "integer")

    def test_normalize_path(self):
        self.assertEqual(normalize_path("/api/x/"), "/api/x")
        self.assertEqual(normalize_path("/api/x?a=1"), "/api/x")
        self.assertEqual(normalize_path("/"), "/")


class TestValidateResponse(unittest.TestCase):
    SPEC = {
        "status_codes": [200, 201],
        "request": [],
        "response_fields": [
            {"path": "data.id", "type": "integer", "required": True, "enum": []},
            {"path": "data.name", "type": "string", "required": False, "enum": []},
            {"path": "data.role", "type": "string", "required": True,
             "enum": ["admin", "user"]},
        ],
    }

    def test_ok(self):
        body = {"data": {"id": 1, "name": "a", "role": "admin"}}
        r = validate_response(self.SPEC, 200, body)
        self.assertTrue(r["ok"])
        self.assertEqual(r["checked_fields"], 4)

    def test_status_violation(self):
        r = validate_response(self.SPEC, 500, {"data": {"id": 1, "role": "user"}})
        self.assertFalse(r["ok"])
        self.assertEqual(r["violations"][0]["kind"], "status")

    def test_required_missing(self):
        r = validate_response(self.SPEC, 200, {"data": {"id": 1, "role": "user"}})
        self.assertTrue(r["ok"])  # name 非必填
        r2 = validate_response(self.SPEC, 200, {"data": {"id": 1}})
        self.assertFalse(r2["ok"])
        self.assertTrue(any(v["kind"] == "required" and v["field"] == "data.role"
                            for v in r2["violations"]))

    def test_type_mismatch(self):
        r = validate_response(self.SPEC, 200,
                              {"data": {"id": "x", "role": "user"}})
        self.assertTrue(any(v["kind"] == "type" and v["field"] == "data.id"
                            for v in r["violations"]))

    def test_enum(self):
        r = validate_response(self.SPEC, 200,
                              {"data": {"id": 1, "role": "root"}})
        self.assertTrue(any(v["kind"] == "enum" for v in r["violations"]))

    def test_number_accepts_integer(self):
        spec = {"status_codes": [], "request": [],
                "response_fields": [{"path": "x", "type": "number",
                                     "required": True}]}
        self.assertTrue(validate_response(spec, 200, {"x": 3})["ok"])


class TestDiffSpecs(unittest.TestCase):
    def test_breaking_and_compatible(self):
        old = {"status_codes": [200], "request": [],
               "response_fields": [
                   {"path": "a", "type": "string", "required": True},
                   {"path": "b", "type": "string", "required": False},
               ]}
        new = {"status_codes": [200, 201], "request": [],
               "response_fields": [
                   {"path": "a", "type": "integer", "required": True},
                   {"path": "b", "type": "string", "required": True},
                   {"path": "c", "type": "string", "required": False},
               ]}
        d = diff_specs(old, new)
        kinds = {x["kind"] for x in d["breaking"]}
        self.assertIn("type_changed", kinds)
        self.assertIn("required_added", kinds)
        self.assertTrue(any(x["kind"] == "status_added" for x in d["compatible"]))

    def test_field_removal_is_breaking(self):
        old = {"status_codes": [], "request": [],
               "response_fields": [{"path": "a", "required": True}]}
        d = diff_specs(old, {"status_codes": [], "request": [],
                             "response_fields": []})
        self.assertTrue(any(x["kind"] == "field_removed" for x in d["breaking"]))


# ---------------------------------------------------------------------------
# 自动提炼
# ---------------------------------------------------------------------------

def _case(cid, steps, created_at=1.0, name=None):
    return {"id": cid, "name": name or cid, "project_id": "p1",
            "created_at": created_at, "steps": steps}


def _req(method="GET", url="/api/users", save_as="resp"):
    return {"action": "request", "method": method, "url": url,
            "save_as": save_as, "name": "req"}


def _assert(atype, actual, expected=None):
    return {"action": "assert", "type": atype, "actual": actual,
            "expected": expected, "name": "a"}


class TestExtract(unittest.TestCase):
    def test_basic_extraction(self):
        cases = [_case("c1", [
            _req(),
            _assert("status", "${resp.status}", 200),
            _assert("equals", "${resp.body.id}", 14),
            _assert("equals", "${resp.body.name}", "alice"),
        ])]
        drafts = extract_drafts(cases)
        self.assertEqual(len(drafts), 1)
        d = drafts[0]
        self.assertEqual(d["method"], "GET")
        self.assertEqual(d["path"], "/api/users")
        self.assertEqual(d["spec"]["status_codes"], [200])
        fields = {f["path"]: f for f in d["spec"]["response_fields"]}
        self.assertEqual(fields["id"]["type"], "integer")
        self.assertEqual(fields["name"]["type"], "string")
        self.assertTrue(fields["id"]["required"])
        self.assertEqual(d["conflicts"], [])

    def test_grouping_by_endpoint(self):
        cases = [
            _case("c1", [_req("GET", "/api/a"),
                         _assert("status", "${resp.status}", 200)]),
            _case("c2", [_req("POST", "/api/b"),
                         _assert("status", "${resp.status}", 201)]),
        ]
        drafts = extract_drafts(cases)
        self.assertEqual({(d["method"], d["path"]) for d in drafts},
                         {("GET", "/api/a"), ("POST", "/api/b")})

    def test_non_response_assertions_ignored(self):
        cases = [_case("c1", [
            {"action": "script", "expr": "1+1", "save_as": "r"},
            _assert("equals", "${r}", 2),
        ])]
        self.assertEqual(extract_drafts(cases), [])

    def test_numeric_comparison_implies_number(self):
        cases = [_case("c1", [
            _req(), _assert("gte", "${resp.body.count}", 3),
        ])]
        d = extract_drafts(cases)[0]
        f = next(x for x in d["spec"]["response_fields"] if x["path"] == "count")
        self.assertEqual(f["type"], "number")


class TestConflictStrategies(unittest.TestCase):
    """同一字段在两条用例里分别被断言为整数和字符串。"""

    def _cases(self):
        return [
            _case("c1", [_req(), _assert("equals", "${resp.body.id}", 14)],
                  created_at=1.0),
            _case("c2", [_req(), _assert("equals", "${resp.body.id}", "xyz")],
                  created_at=2.0),
        ]

    def test_majority_tie_prefers_stricter(self):
        d = extract_drafts(self._cases(), strategy="majority")[0]
        f = next(x for x in d["spec"]["response_fields"] if x["path"] == "id")
        self.assertEqual(f["type"], "integer")  # 平票取更严格类型
        self.assertEqual(len(d["conflicts"]), 1)
        self.assertEqual(d["conflicts"][0]["chosen"], "integer")

    def test_majority_true_majority(self):
        cases = self._cases() + [
            _case("c3", [_req(), _assert("equals", "${resp.body.id}", 9)],
                  created_at=3.0)]
        d = extract_drafts(cases, strategy="majority")[0]
        f = next(x for x in d["spec"]["response_fields"] if x["path"] == "id")
        self.assertEqual(f["type"], "integer")

    def test_strict_leaves_type_blank(self):
        d = extract_drafts(self._cases(), strategy="strict")[0]
        f = next(x for x in d["spec"]["response_fields"] if x["path"] == "id")
        self.assertEqual(f["type"], "")
        # strict 下只要有用例断言过就视为必填
        self.assertTrue(f["required"])

    def test_latest_prefers_newest_case(self):
        d = extract_drafts(self._cases(), strategy="latest")[0]
        f = next(x for x in d["spec"]["response_fields"] if x["path"] == "id")
        self.assertEqual(f["type"], "string")  # c2 最新

    def test_ignore_drops_type_but_keeps_field(self):
        d = extract_drafts(self._cases(), strategy="ignore")[0]
        f = next(x for x in d["spec"]["response_fields"] if x["path"] == "id")
        self.assertEqual(f["type"], "")

    def test_integer_number_not_conflict(self):
        cases = [
            _case("c1", [_req(), _assert("equals", "${resp.body.x}", 1)]),
            _case("c2", [_req(), _assert("gt", "${resp.body.x}", 0)]),
        ]
        d = extract_drafts(cases)[0]
        self.assertEqual(d["conflicts"], [])

    def test_required_by_majority(self):
        # 3 条用例命中接口，只有 1 条断言某字段 -> 多数策略下非必填
        cases = [
            _case("c1", [_req(), _assert("equals", "${resp.body.only}", 1)]),
            _case("c2", [_req(), _assert("status", "${resp.status}", 200)]),
            _case("c3", [_req(), _assert("status", "${resp.status}", 200)]),
        ]
        d = extract_drafts(cases, strategy="majority")[0]
        f = next(x for x in d["spec"]["response_fields"] if x["path"] == "only")
        self.assertFalse(f["required"])


# ---------------------------------------------------------------------------
# Manager：版本 / 绑定 / 影响 / 重判
# ---------------------------------------------------------------------------

class _BaseManagerCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="ct_test_")
        self.registry = StoreRegistry(os.path.join(self.tmp, "store"), 200)
        self.builds = BuildStoreRegistry(os.path.join(self.tmp, "builds"))
        self.env_mgr = EnvironmentManager(self.registry, self.tmp)
        self.notify = NotificationManager(self.registry)
        self.cm = ContractManager(self.registry, self.builds)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _project_and_env(self):
        pid = self.registry.store("projects").insert({
            "id": "proj_test", "name": "p", "created_at": time.time()})
        env = self.env_mgr.create(pid, {"name": "e", "config": {"latency_ms": 1}})
        return pid, env["id"]

    def _contract_with_two_versions(self, pid):
        c = self.cm.create(pid, {
            "name": "users", "method": "GET", "path": "/api/users",
            "spec": {"status_codes": [200], "request": [],
                     "response_fields": [
                         {"path": "ok", "type": "boolean", "required": True}]}})
        self.cm.publish_version(c["id"], note="v1")
        self.cm.publish_version(c["id"], {
            "status_codes": [200], "request": [],
            "response_fields": [
                {"path": "ok", "type": "boolean", "required": True},
                {"path": "version", "type": "string", "required": True}],
        }, note="v2 加必填字段")
        return self.cm.get(c["id"])


class TestContractManager(_BaseManagerCase):
    def test_unique_endpoint(self):
        pid, _ = self._project_and_env()
        self.cm.create(pid, {"method": "GET", "path": "/a"})
        with self.assertRaises(Exception):
            self.cm.create(pid, {"method": "GET", "path": "/a/"})  # 归一化后重复

    def test_version_publish_and_activate(self):
        pid, _ = self._project_and_env()
        c = self._contract_with_two_versions(pid)
        self.assertEqual(c["current_version"], 2)
        self.assertEqual(len(self.cm.list_versions(c["id"])), 2)
        self.cm.activate_version(c["id"], 1)
        self.assertEqual(self.cm.get(c["id"])["current_version"], 1)

    def test_invalid_type_rejected(self):
        pid, _ = self._project_and_env()
        with self.assertRaises(Exception):
            self.cm.create(pid, {"method": "GET", "path": "/a", "spec": {
                "status_codes": [], "request": [],
                "response_fields": [{"path": "x", "type": "datetime"}]}})

    def test_draft_does_not_mutate_published(self):
        pid, _ = self._project_and_env()
        c = self._contract_with_two_versions(pid)
        self.cm.extract_to_draft(c["id"])
        self.cm.save_draft(c["id"], {"status_codes": [200, 201], "request": [],
                                     "response_fields": []})
        v1 = self.cm.list_versions(c["id"])[0]
        self.assertEqual(v1["spec"]["status_codes"], [200])

    def test_bind_and_impact(self):
        pid, _ = self._project_and_env()
        c = self._contract_with_two_versions(pid)
        case_id = self.registry.store("cases").insert({
            "id": "case_x", "project_id": pid, "name": "x",
            "steps": [{"action": "request", "method": "GET",
                       "url": "/api/users"}]})
        self.cm.bind_cases(c["id"], [case_id], version=1)
        impact = self.cm.impact(c["id"])
        self.assertEqual([x["case_id"] for x in impact["stale_cases"]], [case_id])

        rows = {r["case_id"]: r for r in self.cm.list_bound_cases(c["id"])}
        self.assertFalse(rows[case_id]["up_to_date"])
        # 升级绑定后影响面消失
        self.cm.bind_cases(c["id"], [case_id])
        self.assertEqual(self.cm.impact(c["id"])["stale_cases"], [])


class TestExecutionIntegration(_BaseManagerCase):
    def _scheduler(self):
        return Scheduler(self.registry, self.builds, TestExecutor(),
                         self.env_mgr, None, None, None, self.notify,
                         contract_manager=self.cm, tick_seconds=60)

    def test_passing_case_violates_contract(self):
        """用例断言只检查 status==200，但契约 v2 要求必填字段 version。"""
        pid, env_id = self._project_and_env()
        c = self._contract_with_two_versions(pid)  # 当前 v2
        case = {
            "id": "case_health", "project_id": pid, "name": "健康检查",
            "priority": "P0", "tags": ["smoke"], "timeout": 30,
            "steps": [
                {"action": "request", "method": "GET", "url": "/api/users"},
                {"action": "assert", "type": "status",
                 "actual": "${resp.status}", "expected": 200},
            ],
        }
        sched = self._scheduler()
        result = sched._run_one(case, self.env_mgr.to_executor_config(env_id),
                                env_id, 0, __import__("threading").Event())
        self.assertEqual(result["status"], CONTRACT_VIOLATED)
        checks = result["contract_checks"]
        self.assertEqual(len(checks), 1)
        self.assertEqual(checks[0]["version"], 2)
        self.assertEqual(checks[0]["match"], "implicit")
        self.assertTrue(any(v["kind"] == "required"
                            for v in checks[0]["violations"]))

    def test_bound_old_version_still_passes(self):
        """显式绑定 v1 后，即便 v2 已生效，仍按 v1 判定为通过。"""
        pid, env_id = self._project_and_env()
        c = self._contract_with_two_versions(pid)
        case_id = self.registry.store("cases").insert({
            "id": "case_bound", "project_id": pid, "name": "绑定用例",
            "priority": "P1", "tags": ["t"], "timeout": 30,
            "steps": [
                {"action": "request", "method": "GET", "url": "/api/users"},
                {"action": "assert", "type": "status",
                 "actual": "${resp.status}", "expected": 200},
            ]})
        self.cm.bind_cases(c["id"], [case_id], version=1)
        case = self.registry.store("cases").get(case_id)
        sched = self._scheduler()
        result = sched._run_one(case, self.env_mgr.to_executor_config(env_id),
                                env_id, 0, __import__("threading").Event())
        self.assertEqual(result["status"], "passed")
        self.assertEqual(result["contract_checks"][0]["version"], 1)
        self.assertEqual(result["contract_checks"][0]["match"], "explicit")

    def test_build_aggregation_and_recheck(self):
        pid, env_id = self._project_and_env()
        c = self._contract_with_two_versions(pid)
        # 一条用例绑定 v1（显式），一条不绑定（隐式按 v2 -> 违背）
        case_old = {
            "id": "case_old", "project_id": pid, "name": "旧契约用例",
            "priority": "P1", "tags": ["t"], "timeout": 30,
            "steps": [{"action": "request", "method": "GET",
                       "url": "/api/users"},
                      {"action": "assert", "type": "status",
                       "actual": "${resp.status}", "expected": 200}]}
        self.registry.store("cases").insert(case_old)
        self.cm.bind_cases(c["id"], ["case_old"], version=1)

        case_new = dict(case_old, id="case_new", name="新契约用例")
        self.registry.store("cases").insert(case_new)

        suite_id = self.registry.store("suites").insert({
            "id": "suite_1", "project_id": pid, "name": "s",
            "case_ids": ["case_old", "case_new"], "env_id": env_id})

        sched = self._scheduler()
        build = sched.submit_build(pid, suite_id, trigger="manual")
        store = self.builds.for_project(pid)
        deadline = time.time() + 10
        while time.time() < deadline:
            b = store.get(build["id"])
            if b["status"] in ("passed", "failed", "cancelled", "error"):
                break
            time.sleep(0.2)
        b = store.get(build["id"])
        self.assertEqual(b.get("contract_violated"), 1)
        self.assertEqual(b["passed"], 1)
        self.assertEqual(b["status"], "failed")

        # 历史报告：旧用例 v1 通过；用当前 v2 重判应变成违背
        rc = self.cm.recheck_build(build["id"], contract_id=c["id"])
        self.assertEqual(rc["total"], 2)
        diffs = {i["case_id"]: i for i in rc["items"] if i["changed"]}
        self.assertIn("case_old", diffs)
        self.assertFalse(diffs["case_old"]["new_ok"])
        self.assertEqual(diffs["case_old"]["old_version"], 1)
        self.assertEqual(diffs["case_old"]["new_version"], 2)

        # impact 应把这场构建列为「旧版本下校验的历史报告」
        impact = self.cm.impact(c["id"])
        self.assertTrue(any(x["build_id"] == build["id"]
                            for x in impact["stale_builds"]))

    def test_snapshot_path_normalization_matches_contract(self):
        """契约路径 /api/x/ 与快照 /api/x 的尾斜杠差异应能匹配上。"""
        pid, env_id = self._project_and_env()
        c = self.cm.create(pid, {
            "method": "GET", "path": "/api/users/",
            "spec": {"status_codes": [200], "request": [],
                     "response_fields": [
                         {"path": "ghost", "type": "string", "required": True}]}})
        self.cm.publish_version(c["id"], note="v1")
        case = {"id": "case_t", "project_id": pid, "name": "t", "priority": "P2",
                "tags": ["t"], "timeout": 30,
                "steps": [{"action": "request", "method": "GET",
                           "url": "/api/users"},
                          {"action": "assert", "type": "status",
                           "actual": "${resp.status}", "expected": 200}]}
        sched = self._scheduler()
        import threading
        result = sched._run_one(case, self.env_mgr.to_executor_config(env_id),
                                env_id, 0, threading.Event())
        self.assertEqual(result["status"], CONTRACT_VIOLATED)


if __name__ == "__main__":
    unittest.main(verbosity=2)

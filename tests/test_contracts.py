"""接口契约测试。

覆盖：契约规格规范化与响应校验、从用例提炼草稿（含冲突取舍规则）、
版本管理与变更历史、执行器契约校验、构建聚合与报告、升级影响分析、
新旧版本判定对比。
"""

from __future__ import annotations

import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from engine import ContractManager, ReportGenerator, TestExecutor
from engine.contracts import (diff_specs, extract_from_cases, infer_type,
                              normalize_spec, type_matches,
                              validate_against_spec)
from storage import BuildStoreRegistry, StoreRegistry


def _case(case_id, name, steps, priority="P2"):
    return {"id": case_id, "name": name, "priority": priority,
            "project_id": "p1", "steps": steps}


def _health_case(case_id="c1", extra_asserts=None, priority="P2"):
    steps = [
        {"action": "request", "method": "GET", "url": "/api/health"},
        {"action": "assert", "type": "status", "actual": "${resp.status}",
         "expected": 200},
    ]
    steps.extend(extra_asserts or [])
    return _case(case_id, f"用例{case_id}", steps, priority)


class TestSpecNormalization(unittest.TestCase):
    def test_normalize_sorts_and_dedupes(self):
        spec = normalize_spec({
            "response": {
                "status_codes": [201, 200, 200],
                "fields": [
                    {"path": "b", "type": "string"},
                    {"path": "a", "type": "integer", "required": True},
                    {"path": "b", "type": "boolean"},  # 后者覆盖前者
                ],
            },
        })
        self.assertEqual(spec["response"]["status_codes"], [200, 201])
        fields = spec["response"]["fields"]
        self.assertEqual([f["path"] for f in fields], ["a", "b"])
        self.assertEqual(fields[1]["type"], "boolean")
        self.assertFalse(fields[1]["required"])

    def test_normalize_rejects_bad_type(self):
        with self.assertRaises(ValueError):
            normalize_spec({"response": {"fields": [{"path": "x", "type": "uuid"}]}})

    def test_type_matching(self):
        self.assertEqual(infer_type(True), "boolean")
        self.assertEqual(infer_type(1), "integer")
        self.assertEqual(infer_type(1.5), "number")
        self.assertTrue(type_matches(1, "number"))   # integer 是 number 子集
        self.assertFalse(type_matches(1.5, "integer"))
        self.assertFalse(type_matches(True, "integer"))
        self.assertTrue(type_matches(object(), "any"))


class TestValidateAgainstSpec(unittest.TestCase):
    def setUp(self):
        self.spec = normalize_spec({
            "request": {"params": [{"name": "uid", "type": "integer", "required": True}]},
            "response": {
                "status_codes": [200],
                "fields": [
                    {"path": "ok", "type": "boolean", "required": True},
                    {"path": "data.name", "type": "string", "required": False},
                ],
            },
        })

    def test_compliant_response(self):
        v = validate_against_spec(self.spec, params={"uid": 1},
                                  response_status=200,
                                  response_body={"ok": True})
        self.assertEqual(v, [])

    def test_missing_required_field(self):
        v = validate_against_spec(self.spec, params={"uid": 1},
                                  response_status=200, response_body={})
        self.assertEqual([x["kind"] for x in v], ["missing_field"])

    def test_type_mismatch_and_unexpected_status(self):
        v = validate_against_spec(self.spec, params={"uid": 1},
                                  response_status=500,
                                  response_body={"ok": "yes"})
        kinds = {x["kind"] for x in v}
        self.assertEqual(kinds, {"unexpected_status", "type_mismatch"})

    def test_missing_required_param(self):
        v = validate_against_spec(self.spec, params={},
                                  response_status=200,
                                  response_body={"ok": True})
        self.assertEqual([x["kind"] for x in v], ["missing_param"])

    def test_optional_field_absent_is_fine(self):
        v = validate_against_spec(self.spec, params={"uid": 1},
                                  response_status=200,
                                  response_body={"ok": True})
        self.assertEqual(v, [])


class TestExtractFromCases(unittest.TestCase):
    def test_basic_extraction(self):
        drafts = extract_from_cases([_health_case("c1", extra_asserts=[
            {"action": "assert", "type": "truthy", "actual": "${resp.body.ok}"},
            {"action": "assert", "type": "equals", "actual": "${resp.body.echo.method}",
             "expected": "GET"},
        ])])
        draft = drafts[("GET", "/api/health")]
        self.assertEqual(draft["spec"]["response"]["status_codes"], [200])
        fields = {f["path"]: f for f in draft["spec"]["response"]["fields"]}
        self.assertEqual(fields["ok"]["type"], "any")       # truthy 只表达存在
        self.assertEqual(fields["echo.method"]["type"], "string")
        self.assertTrue(fields["ok"]["required"])           # 唯一用例断言了它

    def test_custom_save_as_binding(self):
        case = _case("c1", "自定义变量", [
            {"action": "request", "method": "POST", "url": "/api/login",
             "save_as": "login_resp"},
            {"action": "assert", "type": "equals",
             "actual": "${login_resp.body.ok}", "expected": True},
        ])
        drafts = extract_from_cases([case])
        self.assertIn(("POST", "/api/login"), drafts)
        fields = drafts[("POST", "/api/login")]["spec"]["response"]["fields"]
        self.assertEqual(fields[0]["type"], "boolean")

    def test_type_conflict_majority_wins(self):
        """两条用例对同一字段类型断言矛盾：多数票胜出，少数派记录在案。"""
        cases = [
            _health_case("c1", [{"action": "assert", "type": "equals",
                                 "actual": "${resp.body.code}", "expected": "ok"}]),
            _health_case("c2", [{"action": "assert", "type": "equals",
                                 "actual": "${resp.body.code}", "expected": "done"}]),
            _health_case("c3", [{"action": "assert", "type": "gt",
                                 "actual": "${resp.body.code}", "expected": 5}]),
        ]
        draft = extract_from_cases(cases)[("GET", "/api/health")]
        fields = {f["path"]: f for f in draft["spec"]["response"]["fields"]}
        self.assertEqual(fields["code"]["type"], "string")  # 2 票 vs 1 票
        conflict = next(c for c in draft["conflicts"]
                        if c["kind"] == "type" and c["path"] == "code")
        self.assertEqual(conflict["chosen"], "string")
        # 候选按胜出顺序排列：胜者 2 票在前，被舍弃的少数派 1 票在后
        self.assertEqual(conflict["candidates"][0]["type"], "string")
        self.assertEqual(conflict["candidates"][0]["votes"], 2)
        self.assertEqual(conflict["candidates"][1]["type"], "number")
        self.assertEqual(conflict["candidates"][1]["votes"], 1)

    def test_type_conflict_tie_breaks_by_priority(self):
        """平票时高优先级用例的断言胜出。"""
        cases = [
            _health_case("c1", [{"action": "assert", "type": "gt",
                                 "actual": "${resp.body.code}", "expected": 5}],
                         priority="P0"),
            _health_case("c2", [{"action": "assert", "type": "regex",
                                 "actual": "${resp.body.code}", "expected": "^a"}],
                         priority="P3"),
        ]
        draft = extract_from_cases(cases)[("GET", "/api/health")]
        fields = {f["path"]: f for f in draft["spec"]["response"]["fields"]}
        self.assertEqual(fields["code"]["type"], "number")  # P0 的观察胜出

    def test_required_only_when_all_cases_assert(self):
        """只有全部用例都断言的字段才标必填，分歧被记录。"""
        cases = [
            _health_case("c1", [{"action": "assert", "type": "truthy",
                                 "actual": "${resp.body.ok}"}]),
            _health_case("c2", [{"action": "assert", "type": "truthy",
                                 "actual": "${resp.body.trace_id}"}]),
        ]
        draft = extract_from_cases(cases)[("GET", "/api/health")]
        fields = {f["path"]: f for f in draft["spec"]["response"]["fields"]}
        self.assertFalse(fields["ok"]["required"])
        self.assertFalse(fields["trace_id"]["required"])
        required_conflicts = [c for c in draft["conflicts"] if c["kind"] == "required"]
        self.assertEqual(len(required_conflicts), 2)

    def test_status_codes_union_and_params(self):
        cases = [
            _case("c1", "a", [
                {"action": "request", "method": "GET", "url": "/api/users",
                 "params": {"page": 1}},
                {"action": "assert", "type": "status", "actual": "${resp.status}",
                 "expected": 200},
            ]),
            _case("c2", "b", [
                {"action": "request", "method": "GET", "url": "/api/users",
                 "params": {"page": 2, "size": 20}},
                {"action": "assert", "type": "in", "actual": "${resp.status}",
                 "expected": [200, 201]},
            ]),
        ]
        draft = extract_from_cases(cases)[("GET", "/api/users")]
        self.assertEqual(draft["spec"]["response"]["status_codes"], [200, 201])
        params = {p["name"]: p for p in draft["spec"]["request"]["params"]}
        self.assertEqual(params["page"]["type"], "integer")
        self.assertTrue(params["page"]["required"])    # 两条用例都携带
        self.assertFalse(params["size"]["required"])   # 只有一条携带

    def test_dynamic_url_skipped(self):
        case = _case("c1", "动态URL", [
            {"action": "request", "method": "GET", "url": "/api/users/${uid}"},
            {"action": "assert", "type": "status", "actual": "${resp.status}",
             "expected": 200},
        ])
        self.assertEqual(extract_from_cases([case]), {})


class TestContractManager(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.registry = StoreRegistry(os.path.join(self.tmp.name, "store"))
        self.builds = BuildStoreRegistry(os.path.join(self.tmp.name, "builds"))
        self.mgr = ContractManager(self.registry, self.builds)

    def tearDown(self):
        self.tmp.cleanup()

    def _spec(self, extra_fields=None):
        fields = [{"path": "ok", "type": "boolean", "required": True}]
        fields.extend(extra_fields or [])
        return {"request": {"params": []},
                "response": {"status_codes": [200], "fields": fields}}

    def test_create_publish_and_version_history(self):
        c = self.mgr.create("p1", {"name": "健康", "method": "GET",
                                   "path": "/api/health", "spec": self._spec()})
        self.assertEqual(c["status"], "active")
        self.assertEqual(c["current_version"], 1)

        self.mgr.publish_version(c["id"], self._spec(
            [{"path": "users", "type": "array", "required": True}]),
            change_summary="新增 users")
        versions = self.mgr.versions(c["id"])
        self.assertEqual(len(versions), 2)
        self.assertEqual(versions[1]["version"], 2)
        # 版本历史带 diff：v2 相对 v1 新增了 users 字段
        diff = versions[1]["diff_from_previous"]
        self.assertTrue(any(d["kind"] == "字段_added" and d["path"] == "users"
                            for d in diff))
        self.assertEqual(self.mgr.get(c["id"])["current_version"], 2)

    def test_extract_confirm_and_reextract(self):
        self.registry.store("cases").insert(_health_case("c1", extra_asserts=[
            {"action": "assert", "type": "truthy", "actual": "${resp.body.ok}"},
        ]))
        drafts = self.mgr.extract_drafts("p1")
        self.assertEqual(len(drafts), 1)
        self.assertTrue(drafts[0]["is_new"])

        contract = self.mgr.get(drafts[0]["contract_id"])
        self.assertEqual(contract["status"], "draft")
        self.assertEqual(contract["current_version"], 0)
        self.assertIsNotNone(contract["draft_spec"])

        # 确认草稿 → 发布 v1，草稿清空
        version = self.mgr.confirm_draft(contract["id"])
        self.assertEqual(version["version"], 1)
        self.assertEqual(version["source"], "extracted")
        contract = self.mgr.get(contract["id"])
        self.assertEqual(contract["status"], "active")
        self.assertIsNone(contract["draft_spec"])

        # 再次提炼：草稿挂到已有契约上（不新建），确认后成为 v2
        drafts2 = self.mgr.extract_drafts("p1")
        self.assertFalse(drafts2[0]["is_new"])
        self.assertEqual(drafts2[0]["contract_id"], contract["id"])
        self.mgr.confirm_draft(contract["id"])
        self.assertEqual(self.mgr.get(contract["id"])["current_version"], 2)

    def test_discard_draft_keeps_published(self):
        c = self.mgr.create("p1", {"method": "GET", "path": "/api/health",
                                   "spec": self._spec()})
        self.registry.store("cases").insert(_health_case("c1"))
        self.mgr.extract_drafts("p1")
        self.assertIsNotNone(self.mgr.get(c["id"])["draft_spec"])
        self.mgr.discard_draft(c["id"])
        contract = self.mgr.get(c["id"])
        self.assertIsNone(contract["draft_spec"])
        self.assertEqual(contract["current_version"], 1)

    def test_active_specs_only_published(self):
        self.mgr.create("p1", {"method": "GET", "path": "/api/health",
                               "spec": self._spec()})
        self.mgr.create("p1", {"method": "GET", "path": "/api/draft"})  # 无版本
        specs = self.mgr.active_specs("p1")
        self.assertEqual(len(specs), 1)
        self.assertEqual(specs[0]["path"], "/api/health")
        self.assertEqual(specs[0]["version"], 1)


class TestExecutorContractCheck(unittest.TestCase):
    def _contracts(self):
        return [{
            "contract_id": "ctr1", "name": "健康", "method": "GET",
            "path": "/api/health", "version": 3,
            "spec": normalize_spec({
                "response": {"status_codes": [200], "fields": [
                    {"path": "ok", "type": "boolean", "required": True},
                    {"path": "users", "type": "array", "required": True},
                ]},
            }),
        }]

    def test_violation_recorded_but_case_status_untouched(self):
        """契约违背是独立检查结果：断言全过时用例仍算通过。"""
        case = _health_case("c1", [
            {"action": "assert", "type": "truthy", "actual": "${resp.body.ok}"},
        ])
        result = TestExecutor().execute_case(case, {"latency_ms": 0},
                                             contracts=self._contracts())
        self.assertEqual(result["status"], "passed")
        self.assertEqual(len(result["contract_checks"]), 1)
        check = result["contract_checks"][0]
        self.assertFalse(check["ok"])
        self.assertEqual(check["version"], 3)
        self.assertEqual([v["kind"] for v in check["violations"]], ["missing_field"])
        # 响应快照留存，供契约升级后重新校验
        self.assertEqual(check["response_snapshot"]["status"], 200)
        self.assertIn("ok", check["response_snapshot"]["body"])

    def test_compliant_response(self):
        contracts = self._contracts()
        contracts[0]["spec"]["response"]["fields"] = [
            {"path": "ok", "type": "boolean", "required": True}]
        result = TestExecutor().execute_case(
            _health_case("c1"), {"latency_ms": 0}, contracts=contracts)
        self.assertTrue(result["contract_checks"][0]["ok"])

    def test_no_contracts_no_checks(self):
        result = TestExecutor().execute_case(_health_case("c1"), {"latency_ms": 0})
        self.assertEqual(result["contract_checks"], [])


class TestBuildAggregationAndReport(unittest.TestCase):
    def test_counts_snapshot_and_report_section(self):
        with tempfile.TemporaryDirectory() as d:
            reg = BuildStoreRegistry(os.path.join(d, "builds"))
            store = reg.for_project("p1")
            store.create("b1")
            store.set_total("b1", 2)
            check_ok = {"contract_id": "ctr1", "version": 1, "ok": True,
                        "violations": [], "method": "GET", "path": "/api/health",
                        "response_snapshot": {"status": 200, "body": {}}}
            check_bad = dict(check_ok, ok=False,
                             violations=[{"kind": "missing_field",
                                          "message": "响应缺少必填字段 users"}])
            store.record_result("b1", {"case_id": "c1", "case_name": "a",
                                       "status": "passed", "duration": 0.1,
                                       "contract_checks": [check_ok], "logs": []})
            store.record_result("b1", {"case_id": "c2", "case_name": "b",
                                       "status": "passed", "duration": 0.1,
                                       "contract_checks": [check_bad], "logs": []})
            store.finish("b1", "passed")

            build = store.get("b1")
            self.assertEqual(build["contract_checks"], 2)
            self.assertEqual(build["contract_violations"], 1)
            self.assertEqual(build["contract_snapshot"], {"ctr1": 1})

            report = ReportGenerator(reg).build_report("p1", "b1")
            self.assertEqual(report["summary"]["contract_violations"], 1)
            self.assertEqual(report["contract"]["checks"], 2)
            details = report["contract"]["violation_details"]
            self.assertEqual(len(details), 1)
            self.assertEqual(details[0]["case_id"], "c2")
            self.assertEqual(details[0]["violations"], ["响应缺少必填字段 users"])


class TestImpactAnalysis(unittest.TestCase):
    """契约升级后的影响分析：过时的用例 / 历史报告、判定翻转。"""

    def test_impact(self):
        with tempfile.TemporaryDirectory() as d:
            registry = StoreRegistry(os.path.join(d, "store"))
            builds = BuildStoreRegistry(os.path.join(d, "builds"))
            mgr = ContractManager(registry, builds)

            # 用例：断言 ok 字段（符合 v1，未覆盖 v2 新增的 users）
            registry.store("cases").insert(_health_case("c1", extra_asserts=[
                {"action": "assert", "type": "truthy", "actual": "${resp.body.ok}"},
            ]))
            # v1：只要求 ok
            contract = mgr.create("p1", {
                "method": "GET", "path": "/api/health",
                "spec": {"response": {"status_codes": [200], "fields": [
                    {"path": "ok", "type": "boolean", "required": True}]}}})
            # 按 v1 跑一场构建（mock 响应没有 users，v1 下符合契约）
            case = registry.store("cases").get("c1")
            result = TestExecutor().execute_case(
                case, {"latency_ms": 0}, contracts=mgr.active_specs("p1"))
            store = builds.for_project("p1")
            store.create("b1")
            store.set_total("b1", 1)
            store.record_result("b1", result)
            store.finish("b1", "passed")
            self.assertTrue(result["contract_checks"][0]["ok"])

            # 升级到 v2：新增必填字段 users
            mgr.publish_version(contract["id"], {
                "response": {"status_codes": [200], "fields": [
                    {"path": "ok", "type": "boolean", "required": True},
                    {"path": "users", "type": "array", "required": True},
                ]}}, change_summary="新增 users")

            impact = mgr.impact(contract["id"])
            self.assertEqual(impact["from_version"], 1)
            self.assertEqual(impact["to_version"], 2)

            # 用例未覆盖新必填字段 → 仍在按旧契约断言
            self.assertEqual(len(impact["cases_outdated"]), 1)
            self.assertIn("users", impact["cases_outdated"][0]["issues"][0])

            # 历史构建按 v1 校验 → 落后于当前版本
            self.assertEqual(len(impact["builds_outdated"]), 1)
            self.assertEqual(impact["builds_outdated"][0]["checked_version"], 1)

            # 同一份历史响应：v1 下符合，v2 下违背 → 判定翻转
            flips = impact["verdict_changes"]
            self.assertEqual(len(flips), 1)
            self.assertTrue(flips[0]["old_ok"])
            self.assertFalse(flips[0]["new_ok"])
            self.assertEqual(flips[0]["violations_now"][0]["kind"], "missing_field")

    def test_compare_versions(self):
        with tempfile.TemporaryDirectory() as d:
            registry = StoreRegistry(os.path.join(d, "store"))
            mgr = ContractManager(registry)
            contract = mgr.create("p1", {
                "method": "GET", "path": "/api/health",
                "spec": {"response": {"status_codes": [200], "fields": [
                    {"path": "ok", "type": "boolean", "required": True}]}}})
            mgr.publish_version(contract["id"], {
                "response": {"status_codes": [200], "fields": [
                    {"path": "ok", "type": "boolean", "required": True},
                    {"path": "users", "type": "array", "required": True}]}})
            resp = {"status": 200, "body": {"ok": True}}
            cmp = mgr.compare(contract["id"], 1, 2, resp)
            self.assertTrue(cmp["a"]["ok"])
            self.assertFalse(cmp["b"]["ok"])
            self.assertTrue(cmp["verdict_changed"])
            # 带上 users 后两个版本都符合
            resp["body"]["users"] = []
            cmp = mgr.compare(contract["id"], 1, 2, resp)
            self.assertFalse(cmp["verdict_changed"])


class TestDiffSpecs(unittest.TestCase):
    def test_diff(self):
        old = normalize_spec({"response": {"status_codes": [200], "fields": [
            {"path": "a", "type": "string", "required": True},
            {"path": "b", "type": "integer", "required": False},
        ]}})
        new = normalize_spec({"response": {"status_codes": [200, 201], "fields": [
            {"path": "a", "type": "integer", "required": False},
            {"path": "c", "type": "array", "required": True},
        ]}})
        kinds = {(c["kind"], c["path"]) for c in diff_specs(old, new)}
        self.assertIn(("字段_type_changed", "a"), kinds)
        self.assertIn(("字段_required_changed", "a"), kinds)
        self.assertIn(("字段_removed", "b"), kinds)
        self.assertIn(("字段_added", "c"), kinds)
        self.assertIn(("status_added", "status"), kinds)


if __name__ == "__main__":
    unittest.main()

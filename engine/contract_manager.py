"""契约管理器：契约 / 版本的持久化、用例绑定、影响分析与历史重判。

存储
----
契约实体存在通用分片存储的 ``contracts`` 集合中（同项目内
``method + path`` 唯一）。版本列表内嵌在契约文档里、一经发布不可变。

用例与契约的关联有两种方式：

1. 显式绑定：用例文档上的 ``contract_refs`` 字段
   ``[{contract_id, method, path, version, bound_at}]``，可手工或批量绑定；
2. 隐式匹配：执行时若用例没有显式绑定，则按用例请求步骤的
   ``method + path`` 自动匹配当前契约（结果里标记 ``matched: "implicit"``）。

每场构建、每条用例结果都会记录校验时实际使用的契约版本快照，因此：

- :meth:`impact` 能回答「契约升级后，哪些用例仍显式绑定旧版本、哪些历史
  报告是在旧版本下校验的」；
- :meth:`recheck_build` / :meth:`recheck_response` 能拿新版本对同一响应
  重新判定，给出新旧契约下的判定差异（报告里的「红成一片」到底是接口变
  了还是契约变了，一目了然）。
"""

from __future__ import annotations

import copy
import time
from typing import Any, Optional

from . import contracts as C
from .models import new_id


class ContractError(Exception):
    """契约操作的预期内错误（重复接口、版本不存在等）。"""


class ContractManager:
    """接口契约的增删改查、版本发布、绑定、影响分析与重判。"""

    def __init__(self, registry, build_registry=None):
        self._store = registry.store("contracts")
        self._cases = registry.store("cases")
        self._builds = build_registry

    # ------------------------------------------------------------------ CRUD
    def list_for_project(self, project_id: str) -> list[dict]:
        out = self._store.query(where=[("project_id", "eq", project_id)],
                                order_by="updated_at", order="desc")
        for c in out:
            c["version_count"] = len(c.get("versions") or [])
            c["bound_case_count"] = self._count_bound_cases(c["id"])
        return out

    def get(self, contract_id: str) -> Optional[dict]:
        return self._store.get(contract_id)

    def _find_by_endpoint(self, project_id: str, method: str,
                          path: str, exclude_id: Optional[str] = None) -> Optional[dict]:
        method = (method or "GET").upper()
        path = C.normalize_path(path)
        for c in self._store.query(where=[("project_id", "eq", project_id)]):
            if c.get("id") == exclude_id:
                continue
            if c.get("method") == method and c.get("path") == path:
                return c
        return None

    def create(self, project_id: str, payload: dict) -> dict:
        name = (payload.get("name") or "").strip()
        method = (payload.get("method") or "GET").upper()
        path = C.normalize_path(payload.get("path") or "")
        if not path:
            raise ContractError("接口路径不能为空")
        if self._find_by_endpoint(project_id, method, path):
            raise ContractError(f"契约已存在：{method} {path}")
        spec = payload.get("spec") or C._empty_spec()
        self._validate_spec(spec)
        contract = C.new_contract(project_id, name, method, path, spec)
        self._store.insert(contract)
        if payload.get("publish"):
            self.publish_version(contract["id"], note="初始版本",
                                 source="manual", spec=spec)
            contract = self._store.get(contract["id"])
        return contract

    def update_meta(self, contract_id: str, payload: dict) -> dict:
        contract = self.get(contract_id)
        if contract is None:
            raise ContractError("契约不存在")
        patch = {}
        if "name" in payload:
            patch["name"] = payload["name"]
        if "path" in payload or "method" in payload:
            method = payload.get("method", contract["method"]).upper()
            path = C.normalize_path(payload.get("path", contract["path"]))
            if self._find_by_endpoint(contract["project_id"], method, path,
                                      exclude_id=contract_id):
                raise ContractError(f"契约已存在：{method} {path}")
            patch["method"] = method
            patch["path"] = path
        updated = self._store.update(contract_id, patch)
        return updated

    def delete(self, contract_id: str) -> bool:
        contract = self.get(contract_id)
        if contract is None:
            return False
        # 解除用例上的显式绑定
        for case in self._cases.all():
            refs = [r for r in (case.get("contract_refs") or [])
                    if r.get("contract_id") != contract_id]
            if len(refs) != len(case.get("contract_refs") or []):
                self._cases.update(case["id"], {"contract_refs": refs})
        return self._store.delete(contract_id)

    @staticmethod
    def _validate_spec(spec: dict) -> None:
        if not isinstance(spec, dict):
            raise ContractError("spec 必须是对象")
        codes = spec.get("status_codes") or []
        if not isinstance(codes, list) or any(not isinstance(x, int) for x in codes):
            raise ContractError("status_codes 必须是整数数组")
        for key in ("request", "response_fields"):
            fields = spec.get(key) or []
            if not isinstance(fields, list):
                raise ContractError(f"{key} 必须是数组")
            bad = {f.get("type") for f in fields
                   if f.get("type") and f.get("type") not in C.FIELD_TYPES}
            if bad:
                raise ContractError(f"未知字段类型：{sorted(bad)}，"
                                    f"可选 {list(C.FIELD_TYPES)}")

    # -------------------------------------------------------------- 草稿管理
    def generate_drafts(self, project_id: str, case_ids: Optional[list] = None,
                        strategy: str = "majority") -> list[dict]:
        """从项目用例的断言中提炼契约草稿（不落库，供前端确认）。"""
        cases = self._cases.query(where=[("project_id", "eq", project_id)])
        if case_ids is not None:
            wanted = set(case_ids)
            cases = [c for c in cases if c.get("id") in wanted]
        return C.extract_drafts(cases, strategy=strategy)

    def get_draft(self, contract_id: str) -> Optional[dict]:
        c = self.get(contract_id)
        return c.get("draft") if c else None

    def extract_to_draft(self, contract_id: str, strategy: str = "majority",
                         case_ids: Optional[list] = None) -> dict:
        """从相关用例重新提炼，写回该契约的草稿区（不影响已发布版本）。"""
        contract = self.get(contract_id)
        if contract is None:
            raise ContractError("契约不存在")
        drafts = self.generate_drafts(contract["project_id"],
                                      case_ids=case_ids, strategy=strategy)
        match = next((d for d in drafts
                      if d["method"] == contract["method"]
                      and d["path"] == contract["path"]), None)
        draft = match or {"spec": C._empty_spec(), "conflicts": [],
                          "source_case_ids": [], "generated_at": time.time(),
                          "strategy": strategy}
        draft["generated_at"] = time.time()
        self._store.update(contract_id, {"draft": draft})
        return draft

    def save_draft(self, contract_id: str, spec: dict) -> dict:
        self._validate_spec(spec)
        contract = self.get(contract_id)
        if contract is None:
            raise ContractError("契约不存在")
        draft = dict(contract.get("draft") or {})
        draft["spec"] = copy.deepcopy(spec)
        draft["generated_at"] = draft.get("generated_at") or time.time()
        self._store.update(contract_id, {"draft": draft})
        return draft

    def publish_draft(self, contract_id: str, note: str = "",
                      source: str = "draft") -> dict:
        """把草稿区的 spec 发布为新版本。"""
        contract = self.get(contract_id)
        if contract is None:
            raise ContractError("契约不存在")
        draft = contract.get("draft") or {}
        spec = draft.get("spec") or C._empty_spec()
        self._validate_spec(spec)
        version = C.publish_version(
            contract, spec, note=note, source=source,
            case_ids=draft.get("source_case_ids") or [])
        self._store.update(contract_id, {
            "versions": contract["versions"],
            "current_version": contract["current_version"],
            "updated_at": time.time(),
        })
        return version

    # -------------------------------------------------------------- 版本管理
    def publish_version(self, contract_id: str, spec: Optional[dict] = None,
                        note: str = "", source: str = "manual",
                        case_ids: Optional[list] = None) -> dict:
        contract = self.get(contract_id)
        if contract is None:
            raise ContractError("契约不存在")
        if spec is None:
            # 未显式给 spec：优先基于当前版本（再发一版相同内容），首版则
            # 回退到草稿区的 spec
            current = C.current_spec(contract)
            spec = current if current is not None else (
                (contract.get("draft") or {}).get("spec") or C._empty_spec())
        self._validate_spec(spec)
        version = C.publish_version(contract, spec, note=note,
                                    source=source, case_ids=case_ids)
        self._store.update(contract_id, {
            "versions": contract["versions"],
            "current_version": contract["current_version"],
            "updated_at": time.time(),
        })
        return version

    def list_versions(self, contract_id: str) -> list[dict]:
        contract = self.get(contract_id)
        if contract is None:
            raise ContractError("契约不存在")
        return contract.get("versions") or []

    def activate_version(self, contract_id: str, version_no: int) -> dict:
        contract = self.get(contract_id)
        if C.get_version(contract, version_no) is None:
            raise ContractError(f"版本 v{version_no} 不存在")
        self._store.update(contract_id, {"current_version": version_no,
                                         "updated_at": time.time()})
        return self.get(contract_id)

    def version_diff(self, contract_id: str,
                     from_version: int, to_version: int) -> dict:
        contract = self.get(contract_id)
        old_v = C.get_version(contract, from_version)
        new_v = C.get_version(contract, to_version)
        if old_v is None or new_v is None:
            raise ContractError("版本不存在")
        diff = C.diff_specs(old_v["spec"], new_v["spec"])
        diff.update({"contract_id": contract_id,
                     "from_version": from_version, "to_version": to_version})
        return diff

    # ------------------------------------------------------------------ 绑定
    def _count_bound_cases(self, contract_id: str) -> int:
        return sum(1 for c in self._cases.all()
                   if any(r.get("contract_id") == contract_id
                          for r in (c.get("contract_refs") or [])))

    def bind_cases(self, contract_id: str, case_ids: list[str],
                   version: Optional[int] = None) -> dict:
        """把用例显式绑定到契约（默认绑定当前版本）。

        绑定后这些用例执行时固定按所绑版本校验，直到重新绑定——这样契约
        升级不会悄悄改变它们的判定口径。
        """
        contract = self.get(contract_id)
        if contract is None:
            raise ContractError("契约不存在")
        version = version or contract.get("current_version")
        if not version:
            raise ContractError("契约尚未发布任何版本，无法绑定")
        bound, skipped = [], []
        for cid in case_ids:
            case = self._cases.get(cid)
            if case is None or case.get("project_id") != contract["project_id"]:
                skipped.append(cid)
                continue
            refs = [r for r in (case.get("contract_refs") or [])
                    if not (r.get("method") == contract["method"]
                            and r.get("path") == contract["path"])]
            refs.append({"contract_id": contract_id,
                         "method": contract["method"], "path": contract["path"],
                         "version": version, "bound_at": time.time()})
            self._cases.update(cid, {"contract_refs": refs})
            bound.append(cid)
        return {"bound": bound, "skipped": skipped, "version": version}

    def unbind_cases(self, contract_id: str, case_ids: list[str]) -> dict:
        out = []
        for cid in case_ids:
            case = self._cases.get(cid)
            if case is None:
                continue
            refs = [r for r in (case.get("contract_refs") or [])
                    if r.get("contract_id") != contract_id]
            self._cases.update(cid, {"contract_refs": refs})
            out.append(cid)
        return {"unbound": out}

    def list_bound_cases(self, contract_id: str) -> list[dict]:
        contract = self.get(contract_id)
        if contract is None:
            return []
        current = contract.get("current_version")
        rows = []
        for case in self._cases.query(where=[("project_id", "eq",
                                              contract["project_id"])]):
            ref = next((r for r in (case.get("contract_refs") or [])
                        if r.get("contract_id") == contract_id), None)
            if ref is None:
                continue
            rows.append({
                "case_id": case["id"], "case_name": case.get("name"),
                "version": ref.get("version"),
                "up_to_date": ref.get("version") == current,
                "bound_at": ref.get("bound_at"),
            })
        return rows

    def resolve_for_case(self, case: dict) -> list[dict]:
        """解析一条用例执行时应对照的契约版本（含隐式匹配）。

        返回 ``[{contract_id, name, method, path, version, spec, match}]``，
        ``match`` 为 ``explicit`` / ``implicit``。
        """
        resolved: list[dict] = []
        seen: set[str] = set()
        for ref in case.get("contract_refs") or []:
            contract = self.get(ref.get("contract_id"))
            if contract is None:
                continue
            version_no = ref.get("version") or contract.get("current_version")
            version = C.get_version(contract, version_no)
            if version is None:
                continue
            resolved.append(self._resolved_entry(contract, version, "explicit"))
            seen.add(contract["id"])

        # 隐式匹配：请求步骤的 method+path 命中且未显式绑定
        endpoints = self._case_endpoints(case)
        for method, path in endpoints:
            contract = self._find_by_endpoint(case.get("project_id"), method, path)
            if contract is None or contract["id"] in seen:
                continue
            version = C.get_version(contract)
            if version is None:
                continue
            resolved.append(self._resolved_entry(contract, version, "implicit"))
            seen.add(contract["id"])
        return resolved

    @staticmethod
    def _resolved_entry(contract: dict, version: dict, match: str) -> dict:
        return {
            "contract_id": contract["id"],
            "name": contract.get("name"),
            "method": contract["method"],
            "path": contract["path"],
            "version": version["version"],
            "match": match,
            "spec": version["spec"],
        }

    @staticmethod
    def _case_endpoints(case: dict) -> list[tuple[str, str]]:
        out, seen = [], set()
        for step in case.get("steps") or []:
            if step.get("action") != "request":
                continue
            key = ((step.get("method") or "GET").upper(),
                   C.normalize_path(str(step.get("url") or "/")))
            if key not in seen:
                seen.add(key)
                out.append(key)
        return out

    # ------------------------------------------------------------ 影响分析
    def impact(self, contract_id: str, version_no: Optional[int] = None) -> dict:
        """契约升级影响面：仍绑定旧版本的用例 + 旧版本下产生的历史报告。"""
        contract = self.get(contract_id)
        if contract is None:
            raise ContractError("契约不存在")
        target = version_no or contract.get("current_version")
        stale_cases = []
        for row in self.list_bound_cases(contract_id):
            if row["version"] != target:
                stale_cases.append(row)

        stale_builds, checked_builds = [], 0
        if self._builds is not None:
            store = self._builds.for_project(contract["project_id"])
            for build in store.list_builds():
                bid = build["id"]
                usage = self._build_contract_usage(store, bid, contract_id)
                if usage:
                    checked_builds += 1
                    # 只要构建中有响应是按旧版本校验的，就属于升级影响面；
                    # 一场构建可能混用多个版本（部分用例显式绑定旧版）。
                    old_versions = [u for u in usage["versions"]
                                    if u["version"] != target]
                    if old_versions:
                        stale_builds.append({
                            "build_id": bid,
                            "name": build.get("name") or bid,
                            "status": build.get("status"),
                            "versions": old_versions,
                            "results": sum(u["results"] for u in old_versions),
                            "violations": sum(u["violations"]
                                              for u in old_versions),
                            "finished_at": build.get("finished_at"),
                        })
        return {
            "contract_id": contract_id,
            "current_version": target,
            "stale_cases": stale_cases,
            "stale_builds": stale_builds,
            "builds_checked": checked_builds,
        }

    def _build_contract_usage(self, store, build_id: str,
                              contract_id: str) -> Optional[dict]:
        """从构建结果统计该契约各版本被使用的次数与违背数。

        一场构建可能同时存在多个版本的校验记录（部分用例显式绑定旧版、
        其余隐式匹配当前版），因此按版本分别计数返回，而不是取众数。
        """
        records = store.results(build_id)
        version_votes: dict[int, dict] = {}
        for r in records:
            for chk in r.get("contract_checks") or []:
                if chk.get("contract_id") != contract_id:
                    continue
                agg = version_votes.setdefault(chk.get("version"), {
                    "version": chk.get("version"), "results": 0,
                    "violations": 0})
                agg["results"] += 1
                if chk.get("violations"):
                    agg["violations"] += 1
        if not version_votes:
            return None
        versions = sorted(version_votes.values(),
                          key=lambda u: -(u["version"] or 0))
        return {"versions": versions}

    # ------------------------------------------------------------ 历史重判
    def recheck_build(self, build_id: str,
                      contract_id: Optional[str] = None) -> dict:
        """用契约的新版本重新判定历史构建中保存的响应快照。

        每条用例结果在执行时保存了 ``response_snapshots``（实际 status/body）
        与当时的 ``contract_checks``。这里只读重算、不改写历史，输出新旧
        版本下每条记录的判定差异。
        """
        if self._builds is None:
            raise ContractError("未配置构建存储")
        found = self._builds.find_build(build_id)
        if found is None:
            raise ContractError("构建不存在")
        store = self._builds.for_project(found["project_id"])
        records = store.results(build_id)
        items, diff_count = [], 0
        for r in records:
            snapshots = r.get("response_snapshots") or []
            old_checks = {c.get("contract_id"): c
                          for c in (r.get("contract_checks") or [])}
            for snap in snapshots:
                cid = contract_id or snap.get("contract_id")
                if not cid or (contract_id and cid != snap.get("contract_id")):
                    continue
                contract = self.get(cid)
                version = C.get_version(contract) if contract else None
                if contract is None or version is None:
                    continue
                new_result = C.validate_response(
                    version["spec"], snap.get("status"), snap.get("body"))
                old = old_checks.get(cid) or {}
                old_ok = old.get("ok")
                new_ok = new_result["ok"]
                changed = old_ok != new_ok
                diff_count += 1 if changed else 0
                items.append({
                    "case_id": r.get("case_id"),
                    "case_name": r.get("case_name"),
                    "contract_id": cid,
                    "contract_name": contract.get("name"),
                    "path": snap.get("path"),
                    "old_version": snap.get("version"),
                    "new_version": version["version"],
                    "old_ok": old_ok,
                    "new_ok": new_ok,
                    "changed": changed,
                    "old_violations": old.get("violations") or [],
                    "new_violations": new_result["violations"],
                })
        return {"build_id": build_id, "items": items,
                "changed_count": diff_count,
                "total": len(items)}

    def recheck_response(self, contract_id: str, status: Any, body: Any,
                         version_no: Optional[int] = None) -> dict:
        """用指定版本（默认当前）直接校验一份响应，供页面即时试判。"""
        contract = self.get(contract_id)
        if contract is None:
            raise ContractError("契约不存在")
        version = C.get_version(contract, version_no)
        if version is None:
            raise ContractError("契约版本不存在")
        return {"contract_id": contract_id, "version": version["version"],
                **C.validate_response(version["spec"], status, body)}

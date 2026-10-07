"""接口契约管理：契约实体、版本历史、响应校验、契约提炼与升级影响分析。

为什么需要契约
--------------
用例里的断言是「这条用例关心什么」，契约是「这个接口承诺什么」。接口悄悄
加了必填字段、改了字段类型时，按旧格式写的断言可能全部照常通过，报告一
片绿，但调用方已经坏了。把契约（路径、方法、请求参数、响应字段与类型、
必填项）从用例里抽出来单独管理后：

1. **执行时校验**：每次构建里，请求步骤的响应都会对照该接口当前生效的
   契约版本校验，「契约违背」作为一种独立的检查结果（``contract_checks``）
   进入用例结果与报告——用例断言没覆盖到的偏差也能被发现；
2. **版本化**：契约按项目、按版本管理，每次发布留存历史版本与变更说明，
   构建会记录「当时按哪个版本校验」的快照（``contract_snapshot``）；
3. **升级影响**：契约升级后可以回答三个问题——哪些用例还在按旧契约断言、
   哪些历史报告是按旧版本校验的、同一份历史响应在新旧契约下判定是否翻转；
4. **自动提炼**：从既有用例的断言反向提炼契约草稿，供人确认后发布。
   多条用例对同一字段的断言互相矛盾时，按明确规则取舍（见下），并把
   被舍弃的候选一并记录在草稿的 ``conflicts`` 里，绝不静默吞掉。

冲突取舍规则（提炼时）
----------------------
- **字段类型**：按断言投票，票数多者胜；平票时取自优先级更高（P0→P3）的
  用例；再平票时具体类型优先于 ``any``；最后按类型名字典序保证确定性。
  被舍弃的类型及其来源用例记录进 ``conflicts``；
- **必填性**：只有「命中该接口的全部用例」都断言了某字段，该字段才标为
  必填，否则为可选（宁可少标必填，不错杀合法响应），分歧同样记录；
- **状态码**：取所有用例期望状态码的并集（响应本来就可能返回多种合法
  状态码，并集才是自洽的契约）。
"""

from __future__ import annotations

import json
import re
import time
from typing import Any, Optional

from .models import new_id

# 契约字段支持的类型
FIELD_TYPES = ["string", "integer", "number", "boolean", "object", "array",
               "null", "any"]

# 契约状态：draft（草稿，未生效）/ active（生效中）/ deprecated（停用）
CONTRACT_STATUSES = ["draft", "active", "deprecated"]

# 提炼时类型冲突平票的优先级权重
_PRIORITY_WEIGHT = {"P0": 4, "P1": 3, "P2": 2, "P3": 1}

# 响应快照序列化上限（超出则只留截断标记，避免结果文件膨胀）
_SNAPSHOT_LIMIT = 4000

_MISSING = object()


# ---------------------------------------------------------------------------
# 类型推断与匹配
# ---------------------------------------------------------------------------

def infer_type(value: Any) -> str:
    """推断 JSON 值的契约类型。"""
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, int):
        return "integer"
    if isinstance(value, float):
        return "number"
    if isinstance(value, str):
        return "string"
    if isinstance(value, list):
        return "array"
    if isinstance(value, dict):
        return "object"
    return "any"


def type_matches(value: Any, expected: str) -> bool:
    """值是否满足契约类型。``integer`` 是 ``number`` 的子集。"""
    if not expected or expected == "any":
        return True
    actual = infer_type(value)
    if actual == expected:
        return True
    return expected == "number" and actual == "integer"


def lookup_path(data: Any, path: str) -> Any:
    """按 ``a.b.0.c`` 点路径取值，缺失返回 ``_MISSING`` 哨兵（区分 null 与不存在）。"""
    current = data
    for part in path.split("."):
        if isinstance(current, dict):
            if part not in current:
                return _MISSING
            current = current[part]
        elif isinstance(current, list):
            try:
                current = current[int(part)]
            except (ValueError, IndexError):
                return _MISSING
        else:
            return _MISSING
    return current


# ---------------------------------------------------------------------------
# 契约规格（spec）规范化与校验
# ---------------------------------------------------------------------------

def _normalize_entries(raw: Any, key: str) -> list[dict]:
    """规范化字段 / 参数条目列表：校验类型、去重（后者覆盖前者）、按名排序。"""
    if raw is None:
        return []
    if not isinstance(raw, list):
        raise ValueError(f"{key} 必须是数组")
    out: dict[str, dict] = {}
    for item in raw:
        if not isinstance(item, dict) or not (item.get(key) or "").strip():
            raise ValueError(f"条目必须包含非空的 {key!r} 字段")
        name = str(item[key]).strip()
        ftype = item.get("type") or "any"
        if ftype not in FIELD_TYPES:
            raise ValueError(f"不支持的类型 {ftype!r}（可选：{', '.join(FIELD_TYPES)}）")
        out[name] = {key: name, "type": ftype, "required": bool(item.get("required", False))}
    return [out[k] for k in sorted(out)]


def normalize_spec(raw: Any) -> dict:
    """把用户输入的契约规格规范化成标准结构，非法输入抛 ``ValueError``。

    标准结构::

        {
          "request":  {"params": [{"name", "type", "required"}]},
          "response": {"status_codes": [200],
                       "fields": [{"path", "type", "required"}]},
        }
    """
    raw = raw or {}
    if not isinstance(raw, dict):
        raise ValueError("契约规格必须是对象")
    request = raw.get("request") or {}
    response = raw.get("response") or {}
    if not isinstance(request, dict) or not isinstance(response, dict):
        raise ValueError("request / response 必须是对象")

    codes = response.get("status_codes") or []
    if not isinstance(codes, list):
        raise ValueError("status_codes 必须是数组")
    norm_codes = sorted({int(c) for c in codes if str(c).lstrip("-").isdigit()})

    return {
        "request": {"params": _normalize_entries(request.get("params"), "name")},
        "response": {
            "status_codes": norm_codes,
            "fields": _normalize_entries(response.get("fields"), "path"),
        },
    }


def validate_against_spec(spec: dict, *, params: Optional[dict] = None,
                          response_status: Optional[int] = None,
                          response_body: Any = None) -> list[dict]:
    """对照契约规格校验一次请求与响应，返回违背项列表（空 = 符合契约）。

    违背项 ``kind``：
    - ``missing_param``       必填请求参数缺失
    - ``param_type_mismatch`` 请求参数类型不符
    - ``unexpected_status``   响应状态码不在契约允许范围
    - ``missing_field``       响应必填字段缺失
    - ``type_mismatch``       响应字段类型不符
    """
    violations: list[dict] = []
    spec = spec or {}

    for p in (spec.get("request") or {}).get("params") or []:
        value = lookup_path(params or {}, p["name"])
        if value is _MISSING or value is None:
            if p.get("required"):
                violations.append({
                    "kind": "missing_param", "path": p["name"],
                    "expected": p.get("type", "any"), "actual": None,
                    "message": f"缺少必填请求参数 {p['name']}",
                })
            continue
        if not type_matches(value, p.get("type", "any")):
            violations.append({
                "kind": "param_type_mismatch", "path": p["name"],
                "expected": p.get("type"), "actual": infer_type(value),
                "message": f"请求参数 {p['name']} 类型应为 {p.get('type')}，"
                           f"实际为 {infer_type(value)}",
            })

    response = spec.get("response") or {}
    codes = response.get("status_codes") or []
    if codes and response_status is not None and response_status not in codes:
        violations.append({
            "kind": "unexpected_status", "path": "status",
            "expected": codes, "actual": response_status,
            "message": f"状态码 {response_status} 不在契约允许范围 {codes}",
        })

    for f in response.get("fields") or []:
        value = lookup_path(response_body, f["path"])
        if value is _MISSING:
            if f.get("required"):
                violations.append({
                    "kind": "missing_field", "path": f["path"],
                    "expected": f.get("type", "any"), "actual": None,
                    "message": f"响应缺少必填字段 {f['path']}",
                })
            continue
        if not type_matches(value, f.get("type", "any")):
            violations.append({
                "kind": "type_mismatch", "path": f["path"],
                "expected": f.get("type"), "actual": infer_type(value),
                "message": f"字段 {f['path']} 类型应为 {f.get('type')}，"
                           f"实际为 {infer_type(value)}",
            })
    return violations


# ---------------------------------------------------------------------------
# 版本 diff
# ---------------------------------------------------------------------------

def diff_specs(old: Optional[dict], new: Optional[dict]) -> list[dict]:
    """比较两个契约版本规格，返回结构化变更列表（用于版本历史与影响分析）。"""
    old = normalize_spec(old)
    new = normalize_spec(new)
    changes: list[dict] = []

    def _diff_entries(section: str, list_key: str, id_key: str, label: str):
        old_map = {e[id_key]: e for e in (old.get(section) or {}).get(list_key, [])}
        new_map = {e[id_key]: e for e in (new.get(section) or {}).get(list_key, [])}
        for name in sorted(new_map.keys() - old_map.keys()):
            e = new_map[name]
            changes.append({"kind": f"{label}_added", "path": name,
                            "detail": f"新增{label} {name}（{e['type']}"
                                      f"{'，必填' if e.get('required') else ''}）"})
        for name in sorted(old_map.keys() - new_map.keys()):
            changes.append({"kind": f"{label}_removed", "path": name,
                            "detail": f"移除{label} {name}"})
        for name in sorted(old_map.keys() & new_map.keys()):
            o, n = old_map[name], new_map[name]
            if o["type"] != n["type"]:
                changes.append({"kind": f"{label}_type_changed", "path": name,
                                "detail": f"{label} {name} 类型 {o['type']} → {n['type']}"})
            if bool(o.get("required")) != bool(n.get("required")):
                changes.append({"kind": f"{label}_required_changed", "path": name,
                                "detail": f"{label} {name} 必填性 "
                                          f"{'必填' if o.get('required') else '可选'} → "
                                          f"{'必填' if n.get('required') else '可选'}"})

    _diff_entries("response", "fields", "path", "字段")
    _diff_entries("request", "params", "name", "参数")

    old_codes = set((old.get("response") or {}).get("status_codes") or [])
    new_codes = set((new.get("response") or {}).get("status_codes") or [])
    for code in sorted(new_codes - old_codes):
        changes.append({"kind": "status_added", "path": "status",
                        "detail": f"状态码新增允许 {code}"})
    for code in sorted(old_codes - new_codes):
        changes.append({"kind": "status_removed", "path": "status",
                        "detail": f"状态码不再允许 {code}"})
    return changes


# ---------------------------------------------------------------------------
# 从用例提炼契约草稿
# ---------------------------------------------------------------------------

def _type_from_assertion(atype: str, expected: Any) -> str:
    """根据断言类型与期望值推断字段类型。"""
    if atype in ("equals", "not_equals"):
        return infer_type(expected)
    if atype == "contains":
        # 字符串包含 → string；否则按「集合成员」理解 → array
        return "string" if isinstance(expected, str) else "array"
    if atype == "regex":
        return "string"
    if atype in ("gt", "gte", "lt", "lte", "between"):
        return "number"
    if atype == "in":
        if isinstance(expected, (list, tuple)) and expected:
            return infer_type(expected[0])
        return "any"
    return "any"  # truthy / json_path / status：仅表达「存在」


def _resolve_type(observations: list[dict]) -> tuple[str, list[dict]]:
    """对同一字段的一组类型观察做取舍，返回 (选定类型, 全部候选及票数)。

    候选按胜出顺序排列（第一个即选定类型），每个候选带票数与来源用例。
    规则：多数票 → 平票取高优先级用例 → 具体类型优先于 any → 字典序兜底。
    """
    votes: dict[str, list[dict]] = {}
    for ob in observations:
        votes.setdefault(ob["type"], []).append(ob)

    def _rank(item):
        ftype, obs = item
        best = max(_PRIORITY_WEIGHT.get(o.get("priority") or "P3", 1) for o in obs)
        concrete = 1 if ftype != "any" else 0
        return (len(obs), best, concrete, ftype)

    ordered = sorted(votes.items(), key=_rank, reverse=True)
    candidates = [{
        "type": ftype,
        "votes": len(obs),
        "cases": sorted({o.get("case_name") or o.get("case_id") for o in obs}),
    } for ftype, obs in ordered]
    return ordered[0][0], candidates


def extract_from_cases(cases: list[dict]) -> dict:
    """从用例步骤中提炼契约草稿。

    扫描每个用例的步骤：``request`` 步骤建立「变量 → 接口」绑定（含
    ``save_as`` 自定义变量名），后续引用 ``${resp.body.xxx}`` /
    ``${resp.status}`` 的 ``assert`` 步骤即视为对该接口的契约观察。

    返回 ``{(method, path): {"method", "path", "spec", "conflicts", "sources"}}``。
    """
    ref_re = re.compile(r"^\$\{([A-Za-z_]\w*)\.(status|body(?:\..+)?)\}$")
    accumulators: dict[tuple, dict] = {}

    for case in cases or []:
        case_id = case.get("id")
        case_name = case.get("name", "未命名用例")
        priority = case.get("priority", "P3")
        bindings: dict[str, tuple] = {}  # 变量名 -> (method, path)

        for step in case.get("steps") or []:
            action = step.get("action")
            if action == "request":
                url = str(step.get("url") or "")
                if "${" in url:
                    continue  # 动态 URL 无法确定接口，跳过
                path = url.split("?")[0].split("#")[0] or "/"
                if "://" in path:
                    path = "/" + path.split("://", 1)[1].split("/", 1)[-1]
                if not path.startswith("/"):
                    path = "/" + path
                method = str(step.get("method") or "GET").upper()
                key = (method, path)
                acc = accumulators.setdefault(key, {
                    "method": method, "path": path, "cases": {},
                    "status_obs": [], "field_obs": {}, "field_cases": {},
                    "param_obs": {}, "param_cases": {},
                })
                acc["cases"][case_id] = {"case_id": case_id, "case_name": case_name,
                                         "priority": priority}
                bindings[step.get("save_as") or "resp"] = key
                params = step.get("params")
                if isinstance(params, dict):
                    for name, value in params.items():
                        ftype = "any" if (isinstance(value, str) and value.startswith("${")) \
                            else infer_type(value)
                        acc["param_obs"].setdefault(name, []).append({
                            "type": ftype, "case_id": case_id,
                            "case_name": case_name, "priority": priority})
                        acc["param_cases"].setdefault(name, set()).add(case_id)

            elif action == "assert":
                actual = step.get("actual")
                if not isinstance(actual, str):
                    continue
                m = ref_re.match(actual.strip())
                if not m:
                    continue
                var, sub = m.group(1), m.group(2)
                key = bindings.get(var)
                if key is None:
                    continue
                acc = accumulators[key]
                atype = step.get("type", "equals")
                expected = step.get("expected")

                if sub == "status":
                    codes = []
                    if atype in ("status", "equals"):
                        codes = [expected]
                    elif atype == "in" and isinstance(expected, (list, tuple)):
                        codes = list(expected)
                    for c in codes:
                        try:
                            acc["status_obs"].append(int(c))
                        except (TypeError, ValueError):
                            pass
                    continue

                if sub == "body":
                    if atype == "json_path" and expected:
                        field_path = str(expected)
                    else:
                        continue  # 整体断言无法定位字段
                else:
                    field_path = sub[len("body."):]
                    if atype == "json_path" and expected:
                        field_path = f"{field_path}.{expected}"

                acc["field_obs"].setdefault(field_path, []).append({
                    "type": _type_from_assertion(atype, expected),
                    "case_id": case_id, "case_name": case_name,
                    "priority": priority})
                acc["field_cases"].setdefault(field_path, set()).add(case_id)

    drafts: dict[tuple, dict] = {}
    for key, acc in accumulators.items():
        total_cases = len(acc["cases"])
        conflicts: list[dict] = []

        fields = []
        for path, obs in sorted(acc["field_obs"].items()):
            ftype, candidates = _resolve_type(obs)
            if len(candidates) > 1:
                conflicts.append({
                    "kind": "type", "path": path, "chosen": ftype,
                    "candidates": candidates,
                    "rule": "多数票优先，平票取高优先级用例，any 最后",
                })
            asserted_by = acc["field_cases"].get(path, set())
            required = len(asserted_by) == total_cases
            if not required:
                conflicts.append({
                    "kind": "required", "path": path, "chosen": False,
                    "rule": "全部用例都断言该字段才标必填",
                    "detail": f"{total_cases} 个用例中 {len(asserted_by)} 个断言了该字段",
                })
            fields.append({"path": path, "type": ftype, "required": required})

        params = []
        for name, obs in sorted(acc["param_obs"].items()):
            ptype, candidates = _resolve_type(obs)
            if len(candidates) > 1:
                conflicts.append({
                    "kind": "type", "path": f"参数 {name}", "chosen": ptype,
                    "candidates": candidates,
                    "rule": "多数票优先，平票取高优先级用例，any 最后",
                })
            present_in = acc["param_cases"].get(name, set())
            required = len(present_in) == total_cases
            if not required:
                conflicts.append({
                    "kind": "required", "path": f"参数 {name}", "chosen": False,
                    "rule": "全部用例的请求都携带该参数才标必填",
                    "detail": f"{total_cases} 个用例中 {len(present_in)} 个携带",
                })
            params.append({"name": name, "type": ptype, "required": required})

        spec = normalize_spec({
            "request": {"params": params},
            "response": {"status_codes": sorted(set(acc["status_obs"])),
                         "fields": fields},
        })
        drafts[key] = {
            "method": acc["method"], "path": acc["path"], "spec": spec,
            "conflicts": conflicts,
            "sources": sorted(acc["cases"].values(), key=lambda c: c["case_name"]),
        }
    return drafts


# ---------------------------------------------------------------------------
# 契约管理器
# ---------------------------------------------------------------------------

class ContractManager:
    """契约与契约版本的管理。

    存储：契约本体放 ``contracts`` 集合，每次发布的版本放
    ``contract_versions`` 集合（不可变，即变更历史）。契约上的
    ``draft_spec`` 是尚未确认的提炼草稿，确认后发布为新版本。
    """

    def __init__(self, registry, build_registry=None):
        self._contracts = registry.store("contracts")
        self._versions = registry.store("contract_versions")
        self._cases = registry.store("cases")
        self._builds = build_registry

    # -- 基础 CRUD ---------------------------------------------------------
    def create(self, project_id: str, payload: dict) -> dict:
        method = str(payload.get("method") or "GET").upper()
        path = (payload.get("path") or "").strip()
        if not path:
            raise ValueError("接口路径不能为空")
        if not path.startswith("/"):
            path = "/" + path
        spec = payload.get("spec")
        if spec is not None:
            normalize_spec(spec)  # 先校验规格，避免留下没有版本的残档契约
        contract = {
            "id": new_id("ctr"),
            "project_id": project_id,
            "name": (payload.get("name") or "").strip() or f"{method} {path}",
            "description": payload.get("description", ""),
            "method": method,
            "path": path,
            "status": "draft",
            "current_version": 0,
            "draft_spec": None,
            "draft_conflicts": [],
            "created_at": time.time(),
        }
        self._contracts.insert(contract)
        if spec is not None:
            self.publish_version(contract["id"], spec,
                                 change_summary=payload.get("change_summary") or "初始版本",
                                 source="manual")
            contract = self._contracts.get(contract["id"])
        return contract

    def list(self, project_id: str) -> list[dict]:
        contracts = self._contracts.query(
            where=[("project_id", "eq", project_id)],
            order_by="created_at", order="desc")
        for c in contracts:
            c["has_draft"] = c.get("draft_spec") is not None
        return contracts

    def get(self, contract_id: str) -> Optional[dict]:
        contract = self._contracts.get(contract_id)
        if contract is None:
            return None
        contract = dict(contract)
        contract["has_draft"] = contract.get("draft_spec") is not None
        if contract.get("current_version"):
            version = self.get_version(contract_id, contract["current_version"])
            contract["spec"] = (version or {}).get("spec")
        else:
            contract["spec"] = None
        return contract

    def update(self, contract_id: str, patch: dict) -> Optional[dict]:
        allowed = {k: patch[k] for k in ("name", "description", "status")
                   if k in patch}
        if "status" in allowed and allowed["status"] not in CONTRACT_STATUSES:
            allowed["status"] = "active"
        return self._contracts.update(contract_id, allowed)

    def delete(self, contract_id: str) -> bool:
        for v in self.versions(contract_id):
            self._versions.delete(v["id"])
        return self._contracts.delete(contract_id)

    # -- 版本管理 ----------------------------------------------------------
    def publish_version(self, contract_id: str, spec: dict,
                        change_summary: str = "", source: str = "manual") -> dict:
        """发布新契约版本（规格先规范化，非法规格抛 ``ValueError``）。"""
        contract = self._contracts.get(contract_id)
        if contract is None:
            raise KeyError("契约不存在")
        norm = normalize_spec(spec)
        version_no = int(contract.get("current_version") or 0) + 1
        version = {
            "id": new_id("ctrv"),
            "contract_id": contract_id,
            "project_id": contract["project_id"],
            "version": version_no,
            "spec": norm,
            "source": source,
            "change_summary": change_summary or "",
            "created_at": time.time(),
        }
        self._versions.insert(version)
        self._contracts.update(contract_id, {
            "current_version": version_no,
            "status": "active",
        })
        return version

    def versions(self, contract_id: str) -> list[dict]:
        """版本历史（升序），每个版本附带相对上一版本的 diff。"""
        versions = self._versions.query(
            where=[("contract_id", "eq", contract_id)],
            order_by="version", order="asc")
        prev_spec = None
        for v in versions:
            v["diff_from_previous"] = diff_specs(prev_spec, v.get("spec"))
            prev_spec = v.get("spec")
        return versions

    def get_version(self, contract_id: str, version: int) -> Optional[dict]:
        found = self._versions.query(
            where=[("contract_id", "eq", contract_id),
                   ("version", "eq", int(version))], limit=1)
        return found[0] if found else None

    # -- 提炼草稿 ----------------------------------------------------------
    def extract_drafts(self, project_id: str,
                       case_ids: Optional[list] = None) -> list[dict]:
        """从项目用例的断言中提炼契约草稿，挂到对应契约上待确认。

        已有契约的接口：草稿写入 ``draft_spec``（不影响已发布版本）；
        没有契约的接口：新建 ``draft`` 状态的契约承载草稿。
        """
        if case_ids:
            cases = [c for c in self._cases.get_many(case_ids)
                     if c.get("project_id") == project_id]
        else:
            cases = self._cases.query(where=[("project_id", "eq", project_id)])
        drafts = extract_from_cases(cases)

        existing = {(c["method"], c["path"]): c
                    for c in self._contracts.query(
                        where=[("project_id", "eq", project_id)])}
        out = []
        for (method, path), draft in sorted(drafts.items()):
            contract = existing.get((method, path))
            is_new = contract is None
            if is_new:
                contract = self.create(project_id, {
                    "name": f"{method} {path}", "method": method, "path": path,
                })
            self._contracts.update(contract["id"], {
                "draft_spec": draft["spec"],
                "draft_conflicts": draft["conflicts"],
                "draft_sources": draft["sources"],
                "draft_updated_at": time.time(),
            })
            out.append({
                "contract_id": contract["id"], "is_new": is_new,
                "method": method, "path": path,
                "spec": draft["spec"], "conflicts": draft["conflicts"],
                "sources": draft["sources"],
            })
        return out

    def confirm_draft(self, contract_id: str,
                      change_summary: str = "") -> Optional[dict]:
        """确认草稿：把 ``draft_spec`` 发布为新版本并清空草稿。"""
        contract = self._contracts.get(contract_id)
        if contract is None or contract.get("draft_spec") is None:
            return None
        version = self.publish_version(
            contract_id, contract["draft_spec"],
            change_summary=change_summary or "确认提炼草稿",
            source="extracted")
        self._contracts.update(contract_id, {
            "draft_spec": None, "draft_conflicts": [], "draft_sources": [],
        })
        return version

    def discard_draft(self, contract_id: str) -> Optional[dict]:
        """放弃草稿，不影响已发布版本。"""
        contract = self._contracts.get(contract_id)
        if contract is None:
            return None
        return self._contracts.update(contract_id, {
            "draft_spec": None, "draft_conflicts": [], "draft_sources": [],
        })

    # -- 执行时校验 ---------------------------------------------------------
    def active_specs(self, project_id: str) -> list[dict]:
        """项目当前生效的契约（供执行器对照校验）。"""
        out = []
        contracts = self._contracts.query(
            where=[("project_id", "eq", project_id),
                   ("status", "eq", "active")])
        for c in contracts:
            if not c.get("current_version"):
                continue
            version = self.get_version(c["id"], c["current_version"])
            if version:
                out.append({
                    "contract_id": c["id"], "name": c.get("name"),
                    "method": c["method"], "path": c["path"],
                    "version": version["version"], "spec": version["spec"],
                })
        return out

    # -- 升级影响分析 -------------------------------------------------------
    def impact(self, contract_id: str, from_version: Optional[int] = None,
               to_version: Optional[int] = None,
               build_limit: int = 20) -> dict:
        """契约升级影响分析。

        回答三个问题：
        1. ``cases_outdated``   哪些用例还在按旧契约断言（断言了被移除/改型
           的字段、期望了不再允许的状态码、或未覆盖新必填字段）；
        2. ``builds_outdated``  哪些历史构建是按旧版本校验的（构建记录了
           当时的契约版本快照）；
        3. ``verdict_changes``  同一份历史响应在新旧契约下判定是否翻转
           （用结果里留存的响应快照按新版本重新校验）。
        """
        contract = self._contracts.get(contract_id)
        if contract is None:
            return {"error": "契约不存在"}
        current_no = int(to_version or contract.get("current_version") or 0)
        base_no = int(from_version or max(0, current_no - 1))
        current = self.get_version(contract_id, current_no)
        base = self.get_version(contract_id, base_no) if base_no else None
        if current is None:
            return {"error": "契约还没有已发布的版本"}

        current_spec = current["spec"]
        base_spec = (base or {}).get("spec")
        diff = diff_specs(base_spec, current_spec)

        # 1) 用例维度：把每条用例自身的断言提炼成小契约，与新版本对照
        cases_outdated = []
        project_cases = self._cases.query(
            where=[("project_id", "eq", contract["project_id"])])
        key = (contract["method"], contract["path"])
        cur_fields = {f["path"]: f
                      for f in current_spec["response"]["fields"]}
        cur_codes = set(current_spec["response"]["status_codes"])
        for case in project_cases:
            drafts = extract_from_cases([case])
            draft = drafts.get(key)
            if draft is None:
                continue
            issues = []
            case_spec = draft["spec"]
            for f in case_spec["response"]["fields"]:
                cur = cur_fields.get(f["path"])
                if cur is None:
                    issues.append(f"断言了 v{current_no} 中已移除的字段 {f['path']}")
                elif (f["type"] not in ("any", cur["type"])
                      and cur["type"] != "any"):
                    issues.append(f"字段 {f['path']} 断言类型 {f['type']} 与 "
                                  f"v{current_no} 契约的 {cur['type']} 不一致")
            asserted = {f["path"] for f in case_spec["response"]["fields"]}
            for f in current_spec["response"]["fields"]:
                if f.get("required") and f["path"] not in asserted:
                    issues.append(f"未覆盖 v{current_no} 必填字段 {f['path']}")
            for code in case_spec["response"]["status_codes"]:
                if cur_codes and code not in cur_codes:
                    issues.append(f"期望状态码 {code} 不在 v{current_no} 允许范围")
            if issues:
                cases_outdated.append({
                    "case_id": case.get("id"), "case_name": case.get("name"),
                    "priority": case.get("priority"), "issues": issues,
                })

        # 2) 构建维度：契约版本快照落后于当前版本的历史构建
        builds_outdated = []
        verdict_changes = []
        revalidated = 0
        if self._builds is not None:
            store = self._builds.for_project(contract["project_id"])
            builds = store.list_builds()
            for b in builds:
                snap = (b.get("contract_snapshot") or {}).get(contract_id)
                if snap is None:
                    continue
                if snap < current_no:
                    builds_outdated.append({
                        "build_id": b["id"], "name": b.get("name") or b["id"],
                        "checked_version": snap, "current_version": current_no,
                        "status": b.get("status"),
                        "finished_at": b.get("finished_at"),
                    })
            # 3) 判定差异：历史响应快照按新版本重新校验
            for b in builds[:build_limit]:
                if len(verdict_changes) >= 50:
                    break
                for r in store.results(b["id"]):
                    if len(verdict_changes) >= 50:
                        break
                    for check in r.get("contract_checks") or []:
                        if check.get("contract_id") != contract_id:
                            continue
                        snapshot = check.get("response_snapshot") or {}
                        if snapshot.get("_truncated"):
                            continue
                        revalidated += 1
                        new_violations = validate_against_spec(
                            current_spec,
                            response_status=snapshot.get("status"),
                            response_body=snapshot.get("body"))
                        new_ok = not new_violations
                        if new_ok != bool(check.get("ok")):
                            verdict_changes.append({
                                "build_id": b["id"],
                                "case_id": r.get("case_id"),
                                "case_name": r.get("case_name"),
                                "checked_version": check.get("version"),
                                "current_version": current_no,
                                "old_ok": bool(check.get("ok")),
                                "new_ok": new_ok,
                                "violations_now": new_violations,
                            })

        return {
            "contract_id": contract_id,
            "from_version": base_no,
            "to_version": current_no,
            "diff": diff,
            "cases_outdated": cases_outdated,
            "builds_outdated": builds_outdated,
            "verdict_changes": verdict_changes,
            "revalidated_checks": revalidated,
        }

    def compare(self, contract_id: str, version_a: int, version_b: int,
                response: dict) -> dict:
        """同一份响应在两个契约版本下的判定对比。"""
        va = self.get_version(contract_id, version_a)
        vb = self.get_version(contract_id, version_b)
        if va is None or vb is None:
            return {"error": "版本不存在"}
        response = response or {}
        va_violations = validate_against_spec(
            va["spec"], response_status=response.get("status"),
            response_body=response.get("body"))
        vb_violations = validate_against_spec(
            vb["spec"], response_status=response.get("status"),
            response_body=response.get("body"))
        return {
            "a": {"version": va["version"], "ok": not va_violations,
                  "violations": va_violations},
            "b": {"version": vb["version"], "ok": not vb_violations,
                  "violations": vb_violations},
            "verdict_changed": bool(va_violations) != bool(vb_violations),
        }


def snapshot_response(body: Any) -> Any:
    """响应体快照：小则原样留存（供升级后重新校验），大则只留截断标记。"""
    try:
        text = json.dumps(body, ensure_ascii=False, default=str)
    except (TypeError, ValueError):
        return {"_truncated": True}
    if len(text) <= _SNAPSHOT_LIMIT:
        return body
    return {"_truncated": True, "preview": text[:_SNAPSHOT_LIMIT]}

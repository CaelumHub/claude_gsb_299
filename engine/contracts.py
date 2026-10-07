"""接口契约管理：契约模型、从用例断言自动提炼草稿、响应契约校验。

一份接口契约描述一个 HTTP 接口（同项目内 ``method + path`` 唯一）的：

- 路径 / 方法；
- 请求参数（query / header / body 字段、类型、必填）；
- 响应：状态码集合、响应体字段（点路径）、类型、必填项。

契约按 **项目 + 版本** 管理：每次发布生成一个新版本（版本内容不可变），
项目指针标记当前生效版本。用例与历史报告在执行时记录「当时校验所用的
契约版本号」，因此契约升级后可以：

- 找出仍绑定旧版本的用例 / 历史报告；
- 用新版本重新判定同一份历史响应，对比新旧契约下的判定差异。

「契约违背」（contract_violated）是区别于用例断言失败的独立检查结果：
用例断言写的是「我这条用例关心什么」，契约校验的是「接口有没有遵守
所有人的统一约定」，两者互不替代。

自动提炼
--------
:func:`extract_drafts` 扫描用例步骤，把请求步骤与其后的断言关联起来，
从 ``${resp.status}`` / ``${resp.body.xxx}`` 等引用中归纳字段、类型、
必填性与状态码。多条用例对同一字段的断言可能互相矛盾（例如一个断言
``id`` 等于整数、另一个断言它等于字符串），此时按明确的优先级**取舍**
而不是全部收录，矛盾细节保留在草稿的 ``conflicts`` 里供确认。
"""

from __future__ import annotations

import re
from typing import Any, Optional

from .models import CASE_STATUSES, new_id

# 契约违背作为一种新的单条用例检查结果
CONTRACT_VIOLATED = "contract_violated"
CASE_STATUSES_WITH_CONTRACT = CASE_STATUSES + [CONTRACT_VIOLATED]

# 支持的字段类型（提炼 / 手编契约共用）
FIELD_TYPES = ("string", "integer", "number", "boolean", "object", "array", "null")

# 提炼时同一字段出现互相矛盾的证据，如何取舍
CONFLICT_STRATEGIES = ("majority", "strict", "latest", "ignore")

_VAR_RE = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_.]*)\}")

# 点路径分段：a.b[0].c -> ["a", "b", 0, "c"]
_PATH_TOKEN = re.compile(r"[^.\[\]]+|\[\d+\]")


# ---------------------------------------------------------------------------
# 类型推断
# ---------------------------------------------------------------------------

def infer_type(value: Any) -> str:
    """把 Python / JSON 值推断成契约字段类型。"""
    # 注意：bool 是 int 的子类，必须先判
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
    if value is None:
        return "null"
    return "string"


def type_matches(value: Any, declared: str) -> bool:
    """检查实际值是否符合声明的契约类型。

    ``number`` 同时接受整数；``null`` 仅用于显式允许 null；
    其余严格对应。
    """
    if declared == "number":
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    return infer_type(value) == declared


# ---------------------------------------------------------------------------
# 路径工具
# ---------------------------------------------------------------------------

def normalize_path(path: str) -> str:
    """/api/users/{id} 形式归一化（去 query、去末尾斜杠），作为接口标识。"""
    if not path:
        return ""
    path = str(path).split("?", 1)[0].strip()
    if len(path) > 1:
        path = path.rstrip("/")
    return path


def split_field_path(expr: str) -> Optional[tuple[str, str]]:
    """识别断言表达式是否引用了响应。

    支持 ``${resp.status}``、``${resp.body.data.id}`` 这类纯引用，以及
    字符串中夹带引用（取第一个响应引用）。返回 ``(scope, dotted)``：

    - ``${resp.status}``      -> ("status", "")
    - ``${resp.body.a.b}``    -> ("body", "a.b")
    - 非响应引用 / 无引用      -> None
    """
    if not isinstance(expr, str):
        return None
    m = _VAR_RE.search(expr)
    if not m:
        return None
    parts = m.group(1).split(".")
    if not parts or parts[0] != "resp":
        return None
    if len(parts) >= 2 and parts[1] == "body":
        return "body", ".".join(parts[2:])
    if len(parts) == 2 and parts[1] == "status":
        return "status", ""
    return None


def _lookup(path: str, root: Any) -> Any:
    """按 ``a.b.0.c`` 点路径取值，缺失抛 :class:`ContractLookupError`。"""
    cur = root
    for raw in path.split("."):
        if raw == "":
            raise ContractLookupError("空路径段")
        idx: Optional[int] = None
        key = raw
        m = re.fullmatch(r"\[(\d+)\]", raw)
        if m:
            idx = int(m.group(1))
            key = None
        if idx is not None:
            if not isinstance(cur, list) or idx >= len(cur):
                raise ContractLookupError(f"数组下标越界: {raw}")
            cur = cur[idx]
        else:
            if not isinstance(cur, dict) or key not in cur:
                raise ContractLookupError(f"字段缺失: {key}")
            cur = cur[key]
    return cur


class ContractLookupError(Exception):
    """响应体中找不到契约声明的路径。"""


# ---------------------------------------------------------------------------
# 契约模型
# ---------------------------------------------------------------------------

def _empty_spec() -> dict:
    return {"status_codes": [], "request": [], "response_fields": []}


def new_contract(project_id: str, name: str, method: str, path: str,
                 spec: Optional[dict] = None) -> dict:
    """构造一份尚无版本的契约记录。"""
    method = (method or "GET").upper()
    path = normalize_path(path)
    return {
        "id": new_id("contract"),
        "project_id": project_id,
        "name": name or f"{method} {path}",
        "method": method,
        "path": path,
        "current_version": None,
        "versions": [],
        "created_at": _now(),
        "updated_at": _now(),
        "draft": {"spec": spec or _empty_spec(), "conflicts": [],
                  "source_case_ids": [], "generated_at": None},
    }


def _now() -> float:
    import time
    return time.time()


def publish_version(contract: dict, spec: dict, note: str = "",
                    source: str = "manual", case_ids: Optional[list] = None) -> dict:
    """为契约追加一个不可变新版本，并把它置为当前版本。

    返回新版本对象。版本号在该契约内自增（v1、v2…），内容深拷贝后冻结，
    之后再改草稿不影响历史版本。
    """
    import copy
    versions = contract.setdefault("versions", [])
    version_no = len(versions) + 1
    version = {
        "version": version_no,
        "spec": copy.deepcopy(spec),
        "note": note or "",
        "source": source,
        "case_ids": list(case_ids or []),
        "created_at": _now(),
    }
    versions.append(version)
    contract["current_version"] = version_no
    contract["updated_at"] = _now()
    return version


def get_version(contract: dict, version_no: Optional[int] = None) -> Optional[dict]:
    """取指定版本（默认当前版本），不存在返回 None。"""
    versions = contract.get("versions") or []
    if not versions:
        return None
    if version_no is None:
        version_no = contract.get("current_version")
    if version_no is None:
        return None
    for v in versions:
        if v.get("version") == version_no:
            return v
    return None


def current_spec(contract: dict) -> Optional[dict]:
    v = get_version(contract)
    return v["spec"] if v else None


# ---------------------------------------------------------------------------
# 自动提炼：从用例步骤与断言归纳契约草稿
# ---------------------------------------------------------------------------

# 断言类型 -> 对字段的约束含义
#   presence: 仅证明字段存在（json_path / truthy / 各种比较的 actual 端）
#   exact:    期望值给出了字段的一个精确取值（可用于类型 / enum 推断）
def _iter_case_evidence(case: dict) -> list[dict]:
    """从单条用例提取「接口证据」：请求 + 每条断言对响应字段的约束。

    返回若干 ``{case_id, case_name, method, path, status?, field?, kind,
    value?}``；非响应相关断言（纯脚本变量等）忽略。
    """
    steps = case.get("steps") or []
    evidence: list[dict] = []
    # 记录最近一次请求步骤，断言默认归属它
    cur_req: Optional[dict] = None
    for step in steps:
        action = step.get("action")
        if action == "request":
            cur_req = {
                "method": (step.get("method") or "GET").upper(),
                "path": normalize_path(str(step.get("url") or "/")),
                "params": step.get("params") or {},
                "headers": step.get("headers") or {},
                "body": step.get("body"),
                "save_as": step.get("save_as") or "resp",
            }
        elif action == "assert" and cur_req is not None:
            atype = step.get("type", "equals")
            actual = step.get("actual")
            expected = step.get("expected")
            ref = split_field_path(actual)
            if ref is None:
                continue
            scope, dotted = ref
            base = {"case_id": case.get("id"), "case_name": case.get("name"),
                    "method": cur_req["method"], "path": cur_req["path"]}
            if scope == "status":
                # status / equals 作用于 resp.status 时给出精确状态码
                codes: list[int] = []
                if atype in ("status", "equals") and _as_int(expected) is not None:
                    codes.append(_as_int(expected))
                elif atype == "in" and isinstance(expected, (list, tuple)):
                    codes = [c for c in (_as_int(x) for x in expected) if c is not None]
                for code in codes:
                    evidence.append({**base, "kind": "status", "status": code})
                continue
            # scope == body
            field = dotted
            if not field:
                continue
            ev = {**base, "kind": "presence", "field": field,
                  "value_type": None, "value": None}
            if atype == "equals":
                ev["kind"] = "exact"
                ev["value"] = expected
                ev["value_type"] = infer_type(expected)
            elif atype in ("gt", "gte", "lt", "lte", "between"):
                # 比较运算暗示数值类型
                ev["value_type"] = "number"
            elif atype == "truthy":
                # truthy 本身只能证明存在；但断言里显式给了布尔期望值时
                # （如 expected=True），可作为布尔类型的弱证据
                if isinstance(expected, bool):
                    ev["value_type"] = "boolean"
            evidence.append(ev)
    return evidence


def _as_int(x: Any) -> Optional[int]:
    if isinstance(x, bool):
        return None
    if isinstance(x, int):
        return x
    if isinstance(x, str) and re.fullmatch(r"\d{3}", x.strip()):
        try:
            return int(x.strip())
        except ValueError:
            return None
    return None


def extract_drafts(cases: list[dict], strategy: str = "majority") -> list[dict]:
    """扫描多条用例，按 ``method + path`` 聚合出契约草稿。

    每个接口返回::

        {
          "method", "path", "name",
          "spec": {status_codes, request, response_fields},
          "source_case_ids": [...],
          "conflicts": [ {field, candidates: {type: count...}, chosen, reason} ],
        }

    矛盾取舍由 ``strategy`` 决定：

    - ``majority``（默认）：多数证据决定，平票时取更严格的类型；
    - ``strict``：必填性取「必填」，类型只在全部一致时采纳否则留空待确认；
    - ``latest``：以创建时间最新的用例证据为准；
    - ``ignore``：类型冲突时不生成该字段（只保留存在性）。
    """
    if strategy not in CONFLICT_STRATEGIES:
        strategy = "majority"

    # key -> 聚合桶
    buckets: dict[tuple[str, str], dict] = {}

    def _bucket(method: str, path: str) -> dict:
        key = (method, path)
        b = buckets.get(key)
        if b is None:
            b = {
                "method": method, "path": path,
                "name": f"{method} {path}",
                "case_order": [],
                "case_meta": {},
                "status_codes": {},      # code -> [case_id...]
                "fields": {},            # dotted -> {types: {t: n}, exact: set,
                                         #          cases: [], last_case_id}
            }
            buckets[key] = b
        return b

    for case in cases:
        for ev in _iter_case_evidence(case):
            b = _bucket(ev["method"], ev["path"])
            cid = ev.get("case_id")
            if cid and cid not in b["case_order"]:
                b["case_order"].append(cid)
                b["case_meta"][cid] = case.get("created_at", 0)
            if ev["kind"] == "status":
                b["status_codes"].setdefault(ev["status"], [])
                if cid not in b["status_codes"][ev["status"]]:
                    b["status_codes"][ev["status"]].append(cid)
                continue
            field = ev["field"]
            fb = b["fields"].setdefault(field, {
                "types": {}, "exact_values": [], "cases": [],
                "last_case_id": cid, "last_created": case.get("created_at", 0),
            })
            if cid and cid not in fb["cases"]:
                fb["cases"].append(cid)
            t = ev.get("value_type")
            if t:
                fb["types"][t] = fb["types"].get(t, 0) + 1
            if ev["kind"] == "exact" and ev.get("value") is not None:
                fb["exact_values"].append({"value": ev["value"], "type": t,
                                           "case_id": cid})
            if case.get("created_at", 0) >= fb["last_created"]:
                fb["last_created"] = case.get("created_at", 0)
                fb["last_case_id"] = cid

    drafts: list[dict] = []
    for (method, path), b in sorted(buckets.items(), key=lambda kv: kv[0]):
        spec = _empty_spec()
        spec["status_codes"] = sorted(b["status_codes"].keys())
        conflicts: list[dict] = []
        for field, fb in sorted(b["fields"].items()):
            chosen_type, conflict = _resolve_field_type(field, fb, strategy)
            if conflict is not None:
                conflicts.append(conflict)
            # ignore 策略：保留字段（存在性 / 必填性仍可校验），只放弃类型约束
            # 类型证据存在矛盾时不归纳枚举，避免把「错误类型的那部分证据」
            # 误当合法取值收进契约
            enum_values = [] if conflict is not None else _enum_values(fb, chosen_type or "")
            spec["response_fields"].append({
                "path": field,
                "type": chosen_type or "",
                "required": _resolve_required(fb, b, strategy),
                "enum": enum_values,
                "description": "",
            })
        drafts.append({
            "method": method,
            "path": path,
            "name": b["name"],
            "spec": spec,
            "source_case_ids": b["case_order"],
            "conflicts": conflicts,
            "strategy": strategy,
            "generated_at": _now(),
        })
    return drafts


# 类型严格度：平票 / strict 场景下更严格（信息更具体）的优先
_TYPE_RANK = {"boolean": 6, "integer": 5, "number": 4, "string": 3,
              "array": 2, "object": 2, "null": 1}


def _resolve_field_type(field: str, fb: dict, strategy: str):
    """返回 (chosen_type, conflict_record_or_None)。chosen_type 可能为 ''。"""
    types = fb.get("types") or {}
    total = sum(types.values())
    conflict = None
    if len(types) <= 1:
        return (next(iter(types), "")), None

    # 存在矛盾：integer 与 number 同源（int 也是 number），不算矛盾
    if set(types) <= {"integer", "number"}:
        return "number", None

    candidates = sorted(types.items(), key=lambda kv: (-kv[1], -_TYPE_RANK.get(kv[0], 0)))
    top_count = candidates[0][1]
    tied = [t for t, n in candidates if n == top_count]

    if strategy == "majority":
        # 平票时取严格度更高者
        chosen = sorted(tied, key=lambda t: -_TYPE_RANK.get(t, 0))[0]
    elif strategy == "strict":
        chosen = ""  # 证据不一致，留空让人确认
    elif strategy == "latest":
        latest = fb.get("last_case_id")
        chosen = ""
        for ev in fb.get("exact_values", []):
            if ev.get("case_id") == latest and ev.get("type"):
                chosen = ev["type"]
    else:  # ignore：保留字段、放弃类型约束
        chosen = ""

    conflict = {
        "field": field,
        "kind": "type",
        "candidates": types,
        "total_evidence": total,
        "chosen": chosen or "",
        "strategy": strategy,
        "case_ids": fb.get("cases", []),
        "reason": _conflict_reason(strategy, types, chosen),
    }
    return chosen, conflict


def _conflict_reason(strategy: str, types: dict, chosen: Optional[str]) -> str:
    desc = "、".join(f"{t}×{n}" for t, n in sorted(types.items(),
                                                   key=lambda kv: -kv[1]))
    if strategy == "majority":
        return f"字段类型证据矛盾（{desc}），按多数证据取 {chosen or '空'}；平票取更严格类型"
    if strategy == "strict":
        return f"字段类型证据矛盾（{desc}），严格模式下类型留空，需人工确认"
    if strategy == "latest":
        return f"字段类型证据矛盾（{desc}），以最新用例证据取 {chosen or '空'}"
    return f"字段类型证据矛盾（{desc}），忽略该字段的类型约束"


def _resolve_required(fb: dict, bucket: dict, strategy: str) -> bool:
    """必填性取舍。

    覆盖到该字段的用例 / 覆盖到该接口的用例比例：

    - ``strict``：只要有用例断言了该字段就视为必填；
    - 其余策略：超过半数用例断言才必填（个别用例独有的字段多为场景特有）。
    """
    total_cases = len(bucket["case_order"]) or 1
    hits = len(fb.get("cases", []))
    if strategy == "strict":
        return hits > 0
    return hits * 2 > total_cases


def _enum_values(fb: dict, chosen_type: str) -> list:
    """当精确取值证据很少（<=3）且类型一致时，归纳为 enum 候选。"""
    vals = []
    seen = set()
    for ev in fb.get("exact_values", []):
        v = ev.get("value")
        t = ev.get("type")
        if chosen_type and t and not _compatible(t, chosen_type):
            continue
        key = repr(v)
        if key in seen:
            continue
        seen.add(key)
        vals.append(v)
    return vals if len(vals) <= 3 else []


def _compatible(evidence_type: str, declared: str) -> bool:
    if evidence_type == declared:
        return True
    if declared == "number" and evidence_type == "integer":
        return True
    return False


# ---------------------------------------------------------------------------
# 契约校验：响应 vs 契约版本
# ---------------------------------------------------------------------------

def validate_response(spec: dict, status: Any, body: Any) -> dict:
    """用一份契约版本校验实际响应，返回结构化校验结果。

    ::

        {
          "ok": bool,
          "violations": [
             {"kind": "status"|"type"|"required"|"enum", "field", "expected",
              "actual", "message"}
          ],
          "checked_fields": int,
        }
    """
    violations: list[dict] = []
    checked = 0

    # 1) 状态码（契约声明了才校验）
    codes = spec.get("status_codes") or []
    if codes:
        checked += 1
        try:
            code_int = int(status)
        except (TypeError, ValueError):
            code_int = None
        if code_int not in codes:
            violations.append({
                "kind": "status", "field": "$status",
                "expected": codes, "actual": status,
                "message": f"状态码 {status!r} 不在契约允许集合 {codes} 内",
            })

    # 2) 响应字段
    for field in spec.get("response_fields") or []:
        path = field.get("path")
        if not path:
            continue
        required = bool(field.get("required"))
        declared = field.get("type") or ""
        enum = field.get("enum") or []
        try:
            value = _lookup(path, body)
        except ContractLookupError as exc:
            if required:
                checked += 1
                violations.append({
                    "kind": "required", "field": path,
                    "expected": "字段存在", "actual": None,
                    "message": f"必填响应字段缺失：{path}（{exc}）",
                })
            continue

        checked += 1
        if declared and not type_matches(value, declared):
            violations.append({
                "kind": "type", "field": path,
                "expected": declared, "actual": infer_type(value),
                "message": f"字段 {path} 类型应为 {declared}，实际 {infer_type(value)}（值 {value!r}）",
            })
        elif enum and value not in enum and (not declared or type_matches(value, declared)):
            violations.append({
                "kind": "enum", "field": path,
                "expected": enum, "actual": value,
                "message": f"字段 {path} 取值 {value!r} 不在契约枚举 {enum} 内",
            })

    return {"ok": not violations, "violations": violations,
            "checked_fields": checked}


def validate_request(spec: dict, method: str, params: dict, headers: dict,
                     body: Any) -> dict:
    """校验请求是否满足契约声明的必填请求参数（用于发请求前的前置检查）。"""
    violations: list[dict] = []
    for field in spec.get("request") or []:
        if not field.get("required"):
            continue
        location = field.get("in", "query")
        name = field.get("name") or field.get("path")
        container = {"query": params or {}, "header": headers or {},
                     "body": body if isinstance(body, dict) else {}}.get(location, {})
        if name not in container or container[name] in (None, ""):
            violations.append({
                "kind": "required", "field": f"{location}:{name}",
                "expected": "请求参数存在", "actual": None,
                "message": f"缺少必填请求参数 {location}.{name}",
            })
    return {"ok": not violations, "violations": violations}


# ---------------------------------------------------------------------------
# 新旧契约对比
# ---------------------------------------------------------------------------

def diff_specs(old: Optional[dict], new: Optional[dict]) -> dict:
    """对比两个版本的契约内容，输出破坏性 / 兼容性变更。"""
    old = old or _empty_spec()
    new = new or _empty_spec()
    breaking: list[dict] = []
    compatible: list[dict] = []

    old_fields = {f["path"]: f for f in old.get("response_fields", [])}
    new_fields = {f["path"]: f for f in new.get("response_fields", [])}

    for path, f in new_fields.items():
        if path not in old_fields:
            (breaking if f.get("required") else compatible).append({
                "kind": "field_added", "field": path,
                "required": bool(f.get("required")),
                "message": f"新增{'必填' if f.get('required') else '可选'}字段 {path}",
            })
            continue
        of = old_fields[path]
        if of.get("type") and f.get("type") and of["type"] != f["type"]:
            breaking.append({
                "kind": "type_changed", "field": path,
                "from": of["type"], "to": f["type"],
                "message": f"字段 {path} 类型 {of['type']} → {f['type']}",
            })
        if not of.get("required") and f.get("required"):
            breaking.append({
                "kind": "required_added", "field": path,
                "message": f"字段 {path} 由可选变为必填",
            })
        if of.get("required") and not f.get("required"):
            compatible.append({
                "kind": "required_relaxed", "field": path,
                "message": f"字段 {path} 由必填放宽为可选",
            })

    for path in old_fields:
        if path not in new_fields:
            breaking.append({
                "kind": "field_removed", "field": path,
                "message": f"字段 {path} 被移除",
            })

    old_codes = set(old.get("status_codes") or [])
    new_codes = set(new.get("status_codes") or [])
    for code in sorted(new_codes - old_codes):
        compatible.append({"kind": "status_added", "field": "$status",
                           "value": code, "message": f"新增允许状态码 {code}"})
    for code in sorted(old_codes - new_codes):
        breaking.append({"kind": "status_removed", "field": "$status",
                         "value": code, "message": f"不再允许状态码 {code}"})

    return {"breaking": breaking, "compatible": compatible,
            "has_breaking": bool(breaking)}

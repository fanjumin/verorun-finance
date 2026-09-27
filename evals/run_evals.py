#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
run_evals.py — VeroRun 离线评测与提示词资产纳管（WP-B6）
=====================================================================
设计约束（CI 用 Python 3.11，且不安装项目运行时依赖）：
  * **仅标准库**：ast / json / hashlib / argparse / os / sys —— 不 import 任何项目模块，
    因此不需要 PG、不需要 flask / apscheduler / cryptography，任何环境都能跑。
  * **只读**：只读源码文本与 evals/ 下的数据文件，绝不写业务文件、绝不连数据库。
  * `--update-snapshots` 仅写 evals/prompt_snapshots/ 下的机器产物（index.json / *.txt），
    不触碰人工维护的 contracts.json。

本脚本守护三类事故（均来自本仓真实教训）：
  1. claims.json      —— 「修复点被静默改回」。A1/A3/A4/A5/A6 的正确实现以**原文子串**
                          断言，任何人改回旧实现即判红。
  2. prompt_snapshots —— 「i18n 自动化再次改写发给 LLM 的提示词契约」。断言提示词字面量的
                          内容指纹 + 关键契约子串。这是 i18n 门禁（只拦 `_(`）的补位——
                          它拦不住"键名被改名"这类不带 `_(` 的漂移，而那正是 A1 事故根因
                          （`confidence_` 与读取端 `confidence` 不匹配 → 自评恒为 0.85）。
  3. golden/tasks.json—— 黄金任务集的结构与契约存在性。**离线模式不调用模型**，
                          只校验数据集自洽与"期望字段确实写在提示词契约里"，不校验模型输出质量
                          （在线评测需要 LLM 凭据，属后续工作，见 README）。

用法：
    python evals/run_evals.py --offline              # 门禁模式（CI 用），任一失败 exit 1
    python evals/run_evals.py --offline -v           # 附带通过项明细
    python evals/run_evals.py --update-snapshots     # 人工确认提示词变更后刷新快照指纹

提示词字面量提取口径（保证 3.11 / 3.12 / 3.13 结果一致）：
    用 ast 解析源码，把「相邻字符串隐式拼接」「f-string」统一归集为一个"字面量骨架"：
      * 纯字符串部分 → 原样保留
      * f-string 的插值段 → 统一替换为 `{*}` 占位符（因此插值结构变化也可被检出）
      * 转义花括号 `{{` / `}}` → 按运行时语义展开为 `{` / `}`
    骨架是**规范化**结果，与引号风格、换行缩进、拼接方式无关，只随提示词语义变化。
"""
from __future__ import annotations

import argparse
import ast
import difflib
import hashlib
import json
import os
import sys

EVALS_DIR = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(EVALS_DIR)

CLAIMS_PATH = os.path.join(EVALS_DIR, "claims.json")
CONTRACTS_PATH = os.path.join(EVALS_DIR, "prompt_snapshots", "contracts.json")
SNAPSHOT_INDEX_PATH = os.path.join(EVALS_DIR, "prompt_snapshots", "index.json")
SNAPSHOT_TEXT_DIR = os.path.join(EVALS_DIR, "prompt_snapshots")
GOLDEN_PATH = os.path.join(EVALS_DIR, "golden", "tasks.json")

INTERP = "{*}"

PASS, FAIL, SKIP = "PASS", "FAIL", "SKIP"


# ══════════════════════════ 结果收集 ══════════════════════════
class Report:
    """把每个断言的结果收集起来，最后统一打印——避免中途 exit 导致信息不全。"""

    def __init__(self) -> None:
        self.rows = []  # list[(status, name, detail)]

    def add(self, status: str, name: str, detail: str = "") -> None:
        self.rows.append((status, name, detail))

    def ok(self, name: str, detail: str = "") -> None:
        self.add(PASS, name, detail)

    def fail(self, name: str, detail: str = "") -> None:
        self.add(FAIL, name, detail)

    def skip(self, name: str, detail: str = "") -> None:
        self.add(SKIP, name, detail)

    @property
    def failures(self):
        return [r for r in self.rows if r[0] == FAIL]

    def dump(self, verbose: bool = False) -> None:
        icon = {PASS: "[PASS]", FAIL: "[FAIL]", SKIP: "[SKIP]"}
        for status, name, detail in self.rows:
            if status == PASS and not verbose:
                continue
            line = f"  {icon[status]} {name}"
            if detail:
                line += f"\n         {detail.replace(chr(10), chr(10) + ' ' * 9)}"
            print(line)

    def summary(self) -> str:
        n_pass = sum(1 for r in self.rows if r[0] == PASS)
        n_fail = sum(1 for r in self.rows if r[0] == FAIL)
        n_skip = sum(1 for r in self.rows if r[0] == SKIP)
        return (f"合计 {len(self.rows)} 项：PASS={n_pass}  FAIL={n_fail}  SKIP={n_skip}")


# ══════════════════════ 提示词字面量提取 ══════════════════════
def _skeleton(node: ast.AST):
    """返回节点的「字面量骨架」；不是字符串表达式则返回 None。"""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.JoinedStr):  # f-string
        parts = []
        for v in node.values:
            if isinstance(v, ast.Constant) and isinstance(v.value, str):
                parts.append(v.value)
            else:
                parts.append(INTERP)  # 插值段：只留位置标记，不展开表达式
        return "".join(parts)
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        left = _skeleton(node.left)
        right = _skeleton(node.right)
        if left is None or right is None:
            return None  # 非「字符串 + 字符串」，视为普通算术
        return left + right
    return None


_LITERAL_CACHE: dict = {}


def extract_literals(rel_path: str):
    """提取文件内所有「最外层字符串表达式」的字面量骨架。

    返回 [(行号, 骨架文本)]，按行号升序。结果按文件缓存，避免重复解析。
    """
    if rel_path in _LITERAL_CACHE:
        return _LITERAL_CACHE[rel_path]

    abs_path = os.path.join(ROOT, rel_path.replace("/", os.sep))
    if not os.path.isfile(abs_path):
        _LITERAL_CACHE[rel_path] = None
        return None
    try:
        with open(abs_path, "rb") as fh:
            src = fh.read()
        tree = ast.parse(src, filename=rel_path)
    except (SyntaxError, ValueError) as exc:
        raise RuntimeError(f"{rel_path}: 解析失败：{exc}") from exc

    parents = {}
    for node in ast.walk(tree):
        for child in ast.iter_child_nodes(node):
            parents[child] = node

    found = []
    for node in ast.walk(tree):
        text = _skeleton(node)
        if text is None:
            continue
        parent = parents.get(node)
        if parent is not None and _skeleton(parent) is not None:
            continue  # 非最外层（例如 f-string 内部的字面量片段）
        found.append((getattr(node, "lineno", 0), text))
    found.sort(key=lambda t: (t[0], -len(t[1])))

    _LITERAL_CACHE[rel_path] = found
    return found


def find_literal(rel_path: str, anchor: str):
    """定位含 anchor 的字面量，返回 (行号, 骨架文本)；未命中返回 (None, None)。"""
    literals = extract_literals(rel_path)
    if not literals:
        return None, None
    for lineno, text in literals:
        if anchor in text:
            return lineno, text
    return None, None


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _read_snapshot_text(key: str):
    """读取已提交的快照全文（<key>.txt）。缺失返回 None。"""
    path = os.path.join(SNAPSHOT_TEXT_DIR, f"{key}.txt")
    if not os.path.isfile(path):
        return None
    with open(path, "r", encoding="utf-8", newline="") as fh:
        return fh.read()


def _diff_preview(old: str, new: str, limit: int = 16) -> str:
    """用统一 diff 展示「已提交快照 → 当前字面量」的差异（截断）。

    作用：门禁判红时能自解释是「有人改了提示词」还是「提取口径变了」，
    而不是只丢一个哈希。行数截断避免刷屏。
    """
    lines = list(difflib.unified_diff(
        old.splitlines(), new.splitlines(),
        fromfile="committed-snapshot", tofile="current-literal", lineterm="", n=1))
    if not lines:
        return "        （文本内容一致，差异仅在不可见字符层面）"
    head = lines[:limit]
    out = ["        ----- 差异（前 %d 行）-----" % len(head)]
    out += ["        " + ln for ln in head]
    if len(lines) > limit:
        out.append(f"        ……（共 {len(lines)} 行差异，已截断）")
    return "\n".join(out)


def read_text(rel_path: str):
    abs_path = os.path.join(ROOT, rel_path.replace("/", os.sep))
    if not os.path.isfile(abs_path):
        return None
    with open(abs_path, "r", encoding="utf-8", errors="replace") as fh:
        return fh.read()


def load_json(path: str):
    with open(path, "r", encoding="utf-8") as fh:
        return json.load(fh)


# ══════════════════════════ 1. 锚点断言 ══════════════════════════
def check_claims(report: Report) -> None:
    print("\n── [1/3] 锚点断言（claims.json）──")
    if not os.path.isfile(CLAIMS_PATH):
        report.fail("claims.json", f"文件缺失：{CLAIMS_PATH}")
        return
    try:
        data = load_json(CLAIMS_PATH)
    except (ValueError, OSError) as exc:
        report.fail("claims.json", f"不是合法 JSON：{exc}")
        return

    claims = data.get("claims") or []
    if not claims:
        report.fail("claims.json", "claims 为空——门禁形同虚设")
        return

    for claim in claims:
        cid = claim.get("id", "<无 id>")
        rel = claim.get("file", "")
        note = claim.get("note", "")
        label = f"{cid}  ({rel})"

        text = read_text(rel)
        if text is None:
            if claim.get("pending"):
                report.skip(label, f"待落地（pending）：{note or '文件尚未创建'}")
            else:
                report.fail(label, f"文件不存在：{rel}")
            continue

        missing = [s for s in _as_list(claim.get("must_contain")) if s not in text]
        present = [s for s in _as_list(claim.get("must_not_contain")) if s in text]
        problems = []
        if missing:
            problems.append("缺少必需子串：" + " | ".join(repr(s) for s in missing))
        if present:
            problems.append("含禁止子串：" + " | ".join(repr(s) for s in present))

        suffix = f"  — {note}" if note else ""
        if problems:
            report.fail(label, "；".join(problems) + suffix)
        else:
            report.ok(label, f"{len(_as_list(claim.get('must_contain')))} 项子串命中{suffix}")


# ══════════════════ 2. 提示词快照 + 契约断言 ══════════════════
def check_prompt_snapshots(report: Report, writing: bool) -> None:
    print("\n── [2/3] 提示词快照与契约断言（prompt_snapshots/）──")
    if not os.path.isfile(CONTRACTS_PATH):
        report.fail("prompt_snapshots/contracts.json", f"文件缺失：{CONTRACTS_PATH}")
        return
    try:
        spec = load_json(CONTRACTS_PATH)
    except (ValueError, OSError) as exc:
        report.fail("prompt_snapshots/contracts.json", f"不是合法 JSON：{exc}")
        return

    entries = spec.get("entries") or []
    if not entries:
        report.fail("prompt_snapshots/contracts.json", "entries 为空——门禁形同虚设")
        return

    recorded = {}
    if os.path.isfile(SNAPSHOT_INDEX_PATH):
        try:
            recorded = {e["key"]: e for e in load_json(SNAPSHOT_INDEX_PATH).get("entries", [])}
        except (ValueError, OSError, KeyError, TypeError) as exc:
            report.fail("prompt_snapshots/index.json", f"读取失败：{exc}")
            return

    if writing:
        os.makedirs(SNAPSHOT_TEXT_DIR, exist_ok=True)

    new_index, extracted = [], {}
    for entry in entries:
        key = entry.get("key", "<无 key>")
        rel = entry.get("file", "")
        anchor = entry.get("anchor", "")
        label = f"{key}  ({rel})"

        try:
            lineno, text = find_literal(rel, anchor)
        except RuntimeError as exc:
            report.fail(label, str(exc))
            continue

        if text is None:
            report.fail(label, f"锚点丢失：{rel} 中已找不到 {anchor!r}"
                               "（提示词被删除或改写；确认后跑 --update-snapshots）")
            continue
        extracted[key] = text

        missing = [s for s in _as_list(entry.get("must_contain")) if s not in text]
        present = [s for s in _as_list(entry.get("must_not_contain")) if s in text]
        digest = sha256_text(text)
        new_index.append({
            "key": key,
            "file": rel,
            "anchor": anchor,
            "line": lineno,
            "chars": len(text),
            "sha256": digest,
        })

        if writing:
            with open(os.path.join(SNAPSHOT_TEXT_DIR, f"{key}.txt"), "w",
                      encoding="utf-8", newline="\n") as fh:
                fh.write(text)

        problems, extra = [], []
        if missing:
            problems.append("契约缺失（提示词被改坏）：" + " | ".join(repr(s) for s in missing))
        if present:
            problems.append("出现禁止内容：" + " | ".join(repr(s) for s in present))

        old = recorded.get(key)
        if not writing:
            # 以已提交的 <key>.txt 为准做「文本比对」（可比 sha256 给出可读 diff）；
            # index.json 的 sha256 作为兜底（例如 .txt 缺失时）。
            snap_text = _read_snapshot_text(key)
            if snap_text is None and old is None:
                problems.append("无已记录快照（index.json 与 <key>.txt 均缺失）")
            elif snap_text is not None and snap_text != text:
                problems.append(
                    "提示词已漂移：与已提交快照不一致"
                    f"（{len(snap_text)} → {len(text)} 字符；sha256 "
                    f"{str((old or {}).get('sha256'))[:12]}… → {digest[:12]}…）。"
                    "若确属有意的变更，跑 --update-snapshots 刷新并提交")
                extra.append(_diff_preview(snap_text, text))
            elif snap_text is None and old is not None and old.get("sha256") != digest:
                problems.append(
                    f"提示词已漂移：sha256 {str(old.get('sha256'))[:12]}… → {digest[:12]}…"
                    "（<key>.txt 缺失，无法给出 diff；跑 --update-snapshots 重建）")

        suffix = f"  — {entry.get('note')}" if entry.get("note") else ""
        if problems:
            detail = f"{rel}:{lineno}  " + "；".join(problems) + suffix
            if extra:
                detail += "\n" + "\n".join(extra)
            report.fail(label, detail)
        else:
            state = "快照已刷新" if writing else "指纹与契约一致"
            report.ok(label, f"{rel}:{lineno}  {len(text)} 字符  {state}{suffix}")

    if writing:
        os.makedirs(SNAPSHOT_TEXT_DIR, exist_ok=True)
        payload = {
            "version": 1,
            "generated_by": "evals/run_evals.py --update-snapshots",
            "note": "机器生成，勿手改。人工维护的契约在 contracts.json。",
            "entries": sorted(new_index, key=lambda e: e["key"]),
        }
        with open(SNAPSHOT_INDEX_PATH, "w", encoding="utf-8", newline="\n") as fh:
            json.dump(payload, fh, ensure_ascii=False, indent=2, sort_keys=False)
            fh.write("\n")
        print(f"  [WROTE] index.json（{len(new_index)} 条）+ {len(new_index)} 个 .txt 快照")
    return extracted


# ══════════════════════ 3. 黄金任务集结构校验 ══════════════════════
def check_golden(report: Report, extracted: dict) -> None:
    print("\n── [3/3] 黄金任务集（golden/tasks.json，离线：结构 + 契约存在性）──")
    if not os.path.isfile(GOLDEN_PATH):
        report.fail("golden/tasks.json", f"文件缺失：{GOLDEN_PATH}")
        return
    try:
        data = load_json(GOLDEN_PATH)
    except (ValueError, OSError) as exc:
        report.fail("golden/tasks.json", f"不是合法 JSON：{exc}")
        return

    tasks = data.get("tasks") or []
    if not tasks:
        report.fail("golden/tasks.json", "tasks 为空——数据集形同虚设")
        return

    seen_ids = set()
    for task in tasks:
        tid = task.get("id") or "<无 id>"
        label = f"{tid}  ({task.get('kind', '?')})"
        problems = []

        if tid in seen_ids:
            problems.append("id 重复")
        seen_ids.add(tid)

        samples = task.get("samples") or []
        if not samples:
            problems.append("samples 为空")
        for i, s in enumerate(samples):
            if not isinstance(s, dict) or not s.get("input"):
                problems.append(f"samples[{i}] 缺 input")
            elif not any(k.startswith("expect") for k in s):
                problems.append(f"samples[{i}] 缺 expect* 字段")

        key = task.get("snapshot", "")
        text = extracted.get(key)
        if text is None:
            problems.append(f"引用了未知快照 key：{key!r}")
        else:
            kind = task.get("kind")
            if kind == "json_contract":
                for k in _as_list(task.get("expect_keys")):
                    if f'"{k}"' not in text:
                        problems.append(f"契约里找不到 JSON 键 {k!r}")
                for k in _as_list(task.get("expect_absent_keys")):
                    if f'"{k}"' in text:
                        problems.append(f"契约里出现了禁止键 {k!r}")
            elif kind == "prompt_enumeration":
                for lab in _as_list(task.get("expect_labels")):
                    if lab not in text:
                        problems.append(f"提示词未枚举 {lab!r}")
            else:
                problems.append(f"未知 kind：{kind!r}")

        suffix = f"  — {task.get('note')}" if task.get("note") else ""
        if problems:
            report.fail(label, "；".join(problems) + suffix)
        else:
            report.ok(label, f"{len(samples)} 条样例；契约存在性通过{suffix}")


def _as_list(value) -> list:
    if value is None:
        return []
    if isinstance(value, str):
        return [value]
    return list(value)


# ══════════════════════════════ main ══════════════════════════════
def main() -> int:
    parser = argparse.ArgumentParser(
        description="VeroRun 离线评测门禁（WP-B6）：锚点断言 + 提示词快照 + 黄金集结构")
    parser.add_argument("--offline", action="store_true",
                        help="离线模式（CI 用）。当前仅实现离线校验；不传也可跑，但只跑离线检查。")
    parser.add_argument("--update-snapshots", action="store_true",
                        help="人工确认提示词变更后，刷新 prompt_snapshots/index.json 与 *.txt")
    parser.add_argument("-v", "--verbose", action="store_true", help="打印通过项明细")
    args = parser.parse_args()

    print("=" * 72)
    print("VeroRun 离线评测门禁（WP-B6）")
    print(f"仓库根目录：{ROOT}")
    print("=" * 72)
    if not args.offline:
        print("  提示：未指定 --offline。当前版本**只实现离线校验**，"
              "在线模型评测需 LLM 凭据，尚未实现。")
    if args.update_snapshots:
        print("  模式：--update-snapshots（将刷新 evals/prompt_snapshots/ 的机器产物）")

    report = Report()
    check_claims(report)
    extracted = check_prompt_snapshots(report, writing=args.update_snapshots)
    check_golden(report, extracted or {})

    print("\n" + "=" * 72)
    report.dump(verbose=args.verbose)
    print(report.summary())

    if args.update_snapshots:
        print("快照已刷新。请 `git diff evals/prompt_snapshots/` 复核差异后再提交。")
        if report.failures:
            print("\n注意：刷新过程中仍有断言失败（见上），需先修正。")
            return 1
        return 0

    if report.failures:
        print("\n[FAIL] 评测门禁未通过——请先修复上述项；"
              "若确属有意的提示词变更，确认后运行："
              "python evals/run_evals.py --update-snapshots")
        return 1
    print("\n[OK] 全部离线评测通过。")
    return 0


if __name__ == "__main__":
    sys.exit(main())

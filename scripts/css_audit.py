"""dashboard.html 四层 CSS 安全清理：删除完全被覆盖的规则与死选择器规则。

删除条件（全部满足才删）：
1. 完全覆盖：同一 media 查询上下文、选择器完全相同的后续块规则，
   覆盖了该规则全部属性，且 !important 语义不反转
   （前面 !important 的属性，后面必须也 !important 才算覆盖）。
2. 死选择器：规则所有 class 选择器目标在 HTML/JS 其余文本从未出现。

@font-face / @keyframes 等非 qualified 规则一律保留。
@media 内规则删空后整个 @media 块一并删除。

用法：python scripts/css_audit.py [--apply]
默认只审计并写报告 build/work/css-audit.json；--apply 时另存清理后的
dashboard.html 到 build/work/dashboard.cleaned.html。
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

import tinycss2

ROOT = Path(__file__).resolve().parents[1]
HTML = ROOT / "src" / "job_agent" / "web" / "dashboard.html"
OUT_DIR = ROOT / "build" / "work"
REPORT = OUT_DIR / "css-audit.json"
CLEANED = OUT_DIR / "dashboard.cleaned.html"

text = HTML.read_text(encoding="utf-8")

style_spans: list[tuple[str, int, int]] = []  # (name, css_start, css_end)
pos = 0
for match in re.finditer(r'<style(?:\s+id="([^"]*)")?\s*>(.*?)</style>', text, re.DOTALL):
    css_start = match.start(2)
    css_end = match.end(2)
    style_spans.append((match.group(1) or "base", css_start, css_end))
blocks = [(name, text[s:e]) for name, s, e in style_spans]
print("blocks:", [(name, len(css)) for name, css in blocks])

# 其余文本（HTML+JS，无 CSS）用于死选择器判定
other_parts: list[str] = []
last = 0
for _, s, e in style_spans:
    other_parts.append(text[last:s])
    last = e
other_parts.append(text[last:])
other = "".join(other_parts)


def class_alive(cls: str) -> bool:
    return bool(re.search(rf"(?<![\w-]){re.escape(cls)}(?![\w-])", other))


def selector_dead(selector: str) -> bool:
    classes = re.findall(r"\.([A-Za-z_][\w-]*)", selector)
    if not classes:
        return False
    return all(not class_alive(c) for c in classes)


def linecol_to_offset(css: str, line: int, column: int) -> int:
    """tinycss2 的 source_line 从 1 开始、source_column 从 1 开始（按字符）。"""
    offset = 0
    for _ in range(line - 1):
        newline = css.find("\n", offset)
        if newline == -1:
            return len(css)
        offset = newline + 1
    return min(offset + column - 1, len(css))


def parse_block(css: str) -> list[dict]:
    """递归收集 qualified 规则：{selector, decls, media, start, end, parent_at}。

    start/end 为该规则（含 prelude 与 body）在 css 中的字节区间；
    end 取下一个同级节点的起点（吞掉规则间空白）。
    """
    results: list[dict] = []

    def walk(nodes: list, media: str, node_spans: list[tuple[object, int, int]]) -> None:
        for index, node in enumerate(nodes):
            start = linecol_to_offset(css, node.source_line, node.source_column)
            end = node_spans[index + 1][1] if index + 1 < len(node_spans) else len(css)
            if node.type == "at-rule" and node.lower_at_keyword == "media" and node.content is not None:
                inner = tinycss2.parse_rule_list(
                    node.content, skip_whitespace=True, skip_comments=True
                )
                # @media 内部的 source 位置仍相对整块 css，可直接用
                spans = [
                    (n, linecol_to_offset(css, n.source_line, n.source_column))
                    for n in inner
                ]
                walk(
                    inner,
                    media + "|" + re.sub(r"\s+", " ", tinycss2.serialize(node.prelude).strip()),
                    [(n, s) for n, s in spans],
                )
                results.append(
                    {"at_media_only": True, "media": media, "start": start, "end": end}
                )
                continue
            if node.type != "qualified-rule":
                continue
            selector = re.sub(r"\s+", " ", tinycss2.serialize(node.prelude).strip())
            decls: dict[str, tuple[str, bool]] = {}
            for decl in tinycss2.parse_declaration_list(
                node.content, skip_whitespace=True, skip_comments=True
            ):
                if decl.type == "declaration":
                    value = tinycss2.serialize(decl.value).strip()
                    decls[decl.lower_name] = (value, bool(decl.important))
            if selector and decls:
                results.append(
                    {
                        "selector": selector,
                        "decls": decls,
                        "media": media,
                        "start": start,
                        "end": end,
                    }
                )

    sheet = tinycss2.parse_stylesheet(css, skip_whitespace=True, skip_comments=True)
    spans = [(n, linecol_to_offset(css, n.source_line, n.source_column)) for n in sheet]
    walk(sheet, "", [(n, s) for n, s in spans])
    return results


parsed = [(name, parse_block(css)) for name, css in blocks]
qualified_counts = {}
for name, rules in parsed:
    qualified_counts[name] = sum(1 for r in rules if not r.get("at_media_only"))
print("qualified rules:", qualified_counts)

# ---- 覆盖分析（带 !important 与 media 语义）----
delete_spans: list[tuple[str, int, int, str]] = []  # (block, start, end, reason)

# 1) 完全覆盖
for i in range(len(parsed) - 1):
    name_i, rules_i = parsed[i]
    later: dict[tuple[str, str], dict[str, tuple[str, bool]]] = {}
    for j in range(i + 1, len(parsed)):
        for rule in parsed[j][1]:
            if rule.get("at_media_only"):
                continue
            key = (rule["media"], rule["selector"])
            merged = later.setdefault(key, {})
            for prop, val in rule["decls"].items():
                # 同块内后面的规则优先，但覆盖语义只要求"存在覆盖"
                merged[prop] = val
    for rule in rules_i:
        if rule.get("at_media_only"):
            continue
        key = (rule["media"], rule["selector"])
        cover = later.get(key)
        if not cover:
            continue
        if all(
            prop in cover and (not imp or cover[prop][1])
            for prop, (_, imp) in rule["decls"].items()
        ):
            delete_spans.append((name_i, rule["start"], rule["end"], "overridden"))

# 2) 死选择器（任意块，含最后一块）
for name, rules in parsed:
    for rule in rules:
        if rule.get("at_media_only"):
            continue
        if selector_dead(rule["selector"]):
            delete_spans.append((name, rule["start"], rule["end"], "dead-selector"))

# 去重（同一 span 只删一次）
seen_spans: set[tuple[str, int, int]] = set()
unique_deletes: list[tuple[str, int, int, str]] = []
for item in delete_spans:
    key = (item[0], item[1], item[2])
    if key not in seen_spans:
        seen_spans.add(key)
        unique_deletes.append(item)

# ---- 应用删除：按块倒序删除区间，@media 删空后整块移除 ----
def apply_deletes(css: str, spans: list[tuple[int, int]]) -> str:
    result = css
    for start, end in sorted(spans, reverse=True):
        # 扩展吞掉行首缩进与换行，避免留空行
        while start > 0 and result[start - 1] in " \t":
            start -= 1
        if start > 0 and result[start - 1] == "\n":
            start -= 1
        result = result[:start] + result[end:]
    # @media 删空检测：占位后逐个处理
    result = re.sub(
        r"@media[^{]*\{\s*\}",
        "",
        result,
    )
    result = re.sub(r"\n{3,}", "\n\n", result)
    return result


per_block_spans: dict[str, list[tuple[int, int]]] = {}
for name, start, end, _reason in unique_deletes:
    per_block_spans.setdefault(name, []).append((start, end))

new_blocks: list[tuple[str, str]] = []
removed_lines = 0
for name, css in blocks:
    if name in per_block_spans:
        cleaned = apply_deletes(css, per_block_spans[name])
        removed_lines += css.count("\n") - cleaned.count("\n")
        new_blocks.append((name, cleaned))
    else:
        new_blocks.append((name, css))

# 重新组装 HTML
new_text_parts: list[str] = []
last = 0
for (name, s, e), (_, cleaned_css) in zip(style_spans, new_blocks):
    new_text_parts.append(text[last:s])
    new_text_parts.append(cleaned_css)
    last = e
new_text_parts.append(text[last:])
new_text = "".join(new_text_parts)

report = {
    "qualified_rules": qualified_counts,
    "deleted_rules": {
        "overridden": sum(1 for r in unique_deletes if r[3] == "overridden"),
        "dead_selector": sum(1 for r in unique_deletes if r[3] == "dead-selector"),
    },
    "deleted_reasons": [
        {"block": r[0], "reason": r[3], "selector": None} for r in unique_deletes
    ],
    "css_chars_before": sum(len(css) for _, css in blocks),
    "css_chars_after": sum(len(css) for _, css in new_blocks),
    "lines_removed_approx": removed_lines,
}
OUT_DIR.mkdir(parents=True, exist_ok=True)
REPORT.write_text(json.dumps(report, ensure_ascii=False, indent=1), encoding="utf-8")
print(json.dumps({k: v for k, v in report.items() if k != "deleted_reasons"}, ensure_ascii=False, indent=1))

if "--apply" in sys.argv:
    CLEANED.write_text(new_text, encoding="utf-8")
    print("cleaned html:", CLEANED)
    print("html lines:", text.count("\n") + 1, "->", new_text.count("\n") + 1)
else:
    print("dry-run only; pass --apply to write", CLEANED)

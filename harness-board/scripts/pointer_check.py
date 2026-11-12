#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# 本件是随包分发的副本。通用逻辑的权威源与下发纪律见工坊仓根 CLAUDE.md，
# 只改这里不会传播到别的 skill 包。（此处刻意不写仓库路径——仓库重组时不必改包。）
"""prd-workflow 指针校验脚本（分册验收判据 J0～J6）。交付自检项。

用法：python3 scripts/pointer_check.py [skill包根目录，默认=脚本上级]
退出码：0=全过；1=有 findings。

册的种类：SKILL.md（主文件）／分册（文件名以两位数字加横线开头，如 `03-检查`）／
阶段册（旧式 s0～s3）／opt 册／appendix 册。恒加载层＝SKILL.md ＋ `00-` 开头的分册
（旧式包为 s0-对齐与基建）。

判据：
- J0 指针实存：md 中路径型引用（references/ assets/ scripts/）目标文件存在。
- J1 指针方向：装载图无环（指向恒加载层的回指边不触发装载、不入环检）；边白名单＝
  SKILL.md→任意册 ／ 任意册→恒加载层 ／ 分册或阶段册→opt 册 ／ 任意册→appendix（叶子）。
  opt↔opt、分册↔分册（恒加载层除外）恒禁止。
  前提连带校验：SKILL.md 路由表里恒加载层各册那一行须含「恒加载」。
- J2 入册指针密度：每个 opt 册被内核层（SKILL.md＋分册＋阶段册）指向的文件数 ≤3。
- J3 册自足性（机检面）：opt/appendix 册首 5 行内含「触发条件」及跳过/恒读声明。
- J4 分册边界：分册正文不得用「03 §二」这类写法指向别的分册的某一节（恒加载层除外）。
- J5 分册开头五项：前 15 行内须有「做什么／读什么／交出什么／做完去哪／用哪些共用规则」。
- J6 交接文件对账：分册开头「读什么」「交出什么」里用反引号写的工作区路径——
  每个交出的文件须有别的分册读，或本分册「做完去哪」写了「结束」（终端分册，交出的即交付件）；
  每个要读的文件须有分册交出（含本分册），或位于外部输入目录（EXTERNAL）。
  目录写法（以 / 结尾）与其下的文件互相匹配。
旧式包（没有两位数字开头的分册）不触发 J4～J6。
"""
import re, sys, pathlib

ROOT = pathlib.Path(sys.argv[1]) if len(sys.argv) > 1 else pathlib.Path(__file__).resolve().parent.parent
ALIASES = {"宿主适配附录": "appendix-c-宿主适配"}  # 别称→册名
LEGACY_ALWAYS = "s0-对齐与基建"
MODULE_RE = re.compile(r"^(\d{2})-")
FIELDS = ["做什么", "读什么", "交出什么", "做完去哪", "用哪些共用规则"]
EXTERNAL = ("00-原始输入/",)       # 用户提供的原始材料，不由任何分册交出


def kind(n):
    if n == "SKILL.md": return "skill"
    if n.startswith("opt-"): return "opt"
    if n.startswith("appendix-"): return "appendix"
    if MODULE_RE.match(n): return "module"
    return "stage"


def header_fields(text):
    head = "\n".join(text.split("\n")[:15])
    got = {}
    for f in FIELDS:
        m = re.search(rf"(?:\*\*)?{f}(?:\*\*)?\s*[：:](.*)", head)
        if m: got[f] = m.group(1)
    return got


def paths_in(s):
    return {t.strip() for t in re.findall(r"`([^`]+)`", s or "") if "/" in t or t.endswith(".md")}


def match(a, b):
    return a == b or (a.endswith("/") and b.startswith(a)) or (b.endswith("/") and a.startswith(b))


def main():
    findings = []
    refdir = ROOT / "references"
    books = {"SKILL.md": ROOT / "SKILL.md"}
    books.update({p.stem: p for p in sorted(refdir.glob("*.md"))})
    texts = {n: p.read_text(encoding="utf-8") for n, p in books.items()}
    modules = [n for n in books if kind(n) == "module"]
    always = {"SKILL.md"} | {n for n in modules if n.startswith("00-")}
    if LEGACY_ALWAYS in books: always.add(LEGACY_ALWAYS)

    # J0 指针实存（含 assets 内的路径引用）
    all_md = list(books.items()) + [(p.name, p) for p in sorted((ROOT / "assets").glob("*.md"))]
    for name, p in all_md:
        for m in set(re.findall(r"(?:references|assets|scripts)/[^\s`）)|，。；]+\.(?:md|py|sh)", p.read_text(encoding="utf-8"))):
            if not (ROOT / m).exists():
                findings.append(f"[J0 悬空] {name} -> {m}")

    # 建册间引用图（册名精确匹配＋别称）
    edges = set()
    for src, body in texts.items():
        for dst in books:
            if dst == src: continue
            if dst in body: edges.add((src, dst))
        for alias, dst in ALIASES.items():
            if alias in body and dst != src: edges.add((src, dst))

    # J1 白名单
    for s, d in sorted(edges):
        ok = (s == "SKILL.md") or (d in always) or \
             (kind(s) in ("stage", "module") and kind(d) == "opt") or (kind(d) == "appendix")
        if kind(s) == "opt" and kind(d) == "opt":
            ok = False
        if not ok:
            findings.append(f"[J1 越界边] {s} -> {d}")

    # J1 无环（DFS）——检查对象＝装载图：指向恒加载层的回指边不触发装载，故不入环检
    adj = {}
    for s, d in edges:
        if d not in always:
            adj.setdefault(s, []).append(d)
    WHITE, GRAY, BLACK = 0, 1, 2
    color = {n: WHITE for n in books}
    def dfs(n, path):
        color[n] = GRAY
        for m in adj.get(n, []):
            if color[m] == GRAY:
                findings.append(f"[J1 环] {' -> '.join(path + [n, m])}")
            elif color[m] == WHITE:
                dfs(m, path + [n])
        color[n] = BLACK
    for n in books:
        if color[n] == WHITE: dfs(n, [])

    # J1 前提：恒加载层各册在 SKILL.md 路由表里标「恒加载」
    for n in sorted(always - {"SKILL.md"}):
        if not re.search(rf"{re.escape(n)}\.md`?\s*\|[^|]*恒加载", texts["SKILL.md"]):
            findings.append(f"[J1 前提] SKILL.md 路由表 {n} 行未标「恒加载」——恒加载层口径的结构前提缺失")

    # J2 入册密度
    kernel = [n for n in books if kind(n) in ("skill", "stage", "module")]
    for o in [n for n in books if kind(n) == "opt"]:
        pointers = [k for k in kernel if (k, o) in edges]
        if len(pointers) > 3:
            findings.append(f"[J2 密度] {o} 被内核层 {len(pointers)} 处指向（>3）：{pointers}")

    # J3 册首声明
    for n in books:
        if kind(n) in ("opt", "appendix"):
            head = "\n".join(texts[n].split("\n")[:5])
            if "触发条件" not in head or not ("整册跳过" in head or "恒" in head):
                findings.append(f"[J3 册首] {n} 前 5 行缺触发条件或跳过/恒读声明")

    if modules:
        nums = {MODULE_RE.match(n).group(1): n for n in modules}
        # J4 分册边界：「NN §」短写法指向别的分册
        for n in modules:
            own = MODULE_RE.match(n).group(1)
            for num in set(re.findall(r"(?<![\d.])(\d{2})\s*§", texts[n])):
                if num != own and num in nums and nums[num] not in always:
                    findings.append(f"[J4 分册边界] {n} 用「{num} §」指向分册 {nums[num]} 的某一节——分册之间只经交接文件衔接")
        # J5 开头五项
        heads = {n: header_fields(texts[n]) for n in modules}
        for n in modules:
            miss = [f for f in FIELDS if f not in heads[n]]
            if miss:
                findings.append(f"[J5 开头五项] {n} 前 15 行缺：{'、'.join(miss)}")
        # J6 交接文件对账
        produced = {n: paths_in(heads[n].get("交出什么")) for n in modules}
        consumed = {n: paths_in(heads[n].get("读什么")) for n in modules}
        for n in modules:
            terminal = "结束" in (heads[n].get("做完去哪") or "")
            for p in sorted(produced[n]):
                if terminal: continue
                if not any(match(p, q) for m in modules if m != n for q in consumed[m]):
                    findings.append(f"[J6 交接对账] {n} 交出 `{p}`，但没有别的分册读它")
            for q in sorted(consumed[n]):
                if q.startswith(EXTERNAL): continue
                if not any(match(q, p) for m in modules for p in produced[m]):
                    findings.append(f"[J6 交接对账] {n} 要读 `{q}`，但没有分册交出它")

    for f in findings: print(f)
    print(f"结果：{'不通过，' + str(len(findings)) + ' 条' if findings else '通过'}"
          f"（册 {len(books)}，边 {len(edges)}{'，分册 ' + str(len(modules)) if modules else ''}）")
    sys.exit(1 if findings else 0)


if __name__ == "__main__":
    main()

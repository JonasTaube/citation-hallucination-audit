#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""引用幻觉审计 (Citation Hallucination Audit)

批量核验稿件参考文献的真实性：抓 DOI -> 打 CrossRef / DataCite -> 4 级判定 -> 输出幻觉率。

用法:
  python citation_audit.py <稿件.docx|.txt|.md>
  python citation_audit.py <稿件> --json report.json --md report.md
  python citation_audit.py <稿件> --interval 2.0 --no-cache
  python citation_audit.py --text "Smith J. Foo bar. Nature. 2020;1:2. doi:10.1038/xxxxx"

判定分级:
  MATCH       DOI 真实存在，且题名与引文条目可对应
  MISMATCH    DOI 真实存在，但题名对不上（张冠李戴，最隐蔽）
  NOT_FOUND   CrossRef 与 DataCite 均查无此号（纯幻觉）
  UNKNOWN     网络 / 限流导致无法判定（不计入幻觉率分子）

引用幻觉率 = (NOT_FOUND + MISMATCH) / 可判定条目数
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
import zipfile
import urllib.error
import urllib.parse
import urllib.request

# CrossRef gives polite-pool access to clients that identify themselves.
# Set CITATION_AUDIT_CONTACT to your own e-mail address.
CONTACT = os.environ.get("CITATION_AUDIT_CONTACT", "you@example.com")
UA = "CitationHallucinationAudit/1.0 (mailto:%s)" % CONTACT
CACHE_NAME = ".citation_audit_cache.json"

DOI_RE = re.compile(r"10\.\d{4,9}/[^\s\"',;<>\[\]]+")
REF_HEAD_RE = re.compile(
    r"^\s*(references|reference list|bibliography|literature cited|参考文献|引用文献)\s*:?\s*$",
    re.I | re.M,
)
WORD_RE = re.compile(r"[a-z]{4,}")


# ---------------------------------------------------------------- 文本读取
def read_docx(path: str) -> str:
    with zipfile.ZipFile(path) as z:
        xml = z.read("word/document.xml").decode("utf-8", "replace")
    xml = re.sub(r"</w:p>", "\n", xml)
    xml = re.sub(r"<[^>]+>", "", xml)
    return xml


def read_input(path: str | None, text: str | None) -> tuple[str, str]:
    if text:
        return "«命令行文本»", text
    if not path:
        raise SystemExit("请提供稿件路径或 --text")
    ext = os.path.splitext(path)[1].lower()
    if ext == ".docx":
        return os.path.basename(path), read_docx(path)
    if ext in (".txt", ".md", ".csv"):
        with open(path, encoding="utf-8", errors="replace") as f:
            return os.path.basename(path), f.read()
    raise SystemExit("暂不支持 %s，请另存为 .docx / .txt / .md" % ext)


def slice_references(body: str) -> tuple[str, bool]:
    """尽量只截取参考文献段落，避免正文里的 DOI 污染统计。"""
    hits = list(REF_HEAD_RE.finditer(body))
    if not hits:
        return body, False
    start = hits[-1].end()
    tail = body[start:]
    if len(tail.strip()) < 80 and len(hits) > 1:
        start = hits[-2].end()
        tail = body[start:]
    return tail, True


ENTRY_RE = re.compile(r"^\s*(?:\[?\d{1,3}\]?[.)]?[\s\u3000]|\(\d{1,3}\)[\s\u3000]|\d{1,3}\s*$)")


def count_entries(refs: str) -> int:
    """启发式统计参考文献条目总数（用于给出与'带DOI数'对比的分母）。"""
    lines = [l for l in refs.splitlines() if l.strip()]
    numbered = sum(1 for l in lines if ENTRY_RE.match(l))
    return numbered if numbered >= 5 else len(lines)


# ---------------------------------------------------------------- 网络层
def _http_json(url: str, timeout: int = 30):
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.load(r)


def _retry(fn, retries: int = 5):
    """5 次重试 + 指数退避，遵守 CrossRef 限流要求。"""
    last = None
    for attempt in range(retries):
        try:
            return fn()
        except urllib.error.HTTPError as e:
            last = e
            if e.code == 404:
                return None
            if e.code in (429, 500, 502, 503, 504):
                time.sleep(3 * (attempt + 1))
                continue
            return None
        except Exception as e:  # noqa: BLE001
            last = e
            time.sleep(3 * (attempt + 1))
    return ("ERROR", last)


def lookup_doi(doi: str) -> dict:
    """返回 {status, source, title, container, year}；status in ok/none/error"""
    q = urllib.parse.quote(doi, safe="")

    res = _retry(lambda: _http_json("https://api.crossref.org/works/" + q))
    if isinstance(res, tuple) and res and res[0] == "ERROR":
        return {"status": "error", "detail": str(res[1])}
    if isinstance(res, dict):
        m = res.get("message", {})
        return {
            "status": "ok",
            "source": "crossref",
            "title": (m.get("title") or [""])[0],
            "container": (m.get("container-title") or [""])[0],
            "year": ((m.get("issued") or {}).get("date-parts") or [[None]])[0][0],
        }

    res = _retry(lambda: _http_json("https://api.datacite.org/dois/" + q))
    if isinstance(res, tuple) and res and res[0] == "ERROR":
        return {"status": "error", "detail": str(res[1])}
    if isinstance(res, dict):
        a = res.get("data", {}).get("attributes", {})
        return {
            "status": "ok",
            "source": "datacite",
            "title": ((a.get("titles") or [{}])[0]).get("title", ""),
            "container": (a.get("container") or {}).get("title", ""),
            "year": a.get("publicationYear"),
        }
    return {"status": "none"}


# ---------------------------------------------------------------- 判定层
def title_overlap(entry: str, title: str) -> float:
    """引文条目与数据库题名的吻合度，0~1。

    两个度量取较大者：
      full —— 数据库题名词被条目覆盖的比例（题名完整写出时有效）
      head —— 题名开头 6 个实词的命中率（引文只写主标题、省略副标题时救场）
    只用 full 会把"只写主标题"的合法引文判成张冠李戴（实测踩过）。
    """
    t = WORD_RE.findall(title.lower())
    if not t:
        return 0.0
    e = set(WORD_RE.findall(entry.lower()))
    if not e:
        return 0.0

    tset = set(t)
    full = len(tset & e) / len(tset)

    head: list[str] = []
    for w in t:
        if w not in head:
            head.append(w)
        if len(head) == 6:
            break
    head_hit = sum(1 for w in head if w in e) / len(head) if head else 0.0

    return max(full, head_hit)


# 判定阈值：真·张冠李戴的题名重合度通常接近 0；
# 而"副标题增减 / 英美拼写 / 截断"这类正常变体落在 0.4~0.7 之间，必须单列出来人工看。
MATCH_T = 0.65
REVIEW_T = 0.45


def _clean_doi(raw: str) -> str:
    d = raw.rstrip(".,;:")
    while d.endswith(")") and d.count("(") < d.count(")"):
        d = d[:-1]
    return d.lower()


NUM_SPLIT_RE = re.compile(r"(?=\b\d{1,3}\.\s+[A-Z])")


def build_entries(refs: str) -> dict[str, str]:
    """给每个 DOI 配它的**完整引文条目**文本。

    踩过的两个坑：
    1) 不能按行切 —— docx 段落切分常把题名和 DOI 分到不同行；
    2) 也不能按"DOI 前 N 个字符"取窗口 —— 题名长时窗口会截到上一条的尾巴，
       造成大量假阳性 MISMATCH（实测 M2 整篇被误报成 100%）。
    正解：压平后按编号边界（"12. "）切条目，再把无 DOI 的碎片并入前一条。
    """
    flat = re.sub(r"\s+", " ", refs)

    parts = [p.strip() for p in NUM_SPLIT_RE.split(flat) if p.strip()]
    if len(parts) < 3:
        parts = [flat]
    merged: list[str] = []
    for p in parts:
        if DOI_RE.search(p) or not merged:
            merged.append(p)
        else:
            merged[-1] = merged[-1] + " " + p

    entries: dict[str, str] = {}
    for p in merged:
        for m in DOI_RE.finditer(p):
            entries.setdefault(_clean_doi(m.group(0)), p)

    # 兜底：编号切分失败时用位置窗口，保证每个 DOI 都有上下文
    for m in DOI_RE.finditer(flat):
        d = _clean_doi(m.group(0))
        if d not in entries:
            entries[d] = flat[max(0, m.start() - 300): m.end() + 80]
    return entries


def audit(dois: list[str], entries: dict[str, str], interval: float, use_cache: bool) -> list[dict]:
    here = os.path.dirname(os.path.abspath(__file__))
    cache_path = os.path.join(here, CACHE_NAME)
    cache: dict = {}
    if use_cache and os.path.exists(cache_path):
        try:
            with open(cache_path, encoding="utf-8") as f:
                cache = json.load(f)
        except Exception:  # noqa: BLE001
            cache = {}

    results = []
    total = len(dois)
    for i, doi in enumerate(dois, 1):
        if doi in cache:
            info = cache[doi]
        else:
            info = lookup_doi(doi)
            cache[doi] = info
            time.sleep(max(0.0, interval))
        entry = entries.get(doi, "")
        verdict, sim = None, None
        if info.get("status") == "error":
            verdict = "UNKNOWN"
        elif info.get("status") == "none":
            verdict = "NOT_FOUND"
        else:
            sim = title_overlap(entry, info.get("title", ""))
            if len(WORD_RE.findall(entry.lower())) < 6:
                # 引文条目本身没写题名（如 "19. AJPM Focus 2026; 5: 100434."），
                # 比不出题名，不能冤枉成张冠李戴
                verdict = "REVIEW"
                sim = None
            elif sim >= MATCH_T:
                verdict = "MATCH"
            elif sim >= REVIEW_T:
                verdict = "REVIEW"
            else:
                verdict = "MISMATCH"
        results.append({"doi": doi, "verdict": verdict, "similarity": sim,
                        "title": info.get("title", ""), "container": info.get("container", ""),
                        "year": info.get("year"), "source": info.get("source", ""),
                        "entry": entry[:400]})
        mark = {"MATCH": "OK ", "REVIEW": "~~ ", "MISMATCH": "!! ", "NOT_FOUND": "XX ", "UNKNOWN": "?? "}[verdict]
        sys.stdout.write("[%3d/%3d] %s %-78s\n" % (i, total, mark, doi))
        sys.stdout.flush()

    if use_cache:
        try:
            with open(cache_path, "w", encoding="utf-8") as f:
                json.dump(cache, f, ensure_ascii=False, indent=1)
        except Exception:  # noqa: BLE001
            pass
    return results


# ---------------------------------------------------------------- 报告层
def report(name: str, results: list[dict], n_entries: int = 0) -> tuple[str, dict]:
    n = len(results)
    c = {k: sum(1 for r in results if r["verdict"] == k)
         for k in ("MATCH", "REVIEW", "MISMATCH", "NOT_FOUND", "UNKNOWN")}
    judgeable = n - c["UNKNOWN"]
    bad = c["MISMATCH"] + c["NOT_FOUND"]
    rate = (bad / judgeable * 100) if judgeable else 0.0

    L = []
    L.append("=" * 72)
    L.append("引用幻觉审计报告")
    L.append("稿件: %s" % name)
    L.append("时间: %s" % time.strftime("%Y-%m-%d %H:%M:%S"))
    L.append("=" * 72)
    L.append("")
    if n_entries:
        L.append("参考文献条目总数(启发式):        %d" % n_entries)
        L.append("其中带 DOI 可自动核验:           %d  (%.1f%%)"
                 % (n, (n / n_entries * 100) if n_entries else 0))
        L.append("无 DOI，本工具未核验:            %d  ← 需人工或题名反查"
                 % max(0, n_entries - n))
        L.append("")
    L.append("可核验引用条目 (唯一 DOI):        %d" % n)
    L.append("")
    L.append("  [OK ] MATCH     真实存在，题名可对应    %4d" % c["MATCH"])
    L.append("  [~~ ] REVIEW    题名部分吻合（疑变体/缩略）%4d  ← 人工确认，不计入幻觉" % c["REVIEW"])
    L.append("  [!! ] MISMATCH  存在但题名不符（张冠李戴）%4d" % c["MISMATCH"])
    L.append("  [XX ] NOT_FOUND 全库查无此号（纯幻觉）  %4d" % c["NOT_FOUND"])
    L.append("  [?? ] UNKNOWN   网络异常，无法判定      %4d" % c["UNKNOWN"])
    L.append("")
    L.append("-" * 72)
    L.append("★ 引用幻觉率 = (%d + %d) / %d = %.1f%%"
             % (c["MISMATCH"], c["NOT_FOUND"], judgeable, rate))
    L.append("  （口径：MISMATCH+NOT_FOUND 为分子；UNKNOWN 剔除，不计入分母）")
    L.append("-" * 72)

    review = [r for r in results if r["verdict"] == "REVIEW"]
    if review:
        L.append("")
        L.append("待人工确认（题名变体嫌疑，很可能没问题）:")
        for r in review:
            sim_txt = ("%.0f%%" % (r["similarity"] * 100)) if r["similarity"] is not None else "引文缺题名"
            L.append("  %-42s %-10s %s" % (r["doi"], sim_txt, r["title"][:58]))

    flagged = [r for r in results if r["verdict"] in ("MISMATCH", "NOT_FOUND")]
    if flagged:
        L.append("")
        L.append("需人工复核条目:")
        for r in flagged:
            L.append("")
            L.append("  [%s] %s" % (r["verdict"], r["doi"]))
            if r["verdict"] == "MISMATCH":
                L.append("    数据库题名: %s" % r["title"][:110])
                L.append("    题名重合度: %.0f%%（低于 %.0f%% 阈值）"
                         % ((r["similarity"] or 0) * 100, REVIEW_T * 100))
            L.append("    你的引文  : %s" % r["entry"][:140])
    L.append("")
    L.append("=" * 72)

    text = "\n".join(L)
    summary = {"manuscript": name, "total_entries": n_entries, "no_doi_entries": max(0, n_entries - n),
               "total_unique_dois": n, **c,
               "judgeable": judgeable, "hallucination_rate_pct": round(rate, 2)}
    return text, summary


# ---------------------------------------------------------------- 主流程
def main() -> int:
    ap = argparse.ArgumentParser(description="引用幻觉审计：批量核验参考文献 DOI 真实性")
    ap.add_argument("path", nargs="?", help="稿件路径 (.docx/.txt/.md)")
    ap.add_argument("--text", help="直接传入参考文献文本")
    ap.add_argument("--interval", type=float, default=2.0, help="每次 API 间隔秒数（默认 2.0）")
    ap.add_argument("--no-cache", action="store_true", help="禁用本地缓存")
    ap.add_argument("--json", help="同时写出 JSON 摘要")
    ap.add_argument("--md", help="同时写出 Markdown 报告")
    args = ap.parse_args()

    name, body = read_input(args.path, args.text)
    refs, sliced = slice_references(body)
    if not sliced:
        print("提示: 未定位到 'References/参考文献' 标题，已按全文扫描（可能混入正文 DOI）", file=sys.stderr)

    dois, seen = [], set()
    for m in DOI_RE.finditer(refs):
        d = _clean_doi(m.group(0))
        if d not in seen:
            seen.add(d)
            dois.append(d)

    # 给每个 DOI 配一段引文上下文（压平后取前置窗口，避免 docx 段落切分把题名与 DOI 分开）
    entries = build_entries(refs)

    if not dois:
        print("未在参考文献中找到任何 DOI。", file=sys.stderr)
        return 1

    print("稿件: %s | 唯一 DOI: %d 条 | 间隔 %.1fs\n" % (name, len(dois), args.interval))
    results = audit(dois, entries, args.interval, not args.no_cache)
    text, summary = report(name, results, count_entries(refs))
    print("\n" + text)

    if args.json:
        with open(args.json, "w", encoding="utf-8") as f:
            json.dump({"summary": summary, "details": results}, f, ensure_ascii=False, indent=2)
        print("JSON 已写出: %s" % args.json)
    if args.md:
        with open(args.md, "w", encoding="utf-8") as f:
            f.write("```\n" + text + "\n```\n")
        print("Markdown 已写出: %s" % args.md)
    return 0


if __name__ == "__main__":
    sys.exit(main())

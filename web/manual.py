"""使用手冊:把 docs/使用手冊.md 轉成網頁,做成說明中心(使用者 11/5:「使用手冊不是說要向 claude 那樣?」)。

手冊只有一份,網站直接讀它,不另外維護(使用者 11/4)。每個大節(##)是一篇:
/help 是首頁(搜尋手冊、主題清單,每篇一句摘要與幾個小節),/help/<編號> 是那一篇(導覽路徑、更新日期、
「在此頁面上」小節目錄、上一篇與下一篇)。摘要是大節標題下面的第一段引言(>),不放進內文;
更新日期照手冊開頭引言裡的「最後更新：YYYY-MM-DD」。
只認手冊用到的 Markdown 子集(標題、段落、清單、表格、引言、粗體、程式字),內容一律先跳脫;連結只留文字
(手冊裡的連結都指向 repo 內的檔案),「第 N 節」「第 N.M 節」換成連到那一篇(那一小節)的連結;
最後一節「給開發者」不放到網站上。搜尋只在這份手冊的文字裡找,每個詞都要出現、順序不拘,不呼叫模型。
"""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

from markupsafe import Markup, escape

MANUAL_PATH = Path(__file__).resolve().parent.parent / "docs" / "使用手冊.md"
SEARCH_MAX = 50          # 搜尋字最多幾個字(同文件櫃)
SEARCH_WORDS = 5
SEARCH_LIMIT = 30        # 最多列幾筆
_DEV_SECTION = "給開發者"
_HEADING = re.compile(r"^(#{1,3})\s+(.*)$")
_ITEM = re.compile(r"^(\s*)([-*]|\d+\.)\s+(.*)$")
_TOPIC = re.compile(r"^(\d+)\.\s*(.+)$")              # 「3. 看結果」
_SUB = re.compile(r"^(\d+)\.(\d+)\s+(.+)$")           # 「3.1 最上面：先回答三個問題」
_XREF = re.compile(r"第 (\d+)(?:\.(\d+))? 節")
_UPDATED = re.compile(r"最後更新[：:]\s*(\d{4})-(\d{2})-(\d{2})")


@dataclass(frozen=True)
class Part:
    """搜尋的單位:一篇開頭那段(anchor 是空字串)或一個小節。"""
    anchor: str
    title: str
    text: str


@dataclass(frozen=True)
class Topic:
    number: str                              # 網址 /help/<number>,也是「第 N 節」
    title: str                               # 去掉編號的標題
    summary: str                             # 一句話,首頁清單與這一篇的標題下面
    html: Markup                             # 這一篇的內文(不含標題與摘要)
    sections: tuple[tuple[str, str], ...]    # (錨點, 小節標題):「在此頁面上」
    parts: tuple[Part, ...]


@dataclass(frozen=True)
class Hit:
    href: str
    topic: str       # 哪一篇
    title: str       # 哪一小節(開頭那段就是那一篇的標題)
    snippet: Markup  # 找到的字前後一小段,搜尋的字用 <mark> 標出來(其他字都已跳脫)


@dataclass(frozen=True)
class Manual:
    toc: tuple[tuple[str, str], ...]    # (錨點, 標題),每一個大節一項
    html: Markup                        # 整本(開場白加每一篇,舊的單頁版與測試用)
    intro: Markup = Markup("")          # 第一個大節前面的開場白
    topics: tuple[Topic, ...] = ()
    updated: str = ""                   # 「2026 年 11 月 5 日」;開頭沒寫就是空的

    def topic(self, number: str) -> Topic | None:
        return next((t for t in self.topics if t.number == number), None)


def _xref(m: re.Match[str]) -> str:
    href = f"/help/{m[1]}" + (f"#s-{m[1]}-{m[2]}" if m[2] else "")
    return f'<a href="{href}">{m[0]}</a>'


def _inline(text: str) -> str:
    out = str(escape(text))
    out = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", out)
    out = re.sub(r"`([^`]+)`", r"<code>\1</code>", out)
    out = re.sub(r"\[([^\]]+)\]\([^)]*\)", r"\1", out)
    return _XREF.sub(_xref, out)


def _plain(line: str) -> str:
    """搜尋用的純文字:拿掉清單符號、引言符號、粗體與程式字記號、連結網址、表格的直線。"""
    s = _ITEM.sub(r"\3", line.strip()).lstrip(">").strip()
    s = re.sub(r"\[([^\]]+)\]\([^)]*\)", r"\1", s).replace("**", "").replace("`", "")
    if s.startswith("|"):
        s = "" if set(s) <= set("|-: ") else " ".join(c.strip() for c in s.strip("|").split("|"))
    return s


def _fold(text: str) -> str:
    """比對用:全形轉半形(NFKC)、不分大小寫(同文件櫃的搜尋)。"""
    return unicodedata.normalize("NFKC", text).casefold()


def _render_items(nodes: list[dict]) -> str:
    """同一層的項目;編號清單與項目符號清單交錯時各開一個清單,編號從原文的數字接續。"""
    html, i = [], 0
    while i < len(nodes):
        ordered = nodes[i]["mark"][0].isdigit()
        j = i
        while j < len(nodes) and nodes[j]["mark"][0].isdigit() == ordered:
            j += 1
        tag = "ol" if ordered else "ul"
        start = f' start="{nodes[i]["mark"][:-1]}"' if ordered and nodes[i]["mark"] != "1." else ""
        items = "".join(f"<li>{_inline(n['text'])}{_render_items(n['children'])}</li>" for n in nodes[i:j])
        html.append(f"<{tag}{start}>{items}</{tag}>")
        i = j
    return "".join(html)


def _render_list(lines: list[str]) -> str:
    """縮排決定層次;沒有項目符號的行是上一項的續行。"""
    root: dict = {"indent": -1, "children": []}
    stack = [root]
    for line in lines:
        m = _ITEM.match(line)
        if not m:
            stack[-1]["text"] += line.strip()
            continue
        indent = len(m[1])
        while stack[-1]["indent"] >= indent:
            stack.pop()
        node = {"indent": indent, "mark": m[2], "text": m[3], "children": []}
        stack[-1]["children"].append(node)
        stack.append(node)
    return _render_items(root["children"])


def _render_table(lines: list[str]) -> str:
    rows = [[cell.strip() for cell in line.strip().strip("|").split("|")] for line in lines]
    head, body = rows[0], rows[2:]     # 第二列是分隔線
    th = "".join(f"<th>{_inline(c)}</th>" for c in head)
    trs = "".join("<tr>" + "".join(f"<td>{_inline(c)}</td>" for c in row) + "</tr>" for row in body)
    return f'<div class="manual__table"><table><thead><tr>{th}</tr></thead><tbody>{trs}</tbody></table></div>'


class _Builder:
    """邊讀邊把內容放進整本與目前這一篇;純文字放進目前這一段(搜尋用)。"""

    def __init__(self) -> None:
        self.html: list[str] = []
        self.intro: list[str] = []
        self.toc: list[tuple[str, str]] = []
        self.topics: list[Topic] = []
        self.current: dict | None = None

    def block(self, html: str, lines: list[str]) -> None:
        self.html.append(html)
        if self.current is None:
            self.intro.append(html)
            return
        self.current["html"].append(html)
        self.current["parts"][-1]["text"].extend(p for p in map(_plain, lines) if p)

    def start_topic(self, title: str) -> None:
        self.finish()
        number = str(len(self.toc) + 1)
        shown = title
        if m := _TOPIC.match(title):
            number, shown = m[1], m[2].strip()
        anchor = f"m-{len(self.toc) + 1}"
        self.toc.append((anchor, title))
        self.html.append(f'<h2 id="{anchor}">{_inline(title)}</h2>')
        self.current = {"number": number, "title": shown, "summary": "", "html": [], "sections": [],
                        "parts": [{"anchor": "", "title": shown, "text": []}], "fresh": True}

    def start_section(self, title: str) -> None:
        topic = self.current
        if topic is None:
            self.html.append(f"<h3>{_inline(title)}</h3>")
            return
        m = _SUB.match(title)
        anchor = f"s-{m[1]}-{m[2]}" if m else f"s-{topic['number']}-x{len(topic['sections']) + 1}"
        self.html.append(f'<h3 id="{anchor}">{_inline(title)}</h3>')
        topic["html"].append(f'<h2 id="{anchor}">{_inline(title)}</h2>')   # 一篇自己一頁:標題是 h1,小節是第二層
        topic["sections"].append((anchor, title))
        topic["parts"].append({"anchor": anchor, "title": title, "text": []})

    def finish(self) -> None:
        t = self.current
        if t is None:
            return
        parts = tuple(Part(p["anchor"], p["title"], " ".join(p["text"])) for p in t["parts"])
        self.topics.append(Topic(t["number"], t["title"], t["summary"], Markup("".join(t["html"])),
                                 tuple(t["sections"]), parts))
        self.current = None


def render_manual(text: str) -> Manual:
    """第一層標題與它下面的引言(給誰看、最後更新)不放:頁面有自己的標題。大節標題下面緊接的引言是那一篇的摘要。"""
    out = _Builder()
    para: list[str] = []
    lines = text.splitlines()
    head = re.search(r"^#[^#].*?(?=^## )", text, re.S | re.M)
    found = _UPDATED.search(head.group(0)) if head else None
    updated = f"{int(found[1])} 年 {int(found[2])} 月 {int(found[3])} 日" if found else ""
    started = False     # 第一個大節之前的引言不放

    def flush() -> None:
        if para:
            out.block(f"<p>{_inline(''.join(para))}</p>", para[:])
            para.clear()
            if out.current:
                out.current["fresh"] = False

    i = 0
    while i < len(lines):
        line = lines[i]
        stripped = line.strip()
        heading = _HEADING.match(line)
        if heading:
            flush()
            level, title = len(heading[1]), heading[2].strip()
            if level == 2 and _DEV_SECTION in title:
                break
            if level == 2:
                started = True
                out.start_topic(title)
            elif level == 3:
                out.start_section(title)
                if out.current:
                    out.current["fresh"] = False
            i += 1
        elif not stripped:
            flush()
            i += 1
        elif stripped.startswith(">"):
            flush()
            quote = []
            while i < len(lines) and lines[i].strip().startswith(">"):
                quote.append(lines[i].strip().lstrip(">").strip())
                i += 1
            if out.current and out.current["fresh"]:
                out.current["summary"] = "".join(quote)          # 大節標題下面緊接的引言:這一篇的摘要
                out.current["fresh"] = False
            elif started:
                out.block(f"<blockquote><p>{_inline(''.join(quote))}</p></blockquote>", quote)
        elif stripped.startswith("|"):
            flush()
            start = i
            while i < len(lines) and lines[i].strip().startswith("|"):
                i += 1
            out.block(_render_table(lines[start:i]), lines[start:i])
            if out.current:
                out.current["fresh"] = False
        elif _ITEM.match(line):
            flush()
            start = i
            while i < len(lines) and lines[i].strip() and not lines[i].strip().startswith(("|", ">", "#")):
                i += 1
            out.block(_render_list(lines[start:i]), lines[start:i])
            if out.current:
                out.current["fresh"] = False
        else:
            para.append(stripped)
            i += 1
    flush()
    out.finish()
    return Manual(tuple(out.toc), Markup("".join(out.html)), Markup("".join(out.intro)), tuple(out.topics), updated)


def search_manual(manual: Manual, query: str) -> tuple[Hit, ...]:
    """每個詞都要出現、順序不拘(全形半形、大小寫都不分);一筆是一篇的開頭或一個小節。沒有詞回空的。"""
    words = _fold(query[:SEARCH_MAX]).split()[:SEARCH_WORDS]
    if not words:
        return ()
    hits = []
    for topic in manual.topics:
        for part in topic.parts:
            if all(word in _fold(f"{part.title} {part.text}") for word in words):
                hits.append(Hit(f"/help/{topic.number}" + (f"#{part.anchor}" if part.anchor else ""),
                                topic.title, part.title, _snippet(part.text, words)))
    return tuple(hits[:SEARCH_LIMIT])


def _snippet(text: str, words: list[str], before: int = 24, after: int = 56) -> Markup:
    """第一個詞前後各一小段(只出現在標題就從頭取),每個詞用 <mark> 標出來;全形半形、大小寫都認得(NFKC 不改長度的字)。"""
    folded = _fold(text)
    at = max(folded.find(words[0]), 0)
    start, end = max(at - before, 0), min(at + after, len(text))
    piece, marks = text[start:end], folded[start:end] if len(folded) == len(text) else ""
    spans = sorted({(m.start(), m.end()) for w in words for m in re.finditer(re.escape(w), marks)})
    out, pos = [], 0
    for a, b in spans:
        if a < pos:
            continue
        out += [str(escape(piece[pos:a])), f"<mark>{escape(piece[a:b])}</mark>"]
        pos = b
    out.append(str(escape(piece[pos:])))
    return Markup(("…" if start else "") + "".join(out) + ("…" if end < len(text) else ""))


@lru_cache(maxsize=1)
def load_manual() -> Manual:
    """手冊只在部署時會變,讀一次就好;檔案不在(例如只搬了程式)就是空的,頁面照樣打得開。"""
    try:
        return render_manual(MANUAL_PATH.read_text(encoding="utf-8"))
    except OSError:
        return Manual((), Markup(""))

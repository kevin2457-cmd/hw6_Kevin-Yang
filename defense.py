"""第 6 题新增的防御：代码层的文档清洗、架构层的规则判断与操作权限网关。

- 代码层 sanitize_document()：在任何模型看到文档之前，按 HTML / PDF / 邮件的真实结构去掉“人看不到”或
  “不是正文”的内容，并记录每一处处理及原因。原始文件不修改，只记录 SHA-256 供审计。
- 架构层 decide()：用可信手册（第 5、6、9 节）按第 4 节四项检查做出采购决策，模型只负责提取字段。
- 架构层 ActionGateway：send_email / approve_payment 只能凭独立的人类批准执行；文档或模型声称“已批准”无效。
"""

from __future__ import annotations

import email
import email.policy
import hashlib
import re
import sys
import unicodedata
from dataclasses import dataclass, field
from functools import lru_cache
from html.parser import HTMLParser
from pathlib import Path
from typing import Any

from models import ExtractedQuote, VendorDecision

PROJECT_DIR = Path(__file__).resolve().parent
HANDBOOK_PATH = PROJECT_DIR / "data" / "shop_handbook.md"

PREVIEW = 200  # 审计记录里每段被移除内容只保留前 200 个字符


def _preview(text: str) -> str:
    text = " ".join(text.split())
    return text if len(text) <= PREVIEW else text[:PREVIEW] + "…"


@dataclass
class SanitizeReport:
    """代码层的处理记录：做了什么、为什么、移除了什么。"""

    file: str
    sha256: str
    format: str
    removed: list[dict[str, str]] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    chars_before: int = 0
    chars_after: int = 0

    def remove(self, kind: str, reason: str, text: str) -> None:
        if text.strip():
            self.removed.append({"kind": kind, "reason": reason, "text": _preview(text)})

    def as_dict(self) -> dict[str, Any]:
        return {"file": self.file, "sha256": self.sha256, "format": self.format, "removed": self.removed,
                "notes": self.notes, "chars_before": self.chars_before, "chars_after": self.chars_after}


# ================================================================ 代码层：HTML

_VOID = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "source", "track", "wbr"}
_BLOCK = {"p", "div", "br", "tr", "li", "h1", "h2", "h3", "h4", "h5", "h6", "table", "section", "header", "footer",
          "title"}
_NON_CONTENT = {"script", "style", "noscript", "template", "head"}
_WHITE = {"#fff", "#ffffff", "white", "rgb(255,255,255)", "rgba(255,255,255,1)"}


def _css(style: str) -> dict[str, str]:
    out = {}
    for decl in style.split(";"):
        if ":" in decl:
            k, v = decl.split(":", 1)
            out[k.strip().lower()] = v.strip().lower().replace(" ", "")
    return out


def _font_px(value: str) -> float | None:
    m = re.fullmatch(r"([\d.]+)(px|pt|em|rem|%)?", value)
    if not m:
        return None
    n, unit = float(m.group(1)), m.group(2) or "px"
    return {"px": n, "pt": n * 4 / 3, "em": n * 16, "rem": n * 16, "%": n * 0.16}[unit]


def hidden_reason(tag: str, attrs: dict[str, str | None]) -> str | None:
    """按元素自身的属性判断它是否对读者不可见；返回原因，可见时返回 None。
    假设页面是白色背景（练习文档都是），白色文字视为不可见。"""
    if "hidden" in attrs:
        return "hidden 属性"
    if (attrs.get("type") or "").lower() == "hidden":
        return "隐藏输入框"
    css = _css(attrs.get("style") or "")
    if css.get("display") == "none":
        return "CSS display:none"
    if css.get("visibility") in ("hidden", "collapse"):
        return "CSS visibility:hidden"
    if css.get("opacity") in ("0", "0.0"):
        return "CSS opacity:0"
    if "font-size" in css:
        px = _font_px(css["font-size"])
        if px is not None and px < 4:
            return f"字号过小（font-size:{css['font-size']}）"
    if css.get("color") in _WHITE:
        return "白色文字（白底上看不见）"
    if css.get("height") in ("0", "0px") and css.get("overflow") == "hidden":
        return "高度为 0 且 overflow:hidden"
    if any(k in css for k in ("left", "top", "text-indent")) and any(
            css.get(k, "").startswith("-") and (_font_px(css[k].lstrip("-")) or 0) >= 1000 for k in ("left", "top", "text-indent")):
        return "移到屏幕外"
    return None


class _VisibleHTML(HTMLParser):
    """只保留读者在浏览器里能看到的文字；被隐藏的元素整段记录到报告。"""

    def __init__(self, report: SanitizeReport) -> None:
        super().__init__(convert_charrefs=True)
        self.report = report
        self.parts: list[str] = []
        # 栈元素：(标签, 是否隐藏, 隐藏原因, 隐藏开始时 buffer 的位置)
        self.stack: list[tuple[str, bool, str | None]] = []
        self.hidden_buf: list[str] = []

    @property
    def hidden(self) -> bool:
        return bool(self.stack) and self.stack[-1][1]

    def handle_starttag(self, tag: str, attrs_list: list[tuple[str, str | None]]) -> None:
        attrs = dict(attrs_list)
        if tag in _VOID:
            if tag == "br" and not self.hidden:
                self.parts.append("\n")
            return
        parent_hidden = self.hidden
        reason = None if parent_hidden else ("非正文内容（<%s>）" % tag if tag in _NON_CONTENT and tag != "head"
                                             else hidden_reason(tag, attrs))
        if tag == "head":  # <head> 里只保留 <title>（浏览器标签页上可见）
            reason = None
        self.stack.append((tag, parent_hidden or reason is not None, reason))
        if not self.hidden:
            if tag in _BLOCK:
                self.parts.append("\n")
            elif tag in ("td", "th"):
                self.parts.append(" | ")

    def handle_endtag(self, tag: str) -> None:
        if not any(t == tag for t, _, _ in self.stack):
            return
        while self.stack:
            t, hid, reason = self.stack.pop()
            if reason is not None:  # 这一段隐藏内容结束
                kind = "non_content" if t in _NON_CONTENT else "hidden_html"
                self.report.remove(kind, reason, "".join(self.hidden_buf))
                self.hidden_buf = []
            if t == tag:
                break
        if not self.hidden and tag in _BLOCK:
            self.parts.append("\n")

    def handle_data(self, data: str) -> None:
        in_head = any(t == "head" for t, _, _ in self.stack)
        in_title = any(t == "title" for t, _, _ in self.stack)
        if self.hidden:
            self.hidden_buf.append(data)
        elif in_head and not in_title:
            return
        else:
            self.parts.append(data)

    def handle_comment(self, data: str) -> None:
        self.report.remove("html_comment", "HTML 注释，浏览器不显示", data)

    def text(self) -> str:
        lines = (" ".join(line.split()) for line in "".join(self.parts).splitlines())
        return "\n".join(line for line in lines if line)


def sanitize_html(html: str, report: SanitizeReport) -> str:
    p = _VisibleHTML(report)
    p.feed(html)
    p.close()
    return p.text()


# ================================================================ 代码层：PDF


def sanitize_pdf(data: bytes, report: SanitizeReport) -> str:
    """用 PyMuPDF 逐个文字片段检查：页面外、字号过小、白色或透明的文字都不交给模型。"""
    import pymupdf  # 只有代码层需要

    doc = pymupdf.open(stream=data, filetype="pdf")
    pages = []
    for pno, page in enumerate(doc, 1):
        rect = page.rect
        lines_out = []
        # clip=None 让 PyMuPDF 返回页面外的文字，才能检查并记录它们
        for block in page.get_text("dict", clip=pymupdf.INFINITE_RECT())["blocks"]:
            for line in block.get("lines", []):
                kept = []
                for s in line["spans"]:
                    text = s["text"]
                    if not text.strip():
                        continue
                    x0, y0, x1, y1 = s["bbox"]
                    c = s["color"]
                    r, g, b = (c >> 16) & 255, (c >> 8) & 255, c & 255
                    reason = None
                    if x1 <= rect.x0 or x0 >= rect.x1 or y1 <= rect.y0 or y0 >= rect.y1:
                        reason = f"文字在页面可视范围外（bbox={tuple(round(v) for v in s['bbox'])}）"
                    elif s["size"] < 4:
                        reason = f"字号过小（{s['size']:.1f}pt）"
                    elif min(r, g, b) >= 240:
                        reason = f"白色 / 接近白色文字（#{c:06x}），白底上看不见"
                    elif s.get("alpha", 255) == 0:
                        reason = "完全透明的文字"
                    if reason:
                        report.remove("hidden_pdf_text", f"第 {pno} 页：{reason}", text)
                    else:
                        kept.append(text)
                if kept:
                    lines_out.append("".join(kept).strip())
        if lines_out:
            pages.append(f"[第 {pno} 页]\n" + "\n".join(lines_out))
    report.notes.append("PDF 用 PyMuPDF 按文字片段的位置、字号、颜色过滤；假设页面背景为白色。")
    return "\n\n".join(pages)


# ================================================================ 代码层：邮件


def sanitize_eml(data: bytes, report: SanitizeReport) -> str:
    """邮件：保留信头；有 HTML 部分时按“邮件客户端显示的样子”清洗 HTML，否则用纯文本；附件同样清洗。"""
    msg = email.message_from_bytes(data, policy=email.policy.default)
    lines = [f"{h}: {msg[h]}" for h in ("From", "To", "Cc", "Subject", "Date") if msg[h]]
    html_part = msg.get_body(preferencelist=("html",))
    plain_part = msg.get_body(preferencelist=("plain",))
    if html_part is not None:
        lines += ["", sanitize_html(html_part.get_content(), report)]
        if plain_part is not None:
            report.notes.append("邮件同时有纯文本和 HTML 部分：按邮件客户端默认显示的 HTML 部分清洗后使用，纯文本部分未使用。")
    elif plain_part is not None:
        lines += ["", plain_part.get_content().strip()]
    for part in msg.iter_attachments():
        name = part.get_filename() or "(未命名附件)"
        ctype = part.get_content_type()
        lines += ["", f"[附件: {name} ({ctype})]"]
        if ctype == "application/pdf":
            lines.append(sanitize_pdf(part.get_content(), report))
        elif ctype == "text/html":
            lines.append(sanitize_html(part.get_content(), report))
        elif ctype.startswith("text/"):
            lines.append(part.get_content().strip())
        else:
            report.remove("attachment", "不支持的附件类型，未交给模型", name)
    return "\n".join(lines).strip()


# ================================================================ 代码层：通用文字处理

# 只针对“写给 AI / 工具”的句子，不针对 email、payment、链接等正常业务词：
# 内部工具名（真实供应商不可能知道）和典型的角色 / 规则覆盖说法。
_AI_DIRECTED = re.compile(
    r"\b(send_email|approve_payment|read_vendor_document)\b"
    r"|\bignore (?:all |any )?(?:previous|prior|above|earlier) (?:instructions|rules|prompts?)\b"
    r"|\b(?:system|developer) (?:prompt|message|instructions?)\b"
    r"|\byou are (?:now )?(?:an? )?(?:ai|assistant|agent|language model)\b"
    r"|\bas an ai\b|\bnew instructions?\b|\b(?:ai|llm) (?:agent|assistant|reviewer)s?\b",
    re.I,
)


def strip_invisible_chars(text: str, report: SanitizeReport) -> str:
    """去掉零宽字符等不可见的格式控制字符（Unicode Cf 类），它们常被用来拆开关键词。"""
    count = sum(1 for ch in text if unicodedata.category(ch) == "Cf")
    if count:
        report.remove("invisible_chars", f"去掉 {count} 个零宽 / 格式控制字符", f"{count} chars")
        text = "".join(ch for ch in text if unicodedata.category(ch) != "Cf")
    return unicodedata.normalize("NFKC", text)


def quarantine_ai_directed(text: str, report: SanitizeReport) -> str:
    """把明显写给 AI 的句子移出正文（按句子，不按整份文档）。"""
    out_lines = []
    for line in text.splitlines():
        sentences = re.split(r"(?<=[.!?])\s+", line)
        kept = []
        for s in sentences:
            if _AI_DIRECTED.search(s):
                report.remove("ai_directed_instruction", "可见文字中写给 AI 或内部工具的指令", s)
            else:
                kept.append(s)
        if kept:
            out_lines.append(" ".join(kept))
    return "\n".join(out_lines)


def sanitize_document(path: Path) -> tuple[str, SanitizeReport]:
    data = path.read_bytes()
    suffix = path.suffix.lower()
    report = SanitizeReport(file=path.name, sha256=hashlib.sha256(data).hexdigest(), format=suffix.lstrip("."))
    if suffix in (".html", ".htm"):
        text = sanitize_html(data.decode("utf-8", errors="replace"), report)
    elif suffix == ".pdf":
        text = sanitize_pdf(data, report)
    elif suffix == ".eml":
        text = sanitize_eml(data, report)
    else:
        raise ValueError(f"不支持的文件类型 {suffix}，只支持 .html / .htm / .pdf / .eml")
    report.chars_before = len(data)
    text = strip_invisible_chars(text, report)
    text = quarantine_ai_directed(text, report)
    report.chars_after = len(text)
    return text, report


# ================================================================ 架构层：依据可信手册的决策


def _table_rows(section_title: str) -> list[list[str]]:
    hb = HANDBOOK_PATH.read_text(encoding="utf-8")
    m = re.search(rf"^## {re.escape(section_title)}\n(.*?)(?=^## |\Z)", hb, re.S | re.M)
    if not m:
        raise ValueError(f"手册里找不到章节：{section_title}")
    rows = []
    for line in m.group(1).splitlines():
        if line.startswith("|") and not re.match(r"^\|[\s|:-]+\|$", line):
            rows.append([c.strip() for c in line.strip().strip("|").split("|")])
    return rows[1:]  # 去掉表头


def _norm(name: str | None) -> str:
    return " ".join((name or "").casefold().split())


@lru_cache(maxsize=1)
def handbook_rules() -> dict[str, Any]:
    approved = {_norm(r[1]): r[1] for r in _table_rows("5. Approved vendors")}
    removed = {_norm(r[0]): (r[0], r[1]) for r in _table_rows("6. Removed and pending vendors (never recommend)")}
    catalogue = {r[0].upper(): float(r[4].lstrip("$")) for r in _table_rows("9. Product catalogue")}
    return {"approved": approved, "removed": removed, "catalogue": catalogue}


MAX_LEAD_TIME_DAYS = 21  # 手册第 4 节
MAX_MINIMUM_ORDER = 600  # 手册第 4 节


def decide(q: ExtractedQuote) -> tuple[VendorDecision, list[dict[str, Any]]]:
    """按手册第 4 节的四项检查做决策，返回最终答案和每一项检查的明细。模型不参与这一步。"""
    rules = handbook_rules()
    checks: list[dict[str, Any]] = []

    v = _norm(q.vendor)
    if not q.vendor:
        checks.append({"check": "approved_vendor", "passed": False, "detail": "the document does not name a vendor"})
    elif v in rules["removed"]:
        name, status = rules["removed"][v]
        checks.append({"check": "approved_vendor", "passed": False, "detail": f"{name} is on the removed/pending list in handbook section 6 ({status})"})
    elif v in rules["approved"]:
        checks.append({"check": "approved_vendor", "passed": True, "detail": f"{rules['approved'][v]} is on the approved list"})
    else:
        checks.append({"check": "approved_vendor", "passed": False, "detail": f"{q.vendor} is not on the approved vendor list in handbook section 5"})

    sku = (q.sku or "").upper()
    if q.unit_price is None:
        checks.append({"check": "price", "passed": False, "detail": "the document does not state a unit price"})
    elif sku not in rules["catalogue"]:
        checks.append({"check": "price", "passed": False, "detail": f"SKU {q.sku} is not in the handbook catalogue, so the max unit cost cannot be checked"})
    else:
        cap = rules["catalogue"][sku]
        checks.append({"check": "price", "passed": q.unit_price <= cap,
                       "detail": f"unit price ${q.unit_price:.2f} {'<=' if q.unit_price <= cap else '>'} max unit cost ${cap:.2f}"})

    if q.lead_time_days is None:
        checks.append({"check": "lead_time", "passed": False, "detail": "the document does not state a lead time"})
    else:
        ok = q.lead_time_days <= MAX_LEAD_TIME_DAYS
        checks.append({"check": "lead_time", "passed": ok,
                       "detail": f"lead time {q.lead_time_days} days {'<=' if ok else '>'} {MAX_LEAD_TIME_DAYS} days"})

    if q.minimum_order is None:
        checks.append({"check": "minimum_order", "passed": False, "detail": "the document does not state a minimum order"})
    else:
        ok = q.minimum_order <= MAX_MINIMUM_ORDER
        checks.append({"check": "minimum_order", "passed": ok,
                       "detail": f"minimum order {q.minimum_order} {'<=' if ok else '>'} {MAX_MINIMUM_ORDER} units"})

    failed = [c for c in checks if not c["passed"]]
    names = {"approved_vendor": "approved-vendor", "price": "price", "lead_time": "lead-time",
             "minimum_order": "minimum-order"}
    if failed:
        reason = "Do not recommend: the " + " and ".join(names[c["check"]] for c in failed) + \
                 " check" + ("s fail" if len(failed) > 1 else " fails") + " (" + "; ".join(c["detail"] for c in failed) + ")."
        decision = "do_not_recommend"
    else:
        reason = "Recommend: all four purchasing checks pass (approved vendor, price, lead time, minimum order)."
        decision = "recommend"
    return VendorDecision(vendor=q.vendor, sku=q.sku, unit_price=q.unit_price, lead_time_days=q.lead_time_days,
                          minimum_order=q.minimum_order, decision=decision, reason=reason), checks


# ================================================================ 架构层：操作权限


@dataclass(frozen=True)
class HumanApproval:
    """独立的人类批准。只能由 issue_human_approval() 在交互式终端里由真人输入后生成；
    文档文字或模型输出不可能生成这个对象，所以“已经获得批准”之类的说法没有任何作用。"""

    approver: str
    action: str
    args_digest: str


def _digest(action: str, args: dict[str, Any]) -> str:
    return hashlib.sha256(f"{action}:{sorted(args.items())}".encode()).hexdigest()[:16]


def issue_human_approval(action: str, args: dict[str, Any]) -> HumanApproval | None:
    """人类在终端里逐项确认后才签发批准。非交互环境（例如 Agent 运行时）直接返回 None。"""
    if not sys.stdin.isatty():
        return None
    print(f"需要人工批准：{action} {args}")
    name = input("审批人姓名（留空拒绝）：").strip()
    if not name or input(f"输入 APPROVE 确认执行 {action}：").strip() != "APPROVE":
        return None
    return HumanApproval(approver=name, action=action, args_digest=_digest(action, args))


def approval_valid(approval: HumanApproval | None, action: str, args: dict[str, Any]) -> bool:
    return (isinstance(approval, HumanApproval) and approval.action == action
            and approval.args_digest == _digest(action, args))

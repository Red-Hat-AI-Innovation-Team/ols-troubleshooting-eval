"""Final HTML-to-text converter for SO CPT data.

Based on bs4 strategy, simplified for CPT:
- Code blocks preserved with indentation + language hints
- Inline code preserved with backticks
- Headings converted to markdown
- Lists converted to bullets
- Bold/italic/images stripped (noise for CPT)
- Paragraph breaks preserved
"""
from __future__ import annotations

import re
from bs4 import BeautifulSoup, NavigableString, Tag

_LANG_RE = re.compile(r"(?:language|lang)[_-](\w+)", re.IGNORECASE)


def _lang_hint(tag: Tag) -> str:
    for t in (tag, *tag.parents):
        if not isinstance(t, Tag):
            continue
        for cls in (t.get("class") or []):
            m = _LANG_RE.search(cls)
            if m:
                return m.group(1)
    return ""


def _children(tag: Tag) -> str:
    parts: list[str] = []
    for ch in tag.children:
        if isinstance(ch, NavigableString):
            parts.append(str(ch))
        elif isinstance(ch, Tag):
            parts.append(_convert(ch))
    return "".join(parts)


def _convert(tag: Tag) -> str:
    # Code block: <pre><code>
    if tag.name == "pre" and tag.code:
        lang = _lang_hint(tag.code) if isinstance(tag.code, Tag) else ""
        raw = (tag.code.get_text() if isinstance(tag.code, Tag) else tag.get_text()).strip("\n")
        return "\n```" + lang + "\n" + raw + "\n```\n"

    # Inline code
    if tag.name == "code":
        return "`" + tag.get_text() + "`"

    # Headings
    if tag.name in ("h1", "h2", "h3", "h4", "h5", "h6"):
        inner = _children(tag).strip()
        if not inner:
            return ""
        hashes = "#" * int(tag.name[1])
        return "\n" + hashes + " " + inner + "\n"

    # Lists
    if tag.name in ("ul", "ol"):
        items = []
        for ch in tag.children:
            if isinstance(ch, Tag) and ch.name == "li":
                inner = _children(ch).strip()
                inner = re.sub(r"\n{2,}", "\n  ", inner)
                items.append("- " + inner)
        return "\n" + "\n".join(items) + "\n"

    if tag.name == "li":
        inner = _children(tag).strip()
        inner = re.sub(r"\n{2,}", "\n  ", inner)
        return "- " + inner

    # Links: text only
    if tag.name == "a":
        return _children(tag)

    # Paragraphs / divs
    if tag.name in ("p", "div"):
        inner = _children(tag).strip()
        return "\n" + inner + "\n" if inner else ""

    # Blockquote
    if tag.name == "blockquote":
        inner = _children(tag).strip()
        if not inner:
            return ""
        return "\n" + "\n".join("> " + l for l in inner.splitlines()) + "\n"

    # Line breaks
    if tag.name == "br":
        return "\n"
    if tag.name == "hr":
        return "\n---\n"

    # Skip: images, bold/italic just pass through text
    if tag.name == "img":
        return ""
    if tag.name in ("strong", "b", "em", "i"):
        return _children(tag)

    # Fallback: recurse
    return _children(tag)


def html_to_text(html: str) -> str:
    """Convert SO HTML to clean plain text for CPT."""
    if not html or not html.strip():
        return ""
    soup = BeautifulSoup(html, "html.parser")
    text = _children(soup)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()

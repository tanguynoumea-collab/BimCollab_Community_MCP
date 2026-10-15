"""Read/write BIMcollab Zoom native "pseudo-XML" files.

Zoom files (.bcsv, .bcr, .bcl, .bcsp, .bcp) are NOT well-formed XML: after the
``<?xml version="1.0"?>`` prolog they contain several root elements in a row
(a header element such as ``<bimcollabsmartviewfile>`` followed by a body such
as ``<SMARTVIEWSETS>``). They are UTF-8, CRLF, 4-space indented, and never use
self-closing tags.

This module parses such a file into a list of ElementTree roots and serializes
it back byte-for-byte in Zoom's own style, so files edited by the CLI can be
imported by Zoom and diffed cleanly.
"""

from __future__ import annotations

import os
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from pathlib import Path

PROLOG = '<?xml version="1.0"?>'
INDENT = "    "
WRAPPER = "__bczoom_root__"


class ZoomFormatError(ValueError):
    """Raised when a file is not a valid Zoom pseudo-XML document."""


@dataclass
class ZoomDocument:
    """A parsed Zoom file: an ordered list of root elements."""

    roots: list[ET.Element] = field(default_factory=list)

    # -- accessors ---------------------------------------------------------
    @property
    def header(self) -> ET.Element:
        if not self.roots:
            raise ZoomFormatError("Document has no root elements")
        return self.roots[0]

    @property
    def body(self) -> ET.Element:
        if len(self.roots) < 2:
            raise ZoomFormatError("Document has no body element")
        return self.roots[1]

    def root(self, tag: str) -> ET.Element | None:
        for r in self.roots:
            if r.tag == tag:
                return r
        return None

    def copy(self) -> "ZoomDocument":
        return parse_string(serialize(self))


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------

def _normalize_newlines(text: str) -> str:
    return text.replace("\r\n", "\n").replace("\r", "\n")


# Zoom writes raw C0 control characters (e.g. 0x1F typed into a title), which
# XML 1.0 forbids. They are swapped for Private Use Area code points while the
# document is in memory and restored on serialization.
_CTRL_BASE = 0xF700
_CTRL_CHARS = [c for c in range(0x20) if c not in (0x09, 0x0A, 0x0D)]
_PROTECT = {c: chr(_CTRL_BASE + c) for c in _CTRL_CHARS}
_RESTORE = {_CTRL_BASE + c: chr(c) for c in _CTRL_CHARS}


def parse_string(text: str) -> ZoomDocument:
    text = _normalize_newlines(text).lstrip("﻿").translate(_PROTECT)
    stripped = text.lstrip()
    if stripped.startswith("<?xml"):
        end = stripped.find("?>")
        if end < 0:
            raise ZoomFormatError("Unterminated XML prolog")
        stripped = stripped[end + 2:]
    try:
        wrapper = ET.fromstring(f"<{WRAPPER}>{stripped}</{WRAPPER}>")
    except ET.ParseError as exc:
        raise ZoomFormatError(f"Not a valid Zoom file: {exc}") from exc
    roots = list(wrapper)
    if not roots:
        raise ZoomFormatError("File contains no elements")
    return ZoomDocument(roots=roots)


def parse_file(path: str | os.PathLike) -> ZoomDocument:
    p = Path(path)
    if not p.exists():
        raise FileNotFoundError(f"File not found: {p}")
    data = p.read_bytes()
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        # Older Zoom builds occasionally wrote ANSI text; be lenient on read.
        text = data.decode("cp1252", errors="replace")
    return parse_string(text)


# ---------------------------------------------------------------------------
# Serialization (Zoom style)
# ---------------------------------------------------------------------------

def _escape(text: str) -> str:
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _serialize_element(el: ET.Element, depth: int, out: list[str]) -> None:
    ind = INDENT * depth
    children = list(el)
    if not children:
        out.append(f"{ind}<{el.tag}>{_escape(el.text or '')}</{el.tag}>")
        return
    out.append(f"{ind}<{el.tag}>")
    for child in children:
        _serialize_element(child, depth + 1, out)
    out.append(f"{ind}</{el.tag}>")


def serialize(doc: ZoomDocument) -> str:
    """Serialize to a str with LF newlines (callers convert to CRLF on disk)."""
    blocks = []
    for root in doc.roots:
        lines: list[str] = []
        _serialize_element(root, 0, lines)
        blocks.append("\n".join(lines))
    return (PROLOG + "\n" + "\n\n".join(blocks) + "\n").translate(_RESTORE)


def to_bytes(doc: ZoomDocument) -> bytes:
    return serialize(doc).replace("\n", "\r\n").encode("utf-8")


def write_file(doc: ZoomDocument, path: str | os.PathLike) -> int:
    """Write atomically (temp file + replace). Returns bytes written."""
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    data = to_bytes(doc)
    tmp = p.with_name(p.name + ".tmp")
    tmp.write_bytes(data)
    os.replace(tmp, p)
    return len(data)


# ---------------------------------------------------------------------------
# Small element helpers
# ---------------------------------------------------------------------------

def sub(parent: ET.Element, tag: str, text: str | None = None) -> ET.Element:
    el = ET.SubElement(parent, tag)
    if text is not None:
        el.text = str(text)
    return el


def text_of(el: ET.Element | None, tag: str, default: str = "") -> str:
    if el is None:
        return default
    child = el.find(tag)
    if child is None or child.text is None:
        return default
    return restore_text(child.text)


def restore_text(text: str) -> str:
    """Undo the control-character protection for display/comparison."""
    return text.translate(_RESTORE)

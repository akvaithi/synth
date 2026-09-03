"""Text extraction from the file types that actually carry information.

Newsletters, flyers and programme announcements put the substance in a PDF or a .docx, not
in the subject line, so extraction is what makes those sources usable at all.

Three engines, each where it is strongest:

* **MarkItDown** (Microsoft) for the Office family. It emits Markdown rather than flat text,
  so headings, tables and lists survive — which matters, because a degree audit or a
  scholarship table loses its meaning when flattened into prose.
* **PDFKit** for PDFs. Native, fast, no dependencies. MarkItDown's PDF extra pulls
  `pdfminer-six` -> `cryptography`, which has no x86_64 macOS wheel and needs a Rust
  toolchain to build, and it is only wanted for encrypted PDFs.
* **textutil** for the rich-text formats the other two do not cover (.rtf, .doc, .odt).
"""
from __future__ import annotations

import os
import subprocess
import unicodedata
import xml.etree.ElementTree as ET
import zipfile

# MarkItDown: structure-preserving, best for anything tabular or slide-based.
MARKITDOWN = {".docx", ".pptx", ".xlsx", ".xls", ".msg", ".csv", ".json", ".xml",
              ".html", ".htm", ".epub"}
# textutil: the rich-text formats MarkItDown does not handle.
TEXTUTIL = {".doc", ".rtf", ".rtfd", ".odt", ".webarchive", ".wordml"}
PLAIN = {".txt", ".md", ".markdown", ".tsv", ".yaml", ".yml", ".ics", ".log"}
PDF = {".pdf"}
# Apple iWork bundles: zipped, with no reliable plain-text payload.
IWORK = {".pages", ".key", ".numbers"}
SKIP = {".jpg", ".jpeg", ".png", ".gif", ".heic", ".mov", ".mp4", ".zip", ".dmg",
        ".sketch", ".psd", ".ai", ".xcf", ".ds_store"}

MAX_CHARS = 200_000


class ExtractionError(RuntimeError):
    pass


def _clean(text: str) -> str:
    text = unicodedata.normalize("NFC", text)
    lines = [ln.rstrip() for ln in text.splitlines()]
    out, blanks = [], 0
    for ln in lines:
        if ln.strip():
            blanks = 0
            out.append(ln)
        else:
            blanks += 1
            if blanks <= 1:
                out.append("")
    return "\n".join(out).strip()[:MAX_CHARS]


_MD = None


def _markitdown(path: str) -> str:
    global _MD
    if _MD is None:
        from markitdown import MarkItDown
        _MD = MarkItDown(enable_plugins=False)
    result = _MD.convert(path)
    text = result.text_content or ""
    if not text.strip():
        raise ExtractionError("MarkItDown returned no content")
    return text


def _textutil(path: str) -> str:
    r = subprocess.run(["/usr/bin/textutil", "-stdout", "-convert", "txt", path],
                       capture_output=True, timeout=120)
    if r.returncode != 0:
        raise ExtractionError(r.stderr.decode()[:200] or "textutil failed")
    return r.stdout.decode("utf-8", errors="replace")


def _docx_xml(path: str) -> str:
    """Fallback for .docx: it is a zip of XML, so the text is recoverable without any parser."""
    with zipfile.ZipFile(path) as z:
        xml = z.read("word/document.xml").decode("utf-8", errors="replace")
    root = ET.fromstring(xml)
    ns = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
    paras = []
    for p in root.iter(f"{ns}p"):
        runs = [t.text or "" for t in p.iter(f"{ns}t")]
        paras.append("".join(runs))
    return "\n".join(paras)


def _pdf(path: str) -> str:
    from Quartz import PDFDocument
    from Foundation import NSURL

    url = NSURL.fileURLWithPath_(path)
    doc = PDFDocument.alloc().initWithURL_(url)
    if doc is None:
        raise ExtractionError("PDFKit could not open the document")
    if doc.isLocked():
        raise ExtractionError("PDF is password protected")
    text = doc.string()
    if not text or not text.strip():
        # Scanned documents carry no text layer. Say so rather than returning silence.
        raise ExtractionError(f"no text layer ({doc.pageCount()} pages, likely scanned)")
    return text


def _iwork(path: str) -> str:
    """iWork bundles sometimes embed a preview PDF; otherwise there is nothing to read."""
    try:
        with zipfile.ZipFile(path) as z:
            for name in z.namelist():
                if name.lower().endswith("preview.pdf"):
                    import tempfile
                    with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as tmp:
                        tmp.write(z.read(name))
                        tmp_path = tmp.name
                    try:
                        return _pdf(tmp_path)
                    finally:
                        os.unlink(tmp_path)
    except zipfile.BadZipFile:
        pass
    raise ExtractionError("iWork document with no readable preview")


def is_icloud_placeholder(path: str) -> bool:
    """iCloud evicts file contents and leaves a .icloud stub; reading it yields nothing."""
    base = os.path.basename(path)
    return base.startswith(".") and base.endswith(".icloud")


def is_dataless(path: str) -> bool:
    """Whether iCloud has evicted this file's contents from the disk.

    Size is not the test, which is what made the old check useless. The file provider reports
    an evicted file at its full logical size -- that is the whole point of the placeholder --
    so `getsize(path) > 0` is true for a file whose bytes are entirely absent. What actually
    distinguishes one is that it occupies no blocks.

    Reading a dataless file normally triggers materialisation and blocks until it lands. Under
    load the file provider returns EDEADLK instead ("Resource deadlock avoided"), which is
    where a sweep's failures come from: 44 in one pass here, all of them transient.
    """
    try:
        st = os.stat(path)
    except OSError:
        return False
    return st.st_blocks == 0 and st.st_size > 0


def materialise(path: str, timeout: int = 60) -> bool:
    """Ask iCloud to download an evicted file. Returns True if the bytes are present."""
    if not os.path.exists(path):
        return False
    if os.path.getsize(path) > 0 and not is_dataless(path):
        return True
    subprocess.run(["/usr/bin/brctl", "download", path], capture_output=True, timeout=timeout)
    return os.path.exists(path) and os.path.getsize(path) > 0 and not is_dataless(path)


def extract(path: str) -> str:
    ext = os.path.splitext(path)[1].lower()
    if ext in SKIP:
        raise ExtractionError(f"binary or media type ({ext})")
    if not os.path.exists(path):
        raise ExtractionError("file not present")
    if ext in PLAIN:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            return _clean(f.read(MAX_CHARS * 2))
    if ext in PDF:
        return _clean(_pdf(path))
    if ext in IWORK:
        return _clean(_iwork(path))
    if ext in MARKITDOWN:
        try:
            return _clean(_markitdown(path))
        except Exception as e:
            # Fall through the chain rather than losing the document.
            for fallback in (_textutil, _docx_xml) if ext == ".docx" else (_textutil,):
                try:
                    return _clean(fallback(path))
                except Exception:
                    continue
            raise ExtractionError(f"all engines failed for {ext}: {e}") from e
    if ext in TEXTUTIL:
        return _clean(_textutil(path))
    # Unknown extension: try textutil, then give up honestly.
    try:
        return _clean(_textutil(path))
    except Exception as e:
        raise ExtractionError(f"unsupported type {ext or '(none)'}: {e}") from e

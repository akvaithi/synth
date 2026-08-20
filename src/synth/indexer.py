"""Loads extracted text into the searchable document index, and OCRs what had no text layer."""
from __future__ import annotations

import os
import time

from synth import config, db, ingest

DOCUMENTS = os.path.expanduser(config.DOCUMENTS_ROOT)


def build(conn, batch: int = 200) -> dict:
    """Insert cached extracted text into `document` so FTS can reach it."""
    rows = conn.execute(
        "SELECT s.id, s.native_id, s.detail, s.content_hash FROM source s "
        "LEFT JOIN document d ON d.source_id = s.id "
        "WHERE s.kind = 'file' AND d.id IS NULL AND s.content_hash IS NOT NULL"
    ).fetchall()
    stats = {"considered": len(rows), "indexed": 0, "missing_text": 0, "chars": 0}
    for i, r in enumerate(rows, 1):
        cached = ingest.cached_text_path(r["content_hash"])
        if not os.path.exists(cached):
            stats["missing_text"] += 1
            continue
        with open(cached, encoding="utf-8") as f:
            text = f.read()
        if not text.strip():
            stats["missing_text"] += 1
            continue
        conn.execute(
            "INSERT INTO document (source_id, path, title, text, chars) VALUES (?,?,?,?,?) "
            "ON CONFLICT (source_id) DO UPDATE SET text=excluded.text, chars=excluded.chars",
            (r["id"], r["native_id"], os.path.basename(r["native_id"]), text, len(text)))
        stats["indexed"] += 1
        stats["chars"] += len(text)
        if i % batch == 0:
            conn.commit()
    conn.commit()
    return stats


def ocr_pass(conn, limit: int | None = None, max_pages: int = 20) -> dict:
    """OCR the PDFs that had no text layer. Native Vision; nothing leaves the machine."""
    from synth.ocr import ocr_pdf

    rows = conn.execute(
        "SELECT id, native_id, content_hash FROM source "
        "WHERE kind = 'file' AND detail LIKE '%no text layer%'"
    ).fetchall()
    if limit:
        rows = rows[:limit]
    stats = {"considered": len(rows), "ocred": 0, "empty": 0, "failed": 0, "chars": 0}
    t0 = time.time()
    for i, r in enumerate(rows, 1):
        path = os.path.join(DOCUMENTS, r["native_id"])
        if not os.path.exists(path):
            stats["failed"] += 1
            continue
        try:
            text = ocr_pdf(path, max_pages=max_pages)
        except Exception:
            stats["failed"] += 1
            continue
        if not text.strip():
            stats["empty"] += 1
            continue
        target = ingest.cached_text_path(r["content_hash"])
        os.makedirs(os.path.dirname(target), exist_ok=True)
        with open(target, "w", encoding="utf-8") as f:
            f.write(text)
        conn.execute(
            "UPDATE source SET detail = ? WHERE id = ?",
            (f"{os.path.basename(r['native_id'])} [OCR]", r["id"]))
        stats["ocred"] += 1
        stats["chars"] += len(text)
        if i % 10 == 0:
            conn.commit()
            print(f"  {i}/{len(rows)}  ocred={stats['ocred']} "
                  f"{(time.time()-t0)/i:.1f}s/doc", flush=True)
    conn.commit()
    stats["seconds"] = round(time.time() - t0, 1)
    return stats

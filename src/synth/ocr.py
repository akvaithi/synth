"""OCR for scanned PDFs, using the Vision framework that ships with macOS.

About a fifth of the PDFs in Documents have no text layer — scans of forms, transcripts,
letters and receipts. They are often exactly the documents carrying hard facts, so skipping
them would lose the most authoritative material in the archive.

Vision is native, free and offline, which matters: none of this leaves the machine.
"""
from __future__ import annotations


MAX_PAGES = 40          # a scanned book is not worth ten minutes
RENDER_SCALE = 2.0      # Vision needs resolution; page thumbnails at 1x read poorly


def _cgimage_for_page(page, scale: float = RENDER_SCALE):
    from Quartz import kPDFDisplayBoxMediaBox as MEDIA_BOX
    import Quartz

    bounds = page.boundsForBox_(MEDIA_BOX)
    w = int(bounds.size.width * scale)
    h = int(bounds.size.height * scale)
    if w <= 0 or h <= 0:
        return None
    space = Quartz.CGColorSpaceCreateDeviceRGB()
    ctx = Quartz.CGBitmapContextCreate(
        None, w, h, 8, 0, space, Quartz.kCGImageAlphaNoneSkipLast)
    if ctx is None:
        return None
    Quartz.CGContextSetRGBFillColor(ctx, 1.0, 1.0, 1.0, 1.0)
    Quartz.CGContextFillRect(ctx, Quartz.CGRectMake(0, 0, w, h))
    Quartz.CGContextScaleCTM(ctx, scale, scale)
    Quartz.CGContextTranslateCTM(ctx, -bounds.origin.x, -bounds.origin.y)
    page.drawWithBox_toContext_(MEDIA_BOX, ctx)
    return Quartz.CGBitmapContextCreateImage(ctx)


def _recognise(cgimage) -> str:
    import Vision

    request = Vision.VNRecognizeTextRequest.alloc().init()
    request.setRecognitionLevel_(0)          # 0 = accurate
    request.setUsesLanguageCorrection_(True)
    handler = Vision.VNImageRequestHandler.alloc().initWithCGImage_options_(cgimage, None)
    ok, err = handler.performRequests_error_([request], None)
    if not ok:
        return ""
    lines = []
    for obs in request.results() or []:
        best = obs.topCandidates_(1)
        if best and len(best):
            lines.append(best[0].string())
    return "\n".join(lines)


def ocr_pdf(path: str, max_pages: int = MAX_PAGES) -> str:
    from Foundation import NSURL
    from Quartz import PDFDocument

    doc = PDFDocument.alloc().initWithURL_(NSURL.fileURLWithPath_(path))
    if doc is None:
        raise RuntimeError("PDFKit could not open the document")
    pages = min(doc.pageCount(), max_pages)
    chunks = []
    for i in range(pages):
        page = doc.pageAtIndex_(i)
        if page is None:
            continue
        img = _cgimage_for_page(page)
        if img is None:
            continue
        text = _recognise(img)
        if text.strip():
            chunks.append(text)
    if doc.pageCount() > max_pages:
        chunks.append(f"\n[OCR stopped at {max_pages} of {doc.pageCount()} pages]")
    return "\n\n".join(chunks)

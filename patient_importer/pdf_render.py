"""Serialize PDFium calls shared by extraction and document previews."""
from threading import RLock


# PDFium shares process-wide state even when callers use different documents.
PDFIUM_LOCK = RLock()

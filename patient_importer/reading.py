"""Read one preserved original in a worker process. Takes and returns plain data."""
from pathlib import Path
import time

from . import analysis, extraction
from .telemetry_schema import failure_code


class ReadingFailed(Exception):
    """A worker could not read a file. The message is the one the reader raised.

    `place` is the diagnostics code of where it failed: names from the code, never the message.
    """
    def __init__(self, message, place=None):
        super().__init__(message)
        self.place = place


def read_original(job):
    """Extract text and parse it. Failures come back as text, attributed to their stage.

    Storage decides what a failure means, in file order, exactly as for a file read
    in the main process.
    """
    original, filename = job
    started = time.monotonic()
    try:
        extracted = extraction.extract(Path(original))
    except Exception as exc:
        return {'extraction_error': str(exc), 'failure': failure_code('extraction', exc)}
    try:
        parsed = analysis.analyze(extracted, filename)
    except Exception as exc:
        return {'extracted': extracted, 'analysis_error': str(exc), 'failure': failure_code('analysis', exc)}
    # How long the document took, for the optional speed ranges.
    return {'extracted': extracted, 'parsed': parsed, 'seconds': time.monotonic() - started}

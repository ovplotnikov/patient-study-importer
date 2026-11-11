"""Read one preserved original in a worker process. Takes and returns plain data."""
from pathlib import Path

from . import analysis, extraction


class ReadingFailed(Exception):
    """A worker could not read a file. The message is the one the reader raised."""


def read_original(job):
    """Extract text and parse it. Failures come back as text, attributed to their stage.

    Storage decides what a failure means, in file order, exactly as for a file read
    in the main process.
    """
    original, filename = job
    try:
        extracted = extraction.extract(Path(original))
    except Exception as exc:
        return {'extraction_error': str(exc)}
    try:
        return {'extracted': extracted, 'parsed': analysis.analyze(extracted, filename)}
    except Exception as exc:
        return {'extracted': extracted, 'analysis_error': str(exc)}

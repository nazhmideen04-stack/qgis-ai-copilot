"""
Link between running code and the status panel.

Code (download_osm, run_alg, processing.run in AI code) reports progress through
report_status() and make_feedback(); the status panel subscribes through set_status_handler().
The «Stop» button calls request_cancel() — every active Processing algorithm gets cancel().
"""
from qgis.core import QgsApplication, QgsProcessingFeedback

_handler = None
_active_feedbacks = []
_cancel_requested = False
_outputs = []


class CancelledByUser(Exception):
    """The user pressed «Stop» while code was running."""


def set_status_handler(handler):
    """handler(stage: str, detail: str, percent: float | None), or None to disable."""
    global _handler
    _handler = handler


def report_status(stage, detail="", percent=None):
    if _handler is not None:
        _handler(stage, detail, percent)


def register_output(path):
    """Registers a created file — the panel will show «open file / folder» links."""
    if path and path not in _outputs:
        _outputs.append(path)


def take_outputs():
    files = list(_outputs)
    _outputs.clear()
    return files


def reset_cancel():
    global _cancel_requested
    _cancel_requested = False
    _active_feedbacks.clear()


def request_cancel():
    global _cancel_requested
    _cancel_requested = True
    for feedback in list(_active_feedbacks):
        feedback.cancel()


def is_cancel_requested():
    return _cancel_requested


def check_cancel():
    if _cancel_requested:
        raise CancelledByUser("Stopped by user.")


class StatusFeedback(QgsProcessingFeedback):
    """Feedback for processing.run: forwards the algorithm's percent and messages to the status panel."""

    def __init__(self, stage):
        super().__init__()
        self.stage = stage
        self.detail = ""
        self.progressChanged.connect(self._on_progress)
        _active_feedbacks.append(self)
        if _cancel_requested:
            self.cancel()

    def _on_progress(self, percent):
        report_status(self.stage, self.detail, percent)

    def setProgressText(self, text):
        self.detail = str(text)
        report_status(self.stage, self.detail, self.progress() or None)
        super().setProgressText(text)

    def pushInfo(self, info):
        self.detail = str(info).splitlines()[0][:120] if info else ""
        report_status(self.stage, self.detail, self.progress() or None)
        super().pushInfo(info)

    def release(self):
        if self in _active_feedbacks:
            _active_feedbacks.remove(self)


def make_feedback(stage):
    check_cancel()
    return StatusFeedback(stage)


def _default_stage(algorithm):
    alg_id = algorithm if isinstance(algorithm, str) else algorithm.id()
    alg = QgsApplication.processingRegistry().algorithmById(alg_id)
    return f"Running: {alg.displayName()}" if alg is not None else f"Running: {alg_id}"


def run_processing(algorithm, params, stage=None, **kwargs):
    """processing.run with progress in the status panel and «Stop» support."""
    import processing  # imported here: QGIS loads the Processing module after plugins
    feedback = kwargs.pop("feedback", None) or make_feedback(stage or _default_stage(algorithm))
    if isinstance(feedback, StatusFeedback):
        report_status(feedback.stage, "", None)
    try:
        result = processing.run(algorithm, params, feedback=feedback, **kwargs)
    finally:
        if isinstance(feedback, StatusFeedback):
            feedback.release()
    check_cancel()
    return result


class ProcessingProxy:
    """Replaces the `processing` module in AI code: processing.run(...) shows progress automatically.
    All other attributes (runAndLoadResults, execAlgorithmDialog...) come from the real module."""

    def __getattr__(self, name):
        import processing
        return getattr(processing, name)

    def run(self, algorithm, params, *args, **kwargs):
        if len(args) >= 2:  # feedback passed positionally (onFinish, feedback, ...)
            import processing
            return processing.run(algorithm, params, *args, **kwargs)
        return run_processing(algorithm, params, *args, **kwargs)

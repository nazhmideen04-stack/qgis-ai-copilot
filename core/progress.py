"""
Link between running code and the status panel.

Code (download_osm, run_alg, processing.run in AI code) reports progress through
report_status() and make_feedback(); the status panel subscribes through set_status_handler().
The «Stop» button calls request_cancel() — every active Processing algorithm gets cancel().
"""
from qgis.core import QgsApplication, QgsMapLayer, QgsProcessingException, QgsProcessingFeedback, QgsProject, QgsTask
from qgis.PyQt.QtCore import QEventLoop, QTimer, pyqtSignal

from .compat import algorithm_runs_in_main_thread

_handler = None
_active_feedbacks = []
_cancel_requested = False
_outputs = []
_background = True       # heavy Processing algorithms run in a background thread (the window stays responsive)
_running_tasks = []      # keeps Python references to background tasks until they end


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
    """Feedback for processing.run: forwards the algorithm's percent and messages to the status panel.
    Thread-safe: when the algorithm runs in a background thread, messages travel to the main thread via a signal."""

    message_signal = pyqtSignal(str)

    def __init__(self, stage):
        super().__init__()
        self.stage = stage
        self.detail = ""
        self.errors = []
        self.progressChanged.connect(self._on_progress)
        self.message_signal.connect(self._on_message)
        _active_feedbacks.append(self)
        if _cancel_requested:
            self.cancel()

    def _on_progress(self, percent):
        report_status(self.stage, self.detail, percent)

    def _on_message(self, text):
        self.detail = text
        report_status(self.stage, self.detail, self.progress() or None)

    def setProgressText(self, text):
        self.message_signal.emit(str(text))
        super().setProgressText(text)

    def pushInfo(self, info):
        self.message_signal.emit(str(info).splitlines()[0][:120] if info else "")
        super().pushInfo(info)

    def reportError(self, error, fatalError=False):
        self.errors.append(str(error))
        super().reportError(error, fatalError)

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


def set_background(enabled):
    """Turns background execution of Processing algorithms on or off (setting in the panel)."""
    global _background
    _background = bool(enabled)


def background_enabled():
    return _background


def _wait_for(task, on_cancel=None):
    """Runs a QgsTask and waits for it WITHOUT freezing QGIS: a local event loop keeps the window, the map and the
    status panel alive. Stop cancels the task. Returns when the task has ended (or at once after Stop)."""
    loop = QEventLoop()
    state = {"ended": False}

    def ended(*_args):
        state["ended"] = True
        loop.quit()

    def watch_cancel():
        if _cancel_requested and not state["ended"]:
            if on_cancel is not None:
                on_cancel()
            task.cancel()
            loop.quit()

    task.taskCompleted.connect(ended)
    task.taskTerminated.connect(ended)
    _running_tasks.append(task)
    task.taskCompleted.connect(lambda: _forget(task))
    task.taskTerminated.connect(lambda: _forget(task))
    timer = QTimer()
    timer.timeout.connect(watch_cancel)
    timer.start(150)
    QgsApplication.taskManager().addTask(task)
    if not state["ended"]:
        loop.exec()
    timer.stop()
    return state["ended"]


def _forget(task):
    if task in _running_tasks:
        _running_tasks.remove(task)


def _layer_ids(params):
    """Layers of the project are passed by id, as the Processing dialog does (safe in a background thread)."""
    project = QgsProject.instance()
    out = {}
    for key, value in params.items():
        if isinstance(value, QgsMapLayer) and project.mapLayer(value.id()) is not None:
            value = value.id()
        elif isinstance(value, (list, tuple)):
            value = [v.id() if isinstance(v, QgsMapLayer) and project.mapLayer(v.id()) is not None else v
                     for v in value]
        out[key] = value
    return out


def _run_in_background(alg_id, params, feedback):
    """processing.run in a background thread. Same result as processing.run: memory outputs become layer objects."""
    from qgis.core import QgsProcessingAlgRunnerTask
    from processing.tools import dataobjects
    alg = QgsApplication.processingRegistry().algorithmById(alg_id)
    context = dataobjects.createContext(feedback)
    params = _layer_ids(params)
    ok, message = alg.checkParameterValues(params, context)
    if not ok:
        raise QgsProcessingException(f"Unable to execute algorithm: {message}")
    task = QgsProcessingAlgRunnerTask(alg, params, context, feedback)
    outcome = {}
    task.executed.connect(lambda success, results: outcome.update(success=success, results=results))
    _wait_for(task)
    check_cancel()
    if not outcome.get("success"):
        details = "\n".join(feedback.errors[-5:]) if isinstance(feedback, StatusFeedback) else ""
        raise QgsProcessingException(f"There were errors executing the algorithm {alg_id}."
                                     + (f"\n{details}" if details else ""))
    results = dict(outcome.get("results") or {})
    for out in alg.outputDefinitions():  # like processing.run: temporary layers -> layer objects
        value = results.get(out.name())
        if isinstance(value, str) and value:
            layer = context.takeResultLayer(value)
            if layer is not None:
                results[out.name()] = layer
    return results


def _can_run_in_background(algorithm, kwargs):
    if not _background or kwargs:  # a custom context / child algorithm: keep the standard way
        return False
    alg_id = algorithm if isinstance(algorithm, str) else algorithm.id()
    alg = QgsApplication.processingRegistry().algorithmById(alg_id)
    return alg is not None and not algorithm_runs_in_main_thread(alg)


def run_processing(algorithm, params, stage=None, **kwargs):
    """processing.run with progress in the status panel and «Stop» support.
    Algorithms that allow threads run in the background: QGIS does not freeze while they work."""
    import processing  # imported here: QGIS loads the Processing module after plugins
    feedback = kwargs.pop("feedback", None) or make_feedback(stage or _default_stage(algorithm))
    if isinstance(feedback, StatusFeedback):
        report_status(feedback.stage, "", None)
    try:
        if _can_run_in_background(algorithm, kwargs):
            alg_id = algorithm if isinstance(algorithm, str) else algorithm.id()
            result = _run_in_background(alg_id, dict(params), feedback)
        else:
            result = processing.run(algorithm, params, feedback=feedback, **kwargs)
    finally:
        if isinstance(feedback, StatusFeedback):
            feedback.release()
    check_cancel()
    return result


def run_in_background(func, *args, stage="Working in the background…", **kwargs):
    """Runs a slow pure-Python computation in a background thread while QGIS stays responsive and returns its result.
    The function must NOT touch layers, the project or the interface (read the data first, pass plain values in)."""
    check_cancel()
    outcome = {}

    def work(_task):
        try:
            outcome["value"] = func(*args, **kwargs)
        except Exception as e:  # re-raised in the main thread
            outcome["error"] = e
        return True

    task = QgsTask.fromFunction(stage, work)
    report_status(stage, getattr(func, "__name__", ""), None)
    _wait_for(task)
    check_cancel()
    if "error" in outcome:
        raise outcome["error"]
    return outcome.get("value")


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

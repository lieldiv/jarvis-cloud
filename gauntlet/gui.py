"""
PySide6 GUI for the Gauntlet Loop.

Layout:
  - Goal input (QLineEdit) + Run/Stop buttons
  - Status label (Idle / Running - round N / Done / Failed)
  - Live log (QTextEdit, append-only) showing each Builder action,
    tool observation, and Critic verdict as they happen

The loop runs on a QThread via GauntletWorker so the GUI never blocks -
Qt signals carry events from the worker thread back to the main thread,
which is required (Qt widgets must only be touched from the GUI thread).
"""

from __future__ import annotations

import html

from PySide6.QtCore import QObject, Qt, QThread, Signal
from PySide6.QtWidgets import (
    QApplication,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMainWindow,
    QPushButton,
    QSpinBox,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from .config import MAX_GAUNTLET_ROUNDS
from .loop import GauntletLoop


class GauntletWorker(QObject):
    log_line = Signal(str)
    status_changed = Signal(str)
    finished = Signal(bool, str)

    def __init__(self, goal: str, max_rounds: int):
        super().__init__()
        self.goal = goal
        self.max_rounds = max_rounds
        self.loop = GauntletLoop(max_rounds=max_rounds)

    def stop(self):
        self.loop.request_stop()

    def run(self):
        def on_event(event_type: str, payload: dict):
            if event_type == "start":
                self.status_changed.emit("Running - round 1")
                self.log_line.emit(f"<b>GOAL:</b> {html.escape(payload['goal'])}")
            elif event_type == "round_start":
                self.status_changed.emit(f"Running - round {payload['round']}")
                self.log_line.emit(f"<hr><b>Round {payload['round']}</b>")
            elif event_type == "action":
                self.log_line.emit(
                    f"<span style='color:#5b8def'>Builder -&gt; {html.escape(payload['tool'])}"
                    f"({html.escape(str(payload['args']))})</span>"
                )
            elif event_type == "observation":
                self.log_line.emit(
                    f"<span style='color:#aaaaaa'>Observation: "
                    f"{html.escape(str(payload['observation']))}</span>"
                )
            elif event_type == "critic":
                color = {"PASS": "#4caf50", "RETRY": "#e07a1f", "CONTINUE": "#9e9e9e"}.get(
                    payload["verdict"], "#9e9e9e"
                )
                self.log_line.emit(
                    f"<span style='color:{color}'>Critic: {payload['verdict']} - "
                    f"{html.escape(payload['feedback'])}</span>"
                )
            elif event_type == "builder_refused":
                self.log_line.emit(
                    f"<i>Builder responded without a tool call: "
                    f"{html.escape(str(payload.get('text') or ''))}</i>"
                )
            elif event_type == "error":
                self.log_line.emit(
                    f"<span style='color:#e53935'><b>Error ({payload['stage']}):</b> "
                    f"{html.escape(payload['error'])}</span>"
                )
            elif event_type == "complete":
                self.log_line.emit(
                    f"<b style='color:#4caf50'>COMPLETE:</b> {html.escape(payload['summary'])}"
                )
            elif event_type == "exhausted":
                self.log_line.emit(
                    f"<b style='color:#e53935'>Gave up after {payload['rounds']} rounds.</b>"
                )
            elif event_type == "stopped":
                self.log_line.emit("<b>Stopped by user.</b>")

        result = self.loop.run(self.goal, on_event=on_event)
        self.status_changed.emit("Done" if result.success else "Failed")
        self.finished.emit(result.success, result.summary)


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("J.A.R.V.I.S. - Gauntlet Loop")
        self.resize(760, 560)

        self._thread: QThread | None = None
        self._worker: GauntletWorker | None = None

        central = QWidget()
        self.setCentralWidget(central)
        layout = QVBoxLayout(central)

        goal_row = QHBoxLayout()
        self.goal_input = QLineEdit()
        self.goal_input.setPlaceholderText(
            "e.g. Play Bohemian Rhapsody on Spotify, then check the weather in London"
        )
        self.goal_input.returnPressed.connect(self.on_run_clicked)
        goal_row.addWidget(QLabel("Goal:"))
        goal_row.addWidget(self.goal_input, stretch=1)

        goal_row.addWidget(QLabel("Max rounds:"))
        self.rounds_spin = QSpinBox()
        self.rounds_spin.setRange(1, 30)
        self.rounds_spin.setValue(MAX_GAUNTLET_ROUNDS)
        goal_row.addWidget(self.rounds_spin)

        self.run_button = QPushButton("Run")
        self.run_button.clicked.connect(self.on_run_clicked)
        goal_row.addWidget(self.run_button)

        self.stop_button = QPushButton("Stop")
        self.stop_button.setEnabled(False)
        self.stop_button.clicked.connect(self.on_stop_clicked)
        goal_row.addWidget(self.stop_button)

        layout.addLayout(goal_row)

        self.status_label = QLabel("Idle")
        self.status_label.setStyleSheet("font-weight: bold; padding: 4px;")
        layout.addWidget(self.status_label)

        self.log_view = QTextEdit()
        self.log_view.setReadOnly(True)
        self.log_view.setStyleSheet(
            "background-color: #1e1e1e; color: #e0e0e0; font-family: Consolas, monospace;"
        )
        layout.addWidget(self.log_view, stretch=1)

    def on_run_clicked(self):
        goal = self.goal_input.text().strip()
        if not goal:
            return
        if self._thread is not None:
            return  # already running

        self.log_view.clear()
        self.run_button.setEnabled(False)
        self.stop_button.setEnabled(True)
        self.goal_input.setEnabled(False)

        self._thread = QThread(self)
        self._worker = GauntletWorker(goal, self.rounds_spin.value())
        self._worker.moveToThread(self._thread)

        self._thread.started.connect(self._worker.run)
        self._worker.log_line.connect(self.append_log)
        self._worker.status_changed.connect(self.status_label.setText)
        self._worker.finished.connect(self.on_finished)

        self._thread.start()

    def on_stop_clicked(self):
        if self._worker is not None:
            self._worker.stop()
            self.status_label.setText("Stopping...")

    def append_log(self, html_line: str):
        self.log_view.append(html_line)

    def on_finished(self, success: bool, summary: str):
        self.append_log(f"<hr><i>Run finished ({'success' if success else 'failed'}).</i>")

        if self._thread is not None:
            self._thread.quit()
            self._thread.wait()
        self._thread = None
        self._worker = None

        self.run_button.setEnabled(True)
        self.stop_button.setEnabled(False)
        self.goal_input.setEnabled(True)


def main():
    app = QApplication.instance() or QApplication([])
    app.setStyle("Fusion")
    window = MainWindow()
    window.show()
    app.exec()


if __name__ == "__main__":
    main()

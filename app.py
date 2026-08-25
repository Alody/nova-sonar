import sys
import time
import logging
import subprocess
import copy
import os
from pathlib import Path
from concurrent.futures import Future, ThreadPoolExecutor

import numpy as np

from PySide6.QtCore import (
    QObject,
    QThread,
    Signal,
    Qt,
    QTimer,
    QPointF,
    QSize,
    QEvent,
)

from PySide6.QtGui import (
    QAction,
    QBrush,
    QColor,
    QLinearGradient,
    QPainter,
    QPainterPath,
    QPen,
    QIcon,
)

from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QComboBox,
    QDoubleSpinBox,
    QFrame,
    QHBoxLayout,
    QPushButton,
    QHeaderView,
    QLabel,
    QMainWindow,
    QMenu,
    QSlider,
    QTabWidget,
    QTableWidget,
    QTableWidgetItem,
    QSystemTrayIcon,
    QVBoxLayout,
    QWidget,
)

from nova7x import Nova7X, ChatMixState
from pipewire_mixer import (
    PipeWireMixer,
    AudioStream,
)

from pipewire_eq import PipeWireEQ
from pipewire_mic_eq import PipeWireMicEQ
from pipewire_spatial import PipeWireSpatial


LOGGER = logging.getLogger("nova_sonar")


class EQSlider(QSlider):
    """Vertical EQ control with a quick neutral reset."""

    def mouseDoubleClickEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self.setValue(0)
            event.accept()
            return
        super().mouseDoubleClickEvent(event)


class SpectrumWorker(QThread):
    spectrum_ready = Signal(object)

    def __init__(self, device="nova_sonar_eq.monitor"):
        super().__init__()
        self.device = device
        self._running = True
        self._active = False
        self._process = None

    def set_active(self, active: bool):
        self._active = bool(active)
        if not self._active and self._process is not None:
            try:
                self._process.terminate()
            except OSError:
                pass

    def stop(self):
        self._running = False
        self.set_active(False)

    def run(self):
        command = [
            "parec", f"--device={self.device}",
            "--format=float32le", "--rate=48000", "--channels=1",
            "--latency-msec=10", "--process-time-msec=5",
        ]
        # A 2048-sample window limits FFT/repaint traffic to about 23 FPS.
        frame_size = 2048
        frame_bytes = frame_size * 4
        window = np.hanning(frame_size)
        normalization = max(1.0, window.sum() / 2.0)
        frequencies = np.fft.rfftfreq(frame_size, 1.0 / 48000.0)
        display_frequencies = np.geomspace(20.0, 20000.0, 128)
        smoothed = np.full(128, -90.0)

        while self._running:
            if not self._active:
                self.msleep(100)
                continue

            try:
                self._process = subprocess.Popen(
                    command,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.DEVNULL,
                    bufsize=0,
                )
                pending = bytearray()
                while (
                    self._running
                    and self._active
                    and self._process.stdout is not None
                ):
                    chunk = self._process.stdout.read(frame_bytes - len(pending))
                    if not chunk:
                        break
                    pending.extend(chunk)
                    if len(pending) < frame_bytes:
                        continue

                    frame = bytes(pending)
                    pending.clear()
                    samples = np.frombuffer(frame, dtype="<f4", count=frame_size)
                    magnitude = np.abs(np.fft.rfft(samples * window)) / normalization
                    db = 20.0 * np.log10(np.maximum(magnitude, 1e-7))
                    current = np.interp(display_frequencies, frequencies, db)
                    # Peaks rise immediately; falling energy decays smoothly.
                    smoothed = np.maximum(current, smoothed * 0.40 + current * 0.60)
                    self.spectrum_ready.emit(smoothed.astype(float).tolist())
            except OSError as error:
                LOGGER.warning("Spectrum analyzer reconnecting: %s", error)
            finally:
                if self._process is not None:
                    try:
                        self._process.terminate()
                    except OSError:
                        pass
                    self._process = None

            if self._running and self._active:
                self.msleep(500)


class AudioEventWorker(QThread):
    topology_changed = Signal()

    def __init__(self):
        super().__init__()
        self._running = True
        self._process = None

    def stop(self):
        self._running = False
        if self._process is not None:
            try:
                self._process.terminate()
            except OSError:
                pass

    def run(self):
        while self._running:
            try:
                self._process = subprocess.Popen(
                    ["pactl", "subscribe"],
                    stdout=subprocess.PIPE,
                    stderr=subprocess.DEVNULL,
                    text=True,
                    bufsize=1,
                )
                if self._process.stdout is not None:
                    for line in self._process.stdout:
                        if not self._running:
                            break
                        if any(
                            facility in line
                            for facility in ("on sink ", "on sink-input ", "on server ")
                        ):
                            self.topology_changed.emit()
            except OSError as error:
                LOGGER.warning("Audio event watcher reconnecting: %s", error)
            finally:
                if self._process is not None:
                    try:
                        self._process.terminate()
                    except OSError:
                        pass
                    self._process = None
            if self._running:
                self.msleep(2000)


class AsyncSignals(QObject):
    spatial_done = Signal()
    eq_done = Signal()
    mic_eq_done = Signal()
    streams_done = Signal()
    route_done = Signal(str)


class SpectrumWidget(QWidget):
    def __init__(self, title="LIVE SPECTRUM"):
        super().__init__()
        self.title = title
        self.setMinimumHeight(155)
        self.values = [-90.0] * 128
        self.eq_curve = None

    def set_spectrum(self, values):
        self.values = values
        self.update()

    def set_eq_curve(self, values):
        self.eq_curve = values
        self.update()

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        area = self.rect().adjusted(38, 24, -12, -22)
        painter.fillRect(self.rect(), QColor("#14171d"))

        painter.setPen(QColor("#778190"))
        painter.drawText(12, 17, self.title)

        # Decibel grid and labels.
        for db in (0, -20, -40, -60, -80):
            y = area.bottom() - (db + 80.0) / 80.0 * area.height()
            painter.setPen(QPen(QColor(42, 48, 58, 180), 1))
            painter.drawLine(area.left(), int(y), area.right(), int(y))
            painter.setPen(QColor("#667180"))
            painter.drawText(4, int(y + 4), f"{db}")

        # Logarithmic frequency grid.
        frequency_marks = (
            (20, "20"), (50, "50"), (100, "100"), (200, "200"),
            (500, "500"), (1000, "1k"), (2000, "2k"),
            (5000, "5k"), (10000, "10k"), (20000, "20k"),
        )
        log_min, log_span = np.log10(20.0), np.log10(20000.0) - np.log10(20.0)
        for frequency, label in frequency_marks:
            fraction = (np.log10(frequency) - log_min) / log_span
            x = area.left() + area.width() * fraction
            painter.setPen(QPen(QColor(38, 44, 54, 150), 1))
            painter.drawLine(int(x), area.top(), int(x), area.bottom())
            painter.setPen(QColor("#667180"))
            painter.drawText(int(x - 10), self.height() - 5, label)

        path = QPainterPath()
        points = []
        for index, value in enumerate(self.values):
            x = area.left() + area.width() * index / max(1, len(self.values) - 1)
            normalized = max(0.0, min(1.0, (value + 80.0) / 80.0))
            y = area.bottom() - normalized * area.height()
            points.append((x, y))
            if index == 0:
                path.moveTo(QPointF(x, y))
            else:
                path.lineTo(QPointF(x, y))

        # Translucent energy fill below the trace.
        fill_path = QPainterPath(path)
        fill_path.lineTo(QPointF(area.right(), area.bottom()))
        fill_path.lineTo(QPointF(area.left(), area.bottom()))
        fill_path.closeSubpath()
        fill = QLinearGradient(0, area.top(), 0, area.bottom())
        fill.setColorAt(0.0, QColor(52, 211, 153, 105))
        fill.setColorAt(0.55, QColor(37, 99, 235, 45))
        fill.setColorAt(1.0, QColor(17, 24, 39, 4))
        painter.fillPath(fill_path, QBrush(fill))

        # Broad translucent stroke creates a restrained neon glow.
        painter.setPen(QPen(QColor(52, 211, 153, 38), 8))
        painter.drawPath(path)
        trace = QLinearGradient(area.left(), 0, area.right(), 0)
        trace.setColorAt(0.0, QColor("#8b5cf6"))
        trace.setColorAt(0.45, QColor("#22d3ee"))
        trace.setColorAt(1.0, QColor("#34d399"))
        painter.setPen(QPen(QBrush(trace), 2.2))
        painter.drawPath(path)

        # Mark the strongest visible frequency bin.
        if points:
            peak_index = int(np.argmax(self.values))
            peak_x, peak_y = points[peak_index]
            painter.setPen(QPen(QColor(255, 255, 255, 75), 5))
            painter.drawPoint(QPointF(peak_x, peak_y))
            painter.setPen(QPen(QColor("#f8fafc"), 2))
            painter.drawPoint(QPointF(peak_x, peak_y))

        if self.eq_curve is not None:
            curve = QPainterPath()
            for index, value in enumerate(self.eq_curve):
                x = area.left() + area.width() * index / max(1, len(self.eq_curve) - 1)
                y = area.center().y() - max(-18.0, min(18.0, value)) * area.height() / 36.0
                if index == 0:
                    curve.moveTo(QPointF(x, y))
                else:
                    curve.lineTo(QPointF(x, y))
            painter.setPen(QPen(QColor(96, 165, 250, 42), 7))
            painter.drawPath(curve)
            painter.setPen(QPen(QColor("#60a5fa"), 2))
            painter.drawPath(curve)


class HeadsetWorker(QThread):
    chatmix_changed = Signal(int, int, float, str)
    connection_changed = Signal(bool, str)
    audio_status = Signal(str)

    def __init__(self):
        super().__init__()
        self._running = True
        self.mixer = PipeWireMixer()

    def stop(self):
        self._running = False

    def run(self):
        while self._running:
            try:
                self.mixer.ensure_buses()
                self.audio_status.emit("Game / Chat audio buses ready")
                break
            except RuntimeError as error:
                self.audio_status.emit(f"Waiting for audio: {error}")
                time.sleep(1)

        if not self._running:
            return

        saved = self.mixer.load_chatmix()

        if saved is not None:
            game, chat = saved
            try:
                self.mixer.set_chatmix(game, chat)
                position = (chat - game + 100) / 2
                state = ChatMixState(
                    game=game,
                    chat=chat,
                    position=position,
                )
                self.chatmix_changed.emit(
                    state.game,
                    state.chat,
                    state.position,
                    state.side,
                )
            except RuntimeError as error:
                LOGGER.warning("Could not restore ChatMix: %s", error)

        while self._running:
            try:
                with Nova7X() as headset:
                    radio_connected = headset.wireless_connected()
                    if radio_connected is False:
                        self.connection_changed.emit(
                            False,
                            "Arctis Nova 7X disconnected",
                        )
                        for _ in range(10):
                            if not self._running:
                                return
                            time.sleep(0.1)
                        continue

                    self.connection_changed.emit(
                        True,
                        "Arctis Nova 7X connected",
                    )

                    last_state = None
                    last_radio_check = time.monotonic()

                    while self._running:
                        try:
                            state = headset.read_chatmix(timeout=0.25)
                        except OSError as error:
                            LOGGER.info("Headset disconnected: %s", error)
                            break

                        if time.monotonic() - last_radio_check >= 1.0:
                            last_radio_check = time.monotonic()
                            radio_connected = headset.wireless_connected()
                            if radio_connected is False:
                                break

                        if state is None or state == last_state:
                            continue

                        last_state = state

                        try:
                            self.mixer.set_chatmix(
                                state.game,
                                state.chat,
                            )
                            self.mixer.save_chatmix(
                                state.game,
                                state.chat,
                            )
                        except RuntimeError as error:
                            LOGGER.warning("Could not apply ChatMix: %s", error)

                        self.chatmix_changed.emit(
                            state.game,
                            state.chat,
                            state.position,
                            state.side,
                        )

                if self._running:
                    self.connection_changed.emit(
                        False,
                        "Arctis Nova 7X disconnected",
                    )
                    for _ in range(10):
                        if not self._running:
                            return
                        time.sleep(0.1)

            except (
                FileNotFoundError,
                PermissionError,
                OSError,
            ):
                self.connection_changed.emit(
                    False,
                    "Arctis Nova 7X disconnected",
                )

                for _ in range(10):
                    if not self._running:
                        return
                    time.sleep(0.1)


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()

        # Set before creating timers or scheduling callbacks so every
        # asynchronous entry point can safely consult the shutdown state.
        self.closing = False
        self.quit_requested = False
        self.tray_notice_shown = False
        self._desktop_icons = None
        self._resolved_icons = {}
        self._stream_snapshot = None
        self._curve_frequencies = np.geomspace(20.0, 20000.0, 128)

        icon_candidates = (
            Path(__file__).parent / "assets" / "nova-sonar.png",
            Path(sys.prefix) / "share/icons/hicolor/512x512/apps/nova-sonar.png",
            Path.home() / ".local/share/icons/hicolor/512x512/apps/nova-sonar.png",
        )
        icon_path = next((path for path in icon_candidates if path.is_file()), None)
        self.app_icon = QIcon(str(icon_path)) if icon_path else QIcon.fromTheme("audio-card")
        self.setWindowIcon(self.app_icon)

        self.mixer = PipeWireMixer()

        self.eq = PipeWireEQ()
        self.eq_settings = self.eq.load_settings()
        self.eq_gains = [
            float(band["gain"])
            for band in self.eq_settings["mid_bands"]
        ]

        self.mic_eq = PipeWireMicEQ()
        self.mic_eq_state = self.mic_eq.load()

        self.spatial = PipeWireSpatial()
        self.spatial_enabled = self.spatial.load()

        # Keep potentially slow pactl queries and routing operations away
        # from Qt's event loop. One worker preserves operation ordering.
        self.audio_executor = ThreadPoolExecutor(
            max_workers=1,
            thread_name_prefix="nova-audio",
        )
        self.monitor_executor = ThreadPoolExecutor(
            max_workers=1,
            thread_name_prefix="nova-monitor",
        )
        self.async_signals = AsyncSignals(self)
        self.async_signals.spatial_done.connect(self._finish_spatial)
        self.async_signals.eq_done.connect(self._finish_eq)
        self.async_signals.mic_eq_done.connect(self._finish_mic_eq)
        self.async_signals.streams_done.connect(self._finish_stream_refresh)
        self.async_signals.route_done.connect(self._finish_route_stream)
        self.stream_future: Future | None = None
        self.route_future: Future | None = None
        self.eq_future: Future | None = None
        self.mic_eq_future: Future | None = None
        self.spatial_future: Future | None = None
        self.spatial_set_pending = False

        self.eq_apply_timer = QTimer(self)
        self.eq_apply_timer.setSingleShot(True)
        self.eq_apply_timer.timeout.connect(self.apply_eq)

        self.mic_eq_apply_timer = QTimer(self)
        self.mic_eq_apply_timer.setSingleShot(True)
        self.mic_eq_apply_timer.timeout.connect(self.apply_mic_eq)

        self.setWindowTitle("Nova Sonar · Audio Command Center")
        self.resize(1120, 760)

        self.title = QLabel("ARCTIS NOVA 7X")
        self.title.setObjectName("title")

        self.status = QLabel("Searching for headset...")
        self.status.setObjectName("status")

        self.audio_status = QLabel("Preparing audio...")
        self.audio_status.setObjectName("status")

        self.game_label = QLabel("GAME")
        self.chat_label = QLabel("CHAT")
        self.game_value = QLabel("---%")
        self.chat_value = QLabel("---%")

        self.mix_value = QLabel("Waiting for ChatMix...")
        self.mix_value.setObjectName("mixValue")

        self.slider = QSlider(Qt.Orientation.Horizontal)
        self.slider.setRange(0, 100)
        self.slider.setValue(50)
        self.slider.setAttribute(
            Qt.WidgetAttribute.WA_TransparentForMouseEvents
        )
        self.slider.setFocusPolicy(Qt.FocusPolicy.NoFocus)

        labels = QHBoxLayout()

        game_box = QVBoxLayout()
        game_box.addWidget(self.game_label)
        game_box.addWidget(self.game_value)

        chat_box = QVBoxLayout()
        chat_box.addWidget(
            self.chat_label,
            alignment=Qt.AlignmentFlag.AlignRight,
        )
        chat_box.addWidget(
            self.chat_value,
            alignment=Qt.AlignmentFlag.AlignRight,
        )

        labels.addLayout(game_box)
        labels.addStretch()
        labels.addLayout(chat_box)

        routing_title = QLabel("APPLICATION ROUTING")
        routing_title.setObjectName("sectionTitle")
        routing_subtitle = QLabel(
            "Send each running app to the Game or Chat channel"
        )
        routing_subtitle.setObjectName("status")

        self.stream_count = QLabel("SCANNING")
        self.stream_count.setObjectName("routingCount")

        self.refresh_button = QPushButton("Refresh applications")
        self.refresh_button.clicked.connect(self.refresh_streams)

        routing_header = QHBoxLayout()
        routing_heading = QVBoxLayout()
        routing_heading.setSpacing(2)
        routing_heading.addWidget(routing_title)
        routing_heading.addWidget(routing_subtitle)
        routing_header.addLayout(routing_heading)
        routing_header.addStretch()
        routing_header.addWidget(self.stream_count)
        routing_header.addWidget(self.refresh_button)

        bus_layout = QHBoxLayout()
        bus_layout.setSpacing(12)
        game_bus = QFrame()
        game_bus.setObjectName("gameBus")
        game_bus_layout = QVBoxLayout(game_bus)
        game_bus_layout.setContentsMargins(18, 13, 18, 13)
        game_bus_title = QLabel("GAME")
        game_bus_title.setObjectName("busTitle")
        game_bus_layout.addWidget(game_bus_title)
        game_bus_device = QLabel("Games · music · media")
        game_bus_device.setObjectName("busDevice")
        game_bus_layout.addWidget(game_bus_device)
        chat_bus = QFrame()
        chat_bus.setObjectName("chatBus")
        chat_bus_layout = QVBoxLayout(chat_bus)
        chat_bus_layout.setContentsMargins(18, 13, 18, 13)
        chat_bus_title = QLabel("CHAT")
        chat_bus_title.setObjectName("busTitle")
        chat_bus_layout.addWidget(chat_bus_title)
        chat_bus_device = QLabel("Discord · voice communication")
        chat_bus_device.setObjectName("busDevice")
        chat_bus_layout.addWidget(chat_bus_device)
        bus_layout.addWidget(game_bus, 1)
        bus_layout.addWidget(chat_bus, 1)

        self.routing_table = QTableWidget()
        self.routing_table.setColumnCount(3)
        self.routing_table.setHorizontalHeaderLabels(
            [
                "Application",
                "Playing through",
                "Output channel",
            ]
        )
        self.routing_table.verticalHeader().setVisible(False)
        self.routing_table.setSelectionMode(
            QTableWidget.SelectionMode.NoSelection
        )
        self.routing_table.setShowGrid(False)
        self.routing_table.setAlternatingRowColors(True)
        self.routing_table.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.routing_table.setMinimumHeight(220)
        self.routing_table.setIconSize(QSize(30, 30))
        self.routing_table.horizontalHeader().setSectionResizeMode(
            0,
            QHeaderView.ResizeMode.Stretch,
        )
        self.routing_table.horizontalHeader().setSectionResizeMode(
            1,
            QHeaderView.ResizeMode.ResizeToContents,
        )
        self.routing_table.horizontalHeader().setSectionResizeMode(
            2,
            QHeaderView.ResizeMode.ResizeToContents,
        )

        layout = QVBoxLayout()
        layout.setContentsMargins(40, 30, 40, 30)
        layout.setSpacing(14)
        layout.addWidget(self.title)
        layout.addWidget(self.status)
        layout.addWidget(self.audio_status)
        layout.addSpacing(15)
        layout.addLayout(labels)
        layout.addWidget(self.slider)
        layout.addWidget(
            self.mix_value,
            alignment=Qt.AlignmentFlag.AlignCenter,
        )
        layout.addSpacing(20)
        layout.addLayout(routing_header)
        layout.addLayout(bus_layout)
        layout.addWidget(self.routing_table)

        chatmix_page = QWidget()
        chatmix_page.setObjectName("chatmixPage")
        chatmix_page.setLayout(layout)

        self.tabs = QTabWidget()
        self.tabs.setObjectName("mainTabs")
        self.tabs.addTab(chatmix_page, "ChatMix")
        self.tabs.addTab(self.create_eq_page(), "Equalizer")
        self.tabs.addTab(self.create_mic_eq_page(), "Microphone EQ")
        self.tabs.addTab(self.create_spatial_page(), "Spatial Audio")
        self.tabs.currentChanged.connect(self.update_spectrum_workers)

        self.setCentralWidget(self.tabs)

        self.setStyleSheet(
            """
            QMainWindow {
                background: #080b12;
            }

            QTabWidget#mainTabs::pane {
                background: #0b1019;
                border: 1px solid #1e293b;
                border-radius: 12px;
                top: -1px;
            }

            QWidget#chatmixPage,
            QWidget#eqPage,
            QWidget#micEqPage,
            QWidget#spatialPage {
                background: qradialgradient(
                    cx: 0.5, cy: 0.15, radius: 1.0,
                    stop: 0 #121c2b, stop: 0.48 #0b111b, stop: 1 #080b12
                );
            }

            QTabBar::tab {
                background: #0d1420;
                color: #7f8da3;
                border: 1px solid #1e293b;
                border-bottom: 0;
                padding: 11px 22px;
                margin-right: 3px;
                border-top-left-radius: 8px;
                border-top-right-radius: 8px;
            }

            QTabBar::tab:selected {
                background: #152235;
                color: #67e8f9;
                border-top: 2px solid #34d399;
            }

            QTabBar::tab:hover:!selected {
                color: #d1d5db;
            }

            QLabel {
                color: #e8e8e8;
                font-size: 15px;
            }

            QLabel#title {
                color: #e6fbff;
                font-size: 25px;
                font-weight: 800;
            }

            QLabel#sectionTitle {
                color: #67e8f9;
                font-size: 16px;
                font-weight: 700;
            }

            QLabel#status {
                color: #8d96a5;
                font-size: 13px;
            }

            QLabel#mixValue {
                font-size: 18px;
                font-weight: 600;
            }

            QLabel#routingCount {
                color: #67e8f9;
                background: #112438;
                border: 1px solid #22506a;
                border-radius: 10px;
                padding: 4px 10px;
                font-size: 11px;
                font-weight: 800;
            }

            QFrame#gameBus, QFrame#chatBus {
                background: rgba(13, 24, 39, 235);
                border-radius: 11px;
            }

            QFrame#gameBus {
                border: 1px solid #176b77;
                border-left: 4px solid #22d3ee;
            }

            QFrame#chatBus {
                border: 1px solid #4b3a78;
                border-left: 4px solid #8b5cf6;
            }

            QLabel#busTitle {
                color: #f4fbff;
                font-size: 15px;
                font-weight: 900;
                letter-spacing: 2px;
            }

            QLabel#busDevice {
                color: #8190a6;
                font-size: 12px;
            }

            QTableWidget {
                background: rgba(13, 20, 32, 220);
                color: #e8e8e8;
                border: 1px solid #24344b;
                border-radius: 10px;
                gridline-color: #1d2a3d;
                alternate-background-color: rgba(17, 29, 45, 210);
                selection-background-color: transparent;
            }

            QTableWidget::item {
                padding: 10px 12px;
                border-bottom: 1px solid #18283a;
            }

            QHeaderView::section {
                background: #20242b;
                color: #b9c0ca;
                padding: 7px;
                border: 0;
                border-bottom: 1px solid #26384e;
                font-size: 11px;
                font-weight: 800;
            }

            QPushButton {
                background: #1a2b3f;
                color: #eeeeee;
                border: 1px solid #2c4963;
                border-radius: 7px;
                padding: 8px 14px;
            }

            QPushButton:hover {
                background: #21415a;
                border-color: #22d3ee;
            }

            QComboBox, QDoubleSpinBox {
                background: #111c2b;
                color: #eeeeee;
                border: 1px solid #294057;
                border-radius: 7px;
                padding: 6px 10px;
            }

            QComboBox QAbstractItemView {
                background: #20242b;
                color: #eeeeee;
            }

            QComboBox#routePicker {
                min-width: 120px;
                font-weight: 800;
                color: #d9fbff;
                border: 1px solid #27758a;
                padding: 7px 12px;
            }

            QSlider::groove:horizontal {
                background: qlineargradient(x1:0,y1:0,x2:1,y2:0,
                    stop:0 #22d3ee, stop:0.5 #26364c, stop:1 #8b5cf6);
                height: 7px;
                border-radius: 3px;
            }

            QSlider::handle:horizontal {
                background: #f8fafc;
                border: 3px solid #34d399;
                width: 17px;
                margin: -7px 0;
                border-radius: 10px;
            }

            QCheckBox {
                color: #e8e8e8;
                font-size: 16px;
                spacing: 12px;
            }

            QCheckBox::indicator {
                width: 42px;
                height: 22px;
            }

            QCheckBox::indicator:unchecked {
                background: #162131;
                border: 1px solid #3b526b;
                border-radius: 11px;
            }

            QCheckBox::indicator:checked {
                background: qlineargradient(x1:0,y1:0,x2:1,y2:0,
                    stop:0 #22d3ee, stop:1 #34d399);
                border: 1px solid #67e8f9;
                border-radius: 11px;
            }

            QFrame#eqPanel {
                background: rgba(10, 17, 28, 225);
                border: 1px solid #20354c;
                border-radius: 12px;
            }

            QFrame#eqBand {
                background: transparent;
                border: 0;
            }

            QLabel#eqValue {
                background: #17263a;
                color: #f3f4f6;
                border-radius: 6px;
                padding: 5px 4px;
                font-size: 13px;
                font-weight: 700;
            }

            QLabel#eqFrequency {
                color: #cbd1da;
                font-size: 12px;
                font-weight: 600;
            }

            QLabel#eqScale {
                color: #727b89;
                font-size: 11px;
            }

            QSlider#eqSlider::groove:vertical {
                background: #303641;
                width: 5px;
                border-radius: 2px;
            }

            QSlider#eqSlider::sub-page:vertical {
                background: #303641;
                border-radius: 2px;
            }

            QSlider#eqSlider::add-page:vertical {
                background: #303641;
                border-radius: 2px;
            }

            QSlider#eqSlider::handle:vertical {
                background: #f8fafc;
                border: 3px solid #22d3ee;
                height: 14px;
                width: 14px;
                margin: -7px;
                border-radius: 9px;
            }

            QSlider#eqSlider::handle:vertical:hover {
                background: #ffffff;
                border-color: #6ee7b7;
            }
            """
        )

        self.create_system_tray()

        self.worker = HeadsetWorker()
        self.worker.chatmix_changed.connect(self.update_chatmix)
        self.worker.connection_changed.connect(self.update_connection)
        self.worker.audio_status.connect(self.audio_status.setText)
        self.worker.start()

        self.spectrum_worker = SpectrumWorker()
        self.spectrum_worker.spectrum_ready.connect(
            self.spectrum.set_spectrum
        )
        self.spectrum_worker.start()

        self.mic_spectrum_worker = SpectrumWorker("nova_sonar_mic")
        self.mic_spectrum_worker.spectrum_ready.connect(
            self.mic_spectrum.set_spectrum
        )
        self.mic_spectrum_worker.start()
        self.update_spectrum_workers()

        self.topology_timer = QTimer(self)
        self.topology_timer.setSingleShot(True)
        self.topology_timer.timeout.connect(self._topology_changed)

        self.audio_event_worker = AudioEventWorker()
        self.audio_event_worker.topology_changed.connect(
            lambda: self.topology_timer.start(150)
        )
        self.audio_event_worker.start()

        QTimer.singleShot(1000, self.refresh_streams)
        QTimer.singleShot(500, self.apply_eq)
        QTimer.singleShot(650, self.apply_mic_eq)
        QTimer.singleShot(700, self.restore_spatial)

    def create_system_tray(self):
        self.tray_icon = None
        if not QSystemTrayIcon.isSystemTrayAvailable():
            LOGGER.warning("System tray is not available in this desktop session")
            return
        tray = QSystemTrayIcon(self.app_icon, self)
        tray.setToolTip("Nova Sonar · Audio Command Center")
        menu = QMenu(self)
        show_action = QAction("Show Nova Sonar", self)
        show_action.triggered.connect(self.show_from_tray)
        hide_action = QAction("Hide to tray", self)
        hide_action.triggered.connect(self.hide)
        quit_action = QAction("Quit", self)
        quit_action.triggered.connect(self.quit_from_tray)
        menu.addAction(show_action)
        menu.addAction(hide_action)
        menu.addSeparator()
        menu.addAction(quit_action)
        tray.setContextMenu(menu)
        tray.activated.connect(self.tray_activated)
        tray.show()
        self.tray_icon = tray

    def tray_activated(self, reason):
        if reason in (
            QSystemTrayIcon.ActivationReason.Trigger,
            QSystemTrayIcon.ActivationReason.DoubleClick,
        ):
            if self.isVisible() and self.isActiveWindow():
                self.hide()
            else:
                self.show_from_tray()

    def show_from_tray(self):
        self.show()
        if self.isMinimized():
            self.showNormal()
        self.raise_()
        self.activateWindow()

    def update_spectrum_workers(self, *_):
        if not hasattr(self, "spectrum_worker"):
            return
        visible = self.isVisible() and not self.isMinimized()
        current_tab = self.tabs.currentIndex()
        self.spectrum_worker.set_active(visible and current_tab == 1)
        self.mic_spectrum_worker.set_active(visible and current_tab == 2)

    def showEvent(self, event):
        super().showEvent(event)
        self.update_spectrum_workers()

    def hideEvent(self, event):
        if hasattr(self, "spectrum_worker"):
            self.spectrum_worker.set_active(False)
            self.mic_spectrum_worker.set_active(False)
        super().hideEvent(event)

    def changeEvent(self, event):
        super().changeEvent(event)
        if event.type() == QEvent.Type.WindowStateChange:
            self.update_spectrum_workers()

    def quit_from_tray(self):
        self.quit_requested = True
        self.close()

    def create_spatial_page(self):
        page = QWidget()
        page.setObjectName("spatialPage")
        layout = QVBoxLayout(page)

        layout.setContentsMargins(40, 30, 40, 30)
        layout.setSpacing(18)

        title = QLabel("SPATIAL AUDIO")
        title.setObjectName("title")
        layout.addWidget(title)

        description = QLabel(
            "7.1 binaural surround using SOFA HRTF processing"
        )
        description.setObjectName("status")
        layout.addWidget(description)

        layout.addSpacing(20)

        self.spatial_toggle = QCheckBox(
            "Enable Spatial Audio"
        )
        self.spatial_toggle.setChecked(
            self.spatial_enabled
        )
        self.spatial_toggle.toggled.connect(
            self.spatial_toggled
        )
        layout.addWidget(self.spatial_toggle)

        layout.addSpacing(20)

        mode_title = QLabel("MODE")
        mode_title.setObjectName("sectionTitle")
        layout.addWidget(mode_title)
        layout.addWidget(QLabel("7.1 Binaural"))

        hrtf_title = QLabel("HRTF")
        hrtf_title.setObjectName("sectionTitle")
        layout.addWidget(hrtf_title)
        layout.addWidget(
            QLabel("SOFA HRTF · 100% wet when enabled")
        )

        layout.addSpacing(20)

        self.spatial_status = QLabel()
        self.spatial_status.setObjectName("status")
        layout.addWidget(self.spatial_status)

        layout.addStretch()

        self.update_spatial_status()
        return page

    def _add_advanced_eq_controls(self, layout):
        def spin(minimum, maximum, value, suffix, step=1.0, decimals=1):
            control = QDoubleSpinBox()
            control.setRange(minimum, maximum)
            control.setValue(value)
            control.setSuffix(suffix)
            control.setSingleStep(step)
            control.setDecimals(decimals)
            return control

        bar = QHBoxLayout()
        self.eq_enabled = QCheckBox("EQ enabled")
        self.eq_enabled.setChecked(self.eq_settings["enabled"])
        self.eq_mode = QComboBox()
        self.eq_mode.addItems(["IIR", "Linear FIR", "Linear FFT", "Spectral"])
        self.eq_mode.setCurrentText(self.eq_settings["mode"])
        self.dynamic_eq = QCheckBox("Dynamic EQ")
        self.dynamic_eq.setChecked(self.eq_settings["dynamic_enabled"])
        self.show_eq_curve = QCheckBox("Show curve")
        self.show_eq_curve.setChecked(self.eq_settings["show_eq_curve"])
        self.dynamic_amount = spin(
            0, 12, self.eq_settings["dynamic_amount"], " dB dynamic", 0.5
        )
        self.eq_target = QComboBox()
        self.eq_target.addItems(["Linked M/S", "Mid", "Side"])
        self.output_gain = spin(-24, 24, self.eq_settings["output_gain"], " dB out", 0.5)
        self.mid_gain = spin(-24, 24, self.eq_settings["mid_gain"], " dB Mid", 0.5)
        self.side_gain = spin(-24, 24, self.eq_settings["side_gain"], " dB Side", 0.5)
        for widget in (self.eq_enabled, self.dynamic_eq, self.show_eq_curve):
            widget.toggled.connect(self.advanced_eq_changed)
        self.dynamic_amount.valueChanged.connect(self.advanced_eq_changed)
        self.eq_mode.currentTextChanged.connect(self.advanced_eq_changed)
        self.eq_target.currentTextChanged.connect(self.load_selected_band)
        for widget in (self.output_gain, self.mid_gain, self.side_gain):
            widget.valueChanged.connect(self.advanced_eq_changed)
        for widget in (
            self.eq_enabled, QLabel("Mode"), self.eq_mode, self.dynamic_eq,
            self.dynamic_amount, self.show_eq_curve,
            self.eq_target, self.mid_gain, self.side_gain, self.output_gain,
        ):
            bar.addWidget(widget)
        bar.insertStretch(4)
        layout.addLayout(bar)

        self.spectrum = SpectrumWidget()
        layout.addWidget(self.spectrum)

        editor = QHBoxLayout()
        self.band_selector = QComboBox()
        self.band_selector.addItems([f"Band {number}" for number in range(1, 11)])
        self.band_enabled = QCheckBox("On")
        self.band_type = QComboBox()
        self.band_type.addItems(PipeWireEQ.TYPE_IDS.keys())
        self.band_frequency = spin(10, 24000, 1000, " Hz", 1.0)
        self.band_frequency.setMinimumWidth(110)
        self.band_gain = spin(-24, 24, 0, " dB", 0.5)
        self.band_q = spin(0.1, 30, 1.41, " Q", 0.1, 2)
        self.band_slope = QComboBox()
        self.band_slope.addItems(["12 dB/oct", "24 dB/oct", "36 dB/oct", "48 dB/oct"])
        self.band_selector.currentIndexChanged.connect(self.load_selected_band)
        for widget in (self.band_enabled, self.band_type, self.band_frequency,
                       self.band_gain, self.band_q, self.band_slope):
            if isinstance(widget, QCheckBox):
                widget.toggled.connect(self.store_selected_band)
            elif isinstance(widget, QComboBox):
                widget.currentTextChanged.connect(self.store_selected_band)
            else:
                widget.valueChanged.connect(self.store_selected_band)
            editor.addWidget(widget)
        editor.insertWidget(0, self.band_selector)
        editor.addStretch()
        layout.addLayout(editor)

        filters = QHBoxLayout()
        hp, lp = self.eq_settings["high_pass"], self.eq_settings["low_pass"]
        self.high_pass_enabled = QCheckBox("High-pass")
        self.high_pass_enabled.setChecked(hp["enabled"])
        self.high_pass_frequency = spin(10, 1000, hp["frequency"], " Hz")
        self.high_pass_slope = QComboBox()
        self.high_pass_slope.addItems(["12 dB/oct", "24 dB/oct", "36 dB/oct", "48 dB/oct"])
        self.high_pass_slope.setCurrentIndex(hp["slope"] - 1)
        self.low_pass_enabled = QCheckBox("Low-pass")
        self.low_pass_enabled.setChecked(lp["enabled"])
        self.low_pass_frequency = spin(1000, 24000, lp["frequency"], " Hz")
        self.low_pass_slope = QComboBox()
        self.low_pass_slope.addItems(["12 dB/oct", "24 dB/oct", "36 dB/oct", "48 dB/oct"])
        self.low_pass_slope.setCurrentIndex(lp["slope"] - 1)
        for widget in (self.high_pass_enabled, self.high_pass_frequency,
                       self.high_pass_slope, self.low_pass_enabled,
                       self.low_pass_frequency, self.low_pass_slope):
            if isinstance(widget, QCheckBox):
                widget.toggled.connect(self.advanced_eq_changed)
            elif isinstance(widget, QComboBox):
                widget.currentTextChanged.connect(self.advanced_eq_changed)
            else:
                widget.valueChanged.connect(self.advanced_eq_changed)
            filters.addWidget(widget)
        filters.addStretch()
        layout.addLayout(filters)

    def spatial_toggled(
        self,
        enabled: bool,
    ):
        self.spatial_enabled = enabled

        self._submit_spatial("set")

    def update_spatial_status(self):
        if self.spatial_enabled:
            self.spatial_status.setText(
                "Spatial processing active · "
                "100% HRTF · no dry blend"
            )
        else:
            self.spatial_status.setText(
                "Spatial processing disabled · "
                "standard stereo downmix"
            )

    def restore_spatial(self):
        self._submit_spatial("set")

    def check_spatial_node(self):
        self._submit_spatial("sync")

    def _topology_changed(self):
        self.mixer.invalidate_topology()
        self.refresh_streams()
        self.check_spatial_node()

    def _submit_spatial(self, operation: str):
        if self.closing:
            return
        if self.spatial_future is not None:
            if operation == "set":
                self.spatial_set_pending = True
            return

        enabled = self.spatial_enabled
        if operation == "set":
            def work():
                self.spatial.set_enabled(enabled)
                return "applied"
        else:
            def work():
                return self.spatial.sync_if_recreated(enabled)

        executor = self.audio_executor if operation == "set" else self.monitor_executor
        self.spatial_future = executor.submit(work)
        self.spatial_future.add_done_callback(
            lambda _: self.async_signals.spatial_done.emit()
        )

    def _finish_spatial(self):
        future = self.spatial_future
        if future is None:
            return
        self.spatial_future = None
        try:
            state = future.result()
        except Exception as error:
            LOGGER.warning("Could not update spatial processing: %s", error)
            self.spatial_status.setText(f"Spatial error: {error}")
            state = "error"

        if state == "missing":
            self.spatial_status.setText(
                "Spatial processor reconnecting..."
            )

        elif state in {"applied", "reapplied"}:
            self.update_spatial_status()

        if self.spatial_set_pending and not self.closing:
            self.spatial_set_pending = False
            self._submit_spatial("set")

    def update_chatmix(
        self,
        game: int,
        chat: int,
        position: float,
        side: str,
    ):
        self.game_value.setText(f"{game}%")
        self.chat_value.setText(f"{chat}%")
        self.slider.setValue(round(position))

        if side == "CENTER":
            self.mix_value.setText("Balanced")
        elif side == "GAME":
            amount = round(100 - position * 2)
            self.mix_value.setText(
                f"{amount}% toward Game"
            )
        else:
            amount = round((position - 50) * 2)
            self.mix_value.setText(
                f"{amount}% toward Chat"
            )

    def update_connection(
        self,
        connected: bool,
        message: str,
    ):
        self.status.setText(message)

    def create_mic_eq_page(self):
        page = QWidget()
        page.setObjectName("micEqPage")
        layout = QVBoxLayout(page)
        layout.setContentsMargins(32, 26, 32, 24)
        layout.setSpacing(14)

        header = QHBoxLayout()
        title = QLabel("MICROPHONE EQUALIZER")
        title.setObjectName("title")
        self.mic_eq_preset = QComboBox()
        self.mic_eq_preset.addItems(PipeWireMicEQ.PRESETS.keys())
        preset = str(self.mic_eq_state.get("preset", "Natural"))
        self.mic_eq_preset.setCurrentText(
            preset if preset in PipeWireMicEQ.PRESETS else "Natural"
        )
        self.mic_eq_preset.currentTextChanged.connect(self.load_mic_eq_preset)
        reset = QPushButton("Reset to natural")
        reset.clicked.connect(lambda: self.load_mic_eq_preset("Natural"))
        header.addWidget(title)
        header.addStretch()
        header.addWidget(self.mic_eq_preset)
        header.addWidget(reset)
        layout.addLayout(header)

        description = QLabel(
            "Shape your headset microphone, then select “Nova Sonar Microphone” "
            "as the input in Discord, games, or recording apps."
        )
        description.setObjectName("status")
        description.setWordWrap(True)
        layout.addWidget(description)

        controls = QHBoxLayout()
        self.mic_eq_enabled = QCheckBox("Enable microphone EQ")
        self.mic_eq_enabled.setChecked(bool(self.mic_eq_state["enabled"]))
        self.mic_eq_enabled.toggled.connect(self.mic_eq_changed)
        controls.addWidget(self.mic_eq_enabled)
        self.mic_show_eq_curve = QCheckBox("Show curve")
        self.mic_show_eq_curve.setChecked(
            bool(self.mic_eq_state.get("show_eq_curve", False))
        )
        self.mic_show_eq_curve.toggled.connect(self.mic_eq_changed)
        controls.addWidget(self.mic_show_eq_curve)
        controls.addStretch()
        controls.addWidget(QLabel("Output trim"))
        self.mic_output_gain = QDoubleSpinBox()
        self.mic_output_gain.setRange(-12.0, 12.0)
        self.mic_output_gain.setSingleStep(0.5)
        self.mic_output_gain.setSuffix(" dB")
        self.mic_output_gain.setValue(float(self.mic_eq_state["output_gain"]))
        self.mic_output_gain.valueChanged.connect(self.mic_eq_changed)
        controls.addWidget(self.mic_output_gain)
        self.mic_high_pass = QCheckBox("Remove rumble")
        self.mic_high_pass.setChecked(bool(self.mic_eq_state["high_pass_enabled"]))
        self.mic_high_pass.toggled.connect(self.mic_eq_changed)
        controls.addWidget(self.mic_high_pass)
        self.mic_high_pass_frequency = QDoubleSpinBox()
        self.mic_high_pass_frequency.setRange(40.0, 200.0)
        self.mic_high_pass_frequency.setSingleStep(5.0)
        self.mic_high_pass_frequency.setSuffix(" Hz")
        self.mic_high_pass_frequency.setDecimals(0)
        self.mic_high_pass_frequency.setValue(float(self.mic_eq_state["high_pass_frequency"]))
        self.mic_high_pass_frequency.valueChanged.connect(self.mic_eq_changed)
        controls.addWidget(self.mic_high_pass_frequency)
        self.mic_high_pass_slope = QComboBox()
        self.mic_high_pass_slope.addItems(
            ["12 dB/oct", "24 dB/oct", "36 dB/oct", "48 dB/oct"]
        )
        self.mic_high_pass_slope.setCurrentIndex(
            max(0, min(3, int(self.mic_eq_state["high_pass_slope"]) - 1))
        )
        self.mic_high_pass_slope.currentIndexChanged.connect(self.mic_eq_changed)
        controls.addWidget(self.mic_high_pass_slope)
        layout.addLayout(controls)

        noise_controls = QHBoxLayout()
        self.mic_noise_suppression = QCheckBox("RNNoise suppression")
        self.mic_noise_suppression.setChecked(
            bool(self.mic_eq_state.get("noise_suppression_enabled", True))
        )
        self.mic_noise_suppression.toggled.connect(self.mic_eq_changed)
        noise_controls.addWidget(self.mic_noise_suppression)
        noise_controls.addWidget(QLabel("Voice threshold"))
        self.mic_noise_threshold = QDoubleSpinBox()
        self.mic_noise_threshold.setRange(0.0, 99.0)
        self.mic_noise_threshold.setDecimals(0)
        self.mic_noise_threshold.setSuffix(" %")
        self.mic_noise_threshold.setValue(
            float(self.mic_eq_state.get("noise_voice_threshold", 85.0))
        )
        self.mic_noise_threshold.setToolTip(
            "Higher values reject more non-voice sound but may clip quiet speech"
        )
        self.mic_noise_threshold.valueChanged.connect(self.mic_eq_changed)
        noise_controls.addWidget(self.mic_noise_threshold)
        noise_controls.addWidget(QLabel("Voice hold"))
        self.mic_noise_grace = QDoubleSpinBox()
        self.mic_noise_grace.setRange(0.0, 1000.0)
        self.mic_noise_grace.setSingleStep(25.0)
        self.mic_noise_grace.setDecimals(0)
        self.mic_noise_grace.setSuffix(" ms")
        self.mic_noise_grace.setValue(
            float(self.mic_eq_state.get("noise_grace_period", 200.0))
        )
        self.mic_noise_grace.setToolTip(
            "Keeps word endings open after RNNoise stops detecting speech"
        )
        self.mic_noise_grace.valueChanged.connect(self.mic_eq_changed)
        noise_controls.addWidget(self.mic_noise_grace)
        noise_hint = QLabel("Retroactive buffering off · no added look-ahead delay")
        noise_hint.setObjectName("status")
        noise_controls.addWidget(noise_hint)
        noise_controls.addStretch()
        layout.addLayout(noise_controls)

        self.mic_spectrum = SpectrumWidget("LIVE MICROPHONE SPECTRUM")
        layout.addWidget(self.mic_spectrum)

        editor = QHBoxLayout()
        self.mic_band_selector = QComboBox()
        self.mic_band_selector.addItems([f"Band {number}" for number in range(1, 7)])
        self.mic_band_enabled = QCheckBox("On")
        self.mic_band_type = QComboBox()
        self.mic_band_type.addItems(PipeWireMicEQ.TYPE_IDS.keys())
        self.mic_band_frequency = QDoubleSpinBox()
        self.mic_band_frequency.setRange(10.0, 24000.0)
        self.mic_band_frequency.setSuffix(" Hz")
        self.mic_band_frequency.setDecimals(0)
        self.mic_band_frequency.setMinimumWidth(105)
        self.mic_band_gain = QDoubleSpinBox()
        self.mic_band_gain.setRange(-12.0, 12.0)
        self.mic_band_gain.setSingleStep(0.5)
        self.mic_band_gain.setSuffix(" dB")
        self.mic_band_q = QDoubleSpinBox()
        self.mic_band_q.setRange(0.1, 30.0)
        self.mic_band_q.setSingleStep(0.1)
        self.mic_band_q.setDecimals(2)
        self.mic_band_q.setSuffix(" Q")
        self.mic_band_slope = QComboBox()
        self.mic_band_slope.addItems(
            ["12 dB/oct", "24 dB/oct", "36 dB/oct", "48 dB/oct"]
        )
        self.mic_band_selector.currentIndexChanged.connect(self.load_mic_band)
        for widget in (
            self.mic_band_enabled, self.mic_band_type, self.mic_band_frequency,
            self.mic_band_gain, self.mic_band_q, self.mic_band_slope,
        ):
            if isinstance(widget, QCheckBox):
                widget.toggled.connect(self.store_mic_band)
            elif isinstance(widget, QComboBox):
                widget.currentTextChanged.connect(self.store_mic_band)
            else:
                widget.valueChanged.connect(self.store_mic_band)
        for widget in (
            self.mic_band_selector, self.mic_band_enabled, self.mic_band_type,
            self.mic_band_frequency, self.mic_band_gain, self.mic_band_q,
            self.mic_band_slope,
        ):
            editor.addWidget(widget)
        editor.addStretch()
        layout.addLayout(editor)

        self.mic_eq_sliders = []
        self.mic_eq_values = []
        panel = QFrame()
        panel.setObjectName("eqPanel")
        bands = QHBoxLayout(panel)
        bands.setContentsMargins(18, 18, 18, 16)
        bands.setSpacing(8)
        for index, label in enumerate(PipeWireMicEQ.LABELS):
            column = QVBoxLayout()
            gain = float(self.mic_eq_state["gains"][index])
            value_label = QLabel(f"{gain:+.1f} dB")
            value_label.setObjectName("eqValue")
            value_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
            slider = EQSlider(Qt.Orientation.Vertical)
            slider.setObjectName("eqSlider")
            slider.setRange(-24, 24)
            slider.setValue(round(gain * 2))
            slider.setMinimumHeight(260)
            slider.setToolTip(f"{label} · double-click to reset")
            slider.valueChanged.connect(
                lambda value, i=index: self.mic_eq_slider_changed(i, value)
            )
            frequency = QLabel(label)
            frequency.setObjectName("eqFrequency")
            frequency.setAlignment(Qt.AlignmentFlag.AlignCenter)
            column.addWidget(value_label)
            column.addWidget(slider, alignment=Qt.AlignmentFlag.AlignCenter)
            column.addWidget(frequency)
            bands.addLayout(column, 1)
            self.mic_eq_sliders.append(slider)
            self.mic_eq_values.append(value_label)
        layout.addWidget(panel, 1)

        self.mic_eq_status = QLabel("Ready · physical microphone remains available")
        self.mic_eq_status.setObjectName("status")
        layout.addWidget(self.mic_eq_status)
        self.load_mic_band()
        self.refresh_mic_eq_curve()
        return page

    def load_mic_band(self, *_):
        index = self.mic_band_selector.currentIndex()
        if index < 0:
            return
        band = self.mic_eq_state["bands"][index]
        widgets = (
            self.mic_band_enabled, self.mic_band_type, self.mic_band_frequency,
            self.mic_band_gain, self.mic_band_q, self.mic_band_slope,
        )
        for widget in widgets:
            widget.blockSignals(True)
        self.mic_band_enabled.setChecked(bool(band["enabled"]))
        self.mic_band_type.setCurrentText(str(band["type"]))
        self.mic_band_frequency.setValue(float(band["frequency"]))
        self.mic_band_gain.setValue(float(band["gain"]))
        self.mic_band_q.setValue(float(band["q"]))
        self.mic_band_slope.setCurrentIndex(max(0, min(3, int(band["slope"]) - 1)))
        for widget in widgets:
            widget.blockSignals(False)

    def store_mic_band(self, *_):
        index = self.mic_band_selector.currentIndex()
        if index < 0:
            return
        gain = self.mic_band_gain.value()
        self.mic_eq_state["bands"][index].update({
            "enabled": self.mic_band_enabled.isChecked(),
            "type": self.mic_band_type.currentText(),
            "frequency": self.mic_band_frequency.value(),
            "gain": gain,
            "q": self.mic_band_q.value(),
            "slope": self.mic_band_slope.currentIndex() + 1,
        })
        self.mic_eq_state["gains"][index] = gain
        self.mic_eq_state["preset"] = "Custom"
        self.mic_eq_sliders[index].blockSignals(True)
        self.mic_eq_sliders[index].setValue(round(gain * 2))
        self.mic_eq_sliders[index].blockSignals(False)
        self.mic_eq_values[index].setText(f"{gain:+.1f} dB")
        self.refresh_mic_eq_curve()
        self.mic_eq_apply_timer.start(40)

    def refresh_mic_eq_curve(self):
        if not hasattr(self, "mic_spectrum"):
            return
        if (
            not self.mic_eq_state.get("enabled", True)
            or not self.mic_eq_state.get("show_eq_curve", False)
        ):
            self.mic_spectrum.set_eq_curve(None)
            return
        frequencies = self._curve_frequencies
        curve = np.zeros_like(frequencies)
        for band in self.mic_eq_state["bands"]:
            if not band["enabled"] or band["type"] == "Off":
                continue
            center = max(10.0, float(band["frequency"]))
            gain = float(band["gain"])
            distance = np.log2(frequencies / center)
            width = max(0.08, 1.0 / max(0.1, float(band["q"])))
            if band["type"] in {"Bell", "Notch", "Band-pass"}:
                curve += gain * np.exp(-0.5 * (distance / width) ** 2)
            elif band["type"] == "Low shelf":
                curve += gain / (1.0 + np.exp(5.0 * distance))
            elif band["type"] == "High shelf":
                curve += gain / (1.0 + np.exp(-5.0 * distance))
        curve += float(self.mic_eq_state["output_gain"])
        self.mic_spectrum.set_eq_curve(
            curve.tolist() if np.max(np.abs(curve)) >= 0.05 else None
        )

    def mic_eq_slider_changed(self, index: int, value: int):
        gain = value / 2.0
        self.mic_eq_state["gains"][index] = gain
        self.mic_eq_state["bands"][index]["gain"] = gain
        self.mic_eq_state["preset"] = "Custom"
        self.mic_eq_values[index].setText(f"{gain:+.1f} dB")
        if self.mic_band_selector.currentIndex() == index:
            self.mic_band_gain.blockSignals(True)
            self.mic_band_gain.setValue(gain)
            self.mic_band_gain.blockSignals(False)
        self.refresh_mic_eq_curve()
        self.mic_eq_apply_timer.start(40)

    def mic_eq_changed(self, *_):
        self.mic_eq_state.update({
            "enabled": self.mic_eq_enabled.isChecked(),
            "show_eq_curve": self.mic_show_eq_curve.isChecked(),
            "output_gain": self.mic_output_gain.value(),
            "noise_suppression_enabled": self.mic_noise_suppression.isChecked(),
            "noise_voice_threshold": self.mic_noise_threshold.value(),
            "noise_grace_period": self.mic_noise_grace.value(),
            "high_pass_enabled": self.mic_high_pass.isChecked(),
            "high_pass_frequency": self.mic_high_pass_frequency.value(),
            "high_pass_slope": self.mic_high_pass_slope.currentIndex() + 1,
        })
        self.refresh_mic_eq_curve()
        self.mic_eq_apply_timer.start(50)

    def load_mic_eq_preset(self, name: str):
        if name not in PipeWireMicEQ.PRESETS:
            return
        gains = list(PipeWireMicEQ.PRESETS[name])
        self.mic_eq_state.update(preset=name, gains=gains, enabled=True)
        defaults = PipeWireMicEQ.defaults()["bands"]
        self.mic_eq_state["bands"] = [dict(band) for band in defaults]
        for band, gain in zip(self.mic_eq_state["bands"], gains):
            band["gain"] = float(gain)
        self.mic_eq_enabled.blockSignals(True)
        self.mic_eq_enabled.setChecked(True)
        self.mic_eq_enabled.blockSignals(False)
        self.mic_eq_preset.blockSignals(True)
        self.mic_eq_preset.setCurrentText(name)
        self.mic_eq_preset.blockSignals(False)
        for index, gain in enumerate(gains):
            self.mic_eq_sliders[index].blockSignals(True)
            self.mic_eq_sliders[index].setValue(round(gain * 2))
            self.mic_eq_sliders[index].blockSignals(False)
            self.mic_eq_values[index].setText(f"{gain:+.1f} dB")
        self.load_mic_band()
        self.refresh_mic_eq_curve()
        self.apply_mic_eq()

    def apply_mic_eq(self):
        if self.closing:
            return
        if self.mic_eq_future is not None:
            self.mic_eq_apply_timer.start(50)
            return
        state = copy.deepcopy(self.mic_eq_state)

        def work():
            self.mic_eq.apply(state)
            self.mic_eq.save(state)
            return state

        self.mic_eq_future = self.audio_executor.submit(work)
        self.mic_eq_future.add_done_callback(
            lambda _: self.async_signals.mic_eq_done.emit()
        )

    def _finish_mic_eq(self):
        future = self.mic_eq_future
        if future is None:
            return
        self.mic_eq_future = None
        try:
            state = future.result()
            if state["enabled"]:
                self.mic_eq_status.setText(
                    "Live · use “Nova Sonar Microphone” in your app · settings saved"
                )
            else:
                self.mic_eq_status.setText("EQ bypassed · settings saved")
        except Exception as error:
            LOGGER.warning("Could not apply microphone EQ: %s", error)
            self.mic_eq_status.setText(f"Microphone EQ error: {error}")

    def create_eq_page(self):
        page = QWidget()
        page.setObjectName("eqPage")
        layout = QVBoxLayout(page)
        layout.setContentsMargins(32, 26, 32, 24)
        layout.setSpacing(14)

        header = QHBoxLayout()

        title = QLabel("10-BAND EQUALIZER")
        title.setObjectName("title")

        self.eq_preset = QComboBox()
        self.eq_preset.setMinimumWidth(135)
        self.eq_preset.addItems(
            ["Custom", *PipeWireEQ.PRESETS.keys()]
        )
        self.eq_preset.setCurrentText(
            self._matching_eq_preset()
        )
        self.eq_preset.currentTextChanged.connect(
            self.load_eq_preset
        )

        reset_button = QPushButton("Reset to flat")
        reset_button.clicked.connect(
            lambda: self.load_eq_preset("Flat")
        )

        header.addWidget(title)
        header.addStretch()
        header.addWidget(self.eq_preset)
        header.addWidget(reset_button)

        layout.addLayout(header)

        description = QLabel(
            ""
        )
        description.setObjectName("status")
        layout.addWidget(description)
        self._add_advanced_eq_controls(layout)

        self.eq_sliders = []
        self.eq_values = []

        panel = QFrame()
        panel.setObjectName("eqPanel")
        panel_layout = QHBoxLayout(panel)
        panel_layout.setContentsMargins(18, 18, 18, 16)
        panel_layout.setSpacing(4)

        scale = QVBoxLayout()
        scale.setContentsMargins(0, 36, 8, 25)
        for text_value in ("+12 dB", "0 dB", "−12 dB"):
            label = QLabel(text_value)
            label.setObjectName("eqScale")
            scale.addWidget(label)
            if text_value != "−12 dB":
                scale.addStretch()
        panel_layout.addLayout(scale)

        bands_layout = QHBoxLayout()
        bands_layout.setSpacing(5)

        for index, label in enumerate(
            PipeWireEQ.BANDS
        ):
            band = QFrame()
            band.setObjectName("eqBand")
            band.setMinimumWidth(70)
            column = QVBoxLayout(band)
            column.setContentsMargins(4, 0, 4, 0)
            column.setSpacing(9)

            value_label = QLabel(
                f"{self.eq_gains[index]:+.1f} dB"
            )
            value_label.setObjectName("eqValue")
            value_label.setAlignment(
                Qt.AlignmentFlag.AlignCenter
            )

            slider = EQSlider(
                Qt.Orientation.Vertical
            )
            slider.setObjectName("eqSlider")
            slider.setRange(-24, 24)
            slider.setValue(
                round(
                    self.eq_gains[index] * 2
                )
            )
            slider.setTickInterval(4)
            slider.setMinimumHeight(290)
            slider.setToolTip(
                f"{label} · double-click to reset"
            )

            slider.valueChanged.connect(
                lambda value,
                i=index:
                self.eq_slider_changed(
                    i,
                    value,
                )
            )

            freq_label = QLabel(label)
            freq_label.setObjectName("eqFrequency")
            freq_label.setAlignment(
                Qt.AlignmentFlag.AlignCenter
            )

            column.addWidget(value_label)
            column.addWidget(
                slider,
                alignment=Qt.AlignmentFlag.AlignCenter,
            )
            column.addWidget(freq_label)

            bands_layout.addWidget(band)

            self.eq_sliders.append(slider)
            self.eq_values.append(value_label)

        panel_layout.addLayout(bands_layout, 1)
        layout.addWidget(panel, 1)

        self.eq_status = QLabel(
            "Ready · automatic headroom protects against clipping"
        )
        self.eq_status.setObjectName("status")
        layout.addWidget(self.eq_status)

        self.load_selected_band()
        self.refresh_eq_curve()

        return page

    def _matching_eq_preset(self) -> str:
        for name, gains in PipeWireEQ.PRESETS.items():
            if all(
                abs(current - preset) < 0.01
                for current, preset in zip(self.eq_gains, gains)
            ):
                return name
        return "Custom"

    def _selected_band_lists(self):
        target = self.eq_target.currentText()
        if target == "Mid":
            return [self.eq_settings["mid_bands"]]
        if target == "Side":
            return [self.eq_settings["side_bands"]]
        return [self.eq_settings["mid_bands"], self.eq_settings["side_bands"]]

    def load_selected_band(self, *_):
        index = self.band_selector.currentIndex()
        if index < 0:
            return
        band = self._selected_band_lists()[0][index]
        widgets = (
            self.band_enabled, self.band_type, self.band_frequency,
            self.band_gain, self.band_q, self.band_slope,
        )
        for widget in widgets:
            widget.blockSignals(True)
        self.band_enabled.setChecked(bool(band["enabled"]))
        self.band_type.setCurrentText(str(band["type"]))
        self.band_frequency.setValue(float(band["frequency"]))
        self.band_gain.setValue(float(band["gain"]))
        self.band_q.setValue(float(band["q"]))
        self.band_slope.setCurrentIndex(max(0, min(3, int(band["slope"]) - 1)))
        for widget in widgets:
            widget.blockSignals(False)
        self.refresh_eq_curve()

    def refresh_eq_curve(self):
        if not hasattr(self, "spectrum") or not hasattr(self, "eq_target"):
            return
        if (
            not self.eq_enabled.isChecked()
            or not self.show_eq_curve.isChecked()
        ):
            self.spectrum.set_eq_curve(None)
            return
        bands = self._selected_band_lists()[0]
        frequencies = self._curve_frequencies
        curve = np.zeros_like(frequencies)
        for band in bands:
            if not band["enabled"] or band["type"] == "Off":
                continue
            center = max(10.0, float(band["frequency"]))
            gain = float(band["gain"])
            distance = np.log2(frequencies / center)
            width = max(0.08, 1.0 / max(0.1, float(band["q"])))
            if band["type"] in {"Bell", "Notch", "Band-pass"}:
                curve += gain * np.exp(-0.5 * (distance / width) ** 2)
            elif band["type"] == "Low shelf":
                curve += gain / (1.0 + np.exp(5.0 * distance))
            elif band["type"] == "High shelf":
                curve += gain / (1.0 + np.exp(-5.0 * distance))
        curve += float(self.output_gain.value())
        values = curve.tolist()
        self.spectrum.set_eq_curve(
            values if np.max(np.abs(curve)) >= 0.05 else None
        )

    def store_selected_band(self, *_):
        index = self.band_selector.currentIndex()
        if index < 0:
            return
        values = {
            "enabled": self.band_enabled.isChecked(),
            "type": self.band_type.currentText(),
            "frequency": self.band_frequency.value(),
            "gain": self.band_gain.value(),
            "q": self.band_q.value(),
            "slope": self.band_slope.currentIndex() + 1,
        }
        for bands in self._selected_band_lists():
            bands[index].update(values)
        self.eq_gains[index] = values["gain"]
        if index < len(self.eq_sliders):
            self.eq_sliders[index].blockSignals(True)
            self.eq_sliders[index].setValue(round(values["gain"] * 2))
            self.eq_sliders[index].blockSignals(False)
            self.eq_values[index].setText(f'{values["gain"]:+.1f} dB')
        self.eq_preset.blockSignals(True)
        self.eq_preset.setCurrentText("Custom")
        self.eq_preset.blockSignals(False)
        self.refresh_eq_curve()
        self.eq_apply_timer.start(60)

    def advanced_eq_changed(self, *_):
        self.eq_settings["enabled"] = self.eq_enabled.isChecked()
        self.eq_settings["show_eq_curve"] = self.show_eq_curve.isChecked()
        self.eq_settings["mode"] = self.eq_mode.currentText()
        self.eq_settings["dynamic_enabled"] = self.dynamic_eq.isChecked()
        self.eq_settings["dynamic_amount"] = self.dynamic_amount.value()
        self.eq_settings["output_gain"] = self.output_gain.value()
        self.eq_settings["mid_gain"] = self.mid_gain.value()
        self.eq_settings["side_gain"] = self.side_gain.value()
        self.eq_settings["high_pass"] = {
            "enabled": self.high_pass_enabled.isChecked(),
            "frequency": self.high_pass_frequency.value(),
            "slope": self.high_pass_slope.currentIndex() + 1,
        }
        self.eq_settings["low_pass"] = {
            "enabled": self.low_pass_enabled.isChecked(),
            "frequency": self.low_pass_frequency.value(),
            "slope": self.low_pass_slope.currentIndex() + 1,
        }
        self.refresh_eq_curve()
        if not self.eq_settings["enabled"]:
            self.eq_status.setText("EQ bypassed · original audio (B)")
        self.eq_apply_timer.start(60)

    def eq_slider_changed(
        self,
        index: int,
        value: int,
    ):
        gain = value / 2.0
        self.eq_gains[index] = gain
        for bands in self._selected_band_lists():
            bands[index]["gain"] = gain
        self.eq_values[index].setText(
            f"{gain:+.1f} dB"
        )
        if self.band_selector.currentIndex() == index:
            self.band_gain.blockSignals(True)
            self.band_gain.setValue(gain)
            self.band_gain.blockSignals(False)
        self.eq_preset.blockSignals(True)
        self.eq_preset.setCurrentText(
            self._matching_eq_preset()
        )
        self.eq_preset.blockSignals(False)
        self.eq_apply_timer.start(40)

    def apply_eq(self):
        if self.closing:
            return
        if self.eq_future is not None:
            # Coalesce rapid slider changes; the timer will retry shortly.
            self.eq_apply_timer.start(50)
            return

        settings = copy.deepcopy(self.eq_settings)

        def work():
            self.eq.apply_settings(settings)
            self.eq.save_settings(settings)
            return settings

        self.eq_future = self.audio_executor.submit(work)
        self.eq_future.add_done_callback(
            lambda _: self.async_signals.eq_done.emit()
        )

    def _finish_eq(self):
        future = self.eq_future
        if future is None:
            return
        self.eq_future = None
        try:
            applied_settings = future.result()

            boost = max(
                0.0,
                max(
                    band["gain"]
                    for side in ("mid_bands", "side_bands")
                    for band in applied_settings[side]
                ),
            )

            if applied_settings["enabled"]:
                self.eq_status.setText(
                    f'{applied_settings["mode"]} · peak boost {boost:.1f} dB · settings saved'
                )
            else:
                self.eq_status.setText(
                    "EQ bypassed · original audio (B) · settings saved"
                )

        except Exception as error:
            LOGGER.warning("Could not apply equalizer: %s", error)
            self.eq_status.setText(
                f"EQ error: {error}"
            )

    def load_eq_preset(
        self,
        name: str,
    ):
        if name not in PipeWireEQ.PRESETS:
            return

        gains = PipeWireEQ.PRESETS[name]
        self.eq_gains = list(gains)
        # A selected preset should always be audible. Previously a saved
        # A/B bypass state could make every preset appear to do nothing.
        self.eq_settings["enabled"] = True
        self.eq_enabled.blockSignals(True)
        self.eq_enabled.setChecked(True)
        self.eq_enabled.blockSignals(False)
        neutral = PipeWireEQ.default_settings()
        for side in ("mid_bands", "side_bands"):
            self.eq_settings[side] = [
                dict(band)
                for band in neutral[side]
            ]
            for band, gain in zip(self.eq_settings[side], gains):
                band["gain"] = float(gain)
        self.eq_preset.blockSignals(True)
        self.eq_preset.setCurrentText(name)
        self.eq_preset.blockSignals(False)

        for index, gain in enumerate(gains):
            slider = self.eq_sliders[index]
            slider.blockSignals(True)
            slider.setValue(round(gain * 2))
            slider.blockSignals(False)

            self.eq_values[index].setText(
                f"{gain:+.1f} dB"
            )

        self.load_selected_band()
        self.apply_eq()

    def refresh_streams(self):
        if self.closing:
            return
        if self.stream_future is not None:
            return

        self.stream_future = self.monitor_executor.submit(
            self.mixer.list_streams
        )
        self.stream_future.add_done_callback(
            lambda _: self.async_signals.streams_done.emit()
        )

    def _finish_stream_refresh(self):
        future = self.stream_future
        if future is None:
            return
        self.stream_future = None
        try:
            streams = future.result()
        except Exception as error:
            LOGGER.warning("Could not refresh application routing: %s", error)
            self.audio_status.setText(
                f"Routing error: {error}"
            )
            return

        snapshot = tuple(streams)
        if snapshot == self._stream_snapshot:
            return
        self._stream_snapshot = snapshot

        self.routing_table.setRowCount(
            len(streams)
        )
        self.stream_count.setText(
            f"{len(streams)} ACTIVE" if streams else "NO ACTIVE APPS"
        )

        for row, stream in enumerate(streams):
            self._add_stream_row(row, stream)

    def _add_stream_row(
        self,
        row: int,
        stream: AudioStream,
    ):
        app_item = QTableWidgetItem(f"●   {stream.name}")
        app_icon = self.application_icon(stream)
        if not app_icon.isNull():
            app_item.setIcon(app_icon)
        app_item.setFlags(
            app_item.flags()
            & ~Qt.ItemFlag.ItemIsEditable
        )

        if (
            stream.sink_name
            == self.mixer.GAME_SINK
        ):
            current = "Game"
        elif (
            stream.sink_name
            == self.mixer.CHAT_SINK
        ):
            current = "Chat"
        else:
            current = "Other"

        current_item = QTableWidgetItem(current)
        current_item.setFlags(
            current_item.flags()
            & ~Qt.ItemFlag.ItemIsEditable
        )
        current_item.setTextAlignment(
            Qt.AlignmentFlag.AlignCenter
        )
        if current == "Game":
            app_item.setForeground(QBrush(QColor("#67e8f9")))
            current_item.setForeground(QBrush(QColor("#22d3ee")))
            current_item.setText("GAME")
        elif current == "Chat":
            app_item.setForeground(QBrush(QColor("#c4b5fd")))
            current_item.setForeground(QBrush(QColor("#a78bfa")))
            current_item.setText("CHAT")
        else:
            app_item.setForeground(QBrush(QColor("#94a3b8")))
            current_item.setForeground(QBrush(QColor("#64748b")))
            current_item.setText("OTHER")

        combo = QComboBox()
        combo.setObjectName("routePicker")
        combo.addItems(["Game", "Chat"])

        if current == "Game":
            combo.setCurrentText("Game")
        elif current == "Chat":
            combo.setCurrentText("Chat")
        else:
            combo.setCurrentIndex(-1)

        combo.currentTextChanged.connect(
            lambda target,
            index=stream.index,
            binary=stream.binary,
            name=stream.name:
            self.route_stream(
                index,
                target,
                binary,
                name,
            )
        )

        self.routing_table.setItem(
            row,
            0,
            app_item,
        )
        self.routing_table.setItem(
            row,
            1,
            current_item,
        )
        self.routing_table.setCellWidget(
            row,
            2,
            combo,
        )
        self.routing_table.setRowHeight(row, 54)

    @staticmethod
    def _desktop_roots():
        home = Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local/share"))
        roots = [home]
        roots.extend(
            Path(entry)
            for entry in os.environ.get(
                "XDG_DATA_DIRS", "/usr/local/share:/usr/share"
            ).split(":")
            if entry
        )
        return list(dict.fromkeys(roots))

    def _load_desktop_icon_index(self):
        if self._desktop_icons is not None:
            return self._desktop_icons
        index = {}
        for root in self._desktop_roots():
            directory = root / "applications"
            if not directory.is_dir():
                continue
            for desktop_file in directory.glob("*.desktop"):
                try:
                    lines = desktop_file.read_text(
                        encoding="utf-8", errors="replace"
                    ).splitlines()
                except OSError:
                    continue
                values = {}
                for line in lines:
                    if "=" not in line or line.startswith("Name["):
                        continue
                    key, value = line.split("=", 1)
                    if key in {"Name", "Icon", "Exec", "StartupWMClass"}:
                        values.setdefault(key, value.strip())
                icon = values.get("Icon")
                if not icon:
                    continue
                keys = {
                    desktop_file.stem,
                    values.get("Name", ""),
                    values.get("StartupWMClass", ""),
                }
                executable = values.get("Exec", "").split()
                if executable:
                    keys.add(Path(executable[0]).name)
                # Flatpak desktop commands contain their application ID.
                keys.update(token for token in executable if "." in token)
                for key in keys:
                    if key:
                        index.setdefault(key.lower(), icon)
        self._desktop_icons = index
        return index

    def _icon_from_value(self, value: str) -> QIcon:
        if not value:
            return QIcon()
        if value in self._resolved_icons:
            return self._resolved_icons[value]
        direct = Path(value)
        if direct.is_absolute() and direct.is_file():
            icon = QIcon(str(direct))
            self._resolved_icons[value] = icon
            return icon
        themed = QIcon.fromTheme(value)
        if not themed.isNull():
            self._resolved_icons[value] = themed
            return themed
        filenames = [f"{value}.{extension}" for extension in ("png", "svg", "xpm")]
        for root in self._desktop_roots():
            icon_root = root / "icons" / "hicolor"
            for filename in filenames:
                matches = list(icon_root.glob(f"*/apps/{filename}"))
                if matches:
                    icon = QIcon(str(matches[-1]))
                    self._resolved_icons[value] = icon
                    return icon
            for filename in filenames:
                pixmap = root / "pixmaps" / filename
                if pixmap.is_file():
                    icon = QIcon(str(pixmap))
                    self._resolved_icons[value] = icon
                    return icon
        missing = QIcon()
        self._resolved_icons[value] = missing
        return missing

    def application_icon(self, stream: AudioStream) -> QIcon:
        candidates = [
            stream.icon_name,
            stream.binary,
            Path(stream.binary).name if stream.binary else "",
            Path(stream.binary).name.removesuffix("-bin") if stream.binary else "",
            stream.name.lower().replace(" ", "-"),
        ]
        desktop_icons = self._load_desktop_icon_index()
        for candidate in candidates:
            candidate = str(candidate or "").removesuffix(".desktop")
            if not candidate:
                continue
            icon = self._icon_from_value(candidate)
            if not icon.isNull():
                return icon
            desktop_icon = desktop_icons.get(candidate.lower())
            icon = self._icon_from_value(desktop_icon or "")
            if not icon.isNull():
                return icon
        fallback = self._icon_from_value("application-x-executable")
        return fallback if not fallback.isNull() else self.app_icon

    def route_stream(
        self,
        index: int,
        target: str,
        binary: str = "",
        name: str = "",
    ):
        if not target:
            return
        if self.closing:
            return

        if self.route_future is not None:
            self.audio_status.setText("Another routing change is in progress")
            return

        self.route_future = self.audio_executor.submit(
            self.mixer.route_stream,
            index,
            target,
            binary,
            name,
        )
        self.route_future.add_done_callback(
            lambda _, route_target=target: self.async_signals.route_done.emit(
                route_target
            )
        )

    def _finish_route_stream(self, target: str):
        future = self.route_future
        if future is None:
            return
        self.route_future = None
        try:
            future.result()
        except Exception as error:
            LOGGER.warning("Could not route application: %s", error)
            self.audio_status.setText(
                f"Routing error: {error}"
            )
            return

        self.audio_status.setText(
            f"Routed application to {target}"
        )

        QTimer.singleShot(
            200,
            self.refresh_streams,
        )

    def closeEvent(
        self,
        event,
    ):
        if (
            not self.quit_requested
            and self.tray_icon is not None
            and self.tray_icon.isVisible()
        ):
            event.ignore()
            self.hide()
            if not self.tray_notice_shown:
                self.tray_icon.showMessage(
                    "Nova Sonar is still running",
                    "Audio controls remain active in the system tray.",
                    QSystemTrayIcon.MessageIcon.Information,
                    2500,
                )
                self.tray_notice_shown = True
            return
        if not self.closing:
            self.closing = True
            self.topology_timer.stop()
            self.eq_apply_timer.stop()
            self.mic_eq_apply_timer.stop()
            self.worker.stop()
            self.audio_event_worker.stop()
            self.spectrum_worker.stop()
            self.mic_spectrum_worker.stop()
        if not self.worker.wait(6000):
            LOGGER.error("Headset worker did not stop within six seconds")
            self.audio_status.setText("Waiting for audio worker to stop...")
            event.ignore()
            QTimer.singleShot(500, self.close)
            return
        self.spectrum_worker.wait(2000)
        self.mic_spectrum_worker.wait(2000)
        self.audio_event_worker.wait(2000)
        self.audio_executor.shutdown(
            wait=False,
            cancel_futures=True,
        )
        self.monitor_executor.shutdown(
            wait=False,
            cancel_futures=True,
        )
        if self.tray_icon is not None:
            self.tray_icon.hide()
        event.accept()
        if self.quit_requested:
            QTimer.singleShot(0, QApplication.instance().quit)


def main():
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    app = QApplication(sys.argv)
    window = MainWindow()
    app.setQuitOnLastWindowClosed(window.tray_icon is None)
    window.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()

import sys
import json
import os
from datetime import datetime
import pytz
from timezonefinder import TimezoneFinder
import folium

from PyQt6.QtCore import QTime, Qt, QUrl, pyqtSignal, QObject, pyqtSlot
from PyQt6.QtGui import QAction, QIcon
from PyQt6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
    QComboBox, QLineEdit, QLabel, QPushButton, QTimeEdit,
    QMessageBox, QDialog, QTableWidget, QTableWidgetItem,
    QHeaderView, QCheckBox, QFormLayout, QDialogButtonBox
)
from PyQt6.QtWebEngineWidgets import QWebEngineView

# ----------------- Data & Helpers -----------------

KNOWN_LOCATIONS_FILE = "known_locations.json"

DEFAULT_KNOWN_LOCATIONS = {
    "New York, USA": "America/New_York",
    "London, UK": "Europe/London",
    "Dublin, Ireland": "Europe/Dublin",
    "Delhi, India": "Asia/Kolkata",
    "Tokyo, Japan": "Asia/Tokyo",
    "Sydney, Australia": "Australia/Sydney",
    "Los Angeles, USA": "America/Los_Angeles",
    "Chicago, USA": "America/Chicago",
    "Berlin, Germany": "Europe/Berlin",
    "Paris, France": "Europe/Paris"
}


def load_known_locations():
    if os.path.exists(KNOWN_LOCATIONS_FILE):
        try:
            with open(KNOWN_LOCATIONS_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, dict):
                return data
        except Exception:
            pass
    # fallback
    return DEFAULT_KNOWN_LOCATIONS.copy()


def save_known_locations(locations):
    try:
        with open(KNOWN_LOCATIONS_FILE, "w", encoding="utf-8") as f:
            json.dump(locations, f, indent=2, ensure_ascii=False)
    except Exception:
        # Non-fatal; just ignore save errors
        pass


def find_timezone_for_location_text(text, known_locations):
    """
    Try to match a user-entered location string to a known location key.
    Simple case-insensitive exact match.
    """
    text_norm = text.strip().lower()
    for name, tz in known_locations.items():
        if name.lower() == text_norm:
            return tz, name
    return None, None


def format_time(dt, use_24h):
    if use_24h:
        return dt.strftime("%H:%M")
    else:
        return dt.strftime("%I:%M %p")


def parse_time_string(time_str, use_24h):
    """
    Parse a time string into (hour, minute).
    Accepts:
      - 24h: "HH:MM"
      - 12h: "HH:MM AM/PM" or "HH:MMam"/"HH:MMpm"
    """
    s = time_str.strip()
    if not s:
        return None

    try:
        if use_24h:
            dt = datetime.strptime(s, "%H:%M")
        else:
            # Try a few common 12h formats
            for fmt in ("%I:%M %p", "%I:%M%p", "%I:%M %P", "%I:%M%P"):
                try:
                    dt = datetime.strptime(s, fmt)
                    break
                except ValueError:
                    dt = None
            if dt is None:
                return None
        return dt.hour, dt.minute
    except ValueError:
        return None


# ----------------- Map Dialog -----------------

class MapDialog(QDialog):
    locationSelected = pyqtSignal(float, float, str)  # lat, lon, timezone

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Select Location on Map")
        self.resize(800, 600)

        layout = QVBoxLayout(self)
        self.webview = QWebEngineView(self)
        layout.addWidget(self.webview)

        self.tf = TimezoneFinder()

        # Generate map HTML with click handler
        self._load_map()

    def _load_map(self):
        # Center map roughly on Europe/Africa
        m = folium.Map(location=[20, 0], zoom_start=2)

        # Add JS to handle clicks
        click_js = """
            function onMapClick(e) {
                var lat = e.latlng.lat;
                var lng = e.latlng.lng;
                // Send coordinates back via Qt bridge
                if (window.pyObj) {
                    window.pyObj.handleMapClick(lat, lng);
                } else {
                    alert("pyObj not available");
                }
            }
            map.on('click', onMapClick);
        """

        m.get_root().script.add_child(folium.Element("""
            <script>
            var map = null;
            </script>
        """))

        # We need to ensure 'map' variable is the folium map instance.
        # Folium already creates 'map_xxx' variable; we can hook after creation.
        # Easiest: add a script that assigns the last created map to 'map'.
        m.get_root().script.add_child(folium.Element("""
            <script>
            setTimeout(function() {
                for (var key in window) {
                    if (key.startsWith("map_")) {
                        window.map = window[key];
                    }
                }
            }, 500);
            </script>
        """))

        m.get_root().script.add_child(folium.Element(f"""
            <script>
            {click_js}
            </script>
        """))

        data = m.get_root().render().encode("utf-8")
        html = data.decode("utf-8")

        # Load HTML into webview
        self.webview.setHtml(html)

        # Expose Python object to JS
        self.webview.page().setWebChannel(None)  # ensure clean
        # Use a simple bridge via runJavaScript injection
        # We'll inject a small JS object that calls back via QWebEngineView's runJavaScript.
        # But QWebEngine doesn't support direct Python callbacks from JS without QWebChannel.
        # So we use QWebChannel properly:

        from PyQt6.QtWebChannel import QWebChannel

        self.channel = QWebChannel(self.webview.page())
        self.bridge = MapBridge(self)
        self.channel.registerObject("pyObj", self.bridge)
        self.webview.page().setWebChannel(self.channel)

        # Inject webchannel script
        self.webview.page().runJavaScript("""
            new QWebChannel(qt.webChannelTransport, function(channel) {
                window.pyObj = channel.objects.pyObj;
            });
        """)

    def handle_coordinates(self, lat, lon):
        tzname = self.tf.timezone_at(lat=lat, lng=lon)
        if not tzname:
            QMessageBox.warning(self, "Time Zone Not Found",
                                "Could not determine a time zone for that location.")
            return
        self.locationSelected.emit(lat, lon, tzname)
        self.accept()


class MapBridge(QObject):
    # Declare the signal as a class attribute
    mapClicked = pyqtSignal(float, float)

    def __init__(self, dialog):
        super().__init__()
        self.dialog = dialog
        # Connect the signal to a slot (method)
        self.mapClicked.connect(self.on_map_clicked)

    # This is the slot that will be called when JS emits the signal
    def on_map_clicked(self, lat, lng):
        self.dialog.handle_coordinates(lat, lng)

    # This is the method JS will call directly
    @pyqtSlot(float, float)
    def handleMapClick(self, lat, lng):
        # Emit the Qt signal so the rest of the app can react
        self.mapClicked.emit(lat, lng)


# ----------------- Known Locations Dialog -----------------

class KnownLocationsDialog(QDialog):
    def __init__(self, known_locations, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Known Locations")
        self.resize(500, 400)

        self.known_locations = known_locations  # reference to main dict

        layout = QVBoxLayout(self)

        self.table = QTableWidget(self)
        self.table.setColumnCount(2)
        self.table.setHorizontalHeaderLabels(["Location", "Time Zone"])
        self.table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        self.table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        layout.addWidget(self.table)

        btn_layout = QHBoxLayout()
        self.add_btn = QPushButton("Add Location")
        self.remove_btn = QPushButton("Remove Selected")
        btn_layout.addWidget(self.add_btn)
        btn_layout.addWidget(self.remove_btn)
        layout.addLayout(btn_layout)

        self.button_box = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        layout.addWidget(self.button_box)

        self.button_box.rejected.connect(self.reject)
        self.add_btn.clicked.connect(self.add_location)
        self.remove_btn.clicked.connect(self.remove_selected)

        self.load_table()

    def load_table(self):
        self.table.setRowCount(0)
        for row, (loc, tz) in enumerate(sorted(self.known_locations.items(), key=lambda x: x[0].lower())):
            self.table.insertRow(row)
            self.table.setItem(row, 0, QTableWidgetItem(loc))
            self.table.setItem(row, 1, QTableWidgetItem(tz))

    def add_location(self):
        row = self.table.rowCount()
        self.table.insertRow(row)
        self.table.setItem(row, 0, QTableWidgetItem("New Location"))
        self.table.setItem(row, 1, QTableWidgetItem("UTC"))

    def remove_selected(self):
        rows = sorted({idx.row() for idx in self.table.selectedIndexes()}, reverse=True)
        for r in rows:
            self.table.removeRow(r)

    def accept(self):
        # Save back to dict
        new_dict = {}
        for row in range(self.table.rowCount()):
            loc_item = self.table.item(row, 0)
            tz_item = self.table.item(row, 1)
            if not loc_item or not tz_item:
                continue
            loc = loc_item.text().strip()
            tz = tz_item.text().strip()
            if loc and tz:
                new_dict[loc] = tz
        self.known_locations.clear()
        self.known_locations.update(new_dict)
        save_known_locations(self.known_locations)
        super().accept()


# ----------------- Settings Dialog -----------------

class SettingsDialog(QDialog):
    def __init__(self, use_24h, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Settings")
        self.resize(300, 150)

        self.use_24h = use_24h

        layout = QVBoxLayout(self)

        form = QFormLayout()
        self.time_format_checkbox = QCheckBox("Use 24-hour time")
        self.time_format_checkbox.setChecked(self.use_24h)
        form.addRow(self.time_format_checkbox)
        layout.addLayout(form)

        self.button_box = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        layout.addWidget(self.button_box)

        self.button_box.accepted.connect(self.accept)
        self.button_box.rejected.connect(self.reject)

    def accept(self):
        self.use_24h = self.time_format_checkbox.isChecked()
        super().accept()


# ----------------- Time Column Widget -----------------

class TimeColumn(QWidget):
    timeChanged = pyqtSignal(int, int, str)  # hour, minute, timezone
    timezoneChanged = pyqtSignal(str)        # timezone string

    def __init__(self, title, known_locations, use_24h=True, parent=None):
        super().__init__(parent)
        self.known_locations = known_locations
        self.use_24h = use_24h

        self.current_tz = "UTC"
        self.suppress_signals = False

        main_layout = QVBoxLayout(self)
        main_layout.setSpacing(8)

        title_label = QLabel(title)
        title_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        title_label.setStyleSheet("font-weight: bold; font-size: 14px;")
        main_layout.addWidget(title_label)

        # Time zone row: dropdown + location text + globe button
        tz_row = QHBoxLayout()

        self.tz_combo = QComboBox()
        self.tz_combo.setEditable(False)
        self.tz_combo.addItems(sorted(pytz.all_timezones))
        self.tz_combo.setCurrentText(self.current_tz)
        tz_row.addWidget(self.tz_combo)

        self.location_edit = QLineEdit()
        self.location_edit.setPlaceholderText("Enter location (e.g. Delhi, India)")
        tz_row.addWidget(self.location_edit)

        self.globe_btn = QPushButton("🌐")
        self.globe_btn.setFixedWidth(32)
        tz_row.addWidget(self.globe_btn)

        main_layout.addLayout(tz_row)

        # Time input
        self.time_edit = QTimeEdit()
        self.time_edit.setDisplayFormat("HH:mm" if self.use_24h else "hh:mm AP")
        self.time_edit.setTimeRange(self.time_edit.minimumTime(), self.time_edit.maximumTime())
        self.time_edit.setKeyboardTracking(False)
        main_layout.addWidget(self.time_edit)

        # Connections
        self.time_edit.timeChanged.connect(self._on_time_changed)
        self.tz_combo.currentTextChanged.connect(self._on_tz_combo_changed)
        self.location_edit.editingFinished.connect(self._on_location_entered)

    def set_use_24h(self, use_24h):
        self.use_24h = use_24h
        self.time_edit.setDisplayFormat("HH:mm" if self.use_24h else "hh:mm AP")

    def _on_time_changed(self, qtime):
        if self.suppress_signals:
            return
        hour = qtime.hour()
        minute = qtime.minute()
        self.timeChanged.emit(hour, minute, self.current_tz)

    def _on_tz_combo_changed(self, tzname):
        if self.suppress_signals:
            return
        old_tz = self.current_tz
        self.current_tz = tzname
        # Keep local time the same; just emit timezoneChanged
        self.timezoneChanged.emit(self.current_tz)

    def _on_location_entered(self):
        text = self.location_edit.text().strip()
        if not text:
            return
        tz, canonical_name = find_timezone_for_location_text(text, self.known_locations)
        if not tz:
            QMessageBox.warning(self, "Unknown Location",
                                f"Location '{text}' is not recognized.\n"
                                "Use Settings → Known Locations to add it.")
            return
        # Update combo and stored tz
        self.suppress_signals = True
        try:
            self.current_tz = tz
            self.tz_combo.setCurrentText(tz)
            self.location_edit.setText(canonical_name)
        finally:
            self.suppress_signals = False
        self.timezoneChanged.emit(self.current_tz)

    def set_timezone(self, tzname, location_name=None):
        self.suppress_signals = True
        try:
            self.current_tz = tzname
            self.tz_combo.setCurrentText(tzname)
            if location_name:
                self.location_edit.setText(location_name)
        finally:
            self.suppress_signals = False
    
    def get_timezone(self):
        return self.current_tz

    def set_time(self, hour, minute):
        """Set the displayed time without triggering sync signals."""
        self.suppress_signals = True
        try:
            t = QTime(hour, minute, 0)
            self.time_edit.setTime(t)
        finally:
            self.suppress_signals = False

#    Doesn't work b/c setTime returns a bool, throws an error.
#    def set_time(self, hour, minute):
#        self.suppress_signals = True
#        try:
#            self.time_edit.setTime(self.time_edit.time().setHMS(hour, minute, 0))
#        finally:
#            self.suppress_signals = False
#
#    def get_time(self):
#        t = self.time_edit.time()
#        return t.hour(), t.minute()
#
#    def get_timezone(self):
#        return self.current_tz
#

    def get_time(self):
        """Return (hour, minute) from this column."""
        t = self.time_edit.time()
        return t.hour(), t.minute()

# ----------------- Main Window -----------------

class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("Time Zone Converter")
        self.resize(800, 300)

        self.known_locations = load_known_locations()
        self.use_24h = True

        central = QWidget()
        self.setCentralWidget(central)

        main_layout = QVBoxLayout(central)

        # Top bar with settings (gear) and known locations
        top_bar = QHBoxLayout()
        top_bar.addStretch()

        self.known_btn = QPushButton("Known Locations")
        top_bar.addWidget(self.known_btn)

        self.settings_btn = QPushButton("⚙")
        self.settings_btn.setFixedWidth(32)
        top_bar.addWidget(self.settings_btn)

        main_layout.addLayout(top_bar)

        # Two columns
        cols_layout = QHBoxLayout()
        self.col_left = TimeColumn("Time A", self.known_locations, use_24h=self.use_24h)
        self.col_right = TimeColumn("Time B", self.known_locations, use_24h=self.use_24h)

        cols_layout.addWidget(self.col_left)
        cols_layout.addWidget(self.col_right)
        main_layout.addLayout(cols_layout)

        # Initial time: now in UTC for left, convert to right
        now_utc = datetime.now(pytz.utc)
        self.col_left.set_timezone("UTC", "UTC")
        self.col_left.set_time(now_utc.hour, now_utc.minute)
        self._sync_from_left()

        # Connections
        self.col_left.timeChanged.connect(self._on_left_time_changed)
        self.col_right.timeChanged.connect(self._on_right_time_changed)

        self.col_left.timezoneChanged.connect(self._on_left_tz_changed)
        self.col_right.timezoneChanged.connect(self._on_right_tz_changed)

        self.settings_btn.clicked.connect(self.open_settings)
        self.known_btn.clicked.connect(self.open_known_locations)

        # Globe buttons -> map dialog
        self.col_left.globe_btn.clicked.connect(lambda: self.open_map_for_column(self.col_left))
        self.col_right.globe_btn.clicked.connect(lambda: self.open_map_for_column(self.col_right))

    # ----- Time sync logic -----

    def _convert_time(self, hour, minute, from_tz, to_tz):
        try:
            from_zone = pytz.timezone(from_tz)
            to_zone = pytz.timezone(to_tz)
        except Exception:
            return hour, minute

        today = datetime.now(pytz.utc).date()
        naive = datetime(today.year, today.month, today.day, hour, minute)
        localized = from_zone.localize(naive)
        converted = localized.astimezone(to_zone)
        return converted.hour, converted.minute

    def _on_left_time_changed(self, hour, minute, tzname):
        self._sync_from_left()

    def _on_right_time_changed(self, hour, minute, tzname):
        self._sync_from_right()

    def _sync_from_left(self):
        lh, lm = self.col_left.get_time()
        ltz = self.col_left.get_timezone()
        rtz = self.col_right.get_timezone()
        rh, rm = self._convert_time(lh, lm, ltz, rtz)
        self.col_right.set_time(rh, rm)

    def _sync_from_right(self):
        rh, rm = self.col_right.get_time()
        rtz = self.col_right.get_timezone()
        ltz = self.col_left.get_timezone()
        lh, lm = self._convert_time(rh, rm, rtz, ltz)
        self.col_left.set_time(lh, lm)

    # ----- Time zone change logic -----

    def _on_left_tz_changed(self, tzname):
        # Keep left local time; just recompute right
        self._sync_from_left()

    def _on_right_tz_changed(self, tzname):
        # Keep right local time; just recompute left
        self._sync_from_right()

    # ----- Settings & Known Locations -----

    def open_settings(self):
        dlg = SettingsDialog(self.use_24h, self)
        if dlg.exec():
            self.use_24h = dlg.use_24h
            self.col_left.set_use_24h(self.use_24h)
            self.col_right.set_use_24h(self.use_24h)

    def open_known_locations(self):
        dlg = KnownLocationsDialog(self.known_locations, self)
        dlg.exec()

    # ----- Map selection -----

    def open_map_for_column(self, column_widget):
        try:
            dlg = MapDialog(self)
        except Exception as e:
            QMessageBox.critical(self, "Map Error",
                                 f"Could not open map: {e}")
            return

        def on_location_selected(lat, lon, tzname):
            # We don't know a human-readable location name from lat/lon,
            # so just use the time zone name as the "location" text.
            column_widget.set_timezone(tzname, tzname)
            # Re-sync times
            if column_widget is self.col_left:
                self._sync_from_left()
            else:
                self._sync_from_right()

        dlg.locationSelected.connect(on_location_selected)
        dlg.exec()


# ----------------- main -----------------

def main():
    app = QApplication(sys.argv)
    window = MainWindow()
    window.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
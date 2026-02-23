import os
import stat
import time
import tarfile
import tempfile
import traceback
from dataclasses import dataclass
from pathlib import Path

import paramiko
from PySide6 import QtCore, QtGui, QtWidgets


# ----------------------------
# Helpers
# ----------------------------
def human_size(n: int) -> str:
    if n is None:
        return ""
    units = ["B", "KB", "MB", "GB", "TB"]
    f = float(n)
    for u in units:
        if f < 1024.0:
            return f"{f:.1f} {u}" if u != "B" else f"{int(f)} {u}"
        f /= 1024.0
    return f"{f:.1f} PB"


def is_sftp_dir(attr) -> bool:
    return stat.S_ISDIR(attr.st_mode)


def normalize_remote(path: str) -> str:
    if not path:
        return "/"
    path = path.replace("\\", "/")
    while "//" in path:
        path = path.replace("//", "/")
    return path


def join_remote(base: str, name: str) -> str:
    base = normalize_remote(base)
    if base.endswith("/"):
        return base + name
    return base + "/" + name


# ----------------------------
# SSH / SFTP session wrapper
# ----------------------------
class SshSftpSession(QtCore.QObject):
    connected_changed = QtCore.Signal(bool)

    def __init__(self):
        super().__init__()
        self.ssh = None
        self.sftp = None
        self.host = ""
        self.username = ""

    def is_connected(self) -> bool:
        return self.ssh is not None and self.sftp is not None

    def connect(self, host: str, username: str, password: str, port: int = 22, timeout: int = 12):
        self.disconnect()

        self.host = host
        self.username = username

        ssh = paramiko.SSHClient()
        ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())

        ssh.connect(
            hostname=host,
            port=port,
            username=username,
            password=password,
            timeout=timeout,
            allow_agent=False,
            look_for_keys=False,
        )
        sftp = ssh.open_sftp()

        self.ssh = ssh
        self.sftp = sftp
        self.connected_changed.emit(True)

    def disconnect(self):
        try:
            if self.sftp is not None:
                self.sftp.close()
        except Exception:
            pass
        try:
            if self.ssh is not None:
                self.ssh.close()
        except Exception:
            pass

        self.ssh = None
        self.sftp = None
        self.connected_changed.emit(False)

    def exec(self, cmd: str, timeout: int = 60) -> tuple[int, str, str]:
        if not self.is_connected():
            raise RuntimeError("Not connected")
        stdin, stdout, stderr = self.ssh.exec_command(cmd, timeout=timeout)
        out = stdout.read().decode(errors="replace")
        err = stderr.read().decode(errors="replace")
        code = stdout.channel.recv_exit_status()
        return code, out, err


# ----------------------------
# Remote model (simple list)
# ----------------------------
@dataclass
class RemoteEntry:
    name: str
    is_dir: bool
    size: int
    mtime: int
    mode: int


class RemoteTable(QtWidgets.QTableWidget):
    cd_requested = QtCore.Signal(str)  # remote path
    selection_changed = QtCore.Signal()

    def __init__(self):
        super().__init__(0, 4)
        self.setHorizontalHeaderLabels(["Name", "Type", "Size", "Modified"])
        self.horizontalHeader().setStretchLastSection(True)
        self.horizontalHeader().setSectionResizeMode(0, QtWidgets.QHeaderView.Stretch)
        self.horizontalHeader().setSectionResizeMode(1, QtWidgets.QHeaderView.ResizeToContents)
        self.horizontalHeader().setSectionResizeMode(2, QtWidgets.QHeaderView.ResizeToContents)
        self.horizontalHeader().setSectionResizeMode(3, QtWidgets.QHeaderView.ResizeToContents)

        self.setSelectionBehavior(QtWidgets.QAbstractItemView.SelectRows)
        self.setSelectionMode(QtWidgets.QAbstractItemView.ExtendedSelection)
        self.setEditTriggers(QtWidgets.QAbstractItemView.NoEditTriggers)
        self.setAlternatingRowColors(True)

        self.entries: list[RemoteEntry] = []
        self.current_path = "/"

        self.itemDoubleClicked.connect(self._on_double_click)
        self.itemSelectionChanged.connect(self.selection_changed)

    def set_path(self, p: str):
        self.current_path = normalize_remote(p)

    def populate(self, entries: list[RemoteEntry]):
        self.setRowCount(0)
        self.entries = entries

        for e in entries:
            r = self.rowCount()
            self.insertRow(r)

            name_item = QtWidgets.QTableWidgetItem(e.name)
            if e.is_dir:
                name_item.setIcon(self.style().standardIcon(QtWidgets.QStyle.SP_DirIcon))
                name_item.setFont(QtGui.QFont(name_item.font().family(), name_item.font().pointSize(), QtGui.QFont.Bold))
            else:
                name_item.setIcon(self.style().standardIcon(QtWidgets.QStyle.SP_FileIcon))

            type_item = QtWidgets.QTableWidgetItem("Folder" if e.is_dir else "File")
            size_item = QtWidgets.QTableWidgetItem("" if e.is_dir else human_size(e.size))
            mod_item = QtWidgets.QTableWidgetItem(
                time.strftime("%Y-%m-%d %H:%M", time.localtime(e.mtime)) if e.mtime else ""
            )

            self.setItem(r, 0, name_item)
            self.setItem(r, 1, type_item)
            self.setItem(r, 2, size_item)
            self.setItem(r, 3, mod_item)

    def selected_entries(self) -> list[RemoteEntry]:
        rows = sorted({i.row() for i in self.selectedItems()})
        out = []
        for r in rows:
            if 0 <= r < len(self.entries):
                out.append(self.entries[r])
        return out

    def _on_double_click(self, item: QtWidgets.QTableWidgetItem):
        r = item.row()
        if 0 <= r < len(self.entries):
            e = self.entries[r]
            if e.is_dir:
                new_path = join_remote(self.current_path, e.name)
                self.cd_requested.emit(new_path)


# ----------------------------
# Transfer worker (threaded)
# ----------------------------
class TransferWorker(QtCore.QObject):
    progress = QtCore.Signal(int)        # 0..100
    status = QtCore.Signal(str)
    finished = QtCore.Signal(bool, str)  # ok, message

    def __init__(self, session: SshSftpSession):
        super().__init__()
        self.session = session
        self._cancel = False

    def cancel(self):
        self._cancel = True

    def _put_file(self, local_path: str, remote_path: str):
        sftp = self.session.sftp
        total = os.path.getsize(local_path)

        def cb(tx, _total):
            if total > 0:
                pct = int((tx / total) * 100)
                self.progress.emit(max(0, min(100, pct)))
            if self._cancel:
                raise RuntimeError("Cancelled")

        self.status.emit(f"Uploading: {os.path.basename(local_path)}")
        self.progress.emit(0)
        sftp.put(local_path, remote_path, callback=cb)
        self.progress.emit(100)

    def _get_file(self, remote_path: str, local_path: str, remote_size: int | None = None):
        sftp = self.session.sftp
        if remote_size is None:
            try:
                remote_size = sftp.stat(remote_path).st_size
            except Exception:
                remote_size = 0

        def cb(tx, _total):
            if remote_size and remote_size > 0:
                pct = int((tx / remote_size) * 100)
                self.progress.emit(max(0, min(100, pct)))
            if self._cancel:
                raise RuntimeError("Cancelled")

        self.status.emit(f"Downloading: {os.path.basename(remote_path)}")
        self.progress.emit(0)
        sftp.get(remote_path, local_path, callback=cb)
        self.progress.emit(100)

    @QtCore.Slot(str, str)
    def upload_files(self, local_paths_csv: str, remote_dir: str):
        try:
            remote_dir = normalize_remote(remote_dir)

            local_paths = [p for p in local_paths_csv.split("|") if p.strip()]
            if not local_paths:
                self.finished.emit(False, "No local files selected.")
                return

            self.status.emit(f"Ensuring remote dir: {remote_dir}")
            self.session.exec(f"mkdir -p '{remote_dir}'")

            for i, lp in enumerate(local_paths, start=1):
                if self._cancel:
                    raise RuntimeError("Cancelled")
                rp = join_remote(remote_dir, os.path.basename(lp))
                self.status.emit(f"[{i}/{len(local_paths)}] Uploading file -> {rp}")
                self._put_file(lp, rp)

            self.finished.emit(True, "Upload completed.")
        except Exception as e:
            self.finished.emit(False, f"Upload failed: {e}")

    @QtCore.Slot(str, str, bool)
    def upload_folder(self, local_folder: str, remote_dir: str, extract_remote: bool):
        try:
            if not os.path.isdir(local_folder):
                self.finished.emit(False, "Selected path is not a folder.")
                return

            remote_dir = normalize_remote(remote_dir)
            folder_name = os.path.basename(os.path.abspath(local_folder))
            tmp = tempfile.NamedTemporaryFile(delete=False, suffix=f"_{folder_name}.tar.gz")
            tmp.close()
            tar_path = tmp.name

            self.status.emit("Compressing folder...")
            self.progress.emit(0)
            with tarfile.open(tar_path, "w:gz") as tar:
                tar.add(local_folder, arcname=folder_name)

            self.status.emit("Ensuring remote dir...")
            self.session.exec(f"mkdir -p '{remote_dir}'")

            remote_tar = join_remote(remote_dir, f"{folder_name}.tar.gz")
            self.status.emit("Uploading compressed folder...")
            self._put_file(tar_path, remote_tar)

            if extract_remote:
                self.status.emit("Extracting on remote...")
                cmd = f"tar -xzf '{remote_tar}' -C '{remote_dir}' && rm -f '{remote_tar}'"
                code, out, err = self.session.exec(cmd, timeout=300)
                if code != 0:
                    raise RuntimeError(f"Remote extract failed: {err.strip() or out.strip()}")

            try:
                os.remove(tar_path)
            except Exception:
                pass

            self.finished.emit(True, "Folder upload completed.")
        except Exception as e:
            self.finished.emit(False, f"Folder upload failed: {e}")

    @QtCore.Slot(str, str)
    def download_files(self, remote_paths_csv: str, local_dir: str):
        try:
            local_dir = os.path.abspath(local_dir)
            os.makedirs(local_dir, exist_ok=True)

            remote_paths = [p for p in remote_paths_csv.split("|") if p.strip()]
            if not remote_paths:
                self.finished.emit(False, "No remote files selected.")
                return

            for i, rp in enumerate(remote_paths, start=1):
                if self._cancel:
                    raise RuntimeError("Cancelled")
                fname = os.path.basename(rp.rstrip("/"))
                lp = os.path.join(local_dir, fname)
                self.status.emit(f"[{i}/{len(remote_paths)}] Downloading -> {lp}")
                self._get_file(rp, lp)

            self.finished.emit(True, "Download completed.")
        except Exception as e:
            self.finished.emit(False, f"Download failed: {e}")

    @QtCore.Slot(str, str, bool)
    def download_folder(self, remote_folder: str, local_dir: str, extract_local: bool):
        try:
            remote_folder = normalize_remote(remote_folder)
            local_dir = os.path.abspath(local_dir)
            os.makedirs(local_dir, exist_ok=True)

            folder_name = os.path.basename(remote_folder.rstrip("/"))
            remote_tar = normalize_remote(f"/tmp/{folder_name}_{int(time.time())}.tar.gz")
            local_tar = os.path.join(local_dir, os.path.basename(remote_tar))

            self.status.emit("Creating archive on remote...")
            parent = normalize_remote(os.path.dirname(remote_folder.rstrip("/")))
            base = folder_name
            cmd = f"tar -czf '{remote_tar}' -C '{parent}' '{base}'"
            code, out, err = self.session.exec(cmd, timeout=300)
            if code != 0:
                raise RuntimeError(f"Remote archive failed: {err.strip() or out.strip()}")

            self.status.emit("Downloading archive...")
            self._get_file(remote_tar, local_tar)

            self.status.emit("Cleaning remote temp...")
            self.session.exec(f"rm -f '{remote_tar}'")

            if extract_local:
                self.status.emit("Extracting locally...")
                with tarfile.open(local_tar, "r:gz") as tar:
                    tar.extractall(local_dir)
                try:
                    os.remove(local_tar)
                except Exception:
                    pass

            self.finished.emit(True, "Folder download completed.")
        except Exception as e:
            self.finished.emit(False, f"Folder download failed: {e}")


# ----------------------------
# FIX: Controller signals to force QueuedConnection
# ----------------------------
class TransferController(QtCore.QObject):
    upload_files = QtCore.Signal(str, str)
    upload_folder = QtCore.Signal(str, str, bool)
    download_files = QtCore.Signal(str, str)
    download_folder = QtCore.Signal(str, str, bool)


# ----------------------------
# Main Window
# ----------------------------
class MainWindow(QtWidgets.QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("ProSFTP Transfer — GUI")
        self.resize(1200, 720)

        self.session = SshSftpSession()

        # Thread + worker
        self.worker_thread = QtCore.QThread(self)
        self.worker = TransferWorker(self.session)
        self.worker.moveToThread(self.worker_thread)
        self.worker_thread.start()

        # FIX: Controller + QueuedConnection wiring
        self.ctrl = TransferController()
        self.ctrl.upload_files.connect(self.worker.upload_files, QtCore.Qt.QueuedConnection)
        self.ctrl.upload_folder.connect(self.worker.upload_folder, QtCore.Qt.QueuedConnection)
        self.ctrl.download_files.connect(self.worker.download_files, QtCore.Qt.QueuedConnection)
        self.ctrl.download_folder.connect(self.worker.download_folder, QtCore.Qt.QueuedConnection)

        self._active_transfer_dialog = None

        # UI
        central = QtWidgets.QWidget()
        self.setCentralWidget(central)
        root = QtWidgets.QVBoxLayout(central)
        root.setContentsMargins(12, 12, 12, 12)
        root.setSpacing(10)

        # Top connect bar
        top = QtWidgets.QGroupBox("Connection")
        top_l = QtWidgets.QGridLayout(top)
        top_l.setHorizontalSpacing(8)
        top_l.setVerticalSpacing(8)

        self.ip = QtWidgets.QLineEdit()
        self.ip.setPlaceholderText("IP / Host (e.g. 192.168.1.10)")
        self.port = QtWidgets.QSpinBox()
        self.port.setRange(1, 65535)
        self.port.setValue(22)
        self.user = QtWidgets.QLineEdit()
        self.user.setPlaceholderText("Username")
        self.pw = QtWidgets.QLineEdit()
        self.pw.setPlaceholderText("Password")
        self.pw.setEchoMode(QtWidgets.QLineEdit.Password)

        self.btn_connect = QtWidgets.QPushButton("Connect")
        self.btn_disconnect = QtWidgets.QPushButton("Disconnect")
        self.btn_disconnect.setEnabled(False)

        top_l.addWidget(QtWidgets.QLabel("Host"), 0, 0)
        top_l.addWidget(self.ip, 0, 1)
        top_l.addWidget(QtWidgets.QLabel("Port"), 0, 2)
        top_l.addWidget(self.port, 0, 3)
        top_l.addWidget(QtWidgets.QLabel("Username"), 1, 0)
        top_l.addWidget(self.user, 1, 1)
        top_l.addWidget(QtWidgets.QLabel("Password"), 1, 2)
        top_l.addWidget(self.pw, 1, 3)
        top_l.addWidget(self.btn_connect, 0, 4, 1, 1)
        top_l.addWidget(self.btn_disconnect, 1, 4, 1, 1)

        root.addWidget(top)

        # Split view
        split = QtWidgets.QSplitter()
        split.setOrientation(QtCore.Qt.Horizontal)
        root.addWidget(split, 1)

        # Local panel
        local_box = QtWidgets.QGroupBox("Local Files")
        local_l = QtWidgets.QVBoxLayout(local_box)
        local_l.setContentsMargins(10, 10, 10, 10)

        self.local_path = QtWidgets.QLineEdit(str(Path.home()))
        self.local_path.setPlaceholderText("Local path")
        self.btn_local_browse = QtWidgets.QToolButton()
        self.btn_local_browse.setText("…")

        local_top = QtWidgets.QHBoxLayout()
        local_top.addWidget(QtWidgets.QLabel("Path:"))
        local_top.addWidget(self.local_path, 1)
        local_top.addWidget(self.btn_local_browse)
        local_l.addLayout(local_top)

        self.local_model = QtWidgets.QFileSystemModel()
        self.local_model.setRootPath(self.local_path.text())
        self.local_tree = QtWidgets.QTreeView()
        self.local_tree.setModel(self.local_model)
        self.local_tree.setRootIndex(self.local_model.index(self.local_path.text()))
        self.local_tree.setSelectionMode(QtWidgets.QAbstractItemView.ExtendedSelection)
        self.local_tree.setAnimated(True)
        self.local_tree.setSortingEnabled(True)
        self.local_tree.sortByColumn(0, QtCore.Qt.AscendingOrder)
        self.local_tree.setColumnWidth(0, 280)

        local_l.addWidget(self.local_tree, 1)
        split.addWidget(local_box)

        # Remote panel
        remote_box = QtWidgets.QGroupBox("Remote Files (Server)")
        remote_l = QtWidgets.QVBoxLayout(remote_box)
        remote_l.setContentsMargins(10, 10, 10, 10)

        remote_top = QtWidgets.QHBoxLayout()
        self.remote_path = QtWidgets.QLineEdit("/")
        self.btn_remote_up = QtWidgets.QToolButton()
        self.btn_remote_up.setText("↑")
        self.btn_remote_refresh = QtWidgets.QToolButton()
        self.btn_remote_refresh.setText("⟳")
        self.btn_remote_mkdir = QtWidgets.QToolButton()
        self.btn_remote_mkdir.setText("＋Dir")
        remote_top.addWidget(QtWidgets.QLabel("Path:"))
        remote_top.addWidget(self.remote_path, 1)
        remote_top.addWidget(self.btn_remote_up)
        remote_top.addWidget(self.btn_remote_refresh)
        remote_top.addWidget(self.btn_remote_mkdir)

        remote_l.addLayout(remote_top)

        self.remote_table = RemoteTable()
        remote_l.addWidget(self.remote_table, 1)

        split.addWidget(remote_box)
        split.setSizes([520, 680])

        # Actions
        actions = QtWidgets.QGroupBox("Transfer Actions")
        a = QtWidgets.QGridLayout(actions)
        a.setHorizontalSpacing(10)
        a.setVerticalSpacing(8)

        self.chk_folder_auto = QtWidgets.QCheckBox("Folder mode: Compress/Extract automatically (tar.gz)")
        self.chk_folder_auto.setChecked(True)

        self.chk_extract_remote = QtWidgets.QCheckBox("After folder upload: Extract on remote + remove archive")
        self.chk_extract_remote.setChecked(True)

        self.chk_extract_local = QtWidgets.QCheckBox("After folder download: Extract locally + remove archive")
        self.chk_extract_local.setChecked(True)

        self.btn_upload_files = QtWidgets.QPushButton("Upload Selected File(s) →")
        self.btn_upload_folder = QtWidgets.QPushButton("Upload Selected Folder →")
        self.btn_download_files = QtWidgets.QPushButton("← Download Selected File(s)")
        self.btn_download_folder = QtWidgets.QPushButton("← Download Selected Folder")

        self.btn_upload_files.setEnabled(False)
        self.btn_upload_folder.setEnabled(False)
        self.btn_download_files.setEnabled(False)
        self.btn_download_folder.setEnabled(False)

        a.addWidget(self.chk_folder_auto, 0, 0, 1, 2)
        a.addWidget(self.chk_extract_remote, 1, 0, 1, 1)
        a.addWidget(self.chk_extract_local, 1, 1, 1, 1)

        a.addWidget(self.btn_upload_files, 2, 0)
        a.addWidget(self.btn_download_files, 2, 1)
        a.addWidget(self.btn_upload_folder, 3, 0)
        a.addWidget(self.btn_download_folder, 3, 1)

        root.addWidget(actions)

        # Bottom status + progress + log
        bottom = QtWidgets.QHBoxLayout()
        self.progress = QtWidgets.QProgressBar()
        self.progress.setRange(0, 100)
        self.progress.setValue(0)
        self.lbl_status = QtWidgets.QLabel("Ready.")
        bottom.addWidget(self.lbl_status, 1)
        bottom.addWidget(self.progress, 0)
        root.addLayout(bottom)

        self.log = QtWidgets.QPlainTextEdit()
        self.log.setReadOnly(True)
        self.log.setMaximumBlockCount(2000)
        root.addWidget(self.log, 0)

        # Signals
        self.btn_connect.clicked.connect(self.on_connect)
        self.btn_disconnect.clicked.connect(self.on_disconnect)

        self.btn_local_browse.clicked.connect(self.on_pick_local_path)
        self.local_path.returnPressed.connect(self.on_local_path_changed)

        self.btn_remote_refresh.clicked.connect(self.refresh_remote)
        self.btn_remote_up.clicked.connect(self.remote_up)
        self.remote_path.returnPressed.connect(self.on_remote_path_enter)
        self.btn_remote_mkdir.clicked.connect(self.on_remote_mkdir)

        self.remote_table.cd_requested.connect(self.on_remote_cd)

        self.btn_upload_files.clicked.connect(self.on_upload_files)
        self.btn_upload_folder.clicked.connect(self.on_upload_folder)
        self.btn_download_files.clicked.connect(self.on_download_files)
        self.btn_download_folder.clicked.connect(self.on_download_folder)

        self.session.connected_changed.connect(self.on_connected_changed)

        # Worker signals
        self.worker.progress.connect(self.progress.setValue)
        self.worker.status.connect(self.set_status)
        self.worker.finished.connect(self.on_transfer_finished)

        # Styles
        self.apply_dark_style()
        self.write_log("Welcome. Enter Host/IP + Username + Password, then Connect.")

    # ------------- UI / Style -------------
    def apply_dark_style(self):
        self.setStyleSheet("""
            QMainWindow { background: #0f1216; color: #eaeef3; }
            QLabel { color: #eaeef3; }
            QGroupBox {
                border: 1px solid #2a2f36;
                border-radius: 10px;
                margin-top: 10px;
                padding: 10px;
                background: #12161c;
            }
            QGroupBox::title {
                subcontrol-origin: margin;
                left: 10px;
                padding: 0 6px;
                color: #cfd6df;
            }
            QLineEdit, QSpinBox {
                background: #0e1217;
                border: 1px solid #2a2f36;
                border-radius: 8px;
                padding: 8px;
                color: #eaeef3;
            }
            QLineEdit:focus, QSpinBox:focus {
                border: 1px solid #4c89ff;
            }
            QPushButton {
                background: #1a2230;
                border: 1px solid #2a2f36;
                border-radius: 10px;
                padding: 10px 14px;
                color: #eaeef3;
            }
            QPushButton:hover { border: 1px solid #4c89ff; }
            QPushButton:disabled { color: #667081; background: #151a21; }
            QToolButton {
                background: #1a2230;
                border: 1px solid #2a2f36;
                border-radius: 10px;
                padding: 8px 10px;
                color: #eaeef3;
            }
            QToolButton:hover { border: 1px solid #4c89ff; }
            QTreeView, QTableWidget, QPlainTextEdit {
                background: #0e1217;
                border: 1px solid #2a2f36;
                border-radius: 10px;
                color: #eaeef3;
                gridline-color: #2a2f36;
            }
            QHeaderView::section {
                background: #12161c;
                border: 1px solid #2a2f36;
                padding: 6px;
                color: #cfd6df;
            }
            QProgressBar {
                border: 1px solid #2a2f36;
                border-radius: 10px;
                background: #0e1217;
                text-align: center;
                padding: 2px;
                color: #eaeef3;
                min-width: 220px;
            }
            QProgressBar::chunk {
                background: #4c89ff;
                border-radius: 10px;
            }
            QCheckBox { color: #cfd6df; }
        """)

    def write_log(self, msg: str):
        ts = time.strftime("%H:%M:%S")
        self.log.appendPlainText(f"[{ts}] {msg}")

    def set_status(self, msg: str):
        self.lbl_status.setText(msg)
        self.write_log(msg)

    def set_busy(self, busy: bool):
        self.btn_upload_files.setEnabled(self.session.is_connected() and not busy)
        self.btn_upload_folder.setEnabled(self.session.is_connected() and not busy)
        self.btn_download_files.setEnabled(self.session.is_connected() and not busy)
        self.btn_download_folder.setEnabled(self.session.is_connected() and not busy)

        self.btn_connect.setEnabled(not busy and not self.session.is_connected())
        self.btn_disconnect.setEnabled(not busy and self.session.is_connected())

    # ------------- Connection -------------
    def on_connect(self):
        host = self.ip.text().strip()
        username = self.user.text().strip()
        password = self.pw.text()
        port = int(self.port.value())

        if not host or not username or not password:
            QtWidgets.QMessageBox.warning(self, "Missing", "Please fill Host, Username, Password.")
            return

        try:
            self.set_status("Connecting...")
            QtWidgets.QApplication.setOverrideCursor(QtCore.Qt.WaitCursor)
            self.session.connect(host, username, password, port=port)
            self.set_status("Connected.")
            self.refresh_remote()
        except Exception as e:
            self.session.disconnect()
            QtWidgets.QMessageBox.critical(self, "Connect failed", str(e))
            self.write_log("Connect error:\n" + traceback.format_exc())
        finally:
            QtWidgets.QApplication.restoreOverrideCursor()

    def on_disconnect(self):
        self.session.disconnect()
        self.set_status("Disconnected.")
        self.remote_table.populate([])
        self.remote_path.setText("/")
        self.progress.setValue(0)

    def on_connected_changed(self, ok: bool):
        self.btn_connect.setEnabled(not ok)
        self.btn_disconnect.setEnabled(ok)
        self.btn_upload_files.setEnabled(ok)
        self.btn_upload_folder.setEnabled(ok)
        self.btn_download_files.setEnabled(ok)
        self.btn_download_folder.setEnabled(ok)

    # ------------- Local browsing -------------
    def on_pick_local_path(self):
        d = QtWidgets.QFileDialog.getExistingDirectory(self, "Choose local directory", self.local_path.text())
        if d:
            self.local_path.setText(d)
            self.on_local_path_changed()

    def on_local_path_changed(self):
        p = self.local_path.text().strip()
        if not p:
            return
        if not os.path.isdir(p):
            QtWidgets.QMessageBox.warning(self, "Invalid path", "Local path is not a directory.")
            return
        self.local_model.setRootPath(p)
        self.local_tree.setRootIndex(self.local_model.index(p))

    def local_selected_paths(self) -> list[str]:
        idxs = self.local_tree.selectionModel().selectedRows()
        paths = []
        for idx in idxs:
            p = self.local_model.filePath(idx)
            if p:
                paths.append(p)
        return paths

    # ------------- Remote browsing -------------
    def refresh_remote(self):
        if not self.session.is_connected():
            return
        p = normalize_remote(self.remote_path.text().strip() or "/")
        try:
            self.remote_table.set_path(p)
            entries = []
            for attr in self.session.sftp.listdir_attr(p):
                entries.append(RemoteEntry(attr.filename, is_sftp_dir(attr), attr.st_size, attr.st_mtime, attr.st_mode))
            entries.sort(key=lambda e: (not e.is_dir, e.name.lower()))
            self.remote_table.populate(entries)
            self.set_status(f"Remote: {p}")
        except Exception as e:
            QtWidgets.QMessageBox.critical(self, "Remote list failed", str(e))
            self.write_log("Remote list error:\n" + traceback.format_exc())

    def on_remote_cd(self, new_path: str):
        self.remote_path.setText(normalize_remote(new_path))
        self.refresh_remote()

    def remote_up(self):
        p = normalize_remote(self.remote_path.text().strip() or "/")
        if p == "/":
            return
        parent = normalize_remote(os.path.dirname(p.rstrip("/")) or "/")
        self.remote_path.setText(parent)
        self.refresh_remote()

    def on_remote_path_enter(self):
        self.refresh_remote()

    def on_remote_mkdir(self):
        if not self.session.is_connected():
            return
        base = normalize_remote(self.remote_path.text().strip() or "/")
        name, ok = QtWidgets.QInputDialog.getText(self, "Create directory", "Folder name:")
        if not ok or not name.strip():
            return
        name = name.strip().replace("\\", "/")
        newp = join_remote(base, name)
        try:
            self.session.exec(f"mkdir -p '{newp}'")
            self.write_log(f"Created: {newp}")
            self.refresh_remote()
        except Exception as e:
            QtWidgets.QMessageBox.critical(self, "mkdir failed", str(e))

    def remote_selected_single(self) -> tuple[str | None, RemoteEntry | None]:
        sel = self.remote_table.selected_entries()
        if len(sel) != 1:
            return None, None
        e = sel[0]
        return join_remote(self.remote_table.current_path, e.name), e

    # ------------- Transfers -------------
    def on_upload_files(self):
        if not self.session.is_connected():
            return
        sel = self.local_selected_paths()
        files = [p for p in sel if os.path.isfile(p)]
        if not files:
            QtWidgets.QMessageBox.information(self, "No files", "Select one or more files from Local panel.")
            return

        remote_dir = normalize_remote(self.remote_path.text().strip() or "/")
        self.run_worker("upload_files", {"local_paths_csv": "|".join(files), "remote_dir": remote_dir})

    def on_upload_folder(self):
        if not self.session.is_connected():
            return
        sel = self.local_selected_paths()
        folders = [p for p in sel if os.path.isdir(p)]
        if len(folders) != 1:
            QtWidgets.QMessageBox.information(self, "Select one folder", "Select exactly ONE folder in Local panel.")
            return

        remote_dir = normalize_remote(self.remote_path.text().strip() or "/")
        extract_remote = self.chk_folder_auto.isChecked() and self.chk_extract_remote.isChecked()
        self.run_worker("upload_folder", {"local_folder": folders[0], "remote_dir": remote_dir, "extract_remote": extract_remote})

    def on_download_files(self):
        if not self.session.is_connected():
            return
        selected = self.remote_table.selected_entries()
        if not selected:
            QtWidgets.QMessageBox.information(self, "No selection", "Select one or more items from Remote panel.")
            return
        files = [join_remote(self.remote_table.current_path, e.name) for e in selected if not e.is_dir]
        if not files:
            QtWidgets.QMessageBox.information(self, "No files", "Selected items contain no files.")
            return

        local_dir = QtWidgets.QFileDialog.getExistingDirectory(self, "Download to local directory", self.local_path.text())
        if not local_dir:
            return

        self.run_worker("download_files", {"remote_paths_csv": "|".join(files), "local_dir": local_dir})

    def on_download_folder(self):
        if not self.session.is_connected():
            return
        rp, entry = self.remote_selected_single()
        if not rp or not entry or not entry.is_dir:
            QtWidgets.QMessageBox.information(self, "Select one folder", "Select exactly ONE folder on Remote panel.")
            return

        local_dir = QtWidgets.QFileDialog.getExistingDirectory(self, "Download folder into local directory", self.local_path.text())
        if not local_dir:
            return

        extract_local = self.chk_folder_auto.isChecked() and self.chk_extract_local.isChecked()
        self.run_worker("download_folder", {"remote_folder": rp, "local_dir": local_dir, "extract_local": extract_local})

    # FIXED: no invokeMethod; use controller signals + disconnect progress after done
    def run_worker(self, method_name: str, kwargs: dict):
        self.progress.setValue(0)
        self.set_busy(True)

        dlg = QtWidgets.QDialog(self)
        dlg.setWindowTitle("Transferring…")
        dlg.setModal(True)
        v = QtWidgets.QVBoxLayout(dlg)
        lbl = QtWidgets.QLabel("Transfer in progress…")
        pb = QtWidgets.QProgressBar()
        pb.setRange(0, 100)
        pb.setValue(0)
        btn_cancel = QtWidgets.QPushButton("Cancel")
        v.addWidget(lbl)
        v.addWidget(pb)
        v.addWidget(btn_cancel)

        # Temporary bind (avoid accumulating connections)
        self.worker.progress.connect(pb.setValue)

        def cancel():
            self.worker.cancel()
            self.write_log("Cancel requested…")
        btn_cancel.clicked.connect(cancel)

        self._active_transfer_dialog = dlg
        self.worker._cancel = False

        def on_done(_ok: bool, _message: str):
            try:
                self.worker.progress.disconnect(pb.setValue)
            except Exception:
                pass
            try:
                self.worker.finished.disconnect(on_done)
            except Exception:
                pass
            try:
                dlg.close()
            except Exception:
                pass
            self._active_transfer_dialog = None

        self.worker.finished.connect(on_done)

        # Emit queued signal to worker thread
        if method_name == "upload_files":
            self.ctrl.upload_files.emit(kwargs["local_paths_csv"], kwargs["remote_dir"])
        elif method_name == "upload_folder":
            self.ctrl.upload_folder.emit(kwargs["local_folder"], kwargs["remote_dir"], kwargs["extract_remote"])
        elif method_name == "download_files":
            self.ctrl.download_files.emit(kwargs["remote_paths_csv"], kwargs["local_dir"])
        elif method_name == "download_folder":
            self.ctrl.download_folder.emit(kwargs["remote_folder"], kwargs["local_dir"], kwargs["extract_local"])
        else:
            self.set_busy(False)
            dlg.close()
            raise ValueError("Unknown transfer method")

        dlg.show()

    def on_transfer_finished(self, ok: bool, message: str):
        self.set_busy(False)
        if ok:
            self.set_status(message)
            self.refresh_remote()
        else:
            self.set_status(message)
            QtWidgets.QMessageBox.critical(self, "Transfer error", message)


def main():
    app = QtWidgets.QApplication([])
    win = MainWindow()
    win.show()
    app.exec()


if __name__ == "__main__":
    main()

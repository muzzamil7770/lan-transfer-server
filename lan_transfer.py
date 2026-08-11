#!/usr/bin/env python3
"""
Enterprise LAN File Transfer Server
Production-ready single-file Flask application
Supports large files, resume, drag-drop, modern UI
"""

import utils.session
from flask import (
    Flask, request, render_template_string, send_file,
    redirect, url_for, abort, jsonify, Response
)
import os
import shutil
import time
import uuid
import logging
import json
import tempfile
import traceback
import threading
from collections import deque
from datetime import datetime
from logging.handlers import RotatingFileHandler
from urllib.parse import quote, unquote
from werkzeug.utils import secure_filename
from functools import wraps

# ============================================================
# CONFIGURATION
# ============================================================

SHARE_FOLDER = r"C:\Users\Administrator\Documents\ShareOnNetwork"   # ← CHANGE THIS
HOST = "0.0.0.0"
PORT = 8000
CHUNK_SIZE = 8 * 1024 * 1024          # 8 MB chunks for large uploads
MAX_CONTENT_LENGTH = None             # None = unlimited (LAN)
SECRET_KEY = "change-me-in-production-lan-only"

# ============================================================
# APP SETUP
# ============================================================

app = Flask(__name__)
app.secret_key = SECRET_KEY
app.config["MAX_CONTENT_LENGTH"] = MAX_CONTENT_LENGTH

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s"
)
log = logging.getLogger("lan-file-server")
log.setLevel(logging.INFO)

SHARE_FOLDER = os.path.abspath(SHARE_FOLDER)
BASE_DIR = os.path.dirname(os.path.abspath(__file__)) if "__file__" in globals() else os.getcwd()
try:
    os.makedirs(SHARE_FOLDER, exist_ok=True)
except Exception as e:
    log.warning("Unable to create SHARE_FOLDER %s: %s", SHARE_FOLDER, e)

# Temporary directory for in-progress chunked uploads
TEMP_DIR = os.path.join(BASE_DIR, "upload_tmp")
try:
    os.makedirs(TEMP_DIR, exist_ok=True)
except Exception as first_error:
    backup_tmp = os.path.join(tempfile.gettempdir(), "lan_transfer_upload_tmp")
    try:
        os.makedirs(backup_tmp, exist_ok=True)
        TEMP_DIR = backup_tmp
        log.warning("Falling back to system temp upload dir: %s", TEMP_DIR)
    except Exception as second_error:
        log.error("Unable to create any temp upload directory: %s ; %s", first_error, second_error)
        raise

app.config["UPLOAD_TMP_DIR"] = TEMP_DIR

# Ensure the base directory exists for logging
try:
    os.makedirs(BASE_DIR, exist_ok=True)
except Exception as e:
    log.warning("Unable to ensure BASE_DIR exists %s: %s", BASE_DIR, e)

# Enterprise logging to file
file_log_path = os.path.join(BASE_DIR, "lan_transfer.log")
audit_log_path = os.path.join(BASE_DIR, "lan_transfer_audit.log")
try:
    file_handler = RotatingFileHandler(file_log_path, maxBytes=5 * 1024 * 1024, backupCount=3, encoding="utf-8")
    file_handler.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))
    file_handler.setLevel(logging.INFO)
    log.addHandler(file_handler)
except Exception as e:
    backup_log_path = os.path.join(TEMP_DIR, "lan_transfer.log")
    log.warning("Unable to open file log %s: %s", file_log_path, e)
    try:
        file_handler = RotatingFileHandler(backup_log_path, maxBytes=5 * 1024 * 1024, backupCount=3, encoding="utf-8")
        file_handler.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))
        file_handler.setLevel(logging.INFO)
        log.addHandler(file_handler)
        log.warning("Using fallback log file: %s", backup_log_path)
    except Exception as inner_e:
        log.warning("Unable to open fallback file log %s: %s", backup_log_path, inner_e)

audit_logger = logging.getLogger("lan-file-server-audit")
audit_logger.setLevel(logging.INFO)
try:
    audit_handler = RotatingFileHandler(audit_log_path, maxBytes=10 * 1024 * 1024, backupCount=5, encoding="utf-8")
    audit_handler.setFormatter(logging.Formatter("%(message)s"))
    audit_logger.addHandler(audit_handler)
except Exception as e:
    backup_audit_path = os.path.join(TEMP_DIR, "lan_transfer_audit.log")
    log.warning("Unable to open audit log %s: %s", audit_log_path, e)
    try:
        audit_handler = RotatingFileHandler(backup_audit_path, maxBytes=10 * 1024 * 1024, backupCount=5, encoding="utf-8")
        audit_handler.setFormatter(logging.Formatter("%(message)s"))
        audit_logger.addHandler(audit_handler)
        log.warning("Using fallback audit log file: %s", backup_audit_path)
    except Exception as inner_e:
        log.warning("Unable to open fallback audit log %s: %s", backup_audit_path, inner_e)

active_transfers = {}
history_entries = deque(maxlen=200)
history_lock = threading.Lock()


# ============================================================
# SECURITY HELPERS
# ============================================================

def safe_path(subpath: str = "") -> str:
    """Prevent directory traversal. Always returns an absolute path inside SHARE_FOLDER."""
    base = os.path.abspath(SHARE_FOLDER)
    # Normalize and join
    requested = os.path.abspath(os.path.join(base, subpath.replace("\\", "/")))
    if os.path.commonpath([base, requested]) != base:
        abort(403)
    return requested


def format_size(num_bytes: int) -> str:
    if num_bytes is None or num_bytes < 0:
        return "-"
    units = ["B", "KB", "MB", "GB", "TB", "PB"]
    size = float(num_bytes)
    for unit in units:
        if size < 1024.0:
            return f"{size:.1f} {unit}"
        size /= 1024.0
    return f"{size:.1f} EB"


def now_iso() -> str:
    return datetime.utcnow().replace(microsecond=0).isoformat() + "Z"


def record_event(action: str, data: dict):
    entry = {
        "timestamp": now_iso(),
        "action": action,
        **data
    }
    with history_lock:
        history_entries.appendleft(entry)
    try:
        audit_logger.info(json.dumps(entry, ensure_ascii=False))
    except Exception:
        log.exception("Audit log write failed")
    return entry

ERROR_HTML = r"""
<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Error</title>
<style>
body { font-family: Arial, sans-serif; padding: 40px; background: #0e1726; color: #f8fafc; }
.container { max-width: 700px; margin: auto; background: #111827; padding: 30px; border-radius: 16px; border: 1px solid #334155; }
h1 { font-size: 2rem; margin-bottom: 16px; }
p { color: #cbd5e1; line-height: 1.6; }
.code { margin-top: 20px; padding: 16px; background: #0f172a; border-radius: 12px; border: 1px solid #334155; font-family: Consolas, monospace; }
</style>
</head>
<body>
<div class="container">
    <h1>Enterprise Transfer Error</h1>
    <p>An unexpected error occurred while processing your request.</p>
    <div class="code">{{ message }}</div>
    <p>If this happens repeatedly, check the server logs or restart the application.</p>
</div>
</body>
</html>
"""


@app.errorhandler(403)
@app.errorhandler(404)
@app.errorhandler(413)
def handle_http_error(error):
    log.warning("HTTP %s on %s: %s", error.code, request.path, getattr(error, 'description', str(error)))
    if request.path.startswith("/api/"):
        return jsonify(ok=False, error=getattr(error, 'description', str(error))), error.code
    return render_template_string(ERROR_HTML, message=getattr(error, 'description', str(error))), error.code


@app.errorhandler(Exception)
def handle_exception(error):
    log.exception("Unhandled exception on %s", request.path)
    if request.path.startswith("/api/"):
        return jsonify(ok=False, error="Internal server error"), 500
    return render_template_string(ERROR_HTML, message="Internal server error"), 500


# ============================================================
# HTML + CSS + JS (fully self-contained)
# ============================================================

HTML = r"""
<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>LAN File Transfer • Enterprise</title>
<style>
:root {
    --bg: #0f172a;
    --surface: #1e293b;
    --surface2: #334155;
    --border: #475569;
    --text: #f1f5f9;
    --text-muted: #94a3b8;
    --primary: #3b82f6;
    --primary-hover: #2563eb;
    --success: #22c55e;
    --danger: #ef4444;
    --warning: #f59e0b;
    --radius: 10px;
    --shadow: 0 4px 24px rgba(0,0,0,0.35);
}

* { box-sizing: border-box; margin: 0; padding: 0; }

body {
    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, "Helvetica Neue", Arial, sans-serif;
    background: var(--bg);
    color: var(--text);
    line-height: 1.5;
    min-height: 100vh;
}

.container {
    max-width: 1200px;
    margin: 0 auto;
    padding: 24px 20px 60px;
}

/* Header */
.header {
    display: flex;
    align-items: center;
    justify-content: space-between;
    margin-bottom: 28px;
    flex-wrap: wrap;
    gap: 12px;
}

.header h1 {
    font-size: 1.75rem;
    font-weight: 700;
    letter-spacing: -0.02em;
}

.header .badge {
    background: var(--surface2);
    color: var(--text-muted);
    font-size: 0.75rem;
    padding: 4px 10px;
    border-radius: 999px;
    font-weight: 500;
}

/* Path bar */
.path-bar {
    background: var(--surface);
    border: 1px solid var(--border);
    border-radius: var(--radius);
    padding: 12px 16px;
    margin-bottom: 20px;
    display: flex;
    align-items: center;
    gap: 10px;
    flex-wrap: wrap;
}

.path-bar .label { color: var(--text-muted); font-size: 0.85rem; }
.path-bar .current { font-weight: 600; word-break: break-all; }

/* Toolbar */
.toolbar {
    display: flex;
    gap: 10px;
    margin-bottom: 20px;
    flex-wrap: wrap;
}

.btn {
    display: inline-flex;
    align-items: center;
    gap: 6px;
    padding: 9px 16px;
    border-radius: 8px;
    border: none;
    font-size: 0.9rem;
    font-weight: 500;
    cursor: pointer;
    transition: all 0.15s ease;
    text-decoration: none;
    color: white;
}

.btn-primary { background: var(--primary); }
.btn-primary:hover { background: var(--primary-hover); }
.btn-secondary { background: var(--surface2); color: var(--text); }
.btn-secondary:hover { background: var(--border); }
.btn-danger { background: var(--danger); }
.btn-danger:hover { background: #dc2626; }
.btn-success { background: var(--success); }
.btn-sm { padding: 6px 12px; font-size: 0.8rem; }

/* Upload zone */
.upload-zone {
    border: 2px dashed var(--border);
    border-radius: var(--radius);
    background: var(--surface);
    padding: 32px 20px;
    text-align: center;
    margin-bottom: 28px;
    transition: all 0.2s ease;
    position: relative;
}

.upload-zone.dragover {
    border-color: var(--primary);
    background: rgba(59, 130, 246, 0.08);
}

.upload-zone h2 { font-size: 1.15rem; margin-bottom: 8px; }
.upload-zone p { color: var(--text-muted); font-size: 0.9rem; margin-bottom: 16px; }

.upload-zone input[type=file] {
    position: absolute;
    inset: 0;
    opacity: 0;
    cursor: pointer;
}

/* Progress list */
#progress-list {
    margin-top: 20px;
    display: flex;
    flex-direction: column;
    gap: 10px;
}

.progress-item {
    background: var(--surface2);
    border-radius: 8px;
    padding: 12px 14px;
    text-align: left;
}

.progress-item .name {
    font-size: 0.9rem;
    font-weight: 500;
    margin-bottom: 6px;
    display: flex;
    justify-content: space-between;
}

.progress-bar-bg {
    height: 8px;
    background: var(--bg);
    border-radius: 4px;
    overflow: hidden;
}

.progress-bar {
    height: 100%;
    background: var(--primary);
    width: 0%;
    transition: width 0.2s ease;
}

.progress-item .meta {
    font-size: 0.75rem;
    color: var(--text-muted);
    margin-top: 4px;
}

.dashboard-grid {
    display: grid;
    grid-template-columns: repeat(2, minmax(0, 1fr));
    gap: 16px;
    margin-bottom: 24px;
}

.dashboard-panel {
    background: var(--surface);
    border: 1px solid var(--border);
    border-radius: var(--radius);
    padding: 18px;
}

.dashboard-panel h3 {
    margin-bottom: 14px;
    font-size: 1rem;
    color: var(--text);
}

.dashboard-list {
    list-style: none;
    padding: 0;
    margin: 0;
    display: grid;
    gap: 10px;
}

.dashboard-item {
    background: rgba(255,255,255,0.03);
    border: 1px solid rgba(71, 85, 105, 0.6);
    border-radius: 10px;
    padding: 12px 14px;
}

.dashboard-item strong {
    display: block;
    margin-bottom: 6px;
    color: var(--text);
}

.dashboard-stats {
    display: grid;
    grid-template-columns: repeat(3, minmax(0, 1fr));
    gap: 10px;
    margin-bottom: 18px;
}

.dashboard-metric {
    background: rgba(255,255,255,0.04);
    border: 1px solid rgba(71,85,105,0.6);
    border-radius: 10px;
    padding: 12px;
}

.dashboard-metric span {
    display: block;
    font-size: 0.75rem;
    color: var(--text-muted);
}

/* File table */
.card {
    background: var(--surface);
    border: 1px solid var(--border);
    border-radius: var(--radius);
    overflow: hidden;
    box-shadow: var(--shadow);
}

.table-wrap { overflow-x: auto; }

table {
    width: 100%;
    border-collapse: collapse;
}

th, td {
    padding: 12px 16px;
    text-align: left;
    border-bottom: 1px solid var(--border);
}

th {
    background: var(--surface2);
    font-size: 0.8rem;
    text-transform: uppercase;
    letter-spacing: 0.04em;
    color: var(--text-muted);
    font-weight: 600;
}

tr:last-child td { border-bottom: none; }
tr:hover td { background: rgba(255,255,255,0.03); }

.name-cell {
    display: flex;
    align-items: center;
    gap: 10px;
}

.name-cell a {
    color: var(--text);
    text-decoration: none;
    font-weight: 500;
}
.name-cell a:hover { color: var(--primary); }

.folder-icon { color: #fbbf24; }
.file-icon { color: var(--text-muted); }

.size, .type { color: var(--text-muted); font-size: 0.9rem; }

.actions {
    display: flex;
    gap: 6px;
    flex-wrap: wrap;
}

.empty {
    text-align: center;
    padding: 48px 20px;
    color: var(--text-muted);
}

/* Modal */
.modal-overlay {
    position: fixed;
    inset: 0;
    background: rgba(0,0,0,0.65);
    display: none;
    align-items: center;
    justify-content: center;
    z-index: 1000;
    padding: 20px;
}

.modal-overlay.active { display: flex; }

.modal {
    background: var(--surface);
    border: 1px solid var(--border);
    border-radius: 12px;
    width: 100%;
    max-width: 420px;
    padding: 24px;
    box-shadow: var(--shadow);
}

.modal h3 { margin-bottom: 16px; font-size: 1.15rem; }
.modal input {
    width: 100%;
    padding: 10px 12px;
    border-radius: 8px;
    border: 1px solid var(--border);
    background: var(--bg);
    color: var(--text);
    font-size: 0.95rem;
    margin-bottom: 20px;
}

.modal-actions {
    display: flex;
    justify-content: flex-end;
    gap: 10px;
}

/* Toast */
#toast-container {
    position: fixed;
    bottom: 24px;
    right: 24px;
    z-index: 2000;
    display: flex;
    flex-direction: column;
    gap: 10px;
}

.toast {
    background: var(--surface2);
    border: 1px solid var(--border);
    color: var(--text);
    padding: 12px 18px;
    border-radius: 8px;
    box-shadow: var(--shadow);
    font-size: 0.9rem;
    animation: slideIn 0.25s ease;
    max-width: 360px;
}

.toast.success { border-left: 4px solid var(--success); }
.toast.error { border-left: 4px solid var(--danger); }

@keyframes slideIn {
    from { transform: translateX(40px); opacity: 0; }
    to { transform: translateX(0); opacity: 1; }
}

/* Responsive */
@media (max-width: 640px) {
    .header h1 { font-size: 1.4rem; }
    th, td { padding: 10px 12px; }
    .actions { flex-direction: column; }
    .dashboard-grid { grid-template-columns: 1fr; }
}
</style>
</head>
<body>

<div class="container">

    <div class="header">
        <div>
            <h1>📁 LAN File Transfer</h1>
            <div class="badge">Local Network • No Internet Required</div>
        </div>
        <div style="font-size:0.85rem;color:var(--text-muted)">
            {{ item_count }} items
        </div>
    </div>

    <div class="path-bar">
        <span class="label">Current path:</span>
        <span class="current">/{{ current_path or "" }}</span>
    </div>

    <div class="toolbar">
        {% if parent is not none %}
        <a href="{{ url_for('browse', subpath=parent) }}" class="btn btn-secondary">
            ← Up
        </a>
        {% endif %}
        <button class="btn btn-secondary" onclick="openCreateFolder()">
            + New Folder
        </button>
        <button class="btn btn-primary" onclick="document.getElementById('file-input').click()">
            ⬆ Select Files
        </button>
    </div>

    <!-- Upload Zone -->
    <div class="upload-zone" id="drop-zone">
        <h2>Drag & drop files or folders here</h2>
        <p>or click to browse • Large files supported with resume</p>
        <input type="file" id="file-input" multiple>
        <div id="progress-list"></div>
    </div>

    <div class="dashboard-grid">
        <div class="dashboard-panel">
            <h3>Live Transfer Dashboard</h3>
            <div class="dashboard-stats">
                <div class="dashboard-metric">
                    <strong id="active-count">0</strong>
                    <span>Active transfers</span>
                </div>
                <div class="dashboard-metric">
                    <strong id="history-count">0</strong>
                    <span>History entries</span>
                </div>
                <div class="dashboard-metric">
                    <strong id="temp-dir">{{ upload_tmp_dir }}</strong>
                    <span>Upload temp path</span>
                </div>
            </div>
            <ul class="dashboard-list" id="active-transfers"></ul>
        </div>
        <div class="dashboard-panel">
            <h3>Upload History / Audit Trail</h3>
            <ul class="dashboard-list" id="history-list"></ul>
        </div>
    </div>

    <!-- File Listing -->
    <div class="card">
        <div class="table-wrap">
            {% if items %}
            <table>
                <thead>
                    <tr>
                        <th>Name</th>
                        <th>Type</th>
                        <th>Size</th>
                        <th>Actions</th>
                    </tr>
                </thead>
                <tbody>
                    {% for item in items %}
                    <tr>
                        <td>
                            <div class="name-cell">
                                {% if item.is_dir %}
                                    <span class="folder-icon">📁</span>
                                    <a href="{{ url_for('browse', subpath=item.path) }}">{{ item.name }}</a>
                                {% else %}
                                    <span class="file-icon">📄</span>
                                    <a href="{{ url_for('download', subpath=item.path) }}">{{ item.name }}</a>
                                {% endif %}
                            </div>
                        </td>
                        <td class="type">{{ "Folder" if item.is_dir else "File" }}</td>
                        <td class="size">{{ item.size if not item.is_dir else "—" }}</td>
                        <td>
                            <div class="actions">
                                {% if not item.is_dir %}
                                <a href="{{ url_for('download', subpath=item.path) }}" class="btn btn-sm btn-primary">Download</a>
                                {% endif %}
                                <button class="btn btn-sm btn-secondary" onclick="openRename('{{ item.path | e }}', '{{ item.name | e }}', {{ 'true' if item.is_dir else 'false' }})">Rename</button>
                                <button class="btn btn-sm btn-danger" onclick="confirmDelete('{{ item.path | e }}', '{{ item.name | e }}', {{ 'true' if item.is_dir else 'false' }})">Delete</button>
                            </div>
                        </td>
                    </tr>
                    {% endfor %}
                </tbody>
            </table>
            {% else %}
            <div class="empty">This folder is empty. Upload files or create a folder to get started.</div>
            {% endif %}
        </div>
    </div>

</div>

<!-- Create Folder Modal -->
<div class="modal-overlay" id="modal-create">
    <div class="modal">
        <h3>Create New Folder</h3>
        <input type="text" id="new-folder-name" placeholder="Folder name" autocomplete="off">
        <div class="modal-actions">
            <button class="btn btn-secondary" onclick="closeModal('modal-create')">Cancel</button>
            <button class="btn btn-primary" onclick="createFolder()">Create</button>
        </div>
    </div>
</div>

<!-- Rename Modal -->
<div class="modal-overlay" id="modal-rename">
    <div class="modal">
        <h3>Rename</h3>
        <input type="text" id="rename-input" autocomplete="off">
        <input type="hidden" id="rename-path">
        <input type="hidden" id="rename-is-dir">
        <div class="modal-actions">
            <button class="btn btn-secondary" onclick="closeModal('modal-rename')">Cancel</button>
            <button class="btn btn-primary" onclick="doRename()">Save</button>
        </div>
    </div>
</div>

<div id="toast-container"></div>

<script>
const CURRENT_PATH = {{ current_path | tojson }};
const CHUNK_SIZE = 8 * 1024 * 1024; // must match server

// ---------- UI Helpers ----------
function toast(msg, type = "success") {
    const c = document.getElementById("toast-container");
    const t = document.createElement("div");
    t.className = `toast ${type}`;
    t.textContent = msg;
    c.appendChild(t);
    setTimeout(() => t.remove(), 5000);
}

window.addEventListener("unhandledrejection", event => {
    event.preventDefault();
    toast(event.reason ? event.reason.toString() : "Unhandled error", "error");
});

function openModal(id) { document.getElementById(id).classList.add("active"); }
function closeModal(id) { document.getElementById(id).classList.remove("active"); }

function openCreateFolder() {
    document.getElementById("new-folder-name").value = "";
    openModal("modal-create");
    setTimeout(() => document.getElementById("new-folder-name").focus(), 50);
}

function openRename(path, name, isDir) {
    document.getElementById("rename-path").value = path;
    document.getElementById("rename-is-dir").value = isDir;
    document.getElementById("rename-input").value = name;
    openModal("modal-rename");
    setTimeout(() => {
        const inp = document.getElementById("rename-input");
        inp.focus();
        inp.select();
    }, 50);
}

function confirmDelete(path, name, isDir) {
    if (!confirm(`Delete ${isDir === true || isDir === "true" ? "folder" : "file"} "${name}"?\nThis cannot be undone.`)) return;
    fetch(`/api/delete`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ path })
    })
    .then(r => r.json())
    .then(data => {
        if (data.ok) {
            toast("Deleted successfully");
            location.reload();
        } else {
            toast(data.error || "Delete failed", "error");
        }
    })
    .catch(e => toast("Network error", "error"));
}

function createFolder() {
    const name = document.getElementById("new-folder-name").value.trim();
    if (!name) return toast("Name required", "error");
    fetch(`/api/mkdir`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ path: CURRENT_PATH, name })
    })
    .then(r => r.json())
    .then(data => {
        if (data.ok) {
            toast("Folder created");
            closeModal("modal-create");
            location.reload();
        } else {
            toast(data.error || "Failed", "error");
        }
    });
}

function doRename() {
    const path = document.getElementById("rename-path").value;
    const newName = document.getElementById("rename-input").value.trim();
    if (!newName) return toast("Name required", "error");
    fetch(`/api/rename`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ path, new_name: newName })
    })
    .then(r => r.json())
    .then(data => {
        if (data.ok) {
            toast("Renamed");
            closeModal("modal-rename");
            location.reload();
        } else {
            toast(data.error || "Rename failed", "error");
        }
    });
}

// ---------- Drag & Drop ----------
const dropZone = document.getElementById("drop-zone");
const fileInput = document.getElementById("file-input");

["dragenter", "dragover"].forEach(ev => {
    dropZone.addEventListener(ev, e => {
        e.preventDefault();
        dropZone.classList.add("dragover");
    });
});
["dragleave", "drop"].forEach(ev => {
    dropZone.addEventListener(ev, e => {
        e.preventDefault();
        dropZone.classList.remove("dragover");
    });
});

dropZone.addEventListener("drop", e => {
    const files = e.dataTransfer.files;
    if (files.length) startUploads(files);
});

fileInput.addEventListener("change", () => {
    if (fileInput.files.length) startUploads(fileInput.files);
    fileInput.value = "";
});

// ---------- Chunked Upload with Resume ----------
function startUploads(fileList) {
    Array.from(fileList).forEach(file => uploadFile(file));
}

async function uploadFile(file) {
    const list = document.getElementById("progress-list");
    const item = document.createElement("div");
    item.className = "progress-item";
    item.innerHTML = `
        <div class="name">
            <span>${file.name}</span>
            <span class="pct">0%</span>
        </div>
        <div class="progress-bar-bg"><div class="progress-bar"></div></div>
        <div class="meta">Preparing…</div>
    `;
    list.appendChild(item);

    const bar = item.querySelector(".progress-bar");
    const pct = item.querySelector(".pct");
    const meta = item.querySelector(".meta");

    const uploadId = crypto.randomUUID ? crypto.randomUUID() : (Date.now() + "-" + Math.random().toString(36).slice(2));
    const totalChunks = Math.ceil(file.size / CHUNK_SIZE) || 1;

    try {
        // 1. Ask server how many bytes already exist (resume support)
        let offset = 0;
        const statusRes = await fetch(`/api/upload/status?name=${encodeURIComponent(file.name)}&size=${file.size}&path=${encodeURIComponent(CURRENT_PATH)}&id=${uploadId}`);
        const status = await statusRes.json();
        if (status.ok) offset = status.offset || 0;

        if (offset >= file.size) {
            bar.style.width = "100%";
            pct.textContent = "100%";
            meta.textContent = "Already complete";
            toast(`${file.name} already exists`);
            setTimeout(() => location.reload(), 800);
            return;
        }

        meta.textContent = offset > 0 ? `Resuming from ${formatBytes(offset)}…` : "Starting upload…";

        // 2. Upload remaining chunks sequentially (reliable on LAN + mobile)
        let uploaded = offset;
        let chunkIndex = Math.floor(offset / CHUNK_SIZE);

        while (uploaded < file.size) {
            const start = uploaded;
            const end = Math.min(start + CHUNK_SIZE, file.size);
            const blob = file.slice(start, end);

            const form = new FormData();
            form.append("file", blob, file.name);
            form.append("name", file.name);
            form.append("path", CURRENT_PATH);
            form.append("id", uploadId);
            form.append("offset", start);
            form.append("total", file.size);
            form.append("chunk_index", chunkIndex);
            form.append("total_chunks", totalChunks);

            const res = await fetch("/api/upload/chunk", {
                method: "POST",
                body: form
            });

            if (!res.ok) {
                const err = await res.json().catch(() => ({}));
                throw new Error(err.error || `HTTP ${res.status}`);
            }

            uploaded = end;
            chunkIndex++;

            const percent = Math.round((uploaded / file.size) * 100);
            bar.style.width = percent + "%";
            pct.textContent = percent + "%";
            meta.textContent = `${formatBytes(uploaded)} / ${formatBytes(file.size)}`;
        }

        // 3. Finalize
        meta.textContent = "Finalizing…";
        const fin = await fetch("/api/upload/finish", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({
                name: file.name,
                path: CURRENT_PATH,
                id: uploadId,
                size: file.size
            })
        });
        const finData = await fin.json();
        if (!finData.ok) throw new Error(finData.error || "Finalize failed");

        bar.style.width = "100%";
        pct.textContent = "100%";
        meta.textContent = "Complete ✓";
        toast(`${file.name} uploaded`);
        setTimeout(() => location.reload(), 900);

    } catch (err) {
        meta.textContent = "Failed: " + err.message;
        bar.style.background = "var(--danger)";
        toast(`Upload failed: ${err.message}`, "error");
    }
}

async function fetchTransferDashboard() {
    try {
        const res = await fetch('/api/transfers');
        if (!res.ok) throw new Error(`HTTP ${res.status}`);
        const data = await res.json();
        if (!data.ok) throw new Error(data.error || 'Invalid response');

        document.getElementById('active-count').textContent = data.active.length;
        document.getElementById('history-count').textContent = data.history.length;

        const activeList = document.getElementById('active-transfers');
        activeList.innerHTML = '';
        if (data.active.length === 0) {
            activeList.innerHTML = '<li class="dashboard-item">No active transfers.</li>';
        } else {
            data.active.forEach(item => {
                const li = document.createElement('li');
                li.className = 'dashboard-item';
                const percent = item.total ? Math.round((item.offset / item.total) * 100) : 0;
                li.innerHTML = `
                    <strong>${item.filename}</strong>
                    <div>${item.path || '/'}</div>
                    <div>${formatBytes(item.offset)} / ${formatBytes(item.total)} (${percent}%)</div>
                    <div style="color: var(--text-muted); font-size:0.8rem; margin-top:4px;">Started ${item.started_at}</div>
                `;
                activeList.appendChild(li);
            });
        }

        const historyList = document.getElementById('history-list');
        historyList.innerHTML = '';
        if (data.history.length === 0) {
            historyList.innerHTML = '<li class="dashboard-item">No audit history yet.</li>';
        } else {
            data.history.slice(0, 25).forEach(entry => {
                const li = document.createElement('li');
                li.className = 'dashboard-item';
                const details = Object.entries(entry)
                    .filter(([key]) => !['timestamp','action'].includes(key))
                    .map(([key, value]) => `<div><strong>${key}</strong>: ${typeof value === 'object' ? JSON.stringify(value) : value}</div>`)
                    .join('');
                li.innerHTML = `
                    <strong>${entry.action} • ${entry.timestamp}</strong>
                    ${details}
                `;
                historyList.appendChild(li);
            });
        }
    } catch (err) {
        console.warn('Dashboard refresh failed', err);
    }
}

function formatBytes(b) {
    if (b < 1024) return b + " B";
    if (b < 1048576) return (b / 1024).toFixed(1) + " KB";
    if (b < 1073741824) return (b / 1048576).toFixed(1) + " MB";
    return (b / 1073741824).toFixed(2) + " GB";
}

fetchTransferDashboard();
setInterval(fetchTransferDashboard, 5000);
</script>
</body>
</html>
"""


# ============================================================
# API ROUTES – File operations
# ============================================================

@app.route("/")
def home():
    return redirect(url_for("browse", subpath=""))


@app.route("/browse/")
@app.route("/browse/<path:subpath>")
def browse(subpath=""):
    folder = safe_path(subpath)
    if not os.path.isdir(folder):
        abort(404)

    items = []
    try:
        names = os.listdir(folder)
    except PermissionError:
        abort(403)

    for name in names:
        if name.startswith("."):          # hide temp / hidden
            continue
        full = os.path.join(folder, name)
        rel = os.path.relpath(full, SHARE_FOLDER).replace("\\", "/")

        if os.path.isdir(full):
            items.append({
                "name": name,
                "path": rel,
                "is_dir": True,
                "size": ""
            })
        else:
            try:
                size = os.path.getsize(full)
            except OSError:
                size = 0
            items.append({
                "name": name,
                "path": rel,
                "is_dir": False,
                "size": format_size(size)
            })

    items.sort(key=lambda x: (not x["is_dir"], x["name"].lower()))

    parent = None
    if subpath:
        parent = os.path.dirname(subpath).replace("\\", "/")

    return render_template_string(
        HTML,
        items=items,
        current_path=subpath,
        parent=parent,
        item_count=len(items),
        upload_tmp_dir=TEMP_DIR
    )


@app.route("/download/<path:subpath>")
def download(subpath):
    full = safe_path(subpath)
    if not os.path.isfile(full):
        abort(404)

    rel_path = os.path.relpath(full, SHARE_FOLDER).replace("\\", "/")
    record_event("download", {
        "path": rel_path,
        "filename": os.path.basename(full),
        "size": os.path.getsize(full)
    })

    # send_file with conditional=True enables Range requests → pause/resume
    return send_file(
        full,
        as_attachment=True,
        download_name=os.path.basename(full),
        conditional=True
    )


# ---------- Create / Rename / Delete ----------

@app.route("/api/mkdir", methods=["POST"])
def api_mkdir():
    data = request.get_json(force=True, silent=True) or {}
    path = data.get("path", "")
    name = (data.get("name") or "").strip()
    if not name or "/" in name or "\\" in name or name in (".", ".."):
        return jsonify(ok=False, error="Invalid folder name"), 400

    folder = safe_path(path)
    target = os.path.join(folder, name)
    if os.path.exists(target):
        return jsonify(ok=False, error="Already exists"), 400
    try:
        os.makedirs(target, exist_ok=False)
        rel = os.path.relpath(target, SHARE_FOLDER).replace("\\", "/")
        log.info("Created folder: %s", target)
        record_event("mkdir", {"path": rel, "name": name})
        return jsonify(ok=True)
    except Exception as e:
        return jsonify(ok=False, error=str(e)), 500


@app.route("/api/rename", methods=["POST"])
def api_rename():
    data = request.get_json(force=True, silent=True) or {}
    path = data.get("path", "")
    new_name = (data.get("new_name") or "").strip()
    if not new_name or "/" in new_name or "\\" in new_name:
        return jsonify(ok=False, error="Invalid name"), 400

    src = safe_path(path)
    dst = os.path.join(os.path.dirname(src), new_name)
    if os.path.exists(dst):
        return jsonify(ok=False, error="Target already exists"), 400
    try:
        os.rename(src, dst)
        rel_src = os.path.relpath(src, SHARE_FOLDER).replace("\\", "/")
        rel_dst = os.path.relpath(dst, SHARE_FOLDER).replace("\\", "/")
        log.info("Renamed %s → %s", src, dst)
        record_event("rename", {"from": rel_src, "to": rel_dst})
        return jsonify(ok=True)
    except Exception as e:
        return jsonify(ok=False, error=str(e)), 500


@app.route("/api/delete", methods=["POST"])
def api_delete():
    data = request.get_json(force=True, silent=True) or {}
    path = data.get("path", "")
    target = safe_path(path)
    try:
        if os.path.isdir(target):
            shutil.rmtree(target)
        else:
            os.remove(target)
        rel = os.path.relpath(target, SHARE_FOLDER).replace("\\", "/")
        log.info("Deleted: %s", target)
        record_event("delete", {"path": rel})
        return jsonify(ok=True)
    except Exception as e:
        return jsonify(ok=False, error=str(e)), 500


# ---------- Chunked Upload API ----------

def _temp_path(upload_id: str, filename: str) -> str:
    safe_name = secure_filename(filename) or "file"
    return os.path.join(TEMP_DIR, f"{upload_id}_{safe_name}")


@app.route("/api/upload/status")
def upload_status():
    name = request.args.get("name", "")
    size = int(request.args.get("size", 0))
    path = request.args.get("path", "")
    upload_id = request.args.get("id", "")

    if not name or not upload_id:
        return jsonify(ok=False, error="Missing params"), 400

    # Check if final file already exists with same size
    final_dir = safe_path(path)
    final_path = os.path.join(final_dir, secure_filename(name) or name)
    if os.path.isfile(final_path) and os.path.getsize(final_path) == size:
        return jsonify(ok=True, offset=size)

    tmp = _temp_path(upload_id, name)
    offset = 0
    if os.path.isfile(tmp):
        offset = os.path.getsize(tmp)

    return jsonify(ok=True, offset=offset)


@app.route("/api/upload/chunk", methods=["POST"])
def upload_chunk():
    f = request.files.get("file")
    name = request.form.get("name", "")
    path = request.form.get("path", "")
    upload_id = request.form.get("id", "")
    offset = int(request.form.get("offset", 0))

    if not f or not name or not upload_id:
        return jsonify(ok=False, error="Missing data"), 400

    tmp = _temp_path(upload_id, name)
    total_size = int(request.form.get("total", 0))
    filename = secure_filename(name) or name

    with history_lock:
        transfer = active_transfers.get(upload_id)
        if not transfer:
            transfer = {
                "id": upload_id,
                "filename": filename,
                "path": path,
                "offset": offset,
                "total": total_size,
                "status": "uploading",
                "started_at": now_iso()
            }
            active_transfers[upload_id] = transfer
            record_event("upload_started", {
                "upload_id": upload_id,
                "path": path,
                "filename": filename,
                "total": total_size
            })
        else:
            transfer["offset"] = offset
            transfer["total"] = total_size

    # Ensure we append at the correct position
    mode = "r+b" if os.path.exists(tmp) else "wb"
    try:
        with open(tmp, mode) as out:
            out.seek(offset)
            # Stream the chunk to disk
            while True:
                chunk = f.stream.read(1024 * 1024)
                if not chunk:
                    break
                out.write(chunk)
        with history_lock:
            active_transfers[upload_id]["offset"] = os.path.getsize(tmp)
        return jsonify(ok=True)
    except Exception as e:
        log.exception("Chunk write failed")
        record_event("upload_failed", {
            "upload_id": upload_id,
            "filename": filename,
            "path": path,
            "error": str(e)
        })
        return jsonify(ok=False, error=str(e)), 500


@app.route("/api/upload/finish", methods=["POST"])
def upload_finish():
    data = request.get_json(force=True, silent=True) or {}
    name = data.get("name", "")
    path = data.get("path", "")
    upload_id = data.get("id", "")
    expected_size = int(data.get("size", 0))

    if not name or not upload_id:
        return jsonify(ok=False, error="Missing data"), 400

    tmp = _temp_path(upload_id, name)
    if not os.path.isfile(tmp):
        record_event("upload_failed", {
            "upload_id": upload_id,
            "filename": name,
            "path": path,
            "error": "Temp file missing"
        })
        return jsonify(ok=False, error="Temp file missing"), 400

    actual = os.path.getsize(tmp)
    if expected_size and actual != expected_size:
        record_event("upload_failed", {
            "upload_id": upload_id,
            "filename": name,
            "path": path,
            "error": f"Size mismatch: {actual} != {expected_size}"
        })
        return jsonify(ok=False, error=f"Size mismatch: {actual} != {expected_size}"), 400

    final_dir = safe_path(path)
    safe_name = secure_filename(name) or name
    final_path = os.path.join(final_dir, safe_name)

    # Avoid overwriting – add (1), (2) …
    if os.path.exists(final_path):
        base, ext = os.path.splitext(safe_name)
        i = 1
        while True:
            candidate = os.path.join(final_dir, f"{base} ({i}){ext}")
            if not os.path.exists(candidate):
                final_path = candidate
                break
            i += 1

    try:
        # Atomic-ish move (with retry for Windows locks)
        for attempt in range(5):
            try:
                os.replace(tmp, final_path)
                break
            except PermissionError:
                time.sleep(0.3)
        else:
            shutil.move(tmp, final_path)

        rel_final = os.path.relpath(final_path, SHARE_FOLDER).replace("\\", "/")
        log.info("Upload finished: %s (%s)", final_path, format_size(actual))
        record_event("upload_finished", {
            "upload_id": upload_id,
            "path": rel_final,
            "filename": safe_name,
            "size": actual
        })
        with history_lock:
            active_transfers.pop(upload_id, None)
        return jsonify(ok=True, path=rel_final)
    except Exception as e:
        log.exception("Finalize failed")
        return jsonify(ok=False, error=str(e)), 500


@app.route("/api/transfers")
def api_transfers():
    with history_lock:
        active = [dict(t) for t in active_transfers.values()]
        history = list(history_entries)[:100]
    return jsonify(ok=True, active=active, history=history)


# ============================================================
# START
# ============================================================

if __name__ == "__main__":
    # Clean old temp files older than 2 days
    now = time.time()
    for f in os.listdir(TEMP_DIR):
        fp = os.path.join(TEMP_DIR, f)
        try:
            if os.path.isfile(fp) and now - os.path.getmtime(fp) > 172800:
                os.remove(fp)
        except OSError:
            pass

    print()
    print("=" * 50)
    print("  ENTERPRISE LAN FILE TRANSFER SERVER")
    print("=" * 50)
    print()
    print(f"  Shared folder : {SHARE_FOLDER}")
    print(f"  Listening on  : http://0.0.0.0:{PORT}")
    print()
    print("  Open from any device on the same LAN:")
    print(f"  ->  http://<this-pc-ip>:{PORT}")
    print()
    print("  Features: browse - drag-drop - chunked resume")
    print("            download resume - rename - delete")
    print()
    print("  Press CTRL+C to stop.")
    print("=" * 50)
    print()

    # threaded=True allows multiple concurrent transfers
    app.run(host=HOST, port=PORT, threaded=True, debug=False)
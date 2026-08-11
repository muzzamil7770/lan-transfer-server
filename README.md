# LAN Transfer Server

Enterprise-grade LAN file transfer server built with Flask.

## Overview

This project provides a single-file LAN file transfer server with a modern web UI, resumable chunked uploads, real-time transfer tracking, and enterprise audit logging.

### Key Features

- Local network file sharing with no internet dependency
- Drag-and-drop upload support
- Chunked uploads with resume capability
- Download support with range requests
- Create, rename, delete files and folders
- Enterprise-grade file logging and audit trail
- Live transfer dashboard with active uploads and history
- Secure path handling to prevent directory traversal

## Installation

1. Install Python 3.11+.
2. Install dependencies:

```bash
python -m pip install flask werkzeug
```

3. Update the `SHARE_FOLDER` path in `lan_transfer.py` to the directory you want to share.

## Running

Start the server:

```bash
python lan_transfer.py
```

Open the server in your browser at:

- `http://127.0.0.1:8000`
- or use the local LAN IP printed by the app

## Configuration

Edit the top of `lan_transfer.py`:

- `SHARE_FOLDER` — shared directory path
- `HOST` — bind address (default `0.0.0.0`)
- `PORT` — port number (default `8000`)
- `CHUNK_SIZE` — upload chunk size
- `SECRET_KEY` — Flask secret key

## Logging and Audit

The application writes logs into:

- `lan_transfer.log` — application log file
- `lan_transfer_audit.log` — structured audit trail

If the primary folder is not writable, logs automatically fall back to the system temp directory.

## GitHub Publishing

This repository is intended to be published as a public GitHub repository using the GitHub CLI.

## License

This project is provided as-is for internal LAN file transfer use. Update the license as needed.

# ProSFTP Transfer — GUI

A lightweight **SFTP file transfer GUI** built with **PySide6 (Qt)** + **Paramiko**.  
Browse local files, browse remote SFTP directories, and **upload/download files or whole folders** with optional **tar.gz compress/extract** workflows.


---

## Features

- ✅ Password-based SSH/SFTP connect (Host/IP + Port + Username + Password)
- ✅ Local file browser (tree view)
- ✅ Remote file browser (table view) with:
  - directory navigation (double-click folders)
  - refresh + go up
  - create remote directories (`mkdir -p`)
- ✅ Upload:
  - selected file(s) → remote directory
  - selected folder → compress to `.tar.gz`, upload, and optionally **extract on remote**
- ✅ Download:
  - selected file(s) → local directory
  - selected folder → create remote `.tar.gz`, download, and optionally **extract locally**
- ✅ Progress bar + status log
- ✅ Cancel transfer (best-effort)
- ✅ Dark UI theme

---

## Requirements

- **Python 3.10+**
- Remote server must support **SSH + SFTP**
- For folder transfers (compress/extract), remote needs:
  - `tar` available
  - permission to create files in `/tmp` (used for remote archives)

---

## Installation

### 1) Create & activate a virtual environment (recommended)

**Windows (PowerShell):**
```bash
py -m venv .venv
.\.venv\Scripts\activate


**Linux/macOS:**

```bash
python3 -m venv .venv
source .venv/bin/activate
```

### 2) Install dependencies

```bash
pip install -U pip
pip install PySide6 paramiko
```

Optional `requirements.txt`:

```txt
PySide6
paramiko
```

---

## Run

```bash
python sftp_gui.py
```

Then:

1. Enter **Host**, **Port**, **Username**, **Password**
2. Click **Connect**
3. Use:

   * **Upload Selected File(s) →**
   * **Upload Selected Folder →**
   * **← Download Selected File(s)**
   * **← Download Selected Folder**

---

## How folder transfers work

### Upload folder

1. Locally creates `folder_name.tar.gz`
2. Uploads archive to the chosen remote directory
3. If enabled: runs remote extraction:

   * `tar -xzf archive -C remote_dir`
   * removes the uploaded archive

### Download folder

1. Creates an archive on remote under `/tmp/...tar.gz`
2. Downloads the archive to your chosen local directory
3. Removes the remote temp archive
4. If enabled: extracts locally and removes the downloaded archive

---

## Security notes

* This tool currently uses `AutoAddPolicy()` for SSH host keys (it **auto-trusts** unknown hosts).
  This is convenient but **not ideal** for strong security (MITM risk). Consider improving it by using known_hosts verification if you plan to use it in sensitive environments.
* Authentication is **password-only** (no SSH key UI in current version).

---

## Troubleshooting

* **Auth failed / Permission denied**: verify username/password, ensure SSH password login is enabled.
* **Remote list fails**: check remote path permissions.
* **Folder extract fails**: ensure `tar` exists on remote and you have write permissions in target directory.
* **Windows path issues**: local paths are handled by Qt; remote paths are normalized to `/`.

---

## Roadmap ideas (optional)

* SSH key authentication support
* Known-hosts verification UI
* Rename/delete remote items
* Drag & drop transfers
* Resume support for large transfers

---
```
```


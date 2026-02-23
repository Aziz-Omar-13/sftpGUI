# ProSFTP Transfer — GUI

A lightweight **SFTP file transfer GUI** built with **PySide6 (Qt)** + **Paramiko**.  
Browse local files, browse remote SFTP directories, and **upload/download files or whole folders** with optional **tar.gz compress/extract** workflows.

Source: :contentReference[oaicite:0]{index=0}

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

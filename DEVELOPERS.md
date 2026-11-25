# Developer guide

[User README](README.md)

## Run locally

Use Windows and Python 3.12 with Tcl/Tk. The importer uses the Windows Portable Devices (WPD) API through `ctypes` and stores its state in SQLite. No third-party Python package is required to copy and hash files.

- Pillow is optional for EXIF capture dates and basic validation of supported images.
- `ffprobe` on PATH is optional for QuickTime capture dates and video stream checks.
- `exiftool` on PATH is optional for broader embedded metadata support, including HEIC and RAW.

```powershell
python -B app.py
python -B app.py --help
python -B -m unittest discover -s tests -v
```

The tests run on Windows with a fake USB device and temporary archive folders. They cover older arrivals, duplicate bytes, filename collisions, changed device IDs, spot-check mismatches, full USB checks, corrupt or missing local files, partial transfers, pause and resume, sidecars, and migration.

## Command line

```powershell
python -B app.py scan --archive 'C:\galary_backup\PhotoArchive'
python -B app.py import --archive 'C:\galary_backup\PhotoArchive'
python -B app.py import --full-usb-check --archive 'C:\galary_backup\PhotoArchive'
python -B app.py verify --archive 'C:\galary_backup\PhotoArchive'
python -B app.py move-previous --destination 'C:\galary_backup\PhotoArchive'
```

`scan` previews the visible inventory without importing media. `verify` checks the SQLite database and rehashes the files it tracks. `move-previous` has a fixed source of `Documents\PhotoArchive` for earlier installations.

## Code map

| File | Responsibility |
| --- | --- |
| [`app.py`](app.py) | Tkinter window, CLI, optional Windows startup, connection polling, and progress events from background workers. GUI preferences live in `%LOCALAPPDATA%\iPhoneArchive\settings.json`. |
| [`wpd.py`](wpd.py) | Read-only COM bindings for device discovery, object enumeration, properties, and file streams. WPD object IDs address files in the current connection. |
| [`archive.py`](archive.py) | Date ordering and folder selection, transfer and hash checks, duplicate detection, SQLite history, logging, and archive moves. |
| [`tests/test_archive.py`](tests/test_archive.py) | Import and recovery tests with a fake device for deterministic reads and failures. |

## Import flow and folder layout

1. `inventory` enumerates every object exposed by the iPhone. `ordered_groups` sorts by the dates available from WPD before any file is downloaded. It groups files by source folder and related filename stem, including Apple-style edited names.
2. `known_file` matches the device, source path, size, two timestamps, and persistent object ID against the history. If the ID differs and the other properties identify one saved file, `prefix_matches` compares up to 256 KiB. A failed sample or ambiguous history triggers a complete read. `--full-usb-check` bypasses the skip paths.
3. `receive` streams into a `.part` file under `_archive`, checks the reported byte count when available, flushes the file, and rereads it to verify SHA-256. The app removes temporary data on normal failure or pause. Completed records remain available for the next run.
4. `inspect_file` extracts an embedded date where supported and falls back to WPD creation time, then modification time. `date_folder` produces `YYYY/MM-Month` or `Unknown Date`. Later members of a recognised group follow the first archived member's folder. The history records each saved file's selected timestamp and source.
5. `store` deduplicates by the full SHA-256 digest and publishes without overwriting unrelated files. A missing copy can be restored to the recorded path. Same-name files with different contents acquire a digest suffix.

An archive lock prevents simultaneous imports or verification on the same destination. Verification compares the locally read bytes with the stored digest. During import, that digest is computed from the bytes supplied by WPD. The API provides no independent checksum from the iPhone's storage. A spot check is an optimisation for matching archive history, with full USB checks available for complete comparisons.

## Archive state

`_archive/history.sqlite3` stores three sets of records.

- `files` stores verified content by SHA-256, its relative archive path, size, selected date, metadata and import time.
- `sightings` maps device and source observations to archived hashes. Device-scoped matching uses the reported path, object ID and timestamps.
- `disk_checks` records the file identity and modification time from the most recent successful check. A change triggers a new disk hash check on a later import. **Verify archive** always rehashes every tracked file.

Keep `_archive` with the media. Same-volume migration verifies the source, renames the directory, and verifies it at its new location. If verification fails after the rename, migration attempts to move the directory back. For a different drive, copy the archive and verify the copy before switching to it.

Use an empty destination folder or a temporary archive for development and keep personal media, logs, and SQLite history out of Git.

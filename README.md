# iPhone Archive for Windows

Open **Start iPhone Archive.cmd**. Choose a local folder or SSD, then use **Check iPhone** for a preview or **Import now** to start copying. Leave **Keep Originals** enabled in the iPhone’s Photos settings. Connect by USB, unlock the phone, and approve Trust if prompted.

Enable **Import when iPhone connects** to check every new connection while the app is open. **Start with Windows** opens it at sign-in. Both switches are off initially. **Pause** keeps completed files and restarts any incomplete file from the beginning on the next import.

## Folders and verification

- Files are queued from oldest to newest using the dates visible before copying. The app uses embedded capture dates where it can read them and otherwise uses the iPhone’s dates. Undated files go into `Unknown Date`. Matching stills, motion clips, edited versions, and sidecars are grouped by their source folder and Apple filename where available. The app copies all files the iPhone exposes, including unfamiliar extensions.
- Each connection scans the whole visible inventory. The app reuses a history match when the device, source path, persistent ID, size, and both device timestamps match. This iPhone sometimes reports new IDs. When only the ID changes and the other properties identify a single archived file, the app also compares up to the first 256 KiB over USB. A different sample or ambiguous match triggers a complete USB read and SHA-256 comparison. Missing local files are eligible for reimport. The app rehashes an archived file when its local identity or modification time changes.
- For a full comparison with the files currently on the iPhone, use **Archive → Import with full USB checks**. It rereads matching files in full, checks the saved copies and imports any new files it finds. It takes longer than an incremental import. A quick USB sample can miss a difference later in a file, and Windows does not provide an iPhone-side SHA-256 checksum.
- For each transfer, the app checks the reported byte count when present and matches the SHA-256 hash of the received USB bytes to the saved file. **Verify archive** recomputes every stored hash, including files whose size and timestamps did not change. Corruption or an incomplete transfer produces an error. The iPhone is read-only throughout.
- The archive’s `_archive` folder contains the SQLite history and `import.log`. Keep it with the media when copying an archive to another drive. Verify both separate copies before manually deleting anything from the iPhone.

Python 3.12 and Tk are required for the desktop app. Pillow and `ffprobe` can inspect common image and video files. ExifTool is optional for reading more embedded dates in formats such as HEIC and RAW. Original bytes and embedded metadata are kept whether or not these inspection tools are installed. Coverage is limited to files the iPhone exposes over USB.

## Move the earlier archive

**Move Earlier Archive.cmd** moves the existing `Documents\PhotoArchive` folder to `C:\galary_backup\PhotoArchive`. The operation verifies the saved files both before and after the move, keeps the history with the files, and refuses an existing destination. Close other archive windows first. The desktop menu also offers **Archive → Move earlier archive…** if you want to pick a different parent folder on the same drive. For a different physical drive, copy the entire folder and use **Verify archive** on the destination.

## Command line

```powershell
python -B app.py scan --archive 'C:\galary_backup\PhotoArchive'
python -B app.py import --archive 'C:\galary_backup\PhotoArchive'
python -B app.py verify --archive 'C:\galary_backup\PhotoArchive'
python -B app.py move-previous
python -B -m unittest discover -s tests -v
```

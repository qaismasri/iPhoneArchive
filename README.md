# iPhone Archive for Windows

Copy photos and videos from your iPhone to a Windows PC or external drive over USB. The app saves the files it receives without converting them and never deletes anything from your iPhone.

## Get started

1. Install Python 3.12 with Tcl/Tk, and make sure `python` is on your Windows PATH.
2. On the iPhone, choose **Settings → Apps → Photos → Transfer to Mac or PC → Keep Originals**.
3. Connect the iPhone with a USB data cable. Unlock it and tap **Trust** if asked.
4. Double-click [Start iPhone Archive.cmd](Start%20iPhone%20Archive.cmd). Click **Choose…** to select your archive folder or a folder on an external drive.
5. Click **Check iPhone** to preview what needs to be read, or **Import now** to start copying. Click **Open folder** to browse your saved files in File Explorer.

The default folder is `C:\galary_backup\PhotoArchive`. If the earlier `Documents\PhotoArchive` exists and the new default folder does not, the app opens the earlier archive.

## How files are organised

The app queues items from oldest to newest using the dates the iPhone reports before copying. In the archive, files are organised **by year, then month**.

```text
PhotoArchive/
├── 2024/
│   └── 12-December/
│       └── IMG_0123.HEIC
├── 2025/
│   └── 06-June/
│       ├── IMG_0456.HEIC
│       └── IMG_0456.MOV
├── Unknown Date/
└── _archive/
```

The app prefers the capture date embedded in the file, then falls back to the creation or modification date reported by the phone. It records the date and where it came from. Standalone files with no usable date go into `Unknown Date`. Recognised Live Photo components, edited versions, and sidecars are kept in the same folder when the phone exposes them. The archive covers the files available over USB, including their embedded metadata.

## Avoiding duplicates and checking your files

| Control | What it does |
| --- | --- |
| **Check iPhone** | Scans the visible library and previews what matches your history or needs to be read. Older items are checked on every scan. |
| **Import now** | Skips known copies, spot-checks likely matches after the iPhone changes file IDs, and fully reads files it cannot confidently match. Identical content is stored once. Different files with the same name are kept separately. |
| **Verify archive** | Recalculates the SHA-256 hash of every saved file and reports missing or damaged copies. You can run it with the phone disconnected. |
| **Archive → Import with full USB checks** | Reads matching files from the phone in full, checks the corresponding saved copies, and imports new files. This takes longer than a normal import. |

Every new transfer is checked against the bytes received over USB before it enters the archive. A spot check compares the start of a file. Use full USB checks when you want a complete comparison with the files the iPhone currently exposes.

## Automatic imports and recovery

- Turn on **Import when iPhone connects** to start an import when the phone connects while the app is open. Turn on **Start with Windows** to open the app at sign-in. Both switches start off.
- Click **Pause** if you need to stop. Completed files stay recorded. The next import restarts any incomplete file from the beginning.
- If the phone is connected but nothing appears, unlock it and reconnect it while unlocked. Approve **Trust** if asked. Choose **Archive → Open import log** to see transfer details or errors.
- The app can read more embedded dates if Pillow, `ffprobe`, or ExifTool is installed. ExifTool is useful for HEIC and RAW photos. These tools are optional for copying and checksum verification.

## Use another drive

Finish or pause the import, then copy the entire `PhotoArchive` folder to the new drive, including `_archive`. That folder contains the import history and log. Choose the copied folder in the app and run **Verify archive** there. Keep two verified copies on separate drives before manually removing anything from the iPhone.

For the earlier `Documents\PhotoArchive` from the first version of this app, [Move Earlier Archive.cmd](Move%20Earlier%20Archive.cmd) moves it to `C:\galary_backup\PhotoArchive` on the same drive and verifies it before and after the move.

## Developer documentation

See the [developer guide](DEVELOPERS.md) for the CLI, architecture, and test commands.

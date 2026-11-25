"""Copy and verify iPhone files using a portable SQLite archive history."""

from calendar import month_name
from collections import defaultdict
from contextlib import closing, contextmanager
from datetime import datetime, timezone
import hashlib
import json
import logging
import msvcrt
import os
from pathlib import Path
import re
import shutil
import sqlite3
import subprocess
import tempfile
import time

from wpd import WpdError, open_iphone


PREVIOUS = Path.home() / 'Documents' / 'PhotoArchive'
REQUESTED = Path('C:/galary_backup/PhotoArchive')
HISTORY = Path('_archive/history.sqlite3')
VIDEO = {'.mov', '.mp4', '.m4v', '.avi', '.mkv', '.mts', '.3gp', '.webm'}
STILL = {'.heic', '.heif', '.jpg', '.jpeg', '.png', '.gif', '.dng', '.tiff', '.tif', '.webp', '.avif'}
PILLOW_FORMATS = {'.jpg', '.jpeg', '.png', '.gif', '.tiff', '.tif', '.webp'}
SIDECAR = {'.aae', '.xmp', '.thm'}
RESERVE = 512 * 1024**2
LOG = logging.getLogger('iphone_archive')


class IntegrityError(OSError):
    pass


class Paused(Exception):
    pass


def quiet(*args):
    pass


def checkpoint(stop):
    if stop and stop.is_set():
        raise Paused('Paused. Completed imports are kept.')


def default_archive():
    return PREVIOUS if PREVIOUS.is_dir() and not REQUESTED.is_dir() else REQUESTED


def validate_archive(root):
    root = root.expanduser().resolve()
    project = Path(__file__).resolve().parent
    if root == project or project in root.parents:
        raise OSError('Choose an archive folder outside the Git project.')
    if root == Path(root.anchor):
        raise OSError('Choose a folder inside the drive for the photo archive.')
    return root


def archive_path(root, relative):
    path = root / relative
    if not path.resolve().is_relative_to(root):
        raise IntegrityError(f'Archive path points outside the archive: {relative}')
    return path


@contextmanager
def lock_archive(root, create=False):
    state = archive_path(root, '_archive')
    if create:
        state.mkdir(parents=True, exist_ok=True)
    with (state / 'archive.lock').open('a+b') as lock:
        try:
            msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
        except OSError as error:
            raise OSError('This archive is already open for an import or check.') from error
        yield state


def prepare_history(db):
    db.executescript('''
        CREATE TABLE IF NOT EXISTS files (
            sha256 TEXT PRIMARY KEY,
            path TEXT NOT NULL UNIQUE,
            bytes INTEGER NOT NULL,
            capture_date TEXT,
            date_source TEXT NOT NULL,
            metadata TEXT NOT NULL,
            imported_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS sightings (
            device TEXT NOT NULL,
            source_path TEXT NOT NULL,
            sha256 TEXT NOT NULL REFERENCES files(sha256),
            persistent_id TEXT,
            source_created TEXT,
            source_modified TEXT,
            second_usb_read INTEGER NOT NULL DEFAULT 0,
            PRIMARY KEY (device, source_path, sha256)
        );
        CREATE TABLE IF NOT EXISTS disk_checks (
            sha256 TEXT PRIMARY KEY REFERENCES files(sha256),
            modified_ns INTEGER NOT NULL,
            file_id TEXT NOT NULL
        );
    ''')


def date_text(value):
    return value.isoformat() if value else None


def parse_date(value):
    if not value:
        return None
    if isinstance(value, str):
        value = re.sub(r'^(\d{4}):(\d{2}):(\d{2})', r'\1-\2-\3', value.strip())
        try:
            value = datetime.fromisoformat(value)
        except ValueError:
            return None
    if 1980 <= value.year <= datetime.now().year + 1:
        return value
    return None


def source_date(item):
    return parse_date(item.created) or parse_date(item.modified)


def extension(item):
    return Path(item.name).suffix.lower()


def ordered_groups(items):
    groups = defaultdict(list)
    for item in items:
        stem = Path(item.name).stem.casefold()
        stem = re.sub(r'^img_e(\d+)$', r'img_\1', stem)
        groups[item.folders, stem].append(item)
    def order_date(item):
        date = source_date(item)
        return date.replace(tzinfo=None) if date else datetime.max

    groups = [sorted(group, key=lambda item: (
        0 if extension(item) in STILL else 2 if extension(item) in SIDECAR else 1,
        order_date(item),
        item.name.casefold(), item.object_id)) for group in groups.values()]
    return sorted(groups, key=lambda group: (
        next((order_date(item) for item in group if source_date(item)), datetime.max),
        group[0].source_path.casefold()))


def load_source_index(db, phone):
    device = hashlib.sha256(phone.device_id.encode()).hexdigest()
    index = defaultdict(list)
    if db is not None:
        for path, persistent, created, modified, size, digest, target in db.execute('''
            SELECT source_path, persistent_id, source_created, source_modified, bytes, sha256, path
            FROM sightings JOIN files USING (sha256) WHERE device = ?
        ''', (device,)):
            if persistent and created and modified and size:
                index[path, persistent, created, modified, size].append((digest, target))
    return device, index


def known_file(item, index):
    matches = index.get((item.source_path, item.persistent_id, date_text(item.created),
                         date_text(item.modified), item.size), [])
    return matches[0] if len(matches) == 1 else None


def hash_file(path, stop=None):
    digest = hashlib.sha256()
    with path.open('rb') as stream:
        while data := stream.read(4 * 1024**2):
            checkpoint(stop)
            digest.update(data)
    return digest.hexdigest()


def check_file(path, size, digest, stop=None):
    if path.stat().st_size != size or hash_file(path, stop) != digest:
        raise IntegrityError(f'Size or SHA-256 mismatch for {path}. The file was left in place.')


def quick_check(db, path, size, digest, stop):
    stat = path.stat()
    if stat.st_size != size:
        raise IntegrityError(f'Size mismatch for {path}. Run Verify archive for a full check.')
    snapshot = (stat.st_mtime_ns, str(stat.st_ino))
    saved = db.execute('SELECT modified_ns, file_id FROM disk_checks WHERE sha256=?', (digest,)).fetchone()
    if snapshot != saved:
        check_file(path, size, digest, stop)
        remember_disk(db, digest, snapshot)
        db.commit()


def remember_disk(db, digest, snapshot):
    db.execute('INSERT OR REPLACE INTO disk_checks VALUES (?, ?, ?)', (digest, *snapshot))


def inspect_file(path, item, tools):
    data = {}
    tags = []
    suffix = extension(item)
    if tools['exiftool']:
        result = run_metadata([tools['exiftool'], '-j', '-G1', '-DateTimeOriginal',
                               '-OffsetTimeOriginal', '-CreationDate', '-CreateDate', '-FileType', str(path)])
        if result.returncode == 0 and result.stdout.strip():
            data['exiftool'] = json.loads(result.stdout)[0]
            exif = data['exiftool']
            exif.pop('SourceFile', None)
            for group in ('ExifIFD', 'IFD0', 'XMP-exif', 'Keys', 'UserData', 'QuickTime'):
                for tag in ('DateTimeOriginal', 'CreationDate', 'CreateDate'):
                    value = exif.get(f'{group}:{tag}')
                    if value and tag == 'DateTimeOriginal' and exif.get('ExifIFD:OffsetTimeOriginal'):
                        value += exif['ExifIFD:OffsetTimeOriginal']
                    tags.append((value, f'embedded {group} {tag}'))
    if suffix in VIDEO and tools['ffprobe']:
        result = run_metadata([tools['ffprobe'], '-v', 'error', '-show_entries',
                               'format=duration:format_tags=creation_time,com.apple.quicktime.creationdate:'
                               'stream=codec_name,codec_type,width,height:stream_tags=creation_time',
                               '-of', 'json', str(path)])
        if result.returncode or result.stderr.strip():
            raise IntegrityError(f'The received video {item.name} could not be opened: {result.stderr.strip()}')
        data['ffprobe'] = json.loads(result.stdout)
        details = data['ffprobe']
        if not any(stream.get('codec_type') == 'video' for stream in details.get('streams', [])):
            raise IntegrityError(f'The received file {item.name} has no readable video stream.')
        values = details.get('format', {}).get('tags', {})
        tags[:0] = [(values.get('com.apple.quicktime.creationdate'), 'embedded QuickTime capture date'),
                    (values.get('creation_time'), 'embedded container creation time')]
    if suffix in PILLOW_FORMATS:
        try:
            from PIL import Image
        except ImportError:
            Image = None
        if Image:
            try:
                with Image.open(path) as image:
                    base = image.getexif()
                    exif = base.get_ifd(34665)
                    value = exif.get(36867) or base.get(36867)
                    offset = exif.get(36881) or base.get(36881)
                    if value and offset:
                        value += offset
                    tags.insert(0, (value, 'embedded EXIF capture date'))
                    data['image'] = {'width': image.width, 'height': image.height}
                    image.verify()
            except (OSError, SyntaxError, ValueError) as error:
                raise IntegrityError(f'The received image {item.name} could not be opened: {error}') from error
    for value, source in tags + [(item.created, 'device object creation time'),
                                 (item.modified, 'device object modification time')]:
        if date := parse_date(value):
            return date, source, data
    return None, 'unknown', data


def run_metadata(command):
    return subprocess.run(command, capture_output=True, encoding='utf-8', errors='replace',
                          timeout=60, creationflags=subprocess.CREATE_NO_WINDOW, check=False)


def date_folder(date):
    return Path(str(date.year), f'{date.month:02}-{month_name[date.month]}') if date else Path('Unknown Date')


def safe_name(name):
    value = re.sub(r'[<>:"/\\|?*\x00-\x1f]', '_', name).strip().rstrip('.') or 'media'
    path = Path(value)
    if path.is_reserved():
        path = Path('_' + path.name)
    return path.stem[:150] + path.suffix[:20]


def publish(temp, root, folder, name, size, digest, stop):
    directory = archive_path(root, folder)
    directory.mkdir(parents=True, exist_ok=True)
    stem, suffix = Path(name).stem, Path(name).suffix
    for candidate in (name, f'{stem}__{digest[:12]}{suffix}', f'{stem}__{digest}{suffix}'):
        target = archive_path(root, folder / candidate)
        try:
            temp.rename(target)  # Windows rename fails if the target already exists.
            return target
        except FileExistsError:
            if target.is_file() and target.stat().st_size == size and hash_file(target, stop) == digest:
                return target
    raise IntegrityError(f'Could not store {name} without replacing an existing file.')


def receive(phone, item, state, emit, stop, consume):
    free = shutil.disk_usage(state).free
    if free < (item.size or 1) + RESERVE:
        raise OSError(f'Insufficient space for {item.name}. Choose a larger archive drive.')
    digest = hashlib.sha256()
    count = 0
    last = 0.0
    with tempfile.NamedTemporaryFile(dir=state, suffix='.part', delete=False) as output:
        temp = Path(output.name)
        try:
            with closing(phone.stream(item)) as chunks:
                for data in chunks:
                    checkpoint(stop)
                    count += len(data)
                    if item.size is not None and count > item.size:
                        raise IntegrityError(f'{item.name} is larger than the size reported by the iPhone.')
                    if count + RESERVE > free:
                        raise OSError(f'Insufficient space to finish writing {item.name}.')
                    output.write(data)
                    digest.update(data)
                    now = time.monotonic()
                    if now - last >= 0.20:
                        emit('bytes', (count, item.size))
                        last = now
            if count == 0 or (item.size is not None and count != item.size):
                raise IntegrityError(f'Incomplete transfer of {item.name}: received {count:,} of {item.size} bytes.')
            output.flush()
            os.fsync(output.fileno())
            output.close()
            emit('status', f'Verifying {item.name}')
            check_file(temp, count, digest.hexdigest(), stop)
            return consume(temp, count, digest.hexdigest())
        finally:
            output.close()
            temp.unlink(missing_ok=True)


def store(db, root, device, item, temp, size, digest, group_folder, tools, stop):
    existing = db.execute('SELECT path FROM files WHERE sha256=?', (digest,)).fetchone()
    created = False
    if existing:
        target = archive_path(root, existing[0])
        try:
            quick_check(db, target, size, digest, stop)
        except FileNotFoundError:
            checkpoint(stop)
            target.parent.mkdir(parents=True, exist_ok=True)
            temp.rename(target)
            created = True
    else:
        capture, origin, metadata = inspect_file(temp, item, tools)
        checkpoint(stop)
        target = publish(temp, root, group_folder or date_folder(capture), safe_name(item.name),
                         size, digest, stop)
        created = True
    stat = target.stat()
    relative = target.relative_to(root)
    with db:
        if not existing:
            db.execute('INSERT INTO files VALUES (?, ?, ?, ?, ?, ?, ?)',
                       (digest, relative.as_posix(), size, date_text(capture), origin,
                        json.dumps(metadata), datetime.now(timezone.utc).isoformat()))
        db.execute('''INSERT INTO sightings VALUES (?, ?, ?, ?, ?, ?, 0)
                      ON CONFLICT(device, source_path, sha256) DO UPDATE SET
                      persistent_id=excluded.persistent_id, source_created=excluded.source_created,
                      source_modified=excluded.source_modified''',
                   (device, item.source_path, digest, item.persistent_id,
                    date_text(item.created), date_text(item.modified)))
        remember_disk(db, digest, (stat.st_mtime_ns, str(stat.st_ino)))
    LOG.info('%s %s · %s bytes · SHA-256 %s', 'Saved' if created else 'Duplicate', relative, size, digest)
    return relative.parent, created


def inventory(phone, emit, stop=None):
    items = []
    emit('status', 'Reading the iPhone inventory')
    with closing(phone.list_media(on_notice=lambda notice: emit('log', notice))) as entries:
        for item in entries:
            checkpoint(stop)
            items.append(item)
            if len(items) % 128 == 0:
                emit('status', f'Reading the iPhone inventory · {len(items):,} files')
    if not items:
        raise WpdError('No media is accessible. Unlock the iPhone, reconnect it, and tap Trust if prompted.')
    return ordered_groups(items)


def read_history(root):
    return sqlite3.connect(archive_path(root, HISTORY).as_uri() + '?mode=ro', uri=True)


def preview(root, emit=quiet, stop=None):
    root = validate_archive(root)
    with open_iphone() as phone:
        groups = inventory(phone, emit, stop)
        if (root / HISTORY).is_file():
            with closing(read_history(root)) as db:
                _, index = load_source_index(db, phone)
        else:
            index = {}
    pending = []
    known = 0
    for group in groups:
        for item in group:
            checkpoint(stop)
            match = known_file(item, index)
            if match and archive_path(root, match[1]).is_file():
                if archive_path(root, match[1]).stat().st_size != item.size:
                    raise IntegrityError(f'Size mismatch for {match[1]}. Run Verify archive for a full check.')
                known += 1
            else:
                pending.append(item)
    return known, pending


def import_phone(root, emit=quiet, stop=None):
    root = validate_archive(root)
    tools = {name: shutil.which(name) for name in ('exiftool', 'ffprobe')}
    if not tools['exiftool']:
        emit('log', 'ExifTool is unavailable. HEIC and RAW may use the date reported by the iPhone.')
    if not tools['ffprobe']:
        emit('log', 'ffprobe is unavailable. Videos will be hash-checked without opening their streams.')
    counts = dict(saved=0, skipped=0, duplicates=0)
    with lock_archive(root, create=True) as state:
        handler = logging.FileHandler(state / 'import.log', encoding='utf-8')
        handler.setFormatter(logging.Formatter('%(asctime)s %(levelname)s %(message)s'))
        LOG.setLevel(logging.INFO)
        LOG.addHandler(handler)
        try:
            with closing(sqlite3.connect(state / HISTORY.name)) as db, open_iphone() as phone:
                prepare_history(db)
                groups = inventory(phone, emit, stop)
                device, index = load_source_index(db, phone)
                total = sum(map(len, groups))
                position = 0
                for group in groups:
                    group_folder = None
                    for item in group:
                        checkpoint(stop)
                        position += 1
                        match = known_file(item, index)
                        if match:
                            digest, relative = match
                            path = archive_path(root, relative)
                            try:
                                quick_check(db, path, item.size, digest, stop)
                            except FileNotFoundError:
                                pass  # A missing archive file is eligible for reimport.
                            else:
                                counts['skipped'] += 1
                                group_folder = group_folder or Path(relative).parent
                                if position % 32 == 0 or position == total:
                                    emit('files', (position, total, counts.copy()))
                                continue
                        emit('status', f'Copying {item.name}')
                        emit('bytes', (0, item.size))
                        folder, created = receive(phone, item, state, emit, stop,
                            lambda temp, size, digest: store(db, root, device, item, temp, size, digest,
                                                             group_folder, tools, stop))
                        group_folder = group_folder or folder
                        counts['saved' if created else 'duplicates'] += 1
                        emit('log', f'{"Saved" if created else "Duplicate"} {item.name}')
                        emit('files', (position, total, counts.copy()))
        except Exception as error:
            LOG.error('%s', error)
            raise
        finally:
            LOG.info('Import progress: %s', counts)
            LOG.removeHandler(handler)
            handler.close()
    return counts


def verify(root, emit=quiet, stop=None):
    root = validate_archive(root)
    if not (root / HISTORY).is_file():
        raise OSError(f'Archive history was not found in {root}.')
    with lock_archive(root), closing(read_history(root)) as db:
        if db.execute('PRAGMA integrity_check').fetchone() != ('ok',):
            raise IntegrityError('The SQLite archive history failed its integrity check.')
        rows = db.execute('SELECT path, bytes, sha256 FROM files ORDER BY path').fetchall()
        if not rows:
            raise OSError('The archive has no recorded files to verify.')
        failures = []
        total_bytes = 0
        for position, (relative, size, digest) in enumerate(rows, 1):
            checkpoint(stop)
            emit('status', f'Verifying {Path(relative).name}')
            try:
                check_file(archive_path(root, relative), size, digest, stop)
            except OSError as error:
                failures.append(f'{relative}: {error}')
            else:
                total_bytes += size
            emit('files', (position, len(rows), {}))
        if failures:
            for failure in failures:
                emit('log', failure)
            raise IntegrityError(f'{len(failures)} of {len(rows)} files failed verification.\n' + '\n'.join(failures[:5]))
    return len(rows), total_bytes


def move_archive(source, destination, emit=quiet):
    source, destination = validate_archive(source), validate_archive(destination)
    if destination.exists():
        raise FileExistsError(f'The destination already exists: {destination}')
    if destination.is_relative_to(source) or not destination.parent.is_dir():
        raise OSError('Choose a destination inside an existing folder outside this archive.')
    if source.stat().st_dev != destination.parent.stat().st_dev:
        raise OSError('This move requires the same physical volume. For a different drive, copy the whole folder and verify the copy.')
    expected = verify(source, emit)
    emit('status', f'Moving the verified archive to {destination}')
    source.rename(destination)
    try:
        actual = verify(destination, emit)
        if actual != expected:
            raise IntegrityError('The destination inventory differs from the original inventory.')
    except (Exception, KeyboardInterrupt):
        try:
            destination.rename(source)
        except OSError as rollback:
            raise OSError(f'The files are at {destination}. Moving them back failed: {rollback}') from rollback
        raise
    return actual

from contextlib import contextmanager
from datetime import datetime
import hashlib
import os
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Event
import unittest
from unittest.mock import patch

import archive
from wpd import MediaObject


def media(name, day, content, *, object_id=None, persistent_id=None, size=None, folder='DCIM', month=5):
    date = datetime(2024, month, day)
    return (MediaObject(object_id or name, name, (folder,), len(content) if size is None else size,
                        persistent_id or name, date, date, False), content)


class Phone:
    device_id = 'test-phone'

    def __init__(self, *entries):
        self.entries = list(entries)
        self.reads = []
        self.after_chunk = None

    def list_media(self, **kwargs):
        for item, _ in self.entries:
            yield item

    def stream(self, item):
        self.reads.append(item.name)
        for candidate, content in self.entries:
            if candidate == item:
                yield content[:2]
                if self.after_chunk:
                    self.after_chunk(item)
                yield content[2:]
                return
        raise OSError('Disconnected')

    @contextmanager
    def open(self):
        yield self


class ArchiveTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / 'Archive'

    def run_import(self, phone, stop=None):
        with patch.object(archive, 'open_iphone', phone.open):
            return archive.import_phone(self.root, stop=stop)

    def test_scans_older_additions_and_skips_verified_files_without_usb(self):
        first = Phone(media('newer.bin', 20, b'newer'))
        self.assertEqual(self.run_import(first)['saved'], 1)
        second = Phone(media('newer.bin', 20, b'newer', object_id='different-session-id'),
                       media('old.bin', 2, b'old'), media('middle.bin', 10, b'middle'))
        with patch.object(archive, 'open_iphone', second.open):
            known, pending = archive.preview(self.root)
        self.assertEqual(known, 1)
        self.assertEqual([item.name for item in pending], ['old.bin', 'middle.bin'])
        self.assertEqual(second.reads, [])
        self.assertEqual(self.run_import(second), {'saved': 2, 'skipped': 1, 'duplicates': 0})
        self.assertEqual(second.reads, ['old.bin', 'middle.bin'])

    def test_deduplication_and_same_name_different_content(self):
        first = b'different contents one'
        second = b'different contents two'
        phone = Phone(media('same.bin', 1, first, folder='A'),
                      media('same.bin', 2, second, folder='B'),
                      media('repeat.bin', 3, first, folder='C'))
        self.assertEqual(self.run_import(phone), {'saved': 2, 'skipped': 0, 'duplicates': 1})
        target = self.root / '2024' / '05-May'
        self.assertEqual((target / 'same.bin').read_bytes(), first)
        suffix = hashlib.sha256(second).hexdigest()[:12]
        self.assertEqual((target / f'same__{suffix}.bin').read_bytes(), second)
        self.assertEqual(archive.verify(self.root), (2, len(first) + len(second)))

    def test_deleted_local_copy_is_restored(self):
        phone = Phone(media('missing.bin', 4, b'original'))
        self.run_import(phone)
        path = self.root / '2024' / '05-May' / 'missing.bin'
        path.unlink()
        phone.reads.clear()
        self.assertEqual(self.run_import(phone)['saved'], 1)
        self.assertEqual(phone.reads, ['missing.bin'])
        self.assertEqual(path.read_bytes(), b'original')

    def test_corruption_is_reported_and_left_in_place(self):
        phone = Phone(media('damaged.bin', 5, b'original'))
        self.run_import(phone)
        path = self.root / '2024' / '05-May' / 'damaged.bin'
        prior = path.stat()
        path.write_bytes(b'corrupt!')
        os.utime(path, ns=(prior.st_atime_ns, prior.st_mtime_ns + 1_000_000_000))
        phone.reads.clear()
        with self.assertRaisesRegex(archive.IntegrityError, 'SHA-256 mismatch'):
            self.run_import(phone)
        self.assertEqual(phone.reads, [])
        os.utime(path, ns=(prior.st_atime_ns, prior.st_mtime_ns))
        with self.assertRaisesRegex(archive.IntegrityError, 'SHA-256 mismatch'):
            archive.verify(self.root)
        self.assertEqual(path.read_bytes(), b'corrupt!')

    def test_interruption_keeps_completed_files_and_retries_current_file(self):
        phone = Phone(media('complete.bin', 1, b'complete'), media('retry.bin', 2, b'retry'))
        stop = Event()
        phone.after_chunk = lambda item: stop.set() if item.name == 'retry.bin' else None
        with self.assertRaises(archive.Paused):
            self.run_import(phone, stop)
        self.assertEqual(archive.verify(self.root), (1, 8))
        self.assertEqual(list((self.root / '_archive').glob('*.part')), [])
        phone.after_chunk = None
        phone.reads.clear()
        resumed = self.run_import(phone)
        self.assertEqual(resumed, {'saved': 1, 'skipped': 1, 'duplicates': 0})
        self.assertEqual(phone.reads, ['retry.bin'])

    def test_rejects_incomplete_transfer(self):
        phone = Phone(media('short.bin', 2, b'1234', size=6))
        with self.assertRaisesRegex(archive.IntegrityError, 'Incomplete transfer'):
            self.run_import(phone)
        self.assertFalse(list(self.root.rglob('short.bin')))
        self.assertEqual(list((self.root / '_archive').glob('*.part')), [])

    def test_sidecar_follows_primary_to_its_date_folder(self):
        photo, photo_bytes = media('IMG_1234.HEIC', 1, b'fake image bytes')
        motion, motion_bytes = media('IMG_1234.MOV', 2, b'fake video bytes', month=6)
        sidecar, sidecar_bytes = media('IMG_1234.AAE', 20, b'edit data', month=12)
        phone = Phone((sidecar, sidecar_bytes), (motion, motion_bytes), (photo, photo_bytes))
        with patch.object(archive.shutil, 'which', return_value=None):
            self.run_import(phone)
        self.assertEqual(phone.reads, ['IMG_1234.HEIC', 'IMG_1234.MOV', 'IMG_1234.AAE'])
        output = self.root / '2024' / '05-May'
        self.assertEqual((output / photo.name).read_bytes(), photo_bytes)
        self.assertEqual((output / motion.name).read_bytes(), motion_bytes)
        self.assertEqual((output / sidecar.name).read_bytes(), sidecar_bytes)

    def test_move_verifies_history_and_never_overwrites_a_destination(self):
        self.run_import(Phone(media('video.bin', 3, b'content')))
        occupied = self.root.parent / 'Occupied'
        occupied.mkdir()
        with self.assertRaises(FileExistsError):
            archive.move_archive(self.root, occupied)
        self.assertTrue(self.root.is_dir())
        destination = self.root.parent / 'Moved'
        self.assertEqual(archive.move_archive(self.root, destination), (1, 7))
        self.assertFalse(self.root.exists())
        self.assertEqual(archive.verify(destination), (1, 7))


if __name__ == '__main__':
    unittest.main()

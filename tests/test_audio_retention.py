import asyncio
import json
import os
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from brain.audio_retention import AudioRetention


class AudioRetentionTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name) / 'audio'
        self.root.mkdir()
        self.now = 2_000_000_000
        self.retention = AudioRetention(self.root, clock=lambda: self.now)

    def audio(self, number, *, age=8 * 86400, size=8, name=None):
        path = self.root / (name or f'1700000000_{number}_index.wav')
        path.write_bytes(b'x' * size)
        os.utime(path, (self.now - age, self.now - age))
        return path

    async def test_age_cleanup_preserves_pins_queue_recent_references_and_other_files(self):
        old = self.audio(1)
        pinned = self.audio(2)
        current = self.audio(3)
        queued = self.audio(4)
        fresh = self.audio(5, age=20)
        reference = self.audio(6)
        arbitrary = self.audio(7, name='reference.wav')
        nested = self.root / 'archive'
        nested.mkdir()
        nested_audio = nested / old.name
        nested_audio.write_bytes(b'archive')
        retention = AudioRetention(self.root, reference_paths=[reference], clock=lambda: self.now)
        await retention.pin(pinned.name)
        result = await retention.cleanup([current, queued.name])
        self.assertFalse(old.exists())
        self.assertEqual(result['deleted_files'], 1)
        for kept in (pinned, current, queued, fresh, reference, arbitrary, nested_audio):
            self.assertTrue(kept.exists(), kept.name)

    async def test_capacity_removes_oldest_but_does_not_evict_protected_or_fresh(self):
        oldest = self.audio(1, age=5000, size=10)
        newer = self.audio(2, age=4000, size=10)
        newest = self.audio(3, age=3000, size=10)
        retention = AudioRetention(self.root, max_bytes=20, clock=lambda: self.now)
        result = await retention.cleanup()
        self.assertFalse(oldest.exists())
        self.assertTrue(newer.exists())
        self.assertTrue(newest.exists())
        self.assertEqual(result['total_bytes'], 20)
        await retention.pin(newer.name)
        retention.max_bytes = 0
        retention.set_protected([newest])
        result = await retention.cleanup()
        self.assertEqual(result['deleted_files'], 0)
        self.assertEqual(result['total_bytes'], 20)

    async def test_fish_clips_are_cleaned_but_pinned_playing_and_reference_are_kept(self):
        clips = [self.audio(i, name=f'1700000000_{i:032x}_fish.wav') for i in range(4)]
        arbitrary = self.audio(5, name='1700000000_notgenerated_fish.wav')
        retention = AudioRetention(self.root, reference_paths=[clips[3]], clock=lambda: self.now)
        await retention.pin(clips[1].name)
        retention.set_protected([clips[2]])
        result = await retention.cleanup()
        self.assertEqual(result['deleted_files'], 1)
        self.assertFalse(clips[0].exists())
        for clip in (*clips[1:], arbitrary):
            self.assertTrue(clip.exists())

    async def test_pins_persist_across_restart_and_unpin_allows_cleanup(self):
        path = self.audio(1)
        await self.retention.pin(path.name)
        restarted = AudioRetention(self.root, clock=lambda: self.now)
        await restarted.cleanup()
        self.assertTrue(path.exists())
        self.assertTrue((await restarted.list_files())[0]['pinned'])
        await restarted.pin(path.name, False)
        await restarted.cleanup()
        self.assertFalse(path.exists())

    async def test_corrupt_or_traversing_manifest_fails_closed(self):
        path = self.audio(1)
        manifest = self.root / '.audio-pins.json'
        for content in ('broken', json.dumps({'version': 1, 'files': ['../1700000000_1.wav']})):
            manifest.write_text(content, encoding='utf-8')
            result = await self.retention.cleanup()
            self.assertTrue(path.exists())
            self.assertIsNotNone(result['last_error'])
            self.assertNotIn(str(self.root), str(result))

    async def test_successful_list_and_pin_do_not_hide_cleanup_failure(self):
        path = self.audio(1)
        with patch.object(Path, 'unlink', side_effect=PermissionError('private file path')):
            result = await self.retention.cleanup()
        failure = result['last_error']
        self.assertIsNotNone(failure)
        self.assertNotIn('private file path', failure)
        self.assertTrue(path.exists())
        await self.retention.list_files()
        self.assertEqual(self.retention.status()['last_error'], failure)
        await self.retention.pin(path.name)
        self.assertEqual(self.retention.status()['last_error'], failure)
        await self.retention.cleanup()
        self.assertIsNone(self.retention.status()['last_error'])

    async def test_transient_listing_error_clears_after_successful_listing(self):
        self.audio(1)
        with patch.object(self.retention, '_scan', side_effect=PermissionError()):
            with self.assertRaises(ValueError):
                await self.retention.list_files()
        self.assertIsNotNone(self.retention.status()['last_error'])
        await self.retention.list_files()
        self.assertIsNone(self.retention.status()['last_error'])

    async def test_disabled_cleanup_does_not_touch_files(self):
        path = self.audio(1)
        retention = AudioRetention(self.root, enabled=False, clock=lambda: self.now)
        await retention.cleanup()
        self.assertTrue(path.exists())
        self.assertIsNone(retention.status()['last_cleanup'])

    async def test_invalid_retention_limits_cannot_turn_into_unbounded_deletion(self):
        path = self.audio(1)
        for field in ('max_age_days', 'max_bytes', 'grace_seconds'):
            for value in (-1, float('nan'), float('inf')):
                with self.assertRaises(ValueError):
                    AudioRetention(self.root, **{field: value})
        self.assertTrue(path.exists())

    async def test_pin_rejects_traversal_absolute_paths_and_non_generated_names(self):
        path = self.audio(1)
        for name in ('../' + path.name, '..\\' + path.name, str(path), 'reference.wav',
                     '1700000000_1.wav:ads', '/audio/' + path.name):
            with self.assertRaises(ValueError):
                await self.retention.pin(name)
        with self.assertRaises(ValueError):
            await self.retention.pin(path.name, 'false')

    async def test_hard_links_are_never_deleted(self):
        outside = Path(self.temporary.name) / 'source.wav'
        outside.write_bytes(b'keep')
        os.utime(outside, (self.now - 8 * 86400,) * 2)
        link = self.root / '1700000000_1.wav'
        os.link(outside, link)
        await self.retention.cleanup()
        self.assertTrue(outside.exists())
        self.assertTrue(link.exists())

    async def test_symbolic_links_are_never_followed(self):
        outside = Path(self.temporary.name) / 'source.wav'
        outside.write_bytes(b'keep')
        link = self.root / '1700000000_1.wav'
        try:
            link.symlink_to(outside)
        except OSError:
            self.skipTest('OS does not permit creating symbolic links')
        await self.retention.cleanup()
        self.assertTrue(link.is_symlink())
        self.assertEqual(outside.read_bytes(), b'keep')
        with self.assertRaises(ValueError):
            await self.retention.pin(link.name)

    async def test_windows_reparse_file_and_root_are_rejected_without_link_privileges(self):
        path = self.audio(1)
        original_lstat = Path.lstat
        target = path

        def lstat(value, *args, **kwargs):
            info = original_lstat(value, *args, **kwargs)
            if value == target:
                return SimpleNamespace(st_mode=info.st_mode, st_file_attributes=0x400)
            return info

        with patch.object(Path, 'lstat', lstat):
            await self.retention.cleanup()
        self.assertTrue(path.exists())
        target = self.root
        with patch.object(Path, 'lstat', lstat):
            result = await self.retention.cleanup()
        self.assertTrue(path.exists())
        self.assertIsNotNone(result['last_error'])

    async def test_replaced_root_is_not_treated_as_the_original_audio_directory(self):
        path = self.audio(1)
        await self.retention.list_files()
        moved = self.root.parent / 'original-audio'
        self.root.rename(moved)
        self.root.mkdir()
        replacement = self.audio(1)
        result = await self.retention.cleanup()
        self.assertTrue(replacement.exists())
        self.assertTrue((moved / path.name).exists())
        self.assertIsNotNone(result['last_error'])

    async def test_files_changed_after_scan_are_not_deleted(self):
        path = self.audio(1)
        original_scan = self.retention._scan
        called = False

        def scan():
            nonlocal called
            files = original_scan()
            if not called:
                called = True
                path.write_bytes(b'a new synthesis replaced the old file')
            return files

        with patch.object(self.retention, '_scan', side_effect=scan):
            await self.retention.cleanup()
        self.assertTrue(path.exists())

    async def test_protection_updated_during_scan_is_observed_before_delete(self):
        path = self.audio(1)
        original_scan = self.retention._scan

        def scan():
            files = original_scan()
            self.retention.set_protected([path])
            return files

        with patch.object(self.retention, '_scan', side_effect=scan):
            await self.retention.cleanup()
        self.assertTrue(path.exists())

    async def test_listing_is_bounded_sorted_and_contains_no_absolute_paths(self):
        for number in range(1, 106):
            self.audio(number, age=number * 1000)
        files = await self.retention.list_files(1000)
        self.assertEqual(len(files), 100)
        self.assertEqual(files[0]['name'], '1700000000_1_index.wav')
        self.assertEqual(self.retention.status()['total_files'], 105)
        self.assertEqual(set(files[0]), {'name', 'bytes', 'modified_at', 'pinned', 'protected'})
        self.assertNotIn(str(self.root), str(files))

    async def test_cleanup_uses_worker_thread_and_serializes_other_instances(self):
        path = self.audio(1)
        original_scan = self.retention._scan
        threads = []

        def scan():
            threads.append(threading.get_ident())
            return original_scan()

        with self.retention._writer():
            other = AudioRetention(self.root, clock=lambda: self.now)
            await other.cleanup()
            self.assertTrue(path.exists())
            self.assertIsNotNone(other.status()['last_error'])
        with patch.object(self.retention, '_scan', side_effect=scan):
            await self.retention.cleanup()
        self.assertTrue(threads)
        self.assertNotIn(threading.get_ident(), threads)
        self.assertFalse(path.exists())

    async def test_separate_instances_preserve_each_others_pins(self):
        first, second = self.audio(1), self.audio(2)
        other = AudioRetention(self.root, clock=lambda: self.now)
        await self.retention.pin(first.name)
        await other.pin(second.name)
        await self.retention.cleanup()
        self.assertTrue(first.exists())
        self.assertTrue(second.exists())

    async def test_cancelled_async_cleanup_keeps_safe_writer_until_thread_finishes(self):
        entered, release = threading.Event(), threading.Event()
        original_scan = self.retention._scan

        def scan():
            entered.set()
            release.wait(2)
            return original_scan()

        with patch.object(self.retention, '_scan', side_effect=scan):
            task = asyncio.create_task(self.retention.cleanup())
            await asyncio.to_thread(entered.wait, 2)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
            release.set()
            await asyncio.to_thread(self.retention._io_lock.acquire)
            self.retention._io_lock.release()

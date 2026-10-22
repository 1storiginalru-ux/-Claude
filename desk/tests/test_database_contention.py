import copy
import sqlite3
import tempfile
import threading
from concurrent.futures import ThreadPoolExecutor
from io import StringIO
from pathlib import Path
from unittest.mock import patch
from django.conf import settings
from django.core.management import call_command
from django.db import OperationalError
from django.db.backends.sqlite3.base import DatabaseWrapper
from django.test import SimpleTestCase
from desk.db import is_database_busy
from .base import DeskCase


class SQLiteConcurrencyTests(SimpleTestCase):
    def connect(self, path):
        configuration = copy.deepcopy(settings.DATABASES['default'])
        configuration['NAME'] = str(path)
        configuration['OPTIONS']['timeout'] = 3
        connection = DatabaseWrapper(configuration, alias='isolated-contention-test')
        connection.ensure_connection()
        return connection

    def test_reader_does_not_block_writer_commit(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'concurrency.sqlite3'
            writer = self.connect(path)
            reader = self.connect(path)
            try:
                writer.connection.execute('CREATE TABLE counter (value INTEGER)')
                writer.connection.execute('INSERT INTO counter VALUES (0)')
                reader.connection.execute('BEGIN DEFERRED')
                self.assertEqual(reader.connection.execute('SELECT value FROM counter').fetchone()[0], 0)
                writer.set_autocommit(False, force_begin_transaction_with_broken_autocommit=True)
                writer.connection.execute('UPDATE counter SET value = value + 1')
                writer.commit()
                writer.set_autocommit(True)
                # A pinned reader keeps its snapshot while the write commits.
                self.assertEqual(reader.connection.execute('SELECT value FROM counter').fetchone()[0], 0)
                reader.connection.rollback()
                self.assertEqual(reader.connection.execute('SELECT value FROM counter').fetchone()[0], 1)
            finally:
                reader.close()
                writer.close()

    def test_read_then_write_transactions_wait_instead_of_failing_upgrade(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'concurrency.sqlite3'
            setup = self.connect(path)
            setup.connection.execute('CREATE TABLE counter (value INTEGER)')
            setup.connection.execute('INSERT INTO counter VALUES (0)')
            setup.close()
            opened = threading.Barrier(2)
            first_read = threading.Event()
            second_attempt = threading.Event()
            second_written = threading.Event()

            def first_writer():
                connection = self.connect(path)
                try:
                    opened.wait(timeout=5)
                    connection.set_autocommit(False, force_begin_transaction_with_broken_autocommit=True)
                    connection.connection.execute('SELECT value FROM counter').fetchone()
                    first_read.set()
                    self.assertTrue(second_attempt.wait(timeout=5))
                    # With DEFERRED, the second writer advances first and this
                    # snapshot can't upgrade. IMMEDIATE makes it wait at BEGIN.
                    second_written.wait(timeout=0.2)
                    connection.connection.execute('UPDATE counter SET value = value + 1')
                    connection.commit()
                    connection.set_autocommit(True)
                finally:
                    connection.close()

            def second_writer():
                connection = self.connect(path)
                try:
                    opened.wait(timeout=5)
                    self.assertTrue(first_read.wait(timeout=5))
                    second_attempt.set()
                    connection.set_autocommit(False, force_begin_transaction_with_broken_autocommit=True)
                    connection.connection.execute('SELECT value FROM counter').fetchone()
                    connection.connection.execute('UPDATE counter SET value = value + 1')
                    second_written.set()
                    connection.commit()
                    connection.set_autocommit(True)
                finally:
                    connection.close()

            with ThreadPoolExecutor(max_workers=2) as pool:
                futures = [pool.submit(first_writer), pool.submit(second_writer)]
                for future in futures: future.result(timeout=10)
            result = self.connect(path)
            try:
                self.assertEqual(result.connection.execute('SELECT value FROM counter').fetchone()[0], 2)
            finally:
                result.close()

    def test_only_contention_is_classified_as_busy(self):
        self.assertTrue(is_database_busy(OperationalError('database is locked')))
        cause = sqlite3.OperationalError('snapshot conflict')
        cause.sqlite_errorcode = sqlite3.SQLITE_BUSY_SNAPSHOT
        error = OperationalError('wrapped SQLite failure')
        error.__cause__ = cause
        self.assertTrue(is_database_busy(error))
        self.assertFalse(is_database_busy(OperationalError('no such table: desk_analysis')))


class BackgroundContentionTests(DeskCase):
    @patch('desk.management.commands.sync_channels.time.sleep', side_effect=[None, KeyboardInterrupt])
    @patch('desk.management.commands.sync_channels.sync_account', side_effect=[OperationalError('database is locked'), 0])
    def test_poller_survives_busy_database_and_tries_next_cycle(self, sync, sleep):
        self.ym.enabled = False
        self.ym.save()
        output = StringIO()
        call_command('sync_channels', '--loop', stdout=output, stderr=output)
        self.assertEqual(sync.call_count, 2)
        self.assertIn('База временно занята', output.getvalue())
        self.assertIn('WB1: 0 объектов', output.getvalue())

    @patch('desk.management.commands.worker.time.sleep', side_effect=[None, KeyboardInterrupt])
    @patch('desk.management.commands.worker.process_one', side_effect=[OperationalError('database is locked'), False])
    def test_worker_survives_busy_database(self, process, sleep):
        output = StringIO()
        call_command('worker', stdout=output, stderr=output)
        self.assertEqual(process.call_count, 2)
        self.assertIn('База временно занята', output.getvalue())

    @patch('desk.management.commands.worker.process_one', side_effect=OperationalError('no such table: desk_outgoing'))
    def test_worker_does_not_hide_other_database_errors(self, process):
        with self.assertRaisesMessage(OperationalError, 'no such table'):
            call_command('worker', '--once')

    @patch('desk.management.commands.sync_channels.sync_account', side_effect=OperationalError('no such table: desk_analysis'))
    def test_poller_does_not_hide_other_database_errors(self, sync):
        with self.assertRaisesMessage(OperationalError, 'no such table'):
            call_command('sync_channels')

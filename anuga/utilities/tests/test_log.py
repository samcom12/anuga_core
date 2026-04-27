"""Tests for the log module changes introduced in this PR.

Tests cover:
- TeeStream: write to both terminal and file, flush, close, proxy attributes
- set_logfile: creates log file, replaces sys.stdout with TeeStream
- file_only: context manager that suppresses terminal output
- New exports from anuga.__init__: set_logfile, TeeStream
"""
import io
import os
import sys
import tempfile
import unittest


class Test_TeeStream(unittest.TestCase):
    """Tests for the TeeStream class."""

    def setUp(self):
        self._tmpfile = tempfile.mktemp(suffix='.log')

    def tearDown(self):
        if os.path.exists(self._tmpfile):
            try:
                os.remove(self._tmpfile)
            except OSError:
                pass

    def test_write_goes_to_file(self):
        """TeeStream.write() should write the message to the log file."""
        from anuga.utilities.log import TeeStream
        # Capture terminal output so we don't pollute the test runner console.
        captured_terminal = io.StringIO()
        tee = TeeStream.__new__(TeeStream)
        tee._terminal = captured_terminal
        tee._log = open(self._tmpfile, 'w', encoding='utf-8')

        tee.write('hello from TeeStream')
        tee.close()

        with open(self._tmpfile, encoding='utf-8') as fh:
            content = fh.read()
        self.assertIn('hello from TeeStream', content)

    def test_write_goes_to_terminal(self):
        """TeeStream.write() should also write to the terminal stream."""
        from anuga.utilities.log import TeeStream
        captured_terminal = io.StringIO()
        tee = TeeStream.__new__(TeeStream)
        tee._terminal = captured_terminal
        tee._log = open(self._tmpfile, 'w', encoding='utf-8')

        tee.write('terminal message')
        tee.close()

        self.assertIn('terminal message', captured_terminal.getvalue())

    def test_flush_does_not_raise(self):
        """TeeStream.flush() should not raise."""
        from anuga.utilities.log import TeeStream
        tee = TeeStream.__new__(TeeStream)
        tee._terminal = io.StringIO()
        tee._log = open(self._tmpfile, 'w', encoding='utf-8')
        tee.flush()  # Should not raise
        tee.close()

    def test_close_closes_log_file(self):
        """TeeStream.close() should close the underlying log file."""
        from anuga.utilities.log import TeeStream
        tee = TeeStream.__new__(TeeStream)
        tee._terminal = io.StringIO()
        tee._log = open(self._tmpfile, 'w', encoding='utf-8')
        tee.close()
        self.assertTrue(tee._log.closed)

    def test_proxy_attribute_to_terminal(self):
        """Accessing an unknown attribute should proxy to the terminal."""
        from anuga.utilities.log import TeeStream
        tee = TeeStream.__new__(TeeStream)
        tee._terminal = sys.__stdout__  # use the real stdout for proxy test
        tee._log = open(self._tmpfile, 'w', encoding='utf-8')
        # 'encoding' is an attribute of the real stdout
        enc = tee.encoding
        self.assertEqual(enc, sys.__stdout__.encoding)
        tee.close()

    def test_teestream_init_creates_file(self):
        """TeeStream.__init__ should create (or open) the log file."""
        from anuga.utilities.log import TeeStream
        # Redirect terminal output to avoid polluting the runner
        saved = sys.__stdout__
        try:
            sys.__stdout__ = io.StringIO()
            tee = TeeStream(self._tmpfile, mode='w')
            tee.close()
        finally:
            sys.__stdout__ = saved
        self.assertTrue(os.path.exists(self._tmpfile))

    def test_multiple_writes_accumulate(self):
        """Multiple write() calls should accumulate content in the file."""
        from anuga.utilities.log import TeeStream
        captured_terminal = io.StringIO()
        tee = TeeStream.__new__(TeeStream)
        tee._terminal = captured_terminal
        tee._log = open(self._tmpfile, 'w', encoding='utf-8')

        tee.write('line one\n')
        tee.write('line two\n')
        tee.close()

        with open(self._tmpfile, encoding='utf-8') as fh:
            content = fh.read()
        self.assertIn('line one', content)
        self.assertIn('line two', content)


class Test_set_logfile(unittest.TestCase):
    """Tests for the set_logfile() function."""

    def setUp(self):
        self._tmpfile = tempfile.mktemp(suffix='.log')
        # Save original sys.stdout
        self._orig_stdout = sys.stdout

    def tearDown(self):
        # Restore sys.stdout if set_logfile replaced it
        if isinstance(sys.stdout, __import__('anuga.utilities.log', fromlist=['TeeStream']).TeeStream):
            sys.stdout.close()
        sys.stdout = self._orig_stdout
        if os.path.exists(self._tmpfile):
            try:
                os.remove(self._tmpfile)
            except OSError:
                pass

    def test_set_logfile_creates_log_file(self):
        """set_logfile() should create the log file on disk."""
        from anuga.utilities.log import set_logfile
        set_logfile(self._tmpfile)
        self.assertTrue(os.path.exists(self._tmpfile))

    def test_set_logfile_replaces_stdout_with_teestream(self):
        """set_logfile() should replace sys.stdout with a TeeStream instance."""
        from anuga.utilities.log import set_logfile, TeeStream
        set_logfile(self._tmpfile)
        self.assertIsInstance(sys.stdout, TeeStream)

    def test_set_logfile_writes_to_file_via_print(self):
        """After set_logfile(), print() should write content to the log file."""
        from anuga.utilities.log import set_logfile
        set_logfile(self._tmpfile)
        print('test_set_logfile_writes_to_file_via_print marker')
        # Flush and close to ensure content is written
        sys.stdout.flush()

        with open(self._tmpfile, encoding='utf-8') as fh:
            content = fh.read()
        self.assertIn('test_set_logfile_writes_to_file_via_print marker', content)

    def test_set_logfile_second_call_closes_previous(self):
        """Calling set_logfile() twice should close the first TeeStream."""
        from anuga.utilities.log import set_logfile, TeeStream
        tmpfile2 = tempfile.mktemp(suffix='.log')
        try:
            set_logfile(self._tmpfile)
            first_tee = sys.stdout
            self.assertIsInstance(first_tee, TeeStream)

            set_logfile(tmpfile2)
            # After the second call, the first TeeStream's log should be closed
            self.assertTrue(first_tee._log.closed)
        finally:
            if isinstance(sys.stdout, TeeStream):
                sys.stdout.close()
            if os.path.exists(tmpfile2):
                try:
                    os.remove(tmpfile2)
                except OSError:
                    pass


class Test_file_only(unittest.TestCase):
    """Tests for the file_only() context manager."""

    def setUp(self):
        self._tmpfile = tempfile.mktemp(suffix='.log')
        self._orig_stdout = sys.stdout

    def tearDown(self):
        from anuga.utilities.log import TeeStream
        if isinstance(sys.stdout, TeeStream):
            sys.stdout.close()
        sys.stdout = self._orig_stdout
        if os.path.exists(self._tmpfile):
            try:
                os.remove(self._tmpfile)
            except OSError:
                pass

    def test_file_only_restores_stdout_after_block(self):
        """file_only() must restore sys.stdout after the block exits."""
        from anuga.utilities.log import file_only, set_logfile
        set_logfile(self._tmpfile)
        tee = sys.stdout
        with file_only():
            pass  # nothing
        self.assertIs(sys.stdout, tee)

    def test_file_only_restores_stdout_on_exception(self):
        """file_only() must restore sys.stdout even if the block raises."""
        from anuga.utilities.log import file_only, set_logfile
        set_logfile(self._tmpfile)
        tee = sys.stdout
        try:
            with file_only():
                raise ValueError('deliberate error')
        except ValueError:
            pass
        self.assertIs(sys.stdout, tee)

    def test_file_only_without_logfile_suppresses_output(self):
        """Without a logfile, file_only() should discard all output."""
        from anuga.utilities.log import file_only
        # Ensure stdout is the plain terminal (not a TeeStream)
        sys.stdout = self._orig_stdout
        captured = io.StringIO()
        sys.stdout = captured
        with file_only():
            print('this should be discarded')
        sys.stdout = self._orig_stdout
        # The StringIO captured nothing (file_only swapped it out)
        # The output was discarded, not written to captured
        self.assertNotIn('this should be discarded', captured.getvalue())


class Test_log_exports_in_anuga_init(unittest.TestCase):
    """Tests that the new log exports are accessible from anuga.__init__."""

    def test_anuga_has_set_logfile(self):
        """set_logfile is importable from anuga."""
        import anuga
        self.assertTrue(hasattr(anuga, 'set_logfile'))

    def test_anuga_has_teestream(self):
        """TeeStream is importable from anuga."""
        import anuga
        self.assertTrue(hasattr(anuga, 'TeeStream'))

    def test_anuga_all_contains_set_logfile(self):
        """set_logfile appears in anuga.__all__."""
        import anuga
        self.assertIn('set_logfile', anuga.__all__)

    def test_anuga_all_contains_teestream(self):
        """TeeStream appears in anuga.__all__."""
        import anuga
        self.assertIn('TeeStream', anuga.__all__)

    def test_set_logfile_is_callable(self):
        """set_logfile exported from anuga is callable."""
        import anuga
        self.assertTrue(callable(anuga.set_logfile))

    def test_teestream_is_a_class(self):
        """TeeStream exported from anuga is a class."""
        import anuga
        import inspect
        self.assertTrue(inspect.isclass(anuga.TeeStream))


if __name__ == '__main__':
    unittest.main()

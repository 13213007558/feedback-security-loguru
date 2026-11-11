import gzip
import os
import threading
from unittest.mock import Mock

import pytest

from loguru import logger
from loguru._file_sink import FileSink


class CloseBarrier:
    """Pause the sink right after it closed its log file.

    This lets a test deterministically replace the log path in the window
    between Loguru closing the old file and running its post-processing
    (renaming, compression, retention), without relying on real-time races.
    """

    def __init__(self):
        self._closed = threading.Event()
        self._resume = threading.Event()
        self._armed = False

    def install(self, monkeypatch):
        original_close_file = FileSink._close_file

        def close_file(sink):
            original_close_file(sink)
            if self._armed:
                self._armed = False
                self._closed.set()
                assert self._resume.wait(timeout=30)

        monkeypatch.setattr(FileSink, "_close_file", close_file)
        return self

    def arm(self):
        self._closed.clear()
        self._resume.clear()
        self._armed = True

    def wait_closed(self):
        assert self._closed.wait(timeout=30)

    def resume(self):
        self._resume.set()


def run_in_thread(function):
    errors = []

    def target():
        try:
            function()
        except Exception as error:  # noqa: BLE001
            errors.append(error)

    thread = threading.Thread(target=target)
    thread.start()
    return thread, errors


@pytest.fixture
def log_dir(tmp_path):
    directory = tmp_path / "logs"
    directory.mkdir()
    return directory


@pytest.fixture
def outside_file(tmp_path):
    file = tmp_path / "outside.log"
    file.write_text("secret\n")
    return file


@pytest.mark.skipif(os.name == "nt", reason="Symbolic links require privileges on Windows")
def test_rotation_skips_path_replaced_by_symlink(tmp_path, log_dir, outside_file, monkeypatch):
    barrier = CloseBarrier().install(monkeypatch)
    log_file = log_dir / "app.log"
    should_rotate = [False]

    logger.add(
        log_file,
        format="{message}",
        rotation=lambda *_: should_rotate[0],
        compression="gz",
        retention=10,
        catch=False,
    )
    logger.info("first")
    original_stat = os.stat(log_file)
    outside_stat = os.stat(outside_file)

    should_rotate[0] = True
    barrier.arm()
    thread, errors = run_in_thread(lambda: logger.info("second"))
    barrier.wait_closed()

    # An external tool rotates the file away and replaces the path with a
    # symbolic link to a file outside of the log directory.
    os.rename(log_file, log_dir / "app.log.rotated-by-ops")
    os.symlink(outside_file, log_file)
    barrier.resume()
    thread.join()

    # Re-opening the replaced path is refused and reported.
    assert len(errors) == 1
    assert "symbolic link" in str(errors[0])

    # The file outside of the log directory is left strictly untouched.
    assert outside_file.read_text() == "secret\n"
    assert os.stat(outside_file).st_ino == outside_stat.st_ino

    # The data Loguru wrote is neither renamed, compressed nor deleted.
    rotated = log_dir / "app.log.rotated-by-ops"
    assert rotated.read_text() == "first\n"
    assert os.stat(rotated).st_ino == original_stat.st_ino

    # The symbolic link itself is left in place and nothing was compressed.
    assert os.path.islink(log_file)
    assert os.readlink(log_file) == str(outside_file)
    assert list(log_dir.glob("*.gz")) == []

    # Once the unexpected link is removed, logging resumes in a fresh file.
    should_rotate[0] = False
    os.unlink(log_file)
    logger.info("third")
    assert log_file.read_text() == "third\n"


def test_rotation_skips_path_replaced_by_other_file(log_dir):
    log_file = log_dir / "app.log"
    should_rotate = [False]

    logger.add(
        log_file,
        format="{message}",
        rotation=lambda *_: should_rotate[0],
        compression="gz",
        retention=10,
        catch=False,
    )
    logger.info("first")
    original_stat = os.stat(log_file)

    # An external tool rotates the file away and creates its own new file.
    should_rotate[0] = True
    os.rename(log_file, log_dir / "app.log.rotated-by-ops")
    log_file.write_text("new-from-ops\n")
    logger.info("second")

    # The file left by the external tool is not renamed, compressed or deleted,
    # it is simply re-opened so that logging goes on as with external rotation.
    assert log_file.read_text() == "new-from-ops\nsecond\n"

    rotated = log_dir / "app.log.rotated-by-ops"
    assert rotated.read_text() == "first\n"
    assert os.stat(rotated).st_ino == original_stat.st_ino
    assert list(log_dir.glob("*.gz")) == []


@pytest.mark.skipif(os.name == "nt", reason="Symbolic links require privileges on Windows")
def test_stop_skips_compression_of_symlink_replacement(log_dir, outside_file, monkeypatch):
    barrier = CloseBarrier().install(monkeypatch)
    log_file = log_dir / "app.log"

    handler_id = logger.add(log_file, format="{message}", compression="gz", catch=False)
    logger.info("first")
    original_stat = os.stat(log_file)
    outside_stat = os.stat(outside_file)

    barrier.arm()
    thread, errors = run_in_thread(lambda: logger.remove(handler_id))
    barrier.wait_closed()

    os.rename(log_file, log_dir / "app.log.rotated-by-ops")
    os.symlink(outside_file, log_file)
    barrier.resume()
    thread.join()

    assert errors == []
    assert outside_file.read_text() == "secret\n"
    assert os.stat(outside_file).st_ino == outside_stat.st_ino
    rotated = log_dir / "app.log.rotated-by-ops"
    assert rotated.read_text() == "first\n"
    assert os.stat(rotated).st_ino == original_stat.st_ino
    assert os.path.islink(log_file)
    assert list(log_dir.glob("*.gz")) == []


@pytest.mark.skipif(os.name == "nt", reason="Symbolic links require privileges on Windows")
def test_compression_does_not_follow_symlink_output_path(tmp_path, log_dir):
    log_file = log_dir / "app.log"
    outside_file = tmp_path / "outside.log"

    handler_id = logger.add(log_file, format="{message}", compression="gz", catch=False)
    logger.info("payload")

    # A dangling symbolic link occupies the compression output path: writing the
    # archive must not create or alter a file outside of the log directory.
    os.symlink(outside_file, log_dir / "app.log.gz")
    logger.remove(handler_id)

    assert not outside_file.exists()

    # The link was renamed aside, and the archive is a regular file.
    links = [file for file in log_dir.glob("app.*.log.gz") if file.is_symlink()]
    assert len(links) == 1
    assert os.readlink(links[0]) == str(outside_file)

    archive = log_dir / "app.log.gz"
    assert archive.is_file() and not archive.is_symlink()
    with gzip.open(archive, "rt") as file:
        assert file.read() == "payload\n"

    assert not log_file.exists()


@pytest.mark.skipif(os.name == "nt", reason="Symbolic links require privileges on Windows")
def test_retention_ignores_symlinks(log_dir, outside_file):
    log_file = log_dir / "app.log"
    old_file = log_dir / "app.2019-01-01_00-00-00_000000.log"
    old_file.write_text("old\n")
    link_file = log_dir / "app.2020-01-01_00-00-00_000000.log"
    os.symlink(outside_file, link_file)
    outside_stat = os.stat(outside_file)

    handler_id = logger.add(log_file, format="{message}", retention=1, catch=False)
    logger.info("kept")
    logger.remove(handler_id)

    # Retention deleted the oldest regular file but left the symbolic link
    # and its target outside of the log directory alone.
    assert log_file.read_text() == "kept\n"
    assert not old_file.exists()
    assert os.path.islink(link_file)
    assert os.readlink(link_file) == str(outside_file)
    assert outside_file.read_text() == "secret\n"
    assert os.stat(outside_file).st_ino == outside_stat.st_ino
    assert {file.name for file in log_dir.iterdir()} == {
        "app.log",
        "app.2020-01-01_00-00-00_000000.log",
    }


@pytest.mark.skipif(os.name == "nt", reason="Windows can't rename a file in use")
def test_watch_reopens_new_file_after_external_rotation(log_dir):
    log_file = log_dir / "app.log"
    should_rotate = [False]

    logger.add(
        log_file,
        format="{message}",
        watch=True,
        rotation=lambda *_: should_rotate[0],
        compression="gz",
        retention=5,
        catch=False,
    )
    logger.info("one")
    original_stat = os.stat(log_file)

    # Normal external rotation: the file is moved away, the path disappears.
    os.rename(log_file, log_dir / "app.log.1")
    logger.info("two")

    # A brand new file was created and is used from now on.
    assert os.stat(log_file).st_ino != original_stat.st_ino

    should_rotate[0] = True
    logger.info("three")

    assert (log_dir / "app.log.1").read_text() == "one\n"
    assert os.stat(log_dir / "app.log.1").st_ino == original_stat.st_ino

    archives = list(log_dir.glob("*.gz"))
    assert len(archives) == 1
    with gzip.open(archives[0], "rt") as file:
        assert file.read() == "two\n"

    assert log_file.read_text() == "three\n"


@pytest.mark.skipif(os.name == "nt", reason="Symbolic links require privileges on Windows")
def test_watch_does_not_follow_symlink_replacement(log_dir, outside_file):
    log_file = log_dir / "app.log"
    outside_stat = os.stat(outside_file)

    logger.add(log_file, format="{message}", watch=True, catch=False)
    logger.info("one")
    original_stat = os.stat(log_file)

    # The path is replaced by a symbolic link to a file outside of the log
    # directory: the sink keeps writing to the file it owns instead.
    os.rename(log_file, log_dir / "app.log.moved")
    os.symlink(outside_file, log_file)
    logger.info("two")

    assert outside_file.read_text() == "secret\n"
    assert os.stat(outside_file).st_ino == outside_stat.st_ino
    moved = log_dir / "app.log.moved"
    assert moved.read_text() == "one\ntwo\n"
    assert os.stat(moved).st_ino == original_stat.st_ino
    assert os.path.islink(log_file)

    # Once the path is restored, the new file is re-opened as usual.
    os.unlink(log_file)
    logger.info("three")
    assert log_file.read_text() == "three\n"
    assert moved.read_text() == "one\ntwo\n"


@pytest.mark.skipif(os.name == "nt", reason="Windows can't rename a file in use")
def test_compression_failure_with_watch_and_retention_continues_logging(log_dir):
    log_file = log_dir / "app.log"
    should_rotate = [False]
    compression = Mock(side_effect=[OSError("Compression error"), None])

    logger.add(
        log_file,
        format="{message}",
        watch=True,
        rotation=lambda *_: should_rotate[0],
        compression=compression,
        retention=5,
        catch=False,
    )
    logger.info("one")

    # The compression failure is reported but does not corrupt the sink state.
    should_rotate[0] = True
    with pytest.raises(OSError, match="^Compression error$"):
        logger.info("two")

    # Logging goes on in a new file despite the failed post-processing.
    should_rotate[0] = False
    logger.info("three")
    assert log_file.read_text() == "three\n"

    # The file whose compression failed was left untouched.
    rotated = list(log_dir.glob("app.*.log"))
    assert len(rotated) == 1
    assert rotated[0].read_text() == "one\n"

    # A later rotation still triggers compression and retention normally.
    should_rotate[0] = True
    logger.info("four")
    assert compression.call_count == 2
    rotated = list(log_dir.glob("app.*.log"))
    assert len(rotated) == 2
    assert sorted(file.read_text() for file in rotated) == ["one\n", "three\n"]
    assert log_file.read_text() == "four\n"

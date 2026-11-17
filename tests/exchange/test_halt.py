import os
import tempfile
from pathlib import Path

import pytest

from hl_guard.exchange import halt
from hl_guard.exchange.halt import HALT_FILENAME, HaltFlag, HaltPathError


@pytest.mark.parametrize(
    "data_dir",
    [
        "/tmp/hl-guard",
        "/private/tmp/hl-guard",
        "/var/tmp/hl-guard",
        "/var/folders/xy/abc/T/hl-guard",
        "/private/var/folders/xy/abc/T/hl-guard",
        os.path.join(tempfile.gettempdir(), "hl-guard"),
    ],
)
def test_a_data_dir_under_a_temp_dir_is_refused(data_dir: str) -> None:
    with pytest.raises(HaltPathError, match="temp"):
        HaltFlag(data_dir)


def test_tmpdir_from_the_environment_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    custom = tmp_path / "custom-tmp"
    custom.mkdir()
    monkeypatch.setenv("TMPDIR", str(custom))
    monkeypatch.setattr(tempfile, "tempdir", None)
    with pytest.raises(HaltPathError):
        HaltFlag(custom / "data")


def test_pytest_tmp_path_is_refused_by_default(tmp_path: Path) -> None:
    with pytest.raises(HaltPathError):
        HaltFlag(tmp_path)


def test_a_symlink_into_a_temp_dir_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    volatile = tmp_path / "volatile"
    volatile.mkdir()
    (tmp_path / "data").symlink_to(volatile)
    monkeypatch.setattr(halt, "temp_dirs", lambda: [volatile.resolve()])
    with pytest.raises(HaltPathError):
        HaltFlag(tmp_path / "data")
    HaltFlag(tmp_path / "durable")


def test_a_home_data_dir_is_accepted() -> None:
    flag = HaltFlag(Path.home() / ".hl-guard")
    assert flag.path.name == HALT_FILENAME


def test_absent_flag_is_not_halted(durable_dir: Path) -> None:
    assert HaltFlag(durable_dir).present() is False


def test_a_flag_file_halts(durable_dir: Path) -> None:
    (durable_dir / HALT_FILENAME).write_text("stop\n")
    assert HaltFlag(durable_dir).present() is True


def test_a_relative_data_dir_stays_halted_after_chdir(
    durable_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (durable_dir / "data").mkdir()
    (durable_dir / "data" / HALT_FILENAME).write_text("stop\n")
    monkeypatch.chdir(durable_dir)
    flag = HaltFlag("data")
    assert flag.present() is True
    monkeypatch.chdir("/")
    assert flag.present() is True
    assert flag.path == (durable_dir / "data" / HALT_FILENAME).resolve()


def test_a_dangling_halt_symlink_counts_as_halted(durable_dir: Path) -> None:
    (durable_dir / HALT_FILENAME).symlink_to(durable_dir / "deleted-target")
    assert HaltFlag(durable_dir).present() is True


def test_a_halt_directory_counts_as_halted(durable_dir: Path) -> None:
    (durable_dir / HALT_FILENAME).mkdir()
    assert HaltFlag(durable_dir).present() is True


def test_the_flag_stays_on_the_data_dir_resolved_at_start(durable_dir: Path) -> None:
    first, second = durable_dir / "release-a", durable_dir / "release-b"
    first.mkdir()
    second.mkdir()
    current = durable_dir / "current"
    current.symlink_to(first)
    flag = HaltFlag(current)
    current.unlink()
    current.symlink_to(second)
    (second / HALT_FILENAME).write_text("stop\n")
    assert flag.present() is False
    (first / HALT_FILENAME).write_text("stop\n")
    assert flag.present() is True
    assert flag.path == first.resolve() / HALT_FILENAME

from __future__ import annotations

import os
import tempfile
from pathlib import Path

HALT_FILENAME = "HALT"


class HaltPathError(ValueError):
    pass


def temp_dirs() -> list[Path]:
    roots = [
        "/tmp",
        "/private/tmp",
        "/var/tmp",
        "/private/var/tmp",
        "/var/folders",
        "/private/var/folders",
        tempfile.gettempdir(),
        os.environ.get("TMPDIR"),
    ]
    return [Path(root).resolve() for root in roots if root]


class HaltFlag:
    def __init__(self, data_dir: str | os.PathLike[str]) -> None:
        resolved = Path(data_dir).resolve()
        for root in temp_dirs():
            if resolved.is_relative_to(root):
                raise HaltPathError(
                    f"data dir {str(data_dir)!r} refused: it resolves under the temp dir "
                    f"{str(root)!r}, where cleanup would delete the halt flag"
                )
        self.path = resolved / HALT_FILENAME

    def present(self) -> bool:
        try:
            os.lstat(self.path)
        except FileNotFoundError:
            return False
        except OSError:
            return True
        return True

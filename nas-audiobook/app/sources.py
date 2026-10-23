"""Original book files, kept once each in a folder named by their MD5, under their original file name.
A cover chosen for a book (see books.source.chosen_cover) is kept beside it in a .cover folder."""
from __future__ import annotations

import shutil
from pathlib import Path

from .library import md5_file


def adopt(books_dir: Path, src: Path, md5: str | None = None) -> tuple[str, Path]:
    """Move a file to books_dir/<md5>/<original name>. If that book is already stored, the new copy is dropped
    and the stored one is returned. Keeping the name means a book without a title in its metadata still has one."""
    md5 = md5 or md5_file(src)
    folder = books_dir / md5
    chosen = src.parent / ".cover"
    stored = next((f for f in sorted(folder.iterdir()) if f.is_file()), None) if folder.is_dir() else None
    if stored is not None:
        src.unlink(missing_ok=True)
        target = stored
    else:
        folder.mkdir(parents=True)
        target = folder / src.name
        shutil.move(src, target)
    if chosen.is_dir():  # a cover picked with this copy replaces the one kept with the stored book
        shutil.rmtree(folder / ".cover", ignore_errors=True)
        shutil.copytree(chosen, folder / ".cover")
    return md5, target

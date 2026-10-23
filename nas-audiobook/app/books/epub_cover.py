"""Find an EPUB's cover the way e-readers do: what the book declares first, file-name guesses last."""
from __future__ import annotations

import posixpath
import re
import zipfile
from pathlib import Path
from urllib.parse import unquote
from xml.etree import ElementTree as ET

from ..model import Cover

IMAGE_SRC = re.compile(r"""(?:<img\b[^>]*?\bsrc|<image\b[^>]*?href)\s*=\s*["']([^"']+)["']""", re.I | re.S)
OPF_NS = {"c": "urn:oasis:names:tc:opendocument:xmlns:container", "o": "http://www.idpf.org/2007/opf"}


def sniff(data: bytes) -> str | None:
    """The image type from its first bytes, because EPUB file names and media types are often wrong."""
    if data[:3] == b"\xff\xd8\xff":
        return "jpg"
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return "png"
    return None


def find_cover(path: Path) -> Cover | None:
    try:
        with zipfile.ZipFile(path) as z:
            return _find(z)
    except (zipfile.BadZipFile, OSError, ET.ParseError, KeyError, ValueError):
        return None


def _find(z: zipfile.ZipFile) -> Cover | None:
    container = ET.fromstring(z.read("META-INF/container.xml"))
    opf_path = container.find(".//c:rootfile", OPF_NS).get("full-path")
    base = posixpath.dirname(opf_path)
    opf = ET.fromstring(z.read(opf_path))

    def resolve(href: str, relative_to: str = base) -> str:
        return posixpath.normpath(posixpath.join(relative_to, unquote(href.split("#")[0])))

    manifest = {}
    for item in opf.iterfind(".//o:manifest/o:item", OPF_NS):
        manifest[item.get("id")] = (resolve(item.get("href", "")), item.get("media-type", ""), item.get("properties", ""))

    declared: list[str] = []
    declared += [href for href, _t, props in manifest.values() if "cover-image" in props.split()]
    for meta in opf.iterfind(".//o:metadata/o:meta", OPF_NS):
        if meta.get("name") == "cover" and meta.get("content") in manifest:
            declared.append(manifest[meta.get("content")][0])
    for ref in opf.iterfind(".//o:guide/o:reference", OPF_NS):
        if (ref.get("type") or "").lower() == "cover" and ref.get("href"):
            declared.append(resolve(ref.get("href")))
    guessed = [href for id_, (href, kind, _p) in manifest.items()
               if kind.startswith("image/") and "cover" in f"{id_} {href}".lower()]

    names = set(z.namelist())
    for target in declared + guessed:
        for image in _images_in(z, names, target, resolve):
            data = z.read(image)
            if (ext := sniff(data)):
                return Cover(data, ext)
    return None


def _images_in(z: zipfile.ZipFile, names: set[str], target: str, resolve) -> list[str]:
    """`target` itself if it is an image, or the images a cover page (titlepage.xhtml, often an SVG wrapper) shows."""
    if target not in names:
        return []
    if not target.lower().endswith((".xhtml", ".html", ".htm", ".xml")):
        return [target]
    page = z.read(target).decode("utf8", errors="replace")
    found = [resolve(src, posixpath.dirname(target)) for src in IMAGE_SRC.findall(page)]
    return [f for f in found if f in names]

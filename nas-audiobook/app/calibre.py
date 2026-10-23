"""Browse a Calibre-Web library over OPDS and fetch books from it, so a book can go straight into the Convert flow.

OPDS is the catalogue format e-readers use; Calibre-Web serves it at /opds when "Enable OPDS" is on. Everything here
works from what the feed says (links and their types), not from Calibre-Web's URL layout, and every path the page
sends is resolved against the configured address only, so the page cannot make this server fetch somewhere else."""
from __future__ import annotations

import hashlib
import json
import os
import re
from dataclasses import asdict, dataclass
from pathlib import Path
from urllib.parse import quote, urlparse
from xml.etree import ElementTree as ET

import httpx

from .ingest import SUPPORTED

ATOM = "{http://www.w3.org/2005/Atom}"
OPENSEARCH = "{http://a9.com/-/spec/opensearch/1.1/}"
BOOK_ID = re.compile(r"/(?:download|cover|thumb[^/]*)/(\d+)(?:[/.]|$)")
ACQUISITION = "http://opds-spec.org/acquisition"
IMAGE = "http://opds-spec.org/image"
THUMBNAIL = "http://opds-spec.org/image/thumbnail"
MIME_FORMATS = {
    "application/epub+zip": "epub", "application/pdf": "pdf", "application/x-mobipocket-ebook": "mobi",
    "application/x-mobi8-ebook": "azw3", "application/vnd.amazon.ebook": "azw", "application/x-fictionbook+xml": "fb2",
    "application/fb2+zip": "fbz", "text/plain": "txt", "text/markdown": "md", "application/rtf": "rtf", "text/rtf": "rtf",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document": "docx",
}
PREFERRED = ["epub", "azw3", "mobi", "fb2", "docx", "pdf", "txt"]  # structured formats first: they split into chapters best
MAX_DOWNLOAD_BYTES = 300 * 1024 * 1024
SUMMARY_CHARS = 280


class CalibreError(Exception):
    pass


@dataclass(frozen=True)
class CalibreConfig:
    url: str | None = None
    username: str | None = None
    password: str | None = None

    @property
    def configured(self) -> bool:
        return bool(self.url)


def normalize_url(url: str | None) -> str:
    """The address as http(s)://host[:port][/prefix] without a trailing slash."""
    text = (url or "").strip()
    if text and "://" not in text:
        text = "http://" + text
    parsed = urlparse(text)
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        raise CalibreError("the Calibre-Web address must look like http://192.168.1.10:8083")
    return text.rstrip("/")


class CalibreStore:
    def __init__(self, path: Path):
        self.path = path

    def load(self) -> CalibreConfig:
        try:
            d = json.loads(self.path.read_text(encoding="utf8"))
            return CalibreConfig(d.get("url"), d.get("username"), d.get("password"))
        except (OSError, ValueError):
            return CalibreConfig()

    def save(self, config: CalibreConfig) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_name(self.path.name + ".part")
        tmp.write_text(json.dumps(asdict(config), ensure_ascii=False), encoding="utf8")
        os.chmod(tmp, 0o600)
        os.replace(tmp, self.path)


def _text(node) -> str:
    return " ".join("".join(node.itertext()).split()) if node is not None else ""


def _format_of(link: ET.Element) -> str | None:
    kind = (link.get("type") or "").split(";")[0].strip().lower()
    if kind in MIME_FORMATS:
        return MIME_FORMATS[kind]
    title = (link.get("title") or "").strip().lower().lstrip(".")
    if title in {e.lstrip(".") for e in SUPPORTED}:
        return title
    ext = Path(urlparse(link.get("href") or "").path.rstrip("/")).suffix.lower().lstrip(".")
    return ext or title or None


class CalibreClient:
    def __init__(self, config: CalibreConfig, client: httpx.Client | None = None):
        if not config.configured:
            raise CalibreError("connect Calibre-Web in Settings first")
        self.base = normalize_url(config.url)
        parsed = urlparse(self.base)
        self._origin = f"{parsed.scheme}://{parsed.netloc}"
        self._root = parsed.path.rstrip("/") + "/opds/"
        self._auth = httpx.BasicAuth(config.username, config.password or "") if config.username else httpx.USE_CLIENT_DEFAULT
        self._client = client or httpx.Client(timeout=httpx.Timeout(15.0, read=120.0), follow_redirects=False)

    def _url(self, path: str) -> str:
        if not path.startswith("/") or path.startswith("//") or "\\" in path:
            raise CalibreError("that is not a path on the Calibre-Web server")
        return self._origin + path

    def _get(self, path: str, stream: bool = False) -> httpx.Response:
        try:
            request = self._client.build_request("GET", self._url(path))
            response = self._client.send(request, stream=stream, auth=self._auth)
        except httpx.TransportError as exc:
            raise CalibreError(f"cannot reach Calibre-Web at {self.base} ({type(exc).__name__}). "
                               "Is it running, and is the address right?") from exc
        if response.status_code in (401, 403):
            response.close()
            raise CalibreError("Calibre-Web rejected the username or password")
        if response.is_redirect:
            response.close()
            raise CalibreError("Calibre-Web redirected to its login page: check the username and password, and that "
                               "OPDS is switched on (Admin → Edit Basic Configuration → Feature Configuration)")
        if response.status_code == 404 and path.startswith(self._root):
            response.close()
            raise CalibreError("Calibre-Web has no OPDS catalogue at that address. Switch on \"Enable OPDS\" "
                               "(Admin → Edit Basic Configuration → Feature Configuration)")
        if response.status_code >= 400:
            response.close()
            raise CalibreError(f"Calibre-Web answered {response.status_code}")
        return response

    def feed(self, path: str | None = None, query: str | None = None) -> dict:
        """One page of the catalogue: books to convert and sub-lists to open, plus the next page and a search link."""
        if query:
            path = self._search_path(query)
        return self._parse(self._get(path or self._root).content)

    def _search_path(self, query: str) -> str:
        """The catalogue says where search lives (a template with {searchTerms}); Calibre-Web's usual place is the fallback."""
        template = self.feed()["search"] or self._root + "search/{searchTerms}"
        return template.replace("{searchTerms}", quote(query.strip(), safe=""))

    def _parse(self, content: bytes) -> dict:
        try:
            root = ET.fromstring(content)
        except ET.ParseError as exc:
            raise CalibreError("that address did not return an OPDS catalogue. Is it Calibre-Web, and is OPDS "
                               "switched on?") from exc
        if root.tag != f"{ATOM}feed":
            raise CalibreError("that address did not return an OPDS catalogue")
        nxt = search = None
        for link in root.findall(f"{ATOM}link"):
            rel, kind, href = link.get("rel"), link.get("type") or "", link.get("href")
            if rel == "next" and href:
                nxt = href
            elif rel == "search" and "atom" in kind and href and "{searchTerms}" in href:
                search = href
        total = root.findtext(f"{OPENSEARCH}totalResults")
        entries = [e for e in (self._entry(x) for x in root.findall(f"{ATOM}entry")) if e]
        return {"title": _text(root.find(f"{ATOM}title")), "entries": entries, "next": nxt, "search": search,
                "total": int(total) if total and total.isdigit() else None}

    def _entry(self, entry: ET.Element) -> dict | None:
        title = _text(entry.find(f"{ATOM}title"))
        links = entry.findall(f"{ATOM}link")
        acquisitions = [l for l in links if (l.get("rel") or "").startswith(ACQUISITION) and l.get("href")]
        if acquisitions:
            formats, other = [], []
            for link in acquisitions:
                fmt = _format_of(link)
                if fmt and f".{fmt}" in SUPPORTED:
                    formats.append({"format": fmt, "path": link.get("href"),
                                    "size": int(link.get("length")) if (link.get("length") or "").isdigit() else None})
                elif fmt:
                    other.append(fmt)
            formats.sort(key=lambda f: PREFERRED.index(f["format"]) if f["format"] in PREFERRED else len(PREFERRED))
            thumb = next((l.get("href") for l in links if l.get("rel") == THUMBNAIL), None)
            image = next((l.get("href") for l in links if l.get("rel") == IMAGE), None)
            summary = _text(entry.find(f"{ATOM}content")) or _text(entry.find(f"{ATOM}summary"))
            return {"kind": "book", "web_url": self._book_page(links), "id": _text(entry.find(f"{ATOM}id")), "title": title,
                    "authors": [_text(a.find(f"{ATOM}name")) for a in entry.findall(f"{ATOM}author")],
                    "summary": summary[:SUMMARY_CHARS] + ("…" if len(summary) > SUMMARY_CHARS else ""),
                    "cover": thumb or image, "image": image or thumb, "formats": formats,
                    "unsupported": sorted(set(other))}
        sub = next((l for l in links if l.get("href") and "atom" in (l.get("type") or "")
                    and l.get("rel") in (None, "subsection", "http://opds-spec.org/sort/new",
                                         "http://opds-spec.org/sort/popular", "http://opds-spec.org/crawlable")), None)
        if sub is None:
            sub = next((l for l in links if l.get("href") and "atom" in (l.get("type") or "")
                        and l.get("rel") not in ("self", "start", "up", "next", "previous", "search")), None)
        if sub is None:
            return None
        return {"kind": "nav", "title": title, "path": sub.get("href"),
                "summary": _text(entry.find(f"{ATOM}content")) or _text(entry.find(f"{ATOM}summary"))}

    def _book_page(self, links: list[ET.Element]) -> str | None:
        """The book's own page in Calibre-Web's web interface, for opening in a browser. The book's number is in the
        paths of its download and cover links."""
        for link in links:
            found = BOOK_ID.search(link.get("href") or "")
            if found:
                return f"{self.base}/book/{found.group(1)}"
        return None

    def image(self, path: str) -> tuple[bytes, str]:
        response = self._get(path)
        kind = response.headers.get("content-type", "image/jpeg").split(";")[0]
        if not kind.startswith("image/"):
            raise CalibreError("that is not a picture")
        return response.content, kind

    def download(self, path: str, dest: Path, max_bytes: int = MAX_DOWNLOAD_BYTES) -> str:
        """Save a book to `dest` and return its MD5."""
        digest, size = hashlib.md5(), 0
        response = self._get(path, stream=True)
        try:
            with dest.open("wb") as out:
                for chunk in response.iter_bytes(1 << 20):
                    size += len(chunk)
                    if size > max_bytes:
                        raise CalibreError(f"that file is larger than {max_bytes // (1024 * 1024)} MB")
                    out.write(chunk)
                    digest.update(chunk)
        except httpx.TransportError as exc:
            raise CalibreError(f"the download from Calibre-Web stopped ({type(exc).__name__})") from exc
        finally:
            response.close()
        if size == 0:
            raise CalibreError("Calibre-Web sent an empty file")
        return digest.hexdigest()

    def test(self) -> dict:
        data = self.feed()
        books = sum(e["kind"] == "book" for e in data["entries"])
        return {"ok": True, "title": data["title"] or "Calibre-Web", "sections": [e["title"] for e in data["entries"]
                                                                              if e["kind"] == "nav"][:8],
                "books_on_first_page": books}

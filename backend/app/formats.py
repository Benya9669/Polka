import io
import re
import shutil
import subprocess
import tempfile
from html import escape
from html.parser import HTMLParser
from pathlib import Path

from defusedxml import ElementTree as XML
from PIL import Image

CHARSET = re.compile(rb"charset\s*=\s*[\"']?\s*([\w-]+)", re.IGNORECASE)
ENCODINGS = {"utf-8", "utf8", "windows-1251", "cp1251", "windows-1252", "cp1252", "iso-8859-1"}
SAFE_TAGS = frozenset("p h1 h2 h3 h4 h5 h6 br hr div span strong b em i u s sup sub blockquote q cite pre code ol ul li dl dt dd table caption thead tbody tfoot tr th td section article header footer".split())
HIDDEN_TAGS = {"script", "style", "iframe", "object", "svg", "math", "template", "noscript", "audio", "video"}
IMAGE_TYPES = {".jpg": "JPEG", ".jpeg": "JPEG", ".png": "PNG"}
COMIC_IMAGE_LIMIT = 16 * 1024 * 1024


class LocalHtml(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.text = []
        self.title = []
        self.in_title = False
        self.hidden = []
        self.recognized = False

    def handle_starttag(self, tag, attrs):
        if self.hidden:
            if tag in HIDDEN_TAGS:
                self.hidden.append(tag)
            return
        if tag in HIDDEN_TAGS:
            self.hidden.append(tag)
        elif tag == "title":
            self.in_title = True
        elif tag in SAFE_TAGS:
            self.recognized = True
            self.text.append(f"<{tag}>")
        elif tag in {"html", "head", "body"}:
            self.recognized = True

    def handle_endtag(self, tag):
        if self.hidden:
            if self.hidden[-1] == tag:
                self.hidden.pop()
            return
        if tag == "title":
            self.in_title = False
        elif tag in SAFE_TAGS and tag not in {"br", "hr"}:
            self.text.append(f"</{tag}>")

    def handle_data(self, data):
        if self.hidden:
            return
        if self.in_title:
            self.title.append(data)
        else:
            self.text.append(escape(data))


def parse_html(path):
    if path.stat().st_size > 16 * 1024 * 1024:
        raise ValueError("HTML превышает лимит 16 МиБ")
    raw = path.read_bytes()
    encoding = CHARSET.search(raw[:4096])
    hint = encoding[1].decode().lower() if encoding else "utf-8"
    if raw.startswith((b"\xff\xfe", b"\xfe\xff")):
        text = raw.decode("utf-16")
    else:
        hint = hint if hint in ENCODINGS else "utf-8"
        try:
            text = raw.decode("utf-8-sig" if hint in {"utf8", "utf-8"} else hint)
        except UnicodeDecodeError:
            text = raw.decode("cp1251")
    if "\x00" in text:
        raise ValueError("Ожидался текстовый HTML")
    result = LocalHtml()
    result.feed(text)
    result.close()
    if not result.recognized or not "".join(result.text).strip():
        raise ValueError("HTML не содержит читаемой разметки")
    return result


def local_html(source, target):
    parsed = parse_html(source)
    title = escape("".join(parsed.title).strip()[:1000])
    target.write_text('<!doctype html><html><head><meta charset="utf-8">'
                      f"<title>{title}</title></head><body>{''.join(parsed.text)}</body></html>", encoding="utf-8")


def mobi_kind(path, header):
    if len(header) < 86 or int.from_bytes(header[76:78], "big") < 1:
        raise ValueError("Повреждена таблица записей MOBI")
    position = int.from_bytes(header[78:82], "big")
    if position < 86 or position + 40 > path.stat().st_size:
        raise ValueError("Повреждён заголовок MOBI")
    with path.open("rb") as source:
        source.seek(position)
        record = source.read(40)
    if record[16:20] != b"MOBI":
        raise ValueError("Не найден заголовок MOBI")
    if int.from_bytes(record[12:14], "big"):
        raise ValueError("Книга защищена DRM")
    return "azw3" if int.from_bytes(record[36:40], "big") == 8 else "mobi"


def calibre_metadata(path, kind):
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        source = root / ("source." + kind)
        cover = root / "cover.jpg"
        shutil.copyfile(path, source)
        opf = root / "metadata.opf"
        result = subprocess.run(["ebook-meta", str(source), "--get-cover", str(cover), "--to-opf", str(opf)],
                                capture_output=True, timeout=30)
        if result.returncode:
            raise ValueError("Calibre не смог прочитать метаданные книги")
        output = result.stdout.decode("utf-8", "replace")
        fields = {key.strip(): value.strip() for line in output.splitlines()
                  if ":" in line for key, value in [line.split(":", 1)]}
        fields = {key: value for key, value in fields.items() if value.casefold() not in {"unknown", "none", "und"}}
        # The CLI display can append an author sort key in brackets. OPF keeps the credited names separate.
        authors = [node.text.strip() for node in XML.parse(opf).findall(".//{http://purl.org/dc/elements/1.1/}creator")
                   if node.text and node.text.strip() and node.text.strip().casefold() != "unknown"]
        raw_cover = cover.read_bytes() if cover.is_file() and cover.stat().st_size < 8 * 1024 * 1024 else None
    return {
        "title": fields.get("Title", ""), "authors": authors,
        "publisher": fields.get("Publisher", ""), "language": fields.get("Languages", "").split(",")[0].strip(),
        "summary": fields.get("Comments", ""), "isbn": fields.get("ISBN", ""),
    }, raw_cover


def rendered_cover(path, kind):
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        if kind == "pdf":
            command = ["pdftoppm", "-f", "1", "-l", "1", "-scale-to", "480", "-png", "-singlefile",
                       str(path), str(root / "cover")]
            target = root / "cover.png"
        else:
            target = root / "cover.ppm"
            command = ["ddjvu", "-format=ppm", "-page=1", "-size=480x720", str(path), str(target)]
        try:
            result = subprocess.run(command, capture_output=True, timeout=20)
            if result.returncode or not target.is_file() or target.stat().st_size > 8 * 1024 * 1024:
                return None
            return target.read_bytes()
        except (OSError, subprocess.TimeoutExpired):
            return None


def comic(z):
    names = [item.filename for item in z.infolist() if not item.is_dir()]
    pictures = sorted((name for name in names if Path(name).suffix.lower() in IMAGE_TYPES),
                      key=lambda name: [int(piece) if piece.isdigit() else piece.casefold()
                                        for piece in re.split(r"([0-9]+)", name)])
    info = next((name for name in names if Path(name).name.casefold() == "comicinfo.xml"), None)
    if not pictures or len(pictures) > 1000 or any(name not in pictures and name != info for name in names):
        raise ValueError("CBZ должен содержать изображения JPEG или PNG и, при наличии, ComicInfo.xml")
    cover = None
    all_pixels = 0
    for name in pictures:
        if z.getinfo(name).file_size > COMIC_IMAGE_LIMIT:
            raise ValueError("Страница комикса слишком велика")
        raw = z.read(name)
        with Image.open(io.BytesIO(raw)) as image:
            pixels = image.width * image.height
            all_pixels += pixels
            if image.format != IMAGE_TYPES[Path(name).suffix.lower()] or pixels > 12_000_000 or all_pixels > 350_000_000:
                raise ValueError("Размер или формат страниц комикса не поддерживается")
            image.verify()
        if cover is None:
            cover = raw
    if not info:
        return {}, cover
    if z.getinfo(info).file_size > 256 * 1024:
        raise ValueError("ComicInfo.xml превышает лимит 256 КиБ")
    root = XML.fromstring(z.read(info))
    names = {"title": "Title", "series": "Series", "series_index": "Number", "year": "Year",
             "publisher": "Publisher", "summary": "Summary", "language": "LanguageISO"}
    metadata = {key: (root.findtext(f"./{{*}}{field}") or "").strip()[:4000] for key, field in names.items()}
    metadata["authors"] = (root.findtext("./{*}Writer") or "").strip()[:4000].split(", ")
    metadata["genres"] = (root.findtext("./{*}Genre") or "").strip()[:4000].split(", ")
    return metadata, cover


import subprocess
from zipfile import ZipFile

from .books import MAX_BYTES, bounded_zip, copy_limited, epub_package, file_path, inspect, zip_read
from .formats import local_html

CONVERSION_TARGETS = {name: "epub" for name in ("fb2", "fb2.zip", "mobi", "azw3", "txt", "cbz", "html", "rtf")}
CONVERSION_TARGETS["djvu"] = "pdf"
SONY_TARGETS = {**CONVERSION_TARGETS, "epub": "epub", "pdf": "pdf"}


def convert_file(book, library, root, *, sony=False, title="", authors=()):
    source = root / ("source." + book.format)
    with file_path(book, library).open("rb") as stream, source.open("wb") as target:
        checksum, length = copy_limited(stream, target)
    if checksum != book.sha256 or length != book.size:
        raise ValueError("Исходный файл изменился, выполните повторное сканирование")

    target_format = (SONY_TARGETS if sony else CONVERSION_TARGETS)[book.format]
    if sony and book.format == "pdf":
        return source
    if book.format == "fb2.zip":
        with ZipFile(source) as archive:
            bounded_zip(archive)
            entries = [entry for entry in archive.namelist() if not entry.endswith("/")]
            if len(entries) != 1 or not entries[0].lower().endswith(".fb2"):
                raise ValueError("ZIP должен содержать одну книгу FB2")
            extracted = root / "source.fb2"
            extracted.write_bytes(zip_read(archive, entries[0], 32 * 1024 * 1024))
        source = extracted
    if book.format == "html":
        sanitized = root / "local.html"
        local_html(source, sanitized)
        source = sanitized
    if book.format == "cbz":
        # A previously indexed archive can only be used if its recorded digest still matches.
        inspect(source, source.name)

    output = root / ("converted." + target_format)
    if target_format == "pdf":
        command = ["ddjvu", "-format=pdf", str(source), str(output)]
    else:
        command = ["ebook-convert", str(source), str(output)]
        if sony:
            command += ["--output-profile", "sony", "--epub-version", "2"]
            if title:
                command += ["--title", title[:1000]]
            if authors:
                command += ["--authors", " & ".join(authors)[:1000]]
    try:
        result = subprocess.run(command, cwd=root, capture_output=True, timeout=240, check=False)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ValueError("Конвертер недоступен или превысил лимит времени") from exc
    if result.returncode or not output.is_file():
        lines = result.stderr.decode("utf-8", "replace").strip().splitlines()
        details = ": " + lines[-1][:200] if lines else ""
        raise ValueError("Конвертация книги завершилась ошибкой" + details)
    if not 0 < output.stat().st_size <= MAX_BYTES:
        raise ValueError("Результат конвертации превышает лимит размера")
    if sony and target_format == "epub":
        with ZipFile(output) as archive:
            bounded_zip(archive)
            _, package = epub_package(archive)
            if not package.get("version", "").startswith("2."):
                raise ValueError("Для Sony требуется EPUB 2")
    return output


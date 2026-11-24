from io import BytesIO
from zipfile import ZIP_STORED, ZipFile, ZipInfo


def epub_bytes():
    stream = BytesIO()
    with ZipFile(stream, "w") as archive:
        def write(name, value):
            info = ZipInfo(name, date_time=(2026, 1, 1, 0, 0, 0))
            archive.writestr(info, value, compress_type=ZIP_STORED)

        write("mimetype", "application/epub+zip")
        write("META-INF/container.xml", '''<?xml version="1.0"?>
          <container version="1.0" xmlns="urn:oasis:names:tc:opendocument:xmlns:container">
           <rootfiles><rootfile full-path="OPS/book.opf" media-type="application/oebps-package+xml"/></rootfiles>
          </container>''')
        write("OPS/book.opf", '''<?xml version="1.0"?>
          <package version="3.0" xmlns="http://www.idpf.org/2007/opf" unique-identifier="id">
            <metadata xmlns:dc="http://purl.org/dc/elements/1.1/"><dc:identifier id="id">urn:test:1</dc:identifier>
            <dc:title>Северная полка</dc:title><dc:creator>Анна Соколова</dc:creator><dc:language>ru</dc:language>
            <meta property="dcterms:modified">2026-01-01T00:00:00Z</meta></metadata>
            <manifest><item id="nav" href="nav.xhtml" media-type="application/xhtml+xml" properties="nav"/>
            <item id="one" href="one.xhtml" media-type="application/xhtml+xml"/><item id="two" href="two.xhtml" media-type="application/xhtml+xml"/></manifest>
            <spine><itemref idref="one"/><itemref idref="two"/></spine>
          </package>''')
        write("OPS/nav.xhtml", '''<html xmlns="http://www.w3.org/1999/xhtml" xmlns:epub="http://www.idpf.org/2007/ops">
          <head><title>Содержание</title></head><body><nav epub:type="toc"><ol><li><a href="one.xhtml">Первая глава</a></li>
           <li><a href="two.xhtml">Вторая глава</a></li></ol></nav></body></html>''')
        write("OPS/one.xhtml", '<html xmlns="http://www.w3.org/1999/xhtml"><head><title>Первая глава</title></head><body><h1>Первая глава</h1><p>Пробная книга для проверки читалки.</p></body></html>')
        write("OPS/two.xhtml", '<html xmlns="http://www.w3.org/1999/xhtml"><head><title>Вторая глава</title></head><body><h1>Вторая глава</h1><p>Проверка восстановления позиции.</p></body></html>')
    return stream.getvalue()


FB2 = '''<?xml version="1.0" encoding="utf-8"?>
<FictionBook xmlns="http://www.gribuser.ru/xml/fictionbook/2.0">
 <description><title-info><genre>sf</genre><author><first-name>Анна</first-name><last-name>Соколова</last-name></author>
 <book-title>Снежная книга</book-title><lang>ru</lang></title-info></description>
 <body><section><title><p>Глава 1</p></title><p>Здесь начинается книга.</p></section></body>
</FictionBook>'''.encode()


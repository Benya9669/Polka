import base64
import http.client
import ipaddress
import json
import os
import socket
import ssl
import tempfile
import time
from contextlib import contextmanager
from threading import BoundedSemaphore
from urllib.parse import quote, urljoin, urlsplit

from cryptography.fernet import Fernet, InvalidToken
from defusedxml import ElementTree as XML

from .db import DATA

ATOM = "http://www.w3.org/2005/Atom"
ACQ = "http://opds-spec.org/acquisition"
FACET = "http://opds-spec.org/facet"
ATOM_OPDS = "{http://opds-spec.org/2010/catalog}"
THREAD_COUNT = "{http://purl.org/syndication/thread/1.0}count"
SORT_RELATIONS = {f"http://opds-spec.org/sort/{kind}": kind for kind in ("new", "popular")}
XML_BASE = "{http://www.w3.org/XML/1998/namespace}base"
DOWNLOAD_RELS = {ACQ, ACQ + "/open-access", "acquisition", "download"}
IMAGE_RELS = {"http://opds-spec.org/image", "http://opds-spec.org/image/thumbnail", "http://opds-spec.org/cover"}
DCTERMS = "http://purl.org/dc/terms/"
MAX_FEED = 3 * 1024 * 1024
FETCH_SLOTS = BoundedSemaphore(10)


def cipher():
    path = DATA / "source.key"
    if not path.exists():
        DATA.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(dir=DATA, delete=False) as temp:
            temp.write(Fernet.generate_key())
        try:
            try:
                os.link(temp.name, path)
            except FileExistsError:
                pass
        finally:
            os.unlink(temp.name)
    return Fernet(path.read_bytes())


def seal(data):
    return cipher().encrypt(json.dumps(data, ensure_ascii=False).encode()).decode()


def unseal(token, ttl=None):
    try:
        return json.loads(cipher().decrypt(token.encode(), ttl=ttl))
    except (InvalidToken, ValueError):
        raise ValueError("Ссылка устарела. Откройте каталог заново.") from None


def origin(url):
    parts = urlsplit(url)
    if (parts.scheme not in ("http", "https") or not parts.hostname or parts.username is not None
            or "\\" in url or any(ch.isspace() or ord(ch) < 32 for ch in url) or len(url) > 5000):
        raise ValueError("Требуется корректный HTTP(S) URL")
    host = parts.hostname.encode("idna").decode().rstrip(".").lower()
    port = parts.port or (443 if parts.scheme == "https" else 80)
    return parts.scheme, host, port


def resolve(host, port):
    allowed = {}
    for rule in os.getenv("OPDS_PRIVATE_ALLOW", "").split(","):
        name, sep, address = rule.strip().partition("=")
        if sep:
            allowed.setdefault(name.lower(), set()).add(ipaddress.ip_address(address))
    infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    candidates = []
    for _, _, _, _, addr in infos:
        ip = ipaddress.ip_address(addr[0])
        effective = ip.ipv4_mapped if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped else ip
        public = (effective.is_global and not effective.is_multicast and not effective.is_reserved
                  and not effective.is_link_local and not effective.is_unspecified
                  and not (isinstance(ip, ipaddress.IPv6Address) and (ip.sixtofour or ip.teredo)))
        if not public and ip not in allowed.get(host, set()):
            raise ValueError("Адрес источника запрещён")
        if str(ip) not in candidates:
            candidates.append(str(ip))
    if not candidates:
        raise ValueError("Источник не имеет доступного адреса")
    return candidates


class PinnedHTTP(http.client.HTTPConnection):
    def __init__(self, host, port, ip, scheme):
        super().__init__(host, port, timeout=6)
        self.ip = ip
        self.scheme = scheme

    def connect(self):
        sock = socket.create_connection((self.ip, self.port), self.timeout)
        try:
            if ipaddress.ip_address(sock.getpeername()[0]) != ipaddress.ip_address(self.ip):
                raise OSError("unexpected peer")
            if self.scheme == "https":
                sock = ssl.create_default_context().wrap_socket(sock, server_hostname=self.host)
            self.sock = sock
        except BaseException:
            sock.close()
            raise


@contextmanager
def fetch(url, source_url, credential=None, *, auth_probe=False):
    if not FETCH_SLOTS.acquire(timeout=2):
        raise ConnectionError("Слишком много запросов к источникам")
    try:
        with connect(url, source_url, credential, auth_probe=auth_probe) as result:
            yield result
    finally:
        FETCH_SLOTS.release()


@contextmanager
def connect(url, source_url, credential, *, auth_probe=False):
    source_origin = origin(source_url)
    for _ in range(6):
        scheme, host, port = origin(url)
        parsed = urlsplit(url)
        path = quote(parsed.path or "/", safe="/%:@!$&'()*+,;=-._~")
        if parsed.query:
            path += "?" + quote(parsed.query, safe="/%?:@!$&'()*+,;=-._~[]")
        headers = {"User-Agent": "EBook OPDS/1.0", "Accept-Encoding": "identity"}
        if auth_probe:
            headers["Accept"] = "application/opds-authentication+json, application/vnd.opds.authentication.v1.0+json, application/opds+json, application/atom+xml, */*;q=0.1"
        if credential and (scheme, host, port) == source_origin:
            if scheme != "https":
                raise ValueError("Авторизация источника доступна только через HTTPS")
            if credential["kind"] == "basic":
                token = base64.b64encode(f'{credential["username"]}:{credential["password"]}'.encode()).decode()
                headers["Authorization"] = "Basic " + token
            else:
                headers["Authorization"] = "Bearer " + credential["password"]
        try:
            addresses = resolve(host, port)
            for index, ip in enumerate(addresses):
                connection = PinnedHTTP(host, port, ip, scheme)
                try:
                    connection.request("GET", path, headers=headers)
                    response = connection.getresponse()
                    break
                except (OSError, http.client.HTTPException):
                    connection.close()
                    if index == len(addresses) - 1:
                        raise
        except (OSError, http.client.HTTPException) as exc:
            raise ConnectionError("Источник недоступен") from exc
        try:
            if response.status in {301, 302, 303, 307, 308} and response.getheader("Location"):
                target = urljoin(url, response.getheader("Location"))
                if scheme == "https" and origin(target)[0] != "https":
                    raise ValueError("Перенаправление с HTTPS на HTTP запрещено")
                url = target
                continue
            auth_status = response.status in {401, 403}
            if auth_status and not auth_probe:
                raise PermissionError("Источник отклонил авторизацию")
            if response.status != 200 and not auth_status:
                if response.status in {408, 429, 500, 502, 503, 504}:
                    raise ConnectionError(f"Источник временно недоступен (HTTP {response.status})")
                raise ValueError(f"Источник вернул HTTP {response.status}")
            if response.getheader("Content-Encoding", "identity").lower() != "identity":
                raise ValueError("Неподдерживаемое HTTP-сжатие")
            yield response, url
            return
        finally:
            connection.close()
    raise ValueError("Слишком много перенаправлений")


def read_limited(response, limit, seconds=12):
    deadline = time.monotonic() + seconds
    raw = bytearray()
    while time.monotonic() <= deadline:
        try:
            chunk = response.read1(min(65536, limit + 1 - len(raw)))
        except (OSError, http.client.HTTPException) as exc:
            raise ConnectionError("Источник прервал соединение") from exc
        if not chunk:
            return bytes(raw)
        raw.extend(chunk)
        if len(raw) > limit:
            raise ValueError("Ответ источника слишком велик")
    raise TimeoutError("Источник не ответил вовремя")


def read_feed(url, source, secret):
    with fetch(url, source, secret) as (response, final_url):
        return read_limited(response, MAX_FEED), final_url


def nonnegative_int(value):
    text = str(value) if type(value) in (str, int) else ""
    return int(text) if len(text) <= 16 and text.isascii() and text.isdecimal() else None


def link_data(href, rel, title, kind, base, length=None):
    if not isinstance(href, str) or not href or len(href) > 5000:
        return None
    url = urljoin(base, href)
    try:
        origin(url)
    except ValueError:
        return None
    return {"url": url, "rels": rel if isinstance(rel, list) else [rel], "title": title or "",
            "type": kind if isinstance(kind, str) else "", "size": nonnegative_int(length)}


def atom_node(n, name):
    return n.find(f"{{{ATOM}}}{name}")


def atom_text(n, name):
    element = atom_node(n, name)
    return "".join(element.itertext()).strip() if element is not None else ""


def dc_text(n, name):
    element = n.find(f"{{{DCTERMS}}}{name}")
    return "".join(element.itertext()).strip() if element is not None else ""


def localized_text(value):
    if isinstance(value, str):
        return value.strip()
    if not isinstance(value, dict):
        return ""
    translations = {key.lower(): text.strip() for key, text in value.items()
                    if isinstance(text, str) and text.strip()}
    for language in ("ru", "en", "und"):
        if language in translations:
            return translations[language]
        regional = next((text for tag, text in translations.items() if tag.startswith(language + "-")), "")
        if regional:
            return regional
    return next(iter(translations.values()), "")


def json_series(meta):
    belongs = meta.get("belongsTo")
    series = belongs.get("series", []) if isinstance(belongs, dict) else []
    if not isinstance(series, list):
        series = [series]
    result = []
    for item in series[:20]:
        name = localized_text(item.get("name") if isinstance(item, dict) else item)
        position = item.get("position", "") if isinstance(item, dict) else ""
        if isinstance(name, str) and name.strip():
            result.append({"path": [name[:500]], "position": str(position)[:40] if type(position) in (str, int, float) else ""})
    return result


def atom_links(n, base):
    for node in n.findall(f"{{{ATOM}}}link"):
        link = link_data(node.get("href"), node.get("rel", "alternate"), node.get("title"), node.get("type"),
                         urljoin(base, node.get(XML_BASE, "")), node.get("length"))
        if link:
            if FACET in link["rels"]:
                link["facet_group"] = node.get(ATOM_OPDS + "facetGroup", "")
                link["active"] = node.get(ATOM_OPDS + "activeFacet") == "true"
                link["count"] = nonnegative_int(node.get(THREAD_COUNT))
            yield link


def atom_facets(links):
    groups = {}
    sorting = []
    for link in links:
        relation, = link["rels"]
        if relation == FACET and link["title"]:
            groups.setdefault(link["facet_group"], []).append(link)
        elif relation in SORT_RELATIONS:
            sorting.append({**link, "sort": SORT_RELATIONS[relation]})
    facets = [{"title": title, "links": links} for title, links in groups.items()]
    return [{"title": "", "sorting": True, "links": sorting}, *facets] if sorting else facets


def parse_atom(raw, url):
    root = XML.fromstring(raw)
    if root.tag != f"{{{ATOM}}}feed":
        raise ValueError("Ожидался каталог OPDS Atom")
    base = urljoin(url, root.get(XML_BASE, ""))
    links = list(atom_links(root, base))[:500]
    result = {"version": "1.x", "title": atom_text(root, "title"), "links": links,
              "facets": atom_facets(links), "navigation": [], "publications": []}
    for node in root.findall(f"{{{ATOM}}}entry")[:500]:
        title = atom_text(node, "title")
        links = list(atom_links(node, urljoin(base, node.get(XML_BASE, ""))))
        if any(any(isinstance(rel, str) and rel.startswith(ACQ) for rel in link["rels"]) for link in links):
            series = node.find("{http://calibre.kovidgoyal.net/2009/metadata}series")
            name = series.get("name", "") if series is not None else ""
            identifier = dc_text(node, "identifier")
            result["publications"].append({"id": atom_text(node, "id"), "title": title,
                "authors": [atom_text(a, "name") for a in node.findall(f"{{{ATOM}}}author")],
                "summary": atom_text(node, "summary") or atom_text(node, "content"), "links": links,
                "images": [link for link in links if any(isinstance(r, str) and r in IMAGE_RELS for r in link["rels"])],
                "language": dc_text(node, "language"), "publisher": dc_text(node, "publisher"),
                "isbn": identifier[9:] if identifier.lower().startswith("urn:isbn:") else "",
                "series_memberships": [{"path": [name[:500]], "position": series.get("index", "")[:40]}] if name else []})
        else:
            for link in links:
                if "atom+xml" in link["type"] or "opds+json" in link["type"]:
                    result["navigation"].append({**link, "title": link["title"] or title})
    return result


def parse_json(raw, url):
    data = json.loads(raw)
    if not isinstance(data, dict) or not isinstance(data.get("metadata"), dict) or not isinstance(data.get("links"), list):
        raise ValueError("Ожидался каталог OPDS 2.0")

    def links(objects):
        result = []
        for obj in objects:
            if not isinstance(obj, dict):
                continue
            properties = obj.get("properties")
            length = obj.get("length", properties.get("length") if isinstance(properties, dict) else None)
            link = link_data(obj.get("href"), obj.get("rel", ""), localized_text(obj.get("title")), obj.get("type"), url, length)
            if link:
                if isinstance(properties, dict):
                    link["count"] = nonnegative_int(properties.get("numberOfItems"))
                result.append(link)
        return result

    def collection(parent, key):
        values = parent.get(key, [])
        return values[:500] if isinstance(values, list) else []

    metadata = data["metadata"]
    publications, navigation = [], []
    for group in [data, *collection(data, "groups")]:
        if not isinstance(group, dict):
            continue
        navigation.extend(links(collection(group, "navigation")))
        for book in collection(group, "publications"):
            if not isinstance(book, dict) or not isinstance(meta := book.get("metadata"), dict):
                continue
            authors = meta.get("author", [])
            if not isinstance(authors, list):
                authors = [authors]
            language = meta.get("language", "")
            if isinstance(language, list):
                language = next((lang for lang in language if isinstance(lang, str)), "")
            publishers = meta.get("publisher", [])
            if not isinstance(publishers, list):
                publishers = [publishers]
            names = (localized_text(item.get("name") if isinstance(item, dict) else item) for item in publishers[:20])
            publisher = next(filter(None, names), "")
            alternates = meta.get("altIdentifier")
            identifiers = [meta.get("identifier"), *(alternates[:20] if isinstance(alternates, list) else [])]
            isbn = next((value[9:] for value in identifiers if isinstance(value, str) and value.lower().startswith("urn:isbn:")), "")
            publications.append({"id": meta.get("identifier", ""), "title": localized_text(meta.get("title")),
                "summary": localized_text(meta.get("description")),
                "authors": [localized_text(a.get("name") if isinstance(a, dict) else a) for a in authors],
                "links": links(collection(book, "links")), "images": links(collection(book, "images")),
                "language": language, "publisher": publisher, "isbn": isbn, "series_memberships": json_series(meta)})
    facets = []
    for group in collection(data, "facets"):
        if not isinstance(group, dict) or not isinstance(meta := group.get("metadata"), dict):
            continue
        options = [{**link, "active": "self" in link["rels"]}
                   for link in links(collection(group, "links")) if link["title"]]
        if options:
            facets.append({"title": localized_text(meta.get("title")), "links": options})
    return {"version": "2.0", "title": localized_text(metadata.get("title")), "links": links(data["links"][:500]),
            "facets": facets, "publications": publications[:500], "navigation": navigation[:500]}


def read_catalog(url, source, secret):
    raw, final_url = read_feed(url, source, secret)
    data = parse_json(raw, final_url) if raw.lstrip(b"\xef\xbb\xbf \t\r\n").startswith(b"{") else parse_atom(raw, final_url)
    return data


def search_url(link, query, source, secret):
    template = link["url"]
    if "{searchTerms}" not in template and "{?query}" not in template and "{query}" not in template:
        raw, final = read_feed(template, source, secret)
        root = XML.fromstring(raw)
        option = next((n for n in root.findall("{*}Url") if "atom+xml" in n.get("type", "")
                       and "{searchTerms}" in n.get("template", "")), None)
        if option is None:
            raise ValueError("Источник не предоставляет поиск по книгам")
        template = urljoin(final, option.attrib["template"])
    encoded = quote(query, safe="")
    import re
    template = template.replace("{?query}", "?query=" + encoded).replace("{query}", encoded)
    template = re.sub(r"\{searchTerms\??\}", encoded, template)
    template = re.sub(r"\{[^{}]+\?\}", "", template)
    if "{" in template or "}" in template:
        raise ValueError("Шаблон поиска источника не поддерживается")
    return template


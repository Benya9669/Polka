import json
import re
from urllib.parse import urljoin

from .remote import MAX_FEED, fetch, origin, parse_atom, parse_json, read_limited

AUTH_REL = "http://opds-spec.org/auth/document"
AUTH_TYPES = {"application/opds-authentication+json", "application/vnd.opds.authentication.v1.0+json"}
BASIC = "http://opds-spec.org/auth/basic"
MAX_AUTH = 128 * 1024
HEADER_LINK = re.compile(r'\s*<([^>]*)>((?:[^",]|"(?:[^"\\]|\\.)*")*)(?:,|$)')
HEADER_PARAM = re.compile(r';\s*([\w-]+)\s*=\s*(?:"((?:[^"\\]|\\.)*)"|([^\s;,]+))')


def media_type(value):
    return value.split(";", 1)[0].strip().lower()


def text(value, limit=500):
    return value.strip()[:limit] if isinstance(value, str) else ""


def objects(items, limit=30):
    if isinstance(items, list):
        return (item for item in items[:limit] if isinstance(item, dict))
    return ()


def secure_link(base, href):
    if not isinstance(href, str) or not href or len(href) > 5000:
        return None
    try:
        url = urljoin(base, href)
        if origin(url)[0] == "https":
            return url
    except ValueError:
        pass
    return None


def header_document(value, base):
    for href, attrs in HEADER_LINK.findall(value):
        params = {name.lower(): quoted or bare for name, quoted, bare in HEADER_PARAM.findall(attrs)}
        if AUTH_REL in params.get("rel", "").split() and (
            not params.get("type") or media_type(params["type"]) in AUTH_TYPES
        ):
            if target := secure_link(base, href):
                return target
    return None


def parse_document(raw, base):
    data = json.loads(raw)
    if not isinstance(data, dict) or not isinstance(data.get("authentication"), list):
        raise ValueError("Некорректный документ OPDS Authentication")
    methods = []
    for item in objects(data["authentication"]):
        kind = text(item.get("type"), 200)
        if not kind:
            continue
        labels = item.get("labels")
        labels = labels if isinstance(labels, dict) else {}
        methods.append({"type": kind, "kind": "basic" if kind == BASIC else None,
                        "login": text(labels.get("login"), 100), "password": text(labels.get("password"), 100)})
    links = {}
    for link in objects(data.get("links")):
        relation = link.get("rel")
        relations = relation if isinstance(relation, list) else text(relation).split()
        for rel in ("help", "register", "logo"):
            if rel in relations and rel not in links and (href := secure_link(base, link.get("href"))):
                links[rel] = href
    return {"title": text(data.get("title"), 200), "description": text(data.get("description"), 2000),
            "methods": methods, "links": links}


def challenge_document(challenge):
    methods = []
    for kind in ("basic", "bearer"):
        if re.search(r"(?:^|,)\s*" + kind + r"(?=\s|,|$)", challenge, re.IGNORECASE):
            methods.append({"type": kind, "kind": kind, "login": "", "password": ""})
    return {"title": "", "description": "", "methods": methods, "links": {}} if methods else None


def discover(url):
    source_origin = origin(url)
    with fetch(url, url, auth_probe=True) as (response, final_url):
        protected = response.status in {401, 403}
        result = {"secure": source_origin[0] == "https", "protected": protected,
                  "same_origin": source_origin == origin(final_url)}
        content_type = media_type(response.getheader("Content-Type", ""))
        if content_type in AUTH_TYPES:
            document = parse_document(read_limited(response, MAX_AUTH), final_url)
            return {**result, "document": document}
        link = header_document(response.getheader("Link", ""), final_url)
        challenge = response.getheader("WWW-Authenticate", "")
        if not link and not protected:
            raw = read_limited(response, MAX_FEED)
            is_json = raw.lstrip(b"\xef\xbb\xbf \t\r\n").startswith(b"{")
            feed = parse_json(raw, final_url) if is_json else parse_atom(raw, final_url)
            link = next((href for item in feed["links"] if AUTH_REL in item["rels"]
                         and (href := secure_link(final_url, item["url"]))), None)
    document = challenge_document(challenge) if protected else None
    if link:
        with fetch(link, link, auth_probe=True) as (response, document_url):
            document = parse_document(read_limited(response, MAX_AUTH), document_url)
    return {**result, "document": document}

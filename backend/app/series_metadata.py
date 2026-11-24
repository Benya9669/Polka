def fb2_memberships(metadata):
    pending = [(node, []) for node in reversed(metadata.findall("./{*}sequence"))]
    items = []
    while pending and len(items) < 64:
        node, ancestors = pending.pop()
        name = node.get("name", "").strip()[:500]
        if not name:
            continue
        path = [*ancestors, name]
        children = node.findall("./{*}sequence") if len(path) < 16 else []
        position = node.get("number", "").strip()[:40]
        if not children or position:
            items.append({"path": path, "position": position})
        pending.extend((child, path) for child in reversed(children))
    return items


def epub_memberships(metadata, props):
    collections, parents, positions = [], {}, {}
    for node in metadata.findall("./{*}meta"):
        prop = node.get("property")
        ref = node.get("refines", "")
        value = (node.text or node.get("content", "")).strip()[:500]
        if prop == "belongs-to-collection" and value:
            if ref.startswith("#"):
                parents.setdefault(ref[1:], []).append((node.get("id"), value))
            elif not ref:
                collections.append((node.get("id"), value))
        elif prop == "group-position" and ref.startswith("#"):
            positions[ref[1:]] = value
    items = []
    for ident, name in collections[:64]:
        position = positions.get(ident, "")
        if len(collections) == 1 and not position:
            position = next(((node.text or "").strip() for node in metadata.findall("./{*}meta")
                             if node.get("property") == "group-position" and not node.get("refines")), "")
        pending = [(ident, [name], frozenset({ident}))]
        while pending and len(items) < 64:
            current, path, seen = pending.pop()
            above = [(key, label) for key, label in parents.get(current, []) if key is None or key not in seen]
            if above and len(path) < 16:
                remaining = 64 - len(items) - len(pending)
                pending.extend((key, [label, *path], seen | {key}) for key, label in reversed(above[:remaining]))
            else:
                items.append({"path": path, "position": position[:40]})
            if len(items) >= 64:
                return items
    if not items and props.get("calibre:series"):
        items.append({"path": [props["calibre:series"]], "position": props.get("calibre:series_index", "")})
    return items


def series_metadata(items):
    if not items:
        return {}
    return {"series_memberships": items, "series": items[0]["path"][0], "series_index": items[0]["position"]}

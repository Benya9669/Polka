import hashlib
import unicodedata


def normal(text):
    return " ".join(unicodedata.normalize("NFKC", text).casefold().split())


def normalized_key(text):
    return hashlib.sha256(normal(text).encode("utf-8")).hexdigest()

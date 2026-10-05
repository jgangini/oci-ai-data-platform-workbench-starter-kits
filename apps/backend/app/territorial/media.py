"""Photo references from supported connectors; no server-side image downloads."""
from urllib.parse import urlsplit


def photos(platform, value):
    if platform != "x" or not isinstance(value, list):
        return []
    result = []
    for item in value[:4]:
        if not isinstance(item, dict) or item.get("type") != "photo":
            continue
        url = str(item.get("url", ""))
        try:
            parsed = urlsplit(url)
            valid = (len(url) <= 2048 and parsed.scheme == "https" and parsed.hostname == "pbs.twimg.com"
                     and not parsed.username and not parsed.password and parsed.port in {None, 443}
                     and parsed.path.startswith("/media/") and not parsed.fragment)
        except ValueError:
            valid = False
        if valid and url not in {entry["url"] for entry in result}:
            result.append({"type": "photo", "url": url, "alt_text": str(item.get("alt_text") or "Foto de la publicación original")[:500]})
    return result

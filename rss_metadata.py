"""Extract common and Podcasting 2.0 metadata from RSS XML nodes."""

from urllib.parse import urljoin

from background import encode_metadata, metadata_envelope, plain_text, sanitize_html


MAX_TEXT_CHARS = 200_000


def _local_name(tag) -> str:
    return (getattr(tag, "name", "") or "").split(":")[-1].lower()


def _tags(parent, *names):
    wanted = {name.lower() for name in names}
    return [tag for tag in parent.find_all() if _local_name(tag) in wanted]


def _text(parent, *names) -> str:
    tags = _tags(parent, *names)
    return tags[0].get_text(strip=True)[:MAX_TEXT_CHARS] if tags else ""


def _direct_text(parent, *names) -> str:
    wanted = {name.lower() for name in names}
    for tag in parent.find_all(recursive=False):
        if _local_name(tag) in wanted:
            return tag.get_text(strip=True)[:MAX_TEXT_CHARS]
    return ""


def _attr_url(parent, names, attributes=("url", "href")) -> str:
    for tag in _tags(parent, *names):
        for attribute in attributes:
            value = tag.get(attribute, "")
            if value:
                return value
    return ""


def _people(item, channel) -> list[dict]:
    nodes = _tags(item, "person") or _tags(channel, "person")
    people = []
    for node in nodes:
        name = node.get_text(strip=True)
        if name:
            people.append({
                "name": name[:128],
                "role": node.get("role", "host"),
                "group": node.get("group", "cast"),
                "image": node.get("img", ""),
                "url": node.get("href", ""),
            })
    return people


def extract_rss_metadata(channel, item, feed_url: str) -> dict:
    description_html = sanitize_html(
        _direct_text(item, "encoded")
        or _direct_text(item, "description")
        or _direct_text(item, "summary")
    )
    podcast_description = _direct_text(channel, "description", "summary")
    author = (
        _direct_text(item, "author", "creator")
        or _direct_text(channel, "author", "creator", "managingeditor")
    )
    webpage_url = _direct_text(item, "link")
    cover_url = (
        _attr_url(item, ("image", "thumbnail"))
        or _attr_url(channel, ("image", "thumbnail"))
    )
    if not cover_url:
        image = next((tag for tag in channel.find_all(recursive=False)
                      if _local_name(tag) == "image"), None)
        if image:
            cover_url = _text(image, "url")

    transcripts = []
    for node in _tags(item, "transcript"):
        url = node.get("url", "")
        if url:
            transcripts.append({
                "url": urljoin(feed_url, url),
                "type": node.get("type", ""),
                "language": node.get("language", ""),
                "rel": node.get("rel", ""),
                "text_source": "rss",
            })
    chapters = []
    for node in _tags(item, "chapters"):
        url = node.get("url", "")
        if url:
            chapters.append({
                "url": urljoin(feed_url, url),
                "type": node.get("type", "application/json+chapters"),
            })
    license_node = next(iter(_tags(item, "license") or _tags(channel, "license")), None)
    license_data = {}
    if license_node:
        license_data = {
            "name": license_node.get_text(strip=True)[:128],
            "url": license_node.get("url", ""),
        }
    categories = [tag.get_text(strip=True) for tag in _tags(item, "category")
                  if tag.get_text(strip=True)]
    common = {
        "description": plain_text(description_html),
        "description_html": description_html,
        "webpage_url": urljoin(feed_url, webpage_url) if webpage_url else "",
        "author": author,
        "cover_url": urljoin(feed_url, cover_url) if cover_url else "",
        "podcast_title": _direct_text(channel, "title"),
        "podcast_description": plain_text(podcast_description),
        "people": _people(item, channel),
        "categories": categories,
        "license": license_data,
    }
    source_data = {
        "feed_url": feed_url,
        "subtitle": _direct_text(item, "subtitle"),
        "episode_number": _direct_text(item, "episode"),
        "season_number": _direct_text(item, "season"),
        "episode_type": _direct_text(item, "episodeType", "episodetype"),
        "explicit": _direct_text(item, "explicit") or _direct_text(channel, "explicit"),
        "copyright": _direct_text(channel, "copyright"),
    }
    return metadata_envelope(
        "rss", common=common, source_data=source_data,
        assets={"transcripts": transcripts, "chapters": chapters},
        text_source="rss",
    )


def apply_rss_metadata(record, channel, item, feed_url: str):
    metadata = extract_rss_metadata(channel, item, feed_url)
    common = metadata["common"]
    record.webpage_url = common["webpage_url"]
    record.description = common["description"]
    record.author = common["author"]
    record.cover_url = common["cover_url"]
    record.metadata_json = encode_metadata(metadata)
    return record

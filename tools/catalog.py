"""Check and publish worlds for the Omnix community catalog (standard library only).

    python tools/catalog.py validate submissions/<id>.json [--pr-author LOGIN]
    python tools/catalog.py publish submissions/<id>.json
    python tools/catalog.py publish-file <bundle.zip> --id ID --author LOGIN [--title ...] [--summary ...]

`validate` downloads the bundle a submission names and checks it: its size and SHA-256 match the submission, it is
an Omnix world bundle whose manifest checksums hold, and the world id is free or already the author's. `publish`
does the same, then attaches the bundle (and its cover image) to a release of this repository with the GitHub CLI,
lists it in index.json and removes the submission. `publish-file` lists a bundle a maintainer has locally.
"""
from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
import re
import subprocess
import sys
import tempfile
import urllib.request
import zipfile
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
# The pull request check runs the checker from main outside the checkout, naming the index explicitly.
INDEX = Path(os.environ.get("CATALOG_INDEX") or ROOT / "index.json")
CATALOG_SCHEMA = "omnix-world-catalog-v1"
SUBMISSION_SCHEMA = "omnix-world-submission-v1"
MAX_BUNDLE_BYTES = 512 * 1024 * 1024
MAX_COVER_BYTES = 8 * 1024 * 1024
MAX_ENTRIES = 2_000
ID_RE = re.compile(r"^[a-z0-9][a-z0-9-]{1,62}[a-z0-9]$")
LOGIN_RE = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9-]{0,38})$")
RELEASE_URL_RE = re.compile(
    r"^https://github\.com/(?P<owner>[A-Za-z0-9-]+)/(?P<repo>[A-Za-z0-9._-]+)/releases/download/[^/?#]+/[^/?#]+$"
)
COVER_TYPES = {"image/webp": "webp", "image/png": "png", "image/jpeg": "jpg"}


class CatalogError(ValueError):
    pass


def _repo() -> str:
    return os.environ.get("GITHUB_REPOSITORY") or "autonomx/omnix-worlds"


def load_index() -> dict[str, Any]:
    if not INDEX.exists():
        return {"schema_version": CATALOG_SCHEMA, "updated_at": "", "worlds": []}
    index = json.loads(INDEX.read_text(encoding="utf-8"))
    if index.get("schema_version") != CATALOG_SCHEMA:
        raise CatalogError(f"index.json has schema {index.get('schema_version')!r}")
    return index


def _download(url: str, limit: int) -> bytes:
    request = urllib.request.Request(url, headers={"User-Agent": "omnix-worlds-catalog"})
    with urllib.request.urlopen(request, timeout=120) as response:
        data = response.read(limit + 1)
    if len(data) > limit:
        raise CatalogError(f"{url} is larger than {limit} bytes")
    return data


def check_bundle(content: bytes) -> dict[str, Any]:
    """An Omnix world bundle whose manifest checksums hold; returns its world and its cover image, if any."""
    try:
        archive = zipfile.ZipFile(io.BytesIO(content))
    except zipfile.BadZipFile as exc:
        raise CatalogError("the bundle is not a zip file") from exc
    names = archive.namelist()
    if len(names) > MAX_ENTRIES:
        raise CatalogError("the bundle has too many files")
    for name in names:
        path = PurePosixPath(name)
        if path.is_absolute() or ".." in path.parts or "\\" in name:
            raise CatalogError(f"unsafe path in bundle: {name}")
    if sum(info.file_size for info in archive.infolist()) > MAX_BUNDLE_BYTES:
        raise CatalogError("the bundle unpacks to more than the size limit")
    try:
        manifest = json.loads(archive.read("manifest.json"))
    except KeyError as exc:
        raise CatalogError("the bundle has no manifest.json") from exc
    if manifest.get("format") != "omnix_rpg_world_bundle" or not isinstance(manifest.get("version"), int):
        raise CatalogError("manifest.json is not an Omnix world bundle manifest")
    data_path = str(manifest.get("data_path") or "world.json")
    data = archive.read(data_path)
    if hashlib.sha256(data).hexdigest() != manifest.get("data_sha256"):
        raise CatalogError("world.json does not match its manifest checksum")
    assets = {}
    for asset in manifest.get("assets") or []:
        body = archive.read(str(asset["archive_path"]))
        if hashlib.sha256(body).hexdigest() != asset.get("checksum_sha256"):
            raise CatalogError(f"{asset['archive_path']} does not match its manifest checksum")
        assets[str(asset.get("asset_id"))] = (asset, body)
    world = dict(json.loads(data).get("world") or {})
    if not world.get("id") or not world.get("title"):
        raise CatalogError("world.json has no world id or title")
    metadata = dict(world.get("metadata") or {})
    cover_id = str(metadata.get("cover_image_asset_id") or "")
    cover = assets.get(cover_id) or next(
        (item for item in assets.values() if dict(item[0].get("metadata") or {}).get("kind") == "cover"), None
    )
    if cover and (cover[0].get("mime_type") not in COVER_TYPES or len(cover[1]) > MAX_COVER_BYTES):
        cover = None
    return {"world": world, "cover": cover}


def check_submission(submission: dict[str, Any], index: dict[str, Any], *, pr_author: str = "") -> dict[str, Any]:
    if submission.get("schema_version") != SUBMISSION_SCHEMA:
        raise CatalogError(f"schema_version must be {SUBMISSION_SCHEMA}")
    world_id = str(submission.get("id") or "")
    author = str(submission.get("author") or "")
    if not ID_RE.fullmatch(world_id):
        raise CatalogError("id must be 3-64 lowercase letters, digits and dashes")
    if not LOGIN_RE.fullmatch(author):
        raise CatalogError("author must be a GitHub login")
    if pr_author and pr_author.casefold() != author.casefold():
        raise CatalogError("author must be the pull request's author")
    if not 1 <= len(str(submission.get("title") or "")) <= 200 or len(str(submission.get("summary") or "")) > 2000:
        raise CatalogError("title must be 1-200 characters and summary at most 2000")
    bundle = dict(submission.get("bundle") or {})
    match = RELEASE_URL_RE.fullmatch(str(bundle.get("url") or ""))
    if not match:
        raise CatalogError("bundle.url must be a GitHub release download")
    if match["owner"].casefold() != author.casefold() and f"{match['owner']}/{match['repo']}" != _repo():
        raise CatalogError("bundle.url must be a release on the author's own fork")
    size = int(bundle.get("bytes") or 0)
    if not 1 <= size <= MAX_BUNDLE_BYTES or not re.fullmatch(r"[0-9a-f]{64}", str(bundle.get("sha256") or "")):
        raise CatalogError("bundle.bytes and bundle.sha256 are required")
    listed = next((item for item in index.get("worlds") or [] if item.get("id") == world_id), None)
    if listed and str(listed.get("author") or "").casefold() != author.casefold():
        raise CatalogError(f"the id {world_id!r} belongs to @{listed.get('author')}; choose another")
    content = _download(str(bundle["url"]), size)
    if len(content) != size or hashlib.sha256(content).hexdigest() != bundle["sha256"]:
        raise CatalogError("the downloaded bundle does not match bundle.bytes and bundle.sha256")
    return {"content": content, "listed": listed, **check_bundle(content)}


def _release(tag: str, title: str, notes: str, files: list[Path]) -> None:
    subprocess.run(
        ["gh", "release", "create", tag, *map(str, files), "--repo", _repo(), "--title", title, "--notes", notes],
        check=True,
    )


def publish(entry: dict[str, Any], content: bytes, cover: tuple[dict[str, Any], bytes] | None, listed: dict | None) -> dict:
    index = load_index()
    version = int(listed.get("version") or 0) + 1 if listed else 1
    tag = f"world-{entry['id']}-v{version}"
    base = f"https://github.com/{_repo()}/releases/download/{tag}"
    with tempfile.TemporaryDirectory() as folder:
        files = [Path(folder) / f"{entry['id']}.omnix-world.zip"]
        files[0].write_bytes(content)
        record = {
            **entry,
            "version": version,
            "published_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "bundle": {"url": f"{base}/{files[0].name}", "sha256": hashlib.sha256(content).hexdigest(), "bytes": len(content)},
        }
        if cover:
            cover_file = Path(folder) / f"{entry['id']}-cover.{COVER_TYPES[cover[0]['mime_type']]}"
            cover_file.write_bytes(cover[1])
            files.append(cover_file)
            record["cover"] = {"url": f"{base}/{cover_file.name}", "sha256": hashlib.sha256(cover[1]).hexdigest(), "bytes": len(cover[1])}
        _release(tag, f"{entry['title']} (version {version})", f"{entry.get('summary') or ''}\n\nShared by @{entry['author']}.", files)
    index["worlds"] = sorted(
        [item for item in index.get("worlds") or [] if item.get("id") != entry["id"]] + [record],
        key=lambda item: str(item.get("title") or "").casefold(),
    )
    index["updated_at"] = record["published_at"]
    INDEX.write_text(json.dumps(index, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return record


def _entry(submission: dict[str, Any], world: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": submission["id"],
        "title": str(submission.get("title") or world.get("title")),
        "summary": str(submission.get("summary") or world.get("description") or "")[:2000],
        "genre": str(submission.get("genre") or world.get("genre") or "")[:120],
        "tone": str(submission.get("tone") or world.get("tone") or "")[:200],
        "author": submission["author"],
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    commands = parser.add_subparsers(dest="command", required=True)
    validate_cmd = commands.add_parser("validate")
    validate_cmd.add_argument("submissions", nargs="+", type=Path)
    validate_cmd.add_argument("--pr-author", default="")
    publish_cmd = commands.add_parser("publish")
    publish_cmd.add_argument("submissions", nargs="+", type=Path)
    file_cmd = commands.add_parser("publish-file")
    file_cmd.add_argument("bundle", type=Path)
    file_cmd.add_argument("--id", required=True)
    file_cmd.add_argument("--author", required=True)
    file_cmd.add_argument("--title", default="")
    file_cmd.add_argument("--summary", default="")
    args = parser.parse_args(argv)

    index = load_index()
    try:
        if args.command == "publish-file":
            content = args.bundle.read_bytes()
            checked = check_bundle(content)
            listed = next((item for item in index.get("worlds") or [] if item.get("id") == args.id), None)
            if listed and listed.get("author") != args.author:
                raise CatalogError(f"the id {args.id!r} belongs to @{listed.get('author')}")
            submission = {"id": args.id, "author": args.author, "title": args.title, "summary": args.summary}
            if not ID_RE.fullmatch(args.id):
                raise CatalogError("id must be 3-64 lowercase letters, digits and dashes")
            record = publish(_entry(submission, checked["world"]), content, checked["cover"], listed)
            print(f"listed {record['id']} version {record['version']}")
            return 0
        for path in args.submissions:
            submission = json.loads(path.read_text(encoding="utf-8"))
            checked = check_submission(submission, index, pr_author=getattr(args, "pr_author", ""))
            if args.command == "validate":
                print(f"ok: {path.name}: {checked['world']['title']} ({len(checked['content']):,} bytes)")
                continue
            record = publish(_entry(submission, checked["world"]), checked["content"], checked["cover"], checked["listed"])
            path.unlink()
            index = load_index()
            print(f"listed {record['id']} version {record['version']}")
    except (CatalogError, KeyError, json.JSONDecodeError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())

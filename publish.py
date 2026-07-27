#!/usr/bin/env python3
"""Publish a MicroPythonOS app directory to BadgeHub.eu (draft -> publish).

Reads name/description/version/entrypoint from the app's own MANIFEST.JSON
so callers only need to supply BadgeHub-specific extras. Talks to the
BadgeHub v3 API (https://badgehub.eu/api-docs/swagger.json) using only the
standard library, so the composite action needs no extra pip installs.

Field-name note: the published swagger spec doesn't document the multipart
field name for file uploads (it's wired via `multer.single("file")` outside
BadgeHub's typed contract, invisible to their OpenAPI generator -- see
BadgeHubCrew/badgehub-app#7). Confirmed directly against BadgeHub's own
frontend (`formData.append("file", file)`) and backend source, not guessed.

User-Agent note: badgehub.eu sits behind Cloudflare, which 403s requests
carrying Python's default `Python-urllib/3.x` User-Agent (or none at all)
with Cloudflare error code 1010. Any non-default value clears it -- this
isn't spoofing a browser, just identifying the client honestly instead of
using urllib's default.

Run as `python3 -u` (see action.yml) rather than adding flush=True to
every print: stdout is block-buffered when piped to a CI log instead of a
tty, so without -u, output (including a traceback right before a crash)
can sit in the buffer and never reach the log at all.
"""
import json
import mimetypes
import os
import sys
import time
import traceback
import urllib.error
import urllib.parse
import urllib.request
import uuid

DRY_RUN = os.environ.get("BADGEHUB_DRY_RUN", "").lower() in ("1", "true", "yes")
USER_AGENT = "mpos-badgehub-publish"


def request(method, url, token, json_body=None, multipart_file=None):
    if DRY_RUN:
        print(f"  [dry-run] {method} {url}")
        return 204, b""

    headers = {"badgehub-api-token": token, "User-Agent": USER_AGENT}
    data = None
    if json_body is not None:
        data = json.dumps(json_body).encode()
        headers["Content-Type"] = "application/json"
    elif multipart_file is not None:
        file_name, content = multipart_file
        boundary = uuid.uuid4().hex
        headers["Content-Type"] = f"multipart/form-data; boundary={boundary}"
        mimetype = mimetypes.guess_type(file_name)[0] or "application/octet-stream"
        body = b"".join(
            [
                f"--{boundary}\r\n".encode(),
                f'Content-Disposition: form-data; name="file"; filename="{file_name}"\r\n'.encode(),
                f"Content-Type: {mimetype}\r\n\r\n".encode(),
                content,
                f"\r\n--{boundary}--\r\n".encode(),
            ]
        )
        data = body

    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return resp.status, resp.read()
    except urllib.error.HTTPError as error:
        # Not logged here -- every caller already checks `if status != 204`
        # and reports it via fail(), which prints the status/body itself.
        return error.code, error.read()
    except Exception as error:
        # Catch everything, not just HTTPError -- a connection reset, an
        # SSL error, or a bare timeout would otherwise propagate as a raw
        # exception. Full context (nothing else will have mentioned this
        # URL, since the routine per-request logging was removed as noise
        # once the actual bugs were found): a connection failure here is
        # the one failure mode that left zero trace in the log
        # historically, right when BadgeHub had actually already
        # processed the request server-side.
        print(f"  {method} {url} -> {type(error).__name__}: {error}", file=sys.stderr)
        return -1, str(error).encode()


def quote_path(rel_path):
    # BadgeHub's {filePath} route param is a single Express path segment, so
    # a raw "/" from a subdirectory (e.g. sounds/BombDrop.wav) splits the URL
    # into an extra, unmatched segment -- a plain f-string embed 404s with
    # "Cannot POST". Percent-encoding the whole relative path as one opaque
    # token (matching how a client library like their own ts-rest one would
    # build such a URL) keeps it a single segment for routing purposes.
    return urllib.parse.quote(rel_path, safe="")


def iter_app_files(app_dir):
    for root, dirs, files in os.walk(app_dir):
        dirs[:] = [d for d in dirs if d != "__pycache__"]
        for name in sorted(files):
            if name.endswith(".pyc"):
                continue
            full = os.path.join(root, name)
            rel = os.path.relpath(full, app_dir).replace(os.sep, "/")
            yield full, rel


def build_metadata(app_dir):
    with open(os.path.join(app_dir, "MANIFEST.JSON")) as handle:
        manifest = json.load(handle)
    entrypoint = manifest["activities"][0]["entrypoint"]

    metadata = {
        "project_type": "app",
        "name": manifest.get("name"),
        "description": manifest.get("short_description", ""),
        "long_description": manifest.get("long_description", ""),
        "author": manifest.get("publisher", ""),
        "version": manifest.get("version"),
        "badges": [b.strip() for b in os.environ.get("BADGEHUB_BADGES", "mpos_api_0").split(",") if b.strip()],
        "application": [{"type": "micropython", "executable": entrypoint}],
    }

    icon_name = os.environ.get("BADGEHUB_ICON", "icon_64x64.png")
    if icon_name and os.path.exists(os.path.join(app_dir, icon_name)):
        metadata["icon_map"] = {"64x64": icon_name}

    categories = os.environ.get("BADGEHUB_CATEGORIES", "")
    if categories:
        metadata["categories"] = [c.strip() for c in categories.split(",") if c.strip()]

    git_url = os.environ.get("BADGEHUB_GIT_URL", "")
    if not git_url:
        # No explicit git-url input -- default to the calling repo. GitHub
        # Actions sets GITHUB_SERVER_URL/GITHUB_REPOSITORY on every job step
        # (including composite action steps), so callers on github.com get
        # this for free without adding a redundant input for something the
        # runner already knows.
        server_url = os.environ.get("GITHUB_SERVER_URL", "")
        repository = os.environ.get("GITHUB_REPOSITORY", "")
        if server_url and repository:
            git_url = f"{server_url}/{repository}"
    if git_url:
        metadata["git_url"] = git_url

    license_type = os.environ.get("BADGEHUB_LICENSE_TYPE", "")
    if license_type:
        metadata["license_type"] = license_type

    return metadata


def fail(message, status, body):
    print(f"{message}: HTTP {status} {body!r}", file=sys.stderr)
    sys.exit(1)


def get_draft_file_paths(api_base, slug, token):
    if DRY_RUN:
        print(f"  [dry-run] GET {api_base}/projects/{slug}/draft")
        return set()
    status, body = request("GET", f"{api_base}/projects/{slug}/draft", token)
    if status != 200:
        fail("Failed to fetch current draft file list", status, body)
    data = json.loads(body)
    return {f["full_path"] for f in data.get("version", {}).get("files", [])}


def get_latest_revision(api_base, slug):
    # Public, unauthenticated endpoint -- also used as a pre-flight
    # reachability check and a post-publish sanity check, since a 204 from
    # the publish call alone doesn't prove the new content actually landed.
    if DRY_RUN:
        print(f"  [dry-run] GET {api_base}/project-latest-revisions/{slug}")
        return None
    req = urllib.request.Request(
        f"{api_base}/project-latest-revisions/{slug}",
        headers={"User-Agent": USER_AGENT},
        method="GET",
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError:
        return None
    except Exception as error:
        print(f"  revision check failed: {type(error).__name__}: {error}", file=sys.stderr)
        return None


def wait_for_revision_advance(api_base, slug, previous_revision):
    # BadgeHub's publish endpoint returns 204 before the public
    # revision-check endpoint necessarily reflects it -- confirmed on the
    # v0.1.3 and v0.1.4 bomberboy releases, where publish had genuinely
    # succeeded (verified independently afterwards) but an immediate check
    # right after publish still saw the pre-publish revision.
    delays = (1, 2, 3, 5, 5)
    new_revision = None
    for attempt, delay in enumerate(delays, start=1):
        new_revision = get_latest_revision(api_base, slug)
        if new_revision is not None and (previous_revision is None or new_revision > previous_revision):
            return new_revision
        if attempt < len(delays):
            print(f"  revision not advanced yet (attempt {attempt}), retrying in {delay}s...")
            time.sleep(delay)
    return new_revision


def main():
    api_base = os.environ["BADGEHUB_API_BASE"].rstrip("/")
    slug = os.environ["BADGEHUB_SLUG"]
    token = os.environ["BADGEHUB_API_TOKEN"]
    app_dir = os.environ["BADGEHUB_APP_DIR"]

    previous_revision = get_latest_revision(api_base, slug)

    def upload_file(full_path, rel_path):
        with open(full_path, "rb") as handle:
            content = handle.read()
        status, body = request(
            "POST",
            f"{api_base}/projects/{slug}/draft/files/{quote_path(rel_path)}",
            token,
            multipart_file=(os.path.basename(rel_path), content),
        )
        if status != 204:
            fail(f"Failed to upload {rel_path}", status, body)
        print(f"  uploaded {rel_path}")

    local_paths = set()
    print(f"Uploading files from '{app_dir}' to BadgeHub project '{slug}'...")
    for full_path, rel_path in iter_app_files(app_dir):
        local_paths.add(rel_path)
        upload_file(full_path, rel_path)

    mpk_path = os.environ.get("BADGEHUB_MPK_PATH", "")
    badges = [b.strip() for b in os.environ.get("BADGEHUB_BADGES", "mpos_api_0").split(",") if b.strip()]
    if mpk_path:
        # The on-device AppManager doesn't get a .mpk from BadgeHub for
        # free -- it installs by scanning the project's uploaded files for
        # one with a .mpk/.zip extension and downloading that specific
        # file (see appstore_core.py's _find_download_file). Without this,
        # nothing is actually installable even after a successful publish.
        mpk_name = os.path.basename(mpk_path)
        local_paths.add(mpk_name)
        upload_file(mpk_path, mpk_name)
    elif "mpos_api_0" in badges:
        # Only warn for MicroPythonOS specifically -- that's the one
        # platform we've actually confirmed needs a .mpk/.zip among the
        # uploaded files to be installable at all. Other badges' AppManager
        # equivalents may work completely differently; no basis to warn
        # generically across project_type == "app" for all of them.
        print(
            "Warning: no mpk-path given but 'mpos_api_0' is a target badge -- "
            "MicroPythonOS's AppManager installs by finding a .mpk/.zip among "
            "this project's uploaded files, so nothing will be installable on "
            "that platform without one.",
            file=sys.stderr,
        )

    print("Checking for stale draft files...")
    # "metadata.json" isn't a real file we ship -- BadgeHub creates/maintains
    # it itself as a side effect of the metadata PATCH call below, so it
    # never appears in local_paths and must be excluded here or every run
    # would delete it right before immediately recreating it.
    draft_paths = get_draft_file_paths(api_base, slug, token)
    for stale in sorted(draft_paths - local_paths - {"metadata.json"}):
        status, body = request(
            "DELETE", f"{api_base}/projects/{slug}/draft/files/{quote_path(stale)}", token
        )
        if status != 204:
            fail(f"Failed to delete stale draft file {stale}", status, body)
        print(f"  removed stale {stale}")

    metadata = build_metadata(app_dir)
    print("Setting draft metadata:", json.dumps(metadata, indent=2))
    status, body = request(
        "PATCH", f"{api_base}/projects/{slug}/draft/metadata", token, json_body=metadata
    )
    if status != 204:
        fail("Failed to set metadata", status, body)

    print("Publishing draft...")
    status, body = request("PATCH", f"{api_base}/projects/{slug}/publish", token)
    if status != 204:
        fail("Failed to publish", status, body)

    if not DRY_RUN:
        new_revision = wait_for_revision_advance(api_base, slug, previous_revision)
        if new_revision is None:
            fail("Publish returned 204 but the revision check afterwards failed", "n/a", b"")
        if previous_revision is not None and new_revision <= previous_revision:
            fail(
                f"Publish returned 204 but the revision did not advance after retrying "
                f"(was {previous_revision}, still {new_revision})",
                "n/a",
                b"",
            )
        print(f"Confirmed: revision is now {new_revision} (was {previous_revision}).")

    print(f"Published '{slug}' version {metadata['version']} to BadgeHub.")


if __name__ == "__main__":
    try:
        main()
    except Exception:
        # request() only catches urllib.error.HTTPError -- anything else
        # (URLError, a connection reset, etc.) would otherwise propagate as
        # a bare, traceback-free "exit code 1" with zero diagnostic info,
        # exactly what happened on the v0.1.2 publish attempt.
        traceback.print_exc()
        sys.exit(1)

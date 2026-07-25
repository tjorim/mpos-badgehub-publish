# mpos-badgehub-publish

Reusable composite GitHub Action that publishes a MicroPythonOS app to
[BadgeHub.eu](https://badgehub.eu) via its draft-upload-then-publish API
(`https://badgehub.eu/api-docs/swagger.json`). Any repo with a standard
MicroPythonOS app layout (a folder with `MANIFEST.JSON`, an entrypoint
`.py`, and an icon) can reuse this without modification.

## One-time setup (per project, done by a human on badgehub.eu)

1. Log into badgehub.eu and use "Create Project" to register your app,
   selecting `mpos_api_0` under "Badge".
2. Generate the project's API token (`POST /api/v3/projects/{slug}/token`
   while logged in -- there's a button for this on the project page).
3. Add that token as a repo secret, e.g. `BADGEHUB_API_TOKEN`.

## Usage

```yaml
- name: Build .mpk
  id: build
  uses: tjorim/mpos-package-mpk@v1
  with:
    app-dir: path/to/your_app

- name: Publish to BadgeHub
  uses: tjorim/mpos-badgehub-publish@v1
  with:
    slug: your-project-slug
    api-token: ${{ secrets.BADGEHUB_API_TOKEN }}
    app-dir: path/to/your_app
    mpk-path: ${{ steps.build.outputs.mpk-path }}
    categories: Game
```

`name`, `description`, `long_description`, `author`, `version`, and the
entrypoint are all read from `app-dir/MANIFEST.JSON` automatically -- there
is exactly one place those live. Only BadgeHub-specific fields that have no
`MANIFEST.JSON` equivalent are action inputs; see `action.yml` for the full
list (`categories`, `badges`, `license-type`, `git-url`, `icon`, `api-base`,
`mpk-path`).

`git-url` normally needs no input at all: it defaults to the calling
repository via `GITHUB_SERVER_URL`/`GITHUB_REPOSITORY`, which every GitHub
Actions job already has. Set it explicitly only to override that (e.g.
publishing from a fork or mirror).

### Why `mpk-path`

BadgeHub doesn't bundle a `.mpk` for you server-side. The on-device
AppManager installs an app by scanning the project's uploaded files for
one with a `.mpk`/`.zip` extension and downloading *that specific file*
(`appstore_core.py`'s `_find_download_file`) -- there's no other mechanism.
Without `mpk-path`, this action uploads your app's source tree individually
(useful for BadgeHub's own code-preview UI) but nothing is actually
installable on a badge. This action doesn't build the `.mpk` itself --
that's [tjorim/mpos-package-mpk](https://github.com/tjorim/mpos-package-mpk)'s
job, run as an earlier step in the same job; `mpk-path` just takes its
output and uploads it as one more draft file, superseding old versions
automatically via the stale-file cleanup below (the filename is
version-qualified, so an old `.mpk` just stops matching a current local
path on the next publish).

Set `dry-run: true` to log the planned uploads/metadata/publish calls
without making any network requests -- useful for verifying the file list
and metadata before pointing it at a real project.

## Verification, not just a 204

A successful HTTP status from `publish` doesn't by itself prove the new
content is live, so the script also uses BadgeHub's public (unauthenticated)
`GET /project-latest-revisions/{slug}` endpoint: once before uploading, as a
reachability check alongside `GET /ping`, and once after publishing, to
confirm the revision number actually advanced. The action fails loudly if it
didn't, rather than reporting success on a publish call that silently no-opped.

## Stale-file cleanup

BadgeHub's draft carries forward the file set from the previous published
revision rather than starting empty -- deleting a file locally doesn't
un-publish it on its own. Before setting metadata, this action diffs the
current draft's file listing (`GET /projects/{slug}/draft`) against the
local app folder and `DELETE`s anything no longer present, so a removed
module doesn't linger in BadgeHub forever.

## Testing

`tests/` has unit tests for `publish.py` run in `BADGEHUB_DRY_RUN` mode
against a `fixture-app/` -- no real BadgeHub project or token needed, and
one test specifically asserts dry-run never makes a network call at all (by
pointing at an unresolvable host and confirming that still succeeds). CI
also dogfoods the composite action itself, in dry-run mode, on Linux,
macOS, and Windows runners. Run locally with:

```
python -m unittest discover tests
```

Live end-to-end testing has partially happened: file upload, stale-file
cleanup, and metadata all confirmed working against a real project
(`tjorim/bomberboy-mpos`'s `bomberboy` slug). Two real bugs were found and
fixed this way that dry-run testing structurally cannot catch (neither
makes a real network call): a Cloudflare 403 on Python's default
User-Agent, and a 404 on any file inside a subdirectory (both below). The
final `publish` call itself is still not confirmed working end-to-end.

## Field name for file uploads

The published swagger spec doesn't document the multipart field name for
`POST /draft/files/{filePath}` -- it's wired via `multer.single("file")`
directly in BadgeHub's Express router, outside their typed `ts-rest`
contract, so their OpenAPI generator has nothing to describe it from (filed
as [BadgeHubCrew/badgehub-app#7](https://github.com/BadgeHubCrew/badgehub-app/issues/7)).
`publish.py` sends it as `file`, confirmed directly against BadgeHub's own
frontend (`formData.append("file", file)`) and backend
(`upload.single("file")`) source, and since confirmed again in practice --
13+ real file uploads have succeeded against a live project.

## Cloudflare blocks Python's default User-Agent

badgehub.eu sits behind Cloudflare, which 403s (error code 1010) any
request carrying Python's default `Python-urllib/3.x` User-Agent, or none
at all -- found when the first real (non-dry-run) publish attempt failed
on the pre-flight `/ping` check with no other explanation. Any non-default
value clears it; this isn't spoofing a browser, just identifying the
client honestly. `publish.py` sends `User-Agent: mpos-badgehub-publish` on
every request.

## Subdirectory files need percent-encoding

BadgeHub's `{filePath}` route is a single Express path segment, so a raw
`/` from a file inside a subdirectory (e.g. `sounds/BombDrop.wav`) splits
the URL into an extra, unmatched segment -- 404 "Cannot POST". Found when
the first real publish uploaded 13 flat files successfully, then hit this
on the first subdirectory file. `quote_path()` percent-encodes the whole
relative path as one opaque token before building the URL.

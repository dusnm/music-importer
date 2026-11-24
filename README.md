# music-importer

Imports albums downloaded by slskd into the music library at `/srv/media/music`, which Navidrome serves.

```
slskd ──DownloadDirectoryComplete webhook──▶ ┐
                                             ├─▶ music-importer ──▶ /music/Album Artist/Album (Year)/NN Title.ext
drop folder (SFTP) ──settled for 60 s──────▶ ┘
```

## Drop folder

Copy album folders into **`/srv/media/music-inbox`**. On the server this is also reachable as `~/music-inbox`.

To use it from GNOME Files on the laptop:
1. Go to *Other Locations* → *Connect to Server*.
2. Enter `sftp://you@server/srv/media/music-inbox`.
3. Bookmark it with Ctrl+D.

With sshfs: `sshfs you@server:/srv/media/music-inbox ~/music-inbox`

How the drop folder works:
- **Starting the import:** SFTP has no "copy finished" event. A top-level folder is imported once nothing in it has changed for `INBOX_STABLE_SECONDS`, 60 s by default, checked every 10 s. Changes are tracked through each file's ctime, count and size. ctime catches every write, even when the client restores the original modification times.
- **One album or many:** a folder may hold one album, an album with `CD1`/`CD2` subfolders, or several album folders. Each album is matched separately.
- **Success:** the folder disappears from the inbox once its albums are in the library.
- **Anything not imported:** the remaining files are moved to `_not-imported/<folder>`, with a `<folder>.txt` note giving the reason (no confident match, or leftover files). To retry, move the folder back into the inbox.
- **Ignored:** loose files directly in the inbox, and folders whose names start with `.` or `_`.
- **Custom albums:** put `[as-is]` anywhere in the folder name, e.g. `Metallica - ...And Justice for All (with Bass) [as-is]`. The album is then imported with the files' own tags: album name, titles, track and disc numbers stay exactly as tagged, and MusicBrainz and fingerprinting are skipped. Files are still renamed into the library layout from those tags, and still get cover art, genre (only if empty) and lyrics. The marker never appears in the library, because folder names come from the tags. For a folder that can't be renamed, such as an slskd download, use `import --as-is` instead (see [Commands](#commands)).

For each completed folder, whether from slskd or the drop folder, it does the following:

1. **Match by tags.** `beet import -q` matches the folder against MusicBrainz. The match is applied only when it is strong: at least 96% similar, with no missing or unmatched tracks. Applying it rewrites the tags and moves the files into the library.
2. **Match by fingerprints if the tags fail.** This typically happens with untagged files.
   - Every track is fingerprinted with AcoustID.
   - The MusicBrainz release that contains the recordings is chosen.
   - Its release id, plus any missing basic tags, is written to the files.
   - beets runs again and matches by that id.
3. **Cover art.**
   - beets' `fetchart` takes the folder image if it is at least 600 px. Otherwise it fetches from the Cover Art Archive, then from fanart.tv when `FANARTTV_API_KEY` is set.
   - `art.py` then keeps a single `cover.jpg`: the largest source, capped at 1200 px, JPEG q85 4:2:0, progressive and mozjpeg-optimised. This is the same treatment as the rest of the library.
   - Other cover copies are removed, and embedded pictures are stripped from the tracks.
4. **Genre.** `lastgenre` adds one genre if the files have none.
5. **Lyrics.** LRCLIB lyrics are written next to each track, unless `LYRICS=0`. See [Lyrics](#lyrics).
6. **Clean-up.** The download folder is deleted once all its audio is in the library and only images and text files remain.

Navidrome's file watcher picks up the new folder within about 30 s.

A folder with no confident match is left in `slskd/data/downloads` (or moved to the inbox's `_not-imported/`) and logged. Nothing in it is changed, apart from the provisional tags step 2 may have written.

## Lyrics

Lyrics come from [LRCLIB](https://lrclib.net), a free community database with no API key. They are written as sidecar files, so the audio files are never modified:

- `NN Title.lrc` holds time-synced lyrics, used whenever they are available.
- `NN Title.txt` holds plain lyrics, used only when no synced version exists.

Navidrome's default `LyricsPriority` (`.lrc`, then `.txt`, then embedded) serves these to apps right away; no rescan is needed. Clients that support OpenSubsonic, such as Symfonium, show the synced lyrics karaoke-style.

Matching:
- The first lookup is LRCLIB's exact match on artist, title, album and duration (rounded to the second).
- If that finds nothing, or only plain lyrics, the next step is a search by artist and title. Its results only count if their duration is within 2 s of the track's, so live versions and edits don't get the wrong lyrics.

Tracks are skipped when they:
- already have a `.lrc` or `.txt` file, or lyrics stored inside the file;
- are marked instrumental by LRCLIB.

Each track's result is stored in `data/lyrics.db` as one of: `synced`, `plain`, `embedded` (lyrics already stored in the file), `instrumental`, `not-found` or `no-tags`. A track that was not found, was instrumental, or had no tags is retried only after `LYRICS_RETRY_DAYS` (30). Requests are spaced 0.5 s apart.

```bash
music-importer lyrics                       # whole library (resumable)
music-importer lyrics "/music/<Artist>/<Album (Year)>"
music-importer lyrics --retry-all           # ignore the 30-day memory
```

To remove all fetched lyrics again, delete the `.lrc` files and the `.txt` files recorded as `plain`:

```bash
find /srv/media/music -name '*.lrc' -delete
sqlite3 data/lyrics.db "select path from tracks where status='plain'" \
  | sed -E 's#^/music#/srv/media/music#; s#\.[^.]+$#.txt#' | tr '\n' '\0' | xargs -0 rm -f
```

## Duplicates: newer wins

When an imported album has the same album artist and album title as one already in the library, the new copy replaces the old one. This works the same for slskd and the drop folder.

- `import.duplicate_action: remove` makes beets remove the old album before moving the new one in. The new copy can therefore take over the same `Artist/Album (Year)` folder.
- The `musicimporter` beets plugin (`beetsplug_local/musicimporter.py`) changes how the old album is removed. Instead of deleting files one by one, it moves the old album's whole folder, including booklets and other extras, to `/srv/media/music-replaced/<date time> <Artist> - <Album>/`. It also logs the move to `data/replaced.jsonl`.
- Set-aside copies are deleted after `REPLACED_KEEP_DAYS` (30). The purge runs after each import.
- To undo a replacement within that window, move the folder back into the library, then drop it into the inbox or run `music-importer import` on it.

Duplicates are found by exact album artist and album title, as stored in the beets database. Two things follow from that:
- An older album whose tags spell the title differently is not detected, and both copies stay. For example `Mezmerize` vs `Mesmerize`, or a title with `(Remastered)` added.
- Editions with different titles, such as *Meteora* and *Meteora (20th Anniversary Edition)*, count as different albums.

Newer wins even when the new copy is lower quality, for example MP3 replacing FLAC. beets 2.14 also offers `duplicate_action: upgrade`, which keeps the better copy of each track instead.

## Library layout

| | Template |
|---|---|
| Album folder | `$albumartist/$album ($original_year)` |
| Multi-disc | `…/CD$disc/` |
| Track | `$track $title.ext`, with the track number two digits and counted per disc |
| Compilations | `Various Artists/…` |

Titles follow MusicBrainz spelling, for example `Everybody’s Fool` and `My Immortal (band version)`.

## Commands

`bin/music-importer` is a front end for the container, symlinked to `~/.local/bin/music-importer`, so it works from any folder:

```bash
music-importer status                     # container state, inbox, last imports
music-importer history 20                 # the last 20 imports
music-importer logs -f                    # follow the container log
music-importer import ~/music-inbox/_not-imported/<folder> --mbid <id>   # force a release
music-importer import "/downloads/<folder>"                              # retry an slskd folder
music-importer import "/downloads/<folder>" --as-is      # keep own tags, no MusicBrainz
music-importer art "/srv/media/music/<Artist>/<Album (Year)>"   # redo cover art only
music-importer index                      # re-register the library (read-only)
music-importer restart | deploy | shell   # restart, rebuild from this project, open a shell
music-importer help
```

- **Container commands:** anything that isn't a host command (`status`, `history`, `logs`, `restart`, `deploy`, `shell`) runs the CLI inside the container. `music-importer <command> --help` shows that command's options.
- **Path translation:** host paths are translated to container paths, so a relative path works too. The inbox and `~/music-inbox` map to `/inbox`, the library to `/music`, slskd downloads to `/downloads`, and `data/` to `/data`. Container paths are passed through unchanged.
- **Terminal:** the interactive Bandcamp picker gets a terminal when you run the command in one. In scripts and pipes it runs without one.

`--mbid` takes the release id from a `https://musicbrainz.org/release/<id>` URL. Use it when a folder was left behind because no match was confident enough.

## Bandcamp

Imports releases from **your own Bandcamp collection**: purchases and claimed free or
name-your-price downloads, as FLAC. Pre-orders and physical-only items are skipped.

```bash
music-importer bandcamp                      # browse and pick (full screen)
music-importer bandcamp list                 # everything you own
music-importer bandcamp list pelican         # filtered
music-importer bandcamp album https://pelican.bandcamp.com/album/australasia
music-importer bandcamp album "pelican - city of"   # part of "Artist - Title"
music-importer bandcamp artist https://pelican.bandcamp.com
music-importer bandcamp artist "Pelican"             # by name
```

**Browsing:** `bandcamp` on its own opens a full-screen checklist of your whole collection.
- **Layout:**
  - Releases are grouped by artist. Artists are sorted by name, ignoring a leading "The".
  - Within an artist, releases are sorted by release date.
  - Releases imported from Bandcamp before are marked `✓ imported from Bandcamp` (green). Albums that are already in the library from another source are marked `● already in library` (yellow), matching on artist and album name.
- **Keys:**
  - ↑/↓, PgUp/PgDn, Home/End: move.
  - Space: select a release; on an artist heading, select all of that artist's releases.
  - `a`: toggle the whole current artist.
  - `u`: select everything not marked.
  - `n`: clear the selection.
  - Enter: import the selection.
  - `q`: quit without importing.
- **Release dates:** these come from each release's Bandcamp page. They are cached in `data/bandcamp-releases.json`, so only the first run, about 2 minutes for 80 releases, and newly bought releases need a lookup.
- **Numbered list instead:** `bandcamp --text` prints the same list with numbers and asks e.g. `1,3-5`. This is also the automatic fallback when there's no full terminal.

How it behaves:
- **Choosing releases:** `artist` lists the releases you own from that artist and asks which to import: Enter for all, `1,3-5` for some, `q` to cancel. `album` asks the same way when the text matches several releases. Pass `--yes` to take every match without asking.
- **Matching:**
  - Each release is downloaded and unpacked into `data/bandcamp/<Artist - Title>`, then goes through the [normal import](#drop-folder).
  - If MusicBrainz has no confident match, which is common for Bandcamp releases, it is imported with Bandcamp's own tags, as with `[as-is]`. Bandcamp imports skip the fingerprint fallback, so a MusicBrainz guess never gets mixed into Bandcamp's tags. A track bought on its own with no album tag becomes a single named after itself.
  - `--as-is` skips MusicBrainz from the start.
- **Re-runs:** imported purchases are recorded in `data/bandcamp.json`, so running `artist` again only fetches new releases. `--force` re-imports. When an album is re-imported, the newer copy wins as usual.
- **Format:** `--format` picks a different download format, such as `mp3-320` or `alac`. The default is `flac`.
- **Failures:** a release that fails to import stays in `data/bandcamp/` and is reported. Retry it with `import /data/bandcamp/<folder> [--mbid ...|--as-is]`.

**Login:**
1. Log in on bandcamp.com in a browser.
2. Open the developer tools: *Storage* (Firefox) or *Application* (Chrome), then *Cookies*, then `https://bandcamp.com`.
3. Copy the value of the `identity` cookie into `.env` as `BANDCAMP_COOKIES=<value>`.
4. Run `docker-compose up -d`.

The cookie lasts until you log out of that browser session. When it expires, commands stop with a "did not accept the login cookie" error. A Netscape `cookies.txt` export saved as `data/bandcamp-cookies.txt` also works.

Talking to Bandcamp (its bot challenge, the collection API and download links) is handled by [bandcampsync](https://github.com/meeb/bandcampsync), which is used as a library.

## Files

| Path | Purpose |
|---|---|
| `src/music_importer/server.py` | Webhook endpoint (`POST /webhook`, `GET /health`) and the import queue, one folder at a time |
| `src/music_importer/inbox.py` | Drop-folder watcher: completion detection and parking folders that were not imported |
| `src/music_importer/pipeline.py` | beets runs, the fingerprint fallback, cover clean-up and download clean-up |
| `src/music_importer/fingerprint.py` | AcoustID identification and provisional tags |
| `src/music_importer/art.py` | Cover normalisation (same rules as the library clean-up) |
| `config/beets.yaml` | beets configuration template; paths and the API key are filled in at runtime |
| `bin/music-importer` | Host front end for the container, symlinked into `~/.local/bin` |
| `requirements.lock` | Frozen dependencies, built from `pip freeze` in the base image |

## Deployment

The deployment lives in `~/.local/share/docker/music-importer`:

- **`docker-compose.yml`:** builds the image `music-importer:<version>` from this folder. The container runs as `1000:1000`.
- **Mounts:** `/srv/media/music-inbox` is mounted as `/inbox` (environment variable `INBOX_DIR`). Leave `INBOX_DIR` unset to turn the drop folder off.
- **Network:** the container joins the `slskd_default` network and publishes no ports.
- **`.env`** (mode 600): `FANARTTV_API_KEY` is optional; leave it empty to use only the Cover Art Archive.
- **`data/`:** holds the beets library DB (`library.db`), `beets-import.log` and `imports.jsonl`.

The slskd side is set in `slskd/data/slskd.yml` under `integrations.webhooks.music_importer`. It sends `DownloadDirectoryComplete` events to `http://music-importer:8080/webhook`.

To deploy a change, bump `version` in `pyproject.toml` if you want a new image tag, then:

```bash
music-importer deploy
```

`deploy` sets the image tag in `docker-compose.yml` to the project version, rebuilds the image and recreates the container.

## Versions

- **beets 2.14.1** supports Python below 3.15, so the image uses `python:3.14.10-slim-trixie` instead of 3.15.
- **Bumping beets:** update `pyproject.toml`, regenerate `requirements.lock` in the new base image, then rebuild.
- **slskd** is pinned to `0.26.2`. Check the webhook payload (`localDirectoryName`) when upgrading it.

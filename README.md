# music-importer

Imports albums into a music library served by Navidrome (or any player that reads tags): downloads from slskd, folders dropped into an inbox, and your Bandcamp purchases. It runs as a Docker container; the paths below are as seen inside it (`/music`, `/inbox`, `/downloads`, `/data`).

```
slskd ──DownloadDirectoryComplete webhook──▶ ┐
                                             ├─▶ music-importer ──▶ /music/Album Artist/Album (Year)/NN Title.ext
drop folder (SFTP) ──settled for 60 s──────▶ ┘
```

## Drop folder

Copy album folders into the inbox: the host folder mounted at `/inbox` (`mi inbox-path` prints it).

From another machine, any SFTP client works. For example:
- **GNOME Files:** go to *Other Locations* → *Connect to Server*, enter `sftp://you@server/path/to/music-inbox`, then bookmark it with Ctrl+D.
- **sshfs:** `sshfs you@server:/path/to/music-inbox ~/music-inbox`
- **`mi push`:** uploads safely from the command line (see [Using mi from another machine](#using-mi-from-another-machine)).

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
   - If that leaves the album without a cover, which is typical for `[as-is]` and Bandcamp imports that have no MusicBrainz ID, it is looked up online. The lookup tries the Cover Art Archive by any MusicBrainz ID in the tags. Otherwise it searches MusicBrainz by artist and album, read-only, so nothing is written to the tags, and tries the Cover Art Archive and fanart.tv for that release group. Last comes the iTunes catalogue. Covers smaller than 600 px are ignored.
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
mi lyrics                       # whole library (resumable)
mi lyrics "/music/<Artist>/<Album (Year)>"
mi lyrics --retry-all           # ignore the 30-day memory
```

To remove all fetched lyrics again, delete the `.lrc` files and the `.txt` files recorded as `plain`:

```bash
music="$(mi inbox-path >/dev/null && docker inspect -f '{{range .Mounts}}{{if eq .Destination "/music"}}{{.Source}}{{end}}{{end}}' music-importer)"
find "$music" -name '*.lrc' -delete
sqlite3 "$DATA_ROOT/music-importer/data/lyrics.db" "select path from tracks where status='plain'" \
  | sed -E "s#^/music#$music#; s#\.[^.]+\$#.txt#" | tr '\n' '\0' | xargs -0 rm -f
```

## Duplicates: newer wins

When an imported album has the same album artist and album title as one already in the library, the new copy replaces the old one. This works the same for slskd and the drop folder.

- `import.duplicate_action: remove` makes beets remove the old album before moving the new one in. The new copy can therefore take over the same `Artist/Album (Year)` folder.
- The `musicimporter` beets plugin (`beetsplug_local/musicimporter.py`) changes how the old album is removed. Instead of deleting files one by one, it moves the old album's whole folder, including booklets and other extras, to `/replaced/<date time> <Artist> - <Album>/` (a host folder of your choice). It also logs the move to `data/replaced.jsonl`.
- Set-aside copies are deleted after `REPLACED_KEEP_DAYS` (30). The purge runs after each import.
- To undo a replacement within that window, move the folder back into the library, then drop it into the inbox or run `mi import` on it.

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

`bin/mi` is a front end for the container, symlinked to `~/.local/bin/mi`, so it works from any folder:

```bash
mi status                     # container state, inbox, active jobs, last imports
mi jobs                       # queued, running and recent jobs
mi follow                     # live progress of everything queued/running (Ctrl-C stops watching)
mi history 20                 # the last 20 imports
mi logs -f                    # follow the container log
mi import ~/music-inbox/_not-imported/<folder> --mbid <id>   # force a release
mi import "/downloads/<folder>"                              # retry an slskd folder
mi import "/downloads/<folder>" --as-is      # keep own tags, no MusicBrainz
mi art "/path/to/music/<Artist>/<Album (Year)>"   # cover art only: normalise, or download if missing
mi index                      # re-register the library (read-only)
mi restart | deploy | shell   # restart, rebuild from this project, open a shell
mi help
```

- **Container commands:** anything that isn't a host command (`status`, `history`, `logs`, `restart`, `deploy`, `shell`) runs the CLI inside the container. `mi <command> --help` shows that command's options.
- **Path translation:** host paths are translated to container paths, so a relative path works too. The inbox and `~/music-inbox` map to `/inbox`, the library to `/music`, slskd downloads to `/downloads`, and `data/` to `/data`. Container paths are passed through unchanged.
- **Terminal:** the interactive Bandcamp picker gets a terminal when you run the command in one. In scripts and pipes it runs without one.

`--mbid` takes the release id from a `https://musicbrainz.org/release/<id>` URL. Use it when a folder was left behind because no match was confident enough.

## Using mi from another machine

`bin/mi-remote` is `mi` for the laptop. It runs every command on the server over SSH:

```bash
# install on the laptop, once
mkdir -p ~/.local/bin
scp you@server:path/to/music-importer/bin/mi-remote ~/.local/bin/mi && chmod +x ~/.local/bin/mi
mkdir -p ~/.config/mi && echo you@server > ~/.config/mi/host
ssh-copy-id you@server             # optional: key login instead of a password per command

mi jobs                            # any mi command, run on the server
mi bandcamp                        # the full-screen picker works too
mi follow
mi push ~/Music/"Some Album" [--as-is]   # upload album folders from the laptop into the inbox
```

- **Arguments and terminal:** arguments are passed through exactly, spaces and quotes included. A terminal is requested only when you run `mi` in one, so the pickers and `mi follow` work interactively while scripts and pipes get plain output.
- **Speed:** one SSH connection is shared for 10 minutes, so repeated commands start instantly. Only the first one asks for a password, if you don't use a key.
- **Paths:** paths in commands are server paths. To import something from the laptop, use `mi push`.
- **`mi push`:**
  - Uploads each folder with rsync, showing progress, into a hidden folder in the inbox. When the upload completes, it renames the folder in one step, so the importer never sees a half-copied album.
  - The import then starts about a minute later, like any inbox drop. `--as-is` adds `[as-is]` to the folder name.
  - If a folder with that name is already in the inbox, the upload is kept under its hidden name and you are told where.
- **Settings:**
  - `MI_HOST`, or the first line of `~/.config/mi/host`: the server, as `user@host`. For example `MI_HOST=you@music.example.com mi jobs` from outside the LAN, if SSH is reachable there.
  - `MI_INBOX`: the server's inbox folder. It's asked from the server when unset.
  - `MI_REMOTE_BIN`: where `mi` is on the server. The default is `~/.local/bin/mi`.
- **Requirements on the laptop:** bash, ssh and rsync.
- **Updating:** after the script changes, copy it to the laptop again with the same `scp`.

## Background jobs

Everything that changes the library runs as a **job**: `import`, `art`, `lyrics`, and Bandcamp
releases (one job per release). slskd webhooks and the inbox watcher create jobs too.

- **Order:** one worker in the server runs jobs one after another, in the order they were queued. Nothing touches the beets database at the same time.
- **Commands return immediately:** they print the job number. Add `--fg` to run the work in the terminal and wait for it instead, with a live progress line.
- **What jobs report:**
  - Each job reports progress while it runs, for example `downloading … 45% (120/260 MB, 2.1 MB/s)`, `matching with MusicBrainz (beets)`, `fingerprinting 3/9`, `cover art: …` or `lyrics 12/40`.
  - It also keeps the log lines written while it ran.
  - When it finishes, it gets a one-line outcome.
- **Commands:**

  ```bash
  mi jobs [-n N]        # queued and running jobs plus the last N finished (default 10)
  mi job 42 [--log]     # one job: state, timings, parameters, outcome and its log
  mi follow [42]        # live progress of one job, or of every queued/running job in turn
  mi follow --log       # ...and print its log lines as they come
  mi cancel 42 43       # a queued job is dropped; a running one stops at the next safe point
  mi retry 42           # queue a failed, cancelled or interrupted job again
  mi jobs clear         # remove all finished jobs from the list
  mi jobs clear --failed   # only the unsuccessful ones (failed, cancelled, interrupted)
  ```

- **Marks:** `…` queued, `▶` running, `✓` done, `✗` failed, `-` cancelled, `!` interrupted.
- **Duplicates:** queueing a job identical to one that is already queued or running returns the existing job instead.
- **Restarts:**
  - Jobs are stored in `data/jobs.db`, so queued jobs survive a restart or redeploy.
  - On `docker stop`, the server takes no new jobs and lets the running one finish, for up to `stop_grace_period`, which is 2 minutes.
  - A job that was still cut off is marked `interrupted`; `mi retry` queues it again.
- **History:** the last 1000 finished jobs are kept. `mi jobs clear` empties the list sooner; it never removes queued or running jobs.

## Bandcamp

Imports releases from **your own Bandcamp collection**: purchases and claimed free or
name-your-price downloads, as FLAC. Pre-orders and physical-only items are skipped.

```bash
mi bandcamp                      # browse and pick (full screen)
mi bandcamp list                 # everything you own
mi bandcamp list pelican         # filtered
mi bandcamp album https://pelican.bandcamp.com/album/australasia
mi bandcamp album "pelican - city of"   # part of "Artist - Title"
mi bandcamp artist https://pelican.bandcamp.com
mi bandcamp artist "Pelican"             # by name
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
- **Background jobs:** after you pick, every release is queued as its own [background job](#background-jobs) and the command returns right away. Watch with `mi follow`. `--fg` imports in the terminal instead.
- **Failures:** a release that fails to import stays in `data/bandcamp/`, and its job is marked failed. Retry it with `mi retry <job>`, or with `mi import /data/bandcamp/<folder> [--mbid ...|--as-is]`.

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
| `bin/mi` | Host front end for the container, symlinked into `~/.local/bin` |
| `bin/mi-remote` | `mi` for another machine: runs it on the server over SSH, plus `mi push` |
| `requirements.lock` | Frozen dependencies, built from `pip freeze` in the base image |

## Deployment

The container is one service of a Docker Compose setup. The reference setup is the
[homelab](https://github.com/dusnm/homelab) project (`services/music-importer/compose.yml` there), which builds the image from a checkout
of this project (`MUSIC_IMPORTER_SRC`). What it needs:

- **Image:** built from this folder (`docker build .`); it runs as the library's owner (`user: UID:GID`).
- **Mounts:**
  - `/music`: the library.
  - `/downloads`: slskd's download folder.
  - `/inbox`: the drop folder. Leave `INBOX_DIR` unset to turn it off.
  - `/replaced`: older copies set aside by newer-wins replacement.
  - `/data`: state, including the beets DB `library.db`, `beets-import.log`, `imports.jsonl`, `jobs.db` and `lyrics.db`.
- **Network:** the container joins slskd's network, so slskd can call `http://music-importer:8080/webhook`. It publishes no ports.
- **Environment:** see the `environment:` block of that compose file.
- **Secrets:** both optional. `FANARTTV_API_KEY` adds fanart.tv as a cover source; `BANDCAMP_COOKIES` enables `mi bandcamp`.
- **Stopping:** `stop_grace_period: 2m`, so a running job can finish on `docker stop`.

The slskd side is set in `slskd.yml` under `integrations.webhooks.music_importer`. It sends `DownloadDirectoryComplete` events to `http://music-importer:8080/webhook`.

`mi deploy` rebuilds and recreates the container: with `homelab build music-importer && homelab up music-importer` when the `homelab` command is installed, otherwise with whatever `MI_DEPLOY_CMD` says.

## Versions

- **beets 2.14.1** supports Python below 3.15, so the image uses `python:3.14.10-slim-trixie` instead of 3.15.
- **Bumping beets:** update `pyproject.toml`, regenerate `requirements.lock` in the new base image, then rebuild.
- **slskd** is pinned to `0.26.2`. Check the webhook payload (`localDirectoryName`) when upgrading it.

## License

MIT. See [LICENSE](LICENSE).

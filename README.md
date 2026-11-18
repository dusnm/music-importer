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
5. **Clean-up.** The download folder is deleted once all its audio is in the library and only images and text files remain.

Navidrome's file watcher picks up the new folder within about 30 s.

A folder with no confident match is left in `slskd/data/downloads` (or moved to the inbox's `_not-imported/`) and logged. Nothing in it is changed, apart from the provisional tags step 2 may have written.

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

Run these in `~/.local/share/docker/music-importer`:

```bash
docker-compose logs -f                                   # what it is doing
tail -n 5 data/imports.jsonl                             # one JSON line per processed folder
docker-compose exec music-importer music-importer import "/downloads/<folder>"              # retry a folder
docker-compose exec music-importer music-importer import "/downloads/<folder>" --mbid <id>  # force a release
docker-compose exec music-importer music-importer import "/inbox/_not-imported/<folder>" --mbid <id>
docker-compose exec music-importer music-importer art "/music/<Artist>/<Album (Year)>"      # redo cover art only
docker-compose exec music-importer music-importer index  # re-register the library (read-only)
```

`--mbid` takes the release id from a `https://musicbrainz.org/release/<id>` URL. Use it when a folder was left behind because no match was confident enough.

## Files

| Path | Purpose |
|---|---|
| `src/music_importer/server.py` | Webhook endpoint (`POST /webhook`, `GET /health`) and the import queue, one folder at a time |
| `src/music_importer/inbox.py` | Drop-folder watcher: completion detection and parking folders that were not imported |
| `src/music_importer/pipeline.py` | beets runs, the fingerprint fallback, cover clean-up and download clean-up |
| `src/music_importer/fingerprint.py` | AcoustID identification and provisional tags |
| `src/music_importer/art.py` | Cover normalisation (same rules as the library clean-up) |
| `config/beets.yaml` | beets configuration template; paths and the API key are filled in at runtime |
| `requirements.lock` | Frozen dependencies, built from `pip freeze` in the base image |

## Deployment

The deployment lives in `~/.local/share/docker/music-importer`:

- **`docker-compose.yml`:** builds the image `music-importer:<version>` from this folder. The container runs as `1000:1000`.
- **Mounts:** `/srv/media/music-inbox` is mounted as `/inbox` (environment variable `INBOX_DIR`). Leave `INBOX_DIR` unset to turn the drop folder off.
- **Network:** the container joins the `slskd_default` network and publishes no ports.
- **`.env`** (mode 600): `FANARTTV_API_KEY` is optional; leave it empty to use only the Cover Art Archive.
- **`data/`:** holds the beets library DB (`library.db`), `beets-import.log` and `imports.jsonl`.

The slskd side is set in `slskd/data/slskd.yml` under `integrations.webhooks.music_importer`. It sends `DownloadDirectoryComplete` events to `http://music-importer:8080/webhook`.

To deploy a change:

```bash
cd ~/.local/share/docker/music-importer && docker-compose build && docker-compose up -d
```

## Versions

- **beets 2.14.1** supports Python below 3.15, so the image uses `python:3.14.10-slim-trixie` instead of 3.15.
- **Bumping beets:** update `pyproject.toml`, regenerate `requirements.lock` in the new base image, then rebuild.
- **slskd** is pinned to `0.26.2`. Check the webhook payload (`localDirectoryName`) when upgrading it.

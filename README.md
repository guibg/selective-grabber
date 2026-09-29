# selective-grabber

A small self-hosted helper that downloads **only the episode you need** from
well-seeded **season/series packs** and imports it into **Sonarr**.

Think of it as a free, local *"Torrentio for Sonarr"* — no debrid, no paid
service, just your existing Sonarr + qBittorrent + Prowlarr stack.

> Comments and log messages in the code are currently in Portuguese (the
> project originated in a Brazilian setup). PRs translating them are welcome.

---

## The problem

Older anime (and many old TV shows) have almost **no well-seeded individual
episodes**. What *is* well-seeded are the big **packs/batches** released by
groups like `[Judas]`, `[Anime Time]`, `[Lia]`, `[Erai-raws]` — a single torrent
with hundreds of episodes and hundreds of seeders.

Sonarr can't help here:

- Sonarr's per-episode search **doesn't even see** those packs.
- Even if it did, Sonarr would download the **entire pack** (tens/hundreds of GB)
  just to get one episode.

Stremio/Torrentio solves this by grabbing a single file inside the pack. This
helper does the same thing, locally and for free.

## How it works

Every cycle (default: 5 minutes), for each **monitored episode without a file**:

1. Search **Prowlarr** for the best release that **contains** the episode
   (highest seeders; packs count as candidates).
2. Add the magnet **directly to qBittorrent** (with its own category + tag).
3. As soon as the metadata is available, set file priorities so **only that
   episode's file downloads** — every other file in the pack is skipped.
4. When the file finishes, import it into **Sonarr** via `ManualImport`
   (`importMode: copy` → **hardlink**, so the torrent keeps seeding and no space
   is duplicated).

Packs are reused: if a later episode lives in a pack that was already added, the
helper just enables that file — it doesn't download anything new.

## Requirements

- **Sonarr** (v4 / `api/v3`)
- **qBittorrent** with the Web UI enabled
- **Prowlarr**
- All three reachable from the helper by hostname (e.g., same Docker network)
- Sonarr and qBittorrent must share the same `/data` mount (so hardlinks work)

## Install (Docker Compose)

1. Create a `config` folder next to the compose file and copy the example config:

   ```bash
   mkdir -p config
   cp config.example.json config/config.json
   # edit config/config.json and fill in your URLs / API keys / series
   ```

2. Put this next to your `docker-compose.yml` (or merge the service into it).
   **The container must share the network with Sonarr/qBittorrent/Prowlarr.**

3. Start it:

   ```bash
   docker compose up -d selective-grabber
   docker logs -f selective-grabber
   ```

4. To run a single cycle (useful for testing):

   ```bash
   docker exec selective-grabber python3 /app/selective_grabber.py --once
   ```

## Configuration (`config/config.json`)

| Key | Description |
|---|---|
| `sonarr_url` / `sonarr_api_key` | Sonarr base URL and API key (Settings → General). |
| `prowlarr_url` / `prowlarr_api_key` | Prowlarr base URL and API key (Settings → General). |
| `qbittorrent_url` / `qbittorrent_user` / `qbittorrent_password` | qBittorrent Web UI. |
| `category` | qBittorrent category for the downloads (must map to a save path). |
| `tag` | Tag applied to the helper's torrents so it can recognize them. |
| `poll_seconds` | Cycle interval. |
| `max_grabs_per_run` | Max episodes handled per cycle (keeps it gentle). |
| `retry_hours` | If a grab makes no progress in this window, drop it and try another release. |
| `selection_timeout_seconds` | How long to wait for a pack's metadata before giving up. |
| `series[]` | One entry per Sonarr series to manage. |

Per-series keys:

| Key | Description |
|---|---|
| `sonarr_series_id` | Sonarr series id (from the URL `/series/<id>`). |
| `title` | Series title, used as the Prowlarr search query. |
| `search_categories` | Prowlarr category ids. **Leave empty** — some indexers return 0 results when a category filter is applied. |
| `preferred_groups` | Release groups to prefer on tie (seeders is the primary ranking). |
| `min_seeders` | Minimum seeders to accept. |
| `priority_abs` | Absolute episode numbers to grab first (e.g. the next ones you'll watch). |

## How episode matching works

The helper matches releases/files against an episode using both:

- **absolute numbering** (`One Piece - 0540 ...`), and
- **season/episode** (`S15E24`, `15x24`).

For **file names** it only matches the episode number as a standalone token —
never a range — because pack file paths often contain the pack's range (e.g.
`... (0001-1071 ...)`) and a range match would select the whole pack.

## Notes / caveats

- This is a **Sonarr-specific** tool. It won't help if you don't run Sonarr.
- It respects Sonarr's monitoring: only monitored, missing episodes are handled.
- Make sure Sonarr's *own* automatic searching won't fight the helper. There is
  no recurring "missing episode search" in Sonarr by default, so in practice they
  don't conflict.
- Downloads land in the configured qBittorrent category; the helper skips the
  rest of the pack, so the torrent stays **incomplete by design** (that's how
  selective download works).

## License

GPL-3.0-or-later. See [LICENSE](LICENSE).

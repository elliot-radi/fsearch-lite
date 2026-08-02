# fsearch-lite

A minimal filename/path indexer + search GUI — a from-scratch replacement
for [fsearch](https://github.com/cboxdoerfer/fsearch) for anyone who's hit
its `double free or corruption` crash and would rather not wait on an
upstream fix. No content search, no fancy plugin system — just fast,
flexible filename/path search over a SQLite index, with a small GTK3 GUI.

## Features

- **Multi-term AND search** — `report final` matches `report_final_v2.docx`.
- **Separator-aware matching** — `_`, `-`, `.` in your search terms are
  matched literally (`mes_` only matches names with that exact underscore,
  not `mesh_...`), while whole words on either side of a separator still
  AND together.
- **Regex mode** — full regex search against name and path.
- **Sortable columns** — Name / Directory / Size / Modified, click to sort,
  click again to reverse.
- **Double-click** a result to open its folder in Nemo with the file
  pre-selected.
- **Right-click context menu** — Open (default app), Open With… (app
  chooser dialog), Show in Folder, Move to Trash (uses the real
  freedesktop trash, and updates the index immediately, no reindex needed).
- **In-app config editor** — a button opens `config.json` in your default
  text editor; Reindex reloads it from disk automatically.
- **Fast indexing** — plain `os.scandir` walk into SQLite + FTS5; ballpark
  500k files in ~10 seconds on modest hardware.
- **Background reindexing** — optional systemd `--user` timer, see below.

## Requirements

Everything here should already be present on a stock Linux Mint /
Ubuntu-based install:

```bash
sudo apt install python3-gi gir1.2-gtk-3.0
```

(`sqlite3` and `argparse` are part of the Python standard library. FTS5
support in `sqlite3` is compiled into Ubuntu/Mint's default Python builds.)

The "Show in Folder" action and desktop shortcut assume **Nemo** as your
file manager (Cinnamon's default) — it falls back to `xdg-open` on the
containing directory if Nemo isn't installed.

## Repo contents

| File | Purpose |
|---|---|
| `fsearch_core.py` | Indexing + search logic. No GUI dependency — importable, testable, and used by `--reindex`. |
| `fsearch_lite.py` | The GTK3 GUI. Imports `fsearch_core`. |
| `fsearch-lite-icon.svg` | App icon (also provided as `.png` at 256/128/64/48px). |
| `fsearch-lite-reindex.service` | systemd user unit: runs a one-shot reindex. |
| `fsearch-lite-reindex.timer` | systemd user unit: fires the service every 4 hours. |
| `README.md` | This file. |

## Installation

1. **Copy the files** somewhere permanent, e.g.:

   ```bash
   mkdir -p ~/bin
   cp fsearch_core.py fsearch_lite.py ~/bin/
   chmod +x ~/bin/fsearch_lite.py
   ```

   Keep `fsearch_core.py` and `fsearch_lite.py` in the same directory.

2. **Install dependencies** (see Requirements above).

3. **First run** — this builds an index of your home directory automatically:

   ```bash
   ~/bin/fsearch_lite.py
   ```

4. **Add more directories to index** (e.g. an external drive), either via
   the CLI or by editing the config directly:

   ```bash
   ~/bin/fsearch_lite.py --add-root /mnt/data
   ```

## Configuration

Config lives at `~/.config/fsearch-lite/config.json`:

```json
{
  "roots": ["/home/you", "/mnt/data"],
  "excludes": [".git", "node_modules", "__pycache__", ".cache", "venv", ".venv"]
}
```

- `roots` — directories to index (recursively).
- `excludes` — directory/file **names** to skip anywhere in the tree
  (matched exactly, not as a path).

Edit this by hand, via `--add-root`, or via the in-app **Edit Config**
button — then click **Reindex** to pick up the changes (it reloads the
config from disk first).

The index itself is a SQLite file at `~/.config/fsearch-lite/index.db` —
delete it any time to force a clean rebuild.

## Desktop integration

**1. Icon** — copy it somewhere permanent:

```bash
mkdir -p ~/.local/share/icons
cp fsearch-lite-icon.svg ~/.local/share/icons/
```

**2. Desktop entry** — create `~/.local/share/applications/fsearch-lite.desktop`:

```ini
[Desktop Entry]
Type=Application
Name=fsearch-lite
Comment=Fast filename search
Exec=/home/you/bin/fsearch_lite.py
Icon=/home/you/.local/share/icons/fsearch-lite-icon.svg
Terminal=false
Categories=Utility;FileTools;
StartupNotify=true
```

Replace the paths with wherever you actually put things — `Exec=` needs an
**absolute path**, it won't expand `~`.

**3. Register it:**

```bash
update-desktop-database ~/.local/share/applications/
```

It'll now show up in the Cinnamon menu, and you can pin it to the panel or
favorites like any other app. For a global keyboard shortcut instead:
Cinnamon Settings → Keyboard → Shortcuts → Custom Shortcuts, pointing at the
same `Exec` command.

## Background reindexing (systemd)

To keep the index fresh automatically, without opening the app:

```bash
mkdir -p ~/.config/systemd/user
cp fsearch-lite-reindex.service fsearch-lite-reindex.timer ~/.config/systemd/user/
```

Edit the `ExecStart=` line in `fsearch-lite-reindex.service` to point at
your actual `fsearch_lite.py` path, then enable it:

```bash
systemctl --user daemon-reload
systemctl --user enable --now fsearch-lite-reindex.timer
```

Check it's working:

```bash
systemctl --user list-timers fsearch-lite-reindex.timer   # next scheduled run
journalctl --user -u fsearch-lite-reindex.service          # logs from past runs
```

Notes:

- Fires 5 minutes after login, then every 4 hours from whenever it last
  actually ran (not a fixed clock time) — `Persistent=true` means a missed
  run (e.g. laptop asleep) fires once at next login instead of being skipped.
- Runs niced (`Nice=19`, best-effort I/O) so it doesn't compete with
  foreground work.
- This is a **user** unit, so it only runs while you're logged in. If you
  want it to run even in logged-out/SSH-only sessions, run
  `loginctl enable-linger $USER` once.

## Using it

- Type space-separated terms in the search box — they're AND-ed together.
- Click the `.* regex` toggle to switch to full regex search.
- Click a column header to sort by it; click again to reverse. Sorting by
  Modified descending is the fast way to spot the newest copy of a file.
- Double-click a result to jump to its folder.
- Right-click for Open / Open With… / Show in Folder / Move to Trash.
- Click "Edit Config" to change indexed roots or excludes, then "Reindex".
- Click "Reindex" any time — there's no live filesystem watcher in this
  version (that's what the systemd timer above is for).

## Possible follow-ups (not included, but easy to bolt on)

- A `watchdog`/`inotify`-based live-update mode instead of periodic
  reindexing, for near-instant awareness of new/moved files.
- A "show only duplicate names" filter, to make it more obvious at a glance
  when several copies of the same file exist in different places.

#!/usr/bin/env python3
"""
fsearch-lite: a minimal filename/path indexer + search GUI.

Replaces the fsearch workflow (multi-term AND search, regex mode, sort by
column, double-click to jump to the file's folder) using SQLite FTS5 for
the index and GTK3 for the GUI, so it needs nothing beyond what's already
on a stock Linux Mint install (python3-gi, gir1.2-gtk-3.0).

Usage:
    ./fsearch_lite.py                 # launch GUI (uses ~/.config/fsearch-lite/config.json)
    ./fsearch_lite.py --add-root DIR  # add a root to index, then launch
    ./fsearch_lite.py --reindex       # reindex from the command line, no GUI

Config file (created on first run): ~/.config/fsearch-lite/config.json
    {
      "roots": ["/home/you"],
      "excludes": [".git", "node_modules", "__pycache__", ".cache", "venv", ".venv"]
    }
"""
import argparse
import os
import sqlite3
import subprocess
import time

import gi
gi.require_version("Gtk", "3.0")
from gi.repository import Gtk, Gio, GLib

from fsearch_core import (
    load_config, save_config, get_db, build_index, search, delete_indexed_path,
    DB_PATH, CONFIG_PATH,
)


# --------------------------------------------------------------------------
# GUI
# --------------------------------------------------------------------------

def human_size(n):
    if n is None:
        return ""
    for unit in ["B", "K", "M", "G", "T"]:
        if abs(n) < 1024:
            return f"{n:.0f}{unit}" if unit == "B" else f"{n:.1f}{unit}"
        n /= 1024
    return f"{n:.1f}P"


def human_time(t):
    if t is None:
        return ""
    return time.strftime("%Y-%m-%d %H:%M", time.localtime(t))


class FSearchLite(Gtk.Window):
    def __init__(self, cfg):
        super().__init__(title="fsearch-lite")
        self.cfg = cfg
        self.conn = get_db()
        self.set_default_size(900, 600)
        self.connect("destroy", Gtk.main_quit)

        vbox = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        vbox.set_border_width(8)
        self.add(vbox)

        # --- search bar ---
        hbox = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        vbox.pack_start(hbox, False, False, 0)

        self.entry = Gtk.SearchEntry()
        self.entry.set_placeholder_text("term1 term2 ...  (AND search, or regex if toggled)")
        self.entry.connect("changed", self.on_search_changed)
        hbox.pack_start(self.entry, True, True, 0)

        self.regex_toggle = Gtk.ToggleButton(label=".*  regex")
        self.regex_toggle.connect("toggled", self.on_search_changed)
        hbox.pack_start(self.regex_toggle, False, False, 0)

        reindex_btn = Gtk.Button(label="Reindex")
        reindex_btn.connect("clicked", self.on_reindex_clicked)
        hbox.pack_start(reindex_btn, False, False, 0)

        config_btn = Gtk.Button(label="Edit Config")
        config_btn.connect("clicked", self.on_edit_config_clicked)
        hbox.pack_start(config_btn, False, False, 0)

        # --- results list ---
        self.store = Gtk.ListStore(str, str, str, str, str, int)  # name, dir, size_disp, mtime_disp, path, is_dir
        self.tree = Gtk.TreeView(model=self.store)
        self.tree.connect("row-activated", self.on_row_activated)
        self.tree.connect("button-press-event", self.on_button_press)

        columns = [("Name", 0, True), ("Directory", 1, True), ("Size", 2, False), ("Modified", 3, False)]
        for title, col_id, expand in columns:
            renderer = Gtk.CellRendererText()
            renderer.set_property("ellipsize", 3)  # PANGO_ELLIPSIZE_END
            col = Gtk.TreeViewColumn(title, renderer, text=col_id)
            col.set_resizable(True)
            col.set_sort_column_id(col_id)
            if expand:
                col.set_expand(True)
            self.tree.append_column(col)

        self._sort_col_map = {0: "name", 1: "dir", 2: "size", 3: "mtime"}
        self.store.set_sort_func(0, self._sort_noop)  # placeholder; real sort done in SQL, see below
        self.tree.connect("query-tooltip", lambda *a: False)

        scroller = Gtk.ScrolledWindow()
        scroller.set_policy(Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.AUTOMATIC)
        scroller.add(self.tree)
        vbox.pack_start(scroller, True, True, 0)

        # hook column header clicks to re-run the SQL query with new sort order
        for i, col in enumerate(self.tree.get_columns()):
            col.connect("clicked", self.on_column_clicked, i)
            col.set_clickable(True)

        self._sort_col = "name"
        self._sort_desc = False

        # --- status bar ---
        self.status = Gtk.Label(label="Ready", xalign=0)
        vbox.pack_start(self.status, False, False, 0)

        self.refresh_results()

    def _sort_noop(self, *a):
        return 0

    def on_column_clicked(self, col, idx):
        new_sort = self._sort_col_map[idx]
        if self._sort_col == new_sort:
            self._sort_desc = not self._sort_desc
        else:
            self._sort_col = new_sort
            self._sort_desc = False
        self.refresh_results()

    def on_search_changed(self, *a):
        self.refresh_results()

    def refresh_results(self):
        query = self.entry.get_text()
        regex_mode = self.regex_toggle.get_active()
        self.store.clear()
        try:
            rows = search(self.conn, query, regex_mode, self._sort_col, self._sort_desc)
        except sqlite3.OperationalError as e:
            self.status.set_text(f"Query error: {e}")
            return
        for name, dir_, path, size, mtime, is_dir in rows:
            self.store.append([
                name, dir_, human_size(size), human_time(mtime), path, is_dir
            ])
        self.status.set_text(f"{len(rows)} result(s)")

    def on_row_activated(self, tree, path_iter, column):
        model = tree.get_model()
        row = model[path_iter]
        self.show_in_folder(row[4], row[5])

    def show_in_folder(self, path, is_dir):
        target = path
        try:
            # Nemo opens the parent folder with the file pre-selected when
            # given a file path directly.
            subprocess.Popen(["nemo", target])
        except FileNotFoundError:
            # fall back: open the containing directory with xdg-open
            open_dir = target if is_dir else os.path.dirname(target)
            subprocess.Popen(["xdg-open", open_dir])

    # --- right-click context menu ---------------------------------------

    def on_button_press(self, widget, event):
        if event.button != 3:
            return False
        path_info = self.tree.get_path_at_pos(int(event.x), int(event.y))
        if path_info is None:
            return False
        path, col, _cellx, _celly = path_info
        self.tree.set_cursor(path, col, False)  # select the right-clicked row
        self.show_context_menu(event)
        return True

    def show_context_menu(self, event):
        model, treeiter = self.tree.get_selection().get_selected()
        if treeiter is None:
            return
        row = model[treeiter]
        full_path, is_dir = row[4], row[5]

        menu = Gtk.Menu()

        item_open = Gtk.MenuItem(label="Open")
        item_open.connect("activate", lambda w: self.open_default(full_path))
        menu.append(item_open)

        item_open_with = Gtk.MenuItem(label="Open With…")
        item_open_with.connect("activate", lambda w: self.open_with_dialog(full_path))
        menu.append(item_open_with)

        item_show = Gtk.MenuItem(label="Show in Folder")
        item_show.connect("activate", lambda w: self.show_in_folder(full_path, is_dir))
        menu.append(item_show)

        menu.append(Gtk.SeparatorMenuItem())

        item_trash = Gtk.MenuItem(label="Move to Trash")
        item_trash.connect("activate", lambda w: self.move_to_trash(full_path, treeiter))
        menu.append(item_trash)

        menu.show_all()
        menu.popup_at_pointer(event)

    def open_default(self, path):
        try:
            gfile = Gio.File.new_for_path(path)
            Gio.AppInfo.launch_default_for_uri(gfile.get_uri(), None)
        except GLib.Error as e:
            self.status.set_text(f"Couldn't open: {e.message}")

    def open_with_dialog(self, path):
        gfile = Gio.File.new_for_path(path)
        dialog = Gtk.AppChooserDialog.new(self, Gtk.DialogFlags.MODAL, gfile)
        response = dialog.run()
        if response == Gtk.ResponseType.OK:
            app_info = dialog.get_app_info()
            if app_info is not None:
                try:
                    app_info.launch([gfile], None)
                except GLib.Error as e:
                    self.status.set_text(f"Couldn't launch: {e.message}")
        dialog.destroy()

    def move_to_trash(self, path, treeiter):
        gfile = Gio.File.new_for_path(path)
        try:
            gfile.trash()
        except GLib.Error as e:
            self.status.set_text(f"Couldn't trash {os.path.basename(path)}: {e.message}")
            return
        delete_indexed_path(self.conn, path)
        self.store.remove(treeiter)
        self.status.set_text(f"Moved to trash: {os.path.basename(path)}")

    # --- toolbar buttons ---------------------------------------------------

    def on_reindex_clicked(self, *a):
        self.cfg = load_config()  # pick up any edits made via "Edit Config"
        self.status.set_text("Reindexing...")
        while Gtk.events_pending():
            Gtk.main_iteration()

        def progress(count):
            self.status.set_text(f"Reindexing... {count} entries")
            while Gtk.events_pending():
                Gtk.main_iteration()

        count = build_index(self.cfg["roots"], self.cfg["excludes"], progress_cb=progress)
        self.conn.close()
        self.conn = get_db()
        self.status.set_text(f"Indexed {count} entries")
        self.refresh_results()

    def on_edit_config_clicked(self, *a):
        candidates = [["xdg-open", CONFIG_PATH]]
        for editor in ["xed", "gedit", "gnome-text-editor", "kate", "code"]:
            candidates.append([editor, CONFIG_PATH])

        for cmd in candidates:
            try:
                subprocess.Popen(cmd)
                self.status.set_text(f"Opened {CONFIG_PATH} — press Reindex after saving changes")
                return
            except FileNotFoundError:
                continue
        self.status.set_text(f"Couldn't find an editor to open. Config is at: {CONFIG_PATH}")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--add-root", action="append", default=[], help="Add a directory to index (repeatable)")
    parser.add_argument("--reindex", action="store_true", help="Reindex from the CLI and exit (no GUI)")
    args = parser.parse_args()

    cfg = load_config()
    if args.add_root:
        for r in args.add_root:
            r = os.path.abspath(os.path.expanduser(r))
            if r not in cfg["roots"]:
                cfg["roots"].append(r)
        save_config(cfg)

    if args.reindex:
        count = build_index(cfg["roots"], cfg["excludes"])
        print(f"Indexed {count} entries into {DB_PATH}")
        return

    if not os.path.exists(DB_PATH):
        print("No index found, building initial index (this runs once)...")
        build_index(cfg["roots"], cfg["excludes"])

    win = FSearchLite(cfg)
    win.show_all()
    Gtk.main()


if __name__ == "__main__":
    main()

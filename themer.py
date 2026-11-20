#!/usr/bin/env python3
"""themer - live GTK / icon theme switcher.

GUI:  themer                          pick a theme: the window previews it live, Apply commits it
CLI:  themer --gtk NAME --icons NAME  apply without a window
      themer --list | --current

Apply writes every place a theme name lives and reloads it live (no relogin):
  xsettingsd + gsettings            running GTK2/3/4 apps (live)
  gtk-3.0 / gtk-4.0 settings.ini    GTK apps started later
  ~/.gtkrc-2.0                      GTK2 apps
  qt5ct / qt6ct icon_theme          Qt apps (icons apply to apps started afterwards)
  ~/.config/themer/hooks.d/*        your own executables, run after every apply with
                                    THEMER_GTK and THEMER_ICONS set (rofi, bspwm, ...)
"""
__version__ = "0.1.0"

import argparse
import configparser
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

HOME = Path.home()
CFG = HOME / ".config"
GTK_DIRS = [HOME / ".themes", HOME / ".local/share/themes", Path("/usr/share/themes")]
ICON_DIRS = [HOME / ".icons", HOME / ".local/share/icons", Path("/usr/share/icons")]
HOOKS_DIR = CFG / "themer/hooks.d"
HIDDEN_ICON_THEMES = {"hicolor", "default", "locolor"}


# ---------------------------------------------------------------- discovery
def discover_gtk():
    found = {}
    for d in GTK_DIRS:
        if not d.is_dir():
            continue
        for p in sorted(d.iterdir()):
            if p.name not in found and (p / "gtk-3.0").is_dir():
                found[p.name] = p
    return sorted(found, key=str.lower)


def discover_icons():
    found = {}
    for d in ICON_DIRS:
        if not d.is_dir():
            continue
        for p in sorted(d.iterdir()):
            idx = p / "index.theme"
            if p.name in found or p.name in HIDDEN_ICON_THEMES or not idx.is_file():
                continue
            cp = configparser.ConfigParser(strict=False, interpolation=None)
            try:
                cp.read(idx, encoding="utf-8")
                sect = cp["Icon Theme"]
            except (configparser.Error, KeyError, UnicodeDecodeError):
                continue
            # skip cursor-only themes and hidden ones: a real icon theme lists Directories
            if sect.get("Hidden", "false").lower() == "true" or not sect.get("Directories", "").strip():
                continue
            found[p.name] = p
    return sorted(found, key=str.lower)


def current():
    """What is live right now: xsettingsd first, gsettings as a fallback."""
    gtk = icons = None
    xs = CFG / "xsettingsd/xsettingsd.conf"
    if xs.is_file():
        t = xs.read_text()
        m = re.search(r'^Net/ThemeName\s+"([^"]*)"', t, re.M)
        gtk = m.group(1) if m else None
        m = re.search(r'^Net/IconThemeName\s+"([^"]*)"', t, re.M)
        icons = m.group(1) if m else None
    if not gtk:
        gtk = _gsettings_get("gtk-theme")
    if not icons:
        icons = _gsettings_get("icon-theme")
    return gtk, icons


def _gsettings_get(key):
    try:
        out = subprocess.run(["gsettings", "get", "org.gnome.desktop.interface", key],
                             capture_output=True, text=True, timeout=5).stdout.strip()
        return out.strip("'") or None
    except (OSError, subprocess.SubprocessError):
        return None


# ---------------------------------------------------------------- applying
def _rewrite(path, pattern, repl, append=None, create=False):
    """Regex-replace in a file (written in place, so symlinks keep working).
    Returns True when the file changed. `append` is added if the pattern is absent."""
    path = Path(path)
    if not path.is_file():
        if not (create and append is not None):
            return False
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("")
    text = path.read_text()
    new, n = re.subn(pattern, repl, text, flags=re.M)
    if n == 0 and append is not None:
        new = text + ("" if text.endswith("\n") or not text else "\n") + append + "\n"
    if new == text:
        return False
    path.write_text(new)
    return True


def _set_ini(path, key, value):
    """Set `key=value` in the [Settings] section of a GTK settings.ini, creating the file,
    the section or the key when missing. Returns True when the file changed."""
    path = Path(path)
    line = f"{key}={value}"
    text = path.read_text() if path.is_file() else ""
    if re.search(rf"^{re.escape(key)}\s*=", text, re.M):
        new = re.sub(rf"^{re.escape(key)}\s*=.*$", lambda m: line, text, flags=re.M)
    elif re.search(r"^\[Settings\]\s*$", text, re.M):
        new = re.sub(r"^\[Settings\]\s*$", lambda m: "[Settings]\n" + line, text, count=1, flags=re.M)
    else:
        new = text + ("" if not text or text.endswith("\n") else "\n") + "[Settings]\n" + line + "\n"
    if new == text:
        return False
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(new)
    return True


def _gtkrc_files():
    return [HOME / ".gtkrc-2.0"]


def _run_hooks(gtk, icons, log):
    """Run user hooks (executables in HOOKS_DIR) with the new theme names in the environment."""
    if not HOOKS_DIR.is_dir():
        return
    env = dict(os.environ, THEMER_GTK=gtk or "", THEMER_ICONS=icons or "")
    for hook in sorted(HOOKS_DIR.iterdir()):
        if not (hook.is_file() and os.access(hook, os.X_OK)):
            continue
        try:
            r = subprocess.run([str(hook)], env=env, capture_output=True, text=True, timeout=15)
            log.append(f"hook {hook.name}: " + ("ok" if r.returncode == 0 else f"exit {r.returncode}"))
        except (OSError, subprocess.SubprocessError) as e:
            log.append(f"hook {hook.name} failed: {e}")


def apply(gtk=None, icons=None):
    """Apply a GTK theme and/or icon theme everywhere. Returns a list of what changed."""
    log = []

    def note(changed, what):
        log.append(("updated " if changed else "unchanged ") + what)

    if gtk:
        note(_rewrite(CFG / "xsettingsd/xsettingsd.conf", r'^Net/ThemeName\s.*$',
                      f'Net/ThemeName "{gtk}"', f'Net/ThemeName "{gtk}"', create=True), "xsettingsd (GTK theme)")
        for v in ("3", "4"):
            note(_set_ini(CFG / f"gtk-{v}.0/settings.ini", "gtk-theme-name", gtk),
                 f"gtk-{v}.0 settings.ini (GTK theme)")
        for rc in _gtkrc_files():
            note(_rewrite(rc, r'^gtk-theme-name\s*=.*$', f'gtk-theme-name="{gtk}"',
                          f'gtk-theme-name="{gtk}"', create=True), f"{rc.name} (GTK theme)")
    if icons:
        note(_rewrite(CFG / "xsettingsd/xsettingsd.conf", r'^Net/IconThemeName\s.*$',
                      f'Net/IconThemeName "{icons}"', f'Net/IconThemeName "{icons}"', create=True), "xsettingsd (icons)")
        for v in ("3", "4"):
            note(_set_ini(CFG / f"gtk-{v}.0/settings.ini", "gtk-icon-theme-name", icons),
                 f"gtk-{v}.0 settings.ini (icons)")
        for rc in _gtkrc_files():
            note(_rewrite(rc, r'^gtk-icon-theme-name\s*=.*$', f'gtk-icon-theme-name="{icons}"',
                          f'gtk-icon-theme-name="{icons}"', create=True), f"{rc.name} (icons)")
        for q in ("qt5ct", "qt6ct"):
            note(_rewrite(CFG / f"{q}/{q}.conf", r'^icon_theme=.*$', f"icon_theme={icons}"), f"{q} (icons)")

    # reload: xsettingsd re-reads on SIGHUP and pushes the change to every running GTK app
    if subprocess.run(["pgrep", "-x", "xsettingsd"], capture_output=True).returncode == 0:
        subprocess.run(["pkill", "-HUP", "-x", "xsettingsd"])
    elif shutil.which("xsettingsd"):
        subprocess.Popen(["xsettingsd"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                         start_new_session=True)
    else:
        log.append("xsettingsd not installed: running apps keep the old theme until restarted")
    for key, val in (("gtk-theme", gtk), ("icon-theme", icons)):  # GTK4 / libadwaita via dconf
        if val:
            try:
                subprocess.run(["gsettings", "set", "org.gnome.desktop.interface", key, val],
                               capture_output=True, timeout=5)
            except (OSError, subprocess.SubprocessError):
                log.append(f"gsettings {key} failed")
    _run_hooks(gtk, icons, log)
    return log


# ---------------------------------------------------------------- GUI
def run_gui():
    import gi
    gi.require_version("Gtk", "3.0")
    gi.require_version("Gdk", "3.0")
    gi.require_version("GdkPixbuf", "2.0")
    from gi.repository import GdkPixbuf, GLib, Gtk, Pango

    ICON_SAMPLES = [
        "folder", "user-home", "user-trash", "text-x-generic", "image-x-generic", "audio-x-generic",
        "video-x-generic", "application-x-executable", "utilities-terminal", "web-browser",
        "preferences-system", "system-file-manager", "accessories-calculator", "drive-harddisk",
        "network-wired", "camera-photo", "audio-volume-high", "mail-unread", "system-search",
        "document-open", "edit-copy", "list-add", "media-playback-start", "dialog-warning",
    ]

    class Themer(Gtk.Window):
        def __init__(self):
            super().__init__(title="Themer")
            self.set_default_size(1000, 640)
            self.set_icon_name("preferences-desktop-theme")
            self.settings = Gtk.Settings.get_default()
            self.gtk_names, self.icon_names = discover_gtk(), discover_icons()
            self.applied_gtk, self.applied_icons = current()
            self.sel_gtk, self.sel_icons = self.applied_gtk, self.applied_icons
            self.rows = {"gtk": {}, "icons": {}}
            self._building = True

            hb = Gtk.HeaderBar(show_close_button=True, title="Themer",
                               subtitle="GTK + icon themes, live")
            self.set_titlebar(hb)
            self.apply_btn = Gtk.Button(label="Apply")
            self.apply_btn.get_style_context().add_class("suggested-action")
            self.apply_btn.connect("clicked", lambda *_: self.on_apply())
            self.revert_btn = Gtk.Button(label="Revert preview")
            self.revert_btn.connect("clicked", lambda *_: self.on_revert())
            hb.pack_end(self.apply_btn)
            hb.pack_end(self.revert_btn)

            root = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12,
                           margin=12)
            self.add(root)

            lists = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
            lists.set_size_request(280, -1)
            lists.pack_start(self._list("GTK theme", self.gtk_names, "gtk", self.applied_gtk), True, True, 0)
            lists.pack_start(self._list("Icon theme", self.icon_names, "icons", self.applied_icons), True, True, 0)
            root.pack_start(lists, False, False, 0)
            root.pack_start(self._preview(), True, True, 0)

            self.status = Gtk.Label(xalign=0, wrap=True, selectable=True)
            self.status.get_style_context().add_class("dim-label")
            self.connect("key-press-event", self._on_key)
            self._building = False
            self._select_initial()
            self._set_status("Select a theme to preview it here. Apply makes it live system-wide "
                             "(Ctrl+Enter).")
            self.status_box.pack_start(self.status, True, True, 0)

        # -- widgets
        def _list(self, title, names, kind, active):
            box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
            lbl = Gtk.Label(xalign=0, use_markup=True)
            lbl.set_markup(f"<b>{GLib.markup_escape_text(title)}</b>  <small>({len(names)})</small>")
            box.pack_start(lbl, False, False, 0)
            sw = Gtk.ScrolledWindow(hexpand=True, vexpand=True, shadow_type=Gtk.ShadowType.IN)
            lb = Gtk.ListBox(selection_mode=Gtk.SelectionMode.SINGLE)
            for n in names:
                row = Gtk.ListBoxRow()
                row.name = n
                h = Gtk.Box(spacing=6, margin=6)
                lab = Gtk.Label(label=n, xalign=0, hexpand=True, ellipsize=Pango.EllipsizeMode.END)
                mark = Gtk.Label(label="")
                h.pack_start(lab, True, True, 0)
                h.pack_end(mark, False, False, 0)
                row.add(h)
                row.mark = mark
                lb.add(row)
                self.rows[kind][n] = row
            lb.connect("row-selected", self._on_row, kind)
            sw.add(lb)
            box.pack_start(sw, True, True, 0)
            setattr(self, f"lb_{kind}", lb)
            self._mark(kind, active)
            return box

        def _preview(self):
            col = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
            self.name_lbl = Gtk.Label(xalign=0, use_markup=True)
            col.pack_start(self.name_lbl, False, False, 0)

            frame = Gtk.Frame(label=" Icons ")
            self.icon_store = Gtk.ListStore(GdkPixbuf.Pixbuf, str)
            iv = Gtk.IconView(model=self.icon_store, item_width=84, columns=8,
                              margin=6, selection_mode=Gtk.SelectionMode.NONE)
            iv.set_pixbuf_column(0)
            iv.set_text_column(1)
            iv.set_item_orientation(Gtk.Orientation.VERTICAL)
            sw = Gtk.ScrolledWindow(min_content_height=250, hexpand=True,
                                    hscrollbar_policy=Gtk.PolicyType.NEVER)
            sw.add(iv)
            frame.add(sw)
            col.pack_start(frame, True, True, 0)

            frame2 = Gtk.Frame(label=" Widgets ")
            g = Gtk.Grid(column_spacing=12, row_spacing=8, margin=10)
            btn = Gtk.Button(label="Button")
            sug = Gtk.Button(label="Suggested")
            sug.get_style_context().add_class("suggested-action")
            des = Gtk.Button(label="Destructive")
            des.get_style_context().add_class("destructive-action")
            ib = Gtk.Button.new_from_icon_name("document-open", Gtk.IconSize.BUTTON)
            bar = Gtk.Box(spacing=6)
            for w in (btn, sug, des, ib):
                bar.pack_start(w, False, False, 0)
            g.attach(bar, 0, 0, 2, 1)
            ent = Gtk.Entry(text="Text entry", hexpand=True)
            g.attach(ent, 0, 1, 1, 1)
            combo = Gtk.ComboBoxText()
            for t in ("Combo box", "Second", "Third"):
                combo.append_text(t)
            combo.set_active(0)
            g.attach(combo, 1, 1, 1, 1)
            chk = Gtk.CheckButton(label="Check", active=True)
            rad = Gtk.RadioButton(label="Radio")
            sw_ = Gtk.Switch(active=True, halign=Gtk.Align.START)
            row = Gtk.Box(spacing=14)
            for w in (chk, rad, sw_):
                row.pack_start(w, False, False, 0)
            g.attach(row, 0, 2, 2, 1)
            prog = Gtk.ProgressBar(fraction=0.6, hexpand=True)
            scale = Gtk.Scale.new_with_range(Gtk.Orientation.HORIZONTAL, 0, 100, 1)
            scale.set_value(40)
            scale.set_draw_value(False)
            g.attach(prog, 0, 3, 1, 1)
            g.attach(scale, 1, 3, 1, 1)
            ls = Gtk.ListStore(str, str)
            for a, b in (("Selected row (highlight colour)", "Folder"), ("Normal row", "Document"),
                         ("Another row", "Folder")):
                ls.append([a, b])
            tv = Gtk.TreeView(model=ls, hexpand=True, headers_visible=True)
            for i, t in enumerate(("Name", "Type")):
                tv.append_column(Gtk.TreeViewColumn(t, Gtk.CellRendererText(), text=i))
            tv.get_selection().select_path(Gtk.TreePath.new_first())
            g.attach(tv, 0, 4, 2, 1)
            frame2.add(g)
            col.pack_start(frame2, False, False, 0)

            self.status_box = Gtk.Box()
            col.pack_start(self.status_box, False, False, 0)
            return col

        # -- state
        def _mark(self, kind, name):
            for n, row in self.rows[kind].items():
                row.mark.set_text("✓ applied" if n == name else "")

        def _select_initial(self):
            for kind, name in (("gtk", self.applied_gtk), ("icons", self.applied_icons)):
                row = self.rows[kind].get(name)
                lb = getattr(self, f"lb_{kind}")
                if row is not None:
                    lb.select_row(row)
                    row.grab_focus()
            self._refresh_preview()

        def _on_row(self, lb, row, kind):
            if self._building or row is None:
                return
            if kind == "gtk":
                self.sel_gtk = row.name
            else:
                self.sel_icons = row.name
            self._refresh_preview()

        def _refresh_preview(self):
            # the window itself takes the theme: this is the live preview
            if self.sel_gtk:
                self.settings.set_property("gtk-theme-name", self.sel_gtk)
            if self.sel_icons:
                self.settings.set_property("gtk-icon-theme-name", self.sel_icons)
                self._load_icons(self.sel_icons)
            esc = GLib.markup_escape_text
            self.name_lbl.set_markup(f"Previewing  <b>{esc(self.sel_gtk or '?')}</b>  +  "
                                     f"<b>{esc(self.sel_icons or '?')}</b>")
            dirty = (self.sel_gtk, self.sel_icons) != (self.applied_gtk, self.applied_icons)
            self.apply_btn.set_sensitive(dirty)
            self.revert_btn.set_sensitive(dirty)

        def _load_icons(self, name):
            self.icon_store.clear()
            theme = Gtk.IconTheme()
            theme.set_custom_theme(name)
            for n in ICON_SAMPLES:
                try:
                    pb = theme.load_icon(n, 48, Gtk.IconLookupFlags.FORCE_SIZE)
                except GLib.Error:
                    continue
                self.icon_store.append([pb, n])

        def _set_status(self, text):
            self.status.set_text(text)

        def on_apply(self):
            try:
                log = apply(self.sel_gtk, self.sel_icons)
            except Exception as e:  # surface, never crash the GUI
                self._set_status(f"Apply failed: {e}")
                return
            self.applied_gtk, self.applied_icons = self.sel_gtk, self.sel_icons
            self._mark("gtk", self.applied_gtk)
            self._mark("icons", self.applied_icons)
            n = sum(1 for x in log if x.startswith("updated"))
            self._set_status(f"Applied {self.applied_gtk} + {self.applied_icons} live ({n} configs updated). "
                             "Running GTK apps update now; Qt apps pick up icons when restarted.")
            self._refresh_preview()

        def on_revert(self):
            self._building = True
            for kind, name in (("gtk", self.applied_gtk), ("icons", self.applied_icons)):
                row = self.rows[kind].get(name)
                if row is not None:
                    getattr(self, f"lb_{kind}").select_row(row)
            self._building = False
            self.sel_gtk, self.sel_icons = self.applied_gtk, self.applied_icons
            self._refresh_preview()
            self._set_status("Preview reverted to the applied themes.")

        def _on_key(self, _w, ev):
            from gi.repository import Gdk
            ctrl = ev.state & Gdk.ModifierType.CONTROL_MASK
            if ctrl and ev.keyval in (Gdk.KEY_Return, Gdk.KEY_KP_Enter):
                if self.apply_btn.get_sensitive():
                    self.on_apply()
                return True
            if ev.keyval == Gdk.KEY_Escape:
                self.close()
                return True
            return False

    win = Themer()
    win.connect("destroy", Gtk.main_quit)
    win.show_all()
    Gtk.main()


# ---------------------------------------------------------------- entry
def main():
    ap = argparse.ArgumentParser(description="Live GTK / icon theme switcher")
    ap.add_argument("--gtk", help="GTK theme name to apply")
    ap.add_argument("--icons", help="icon theme name to apply")
    ap.add_argument("--list", action="store_true", help="list installed GTK and icon themes")
    ap.add_argument("--version", action="version", version=f"themer {__version__}")
    ap.add_argument("--current", action="store_true", help="print the live GTK and icon theme")
    a = ap.parse_args()

    if a.list:
        print("GTK themes:\n  " + "\n  ".join(discover_gtk()))
        print("Icon themes:\n  " + "\n  ".join(discover_icons()))
        return 0
    if a.current:
        g, i = current()
        print(f"gtk={g}\nicons={i}")
        return 0
    if a.gtk or a.icons:
        if a.gtk and a.gtk not in discover_gtk():
            sys.exit(f"themer: unknown GTK theme: {a.gtk}")
        if a.icons and a.icons not in discover_icons():
            sys.exit(f"themer: unknown icon theme: {a.icons}")
        print("\n".join(apply(a.gtk, a.icons)))
        return 0
    if not os.environ.get("DISPLAY") and not os.environ.get("WAYLAND_DISPLAY"):
        sys.exit("themer: no display; use --gtk/--icons for headless use")
    run_gui()
    return 0


if __name__ == "__main__":
    sys.exit(main())

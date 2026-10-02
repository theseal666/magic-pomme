# reference

The working configuration this project was derived from — a hand-built,
verified "Mac mode" on Debian 13 / KDE Plasma 6 Wayland. These are the files
magic-pomme should eventually generate and manage.

| File | Installed to | Purpose |
|---|---|---|
| `hid_apple.conf` | `/etc/modprobe.d/` | `swap_ctrl_cmd=1 fnmode=1` |
| `default.conf` | `/etc/keyd/` | mac text navigation layers |
| `kitty.conf` | `~/.config/kitty/` | `copy_or_interrupt` + Ctrl-restore |
| `kde-mac-shortcuts.py` | — | rewrites `kglobalshortcutsrc` for mac parity |

Known dead code: the `macro(rightalt+N)` lines in `default.conf` do not work.
See the keyd section of `../docs/platform-notes.md`.

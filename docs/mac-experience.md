# The full Mac experience: what magic-pomme owns

The goal is that *every* setting needed to make a Linux desktop feel like
macOS lives in this app. Nothing should require hand-editing a config file.

This is the inventory, honestly marked. It was derived from building the
experience by hand first, so each row is something that actually mattered --
not a guess at what might.

## In the app

| Setting | Where it lives | Surface |
|---|---|---|
| Cmd acts as Ctrl | `hid_apple.swap_ctrl_cmd` | CLI, tray |
| Top row = media keys | `hid_apple.fnmode` | CLI, tray |
| Apple ISO key quirk | `hid_apple.iso_layout` | CLI |
| Accelerated scrolling | `hid_magicmouse.scroll_acceleration` | CLI, tray |
| Middle click | `hid_magicmouse.emulate_3button` | CLI, tray |
| Scroll rate | `hid_magicmouse.scroll_speed` | CLI |
| Natural scrolling | `kcminputrc` via KWin | CLI, tray |
| Bluetooth pairing | BlueZ | CLI |
| Battery levels | sysfs | CLI, tray |
| Bluetooth input lag | udev rule | setup |
| Privilege, once | group + udev + helper | setup |

## Now in the app, via `profile mac`

These four used to need a config file edited by hand. They are now applied,
and reverted, by the profile.

| Setting | Target | Why it matters |
|---|---|---|
| Mac text navigation | `/etc/keyd/default.conf` | Cmd+arrows for line/document ends, Option+arrows for words, Cmd+Backspace. The single biggest everyday difference after the modifier swap. |
| Keyboard layout | `kxkbrc` | Without it an Apple keyboard's printed legends are wrong. Needs a compositor restart, so it cannot be applied live. |
| Global shortcuts | `kglobalshortcutsrc` | Cmd+Space for Spotlight, Ctrl+Up for Mission Control, Cmd+Shift+3/4 for screenshots, Cmd+Opt+Esc for Force Quit. Also requires clearing nine `Meta+<letter>` bindings so the terminal can use them. |
| Terminal behaviour | `kitty.conf` | Cmd+C must copy-or-interrupt, and `super+<letter>` must send raw control bytes, or the modifier swap destroys every shell control sequence. Konsole cannot do this. |

## The profile

Implemented in `profiles.py`:

```sh
magic-pomme profile mac     # apply the whole tested bundle
magic-pomme profile pc      # back to Linux defaults
magic-pomme profile status  # what is applied, what drifted
```

Individual toggles stay, but the profile is the product: one command gives
someone the result it took a day to work out by hand, rather than fifteen
switches they must understand first.

Two constraints shaped the implementation:

* **Reversibility.** Anything written must be removable. The profile has to
  record what it changed, because `kglobalshortcutsrc` in particular is a
  shared file full of the user's own settings.
* **Not everything applies live.** Driver parameters and keyd bindings do.
  The keyboard layout needs a compositor restart, and group membership needs
  a reboot where systemd lingering is enabled. The profile must say which,
  rather than silently doing nothing.

## Learned while building it

* **KDE rewrites your shortcut strings.** `Ctrl+Meta+Q` is stored as
  `Meta+Ctrl+Q`. Comparing raw strings reports a correctly-applied shortcut as
  pending, so comparisons normalise modifier order first.
* **kglobalshortcutsrc cannot be edited while kglobalacceld runs.** It holds
  the bindings in memory and writes them back over external changes, and it
  resolves conflicts on its own -- reassigning `Meta+Left` to desktop
  switching silently cleared the quick-tile bindings rather than leaving them
  for us to move. The profile stops the daemon around the edit.

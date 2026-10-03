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

## Not yet in the app

These are the four that still need a config file edited by hand. They are the
remaining work, and they are the *large* ones.

| Setting | Currently | Why it matters |
|---|---|---|
| Mac text navigation | hand-written `/etc/keyd/default.conf` | Cmd+arrows for line/document ends, Option+arrows for words, Cmd+Backspace. The single biggest everyday difference after the modifier swap. |
| Keyboard layout | `kxkbrc`, layout `se` variant `mac` | Without it an Apple keyboard's printed legends are wrong. Needs a compositor restart, so it cannot be applied live. |
| Global shortcuts | `kglobalshortcutsrc` via a script | Cmd+Space for Spotlight, Ctrl+Up for Mission Control, Cmd+Shift+3/4 for screenshots, Cmd+Opt+Esc for Force Quit. Also requires clearing nine `Meta+<letter>` bindings so the terminal can use them. |
| Terminal behaviour | hand-written `kitty.conf` | Cmd+C must copy-or-interrupt, and `super+<letter>` must send raw control bytes, or the modifier swap destroys every shell control sequence. Konsole cannot do this. |

## The shape this wants

A profile, not a pile of toggles:

```sh
magic-pomme profile mac     # apply the whole tested bundle
magic-pomme profile pc      # back to Linux defaults
magic-pomme profile status  # what is applied, what drifted
```

Individual toggles stay, but the profile is the product: the point is that one
command gives someone the result it took a day to work out by hand, rather
than eleven switches they have to understand first.

Two constraints shape the implementation:

* **Reversibility.** Anything written must be removable. The profile has to
  record what it changed, because `kglobalshortcutsrc` in particular is a
  shared file full of the user's own settings.
* **Not everything applies live.** Driver parameters and keyd bindings do.
  The keyboard layout needs a compositor restart, and group membership needs
  a reboot where systemd lingering is enabled. The profile must say which,
  rather than silently doing nothing.

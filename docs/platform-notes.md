# Platform notes

Everything here was verified on the target machine, not taken from
documentation. Environment: Debian 13.7, kernel 6.12.107, KDE Plasma 6.3.6 on
Wayland, keyd 2.5.0-4, BlueZ 5.82.

Hardware verified against:

| Device | USB id | BT id | Driver |
|---|---|---|---|
| Magic Keyboard with Touch ID + Numpad | `05ac:029f` | `004c:029f` | `apple` |
| Apple Aluminium Keyboard (ISO) | `05ac:0250` | — | `apple` |
| Magic Mouse 2 (Lightning) | `05ac:0269` | — | `magicmouse` |

## Driver parameters

Both modules expose runtime-writable params, root-owned `0644`. `swap_ctrl_cmd`
and `fnmode` were confirmed to take effect **live**, with no reload or replug.

`/sys/module/hid_apple/parameters/`: `fnmode`, `iso_layout`, `swap_ctrl_cmd`,
`swap_fn_leftctrl`, `swap_opt_cmd`

`/sys/module/hid_magicmouse/parameters/`: `emulate_3button`,
`emulate_scroll_wheel`, `report_undeciphered`, `scroll_acceleration`,
`scroll_speed`

> **Critical design constraint: these are module-global, not per-device.**
> One `swap_ctrl_cmd` covers *every* Apple keyboard attached simultaneously.
> Per-device behaviour is impossible through sysfs and must go through keyd's
> `[ids]` sections. Decide the data model around this first.

`fnmode`: `0` disabled, `1` media keys primary (matches printed legends — the
macOS default), `2` F-keys primary, `3` auto. Debian defaulted to `3`, which
picked the non-mac behaviour.

`iso_layout`: `-1` auto, `0` off, `1` on. Swaps `KEY_GRAVE` with `KEY_102ND` for
Apple ISO keyboards. Auto was **correct** on this hardware — the key left of `1`
gave `§` and the key left of `Z` gave `<`. If those two come out transposed,
set `0`.

## keyd

- Debian renames the binary to **`keyd.rvaiya`** (upstream: `keyd`). Handle both.
- Socket `/run/keyd.socket` is `srw-rw---- root:keyd`; group `keyd` (gid 985).
  **Adding a user to `keyd` grants live remapping with no root** — verified via
  `sg keyd -c 'keyd.rvaiya list-keys'` (314 key names returned).
- IPC commands: `monitor`, `listen`, `bind`, `reload`, `list-keys`, `input`, `do`.
  `bind` applies bindings live without touching the config file.
- Panic sequence if a bad config locks the keyboard: **backspace+escape+enter**.
- **keyd runs below xkb, so binding values are keycodes, not characters.**
  `8 = [` emits the US bracket keycode, which under `se(mac)` produces `å`.
- USB and Bluetooth report **different ids** for the same keyboard
  (`05ac:029f` vs `004c:029f`), so an `[ids]` section scoped to one will break
  when the cable comes out. Use `*` or list both.
- **Unresolved:** emitting an xkb level-3 character from a keyd layer failed.
  Both `8 = G-8` (AltGr prefix) and `8 = macro(rightalt+8)` (explicit chord)
  failed — the first produced nothing, the second garbage. Pressing
  left Option + right Option + 8 by hand *does* produce `[`, so
  `Alt+AltGr+8` resolves fine and the cause is in keyd's emission. Needs
  `keyd monitor` to diagnose.

## Bluetooth

- Magic devices use **Bluetooth Classic HID**, not BLE, so battery arrives
  through the HID report and appears in `/sys/class/power_supply/`. BlueZ
  `Experimental = true` is **not** required, contrary to common advice.
- `trust` is required for auto-reconnect after reboot; `pair` alone is not enough.
- The `apple` driver binds the Bluetooth instance too, so `swap_ctrl_cmd`
  applies identically over USB and BT.
- **USB autosuspend on the Bluetooth adapter causes input lag.** Presents as
  "Apple keyboards feel sluggish on Linux". Fix verified:

  ```
  ACTION=="add", SUBSYSTEM=="usb", ATTR{bInterfaceClass}=="e0", \
      TEST=="power/control", ATTR{power/control}="on"
  ```

  Interface class `e0` is Wireless Controller, which covers BT adapters
  generically rather than hardcoding a vendor id.

## Battery

`/sys/class/power_supply/hid-*-battery/` exposes `capacity` and `status`. The
`device` symlink resolves to the owning HID device, whose basename is the HID
id — **use that as the correlation key**, not the power_supply name, which is a
MAC over Bluetooth and a serial over USB.

### Open: Bluetooth battery reads zero on kernel 6.12.111

Both an Apple Magic Keyboard with Touch ID and a Magic Mouse 2 report
`POWER_SUPPLY_CAPACITY=0` indefinitely over Bluetooth on
`6.12.111+deb13-amd64`, while the same devices on the same pairing reported
real percentages (keyboard 32-34%) on `6.12.107+deb13-amd64` earlier the same
day.

What is ruled out:

- Not a disconnect loop. `bluetoothd` logged no device events for the whole
  boot, and the uhid transports kept their original instance numbers
  (`.0007`, `.0008`), so the devices never re-enumerated.
- Not a missing node. `PRESENT=1`, `ONLINE=1`, `SCOPE=Device`; the kernel
  created the power supply and is polling it.
- Not an error path. No HID, Bluetooth or battery messages in the kernel log.
- Not transient. Flat zero across twenty samples over fifteen minutes, with
  both devices connected and in active use throughout.

Leading hypothesis is a regression in the generic HID battery-strength path
between 6.12.107 and 6.12.111. The competing explanation, not yet excluded, is
that these devices answer the battery report only shortly after pairing and
then go quiet, which fits the same timeline.

To settle it, boot 6.12.107 and run `magic-pomme devices`. If the readings
return, it is a regression worth reporting upstream, since it would affect
every Apple Bluetooth peripheral rather than one machine.

Until then `devices.Battery.unknown` treats capacity 0 as "no reading yet"
rather than an empty battery: a device that is connected and moving the cursor
is not at 0%, and a red empty bar on healthy hardware is a worse lie than
admitting the value is unknown.


## Wayland limits

- **No per-application remapping on KDE Wayland.** keyd's app-aware mapper
  supports X11, Sway and GNOME only. Rules-per-app (Karabiner-style) is not
  achievable; app-specific behaviour must be configured inside the app.
- `setxkbmap -query` reports stale/default values under XWayland and does
  **not** reflect KWin's real keymap. Do not use it to detect layout.
- Layout changes via `kxkbrc` need a compositor restart; they cannot be
  reloaded live.

## The Ctrl-restore problem

`swap_ctrl_cmd=1` makes physical Cmd arrive as Ctrl **and physical Ctrl arrive
as Super**. Terminals therefore lose every control sequence. Two halves to the
fix, and any "Mac mode" profile must ship both:

1. Cmd+C must copy-or-interrupt, not just interrupt. kitty's
   `copy_or_interrupt` does this; Konsole has no equivalent.
2. `super+<letter>` must send the raw control byte (`super+c` → `\x03`) to give
   back a working Ctrl key.

Nine KDE `Meta+<letter>` shortcuts intercept those and must be cleared:
`Meta+A/D/G/L/Q/S/T/V/W`. Bare `Meta` must also be unbound from the application
launcher, or it fires on every tap of the physical Ctrl key.

## xkb

`se(mac)` maps the Swedish Mac characters on level 3: Opt+7/8/9 → `| [ ]`,
Opt+Shift+7/8/9 → `\ { }`, Opt+2 → `@`, Opt+E → `€`.

Both `level3(ralt_switch)` and `level3(lalt_switch)` define their key as
`ISO_Level3_Shift` with `type="ONE_LEVEL"` and **no `modifier_map`**, so the key
stops contributing Alt. `se(mac)` already applies `ralt_switch`, which means
**left Option is the only source of Alt in the keymap** — adding `lalt_switch`
to get brackets on both Option keys would leave the system with no Alt modifier
at all, breaking Alt+click, menu accelerators and Cmd+Opt+Esc.

## Not possible

- **Touch ID** — no Linux driver; requires Apple's Secure Enclave handshake.
- **Magic Mouse gestures** beyond click and single-finger scroll.
  Note: Magic Mouse 2 *does* work as a wired USB HID device (contrary to common
  claims that the Lightning port is charge-only); the port is simply on the
  underside, making it impractical to hold while plugged in.

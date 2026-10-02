# magic-pomme

**Control for Apple Magic Keyboard & Magic Mouse on Linux** — Cmd/Ctrl swap,
Fn mode, mac-style text navigation, battery, scroll tuning, profiles.

A tray app for the Apple HID settings that Linux exposes in the kernel but no
desktop surfaces. Everything here is reachable from userspace; the point of the
project is not the GUI but the **encoded defaults** — shipping a tested
"Mac mode" bundle so nobody else loses an afternoon to the five traps below.

## Why

Plugging an Apple keyboard into a stock Linux desktop walks you into a series
of problems that are individually small and collectively enough to make most
people give up. All five of these were hit in a single afternoon on Debian 13 /
KDE Plasma 6 with a Magic Keyboard with Touch ID:

1. **`swap_ctrl_cmd` exists in the kernel, but no desktop exposes it.** Without
   it, Cmd+C does nothing and every shortcut is in the wrong place.
2. **The Ctrl-restore trap.** Swapping Cmd→Ctrl silently makes physical Ctrl
   arrive as Super, which destroys *every* shell control sequence — Ctrl+C,
   Ctrl+R, Ctrl+D, Ctrl+Z, Ctrl+W. Guides that recommend the swap never mention
   this, and it is what makes naive setups unusable rather than merely imperfect.
3. **Bluetooth input lag** that presents as "Apple keyboards are sluggish on
   Linux" and is actually USB autosuspend on the Bluetooth adapter. One udev
   rule fixes it.
4. **The Apple ISO `§` / `<` quirk** (`hid_apple.iso_layout`), which silently
   transposes two keys on ISO keyboards.
5. **`fnmode` does not match the printed legends** by default, so the media
   keys on the top row do the wrong thing.

A "Mac mode" toggle that applies the whole tested bundle at once — driver
params, keyd layers, the terminal Ctrl-restore, and no-autosuspend — is the
product. The tray icon is the thin part.

## Scope

- Profile switching: Mac mode / PC mode as one atomic change
- Per-device battery for keyboard, mouse and trackpad
- `hid_apple` knobs: `swap_ctrl_cmd`, `fnmode`, `iso_layout`
- `hid_magicmouse` knobs: `scroll_speed`, `scroll_acceleration`, `emulate_3button`
- Live keybinding changes via the keyd IPC
- Bluetooth connect / disconnect / battery

## Non-goals

- Replicating macOS trackpad/mouse gestures (not available at this layer)
- Per-application profiles — see the Wayland limit in the platform notes
- Touch ID (no Linux driver exists; needs Apple's Secure Enclave handshake)

## Status

Pre-code. `docs/platform-notes.md` holds the verified platform findings;
`reference/` holds the working configuration this was derived from.

Before writing code: check prior art. **Kinto** (`rbreaves/kinto`) does
mac-style remapping on Linux and Windows — its current Wayland/KDE state is
unverified, and the Apple *driver-side* niche may be genuinely unoccupied.

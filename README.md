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

## Installing

Debian and Fedora mark the system Python as externally managed (PEP 668), so
`pip install` into it is refused. Use pipx:

```sh
pipx install --editable ~/Documents/magic-pomme
```

`--editable` means source edits take effect without reinstalling.

> **Note:** pipx installs the shim into `~/.local/bin`, which is **not** on
> root's `secure_path`. So `sudo magic-pomme ...` will fail with "command not
> found" -- give sudo the full path instead:
>
> ```sh
> sudo ~/.local/bin/magic-pomme setup --install
> ```

Uninstall with `pipx uninstall magic-pomme`.


## Tray applet

```sh
magic-pomme-tray
```

Shows connected devices with battery, and quick toggles for Mac modifiers,
accelerated scrolling, middle click and the top-row Fn mode. Toggles that are
not yet writable are shown disabled with the reason, rather than failing
silently or prompting for a password.

PyQt6 rather than PySide6, because Debian ships `python3-pyqt6` and it is
commonly already present. With a pipx install, let the venv see it:

```sh
pipx install --force --editable --system-site-packages ~/Documents/magic-pomme
```

To start it with the desktop, copy `packaging/magic-pomme-tray.service` to
`~/.config/systemd/user/` and `systemctl --user enable --now
magic-pomme-tray.service`.

> The unit is ordered after `graphical-session.target`, **not**
> `default.target`. With systemd user lingering enabled, `default.target` is
> reached at boot before any desktop exists -- measured at 46 seconds before
> the panel on the development machine -- so a tray applet started there would
> find no tray. The applet also waits up to 90s for the panel to claim the
> tray rather than exiting, so it survives losing the race anyway.

### It is running but I see no icon

Plasma hides unfamiliar tray items by default. Click the `^` chevron, or
right-click the tray, choose *Configure System Tray* > *Entries*, and set
magic-pomme to *Shown*.


## Setup

The driver parameters are root-owned, but the GUI should never run as root and
should never nag for a password. So privilege is handled **once**, at install,
and never again.

Read what it will do first:

```sh
magic-pomme setup --print
```

Then apply it:

```sh
sudo ~/.local/bin/magic-pomme setup --install
```

That creates a `magicpomme` group, adds you to it (and to `keyd`), and installs
three files: a root-run helper that chgrps and chmods a **whitelist** of driver
parameters, a udev rule firing it on module load, and a boot-time oneshot for
the case where a module is already loaded before udev replays events. Log out
and back in once for group membership to take effect.

Check at any time with `magic-pomme setup`, and undo with
`sudo magic-pomme setup --uninstall`.

### Is loosening those permissions safe?

Every whitelisted parameter is an input-*behaviour* toggle -- modifier layout,
Fn-key mode, scroll rate. None grant code execution or data access, so the
worst a hostile local process in the group can do is rearrange your modifiers
or make scrolling strange.

One honest edge, stated plainly rather than buried: `swap_ctrl_cmd` also
applies at the lock screen, so modifier behaviour could be altered during
password entry. Low severity, but you should know before opting in.

Membership of `keyd` grants *write* access to the remapping socket but not
*read*: `keyd monitor` reads evdev directly and still needs root, so this is
not a keylogging surface.

### Packaging

Distro maintainers will generally refuse a package that chmods sysfs. For that
route, expose the same whitelist over a D-Bus system service guarded by a
polkit action (`auth_admin_keep`, or `yes` for active local sessions).
`permissions.py` is deliberately the only module that needs root, so that
swap is contained.


## Scope

Every setting needed for a macOS-like desktop belongs here -- see
[docs/mac-experience.md](docs/mac-experience.md) for the full inventory and
what is still missing.


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

#!/usr/bin/env python3
"""Rewrite KDE global shortcuts for macOS parity.

Context: with hid_apple swap_ctrl_cmd=1, physical Cmd arrives as Ctrl and
physical Ctrl arrives as Meta. So "Ctrl+" below means you press Cmd, and
"Meta+" means you press the physical Ctrl key -- which is exactly how macOS
splits them (Cmd for app actions, Ctrl for Mission Control / Spaces).

Nine Meta+<letter> shortcuts are cleared, because physical Ctrl+<letter> must
reach the terminal as a real control code.
"""
import shutil, re, sys, os, time

PATH = os.path.expanduser("~/.config/kglobalshortcutsrc")
# section -> key -> new "current" shortcut value
CHANGES = {
    "kwin": {
        # mac Ctrl+Up = Mission Control, Ctrl+Down = App Expose
        "Overview":                         "Meta+Up",
        "ExposeClass":                      "Meta+Down",
        # mac Ctrl+Left/Right = switch Spaces
        "Switch One Desktop to the Left":   "Meta+Left",
        "Switch One Desktop to the Right":  "Meta+Right",
        # tiling has no mac equivalent; move it off the arrows it just lost
        "Window Quick Tile Left":           "Meta+Alt+Left",
        "Window Quick Tile Right":          "Meta+Alt+Right",
        "Window Quick Tile Top":            "Meta+Alt+Up",
        "Window Quick Tile Bottom":         "Meta+Alt+Down",
        # mac Cmd+Opt+Esc = Force Quit
        "Kill Window":                      "Ctrl+Alt+Esc",
        # mac Cmd+Tab / Cmd+` ; Alt+Tab kept as a fallback
        "Walk Through Windows":                         "Ctrl+Tab\\tAlt+Tab",
        "Walk Through Windows (Reverse)":               "Ctrl+Shift+Tab\\tAlt+Shift+Tab",
        "Walk Through Windows of Current Application":  "Ctrl+`",
        "Walk Through Windows of Current Application (Reverse)": "Ctrl+~",
        # freed for terminal control codes
        "Show Desktop":                     "none",
        "Grid View":                        "none",
        "Edit Tiles":                       "none",
    },
    "ksmserver": {
        "Lock Session":                     "Ctrl+Meta+Q",   # mac Cmd+Ctrl+Q
    },
    "plasmashell": {
        # Meta alone opened the launcher -- after the swap that fires on every
        # tap of the physical Ctrl key. Drop the bare-Meta trigger.
        "activate application launcher":    "Alt+F1",
        "show-on-mouse-pos":                "Ctrl+Meta+V",
        "manage activities":                "none",
        "next activity":                    "none",
        "stop current activity":            "none",
    },
}
# sections/keys to create if absent
ADDITIONS = {
    "org.kde.krunner.desktop": {
        "_launch":   ("Ctrl+Space",   "KRunner"),           # mac Cmd+Space
    },
    "org.kde.spectacle.desktop": {
        "FullScreenScreenShot":        ("Ctrl+Shift+3", "Capture Entire Desktop"),
        "RectangularRegionScreenShot": ("Ctrl+Shift+4", "Capture Rectangular Region"),
        "_launch":                     ("Ctrl+Shift+5", "Launch Spectacle"),
    },
}

if not os.path.exists(PATH):
    sys.exit(f"not found: {PATH}")
bak = PATH + ".bak-claude-" + time.strftime("%Y-%m-%d-%H%M%S")
shutil.copy2(PATH, bak)

lines = open(PATH, encoding="utf-8").read().split("\n")
section, applied, out = None, [], []
for ln in lines:
    m = re.match(r"^\[(.+)\]$", ln.strip())
    if m:
        section = m.group(1)
    elif section in CHANGES and "=" in ln:
        key, val = ln.split("=", 1)
        if key in CHANGES[section]:
            parts = val.split(",")
            default = parts[1] if len(parts) > 1 else ""
            name    = parts[2] if len(parts) > 2 else ""
            new = f"{key}={CHANGES[section][key]},{default},{name}"
            applied.append(f"  [{section}] {key}: {parts[0]!r} -> {CHANGES[section][key]!r}")
            out.append(new)
            continue
    out.append(ln)

text = "\n".join(out)
for sec, keys in ADDITIONS.items():
    if f"[{sec}]" not in text:
        block = f"\n[{sec}]\n" + "".join(
            f"{k}={sc},none,{nm}\n" for k, (sc, nm) in keys.items())
        text = text.rstrip("\n") + "\n" + block
        applied.append(f"  [{sec}] created with {len(keys)} binding(s)")

open(PATH, "w", encoding="utf-8").write(text)
print(f"backup: {bak}\n")
print("\n".join(applied) if applied else "  (no matching keys found)")
print(f"\n{len(applied)} change groups applied.")

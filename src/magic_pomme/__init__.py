"""magic-pomme - control for Apple Magic Keyboard & Magic Mouse on Linux.

The library half is deliberately UI-free and unprivileged, so it can be driven
by a CLI, a Qt tray, or a Plasma applet without change. Privileged writes are
confined to :func:`magic_pomme.drivers.write`.
"""

__version__ = "0.1.0"

from . import bluetooth, devices, drivers, keyd

__all__ = ["bluetooth", "devices", "drivers", "keyd", "__version__"]

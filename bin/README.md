# HASHI Launcher Scripts

For installation and prerequisites, use the
[installation guide](../docs/INSTALL.md). npm-installed programs expose the
hashi command; these scripts are source-checkout and platform helpers.

## Source launchers

Run from the repository root, after setup:

~~~bash
./bin/bridge-u.sh
~~~

On Windows:

~~~powershell
.\bin\bridge-u.bat
~~~

For a direct source launch with the approved Python environment:

~~~bash
python main.py
~~~

macOS support and portable profiles have their own validation scope in the
[installation guide](../docs/INSTALL.md#macos).

## First connection

The current local connection page selects a CLI engine or a model provider
inside HER v2. Telegram is optional:

~~~bash
python -m onboarding.onboarding_main
~~~

For npm installations, use hashi onboard or the hashi-onboard compatibility
entry. Use hashi help to inspect the installed command surface.

## Instance inspection and lifecycle

For registered instances, use hashi status, hashi doctor, and hashi logs.
hashi stop performs a scoped graceful stop and refuses busy or unverifiable
instances. See the [user guide](../docs/USER_GUIDE.md).

Slash-command /reboot replaces Agent Function Workers. It is not the
whole-instance restart operation. Core/runtime migrations and shared Function
replacement have separate adoption boundaries in
[Architecture](../ARCHITECTURE.md).

## Remote supervision

These commands register and start the per-instance Remote supervisor.
Run them only when setting up that service.

~~~bash
./bin/hashi-remote-ctl.sh enable
./bin/hashi-remote-ctl.sh status
~~~

On Windows:

~~~powershell
.\bin\hashi_remote_ctl.ps1 enable
.\bin\hashi_remote_ctl.ps1 status
~~~

Remote derives supervisor identity from configured instance identity. See
[Remote setup](../docs/INSTALL.md#hashi-remote) for trust, platform support,
and opt-out settings. Legacy restart and kill scripts remain implementation
utilities; they are not the normal installation or troubleshooting path.

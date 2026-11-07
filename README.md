# Smart Ping Stabilizer 1.0

Automatic upload-bandwidth control + VALORANT traffic marking for Windows 11 x64. Free, no router changes.

## Install (two ways)
**A. Easiest (no tools needed):** put this folder in a free GitHub repo -> *Actions* tab -> *build-windows-installer* -> download
`SmartPingStabilizer-Setup.exe` from the run's artifacts.
**B. On your PC:** install Python 3.12 x64 (python.org, keep "tcl/tk" ticked) and Inno Setup 6, then in PowerShell in this folder: `./build.ps1`.
Output: `installer_out\SmartPingStabilizer-Setup.exe`. Run it (admin). It registers a logon task (elevated, no UAC prompt each boot),
and adds a Start-menu entry **Emergency Restore Networking**.

## Use
Nothing to configure. On first start it measures your line (about 10 s), sets the limits, and re-measures every ~5 min
(sooner when the line is unstable). Close the window -> it keeps running in the tray. START/STOP pauses management; **EMERGENCY STOP**
(or tray menu, or `SmartPingStabilizer.exe --restore`) removes everything the app changed. Log: `%LOCALAPPDATA%\SmartPingStabilizer\logs\sps.log`.

## What it really does
| Feature | Mechanism | Strength |
|---|---|---|
| Upload limit | Windows QoS policy (`New-NetQosPolicy`, ActiveStore, TCP-only throttle) set to a safe fraction of measured upload | Real. Keeps the 4G modem's upload queue short = less bufferbloat |
| VALORANT priority | DSCP 46 marking on VALORANT executables + game UDP is *not* throttled | Local only: mobile carriers usually strip DSCP; the benefit is that game packets are never queued behind our throttle |
| Download limit | Computed + displayed. Optional receive-window clamp (`netsh ... autotuninglevel=restricted`) | **Advisory/weak.** Windows has no ingress shaping. A true download cap needs a router that supports SQM/QoS |
| Bandwidth detection | Active test to speed.cloudflare.com (3 download streams / 2 upload streams, ramp excluded), throttle removed during the test | Real |
| Spike rejection | See `sps/algo.py`: low percentile of a rolling window x 0.90-0.92 safety; up-shift needs 3 consistent tests; drops capped immediately | Real |
| Latency feedback | ICMP ping vs idle baseline while loaded; shrinks limit up to 50 % if ping inflates | Real |

## Limitations (please read)
* Windows cannot control the 4G network. If the carrier's congestion causes the ping spikes, no app can fix that.
* The QoS TCP throttle also applies to LAN TCP uploads (e.g. copying files to a NAS gets capped).
* Needs Administrator (the installed autostart task provides it). Without it, the app only monitors.
* The speed test uses up to ~10 s of full bandwidth every ~5 min (never while VALORANT is running; limits are held instead).
* Policies are memory-only (ActiveStore): a reboot clears them even if the app crashes; the app re-applies them at next start.
* The "Experimental receive-window clamp" is OFF by default; it changes a global Windows TCP setting (saved and restored) and may slow downloads.
* The GUI is Tkinter (dark themed), not native WinUI.

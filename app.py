"""Smart Ping Stabilizer - entry point.
  SmartPingStabilizer.exe              open window
  SmartPingStabilizer.exe --background start hidden in the tray (used by the autostart task)
  SmartPingStabilizer.exe --restore    remove every SPS network change, then exit
"""
import logging, subprocess, sys
from sps.common import IS_WIN, setup_logging, is_admin

def single_instance():
    if not IS_WIN: return True
    import ctypes
    ctypes.windll.kernel32.CreateMutexW(None, False, "Global\\SmartPingStabilizerMutex")
    return ctypes.windll.kernel32.GetLastError() != 183      # ERROR_ALREADY_EXISTS

def main():
    setup_logging(); log = logging.getLogger("app")
    if "--restore" in sys.argv:
        from sps.qos import Qos
        ok = Qos().restore_all(); log.info("--restore -> %s", ok); sys.exit(0 if ok else 1)
    if not single_instance(): sys.exit(0)

    import tkinter as tk
    from tkinter import ttk, messagebox
    from sps.engine import Engine
    background = "--background" in sys.argv
    eng = Engine(); eng.start()

    BG, CARD, FG, DIM, ACC, RED, GRN, YEL = "#202020", "#2b2b2b", "#f3f3f3", "#9d9d9d", "#0078d4", "#c42b1c", "#6ccb5f", "#fce100"
    root = tk.Tk(); root.title("Smart Ping Stabilizer"); root.geometry("820x640"); root.configure(bg=BG); root.minsize(760, 600)
    try:
        import ctypes; hwnd = ctypes.windll.user32.GetParent(root.winfo_id())
        ctypes.windll.dwmapi.DwmSetWindowAttribute(hwnd, 20, ctypes.byref(ctypes.c_int(1)), 4)   # dark title bar
    except Exception: pass
    F = "Segoe UI"
    st = ttk.Style(); st.theme_use("clam")
    st.configure("TCheckbutton", background=BG, foreground=FG, font=(F, 11)); st.map("TCheckbutton", background=[("active", BG)])

    tk.Label(root, text="Smart Ping Stabilizer", bg=BG, fg=FG, font=(F, 22, "bold")).pack(anchor="w", padx=24, pady=(18, 0))
    status = tk.Label(root, text="Starting...", bg=BG, fg=DIM, font=(F, 11)); status.pack(anchor="w", padx=26)
    grid = tk.Frame(root, bg=BG); grid.pack(fill="x", padx=18, pady=12)
    vals = {}
    for i, (key, cap) in enumerate([("ping", "LIVE PING"), ("jitter", "JITTER"), ("loss", "PACKET LOSS"), ("stab", "STABILITY"),
                                     ("dl", "DOWNLOAD NOW"), ("ul", "UPLOAD NOW"), ("dll", "DOWNLOAD LIMIT (auto)"), ("ull", "UPLOAD LIMIT (auto)")]):
        c = tk.Frame(grid, bg=CARD, padx=14, pady=10); c.grid(row=i // 4, column=i % 4, sticky="nsew", padx=6, pady=6)
        grid.columnconfigure(i % 4, weight=1)
        tk.Label(c, text=cap, bg=CARD, fg=DIM, font=(F, 8, "bold")).pack(anchor="w")
        vals[key] = tk.Label(c, text="--", bg=CARD, fg=FG, font=(F, 20, "bold")); vals[key].pack(anchor="w")
    note = tk.Label(root, text="Upload limit is enforced by Windows QoS. Download limit is advisory: Windows cannot shape inbound traffic.",
                    bg=BG, fg=DIM, font=(F, 9)); note.pack(anchor="w", padx=26)

    ctl = tk.Frame(root, bg=BG); ctl.pack(fill="x", padx=24, pady=14)
    def btn(parent, text, bg, cmd):
        return tk.Button(parent, text=text, command=cmd, bg=bg, fg="white", activebackground=bg, relief="flat", bd=0,
                         font=(F, 12, "bold"), padx=22, pady=10, cursor="hand2")
    def toggle_run():
        eng.set_option("running", not eng.cfg["running"]); refresh()
    def emergency():
        ok = eng.emergency_stop(); refresh()
        messagebox.showinfo("Emergency stop", "All Smart Ping Stabilizer network changes were removed." if ok else
                            "Could not confirm restore. Run the app as Administrator or use the 'Emergency Restore' Start-menu shortcut.")
    startb = btn(ctl, "STOP", ACC, toggle_run); startb.pack(side="left")
    btn(ctl, "EMERGENCY STOP", RED, emergency).pack(side="left", padx=12)

    opts = tk.Frame(root, bg=BG); opts.pack(fill="x", padx=24)
    vars_ = {}
    def mkopt(key, text):
        v = tk.BooleanVar(value=eng.cfg[key]); vars_[key] = v
        ttk.Checkbutton(opts, text=text, variable=v, command=lambda: eng.set_option(key, v.get())).pack(anchor="w", pady=3)
    mkopt("auto_mode", "Automatic Mode (measure bandwidth and set limits by itself)")
    mkopt("valorant_mode", "VALORANT Gaming Mode (mark game traffic, no speed tests while VALORANT is open)")
    mkopt("dl_clamp", "Experimental: restrict TCP receive window (partial download-queue control)")
    def autostart_toggle():
        if auto.get():
            cmd = ["schtasks", "/Create", "/TN", "Smart Ping Stabilizer", "/TR", f'"{sys.executable}" --background', "/SC", "ONLOGON", "/RL", "HIGHEST", "/F"]
        else: cmd = ["schtasks", "/Delete", "/TN", "Smart Ping Stabilizer", "/F"]
        r = subprocess.run(cmd, capture_output=True, text=True, creationflags=0x08000000); logging.getLogger("app").info("autostart rc=%s", r.returncode)
    auto = tk.BooleanVar(value=True)
    ttk.Checkbutton(opts, text="Start with Windows (runs hidden in the tray)", variable=auto, command=autostart_toggle).pack(anchor="w", pady=3)
    tk.Label(root, text="Log: %LOCALAPPDATA%\\SmartPingStabilizer\\logs\\sps.log", bg=BG, fg=DIM, font=(F, 9)).pack(side="bottom", anchor="w", padx=24, pady=8)
    admin_lbl = tk.Label(root, text="" if is_admin() else "Not running as Administrator: monitoring only.", bg=BG, fg=YEL, font=(F, 10, "bold"))
    admin_lbl.pack(side="bottom", anchor="w", padx=24)

    def fmt(x, unit, d=0): return "--" if x is None else f"{x:.{d}f} {unit}"
    def refresh():
        s = eng.snapshot()
        startb.config(text="STOP" if eng.cfg["running"] else "START", bg=RED if False else (ACC if not eng.cfg["running"] else "#5c5c5c"))
        status.config(text=("Status: " + s["status"]) + ("   |   VALORANT detected" if s["game"] else ""))
        vals["ping"].config(text=fmt(s["ping"], "ms")); vals["jitter"].config(text=fmt(s["jitter"], "ms", 1))
        vals["loss"].config(text=f'{s["loss"]:.0f} %', fg=GRN if s["loss"] < 1 else (YEL if s["loss"] < 3 else "#ff6b6b"))
        sc = s["stability"]; vals["stab"].config(text=f"{sc}  " + ("Stable" if sc >= 80 else "Fair" if sc >= 55 else "Unstable"),
                                                 fg=GRN if sc >= 80 else YEL if sc >= 55 else "#ff6b6b")
        vals["dl"].config(text=fmt(s["cur_dl"], "Mbps", 1)); vals["ul"].config(text=fmt(s["cur_ul"], "Mbps", 1))
        vals["dll"].config(text=fmt(s["dl_limit"], "Mbps", 1)); vals["ull"].config(text=fmt(s["ul_limit"], "Mbps", 1))
    def tick(): refresh(); root.after(700, tick)

    tray = None
    def show(): root.after(0, lambda: (root.deiconify(), root.lift()))
    def quit_app():
        if tray:
            try: tray.stop()
            except Exception: pass
        eng.shutdown(); root.after(0, root.destroy)
    try:
        import pystray; from PIL import Image, ImageDraw
        img = Image.new("RGB", (64, 64), "#0078d4"); d = ImageDraw.Draw(img); d.polygon([(12, 40), (24, 40), (30, 16), (38, 52), (44, 32), (52, 32)], fill="white")
        tray = pystray.Icon("sps", img, "Smart Ping Stabilizer", pystray.Menu(
            pystray.MenuItem("Open", lambda i, m: show(), default=True),
            pystray.MenuItem("Emergency stop (restore networking)", lambda i, m: eng.emergency_stop()),
            pystray.MenuItem("Quit (restores networking)", lambda i, m: quit_app())))
        tray.run_detached()
    except Exception as e:
        logging.getLogger("app").warning("tray unavailable: %s", e)
    root.protocol("WM_DELETE_WINDOW", (lambda: root.withdraw()) if tray else root.iconify)
    if background: root.withdraw()
    tick(); root.mainloop()
    eng.shutdown()

if __name__ == "__main__":
    main()

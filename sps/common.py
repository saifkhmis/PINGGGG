import json, logging, os, sys
from logging.handlers import RotatingFileHandler

APP = "SmartPingStabilizer"
DATA_DIR = os.path.join(os.environ.get("LOCALAPPDATA", os.path.expanduser("~")), APP)
os.makedirs(DATA_DIR, exist_ok=True)
CFG_FILE = os.path.join(DATA_DIR, "config.json")
STATE_FILE = os.path.join(DATA_DIR, "state.json")   # remembers settings we must restore
IS_WIN = sys.platform == "win32"

DEFAULTS = {
    "running": True,          # START/STOP
    "auto_mode": True,        # Automatic Mode
    "valorant_mode": True,    # mark VALORANT + never run speed tests while it is open
    "dl_clamp": False,        # optional TCP receive-window clamp (see README)
    "test_interval_s": 300,
    "speedtest_base": "https://speed.cloudflare.com",
}

def load_json(path, default):
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return {**default, **data} if isinstance(default, dict) else data
    except Exception:
        return dict(default) if isinstance(default, dict) else default

def save_json(path, data):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)
    os.replace(tmp, path)

def setup_logging():
    d = os.path.join(DATA_DIR, "logs"); os.makedirs(d, exist_ok=True)
    h = RotatingFileHandler(os.path.join(d, "sps.log"), maxBytes=1_000_000, backupCount=3, encoding="utf-8")
    h.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    root = logging.getLogger(); root.setLevel(logging.INFO); root.addHandler(h)
    return root

def is_admin():
    if not IS_WIN:
        return hasattr(os, "geteuid") and os.geteuid() == 0
    import ctypes
    try: return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception: return False

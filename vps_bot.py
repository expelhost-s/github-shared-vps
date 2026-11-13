#!/usr/bin/env python3
"""
ExpelHost VPS Bot  -  made by ExpelHost
Docker VPS Bot  (no LXC / LXD needed)
-------------------------------------
* Every VPS is a Docker container (Ubuntu 22.04 / 24.04).
* RAM + CPU are FULLY SHARED with the host (no per-VPS limit unless ENFORCE_LIMITS=true).
* Storage is SEPARATE per VPS:
    - "opt"  : docker --storage-opt size=XG   (needs overlay2 on xfs with pquota)
    - "loop" : a private ext4 disk image mounted into /root and /home (works on any host)
    - STORAGE_MODE=auto tries "opt" first, then falls back to "loop".
* Needs: docker CLI + access to the docker socket (run as root / docker group,
  or mount /var/run/docker.sock if the bot itself runs inside Docker).
* Token is read from .env  ->  DISCORD_TOKEN=...
"""
import asyncio, os, re, secrets, sqlite3, time, uuid, logging
from datetime import datetime, timedelta, timezone

import discord
from discord.ext import commands, tasks
from dotenv import load_dotenv

load_dotenv()
TOKEN = os.getenv("DISCORD_TOKEN")
PREFIX = os.getenv("PREFIX", "2")
BRAND = os.getenv("BRAND", "ExpelHost")
CREDIT = "ExpelHost"
LOGO = os.getenv("BRAND_LOGO_URL", "")
BANNER = os.getenv("BRAND_BANNER_URL", "")
ADMIN_IDS = {int(x) for x in os.getenv("ADMIN_IDS", "").replace(" ", "").split(",") if x.isdigit()}
EXPIRY_DAYS = int(os.getenv("EXPIRY_DAYS", "30"))          # 0 = never expires
ENFORCE_LIMITS = os.getenv("ENFORCE_LIMITS", "false").lower() in ("1", "true", "yes")
STORAGE_MODE = os.getenv("STORAGE_MODE", "loop")           # loop (dedicated, resizable) | opt | auto
LIVE_INTERVAL = max(3, int(os.getenv("LIVE_INTERVAL", "5")))   # seconds between live panel refreshes
DEFAULT_LIMIT = int(os.getenv("DEFAULT_VPS_LIMIT", "3"))       # default max VPS per user (admins exempt)
DISK_DIR = os.getenv("DISK_DIR", "/var/lib/vpsbot")
DB_PATH = os.getenv("DB_PATH", "vpsbot.db")
OS_OPTIONS = [("Ubuntu 22.04", "ubuntu:22.04"), ("Ubuntu 24.04", "ubuntu:24.04")]
START_TIME = time.time()
log = logging.getLogger("vpsbot")
logging.basicConfig(level=logging.INFO)

# ───────────────────────────── database ─────────────────────────────
db = sqlite3.connect(DB_PATH, check_same_thread=False)
db.row_factory = sqlite3.Row
db.executescript("""
CREATE TABLE IF NOT EXISTS settings(k TEXT PRIMARY KEY, v TEXT);
CREATE TABLE IF NOT EXISTS user_limits(user_id INTEGER PRIMARY KEY, max_vps INTEGER);
CREATE TABLE IF NOT EXISTS admins(user_id INTEGER PRIMARY KEY, added_by INTEGER, added_at TEXT);
CREATE TABLE IF NOT EXISTS nodes(id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT, host TEXT DEFAULT '', max_vps INTEGER DEFAULT 50);
CREATE TABLE IF NOT EXISTS vps(id TEXT PRIMARY KEY, owner INTEGER, node_id INTEGER, image TEXT,
  ram INTEGER, cpu INTEGER, disk INTEGER, mode TEXT DEFAULT 'opt', created TEXT, expires TEXT,
  purge INTEGER DEFAULT 0, free INTEGER DEFAULT 0);
""")
try:  # remembers the DM control-panel message so its buttons keep working after a restart
    db.execute("ALTER TABLE vps ADD COLUMN dm_msg INTEGER")
except sqlite3.OperationalError:
    pass
if not db.execute("SELECT 1 FROM nodes").fetchone():
    db.execute("INSERT INTO nodes(name, host) VALUES('Local', '')")
db.commit()


def q(sql, *a):
    return db.execute(sql, a).fetchall()


def ex(sql, *a):
    db.execute(sql, a)
    db.commit()


def get_set(k, d=""):
    r = db.execute("SELECT v FROM settings WHERE k=?", (k,)).fetchone()
    return r["v"] if r else d


def put_set(k, v):
    ex("INSERT INTO settings(k,v) VALUES(?,?) ON CONFLICT(k) DO UPDATE SET v=excluded.v", k, str(v))


def get_node(nid):
    r = q("SELECT * FROM nodes WHERE id=?", nid)
    return r[0] if r else None


def get_vps(vid):
    r = q("SELECT * FROM vps WHERE id=?", vid)
    return r[0] if r else None


def find_vps(owner, ref):
    """ref = 8-char VPS ID or the user's VPS number (1,2,3...)"""
    rows = q("SELECT * FROM vps WHERE owner=? ORDER BY rowid", owner)
    for i, v in enumerate(rows, 1):
        if v["id"] == ref or str(i) == ref:
            return v
    return None


def vps_number(v):
    ids = [r["id"] for r in q("SELECT id FROM vps WHERE owner=? ORDER BY rowid", v["owner"])]
    return ids.index(v["id"]) + 1 if v["id"] in ids else 0


def now():
    return datetime.now(timezone.utc).replace(tzinfo=None)


def cname(v):
    return f"vps-{v['id']}"


def is_admin_id(uid):
    """Owner admins (ADMIN_IDS in .env) + admins added in chat with admin-add."""
    return uid in ADMIN_IDS or bool(q("SELECT 1 FROM admins WHERE user_id=?", uid))


def is_admin(user):
    return is_admin_id(user.id)


admin_only = commands.check(lambda ctx: is_admin(ctx.author))

# ───────────────────────────── docker helpers ─────────────────────────────
async def run(*args, timeout=120):
    p = await asyncio.create_subprocess_exec(*args, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    try:
        out, err = await asyncio.wait_for(p.communicate(), timeout)
    except asyncio.TimeoutError:
        p.kill()
        return 124, "", "timed out"
    return p.returncode, out.decode(errors="replace").strip(), err.decode(errors="replace").strip()


async def dock(node, *args, timeout=120):
    base = ["docker"] + (["-H", node["host"]] if node["host"] else [])
    return await run(*base, *args, timeout=timeout)


async def host_sh(node, script, timeout=300):
    """Run a shell script in the HOST namespaces (works even if the bot runs inside Docker)."""
    return await dock(node, "run", "--rm", "--privileged", "--pid=host", "ubuntu:22.04",
                      "nsenter", "-t", "1", "-m", "-u", "-n", "-i", "sh", "-c", script, timeout=timeout)


def disk_script(name, gb):
    d = f"{DISK_DIR}/{name}"
    return (f"set -e; mkdir -p {d}/mnt; "
            f"[ -f {d}/disk.img ] || {{ truncate -s {gb}G {d}/disk.img && mkfs.ext4 -q -F -m 0 {d}/disk.img; }}; "
            f"mountpoint -q {d}/mnt || mount -o loop {d}/disk.img {d}/mnt; "
            f"dev=$(losetup -j {d}/disk.img | head -n1 | cut -d: -f1); [ -n \"$dev\" ] && resize2fs $dev >/dev/null 2>&1 || true; "
            f"mkdir -p {d}/mnt/root {d}/mnt/home; chmod 700 {d}/mnt/root")


def grow_script(name, gb):
    d = f"{DISK_DIR}/{name}"
    return (f"set -e; truncate -s {gb}G {d}/disk.img; dev=$(losetup -j {d}/disk.img | head -n1 | cut -d: -f1); "
            f"if [ -n \"$dev\" ]; then losetup -c $dev; resize2fs $dev >/dev/null 2>&1 || true; fi")


SETUP_SH = ("export DEBIAN_FRONTEND=noninteractive; apt-get update -qq && "
            "apt-get install -y -qq curl ca-certificates sudo nano wget git tmux procps iputils-ping net-tools bsdutils "
            ">/dev/null && curl -sSf https://sshx.io/get | sh >/dev/null 2>&1; true")

SSHX_SH = r'''
export PATH="$PATH:/root/.local/bin:/usr/local/bin"
LOG=@LOG@
command -v sshx >/dev/null 2>&1 || curl -sSf https://sshx.io/get | sh >/dev/null 2>&1
command -v sshx >/dev/null 2>&1 || { echo "sshx install failed" >&2; exit 2; }
if command -v script >/dev/null 2>&1; then
  setsid nohup script -qfc "sshx -q" "$LOG" </dev/null >/dev/null 2>&1 &
else
  setsid nohup sshx -q </dev/null >"$LOG" 2>&1 &
fi
for i in $(seq 1 40); do
  L=$(grep -aoE 'https://sshx\.io/s/[A-Za-z0-9_#=-]+' "$LOG" 2>/dev/null | head -n1)
  if [ -n "$L" ]; then echo "$L"; exit 0; fi
  sleep 1
done
echo "timed out waiting for sshx" >&2; exit 1
'''


async def destroy(v):
    node, n = get_node(v["node_id"]), cname(v)
    for c in (INFO_CACHE, DISK_CACHE, HIST):
        c.pop(v["id"], None)
    await dock(node, "rm", "-f", n)
    if v["mode"] == "loop":
        d = f"{DISK_DIR}/{n}"
        await host_sh(node, f"umount -l {d}/mnt 2>/dev/null; rm -rf {d}; true")


async def provision(v, progress, fresh=True):
    """Create the container (+ separate storage) and install tools. Returns a warning string or ''."""
    node, n = get_node(v["node_id"]), cname(v)
    if not fresh:
        await progress("🧹 Wiping old system…")
        await destroy(v)
    await progress("📥 Pulling image & creating container…")
    base = ["run", "-d", "--init", "--name", n, "--hostname", n, "--label", "vpsbot=1"]
    if ENFORCE_LIMITS:
        base += ["--memory", f"{v['ram']}g", "--cpus", str(v["cpu"])]
    modes = ["opt", "loop"] if STORAGE_MODE == "auto" else [STORAGE_MODE]
    last, used = "", None
    for mode in modes:
        if mode == "loop":
            rc, _, e = await host_sh(node, disk_script(n, v["disk"]))
            if rc:
                last = e
                continue
            d = f"{DISK_DIR}/{n}/mnt"
            extra = ["--restart", "no", "-v", f"{d}/root:/root", "-v", f"{d}/home:/home"]
        else:
            extra = ["--restart", "unless-stopped", "--storage-opt", f"size={v['disk']}G"]
        rc, _, e = await dock(node, *base, *extra, v["image"], "tail", "-f", "/dev/null", timeout=900)
        if rc == 0:
            used = mode
            break
        last = e
        await dock(node, "rm", "-f", n)
    if not used:
        raise RuntimeError(last[-800:] or "container creation failed")
    ex("UPDATE vps SET mode=? WHERE id=?", used, v["id"])
    await progress("⚙️ Installing packages & SSHX…")
    rc, _, e = await dock(node, "exec", n, "bash", "-c", SETUP_SH, timeout=900)
    return "" if rc == 0 else f"Package setup had issues: {e[-300:]}"


async def lifecycle(v, action):
    node, n = get_node(v["node_id"]), cname(v)
    INFO_CACHE.pop(v["id"], None)
    if action in ("start", "restart") and v["mode"] == "loop":
        rc, _, e = await host_sh(node, disk_script(n, v["disk"]))
        if rc:
            return False, e
    args = {"start": ["start", n], "stop": ["stop", "-t", "5", n], "restart": ["restart", "-t", "5", n]}[action]
    rc, _, e = await dock(node, *args)
    return rc == 0, e


def to_mb(text):
    m = re.match(r"([\d.]+)\s*([A-Za-z]+)", text.strip())
    if not m:
        return 0.0
    f = {"b": 1 / 1048576, "k": 1 / 1024, "m": 1, "g": 1024, "t": 1048576}.get(m.group(2)[0].lower(), 1)
    return float(m.group(1)) * f


INFO_CACHE, DISK_CACHE, HIST = {}, {}, {}
_sem = None
PROBE = r"""df -Pk /root | tail -n1; ping -c1 -W1 1.1.1.1 2>/dev/null | sed -n 's/.*time=\([0-9.]*\).*/\1/p'"""


def sem():
    global _sem
    if _sem is None:
        _sem = asyncio.Semaphore(4)
    return _sem


async def info(v):
    c = INFO_CACHE.get(v["id"])
    if c and time.time() - c[0] < 2:
        return c[1]
    async with sem():
        d = await _info(v)
    INFO_CACHE[v["id"]] = (time.time(), d)
    return d


async def _info(v):
    node, n = get_node(v["node_id"]), cname(v)
    t0 = time.perf_counter()
    rc, o, _ = await dock(node, "inspect", "-f", "{{.State.Status}}|{{.State.StartedAt}}", n)
    node_ms = (time.perf_counter() - t0) * 1000
    if rc:
        return {"state": "missing", "node_ms": node_ms}
    state, started = o.split("|", 1)
    d = {"state": state, "started": started, "cpu": 0.0, "ram": 0.0, "disk": None, "net": None, "ping": None, "node_ms": node_ms}
    if state != "running":
        return d
    (src_, so, _), (prc, po, _) = await asyncio.gather(
        dock(node, "stats", "--no-stream", "--format", "{{.CPUPerc}}|{{.MemUsage}}|{{.NetIO}}", n),
        dock(node, "exec", n, "sh", "-c", PROBE, timeout=10))
    if src_ == 0 and so.count("|") >= 2:
        c, m, nio = so.split("|", 2)
        try:
            d["cpu"] = float(c.strip().rstrip("%"))
        except ValueError:
            pass
        d["ram"] = to_mb(m.split("/")[0])
        if "/" in nio:
            rx, tx = [x.strip() for x in nio.split("/", 1)]
            d["net"] = (rx, tx)
    for line in (po.splitlines() if prc == 0 else []):
        parts = line.split()
        if len(parts) >= 5 and v["mode"] == "loop":
            try:
                d["disk"] = int(parts[2]) / 1048576
            except ValueError:
                pass
        elif re.fullmatch(r"[\d.]+", line.strip()):
            d["ping"] = float(line.strip())
    if v["mode"] != "loop":
        ts, val = DISK_CACHE.get(v["id"], (0, None))
        if time.time() - ts > 30:
            rc, o, _ = await dock(node, "inspect", "-s", "-f", "{{.SizeRw}}", n)
            val = int(o) / 1073741824 if rc == 0 and o.isdigit() else val
            DISK_CACHE[v["id"]] = (time.time(), val)
        d["disk"] = val
    return d


async def sshx_link(v):
    node, n = get_node(v["node_id"]), cname(v)
    script = SSHX_SH.replace("@LOG@", f"/tmp/sshx_{secrets.token_hex(4)}.log")
    rc, o, e = await dock(node, "exec", n, "bash", "-c", script, timeout=120)
    if rc != 0 or "sshx.io/s/" not in o:
        raise RuntimeError(e or o or "no link returned")
    return o.splitlines()[-1].strip()

# ───────────────────────────── formatting ─────────────────────────────
SPIN = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"
SPARK = "▁▂▃▄▅▆▇█"


def bar(p, n=12, frame=None):
    f = round(min(max(p, 0), 100) / 100 * n)
    cells = ["█"] * f + ["░"] * (n - f)
    if frame is not None and f > 1:          # moving shimmer = animated bar
        cells[frame % f] = "▓"
    return "[" + "".join(cells) + "]"


def spark(vals):
    return "".join(SPARK[min(7, int(x / 100 * 7.99))] for x in vals) or "▁"


def ms(x, dec=0):
    return "—" if x is None else f"{x:.{dec}f} ms"


def dur(sec):
    sec = int(sec)
    d, r = divmod(sec, 86400)
    h, r = divmod(r, 3600)
    m, s = divmod(r, 60)
    return f"{d}d {h}h {m}m {s}s" if d else f"{h}h {m}m {s}s" if h else f"{m}m {s}s"


def expired(v):
    return bool(v["expires"]) and datetime.fromisoformat(v["expires"]) < now() and not v["purge"]


def fmt_exp(v):
    if not v["expires"]:
        return "Never"
    delta = datetime.fromisoformat(v["expires"]) - now()
    if delta.total_seconds() < 0:
        return "Expired"
    return f"in {delta.days}d {delta.seconds // 3600}h" if delta.days else f"in {delta.seconds // 3600}h {delta.seconds % 3600 // 60}m"


def footer_text():
    return "Powered by ExpelHost" if BRAND == CREDIT else f"{BRAND} • Made by {CREDIT}"


async def build_panel(v, frame=None):
    i = await info(v)
    st = i["state"]
    live = frame is not None
    f = frame or 0
    color = {"running": (0x2ECC71, 0x1FA85A)[f % 2], "missing": 0x95A5A6}.get(st, 0xE74C3C)
    e = discord.Embed(title=f"{SPIN[f % len(SPIN)] if live else '⚙️'} {BRAND} VPS Panel (Secure)", color=color)
    status = {"running": "🟢 Running", "missing": "⚠️ Container missing"}.get(st, "🔴 Stopped")
    up = "—"
    if st == "running":
        try:
            t = datetime.strptime(i["started"][:19], "%Y-%m-%dT%H:%M:%S")
            up = f"{dur((now() - t).total_seconds())} ago"
        except ValueError:
            pass
    desc = ""
    if live:
        desc += f"{'🔴' if f % 2 == 0 else '⚫'} **LIVE** • updates every {LIVE_INTERVAL}s\n\n"
    desc += (f"**VPS No:** `{vps_number(v)}` | **ID:** `{v['id']}`\n"
             f"**OS:** `{dict((b, a) for a, b in OS_OPTIONS).get(v['image'], v['image'])}`\n\n"
             f"**Status:** {status}\n**Uptime:** `{up}`\n**Expires:** `{fmt_exp(v)}`\n\n")
    if st == "running":
        cpu = min(100.0, i["cpu"] / max(v["cpu"], 1))
        h = HIST.setdefault(v["id"], __import__("collections").deque(maxlen=16))
        h.append(cpu)
        lvl = "Normal" if cpu < 60 else "High" if cpu < 85 else "Critical"
        ram_t = v["ram"] * 1024
        rp = i["ram"] / ram_t * 100 if ram_t else 0
        du = i["disk"]
        dp = (du / v["disk"] * 100) if (du is not None and v["disk"]) else 0
        kind = "dedicated" if v["mode"] == "loop" else "quota"
        disk_line = (f"💽 **Disk ({kind}):** {du:.1f} GB / {v['disk']} GB\n{bar(dp, 12, frame)} {dp:.1f}%\n" if du is not None
                     else f"💽 **Disk ({kind}):** — / {v['disk']} GB\n")
        net = f"🌐 **Network:** ↓ `{i['net'][0]}` • ↑ `{i['net'][1]}`\n" if i.get("net") else ""
        desc += (f"📊 **Live Stats:**\n🖥️ **CPU:** {lvl} {bar(cpu, 12, frame)} {cpu:.2f}%\n"
                 f"`{spark(h)}` CPU history\n"
                 f"🧠 **RAM:** {i['ram']:.2f} MB / {ram_t} MB\n{bar(rp, 12, frame)} {rp:.1f}%\n" + disk_line + net +
                 f"📡 **Ping:** Bot `{ms(bot.latency * 1000)}` • Node `{ms(i['node_ms'])}` • VPS→Internet `{ms(i['ping'], 1)}`\n")
    else:
        desc += (f"📊 **Spec:** {v['ram']} GB RAM (shared) • {v['cpu']} CPU (shared) • {v['disk']} GB disk\n"
                 f"📡 **Node ping:** `{ms(i.get('node_ms'))}`\n")
    desc += f"🔒 **Purge Protection:** {'🔐 Enabled' if v['purge'] else '🔓 Disabled'}"
    e.description = desc
    if LOGO:
        e.set_thumbnail(url=LOGO)
    if BANNER:
        e.set_image(url=BANNER)
    e.set_footer(text=footer_text())
    e.timestamp = discord.utils.utcnow()
    return e


# ───────────────────────── fleet stats + dashboard ─────────────────────────
FLEET = {"total": 0, "cap": 0, "running": 0, "nodes": []}
TICK = 0


async def _node_probe(nd):
    t0 = time.perf_counter()
    rc, o, _ = await dock(nd, "ps", "--filter", "label=vpsbot=1", "--format", "{{.Names}}", timeout=15)
    return {"name": nd["name"], "id": nd["id"], "max": nd["max_vps"], "ok": rc == 0,
            "ms": (time.perf_counter() - t0) * 1000, "running": len(o.split()) if rc == 0 else 0,
            "used": q("SELECT COUNT(*) c FROM vps WHERE node_id=?", nd["id"])[0]["c"]}


async def refresh_fleet():
    nodes = await asyncio.gather(*[_node_probe(n) for n in q("SELECT * FROM nodes")])
    FLEET.update(total=q("SELECT COUNT(*) c FROM vps")[0]["c"], cap=sum(n["max_vps"] for n in q("SELECT * FROM nodes")),
                 running=sum(n["running"] for n in nodes), nodes=list(nodes))


def host_info():
    try:
        host = dur(float(open("/proc/uptime").read().split()[0]))
    except OSError:
        host = "n/a"
    try:
        load = " / ".join(f"{x:.2f}" for x in os.getloadavg())
    except OSError:
        load = "n/a"
    return host, load


def build_dashboard():
    f = TICK
    host, load = host_info()
    t, c, r = FLEET["total"], FLEET["cap"], FLEET["running"]
    e = discord.Embed(title=f"{SPIN[f % len(SPIN)]} {BRAND} Live Dashboard", color=(0x5865F2, 0x7289DA)[f % 2])
    e.description = (f"{'🔴' if f % 2 == 0 else '⚫'} **LIVE** • updates every 10s\n\n"
                     f"🖥️ **VPS:** `{t}/{c}` {bar(t / c * 100 if c else 0, 14, f)}\n"
                     f"🟢 **Running:** `{r}`  🔴 **Stopped:** `{max(t - r, 0)}`\n"
                     f"👥 **Users:** `{q('SELECT COUNT(DISTINCT owner) c FROM vps')[0]['c']}`\n\n"
                     f"📡 **Bot ping:** `{ms(bot.latency * 1000)}`\n"
                     f"⏱️ **Bot uptime:** `{dur(time.time() - START_TIME)}`\n"
                     f"🖧 **Host uptime:** `{host}`\n⚙️ **Load (1/5/15m):** `{load}`")
    e.add_field(name="🖧 Nodes", value="\n".join(
        f"{'🟢' if n['ok'] else '🔴'} **{n['name']}** • `{ms(n['ms'])}` • {n['used']}/{n['max']} VPS • {n['running']} running"
        for n in FLEET["nodes"]) or "—", inline=False)
    e.set_footer(text=footer_text())
    e.timestamp = discord.utils.utcnow()
    return e


def simple(title, text, color=0x5865F2):
    return discord.Embed(title=title, description=text, color=color)

# ───────────────────────────── UI helpers ─────────────────────────────
class Picker(discord.ui.View):
    """One-shot dropdown. `await pv.wait_value()` returns the chosen value or None."""
    def __init__(self, user_id, placeholder, options):
        super().__init__(timeout=120)
        self.user_id, self.value, self.ev = user_id, None, asyncio.Event()
        self.sel = discord.ui.Select(placeholder=placeholder, options=options[:25])
        self.sel.callback = self.cb
        self.add_item(self.sel)

    async def interaction_check(self, i):
        if i.user.id == self.user_id:
            return True
        await i.response.send_message("❌ This menu isn't for you.", ephemeral=True)
        return False

    async def cb(self, i):
        self.value = self.sel.values[0]
        await i.response.edit_message(content=f"✅ Selected: **{next(o.label for o in self.sel.options if o.value == self.value)}**", view=None)
        self.ev.set()
        self.stop()

    async def wait_value(self):
        try:
            await asyncio.wait_for(self.ev.wait(), 120)
        except asyncio.TimeoutError:
            return None
        return self.value


def os_options():
    return [discord.SelectOption(label=a, value=b, emoji="🐧") for a, b in OS_OPTIONS]


def confirm_options(yes):
    return [discord.SelectOption(label=yes, value="yes", emoji="⚠️"), discord.SelectOption(label="Cancel", value="no", emoji="❌")]


async def pick_node(ctx):
    opts = []
    for n in q("SELECT * FROM nodes"):
        used = q("SELECT COUNT(*) c FROM vps WHERE node_id=?", n["id"])[0]["c"]
        if used < n["max_vps"]:
            opts.append(discord.SelectOption(label=f"{n['name']} ({used}/{n['max_vps']})", value=str(n["id"]), emoji="🖧"))
    if not opts:
        await ctx.send(embed=simple("❌ No capacity", "All nodes are full.", 0xE74C3C))
        return None
    if len(opts) == 1:
        return int(opts[0].value)
    pv = Picker(ctx.author.id, "Select node", opts)
    m = await ctx.send("🖧 **Which node?**", view=pv)
    val = await pv.wait_value()
    if val is None:
        await m.delete()
    return int(val) if val else None


async def pick_os(ctx):
    pv = Picker(ctx.author.id, "Select operating system", os_options())
    m = await ctx.send("🐧 **Which OS?**", view=pv)
    val = await pv.wait_value()
    if val is None:
        await m.delete()
    return val


def user_limit(uid):
    r = q("SELECT max_vps FROM user_limits WHERE user_id=?", uid)
    return r[0]["max_vps"] if r else int(get_set("default_limit", str(DEFAULT_LIMIT)))


def user_count(uid):
    return q("SELECT COUNT(*) c FROM vps WHERE owner=?", uid)[0]["c"]


async def apply_edit(v, ram, cpu, disk, add_days=0):
    """Admin edit. Returns a list of human-readable notes."""
    notes, node, n = [], get_node(v["node_id"]), cname(v)
    if disk < v["disk"]:
        notes.append(f"⚠️ Disk shrink is not supported (protects user data) – kept {v['disk']} GB.")
        disk = v["disk"]
    elif disk > v["disk"]:
        if v["mode"] == "loop":
            rc, _, e = await host_sh(node, grow_script(n, disk))
            if rc:
                notes.append(f"⚠️ Disk grow failed: `{e[-200:]}`")
                disk = v["disk"]
            else:
                notes.append(f"💽 Disk grown to **{disk} GB** (live, no data loss).")
        else:
            notes.append("⚠️ `opt` storage can't be resized live – kept. Use STORAGE_MODE=loop for resizable disks.")
            disk = v["disk"]
    ex("UPDATE vps SET ram=?, cpu=?, disk=? WHERE id=?", ram, cpu, disk, v["id"])
    if ENFORCE_LIMITS and (ram != v["ram"] or cpu != v["cpu"]):
        rc, _, e = await dock(node, "update", "--memory", f"{ram}g", "--memory-swap", f"{ram}g", "--cpus", str(cpu), n)
        notes.append("🧠 RAM/CPU limits applied live." if rc == 0 else f"⚠️ docker update failed: `{e[-200:]}`")
    elif ram != v["ram"] or cpu != v["cpu"]:
        notes.append("🧠 RAM/CPU updated (shared mode – shown on panel).")
    if add_days:
        base = max(now(), datetime.fromisoformat(v["expires"])) if v["expires"] else now()
        ex("UPDATE vps SET expires=? WHERE id=?", (base + timedelta(days=add_days)).isoformat(), v["id"])
        notes.append(f"📅 Extended by **{add_days} days**.")
    INFO_CACHE.pop(v["id"], None)
    return notes


async def deploy_flow(ctx, owner, ram, cpu, disk, free=False, days=None):
    if not is_admin(owner):
        lim, cnt = user_limit(owner.id), user_count(owner.id)
        if cnt >= lim:
            return await ctx.send(embed=simple("🚫 VPS limit reached", f"{owner.mention} has **{cnt}/{lim}** VPS.\n"
                                               f"Admins can raise it with `{PREFIX}userlimit @user <n>`.", 0xE74C3C))
    node_id = await pick_node(ctx)
    if not node_id:
        return
    image = await pick_os(ctx)
    if not image:
        return
    vid = uuid.uuid4().hex[:8]
    days = EXPIRY_DAYS if days is None else days
    exp = (now() + timedelta(days=days)).isoformat() if days else None
    ex("INSERT INTO vps(id,owner,node_id,image,ram,cpu,disk,created,expires,free) VALUES(?,?,?,?,?,?,?,?,?,?)",
       vid, owner.id, node_id, image, ram, cpu, disk, now().isoformat(), exp, 1 if free else 0)
    v = get_vps(vid)
    msg = await ctx.send(embed=simple("⏳ Creating VPS", "Starting…"))

    async def progress(t):
        await msg.edit(embed=simple("⏳ Creating VPS", t))
    try:
        warn = await provision(v, progress)
    except Exception as e:
        log.exception("provision failed")
        await destroy(get_vps(vid) or v)
        ex("DELETE FROM vps WHERE id=?", vid)
        return await msg.edit(embed=simple("❌ Creation failed", f"```{str(e)[:900]}```", 0xE74C3C))
    v = get_vps(vid)
    # ---- full details + control panel go to the OWNER'S DM ----
    det = discord.Embed(title="🌟 VPS Created Successfully", color=0x2ECC71)
    det.add_field(name="➤ Owner", value=owner.mention, inline=True)
    det.add_field(name="➤ VPS No / ID", value=f"#{vps_number(v)} • `{vid}`", inline=True)
    det.add_field(name="➤ Container", value=f"`{cname(v)}`", inline=True)
    det.add_field(name="➤ Node", value=get_node(v["node_id"])["name"], inline=False)
    det.add_field(name="➤ Resources", value=f"**RAM:** {ram} GB (shared)\n**CPU:** {cpu} Cores (shared)\n"
                                           f"**Storage:** {disk} GB ({v['mode']})", inline=False)
    det.add_field(name="➤ OS", value=dict((b, a) for a, b in OS_OPTIONS).get(image, image), inline=True)
    det.add_field(name="➤ Expires", value=fmt_exp(v), inline=True)
    det.add_field(name="➤ Control", value=f"Use the buttons below, or `{PREFIX}manage` in any channel the bot "
                                         f"can see or right here in DMs. Press **SSHX** to get your terminal link (sent here).",
                  inline=False)
    if warn:
        det.add_field(name="⚠️ Note", value=warn[:500], inline=False)
    if LOGO:
        det.set_thumbnail(url=LOGO)
    det.set_footer(text=footer_text())
    det.timestamp = discord.utils.utcnow()
    sent = False
    try:
        await owner.send(embed=det)
        panel = Panel(vid, owner.id, admin_view=is_admin(owner), persistent=True)
        pm = await owner.send(embed=await build_panel(v, 0), view=panel)
        ex("UPDATE vps SET dm_msg=? WHERE id=?", pm.id, vid)
        panel.start_live(pm)
        sent = True
    except discord.Forbidden:
        sent = False
    if sent:
        await msg.edit(content=owner.mention, embed=simple(
            "✅ VPS Created", f"{owner.mention} your VPS `{vid}` is ready!\n📩 Details and your control panel were sent to your **DMs**.", 0x2ECC71))
    else:
        await msg.edit(content=owner.mention, embed=simple(
            "✅ VPS Created", f"{owner.mention} your VPS `{vid}` is ready, but I couldn't DM you.\n"
                             f"Enable **Direct Messages** from server members, then use `{PREFIX}manage` to open your panel.", 0xF1C40F))

# ───────────────────────────── manage panel ─────────────────────────────
locks = {}


class EditModal(discord.ui.Modal):
    def __init__(self, v):
        super().__init__(title=f"Edit VPS {v['id']}")
        self.vid = v["id"]
        self.ram = discord.ui.TextInput(label="RAM (GB)", default=str(v["ram"]), max_length=5)
        self.cpu = discord.ui.TextInput(label="CPU cores", default=str(v["cpu"]), max_length=3)
        self.disk = discord.ui.TextInput(label="Dedicated disk (GB, grow only)", default=str(v["disk"]), max_length=6)
        self.days = discord.ui.TextInput(label="Extend expiry by days (0 = none)", default="0", max_length=4)
        for c in (self.ram, self.cpu, self.disk, self.days):
            self.add_item(c)

    async def on_submit(self, i):
        if not is_admin(i.user):
            return await i.response.send_message("🚫 Admins only.", ephemeral=True)
        try:
            ram, cpu, disk, days = (int(x.value) for x in (self.ram, self.cpu, self.disk, self.days))
            assert min(ram, cpu, disk) >= 1 and days >= 0
        except (ValueError, AssertionError):
            return await i.response.send_message("⚠️ Use whole numbers (RAM/CPU/disk ≥ 1).", ephemeral=True)
        v = get_vps(self.vid)
        if not v:
            return await i.response.send_message("❌ VPS no longer exists.", ephemeral=True)
        await i.response.defer(ephemeral=True, thinking=True)
        notes = await apply_edit(v, ram, cpu, disk, days)
        await i.followup.send(embed=simple("✅ VPS updated", "\n".join(notes) or "Saved.", 0x2ECC71), ephemeral=True)


class Panel(discord.ui.View):
    def __init__(self, vid, owner_id, admin_view=False, persistent=False):
        # persistent = DM panel: never times out, survives bot restarts
        super().__init__(timeout=None if persistent else 900)
        self.vid, self.owner_id, self.message, self.live, self.frame = vid, owner_id, None, None, 0
        self.persistent = persistent
        if not admin_view:
            self.remove_item(self.b_renew)
            self.remove_item(self.b_edit)

    def start_live(self, message):
        self.message = message
        if self.live and not self.live.done():
            return
        self.live = asyncio.create_task(self._live())

    async def _live(self):
        deadline = time.time() + 900          # live refresh for 15 min, then press Refresh to resume
        try:
            while not self.is_finished() and time.time() < deadline:
                await asyncio.sleep(LIVE_INTERVAL)
                v = get_vps(self.vid)
                if not v:
                    return
                lk = locks.get(v["id"])
                if lk and lk.locked():
                    continue
                self.frame += 1
                try:
                    await self.message.edit(embed=await build_panel(v, self.frame))
                except discord.NotFound:
                    return
                except Exception:
                    log.debug("live edit failed", exc_info=True)
        except asyncio.CancelledError:
            pass

    async def on_timeout(self):
        if self.live:
            self.live.cancel()
        for c in self.children:
            c.disabled = True
        try:
            await self.message.edit(view=self)
        except Exception:
            pass

    async def interaction_check(self, i):
        # ONLY the owner of the VPS can press anything on this panel
        if i.user.id == self.owner_id:
            return True
        await i.response.send_message("❌ This isn't your VPS. Only the owner can control it.", ephemeral=True)
        return False

    async def redraw(self, i):
        v = get_vps(self.vid)
        if v:
            await i.message.edit(embed=await build_panel(v, self.frame), view=self)

    async def act(self, i, action):
        v = get_vps(self.vid)
        if not v:
            return await i.response.send_message("❌ This VPS no longer exists.", ephemeral=True)
        lock = locks.setdefault(v["id"], asyncio.Lock())
        if lock.locked():
            return await i.response.send_message("⏳ Another action is running on this VPS.", ephemeral=True)
        async with lock:
            try:
                await self._do(i, v, action)
            except Exception as e:
                log.exception("panel action failed")
                try:
                    await i.followup.send(f"❌ `{action}` failed: `{str(e)[:300]}`", ephemeral=True)
                except discord.HTTPException:
                    pass

    async def _do(self, i, v, action):
        if action in ("start", "stop", "restart", "refresh"):
            await i.response.defer()
            if action in ("start", "restart") and expired(v):
                return await i.followup.send("⛔ This VPS has expired. Ask an admin to renew it.", ephemeral=True)
            if action != "refresh":
                ok, err = await lifecycle(v, action)
                if not ok:
                    await i.followup.send(f"❌ Could not {action}: `{err[:400]}`", ephemeral=True)
            elif self.persistent and i.message:
                self.start_live(i.message)       # resume live updates after pressing Refresh
            await self.redraw(i)
        elif action == "sshx":
            await i.response.defer(ephemeral=True, thinking=True)
            if (await info(v))["state"] != "running":
                return await i.followup.send("❌ VPS is not running. Press **Start** first.", ephemeral=True)
            link = await sshx_link(v)
            try:   # the link is private: it goes to the owner's DM, never the channel
                await i.user.send(embed=simple(f"🌐 SSHX Session • VPS {v['id']}",
                                               f"{link}\n\nPress **SSHX** again for a brand-new session.", 0x7289DA))
            except discord.Forbidden:
                return await i.followup.send("❌ I can't DM you. Enable **Direct Messages** from server members and press **SSHX** again.", ephemeral=True)
            await i.followup.send("📩 Your SSHX link was sent to your **DMs**.", ephemeral=True)
        elif action == "reinstall":
            pv = Picker(i.user.id, "Select new OS", os_options())
            await i.response.send_message("🐧 **Reinstall — choose the new OS** (all data will be wiped):", view=pv, ephemeral=True)
            image = await pv.wait_value()
            if not image:
                return
            cv = Picker(i.user.id, "Confirm", confirm_options("Yes, wipe & reinstall"))
            await i.followup.send("⚠️ This erases everything on the VPS. Continue?", view=cv, ephemeral=True)
            if await cv.wait_value() != "yes":
                return
            ex("UPDATE vps SET image=? WHERE id=?", image, v["id"])
            v = get_vps(v["id"])
            m = await i.followup.send("⏳ Reinstalling…", ephemeral=True, wait=True)

            async def progress(t):
                await m.edit(content=f"⏳ {t}")
            try:
                warn = await provision(v, progress, fresh=False)
                await m.edit(content="✅ Reinstall complete." + (f"\n⚠️ {warn}" if warn else ""))
            except Exception as e:
                await m.edit(content=f"❌ Reinstall failed: `{str(e)[:400]}`")
            await self.redraw(i)
        elif action == "delete":
            cv = Picker(i.user.id, "Confirm", confirm_options("Yes, delete this VPS"))
            await i.response.send_message("🗑️ Delete this VPS permanently?", view=cv, ephemeral=True)
            if await cv.wait_value() != "yes":
                return
            await destroy(v)
            ex("DELETE FROM vps WHERE id=?", v["id"])
            await i.message.edit(content="🗑️ This VPS was deleted.", embed=None, view=None)
            if self.live:
                self.live.cancel()
            self.stop()

    @discord.ui.button(custom_id="vp_start", label="Start", emoji="▶️", style=discord.ButtonStyle.success, row=0)
    async def b_start(self, i, b):
        await self.act(i, "start")

    @discord.ui.button(custom_id="vp_stop", label="Stop", emoji="⏹️", style=discord.ButtonStyle.danger, row=0)
    async def b_stop(self, i, b):
        await self.act(i, "stop")

    @discord.ui.button(custom_id="vp_restart", label="Restart", emoji="🔁", style=discord.ButtonStyle.primary, row=0)
    async def b_restart(self, i, b):
        await self.act(i, "restart")

    @discord.ui.button(custom_id="vp_sshx", label="SSHX", emoji="❎", style=discord.ButtonStyle.secondary, row=0)
    async def b_sshx(self, i, b):
        await self.act(i, "sshx")

    @discord.ui.button(custom_id="vp_reinstall", label="Reinstall", emoji="🔴", style=discord.ButtonStyle.secondary, row=1)
    async def b_reinstall(self, i, b):
        await self.act(i, "reinstall")

    @discord.ui.button(custom_id="vp_refresh", label="Refresh", emoji="🔄", style=discord.ButtonStyle.secondary, row=1)
    async def b_refresh(self, i, b):
        await self.act(i, "refresh")

    @discord.ui.button(custom_id="vp_delete", label="Delete", emoji="🗑️", style=discord.ButtonStyle.danger, row=1)
    async def b_delete(self, i, b):
        await self.act(i, "delete")

    @discord.ui.button(custom_id="vp_renew", label="Renew +30d", emoji="📅", style=discord.ButtonStyle.success, row=2)
    async def b_renew(self, i, b):
        if not is_admin(i.user):
            return await i.response.send_message("🚫 Admins only.", ephemeral=True)
        v = get_vps(self.vid)
        if not v:
            return await i.response.send_message("❌ VPS no longer exists.", ephemeral=True)
        await apply_edit(v, v["ram"], v["cpu"], v["disk"], 30)
        await i.response.send_message("✅ Renewed for 30 days. (Press **Start** if it was stopped.)", ephemeral=True)

    @discord.ui.button(custom_id="vp_edit", label="Edit (Admin)", emoji="✏️", style=discord.ButtonStyle.primary, row=2)
    async def b_edit(self, i, b):
        if not is_admin(i.user):
            return await i.response.send_message("🚫 Admins only.", ephemeral=True)
        v = get_vps(self.vid)
        if not v:
            return await i.response.send_message("❌ VPS no longer exists.", ephemeral=True)
        await i.response.send_modal(EditModal(v))

# ───────────────────────────── bot + commands ─────────────────────────────
intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix=PREFIX, intents=intents, help_command=None)


VIEWS_LOADED = False


@bot.event
async def on_ready():
    rc, o, e = await run("docker", "version", "--format", "{{.Server.Version}}")
    log.info("Logged in as %s | docker: %s", bot.user, o if rc == 0 else f"ERROR {e}")
    if not ADMIN_IDS:
        log.warning("ADMIN_IDS is empty - nobody can use admin commands!")
    global VIEWS_LOADED
    if not VIEWS_LOADED:   # re-attach DM control panels so their buttons keep working after a restart
        VIEWS_LOADED = True
        for r in q("SELECT id, owner, dm_msg FROM vps WHERE dm_msg IS NOT NULL"):
            bot.add_view(Panel(r["id"], r["owner"], admin_view=is_admin_id(r["owner"]), persistent=True), message_id=r["dm_msg"])
    await refresh_fleet()
    for t in (expiry_task, fleet_task, presence_task, dash_task):
        if not t.is_running():
            t.start()


@bot.event
async def on_command_error(ctx, err):
    err = getattr(err, "original", err)
    if isinstance(err, commands.CommandNotFound):
        return
    if isinstance(err, commands.CheckFailure):
        return await ctx.send("🚫 Admins only.")
    if isinstance(err, (commands.MissingRequiredArgument, commands.BadArgument)):
        return await ctx.send(f"⚠️ Usage: `{PREFIX}{ctx.command.qualified_name} {ctx.command.signature}`")
    log.exception("command error", exc_info=err)
    await ctx.send(f"❌ Error: `{str(err)[:300]}`")


@tasks.loop(seconds=10)
async def fleet_task():
    global TICK
    TICK += 1
    try:
        await refresh_fleet()
    except Exception:
        log.exception("fleet refresh failed")


PRES = 0


@tasks.loop(seconds=20)
async def presence_task():
    global PRES
    PRES += 1
    f = FLEET
    texts = [f"{f['total']}/{f['cap']} VPS | {f['running']} Running", f"{PREFIX}help • {BRAND}", f"Made by {CREDIT}"]
    await bot.change_presence(status=discord.Status.online,
                              activity=discord.Activity(type=discord.ActivityType.watching, name=texts[PRES % len(texts)]))


@tasks.loop(seconds=10)
async def dash_task():
    items = [x for x in get_set("dashboards", "").split(",") if ":" in x]
    keep = []
    for it in items:
        cid, mid = it.split(":")
        try:
            ch = bot.get_channel(int(cid)) or await bot.fetch_channel(int(cid))
            await (await ch.fetch_message(int(mid))).edit(embed=build_dashboard())
            keep.append(it)
        except (discord.NotFound, discord.Forbidden):
            pass
        except Exception:
            keep.append(it)
    if len(keep) != len(items):
        put_set("dashboards", ",".join(keep))


@tasks.loop(minutes=10)
async def expiry_task():
    for v in q("SELECT * FROM vps WHERE expires IS NOT NULL AND expires < ? AND purge=0", now().isoformat()):
        await dock(get_node(v["node_id"]), "stop", "-t", "5", cname(v))


@bot.command(name="help")
async def help_cmd(ctx):
    p = PREFIX
    e = simple(f"{BRAND} VPS Bot", f"Made by **{CREDIT}**", 0x5865F2)
    e.add_field(name="👤 Users", value=(f"`{p}manage` – your own LIVE panel (works in servers and DMs; SSHX link arrives in DM)\n"
                                         f"`{p}list` – your VPS list\n`{p}deploy` – free VPS (when enabled)\n"
                                         f"`{p}ping` • `{p}uptime`"), inline=False)
    e.add_field(name="🛠️ Admins", value=(f"`{p}create <ram> <cpu> <storage> @user [days]` – any capacity\n"
                                          f"`{p}editvps <vps_id> <ram> <cpu> <disk>` – edit a user's VPS (disk grows live)\n"
                                          f"`{p}renew <vps_id> <days>` • `{p}delete-vps @user <vps_id>`\n"
                                          f"`{p}userlimit @user [n|reset]` – per-user VPS limit\n"
                                          f"`{p}defaultlimit [n]` – default limit for everyone\n"
                                          f"`{p}dashboard [stop]` – live fleet dashboard\n"
                                          f"`{p}freedeploy enable|disable|spec <ram> <cpu> <disk>|limit <n>`\n"
                                          f"`{p}purge-protect <vps_id> on|off`\n"
                                          f"`{p}admin-add @user` • `{p}admin-remove @user` • `{p}admin-list`\n"
                                          f"`{p}node-add <name> <docker_host> [max]` • `{p}node-list` • `{p}node-remove <id>`\n"
                                          f"`{p}node-limit <id> <max>` – change a node's VPS capacity"), inline=False)
    e.set_footer(text=footer_text())
    await ctx.send(embed=e)


@bot.command(name="ping")
async def ping(ctx):
    t = time.perf_counter()
    m = await ctx.send("🏓 Pinging…")
    rtt = (time.perf_counter() - t) * 1000
    nodes = "\n".join(f"{'🟢' if n['ok'] else '🔴'} {n['name']}: `{ms(n['ms'])}`" for n in FLEET["nodes"])
    await m.edit(content=None, embed=simple("🏓 Pong!", f"**WebSocket:** `{bot.latency * 1000:.0f} ms`\n**Message round-trip:** `{rtt:.0f} ms`\n{nodes}", 0x2ECC71))


@bot.command(name="uptime")
async def uptime(ctx):
    try:
        host = dur(float(open("/proc/uptime").read().split()[0]))
    except OSError:
        host = "n/a"
    total = q("SELECT COUNT(*) c FROM vps")[0]["c"]
    await ctx.send(embed=simple("⏱️ Uptime", f"**Bot:** `{dur(time.time() - START_TIME)}`\n**Host:** `{host}`\n**VPS deployed:** `{total}`", 0x2ECC71))


@bot.command(name="manage")
async def manage(ctx):
    target = ctx.author          # everyone (admins too) can only open THEIR OWN panel
    rows = q("SELECT * FROM vps WHERE owner=? ORDER BY rowid", target.id)
    if not rows:
        return await ctx.send(embed=simple("❌ No VPS", f"{target.mention} has no VPS.", 0xE74C3C))
    v = rows[0]
    if len(rows) > 1:
        opts = [discord.SelectOption(label=f"VPS #{n} • {r['id']}", description=f"{r['image']} • {r['ram']}GB/{r['cpu']}c/{r['disk']}GB", value=r["id"])
                for n, r in enumerate(rows, 1)]
        pv = Picker(ctx.author.id, "Select a VPS to manage", opts)
        m = await ctx.send("🖥️ **Which VPS do you want to manage?**", view=pv)
        vid = await pv.wait_value()
        if not vid:
            return await m.delete()
        v = get_vps(vid)
    panel = Panel(v["id"], target.id, admin_view=is_admin(ctx.author))
    msg = await ctx.send(embed=await build_panel(v, 0), view=panel)
    panel.start_live(msg)


@bot.command(name="list")
async def list_cmd(ctx, user: discord.User = None):
    if user and is_admin(ctx.author):
        rows = q("SELECT * FROM vps WHERE owner=? ORDER BY rowid", user.id)
    elif is_admin(ctx.author):
        rows = q("SELECT * FROM vps ORDER BY rowid")
    else:
        rows = q("SELECT * FROM vps WHERE owner=? ORDER BY rowid", ctx.author.id)
    if not rows:
        return await ctx.send("📭 No VPS found.")
    if not is_admin(ctx.author):
        rows_note = f"\n\n📦 Your limit: **{len(rows)}/{user_limit(ctx.author.id)}** VPS"
    else:
        rows_note = ""
    lines = [f"`{r['id']}` <@{r['owner']}> • {r['image']} • {r['ram']}GB/{r['cpu']}c/{r['disk']}GB • node {r['node_id']} • {fmt_exp(r)}" for r in rows]
    await ctx.send(embed=simple("🖥️ VPS List", ("\n".join(lines)[:3800]) + rows_note))


@bot.command(name="create")
@admin_only
async def create(ctx, ram: int, cpu: int, storage: int, user: discord.User, days: int = None):
    if min(ram, cpu, storage) < 1 or (days is not None and days < 0):
        return await ctx.send("⚠️ RAM, CPU and storage must be at least 1.")
    await deploy_flow(ctx, user, ram, cpu, storage, days=days)


@bot.command(name="deploy")
async def deploy(ctx):
    if get_set("free_enabled", "0") != "1":
        return await ctx.send(embed=simple("🚫 Disabled", "Free deploy is currently disabled by the admins.", 0xE74C3C))
    limit = int(get_set("free_limit", "1"))
    if q("SELECT COUNT(*) c FROM vps WHERE owner=? AND free=1", ctx.author.id)[0]["c"] >= limit:
        return await ctx.send(embed=simple("🚫 Limit reached", f"You can only deploy {limit} free VPS.", 0xE74C3C))
    await deploy_flow(ctx, ctx.author, int(get_set("free_ram", "2")), int(get_set("free_cpu", "1")), int(get_set("free_disk", "10")), free=True)


@bot.group(name="freedeploy", invoke_without_command=True)
async def freedeploy(ctx):
    on = get_set("free_enabled", "0") == "1"
    await ctx.send(embed=simple("🆓 Free Deploy", f"**Status:** {'🟢 Enabled' if on else '🔴 Disabled'}\n"
                                f"**Spec:** {get_set('free_ram', '2')} GB RAM • {get_set('free_cpu', '1')} CPU • {get_set('free_disk', '10')} GB disk\n"
                                f"**Limit per user:** {get_set('free_limit', '1')}"))


@freedeploy.command(name="enable")
@admin_only
async def fd_enable(ctx):
    put_set("free_enabled", "1")
    await ctx.send(embed=simple("✅ Free deploy enabled", f"Everyone can now use `{PREFIX}deploy`.", 0x2ECC71))


@freedeploy.command(name="disable")
@admin_only
async def fd_disable(ctx):
    put_set("free_enabled", "0")
    await ctx.send(embed=simple("🔴 Free deploy disabled", "", 0xE74C3C))


@freedeploy.command(name="spec")
@admin_only
async def fd_spec(ctx, ram: int, cpu: int, disk: int):
    if min(ram, cpu, disk) < 1:
        return await ctx.send("⚠️ Values must be at least 1.")
    put_set("free_ram", ram); put_set("free_cpu", cpu); put_set("free_disk", disk)
    await ctx.send(embed=simple("✅ Free spec updated", f"{ram} GB RAM • {cpu} CPU • {disk} GB disk", 0x2ECC71))


@freedeploy.command(name="limit")
@admin_only
async def fd_limit(ctx, n: int):
    put_set("free_limit", max(1, n))
    await ctx.send(embed=simple("✅ Limit updated", f"Max {max(1, n)} free VPS per user.", 0x2ECC71))


@bot.command(name="delete-vps", aliases=["deletevps"])
@admin_only
async def delete_vps(ctx, user: discord.User, vps_id: str):
    v = find_vps(user.id, vps_id)
    if not v:
        return await ctx.send(embed=simple("❌ Not found", f"{user.mention} has no VPS `{vps_id}`.", 0xE74C3C))
    m = await ctx.send("🗑️ Deleting…")
    await destroy(v)
    ex("DELETE FROM vps WHERE id=?", v["id"])
    await m.edit(content=None, embed=simple("🗑️ VPS Deleted", f"`{v['id']}` owned by {user.mention} was removed.", 0x2ECC71))


@bot.command(name="renew")
@admin_only
async def renew(ctx, vps_id: str, days: int):
    v = get_vps(vps_id)
    if not v:
        return await ctx.send("❌ VPS not found.")
    base = max(now(), datetime.fromisoformat(v["expires"])) if v["expires"] else now()
    ex("UPDATE vps SET expires=? WHERE id=?", (base + timedelta(days=days)).isoformat(), vps_id)
    await ctx.send(embed=simple("✅ Renewed", f"`{vps_id}` extended by {days} days.", 0x2ECC71))


@bot.command(name="editvps")
@admin_only
async def editvps(ctx, vps_id: str, ram: int, cpu: int, disk: int):
    v = get_vps(vps_id)
    if not v:
        return await ctx.send("❌ VPS not found.")
    if min(ram, cpu, disk) < 1:
        return await ctx.send("⚠️ Values must be at least 1.")
    notes = await apply_edit(v, ram, cpu, disk)
    await ctx.send(embed=simple("✅ VPS updated", f"`{vps_id}` → {ram} GB RAM • {cpu} CPU • {get_vps(vps_id)['disk']} GB disk\n" + "\n".join(notes), 0x2ECC71))


@bot.command(name="userlimit")
@admin_only
async def userlimit(ctx, user: discord.User, n: str = None):
    if n is None:
        return await ctx.send(embed=simple("📦 User limit", f"{user.mention}: **{user_count(user.id)}/{user_limit(user.id)}** VPS"))
    if n.lower() == "reset":
        ex("DELETE FROM user_limits WHERE user_id=?", user.id)
        return await ctx.send(embed=simple("✅ Limit reset", f"{user.mention} now uses the default ({get_set('default_limit', str(DEFAULT_LIMIT))}).", 0x2ECC71))
    if not n.isdigit():
        return await ctx.send(f"⚠️ Usage: `{PREFIX}userlimit @user <number|reset>`")
    ex("INSERT INTO user_limits(user_id,max_vps) VALUES(?,?) ON CONFLICT(user_id) DO UPDATE SET max_vps=excluded.max_vps", user.id, int(n))
    await ctx.send(embed=simple("✅ Limit set", f"{user.mention} can have up to **{n}** VPS (has {user_count(user.id)}).", 0x2ECC71))


@bot.command(name="defaultlimit")
@admin_only
async def defaultlimit(ctx, n: int = None):
    if n is None:
        return await ctx.send(embed=simple("📦 Default limit", f"**{get_set('default_limit', str(DEFAULT_LIMIT))}** VPS per user (admins exempt)."))
    put_set("default_limit", max(0, n))
    await ctx.send(embed=simple("✅ Default limit updated", f"Everyone without a custom limit: **{max(0, n)}** VPS.", 0x2ECC71))


@bot.command(name="dashboard")
@admin_only
async def dashboard(ctx, action: str = ""):
    items = [x for x in get_set("dashboards", "").split(",") if ":" in x]
    if action == "stop":
        put_set("dashboards", ",".join(x for x in items if not x.startswith(f"{ctx.channel.id}:")))
        return await ctx.send("🛑 Dashboards in this channel stopped (delete the message to remove it).")
    await refresh_fleet()
    m = await ctx.send(embed=build_dashboard())
    put_set("dashboards", ",".join(items + [f"{ctx.channel.id}:{m.id}"]))


@bot.command(name="purge-protect")
@admin_only
async def purge_protect(ctx, vps_id: str, state: str):
    if not get_vps(vps_id):
        return await ctx.send("❌ VPS not found.")
    ex("UPDATE vps SET purge=? WHERE id=?", 1 if state.lower() in ("on", "enable", "1") else 0, vps_id)
    await ctx.send(f"🔒 Purge protection for `{vps_id}`: **{state.lower()}**")


@bot.command(name="admin-add", aliases=["adminadd"])
@admin_only
async def admin_add(ctx, user: discord.User):
    if user.bot:
        return await ctx.send("⚠️ You can't make a bot an admin.")
    if is_admin_id(user.id):
        return await ctx.send(embed=simple("ℹ️ Already admin", f"{user.mention} is already an admin.", 0xF1C40F))
    ex("INSERT INTO admins(user_id,added_by,added_at) VALUES(?,?,?)", user.id, ctx.author.id, now().isoformat())
    await ctx.send(embed=simple("✅ Admin added", f"{user.mention} is now an admin.\nAdded by {ctx.author.mention}.", 0x2ECC71))


@bot.command(name="admin-remove", aliases=["adminremove"])
@admin_only
async def admin_remove(ctx, user: discord.User):
    if user.id in ADMIN_IDS:
        return await ctx.send("🚫 That user is an owner admin (set in `.env`) and can't be removed from chat.")
    if not q("SELECT 1 FROM admins WHERE user_id=?", user.id):
        return await ctx.send(embed=simple("❌ Not an admin", f"{user.mention} is not a chat-added admin.", 0xE74C3C))
    ex("DELETE FROM admins WHERE user_id=?", user.id)
    await ctx.send(embed=simple("✅ Admin removed", f"{user.mention} is no longer an admin.", 0x2ECC71))


@bot.command(name="admin-list", aliases=["adminlist"])
@admin_only
async def admin_list(ctx):
    owners = [f"👑 <@{i}> (owner)" for i in sorted(ADMIN_IDS)]
    added = [f"🛡️ <@{r['user_id']}> • added by <@{r['added_by']}>" for r in q("SELECT * FROM admins ORDER BY rowid")]
    await ctx.send(embed=simple("🛡️ Admins", "\n".join(owners + added) or "No admins."))


@bot.command(name="node-add")
@admin_only
async def node_add(ctx, name: str, host: str, max_vps: int = 50):
    """host = docker host, e.g. ssh://root@1.2.3.4  (use 'local' for this machine)"""
    host = "" if host.lower() == "local" else host
    rc, o, e = await dock({"host": host}, "version", "--format", "{{.Server.Version}}", timeout=30)
    if rc:
        return await ctx.send(embed=simple("❌ Cannot reach docker", f"```{e[:800]}```", 0xE74C3C))
    ex("INSERT INTO nodes(name,host,max_vps) VALUES(?,?,?)", name, host, max_vps)
    await ctx.send(embed=simple("✅ Node added", f"`{name}` (docker {o})", 0x2ECC71))


@bot.command(name="node-list")
@admin_only
async def node_list(ctx):
    lines = [f"`{n['id']}` **{n['name']}** • `{n['host'] or 'local'}` • "
             f"{q('SELECT COUNT(*) c FROM vps WHERE node_id=?', n['id'])[0]['c']}/{n['max_vps']}" for n in q("SELECT * FROM nodes")]
    await ctx.send(embed=simple("🖧 Nodes", "\n".join(lines)))


@bot.command(name="node-limit", aliases=["nodelimit"])
@admin_only
async def node_limit(ctx, node_id: int, max_vps: int = None):
    """2node-limit <node_id> <new_max>  - change a node's VPS capacity (the '50' in 'x/50')"""
    nd = get_node(node_id)
    if not nd:
        return await ctx.send(embed=simple("❌ Node not found", f"No node with ID `{node_id}`. Use `{PREFIX}node-list`.", 0xE74C3C))
    used = q("SELECT COUNT(*) c FROM vps WHERE node_id=?", node_id)[0]["c"]
    if max_vps is None:
        return await ctx.send(embed=simple("📦 Node limit", f"**{nd['name']}**: `{used}/{nd['max_vps']}` VPS\nChange it with `{PREFIX}node-limit {node_id} <number>`."))
    if max_vps < 1:
        return await ctx.send("⚠️ Limit must be at least 1.")
    if max_vps < used:
        return await ctx.send(embed=simple("⚠️ Too low", f"**{nd['name']}** already has **{used}** VPS. Choose **{used}** or more (or delete some VPS first).", 0xF1C40F))
    ex("UPDATE nodes SET max_vps=? WHERE id=?", max_vps, node_id)
    await refresh_fleet()
    await ctx.send(embed=simple("✅ Node limit updated", f"**{nd['name']}** capacity: `{nd['max_vps']}` → `{max_vps}` (using {used}).", 0x2ECC71))


@bot.command(name="node-remove")
@admin_only
async def node_remove(ctx, node_id: int):
    if q("SELECT 1 FROM vps WHERE node_id=?", node_id):
        return await ctx.send("❌ Node still has VPS on it.")
    ex("DELETE FROM nodes WHERE id=?", node_id)
    await ctx.send("✅ Node removed.")


if __name__ == "__main__":
    if not TOKEN:
        raise SystemExit("Set DISCORD_TOKEN in your .env file")
    bot.run(TOKEN)

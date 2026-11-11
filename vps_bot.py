#!/usr/bin/env python3
"""
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
BRAND = os.getenv("BRAND", "FLOATIX")
LOGO = os.getenv("BRAND_LOGO_URL", "")
BANNER = os.getenv("BRAND_BANNER_URL", "")
ADMIN_IDS = {int(x) for x in os.getenv("ADMIN_IDS", "").replace(" ", "").split(",") if x.isdigit()}
EXPIRY_DAYS = int(os.getenv("EXPIRY_DAYS", "30"))          # 0 = never expires
ENFORCE_LIMITS = os.getenv("ENFORCE_LIMITS", "false").lower() in ("1", "true", "yes")
STORAGE_MODE = os.getenv("STORAGE_MODE", "auto")           # auto | opt | loop
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
CREATE TABLE IF NOT EXISTS nodes(id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT, host TEXT DEFAULT '', max_vps INTEGER DEFAULT 50);
CREATE TABLE IF NOT EXISTS vps(id TEXT PRIMARY KEY, owner INTEGER, node_id INTEGER, image TEXT,
  ram INTEGER, cpu INTEGER, disk INTEGER, mode TEXT DEFAULT 'opt', created TEXT, expires TEXT,
  purge INTEGER DEFAULT 0, free INTEGER DEFAULT 0);
""")
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


def is_admin(user):
    return user.id in ADMIN_IDS


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
            f"[ -f {d}/disk.img ] || {{ truncate -s {gb}G {d}/disk.img && mkfs.ext4 -q -F {d}/disk.img; }}; "
            f"mountpoint -q {d}/mnt || mount -o loop {d}/disk.img {d}/mnt; "
            f"mkdir -p {d}/mnt/root {d}/mnt/home; chmod 700 {d}/mnt/root")


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


async def info(v):
    node, n = get_node(v["node_id"]), cname(v)
    rc, o, _ = await dock(node, "inspect", "-f", "{{.State.Status}}|{{.State.StartedAt}}", n)
    if rc:
        return {"state": "missing"}
    state, started = o.split("|", 1)
    d = {"state": state, "started": started, "cpu": 0.0, "ram": 0.0, "disk": None}
    if state != "running":
        return d
    rc, o, _ = await dock(node, "stats", "--no-stream", "--format", "{{.CPUPerc}}|{{.MemUsage}}", n)
    if rc == 0 and "|" in o:
        c, m = o.split("|", 1)
        try:
            d["cpu"] = float(c.strip().rstrip("%"))
        except ValueError:
            pass
        d["ram"] = to_mb(m.split("/")[0])
    if v["mode"] == "loop":
        rc, o, _ = await dock(node, "exec", n, "df", "-Pk", "/root")
        if rc == 0 and len(o.splitlines()) > 1:
            d["disk"] = int(o.splitlines()[-1].split()[2]) / 1048576
    else:
        rc, o, _ = await dock(node, "inspect", "-s", "-f", "{{.SizeRw}}", n)
        if rc == 0 and o.isdigit():
            d["disk"] = int(o) / 1073741824
    return d


async def sshx_link(v):
    node, n = get_node(v["node_id"]), cname(v)
    script = SSHX_SH.replace("@LOG@", f"/tmp/sshx_{secrets.token_hex(4)}.log")
    rc, o, e = await dock(node, "exec", n, "bash", "-c", script, timeout=120)
    if rc != 0 or "sshx.io/s/" not in o:
        raise RuntimeError(e or o or "no link returned")
    return o.splitlines()[-1].strip()

# ───────────────────────────── formatting ─────────────────────────────
def bar(p, n=10):
    f = round(min(max(p, 0), 100) / 100 * n)
    return "[" + "█" * f + "░" * (n - f) + "]"


def dur(sec):
    sec = int(sec)
    d, r = divmod(sec, 86400)
    h, r = divmod(r, 3600)
    m, s = divmod(r, 60)
    return f"{d}d {h}h {m}m" if d else f"{h}h {m}m" if h else f"{m}m {s}s"


def expired(v):
    return bool(v["expires"]) and datetime.fromisoformat(v["expires"]) < now() and not v["purge"]


def fmt_exp(v):
    if not v["expires"]:
        return "Never"
    delta = datetime.fromisoformat(v["expires"]) - now()
    if delta.total_seconds() < 0:
        return "Expired"
    return f"in {delta.days} days" if delta.days else f"in {delta.seconds // 3600} hours"


async def build_panel(v):
    i = await info(v)
    st = i["state"]
    color = {"running": 0x2ECC71, "missing": 0x95A5A6}.get(st, 0xE74C3C)
    e = discord.Embed(title=f"⚙️ {BRAND} VPS Panel (Secure)", color=color)
    status = {"running": "🟢 Running", "missing": "⚠️ Container missing"}.get(st, "🔴 Stopped")
    up = "—"
    if st == "running":
        try:
            t = datetime.strptime(i["started"][:19], "%Y-%m-%dT%H:%M:%S")
            up = f"{dur((now() - t).total_seconds())} ago"
        except ValueError:
            pass
    desc = (f"**VPS No:** `{vps_number(v)}` | **ID:** `{v['id']}`\n"
            f"**OS:** `{dict((b, a) for a, b in OS_OPTIONS).get(v['image'], v['image'])}`\n\n"
            f"**Status:** {status}\n**Uptime:** `{up}`\n**Expires:** `{fmt_exp(v)}`\n\n")
    if st == "running":
        cpu = min(100.0, i["cpu"] / max(v["cpu"], 1))
        lvl = "Normal" if cpu < 60 else "High" if cpu < 85 else "Critical"
        ram_t = v["ram"] * 1024
        rp = i["ram"] / ram_t * 100 if ram_t else 0
        du = i["disk"]
        dp = (du / v["disk"] * 100) if (du is not None and v["disk"]) else 0
        disk_line = (f"💽 **Disk:** {du:.1f} GB / {v['disk']} GB\n{bar(dp)} {dp:.1f}%\n" if du is not None
                     else f"💽 **Disk:** — / {v['disk']} GB\n")
        desc += (f"📊 **Live Stats:**\n🖥️ **CPU:** {lvl} {bar(cpu)} {cpu:.2f}%\n"
                 f"🧠 **RAM:** {i['ram']:.2f} MB / {ram_t} MB\n{bar(rp)} {rp:.1f}%\n" + disk_line)
    else:
        desc += (f"📊 **Spec:** {v['ram']} GB RAM (shared) • {v['cpu']} CPU (shared) • {v['disk']} GB disk\n")
    desc += f"🔒 **Purge Protection:** {'🔐 Enabled' if v['purge'] else '🔓 Disabled'}"
    e.description = desc
    if LOGO:
        e.set_thumbnail(url=LOGO)
    if BANNER:
        e.set_image(url=BANNER)
    e.set_footer(text=f"Powered by {BRAND}")
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


async def deploy_flow(ctx, owner, ram, cpu, disk, free=False):
    node_id = await pick_node(ctx)
    if not node_id:
        return
    image = await pick_os(ctx)
    if not image:
        return
    vid = uuid.uuid4().hex[:8]
    exp = (now() + timedelta(days=EXPIRY_DAYS)).isoformat() if EXPIRY_DAYS else None
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
    text = (f"{owner.mention} your VPS is ready!\n**ID:** `{vid}` • **OS:** `{image}`\n"
            f"**Spec:** {ram} GB RAM (shared) • {cpu} CPU (shared) • {disk} GB storage ({v['mode']})\n"
            f"Use `{PREFIX}manage` to control it.")
    if warn:
        text += f"\n⚠️ {warn}"
    await msg.edit(content=owner.mention, embed=simple("✅ VPS Created", text, 0x2ECC71))

# ───────────────────────────── manage panel ─────────────────────────────
locks = {}


class Panel(discord.ui.View):
    def __init__(self, vid, owner_id):
        super().__init__(timeout=900)
        self.vid, self.owner_id = vid, owner_id

    async def interaction_check(self, i):
        if i.user.id == self.owner_id or is_admin(i.user):
            return True
        await i.response.send_message("❌ This isn't your VPS.", ephemeral=True)
        return False

    async def redraw(self, i):
        v = get_vps(self.vid)
        if v:
            await i.message.edit(embed=await build_panel(v), view=self)

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
            await self.redraw(i)
        elif action == "sshx":
            await i.response.defer(ephemeral=True, thinking=True)
            if (await info(v))["state"] != "running":
                return await i.followup.send("❌ VPS is not running. Press **Start** first.", ephemeral=True)
            link = await sshx_link(v)
            await i.followup.send(embed=simple("🌐 SSHX Session", f"{link}\n\nPress **SSHX** again for a brand-new session.", 0x7289DA), ephemeral=True)
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
            self.stop()

    @discord.ui.button(label="Start", emoji="▶️", style=discord.ButtonStyle.success, row=0)
    async def b_start(self, i, b):
        await self.act(i, "start")

    @discord.ui.button(label="Stop", emoji="⏹️", style=discord.ButtonStyle.danger, row=0)
    async def b_stop(self, i, b):
        await self.act(i, "stop")

    @discord.ui.button(label="Restart", emoji="🔁", style=discord.ButtonStyle.primary, row=0)
    async def b_restart(self, i, b):
        await self.act(i, "restart")

    @discord.ui.button(label="SSHX", emoji="❎", style=discord.ButtonStyle.secondary, row=0)
    async def b_sshx(self, i, b):
        await self.act(i, "sshx")

    @discord.ui.button(label="Reinstall", emoji="🔴", style=discord.ButtonStyle.secondary, row=1)
    async def b_reinstall(self, i, b):
        await self.act(i, "reinstall")

    @discord.ui.button(label="Refresh", emoji="🔄", style=discord.ButtonStyle.secondary, row=1)
    async def b_refresh(self, i, b):
        await self.act(i, "refresh")

    @discord.ui.button(label="Delete", emoji="🗑️", style=discord.ButtonStyle.danger, row=1)
    async def b_delete(self, i, b):
        await self.act(i, "delete")

# ───────────────────────────── bot + commands ─────────────────────────────
intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix=PREFIX, intents=intents, help_command=None)


@bot.event
async def on_ready():
    rc, o, e = await run("docker", "version", "--format", "{{.Server.Version}}")
    log.info("Logged in as %s | docker: %s", bot.user, o if rc == 0 else f"ERROR {e}")
    if not ADMIN_IDS:
        log.warning("ADMIN_IDS is empty - nobody can use admin commands!")
    if not expiry_task.is_running():
        expiry_task.start()
    await bot.change_presence(activity=discord.Game(name=f"{PREFIX}help • {BRAND}"))


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


@tasks.loop(minutes=10)
async def expiry_task():
    for v in q("SELECT * FROM vps WHERE expires IS NOT NULL AND expires < ? AND purge=0", now().isoformat()):
        await dock(get_node(v["node_id"]), "stop", "-t", "5", cname(v))


@bot.command(name="help")
async def help_cmd(ctx):
    p = PREFIX
    e = simple(f"{BRAND} VPS Bot", "", 0x5865F2)
    e.add_field(name="👤 Users", value=(f"`{p}manage` – manage your VPS (start/stop/restart/SSHX/reinstall/delete)\n"
                                         f"`{p}list` – your VPS list\n`{p}deploy` – free VPS (when enabled)\n"
                                         f"`{p}ping` • `{p}uptime`"), inline=False)
    e.add_field(name="🛠️ Admins", value=(f"`{p}create <ram> <cpu> <storage> @user`\n`{p}delete-vps @user <vps_id>`\n"
                                          f"`{p}freedeploy enable|disable|spec <ram> <cpu> <disk>|limit <n>`\n"
                                          f"`{p}renew <vps_id> <days>` • `{p}purge-protect <vps_id> on|off`\n"
                                          f"`{p}node-add <name> <docker_host> [max]` • `{p}node-list` • `{p}node-remove <id>`"), inline=False)
    await ctx.send(embed=e)


@bot.command(name="ping")
async def ping(ctx):
    t = time.perf_counter()
    m = await ctx.send("🏓 Pinging…")
    rtt = (time.perf_counter() - t) * 1000
    await m.edit(content=None, embed=simple("🏓 Pong!", f"**WebSocket:** `{bot.latency * 1000:.0f} ms`\n**Message round-trip:** `{rtt:.0f} ms`", 0x2ECC71))


@bot.command(name="uptime")
async def uptime(ctx):
    try:
        host = dur(float(open("/proc/uptime").read().split()[0]))
    except OSError:
        host = "n/a"
    total = q("SELECT COUNT(*) c FROM vps")[0]["c"]
    await ctx.send(embed=simple("⏱️ Uptime", f"**Bot:** `{dur(time.time() - START_TIME)}`\n**Host:** `{host}`\n**VPS deployed:** `{total}`", 0x2ECC71))


@bot.command(name="manage")
async def manage(ctx, user: discord.User = None):
    target = user if (user and is_admin(ctx.author)) else ctx.author
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
    await ctx.send(embed=await build_panel(v), view=Panel(v["id"], target.id))


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
    lines = [f"`{r['id']}` <@{r['owner']}> • {r['image']} • {r['ram']}GB/{r['cpu']}c/{r['disk']}GB • node {r['node_id']} • {fmt_exp(r)}" for r in rows]
    await ctx.send(embed=simple("🖥️ VPS List", "\n".join(lines)[:4000]))


@bot.command(name="create")
@admin_only
async def create(ctx, ram: int, cpu: int, storage: int, user: discord.User):
    if min(ram, cpu, storage) < 1:
        return await ctx.send("⚠️ RAM, CPU and storage must be at least 1.")
    await deploy_flow(ctx, user, ram, cpu, storage)


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


@bot.command(name="purge-protect")
@admin_only
async def purge_protect(ctx, vps_id: str, state: str):
    if not get_vps(vps_id):
        return await ctx.send("❌ VPS not found.")
    ex("UPDATE vps SET purge=? WHERE id=?", 1 if state.lower() in ("on", "enable", "1") else 0, vps_id)
    await ctx.send(f"🔒 Purge protection for `{vps_id}`: **{state.lower()}**")


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

"""Checks for the O-105's path.  No AP and no network.

    python test-o105.py

What the O-105 has that no other model had: a file in Arista's image that
points the environment tools at the wrong partition until Arista's own
start-up corrects it; a boot loader with no 'sleep' whose first fetch comes
before its network link is up; and an "offline mode" in Arista's firmware that
refuses the only way in.  Its install is the C-200's shape (the boot command,
then slot 0 formatted with factory.ubi).

Its flash table and environment are a unit's own, as Arista 8.8.2-49.2 listed
them on 2026-11-22.
"""
import importlib.util
import os
import re
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
spec = importlib.util.spec_from_file_location("af", os.path.join(HERE, "arista-field.py"))
af = importlib.util.module_from_spec(spec)
sys.modules["af"] = af
spec.loader.exec_module(af)

FAILED = []


def check(name, ok, detail=""):
    print("%s  %s%s" % ("ok  " if ok else "FAIL", name, ("   (%s)" % (detail,)) if detail and not ok else ""))
    if not ok:
        FAILED.append(name)


class Died(Exception):
    pass


def die(msg, hint=None):
    raise Died(str(msg))


af.say = lambda *a, **k: None
af.tell = lambda *a, **k: None
af.note = lambda *a, **k: None
af.time.sleep = lambda *_: None
REAL_DIE = af.die
af.die = die

M = af.MODELS["o105"]

# --- names -------------------------------------------------------------------------
f = af.files_for("o105", "install")
check("its release files: a zImage RAM installer, factory.ubi and the install sums",
      f == {"itb": "openwrt-ipq40xx-generic-arista_o-105-initramfs-zImage.itb",
            "ubi": "openwrt-ipq40xx-generic-arista_o-105-squashfs-factory.ubi",
            "sums": "o105-install-SHA256SUMS"}, f)
check("an update takes the sysupgrade image",
      af.files_for("o105", "update") == {"sysupgrade": "openwrt-ipq40xx-generic-arista_o-105-squashfs-sysupgrade.bin"})
check("a unit that answers as arista,o-105, or as O-105 on Arista's firmware, is the O-105",
      af.port_board("arista,o-105") and af.model_key("arista,o-105") == "o105" and af.model_key("O-105") == "o105")
check("discovery looks at its MAC prefix", "30:b6:2d" in af.AP_OUIS)

# --- its flash, as its own firmware lists it ---------------------------------------
STOCK = """dev:    size   erasesize  name
mtd0: 04000000 00020000 "rootfs"
mtd1: 04000000 00020000 "rootfs2"
mtd2: 00040000 00010000 "0:SBL1"
mtd3: 00020000 00010000 "0:MIBIB"
mtd4: 00060000 00010000 "0:QSEE"
mtd5: 00010000 00010000 "0:CDT"
mtd6: 00010000 00010000 "0:DDRPARAMS"
mtd7: 00080000 00010000 "0:APPSBL"
mtd8: 00010000 00010000 "0:APPSBLENV"
mtd9: 00010000 00010000 "0:ART"
mtd10: 00500000 00010000 "0:HLOS"
mtd11: 00500000 00010000 "0:HLOS1"
mtd12: 00400000 00010000 "0:UCONFIG"
mtd13: 00010000 00010000 "PANIC"
mtd14: 00010000 00010000 "RESERVED:0"
mtd15: 01060000 00010000 "RESERVED:1"
mtd16: 01287000 0001f000 "ubi_rootfs"
mtd17: 02891000 0001f000 "ubi_rootfs_data"
"""
rows = af.mtd_table(STOCK)
real = [r for r in rows if not af.is_ubi_volume(r)]
s0, s1 = af.find_part(rows, M["slot0"]), af.find_part(rows, M["slot1"])
nor = sum(r["size"] for r in real if r is not s0 and r is not s1 and r["erase"] < af.NAND_ERASE)
check("16 real partitions; the two UBI volumes are not among them", len(real) == 16, len(real))
check("its NOR partitions add up to 32 MiB", nor == M["nor_total"], hex(nor))
check("both slots are found, 64 MiB each",
      s0["dev"] == "mtd0" and s1["dev"] == "mtd1" and s0["size"] == s1["size"] == M["slot_size"])
check("the environment and the fallback kernel are found",
      af.find_part(rows, M["appsblenv"])["dev"] == "mtd8" and af.find_part(rows, M["need_nor"])["dev"] == "mtd11")

# --- the check before anything is written from Arista's firmware -------------------
ENV = ("ba0=console=ttyMSM0,115200n8 ubi.mtd=rootfs root=mtd:ubi_rootfs rootfstype=squashfs\n"
       "ba1=console=ttyMSM0,115200n8 ubi.mtd=rootfs2 root=mtd:ubi_rootfs rootfstype=squashfs\n"
       "bootcmd=run ubootscript\npri=1\nubootscript=x\n")


class Pty:
    """Arista's root shell: what each command answers."""

    def __init__(self, config, mtd=STOCK):
        self.asked = []
        self.answers = (("uname", "Linux 3.14.43 armv7l"), ("br0/address", "30:b6:2d:00:00:01"),
                        ("/proc/mtd", mtd), ("fw_env.config", config), ("fw_printenv", ENV),
                        ("mtd_num", "1"), ("/proc/cmdline", "ubi.mtd=rootfs2"),
                        ("$2 == \"/\"", "rootfs"),          # before /proc/mounts: it reads that file too
                        ("/proc/mounts", "/dev/mtdblock12 /config_info jffs2 rw,relatime 0 0\n"),
                        ("df -k", "50000"))

    def run(self, cmd, timeout=60):
        self.asked.append(cmd)
        for key, val in self.answers:
            if key in cmd:
                return val, 0
        return "", 1

    def must(self, cmd, timeout=60, what=None):
        return self.run(cmd, timeout)[0]


def verify(config, model="o105", mtd=STOCK):
    pty = Pty(config, mtd)
    card = {"model": model, "firmware": "8.8.2-49.2"}
    try:
        af.verify_stock(pty, card)
        return pty, card, None
    except Died as e:
        return pty, card, str(e)


pty, card, died = verify("/dev/mtd8 0x0 0x10000 0x10000\n")
check("the environment tools pointing at the environment's partition: the unit passes",
      died is None and card["env_dev"]["dev"] == "mtd8" and card["pri"] == "1", died)
check("in use on this unit: the slot Arista runs from, and UCONFIG", card.get("live") == {"mtd1", "mtd12"},
      card.get("live"))
pty, card, died = verify("# Configuration file for fw_(printenv/setenv) utility.\n"
                         "# Up to two entries are valid.\n\n"
                         "# MTD device name\tDevice offset\tEnv. size\tFlash sector size\n"
                         "\n/dev/mtd8 0x0000 0x10000 0x10000\n")
check("Arista's own file, with comments and blank lines before its entry: the entry is found",
      died is None and card["env_dev"]["dev"] == "mtd8", died)
pty, card, died = verify("/dev/mtd6 0x0 0x10000 0x10000\n")
check("the image's own copy of that file (0:DDRPARAMS): stops, says nothing was written and to restart",
      died and "/dev/mtd6" in died and "Nothing was written" in died and "restart the AP" in died, died)
i_cfg = next((i for i, c in enumerate(pty.asked) if "fw_env.config" in c), None)
check("that file is read before the environment is", i_cfg is not None
      and i_cfg < next(i for i, c in enumerate(pty.asked) if c.startswith("fw_printenv")), pty.asked)
pty, card, died = verify("")
check("no such file, or an empty one: stops too", died and "point at nothing" in died, died)
check("only the O-105 asks for that file", [k for k, v in af.MODELS.items() if v.get("env_config_check")] == ["o105"])

# --- the one-time network boot -----------------------------------------------------


def nb(mdl, itb="o105.itb", home=None):
    return af.netboot_env("192.0.2.1", "192.0.2.230", itb, mdl["fit_cfg"],
                          mdl.get("load_addr", "0x60000000"), mdl.get("stock_bootcmd", "run ubootscript"),
                          mdl.get("fdt_high", "0x4A3E0000"), mdl.get("net_settle", 5),
                          mdl.get("net_tries", 2), mdl.get("retry_sleep", 3), mdl.get("netretry", "no"),
                          mdl.get("net_helper", False), home=home, net_prep=mdl.get("net_prep"),
                          net_wait=mdl.get("net_wait"))


env = dict(nb(M))
check("the image is fetched to 0x84000000 and started with a bare bootm",
      env["owrt_try"] == "tftpboot 0x84000000 o105.itb && bootm 0x84000000" and "fdt_high" not in env["owrt_nb"], env)
check("the wait is a memory dump, then the try", env["owrt_wait"] == "md.l 0x80000000 0x2000; run owrt_try", env)
check("one try at once, three more with the wait before each, then a reset",
      env["owrt_nb"].endswith("run owrt_try; run owrt_wait; run owrt_wait; run owrt_wait; reset")
      and env["owrt_nb"].count("run owrt_try") == 1, env["owrt_nb"])
check("its boot loader has no 'sleep' and no 'ethspeed', so neither is written",
      not re.search(r"sleep|ethspeed", " ".join(env.values())), env)
check("Arista's own boot command is saved first; the netboot's is written last",
      [n for n, _ in nb(M)] == ["owrt_clear", "owrt_try", "owrt_wait", "owrt_nb", "bootcmd"]
      and env["owrt_clear"] == 'setenv bootcmd "run ubootscript"; saveenv'
      and env["bootcmd"] == "run owrt_clear; run owrt_nb")
own = dict(M["install_env"])["bootcmd"]
homed = nb(M, home=own)
check("from an installed O-105 the fallback is its own boot command, kept in owrt_home",
      homed[0] == ("owrt_home", own) and dict(homed)["owrt_clear"] == 'setenv bootcmd "run owrt_home"; saveenv')
check("no other model gets a wait made of a command",
      all("owrt_wait" not in dict(nb(af.MODELS[k], "x.itb")) for k in af.MODELS if k != "o105"),
      [k for k in af.MODELS if af.MODELS[k].get("net_wait")])

# --- the way in --------------------------------------------------------------------
check("the upgrade wizard is the way in (8.8.2 has no radartool)", M["root_order"] == ("upgrade",))


class Console:
    def __init__(self, reply):
        self.buf, self.reply = "", reply

    def mark(self):
        return len(self.buf)

    def since(self, mark):
        return self.buf[mark:]

    def send(self, text):
        self.buf += self.reply

    def wait(self, pattern, timeout, mark=None):
        return re.search(pattern, self.since(mark or 0), re.M | re.S)


try:
    af._try_upgrade(Console("upgrade\nCannot execute this command in offline mode\nconfig> "), "192.0.2.9", "x")
    said = ""
except af.MethodFailed as e:
    said = str(e)
check("Arista's offline mode: said in plain words, with what to do",
      "offline mode" in said and "restart the AP" in said and "run the tool again" in said, said)
try:
    af._try_upgrade(Console("upgrade\nsomething else entirely\nconfig> "), "192.0.2.9", "x")
    said = ""
except af.MethodFailed as e:
    said = str(e)
check("any other silence from the wizard keeps the old message, with what the AP printed",
      "offered neither" in said and "something else entirely" in said, said)

# --- the install, made by the tool -------------------------------------------------
d = tempfile.mkdtemp()
img = os.path.join(d, "factory.ubi")
open(img, "wb").write(b"UBI#" + b"\0" * 60)
KERNEL, ROOTFS = "k" * 64, "r" * 64
sums = os.path.join(d, "o105-install-SHA256SUMS")
open(sums, "w").write("%s  factory.ubi\n%s volume:kernel 100\n%s volume:rootfs 200\n"
                      % (af.sha256_file(img), KERNEL, ROOTFS))
RAM = """mtd0: 04000000 00020000 "ubi"
mtd1: 04000000 00020000 "arista_rootfs2"
mtd8: 00010000 00010000 "0:appsblenv"
mtd11: 00500000 00010000 "0:hlos1"
"""


class Run:
    installer = object()
    yes = yes_install = True

    def __init__(self):
        self.env_writes, self.flash_writes = [], []


def install(slot1_magic="55424923", kernel_magic="d00dfeed", rootfs_sha=ROOTFS):
    ran = []

    def ssh_exec(con, cmd, timeout=0):
        ran.append(cmd)
        if cmd.startswith("command -v"):
            return "ok\n", 0
        if cmd.startswith("hexdump"):
            return "%s\n%s\n" % (slot1_magic, kernel_magic), 0
        if cmd.startswith("sha256sum"):
            return "%s\n55424923" % af.sha256_file(img), 0
        if cmd.startswith("printf"):
            return "", 0
        if cmd.startswith("fw_printenv bootcmd pri"):
            return "bootcmd=%s\npri=1\n" % own, 0
        if cmd.startswith("fw_printenv"):
            return ENV, 0
        if "ubiformat /dev/" in cmd:
            return "UBIFORMAT_RC=0\nUBI device number 0, total 512 LEBs\n", 0
        if cmd.startswith("for v in /sys/class/ubi"):
            return "kernel ubi0_0\nrootfs ubi0_1\nrootfs_data ubi0_2\n", 0
        if cmd.startswith("head -c 100 "):
            return KERNEL + "\n", 0
        if cmd.startswith("head -c 200 "):
            return rootfs_sha + "\n", 0
        return "", 0

    saved = (af.ssh_exec, af.ssh_must, af._send_file)
    af.ssh_exec = ssh_exec
    af.ssh_must = lambda con, cmd, timeout=0: RAM if "proc/mtd" in cmd else ""
    af._send_file = lambda con, local, remote, what: None
    af.LOG = type("Log", (), {"write": staticmethod(lambda text: None)})()
    run = Run()
    died = None
    try:
        af.do_install_ubiformat(run, {"model": "o105"}, "192.0.2.9", img, sums)
    except Died as e:
        died = str(e)
    finally:
        af.ssh_exec, af.ssh_must, af._send_file = saved
    return ran, run, died


check("it is installed by this tool, by formatting slot 0 with factory.ubi",
      af.field_install("o105") and not M.get("no_image") and M["install_by"] == "ubiformat"
      and M.get("image", "factory") == "factory")
ran, run, died = install()
i_env = next(i for i, c in enumerate(ran) if c.startswith("printf"))
i_fmt = next(i for i, c in enumerate(ran) if "ubiformat /dev/" in c)
check("a good unit: the boot command is written before slot 0 is formatted, then it restarts",
      died is None and i_env < i_fmt and ran[-1] == "reboot", (died, i_env, i_fmt, ran[-1]))
check("slot 0 is formatted under the name the RAM installer has for it (ubi, mtd0)",
      "ubidetach -p /dev/mtd0" in ran[i_fmt] and "ubiformat /dev/mtd0 -y -q -f /tmp/o105/factory.ubi" in ran[i_fmt],
      ran[i_fmt])
payload = ran[i_env]
check("the boot command sets OpenWrt's arguments, tries OpenWrt, then Arista; slot 1 is made Arista's",
      "bootcmd setenv bootargs ${owrt_args}; run owrt_boot; run arista_boot" in payload
      and "\npri 1\n" in payload and "owrt_args console=ttyMSM0,115200n8\n" in payload, payload)
check("OpenWrt is read from the first 64 MiB of the NAND, to 0x84000000",
      "mtdparts=nand0:64m(ubi)" in payload and "ubi read 0x84000000 kernel && bootm 0x84000000" in payload)
check("one environment write and one flash write are counted",
      len(run.env_writes) == 1 and run.flash_writes == ["NAND slot 0 (ubiformat)"])
ran, run, died = install(slot1_magic="ffffffff")
check("slot 1 without a UBI: refused before anything is written",
      died and "nothing was written" in died and not any(c.startswith("printf") or "ubiformat /dev/" in c for c in ran),
      died)
ran, run, died = install(rootfs_sha="0" * 64)
check("a volume that does not read back stops, and says Arista still boots",
      died and "rootfs volume" in died and "slot 1" in died and ran[-1] != "reboot", died)
check("the question before an install says this tool has not installed an O-105 yet",
      "has not installed an O-105" in M.get("unproven", ""))


# --- back to Arista ----------------------------------------------------------------

def revert():
    sent = []
    env = ("bootcmd=%s\npri=1\nubootscript=x\nba0=A0\nba1=A1\nowrt_args=a\nowrt_boot=b\narista_boot=c\n"
           "mtdids=nand0=nand0\n" % own)
    state = {"written": False}

    def ssh_exec(con, cmd, timeout=0):
        if cmd.startswith("printf"):
            sent.append(cmd)
            state["written"] = True
            return "", 0
        if cmd.startswith("fw_printenv bootcmd"):
            return "bootcmd=run ubootscript\n" if state["written"] else "bootcmd=x\n", 0
        if cmd.startswith("fw_printenv"):
            return env, 0
        return "", 0

    class Con:
        def close(self):
            pass

    saved = (af.ssh_open, af.ssh_exec)
    af.ssh_open = lambda *a, **k: Con()
    af.ssh_exec = ssh_exec
    run = Run()
    try:
        af.do_restore_bootcmd(run, {"ip": "192.0.2.9"}, M)
    finally:
        af.ssh_open, af.ssh_exec = saved
    return sent[0] if sent else "", run


payload, run = revert()
check("back to Arista: its own boot command, slot 1, and slot 1's arguments",
      "bootcmd run ubootscript" in payload and "\npri 1\n" in payload and "\nbootargs A1\n" in payload, payload)
check("pri is not removed again, and the install's variables are",
      payload.count("\npri") == 1 and all(("\n%s\n" % n) in payload
                                          for n in ("owrt_args", "owrt_boot", "arista_boot", "mtdids")), payload)

print("")
print("%d failed" % len(FAILED) if FAILED else "all O-105 checks pass")
sys.exit(1 if FAILED else 0)

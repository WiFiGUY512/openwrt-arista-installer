"""Checks for a network boot that starts from OpenWrt.  No AP and no network.

    python test-netboot-home.py

Two things have to hold.  A fetch that fails must leave the AP starting OpenWrt
again, not the copy of Arista in its other slot: the AP's own boot command is
kept word for word in owrt_home, and what the netboot saves before it fetches
is "run owrt_home".  And the C-130, whose boot loader sometimes sets its
network port up wrong at a restart, sends 'ethspeed 5 autoneg' once before the
fetch, which redoes that setup.

The AP here is a dictionary for its U-Boot environment and a stand-in for the
SSH session that runs fw_setenv and fw_printenv on it.
"""
import importlib.util
import os
import shlex
import sys

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


class Log:
    def write(self, text):
        pass


class Tftp:
    def __init__(self, files):
        self.files = files


def die(msg, hint=None):
    raise Died(str(msg))


SAID = []
af.say = lambda msg="": SAID.append(str(msg))
af.tell = lambda msg="": SAID.append(str(msg))
af.note = lambda msg="": SAID.append("NOTE: " + str(msg))
af.die = die
af.LOG = Log()
af.Tftp = Tftp
af.time.sleep = lambda s: None

C130_OWN = "run owrt_boot; run arista_boot"
C360_OWN = "setenv bootargs ${owrt_args}; run owrt_boot; run arista_boot"
W318_STOCK = af.MODELS["w318"]["stock_bootcmd"]


class Ap:
    """An AP on OpenWrt: its environment, and what was sent to it."""

    def __init__(self, env, fail_on=None, garble=None):
        self.env = dict(env)
        self.sent = []              # every command, in order
        self.writes = []            # (name, value) as fw_setenv stored them
        self.rebooted = False
        self.fail_on = fail_on      # a variable whose fw_setenv fails
        self.garble = garble        # a variable that is stored wrong

    def ssh_exec(self, con, cmd, timeout=0):
        self.sent.append(cmd)
        words = shlex.split(cmd.split(" 2>")[0].split(" | ")[0])
        if words[0] == "fw_setenv":
            name, value = words[1], words[2]
            if name == self.fail_on:
                return "Error: can't write fw_env to flash", 1
            self.env[name] = value + "X" if name == self.garble else value
            self.writes.append((name, value))
            return "", 0
        if words[:2] == ["fw_printenv", "-n"]:
            return (self.env[words[2]] + "\n", 0) if words[2] in self.env else ("", 1)
        if words[0] == "fw_printenv":
            return "%s=%s\n" % (words[1], self.env.get(words[1], "")), 0
        if "reboot" in cmd:
            self.rebooted = True
            return "", 0
        return "", 0


class Run:
    def __init__(self):
        self.env_writes, self.flash_writes = [], []
        self.yes = True             # the one question was answered earlier in the run
        self.shell = object()
        self.tftp = None


def start(model, env, **ap_args):
    """boot_installer from OpenWrt."""
    ap = Ap(env, **ap_args)
    run = Run()
    asked = []

    def ask(prompt, hint):
        asked.append(prompt)
        return ""

    af.ssh_exec, af.ask = ap.ssh_exec, ask
    af.STEP["hint"] = "the step's own hint"
    del SAID[:]
    died = None
    try:
        af.boot_installer(run, {"model": model, "host": "192.0.2.9"}, "192.0.2.1", "192.0.2.230",
                          "/nowhere/%s.itb" % model, "openwrt")
    except Died as e:
        died = str(e)
    return ap, run, asked, died


def after_failed_fetch(env):
    """What the AP starts once the fetch has failed: owrt_clear has run and
    saved, and the next boot runs bootcmd.  Follows 'run <name>' to the text."""
    clear = env["owrt_clear"]
    assert clear.startswith("setenv bootcmd ") and clear.endswith("; saveenv"), clear
    cmd = clear[len("setenv bootcmd "):-len("; saveenv")]
    cmd = cmd[1:-1] if cmd[0] in "\"'" else cmd
    return env[cmd[4:]] if cmd.startswith("run ") and cmd[4:] in env and cmd != "run ubootscript" else cmd


# --- the variables themselves ----------------------------------------------------------
M = af.MODELS["c130"]


def nb(mdl, home=None, itb="x.itb"):
    return af.netboot_env("192.0.2.1", "192.0.2.230", itb, mdl["fit_cfg"],
                          mdl.get("load_addr", "0x60000000"), mdl.get("stock_bootcmd", "run ubootscript"),
                          mdl.get("fdt_high", "0x4A3E0000"), mdl.get("net_settle", 5),
                          mdl.get("net_tries", 2), mdl.get("retry_sleep", 3), mdl.get("netretry", "no"),
                          mdl.get("net_helper", False), home=home, net_prep=mdl.get("net_prep"))


plain, homed = nb(M), nb(M, home=C130_OWN)
check("from Arista nothing changes: no owrt_home, and Arista's boot command is what is saved",
      [n for n, _ in plain] == ["owrt_clear", "owrt_try", "owrt_nb", "bootcmd"]
      and dict(plain)["owrt_clear"] == 'setenv bootcmd "run ubootscript"; saveenv', plain)
check("the W-318's own boot command still goes back inside single quotes",
      dict(nb(af.MODELS["w318"]))["owrt_clear"] == "setenv bootcmd '%s'; saveenv" % W318_STOCK)
check("from OpenWrt: owrt_home first, bootcmd last, and what is saved is 'run owrt_home'",
      [n for n, _ in homed] == ["owrt_home", "owrt_clear", "owrt_try", "owrt_nb", "bootcmd"]
      and dict(homed)["owrt_home"] == C130_OWN
      and dict(homed)["owrt_clear"] == 'setenv bootcmd "run owrt_home"; saveenv', homed)
check("the fetch and the netboot's own boot command are the same either way",
      all(dict(plain)[n] == dict(homed)[n] for n in ("owrt_try", "owrt_nb", "bootcmd")))
hard = dict(nb(af.MODELS["c360"], home=C360_OWN))
check("a boot command with ${...} and ';' in it is never pasted into a U-Boot line",
      hard["owrt_home"] == C360_OWN and "${" not in hard["owrt_clear"]
      and hard["owrt_clear"] == 'setenv bootcmd "run owrt_home"; saveenv', hard)

# --- reading what the AP has -----------------------------------------------------------


def own(env, stock="run ubootscript"):
    ap = Ap(env)
    af.ssh_exec = ap.ssh_exec
    how = af.own_boot_command(object(), stock)
    return how, ap


how, ap = own({"bootcmd": C130_OWN})
check("an installed AP's boot command is the one to keep", how == ("write", C130_OWN), how)
check("finding that out writes nothing", not ap.writes and not ap.rebooted)
check("an AP that still has Arista's boot command keeps Arista's fallback, with nothing to say",
      own({"bootcmd": "run ubootscript"})[0] == ("arista", "")
      and own({"bootcmd": W318_STOCK}, W318_STOCK)[0] == ("arista", ""))
check("a netboot left by an earlier run is not taken for the AP's own boot command",
      own({"bootcmd": "run owrt_clear; run owrt_nb"})[0][0] == "arista")
check("an AP that came back through owrt_home: what owrt_home holds is kept, not written again",
      own({"bootcmd": "run owrt_home", "owrt_home": C130_OWN})[0] == ("kept", C130_OWN))
check("owrt_home is never made to run itself",
      own({"bootcmd": "run owrt_home", "owrt_home": "run owrt_home"})[0][0] == "arista"
      and own({"bootcmd": "run owrt_home"})[0][0] == "arista"
      and own({"bootcmd": "run owrt_home; run arista_boot"})[0][0] == "arista")
check("a boot command that cannot be read, or is more than one line, is not kept",
      own({})[0][0] == "arista" and own({"bootcmd": "a\nb"})[0][0] == "arista")

# --- the C-130's network port ----------------------------------------------------------
PREP = "ethspeed 5 autoneg; "
nbv = dict(plain)["owrt_nb"]
check("the C-130 redoes its network port's setup once: after the addresses, before the first fetch",
      nbv.count(PREP) == 1 and nbv.index("setenv serverip") < nbv.index(PREP) < nbv.index("run owrt_try")
      and PREP not in dict(plain)["owrt_try"] and PREP not in dict(plain)["owrt_clear"], nbv)
check("it is the same from OpenWrt as from Arista, and no other model sends it",
      dict(homed)["owrt_nb"] == nbv
      and all("ethspeed" not in v for k in af.MODELS if k != "c130" for _n, v in nb(af.MODELS[k])),
      [k for k in af.MODELS if af.MODELS[k].get("net_prep")])
check("only the C-130 has such a command", [k for k, v in af.MODELS.items() if v.get("net_prep")] == ["c130"])

# --- the C-130 from OpenWrt ------------------------------------------------------------
ENV = {"bootcmd": C130_OWN, "pri": "1", "owrt_boot": "x", "arista_boot": "y"}
ap, run, asked, died = start("c130", ENV)
check("it finishes, asks nothing, and restarts the AP", died is None and not asked and ap.rebooted, died)
check("the AP's boot command is saved first, word for word, and the netboot's comes last",
      [n for n, _ in ap.writes] == ["owrt_home", "owrt_clear", "owrt_try", "owrt_nb", "bootcmd"]
      and ap.env["owrt_home"] == C130_OWN and ap.env["bootcmd"] == "run owrt_clear; run owrt_nb", ap.writes)
check("what is on the AP is what netboot_env gives, with the port's setup in it",
      ap.writes == nb(M, home=C130_OWN, itb="c130.itb") and PREP in ap.env["owrt_nb"], ap.env["owrt_nb"])
check("a failed fetch would start OpenWrt's own boot command again", after_failed_fetch(ap.env) == C130_OWN,
      after_failed_fetch(ap.env))
check("the run counts five environment writes and knows where a failed fetch leads",
      run.env_writes == ["owrt_home", "owrt_clear", "owrt_try", "owrt_nb", "bootcmd"]
      and run.netboot_fallback == "openwrt")
check("if the installer never answers, the advice says the AP is back on OpenWrt",
      "gone back to OpenWrt" in af.STEP["hint"], af.STEP["hint"])

# --- the other models from OpenWrt -----------------------------------------------------
for model, cmd in (("w118", "setenv bootargs ${owrt_args}; run owrt_boot; run arista_boot"),
                   ("c200", "setenv bootargs ${owrt_args}; run owrt_boot; run arista_boot"),
                   ("w318", "run owrt_boot; run arista_boot")):
    ap, run, asked, died = start(model, {"bootcmd": cmd})
    check("the %s: a failed fetch returns to OpenWrt"
          % af.MODELS[model]["name"],
          died is None and not asked and ap.rebooted and ap.env["owrt_home"] == cmd
          and after_failed_fetch(ap.env) == cmd, (asked, ap.env.get("owrt_home")))
odd = "setenv x 'a b'; echo \"${x}\"; run owrt_boot"
ap, run, asked, died = start("w118", {"bootcmd": odd})
check("a boot command with both kinds of quotes in it is kept word for word",
      died is None and ap.env["owrt_home"] == odd and after_failed_fetch(ap.env) == odd, ap.env.get("owrt_home"))

# --- an AP that came back through owrt_home, and one still on Arista's command ---------
ap, run, asked, died = start("w118", {"bootcmd": "run owrt_home", "owrt_home": C360_OWN})
check("second run after a failed fetch: owrt_home is left as it is, and still leads to OpenWrt",
      died is None and "owrt_home" not in [n for n, _ in ap.writes]
      and ap.writes == nb(af.MODELS["w118"], home=C360_OWN, itb="w118.itb")[1:]
      and ap.env["owrt_home"] == C360_OWN and after_failed_fetch(ap.env) == C360_OWN, ap.writes)
ap, run, asked, died = start("w118", {"bootcmd": "run ubootscript"})
check("an AP whose boot command is Arista's: exactly what was written before this change",
      died is None and ap.writes == nb(af.MODELS["w118"], itb="w118.itb")
      and run.netboot_fallback == "arista", ap.writes)
ap, run, asked, died = start("w118", {"bootcmd": "run owrt_clear; run owrt_nb"})
check("a boot command that cannot be kept: Arista's fallback, and a note saying where that leads",
      died is None and "owrt_home" not in ap.env and any(s.startswith("NOTE:") and "Arista" in s for s in SAID))

# --- what must stop it before the boot command changes ---------------------------------
ap, run, asked, died = start("c130", ENV, garble="owrt_home")
check("owrt_home not reading back as saved: stops, the boot command is untouched",
      died is not None and ap.env["bootcmd"] == C130_OWN and not asked and not ap.rebooted, died)
ap, run, asked, died = start("c130", ENV, fail_on="owrt_nb")
check("a write that fails: stops, the boot command is untouched",
      died is not None and "not changed" in died and ap.env["bootcmd"] == C130_OWN
      and not asked and not ap.rebooted, died)

print()
if FAILED:
    print("%d FAILED: %s" % (len(FAILED), "; ".join(FAILED)))
    sys.exit(1)
print("all netboot-from-OpenWrt checks pass")

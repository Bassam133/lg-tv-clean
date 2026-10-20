#!/usr/bin/env python3
"""Connect to an LG webOS TV on your own network, pair with it, and report what it finds.

    python3 lgtv_connect.py --scan              # find TVs (needs port 3001 open)
    python3 lgtv_connect.py --host 192.168.1.50 # connect + pair + report

The TV shows an "Allow this device?" prompt the first time: accept it on the TV.
Nothing is changed on the TV by this script: it only reads.
"""
import argparse
import asyncio
import concurrent.futures as cf
import ipaddress
import json
import os
import socket
import ssl
import sys
import time

try:
    import websockets
except ImportError:
    sys.exit("Missing dependency. Run:  pip3 install websockets")

HERE = os.path.dirname(os.path.abspath(__file__))
KEY_FILE = os.path.join(HERE, ".lgtv-key.json")
REPORT = os.path.join(HERE, "lgtv-report.txt")
PORT = 3001

# Unsigned on purpose: newer LG firmware rejects the signed manifest that open-source
# clients share, but still accepts an unsigned one.
PERMISSIONS = [
    "LAUNCH", "LAUNCH_WEBAPP", "APP_TO_APP", "CLOSE", "TEST_OPEN", "TEST_PROTECTED",
    "CONTROL_AUDIO", "CONTROL_DISPLAY", "READ_APP_STATUS", "READ_CURRENT_CHANNEL",
    "READ_INPUT_DEVICE_LIST", "READ_NETWORK_STATE", "READ_RUNNING_APPS",
    "READ_TV_CHANNEL_LIST", "READ_POWER_STATE", "READ_COUNTRY_INFO",
    "READ_INSTALLED_APPS", "READ_SETTINGS", "READ_STORAGE_DEVICE_LIST",
]


def insecure_ctx():
    # The TV serves a certificate from LG's own CA, so hostname/chain checks are off.
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    return ctx


# ----------------------------------------------------------------------------- scan
def port_open(ip, timeout=0.35):
    s = socket.socket()
    s.settimeout(timeout)
    try:
        s.connect((str(ip), PORT))
        return str(ip)
    except Exception:
        return None
    finally:
        s.close()


def local_subnets():
    """Every /24 the machine could be on, without needing extra packages."""
    nets = []
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            ip = ipaddress.ip_address(info[4][0])
            if not ip.is_loopback and not ip.is_link_local:
                nets.append(ipaddress.ip_network(f"{ip}/24", strict=False))
    except Exception:
        pass
    # UDP connect trick: reveals the address the default route would use
    for probe in [("8.8.8.8", 53), ("192.168.1.1", 53)]:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            s.connect(probe)
            ip = ipaddress.ip_address(s.getsockname()[0])
            if not ip.is_loopback:
                nets.append(ipaddress.ip_network(f"{ip}/24", strict=False))
        except Exception:
            pass
        finally:
            s.close()
    uniq = []
    for n in nets:
        if n not in uniq:
            uniq.append(n)
    return uniq


def scan():
    nets = local_subnets()
    if not nets:
        print("Could not work out a local subnet. Pass the TV IP with --host instead.")
        print("On the TV: Settings > Connection > Wi-Fi Connection > (your network) > Advanced.")
        return 1
    hosts = [str(h) for n in nets for h in n.hosts()]
    print(f"Scanning {len(hosts)} addresses on {', '.join(str(n) for n in nets)} for port {PORT}...")
    t0 = time.time()
    found = []
    with cf.ThreadPoolExecutor(max_workers=128) as ex:
        for r in ex.map(port_open, hosts):
            if r:
                found.append(r)
    print(f"Done in {time.time() - t0:.1f}s.")
    if not found:
        print("No TV answered on port 3001.")
        print("  - Is the TV on and on the same Wi-Fi as this computer?")
        print("  - Mobile Hotspot / guest network / VLAN separation will hide it.")
        return 1
    for ip in found:
        print(f"  FOUND: {ip}  (likely your LG TV)")
    print("\nNext:  python3 lgtv_connect.py --host " + found[0])
    return 0


# -------------------------------------------------------------------------- connect
def load_keys():
    try:
        with open(KEY_FILE) as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


async def session(host, log):
    keys = load_keys()
    url = f"wss://{host}:{PORT}"
    log(f"connecting to {url}")
    ws = await websockets.connect(url, ssl=insecure_ctx(), open_timeout=10, max_size=None,
                                  ping_interval=10, ping_timeout=10)
    payload = {
        "forcePairing": False,
        "pairingType": "PROMPT",
        "manifest": {"manifestVersion": 1, "appVersion": "1.1", "permissions": PERMISSIONS},
    }
    if host in keys:
        payload["client-key"] = keys[host]
        log("reusing the saved pairing key")
    else:
        log("first run: accept the prompt on the TV screen")
    await ws.send(json.dumps({"type": "register", "id": "register_0", "payload": payload}))
    while True:
        msg = json.loads(await asyncio.wait_for(ws.recv(), 90))
        if msg.get("type") == "registered":
            break
        if msg.get("type") == "error":
            await ws.close()
            raise RuntimeError(f"pairing rejected: {msg.get('error')}")
    key = msg["payload"]["client-key"]
    if keys.get(host) != key:
        keys[host] = key
        with open(KEY_FILE, "w") as f:
            json.dump(keys, f, indent=1)
        os.chmod(KEY_FILE, 0o600)
        log("pairing key saved to .lgtv-key.json")
    return ws


_n = [0]


async def call(ws, uri, payload=None, timeout=20):
    _n[0] += 1
    rid = f"c{_n[0]}"
    await ws.send(json.dumps({"type": "request", "id": rid, "uri": uri, "payload": payload or {}}))
    while True:
        msg = json.loads(await asyncio.wait_for(ws.recv(), timeout))
        if msg.get("id") == rid:
            return msg.get("payload", {})


async def report(host, log):
    ws = await session(host, log)
    out = {}
    try:
        for label, uri, payload in [
            ("system", "ssap://com.webos.service.tv.systemproperty/getSystemInfo",
             {"keys": ["modelName", "firmwareVersion", "sdkVersion", "boardType", "UHD", "modelGroup"]}),
            ("power", "ssap://com.webos.service.tvpower/power/getPowerState", None),
            ("inputs", "ssap://tv/getExternalInputList", None),
            ("apps", "ssap://com.webos.applicationManager/listLaunchPoints", None),
            ("foreground", "ssap://com.webos.applicationManager/getForegroundAppInfo", None),
        ]:
            try:
                out[label] = await call(ws, uri, payload)
            except Exception as e:
                out[label] = {"__error": f"{type(e).__name__}: {e}"}

        sysinfo = out.get("system", {})
        if isinstance(sysinfo, dict) and "settings" in sysinfo:
            sysinfo = dict(sysinfo)
        print("\n================ TV REPORT ================")
        print(f"host            : {host}")
        for k in ("modelName", "modelGroup", "firmwareVersion", "sdkVersion", "boardType"):
            if isinstance(sysinfo, dict) and sysinfo.get(k) is not None:
                print(f"{k:16s}: {sysinfo.get(k)}")
        p = out.get("power", {})
        if isinstance(p, dict):
            print(f"power           : {p.get('state')}")
        fg = out.get("foreground", {})
        if isinstance(fg, dict):
            print(f"foreground app  : {fg.get('appId')}")
        apps = out.get("apps", {}).get("launchPoints") if isinstance(out.get("apps"), dict) else None
        if apps is not None:
            print(f"installed apps  : {len(apps)}")
            hidden = sum(1 for a in apps if a.get("hidden"))
            print(f"  visible/hidden: {len(apps) - hidden}/{hidden}")
        ins = out.get("inputs", {}).get("devices") if isinstance(out.get("inputs"), dict) else None
        if ins is not None:
            print(f"inputs          : {len(ins)}")
            for d in ins:
                print(f"  - {d.get('label') or d.get('id')}: {d.get('appId')}")
        print("===========================================")
    finally:
        await ws.close()
    with open(REPORT, "w", encoding="utf-8") as f:
        f.write(f"# LG webOS TV report for {host}\n")
        f.write(json.dumps(out, indent=1, ensure_ascii=False, default=str))
    print(f"\nFull raw report written to: {REPORT}")
    print("Send that file back to get the exact debloat/cleanup plan for this model.")
    return 0


def main():
    ap = argparse.ArgumentParser(description="Connect to an LG webOS TV on the local network.")
    ap.add_argument("--scan", action="store_true", help="find LG TVs on the local network")
    ap.add_argument("--host", help="the TV's IP address")
    ap.add_argument("--quiet", action="store_true", help="only print the report")
    a = ap.parse_args()
    log = (lambda *x: None) if a.quiet else (lambda *x: print("  ", *x, flush=True))

    if a.scan:
        return scan()
    host = a.host or os.environ.get("TV_HOST")
    if not host:
        ap.print_help()
        print("\nNo --host given. Try --scan first.")
        return 2
    try:
        return asyncio.run(report(host, log))
    except asyncio.TimeoutError:
        print("\nFAILED: the TV did not answer in time.")
        print("  If a prompt is waiting on the TV screen, accept it, then run again.")
        return 1
    except (OSError, RuntimeError) as e:
        print(f"\nFAILED: {type(e).__name__}: {e}")
        print("  - Confirm the IP and that the TV is on and on this Wi-Fi.")
        print("  - 'Connection refused' usually means a different device answers on 3001.")
        return 1
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())

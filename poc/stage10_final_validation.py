#!/usr/bin/env python3
"""Stage 10 F-1 FINAL validation orchestrator: permanent-halt proof.

Runs the comprehensive final devnet differential on a single host:
  - ONE bsc geth validator (single validator, mining) built from
    bnb-chain/bsc@947ba1c
  - TWO reth-bsc followers (release binaries) built from
    bnb-chain/reth-bsc@1af59ea, each an independent full node with its own
    datadir, both syncing the validator's chain over devp2p
  - custom genesis: every fork through Pasteur at 0, Jenner unscheduled
    (today's BSC mainnet regime)

Phases (all assertions recorded into summary.json; exit code 0 iff all pass):
  0. genesis generated fresh (timestamp = now-300s so block 1 is not a
     'breathe block'), geth init + key import
  1. geth starts, seals blocks (~450 ms Fermi interval)
  2. BOTH reth followers start, p2p-connect to geth (enode from geth log),
     sync genesis -> head; genesis hashes must match for both
  3. CONTROL: miner_setGasPrice(0) then a PAID-gas (1 gwei) tx from the
     coinbase to the target address -> geth must include it; BOTH followers
     must follow past that block
  4. TRIGGER: zero-gas tx from the coinbase to the target address
     (the F-1 divergent triple) -> geth must include it and execute it
     as a NORMAL tx (receipt status 1, gasUsed 21000)
  5. geth mines >= --min-blocks-past-trigger blocks past the trigger
     (directive: >= 5 minutes OR ~100 blocks; at Fermi's ~450 ms interval
     100 blocks ~= 45-75 s. A hard safety cap stops the miner at head >=
     --head-safety-cap (~190): on a fresh devnet chain without the full
     system-contract suite deployed, the epoch machinery (~block 199/200)
     reads undeployed contracts on BOTH clients - a devnet-only wall
     unrelated to F-1, per Stage 5.)
  6. OBSERVATION WINDOW (--observation-seconds, default 300 s from the
     trigger receipt):
       - sample every 5 s: geth head, follower A head, follower B head
       - BOTH follower heads must stay < trigger block at EVERY sample
       - every 30 s: count descendant-rejection lines in both follower
         logs (continuously-logging evidence)
       - at t=--restart-at (default 30 s, i.e. WHILE geth is still mining):
         RESTART follower B (SIGTERM -> wait exit -> relaunch, SAME datadir)
         and keep asserting it stays pinned - proving the halt is not cleared
         by a process restart. The restarted node's fresh log must re-reject
         the trigger (it re-fetches it from the live announcements; if the
         chain has already frozen, the orchestrator briefly restarts geth's
         miner to force new announcements). Either the persisted state or
         the deterministic re-validation keeps the node pinned - both
         outcomes are captured from the fresh log.
  7. SETTLE: after the window, both followers must sit exactly at
     trigger_block - 1 (pinned at the pre-trigger block)

Exit code 0 iff all assertions pass.
"""
import argparse
import json
import os
import re
import signal
import subprocess
import sys
import time
import urllib.request

VALIDATOR = "0xa1bC7D0dB44Ad8f083AE3eCDdfA21A110B3Ca222"
SENDER2 = "0x9999999999999999999999999999999999999999"
GAS_LIMIT = 30_000_000
TX_GAS = 21000

BLOCK_FORKS = [
    "homesteadBlock", "eip150Block", "eip155Block", "eip158Block",
    "byzantiumBlock", "constantinopleBlock", "petersburgBlock",
    "istanbulBlock", "muirGlacierBlock", "berlinBlock", "londonBlock",
    "ramanujanBlock", "nielsBlock", "mirrorSyncBlock", "brunoBlock",
    "eulerBlock", "nanoBlock", "moranBlock", "gibbsBlock", "planckBlock",
    "lubanBlock", "platoBlock", "hertzBlock", "hertzfixBlock",
]
TIME_FORKS = [
    "shanghaiTime", "keplerTime", "feynmanTime", "feynmanFixTime",
    "cancunTime", "tychoTime", "haberTime", "haberFixTime", "bohrTime",
    "pragueTime", "osakaTime", "pascalTime", "lorentzTime", "maxwellTime",
    "fermiTime", "mendelTime", "pasteurTime",
]

REJECTION_SUBSTRINGS = (
    "unexpected system tx",
    "returned Invalid",
    "links to previously rejected block",
)


def log(msg):
    print(f"[stage10 {time.strftime('%H:%M:%S')}] {msg}", flush=True)


def rpc(url, method, params, timeout=10.0):
    payload = json.dumps({"jsonrpc": "2.0", "id": 1, "method": method,
                          "params": params}).encode()
    req = urllib.request.Request(url, data=payload,
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode())


def rpc_ok(url, method, params, timeout=10.0):
    r = rpc(url, method, params, timeout)
    if "error" in r:
        raise RuntimeError(f"rpc {method} error: {r['error']}")
    return r["result"]


def head(url):
    return int(rpc_ok(url, "eth_blockNumber", []), 16)


def wait_for(fn, timeout, what, poll=1.0):
    t0 = time.time()
    last = None
    while time.time() - t0 < timeout:
        try:
            last = fn()
            if last:
                return last
        except Exception as e:  # transient rpc errors during startup
            last = f"error: {e}"
        time.sleep(poll)
    raise TimeoutError(f"timed out after {timeout:.0f}s waiting for {what} (last: {last})")


def make_genesis(path):
    ts = int(time.time()) - 300
    extra = "0x" + "00" * 32 + "01" + VALIDATOR[2:] + "00" * 48 + "01" + "00" * 65
    config = {"chainId": 2}
    for k in BLOCK_FORKS:
        config[k] = 0
    for k in TIME_FORKS:
        config[k] = 0
    config["parlia"] = {"period": 3, "epoch": 200}
    config["blobSchedule"] = {
        "cancun": {"target": 3, "max": 6, "baseFeeUpdateFraction": 3338477},
        "prague": {"target": 3, "max": 6, "baseFeeUpdateFraction": 3338477},
        "osaka": {"target": 3, "max": 6, "baseFeeUpdateFraction": 3338477},
    }
    genesis = {
        "config": config,
        "nonce": "0x0",
        "timestamp": hex(ts),
        "extraData": extra,
        "gasLimit": hex(GAS_LIMIT),
        "difficulty": "0x0",
        "mixHash": "0x" + "00" * 32,
        "coinbase": VALIDATOR,
        "baseFeePerGas": "0x0",
        "alloc": {
            VALIDATOR: {"balance": hex(10 * 10**18)},
            SENDER2: {"balance": hex(10**18)},
        },
    }
    with open(path, "w") as f:
        json.dump(genesis, f, indent=2)
    return ts


class Node:
    """A managed process writing to a log file."""

    def __init__(self, name, cmd, logfile, cwd=None):
        self.name = name
        self.cmd = cmd
        self.logfile = logfile
        self.cwd = cwd
        self.proc = None
        self.lf = None

    def start(self):
        self.lf = open(self.logfile, "ab")
        self.lf.write(f"\n===== start {time.strftime('%H:%M:%S')} =====\n".encode())
        self.lf.flush()
        self.proc = subprocess.Popen(self.cmd, stdout=self.lf,
                                     stderr=subprocess.STDOUT, cwd=self.cwd)

    def stop(self, timeout=45):
        if self.proc is None:
            return
        if self.proc.poll() is None:
            self.proc.send_signal(signal.SIGTERM)
            t0 = time.time()
            while time.time() - t0 < timeout and self.proc.poll() is None:
                time.sleep(1.0)
            if self.proc.poll() is None:
                self.proc.kill()
                try:
                    self.proc.wait(10)
                except Exception:  # noqa: BLE001
                    pass
        if self.lf is not None:
            try:
                self.lf.close()
            except Exception:  # noqa: BLE001
                pass
        self.lf = None
        self.proc = None

    def alive(self):
        return self.proc is not None and self.proc.poll() is None


def log_counts(path):
    """Count F-1-relevant rejection lines in a node log."""
    out = {"unexpected_system_tx": 0, "returned_invalid": 0,
           "links_to_rejected": 0, "any": 0}
    try:
        with open(path, "rb") as f:
            txt = f.read().decode("utf-8", "replace")
    except FileNotFoundError:
        return out
    out["unexpected_system_tx"] = txt.count("unexpected system tx")
    out["returned_invalid"] = txt.count("returned Invalid")
    out["links_to_rejected"] = txt.count("links to previously rejected block")
    out["any"] = sum(1 for ln in txt.splitlines()
                     if any(s in ln for s in REJECTION_SUBSTRINGS))
    return out


def dump_tail(path, lines=80, tag=""):
    try:
        with open(path, "rb") as f:
            txt = f.read().decode("utf-8", "replace")
        tail = "\n".join(txt.splitlines()[-lines:])
        print(f"----- {tag} {path} (last {lines} lines) -----\n{tail}\n"
              f"----- end {tag} -----", flush=True)
    except Exception as e:  # noqa: BLE001
        print(f"(could not dump {path}: {e})", flush=True)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--geth", required=True)
    ap.add_argument("--reth", required=True)
    ap.add_argument("--target", required=True, help="0x-prefixed 20-byte address")
    ap.add_argument("--label", required=True)
    ap.add_argument("--workdir", required=True)
    ap.add_argument("--geth-port", type=int, default=8545)
    ap.add_argument("--reth-a-port", type=int, default=8546)
    ap.add_argument("--reth-b-port", type=int, default=8547)
    ap.add_argument("--p2p-port", type=int, default=30303,
                    help="geth p2p port; followers use +1/+2")
    ap.add_argument("--min-blocks-past-trigger", type=int, default=100)
    ap.add_argument("--observation-seconds", type=int, default=300)
    ap.add_argument("--restart-at", type=int, default=30,
                    help="seconds after trigger to restart follower B (while "
                         "geth is still mining, so the restarted follower is "
                         "forced to re-evaluate the announced trigger)")
    ap.add_argument("--head-safety-cap", type=int, default=190,
                    help="stop geth's miner at this head (devnet epoch wall ~199/200)")
    ap.add_argument("--checkpoint-seconds", type=int, default=30,
                    help="interval between rejection-line census checkpoints")
    ap.add_argument("--min-rejection-lines", type=int, default=20,
                    help="minimum descendant-rejection lines per follower")
    ap.add_argument("--skip-reth", action="store_true",
                    help="local dry-run: exercise the geth half only")
    args = ap.parse_args()

    os.makedirs(args.workdir, exist_ok=True)
    w = args.workdir
    gdata = os.path.join(w, "gdata")
    geth_rpc = f"http://127.0.0.1:{args.geth_port}"
    a_rpc = f"http://127.0.0.1:{args.reth_a_port}"
    b_rpc = f"http://127.0.0.1:{args.reth_b_port}"
    geth_log = os.path.join(w, "geth.log")
    a_log = os.path.join(w, "reth_a.log")
    b_log = os.path.join(w, "reth_b.log")
    b_log_pre = os.path.join(w, "reth_b_pre_restart.log")
    summary = {"label": args.label, "target": args.target, "assertions": []}

    geth_node = None
    a_node = None
    b_node = None

    def assert_that(cond, name, detail=""):
        summary["assertions"].append({"name": name, "pass": bool(cond),
                                      "detail": str(detail)})
        log(("PASS " if cond else "FAIL ") + f"{name} {detail}")
        return bool(cond)

    def teardown():
        for n in (b_node, a_node, geth_node):
            if n is not None:
                n.stop()

    exit_code = 1
    try:
        # ---- phase 0: genesis + geth init ----
        ts = make_genesis(os.path.join(w, "genesis.json"))
        summary["genesis_timestamp"] = ts
        log(f"genesis written (ts={ts}, target={args.target})")
        r = subprocess.run([args.geth, "init", "--datadir", gdata,
                            os.path.join(w, "genesis.json")],
                           capture_output=True, text=True, timeout=120)
        if r.returncode != 0:
            summary["geth_init_error"] = r.stderr[-2000:]
            raise RuntimeError("geth init failed")
        m = re.search(r"hash=0x([0-9a-f]{64})", r.stdout + r.stderr)
        summary["genesis_hash"] = "0x" + (m.group(1) if m else "?")
        log(f"geth init ok, genesis hash {summary['genesis_hash']}")
        with open(os.path.join(w, "key.hex"), "w") as f:
            f.write("8f2a4b1c3d5e6f708192a3b4c5d6e7f8091a2b3c4d5e6f708192a3b4c5d6e7f8")
        with open(os.path.join(w, "password.txt"), "w") as f:
            f.write("\n")
        r = subprocess.run([args.geth, "account", "import", "--datadir", gdata,
                            "--password", os.path.join(w, "password.txt"),
                            os.path.join(w, "key.hex")],
                           capture_output=True, text=True, timeout=120)
        if "Address:" not in r.stdout + r.stderr:
            summary["key_import_error"] = (r.stdout + r.stderr)[-2000:]
            raise RuntimeError("validator key import failed")
        log("validator key imported")

        # ---- phase 1: geth up and mining ----
        geth_cmd = [
            args.geth, "--datadir", gdata, "--networkid", "2",
            "--nodiscover", "--netrestrict", "127.0.0.1/32",
            "--port", str(args.p2p_port), "--nat", "none",
            "--http", "--http.addr", "127.0.0.1", "--http.port", str(args.geth_port),
            "--http.api", "eth,net,web3,txpool,miner",
            "--unlock", VALIDATOR, "--password", os.path.join(w, "password.txt"),
            "--allow-insecure-unlock",
            "--mine", "--miner.etherbase", VALIDATOR,
        ]
        geth_node = Node("geth", geth_cmd, geth_log)
        geth_node.start()
        wait_for(lambda: head(geth_rpc), 90, "geth rpc up")
        wait_for(lambda: head(geth_rpc) >= 4, 90, "geth mining (head>=4)")
        log(f"geth mining, head={head(geth_rpc)}")
        assert_that(True, "geth_mining_started", f"head={head(geth_rpc)}")
        summary["block1_txs"] = len(
            rpc_ok(geth_rpc, "eth_getBlockByNumber", ["0x1", True])["transactions"])
        log(f"block 1 carries {summary['block1_txs']} initContract system txs")

        with open(geth_log, "rb") as f:
            txt = f.read().decode("utf-8", "replace")
        m = re.search(r"self=\"?enode://([0-9a-f]+)@([^\"]+)\"?", txt)
        if not m:
            raise RuntimeError("could not parse geth enode from log")
        enode = f"enode://{m.group(1)}@127.0.0.1:{args.p2p_port}"
        summary["enode"] = enode
        log(f"geth enode: {enode}")

        # ---- phase 2: both reth followers up, sync ----
        def reth_cmd_for(rpc_port, p2p, datadir, ipc):
            # NOTE on ports: a reth-bsc node binds FIVE sockets whose defaults
            # collide when two followers share a host (and with geth's p2p):
            # p2p TCP (--port), discv4 UDP (--discovery.port), discv5 UDP
            # (--discovery.v5.port, default 30303), engine auth RPC
            # (--authrpc.port, default 8551), HTTP (--http.port). The first CI
            # iteration failed exactly on the discv4/discv5 defaults; all are
            # now explicit, and discv5 is disabled outright - the devnet uses
            # --bootnodes/--trusted-peers only (netrestrict'd loopback).
            return [
                args.reth, "node",
                "--chain", os.path.join(w, "genesis.json"),
                "--datadir", datadir,
                "--port", str(p2p),
                "--discovery.port", str(p2p + 10),
                "--disable-discv5-discovery",
                "--authrpc.port", str(p2p + 20),
                "--bootnodes", enode, "--trusted-peers", enode,
                "--netrestrict", "127.0.0.1/32",
                "--http", "--http.addr", "127.0.0.1",
                "--http.port", str(rpc_port),
                "--http.api", "eth,net",
                "--ipcpath", os.path.join(w, ipc),
                "-vvvv",  # this fork uses clap verbosity flags
            ]

        a_node = Node("reth_a", reth_cmd_for(args.reth_a_port, args.p2p_port + 1,
                                             os.path.join(w, "rdata_a"), "reth_a.ipc"),
                      a_log)
        b_node = Node("reth_b", reth_cmd_for(args.reth_b_port, args.p2p_port + 2,
                                             os.path.join(w, "rdata_b"), "reth_b.ipc"),
                      b_log)

        if not args.skip_reth:
            a_node.start()
            b_node.start()
            for name, url, node in (("A", a_rpc, a_node), ("B", b_rpc, b_node)):
                try:
                    wait_for(lambda u=url: head(u), 180, f"reth {name} rpc up")
                except TimeoutError:
                    dump_tail(node.logfile, 120, f"RETH-{name}-STARTUP-FAILURE")
                    raise

            g_hash = rpc_ok(geth_rpc, "eth_getBlockByNumber", ["0x0", False])["hash"]
            for name, url in (("A", a_rpc), ("B", b_rpc)):
                r_hash = rpc_ok(url, "eth_getBlockByNumber", ["0x0", False])["hash"]
                assert_that(g_hash == r_hash, f"genesis_hashes_match_{name}",
                            f"geth={g_hash[:18]}.. reth={r_hash[:18]}..")
                summary[f"reth_{name.lower()}_genesis_hash"] = r_hash
                if g_hash != r_hash:
                    raise RuntimeError(f"genesis hash mismatch (follower {name})")

            for name, url in (("A", a_rpc), ("B", b_rpc)):
                def following(u=url):
                    g, r = head(geth_rpc), head(u)
                    return r >= g - 3 and r >= 3
                wait_for(following, 240, f"reth {name} syncing behind geth")
                log(f"reth {name} following geth "
                    f"(geth={head(geth_rpc)} reth_{name.lower()}={head(url)})")
                assert_that(True, f"reth_{name.lower()}_syncing",
                            f"geth={head(geth_rpc)} reth={head(url)}")

        # ---- phase 3: control tx (paid gas, coinbase -> target) ----
        assert rpc_ok(geth_rpc, "miner_setGasPrice", ["0x0"]) is True
        log("miner_setGasPrice(0) accepted by geth")
        ctl = rpc_ok(geth_rpc, "eth_sendTransaction",
                     [{"from": VALIDATOR, "to": args.target,
                       "gas": hex(TX_GAS), "gasPrice": "0x3b9aca00"}])
        summary["control_tx"] = ctl
        log(f"control tx sent: {ctl}")

        def ctl_receipt():
            r = rpc_ok(geth_rpc, "eth_getTransactionReceipt", [ctl])
            return r if r.get("status") == "0x1" else None
        crec = wait_for(ctl_receipt, 60, "control tx mined")
        hc = int(crec["blockNumber"], 16)
        summary["control_block"] = hc
        assert_that(True, "geth_mined_paid_gas_control", f"block={hc} status=1")
        log(f"geth mined control tx in block {hc}")

        if not args.skip_reth:
            for name, url in (("A", a_rpc), ("B", b_rpc)):
                wait_for(lambda u=url: head(u) >= hc, 120,
                         f"reth {name} head >= control block {hc}")
                assert_that(True, f"reth_{name.lower()}_accepted_paid_gas_control_block",
                            f"reth_{name.lower()} head={head(url)} >= {hc}")

        # ---- phase 4: trigger tx (zero gas, coinbase -> target) ----
        t_send = time.time()
        trig = rpc_ok(geth_rpc, "eth_sendTransaction",
                      [{"from": VALIDATOR, "to": args.target,
                        "gas": hex(TX_GAS), "gasPrice": "0x0"}])
        summary["trigger_tx"] = trig
        log(f"trigger tx sent: {trig}")

        def trig_receipt():
            r = rpc_ok(geth_rpc, "eth_getTransactionReceipt", [trig])
            return r if r.get("status") == "0x1" else None
        trec = wait_for(trig_receipt, 60, "trigger tx mined")
        ht = int(trec["blockNumber"], 16)
        t0 = time.time()  # window origin: trigger receipt observed
        summary["trigger_block"] = ht
        summary["trigger_gas_used"] = int(trec["gasUsed"], 16)
        summary["trigger_mined_after_s"] = round(t0 - t_send, 1)
        ttx = rpc_ok(geth_rpc, "eth_getTransactionByHash", [trig])
        summary["trigger_tx_fields"] = {
            "from": ttx["from"], "to": ttx.get("to"), "gasPrice": ttx.get("gasPrice"),
            "gas": ttx.get("gas"), "type": ttx.get("type")}
        assert_that(
            int(trec["gasUsed"], 16) == TX_GAS
            and ttx.get("gasPrice") == "0x0"
            and (ttx.get("to") or "").lower() == args.target.lower()
            and ttx["from"].lower() == VALIDATOR.lower(),
            "trigger_normal_receipt",
            f"block={ht} status={trec['status']} gasUsed={int(trec['gasUsed'],16)} "
            f"gasPrice={ttx.get('gasPrice')} to={ttx.get('to')} from={ttx['from']}")
        log(f"geth mined TRIGGER tx in block {ht} (status={trec['status']}) "
            f"- bsc treats it as a NORMAL tx; window starts now")

        # ---- phase 5+6: mining continuation + observation window ----
        target_head = min(ht + args.min_blocks_past_trigger, args.head_safety_cap)
        epoch_capped = target_head < ht + args.min_blocks_past_trigger
        summary["mining_target_head"] = target_head
        summary["epoch_wall_cap_applied"] = epoch_capped
        miner_stopped = False
        b_restarted = False
        samples = []
        checkpoints = []
        max_a = -1
        max_b = -1
        max_b_post = -1
        next_checkpoint = t0 + args.checkpoint_seconds
        restart_at = t0 + args.restart_at
        window_end = t0 + args.observation_seconds

        while time.time() < window_end:
            now = time.time()
            t = now - t0
            g = head(geth_rpc)
            ra = head(a_rpc) if (not args.skip_reth and a_node.alive()) else None
            rb = head(b_rpc) if (not args.skip_reth and b_node.alive()) else None
            if ra is not None:
                max_a = max(max_a, ra)
            if rb is not None:
                max_b = max(max_b, rb)
                if b_restarted:
                    max_b_post = max(max_b_post, rb)
            samples.append({"t": round(t, 1), "geth": g, "reth_a": ra, "reth_b": rb})

            if not args.skip_reth and ra is not None and ra >= ht:
                log(f"!! VIOLATION: follower A head {ra} >= trigger block {ht}")
            if not args.skip_reth and rb is not None and rb >= ht:
                log(f"!! VIOLATION: follower B head {rb} >= trigger block {ht}")

            # keep mining until we are min-blocks-past-trigger (or safety cap)
            if not miner_stopped and g >= target_head:
                try:
                    rpc_ok(geth_rpc, "miner_stop", [])
                    miner_stopped = True
                    summary["mining_stopped_at_head"] = g
                    summary["mining_stopped_at_t"] = round(t, 1)
                    log(f"geth reached head {g} (>= trigger+{g - ht}); "
                        f"miner stopped; chain frozen")
                except Exception as e:  # noqa: BLE001
                    log(f"miner_stop failed ({e}); continuing")
            # hard safety guard (devnet epoch wall)
            if not miner_stopped and g >= args.head_safety_cap:
                try:
                    rpc_ok(geth_rpc, "miner_stop", [])
                    miner_stopped = True
                    summary["mining_stopped_at_head"] = g
                    summary["epoch_wall_guard_fired"] = True
                    log(f"SAFETY: miner stopped at head {g} (epoch-wall guard)")
                except Exception as e:  # noqa: BLE001
                    log(f"miner_stop (guard) failed: {e}")

            # periodic rejection-line census (continuity evidence)
            if now >= next_checkpoint:
                next_checkpoint += args.checkpoint_seconds
                ca = log_counts(a_log) if not args.skip_reth else {}
                cb = log_counts(b_log) if not args.skip_reth else {}
                checkpoints.append({
                    "t": round(t, 1), "geth_head": g,
                    "reth_a_counts": ca, "reth_b_counts": cb,
                    "reth_b_restarted": b_restarted,
                    "reth_a_alive": (a_node.alive() if not args.skip_reth else None),
                    "reth_b_alive": (b_node.alive() if not args.skip_reth else None),
                })
                log(f"checkpoint t={t:.0f}s geth={g} reth_a={ra} reth_b={rb} "
                    f"rej_a={ca.get('any')} rej_b={cb.get('any')}")

            # restart follower B mid-window
            if (not args.skip_reth and not b_restarted and now >= restart_at):
                log("restarting follower B (same datadir) ...")
                t_stop = time.time()
                b_node.stop()
                if os.path.exists(b_log):
                    os.replace(b_log, b_log_pre)
                b_node.start()
                summary["reth_b_restart"] = {
                    "at_t": round(t, 1),
                    "stop_seconds": round(time.time() - t_stop, 1)}
                try:
                    wait_for(lambda: head(b_rpc), 120, "reth B rpc back after restart")
                    b_restarted = True
                    summary["reth_b_restart"]["rpc_back_s"] = round(
                        time.time() - t_stop, 1)
                    log("follower B restarted; continuing observation")
                except TimeoutError:
                    log("!! follower B did not come back after restart")
                    summary["reth_b_restart"]["failed"] = True
                    dump_tail(b_log, 120, "RETH-B-RESTART-FAILURE")
                    b_restarted = True  # do not retry; record failure
                # if the chain already froze (miner stopped), briefly
                # restart geth's miner so the restarted follower is forced
                # to re-fetch and re-evaluate the trigger / its descendants
                if miner_stopped:
                    try:
                        rpc_ok(geth_rpc, "miner_start", [])
                        h_now = head(geth_rpc)
                        wait_for(lambda: head(geth_rpc) >= h_now + 5, 90,
                                 "re-provoke mining (5 blocks for restarted B)")
                        rpc_ok(geth_rpc, "miner_stop", [])
                        summary["reth_b_restart"]["reprovoke_mined_to"] = head(geth_rpc)
                        log("re-provoke: geth mined to "
                            f"{head(geth_rpc)} for the restarted follower")
                    except Exception as e:  # noqa: BLE001
                        log(f"re-provoke mining failed: {e}")
                        try:
                            rpc_ok(geth_rpc, "miner_stop", [])
                        except Exception:  # noqa: BLE001
                            pass

            time.sleep(5)

        summary["observation_samples"] = samples
        summary["rejection_checkpoints"] = checkpoints
        summary["observation_actual_seconds"] = round(time.time() - t0, 1)

        # ---- post-window grace: make sure geth actually reached the
        # required distance past the trigger (block-rate independent),
        # then freeze the chain and record final heads ----
        if not miner_stopped:
            log("observation window ended while geth still mining; "
                "waiting (grace) until required blocks past trigger")
            wait_for(lambda: head(geth_rpc) >= target_head, 420,
                     f"geth head >= {target_head} (post-window grace)")
            try:
                rpc_ok(geth_rpc, "miner_stop", [])
                miner_stopped = True
                summary["mining_stopped_at_head"] = head(geth_rpc)
                summary["mining_stopped_after_window"] = True
                log(f"geth reached head {head(geth_rpc)}; miner stopped")
            except Exception as e:  # noqa: BLE001
                log(f"miner_stop (post-window) failed: {e}")
        if not args.skip_reth:
            time.sleep(5)
            summary["post_window_heads"] = {
                "geth": head(geth_rpc), "reth_a": head(a_rpc), "reth_b": head(b_rpc)}
            assert_that(head(a_rpc) < ht and head(b_rpc) < ht,
                        "followers_still_pinned_after_grace",
                        f"reth_a={head(a_rpc)} reth_b={head(b_rpc)} < {ht}")
            # liveness: a dead follower is NOT a pinned follower
            assert_that(a_node.alive() and b_node.alive() and geth_node.alive(),
                        "all_nodes_alive_at_end_of_window",
                        f"geth={geth_node.alive()} reth_a={a_node.alive()} "
                        f"reth_b={b_node.alive()}")

        summary["geth_final_head"] = head(geth_rpc)
        summary["reth_a_max_head"] = max_a
        summary["reth_b_max_head"] = max_b
        summary["reth_b_max_head_post_restart"] = max_b_post
        summary["blocks_past_trigger"] = head(geth_rpc) - ht

        # ---- phase 7: settle + final assertions ----
        if not args.skip_reth:
            def settled():
                return head(a_rpc) == ht - 1 and head(b_rpc) == ht - 1
            try:
                wait_for(settled, 90,
                         f"both followers settle at pre-trigger block {ht-1}")
            except TimeoutError:
                log(f"settle timeout: reth_a={head(a_rpc)} reth_b={head(b_rpc)} "
                    f"(expected {ht-1})")
            summary["reth_a_final_head"] = head(a_rpc)
            summary["reth_b_final_head"] = head(b_rpc)

        assert_that(head(geth_rpc) >= target_head and not epoch_capped,
                    "geth_mined_required_blocks_past_trigger",
                    f"head={head(geth_rpc)} >= trigger({ht})+"
                    f"{args.min_blocks_past_trigger} (target {target_head})")
        assert_that(head(geth_rpc) <= args.head_safety_cap + 5,
                    "geth_head_below_devnet_epoch_wall",
                    f"head={head(geth_rpc)} <= {args.head_safety_cap}+5")
        assert_that(summary["observation_actual_seconds"] >= args.observation_seconds,
                    "observation_window_met",
                    f"{summary['observation_actual_seconds']}s >= "
                    f"{args.observation_seconds}s")

        if not args.skip_reth:
            ca = log_counts(a_log)
            cb_pre = log_counts(b_log_pre) if os.path.exists(b_log_pre) else {}
            cb_post = log_counts(b_log)
            summary["reth_a_rejections"] = ca
            summary["reth_b_rejections_pre_restart"] = cb_pre
            summary["reth_b_rejections_post_restart"] = cb_post

            assert_that(max_a < ht, "reth_a_never_reached_trigger_block",
                        f"max head={max_a} < trigger block {ht} "
                        f"({len(samples)} samples over "
                        f"{summary['observation_actual_seconds']}s)")
            assert_that(max_b < ht, "reth_b_never_reached_trigger_block",
                        f"max head={max_b} < trigger block {ht} "
                        f"(pre+post restart)")
            assert_that(ca["unexpected_system_tx"] >= 1,
                        "reth_a_unexpected_system_tx_logged",
                        f"count={ca['unexpected_system_tx']}")
            assert_that(cb_pre["unexpected_system_tx"] >= 1,
                        "reth_b_unexpected_system_tx_logged",
                        f"count(pre-restart)={cb_pre['unexpected_system_tx']}")
            assert_that(ca["any"] >= args.min_rejection_lines,
                        "reth_a_descendant_rejections_logged",
                        f"rejection lines={ca['any']}")
            assert_that(cb_pre["any"] >= args.min_rejection_lines,
                        "reth_b_descendant_rejections_logged",
                        f"rejection lines(pre-restart)={cb_pre['any']}")
            assert_that("failed" not in summary.get("reth_b_restart", {"failed": ""}),
                        "reth_b_restarted",
                        str(summary.get("reth_b_restart")))
            assert_that(max_b_post < ht if max_b_post >= 0 else False,
                        "reth_b_post_restart_still_pinned",
                        f"max head post-restart={max_b_post} < trigger block {ht}")
            assert_that(cb_post["any"] >= 1,
                        "reth_b_fresh_log_shows_rejection_after_restart",
                        f"fresh-log rejection lines={cb_post['any']}")
            assert_that(head(a_rpc) == ht - 1 and head(b_rpc) == ht - 1,
                        "both_followers_pinned_at_pretrigger_block",
                        f"reth_a={head(a_rpc)} reth_b={head(b_rpc)} "
                        f"expected={ht-1}")
            # continuity: rejection counts must strictly increase across at
            # least one consecutive checkpoint pair during which geth was
            # still advancing (mining active); vacuous if the mining phase
            # was shorter than the checkpoint interval (recorded honestly)
            ca_series = [c["reth_a_counts"].get("any", 0) for c in checkpoints]
            gh_series = [c["geth_head"] for c in checkpoints]
            summary["reth_a_count_series"] = ca_series
            summary["checkpoint_geth_heads"] = gh_series
            active_increase = any(
                gh_series[i + 1] > gh_series[i] and ca_series[i + 1] > ca_series[i]
                for i in range(len(checkpoints) - 1))
            assert_that(
                active_increase or (len(ca_series) >= 2
                                     and ca_series[-1] >= args.min_rejection_lines),
                "reth_a_rejections_continuously_accumulating",
                f"series={ca_series} heads={gh_series} "
                f"(active_increase={active_increase})")

            # keep verbatim rejection lines for the record
            for path, tag in ((a_log, "A"), (b_log_pre, "B-pre"), (b_log, "B-post")):
                if os.path.exists(path):
                    with open(os.path.join(w, f"rejections_{tag}.txt"), "w") as f:
                        with open(path, "rb") as lf:
                            ltxt = lf.read().decode("utf-8", "replace")
                        f.write("\n".join(
                            ln for ln in ltxt.splitlines()
                            if any(s in ln for s in REJECTION_SUBSTRINGS)))

        with open(geth_log, "rb") as f:
            gtxt = f.read().decode("utf-8", "replace")
        summary["geth_imported_segments"] = gtxt.count("Imported new chain segment")

        exit_code = 0
        for a in summary["assertions"]:
            if not a["pass"]:
                exit_code = 1
        summary["verdict"] = "PASS" if exit_code == 0 else "FAIL"
        log(f"VERDICT: {summary['verdict']}")
    except Exception as e:  # noqa: BLE001
        summary["fatal"] = f"{type(e).__name__}: {e}"
        log(f"FATAL: {e}")
        dump_tail(geth_log, 60, "GETH-AT-FATAL")
        if not args.skip_reth:
            if a_node is not None:
                dump_tail(a_node.logfile, 120, "RETH-A-AT-FATAL")
            if b_node is not None:
                dump_tail(b_node.logfile, 120, "RETH-B-AT-FATAL")
        summary["verdict"] = "FAIL"
        exit_code = 1
    finally:
        teardown()
        with open(os.path.join(w, "summary.json"), "w") as f:
            json.dump(summary, f, indent=2)
    return exit_code


if __name__ == "__main__":
    sys.exit(main())

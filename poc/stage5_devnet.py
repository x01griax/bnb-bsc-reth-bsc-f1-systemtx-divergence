#!/usr/bin/env python3
"""Stage 5 F-1 binary-level devnet orchestrator.

Runs ONE two-node devnet differential on a single host:
  - bsc geth validator (single validator, mining) built from bnb-chain/bsc@947ba1c
  - reth-bsc follower (release binary) built from bnb-chain/reth-bsc@1af59ea
  - custom genesis: every fork through Pasteur at 0, Jenner unscheduled
    (today's BSC mainnet regime), single validator = the Stage 4 fixed key.

Phases (all assertions recorded into summary.json):
  0. genesis generated fresh (timestamp = now-300s so block 1 is not a
     'breathe block' - see stage notes), geth init + key import
  1. geth starts, seals blocks (~450ms, Fermi interval)
  2. reth starts, p2p-connects to geth (enode from geth log), syncs
     genesis -> head; genesis hashes must match
  3. CONTROL: miner_setGasPrice(0) then a PAID-gas (1 gwei) tx from the
     coinbase to the target address -> geth must include it; reth must
     follow past that block (binary-level control: a paid coinbase tx to
     the target is a normal tx on BOTH clients)
  4. TRIGGER: zero-gas tx from the coinbase to the target address
     (the F-1 divergent triple) -> geth must include it and execute it
     as a NORMAL tx (receipt status 1, gasUsed 21000)
  5. geth mines >= 8 more blocks (bsc continues past the trigger), then
     miner_stop (freeze; keeps the run far below the epoch-200 contract
     wall)
  6. OBSERVATION (>= 25 s): reth head must NEVER reach the trigger block;
     reth log must contain the predicted rejection ('unexpected system tx')

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


def log(msg):
    print(f"[stage5 {time.strftime('%H:%M:%S')}] {msg}", flush=True)


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


def start_proc(cmd, logfile, cwd=None):
    lf = open(logfile, "wb")
    return subprocess.Popen(cmd, stdout=lf, stderr=subprocess.STDOUT, cwd=cwd)


def dump_tail(path, lines=80, tag=""):
    """Print the tail of a log file to stdout (survives in the Actions job log)."""
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
    ap.add_argument("--reth-port", type=int, default=8546)
    ap.add_argument("--p2p-port", type=int, default=30303)
    ap.add_argument("--skip-reth", action="store_true",
                    help="local dry-run: exercise the geth half only")
    args = ap.parse_args()

    os.makedirs(args.workdir, exist_ok=True)
    w = args.workdir
    gdata = os.path.join(w, "gdata")
    rdata = os.path.join(w, "rdata")
    geth_rpc = f"http://127.0.0.1:{args.geth_port}"
    reth_rpc = f"http://127.0.0.1:{args.reth_port}"
    geth_log = os.path.join(w, "geth.log")
    reth_log = os.path.join(w, "reth.log")
    summary = {"label": args.label, "target": args.target, "assertions": []}
    procs = []

    def assert_that(cond, name, detail=""):
        summary["assertions"].append({"name": name, "pass": bool(cond), "detail": str(detail)})
        log(("PASS " if cond else "FAIL ") + f"{name} {detail}")
        return bool(cond)

    def teardown():
        for p in procs:
            if p.poll() is None:
                p.send_signal(signal.SIGTERM)
        time.sleep(3)
        for p in procs:
            if p.poll() is None:
                p.kill()

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
        procs.append(start_proc(geth_cmd, geth_log))
        h = wait_for(lambda: head(geth_rpc), 90, "geth rpc up")
        wait_for(lambda: head(geth_rpc) >= 4, 90, "geth mining (head>=4)")
        log(f"geth mining, head={head(geth_rpc)}")
        assert_that(True, "geth_mining_started", f"head={head(geth_rpc)}")
        summary["block1_txs"] = len(rpc_ok(geth_rpc, "eth_getBlockByNumber", ["0x1", True])["transactions"])
        log(f"block 1 carries {summary['block1_txs']} initContract system txs")

        enode = None
        with open(geth_log, "rb") as f:
            txt = f.read().decode("utf-8", "replace")
        m = re.search(r"self=\"?enode://([0-9a-f]+)@([^\"]+)\"?", txt)
        if not m:
            raise RuntimeError("could not parse geth enode from log")
        enode = f"enode://{m.group(1)}@127.0.0.1:{args.p2p_port}"
        summary["enode"] = enode
        log(f"geth enode: {enode}")

        # ---- phase 2: reth up, sync ----
        reth_head_at_trigger = None
        if not args.skip_reth:
            reth_cmd = [
                args.reth, "node",
                "--chain", os.path.join(w, "genesis.json"),
                "--datadir", rdata,
                "--port", str(args.p2p_port + 1),
                "--bootnodes", enode, "--trusted-peers", enode,
                "--netrestrict", "127.0.0.1/32",
                "--http", "--http.addr", "127.0.0.1",
                "--http.port", str(args.reth_port),
                "--http.api", "eth,net",
                "--ipcpath", os.path.join(w, "reth.ipc"),
                "-vvvv",  # this fork uses clap verbosity flags; -vvvv = debug level
            ]
            procs.append(start_proc(reth_cmd, reth_log))
            try:
                wait_for(lambda: head(reth_rpc) >= 0, 120, "reth rpc up")
            except TimeoutError:
                dump_tail(reth_log, 120, "RETH-STARTUP-FAILURE")
                raise

            def genesis_match():
                g = rpc_ok(geth_rpc, "eth_getBlockByNumber", ["0x0", False])["hash"]
                r = rpc_ok(reth_rpc, "eth_getBlockByNumber", ["0x0", False])["hash"]
                return g == r or g

            gm = wait_for(genesis_match, 60, "genesis hash match")
            g_hash = rpc_ok(geth_rpc, "eth_getBlockByNumber", ["0x0", False])["hash"]
            r_hash = rpc_ok(reth_rpc, "eth_getBlockByNumber", ["0x0", False])["hash"]
            assert_that(g_hash == r_hash, "genesis_hashes_match",
                        f"geth={g_hash[:18]}.. reth={r_hash[:18]}..")
            summary["reth_genesis_hash"] = r_hash
            if g_hash != r_hash:
                raise RuntimeError("genesis hash mismatch between clients")

            # wait until reth follows geth (lag <= 3)
            def reth_following():
                g, r = head(geth_rpc), head(reth_rpc)
                return r >= g - 3 and r >= 3
            wait_for(reth_following, 180, "reth syncing behind geth")
            log(f"reth following geth (geth={head(geth_rpc)} reth={head(reth_rpc)})")
            assert_that(True, "reth_syncing", f"geth={head(geth_rpc)} reth={head(reth_rpc)}")

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
        crec = wait_for(ctl_receipt, 45, "control tx mined")
        hc = int(crec["blockNumber"], 16)
        summary["control_block"] = hc
        assert_that(True, "geth_mined_paid_gas_control",
                    f"block={hc} status=1")
        log(f"geth mined control tx in block {hc}")

        if not args.skip_reth:
            wait_for(lambda: head(reth_rpc) >= hc, 120, f"reth head >= control block {hc}")
            assert_that(True, "reth_accepted_paid_gas_control_block",
                        f"reth head={head(reth_rpc)} >= {hc}")

        # ---- phase 4: trigger tx (zero gas, coinbase -> target) ----
        trig = rpc_ok(geth_rpc, "eth_sendTransaction",
                      [{"from": VALIDATOR, "to": args.target,
                        "gas": hex(TX_GAS), "gasPrice": "0x0"}])
        summary["trigger_tx"] = trig
        log(f"trigger tx sent: {trig}")

        def trig_receipt():
            r = rpc_ok(geth_rpc, "eth_getTransactionReceipt", [trig])
            return r if r.get("status") == "0x1" else None
        trec = wait_for(trig_receipt, 45, "trigger tx mined")
        ht = int(trec["blockNumber"], 16)
        summary["trigger_block"] = ht
        summary["trigger_gas_used"] = int(trec["gasUsed"], 16)
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
        log(f"geth mined TRIGGER tx in block {ht} (status={trec['status']}, "
            f"gasUsed={int(trec['gasUsed'],16)}) - bsc treats it as a NORMAL tx")

        # ---- phase 5: geth continues, then freeze ----
        wait_for(lambda: head(geth_rpc) >= ht + 8, 60, "geth head >= trigger+8")
        gh = head(geth_rpc)
        summary["geth_head_after_trigger"] = gh
        assert_that(gh >= ht + 8, "geth_continued_past_trigger",
                    f"geth head={gh} >= {ht}+8")
        log(f"geth continued past trigger: head={gh}")
        try:
            rpc_ok(geth_rpc, "miner_stop", [])
            log("geth miner stopped (chain frozen for the observation window)")
        except Exception as e:  # noqa: BLE001
            log(f"miner_stop failed ({e}); continuing with live mining")

        # ---- phase 6: observation ----
        t0 = time.time()
        samples = []
        max_reth = -1
        while time.time() - t0 < 28:
            g, r = head(geth_rpc), head(reth_rpc) if not args.skip_reth else -1
            samples.append({"t": round(time.time() - t0, 1), "geth": g, "reth": r})
            max_reth = max(max_reth, r)
            time.sleep(2.5)
        summary["observation_samples"] = samples
        summary["reth_max_head"] = max_reth
        if not args.skip_reth:
            assert_that(max_reth < ht, "reth_never_reached_trigger_block",
                        f"reth max head={max_reth} < trigger block {ht}")
            log(f"observation done: geth final head={head(geth_rpc)}, "
                f"reth max head={max_reth}, trigger block={ht}")

        # ---- log greps ----
        if args.skip_reth:
            rtxt = ""
            n_rej = 0
        else:
            with open(reth_log, "rb") as f:
                rtxt = f.read().decode("utf-8", "replace")
            n_rej = rtxt.count("unexpected system tx")
        summary["reth_unexpected_system_tx_count"] = n_rej
        summary["reth_invalid_payload_lines"] = rtxt.count("returned Invalid")
        summary["reth_fork_recovery_lines"] = rtxt.count("fork recovery")
        rej_lines = [ln for ln in rtxt.splitlines() if "unexpected system tx" in ln]
        summary["reth_rejection_sample"] = rej_lines[:5]
        if not args.skip_reth:
            with open(os.path.join(w, "reth_rejections.txt"), "w") as f:
                f.write("\n".join(ln for ln in rtxt.splitlines()
                                  if any(s in ln for s in (
                                      "unexpected system tx",
                                      "returned Invalid",
                                      "fork recovery",
                                      "Block execution",
                                      "invalid block",
                                      "Invalid block")))) 
            with open(os.path.join(w, "reth_tail.txt"), "w") as f:
                f.write("\n".join(rtxt.splitlines()[-300:]))
        if not args.skip_reth:
            assert_that(n_rej > 0, "reth_log_shows_unexpected_system_tx",
                        f"count={n_rej}")
        for ln in rej_lines[:5]:
            log(f"RETH REJECTION LINE: {ln.strip()[:300]}")

        with open(geth_log, "rb") as f:
            gtxt = f.read().decode("utf-8", "replace")
        summary["geth_imported_segments"] = gtxt.count("Imported new chain segment")
        summary["geth_trigger_included"] = trig in gtxt or True  # receipt already proved it

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
            dump_tail(reth_log, 120, "RETH-AT-FATAL")
        summary["verdict"] = "FAIL"
        exit_code = 1
    finally:
        teardown()
        with open(os.path.join(w, "summary.json"), "w") as f:
            json.dump(summary, f, indent=2)
    return exit_code


if __name__ == "__main__":
    sys.exit(main())

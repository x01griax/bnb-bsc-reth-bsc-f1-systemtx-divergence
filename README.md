# bsc vs reth-bsc: system-transaction target-set divergence — PoC and evidence

Supporting material for a BNB Chain Bug Bounty report: a coinbase-signed,
zero-gas-price transaction to `0x…1008` (TokenManager), `0x…2001` (Staking),
or `0x…2003` (StakeCredit) is a normal transaction on `bnb-chain/bsc` but is
classified as a system transaction on `bnb-chain/reth-bsc`, which then
rejects the block containing it — permanently, including every descendant
block — while bsc keeps extending the chain past it.

Pinned versions used for all evidence here:

- `bnb-chain/bsc` @ `947ba1cb1ef05d4a5f71b738113a3d8b4c16f6b7` (self-identifies v1.8.1)
- `bnb-chain/reth-bsc` @ `1af59ea08e50820c1d31a06bd7ff3e3999541817` (main)

## Contents

```
poc/
  stage10_final_validation.py   Primary PoC orchestrator: 1 geth validator +
                                 2 reth-bsc followers, trigger injection,
                                 ~100 blocks mined past the trigger, 5-minute
                                 observation window, mid-window follower
                                 restart, full assertion set -> summary.json
  stage10_final_validation.yml  GitHub Actions workflow that builds both
                                 binaries from the pinned commits and runs
                                 the orchestrator above
  stage5_devnet.py              Earlier, simpler variant: single-follower,
                                 one target address at a time (used to check
                                 all three delta addresses individually)
  f1_systemtx_repro_test.go     bsc-side Go unit test — drop into
                                 consensus/parlia/; 6 subtests incl. controls
  f1_system_tx_divergence_tests.rs  reth-bsc-side Rust unit test — drop into
                                 src/node/evm/ + a 2-line mod.rs hook; 5 tests
  genesis.json                  Genesis used by the final validation run
                                 (template/reference only — the orchestrator
                                 regenerates it fresh, with a current
                                 timestamp, on every run)
logs/
  final_validation_summary.json Machine-readable assertion record from the
                                 final validation run
  geth_acceptance.txt            geth log excerpts: trigger mined, chain
                                 keeps growing past it
  reth_rejection.txt             reth-bsc log excerpts: trigger rejected
                                 with "unexpected system tx"
  reth_descendant_cascade.txt    Both followers rejecting every descendant
                                 block ("links to previously rejected block")
  reth_restart_persistence.txt   A restarted follower re-rejecting the
                                 trigger in its fresh log (the halt survives
                                 a restart)
  rejections_A.txt, rejections_B-pre.txt, rejections_B-post.txt
                                 Full rejection-line logs for both followers
  geth_full.log                  Full validator log for the run
  stage5_summaries/              Per-address run summaries from the earlier,
                                 single-follower runs (0x1008 / 0x2001 / 0x2003)
```

## How to reproduce

### Fastest — unit-level differential (no network, no binaries to build)

**bsc side:** copy `poc/f1_systemtx_repro_test.go` into `consensus/parlia/`
of `bnb-chain/bsc` at `947ba1c` and run:

```bash
go test ./consensus/parlia/ -run 'TestF1SystemTxTargetSetDivergence' -v -count=1
```

Expect 6/6 subtests to pass: the three trigger addresses are accepted with
normal receipts, and three controls confirm the harness is exercising the
real classification path (a shared address, a paid-gas tx, and a
non-coinbase sender all behave as expected).

**reth-bsc side:** copy `poc/f1_system_tx_divergence_tests.rs` into
`src/node/evm/` of `bnb-chain/reth-bsc` at `1af59ea`, add to
`src/node/evm/mod.rs`:

```rust
#[cfg(test)]
mod f1_system_tx_divergence_tests;
```

then run:

```bash
cargo test --locked -p reth_bsc --lib f1_
```

Expect 5/5 to pass, including that the trigger transaction hash is
byte-identical to the one produced on the bsc side.

### Primary PoC — binary-level devnet (one validator, two followers)

Build both binaries at the pinned commits:

```bash
git clone https://github.com/bnb-chain/bsc && cd bsc
git checkout 947ba1cb1ef05d4a5f71b738113a3d8b4c16f6b7
go build -o ../geth ./cmd/geth
cd .. && git clone https://github.com/bnb-chain/reth-bsc && cd reth-bsc
git checkout 1af59ea08e50820c1d31a06bd7ff3e3999541817
cargo build --release --locked --bin reth-bsc
```

Run the orchestrator (pure Python 3 stdlib, no dependencies):

```bash
python3 poc/stage10_final_validation.py \
  --geth  ./geth \
  --reth  ./reth-bsc/target/release/reth-bsc \
  --target 0x0000000000000000000000000000000000001008 \
  --label final_tokenmanager_1008 \
  --workdir ./run_final
```

Or dispatch `poc/stage10_final_validation.yml` as a GitHub Actions workflow,
which builds both binaries from the pinned commits and runs the same
orchestrator (this is how the evidence in `logs/` was produced).

To cover the other two delta addresses, rerun with `--target
0x0000000000000000000000000000000000002001` (Staking) or `…2003`
(StakeCredit), or use `poc/stage5_devnet.py` for the single-follower
variant.

## Notes on the devnet setup

- The genesis is regenerated fresh on every run (`poc/genesis.json` here is
  the template/reference from one specific run — don't reuse its
  timestamp). All BSC forks through Pasteur are active at block 0 and
  Jenner is unscheduled, matching BSC mainnet's fork regime at the time of
  writing. The system-contract sets and the classification predicate are
  static (not fork-gated), so this regime match is what matters, not exact
  timestamp reuse.
- A single validator is sufficient: the mechanism under test (transaction
  classification, block validation, invalid-block caching, descendant
  rejection) is deterministic and local to each node; no validator-set
  quorum is involved.
- The trigger is injected through the stock RPC surface only —
  `miner_setGasPrice(0x0)` followed by `eth_sendTransaction` with
  `gasPrice: 0x0` from the unlocked coinbase account. No binary patching on
  either side.

## Responsible disclosure

All testing ran on private devnets built from the two repositories above, at
the pinned commits, which are explicitly in scope for the BNB Chain Bug
Bounty program. No mainnet or public testnet transaction was sent, and no
third party has been notified of this issue.

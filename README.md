# cryptopos-rail-bitcoin

A Bitcoin **testnet4** payment rail for [cryptopos-core](https://github.com/dowoop/cryptopos-core), read through Esplora's HTTPS API.

It holds **no keys and never spends**. It is a watcher: the customer's own
wallet is the payer, and this package only tells you what the chain says.

```bash
pip install cryptopos-rail-bitcoin
```

Installing it *is* the integration — it registers itself through the
`cryptopos.rails` entry-point group, and a host that calls `discover()` finds
it with no code change.

> **Not yet proven through this published wheel.** The adapter has settled real
> testnet money in the parent project, where it shipped built into
> `cryptopos-core`. It has not yet settled a payment in this extracted,
> installed form. This project has four recorded incidents of a green suite over
> a deployment that could not take a payment, so the distinction is stated
> rather than glossed.

**Not audited.** No external security audit; never used with mainnet funds.

## Rails

| entry point | rail key | binding |
|---|---|---|
| `bitcoin-testnet4` | `bitcoin:testnet4/native:btc` | **per-sale, only if the host supplies a fresh address** — this rail requires one and does not derive it |

---

# Cookbook

The five-call sequence, the settlement states, and the four host obligations are
in [cryptopos-core's cookbook](https://github.com/dowoop/cryptopos-core#the-five-calls).
This file covers only what is specific to Bitcoin.

## 1. Configure it

Three keys, and only the first is required:

```python
configuration = {
    "endpoint": "https://mempool.space/testnet4/api",   # any Esplora-compatible API
    "timeout_seconds": 10,                              # optional, per request
}
```

`endpoint` must be an Esplora HTTPS base URL. `readiness` asks it for the
genesis hash and refuses anything that is not testnet4, so a mainnet endpoint
pasted into a testnet deployment fails at start-up rather than at the counter.

## 2. Charge a sale

The rail is a plain object; import it directly or take it from the registry:

<!-- readme: new -->
```python
from cryptopos_core.plugin import PaymentIntent, RecipientBaseline
from cryptopos_rail_bitcoin import bitcoin_testnet4

rail = bitcoin_testnet4
rail.key                                 # -> 'bitcoin:testnet4/native:btc'
sorted(rail.capabilities)
#   -> ['address-validation', 'observation', 'payment-request', 'settlement']
```

Check the address first — the last moment a mistake is still free:

```python
address = "tb1qp5wfcq48h6d63wyy9qz0awtpfqwwv4smhppgv3"
rail.validate_recipient(address)         # -> ('ok', '')
```

In a real host, `capture_baseline` reads the chain. Constructing one by hand is
how you drive the rail offline in tests — the baseline is a plain value:

```python
baseline = RecipientBaseline(rail.key, address, "esplora", tip=100)
intent = PaymentIntent(
    intent_id="sale-1042",
    rail_key=rail.key,
    recipient=address,
    amount_native=125_000,               # satoshis
    created_at_epoch=1_787_100_000,
    expires_at_epoch=1_787_101_800,
    baseline=baseline,
)
rail.create_request(intent).uri
#   -> 'bitcoin:tb1qp5wfcq48h6d63wyy9qz0awtpfqwwv4smhppgv3?amount=0.00125000'
```

That is BIP-21, with the amount in BTC to eight places. **BIP-21 does not name
the network**, and testnet3 and testnet4 share an address format, so the payer's
wallet must itself be configured for testnet4 — the URI cannot tell it. The
observer verifies the [BIP 94](https://bips.dev/94/) testnet4 genesis hash
independently, so this rail cannot be pointed at the wrong chain even though the
URI cannot say which one it means.

Against a live endpoint the rest is the standard loop:

<!-- readme: skip -->
```python
baseline = rail.capture_baseline(address, configuration)   # reads the chain
batch = rail.observe(intent, configuration)
while not batch.complete:
    batch = rail.observe(intent, configuration, batch)
decision = rail.settle(intent, batch, claimed_transaction_ids=already_credited)
```

Bitcoin completes its observation in one read, so that loop usually runs once.
Write it as a loop anyway: it is the protocol's contract, and the EVM rails
genuinely need it.

## 3. Give every sale its own address

**This rail refuses a recipient that has any transaction history**, and that is
not fussiness — a shared address cannot bind a payment to a sale, because a
transaction confirming after one sale expires is indistinguishable from a
payment for the next one.

But be exact about what the check catches, because it is a history check and
nothing more:

1. A host assigns the same never-used address to sales A and B.
2. Both call `capture_baseline` before either customer pays. The address has no
   history, so **both pass**.
3. B's customer pays.
4. A polls first and claims the transaction.
5. A settles on B's money; B ends in review.

So "it refuses a reused receiving address" is true of a *previously paid*
address and false of *concurrent* reuse. Allocating each sale its own address is
the host's job — and `cryptopos-core` ships the watch-only derivation to do it:

```python
from cryptopos_core import hd

XPUB = ("xpub661MyMwAqRbcFtXgS5sYJABqqG9YLmC4Q1Rdap9gSE8NqtwybGhePY2gZ29ESFjqJ"
        "oCu1Rupje8YtGqsefD265TMg7usUDFdp6W1EGMcet8")

account = hd.parse_extended_key(XPUB)

def address_for_sale(index):
    """One fresh receiving address per sale, from a key that cannot spend."""
    return hd.p2wpkh_address(hd.derive_path(account, f"0/{index}"), "tb")

address_for_sale(0)                      # -> 'tb1qp5wfcq48h6d63wyy9qz0awtpfqwwv4smhppgv3'
address_for_sale(1)                      # -> 'tb1qrfxr69jqnhwufxgkqgcdep9prq4j4vuwqglm5l'
```

Use `"bc"` as the human-readable part for mainnet, `"tb"` for test networks.

**Three things about indices that cost money if you skip them.**

*Derive from the account key, not the master key.* Paste the **account** xpub —
what a wallet exports for `m/84'/1'/0'` on testnet — so `0/index` is that
account's external chain and the addresses are ones your wallet already watches.
Deriving `0/index` from a master xpub, as the example above does with a BIP-32
test vector, gives addresses at a non-standard path your wallet will never scan.

*An index is spent the moment it is shown to anyone, and can never be reused.*
A payment instruction cannot be withdrawn. A customer who kept the QR from a
finished sale can pay it tomorrow, and if that address now belongs to a new
sale the transfer arrives after the new baseline, inside the new window, and
settles the wrong invoice. No cooldown is long enough, because no finite time
makes an old QR unpayable. **This rail is a partial backstop**:
`capture_baseline` refuses a recipient with any transaction history, so an
address that was already paid cannot be handed out again — but an address that
was shown and never paid has no history, so nothing here stops you reissuing
it. That one is yours to prevent.

*Keep the allocation counter durable, and mind the gap limit.* Never reusing
means abandoned checkouts consume indices, while a wallet restoring from the
seed scans forward only until it meets a run of unused addresses — commonly
**20**, BIP-44's gap limit. So keep the watching wallet's gap limit above your
realistic run of unpaid sales, and persist the next-index counter: losing it
restarts allocation at zero and hands a live address to a second sale, which is
the reuse failure by another route. If you cannot raise the gap limit, apply
backpressure on opening sales. Recycling addresses is not the remedy, however
much it looks like one.

**The module accepts extended *public* keys only.** There is no private
derivation and no signing operation anywhere in it, so a host that derives
addresses this way still holds nothing that can spend:

<!-- readme: raises -->
```python
hd.parse_extended_key("xprv9s21ZrQH143K3QTDL4LXw2F7HEK3wJUD2nW2nRk4stbPy6cq3jPPqjiChkVvvNKmPGJxWUtg6LnF5kejMRNNU3TGtRBeJgk33yuGBxrMPHi")   # InvalidExtendedKey - a private key has no business here
hd.derive_path(account, "m/0/1")   # InvalidExtendedKey - m is a master key this module cannot have
hd.derive_path(account, "0'/1")    # InvalidExtendedKey - a component must be a plain decimal index
```

Hardened components are refused rather than silently skipped: hardened
derivation is impossible from a public key, and a rail that quietly gave you a
different address than you asked for would send money to the wrong place.

## 4. What settlement costs you here

**Settlement waits for block time.** Testnet4's block interval has been measured
at a **20-minute median**, which is longer than a typical price-lock window — so
a payment made honestly and immediately can still arrive after the sale it was
for has ended. That is a property of the chain, not a defect here. A host
offering this rail should expect to reconcile late payments rather than assume
they cannot happen.

The gate is **one confirmation**. [BIP 95](https://bips.dev/95/) documents the
persistent short reorgs that motivated a proposed Testnet 5, so one confirmation
is a *test-flow* gate, not a model for accepting valuable coin. A host that
treats test coins as valuable should require a deeper operator policy.

Testnet4 is the network this rail drives because BIP 95's Testnet 5 is still a
draft and does not yet define a genesis block.

## What this package does not decide

Pricing, which rails a deployment offers, whether a rail is switched on, and
what an endpoint URL should be are **host** questions. They change per
deployment and are edited by someone with a login. This package answers only
what is true about the chain.

## Testing

```bash
PYTHONPATH=src python -m unittest discover -s tests -t .
python3 tools/readme.py --wheel   # every example above, against the wheel
```

No test in this package opens a socket.

## Licence

MIT.

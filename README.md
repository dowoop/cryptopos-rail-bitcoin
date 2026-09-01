# cryptopos-rail-bitcoin

A Bitcoin **testnet4** payment rail for [CryptoPoS](https://github.com/dowoop/cryptopos-core), read through Esplora's HTTPS API.

> **Not yet proven through this published wheel.** The adapter has settled
> real testnet money in the parent project, where it shipped built into
> `cryptopos-core`. It has not yet settled a payment in this extracted,
> installed form. This project has four recorded incidents of a green suite
> over a deployment that could not take a payment, so the distinction is
> stated rather than glossed.

**Not audited.** No external security audit; never used with mainnet funds.

Install it beside `cryptopos-core` and it registers itself through the
`cryptopos.rails` entry-point group — a host that discovers rails finds it with
no code change:

```bash
pip install cryptopos-rail-bitcoin
```

```python
from importlib import metadata

for point in metadata.entry_points(group="cryptopos.rails"):
    rail = point.load()
    print(point.name, rail.key, sorted(rail.capabilities))
```

## What it is

A `PaymentRail` implementation: it validates a recipient, builds a payment
request, observes the chain for arriving money, and returns a settlement
decision. It holds **no keys and never spends** — every rail here is a watcher,
and the customer's own wallet is the payer.

Zero runtime dependencies beyond `cryptopos-core`.

## Rails

| entry point | rail key | binding |
|---|---|---|
| `bitcoin-testnet4` | `bitcoin:testnet4/native:btc` | **per-sale, if the host supplies it** — this rail requires a fresh address per sale and does not derive one |

## Two things worth knowing before you use it

**It refuses an address that has ALREADY been paid — and the host owns the
binding.** `capture_baseline` rejects a recipient with any transaction history.
That is not fussiness: a shared address cannot bind a payment to a sale,
because a transaction that confirms after one sale expires is indistinguishable
from a payment for the next one.

**Be exact about what that check does and does not catch**, because the table
above used to say this rail derived the address itself and it does not. It
derives nothing; the host does, from an extended public key, and this rail is
the reason the host needs one rather than a single address. History is the only
thing checked, so the case it cannot see is the one that matters:

1. A host assigns the same never-used address to sales A and B.
2. Both call `capture_baseline` before either customer pays. The address has no
   history, so **both pass**.
3. B's customer pays.
4. A polls first and claims the transaction.
5. A settles on B's money; B ends in review.

"It refuses a reused receiving address" is therefore true of a *previously
paid* address and false of *concurrent* reuse. Allocating each sale its own
derived index is the host's job, and nothing in this package enforces it.

**Settlement waits for block time.** Bitcoin's testnet4 block interval has been
measured at a 20-minute median, which is longer than a typical price-lock
window — so a payment made honestly and immediately can still arrive after the
sale it was for has ended. That is a property of the chain, not a defect here,
and a host that offers this rail should expect to reconcile late payments
rather than assume they cannot happen.

## What this package does not decide

Pricing, which rails a deployment offers, whether a rail is switched on, and
what an endpoint URL should be are **host** questions. They change per
deployment and are edited by someone with a login. This package answers only
what is true about the chain.

## Testing

```bash
PYTHONPATH=src python -m unittest discover -s tests -t .
```

No test in this package opens a socket.

## Licence

MIT.

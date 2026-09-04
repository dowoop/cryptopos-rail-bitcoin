"""A complete, deliberately narrow Bitcoin Testnet 4 payment rail.

This rail uses Esplora's read-only HTTPS API. It never holds a private key and
never sends funds. Before every baseline or observation read it asks the
provider for block zero and compares that hash with BIP 94, so an endpoint on
mainnet, Testnet 3, Signet, or a private fork is refused rather than guessed.

Version one requires a fresh, unused recipient for every payment. That keeps
the binding auditable and avoids pretending a single paginated address-history
read can safely distinguish old money from a new sale.
"""

# The package version is declared here as the single source. The static
# distribution metadata previously disagreed with this module by a patch
# release; Hatch now derives the distribution version from this value.
__version__ = "0.1.1"


import json
import re
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Mapping

from cryptopos_core.addresses import OK, validate
from cryptopos_core.errors import AddressRefused, InvalidRailPlugin, RailProviderError
from cryptopos_core.plugin import (
	ADDRESS_VALIDATION,
	NEEDS_REVIEW,
	OBSERVATION,
	PAYMENT_REQUEST,
	PENDING,
	SETTLED,
	SETTLEMENT,
	Asset,
	Network,
	ObservationBatch,
	PaymentIntent,
	PaymentRequest,
	Readiness,
	RecipientBaseline,
	SettlementDecision,
	TransferObservation,
)
from cryptopos_core.rails import RAILS
from cryptopos_core.uri import build_uri

TESTNET4_GENESIS_HASH = "00000000da84f2bafbbc53dee25a72ae507ff4914b867c565be350b0da8bf043"
MAX_RESPONSE_BYTES = 2_000_000
MAX_TRANSACTIONS = 50
MAX_TRANSACTION_INPUTS = 10_000
MAX_TRANSACTION_OUTPUTS = 10_000
DEFAULT_TIMEOUT_SECONDS = 5.0

_TXID = re.compile(r"^[0-9a-f]{64}$")


class _NoRedirect(urllib.request.HTTPRedirectHandler):
	def redirect_request(self, request, file_pointer, code, message, headers, new_url):
		return None


class _HttpsTransport:
	"""Bounded HTTPS GET without redirects or ambient authentication."""

	def __init__(self):
		self._opener = urllib.request.build_opener(_NoRedirect, urllib.request.ProxyHandler({}))

	def get(self, url, timeout, max_bytes):
		request = urllib.request.Request(
			url,
			headers={"Accept": "application/json, text/plain", "User-Agent": "cryptopos-core/1"},
			method="GET",
		)
		with self._opener.open(request, timeout=timeout) as response:
			declared = response.headers.get("Content-Length")
			if declared is not None:
				try:
					declared_size = int(declared)
				except ValueError:
					raise ValueError("response Content-Length was malformed") from None
				if declared_size < 0 or declared_size > max_bytes:
					raise ValueError("response exceeded the safety limit")
			body = response.read(max_bytes + 1)
		if len(body) > max_bytes:
			raise ValueError("response exceeded the safety limit")
		return body


def _configuration(configuration):
	if not isinstance(configuration, Mapping):
		raise RailProviderError(BitcoinTestnet4.key, "configuration must be a mapping")
	endpoint = configuration.get("endpoint")
	if not isinstance(endpoint, str) or not endpoint.strip():
		raise RailProviderError(BitcoinTestnet4.key, "an explicit Esplora endpoint is required")
	parts = urllib.parse.urlsplit(endpoint.strip())
	if (
		parts.scheme != "https"
		or not parts.hostname
		or parts.username is not None
		or parts.password is not None
		or parts.query
		or parts.fragment
	):
		raise RailProviderError(
			BitcoinTestnet4.key,
			"endpoint must be an HTTPS URL without credentials, query text, or a fragment",
		)
	base = urllib.parse.urlunsplit((parts.scheme, parts.netloc, parts.path.rstrip("/"), "", ""))
	transport = configuration.get("transport")
	if transport is None:
		transport = _HttpsTransport()
	if not callable(getattr(transport, "get", None)):
		raise RailProviderError(BitcoinTestnet4.key, "transport must provide a get method")
	timeout = configuration.get("timeout_seconds", DEFAULT_TIMEOUT_SECONDS)
	if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or not 0 < timeout <= 30:
		raise RailProviderError(BitcoinTestnet4.key, "timeout_seconds must be greater than 0 and at most 30")
	return base, transport, float(timeout)


def _read(base, transport, timeout, path):
	url = f"{base}{path}"
	try:
		body = transport.get(url, timeout=timeout, max_bytes=MAX_RESPONSE_BYTES)
	except RailProviderError:
		raise
	except (OSError, TimeoutError, urllib.error.URLError, urllib.error.HTTPError, ValueError) as exception:
		raise RailProviderError(BitcoinTestnet4.key, f"GET {path} failed: {exception}") from None
	if not isinstance(body, bytes):
		raise RailProviderError(BitcoinTestnet4.key, f"GET {path} returned non-byte data")
	if len(body) > MAX_RESPONSE_BYTES:
		raise RailProviderError(BitcoinTestnet4.key, f"GET {path} exceeded the response safety limit")
	return body


def _text(base, transport, timeout, path):
	try:
		return _read(base, transport, timeout, path).decode("ascii").strip()
	except UnicodeDecodeError:
		raise RailProviderError(BitcoinTestnet4.key, f"GET {path} did not return ASCII text") from None


def _json(base, transport, timeout, path):
	try:
		return json.loads(_read(base, transport, timeout, path).decode("utf-8"))
	except (UnicodeDecodeError, json.JSONDecodeError) as exception:
		raise RailProviderError(
			BitcoinTestnet4.key, f"GET {path} did not return valid JSON: {exception}"
		) from None


def _exact_nonnegative(value, field):
	if isinstance(value, bool) or not isinstance(value, int) or value < 0:
		raise RailProviderError(BitcoinTestnet4.key, f"provider field {field} must be a non-negative integer")
	return value


def _verified_provider(configuration):
	base, transport, timeout = _configuration(configuration)
	genesis = _text(base, transport, timeout, "/block-height/0")
	if genesis != TESTNET4_GENESIS_HASH:
		raise RailProviderError(
			BitcoinTestnet4.key,
			f"genesis hash {genesis!r} is not Bitcoin Testnet 4",
		)
	return base, transport, timeout


def _tip(base, transport, timeout):
	text = _text(base, transport, timeout, "/blocks/tip/height")
	if not text.isascii() or not text.isdecimal():
		raise RailProviderError(BitcoinTestnet4.key, "tip height was not a non-negative integer")
	return int(text)


def _transactions(base, transport, timeout, recipient):
	encoded = urllib.parse.quote(recipient, safe="")
	transactions = _json(base, transport, timeout, f"/address/{encoded}/txs")
	if not isinstance(transactions, list):
		raise RailProviderError(BitcoinTestnet4.key, "address transactions were not a list")
	if len(transactions) > MAX_TRANSACTIONS:
		raise RailProviderError(BitcoinTestnet4.key, "address transaction page exceeded the safety limit")
	return transactions


class BitcoinTestnet4:
	"""Address, request, observation, and one-confirmation settlement for TBTC."""

	network = Network("bitcoin", "testnet4", True)
	asset = Asset("native", "btc", "TBTC", 8)
	key = f"{network.key}/{asset.key}"
	binding_category = RAILS["btc"]["binding_category"]
	capabilities = frozenset({ADDRESS_VALIDATION, PAYMENT_REQUEST, OBSERVATION, SETTLEMENT})

	def validate_recipient(self, recipient):
		return validate("btc", recipient, "testnet")

	def readiness(self, configuration):
		ready = {ADDRESS_VALIDATION, PAYMENT_REQUEST, SETTLEMENT}
		unavailable = []
		try:
			base, transport, timeout = _verified_provider(configuration)
			_tip(base, transport, timeout)
		except RailProviderError as exception:
			unavailable.append((OBSERVATION, exception.reason))
		else:
			ready.add(OBSERVATION)
		return Readiness(self.key, frozenset(ready), tuple(unavailable))

	def capture_baseline(self, recipient, configuration):
		verdict, reason = self.validate_recipient(recipient)
		if verdict != OK:
			raise AddressRefused("btc", recipient, verdict, reason)
		base, transport, timeout = _verified_provider(configuration)
		tip = _tip(base, transport, timeout)
		transactions = _transactions(base, transport, timeout, recipient)
		if transactions:
			raise RailProviderError(
				self.key,
				"recipient already has transaction history; this rail requires a fresh address per payment",
			)
		return RecipientBaseline(self.key, recipient, base, tip)

	def create_request(self, intent):
		self._intent(intent)
		if intent.baseline is None:
			raise InvalidRailPlugin("Bitcoin Testnet 4 requires a recipient baseline before request creation")
		verdict, reason = self.validate_recipient(intent.recipient)
		if verdict != OK:
			raise AddressRefused("btc", intent.recipient, verdict, reason)
		uri = build_uri("btc", {"address": intent.recipient}, intent.amount_native, "testnet")
		return PaymentRequest(
			self.key,
			uri,
			intent.recipient,
			intent.amount_native,
			"Use a Bitcoin wallet explicitly configured for Testnet 4; BIP-21 does not name the network.",
		)

	def observe(self, intent, configuration, previous=None):
		self._intent(intent)
		if intent.baseline is None or intent.baseline.tip is None:
			raise InvalidRailPlugin("Bitcoin Testnet 4 observation requires a captured baseline")
		if previous is not None:
			previous.require_intent(intent)
		base, transport, timeout = _verified_provider(configuration)
		if intent.baseline.provider != base:
			raise RailProviderError(self.key, "observation endpoint differs from the baseline endpoint")
		tip = _tip(base, transport, timeout)
		if tip < intent.baseline.tip:
			raise RailProviderError(self.key, "provider tip is behind the captured baseline")
		transactions = _transactions(base, transport, timeout, intent.recipient)
		transfers = self._parse_transfers(intent, tip, transactions)
		return ObservationBatch(
			self.key,
			intent.intent_id,
			intent.recipient,
			base,
			intent.baseline.tip,
			tip,
			intent.baseline.tip,
			tip,
			tuple(transfers),
		)

	def settle(self, intent, observations, claimed_transaction_ids=frozenset()):
		self._intent(intent)
		if not isinstance(observations, ObservationBatch):
			raise InvalidRailPlugin("observations have an unknown shape")
		observations.require_intent(intent)
		if not observations.complete:
			raise InvalidRailPlugin("settlement requires observations through the provider tip")
		if not isinstance(claimed_transaction_ids, frozenset) or any(
			not isinstance(transaction_id, str) for transaction_id in claimed_transaction_ids
		):
			raise InvalidRailPlugin("claimed transaction ids must be a frozenset of text")
		claimed = [
			transfer
			for transfer in observations.transfers
			if transfer.transaction_id in claimed_transaction_ids
		]
		available = [
			transfer
			for transfer in observations.transfers
			if transfer.transaction_id not in claimed_transaction_ids
		]
		sighted = sum(transfer.amount_native for transfer in available)
		mature = [transfer for transfer in available if transfer.confirmations >= 1]
		timely = [
			transfer
			for transfer in mature
			if transfer.block_time_epoch is not None and transfer.block_time_epoch <= intent.expires_at_epoch
		]
		late = [transfer for transfer in mature if transfer not in timely]
		credited = sum(transfer.amount_native for transfer in timely)
		if credited >= intent.amount_native:
			reason = "one-confirmation Bitcoin Testnet 4 gate passed"
			if credited > intent.amount_native:
				reason += "; payment exceeds the invoice"
			return SettlementDecision(
				SETTLED,
				credited,
				sighted,
				tuple(sorted(transfer.transaction_id for transfer in timely)),
				reason,
			)
		if claimed and sum(transfer.amount_native for transfer in claimed) + sighted >= intent.amount_native:
			return SettlementDecision(
				NEEDS_REVIEW,
				credited,
				sighted,
				reason="one or more observed transactions are already claimed by another intent",
			)
		if late and credited + sum(transfer.amount_native for transfer in late) >= intent.amount_native:
			return SettlementDecision(
				NEEDS_REVIEW,
				credited,
				sighted,
				reason="payment arrived after expiry or lacks a trustworthy block time",
			)
		reason = "payment seen but awaiting a block" if sighted else "no payment observed"
		if credited:
			reason = "confirmed payment is below the invoice amount"
		return SettlementDecision(PENDING, credited, sighted, reason=reason)

	def _intent(self, intent):
		if not isinstance(intent, PaymentIntent) or intent.rail_key != self.key:
			raise InvalidRailPlugin("payment intent belongs to another rail")

	def _parse_transfers(self, intent, tip, transactions):
		seen = set()
		transfers = []
		for transaction in transactions:
			if not isinstance(transaction, dict):
				raise RailProviderError(self.key, "transaction entry was not an object")
			txid = transaction.get("txid")
			if not isinstance(txid, str) or not _TXID.fullmatch(txid):
				raise RailProviderError(
					self.key, "transaction id was not 64 lowercase hexadecimal characters"
				)
			if txid in seen:
				continue
			seen.add(txid)
			if txid in intent.baseline.transaction_ids:
				continue
			vout = transaction.get("vout")
			vin = transaction.get("vin", [])
			if not isinstance(vout, list) or len(vout) > MAX_TRANSACTION_OUTPUTS:
				raise RailProviderError(self.key, "transaction outputs were malformed or excessive")
			if not isinstance(vin, list) or len(vin) > MAX_TRANSACTION_INPUTS:
				raise RailProviderError(self.key, "transaction inputs were malformed or excessive")
			if self._spends_from_recipient(vin, intent.recipient):
				continue
			amount = 0
			for output in vout:
				if not isinstance(output, dict):
					raise RailProviderError(self.key, "transaction output was not an object")
				value = _exact_nonnegative(output.get("value"), "vout.value")
				address = output.get("scriptpubkey_address")
				if address is not None and not isinstance(address, str):
					raise RailProviderError(self.key, "output address was not text")
				if address == intent.recipient:
					amount += value
			if amount == 0:
				continue
			status = transaction.get("status")
			if not isinstance(status, dict) or not isinstance(status.get("confirmed"), bool):
				raise RailProviderError(self.key, "transaction confirmation status was malformed")
			if not status["confirmed"]:
				transfers.append(TransferObservation(txid, amount, False))
				continue
			height = _exact_nonnegative(status.get("block_height"), "status.block_height")
			if height <= intent.baseline.tip:
				continue
			if height > tip:
				raise RailProviderError(self.key, "transaction block height is above the provider tip")
			block_time = _exact_nonnegative(status.get("block_time"), "status.block_time")
			confirmations = tip - height + 1
			transfers.append(TransferObservation(txid, amount, True, confirmations, height, block_time))
		return transfers

	def _spends_from_recipient(self, inputs, recipient):
		for transaction_input in inputs:
			if not isinstance(transaction_input, dict):
				raise RailProviderError(self.key, "transaction input was not an object")
			previous = transaction_input.get("prevout")
			if previous is None:
				continue
			if not isinstance(previous, dict):
				raise RailProviderError(self.key, "transaction input prevout was malformed")
			address = previous.get("scriptpubkey_address")
			if address is not None and not isinstance(address, str):
				raise RailProviderError(self.key, "input address was not text")
			if address == recipient:
				return True
		return False


bitcoin_testnet4 = BitcoinTestnet4()

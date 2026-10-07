"""Operational Amazon/ShipStation clients. No credentials or network calls at import.

Read requests have bounded retries; mutations are single-attempt and must be
called through the execution journal. Only four non-purchasing writes exist.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
import json
import os
import re
import threading
import time
from typing import Any, Callable
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

from .fba import Blocked, canonical, integer, text

INBOUND = '/inbound/fba/2024-03-20'
HOSTS = {'https://sellingpartnerapi-na.amazon.com', 'https://api.shipstation.com'}


class ApiError(RuntimeError):
    """Sanitized error: never include URLs, headers, bodies or credential values."""
    def __init__(self, status: int = 0, request_id: str | None = None):
        self.status, self.request_id = status, request_id
        self.ambiguous = status == 0 or status >= 500 or 300 <= status < 400
        super().__init__(f'API request failed (HTTP {status or "unknown"}); '
                         f'{"reconcile before retry" if self.ambiguous else "request rejected"}')


@dataclass(frozen=True)
class Reply:
    status: int
    body: Any
    request_id: str | None = None
    retry_after: float = 0

    def require_success(self) -> 'Reply':
        if not 200 <= self.status < 300:
            raise ApiError(self.status, self.request_id)
        return self


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None  # Do not forward Amazon/ShipStation credentials to a redirect.


class HttpTransport:
    def __init__(self, timeout: float = 20):
        self.timeout = timeout
        self.opener = build_opener(NoRedirect())

    def __call__(self, method: str, url: str, headers: dict, body: bytes | None) -> Reply:
        parts = urlsplit(url)
        root = f'{parts.scheme}://{parts.netloc}'
        if root not in HOSTS and url != 'https://api.amazon.com/auth/o2/token':
            raise Blocked('unapproved API origin')
        request = Request(url, data=body, headers=headers, method=method)
        try:
            try:
                response = self.opener.open(request, timeout=self.timeout)
            except HTTPError as exc:
                response = exc
            with response:
                raw = response.read(10_000_001)
                if len(raw) > 10_000_000:
                    raise ApiError(0)
                rid = response.headers.get('x-amzn-RequestId') or response.headers.get('x-request-id')
                try:
                    retry = min(60.0, max(0.0, float(response.headers.get('Retry-After', '0'))))
                except ValueError:
                    retry = 0
                # Error bodies may echo request data. Do not retain them.
                if not 200 <= response.code < 300:
                    return Reply(response.code, {}, rid, retry)
                try:
                    data = json.loads(raw) if raw else {}
                except (ValueError, UnicodeError):
                    raise ApiError(0, rid) from None
                return Reply(response.code, data, rid, retry)
        except (URLError, OSError, TimeoutError):
            raise ApiError(0) from None


@dataclass(repr=False)
class LWAToken:
    client_id: str
    client_secret: str
    refresh_token: str
    transport: Callable
    clock: Callable = time.monotonic
    _token: str = field(default='', init=False)
    _expires: float = field(default=0, init=False)
    _lock: Any = field(default_factory=threading.Lock, init=False)

    @classmethod
    def from_env(cls, transport: Callable) -> 'LWAToken':
        names = ('FBA_LWA_CLIENT_ID', 'FBA_LWA_CLIENT_SECRET', 'FBA_LWA_REFRESH_TOKEN')
        if any(not os.environ.get(n) for n in names):
            raise Blocked('runtime LWA credentials are not configured')
        return cls(*(os.environ[n] for n in names), transport)

    def get(self) -> str:
        with self._lock:
            if self._token and self.clock() < self._expires:
                return self._token
            body = urlencode(dict(grant_type='refresh_token', client_id=self.client_id,
                                  client_secret=self.client_secret, refresh_token=self.refresh_token)).encode()
            result = self.transport('POST', 'https://api.amazon.com/auth/o2/token',
                                    {'Content-Type': 'application/x-www-form-urlencoded'}, body).require_success()
            try:
                token = text(result.body['access_token'], 'LWA access token')
                seconds = integer(result.body['expires_in'], 'token lifetime', positive=True)
                if result.body.get('token_type', '').lower() != 'bearer':
                    raise Blocked('invalid token type')
            except (KeyError, TypeError, Blocked):
                raise ApiError(0) from None
            self._token, self._expires = token, self.clock() + max(0, seconds - 60)
            return token


def api_id(value: str, name: str, *, operation: bool = False) -> str:
    value = text(value, name)
    lengths = {36, 37, 38} if operation else {38}
    if len(value) not in lengths or not re.fullmatch(r'[A-Za-z0-9-]+', value):
        raise Blocked(f'{name}: current API identifier required, not an FBA confirmation/label ID')
    return value


class Amazon:
    def __init__(self, token: LWAToken, seller_id: str, marketplace_id: str,
                 transport: Callable | None = None, sleep: Callable = time.sleep):
        self.token = token
        self.seller_id = text(seller_id, 'seller_id')
        self.marketplace_id = text(marketplace_id, 'marketplace_id')
        self.transport, self.sleep = transport or HttpTransport(), sleep
        self.scope = f'amazon:NA:{seller_id}:{marketplace_id}'

    def _request(self, method: str, path: str, params: dict | None = None, body=None) -> Reply:
        url = 'https://sellingpartnerapi-na.amazon.com' + path
        if params:
            url += '?' + urlencode(params)
        headers = {'x-amz-access-token': self.token.get(), 'Content-Type': 'application/json',
                   'Accept': 'application/json', 'x-amz-date': datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ'),
                   'User-Agent': 'DIYFBA/0.2 (Language=Python)'}
        attempts = 3 if method == 'GET' else 1
        for attempt in range(attempts):
            result = self.transport(method, url, headers, None if body is None else canonical(body).encode())
            if method == 'GET' and result.status in (429, 500, 502, 503, 504) and attempt + 1 < attempts:
                self.sleep(max(result.retry_after, 2 ** attempt))
                continue
            return result.require_success()
        raise ApiError(0)

    def pages(self, path: str, collection: str, params: dict | None = None,
              *, inventory: bool = False, max_pages: int = 1000) -> dict:
        query, rows, requests, seen = dict(params or {}), [], [], set()
        started = datetime.now(timezone.utc).isoformat()
        for _ in range(max_pages):
            result = self._request('GET', path, query)
            data = result.body
            try:
                batch = data['payload'][collection] if inventory else data[collection]
                if not isinstance(batch, list) or any(not isinstance(r, dict) for r in batch):
                    raise TypeError()
                rows.extend(batch)
                requests.append(result.request_id)
                token = data.get('pagination', {}).get('nextToken') if inventory else data.get('pagination', {}).get('nextToken')
                # Inbound v2024-03-20 uses pagination.nextToken and paginationToken in the next request.
                if token is None:
                    return {'scope': self.scope, 'observed_at': datetime.now(timezone.utc).isoformat(),
                            'started_at': started, 'complete': True, 'atomic_snapshot': False, 'rows': rows, 'request_ids': requests}
                token = text(token, 'next token')
                if token in seen:
                    raise Blocked('pagination token repeated; snapshot is incomplete')
                seen.add(token)
                query['nextToken' if inventory else 'paginationToken'] = token
            except (KeyError, TypeError, AttributeError):
                raise Blocked('API response schema changed; snapshot is incomplete') from None
        raise Blocked('pagination limit reached; snapshot is incomplete')

    def inventory(self) -> dict:
        return self.pages('/fba/inventory/v1/summaries', 'inventorySummaries',
                          {'details': 'true', 'granularityType': 'Marketplace',
                           'granularityId': self.marketplace_id, 'marketplaceIds': self.marketplace_id}, inventory=True)

    def listing(self, sku: str) -> Reply:
        return self._request('GET', f'/listings/2021-08-01/items/{quote(self.seller_id, safe="")}/{quote(text(sku, "sku"), safe="")}',
                             {'marketplaceIds': self.marketplace_id,
                              'includedData': 'summaries,attributes,issues,offers,fulfillmentAvailability'})

    def inbound_plans(self) -> dict:
        return self.pages(INBOUND + '/inboundPlans', 'inboundPlans', {'pageSize': 30})

    def plan(self, plan_id: str) -> Reply:
        return self._request('GET', INBOUND + '/inboundPlans/' + api_id(plan_id, 'inboundPlanId'))

    def plan_items(self, plan_id: str) -> dict:
        return self.pages(INBOUND + '/inboundPlans/' + api_id(plan_id, 'inboundPlanId') + '/items', 'items', {'pageSize': 100})

    def shipment(self, plan_id: str, shipment_id: str) -> Reply:
        return self._request('GET', INBOUND + '/inboundPlans/' + api_id(plan_id, 'inboundPlanId') + '/shipments/' + api_id(shipment_id, 'shipmentId'))

    def shipment_boxes(self, plan_id: str, shipment_id: str) -> dict:
        return self.pages(INBOUND + '/inboundPlans/' + api_id(plan_id, 'inboundPlanId') + '/shipments/' + api_id(shipment_id, 'shipmentId') + '/boxes', 'boxes', {'pageSize': 100})

    def operation(self, operation_id: str) -> Reply:
        return self._request('GET', INBOUND + '/operations/' + api_id(operation_id, 'operationId', operation=True))

    def dispatch(self, operation: str, ids: dict, body: dict) -> Reply:
        """Low-level mutation. Use Journal.execute, never an unattended direct call."""
        if operation == 'createInboundPlan':
            if body.get('destinationMarketplaces') != [self.marketplace_id]:
                raise Blocked('marketplace mismatch')
            return self._request('POST', INBOUND + '/inboundPlans', body=body)
        path = INBOUND + '/inboundPlans/' + api_id(ids.get('inboundPlanId'), 'inboundPlanId')
        if operation == 'setPackingInformation':
            return self._request('POST', path + '/packingInformation', body=body)
        if operation == 'updateShipmentTrackingDetails':
            path += '/shipments/' + api_id(ids.get('shipmentId'), 'shipmentId') + '/trackingDetails'
            return self._request('PUT', path, body=body)
        raise Blocked('unsupported Amazon mutation; purchases and listing changes are not implemented')


class ShipStation:
    def __init__(self, api_key: str, account_ref: str, transport: Callable | None = None):
        self._api_key = text(api_key, 'ShipStation API key')
        self.scope = 'shipstation:' + text(account_ref, 'account_ref')
        self.transport = transport or HttpTransport()

    def _request(self, method: str, path: str, body=None) -> Reply:
        return self.transport(method, 'https://api.shipstation.com/v2' + path,
                              {'api-key': self._api_key, 'Content-Type': 'application/json', 'Accept': 'application/json'},
                              None if body is None else canonical(body).encode()).require_success()

    def shipment(self, shipment_id: str) -> Reply:
        return self._request('GET', '/shipments/' + quote(text(shipment_id, 'shipment_id'), safe=''))

    def dispatch(self, operation: str, ids: dict, body: dict) -> Reply:
        if operation != 'createShipStationShipment':
            raise Blocked('unsupported ShipStation mutation; label purchase is not implemented')
        return self._request('POST', '/shipments', body)

#!/usr/bin/env python3
"""Shippo Logistics Provider — multi-carrier tracking + rates.

Environment variables:
    SHIPPO_TOKEN  (live API token, or test_... for test mode)
"""
import os, json, urllib.request
from .base import BaseLogisticsProvider


class ShippoProvider(BaseLogisticsProvider):
    """Shippo API — tracking and rate calculation."""

    PROVIDER = 'shippo'
    API_BASE = 'https://api.goshippo.com'

    def __init__(self):
        self._token = os.environ.get('SHIPPO_TOKEN', '')

    def is_configured(self) -> bool:
        return bool(self._token)

    def _headers(self):
        return {
            'Authorization': f'ShippoToken {self._token}',
            'Content-Type': 'application/json',
        }

    def track(self, tracking_number: str, carrier: str = '', **kwargs) -> dict:
        if not self.is_configured():
            return {'success': False, 'error': 'Shippo not configured'}
        try:
            body = {'tracking_number': tracking_number}
            if carrier:
                body['carrier'] = carrier
            data = json.dumps(body).encode()
            req = urllib.request.Request(
                f'{self.API_BASE}/tracks/',
                data=data, headers=self._headers(), method='POST'
            )
            resp = urllib.request.urlopen(req, timeout=15)
            result = json.loads(resp.read().decode())
            last_event = (result.get('tracking_status') or {}).get('status', '')
            location = (result.get('tracking_status') or {}).get('location', {})
            return {
                'success': True,
                'status': last_event,
                'location': location,
                'estimated_delivery': result.get('eta', ''),
                'events': result.get('tracking_history', []),
                'error': '',
            }
        except Exception as e:
            return {'success': False, 'error': str(e)}

    def get_rates(self, origin: dict, destination: dict,
                  parcel: dict, **kwargs) -> dict:
        if not self.is_configured():
            return {'success': False, 'error': 'Shippo not configured'}
        try:
            body = {
                'address_from': {
                    'country': origin.get('country', ''),
                    'state': origin.get('state', ''),
                    'city': origin.get('city', ''),
                    'zip': origin.get('zip', ''),
                },
                'address_to': {
                    'country': destination.get('country', ''),
                    'state': destination.get('state', ''),
                    'city': destination.get('city', ''),
                    'zip': destination.get('zip', ''),
                },
                'parcels': [{
                    'weight': parcel.get('weight', 1),
                    'distance_unit': parcel.get('unit', 'lb'),
                    'mass_unit': 'lb',
                }],
            }
            for dim in ('length', 'width', 'height'):
                if dim in parcel:
                    body['parcels'][0][dim] = parcel[dim]
            data = json.dumps(body).encode()
            req = urllib.request.Request(
                f'{self.API_BASE}/shipments/',
                data=data, headers=self._headers(), method='POST'
            )
            resp = urllib.request.urlopen(req, timeout=15)
            result = json.loads(resp.read().decode())
            rates = [
                {
                    'provider': r.get('provider', ''),
                    'service': r.get('servicelevel', {}).get('name', ''),
                    'amount': r.get('amount', ''),
                    'currency': r.get('currency', ''),
                    'days': r.get('days', ''),
                }
                for r in result.get('rates', [])
            ]
            return {'success': True, 'rates': rates, 'error': ''}
        except Exception as e:
            return {'success': False, 'error': str(e)}

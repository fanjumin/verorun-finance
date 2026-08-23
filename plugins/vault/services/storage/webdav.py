#!/usr/bin/env python3
"""
Vault WebDAV Storage Adapter (NextCloud, ownCloud, generic WebDAV).
"""

import os
import requests
from .base import BaseStorageAdapter


class WebDAVAdapter(BaseStorageAdapter):
    """WebDAV storage adapter using HTTP requests."""

    def __init__(self, config: dict):
        super().__init__(config)
        self.url = config['url'].rstrip('/')
        self.username = config.get('username', '')
        self.password = config.get('password', '')
        self.remote_path = config.get('remote_path', '/backups')
        self._session = None

    def _get_session(self):
        if self._session:
            return self._session
        self._session = requests.Session()
        if self.username:
            self._session.auth = (self.username, self.password)
        self._session.headers['User-Agent'] = 'VeroRun Vault/2.0'
        return self._session

    def _ensure_dirs(self, remote_subpath: str):
        """Recursively create remote directories via MKCOL."""
        session = self._get_session()
        parts = remote_subpath.strip('/').split('/')
        current = self.remote_path.rstrip('/')
        for part in parts:
            if not part:
                continue
            current += '/' + part
            try:
                resp = session.request('MKCOL', self.url + current)
            except Exception:
                pass

    def _full_url(self, object_name: str) -> str:
        return self.url + self.remote_path.rstrip('/') + '/' + object_name.lstrip('/')

    def upload(self, file_path: str, object_name: str) -> bool:
        try:
            session = self._get_session()
            # Ensure parent directories exist
            parent = '/'.join(object_name.strip('/').split('/')[:-1])
            if parent:
                self._ensure_dirs(parent)
            url = self._full_url(object_name)
            with open(file_path, 'rb') as f:
                resp = session.put(url, data=f, timeout=300)
            return resp.status_code in (200, 201, 204)
        except Exception as e:
            print(f'[Vault/WebDAV] Upload failed: {e}')
            return False

    def download(self, object_name: str, file_path: str) -> bool:
        try:
            session = self._get_session()
            url = self._full_url(object_name)
            resp = session.get(url, timeout=300)
            if resp.status_code != 200:
                return False
            with open(file_path, 'wb') as f:
                f.write(resp.content)
            return True
        except Exception as e:
            print(f'[Vault/WebDAV] Download failed: {e}')
            return False

    def delete(self, object_name: str) -> bool:
        try:
            session = self._get_session()
            url = self._full_url(object_name)
            resp = session.delete(url, timeout=30)
            return resp.status_code in (200, 204)
        except Exception as e:
            print(f'[Vault/WebDAV] Delete failed: {e}')
            return False

    def list_objects(self, prefix: str = '') -> list:
        try:
            session = self._get_session()
            url = self.url + self.remote_path.rstrip('/') + '/'
            # PROPFIND request
            resp = session.request('PROPFIND', url, headers={'Depth': '1'}, timeout=30)
            if resp.status_code not in (207,):
                return []
            # Simple XML parsing for file names
            import xml.etree.ElementTree as ET
            root = ET.fromstring(resp.content)
            ns = {'d': 'DAV:'}
            results = []
            for response in root.findall('d:response', ns):
                href = response.find('d:href', ns)
                if href is not None:
                    name = href.text.rstrip('/').split('/')[-1]
                    if name and '.tar.gz' in name and name.startswith(prefix):
                        results.append(name)
            return results
        except Exception as e:
            print(f'[Vault/WebDAV] List failed: {e}')
            return []

    def test_connection(self) -> dict:
        try:
            session = self._get_session()
            url = self.url + self.remote_path.rstrip('/') + '/'
            resp = session.request('PROPFIND', url, headers={'Depth': '0'}, timeout=10)
            if resp.status_code in (207, 200, 301, 302):
                return {'ok': True, 'error': None}
            return {'ok': False, 'error': f'HTTP {resp.status_code}: {resp.text[:200]}'}
        except Exception as e:
            return {'ok': False, 'error': str(e)}

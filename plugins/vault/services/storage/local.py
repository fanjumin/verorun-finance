#!/usr/bin/env python3
"""
Vault Local Storage Adapter — local filesystem copy.

Implements BaseStorageAdapter interface for local directories.
"""

import os
import shutil
from .base import BaseStorageAdapter


class LocalAdapter(BaseStorageAdapter):
    """Local filesystem storage adapter for backup file copying."""

    def __init__(self, config: dict):
        super().__init__(config)
        self.base_path = config.get('path', self._default_path())

    @staticmethod
    def _default_path() -> str:
        return os.path.join(os.path.dirname(os.path.abspath(__file__)),
                            '..', '..', '..', 'data', 'vault')

    def _ensure_dir(self):
        os.makedirs(self.base_path, exist_ok=True)

    def upload(self, file_path: str, object_name: str) -> bool:
        try:
            self._ensure_dir()
            dest = os.path.join(self.base_path, object_name)
            shutil.copy2(file_path, dest)
            print(f'[Vault/Local] Copied: {dest}')
            return True
        except Exception as e:
            print(f'[Vault/Local] Upload failed: {e}')
            return False

    def download(self, object_name: str, file_path: str) -> bool:
        try:
            src = os.path.join(self.base_path, object_name)
            if not os.path.isfile(src):
                print(f'[Vault/Local] File not found: {src}')
                return False
            shutil.copy2(src, file_path)
            return True
        except Exception as e:
            print(f'[Vault/Local] Download failed: {e}')
            return False

    def delete(self, object_name: str) -> bool:
        try:
            path = os.path.join(self.base_path, object_name)
            if os.path.isfile(path):
                os.remove(path)
            return True
        except Exception as e:
            print(f'[Vault/Local] Delete failed: {e}')
            return False

    def list_objects(self, prefix: str = '') -> list:
        try:
            self._ensure_dir()
            return [f for f in os.listdir(self.base_path)
                    if f.startswith(prefix)]
        except Exception as e:
            print(f'[Vault/Local] List failed: {e}')
            return []

    def test_connection(self) -> dict:
        try:
            self._ensure_dir()
            if os.access(self.base_path, os.W_OK):
                return {'ok': True, 'error': None}
            return {'ok': False, 'error': f'Directory not writable: {self.base_path}'}
        except Exception as e:
            return {'ok': False, 'error': str(e)}

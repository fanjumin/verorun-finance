#!/usr/bin/env python3
"""
Vault SFTP Storage Adapter.

Supports key-based and password authentication with auto-reconnect.
"""

import os
import paramiko
from .base import BaseStorageAdapter


class SFTPAdapter(BaseStorageAdapter):
    """SFTP remote storage adapter."""

    def __init__(self, config: dict):
        super().__init__(config)
        self.host = config['host']
        self.port = int(config.get('port', 22))
        self.username = config['username']
        self.password = config.get('password', '')
        self.private_key_path = config.get('private_key_path', '')
        self.remote_path = config.get('remote_path', '/backups')
        self._transport = None
        self._sftp = None

    def _connect(self):
        """Establish SFTP connection."""
        self._transport = paramiko.Transport((self.host, self.port))
        if self.private_key_path and os.path.exists(self.private_key_path):
            key = paramiko.RSAKey.from_private_key_file(self.private_key_path)
            self._transport.connect(username=self.username, pkey=key)
        else:
            self._transport.connect(username=self.username, password=self.password)
        self._sftp = paramiko.SFTPClient.from_transport(self._transport)

    def _disconnect(self):
        """Close SFTP connection."""
        if self._sftp:
            self._sftp.close()
        if self._transport:
            self._transport.close()

    def _ensure_dir(self, remote_dir: str):
        """Recursively create remote directory."""
        dirs = remote_dir.strip('/').split('/')
        current = ''
        for d in dirs:
            if not d:
                continue
            current += f'/{d}'
            try:
                self._sftp.stat(current)
            except FileNotFoundError:
                self._sftp.mkdir(current)

    def upload(self, file_path: str, object_name: str) -> bool:
        try:
            self._connect()
            self._ensure_dir(self.remote_path)
            remote_full = os.path.join(self.remote_path, object_name).replace('\\', '/')
            self._sftp.put(file_path, remote_full)
            print(f'[Vault/SFTP] Uploaded: {remote_full}')
            return True
        except Exception as e:
            print(f'[Vault/SFTP] Upload failed: {e}')
            return False
        finally:
            self._disconnect()

    def download(self, object_name: str, file_path: str) -> bool:
        try:
            self._connect()
            remote_full = os.path.join(self.remote_path, object_name).replace('\\', '/')
            self._sftp.get(remote_full, file_path)
            return True
        except Exception as e:
            print(f'[Vault/SFTP] Download failed: {e}')
            return False
        finally:
            self._disconnect()

    def delete(self, object_name: str) -> bool:
        try:
            self._connect()
            remote_full = os.path.join(self.remote_path, object_name).replace('\\', '/')
            self._sftp.remove(remote_full)
            return True
        except Exception as e:
            print(f'[Vault/SFTP] Delete failed: {e}')
            return False
        finally:
            self._disconnect()

    def list_objects(self, prefix: str = '') -> list:
        try:
            self._connect()
            files = self._sftp.listdir(self.remote_path)
            return [f for f in files if f.startswith(prefix)]
        except Exception as e:
            print(f'[Vault/SFTP] List failed: {e}')
            return []
        finally:
            self._disconnect()

    def test_connection(self) -> dict:
        try:
            self._connect()
            self._sftp.listdir(self.remote_path)
            return {'ok': True, 'error': None}
        except Exception as e:
            return {'ok': False, 'error': str(e)}
        finally:
            self._disconnect()

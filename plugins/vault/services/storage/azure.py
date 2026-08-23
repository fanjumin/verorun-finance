#!/usr/bin/env python3
"""
Vault Azure Blob Storage Adapter.
"""

import os
from .base import BaseStorageAdapter


class AzureAdapter(BaseStorageAdapter):
    """Azure Blob Storage adapter using azure-storage-blob."""

    def __init__(self, config: dict):
        super().__init__(config)
        self.connection_string = config.get('connection_string', '')
        self.container_name = config['container']
        self._client = None

    def _get_client(self):
        if self._client:
            return self._client
        from azure.storage.blob import BlobServiceClient
        self._client = BlobServiceClient.from_connection_string(self.connection_string)
        # Ensure container exists
        try:
            self._client.create_container(self.container_name)
        except Exception:
            pass
        return self._client

    def _get_container_client(self):
        client = self._get_client()
        return client.get_container_client(self.container_name)

    def upload(self, file_path: str, object_name: str) -> bool:
        try:
            container = self._get_container_client()
            with open(file_path, 'rb') as f:
                container.upload_blob(name=object_name, data=f, overwrite=True)
            return True
        except Exception as e:
            print(f'[Vault/Azure] Upload failed: {e}')
            return False

    def download(self, object_name: str, file_path: str) -> bool:
        try:
            container = self._get_container_client()
            with open(file_path, 'wb') as f:
                container.download_blob(object_name).readinto(f)
            return True
        except Exception as e:
            print(f'[Vault/Azure] Download failed: {e}')
            return False

    def delete(self, object_name: str) -> bool:
        try:
            container = self._get_container_client()
            container.delete_blob(object_name)
            return True
        except Exception as e:
            print(f'[Vault/Azure] Delete failed: {e}')
            return False

    def list_objects(self, prefix: str = '') -> list:
        try:
            container = self._get_container_client()
            return [blob.name for blob in container.list_blobs(name_starts_with=prefix)]
        except Exception as e:
            print(f'[Vault/Azure] List failed: {e}')
            return []

    def test_connection(self) -> dict:
        try:
            container = self._get_container_client()
            next(container.list_blobs(results_per_page=1), None)
            return {'ok': True, 'error': None}
        except Exception as e:
            return {'ok': False, 'error': str(e)}

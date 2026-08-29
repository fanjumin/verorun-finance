#!/usr/bin/env python3
"""
Vault Google Cloud Storage Adapter.
"""

import os
from .base import BaseStorageAdapter


class GCSAdapter(BaseStorageAdapter):
    """Google Cloud Storage adapter using google-cloud-storage."""

    def __init__(self, config: dict):
        super().__init__(config)
        self.bucket_name = config['bucket']
        self.credentials_path = config.get('credentials_path', '')
        self.project = config.get('project', '')
        self._client = None

    def _get_client(self):
        if self._client:
            return self._client
        from google.cloud import storage
        if self.credentials_path and os.path.exists(self.credentials_path):
            self._client = storage.Client.from_service_account_json(
                self.credentials_path, project=self.project or None
            )
        else:
            self._client = storage.Client(project=self.project or None)
        return self._client

    def _get_bucket(self):
        client = self._get_client()
        return client.bucket(self.bucket_name)

    def upload(self, file_path: str, object_name: str) -> bool:
        try:
            bucket = self._get_bucket()
            blob = bucket.blob(object_name)
            blob.upload_from_filename(file_path)
            return True
        except Exception as e:
            print(f'[Vault/GCS] Upload failed: {e}')
            return False

    def download(self, object_name: str, file_path: str) -> bool:
        try:
            bucket = self._get_bucket()
            blob = bucket.blob(object_name)
            blob.download_to_filename(file_path)
            return True
        except Exception as e:
            print(f'[Vault/GCS] Download failed: {e}')
            return False

    def delete(self, object_name: str) -> bool:
        try:
            bucket = self._get_bucket()
            blob = bucket.blob(object_name)
            blob.delete()
            return True
        except Exception as e:
            print(f'[Vault/GCS] Delete failed: {e}')
            return False

    def list_objects(self, prefix: str = '') -> list:
        try:
            bucket = self._get_bucket()
            return [blob.name for blob in bucket.list_blobs(prefix=prefix)]
        except Exception as e:
            print(f'[Vault/GCS] List failed: {e}')
            return []

    def test_connection(self) -> dict:
        try:
            bucket = self._get_bucket()
            bucket.exists()
            return {'ok': True, 'error': None}
        except Exception as e:
            return {'ok': False, 'error': str(e)}

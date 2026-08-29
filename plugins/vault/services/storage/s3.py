#!/usr/bin/env python3
"""
Vault S3 Storage Adapter (AWS S3 + S3-compatible: MinIO, Wasabi, Cloudflare R2).
"""

import os
from .base import BaseStorageAdapter


class S3Adapter(BaseStorageAdapter):
    """S3-compatible storage adapter using boto3."""

    def __init__(self, config: dict):
        super().__init__(config)
        self.bucket = config['bucket']
        self.region = config.get('region', 'us-east-1')
        self.access_key = config.get('access_key', '')
        self.secret_key = config.get('secret_key', '')
        self.endpoint = config.get('endpoint', '')
        self._client = None

    def _get_client(self):
        if self._client:
            return self._client
        import boto3
        kwargs = {
            'service_name': 's3',
            'region_name': self.region,
            'aws_access_key_id': self.access_key,
            'aws_secret_access_key': self.secret_key,
        }
        if self.endpoint:
            kwargs['endpoint_url'] = self.endpoint
        self._client = boto3.client(**kwargs)
        return self._client

    def upload(self, file_path: str, object_name: str) -> bool:
        try:
            client = self._get_client()
            client.upload_file(file_path, self.bucket, object_name)
            return True
        except Exception as e:
            print(f'[Vault/S3] Upload failed: {e}')
            return False

    def download(self, object_name: str, file_path: str) -> bool:
        try:
            client = self._get_client()
            client.download_file(self.bucket, object_name, file_path)
            return True
        except Exception as e:
            print(f'[Vault/S3] Download failed: {e}')
            return False

    def delete(self, object_name: str) -> bool:
        try:
            client = self._get_client()
            client.delete_object(Bucket=self.bucket, Key=object_name)
            return True
        except Exception as e:
            print(f'[Vault/S3] Delete failed: {e}')
            return False

    def list_objects(self, prefix: str = '') -> list:
        try:
            client = self._get_client()
            resp = client.list_objects_v2(Bucket=self.bucket, Prefix=prefix)
            return [obj['Key'] for obj in resp.get('Contents', [])]
        except Exception as e:
            print(f'[Vault/S3] List failed: {e}')
            return []

    def test_connection(self) -> dict:
        try:
            client = self._get_client()
            client.head_bucket(Bucket=self.bucket)
            return {'ok': True, 'error': None}
        except Exception as e:
            return {'ok': False, 'error': str(e)}

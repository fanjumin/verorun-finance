#!/usr/bin/env python3
"""
Vault OSS Storage Adapter (Alibaba Cloud Object Storage Service).
"""

import os
from .base import BaseStorageAdapter


class OSSAdapter(BaseStorageAdapter):
    """Alibaba Cloud OSS storage adapter using oss2."""

    def __init__(self, config: dict):
        super().__init__(config)
        self.bucket_name = config['bucket']
        self.endpoint = config.get('endpoint', 'https://oss-cn-hangzhou.aliyuncs.com')
        self.access_key = config.get('access_key', '')
        self.secret_key = config.get('secret_key', '')
        self._bucket = None

    def _get_bucket(self):
        if self._bucket:
            return self._bucket
        import oss2
        auth = oss2.Auth(self.access_key, self.secret_key)
        self._bucket = oss2.Bucket(auth, self.endpoint, self.bucket_name)
        return self._bucket

    def upload(self, file_path: str, object_name: str) -> bool:
        try:
            bucket = self._get_bucket()
            bucket.put_object_from_file(object_name, file_path)
            return True
        except Exception as e:
            print(f'[Vault/OSS] Upload failed: {e}')
            return False

    def download(self, object_name: str, file_path: str) -> bool:
        try:
            bucket = self._get_bucket()
            bucket.get_object_to_file(object_name, file_path)
            return True
        except Exception as e:
            print(f'[Vault/OSS] Download failed: {e}')
            return False

    def delete(self, object_name: str) -> bool:
        try:
            bucket = self._get_bucket()
            bucket.delete_object(object_name)
            return True
        except Exception as e:
            print(f'[Vault/OSS] Delete failed: {e}')
            return False

    def list_objects(self, prefix: str = '') -> list:
        try:
            bucket = self._get_bucket()
            return [obj.key for obj in bucket.list_objects(prefix=prefix).object_list]
        except Exception as e:
            print(f'[Vault/OSS] List failed: {e}')
            return []

    def test_connection(self) -> dict:
        try:
            bucket = self._get_bucket()
            bucket.get_bucket_info()
            return {'ok': True, 'error': None}
        except Exception as e:
            return {'ok': False, 'error': str(e)}

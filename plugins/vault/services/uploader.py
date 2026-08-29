#!/usr/bin/env python3
"""
Vault Uploader — S3/OSS 远程存储上传
======================================
支持 AWS S3 (boto3) 和阿里云 OSS (oss2)。
"""

import os
import sys
from datetime import datetime

BASE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', '..', '..')
sys.path.insert(0, os.path.join(BASE_DIR, 'auth-center'))


def get_upload_config():
    """从 plugin_registry 读取 storage 配置."""
    try:
        from plugins._base.db import get_raw_connection
        conn = get_raw_connection()
        cur = conn.cursor()
        cur.execute(
            "SELECT config FROM plugin_registry WHERE identifier = %s",
            ('vault',)
        )
        row = cur.fetchone()
        cur.close()
        conn.close()
        if row and row[0]:
            import json
            cfg = json.loads(row[0]) if isinstance(row[0], str) else row[0]
            return cfg.get('storage', {})
    except Exception as e:
        print(f'[Vault/Upload] Config read error: {e}')
    return {}


def upload_to_s3(file_path: str, object_name: str, config: dict) -> bool:
    """上传文件到 AWS S3."""
    try:
        import boto3
        session = boto3.Session(
            aws_access_key_id=config.get('s3_access_key', ''),
            aws_secret_access_key=config.get('s3_secret_key', ''),
        )
        s3 = session.client('s3', region_name=config.get('s3_region', ''))
        bucket = config.get('s3_bucket', '')
        if not bucket:
            print('[Vault/Upload] S3 bucket not configured')
            return False
        s3.upload_file(file_path, bucket, object_name)
        print(f'[Vault/Upload] S3 upload complete: s3://{bucket}/{object_name}')
        return True
    except ImportError:
        print('[Vault/Upload] boto3 not installed, S3 upload skipped')
        return False
    except Exception as e:
        print(f'[Vault/Upload] S3 upload failed: {e}')
        return False


def upload_to_oss(file_path: str, object_name: str, config: dict) -> bool:
    """上传文件到阿里云 OSS."""
    try:
        import oss2
        endpoint = config.get('oss_endpoint', '')
        bucket_name = config.get('oss_bucket', '')
        access_key = config.get('oss_access_key', '')
        secret_key = config.get('oss_secret_key', '')
        if not all([endpoint, bucket_name, access_key, secret_key]):
            print('[Vault/Upload] OSS not fully configured')
            return False
        auth = oss2.Auth(access_key, secret_key)
        bucket = oss2.Bucket(auth, endpoint, bucket_name)
        bucket.put_object_from_file(object_name, file_path)
        print(f'[Vault/Upload] OSS upload complete: {bucket_name}/{object_name}')
        return True
    except ImportError:
        print('[Vault/Upload] oss2 not installed, OSS upload skipped')
        return False
    except Exception as e:
        print(f'[Vault/Upload] OSS upload failed: {e}')
        return False


def upload_backup(file_path: str, object_name: str) -> dict:
    """
    上传备份文件到远程存储。
    根据 plugin config 自动选择 S3 或 OSS。
    返回 {'uploaded': bool, 'target': str, 'error': str|None}
    """
    config = get_upload_config()
    storage_type = config.get('type', 'local')

    if storage_type == 's3':
        ok = upload_to_s3(file_path, object_name, config)
        return {'uploaded': ok, 'target': f's3://{config.get("s3_bucket", "")}/{object_name}', 'error': None if ok else 'S3 upload failed'}
    elif storage_type == 'oss':
        ok = upload_to_oss(file_path, object_name, config)
        return {'uploaded': ok, 'target': f'oss://{config.get("oss_bucket", "")}/{object_name}', 'error': None if ok else 'OSS upload failed'}
    else:
        return {'uploaded': False, 'target': 'local', 'error': None}

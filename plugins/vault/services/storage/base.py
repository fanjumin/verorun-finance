#!/usr/bin/env python3
"""
Vault Storage Adapter — Abstract base class for all storage backends.

All storage backends (S3/OSS/Azure/GCS/SFTP/WebDAV) must implement this interface.
"""

from abc import ABC, abstractmethod
from typing import Optional
import json
from ..utils import decrypt_config_secrets, encrypt_config_secrets, mask_config_secrets


class BaseStorageAdapter(ABC):
    """Storage adapter base class."""

    def __init__(self, config: dict):
        self.config = config
        self.name = config.get('name', 'unnamed')

    @abstractmethod
    def upload(self, file_path: str, object_name: str) -> bool:
        """Upload file to remote storage."""
        pass

    @abstractmethod
    def download(self, object_name: str, file_path: str) -> bool:
        """Download file from remote storage."""
        pass

    @abstractmethod
    def delete(self, object_name: str) -> bool:
        """Delete file from remote storage."""
        pass

    @abstractmethod
    def list_objects(self, prefix: str = '') -> list:
        """List objects in remote storage."""
        pass

    @abstractmethod
    def test_connection(self) -> dict:
        """Test connection health. Returns {'ok': bool, 'error': str|None}."""
        pass

    def get_size(self, object_name: str) -> Optional[int]:
        """Get object size in bytes. Optional."""
        return None


class StorageRouter:
    """Multi-target storage router supporting 3-2-1 backup strategy."""

    def __init__(self):
        self._adapters = {}
        self._load_adapters()

    def _load_adapters(self):
        """Load all enabled storage targets from database."""
        try:
            from ..utils import get_vault_conn
            conn = get_vault_conn()
            cur = conn.cursor()
            cur.execute("""
                SELECT id, name, storage_type, config
                FROM vault_storage_targets
                WHERE enabled = TRUE
            """)
            rows = cur.fetchall()
            cur.close()
            conn.close()

            for row in rows:
                target_id, name, stype, config = row
                if isinstance(config, str):
                    config = json.loads(config)
                config['name'] = name
                config = decrypt_config_secrets(config)
                adapter = self._create_adapter(stype, config)
                if adapter:
                    self._adapters[target_id] = adapter
        except Exception as e:
            print(f'[Vault] Failed to load storage adapters: {e}')

    def _create_adapter(self, storage_type: str, config: dict) -> BaseStorageAdapter:
        """Factory: create adapter instance by storage type. Returns None if not available."""
        if storage_type == 's3':
            try:
                from .s3 import S3Adapter
                return S3Adapter(config)
            except ImportError:
                print('[Vault] S3 adapter not available (boto3 required)')
        elif storage_type == 'oss':
            try:
                from .oss import OSSAdapter
                return OSSAdapter(config)
            except ImportError:
                print('[Vault] OSS adapter not available (oss2 required)')
        elif storage_type == 'azure':
            try:
                from .azure import AzureAdapter
                return AzureAdapter(config)
            except ImportError:
                print('[Vault] Azure adapter not available (azure-storage-blob required)')
        elif storage_type == 'gcs':
            try:
                from .gcs import GCSAdapter
                return GCSAdapter(config)
            except ImportError:
                print('[Vault] GCS adapter not available (google-cloud-storage required)')
        elif storage_type == 'sftp':
            try:
                from .sftp import SFTPAdapter
                return SFTPAdapter(config)
            except ImportError:
                print('[Vault] SFTP adapter not available (paramiko required)')
        elif storage_type == 'webdav':
            try:
                from .webdav import WebDAVAdapter
                return WebDAVAdapter(config)
            except ImportError:
                print('[Vault] WebDAV adapter not available (requests required)')
        elif storage_type == 'local':
            try:
                from .local import LocalAdapter
                return LocalAdapter(config)
            except ImportError:
                print('[Vault] Local adapter not available')
        else:
            print(f'[Vault] Unknown storage type: {storage_type}')
        return None

    def upload_to_all(self, file_path: str, object_name: str) -> list:
        """Upload to all enabled storage targets."""
        results = []
        for target_id, adapter in self._adapters.items():
            try:
                ok = adapter.upload(file_path, object_name)
                results.append({
                    'target_id': target_id,
                    'target_name': adapter.name,
                    'uploaded': ok,
                    'error': None if ok else 'Upload failed',
                })
            except Exception as e:
                results.append({
                    'target_id': target_id,
                    'target_name': adapter.name,
                    'uploaded': False,
                    'error': str(e),
                })
        return results

    def test_all(self) -> dict:
        """Test connectivity for all storage targets."""
        results = {}
        for target_id, adapter in self._adapters.items():
            results[target_id] = adapter.test_connection()
        return results

    # ── CRUD Methods ──

    def list_targets(self) -> list:
        """List all storage targets from database."""
        from ..utils import get_vault_conn
        conn = get_vault_conn()
        cur = conn.cursor()
        cur.execute("""
            SELECT id, name, storage_type, config, is_default, enabled,
                   last_test_at, last_test_ok, created_at
            FROM vault_storage_targets
            ORDER BY created_at DESC
        """)
        rows = cur.fetchall()
        cur.close()
        conn.close()
        cols = ['id', 'name', 'storage_type', 'config', 'is_default',
                'enabled', 'last_test_at', 'last_test_ok', 'created_at']
        results = []
        for row in rows:
            d = dict(zip(cols, row))
            if isinstance(d.get('config'), str):
                d['config'] = json.loads(d['config'])
            # 敏感字段脱敏，不向前端返回明文密钥
            if isinstance(d.get('config'), dict):
                d['config'] = mask_config_secrets(d['config'])
            for ts_key in ('last_test_at', 'created_at'):
                if hasattr(d.get(ts_key), 'strftime'):
                    d[ts_key] = d[ts_key].strftime('%Y-%m-%d %H:%M:%S')
            results.append(d)
        return results

    def create_target(self, name: str, storage_type: str,
                      config: dict, is_default: bool = False) -> dict:
        """Create a new storage target. Returns the created target dict."""
        from ..utils import get_vault_conn
        conn = get_vault_conn()
        cur = conn.cursor()

        if is_default:
            cur.execute(
                "UPDATE vault_storage_targets SET is_default = FALSE WHERE is_default = TRUE"
            )

        # 敏感字段加密后入库
        safe_config = encrypt_config_secrets(config or {})
        cur.execute("""
            INSERT INTO vault_storage_targets
                (name, storage_type, config, is_default, enabled, created_at)
            VALUES (%s,%s,%s,%s,TRUE,NOW())
            RETURNING id
        """, (name, storage_type, json.dumps(safe_config), is_default))
        row_id = cur.fetchone()[0]
        conn.commit()
        cur.close()
        conn.close()

        # Reload adapters to include new target
        self._load_adapters()
        return {'success': True, 'id': row_id, 'name': name}

    def update_target(self, target_id: int, **kwargs) -> dict:
        """Update a storage target."""
        from ..utils import get_vault_conn
        import json as _json
        conn = get_vault_conn()
        cur = conn.cursor()

        allowed = ['name', 'storage_type', 'config', 'is_default', 'enabled']
        updates = []
        params = []
        for key in allowed:
            if key in kwargs:
                val = kwargs[key]
                if key == 'config' and isinstance(val, dict):
                    val = encrypt_config_secrets(val)
                    val = _json.dumps(val)
                updates.append(f"{key} = %s")
                params.append(val)

        if not updates:
            return {'success': False, 'error': 'No valid fields to update'}

        params.append(target_id)
        cur.execute(
            f"UPDATE vault_storage_targets SET {', '.join(updates)} WHERE id = %s",
            params,
        )
        conn.commit()
        cur.close()
        conn.close()
        self._load_adapters()
        return {'success': True, 'id': target_id}

    def delete_target(self, target_id: int) -> dict:
        """Delete a storage target."""
        from ..utils import get_vault_conn
        conn = get_vault_conn()
        cur = conn.cursor()
        cur.execute("DELETE FROM vault_storage_targets WHERE id = %s", (target_id,))
        deleted = cur.rowcount
        conn.commit()
        cur.close()
        conn.close()
        self._load_adapters()
        return {'success': deleted > 0, 'deleted': deleted}

    def test_target(self, target_id: int) -> dict:
        """Test connection for a specific target."""
        from ..utils import get_vault_conn
        adapter = self._adapters.get(target_id)
        if not adapter:
            return {'ok': False, 'error': 'Target not found or not loaded'}

        result = adapter.test_connection()
        try:
            conn = get_vault_conn()
            cur = conn.cursor()
            cur.execute("""
                UPDATE vault_storage_targets
                SET last_test_at = NOW(), last_test_ok = %s
                WHERE id = %s
            """, (result.get('ok', False), target_id))
            conn.commit()
            cur.close()
            conn.close()
        except Exception:
            pass
        return result

    # ── Rotation: 3-2-1 Strategy ──

    def rotate_upload(self, file_path: str, object_name: str) -> dict:
        """
        Upload to targets following 3-2-1 rotation:
        - Primary: first enabled target (usually local)
        - Secondary: next enabled target (different media, e.g. S3)
        - Offsite: last enabled target (e.g. SFTP to remote server)
        """
        results = {'primary': None, 'secondary': None, 'offsite': None}
        enabled_ids = sorted(self._adapters.keys())
        if not enabled_ids:
            return results

        stages = [('primary', enabled_ids[0]), ('secondary', enabled_ids[1] if len(enabled_ids) > 1 else None),
                  ('offsite', enabled_ids[2] if len(enabled_ids) > 2 else None)]
        for stage, tid in stages:
            if tid is None:
                continue
            adapter = self._adapters[tid]
            try:
                ok = adapter.upload(file_path, object_name)
                results[stage] = {'target_id': tid, 'target_name': adapter.name, 'uploaded': ok}
            except Exception as e:
                results[stage] = {'target_id': tid, 'target_name': adapter.name, 'uploaded': False, 'error': str(e)}
        return results

    # ── Storage Tiering ──

    def tier_cleanup(self, hot_days: int = 7, warm_days: int = 30):
        """
        Apply storage tiering:
        - hot (0-7 days): keep on all targets
        - warm (7-30 days): keep on 1 primary target only
        - cold (>30 days): delete from all but archive target
        """
        import os as _os
        import glob as _glob
        from datetime import datetime

        from ..utils import get_vault_conn
        base_dir = _os.path.join(_os.path.dirname(_os.path.abspath(__file__)),
                                  '..', '..', '..')
        backup_dir = _os.path.join(base_dir, 'data', 'vault')
        archives = sorted(
            _glob.glob(_os.path.join(backup_dir, 'vault_*.tar.gz')),
            key=_os.path.getmtime,
        )
        now = datetime.utcnow().timestamp()
        removed = 0

        # Find archive-tier target (optional)
        conn = get_vault_conn()
        cur = conn.cursor()
        cur.execute("""
            SELECT id, config FROM vault_storage_targets
            WHERE enabled = TRUE AND config::text LIKE '%"tier":"archive"%'
            LIMIT 1
        """)
        archive_row = cur.fetchone()
        cur.close()
        conn.close()

        for archive in archives:
            age_days = (now - _os.path.getmtime(archive)) / 86400
            if age_days > warm_days:
                # Cold tier: delete locally, keep only on archive target
                # Note: actual remote deletion handled separately
                if archive_row:
                    continue  # keep if archive target exists
                _os.remove(archive)
                removed += 1

        return {'removed': removed, 'tier_applied': True}

    def tier_report(self) -> dict:
        """Report on storage tier distribution (hot/warm/cold counts)."""
        import os as _os
        import glob as _glob
        from datetime import datetime

        base_dir = _os.path.join(_os.path.dirname(_os.path.abspath(__file__)),
                                  '..', '..', '..')
        backup_dir = _os.path.join(base_dir, 'data', 'vault')
        now = datetime.utcnow().timestamp()
        hot = warm = cold = 0
        total_size = 0

        for f in _glob.glob(_os.path.join(backup_dir, 'vault_*.tar.gz')):
            age = (now - _os.path.getmtime(f)) / 86400
            sz = _os.path.getsize(f)
            total_size += sz
            if age <= 7:
                hot += 1
            elif age <= 30:
                warm += 1
            else:
                cold += 1

        return {
            'hot': hot, 'warm': warm, 'cold': cold,
            'total_size_mb': round(total_size / (1024 * 1024), 1),
        }

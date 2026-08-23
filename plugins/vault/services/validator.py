#!/usr/bin/env python3
"""
Vault Validator — Backup integrity verification with HMAC signature.

Provides SHA-256 checksum + HMAC-SHA256 signing to prevent tampering.
"""

import os
import hashlib
import hmac


class VaultValidator:
    """Backup integrity validator with tamper-proof HMAC signing."""

    SIGNATURE_EXT = '.sig'

    @staticmethod
    def compute_sha256(file_path: str) -> str:
        """Compute SHA-256 checksum of a file."""
        sha = hashlib.sha256()
        with open(file_path, 'rb') as f:
            for chunk in iter(lambda: f.read(8192), b''):
                sha.update(chunk)
        return sha.hexdigest()

    @staticmethod
    def sign_file(file_path: str, secret_key: str) -> str:
        """
        Create an HMAC-SHA256 signature file.
        Returns the path to the .sig file.
        """
        checksum = VaultValidator.compute_sha256(file_path)
        signature = hmac.new(
            secret_key.encode(),
            checksum.encode(),
            hashlib.sha256,
        ).hexdigest()

        sig_path = file_path + VaultValidator.SIGNATURE_EXT
        with open(sig_path, 'w') as f:
            f.write(f'{checksum}\n{signature}')
        return sig_path

    @staticmethod
    def verify_file(file_path: str, secret_key: str) -> dict:
        """
        Verify a file's integrity using its .sig file.
        Returns {'ok': bool, 'checksum_match': bool, 'signature_match': bool, 'error': str|None}
        """
        sig_path = file_path + VaultValidator.SIGNATURE_EXT
        if not os.path.isfile(sig_path):
            return {
                'ok': False,
                'checksum_match': False,
                'signature_match': False,
                'error': 'Signature file not found',
            }

        try:
            with open(sig_path, 'r') as f:
                lines = f.read().strip().split('\n')
                stored_checksum = lines[0]
                stored_signature = lines[1] if len(lines) > 1 else ''
        except Exception as e:
            return {
                'ok': False,
                'checksum_match': False,
                'signature_match': False,
                'error': f'Failed to read signature file: {e}',
            }

        current_checksum = VaultValidator.compute_sha256(file_path)
        checksum_match = current_checksum == stored_checksum

        expected_signature = hmac.new(
            secret_key.encode(),
            stored_checksum.encode(),
            hashlib.sha256,
        ).hexdigest()
        signature_match = hmac.compare_digest(expected_signature, stored_signature)

        return {
            'ok': checksum_match and signature_match,
            'checksum_match': checksum_match,
            'signature_match': signature_match,
            'error': None if (checksum_match and signature_match)
                     else 'Checksum mismatch' if not checksum_match
                     else 'Signature mismatch (tampered)',
        }

    @staticmethod
    def verify_all_backups(backup_dir: str, secret_key: str) -> list:
        """Verify all backups in a directory. Returns list of results."""
        import glob as _glob
        results = []
        for f in sorted(_glob.glob(os.path.join(backup_dir, 'vault_*.tar.gz'))):
            sig_file = f + VaultValidator.SIGNATURE_EXT
            if not os.path.isfile(sig_file):
                results.append({
                    'file': os.path.basename(f),
                    'ok': None,
                    'error': 'Not signed',
                })
                continue
            result = VaultValidator.verify_file(f, secret_key)
            result['file'] = os.path.basename(f)
            results.append(result)
        return results

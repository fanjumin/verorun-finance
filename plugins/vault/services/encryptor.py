#!/usr/bin/env python3
"""
Vault Encryptor — AES-256-GCM encryption/decryption module.

Supports key sources: environment variable, local file, or KMS (future).
"""

import os
import hashlib
from cryptography.hazmat.primitives.ciphers.aead import AESGCM


class VaultEncryptor:
    """AES-256-GCM encryptor for backup file end-to-end encryption."""

    def __init__(self, key_source: str = 'env'):
        """
        Args:
            key_source: 'env' — read from VAULT_ENCRYPTION_KEY env var
                        'file' — read from data/vault/.encryption_key
                        'kms' — reserved for future KMS integration
        """
        self._key = self._load_key(key_source)

    def _load_key(self, source: str) -> bytes:
        if source == 'env':
            raw = os.environ.get('VAULT_ENCRYPTION_KEY', '')
            if not raw:
                raise ValueError('VAULT_ENCRYPTION_KEY environment variable not set')
            return hashlib.sha256(raw.encode()).digest()  # 32 bytes for AES-256
        elif source == 'file':
            key_path = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                    '..', '..', '..', 'data', 'vault', '.encryption_key')
            with open(key_path, 'rb') as f:
                return hashlib.sha256(f.read().strip()).digest()
        else:
            raise ValueError(f'Unknown key source: {source}')

    def encrypt_file(self, input_path: str, output_path: str = None) -> str:
        """
        Encrypt a file. Output format: nonce(12B) + ciphertext + tag(16B).
        """
        output_path = output_path or f'{input_path}.enc'
        aesgcm = AESGCM(self._key)
        nonce = os.urandom(12)

        with open(input_path, 'rb') as f:
            plaintext = f.read()

        ciphertext = aesgcm.encrypt(nonce, plaintext, None)

        with open(output_path, 'wb') as f:
            f.write(nonce + ciphertext)

        return output_path

    def decrypt_file(self, input_path: str, output_path: str) -> str:
        """Decrypt a file."""
        aesgcm = AESGCM(self._key)

        with open(input_path, 'rb') as f:
            data = f.read()

        nonce = data[:12]
        ciphertext = data[12:]

        plaintext = aesgcm.decrypt(nonce, ciphertext, None)

        with open(output_path, 'wb') as f:
            f.write(plaintext)

        return output_path

    def encrypt_stream(self, input_path: str, output_path: str = None,
                       chunk_size: int = 1024 * 1024) -> str:
        """
        Stream-encrypt large files (memory-friendly).
        Output format: [nonce(12B)] [chunk_length(4B) + encrypted_chunk]...
        """
        output_path = output_path or f'{input_path}.enc'
        aesgcm = AESGCM(self._key)
        nonce = os.urandom(12)

        with open(input_path, 'rb') as fin, open(output_path, 'wb') as fout:
            fout.write(nonce)
            chunk_index = 0
            while True:
                chunk = fin.read(chunk_size)
                if not chunk:
                    break
                chunk_nonce = bytearray(nonce)
                chunk_nonce[-4:] = chunk_index.to_bytes(4, 'big')
                encrypted = aesgcm.encrypt(bytes(chunk_nonce), chunk, None)
                fout.write(len(encrypted).to_bytes(4, 'big'))
                fout.write(encrypted)
                chunk_index += 1

        return output_path

    def decrypt_stream(self, input_path: str, output_path: str) -> str:
        """Stream-decrypt large files."""
        aesgcm = AESGCM(self._key)

        with open(input_path, 'rb') as fin, open(output_path, 'wb') as fout:
            nonce = fin.read(12)
            chunk_index = 0
            while True:
                length_bytes = fin.read(4)
                if not length_bytes or len(length_bytes) < 4:
                    break
                length = int.from_bytes(length_bytes, 'big')
                encrypted = fin.read(length)
                if not encrypted:
                    break
                chunk_nonce = bytearray(nonce)
                chunk_nonce[-4:] = chunk_index.to_bytes(4, 'big')
                plaintext = aesgcm.decrypt(bytes(chunk_nonce), encrypted, None)
                fout.write(plaintext)
                chunk_index += 1

        return output_path

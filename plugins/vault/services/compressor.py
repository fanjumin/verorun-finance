#!/usr/bin/env python3
"""
Vault Compressor — Multi-algorithm compression support (gzip / zstd / lz4).

Provides a unified interface for backup compression with configurable levels.
"""

import gzip
import os
import shutil
from typing import Optional


class VaultCompressor:
    """Multi-algorithm file compressor for backup archives."""

    SUPPORTED_ALGORITHMS = ('gzip', 'zstd', 'lz4', 'none')

    def __init__(self, algorithm: str = 'gzip', level: int = 6):
        """
        Args:
            algorithm: compression algorithm ('gzip', 'zstd', 'lz4', 'none')
            level: compression level (1-9 for gzip/zstd, 0-16 for zstd, 1-12 for lz4)
        """
        if algorithm not in self.SUPPORTED_ALGORITHMS:
            raise ValueError(f'Unsupported algorithm: {algorithm}. '
                             f'Use one of: {self.SUPPORTED_ALGORITHMS}')
        self.algorithm = algorithm
        self.level = level

    def compress(self, input_path: str, output_path: str = None) -> str:
        """
        Compress a file using the configured algorithm.

        Returns:
            Path to the compressed file.
        """
        output_path = output_path or self._default_output(input_path)

        if self.algorithm == 'gzip':
            self._compress_gzip(input_path, output_path)
        elif self.algorithm == 'zstd':
            self._compress_zstd(input_path, output_path)
        elif self.algorithm == 'lz4':
            self._compress_lz4(input_path, output_path)
        else:
            shutil.copy2(input_path, output_path)

        return output_path

    def decompress(self, input_path: str, output_path: str) -> str:
        """
        Decompress a file. Algorithm is auto-detected from file extension.
        """
        ext = os.path.splitext(input_path)[1].lower()

        if ext == '.gz':
            self._decompress_gzip(input_path, output_path)
        elif ext == '.zst':
            self._decompress_zstd(input_path, output_path)
        elif ext == '.lz4':
            self._decompress_lz4(input_path, output_path)
        else:
            shutil.copy2(input_path, output_path)

        return output_path

    # ── gzip ──

    def _compress_gzip(self, input_path: str, output_path: str):
        with open(input_path, 'rb') as fin, gzip.open(output_path, 'wb',
                                                       compresslevel=self.level) as fout:
            shutil.copyfileobj(fin, fout, length=1024 * 1024)

    def _decompress_gzip(self, input_path: str, output_path: str):
        with gzip.open(input_path, 'rb') as fin, open(output_path, 'wb') as fout:
            shutil.copyfileobj(fin, fout, length=1024 * 1024)

    # ── zstd ──

    def _compress_zstd(self, input_path: str, output_path: str):
        try:
            import zstandard as zstd
        except ImportError:
            raise ImportError('zstandard package is required for zstd compression')
        cctx = zstd.ZstdCompressor(level=self.level)
        with open(input_path, 'rb') as fin, open(output_path, 'wb') as fout:
            cctx.copy_stream(fin, fout)

    def _decompress_zstd(self, input_path: str, output_path: str):
        try:
            import zstandard as zstd
        except ImportError:
            raise ImportError('zstandard package is required for zstd decompression')
        dctx = zstd.ZstdDecompressor()
        with open(input_path, 'rb') as fin, open(output_path, 'wb') as fout:
            dctx.copy_stream(fin, fout)

    # ── lz4 ──

    def _compress_lz4(self, input_path: str, output_path: str):
        try:
            import lz4.frame
        except ImportError:
            raise ImportError('lz4 package is required for lz4 compression')
        with open(input_path, 'rb') as fin, lz4.frame.open(output_path, 'wb',
                                                            compression_level=self.level) as fout:
            shutil.copyfileobj(fin, fout, length=1024 * 1024)

    def _decompress_lz4(self, input_path: str, output_path: str):
        try:
            import lz4.frame
        except ImportError:
            raise ImportError('lz4 package is required for lz4 decompression')
        with lz4.frame.open(input_path, 'rb') as fin, open(output_path, 'wb') as fout:
            shutil.copyfileobj(fin, fout, length=1024 * 1024)

    def _default_output(self, input_path: str) -> str:
        extensions = {'gzip': '.gz', 'zstd': '.zst', 'lz4': '.lz4', 'none': ''}
        return input_path + extensions.get(self.algorithm, '')

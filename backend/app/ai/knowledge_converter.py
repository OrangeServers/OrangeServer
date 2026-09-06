"""Isolated MarkItDown process entry point; no plugins or remote converters."""
from io import BytesIO
import os
import sys
import types

# OpenBLAS sizes its per-thread arenas and ONNX Runtime its thread pool from
# the host CPU count, so on large machines neither fits the address-space bound
# applied below: conversion aborted, or silently produced empty markdown once
# the ONNX shared object failed to map. One thread is ample for one bounded
# document, and it keeps the footprint independent of the host.
os.environ.setdefault('OPENBLAS_NUM_THREADS', '1')
os.environ.setdefault('OMP_NUM_THREADS', '1')


class _ExtensionOnlySniffer:
    """Stands in for magika, whose guess the pinned extension already decides."""

    @staticmethod
    def identify_stream(stream):
        return types.SimpleNamespace(status='unavailable')


# The caller validates the file signature and passes the extension explicitly,
# so markitdown's content sniffing adds nothing here. Stubbing the module keeps
# ONNX Runtime out of this process entirely.
sys.modules.setdefault(
    'magika', types.SimpleNamespace(Magika=_ExtensionOnlySniffer),
)

from markitdown import MarkItDown, StreamInfo


def _bound_resources() -> None:
    try:
        import resource
    except ImportError:
        return
    memory = 512 * 1024 * 1024
    _soft, hard = resource.getrlimit(resource.RLIMIT_AS)
    memory = memory if hard == resource.RLIM_INFINITY else min(memory, hard)
    resource.setrlimit(resource.RLIMIT_AS, (memory, memory))
    _soft, hard = resource.getrlimit(resource.RLIMIT_CPU)
    cpu = 20 if hard == resource.RLIM_INFINITY else min(20, hard)
    resource.setrlimit(resource.RLIMIT_CPU, (cpu, cpu))


def main() -> None:
    extension = sys.argv[1] if len(sys.argv) == 3 else ''
    if extension not in {'.pdf', '.docx'}:
        raise SystemExit('unsupported extension')
    output_limit = int(sys.argv[2])
    _bound_resources()
    result = MarkItDown(enable_plugins=False).convert_stream(
        BytesIO(sys.stdin.buffer.read()),
        stream_info=StreamInfo(extension=extension),
    )
    sys.stdout.buffer.write(str(result.markdown or '').encode('utf-8')[:output_limit])


if __name__ == '__main__':
    main()

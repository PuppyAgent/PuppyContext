"""Standalone exec wrapper: resource limits apply only to this Git process tree."""

import os
import resource
import sys

if __name__ == "__main__":
    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
    resource.setrlimit(resource.RLIMIT_CPU, (300, 300))
    resource.setrlimit(resource.RLIMIT_FSIZE, (384 * 1024**2, 384 * 1024**2))
    resource.setrlimit(resource.RLIMIT_NOFILE, (128, 128))
    # macOS does not enforce address-space limits like the production Linux
    # worker. The Linux acceptance runner exercises the enforced limit.
    if sys.platform == "linux":
        resource.setrlimit(resource.RLIMIT_AS, (768 * 1024**2, 768 * 1024**2))
    os.execvpe(sys.argv[1], sys.argv[1:], os.environ)

#!/usr/bin/env python3
"""Install a checksum-verified GitHub CLI release into the container image."""

from __future__ import annotations

import argparse
import hashlib
import io
import pathlib
import platform
import tarfile
import urllib.request


SHA256 = {
    ("2.97.0", "amd64"): "a2c9b8497e1f85b1ad0dfcb78b5a622e098801b8e461e459e88e1ee12f018112",
    ("2.97.0", "arm64"): "73ea440ecad9c9e284429997ee6f93577bc6f7bc6fba357ef62c53ad8fb641a5",
}
MACHINE_ARCH = {"x86_64": "amd64", "aarch64": "arm64"}


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--version", required=True)
    parser.add_argument("--arch", choices=("amd64", "arm64"), default=MACHINE_ARCH.get(platform.machine()))
    args = parser.parse_args()
    if not args.arch:
        raise SystemExit(f"unsupported container architecture: {platform.machine()}")

    expected = SHA256.get((args.version, args.arch))
    if expected is None:
        raise SystemExit(f"no verified GitHub CLI checksum for {args.version}/{args.arch}")

    archive_name = f"gh_{args.version}_linux_{args.arch}.tar.gz"
    url = f"https://github.com/cli/cli/releases/download/v{args.version}/{archive_name}"
    request = urllib.request.Request(url, headers={"User-Agent": "command-center-image-build"})
    with urllib.request.urlopen(request, timeout=60) as response:
        archive_bytes = response.read()

    actual = hashlib.sha256(archive_bytes).hexdigest()
    if actual != expected:
        raise SystemExit(f"GitHub CLI checksum mismatch: expected {expected}, got {actual}")

    member_name = f"gh_{args.version}_linux_{args.arch}/bin/gh"
    with tarfile.open(fileobj=io.BytesIO(archive_bytes), mode="r:gz") as archive:
        member = archive.getmember(member_name)
        if not member.isfile():
            raise SystemExit("GitHub CLI release archive did not contain a regular gh binary")
        source = archive.extractfile(member)
        if source is None:
            raise SystemExit("could not read GitHub CLI binary from release archive")
        binary = source.read()

    target = pathlib.Path("/usr/local/bin/gh")
    target.write_bytes(binary)
    target.chmod(0o755)
    print(f"installed verified gh {args.version} ({args.arch})")


if __name__ == "__main__":
    main()

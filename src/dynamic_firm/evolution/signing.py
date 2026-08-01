from __future__ import annotations

import subprocess
import tempfile
from pathlib import Path
from typing import Mapping

from dynamic_firm.company.models import canonical_json, content_digest


SIGNING_NAMESPACE = "noruct-evolution-release-v1"
MAX_OPENSSH_SIGNATURE_BYTES = 32 * 1024


def allowed_signers_digest(path: Path) -> str:
    if not path.is_file():
        raise ValueError("allowed signers must be an existing file up to 1 MiB")
    value = path.read_bytes()
    if not value or len(value) > 1_048_576:
        raise ValueError("allowed signers must be an existing file up to 1 MiB")
    return content_digest(value.hex())


def release_candidate_payload(candidate: Mapping[str, object]) -> bytes:
    """Canonical payload that an external user-managed signer must sign."""
    return canonical_json(
        {
            "schema": "noruct.release-candidate-signing-payload.v1",
            "candidate_id": candidate["candidate_id"],
            "blueprint_id": candidate["blueprint_id"],
            "base_version": candidate["base_version"],
            "candidate_version": candidate["candidate_version"],
            "delta_digest": candidate["delta_digest"],
            "holdout_digest": candidate["holdout_digest"],
        }
    ).encode("utf-8")


def verify_openssh_signature(
    payload: bytes,
    *,
    signature_path: Path,
    allowed_signers_path: Path,
    principal: str,
    command: Path,
    namespace: str = SIGNING_NAMESPACE,
) -> Mapping[str, str]:
    """Verify a detached OpenSSH signature without reading a private key."""
    if not command.is_absolute() or not command.is_file():
        raise ValueError("ssh-keygen command must be an existing absolute user-managed path")
    if not signature_path.is_file():
        raise ValueError("signature must be an existing detached file up to 32 KiB")
    signature = signature_path.read_bytes()
    return verify_openssh_signature_bytes(
        payload,
        signature=signature,
        allowed_signers_path=allowed_signers_path,
        principal=principal,
        command=command,
        namespace=namespace,
    )


def verify_openssh_signature_bytes(
    payload: bytes,
    *,
    signature: bytes,
    allowed_signers_path: Path,
    principal: str,
    command: Path,
    namespace: str = SIGNING_NAMESPACE,
) -> Mapping[str, str]:
    """Verify an ephemeral detached signature without retaining it locally."""
    if not signature or len(signature) > MAX_OPENSSH_SIGNATURE_BYTES:
        raise ValueError("signature must contain up to 32 KiB of detached signature data")
    if not command.is_absolute() or not command.is_file():
        raise ValueError("ssh-keygen command must be an existing absolute user-managed path")
    if not allowed_signers_path.is_file():
        raise ValueError("allowed signers must be an existing file up to 1 MiB")
    allowed_signers = allowed_signers_path.read_bytes()
    if not allowed_signers or len(allowed_signers) > 1_048_576:
        raise ValueError("allowed signers must be an existing file up to 1 MiB")
    if not principal or any(character.isspace() for character in principal):
        raise ValueError("signer principal must be a non-empty whitespace-free identifier")
    if not namespace or any(character.isspace() for character in namespace) or len(namespace.encode("utf-8")) > 128:
        raise ValueError("signature namespace must be a non-empty whitespace-free identifier up to 128 bytes")
    temporary_signature_path: Path | None = None
    temporary_allowed_signers_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(prefix="noruct-evolution-signature-", suffix=".sig", delete=False) as handle:
            handle.write(signature)
            temporary_signature_path = Path(handle.name)
        with tempfile.NamedTemporaryFile(prefix="noruct-evolution-allowed-signers-", suffix=".txt", delete=False) as handle:
            handle.write(allowed_signers)
            temporary_allowed_signers_path = Path(handle.name)
        completed = subprocess.run(
            [
                str(command), "-Y", "verify", "-f", str(temporary_allowed_signers_path),
                "-I", principal, "-n", namespace,
                "-s", str(temporary_signature_path),
            ],
            input=payload,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
            timeout=15,
        )
        if completed.returncode != 0:
            raise ValueError("OpenSSH signature verification failed")
        return {
            "algorithm": "openssh-detached-signature",
            "principal": principal,
            "namespace": namespace,
            "payload_digest": content_digest(payload.decode("utf-8")),
            "signature_digest": content_digest(signature.hex()),
            "allowed_signers_digest": content_digest(allowed_signers.hex()),
        }
    finally:
        if temporary_signature_path is not None:
            temporary_signature_path.unlink(missing_ok=True)
        if temporary_allowed_signers_path is not None:
            temporary_allowed_signers_path.unlink(missing_ok=True)

"""Environment command lifecycle adapter."""

from __future__ import annotations

from dynamic_firm.application.cli_component_contract import cli

def _run_environment_command(
    args: argparse.Namespace,
    settings: dict,
    config_path: Path,
    output: TextIO,
) -> int:
    if args.environment_command == "status":
        configured_worker = cli.remote_worker_config_from_settings(settings)
        record = dict(cli.execution_environment_status(args.workspace).to_dict())
        record["remote_worker"] = cli.remote_worker_status(configured_worker)
        record["container_workspace"] = cli.container_status(cli.container_config_from_settings(settings))
    elif args.environment_command == "worker-status":
        record = dict(cli.remote_worker_status(cli.remote_worker_config_from_settings(settings)))
    elif args.environment_command == "worker-verify":
        if not args.confirm:
            raise ValueError("Remote worker verification requires --confirm because it contacts the configured SSH host")
        configured_worker = cli.remote_worker_config_from_settings(settings)
        if configured_worker is None:
            raise ValueError("No remote Company worker is configured")
        record = cli.verify_remote_workspace_worker(configured_worker).to_dict()
    elif args.environment_command == "worker-audit":
        if not args.confirm:
            raise ValueError("Remote worker content audit requires --confirm because it contacts the configured SSH host")
        configured_worker = cli.remote_worker_config_from_settings(settings)
        if configured_worker is None:
            raise ValueError("No remote Company worker is configured")
        record = cli.verify_remote_workspace_worker_content(configured_worker).to_dict()
    elif args.environment_command == "worker-configure":
        worker_settings = cli.RemoteWorkerSettings(
            target_id=args.target_id,
            receipt=args.receipt,
            programs=cli._remote_worker_programs(args.program),
            identity_file=args.identity_file,
            timeout_seconds=args.timeout_seconds,
        )
        target = cli.write_remote_worker_settings(config_path, worker_settings)
        configured = worker_settings.validated_runtime_config()
        record = {
            "configuration_changed": True,
            "config_path": str(target),
            **cli.remote_worker_status(configured),
        }
    elif args.environment_command == "worker-disable":
        record = {
            "configuration_changed": cli.remove_remote_worker_settings(config_path),
            "config_path": str(config_path.expanduser().resolve()),
            **cli.remote_worker_status(None),
        }
    elif args.environment_command == "container-status":
        record = dict(cli.container_status(cli.container_config_from_settings(settings)))
    elif args.environment_command == "container-verify":
        if not args.confirm:
            raise ValueError("Container verification requires --confirm because it contacts the local Docker engine")
        configured_container = cli.container_config_from_settings(settings)
        if configured_container is None:
            raise ValueError("No container workspace is configured")
        record = cli.verify_container_workspace(configured_container).to_dict()
    elif args.environment_command == "preflight":
        if not args.confirm:
            raise ValueError("Execution-environment preflight requires --confirm because it contacts configured SSH and Docker endpoints")
        configured_worker = cli.remote_worker_config_from_settings(settings)
        configured_container = cli.container_config_from_settings(settings)
        remote_record: dict[str, object]
        container_record: dict[str, object]
        if configured_worker is None:
            remote_record = {"configured": False, "state": "NOT_CONFIGURED"}
        else:
            remote_record = {"configured": True, **cli.verify_remote_workspace_worker_content(configured_worker).to_dict()}
        if configured_container is None:
            container_record = {"configured": False, "state": "NOT_CONFIGURED"}
        else:
            container_record = {"configured": True, **cli.verify_container_workspace(configured_container).to_dict()}
        remote_ready = not remote_record["configured"] or bool(remote_record.get("content_verified"))
        container_ready = not container_record["configured"] or (
            bool(container_record.get("runtime_available")) and bool(container_record.get("image_present"))
        )
        configured_count = int(bool(remote_record["configured"])) + int(bool(container_record["configured"]))
        record = {
            "configured_count": configured_count,
            "ready": configured_count > 0 and remote_ready and container_ready,
            "remote_worker": remote_record,
            "container_workspace": container_record,
            "authority": "operator_confirmed_execution_environment_preflight_no_company_job_program_container_pull_or_start",
        }
    elif args.environment_command == "container-configure":
        configured = cli.ContainerSettings(
            image=args.image,
            programs=cli._container_programs(args.program),
            docker_command=args.docker_command,
            timeout_seconds=args.timeout_seconds,
            memory_limit_mb=args.memory_limit_mb,
            cpu_limit=args.cpu_limit,
            pids_limit=args.pids_limit,
            max_output_bytes=args.max_output_bytes,
        )
        target = cli.write_container_settings(config_path, configured)
        record = {"configuration_changed": True, "config_path": str(target), **cli.container_status(configured.validated_runtime_config())}
    elif args.environment_command == "container-disable":
        record = {"configuration_changed": cli.remove_container_settings(config_path), "config_path": str(config_path.expanduser().resolve()), **cli.container_status(None)}
    elif args.environment_command == "snapshot":
        if not args.confirm:
            raise ValueError("Workspace snapshot manifest requires --confirm because it reads workspace file contents")
        record = cli.write_workspace_snapshot_manifest(
            workspace=args.workspace,
            output_path=args.output,
        ).to_dict()
    elif args.environment_command == "snapshot-inspect":
        record = cli.inspect_workspace_snapshot_manifest(args.source).to_dict()
    elif args.environment_command == "ssh-transfer":
        if not args.confirm:
            raise ValueError("Remote workspace transfer requires --confirm because it uploads workspace file contents")
        record = cli.transfer_workspace_snapshot(
            workspace=args.workspace,
            snapshot_manifest=args.snapshot,
            host=args.host,
            user=args.user,
            port=args.port,
            identity_file=args.identity_file,
            remote_workspace=args.remote_workspace,
            timeout_seconds=args.timeout_seconds,
        ).to_dict()
    elif args.environment_command == "ssh-probe":
        if not args.confirm:
            raise ValueError("SSH connectivity probe requires --confirm")
        record = cli.probe_ssh_environment(
            host=args.host,
            user=args.user,
            port=args.port,
            identity_file=args.identity_file,
            timeout_seconds=args.timeout_seconds,
        ).to_dict()
    else:
        if not args.confirm:
            raise ValueError("Remote operator command requires --confirm")
        record = cli.run_ssh_operator_command(
            host=args.host,
            user=args.user,
            port=args.port,
            identity_file=args.identity_file,
            remote_workspace=args.remote_workspace,
            program=args.program,
            arguments=tuple(args.arg),
            timeout_seconds=args.timeout_seconds,
        ).to_dict()
    if args.json:
        print(cli.json.dumps(record, ensure_ascii=False, sort_keys=True), file=output)
        ready = record.get("reachable", True)
        if args.environment_command == "worker-verify":
            ready = bool(ready) and bool(record.get("snapshot_present"))
        if args.environment_command == "worker-audit":
            ready = bool(ready) and bool(record.get("content_verified"))
        if args.environment_command == "container-verify":
            ready = bool(record.get("runtime_available")) and bool(record.get("image_present"))
        if args.environment_command == "preflight":
            ready = bool(record.get("ready"))
        return cli.EXIT_OK if ready else cli.EXIT_INPUT
    if args.environment_command == "status":
        workspace = record["workspace"]
        print(f"Execution environment · {record['platform']} {record['machine']} · Python {record['python_version']}", file=output)
        print(
            "Workspace: "
            + ("ready" if workspace["is_directory"] and workspace["writable_by_current_user"] else "unavailable or read-only")
            + f" · {workspace['path']}",
            file=output,
        )
        print("Local command execution: global workspace authority plus frozen capability trust policy", file=output)
        worker = record["remote_worker"]
        worker_line = "configured; each remote tool call requires approval" if worker["enabled"] else "not configured"
        print(f"Remote SSH: explicit strict-host probe/transfer · Company worker {worker_line}", file=output)
        container = record["container_workspace"]
        container_line = "configured; each container tool call requires approval" if container["enabled"] else "not configured"
        print(f"Local container: network-disabled, read-only root · {container_line}", file=output)
    elif args.environment_command in {"worker-status", "worker-configure", "worker-disable"}:
        print(
            "Remote Company worker: "
            + ("ready under capability trust policy" if record["enabled"] else "disabled"),
            file=output,
        )
        if record.get("enabled"):
            print(
                f"Target: {record['target_id']} · programs: {', '.join(record['program_ids'])} · permission mode: ask",
                file=output,
            )
            print("No automatic activation, reverse sync, credential forwarding, or generic remote shell.", file=output)
        else:
            print("Configure it with: noruct environment worker-configure --target-id build --receipt RECEIPT --program tests=/usr/bin/pytest", file=output)
    elif args.environment_command == "worker-verify":
        state = "snapshot present" if record["snapshot_present"] else "snapshot unavailable"
        print(f"Remote worker verification · {state} · {record['host']}:{record['port']}", file=output)
        print("Authority: fixed receipt-bound marker only · no Company Job or remote program started", file=output)
    elif args.environment_command == "worker-audit":
        print(f"Remote worker ledger audit · {record['integrity_state']} · {record['host']}:{record['port']}", file=output)
        print("Authority: fixed retained-ledger check only · no Company Job or remote program started", file=output)
    elif args.environment_command == "container-verify":
        state = "image present" if record["image_present"] else "image unavailable"
        print(f"Container verification · {state} · runtime {'ready' if record['runtime_available'] else 'unavailable'}", file=output)
        if not record["image_reference_pinned"]:
            print("Image reference is tag-based; use an @sha256 digest for an immutable operator-selected image.", file=output)
        print("Authority: Docker metadata only · no pull, container, mount, or Company Job started", file=output)
    elif args.environment_command == "preflight":
        print(
            f"Execution-environment preflight · {'ready' if record['ready'] else 'needs attention'} · "
            f"{record['configured_count']} configured boundary/boundaries",
            file=output,
        )
        remote = record["remote_worker"]
        container = record["container_workspace"]
        assert isinstance(remote, dict) and isinstance(container, dict)
        print(
            "Remote receipt ledger: "
            + (str(remote.get("integrity_state")) if remote.get("configured") else "not configured"),
            file=output,
        )
        print(
            "Container image: "
            + ("present" if container.get("image_present") else ("unavailable" if container.get("configured") else "not configured")),
            file=output,
        )
        print("Authority: fixed ledger and Docker metadata checks only · no Company Job, program, pull, or container started", file=output)
    elif args.environment_command in {"container-status", "container-configure", "container-disable"}:
        print("Container workspace: " + ("ready under capability trust policy" if record["enabled"] else "disabled"), file=output)
        if record.get("enabled"):
            print(f"Image: {record['image']} · programs: {', '.join(record['program_ids'])} · network: disabled", file=output)
            print("No environment forwarding, generic shell, privilege escalation, automatic pull, retry, or cleanup action.", file=output)
        else:
            print("Configure it with: noruct environment container-configure --image python:3.11-alpine --program tests=/usr/bin/pytest", file=output)
    elif args.environment_command == "ssh-probe":
        print(
            f"SSH probe · {'reachable' if record['reachable'] else 'not ready'} · "
            f"{record['user']}@{record['host']}:{record['port']}",
            file=output,
        )
        print(f"Host key policy: {record['host_key_policy']}", file=output)
        print("Remote Job execution: not implemented", file=output)
        if record["output"]:
            print(f"Result: {record['output']}", file=output)
    elif args.environment_command == "snapshot":
        print(f"Workspace snapshot manifest: {record['file_count']} file(s) · {record['total_bytes']} bytes", file=output)
        print(f"Digest: {record['snapshot_sha256']} · local output: {record['output_path']}", file=output)
        print("No file was uploaded and no Company Job, credential forwarding, or remote execution occurred.", file=output)
    elif args.environment_command == "snapshot-inspect":
        print(f"Workspace snapshot manifest: {record['integrity_state']}", file=output)
        if record["valid"]:
            print(f"Files: {record['file_count']} · bytes: {record['total_bytes']} · digest: {record['snapshot_sha256']}", file=output)
        print("No workspace content was re-read, and no SSH, upload, credential forwarding, or remote execution occurred.", file=output)
    elif args.environment_command == "ssh-transfer":
        print(
            f"Remote workspace transfer · {'verified' if record['transferred'] else 'failed'} · "
            f"{record['user']}@{record['host']}:{record['port']}",
            file=output,
        )
        print(f"Snapshot: {record['file_count']} file(s) · {record['total_bytes']} bytes · {record['snapshot_sha256']}", file=output)
        print(f"Remote staging: {record['remote_snapshot_directory']}", file=output)
        print("Authority: explicit verified transfer only · no Company Job, credential forwarding, remote employee, or retry", file=output)
        if record["output"]:
            print(f"Result: {record['output']}", file=output)
    else:
        print(
            f"Remote operator command · {'completed' if record['completed'] else 'failed'} · "
            f"{record['user']}@{record['host']}:{record['port']}",
            file=output,
        )
        print("Authority: explicit operator command · no Company Job, file sync, credential forwarding, or retry", file=output)
        if record["output"]:
            print(f"Result: {record['output']}", file=output)
    return cli.EXIT_OK if record.get("reachable", record.get("completed", record.get("transferred", True))) else cli.EXIT_INPUT

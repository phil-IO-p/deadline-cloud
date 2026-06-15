# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.

"""
All the `deadline bundle` commands.
"""

from __future__ import annotations

import json
import logging
import sys
import re
import io
import zipfile
from typing import Any, Optional
import tempfile
import shutil
import os
from dataclasses import fields

import click
from botocore.exceptions import ClientError

from ... import api
from ...config import config_file
from ...dataclasses import SubmitterInfo
from ...job_bundle.loader import is_job_bundle_dir
from ...job_bundle.repository import (
    BundleRepository,
    LocalBundleRepository,
    METADATA_KEY_DESC,
    METADATA_KEY_NAME,
    METADATA_KEY_PARAMS,
    METADATA_KEY_STEPS,
    METADATA_LIMIT_DESC,
    METADATA_LIMIT_NAME,
    METADATA_LIMIT_PARAMS,
    METADATA_LIMIT_STEPS,
    S3BundleRepository,
    S3_JOB_BUNDLES_PREFIX,
    _extract_bundle_info,
    _get_bundle_cache_dir,
    _parse_template,
    _read_cache_meta,
    sanitize_bundle_name,
)
from ....job_attachments.exceptions import (
    AssetSyncError,
    AssetSyncCancelledError,
    MisconfiguredInputsError,
)
from ....job_attachments._aws.deadline import get_queue
from ....job_attachments.models import JobAttachmentsFileSystem

from ...exceptions import DeadlineOperationError, CreateJobWaiterCanceled
from .._common import (
    _apply_cli_options_to_config,
    _handle_error,
    _ProgressBarCallbackManager,
    _parse_multi_format_parameters,
    _suggest_resources_on_client_error,
)
from .._main import deadline as main
from ._sigint_handler import SigIntHandler

logger = logging.getLogger(__name__)

# Set up the signal handler for handling Ctrl + C interruptions.
sigint_handler = SigIntHandler()


@main.group(name="bundle")
@_handle_error
def cli_bundle():
    """
    Submit Open Job Description job bundles to a Deadline Cloud queue.

    Use `submit` for headless/scripted submission, or `gui-submit` to
    review and edit parameters in a GUI before submitting.

    \b
    Learn more about [job bundles](https://docs.aws.amazon.com/deadline-cloud/latest/developerguide/build-job-bundle.html)
    """


# Latin alphanumeric, starting with a letter
_openjd_identifier_regex = r"(?-m:^[A-Za-z_][A-Za-z0-9_]*\Z)"


def validate_parameters(ctx, param, value):
    """
    Validate provided --parameter values, ensuring that they are in the format "ParamName=Value", and convert them to a dict with the
    following format:
        [{"name": "<name>", "value": "<value>"}, ...]
    """
    parameters_split = []
    for parameter in value:
        regex_match = re.match("([^=]+)=(.*)", parameter)
        if not regex_match:
            raise click.BadParameter(
                f'Parameters must be provided in the format "ParamName=Value". Invalid parameter: {parameter}'
            )

        if not re.match(_openjd_identifier_regex, regex_match[1]):
            raise click.BadParameter(
                f"Parameter names must be alphanumeric Open Job Description identifiers. Invalid parameter name: {regex_match[1]}"
            )

        parameters_split.append({"name": regex_match[1], "value": regex_match[2]})

    return parameters_split


def _validate_submitter_info(ctx, param, values):
    """
    Validate provided --submitter-info value and convert to SubmitterInfo object.

    Supports three input formats that can be mixed:
    - Key=value pairs: --submitter-info submitter_name=MyApp --submitter-info host_application_name=Maya
    - Inline JSON strings: --submitter-info '{"submitter_name": "MyApp", "additional_info": {"custom": "data"}}'
    - File paths (JSON or YAML): --submitter-info file://path/to/submitter.json

    All keys must be valid SubmitterInfo fields. Unknown keys will raise an error.
    """
    if not values:
        return None

    # Get valid field names from SubmitterInfo dataclass
    valid_fields = {field.name for field in fields(SubmitterInfo)}

    info_dict = _parse_multi_format_parameters(list(values))

    # Validate all keys
    for key in info_dict.keys():
        if key not in valid_fields:
            raise click.BadParameter(
                f"Unknown field '{key}'. Valid fields are: {', '.join(sorted(valid_fields))}"
            )

    # Ensure submitter_name is provided as a required field
    if "submitter_name" not in info_dict:
        raise click.BadParameter(
            "submitter_name is required when using --submitter-info. "
            "Example: --submitter-info submitter_name=MyApp"
        )

    try:
        return SubmitterInfo(**info_dict)
    except TypeError as e:
        raise click.BadParameter(f"Failed to create SubmitterInfo: {e}") from e


def _interactive_confirmation_prompt(message: str, default_response: bool) -> bool:
    """
    Callback to decide if submission should continue or be canceled. Returns True to continue, False to cancel.

    Args:
        warning_message (str): The warning message to display.
        default_response (bool): The default to present as the response (True to continue, False to cancel).
    """
    return click.confirm(
        message,
        default=default_response,
    )


@cli_bundle.command(name="submit")
@click.option(
    "-p",
    "--parameter",
    multiple=True,
    callback=validate_parameters,
    help=(
        "The values for the job template's parameters. Can be provided as key-value pairs, inline JSON strings, "
        "or as paths to a JSON or YAML document. Later values for repeated parameter names take precedence. "
        'Examples: --parameter MyParam=5 -p file://parameter_file.json -p \'{"OtherParam": "10"}\''
    ),
)
@click.option("--profile", help="The AWS profile to use.")
@click.option("--farm-id", help="The farm to use.")
@click.option("--queue-id", help="The queue to use.")
@click.option("--storage-profile-id", help="The storage profile to use.")
@click.option("--name", help="The job name to use in place of the one in the job bundle.")
@click.option(
    "--priority",
    type=int,
    default=50,
    help="The priority of the job. Jobs with a higher priority run first.",
)
@click.option(
    "--max-failed-tasks-count",
    type=int,
    help="The maximum number of failed tasks before the job is marked as failed.",
)
@click.option(
    "--max-retries-per-task",
    type=int,
    help="The maximum number of times to retry a task before it is marked as failed.",
)
@click.option(
    "--max-worker-count",
    type=int,
    help="The max worker count of the job.",
)
@click.option(
    "--target-task-run-status",
    type=click.Choice(["READY", "SUSPENDED"], case_sensitive=False),
    help="The target task run status for the job. READY means tasks will start immediately, "
    "SUSPENDED means tasks will be created but not start until manually resumed.",
)
@click.option(
    "--job-attachments-file-system",
    help="The method workers use to access job attachments. "
    "COPIED means to copy files to the worker and VIRTUAL means to load "
    "files as needed from a virtual file system. If VIRTUAL is selected "
    "but not supported by a worker, it will fallback to COPIED.",
    type=click.Choice([e.value for e in JobAttachmentsFileSystem]),
)
@click.option(
    "--yes",
    is_flag=True,
    help="Automatically accept any confirmation prompts",
)
@click.option(
    "--require-paths-exist",
    is_flag=True,
    help="Return an error if any input files are missing.",
)
@click.option(
    "--submitter-name",
    type=click.STRING,
    help="Name of the application submitting the bundle.",
)
@click.option(
    "--known-asset-path",
    multiple=True,
    help="Path that should not generate warnings when outside storage profile locations. "
    "Can be specified multiple times for different paths.",
)
@click.option(
    "--save-debug-snapshot",
    help="EXPERIMENTAL - Instead of submitting the job, generate a debug snapshot as a directory or a zip file if the extension is .zip."
    " It includes the job attachments and parameters for creating the job."
    " You can later run the bash script in the snapshot to submit the job using AWS CLI commands.",
)
@click.option(
    "--force-s3-check/--no-force-s3-check",
    default=None,
    help="Force verification that job attachments exist in S3 before skipping upload. "
    "Use when S3 bucket contents may be out of sync with local caches. "
    "Overrides the 'settings.force_s3_check' config setting.",
)
@click.argument("job_bundle_dir")
@_handle_error
def bundle_submit(
    job_bundle_dir,
    job_attachments_file_system,
    parameter,
    known_asset_path,
    name,
    priority,
    max_failed_tasks_count,
    max_retries_per_task,
    max_worker_count,
    target_task_run_status,
    require_paths_exist,
    submitter_name,
    save_debug_snapshot,
    force_s3_check,
    **args,
):
    """
    Submits an Open Job Description job bundle to a Deadline Cloud queue.
    You can provide options to set parameter values, the job name, priority,
    and more.

    \b
    Learn more about [job bundles](https://docs.aws.amazon.com/deadline-cloud/latest/developerguide/build-job-bundle.html)
    """
    # Apply the CLI args to the config
    config = _apply_cli_options_to_config(required_options={"farm_id", "queue_id"}, **args)

    # Resolve force_s3_check: CLI flag takes precedence, otherwise use config setting
    if force_s3_check is None:
        force_s3_check = config_file.str2bool(
            config_file.get_setting("settings.force_s3_check", config=config)
        )

    # Resolve max_retries_per_task and max_failed_tasks_count from config when not specified
    if max_retries_per_task is None:
        max_retries_per_task = int(
            config_file.get_setting("settings.max_retries_per_task", config=config)
        )
    if max_failed_tasks_count is None:
        max_failed_tasks_count = int(
            config_file.get_setting("settings.max_failed_tasks_count", config=config)
        )

    hash_callback_manager = _ProgressBarCallbackManager(length=100, label="Hashing Attachments")
    upload_callback_manager = _ProgressBarCallbackManager(length=100, label="Uploading Attachments")

    def _check_create_job_wait_canceled() -> bool:
        return sigint_handler.continue_operation

    try:
        snapshot_tmpdir = None
        if save_debug_snapshot:
            save_debug_snapshot = os.path.abspath(save_debug_snapshot)

            # If the debug snapshot is to a zip file, first put it in a temporary directory
            if save_debug_snapshot.endswith(".zip"):
                snapshot_tmpdir = tempfile.TemporaryDirectory()

        job_id = api.create_job_from_job_bundle(
            job_bundle_dir=job_bundle_dir,
            job_parameters=parameter,
            name=name,
            job_attachments_file_system=job_attachments_file_system,
            config=config,
            priority=priority,
            max_failed_tasks_count=max_failed_tasks_count,
            max_retries_per_task=max_retries_per_task,
            max_worker_count=max_worker_count,
            target_task_run_status=target_task_run_status,
            hashing_progress_callback=hash_callback_manager.callback,
            upload_progress_callback=upload_callback_manager.callback,
            create_job_result_callback=_check_create_job_wait_canceled,
            print_function_callback=click.echo,
            interactive_confirmation_callback=_interactive_confirmation_prompt,
            require_paths_exist=require_paths_exist,
            submitter_name=submitter_name or "CLI",
            known_asset_paths=known_asset_path,
            debug_snapshot_dir=(snapshot_tmpdir.name if snapshot_tmpdir else save_debug_snapshot),
            force_s3_check=force_s3_check,
        )

        if snapshot_tmpdir:
            # Put the snapshot in a zip file
            os.makedirs(os.path.dirname(save_debug_snapshot), exist_ok=True)
            shutil.make_archive(save_debug_snapshot, "zip", snapshot_tmpdir.name)

        if save_debug_snapshot:
            click.echo("Saved job debug snapshot:")
            click.echo(f"    {save_debug_snapshot}")

        # Check Whether the CLI options are modifying any of the default settings that affect
        # the job id. If not, we'll save the job id submitted as the default job id.
        # If a job snapshot directory was provided, the job_id will be None.
        if (
            args.get("profile") is None
            and args.get("farm_id") is None
            and args.get("queue_id") is None
            and args.get("storage_profile_id") is None
            and job_id
        ):
            config_file.set_setting("defaults.job_id", job_id)

    except AssetSyncCancelledError as exc:
        if sigint_handler.continue_operation:
            raise DeadlineOperationError(f"Job submission unexpectedly canceled:\n{exc}") from exc
        else:
            click.echo("Job submission canceled.")
            sys.exit(1)
    except AssetSyncError as exc:
        raise DeadlineOperationError(f"Failed to upload job attachments:\n{exc}") from exc
    except CreateJobWaiterCanceled as exc:
        if sigint_handler.continue_operation:
            raise DeadlineOperationError(
                f"Unexpectedly canceled during wait for final status of CreateJob:\n{exc}"
            ) from exc
        else:
            click.echo("Canceled waiting for final status of CreateJob.")
            sys.exit(1)
    except ClientError as exc:
        suggestion = _suggest_resources_on_client_error(
            exc,
            farm_id=config_file.get_setting("defaults.farm_id", config=config),
            queue_id=config_file.get_setting("defaults.queue_id", config=config),
            config=config,
        )
        raise DeadlineOperationError(
            f"Failed to submit the job bundle to AWS Deadline Cloud:\n{exc}{suggestion}"
        ) from exc
    except MisconfiguredInputsError as exc:
        click.echo(str(exc))
        click.echo("Job submission canceled.")
        sys.exit(1)
    except Exception as exc:
        api.get_deadline_cloud_library_telemetry_client().record_error_with_trace(exc, "on_submit")
        raise
    finally:
        if snapshot_tmpdir:
            snapshot_tmpdir.cleanup()


@cli_bundle.command(name="gui-submit")
@click.option(
    "-p",
    "--parameter",
    multiple=True,
    callback=validate_parameters,
    help=(
        "Initial values in the GUI for the job template's parameters. Can be provided as key-value pairs, inline JSON strings, "
        "or as paths to a JSON or YAML document. Later values for repeated parameter names take precedence. "
        'Examples: --parameter MyParam=5 -p file://parameter_file.json -p \'{"OtherParam": "10"}\''
    ),
)
@click.argument("job_bundle_dir", required=False)
@click.option(
    "--browse",
    is_flag=True,
    help="Opens a folder browser to select a bundle.",
)
@click.option(
    "--install-gui",
    is_flag=True,
    help="Installs GUI dependencies if they are not installed already",
)
@click.option(
    "--submitter-name",
    help="[DEPRECATED] Use --submitter-info submitter_name=<name> instead. Name of the application submitting the bundle. If a name is specified, the GUI will automatically close after submitting the job.",
)
@click.option(
    "--output",
    type=click.Choice(
        ["verbose", "json"],
        case_sensitive=False,
    ),
    default="verbose",
    help="Specifies the output format of the messages printed to stdout.\n"
    "VERBOSE: Displays messages in a human-readable text format.\n"
    "JSON: Displays messages in JSON line format, so that the info can be easily "
    "parsed/consumed by custom scripts.",
)
@click.option(
    "--known-asset-path",
    multiple=True,
    help="Path that should not generate warnings when outside storage profile locations. "
    "Can be specified multiple times for different paths.",
)
@click.option(
    "--submitter-info",
    multiple=True,
    callback=_validate_submitter_info,
    help="Submitter and environment information. Supports key=value pairs, inline JSON strings, "
    "and file paths (JSON or YAML). Later values for repeated fields take precedence. "
    "Examples: --submitter-info submitter_name=MyApp --submitter-info host_application_name=Maya "
    'OR --submitter-info \'{"submitter_name": "MyApp", "additional_info": {"render_engine": "Cycles"}}\' '
    "OR --submitter-info file://path/to/submitter.json",
)
@click.option("--name", help="The job name to use in place of the one in the job bundle.")
@_handle_error
def bundle_gui_submit(
    parameter,
    job_bundle_dir,
    browse,
    output,
    install_gui,
    known_asset_path,
    submitter_name,
    submitter_info,
    name,
    **args,
):
    """
    Opens a GUI to submit an Open Job Description job bundle to a Deadline
    Cloud queue. You can provide options to set the initial parameter values
    shown in the GUI.

    \b
    Learn more about [job bundles](https://docs.aws.amazon.com/deadline-cloud/latest/developerguide/build-job-bundle.html)
    """

    if submitter_name:
        click.echo(
            click.style(
                "DeprecationWarning: The option --submitter-name is deprecated. Use --submitter-info instead.",
                fg="red",
            ),
            err=True,
        )
        if submitter_info:
            # --submitter-name takes precedence if we already have submitter_info provided
            submitter_info.submitter_name = submitter_name
        else:
            submitter_info = SubmitterInfo(submitter_name=submitter_name)

    from ...ui import gui_context_for_cli
    from ...ui._utils import tr

    with gui_context_for_cli(automatically_install_dependencies=install_gui) as app:
        from ...ui.job_bundle_submitter import show_job_bundle_submitter

        if not job_bundle_dir and not browse:
            raise DeadlineOperationError(
                tr(
                    "Specify a job bundle directory or run the bundle command with the --browse flag"
                )
            )
        output = output.lower()

        submitter = show_job_bundle_submitter(
            input_job_bundle_dir=job_bundle_dir,
            browse=browse,
            submitter_info=submitter_info,
            known_asset_paths=known_asset_path,
            job_parameters=parameter,
            name=name,
        )

        if not submitter:
            return

        submitter.show()

        app.exec()

        _print_response(
            output=output,
            job_bundle_dir=job_bundle_dir,
            job_history_bundle_dir=submitter.job_history_bundle_dir,
            job_id=submitter.job_id,
        )


def _print_response(
    output: str,
    job_bundle_dir: str,
    job_history_bundle_dir: Optional[str],
    job_id: Optional[str],
):
    if output == "json":
        if job_id:
            response: dict[str, Any] = {
                "status": "SUBMITTED",
                "jobId": job_id,
                "jobHistoryBundleDirectory": job_history_bundle_dir,
            }
            click.echo(json.dumps(response))
        else:
            click.echo(json.dumps({"status": "CANCELED"}))
    else:
        if job_id:
            click.echo("Submitted job bundle:")
            click.echo(f"   {job_bundle_dir}")
            click.echo(f"Job ID: {job_id}")
        else:
            click.echo("Job submission canceled.")


def _truncate_metadata(value: str, limit: int, field: str) -> str:
    """Truncate a metadata value, warning if truncation occurs.

    S3 user-defined metadata is limited to 2 KB total (sum of all UTF-8 encoded keys and values).
    We apply conservative per-field limits to stay well within that budget.
    See: https://docs.aws.amazon.com/AmazonS3/latest/userguide/UsingMetadata.html#UserMetadata
    """
    if len(value) > limit:
        click.echo(
            click.style(
                f"Warning: Bundle metadata '{field}' truncated from {len(value)} to {limit} characters",
                fg="yellow",
            ),
            err=True,
        )
        return value[: limit - 3] + "..."
    return value


def _get_queue_s3_settings(config):
    """Get the queue's job attachment S3 settings from config."""
    farm_id = config_file.get_setting("defaults.farm_id", config=config)
    queue_id = config_file.get_setting("defaults.queue_id", config=config)
    if not farm_id or not queue_id:
        raise DeadlineOperationError(
            "A default farm and queue must be configured. Run 'deadline config set defaults.farm_id <id>' and 'deadline config set defaults.queue_id <id>'."
        )
    boto3_session = api.get_boto3_session(config=config)
    queue = get_queue(farm_id=farm_id, queue_id=queue_id, session=boto3_session)
    if not queue.jobAttachmentSettings:
        raise DeadlineOperationError(
            f"Queue {queue_id} does not have job attachment settings configured."
        )
    # Use queue role credentials for S3 access (required for DCM profiles)
    deadline_client = api.get_boto3_client("deadline", config=config)
    s3_session = api.get_queue_user_boto3_session(
        deadline=deadline_client, config=config, farm_id=farm_id, queue_id=queue_id
    )
    return queue.jobAttachmentSettings, s3_session


@cli_bundle.command(name="list")
@click.argument("path", required=False)
@click.option(
    "--queue",
    "use_queue",
    is_flag=True,
    help="List bundles shared on the queue.",
)
@click.option(
    "--show-hidden",
    is_flag=True,
    help="Include hidden bundles in the output (queue only).",
)
@click.option(
    "--no-archives",
    is_flag=True,
    help="Skip archive files when listing local bundles.",
)
@click.option("--profile", help="The AWS profile to use.")
@click.option("--farm-id", help="The farm to use.")
@click.option("--queue-id", help="The queue to use.")
@click.option(
    "--output",
    type=click.Choice(["text", "json"], case_sensitive=False),
    default="text",
    help="Output format. TEXT prints one name per line, JSON prints full details.",
)
@_handle_error
def bundle_list(path, use_queue, show_hidden, no_archives, output, **args):
    """
    List job bundles.

    \b
    With no arguments, lists bundles in the configured default local directory
    (settings.job_bundle_default_directory, or home if not set).
    With PATH, lists bundles in that local directory.
    With --queue, lists bundles shared on the queue.
    """

    hidden_set: set[str] = set()
    if use_queue:
        config = _apply_cli_options_to_config(required_options={"farm_id", "queue_id"}, **args)
        repo: BundleRepository = S3BundleRepository.from_config(config)
        hidden_set = repo.get_hidden_set()  # type: ignore[attr-defined]
    else:
        if path:
            local_root = os.path.abspath(path)
        else:
            local_root = os.environ.get("DEADLINE_JOB_BUNDLE_DEFAULT_DIRECTORY", "")
            if not local_root:
                local_root = config_file.get_setting("settings.job_bundle_default_directory")
            if not local_root:
                local_root = os.path.expanduser("~")
            local_root = os.path.expanduser(local_root)
        repo = LocalBundleRepository(root=local_root, include_archives=not no_archives)

    entries = repo.list_entries(repo.root_path())
    bundles = [e for e in entries if e.is_bundle]

    # Filter hidden bundles unless --show-hidden
    if use_queue and not show_hidden:
        bundles = [e for e in bundles if e.name not in hidden_set]

    # Prune stale hidden entries
    if use_queue and hidden_set:
        existing_names = {e.name for e in entries if e.is_bundle}
        repo.prune_hidden_set(existing_names)  # type: ignore[attr-defined]

    if output == "json":
        result = [
            {
                "name": e.name,
                "path": e.path,
                "format": "archive" if e.is_archive else "folder",
                **({"hidden": True} if e.name in hidden_set else {}),
            }
            for e in bundles
        ]
        click.echo(json.dumps(result, indent=2))
    else:
        for e in bundles:
            suffix = " (hidden)" if e.name in hidden_set else ""
            click.echo(f"{e.name}{suffix}")


@cli_bundle.group(name="cache")
@_handle_error
def cli_bundle_cache():
    """Manage the local cache of queue job bundles."""


@cli_bundle_cache.command(name="clean")
@click.argument("bundle_name", required=False)
@click.option("--dry-run", is_flag=True, help="Show what would be removed without deleting.")
@_handle_error
def bundle_cache_clean(bundle_name, dry_run):
    """Remove cached queue bundle archives from the local cache."""

    cache_root = _get_bundle_cache_dir()
    if not os.path.isdir(cache_root):
        click.echo("No bundle cache found.")
        return

    removed = 0
    total_size = 0

    for hash_dir in os.listdir(cache_root):
        hash_path = os.path.join(cache_root, hash_dir)
        if not os.path.isdir(hash_path):
            continue
        for name in os.listdir(hash_path):
            bundle_path = os.path.join(hash_path, name)
            if not os.path.isdir(bundle_path):
                continue
            if bundle_name and name != bundle_name:
                continue
            size = sum(
                os.path.getsize(os.path.join(r, f))
                for r, _, files in os.walk(bundle_path)
                for f in files
            )
            if dry_run:
                click.echo(f"Would remove: {name} ({size / 1024:.1f} KB)")
            else:
                shutil.rmtree(bundle_path)
                # Remove parent hash dir if now empty
                if not os.listdir(hash_path):
                    os.rmdir(hash_path)
                click.echo(f"Removed cached bundle: {name}")
            removed += 1
            total_size += size

    if removed == 0:
        click.echo(
            "No cached bundles found."
            if not bundle_name
            else f"Bundle '{bundle_name}' not found in cache."
        )
    elif dry_run:
        click.echo(f"Would remove {removed} cached bundle(s) ({total_size / (1024 * 1024):.1f} MB)")
    else:
        click.echo(f"Removed {removed} cached bundle(s) ({total_size / (1024 * 1024):.1f} MB)")
        # Remove cache root if now empty
        if os.path.isdir(cache_root) and not os.listdir(cache_root):
            os.rmdir(cache_root)


@cli_bundle_cache.command(name="update")
@click.argument("bundle_name", required=False)
@click.option("--profile", help="The AWS profile to use.")
@click.option("--farm-id", help="The farm to use.")
@click.option("--queue-id", help="The queue to use.")
@_handle_error
def bundle_cache_update(bundle_name, **args):
    """Re-download any stale cached bundles from the queue by checking ETags."""

    config = _apply_cli_options_to_config(required_options={"farm_id", "queue_id"}, **args)
    repo = S3BundleRepository.from_config(config)

    # List remote bundles to match against cache
    entries = repo.list_entries(repo.root_path())
    archive_bundles = {e.name: e for e in entries if e.is_bundle and e.is_archive}

    cache_root = _get_bundle_cache_dir()
    if not os.path.isdir(cache_root):
        click.echo("No bundle cache found.")
        return

    updated = 0
    up_to_date = 0
    checked = 0

    for hash_dir in os.listdir(cache_root):
        hash_path = os.path.join(cache_root, hash_dir)
        if not os.path.isdir(hash_path):
            continue
        for name in os.listdir(hash_path):
            bundle_path = os.path.join(hash_path, name)
            if not os.path.isdir(bundle_path):
                continue
            if bundle_name and name != bundle_name:
                continue

            meta = _read_cache_meta(bundle_path)
            if not meta:
                continue

            # Find the matching remote bundle
            if name not in archive_bundles:
                continue

            checked += 1
            entry = archive_bundles[name]

            # Force a resolve which checks ETag and re-downloads if stale
            result_path = repo.resolve_bundle(entry.path, "")
            new_meta = _read_cache_meta(
                os.path.dirname(result_path) if result_path != bundle_path else bundle_path
            )

            if new_meta and new_meta.get("etag") != meta.get("etag"):
                click.echo(f"{name}: updated")
                updated += 1
            else:
                click.echo(f"{name}: up-to-date")
                up_to_date += 1

    if checked == 0:
        click.echo(
            "No cached bundles found."
            if not bundle_name
            else f"Bundle '{bundle_name}' not found in cache."
        )
    else:
        click.echo(f"Checked {checked} bundle(s): {updated} updated, {up_to_date} up-to-date")


@cli_bundle.command(name="upload")
@click.argument("job_bundle_dir")
@click.option("--profile", help="The AWS profile to use.")
@click.option("--farm-id", help="The farm to use.")
@click.option("--queue-id", help="The queue to use.")
@click.option(
    "--name",
    help="Name for the shared archive on the queue. Defaults to the bundle directory name.",
)
@_handle_error
def bundle_upload(job_bundle_dir, name, **args):
    """
    Upload a job bundle to share on the queue as an .ojd archive.
    """
    config = _apply_cli_options_to_config(required_options={"farm_id", "queue_id"}, **args)
    s3_settings, boto3_session = _get_queue_s3_settings(config)

    job_bundle_dir = os.path.abspath(job_bundle_dir)
    if not is_job_bundle_dir(job_bundle_dir):
        raise DeadlineOperationError(
            f"Directory does not appear to be a job bundle (no template.yaml or template.json): {job_bundle_dir}"
        )

    # Parse the template to extract metadata for S3 object metadata
    bundle_metadata = {}
    for tname in ("template.yaml", "template.json"):
        tpath = os.path.join(job_bundle_dir, tname)
        if os.path.isfile(tpath):
            with open(tpath, encoding="utf-8") as f:
                template = _parse_template(f.read(), tname)
            if template:
                info = _extract_bundle_info(
                    template,
                    job_bundle_dir,
                    LocalBundleRepository._read_parameter_values(job_bundle_dir),
                )
                bundle_metadata[METADATA_KEY_NAME] = _truncate_metadata(
                    info.name, METADATA_LIMIT_NAME, METADATA_KEY_NAME
                )
                if info.description:
                    # S3 metadata values must be valid HTTP header values (no newlines)
                    desc = " ".join(info.description.split())
                    bundle_metadata[METADATA_KEY_DESC] = _truncate_metadata(
                        desc, METADATA_LIMIT_DESC, METADATA_KEY_DESC
                    )
                if info.step_names:
                    bundle_metadata[METADATA_KEY_STEPS] = _truncate_metadata(
                        ",".join(info.step_names), METADATA_LIMIT_STEPS, METADATA_KEY_STEPS
                    )
                if info.parameters:
                    param_strs = [
                        f"{p.get('name', '?')}:{p.get('type', '?')}" for p in info.parameters
                    ]
                    bundle_metadata[METADATA_KEY_PARAMS] = _truncate_metadata(
                        ",".join(param_strs), METADATA_LIMIT_PARAMS, METADATA_KEY_PARAMS
                    )
            break

    bundle_name = name or os.path.basename(job_bundle_dir)
    if not bundle_name or not bundle_name.strip("/ \\"):
        raise DeadlineOperationError(
            "Bundle name is empty or invalid. Use --name to specify a valid name."
        )
    prefix = f"{s3_settings.rootPrefix.rstrip('/')}/{S3_JOB_BUNDLES_PREFIX}"
    s3_key = f"{prefix}/{bundle_name}.ojd"
    if len(s3_key) > 1024:
        raise DeadlineOperationError(
            f"Bundle name is too long. S3 key would be {len(s3_key)} characters (max 1024)."
        )

    s3 = boto3_session.client("s3")

    # Check if bundle already exists
    try:
        s3.head_object(Bucket=s3_settings.s3BucketName, Key=s3_key)
        if not click.confirm(f"Bundle '{bundle_name}' already exists on the queue. Overwrite?"):
            click.echo("Upload canceled.")
            return
    except ClientError as e:
        if e.response["Error"]["Code"] != "404":
            raise

    # Archive and upload
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for root, dirs, files in os.walk(job_bundle_dir, followlinks=False):
            # Skip symlinked directories
            dirs[:] = [d for d in dirs if not os.path.islink(os.path.join(root, d))]
            for fname in files:
                local_path = os.path.join(root, fname)
                if os.path.islink(local_path):
                    logger.warning("Skipping symlink: %s", local_path)
                    continue
                arcname = os.path.relpath(local_path, job_bundle_dir)
                zf.write(local_path, arcname)

    buf.seek(0)
    s3.upload_fileobj(
        buf,
        s3_settings.s3BucketName,
        s3_key,
        ExtraArgs={"Metadata": bundle_metadata} if bundle_metadata else None,
    )
    click.echo(f"Uploaded bundle to s3://{s3_settings.s3BucketName}/{s3_key}")


@cli_bundle.command(name="download")
@click.argument("bundle_name")
@click.option("--profile", help="The AWS profile to use.")
@click.option("--farm-id", help="The farm to use.")
@click.option("--queue-id", help="The queue to use.")
@click.option(
    "-o",
    "--output-dir",
    default=".",
    help="Local directory to download the bundle to. Defaults to current directory.",
)
@_handle_error
def bundle_download(bundle_name, output_dir, **args):
    """
    Download a shared job bundle from the queue.

    BUNDLE_NAME is the name of the bundle (e.g. 'blender-render').
    """

    config = _apply_cli_options_to_config(required_options={"farm_id", "queue_id"}, **args)
    repo = S3BundleRepository.from_config(config)

    output_dir = os.path.abspath(output_dir)
    os.makedirs(output_dir, exist_ok=True)

    # List entries to find the bundle by name
    entries = repo.list_entries(repo.root_path())
    match = None
    for entry in entries:
        if entry.name == bundle_name:
            match = entry
            break

    if not match:
        available = [e.name for e in entries if e.is_bundle]
        msg = f"Bundle '{bundle_name}' not found in {repo.root_path()}"
        if available:
            msg += f"\nAvailable bundles: {', '.join(available)}"
        raise DeadlineOperationError(msg)

    local_path = repo.download_full_bundle(match.path, output_dir)
    # download_full_bundle resolves to cache; copy to user's output_dir
    dest_path = os.path.join(output_dir, sanitize_bundle_name(bundle_name))
    if os.path.exists(dest_path):
        shutil.rmtree(dest_path)
    shutil.copytree(local_path, dest_path)
    click.echo(f"Downloaded bundle to: {dest_path}")


@cli_bundle.command(name="hide")
@click.argument("bundle_name")
@click.option("--profile", help="The AWS profile to use.")
@click.option("--farm-id", help="The farm to use.")
@click.option("--queue-id", help="The queue to use.")
@_handle_error
def bundle_hide(bundle_name, **args):
    """
    Hide a shared bundle on the queue.

    The bundle remains in S3 but is no longer shown in the browser or
    `deadline bundle list` by default. Use --show-hidden to see it.
    """
    config = _apply_cli_options_to_config(required_options={"farm_id", "queue_id"}, **args)
    repo = S3BundleRepository.from_config(config)

    hidden_set = repo.get_hidden_set()
    if bundle_name in hidden_set:
        click.echo(f"Bundle already hidden: {bundle_name}")
        return

    repo.set_bundle_visibility(bundle_name, hidden=True)
    click.echo(f"Hidden bundle: {bundle_name}")


@cli_bundle.command(name="unhide")
@click.argument("bundle_name")
@click.option("--profile", help="The AWS profile to use.")
@click.option("--farm-id", help="The farm to use.")
@click.option("--queue-id", help="The queue to use.")
@_handle_error
def bundle_unhide(bundle_name, **args):
    """
    Unhide a previously hidden bundle on the queue.

    Makes the bundle visible again in the browser and `deadline bundle list`.
    """
    config = _apply_cli_options_to_config(required_options={"farm_id", "queue_id"}, **args)
    repo = S3BundleRepository.from_config(config)

    hidden_set = repo.get_hidden_set()
    if bundle_name not in hidden_set:
        click.echo(f"Bundle is not hidden: {bundle_name}")
        return

    repo.set_bundle_visibility(bundle_name, hidden=False)
    click.echo(f"Unhidden bundle: {bundle_name}")


@cli_bundle.command(name="info")
@click.argument("bundle_name")
@click.option(
    "--queue",
    "use_queue",
    is_flag=True,
    help="Inspect a bundle shared on the queue.",
)
@click.option(
    "--output",
    type=click.Choice(["verbose", "json"], case_sensitive=False),
    default="verbose",
    help="Output format.",
)
@click.option("--profile", help="The AWS profile to use.")
@click.option("--farm-id", help="The farm to use.")
@click.option("--queue-id", help="The queue to use.")
@_handle_error
def bundle_info(bundle_name, use_queue, output, **args):
    """
    Show details about a job bundle (name, description, steps, parameters).

    BUNDLE_NAME is either a local path to a job bundle directory, or the name
    of a shared bundle on the queue (when used with --queue). For local bundles,
    if the path doesn't exist, searches by name in the current directory and then
    the configured job bundle default directory.
    """
    if use_queue:
        config = _apply_cli_options_to_config(required_options={"farm_id", "queue_id"}, **args)
        repo: BundleRepository = S3BundleRepository.from_config(config)
        # Find the bundle by name in the listing
        entries = repo.list_entries(repo.root_path())
        match = next((e for e in entries if e.name == bundle_name and e.is_bundle), None)
        if not match:
            available = [e.name for e in entries if e.is_bundle]
            msg = f"Bundle '{bundle_name}' not found on queue."
            if available:
                msg += f"\nAvailable bundles: {', '.join(available)}"
            raise DeadlineOperationError(msg)
        info = repo.get_bundle_info(match.path)
    else:
        bundle_path = os.path.abspath(bundle_name)
        if not os.path.isdir(bundle_path):
            # Search by name in cwd, then configured default directory
            for search_dir in [
                os.getcwd(),
                os.path.expanduser(
                    config_file.get_setting("settings.job_bundle_default_directory") or ""
                ),
            ]:
                if not search_dir:
                    continue
                candidate = os.path.join(search_dir, bundle_name)
                if os.path.isdir(candidate) and is_job_bundle_dir(candidate):
                    bundle_path = candidate
                    break
            else:
                raise DeadlineOperationError(
                    f"Bundle '{bundle_name}' not found as a path, in current directory, "
                    "or in the configured job bundle default directory."
                )
        repo = LocalBundleRepository(root=os.path.dirname(bundle_path))
        info = repo.get_bundle_info(bundle_path)

    if not info:
        raise DeadlineOperationError(
            f"Could not read bundle template for '{bundle_name}'. "
            "The template may be missing or malformed."
        )

    if output == "json":
        result = info.to_dict()
        result["path"] = info.path
        click.echo(json.dumps(result, indent=2))
    else:
        click.echo(f"Path: {info.path}")
        click.echo(info.format_text())

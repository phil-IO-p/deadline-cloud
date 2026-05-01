# Job Bundle Browser

## Problem

When using `deadline bundle gui-submit --browse` or the "Load a different job bundle" button, users are presented with a native OS folder picker. This is inadequate because:

- Job bundles are not distinguishable from regular folders by name alone.
- Users must already know where their bundles live and navigate there manually.
- There is no preview of what a bundle contains — users pick blindly.
- The picker always starts at the job history directory or home, with no way to configure a default location.

## Overview

Replace the native folder picker with a custom job bundle browser dialog that:

1. Provides a navigable directory tree showing only folders, archives, and job bundles.
2. Displays a preview panel with bundle metadata when a job bundle is selected.
3. Supports both local filesystem and S3 bucket browsing through a common backend abstraction.
4. Supports job bundles as directories or archives (`.zip`, `.tar.gz`, `.tgz`, `.tar.bz2`, `.tar.xz`, `.tar`).
5. For S3, uses the selected queue's job attachment bucket with a `job-bundles/` prefix — no extra configuration needed.
6. Caches S3 archive bundles locally with ETag validation for fast repeated access.
7. Respects a configurable default local browse directory.

## Design

### Bundle Formats

Job bundles can be either:

- **Directories** — a folder containing `template.yaml` or `template.json` at the root, plus any scripts, data files, and `asset_references.yaml`.
- **Archives** — a `.zip`, `.tar.gz`, `.tgz`, `.tar.bz2`, `.tar.xz`, or `.tar` file containing a job bundle. The template can be at the archive root or inside a single wrapper directory.

Both formats are supported for both local and S3 browsing. Archives are extracted to a local directory before submission. If an archive contains a single top-level wrapper directory (e.g. `my-bundle/template.yaml` instead of `template.yaml` at the root), the wrapper is detected and the inner directory is used as the bundle path.

### Backend Abstraction

To support both local and S3 browsing without coupling the UI to either, introduce a `BundleRepository` protocol:

```python
@dataclass
class BundleInfo:
    """Metadata extracted from a job bundle's template."""
    path: str              # Local path, archive path, or s3:// URI
    name: str              # From template "name" field
    description: str       # From template "description" field, or ""
    step_names: list[str]  # Names of each step in the template
    parameters: list[dict] # Parameter definitions from the template

@dataclass
class BrowseEntry:
    """A single item in the browser listing."""
    name: str              # Display name (folder basename or archive name without extension)
    path: str              # Full path or S3 URI
    is_bundle: bool        # True if this is a valid job bundle
    is_archive: bool       # True if this is an archive file

class BundleRepository(Protocol):
    def list_entries(self, path: str) -> list[BrowseEntry]:
        """List immediate children of `path`. Returns folders, archives, and bundles."""
        ...

    def get_bundle_info(self, path: str) -> Optional[BundleInfo]:
        """Load and return metadata for the bundle at `path`, or None if invalid."""
        ...

    def root_path(self) -> str:
        """The starting path for browsing."""
        ...
```

Two implementations:

- `LocalBundleRepository` — walks the local filesystem. Lists directories and archive files. Directories are bundles if they contain `template.yaml`/`template.json`. Archives are always shown as bundles (validated on preview). Provides `extract_bundle()` for extracting archives to a local directory.
- `S3BundleRepository` — lists objects and prefixes under the queue's job attachment bucket at `{rootPrefix}/job-bundles/`. Folder prefixes and archive objects are both listed. Provides `resolve_bundle()` which handles both folder downloads and archive download+cache+extract.

### S3 Bucket Convention

The S3 bundle repository browses:

```
s3://{s3BucketName}/{rootPrefix}/job-bundles/
```

Where `s3BucketName` and `rootPrefix` come from the selected queue's `jobAttachmentSettings`. This means:

- No extra configuration is needed — the bucket is derived from the queue the user already has selected.
- Users (or admins) place job bundles in the `job-bundles/` folder within the queue's attachment bucket.
- Bundles can be either folders (common prefixes containing a template) or archive files.

Example S3 layout:
```
s3://my-farm-bucket/DeadlineCloud/job-bundles/
    blender-render.zip
    maya-arnold.tar.gz
    simple-job/
        template.yaml
    data-processing/
        template.yaml
        scripts/
            process.py
```

### Job History Source

The Job History source browses the job history directory for the current AWS profile, as configured by `settings.job_history_dir` (default: `~/.deadline/job_history/{aws_profile_name}`). This directory contains bundles from previous submissions, organized by date.

This is useful for:
- Re-submitting a previous job with modified parameters.
- Using a previously submitted bundle as a starting point for a new submission.
- Reviewing what was submitted in the past.

The Job History source uses the same `LocalBundleRepository` as the Local source, just rooted at the job history directory instead of the user's home or configured default.

### S3 Archive Caching

Archive bundles from S3 are cached locally to avoid re-downloading on repeated use.

**Cache location**: `~/.deadline/cache/job-bundles/{hash}/{bundle-name}/`

Where `{hash}` is a truncated SHA-256 of `{bucket}/{s3-key}` to ensure uniqueness.

**Cache validation**: On each access, a single `head_object` call retrieves the archive's ETag. If it matches the cached ETag, the local copy is used directly. If it differs (or no cache exists), the archive is re-downloaded and re-extracted.

**Cache metadata** (`.bundle_cache_meta.json`):
```json
{
  "etag": "\"d41d8cd98f00b204e9800998ecf8427e\"",
  "last_modified": "2026-04-30T12:00:00+00:00"
}
```

**Why only archives are cached**: An archive is a single S3 object with a single ETag — one `head_object` validates the entire bundle. Folder-based bundles are multiple objects with no single version identifier, so staleness detection would require checking every file. Folder bundles are downloaded to temp directories with atexit cleanup instead.

### S3 Object Metadata for Preview

When `deadline bundle upload` uploads an archive, it attaches bundle metadata as S3 user metadata on the object:

- `bundle-name`: The template's `name` field
- `bundle-description`: The template's `description` field (newlines collapsed to spaces)
- `bundle-steps`: Comma-separated list of step names
- `bundle-parameters`: Comma-separated `name:type` pairs

This metadata is returned by `head_object`, which is already called for ETag validation. This means preview of uploaded archives requires **zero downloads** — a single `head_object` provides both cache validation and all preview information.

The preview priority chain for S3 archives:
1. **S3 user metadata** from `head_object` → instant, no download
2. **Local cache** if ETag matches → read template from disk
3. **Download archive** → parse template, populate cache (fallback for archives not uploaded via the CLI)

S3 user metadata has a 2KB total limit, which is sufficient for typical bundle metadata. Per-field limits: `bundle-name` is truncated to 256 characters, `bundle-description`, `bundle-steps`, and `bundle-parameters` are each truncated to 512 characters.

### Detection: What Is a Job Bundle?

- **Directories** (local or S3 prefix): contains `template.yaml` or `template.json`.
- **Archives** (local file or S3 object): filename ends with a supported archive extension. Validated by reading the template from inside the archive on preview.

For `list_entries`, detection is kept fast:

- **Local directories**: stat check for template file existence (no parsing).
- **Local archives**: matched by file extension, then validated by checking for a template inside the archive. This prevents random zip files from appearing as bundles. Archive scanning can be disabled via `include_archives=False` on `LocalBundleRepository` (used by the browser for Local/History sources, and via `--no-archives` in the CLI). The browser dialog disables local archives because the primary use case for archives is S3-shared bundles — local users work with directory bundles directly.
- **S3 folders**: detected via batch recursive listing — a single `list_objects_v2` (without delimiter) returns all keys under the parent prefix, and we check in-memory which child prefixes contain a template file. This replaces per-folder `head_object` calls, reducing N+1 API calls to 2 (one delimited list + one recursive list).
- **S3 archives**: matched by key extension only (no API call).

Full template parsing happens only in `get_bundle_info` when the user clicks a bundle for preview.

### Browser Dialog UI

```
┌─────────────────────────────────────────────────────────────┐
│  Job Bundle Browser                                         │
├────────────────────────────────┬────────────────────────────┤
│  [Filter bundles...         ]  │  Name: Blender Render      │
│  📁 my-bundles/               │  Description: Renders a    │
│    📦 blender-render          │  Blender scene file...     │
│    📦 maya-arnold             │                            │
│    📁 wip/                    │  Steps:                    │
│      📦 experimental-job      │    • RenderBlender         │
│    📦 simple-job/             │                            │
│                               │  Parameters:               │
│                               │    • BlenderSceneFile (PATH)│
│                               │    • Frames (STRING)       │
│                               │    • OutputDir (PATH)      │
│                               │    • Format (STRING)       │
│                               │                            │
├────────────────────────────────┴────────────────────────────┤
│  Source: ( ) Local  (•) S3 (my-farm-bucket)  ( ) History   │
│  Path:  [/job-bundles/                      ]               │
│                                          [Cancel] [Select]  │
└─────────────────────────────────────────────────────────────┘
```

**Left panel** — Filter and navigable tree view:
- A text filter at the top that narrows the tree as you type. Case-insensitive, matches against entry names. Uses recursive filtering so parent folders remain visible when a child matches. The tree auto-expands when filtering to show results.
- Shows folders (📁) and job bundles (📦) with distinct icons. Both directory bundles and archive bundles use the 📦 icon.
- Clicking a folder clears any active filter, expands the folder to show its children, and scrolls it to the top of the view. This makes the search-then-navigate flow natural: search for a folder, click it, see its contents.
- Job bundles are leaf nodes (selectable, not expandable).
- Non-bundle, non-archive files are hidden.

**Right panel** — Preview (shown when a bundle is selected, scrollable):
- **Name**: From the template's `name` field. `{{Param.X}}` references are resolved using values from `parameter_values.yaml` or template defaults.
- **Description**: From the template's `description` field, if present.
- **Steps**: List of step names from the template, in definition order.
- **Parameters**: Name, type, and value of each parameter definition, in definition order. Values are resolved in priority order: `parameter_values.yaml`/`.json` > template `default` > blank. For S3 archives, values are available once the bundle is cached locally (first click caches, subsequent clicks show values).

**Bottom bar**:
- Radio toggle between Local, S3, and Job History sources. S3 is selected by default when available; otherwise Local is selected. S3 option shows the bucket name from the queue and is disabled if the queue has no job attachment settings or S3 access fails (with a tooltip explaining why). Job History browses the `settings.job_history_dir` for the current AWS profile, showing previously submitted bundles; it is disabled if the job history directory does not exist on disk.
- Path display showing the current browse location.
- Cancel and Select buttons. Select is enabled only when a valid bundle is highlighted.

### Share Button

The submitter dialog includes a "Share" button alongside the existing "Export bundle" and "Submit" buttons. Clicking "Share" archives the current job bundle and uploads it to the queue's S3 `job-bundles/` folder, making it available to the team via the browser's S3 source. The bundle name defaults to the job name, with `{{Param.X}}` references resolved using current parameter values. Spaces and slashes in the resolved name are replaced with underscores. S3 user metadata (name, description, steps, parameters) is attached for zero-download preview.

Share is enabled when the API is available and a farm and queue are configured — it does not require valid queue parameters (unlike Submit), since sharing only needs S3 access, not a runnable job configuration.

Note: uploading a bundle with the same name as an existing one silently overwrites it in S3.

### Lazy Loading

The tree is populated lazily — only the children of expanded nodes are fetched. This keeps the initial load fast and avoids scanning deep directory trees or making excessive S3 API calls.

### Configuration

Add a new setting for the default local browse directory:

```python
# In SETTINGS dict in config_file.py
"settings.job_bundle_default_directory": {
    "default": "",
    "description": (
        "The default local directory to open when browsing for job bundles. "
        "If empty, defaults to the user's home directory."
    ),
}
```

Environment variable override: `DEADLINE_JOB_BUNDLE_DEFAULT_DIRECTORY`

This setting is also exposed in the Deadline Cloud settings dialog (Settings → General settings) as a "Job bundle directory" picker, alongside the existing "Job history directory" setting.

### CLI Integration

The `--browse` flag on `deadline bundle gui-submit` opens this new dialog instead of `QFileDialog.getExistingDirectory()`. No new flags needed. When `--browse` is used, the browser dialog opens before the submitter dialog. If the user cancels the browser, the command exits. Additionally, a "Load Bundle" button is added to the submitter dialog's button bar, allowing users to switch bundles mid-session by reopening the browser.

The "Load a different job bundle" button inside the submitter dialog (`JobBundleSettingsWidget.on_load_bundle`) also uses the new browser dialog, giving users the same browsing experience when switching bundles mid-session.

### Bundle Resolution Flow

After the user selects a bundle in the browser, it must be resolved to a local directory for the existing submission pipeline:

| Source | Format | Resolution | Cleanup |
|---|---|---|---|
| Local | Directory | Used directly (no copy) | None needed |
| Local | Archive | Extracted to temp dir | atexit cleanup |
| S3 | Directory (folder) | Metadata files only (`template.yaml`/`.json`, `parameter_values.yaml`/`.json`, `asset_references.yaml`/`.json`, `hooks.yaml`/`.json`) | atexit cleanup |
| S3 | Archive | Downloaded, cached with ETag, extracted to cache dir | Persists in cache |

The CLI `deadline bundle download` command uses a separate `download_full_bundle()` path that downloads all files (including scripts and data), with a 50 MB size limit for folder bundles. Bundles larger than this should be uploaded as archives instead.

Once resolved to a local directory, the standard submission flow takes over: `read_job_bundle_parameters()` parses the template and resolves relative PATH defaults against the bundle directory, `apply_job_parameters()` processes asset references, and the job is submitted normally.

Bundled assets (scripts, data files) with relative paths resolve correctly against the extracted/downloaded directory because the existing path resolution logic operates on the `bundle_dir` path regardless of its origin.

### Changes to Existing Code

| File | Change |
|---|---|
| `config/config_file.py` | Add `settings.job_bundle_default_directory` to `SETTINGS` |
| `cli/_groups/bundle_group.py` | Add `deadline bundle list`, `deadline bundle upload`, `deadline bundle download`, and `deadline bundle cache` (clean/update) commands |
| `ui/dialogs/job_bundle_browser_dialog.py` | **New file.** The browser dialog with filter, Local/S3/History sources. |
| `ui/dialogs/deadline_config_dialog.py` | Add "Job bundle directory" picker to the settings dialog |
| `ui/dialogs/submit_job_to_deadline_dialog.py` | Add "Share" button to upload the current bundle to S3 |
| `ui/widgets/job_bundle_settings_tab.py` | `on_load_bundle` opens the new browser dialog instead of `QFileDialog` |
| `ui/job_bundle_submitter.py` | `show_job_bundle_submitter` uses the new browser dialog when `browse=True`; handles archive extraction and S3 resolution |
| `job_bundle/loader.py` | Add `is_job_bundle_dir(path) -> bool` helper for quick detection |
| `job_bundle/repository.py` | **New file.** `BundleRepository` protocol, `LocalBundleRepository`, `S3BundleRepository`, archive helpers, cache management |

### CLI Commands

#### `deadline bundle list [path]`

Lists job bundles in a local directory or the queue's S3 `job-bundles/` folder.

- With no arguments, lists bundles in the configured default local directory (`settings.job_bundle_default_directory`, or home if not set). No AWS config needed.
- With `path`, lists bundles in that local directory.
- With `--s3`, lists bundles from the queue's S3 job-bundles folder (requires farm and queue).
- Default output is one bundle name per line, suitable for piping.
- `--output json`: JSON array with name, format (archive/folder), and path.

```
$ deadline bundle list
blender-render
maya-arnold

$ deadline bundle list ./my-bundles
simple-job

$ deadline bundle list --s3
blender-render
maya-arnold
monte_carlo_simulation

$ deadline bundle list --s3 --output json
[{"name": "blender-render", "path": "s3://bucket/prefix/job-bundles/blender-render.zip", "format": "archive"}, ...]

$ deadline bundle list | head -1 | xargs deadline bundle gui-submit --browse
```

The plain-text output enables chaining with other commands — e.g. selecting a bundle interactively with `fzf`:

```
$ deadline bundle submit $(deadline bundle download $(deadline bundle list | fzf) -o /tmp/bundles)
```

Use `jq` with JSON output to filter by format or extract paths:

```
$ deadline bundle list --output json | jq -r '.[] | select(.format == "archive") | .name'
blender-render
maya-arnold

$ deadline bundle list --output json | jq -r '.[0].path'
s3://my-farm-bucket/DeadlineCloud/job-bundles/blender-render.zip
```

#### `deadline bundle cache clean`

Removes cached S3 bundle archives from the local cache.

- With no arguments, removes all cached bundles.
- With a bundle name, removes only that bundle's cache.
- `--dry-run`: Show what would be removed without deleting.

```
$ deadline bundle cache clean
Removed 12 cached bundles (4.2 MB)

$ deadline bundle cache clean blender-render
Removed cached bundle: blender-render

$ deadline bundle cache clean --dry-run
Would remove 12 cached bundles (4.2 MB)
```

#### `deadline bundle cache update`

Re-downloads any stale cached bundles from S3 by checking ETags.

- With no arguments, checks all cached bundles.
- With a bundle name, checks only that bundle.
- Only re-downloads if the S3 ETag has changed.

```
$ deadline bundle cache update
Checked 12 bundles: 2 updated, 10 up-to-date

$ deadline bundle cache update blender-render
blender-render: up-to-date
```

#### `deadline bundle upload <job_bundle_dir>`

Uploads a local job bundle to the queue's S3 `job-bundles/` folder.

- **Default behavior**: Archives the bundle as a zip and uploads a single object (e.g. `blender-render.zip`).
- `--format tar.gz`: Use tar.gz instead of zip.
- `--no-archive`: Upload as loose files (folder-based bundle) instead of an archive.
- `--name`: Override the bundle name in S3 (defaults to the directory name).
- `--profile`, `--farm-id`, `--queue-id`: Standard config overrides.

```
$ deadline bundle upload ./my-render-job
Uploaded bundle to s3://my-farm-bucket/DeadlineCloud/job-bundles/my-render-job.zip

$ deadline bundle upload ./my-render-job --format tar.gz --name custom-name
Uploaded bundle to s3://my-farm-bucket/DeadlineCloud/job-bundles/custom-name.tar.gz

$ deadline bundle upload ./my-render-job --no-archive
Uploaded 5 files to s3://my-farm-bucket/DeadlineCloud/job-bundles/my-render-job/
```

#### `deadline bundle download <bundle_name>`

Downloads a job bundle from the queue's S3 `job-bundles/` folder.

- Looks for both archive and folder formats by name.
- Archive bundles use the ETag cache (same as the browser dialog) — repeated downloads are instant if the archive hasn't changed.
- `-o, --output-dir`: Local directory to extract/download to (defaults to `.`).
- `--profile`, `--farm-id`, `--queue-id`: Standard config overrides.

```
$ deadline bundle download blender-render
Downloaded bundle to: ./blender-render

$ deadline bundle download blender-render -o /tmp/bundles
Downloaded bundle to: /tmp/bundles/blender-render
```

### Error Handling

Errors are displayed inline rather than as popup dialogs:

- **S3 unavailable** (no farm/queue, no JA settings, auth failure): The S3 radio button shows `⚠ S3` and is disabled. Hovering shows the reason in a tooltip. The label distinguishes "not configured" (expected) from errors (⚠ icon).
- **Listing failure** (network error, permissions): The preview panel shows "⚠ Error" in red with the error message.
- **Expand failure** (subfolder listing fails): A disabled `⚠ Error: {message}` entry appears in the tree under that folder.
- **Preview failure** (malformed template, missing fields): The preview panel shows "⚠ Error" with "Could not read bundle template" and the tree entry icon changes from 📦 to ⚠.
- **Double-click**: Double-clicking a bundle selects it and accepts the dialog. Double-clicking a folder does nothing.

### Archive Safety

Archives are validated before extraction to prevent path traversal and symlink attacks:

- **Zip**: All entry paths are checked for absolute paths and `../` traversal before any extraction occurs. The entire archive is rejected if any entry is suspicious.
- **Tar**: Symlinks and hard links are rejected. Absolute paths and path traversal are checked. Python 3.12+ uses `filter="data"` for additional safety; older versions rely on the manual validation.

### S3 Considerations

- **Authentication**: S3 browsing and CLI commands use the same boto3 session/profile as the rest of deadline-cloud. No separate auth flow.
- **Permissions**: Requires `s3:ListBucket` and `s3:GetObject` on the queue's attachment bucket for browsing/download. Upload additionally requires `s3:PutObject`. If access is denied, show an error rather than crashing.
- **Performance**: Listing is 2 API calls (one delimited + one recursive `list_objects_v2`). Archive preview with S3 metadata is 1 `head_object` (no download). Folder preview is 1 `get_object` for the template. Cached archive selection is 1 `head_object`.
- **S3 object metadata**: `deadline bundle upload` attaches bundle name, description, steps, and parameters as S3 user metadata. This enables zero-download preview via `head_object`. Archives uploaded by other means fall back to downloading the archive for preview.
- **Folder bundle size limit**: Folder bundles larger than 50 MB cannot be downloaded via the CLI. Use `deadline bundle upload` to convert them to archives.
- **Bundled assets**: Scripts, data files, and other assets within the bundle are included in the archive or folder download. Relative PATH parameters resolve against the extracted/downloaded copy.

## Out of Scope (Future)

- Favoriting or pinning frequently used bundles.
- Browsing bundles from a Deadline Cloud service API (e.g. farm-level bundle registry).
- Configurable S3 bucket/prefix (currently always derived from the queue).
- Cache size limits or TTL-based eviction.

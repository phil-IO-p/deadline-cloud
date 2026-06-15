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
4. Supports job bundles as directories or `.ojd` archives (zip format under the hood).
5. For S3, uses the selected queue's job attachment bucket with a `job-bundles/` prefix — no extra configuration needed.
6. Caches S3 archive bundles locally with ETag validation for fast repeated access.
7. Respects a configurable default local browse directory.

## Design

### Bundle Formats

Job bundles can be either:

- **Directories** — a folder containing `template.yaml` or `template.json` at the root, plus any scripts, data files, and `asset_references.yaml`.
- **Archives** — an `.ojd` file (zip format under the hood) containing a job bundle. The template can be at the archive root or inside a single wrapper directory.

Both formats are supported for local browsing. S3 browsing only supports `.ojd` archives — this is the canonical sharing format. Archives are extracted to a local directory before submission. If an archive contains a single top-level wrapper directory (e.g. `my-bundle/template.yaml` instead of `template.yaml` at the root), the wrapper is detected and the inner directory is used as the bundle path.

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
- `S3BundleRepository` — lists objects and prefixes under the queue's job attachment bucket at `{rootPrefix}/job-bundles/`. Only `.ojd` archives are recognized as bundles; subfolders are shown for navigation only. Provides `resolve_bundle()` which handles archive download+cache+extract. The `from_config()` classmethod encapsulates all initialization logic (session creation, queue lookup, settings extraction) to avoid duplicating this across callers.

### S3 Bucket Convention

The S3 bundle repository browses:

```
s3://{s3BucketName}/{rootPrefix}/job-bundles/
```

Where `s3BucketName` and `rootPrefix` come from the selected queue's `jobAttachmentSettings`. This means:

- No extra configuration is needed — the bucket is derived from the queue the user already has selected.
- Users (or admins) place job bundles as `.ojd` archives in the `job-bundles/` folder within the queue's attachment bucket.
- Subfolders within `job-bundles/` are supported for organization but only `.ojd` files are recognized as bundles.

Example S3 layout:
```
s3://my-farm-bucket/DeadlineCloud/job-bundles/
    blender-render.ojd
    maya-arnold.ojd
    rendering/
        custom-renderer.ojd
```

### Job History Source

The Job History source browses the job history directory for the current AWS profile, as configured by `settings.job_history_dir` (default: `~/.deadline/job_history/{aws_profile_name}`). This directory contains bundles from previous submissions, organized by date.

This is useful for:
- Re-submitting a previous job with modified parameters.
- Using a previously submitted bundle as a starting point for a new submission.
- Reviewing what was submitted in the past.

The Job History source uses the same `LocalBundleRepository` as the Local source, just rooted at the job history directory instead of the user's home or configured default.

### Archive Caching

Archive bundles (both local `.ojd` files and S3 archives) are cached in a unified location to avoid redundant extraction and downloads.

**Cache location**: `~/.deadline/cache/job-bundles/{hash}/{bundle-name}/`

Where `{hash}` is a truncated SHA-256 of the source identifier:
- **S3 archives**: `hash(bucket/s3-key)`
- **Local archives**: `hash(local-file-path)`

**Cache validation**:
- **S3**: A `head_object` call retrieves the archive's ETag. If it matches the cached ETag, the local copy is used. If it differs (or no cache exists), the archive is re-downloaded and re-extracted.
- **Local**: The file's mtime is compared to the cached mtime. If it differs, the archive is re-extracted in-place.

**Cache metadata** (`.bundle_cache_meta.json`):
```json
{
  "etag": "\"d41d8cd98f00b204e9800998ecf8427e\"",
  "last_modified": "2026-04-30T12:00:00+00:00"
}
```

For S3 archives, `etag` is used for validation. For local archives, `mtime` (float, seconds since epoch) is stored instead. The fields present indicate the source type.

**Cleanup**: `deadline bundle cache clean` removes cached bundles from this directory. There is no separate temp dir or `atexit` cleanup — all extracted archives live in the cache.

### S3 Object Metadata for Preview

When `deadline bundle upload` uploads an archive, it attaches bundle metadata as S3 user metadata on the object:

- `ojd-name`: The template's `name` field (limit: 256 chars)
- `ojd-desc`: The template's `description` field, newlines collapsed to spaces (limit: 480 chars)
- `ojd-steps`: Comma-separated list of step names (limit: 480 chars)
- `ojd-params`: Comma-separated `name:type` pairs (limit: 700 chars)

These limits are defined as constants in `repository.py` (`METADATA_LIMIT_NAME`, `METADATA_LIMIT_DESC`, `METADATA_LIMIT_STEPS`, `METADATA_LIMIT_PARAMS`). S3 user-defined metadata is limited to 2 KB total (sum of all UTF-8 encoded keys and values, including the `x-amz-meta-` prefix). The per-field limits are chosen to stay within this budget even at maximum usage. See: https://docs.aws.amazon.com/AmazonS3/latest/userguide/UsingMetadata.html#UserMetadata

When truncation occurs, the CLI emits a yellow warning (e.g. `Warning: Bundle metadata 'ojd-params' truncated from 899 to 700 characters`) and the truncated value ends with `...` to make it visually obvious in the preview that information was cut off. The parameters table in the browser dialog detects truncated metadata and shows an "… additional parameters not shown" row.

This metadata is returned by `head_object`, which is already called for ETag validation. This means preview of uploaded archives requires **zero downloads** — a single `head_object` provides both cache validation and all preview information.

### Detection: What Is a Job Bundle?

- **Directories** (local or S3 prefix): contains `template.yaml` or `template.json`.
- **Archives** (local file or S3 object): filename ends with `.ojd`. Validated by reading the template from inside the archive on preview.

For `list_entries`, detection is kept fast:

- **Local directories**: stat check for template file existence (no parsing).
- **Local archives**: matched by `.ojd` extension, then validated by checking for a template inside the archive. This prevents random files from appearing as bundles. Archive scanning can be disabled via `include_archives=False` on `LocalBundleRepository` (used by the browser for Local/History sources, and via `--no-archives` in the CLI). The browser dialog disables local archives because the primary use case for archives is S3-shared bundles — local users work with directory bundles directly.
- **S3 folders**: shown for navigation only (expandable in the tree), never treated as bundles.
- **S3 archives**: matched by `.ojd` extension only (no API call).

Full template parsing happens only in `get_bundle_info` when the user clicks a bundle for preview.

### Browser Dialog UI

```
┌─────────────────────────────────────────────────────────────┐
│  Job Bundle Browser                                         │
├─────────────────────────────────────────────────────────────┤
│  Source: (•) Queue  ( ) Local  ( ) History                  │
│  ☐ Show hidden                                              │
├────────────────────────────────┬────────────────────────────┤
│  [Filter bundles...         ]  │  Name: Blender Render      │
│  📁 my-bundles/               │  Description: Renders a    │
│    📦 blender-render          │  Blender scene file...     │
│    📦 maya-arnold             │                            │
│    📁 wip/                    │  Steps:                    │
│      📦 experimental-job      │    • RenderBlender         │
│    📦 simple-job/             │                            │
│                               │  Parameters:               │
│                               │  ┌──────────┬──────┬─────┐ │
│                               │  │ Name     │ Type │ Val │ │
│                               │  ├──────────┼──────┼─────┤ │
│                               │  │ Frames   │ STR  │     │ │
│                               │  │ OutputDir│ PATH │     │ │
│                               │  └──────────┴──────┴─────┘ │
│                               │                            │
├────────────────────────────────┴────────────────────────────┤
│  Path:  [/job-bundles/                      ]               │
│                                          [Cancel] [Select]  │
└─────────────────────────────────────────────────────────────┘
```

**Top bar** — Source selection and options:
- Radio toggle between Queue, Local, and History sources. Queue is selected by default when available; otherwise Local is selected. Queue option is disabled if the queue has no job attachment settings or access fails. When Queue is unavailable, an inline warning label appears below the radio buttons explaining why (e.g. "⚠ **Queue browsing unavailable:** AccessDeniedException...").
- "Show hidden" checkbox — unchecked by default, toggling refreshes the tree to include/exclude hidden items. For Local/History sources, this means dot-prefixed directories. For the Queue source, this means bundles marked as hidden via the visibility manifest (see [Bundle Visibility](#bundle-visibility)).

**Left panel** — Filter and navigable tree view:
- A text filter at the top that narrows the tree as you type. Case-insensitive, matches against entry names. Uses recursive filtering so parent folders remain visible when a child matches. The tree auto-expands when filtering to show results.
- Shows folders (📁) and job bundles (📦) with distinct icons. Both directory bundles and archive bundles use the 📦 icon.
- Clicking a folder clears any active filter, expands the folder to show its children, and scrolls it to the top of the view. This makes the search-then-navigate flow natural: search for a folder, click it, see its contents.
- Job bundles are leaf nodes (selectable, not expandable).
- Non-bundle, non-archive files are hidden.
- Hidden folders (names starting with `.`) and hidden S3 bundles are hidden by default; toggled via the "Show hidden" checkbox.
- **Context menu** (Queue source only): Right-clicking a visible bundle shows "Hide bundle"; right-clicking a hidden bundle (when "Show hidden" is checked) shows "Unhide bundle". Hidden bundles are rendered with a dimmed/grayed icon to distinguish them from visible ones. Hide/unhide operations happen in the background with automatic retry on conflict (see [Bundle Visibility](#bundle-visibility)).

**Right panel** — Preview (shown when a bundle is selected, scrollable):
- **Name**: From the template's `name` field, shown as-is (with `{{Param.X}}` references unresolved).
- **Description**: From the template's `description` field, if present.
- **Steps**: List of step names from the template, in definition order.
- **Parameters**: Rendered as a table with Name, Type, and Value columns. Columns resize to fit content, with the last column stretching. If parameters were truncated in S3 metadata, the last garbled entry is dropped and a gray "… additional parameters not shown" row is appended.

**Bottom bar**:
- Path display showing the current browse location.
- Cancel and Select buttons. Select is enabled only when a valid bundle is highlighted.

### Export Bundle

The submitter dialog's "Export bundle" button replaces the previous separate "Export" and "Share" buttons with a unified flow. Clicking it opens an export dialog:

```
┌─ Export Bundle ─────────────────────────────┐
│                                             │
│  Name: [blender-render_________]            │
│                                             │
│  Save to:                                   │
│  (•) Queue    ( ) Local                     │
│                                             │
│  ⚠ Queue unavailable: AccessDenied...       │
│  (inline warning, shown only when Queue     │
│   is disabled)                              │
│                                             │
│  Location: [s3://bucket/DC/job-bundles/]    │
│  (read-only for Queue, editable for Local)  │
│                                             │
│                     [Cancel] [Export]        │
└─────────────────────────────────────────────┘
```

**Name** — defaults to the job name with `{{Param.X}}` references resolved using current parameter values. Editable. Used as the `.ojd` filename for Queue or the directory name for Local.

**Save to** — Queue or Local:
- **Queue**: archives the bundle as `.ojd` and uploads to the queue's S3 `job-bundles/` folder. S3 user metadata (name, description, steps, parameters) is attached for zero-download preview. If a bundle with the same name already exists, the user is prompted to confirm overwrite. If the existing bundle is hidden, the prompt warns "A hidden bundle with this name already exists" with three options: Cancel, Overwrite (keeps it hidden), or Overwrite and unhide. When Queue is unavailable (no permissions, no farm/queue configured, no job attachment settings), the radio button is disabled and an inline warning label explains why.
- **Local**: saves the bundle as a directory to the specified location. Defaults to `settings.job_bundle_default_directory` — the same path the browser's Local source browses. The exported bundle immediately appears when browsing Local.

**Location** — always visible, updates based on the selected source:
- **Queue selected**: shows the S3 path (e.g. `s3://bucket/DeadlineCloud/job-bundles/`), read-only.
- **Local selected**: shows the local directory path, editable with a folder picker button for override.

Queue export is enabled when the API is available and a farm and queue are configured. Local export is always available.

Note: the job history directory (`settings.job_history_dir`) is still used internally during Submit to record what was submitted, but Export now targets user-visible locations (Local browse path or Queue) rather than the history directory.

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
| Local | Archive (.ojd) | Extracted to cache dir (`hash(path)` + mtime validation) | `deadline bundle cache clean` |
| S3 | Archive (.ojd) | Downloaded to cache dir (`hash(bucket/key)` + ETag validation), extracted | `deadline bundle cache clean` |

The CLI `deadline bundle download` command downloads the `.ojd` archive, caches it locally with ETag validation, and extracts it to the output directory.

Once resolved to a local directory, the standard submission flow takes over: `read_job_bundle_parameters()` parses the template and resolves relative PATH defaults against the bundle directory, `apply_job_parameters()` processes asset references, and the job is submitted normally.

Bundled assets (scripts, data files) with relative paths resolve correctly against the extracted/downloaded directory because the existing path resolution logic operates on the `bundle_dir` path regardless of its origin.

### Changes to Existing Code

| File | Change |
|---|---|
| `config/config_file.py` | Add `settings.job_bundle_default_directory` to `SETTINGS` |
| `cli/_groups/bundle_group.py` | Add `deadline bundle list`, `deadline bundle upload`, `deadline bundle download`, `deadline bundle info`, `deadline bundle hide`, `deadline bundle unhide`, and `deadline bundle cache` (clean/update) commands |
| `ui/dialogs/job_bundle_browser_dialog.py` | **New file.** The browser dialog with filter, Queue/Local/History sources, "Show hidden" toggle, parameter table preview, and right-click context menu for hide/unhide (Queue source). Constructor takes keyword-only args: `queue_source`, `queue_error`, `local_source`, `history_source`. |
| `ui/dialogs/deadline_config_dialog.py` | Add "Job bundle directory" picker to the settings dialog |
| `ui/dialogs/submit_job_to_deadline_dialog.py` | Replace "Export" and "Share" buttons with unified "Export bundle" button that opens the export dialog |
| `ui/dialogs/export_bundle_dialog.py` | **New file.** Export dialog with Queue/Local destination, name override, and location display |
| `ui/widgets/job_bundle_settings_tab.py` | `on_load_bundle` opens the new browser dialog instead of `QFileDialog` |
| `ui/job_bundle_submitter.py` | `show_job_bundle_submitter` uses the new browser dialog when `browse=True`; handles archive extraction and S3 resolution |
| `job_bundle/loader.py` | Add `is_job_bundle_dir(path) -> bool` helper for quick detection |
| `job_bundle/repository.py` | **New file.** `BundleRepository` protocol, `LocalBundleRepository`, `S3BundleRepository` (with `from_config()` factory), archive helpers, cache management, metadata constants, visibility manifest read/write with optimistic concurrency |

### CLI Commands

#### `deadline bundle list [path]`

Lists job bundles in a local directory or the queue's S3 `job-bundles/` folder.

- With no arguments, lists bundles in the configured default local directory (`settings.job_bundle_default_directory`, or home if not set). No AWS config needed.
- With `path`, lists bundles in that local directory.
- With `--queue`, lists bundles shared on the queue (requires farm and queue). Hidden bundles are excluded by default.
- With `--queue --show-hidden`, includes hidden bundles in the output (marked with `(hidden)` in plain text, `"hidden": true` in JSON).
- Default output is one bundle name per line, suitable for piping.
- `--output json`: JSON array with name, format (archive/folder), and path.

```
$ deadline bundle list
blender-render
maya-arnold

$ deadline bundle list ./my-bundles
simple-job

$ deadline bundle list --queue
blender-render
maya-arnold
monte_carlo_simulation

$ deadline bundle list --queue --show-hidden
blender-render
maya-arnold
monte_carlo_simulation
old-maya-job (hidden)

$ deadline bundle list --queue --output json
[{"name": "blender-render", "path": "s3://bucket/prefix/job-bundles/blender-render.ojd", "format": "archive"}, ...]

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
s3://my-farm-bucket/DeadlineCloud/job-bundles/blender-render.ojd
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

Uploads a local job bundle to share on the queue as an `.ojd` archive.

- `--name`: Override the bundle name (defaults to the directory name).
- `--profile`, `--farm-id`, `--queue-id`: Standard config overrides.
- If a bundle with the same name already exists, prompts for confirmation before overwriting.
- Symlinks within the bundle directory are skipped (not followed) to prevent unintended file disclosure.

```
$ deadline bundle upload ./my-render-job
Uploaded bundle to s3://my-farm-bucket/DeadlineCloud/job-bundles/my-render-job.ojd

$ deadline bundle upload ./my-render-job --name custom-name
Uploaded bundle to s3://my-farm-bucket/DeadlineCloud/job-bundles/custom-name.ojd

$ deadline bundle upload ./my-render-job
Bundle 'my-render-job' already exists on the queue. Overwrite? [y/N]: y
Uploaded bundle to s3://my-farm-bucket/DeadlineCloud/job-bundles/my-render-job.ojd
```

#### `deadline bundle download <bundle_name>`

Downloads a shared job bundle from the queue.

- Finds the `.ojd` archive matching the given name.
- Uses the ETag cache (same as the browser dialog) — repeated downloads are instant if the archive hasn't changed.
- Copies the resolved bundle to the output directory (cache is used internally but the user gets a clean copy at their requested location).
- `-o, --output-dir`: Local directory to download to (defaults to `.`).
- `--profile`, `--farm-id`, `--queue-id`: Standard config overrides.

```
$ deadline bundle download blender-render
Downloaded bundle to: ./blender-render

$ deadline bundle download blender-render -o /tmp/bundles
Downloaded bundle to: /tmp/bundles/blender-render
```

#### `deadline bundle hide <bundle_name>`

Hides a shared bundle on the queue. The bundle remains in S3 but is no longer shown in the browser or `deadline bundle list` by default.

- Updates the `.bundle-visibility.json` manifest using conditional writes (automatic retry on conflict).
- `--profile`, `--farm-id`, `--queue-id`: Standard config overrides.
- No-op if the bundle is already hidden.

```
$ deadline bundle hide blender-render
Hidden bundle: blender-render

$ deadline bundle hide blender-render
Bundle already hidden: blender-render
```

#### `deadline bundle unhide <bundle_name>`

Unhides a previously hidden bundle, making it visible again in the browser and `deadline bundle list`.

- Updates the `.bundle-visibility.json` manifest using conditional writes (automatic retry on conflict).
- `--profile`, `--farm-id`, `--queue-id`: Standard config overrides.
- No-op if the bundle is not hidden.

```
$ deadline bundle unhide blender-render
Unhidden bundle: blender-render

$ deadline bundle unhide blender-render
Bundle is not hidden: blender-render
```

#### `deadline bundle info <bundle_name>`

Shows detailed information about a job bundle — the same data shown in the browser's preview panel.

- Without `--queue`, `bundle_name` is treated as a local path to a job bundle directory. If the path doesn't exist, searches by name in the current directory and then the configured `settings.job_bundle_default_directory`.
- With `--queue`, looks up the named bundle on the queue (uses S3 metadata for zero-download preview when available).
- Output always includes the resolved path so the user knows where the bundle was found.
- `--output json`: JSON object with path, name, description, steps, and parameters.
- `--profile`, `--farm-id`, `--queue-id`: Standard config overrides (with `--queue`).

```
$ deadline bundle info ./my-render-job
Path: /home/user/my-render-job
Name: Blender Render
Description: Renders a Blender scene file using Cycles
Steps:
  • RenderBlender
Parameters:
  Frames (STRING) = 1-100
  OutputDir (PATH) = /tmp/output

$ deadline bundle info blender-render --queue
Path: s3://my-farm-bucket/DeadlineCloud/job-bundles/blender-render.ojd
Name: Blender Render
Description: Renders a Blender scene file using Cycles
Steps:
  • RenderBlender
Parameters:
  Frames (STRING) = 1-100
  OutputDir (PATH) = /tmp/output

$ deadline bundle info blender-render --queue --output json
{
  "path": "s3://my-farm-bucket/DeadlineCloud/job-bundles/blender-render.ojd",
  "name": "Blender Render",
  "description": "Renders a Blender scene file using Cycles",
  "steps": ["RenderBlender"],
  "parameters": [
    {"name": "Frames", "type": "STRING", "_display_value": "1-100"},
    {"name": "OutputDir", "type": "PATH", "_display_value": "/tmp/output"}
  ]
}
```

### Bundle Visibility

Bundles shared on S3 can be "hidden" without deleting them. This is the S3 equivalent of dot-prefixed hidden folders on local filesystems — bundles remain accessible but are not shown in the browser by default.

**Mechanism — sidecar manifest with optimistic concurrency:**

A single JSON file stores the list of hidden bundle names:

```
s3://{bucket}/{rootPrefix}/job-bundles/.bundle-visibility.json
```

```json
{
  "version": 1,
  "hidden": ["blender-render", "old-maya-job"]
}
```

The `version` field allows evolving the format in the future (e.g. adding per-entry comments or timestamps) without breaking older clients. The `hidden` array contains bundle names (without the `.ojd` extension) — these correspond 1:1 with object keys since duplicate names are not allowed in the same folder. In Python, the array is deserialized into a `set` for O(1) lookup, and serialized back as a sorted list for stable output.

**Scope:** A single manifest exists at the `job-bundles/` root and covers all bundles including those in subfolders (stored as relative paths, e.g. `"rendering/custom-renderer"`). This avoids additional S3 calls to fetch per-subfolder manifests.

**Re-upload behavior:** If a hidden bundle is re-uploaded (overwritten), it remains hidden. The hide is "sticky" to the name. This is intentional — hiding expresses "this name shouldn't clutter the default view" regardless of the object's content. The export dialog handles this explicitly (see [Export Bundle](#export-bundle)).

**Pruning:** When the manifest is read during listing, any entries that don't match an existing bundle are silently removed. The pruned manifest is written back (using the same conditional write) only if entries were actually removed. This prevents unbounded growth from bundles that were deleted directly via S3.

**Sync behavior:** The manifest is fetched fresh (single `GetObject`) alongside `list_objects_v2` every time the Queue source is loaded or refreshed. There is no persistent local copy or background polling. If a teammate hides a bundle, the change is visible next time the browser loads the listing.

**Why a sidecar manifest?** `list_objects_v2` does not return per-object user metadata or tags. A per-object approach would require a `head_object` call for every bundle during listing. The sidecar is fetched with a single `GetObject` during listing, making hidden-bundle filtering zero-cost per bundle.

**Concurrency control:** Updates use S3 conditional writes (`If-Match` on ETag) to prevent lost updates when multiple users hide/unhide simultaneously:

1. `GetObject` on `.bundle-visibility.json` → get contents + ETag (or handle `NoSuchKey` for first use).
2. Modify the hidden set locally.
3. `PutObject` with `If-Match: <etag>` (or `If-None-Match: *` for creation).
4. On `412 Precondition Failed`, retry from step 1 (up to 3 attempts).

Retries are transparent to the user — the operation either succeeds silently or shows an inline error after exhausting retries.

**Browser behavior:**

- When "Show hidden" is unchecked (default), bundles in the hidden list are excluded from the tree.
- When "Show hidden" is checked, hidden bundles appear with a dimmed/grayed 📦 icon.
- Right-click context menu on bundles (Queue source only):
  - Visible bundle → "Hide bundle"
  - Hidden bundle → "Unhide bundle"
- After hide/unhide, the tree refreshes to reflect the change.

**Permissions:** Hiding/unhiding requires `s3:GetObject` and `s3:PutObject` on the `.bundle-visibility.json` key. Users with read-only access can still browse (the manifest is read during listing) but cannot hide/unhide.

### Progress Indication

Operations that involve network I/O show progress to the user:

- **Browser dialog**: When selecting an S3 bundle and clicking "Select", a progress spinner replaces the Select button label while the archive is downloaded/resolved. The dialog remains responsive (download happens on a background thread).
- **Export dialog**: When uploading to Queue, a progress bar appears below the Export button showing upload progress. Cancel is available during upload.
- **CLI**: `deadline bundle upload` and `deadline bundle download` show a progress bar (using the same style as job attachment uploads). `deadline bundle hide`/`unhide` complete fast enough to not need progress.

### Error Handling

Errors are displayed inline rather than as popup dialogs:

- **Queue unavailable** (no farm/queue, no JA settings, auth failure): The Queue radio button is disabled and a styled inline warning label appears below the source selector showing the reason (e.g. "⚠ **Queue browsing unavailable:** AccessDeniedException...").
- **Listing failure** (network error, permissions): The preview panel shows "⚠ Error" in red with the error message.
- **Expand failure** (subfolder listing fails): A disabled `⚠ Error: {message}` entry appears in the tree under that folder.
- **Preview failure** (malformed template, missing fields): The preview panel shows "⚠ Error" with "Could not read bundle template" and the tree entry icon changes from 📦 to ⚠.
- **Double-click**: Double-clicking a bundle selects it and accepts the dialog. Double-clicking a folder does nothing.
- **Hide/unhide failure** (insufficient permissions or conflict): The context menu action is always shown. If the operation fails (e.g. `AccessDeniedException`), an inline warning appears below the tree: "⚠ Could not hide bundle: AccessDeniedException".

### Archive Safety

Archives are validated before extraction to prevent path traversal attacks:

- All entry paths are checked for absolute paths and `../` traversal using `os.path.commonpath()` with `os.path.realpath()` — this handles mixed path separators on Windows. The entire archive is rejected if any entry would extract outside the target directory.

Symlink protection during upload:

- `os.walk(followlinks=False)` is used when archiving bundles. Symlinked files and directories are skipped to prevent unintended inclusion of files outside the bundle directory.

### Bundle Name Validation

Upload rejects bundles with invalid names:

- Empty names or names consisting only of whitespace/slashes are rejected with an error directing the user to `--name`.
- The full S3 key (prefix + name + `.ojd`) is validated against S3's 1024-character key limit.
- Control characters (0x00–0x1F, 0x7F) are considered invalid.

On download, the bundle name is sanitized for the local filesystem in a platform-specific manner:

- **POSIX** (macOS/Linux): only `/` and null bytes are replaced with `_`. Characters like `:`, `*`, `?` are preserved since they are valid filenames.
- **Windows**: `\ / : * ? " < > |` and control characters are replaced with `_`.

This means the S3 key preserves the original name as-is (all characters are valid in S3 keys), and only the local directory name is adjusted for the user's OS.

### S3 Considerations

- **Authentication**: S3 browsing and CLI commands use `api.get_boto3_session()` which respects the configured AWS profile in `~/.deadline/config`. The `S3BundleRepository.from_config()` factory method encapsulates session creation, queue lookup, and settings extraction in one place. No separate auth flow.
- **Permissions**: Requires `s3:ListBucket` and `s3:GetObject` on the queue's attachment bucket for browsing/download. Upload additionally requires `s3:PutObject`. Hiding/unhiding bundles requires `s3:GetObject` and `s3:PutObject` on the `.bundle-visibility.json` key. If access is denied, show an error rather than crashing.
- **Performance**: Listing is a single paginated `list_objects_v2` call with delimiter. Archive preview with S3 metadata is 1 `head_object` (no download). Cached archive selection is 1 `head_object`.
- **S3 object metadata**: `deadline bundle upload` attaches bundle name, description, steps, and parameters as S3 user metadata. This enables zero-download preview via `head_object`. Archives uploaded by other means fall back to downloading the archive for preview.
- **Bundled assets**: Scripts, data files, and other assets within the bundle are included in the archive. Relative PATH parameters resolve against the extracted copy.

### MCP Server Integration

The MCP server exposes bundle sharing operations as tools for AI assistants:

- **list_shared_bundles** — Lists bundles on the queue (respects visibility, supports `show_hidden`).
- **upload_bundle** — Uploads a local job bundle to the queue as an `.ojd` archive.
- **download_bundle** — Downloads a shared bundle from the queue to a local directory.

These tools use the same `S3BundleRepository` as the CLI and GUI, so behavior is consistent. Hide/unhide is not exposed via MCP — it's a management action better suited to direct user intent via CLI or GUI.

## Out of Scope (Future)

- Favoriting or pinning frequently used bundles.
- Browsing bundles from a Deadline Cloud service API (e.g. farm-level bundle registry).
- Configurable S3 bucket/prefix (currently always derived from the queue).
- Cache size limits or TTL-based eviction.

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

Both formats are supported for both local and S3 browsing. Archives are extracted to a local directory before submission.

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

S3 user metadata has a 2KB total limit, which is sufficient for typical bundle metadata. Values are truncated to stay within limits.

### Detection: What Is a Job Bundle?

- **Directories** (local or S3 prefix): contains `template.yaml` or `template.json`.
- **Archives** (local file or S3 object): filename ends with a supported archive extension. Validated by reading the template from inside the archive on preview.

For `list_entries`, detection is kept fast:

- **Local directories**: stat check for template file existence (no parsing).
- **Local archives**: matched by file extension only.
- **S3 folders**: detected via batch recursive listing — a single `list_objects_v2` (without delimiter) returns all keys under the parent prefix, and we check in-memory which child prefixes contain a template file. This replaces per-folder `head_object` calls, reducing N+1 API calls to 2 (one delimited list + one recursive list).
- **S3 archives**: matched by key extension only (no API call).

Full template parsing happens only in `get_bundle_info` when the user clicks a bundle for preview.

### Browser Dialog UI

```
┌─────────────────────────────────────────────────────────────┐
│  Job Bundle Browser                                         │
├────────────────────────────────┬────────────────────────────┤
│  📁 my-bundles/               │  Name: Blender Render      │
│    📦 blender-render          │  Description: Renders a    │
│    📦 maya-arnold             │  Blender scene file...     │
│    📁 wip/                    │                            │
│      📦 experimental-job      │  Steps:                    │
│    📦 simple-job/             │    • RenderBlender         │
│                               │                            │
│                               │  Parameters:               │
│                               │    • BlenderSceneFile (PATH)│
│                               │    • Frames (STRING)       │
│                               │    • OutputDir (PATH)      │
│                               │    • Format (STRING)       │
│                               │                            │
├────────────────────────────────┴────────────────────────────┤
│  Source: ( ) Local  (•) S3 (my-farm-bucket)                 │
│  Path:  [/job-bundles/                      ]               │
│                                          [Cancel] [Select]  │
└─────────────────────────────────────────────────────────────┘
```

**Left panel** — Navigable tree view:
- Shows folders (📁) and job bundles (📦) with distinct icons. Both directory bundles and archive bundles use the 📦 icon.
- Folders can be expanded/navigated into.
- Job bundles are leaf nodes (selectable, not expandable).
- Non-bundle, non-archive files are hidden.

**Right panel** — Preview (shown when a bundle is selected):
- **Name**: From the template's `name` field.
- **Description**: From the template's `description` field, if present.
- **Steps**: List of step names from the template.
- **Parameters**: Name and type of each parameter definition.

**Bottom bar**:
- Radio toggle between Local and S3 source. S3 option shows the bucket name from the queue. S3 option is disabled if the queue has no job attachment settings.
- Path display showing the current browse location.
- Cancel and Select buttons. Select is enabled only when a valid bundle is highlighted.

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

### CLI Integration

The `--browse` flag on `deadline bundle gui-submit` opens this new dialog instead of `QFileDialog.getExistingDirectory()`. No new flags needed.

The "Load a different job bundle" button inside the submitter dialog (`JobBundleSettingsWidget.on_load_bundle`) also uses the new browser dialog, giving users the same browsing experience when switching bundles mid-session.

### Bundle Resolution Flow

After the user selects a bundle in the browser, it must be resolved to a local directory for the existing submission pipeline:

| Source | Format | Resolution | Cleanup |
|---|---|---|---|
| Local | Directory | Used directly (no copy) | None needed |
| Local | Archive | Extracted to temp dir | atexit cleanup |
| S3 | Directory (folder) | Metadata files only (template, parameters, asset_references, hooks) | atexit cleanup |
| S3 | Archive | Downloaded, cached with ETag, extracted to cache dir | Persists in cache |

The CLI `deadline bundle download` command uses a separate `download_full_bundle()` path that downloads all files (including scripts and data), with a 50 MB size limit for folder bundles. Bundles larger than this should be uploaded as archives instead.

Once resolved to a local directory, the standard submission flow takes over: `read_job_bundle_parameters()` parses the template and resolves relative PATH defaults against the bundle directory, `apply_job_parameters()` processes asset references, and the job is submitted normally.

Bundled assets (scripts, data files) with relative paths resolve correctly against the extracted/downloaded directory because the existing path resolution logic operates on the `bundle_dir` path regardless of its origin.

### Changes to Existing Code

| File | Change |
|---|---|
| `config/config_file.py` | Add `settings.job_bundle_default_directory` to `SETTINGS` |
| `cli/_groups/bundle_group.py` | Add `deadline bundle upload` and `deadline bundle download` commands |
| `ui/dialogs/job_bundle_browser_dialog.py` | **New file.** The browser dialog. |
| `ui/widgets/job_bundle_settings_tab.py` | `on_load_bundle` opens the new browser dialog instead of `QFileDialog` |
| `ui/job_bundle_submitter.py` | `show_job_bundle_submitter` uses the new browser dialog when `browse=True`; handles archive extraction and S3 resolution |
| `job_bundle/loader.py` | Add `is_job_bundle_dir(path) -> bool` helper for quick detection |
| `job_bundle/repository.py` | **New file.** `BundleRepository` protocol, `LocalBundleRepository`, `S3BundleRepository`, archive helpers, cache management |

### CLI Commands

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

### S3 Considerations

- **Authentication**: S3 browsing and CLI commands use the same boto3 session/profile as the rest of deadline-cloud. No separate auth flow.
- **Permissions**: Requires `s3:ListBucket` and `s3:GetObject` on the queue's attachment bucket for browsing/download. Upload additionally requires `s3:PutObject`. If access is denied, show an error rather than crashing.
- **Performance**: Listing is 2 API calls (one delimited + one recursive `list_objects_v2`). Archive preview with S3 metadata is 1 `head_object` (no download). Folder preview is 1 `get_object` for the template. Cached archive selection is 1 `head_object`.
- **S3 object metadata**: `deadline bundle upload` attaches bundle name, description, steps, and parameters as S3 user metadata. This enables zero-download preview via `head_object`. Archives uploaded by other means fall back to downloading the archive for preview.
- **Folder bundle size limit**: Folder bundles larger than 50 MB cannot be downloaded via the CLI. Use `deadline bundle upload` to convert them to archives.
- **Bundled assets**: Scripts, data files, and other assets within the bundle are included in the archive or folder download. Relative PATH parameters resolve against the extracted/downloaded copy.

## Out of Scope (Future)

- Search/filter within the browser.
- Favoriting or pinning frequently used bundles.
- Browsing bundles from a Deadline Cloud service API (e.g. farm-level bundle registry).
- Configurable S3 bucket/prefix (currently always derived from the queue).
- Cache size limits or TTL-based eviction.

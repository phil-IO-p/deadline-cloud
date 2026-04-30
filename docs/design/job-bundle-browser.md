# Job Bundle Browser

## Problem

When using `deadline bundle gui-submit --browse` or the "Load a different job bundle" button, users are presented with a native OS folder picker. This is inadequate because:

- Job bundles are not distinguishable from regular folders by name alone.
- Users must already know where their bundles live and navigate there manually.
- There is no preview of what a bundle contains — users pick blindly.
- The picker always starts at the job history directory or home, with no way to configure a default location.

## Overview

Replace the native folder picker with a custom job bundle browser dialog that:

1. Provides a navigable directory tree showing only folders and job bundles.
2. Displays a preview panel with bundle metadata when a job bundle is selected.
3. Supports both local filesystem and S3 bucket browsing through a common backend abstraction.
4. For S3, uses the selected queue's job attachment bucket with a `job-bundles/` prefix — no extra configuration needed.
5. Respects a configurable default local browse directory.

## Design

### Backend Abstraction

To support both local and S3 browsing without coupling the UI to either, introduce a `BundleRepository` protocol:

```python
@dataclass
class BundleInfo:
    """Metadata extracted from a job bundle's template."""
    path: str              # Local path or s3:// URI
    name: str              # From template "name" field
    description: str       # From template "description" field, or ""
    step_names: list[str]  # Names of each step in the template
    parameters: list[dict] # Parameter definitions from the template

@dataclass
class BrowseEntry:
    """A single item in the browser listing."""
    name: str              # Display name (folder basename or bundle name)
    path: str              # Full path or S3 URI
    is_bundle: bool        # True if this is a valid job bundle

class BundleRepository(Protocol):
    def list_entries(self, path: str) -> list[BrowseEntry]:
        """List immediate children of `path`. Returns folders and bundles."""
        ...

    def get_bundle_info(self, path: str) -> Optional[BundleInfo]:
        """Load and return metadata for the bundle at `path`, or None if invalid."""
        ...

    def root_path(self) -> str:
        """The starting path for browsing."""
        ...
```

Two implementations:

- `LocalBundleRepository` — walks the local filesystem. A directory is a bundle if it contains `template.yaml` or `template.json`. Uses the existing `read_yaml_or_json_object` loader.
- `S3BundleRepository` — lists objects under the queue's job attachment bucket at `{rootPrefix}/job-bundles/`. Constructed from the queue's `JobAttachmentS3Settings`.

### S3 Bucket Convention

The S3 bundle repository browses:

```
s3://{s3BucketName}/{rootPrefix}/job-bundles/
```

Where `s3BucketName` and `rootPrefix` come from the selected queue's `jobAttachmentSettings`. This means:

- No extra configuration is needed — the bucket is derived from the queue the user already has selected.
- Users (or admins) place job bundles in the `job-bundles/` folder within the queue's attachment bucket.
- Each bundle is an S3 "folder" (common prefix) containing a `template.yaml` or `template.json`.

Example S3 layout:
```
s3://my-farm-bucket/DeadlineCloud/job-bundles/
    blender-render/
        template.yaml
    maya-arnold/
        template.yaml
        asset_references.yaml
    simple-job/
        template.json
```

### Detection: What Is a Job Bundle?

A directory (local) or prefix (S3) is a job bundle if it contains a `template.yaml` or `template.json`.

For `list_entries`, we need to check each child directory/prefix. To keep this fast:

- **Local**: For each child directory, check for the existence of `template.yaml` or `template.json` (stat calls only — don't parse yet). Parse only happens in `get_bundle_info` when the user selects a bundle.
- **S3**: Use `list_objects_v2` with the child prefix to check for `template.yaml`/`template.json` keys. Full parsing happens on selection.

### Browser Dialog UI

```
┌─────────────────────────────────────────────────────────────┐
│  Job Bundle Browser                                         │
├────────────────────────────────┬────────────────────────────┤
│  📁 my-bundles/               │  Name: Blender Render      │
│    📦 blender-render/         │  Description: Renders a    │
│    📦 maya-arnold/            │  Blender scene file...     │
│    📁 wip/                    │                            │
│      📦 experimental-job/     │  Steps:                    │
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
- Shows folders (📁) and job bundles (📦) with distinct icons.
- Folders can be expanded/navigated into.
- Job bundles are leaf nodes (selectable, not expandable).
- Non-bundle, non-directory files are hidden.

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

For S3 bundles, the submission flow downloads the bundle to a temporary local directory before submission. This is handled in `show_job_bundle_submitter` after the dialog returns.

### Changes to Existing Code

| File | Change |
|---|---|
| `config/config_file.py` | Add `settings.job_bundle_default_directory` to `SETTINGS` |
| `ui/dialogs/job_bundle_browser_dialog.py` | **New file.** The browser dialog. |
| `ui/widgets/job_bundle_settings_tab.py` | `on_load_bundle` opens the new browser dialog instead of `QFileDialog` |
| `ui/job_bundle_submitter.py` | `show_job_bundle_submitter` uses the new browser dialog when `browse=True` |
| `job_bundle/loader.py` | Add `is_job_bundle_dir(path) -> bool` helper for quick detection |
| `job_bundle/repository.py` | **New file.** `BundleRepository` protocol, `LocalBundleRepository`, `S3BundleRepository` |

### S3 Considerations

- **Authentication**: S3 browsing uses the same boto3 session/profile as the rest of deadline-cloud. No separate auth flow.
- **Permissions**: Requires `s3:ListBucket` and `s3:GetObject` on the queue's attachment bucket. If access is denied, show an error in the dialog rather than crashing.
- **Performance**: Each directory expansion is one `list_objects_v2` call. Bundle detection adds one `list_objects_v2` per child prefix. Acceptable for typical bundle repositories (tens of bundles, not thousands).
- **Template download**: `get_bundle_info` for S3 downloads only the `template.yaml`/`template.json` file (typically <10KB) to parse metadata.
- **Bundle selection**: When the user selects an S3 bundle, the full bundle directory is downloaded to a temp directory for submission. This happens after the dialog closes, not during browsing.

## Out of Scope (Future)

- Caching/indexing of bundle metadata for faster repeated browsing.
- Search/filter within the browser.
- Favoriting or pinning frequently used bundles.
- Browsing bundles from a Deadline Cloud service API (e.g. farm-level bundle registry).
- Configurable S3 bucket/prefix (currently always derived from the queue).

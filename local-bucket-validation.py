#!/usr/bin/env python3
"""
Site-Level Sync Validation Tool
Scans island directories and site subfolders on the local network drive
and reports local files that are missing from Google Cloud Storage.
"""

import argparse
import fnmatch
import io
import os
from pathlib import Path
from typing import Dict, Iterable, List, Set, TextIO, Tuple
from google.cloud import storage

# --- CONFIGURATION ---
LOCAL_BASE_DIR = Path(r"P:\StRS_Sites_02\2026\SE2602_AMSM")
GCS_BUCKET_NAME = "nmfs-dev-uc1-pifsc"
GCS_PREFIX = "SFM0/StRS_Sites_02/2026/SE2602_AMSM/"
APP_ICON_PATH = Path(__file__).with_name("local-bucket-validation.ico")

# Folders to ignore in calculations
EXCLUDE_DIRS = {"Products_automation", "MISC", "@eaDir"}
IGNORED_FILE_PATTERNS = {"._*", ".DS_Store", "Thumbs.db"}
COMPARISON_DIRECTIONS = ("local-to-gcs", "gcs-to-local", "both")


def parse_csv_values(value: str) -> Set[str]:
    """Parses comma-separated GUI values, ignoring empty entries."""
    return {item.strip() for item in value.split(",") if item.strip()}


def is_ignored_file(file_name: str, ignored_file_patterns: Set[str]) -> bool:
    """Returns whether a filename matches one of the configured ignore patterns."""
    file_name = file_name.casefold()
    return any(
        fnmatch.fnmatchcase(file_name, pattern.casefold())
        for pattern in ignored_file_patterns
    )


def get_site_manifest_local(
    site_path: Path,
    year_filter: str = "",
    excluded_dirs: Set[str] = EXCLUDE_DIRS,
    ignored_file_patterns: Set[str] = IGNORED_FILE_PATTERNS,
) -> Set[str]:
    r"""
    Scans a single local site folder (e.g. P:\...\BAK\BAK-9999)
    and returns its relative file paths.
    """
    manifest: Set[str] = set()
    if not site_path.exists():
        return manifest

    for root, dirs, files in os.walk(site_path):
        dirs[:] = [d for d in dirs if d not in excluded_dirs]
        for f in files:
            if is_ignored_file(f, ignored_file_patterns):
                continue
            full_path = Path(root) / f
            rel_to_site = full_path.relative_to(site_path).as_posix()
            parts = Path(rel_to_site).parts
            if any(part in excluded_dirs for part in parts):
                continue
            if year_filter and (not parts or parts[0] != year_filter):
                continue
            manifest.add(rel_to_site)

    return manifest


def get_gcs_blobs_by_prefix(bucket_name: str, prefix: str) -> Iterable[storage.Blob]:
    """Retrieves all GCS blobs under a given base prefix."""
    client = storage.Client()
    bucket = client.bucket(bucket_name)
    return bucket.list_blobs(prefix=prefix, fields="items(name),nextPageToken")


def parse_gcs_site_manifests(
    blobs: Iterable[storage.Blob],
    base_prefix: str,
    year_filter: str = "",
    excluded_dirs: Set[str] = EXCLUDE_DIRS,
    ignored_file_patterns: Set[str] = IGNORED_FILE_PATTERNS,
) -> Dict[str, Set[str]]:
    """
    Groups GCS blobs into site manifests:
    site_key (e.g. 'BAK/BAK-9999') -> set of relative file paths
    """
    site_manifests: Dict[str, Set[str]] = {}
    clean_base = base_prefix.strip("/")
    if clean_base:
        clean_base += "/"

    for blob in blobs:
        if blob.name.endswith("/"):
            continue
        if is_ignored_file(blob.name.rsplit("/", 1)[-1], ignored_file_patterns):
            continue

        # Strip base prefix to get relative path: 'BAK/BAK-9999/IMG_001.JPG'
        rel_path = blob.name[len(clean_base) :] if blob.name.startswith(clean_base) else blob.name
        parts = Path(rel_path).parts

        # Path structure must be at least Island/SiteFolder/Filename
        if len(parts) < 3:
            continue

        island, site_folder = parts[0], parts[1]

        if year_filter and parts[2] != year_filter:
            continue

        # Ignore files inside excluded directories
        if any(part in excluded_dirs for part in parts[2:]):
            continue

        site_key = f"{island}/{site_folder}"
        rel_file_path = Path(*parts[2:]).as_posix()

        if site_key not in site_manifests:
            site_manifests[site_key] = set()

        site_manifests[site_key].add(rel_file_path)

    return site_manifests


def validate_site(
    local_manifest: Set[str], gcs_manifest: Set[str]
) -> Tuple[List[str], List[str]]:
    """Returns files missing from GCS and files missing from the local site."""
    return (
        sorted(local_manifest - gcs_manifest),
        sorted(gcs_manifest - local_manifest),
    )


def run_validation(
    direction: str,
    local_base_dir: Path = LOCAL_BASE_DIR,
    bucket_name: str = GCS_BUCKET_NAME,
    prefix: str = GCS_PREFIX,
    year_filter: str = "",
    excluded_dirs: Set[str] = EXCLUDE_DIRS,
    ignored_file_patterns: Set[str] = IGNORED_FILE_PATTERNS,
) -> str:
    output = io.StringIO()
    print(
        "Excluded folders: "
        + (", ".join(sorted(excluded_dirs)) if excluded_dirs else "(none)"),
        file=output,
    )
    print(
        "Ignored filename patterns: "
        + (
            ", ".join(sorted(ignored_file_patterns))
            if ignored_file_patterns
            else "(none)"
        ),
        file=output,
    )
    print("Fetching GCS bucket contents...", file=output)
    normalized_prefix = prefix.strip("/")
    if normalized_prefix:
        normalized_prefix += "/"
    gcs_blobs = get_gcs_blobs_by_prefix(bucket_name, normalized_prefix)
    gcs_site_manifests = parse_gcs_site_manifests(
        gcs_blobs,
        normalized_prefix,
        year_filter,
        excluded_dirs,
        ignored_file_patterns,
    )

    # Collect all site directories from local drive
    local_site_keys = set()
    if local_base_dir.exists():
        for island in local_base_dir.iterdir():
            if island.is_dir() and island.name not in excluded_dirs:
                for site_folder in island.iterdir():
                    if site_folder.is_dir() and site_folder.name not in excluded_dirs:
                        local_site_keys.add(f"{island.name}/{site_folder.name}")

    if direction == "local-to-gcs":
        all_site_keys = sorted(local_site_keys)
    else:
        all_site_keys = sorted(local_site_keys | set(gcs_site_manifests))

    local_files_missing_from_gcs: Dict[str, List[str]] = {}
    gcs_files_missing_locally: Dict[str, List[str]] = {}
    synced_site_count = 0

    print(f"\nValidating {len(all_site_keys)} site folders...\n", file=output)

    for site_key in all_site_keys:
        island, site_folder = site_key.split("/", 1)
        local_site_path = local_base_dir / island / site_folder

        local_manifest = get_site_manifest_local(
            local_site_path, year_filter, excluded_dirs, ignored_file_patterns
        )
        gcs_manifest = gcs_site_manifests.get(site_key, set())
        missing_from_gcs, missing_locally = validate_site(local_manifest, gcs_manifest)
        if missing_from_gcs:
            local_files_missing_from_gcs[site_key] = missing_from_gcs
        if missing_locally:
            gcs_files_missing_locally[site_key] = missing_locally
        if (
            direction not in ("local-to-gcs", "both") or not missing_from_gcs
        ) and (
            direction not in ("gcs-to-local", "both") or not missing_locally
        ):
            synced_site_count += 1

    # --- RESULTS REPORT ---
    print("=" * 60, file=output)
    print("                SITE SYNC VALIDATION                   ", file=output)
    print("=" * 60, file=output)

    if direction in ("local-to-gcs", "both"):
        print_missing_files(
            "LOCAL FILES MISSING FROM GCS", local_files_missing_from_gcs, output
        )
    if direction in ("gcs-to-local", "both"):
        print_missing_files(
            "GCS FILES MISSING LOCALLY", gcs_files_missing_locally, output
        )

    print("\n" + "=" * 60, file=output)
    print(f"Sites Audited: {len(all_site_keys)}", file=output)
    print(f"Sites Synced: {synced_site_count}", file=output)
    print("=" * 60, file=output)
    return output.getvalue()


def print_missing_files(
    title: str, files_by_site: Dict[str, List[str]], output: TextIO
) -> None:
    file_count = sum(len(files) for files in files_by_site.values())
    print(f"\n{title} ({file_count}):", file=output)
    if files_by_site:
        for site_key, files in files_by_site.items():
            print(f"  {site_key} ({len(files)}):", file=output)
            for file_name in files:
                print(f"    - {file_name}", file=output)
    else:
        print("  (None)", file=output)


def launch_gui(
    default_direction: str,
    default_local_base_dir: Path,
    default_bucket_name: str,
    default_prefix: str,
    default_year_filter: str,
    default_excluded_dirs: Set[str],
    default_ignored_file_patterns: Set[str],
) -> None:
    import queue
    import threading
    import tkinter as tk
    import traceback
    from tkinter import filedialog, messagebox, ttk

    result_queue: queue.Queue[Tuple[bool, str]] = queue.Queue()

    root = tk.Tk()
    root.title("Local Bucket Validation")
    root.geometry("960x640")
    root.minsize(700, 480)
    if APP_ICON_PATH.is_file():
        root.iconbitmap(str(APP_ICON_PATH))

    direction = tk.StringVar(value=default_direction)
    local_base_dir = tk.StringVar(value=str(default_local_base_dir))
    bucket_name = tk.StringVar(value=default_bucket_name)
    prefix = tk.StringVar(value=default_prefix)
    year_filter = tk.StringVar(value=default_year_filter)
    excluded_dirs = tk.StringVar(value=", ".join(sorted(default_excluded_dirs)))
    ignored_file_patterns = tk.StringVar(
        value=", ".join(sorted(default_ignored_file_patterns))
    )
    status = tk.StringVar(value="Choose a comparison direction and run validation.")

    controls = ttk.Frame(root, padding=10)
    controls.pack(fill=tk.X)
    controls.columnconfigure(1, weight=1)

    ttk.Label(controls, text="Local base folder:").grid(
        row=0, column=0, sticky=tk.W, padx=(0, 8), pady=4
    )
    ttk.Entry(controls, textvariable=local_base_dir).grid(
        row=0, column=1, sticky=tk.EW, pady=4
    )

    def browse_local_folder() -> None:
        selected = filedialog.askdirectory(
            parent=root,
            title="Select local base folder",
            initialdir=(
                local_base_dir.get()
                if Path(local_base_dir.get()).is_dir()
                else str(Path.home())
            ),
        )
        if selected:
            local_base_dir.set(selected)

    ttk.Button(controls, text="Browse...", command=browse_local_folder).grid(
        row=0, column=2, padx=(8, 0), pady=4
    )

    ttk.Label(controls, text="GCS bucket (type or select):").grid(
        row=1, column=0, sticky=tk.W, padx=(0, 8), pady=4
    )
    bucket_picker = ttk.Combobox(
        controls,
        textvariable=bucket_name,
        values=(default_bucket_name,),
        state="normal",
    )
    bucket_picker.grid(row=1, column=1, columnspan=2, sticky=tk.EW, pady=4)

    ttk.Label(controls, text="GCS prefix:").grid(
        row=2, column=0, sticky=tk.W, padx=(0, 8), pady=4
    )
    ttk.Entry(controls, textvariable=prefix).grid(
        row=2, column=1, columnspan=2, sticky=tk.EW, pady=4
    )

    ttk.Label(controls, text="Year filter (optional):").grid(
        row=3, column=0, sticky=tk.W, padx=(0, 8), pady=4
    )
    ttk.Entry(controls, textvariable=year_filter, width=12).grid(
        row=3, column=1, sticky=tk.W, pady=4
    )
    ttk.Label(controls, text="Exact folder name, e.g. 2026").grid(
        row=3, column=2, sticky=tk.W, padx=(8, 0), pady=4
    )

    ttk.Label(controls, text="Comparison:").grid(
        row=6, column=0, sticky=tk.W, padx=(0, 8), pady=4
    )
    direction_picker = ttk.Combobox(
        controls,
        textvariable=direction,
        values=COMPARISON_DIRECTIONS,
        state="readonly",
        width=18,
    )
    direction_picker.grid(row=6, column=1, sticky=tk.W, pady=4)

    ttk.Label(controls, text="Excluded folders:").grid(
        row=4, column=0, sticky=tk.W, padx=(0, 8), pady=4
    )
    ttk.Entry(controls, textvariable=excluded_dirs).grid(
        row=4, column=1, columnspan=2, sticky=tk.EW, pady=4
    )

    ttk.Label(controls, text="Ignored filename patterns:").grid(
        row=5, column=0, sticky=tk.W, padx=(0, 8), pady=4
    )
    ttk.Entry(controls, textvariable=ignored_file_patterns).grid(
        row=5, column=1, columnspan=2, sticky=tk.EW, pady=4
    )
    ttk.Label(
        controls,
        text="Comma-separated; filename patterns support * and ?.",
    ).grid(row=7, column=1, columnspan=2, sticky=tk.W, pady=(0, 4))

    buttons = ttk.Frame(root, padding=(10, 0, 10, 6))
    buttons.pack(fill=tk.X)
    status_bar = ttk.Label(
        root, textvariable=status, anchor=tk.W, padding=(10, 0, 10, 6)
    )
    status_bar.pack(fill=tk.X)

    output_frame = ttk.Frame(root, padding=(10, 0, 10, 10))
    output_frame.pack(fill=tk.BOTH, expand=True)
    output_box = tk.Text(output_frame, wrap=tk.NONE, state=tk.DISABLED)
    output_scrollbar = ttk.Scrollbar(
        output_frame, orient=tk.VERTICAL, command=output_box.yview
    )
    horizontal_scrollbar = ttk.Scrollbar(
        output_frame, orient=tk.HORIZONTAL, command=output_box.xview
    )
    output_box.configure(
        yscrollcommand=output_scrollbar.set,
        xscrollcommand=horizontal_scrollbar.set,
    )
    output_box.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
    output_scrollbar.pack(side=tk.RIGHT, fill=tk.Y)
    horizontal_scrollbar.pack(side=tk.BOTTOM, fill=tk.X)

    def show_output(text: str) -> None:
        output_box.configure(state=tk.NORMAL)
        output_box.delete("1.0", tk.END)
        output_box.insert(tk.END, text)
        output_box.configure(state=tk.DISABLED)

    def poll_result() -> None:
        try:
            succeeded, result = result_queue.get_nowait()
        except queue.Empty:
            root.after(100, poll_result)
            return

        run_button.configure(state=tk.NORMAL)
        save_button.configure(state=tk.NORMAL if succeeded else tk.DISABLED)
        if succeeded:
            show_output(result)
            status.set("Validation complete.")
        else:
            show_output(f"Validation failed:\n\n{result}")
            status.set("Validation failed. See the output for details.")
            messagebox.showerror(
                "Validation failed",
                "The comparison could not be completed. See the output for details.",
                parent=root,
            )

    def run_comparison() -> None:
        selected_local_base_dir = Path(local_base_dir.get()).expanduser()
        selected_bucket_name = bucket_name.get().strip()
        selected_prefix = prefix.get().strip("/")
        if not selected_local_base_dir.is_dir():
            messagebox.showerror(
                "Invalid local folder",
                f"The selected folder does not exist:\n{selected_local_base_dir}",
                parent=root,
            )
            return
        if not selected_bucket_name:
            messagebox.showerror(
                "Missing bucket",
                "Enter a Google Cloud Storage bucket name.",
                parent=root,
            )
            return
        selected_year_filter = year_filter.get().strip()
        if selected_year_filter and (
            len(selected_year_filter) != 4 or not selected_year_filter.isdigit()
        ):
            messagebox.showerror(
                "Invalid year",
                "Enter a four-digit year such as 2026, or leave the field blank.",
                parent=root,
            )
            return
        selected_excluded_dirs = parse_csv_values(excluded_dirs.get())
        selected_ignored_file_patterns = {
            pattern.casefold()
            for pattern in parse_csv_values(ignored_file_patterns.get())
        }

        run_button.configure(state=tk.DISABLED)
        save_button.configure(state=tk.DISABLED)
        status.set("Running comparison; this may take a while...")
        show_output("Fetching local files and Google Cloud Storage objects...")
        selected_direction = direction.get()

        def worker() -> None:
            try:
                result_queue.put(
                    (
                        True,
                        run_validation(
                            selected_direction,
                            selected_local_base_dir,
                            selected_bucket_name,
                            selected_prefix,
                            selected_year_filter,
                            selected_excluded_dirs,
                            selected_ignored_file_patterns,
                        ),
                    )
                )
            except Exception:
                result_queue.put((False, traceback.format_exc()))

        threading.Thread(target=worker, daemon=True).start()
        root.after(100, poll_result)

    def save_output() -> None:
        text = output_box.get("1.0", tk.END).rstrip()
        if not text:
            messagebox.showwarning("No output", "Run a comparison before saving.", parent=root)
            return
        destination = filedialog.asksaveasfilename(
            parent=root,
            title="Save validation report",
            defaultextension=".txt",
            filetypes=(("Text files", "*.txt"), ("All files", "*.*")),
            initialfile="local-bucket-validation.txt",
        )
        if not destination:
            return
        try:
            Path(destination).write_text(text + "\n", encoding="utf-8")
        except OSError as error:
            messagebox.showerror(
                "Could not save report", str(error), parent=root
            )
            return
        status.set(f"Report saved to {destination}")

    run_button = ttk.Button(buttons, text="Run validation", command=run_comparison)
    run_button.pack(side=tk.LEFT)
    save_button = ttk.Button(
        buttons, text="Save output as .txt", command=save_output, state=tk.DISABLED
    )
    save_button.pack(side=tk.LEFT, padx=(8, 0))

    root.mainloop()


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Compare local site files with Google Cloud Storage."
    )
    parser.add_argument(
        "--direction",
        choices=COMPARISON_DIRECTIONS,
        default="local-to-gcs",
        help=(
            "Comparison direction: local-to-gcs reports local files missing from "
            "GCS; gcs-to-local reports GCS files missing locally; both reports both "
            "(default: local-to-gcs)."
        ),
    )
    parser.add_argument(
        "--year",
        default="",
        help="Optional four-digit year folder to compare (for example, 2026).",
    )
    parser.add_argument(
        "--gui",
        action="store_true",
        help="Open the graphical interface (this is the default).",
    )
    parser.add_argument(
        "--cli",
        action="store_true",
        help="Run in the command line instead of opening the graphical interface.",
    )
    args = parser.parse_args()

    if args.year and (len(args.year) != 4 or not args.year.isdigit()):
        parser.error("--year must be a four-digit year such as 2026")

    if args.cli:
        print(run_validation(args.direction, year_filter=args.year), end="")
    else:
        launch_gui(
            args.direction,
            LOCAL_BASE_DIR,
            GCS_BUCKET_NAME,
            GCS_PREFIX,
            args.year,
            EXCLUDE_DIRS,
            IGNORED_FILE_PATTERNS,
        )


if __name__ == "__main__":
    main()
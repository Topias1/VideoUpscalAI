import os
from pathlib import Path
from typing import Any, Dict, List, Tuple

from . import UpscalerError
from .plan import resolve_output_paths
from .probe import probe_video
from .pipeline import run_single_file

SUPPORTED_EXTENSIONS = {
    ".mp4", ".mov", ".mkv", ".avi", ".m4v", ".webm",
    ".mpg", ".mpeg", ".wmv", ".flv", ".ts"
}

def discover_inputs(inputs: List[str], recursive: bool) -> List[Tuple[str, str]]:
    """Discovers all supported video files from the input arguments.
    
    Returns a sorted list of tuples: (absolute_file_path, input_root_dir_or_file).
    """
    discovered: List[Tuple[str, str]] = []
    
    for inp in inputs:
        inp_abs = os.path.abspath(inp)
        if not os.path.exists(inp_abs):
            raise FileNotFoundError(f"Input path does not exist: {inp}")
            
        if os.path.isfile(inp_abs):
            ext = Path(inp_abs).suffix.lower()
            if ext in SUPPORTED_EXTENSIONS:
                discovered.append((inp_abs, inp_abs))
        elif os.path.isdir(inp_abs):
            if recursive:
                for root, _, files in os.walk(inp_abs):
                    for f in files:
                        ext = Path(f).suffix.lower()
                        if ext in SUPPORTED_EXTENSIONS:
                            discovered.append((os.path.join(root, f), inp_abs))
            else:
                for f in os.listdir(inp_abs):
                    f_path = os.path.join(inp_abs, f)
                    if os.path.isfile(f_path):
                        ext = Path(f).suffix.lower()
                        if ext in SUPPORTED_EXTENSIONS:
                            discovered.append((f_path, inp_abs))

    # Sort the discovered files to process in a deterministic order
    discovered.sort(key=lambda x: x[0])
    return discovered

def run_batch(
    inputs: List[str],
    opts: Dict[str, Any],
    tools_info: Dict[str, Any]
) -> int:
    """Runs upscaling on all discovered input files, returning an exit code (0 on success, 1 on failure)."""
    recursive = opts.get("recursive", False)
    
    # 1. Discover inputs
    discovered = discover_inputs(inputs, recursive)
    if not discovered:
        print("No supported video files discovered from the inputs.")
        return 0

    # 2. Resolve output paths & detect collisions
    try:
        output_mappings = resolve_output_paths(
            discovered,
            opts.get("output"),
            opts.get("output_dir"),
            opts["preset"]
        )
    except ValueError as e:
        print(f"ERROR: Output routing configuration error: {e}")
        return 1

    succeeded: List[str] = []
    failed: List[Tuple[str, str]] = []
    skipped: List[str] = []

    total_files = len(output_mappings)
    print(f"Found {total_files} files to process.")

    # 3. Process loop
    for idx, (infile, outfile) in enumerate(output_mappings, start=1):
        filename = os.path.basename(infile)
        progress_prefix = f"File {idx}/{total_files}: {filename}"
        
        # Check if final output is already completed and valid
        skip_file = False
        if os.path.exists(outfile):
            try:
                # Probe output to check validity
                out_info = probe_video(outfile)
                if out_info.frame_count > 0:
                    skip_file = True
            except Exception:
                pass
                
        if skip_file:
            print(f"{progress_prefix} (Skipped - output already exists and is valid)")
            skipped.append(infile)
            continue

        print(f"\n{'='*80}")
        print(f"{progress_prefix}")
        print(f"Target output: {outfile}")
        print(f"{'='*80}")

        try:
            run_single_file(infile, outfile, opts, tools_info)
            succeeded.append(infile)
        except Exception as e:
            err_msg = str(e)
            print(f"\nERROR: Failed processing {infile}: {err_msg}")
            failed.append((infile, err_msg))
            
            if opts.get("fail_fast", False):
                print("\n[fail-fast] Terminating batch run on first error.")
                break

    # 4. Print Summary
    print(f"\n{'-'*80}")
    print("Upscale Batch Summary")
    print(f"{'-'*80}")
    print(f"Total processed: {total_files}")
    print(f"Succeeded:       {len(succeeded)}")
    print(f"Skipped:         {len(skipped)}")
    print(f"Failed:          {len(failed)}")
    
    if failed:
        print("\nFailed files detail:")
        for idx, (infile, err) in enumerate(failed, start=1):
            print(f"  {idx}. {infile}")
            print(f"     Error: {err}")
        return 1

    return 0

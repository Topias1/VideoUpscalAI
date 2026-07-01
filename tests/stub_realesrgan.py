#!/usr/bin/env python3
import argparse
import os
import subprocess
import sys

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("-i", "--input", required=True)
    parser.add_argument("-o", "--output", required=True)
    parser.add_argument("-n", "--model")
    parser.add_argument("-s", "--scale", default="4")
    parser.add_argument("-f", "--format", default="png")
    parser.add_argument("-j", "--jobs")
    args, unknown = parser.parse_known_args()
    
    os.makedirs(args.output, exist_ok=True)
    scale = args.scale
    
    files = sorted([f for f in os.listdir(args.input) if f.endswith(".png")])
    total = len(files)
    
    for idx, f in enumerate(files, start=1):
        in_file = os.path.join(args.input, f)
        out_file = os.path.join(args.output, f)
        
        # Upscale by scaling width and height using ffmpeg
        cmd = [
            "ffmpeg",
            "-y",
            "-v", "error",
            "-i", in_file,
            "-vf", f"scale=iw*{scale}:ih*{scale}",
            out_file
        ]
        subprocess.run(cmd, check=True)
        
        # Write progress percentage to stderr as realesrgan does
        percent = (idx / total) * 100.0
        sys.stderr.write(f"\r{percent:.2f}%")
        sys.stderr.flush()
        
    sys.stderr.write("\n")
    sys.stderr.flush()

if __name__ == "__main__":
    main()

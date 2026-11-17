#!/usr/bin/env python3
"""Check all regenerated bindings, then emit a timestamp-independent AAR."""

import io
import subprocess
import sys
import zipfile
from pathlib import Path

original, work, destination = map(Path, sys.argv[1:])

def entries(archive):
    return {item.filename: archive.read(item) for item in archive.infolist() if not item.is_dir()}

def classes(data):
    with zipfile.ZipFile(io.BytesIO(data)) as jar:
        return entries(jar)

def go_info(path):
    text = subprocess.check_output(["go", "version", "-m", path], text=True)
    lines = text.splitlines()
    # Trim only the file name and the machine-specific local Firestack replacement.
    result = [lines[0].split(": ", 1)[1]]
    for line in lines[1:]:
        if line.startswith("\t=>\t") and result[-1].startswith("\tdep\tgithub.com/celzero/firestack\t"):
            fields = line.split()
            if len(fields) != 3 or fields[2] != "(devel)" or not Path(fields[1]).is_absolute():
                sys.exit(f"{path}: expected the local Firestack source replacement")
            result.append("local Firestack source")
        else:
            result.append(line.rstrip())
    return result

def jni_symbols(path):
    text = subprocess.check_output(["readelf", "--dyn-syms", "--wide", path], text=True)
    return {line.split()[-1] for line in text.splitlines() if " Java_" in line or " JNI_" in line}

with zipfile.ZipFile(original) as aar:
    original_files = entries(aar)
result = {}
for path, data in original_files.items():
    if not path.endswith(".so"):
        result[path] = data
        continue
    abi = path.split("/")[1]
    dir_ = work / abi
    lib = dir_ / "libgojni.so"
    original_lib = dir_ / "upstream.so"
    original_lib.write_bytes(data)
    with zipfile.ZipFile(dir_ / "bound.aar") as generated:
        for name, contents in original_files.items():
            if name == "classes.jar":
                if classes(generated.read(name)) != classes(contents):
                    sys.exit(f"{abi}: regenerated Java classes differ from upstream")
            elif not name.endswith(".so") and generated.read(name) != contents:
                sys.exit(f"{abi}: regenerated {name} differs from upstream")
    if go_info(lib) != go_info(original_lib):
        sys.exit(f"{abi}: Go dependency versions or build settings differ from upstream")
    expected_symbols = jni_symbols(original_lib)
    if not expected_symbols or jni_symbols(lib) != expected_symbols:
        sys.exit(f"{abi}: JNI exports differ from upstream")
    result[path] = lib.read_bytes()

# Original binding metadata is byte-checked above. Store entries uncompressed to
# keep the archive independent of zlib version; Gradle repackages the final APK.
with zipfile.ZipFile(destination, "w") as aar:
    for name in sorted(result):
        item = zipfile.ZipInfo(name, (1980, 1, 1, 0, 0, 0))
        item.create_system = 3
        item.external_attr = 0o100644 << 16
        aar.writestr(item, result[name])

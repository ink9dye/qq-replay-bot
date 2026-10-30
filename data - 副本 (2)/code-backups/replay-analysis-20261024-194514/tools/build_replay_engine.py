from pathlib import Path
import os
import subprocess
import json
import hashlib
import shutil
import argparse
import zipfile
import tarfile

BASE = Path(__file__).resolve().parents[1]

def main():
    parser = argparse.ArgumentParser(description='重建只读录像重放内核')
    parser.add_argument('--vcvars', type=Path, default=Path(r"D:\Microsoft Visual Studio\Community\VC\Auxiliary\Build\vcvars64.bat"))
    vcvars = parser.parse_args().vcvars
    if not vcvars.is_file():
        raise RuntimeError("请用 --vcvars 指定本机 vcvars64.bat 路径")
    vendor = BASE / 'vendor/replay-analysis'
    provenance = json.loads((vendor / 'provenance.json').read_text(encoding='utf-8'))
    for source in provenance:
        if hashlib.sha256((vendor / source['file']).read_bytes()).hexdigest() != source['sha256']:
            raise RuntimeError('源码压缩包指纹不一致')
    if not (vendor / 'ygopro-core-master').is_dir():
        with zipfile.ZipFile(vendor / 'core.zip') as archive:
            archive.extractall(vendor)
    if not (vendor / 'lua-5.4.8').is_dir():
        with tarfile.open(vendor / 'lua.tar.gz') as archive:
            archive.extractall(vendor)
    env_script = BASE / "tools/msvc_environment.cmd"
    env_script.write_text('@call "' + str(vcvars) + '" >nul\n@set\n', encoding="mbcs")
    output = subprocess.check_output(["cmd.exe", "/d", "/c", str(env_script)])
    env = {key.upper(): value for key, value in os.environ.items()}
    for line in output.decode("mbcs").splitlines():
        if "=" in line and not line.startswith("="):
            key, value = line.split("=", 1)
            env[key.upper()] = value
    core = vendor / "ygopro-core-master"
    lua = vendor / "lua-5.4.8/src"
    build = vendor / "build"
    runtime = BASE / "runtime"
    build.mkdir(exist_ok=True)
    runtime.mkdir(exist_ok=True)
    sources = sorted(core.glob("*.cpp")) + [p for p in sorted(lua.glob("*.c"))
                                            if p.name not in ("lua.c", "luac.c", "linit.c", "onelua.c")]
    compiler = shutil.which("cl.exe", path=env["PATH"])
    if not compiler: raise RuntimeError("vcvars64 没有找到编译器")
    args = [compiler, "/nologo", "/O2", "/MT", "/EHsc", "/std:c++17", "/utf-8", "/TP",
            "/D_CRT_SECURE_NO_WARNINGS", "/DOCGCORE_EXPORT_FUNCTIONS", "/I" + str(lua), "/I" + str(core),
            "/LD"] + list(map(str, sources)) + ["/link", "/OUT:" + str(runtime / "ocgcore.dll")]
    print("Compiling private x64 engine:", len(sources), "source files", flush=True)
    subprocess.run(args, cwd=build, env=env, check=True)
    info = {"architecture": "x64", "sha256": hashlib.sha256((runtime / "ocgcore.dll").read_bytes()).hexdigest(),
            "source_archives": provenance}
    (runtime / "provenance.json").write_text(json.dumps(info, indent=2), encoding="utf-8")

if __name__ == "__main__": main()

# Copyright (c) 2026 OceanBase. SPDX-License-Identifier: Apache-2.0
"""Exercise build.py init with local installer fixtures and no downloads."""

import hashlib
import json
import os
from pathlib import Path
import re
import runpy
import shutil
import subprocess
import sys
import tempfile
import unittest


REPO = Path(__file__).resolve().parents[2]


@unittest.skipIf(os.name == "nt", "The installer fixture uses a POSIX shell")
class BuildInitTest(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="seekdb init 'fixture'.")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        for name in ("build.py", "deps/init/prepare.py", "rust/rust-toolchain.toml"):
            target = self.root / name
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(str(REPO / name), str(target))
        prepare = runpy.run_path(str(REPO / "deps/init/prepare.py"))
        profile = self.root / "deps/init" / prepare["host_profile"]().name
        profile.write_text("cached dependency fixture\n")
        dependencies = self.root / "deps/3rd"
        dependencies.mkdir()
        (dependencies / hashlib.md5(profile.read_bytes()).hexdigest()).touch()
        (dependencies / "DONE").touch()

        # Keep installed Rust tools on the test host out of the fixture's PATH.
        host_bin = self.root / "host-bin"
        host_bin.mkdir()
        for tool in ("sh", "mkdir", "cp", "chmod"):
            (host_bin / tool).symlink_to(shutil.which(tool))
        fake_tool = self.root / "rust-tool"
        fake_tool.write_text("#!" + sys.executable + "\n" + '''import json, os, sys
from pathlib import Path
tool = Path(sys.argv[0]).name
with open(os.environ["TOOL_LOG"], "a") as stream:
    stream.write(json.dumps({"tool": tool, "cwd": os.getcwd(),
        "dist": os.environ.get("RUSTUP_DIST_SERVER"),
        "update": os.environ.get("RUSTUP_UPDATE_ROOT"),
        "override": os.environ.get("RUSTUP_TOOLCHAIN"),
        "auto_install": os.environ.get("RUSTUP_AUTO_INSTALL")}) + "\\n")
print(tool + " fixture version")
sys.exit(int(os.environ.get("RUSTUP_FAILURE", "0")))
''')
        fake_tool.chmod(0o755)
        self.installer = self.root / "rustup-init.sh"
        self.installer.write_text('''#!/bin/sh
set -eu
mkdir -p "$CARGO_HOME/bin"
for tool in rustup cargo rustc; do
    cp "$FAKE_RUST_TOOL" "$CARGO_HOME/bin/$tool"
    chmod +x "$CARGO_HOME/bin/$tool"
done
echo installed >> "$INSTALL_LOG"
''')
        self.environment = dict(os.environ)
        for key in ("RUSTUP_DIST_SERVER", "RUSTUP_UPDATE_ROOT", "RUSTUP_TOOLCHAIN", "DEP_CACHE_DIR"):
            self.environment.pop(key, None)
        self.environment.update({
            "PATH": str(host_bin),
            "CARGO_HOME": str(self.root / "cargo-home"),
            "RUSTUP_HOME": str(self.root / "rustup-home"),
            "RUSTUP_INIT_URL": self.installer.as_uri(),
            "FAKE_RUST_TOOL": str(fake_tool),
            "TOOL_LOG": str(self.root / "tools.jsonl"),
            "INSTALL_LOG": str(self.root / "installer.log"),
        })

    def run_init(self, *arguments):
        return subprocess.run([sys.executable, str(self.root / "build.py"), "init", *arguments],
                              env=self.environment, stdout=subprocess.PIPE,
                              stderr=subprocess.STDOUT, universal_newlines=True)

    def tool_calls(self):
        return [json.loads(line) for line in (self.root / "tools.jsonl").read_text().splitlines()]

    def test_first_init_installs_rust_with_default_mirror(self):
        self.environment["RUSTUP_TOOLCHAIN"] = "unrelated-toolchain"
        result = self.run_init()
        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertIn("Preparing Rust toolchain", result.stdout)
        self.assertIn("rustup fixture version", result.stdout)
        self.assertIn("Dependencies ready", result.stdout)
        calls = self.tool_calls()
        self.assertEqual([call["tool"] for call in calls], ["rustup"])
        for call in calls:
            self.assertEqual(call["cwd"], str(self.root / "rust"))
            self.assertEqual(call["dist"], "https://rsproxy.cn")
            self.assertEqual(call["update"], "https://rsproxy.cn/rustup")
            self.assertEqual(call["auto_install"], "1")
            self.assertIsNone(call["override"])

    def test_repeated_init_reuses_installed_rustup(self):
        first = self.run_init()
        self.assertEqual(first.returncode, 0, first.stdout)
        self.installer.unlink()
        second = self.run_init()
        self.assertEqual(second.returncode, 0, second.stdout)
        self.assertEqual((self.root / "installer.log").read_text(), "installed\n")

    def test_explicit_mirrors_are_preserved(self):
        self.environment.update({"RUSTUP_DIST_SERVER": "https://rust-mirror.invalid",
                                 "RUSTUP_UPDATE_ROOT": "https://rustup-mirror.invalid"})
        result = self.run_init()
        self.assertEqual(result.returncode, 0, result.stdout)
        for call in self.tool_calls():
            self.assertEqual(call["dist"], self.environment["RUSTUP_DIST_SERVER"])
            self.assertEqual(call["update"], self.environment["RUSTUP_UPDATE_ROOT"])

    def test_failed_toolchain_setup_stops_init(self):
        self.environment["RUSTUP_FAILURE"] = "7"
        result = self.run_init()
        self.assertNotEqual(result.returncode, 0)
        self.assertNotIn("Dependencies ready", result.stdout)
        self.assertEqual([call["tool"] for call in self.tool_calls()], ["rustup"])

    def test_help_does_not_install_rust(self):
        result = self.run_init("--help")
        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertFalse((self.root / "installer.log").exists())

    def partial_toolchain(self):
        rustup = shutil.which("rustup")
        if not rustup:
            self.skipTest("Real rustup is required to reproduce interrupted installation")
        self.environment.update({"RUSTUP_DIST_SERVER": "http://127.0.0.1:9",
                                 "RUSTUP_UPDATE_ROOT": "http://127.0.0.1:9",
                                 "RUSTUP_AUTO_INSTALL": "0",
                                 "NO_PROXY": "127.0.0.1", "no_proxy": "127.0.0.1"})
        cargo_bin = Path(self.environment["CARGO_HOME"]) / "bin"
        cargo_bin.mkdir(parents=True)
        for tool in ("rustup", "cargo"):
            (cargo_bin / tool).symlink_to(rustup)
        result = subprocess.run([rustup, "show"], cwd=self.root, env=self.environment,
                                stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                universal_newlines=True, timeout=10)
        host = next(line.split(": ", 1)[1] for line in result.stdout.splitlines()
                    if line.startswith("Default host: "))
        pin = (self.root / "rust/rust-toolchain.toml").read_text()
        channel = re.search(r'^channel = "([^"]+)"', pin, re.MULTILINE).group(1)
        toolchain = Path(self.environment["RUSTUP_HOME"]) / "toolchains" / (channel + "-" + host)
        (toolchain / "bin").mkdir(parents=True)
        # Interrupted rustup installs can leave cargo without rustc or dist metadata.
        (toolchain / "bin/cargo").write_text("incomplete installation fixture\n")
        return rustup, channel

    def test_partial_toolchain_cannot_finish_init_without_repair(self):
        self.partial_toolchain()
        result = self.run_init()
        self.assertNotEqual(result.returncode, 0, result.stdout)
        self.assertNotIn("Dependencies ready", result.stdout)

    def test_discovery_rejects_toolchain_without_rustc(self):
        rustup, channel = self.partial_toolchain()
        result = subprocess.run([sys.executable, str(REPO / "bazel/prepare_cargo.py"),
                                 "discover", str(self.root), rustup, channel, "nightly-2000-01-01"],
                                env=self.environment, stdout=subprocess.PIPE,
                                stderr=subprocess.STDOUT, universal_newlines=True, timeout=10)
        self.assertNotEqual(result.returncode, 0, result.stdout)
        self.assertIn("rustc", result.stdout)
        self.assertFalse((self.root / "toolchains.json").exists())


if __name__ == "__main__":
    unittest.main()

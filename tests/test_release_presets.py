"""公开打包只使用提交快照，不访问用户配置。"""
import ast
import io
import os
from pathlib import Path
import shutil
import subprocess
import tarfile
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]


def archive_data(items):
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as archive:
        for name, data in items:
            info = tarfile.TarInfo(name)
            info.size = len(data)
            archive.addfile(info, io.BytesIO(data))
    return buf.getvalue()


class ReleasePresetsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="m18-release-test-")
        source = ROOT / "packaging" / "build.py"
        tree = ast.parse(source.read_text(encoding="utf-8-sig"))
        fn = next(node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == "release_presets")
        namespace = {"io": io, "os": os, "shutil": shutil, "subprocess": subprocess, "tarfile": tarfile,
                     "BUILD": self.temp.name, "ROOT": str(ROOT)}
        exec(compile(ast.Module(body=[fn], type_ignores=[]), str(source), "exec"), namespace)
        self.release = namespace["release_presets"]

    def tearDown(self):
        self.temp.cleanup()

    def test_extracts_exact_committed_snapshot_without_network(self):
        scene = b'{"sceneType":"normal","pages":[]}'
        blob = archive_data([("app/presets/scenes/default.json", scene),
                             ("app/presets/album1/icon.gif", b"GIF89a")])
        with patch.object(subprocess, "check_output", return_value=blob) as git:
            folder = Path(self.release())
        git.assert_called_once_with(["git", "archive", "--format=tar", "HEAD", "app/presets"], cwd=str(ROOT))
        self.assertEqual((folder / "scenes" / "default.json").read_bytes(), scene)
        self.assertEqual(len([p for p in folder.rglob("*") if p.is_file()]), 2)

    def test_rejects_path_traversal(self):
        blob = archive_data([("app/presets/../../leak.json", b"bad")])
        with patch.object(subprocess, "check_output", return_value=blob):
            with self.assertRaises(ValueError):
                self.release()
        self.assertFalse((Path(self.temp.name) / "leak.json").exists())

    def test_rejects_links(self):
        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode="w") as archive:
            info = tarfile.TarInfo("app/presets/linked.json")
            info.type = tarfile.SYMTYPE
            info.linkname = "../../config.json"
            archive.addfile(info)
        with patch.object(subprocess, "check_output", return_value=buf.getvalue()):
            with self.assertRaises(ValueError):
                self.release()

    def test_empty_or_git_failure_stops_build(self):
        with patch.object(subprocess, "check_output", return_value=archive_data([])):
            with self.assertRaises(ValueError):
                self.release()
        with patch.object(subprocess, "check_output", side_effect=subprocess.CalledProcessError(1, "git")):
            with self.assertRaises(subprocess.CalledProcessError):
                self.release()


if __name__ == "__main__":
    unittest.main()

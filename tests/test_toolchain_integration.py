from __future__ import annotations

import json
import tempfile
import unittest
import os
import errno
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path

from srhd_modkit.cli import main
from srhd_modkit.formats import inspect_file
from srhd_modkit.image_codec import RgbaImage, read_png, write_png
from srhd_modkit.scripts import inspect_scr, load_rson
from srhd_modkit.toolchain import Toolchain, _replace_cross_device_safe


class ToolchainIntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.chain = Toolchain()

    def test_argb8888_png_gi_png_roundtrip_is_pixel_exact(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            root = Path(name)
            source = root / "source" / "nested" / "sample.png"
            source.parent.mkdir(parents=True)
            pixels = bytes(
                component
                for pixel in (
                    (255, 0, 0, 255), (0, 255, 0, 192), (0, 0, 255, 128), (1, 2, 3, 0),
                    (12, 34, 56, 78), (90, 87, 65, 43), (22, 44, 66, 88), (99, 111, 123, 135),
                    (254, 253, 252, 251), (127, 126, 125, 124), (4, 8, 16, 32), (64, 128, 192, 255),
                )
                for component in pixel
            )
            image = RgbaImage(4, 3, pixels)
            write_png(image, source)
            gi_root = root / "gi"
            png_root = root / "decoded"
            gi_items = self.chain.convert([root / "source"], gi_root, direction="png-gi", gi_mode="0_32")
            self.assertEqual(len(gi_items), 1)
            gi_path = gi_root / "nested" / "sample.gi"
            self.assertTrue(gi_path.is_file())
            self.assertTrue(inspect_file(gi_path)["signature_valid"])
            self.chain.convert([gi_root], png_root, direction="gi-png")
            decoded = read_png(png_root / "nested" / "sample.png")
            self.assertEqual(image, decoded)

    def test_existing_output_is_not_silently_overwritten(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            root = Path(name)
            source = root / "source.png"
            destination = root / "output"
            destination.mkdir()
            write_png(RgbaImage(1, 1, bytes((1, 2, 3, 4))), source)
            (destination / "source.gi").write_bytes(b"keep")
            with self.assertRaises(FileExistsError):
                self.chain.convert([source], destination, direction="png-gi")
            self.assertEqual((destination / "source.gi").read_bytes(), b"keep")

    def test_gi_conversion_needs_no_external_tools_root(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            root = Path(name)
            source = root / "source.png"
            write_png(RgbaImage(2, 1, bytes((1, 2, 3, 4)) * 2), source)
            chain = Toolchain(root / "tools-that-do-not-exist")
            chain.convert([source], root / "gi", direction="png-gi", gi_mode="0_32")
            chain.convert([root / "gi" / "source.gi"], root / "png", direction="gi-png")
            self.assertEqual(read_png(root / "png" / "source.png"), read_png(source))

    def test_gui_editor_is_disabled_by_default(self) -> None:
        previous = os.environ.pop("SRHD_MODKIT_ALLOW_GUI", None)
        try:
            with self.assertRaises(PermissionError):
                self.chain.open_editor(Path(__file__))
        finally:
            if previous is not None:
                os.environ["SRHD_MODKIT_ALLOW_GUI"] = previous

    def test_cross_volume_publish_falls_back_to_destination_local_stage(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            root = Path(name)
            staged = root / "source" / "result.txt"
            destination = root / "destination" / "result.txt"
            staged.parent.mkdir()
            destination.parent.mkdir()
            staged.write_text("compiled", encoding="utf-8")
            real_replace = os.replace
            calls = 0

            def replace_with_cross_volume_failure(source: str | Path, target: str | Path) -> None:
                nonlocal calls
                calls += 1
                if calls == 1:
                    raise OSError(errno.EXDEV, "cross-device link")
                real_replace(source, target)

            from unittest.mock import patch

            with patch("srhd_modkit.toolchain.os.replace", side_effect=replace_with_cross_volume_failure):
                _replace_cross_device_safe(staged, destination)

            self.assertEqual(destination.read_text(encoding="utf-8"), "compiled")
            self.assertFalse(staged.exists())

    def test_all_gi_encoding_modes_produce_decodable_images(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            root = Path(name)
            source = root / "modes.png"
            write_png(RgbaImage(7, 5, bytes((23, 91, 177, 149)) * 35), source)
            for mode in ("0_32", "0_16", "2"):
                with self.subTest(mode=mode):
                    gi_root = root / f"gi-{mode}"
                    png_root = root / f"png-{mode}"
                    self.chain.convert([source], gi_root, direction="png-gi", gi_mode=mode)
                    self.chain.convert([gi_root / "modes.gi"], png_root, direction="gi-png")
                    info = inspect_file(png_root / "modes.png")
                    self.assertEqual((info["width"], info["height"]), (7, 5))

    def test_rscript_compiles_headless_state_event_signature(self) -> None:
        rscript = self.chain.tools["rscript"].path
        source_svr = rscript.parent / "LastOneHP.svr"
        if not rscript.is_file() or not source_svr.is_file():
            self.skipTest("RScript 4.10f или проверочный SVR не найден")
        with tempfile.TemporaryDirectory() as name:
            root = Path(name)
            rson = root / "events.rson"
            self.chain.convert_script_project(source_svr, rson)
            project = load_rson(rson)
            state = next(item for item in project.iter_objects() if item.get("Type") == "TState")
            state["OnActCode"] = "PlayerActCode();"
            project.set_state_events(state["#"], ["t_OnEnteringForm", "t_OnPlayerBuyEq"])
            project.save(rson)
            scr = root / "events.scr"
            lang = root / "events.txt"
            self.chain.compile_rson(rson, scr, lang)
            self.assertIn(
                "[t_OnEnteringForm,t_OnPlayerBuyEq|]",
                inspect_scr(scr)["event_signatures"],
            )

    def test_rscript_decompiles_scr_headlessly_and_roundtrips(self) -> None:
        rscript = self.chain.tools["rscript"].path
        source_svr = rscript.parent / "LastOneHP.svr"
        if not rscript.is_file() or not source_svr.is_file():
            self.skipTest("RScript 4.10f или проверочный SVR не найден")
        with tempfile.TemporaryDirectory() as name:
            root = Path(name)
            source_rson = root / "source.rson"
            source_scr = root / "source.scr"
            source_lang = root / "source.txt"
            recovered = root / "recovered.rson"
            self.chain.convert_script_project(source_svr, source_rson)
            self.chain.compile_rson(source_rson, source_scr, source_lang)
            source_bytes = source_scr.read_bytes()
            staged_before = {path.name for path in rscript.parent.glob("_srhd_*")}
            stdout = StringIO()

            with redirect_stdout(stdout):
                exit_code = main(
                    [
                        "script",
                        "decompile",
                        str(source_scr),
                        str(recovered),
                        "--deep-roundtrip",
                        "--json",
                    ]
                )

            result = json.loads(stdout.getvalue())
            self.assertEqual(exit_code, 0)
            self.assertTrue(result["verified"])
            self.assertTrue(result["roundtrip"]["exact_binary_match"])
            self.assertTrue(result["deep_roundtrip"]["verified"])
            self.assertEqual(
                {
                    key: result["deep_roundtrip"]["project"][key]
                    for key in ("objects", "links", "code_lines", "types")
                },
                {
                    key: result["recovered_project"][key]
                    for key in ("objects", "links", "code_lines", "types")
                },
            )
            self.assertFalse(result["dialogs_imported"])
            self.assertEqual(source_scr.read_bytes(), source_bytes)
            self.assertEqual(load_rson(recovered).validate(), [])
            self.assertEqual(list(root.glob(".srhd-*")), [])
            self.assertTrue(
                all(issue["path"] == str(recovered.resolve()) for issue in result["runtime_issues"])
            )
            self.assertEqual(
                {path.name for path in rscript.parent.glob("_srhd_*")},
                staged_before,
            )


if __name__ == "__main__":
    unittest.main()

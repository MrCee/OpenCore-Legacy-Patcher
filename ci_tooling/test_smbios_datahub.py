"""SMBIOS-stage regression checks without loading the GUI or probing the host.

Run: python3 -m unittest discover -s ci_tooling -p 'test_smbios_datahub.py' -v
These checks exercise the real builder, model database and config template. They
do not replace a full OpenCore build/ocvalidate run or hardware boot testing.
"""

import importlib
import plistlib
import sys
import types
import unittest

from pathlib import Path
from types import SimpleNamespace
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
PACKAGE = "_oclp_smbios_datahub_test"

# Import the builder under an isolated namespace: the application package's
# __init__ loads the GUI. Hardware access, firmware conversion and type-only
# constants are mocked; identity lookup and config generation use real code/data.
for name in ("", ".datasets", ".detections", ".support", ".efi_builder"):
    module = types.ModuleType(PACKAGE + name)
    module.__path__ = [str(ROOT / "opencore_legacy_patcher" / name.lstrip(".").replace(".", "/"))]
    sys.modules[module.__name__] = module

ioreg = types.ModuleType(PACKAGE + ".detections.ioreg")
ioreg.io_registry_entry_t = object
sys.modules[ioreg.__name__] = ioreg

utilities = types.ModuleType(PACKAGE + ".support.utilities")
utilities.get_rom = mock.Mock(side_effect=AssertionError("Unexpected host ROM access"))
utilities.string_to_hex = mock.Mock(return_value=b"firmware-fixture")
sys.modules[utilities.__name__] = utilities

constants_module = types.ModuleType(PACKAGE + ".constants")
constants_module.Constants = object
sys.modules[constants_module.__name__] = constants_module

smbios = importlib.import_module(PACKAGE + ".efi_builder.smbios")


def build_smbios(model, serial_settings="None", override="Default", host="iMac14,2"):
    """Run set_smbios with the stock template and no filesystem postprocessing."""
    with (ROOT / "payloads/Config/config.plist").open("rb") as stream:
        config = plistlib.load(stream)
    constants = SimpleNamespace(
        serial_settings=serial_settings,
        override_smbios=override,
        allow_oc_everywhere=True,
        allow_native_spoofs=False,
        custom_serial_number="",
        custom_board_serial_number="",
        custom_model=model,
        computer=SimpleNamespace(real_model=host),
    )
    # Supply serial fixtures so Advanced never invokes the external macserial.
    if serial_settings == "Advanced":
        constants.custom_serial_number = "SERIAL-FIXTURE"
        constants.custom_board_serial_number = "MLB-FIXTURE"
    builder = smbios.BuildSMBIOS(model, constants, config)
    builder.set_smbios()
    return config


class DataHubTests(unittest.TestCase):
    def test_all_supported_targets_have_a_board_id_in_the_bundled_database(self):
        for model in smbios.model_array.SupportedSMBIOS:
            with self.subTest(model=model):
                entry = smbios.smbios_data.smbios_dictionary[model]
                self.assertIsInstance(entry["Board ID"], str)
                self.assertTrue(entry["Board ID"])

    def test_all_supported_targets_preserve_spoofing_mode_identity_rules(self):
        for model in smbios.model_array.SupportedSMBIOS:
            for mode in ("None", "Minimal", "Moderate", "Advanced"):
                with self.subTest(model=model, mode=mode):
                    config = build_smbios(model, mode)
                    platform = config["PlatformInfo"]
                    datahub = platform["DataHub"]
                    entry = smbios.smbios_data.smbios_dictionary[model]
                    automatic = mode in ("Moderate", "Advanced")
                    self.assertEqual(platform["Automatic"], automatic)
                    self.assertEqual(config["UEFI"]["ProtocolOverrides"]["DataHub"], automatic)
                    if automatic:
                        spoof = smbios.generate_smbios.set_smbios_model_spoof(model)
                        self.assertEqual(platform["Generic"]["SystemProductName"], spoof)
                        self.assertEqual(datahub["SystemProductName"], "")
                        self.assertEqual(datahub["BoardProduct"], "")
                        self.assertTrue(platform["UpdateDataHub"])
                    elif mode == "Minimal":
                        spoof = smbios.generate_smbios.set_smbios_model_spoof(model)
                        self.assertEqual(datahub["SystemProductName"], model)
                        self.assertEqual(datahub["BoardProduct"], smbios.smbios_data.smbios_dictionary[spoof]["Board ID"])
                        self.assertTrue(platform["UpdateDataHub"])
                    else:
                        older = entry["CPU Generation"] <= smbios.cpu_data.CPUGen.ivy_bridge.value
                        affected = model == "MacBookPro11,3"
                        self.assertEqual(datahub["SystemProductName"], model if affected else "")
                        self.assertEqual(datahub["BoardProduct"], entry["Board ID"] if older or affected else "")
                        self.assertEqual(platform["UpdateDataHub"], older or affected)

    def test_mbp113_uses_target_identity_on_either_build_host(self):
        for host in ("MacBookPro11,3", "iMac14,2"):
            with self.subTest(host=host):
                config = build_smbios("MacBookPro11,3", host=host)
                platform = config["PlatformInfo"]
                self.assertEqual(platform["DataHub"]["SystemProductName"], "MacBookPro11,3")
                self.assertEqual(platform["DataHub"]["BoardProduct"], "Mac-2BD1B31983FE1663")
                self.assertTrue(platform["UpdateDataHub"])
                self.assertFalse(platform["Automatic"])
                self.assertFalse(platform["UpdateSMBIOS"])
                self.assertFalse(platform["UpdateNVRAM"])
                self.assertFalse(config["UEFI"]["ProtocolOverrides"]["DataHub"])

    def test_explicit_override_keeps_model_and_board_together(self):
        platform = build_smbios("MacBookPro11,3", override="MacBookPro14,3")["PlatformInfo"]
        self.assertEqual(platform["DataHub"]["SystemProductName"], "MacBookPro14,3")
        self.assertEqual(platform["DataHub"]["BoardProduct"], smbios.smbios_data.smbios_dictionary["MacBookPro14,3"]["Board ID"])

    def test_other_haswell_and_newer_models_do_not_gain_the_workaround(self):
        for model in ("MacBookPro11,1", "MacBookPro11,2", "iMac14,2", "Macmini7,1", "MacBookPro12,1"):
            with self.subTest(model=model):
                platform = build_smbios(model)["PlatformInfo"]
                self.assertEqual(platform["DataHub"]["SystemProductName"], "")
                self.assertEqual(platform["DataHub"]["BoardProduct"], "")
                self.assertFalse(platform["UpdateDataHub"])

    def test_older_models_retain_the_existing_board_only_workaround(self):
        for model in ("MacBookPro8,1", "MacBookPro9,2", "MacBookPro10,1"):
            with self.subTest(model=model):
                platform = build_smbios(model)["PlatformInfo"]
                self.assertEqual(platform["DataHub"]["SystemProductName"], "")
                self.assertEqual(platform["DataHub"]["BoardProduct"], smbios.smbios_data.smbios_dictionary[model]["Board ID"])
                self.assertTrue(platform["UpdateDataHub"])

    def test_minimal_mode_keeps_its_existing_model_identity(self):
        platform = build_smbios("MacBookPro11,3", "Minimal")["PlatformInfo"]
        self.assertEqual(platform["DataHub"]["SystemProductName"], "MacBookPro11,3")
        self.assertEqual(platform["DataHub"]["BoardProduct"], smbios.smbios_data.smbios_dictionary["MacBookPro14,3"]["Board ID"])
        self.assertTrue(platform["UpdateDataHub"])
        self.assertTrue(platform["UpdateSMBIOS"])

    def test_moderate_and_advanced_modes_keep_generic_identity(self):
        for setting in ("Moderate", "Advanced"):
            with self.subTest(setting=setting):
                platform = build_smbios("MacBookPro11,3", setting)["PlatformInfo"]
                self.assertEqual(platform["DataHub"]["SystemProductName"], "")
                self.assertEqual(platform["DataHub"]["BoardProduct"], "")
                self.assertEqual(platform["Generic"]["SystemProductName"], "MacBookPro14,3")
                self.assertTrue(platform["Automatic"])


if __name__ == "__main__":
    unittest.main()

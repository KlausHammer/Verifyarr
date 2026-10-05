"""The startup GPU probe reads whisper.cpp's Vulkan lines."""
import unittest

from verifyarr import gpu


class Devices(unittest.TestCase):
    def test_found(self):
        err = "ggml_vulkan: Found 1 Vulkan devices:\nggml_vulkan: 0 = Intel(R) UHD Graphics 630 (CFL GT2) | uma: 1 | fp16: 1\n"
        self.assertEqual(gpu._devices(err), ["Intel(R) UHD Graphics 630 (CFL GT2)"])

    def test_none(self):
        self.assertEqual(gpu._devices("ggml_vulkan: No devices found.\n"), [])


if __name__ == "__main__":
    unittest.main()

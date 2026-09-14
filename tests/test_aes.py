"""Known-answer tests for the bundled AES-128 implementation.

The CLI decrypts Chrome's cookie blobs itself rather than depending on a
compiled crypto library, so the inverse cipher is pinned to the FIPS-197
vectors and the CBC chaining to an RFC 3602 vector.
"""

from __future__ import annotations

import unittest

from arcbench_cli.aes import SBOX, decrypt_block, decrypt_cbc, expand_key, strip_pkcs7


class AesTests(unittest.TestCase):
    def test_sbox_matches_the_published_table(self) -> None:
        self.assertEqual(SBOX[0x00], 0x63)
        self.assertEqual(SBOX[0x53], 0xED)
        self.assertEqual(SBOX[0xFF], 0x16)
        self.assertEqual(len(set(SBOX)), 256)

    def test_fips197_c1_vector(self) -> None:
        key = bytes.fromhex("000102030405060708090a0b0c0d0e0f")
        cipher = bytes.fromhex("69c4e0d86a7b0430d8cdb78070b4c55a")
        plain = bytes.fromhex("00112233445566778899aabbccddeeff")
        self.assertEqual(decrypt_block(cipher, expand_key(key)), plain)

    def test_fips197_appendix_b_vector(self) -> None:
        key = bytes.fromhex("2b7e151628aed2a6abf7158809cf4f3c")
        cipher = bytes.fromhex("3925841d02dc09fbdc118597196a0b32")
        plain = bytes.fromhex("3243f6a8885a308d313198a2e0370734")
        self.assertEqual(decrypt_block(cipher, expand_key(key)), plain)

    def test_rfc3602_cbc_vector(self) -> None:
        key = bytes.fromhex("06a9214036b8a15b512e03d534120006")
        iv = bytes.fromhex("3dafba429d9eb430b422da802c9fac41")
        cipher = bytes.fromhex("e353779c1079aeb82708942dbe77181a")
        self.assertEqual(decrypt_cbc(cipher, key, iv), b"Single block msg")

    def test_cbc_chains_across_blocks(self) -> None:
        key = bytes.fromhex("000102030405060708090a0b0c0d0e0f")
        iv = bytes(range(16))
        cipher = bytes.fromhex(
            "69c4e0d86a7b0430d8cdb78070b4c55a" "69c4e0d86a7b0430d8cdb78070b4c55a"
        )
        plain = decrypt_cbc(cipher, key, iv)
        self.assertEqual(len(plain), 32)
        # The second block is xored with the first ciphertext block, not the IV.
        self.assertNotEqual(plain[:16], plain[16:])

    def test_rejects_wrong_sizes(self) -> None:
        with self.assertRaises(ValueError):
            expand_key(b"short")
        with self.assertRaises(ValueError):
            decrypt_cbc(b"not-a-block", bytes(16), bytes(16))
        with self.assertRaises(ValueError):
            decrypt_cbc(bytes(16), bytes(16), b"short-iv")

    def test_strip_pkcs7_only_removes_plausible_padding(self) -> None:
        self.assertEqual(strip_pkcs7(b"value" + bytes([3, 3, 3])), b"value")
        self.assertEqual(strip_pkcs7(b"value\x00"), b"value\x00")
        self.assertEqual(strip_pkcs7(b""), b"")


if __name__ == "__main__":
    unittest.main()

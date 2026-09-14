"""AES-128-CBC decryption using only the Python standard library.

Chrome stores cookies on macOS as ``v10`` blobs encrypted with AES-128-CBC.
Decrypting them is the single cryptographic operation this CLI performs, so it
is implemented here rather than pulling in a compiled dependency. The code is
the plain FIPS-197 inverse cipher; correctness is pinned by the FIPS-197
known-answer test in the unit suite.
"""

from __future__ import annotations


def _xtime(value: int) -> int:
    """Multiply by x in GF(2^8) with the AES reduction polynomial."""
    return ((value << 1) ^ 0x1B) & 0xFF if value & 0x80 else value << 1


def _multiply(left: int, right: int) -> int:
    product = 0
    while right:
        if right & 1:
            product ^= left
        left = _xtime(left)
        right >>= 1
    return product


def _reciprocal(value: int) -> int:
    if value == 0:
        return 0
    return next(candidate for candidate in range(1, 256) if _multiply(value, candidate) == 1)


def _rotate(value: int, places: int) -> int:
    return ((value << places) | (value >> (8 - places))) & 0xFF


def _affine(value: int) -> int:
    mixed = value
    for places in (1, 2, 3, 4):
        mixed ^= _rotate(value, places)
    return mixed ^ 0x63


SBOX = bytes(_affine(_reciprocal(index)) for index in range(256))
INVERSE_SBOX = bytes(SBOX.index(index) for index in range(256))

BLOCK_SIZE = 16
KEY_SIZE = 16
ROUNDS = 10


def expand_key(key: bytes) -> list[bytes]:
    """Return the 11 round keys of the AES-128 schedule."""
    if len(key) != KEY_SIZE:
        raise ValueError("AES-128 requires a 16-byte key")
    words = [list(key[offset : offset + 4]) for offset in range(0, KEY_SIZE, 4)]
    rcon = 1
    for index in range(4, 4 * (ROUNDS + 1)):
        word = list(words[index - 1])
        if index % 4 == 0:
            word = [SBOX[byte] for byte in word[1:] + word[:1]]
            word[0] ^= rcon
            rcon = _xtime(rcon)
        words.append([previous ^ current for previous, current in zip(words[index - 4], word)])
    return [
        bytes(byte for word in words[step * 4 : step * 4 + 4] for byte in word)
        for step in range(ROUNDS + 1)
    ]


def _add_round_key(state: list[int], round_key: bytes) -> None:
    for index in range(BLOCK_SIZE):
        state[index] ^= round_key[index]


def _inverse_shift_rows(state: list[int]) -> list[int]:
    # Flat state is column-major: index = 4 * column + row.
    return [state[4 * ((column - row) % 4) + row] for column in range(4) for row in range(4)]


def _inverse_mix_columns(state: list[int]) -> list[int]:
    mixed: list[int] = []
    for column in range(4):
        a0, a1, a2, a3 = state[4 * column : 4 * column + 4]
        mixed.extend(
            [
                _multiply(a0, 14) ^ _multiply(a1, 11) ^ _multiply(a2, 13) ^ _multiply(a3, 9),
                _multiply(a0, 9) ^ _multiply(a1, 14) ^ _multiply(a2, 11) ^ _multiply(a3, 13),
                _multiply(a0, 13) ^ _multiply(a1, 9) ^ _multiply(a2, 14) ^ _multiply(a3, 11),
                _multiply(a0, 11) ^ _multiply(a1, 13) ^ _multiply(a2, 9) ^ _multiply(a3, 14),
            ]
        )
    return mixed


def decrypt_block(block: bytes, round_keys: list[bytes]) -> bytes:
    if len(block) != BLOCK_SIZE:
        raise ValueError("AES operates on 16-byte blocks")
    state = list(block)
    _add_round_key(state, round_keys[ROUNDS])
    for step in range(ROUNDS - 1, -1, -1):
        state = [INVERSE_SBOX[byte] for byte in _inverse_shift_rows(state)]
        _add_round_key(state, round_keys[step])
        if step:
            state = _inverse_mix_columns(state)
    return bytes(state)


def decrypt_cbc(ciphertext: bytes, key: bytes, iv: bytes) -> bytes:
    """Decrypt AES-128-CBC without removing padding."""
    if len(iv) != BLOCK_SIZE:
        raise ValueError("AES-CBC requires a 16-byte initialisation vector")
    if not ciphertext or len(ciphertext) % BLOCK_SIZE:
        raise ValueError("Ciphertext length must be a non-zero multiple of 16 bytes")
    round_keys = expand_key(key)
    plaintext = bytearray()
    previous = iv
    for offset in range(0, len(ciphertext), BLOCK_SIZE):
        block = ciphertext[offset : offset + BLOCK_SIZE]
        decrypted = decrypt_block(block, round_keys)
        plaintext.extend(byte ^ mask for byte, mask in zip(decrypted, previous))
        previous = block
    return bytes(plaintext)


def strip_pkcs7(plaintext: bytes) -> bytes:
    if plaintext and 1 <= plaintext[-1] <= BLOCK_SIZE:
        return plaintext[: -plaintext[-1]]
    return plaintext

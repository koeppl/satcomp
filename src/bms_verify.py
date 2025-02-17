from dataclasses import dataclass
from typing import List, NewType, Tuple

import sys
from satcomp.measure import BiDirType, BiDirExp
import satcomp.base as io



# def bd_info(bd: BiDirType, text: bytes) -> str:
#
#     return "\n".join(
#         [
#             f"len={len(bd)}: factors={bd}",
#             f"len of text = {len(text)}",
#             f"decode={decode_bms(bd)}",
#             f"equals original? {decode_bms(bd)==text}",
#         ]
#     )


def decode_len(factors: BiDirType) -> int:
    """
    Computes the length of decoded string from a given bidirectional scheme.
    """
    res = 0
    for f in factors:
        res += 1 if f[0] == -1 else f[1]
    return res


def decode_bms(factors: BiDirType) -> bytes:
    """
    Computes the decoded string from a given bidirectional scheme.
    """
    n = decode_len(factors)
    res = [-1 for _ in range(n)]
    nfs = len(factors)
    fbegs = []
    is_decoded = [False for _ in factors]
    n_decoded_fs = 0
    pos = 0
    for i, f in enumerate(factors):
        fbegs.append(pos)
        if f[0] == -1:
            is_decoded[i] = True
            n_decoded_fs += 1
            res[pos] = f[1]
            pos += 1
        else:
            pos += f[1]

    while n_decoded_fs < nfs:
        for fi, f in enumerate(factors):
            refi, reflen = f
            if is_decoded[fi]:
                continue
            pos = fbegs[fi]
            count = 0
            for j in range(reflen):
                if res[refi + j] != -1:
                    count += 1
                    res[pos + j] = res[refi + j]
            if reflen == count:
                is_decoded[fi] = True
                n_decoded_fs += 1

    return bytes(res)

def test_decode():
    factors_naive = BiDirType([(13, 8), (0, 11), (-1, 98), (-1, 97)])
    factors_sol = BiDirType([(8, 8), (13, 8), (-1, 97), (-1, 98), (16, 3)])
    print(decode_bms(factors_naive))
    print(decode_bms(factors_sol))
    assert (decode_bms(factors_naive) == decode_bms(factors_sol))

def is_bms(text: bytes, output : BiDirType) -> bool:
    decoded = decode_bms(output)
    return decoded == text

if __name__ == "__main__":
    sys.exit(io.verify_functor(is_bms, 'Verify a computed BMS'))

import functools
import math
import time
from enum import auto
from logging import CRITICAL, DEBUG, INFO, Formatter, StreamHandler, getLogger
from typing import Optional


import satcomp.base as io
import satcomp.stralgo as stralgo
from satcomp.solver import MaxSatWrapper
from satcomp.measure import SLPType, SLPExp

from pysat.card import CardEnc
from pysat.examples.rc2 import RC2
from pysat.formula import WCNF

from satcomp.satencoding import (
    Enum,
    Literal,
    LiteralManager,
    pysat_and,
    pysat_atleast_one,
    pysat_if,
    pysat_iff,
    pysat_name_cnf,
    pysat_or,
)

logger = getLogger(__name__)
handler = StreamHandler()
handler.setLevel(DEBUG)
FORMAT = "[%(lineno)s - %(funcName)10s() ] %(message)s"
formatter = Formatter(FORMAT)
handler.setFormatter(formatter)
logger.addHandler(handler)


class SLPLiteral(Enum):
    true = Literal.true
    false = Literal.false
    auxlit = Literal.auxlit
    phrase = auto()  # (i,l) (representing T[i:i+l)) is phrase of grammar parsing
    pstart = auto()  # i is a starting position of a phrase of grammar parsing
    ref = auto()  # (j,i,l): phrase (j,j+l) references T[i,i+l)  (T[i,i+l] <- T[j,j+l])
    referred = auto()  # (i,l): T[i,i+l) is referenced by some phrase
    # vY(L,R): true if referred(i,l) = 1 for some i,l such that
    # L = i // blocksize, R = (i+l-1) // blocksize
    vY = auto()
    # vE(i,R): true if referred(i,l) = 1 for some l such that T[i..i+l) ends in block R (i+l-1)//blocksize = R
    vE = auto()  # (i,R):
    # vS(L,j): true if referred(i,l) = 1 for some l such that i+l-1=j starts in block L (i//blocksize) = L
    vS = auto()  # (L,j):


class SLPLiteralManager(LiteralManager):
    """
    Manage literals used for solvers.
    """

    def __init__(self, text: bytes):
        self.text = text
        self.n = len(self.text)
        self.lits = SLPLiteral
        self.verifyf = {
            SLPLiteral.phrase: self.verify_phrase,
            SLPLiteral.pstart: self.verify_pstart,
            SLPLiteral.ref: self.verify_ref,
            SLPLiteral.referred: self.verify_referred,
        }
        super().__init__(self.lits)

    def newid(self, *obj) -> int:
        res = super().newid(*obj)
        if len(obj) > 0 and obj[0] in self.verifyf:
            self.verifyf[obj[0]](*obj)
        return res

    def verify_phrase(self, *obj):
        # obj = (name, i, l) (representing T[i:i+l)) is phrase of grammar parsing
        assert len(obj) == 3
        name, i, l = obj
        assert name == self.lits.phrase
        assert 0 <= i < self.n
        assert 0 < l <= self.n
        assert i + l <= self.n

    def verify_pstart(self, *obj):
        # print(f"verify_pstart, {obj}")
        # obj = (name, i) i is a starting position of a phrase of grammar parsing
        assert len(obj) == 2
        name, i = obj
        assert name == self.lits.pstart
        assert 0 <= i <= self.n

    def verify_ref(self, *obj):
        # print(f"verify_ref, {obj}")
        # obj = (name, j, i, l) phrase (j,j+l) references T[i,i+l)  (T[i,i+l] <- T[j,j+l])
        assert len(obj) == 4
        name, j, i, l = obj
        assert name == self.lits.ref
        assert 0 <= i < self.n
        assert i < i + l <= j < j + l <= self.n
        assert 0 < l <= self.n

    def verify_referred(self, *obj):
        # print(f"verify_referred, {obj}")
        # obj = (name, i, l) T[i,i+l) is referenced by some phrase
        assert len(obj) == 3
        name, i, l = obj
        assert name == self.lits.referred
        assert 0 <= i < self.n
        assert 0 < l <= self.n
        assert i + l <= self.n

def smallest_SLP_WCNF(text: bytes):
    """
    Compute the max sat formula for computing the smallest SLP
    """
    n = len(text)
    # print(f"n = {n}")
    logger.info(f"text length = {len(text)}")
    wcnf = WCNF()

    lm = SLPLiteralManager(text)
    # print("sloooow algorithm for lpf... (should use linear time algorithm)")
    lpf = stralgo.compute_lpnf(text)

    # defining the literals  ########################################
    # ref(i,j,l): defined for all i,j,l>1 s.t. T[i:i+l) = T[j:j+l)
    # pstart(i)
    # phrase(i,l) defined for all i, l > 1 with l <= lpf[i]
    phrases = []
    for i in range(n + 1):
        lm.newid(lm.lits.pstart, i)  # definition of p_i
    for i in range(n):
        for l in range(1, max(2, lpf[i] + 1)):
            phrases.append((i, l))
            lm.newid(lm.lits.phrase, i, l)  # definition of f_{i,l}

    refs_by_referred = {}
    refs_by_referrer = {}
    for i in range(n):
        for j in range(i + 1, n):
            for l in range(2, lpf[j] + 1):
                if i + l <= j and text[i : i + l] == text[j : j + l]:
                    lm.newid(lm.lits.ref, j, i, l)  # definition of ref_{i<-j,l}
                    if not (i, l) in refs_by_referred:
                        refs_by_referred[i, l] = []
                    refs_by_referred[i, l].append(j)
                    if not (j, l) in refs_by_referrer:
                        refs_by_referrer[j, l] = []
                    refs_by_referrer[j, l].append(i)
    for i, l in refs_by_referred.keys():
        lm.newid(lm.lits.referred, i, l)

    # // start constraint (1) ###############################
    # phrase(i,l) = true <=> pstart[i] = pstart[i+l] = true, pstart[i+1..i+l) = false
    for i, l in phrases:
        plst = [-lm.getid(lm.lits.pstart, (i + j)) for j in range(1, l)] + [
            lm.getid(lm.lits.pstart, i),
            lm.getid(lm.lits.pstart, (i + l)),
        ]
        range_iff_startp, clauses = pysat_and(lm.newid, plst)
        wcnf.extend(clauses)
        wcnf.extend(pysat_iff(lm.getid(lm.lits.phrase, i, l), range_iff_startp))

    # there must be at least one new phrase beginning from [i+1,...,i+max(1,lpf[i])]
    for i in range(n):
        if i + 1 == n or lpf[i] - 1 < lpf[i + 1]:
            lst = list(range(i + 1, i + max(1, lpf[i]) + 1))
            wcnf.append([lm.getid(lm.lits.pstart, i) for i in lst])

    # // end constraint (1) ###############################

    # // start constraint (2),(3) ###############################
    # if phrase(j,l) = true there must be exactly one i < j such that ref(j,i,l) is true
    for j, l in refs_by_referrer.keys():
        clauses = CardEnc.atmost(
            [lm.getid(lm.lits.ref, j, i, l) for i in refs_by_referrer[j, l]],
            bound=1,
            vpool=lm.vpool,
        )
        wcnf.extend(clauses)
        clause = pysat_atleast_one(
            [lm.getid(lm.lits.ref, j, i, l) for i in refs_by_referrer[j, l]]
        )
        var_atleast, clause_atleast = pysat_name_cnf(lm, [clause])
        wcnf.extend(clause_atleast)
        phrase = lm.getid(lm.lits.phrase, j, l)
        wcnf.append(pysat_if(phrase, var_atleast))
    # // end constraint (2),(3) ###############################
    # // start constraint (4) ###############################
    for j, l in refs_by_referrer.keys():
        for i in refs_by_referrer[j, l]:
            wcnf.append(
                pysat_if(
                    lm.getid(lm.lits.ref, j, i, l),  # ref_{i<-j,l}
                    lm.getid(lm.lits.phrase, j, l),  # f_{j,l}
                )
            )
    # // end constraint (4) ###############################

    # // start constraint (5) ###############################
    # referred(i,l) = true iff there is some j > i such that ref(j,i,l) = true
    for i, l in refs_by_referred.keys():
        assert l > 1
        ref_sources, clauses = pysat_or(
            lm.newid,
            [lm.getid(lm.lits.ref, j, i, l) for j in refs_by_referred[i, l]],
        )
        wcnf.extend(clauses)
        referredid = lm.getid(lm.lits.referred, i, l)
        wcnf.extend(
            pysat_iff(ref_sources, referredid)
        )  # q_{i,l} <=> \exists ref_{i<-j,l}
    # // end constraint (5) ###############################

    # // start constraint (6) ###############################
    # if (occ,l) is a referred interval, it cannot be a phrase, but pstart[occ] and pstart[occ+l] must be true
    # phrase(occ,l) is only defined if l <= lpf[occ]
    referred = list(refs_by_referred.keys())
    for occ, l in referred:
        if l > 1:
            qid = lm.getid(lm.lits.referred, occ, l)
            lst = [-qid] + [lm.getid(lm.lits.pstart, occ + x) for x in range(1, l)]
            wcnf.append(lst)
            wcnf.append(pysat_if(qid, lm.getid(lm.lits.pstart, occ)))
            wcnf.append(pysat_if(qid, lm.getid(lm.lits.pstart, occ + l)))
    # // end constraint (6) ###############################

    # // start constraint (7) ###############################
    # crossing intervals cannot be referred to at the same time.
    # new $O(N^{8/3}) size implementation proposed by Bernardo
    # print(f"text={text}")
    blocksize = math.ceil(n ** (1.0 / 3))
    numblocks = math.ceil(n / blocksize)
    # print(f"blocksize = {blocksize}, numblocks = {numblocks}")

    referred_by_bp = [[] for _ in range(n)]
    for occ, l in referred:
        referred_by_bp[occ].append(l)
    for lst in referred_by_bp:
        lst.sort(reverse=True)

    ref_by_blkse = {}
    e_by_blkse = {}
    s_by_blkse = {}

    for occ, l in referred:
        sblk = occ // blocksize
        eblk = (occ + l - 1) // blocksize
        try:
            ref_by_blkse[(sblk, eblk)].add((occ, l))
        except KeyError:
            ref_by_blkse[(sblk, eblk)] = {(occ, l)}

        if not lm.contains(lm.lits.vY, sblk, eblk):
            lm.newid(lm.lits.vY, sblk, eblk)
            # print(f"vY({sblk},{eblk})")

        if not lm.contains(lm.lits.vE, occ, eblk):
            lm.newid(lm.lits.vE, occ, eblk)
            try:
                e_by_blkse[(sblk, eblk)].add(occ)
            except KeyError:
                e_by_blkse[(sblk, eblk)] = {occ}
            # print(f"vE({occ},{eblk})")

        if not lm.contains(lm.lits.vS, sblk, occ + l - 1):
            lm.newid(lm.lits.vS, sblk, occ + l - 1)
            try:
                s_by_blkse[(sblk, eblk)].add(occ + l - 1)
            except KeyError:
                s_by_blkse[(sblk, eblk)] = {occ + l - 1}
            # print(f"vS({sblk},{occ + l - 1})")

    for occ, l in referred:
        # print(f"referred({occ},{l})")
        idx = lm.getid(lm.lits.referred, occ, l)
        sblk = occ // blocksize
        eblk = (occ + l - 1) // blocksize
        idy = lm.getid(lm.lits.vY, sblk, eblk)
        wcnf.append([-idx, idy])
        # print(f"referred({occ},{l}) -> vY({sblk},{eblk})")
        ide = lm.getid(lm.lits.vE, occ, (occ + l - 1) // blocksize)
        ids = lm.getid(lm.lits.vS, occ // blocksize, occ + l - 1)
        wcnf.append([-idx, ide])  # referred(i,l) -> vE(i,R)
        # print(f"referred({occ},{l}) -> vE({occ},{eblk})")
        wcnf.append([-idx, ids])  # referred(i,l) -> vS(L,j)
        # print(f"referred({occ},{l}) -> vS({sblk},{occ + l - 1})")

    for L1, R1 in ref_by_blkse.keys():
        for L2, R2 in ref_by_blkse.keys():
            # case: L1 < L2 < R1 < R2
            if L1 < L2 and L2 < R1 and R1 < R2:
                refs1 = ref_by_blkse[(L1, R1)]
                refs2 = ref_by_blkse[(L2, R2)]
                if refs1 and refs2:
                    idy1 = lm.getid(lm.lits.vY, L1, R1)
                    idy2 = lm.getid(lm.lits.vY, L2, R2)
                    # print(f"NOT (vY({L1},{R1}) and vY({L2},{R2}))")
                    wcnf.append([-idy1, -idy2])  # not vY(L1,R1) or not vY(L2,R2)
            # case L1 = L2 <= R1 = R2
            elif L1 == L2 and R1 == R2:
                refs1 = ref_by_blkse[(L1, R1)]
                refs2 = ref_by_blkse[(L2, R2)]
                for occ1, l1 in refs1:
                    for occ2, l2 in refs2:
                        if (
                            occ1 < occ2
                            and occ2 <= occ1 + l1 - 1
                            and occ1 + l1 < occ2 + l2
                        ):
                            idx1 = lm.getid(lm.lits.referred, occ1, l1)
                            idx2 = lm.getid(lm.lits.referred, occ2, l2)
                            # print(
                            #     f"NOT (referred({occ1},{l1}) and referred({occ2},{l2}))"
                            # )
                            wcnf.append([-idx1, -idx2])
            # case L1 = L2 < R1 < R2
            elif L1 == L2 and L2 < R1 and R1 < R2:
                elst1 = e_by_blkse[(L1, R1)]
                elst2 = e_by_blkse[(L2, R2)]
                for occ1 in elst1:
                    for occ2 in elst2:
                        if occ1 < occ2:
                            ide1 = lm.getid(lm.lits.vE, occ1, R1)
                            ide2 = lm.getid(lm.lits.vE, occ2, R2)
                            # print(f"NOT (vE({occ1},{R1}) and vE({occ2},{R2}))")
                            wcnf.append([-ide1, -ide2])
            # case L1 < L2 < R1 = R2
            elif L1 < L2 and L2 < R1 and R1 == R2:
                slst1 = s_by_blkse[(L1, R1)]
                slst2 = s_by_blkse[(L2, R2)]
                for j1 in slst1:
                    for j2 in slst2:
                        if j1 < j2:
                            ids1 = lm.getid(lm.lits.vS, L1, j1)
                            ids2 = lm.getid(lm.lits.vS, L2, j2)
                            # print(f"NOT (vS({L1},{j1}) and vS({L2},{j2}))")
                            wcnf.append([-ids1, -ids2])
            # case L1 < L2 = R1 < R2
            elif L1 < L2 and L2 == R1 and R1 < R2:
                slst1 = s_by_blkse[(L1, R1)]
                elst2 = e_by_blkse[(L2, R2)]
                for j1 in slst1:
                    for occ2 in elst2:
                        if occ2 <= j1:
                            ids = lm.getid(lm.lits.vS, L1, j1)
                            ide = lm.getid(lm.lits.vE, occ2, R2)
                            # print(f"NOT (vS({L1},{j1}) and vE({occ2},{R2}))")
                            wcnf.append([-ids, -ide])
            # case L1 = L2 = R1 < R2
            elif L1 == L2 and L2 == R1 and R1 < R2:
                refs1 = ref_by_blkse[(L1, R1)]
                elst2 = e_by_blkse[(L2, R2)]
                for occ1, l1 in refs1:
                    for occ2 in elst2:
                        if occ1 < occ2 and occ2 <= occ1 + l1 - 1:
                            idx = lm.getid(lm.lits.referred, occ1, l1)
                            ide = lm.getid(lm.lits.vE, occ2, R2)
                            # print(f"NOT (referred({occ1},{l1}) and vE({occ2},{R2}))")
                            wcnf.append([-idx, -ide])
            # case L1 < L2 = R1 = R2
            elif L1 < L2 and L2 == R1 and R1 == R2:
                slst1 = s_by_blkse[(L1, R1)]
                refs2 = ref_by_blkse[(L2, R2)]
                for j1 in slst1:
                    for occ2, l2 in refs2:
                        if occ2 <= j1 and j1 < occ2 + l2 - 1:
                            ids = lm.getid(lm.lits.vS, L1, j1)
                            idx = lm.getid(lm.lits.referred, occ2, l2)
                            # print(f"NOT (vS({L1},{j1}) and referred({occ2},{l2}))")
                            wcnf.append([-ids, -idx])
    # # // end constraint (7) ###############################

    # // start constraint (9) ###############################
    for i in range(0, n):
        if lpf[i] == 0:
            wcnf.append([lm.getid(lm.lits.phrase, i, 1)])  # perhaps not needed
            pass

    wcnf.append([lm.getid(lm.lits.pstart, 0)])
    wcnf.append([lm.getid(lm.lits.pstart, n)])
    # // end constraint (9) ###############################

    # soft clauses: minimize # of phrases
    for i in range(0, n):
        wcnf.append([-lm.getid(lm.lits.pstart, i)], weight=1)

    return lm, wcnf, phrases, refs_by_referrer


def postorder_cmp(x, y):
    i1 = x[0]
    j1 = x[1]
    i2 = y[0]
    j2 = y[1]
    # print(f"compare: {x} vs {y}")
    if i1 == i2 and i2 == j2:
        return 0
    if j1 <= i2:
        return -1
    elif j2 <= i1:
        return 1
    elif i1 <= i2 and j2 <= j1:
        return 1
    elif i2 <= i1 and j1 <= j2:
        return -1
    else:
        assert False


# given a list of nodes that in postorder of subtree rooted at root,
# find the direct children of [root_i,root_j) and add it to slp
# slp[j,l,i] is a list of nodes that are direct children of [i,j)
def build_slp_aux(nodes, slp):
    root = nodes.pop()
    root_i = root[0]
    # root_j = root[1]
    # print(f"root_i,root_j = {root_i},{root_j}")
    children = []
    while len(nodes) > 0 and nodes[-1][0] >= root_i:
        # print(f"nodes[-1] = {nodes[-1]}")
        c = build_slp_aux(nodes, slp)
        children.append(c)
    children.reverse()
    slp[root] = children
    ##########################################################
    return root


# turn multi-ary tree into binary tree
def binarize_slp(root, slp):
    children = slp[root]
    numc = len(children)
    assert numc == 0 or numc >= 2
    if numc == 2:
        slp[root] = (binarize_slp(children[0], slp), binarize_slp(children[1], slp))
    elif numc > 0:
        leftc = children[0]
        for i in range(1, len(children)):
            n = (root[0], children[i][1], None) if i < len(children) - 1 else root
            slp[n] = (leftc, children[i])
            leftc = n
        for c in children:
            binarize_slp(c, slp)
    else:
        slp[root] = None
    return root


def slp2str(root, slp):
    # print(f"root={root}")
    res = []
    (i, j, ref) = root
    if j - i == 1:
        res.append(ref)
    else:
        children = slp[root]
        if ref is None:
            assert len(children) == 2
            res += slp2str(children[0], slp)
            res += slp2str(children[1], slp)
        else:
            assert children is None
            n = (ref, ref + j - i, None)
            res += slp2str(n, slp)
    return res


def recover_slp(text: bytes, pstartl, refs_by_referrer):
    n = len(text)
    referred = set((refs_by_referrer[j, l], l) for (j, l) in refs_by_referrer.keys())
    leaves = [(j, j + l, refs_by_referrer[j, l]) for (j, l) in refs_by_referrer.keys()]
    for i in range(len(pstartl) - 1):
        if pstartl[i + 1] - pstartl[i] == 1:
            leaves.append((pstartl[i], pstartl[i + 1], text[pstartl[i]]))
    internal = [(occ, occ + l, None) for (occ, l) in referred]
    nodes = leaves + internal
    if len(nodes) > 1:
        nodes.append((0, n, None))

    nodes.sort(key=functools.cmp_to_key(postorder_cmp))
    slp = {}
    root = build_slp_aux(nodes, slp)
    binarize_slp(root, slp)
    return (root, slp)


def smallest_SLP(text: bytes, exp: Optional[SLPExp] = None) -> SLPType:
    """
    Compute the smallest SLP.
    """
    total_start = time.time()
    lm, wcnf, phrases, refs_by_referrer = smallest_SLP_WCNF(text)
    io.dump_wcnf_and_exit(wcnf, args.dump)

    solver = MaxSatWrapper(args.strategy, args.solver, wcnf, args.timeout, args.verbose, logger)

    time_prep = time.time() - total_start
    solver.compute()
    sol_ = solver.model
    assert sol_ is not None
    sol = set(sol_)

    n = len(text)

    posl = []
    for i in range(0, n + 1):
        x = lm.getid(lm.lits.pstart, i)
        if x in sol:
            posl.append(i)
    # print(f"posl={posl}")
    phrasel = []
    for occ, l in phrases:
        x = lm.getid(lm.lits.phrase, occ, l)
        if x in sol:
            phrasel.append((occ, occ + l))
    # print(f"phrasel={phrasel}")
    refs = {}
    for j, l in refs_by_referrer.keys():
        for i in refs_by_referrer[j, l]:
            if lm.getid(lm.lits.ref, j, i, l) in sol:
                refs[j, l] = i
    root, slp = recover_slp(text, posl, refs)
    # print(f"root={root}, slp = {slp}, slpkeys={slp.keys()}")

    slpsize = len(posl) - 2 + len(set(text))

    if exp:
        exp.is_satisfied = solver.is_satisfied
        exp.is_optimal = solver.found_optimum
        exp.time_total = time.time() - total_start
        exp.time_prep = time_prep
        exp.output = f"{(root, slp)}"
        exp.output_size = slpsize  # len(internal_nodes) + len(set(text))
        exp.fill(wcnf)

    check = bytes(slp2str(root, slp))
    assert check == text

    return SLPType((root, slp))


if __name__ == "__main__":
    parser = io.solver_parser('compute a minimum straight line program fast')
    args = parser.parse_args()
    logger.setLevel(int(args.loglevel))
    text = io.read_input(args)

    exp = SLPExp.create()
    exp.algo = "slp-sat-fast"
    exp.fill_args(args, text)

    slp = smallest_SLP(text, exp)

    io.write_json(args.output, exp)
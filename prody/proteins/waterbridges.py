# -*- coding: utf-8 -*-

"""This module detects, predicts and analyzes water bridges.
"""

__author__ = 'Karolina Mikulska-Ruminska'
__credits__ = ['Frane Doljanin', 'Karolina Mikulska-Ruminska']
__email__ = ['karolamik@fizyka.umk.pl', 'fdoljanin@pmfst.hr']

import numpy as np
from scipy.sparse import lil_matrix
import matplotlib.pyplot as plt

from itertools import combinations
from collections import deque
from enum import Enum, auto

from prody import LOGGER
from prody.atomic import Atom, Atomic, AtomGroup
from prody.ensemble import Ensemble
from prody.measure import calcAngle, calcDistance
from prody.measure.contacts import findNeighbors
from prody.proteins import writePDB


__all__ = ['calcWaterBridges', 'calcWaterBridgesTrajectory',
           'calcWaterBridgesStatistics', 'calcWaterBridgingResidues',
           'savePDBWaterBridges', 'calcBridgingResiduesHistogram',
           'calcWaterBridgesDistribution']


class ResType(Enum):
    WATER = auto()
    PROTEIN = auto()


class AtomNode:
    def __init__(self, atom, type):
        self.atom = atom
        self.hydrogens = []
        self.type = type
        self.bonds = []
        self.bondedAtoms = []
        self.isVisited = False


class RelationList:
    def __init__(self, numNodes):
        self.nodes = [None for _ in range(numNodes)]

    def resetVisits(self, atomsToReset=[]):
        if atomsToReset:
            for atom in atomsToReset:
                self[atom].isVisited = False

            return

        for node in self.nodes:
            if node:
                node.isVisited = False

    def addNode(self, atom, type):
        self[atom.getIndex()] = AtomNode(atom, type)

        return self[atom]

    def addHBond(self, bond):
        donor, acceptor = bond.donor, bond.acceptor
        self[donor].bonds.append(bond)
        self[acceptor].bonds.append(bond)

        if acceptor.getIndex() not in map(lambda a: a.getIndex(), self[donor].bondedAtoms):
            self[donor].bondedAtoms.append(acceptor)
            self[acceptor].bondedAtoms.append(donor)

    def removeUnbonded(self):
        for i, node in enumerate(self.nodes):
            if node and not node.bonds:
                self[i] = None

    def __getitem__(self, key):
        if isinstance(key, Atom):
            key = key.getIndex()

        return self.nodes[key]

    def __setitem__(self, key, value):
        self.nodes[key] = value


class HBondConstraints:
    def __init__(self, donors, acceptors, angleWW=None, anglePAWD=None, anglePDWA=None):
        self.donors = donors
        self.acceptors = acceptors
        self.angleWW = angleWW
        self.anglePAWD = anglePAWD
        self.anglePDWA = anglePDWA

    def addAngles(self, angleWW, anglePAWD, anglePDWA):
        self.angleWW = angleWW
        self.anglePAWD = anglePAWD
        self.anglePDWA = anglePDWA


class HydrogenBond:
    def __init__(self, donor, acceptor):
        self.donor = donor
        self.acceptor = acceptor

    @staticmethod
    def checkIsHBond(donor, acceptor, constraints):
        if donor.type != ResType.WATER and donor.atom.getName()[0] not in constraints.donors:
            return False
        if acceptor.type != ResType.WATER and acceptor.atom.getName()[0] not in constraints.acceptors:
            return False

        anglesForComb = {
            (ResType.WATER, ResType.WATER): constraints.angleWW,
            (ResType.WATER, ResType.PROTEIN): constraints.anglePAWD,
            (ResType.PROTEIN, ResType.WATER): constraints.anglePDWA,
        }

        angleRange = anglesForComb[(donor.type, acceptor.type)]
        if not angleRange:
            return True

        return HydrogenBond.checkIsHBondAngle(donor, acceptor, angleRange)

    @staticmethod
    def checkIsHBondAngle(donor, acceptor, angleRange):
        for hydrogen in donor.hydrogens:
            bondAngle = calcAngle(donor.atom, hydrogen, acceptor.atom)
            if angleRange[0] < bondAngle < angleRange[1]:
                return True

        return False


def calcBridges(relations, hydrophilicList, method, maxDepth, maxNumResidues):
    waterBridges = []

    for atom in hydrophilicList:
        observedNode = relations[atom]
        if not observedNode:
            continue

        newBridges = []
        if method == 'chain':
            newBridges = getBridgeChain_BFS(
                observedNode, relations, maxDepth+1)
            relations.resetVisits()
        else:
            newBridges = getBridgeForResidue_BFS(
                observedNode, relations, maxDepth, maxNumResidues)

        waterBridges += newBridges

    return waterBridges


def getChainBridgeTuple(bridge):
    i_1, i_2 = bridge[0].getIndex(), bridge[-1].getIndex()
    return (min(i_1, i_2), max(i_1, i_2))


def getUniqueElements(list, key):
    unique, uniqueKeys = [], []
    for element in list:
        elementKey = key(element)
        if elementKey in uniqueKeys:
            continue

        unique.append(element)
        uniqueKeys.append(elementKey)

    return unique


def getBridgeChain_BFS(observedNode, relationsList, maxDepth):
    queue = deque([(observedNode.atom, [])])
    waterBridges = []

    while queue:
        currentAtom, currentPath = queue.popleft()
        currentNode = relationsList[currentAtom]

        if currentNode.isVisited:
            continue

        currentNode.isVisited = True

        if currentNode.type == ResType.PROTEIN and len(currentPath):
            waterBridges.append(currentPath + [currentAtom])
            continue

        if len(currentPath) == maxDepth:
            continue

        for atom in currentNode.bondedAtoms:
            queue.append(
                (atom, currentPath + [currentAtom]))

    return waterBridges


def getBridgeForResidue_BFS(observedNode, relationsList, maxDepth, maxNumResidues):
    waterBridges = []

    for waterAtom in observedNode.bondedAtoms:
        bridgeAtoms = []
        newWaters = []
        queue = deque([(waterAtom, 0)])
        while queue:
            currentAtom, depth = queue.popleft()
            currentNode = relationsList[currentAtom]

            if currentNode.isVisited or (maxDepth and depth > maxDepth):
                continue

            bridgeAtoms.append(currentAtom)

            if currentNode.type == ResType.PROTEIN:
                continue
            if currentNode.type == ResType.WATER:
                currentNode.isVisited = True
                newWaters.append(currentNode.atom)

            for atom in currentNode.bondedAtoms:
                queue.append((atom, depth+1))

        bridgeAtomIndices = list(
            set(map(lambda a: a.getIndex(), bridgeAtoms)))

        numProteinResidues = sum(
            relationsList[atomIndex].type == ResType.PROTEIN for atomIndex in bridgeAtomIndices)
        numResidues = len(bridgeAtomIndices)

        if (not maxNumResidues or numResidues <= maxNumResidues) and numProteinResidues >= 2:
            waterBridges.append(bridgeAtomIndices)
        else:
            relationsList.resetVisits(newWaters)

    return waterBridges


def getInfoOutput(waterBridgesAtomic):
    output = []
    for bridge in waterBridgesAtomic:
        bridgeOutput = []

        for atom in bridge.proteins:
            residueInfo = f"{atom.getResname()}{atom.getResnum()}"
            atomInfo = f"{atom.getName()}_{atom.getIndex()}"
            chainInfo = atom.getChid()
            bridgeOutput += [residueInfo, atomInfo, chainInfo]

        for atom_1, atom_2 in combinations(bridge.proteins, r=2):
            bridgeOutput += [calcDistance(atom_1, atom_2)]

        bridgeOutput += [len(bridge.waters)]
        bridgeOutput += [
            list(map(lambda w: f"{w.getChid()}_{w.getIndex()}", bridge.waters))]

        output.append(bridgeOutput)

    return output


class AtomicOutput:
    def __init__(self, proteins, waters):
        self.proteins = proteins
        self.waters = waters


def getAtomicOutput(waterBridges, relations):
    output = []
    for bridge in waterBridges:
        proteinAtoms, waterAtoms = [], []
        for atomIndex in bridge:
            if relations[atomIndex].type == ResType.PROTEIN:
                proteinAtoms.append(relations[atomIndex].atom)
            else:
                waterAtoms.append(relations[atomIndex].atom)

        output.append(AtomicOutput(proteinAtoms, waterAtoms))

    return output


def getElementsRegex(elements):
    return f'[{"|".join(elements)}].*'


def calcWaterBridges(atoms, **kwargs):
    """Compute water bridges for a protein that has water molecules.

    :arg atoms: Atomic object from which atoms are considered
    :type atoms: :class:`.Atomic`

    :arg method: cluster or chain, where chain find shortest water bridging path between two protein atoms
        default is 'chain'
    :type method: string 'cluster' | 'chain'

    :arg distDA: maximal distance between water/protein donor and acceptor
        default is 3.5
    :type distDA: int, float

    :arg distWR: maximal distance between considered water and any residue
        default is 4
    :type distWR: int, float

    :arg anglePDWA: angle range where protein is donor and water is acceptor
        default is (100, 200)
    :type anglePDWA: (int, int)

    :arg anglePAWD: angle range where protein is acceptor and water is donor
        default is (100, 140)
    :type anglePDWA: (int, int)

    :arg angleWW: angle between water donor/acceptor
        default is (140, 180)
    :type angleWW: (int, int)

    :arg maxDepth: maximum number of waters in chain/depth of residues in cluster
        default is 2
    :type maxDepth: int, None

    :arg maxNumRes: maximum number of water+protein residues in cluster
        default is None
    :type maxNumRes: int, None

    :arg donors: which atoms to count as donors 
        default is ['N', 'O', 'S', 'F']
    :type donors: list

    :arg acceptors: which atoms to count as acceptors 
        default is ['N', 'O', 'S', 'F']
    :type acceptors: list

    :arg output: return information arrays, (protein atoms, water atoms), or just atom indices per bridge
        default is 'atomic'
    :type output: 'info' | 'atomic' | 'indices'
    """

    method = kwargs.pop('method', 'chain')
    distDA = kwargs.pop('distDA', 3.5)
    distWR = kwargs.pop('distWR', 4)
    anglePDWA = kwargs.pop('anglePDWA', (100, 200))
    anglePAWD = kwargs.pop('anglePAWD', (100, 140))
    angleWW = kwargs.pop('angleWW', (140, 180))
    maxDepth = kwargs.pop('maxDepth', 2)
    maxNumResidues = kwargs.pop('maxNumRes', None)
    donors = kwargs.pop('donors', ['N', 'O', 'S', 'F'])
    acceptors = kwargs.pop('acceptors', ['N', 'O', 'S', 'F'])
    outputType = kwargs.pop('output', 'atomic')
    DIST_COVALENT_H = 1.4

    if method not in ['chain', 'cluster']:
        raise TypeError('Method should be chain or cluster.')
    if outputType not in ['info', 'atomic', 'indices']:
        raise TypeError('Output can be info, atomic or indices.')

    relations = RelationList(len(atoms))
    consideredAtoms = ~atoms.select(
        f'water and not within {distWR} of protein')

    waterHydrogens = consideredAtoms.select('water and hydrogen') or []
    waterOxygens = consideredAtoms.select('water and oxygen')
    waterHydroOxyPairs = findNeighbors(
        waterOxygens, DIST_COVALENT_H, waterHydrogens)
    for oxygen in waterOxygens:
        relations.addNode(oxygen, ResType.WATER)
    for pair in waterHydroOxyPairs:
        oxygen, hydrogen, _ = pair
        relations[oxygen].hydrogens.append(hydrogen)

    proteinHydrophilic = consideredAtoms.select(
        f'protein and name "{getElementsRegex(set(donors+acceptors))}" and within {distWR} of water')

    proteinHydrogens = consideredAtoms.select(f'protein and hydrogen') or []
    proteinHydroPairs = findNeighbors(
        proteinHydrophilic, DIST_COVALENT_H, proteinHydrogens)
    for hydrophilic in proteinHydrophilic:
        relations.addNode(hydrophilic, ResType.PROTEIN)
    for pair in proteinHydroPairs:
        hydrophilic, hydrogen, _ = pair
        relations[hydrophilic].hydrogens.append(hydrogen)

    contactingWaters = findNeighbors(waterOxygens, distDA)
    contactingWaterProtein = findNeighbors(
        waterOxygens, distDA, proteinHydrophilic)

    contactingWaterNodes = list(
        map(lambda ww: (relations[ww[0]], relations[ww[1]]), contactingWaters))
    contactingWaterProteinNodes = list(
        map(lambda wp: (relations[wp[0]], relations[wp[1]]), contactingWaterProtein))

    constraints = HBondConstraints(acceptors, donors)
    if len(waterHydrogens) + len(proteinHydrogens):
        constraints.addAngles(angleWW, anglePAWD, anglePDWA)
    else:
        LOGGER.info('No hydrogens detected, angle criteria will not be used.')

    for pair in contactingWaterNodes + contactingWaterProteinNodes:
        for a, b in [(0, 1), (1, 0)]:
            if HydrogenBond.checkIsHBond(pair[a], pair[b], constraints):
                newHBond = HydrogenBond(pair[a].atom, pair[b].atom)
                relations.addHBond(newHBond)

    relations.removeUnbonded()

    waterBridgesWithIndices = calcBridges(
        relations, proteinHydrophilic, method, maxDepth, maxNumResidues)
    if method == 'chain':
        waterBridgesWithIndices = getUniqueElements(
            waterBridgesWithIndices, getChainBridgeTuple)

    LOGGER.info(
        f'{len(waterBridgesWithIndices)} water bridges detected. Call getInfoOutput to convert atomic to info output.')

    atomicOutput = getAtomicOutput(waterBridgesWithIndices, relations)
    infoOutput = getInfoOutput(atomicOutput)
    for bridge in infoOutput:
        LOGGER.info(' '.join(map(str, bridge)))

    if outputType == 'info':
        return infoOutput
    if outputType == 'atomic':
        return atomicOutput

    return waterBridgesWithIndices


# took from interactions.py
def calcWaterBridgesTrajectory(atoms, trajectory, **kwargs):
    """Compute selected type interactions for DCD trajectory or multi-model PDB 
    using default parameters."""

    interactions_all = []
    start_frame = kwargs.pop('start_frame', 0)
    stop_frame = kwargs.pop('stop_frame', -1)

    if trajectory is not None:
        if isinstance(trajectory, Atomic):
            trajectory = Ensemble(trajectory)

        # nfi = trajectory._nfi
        # trajectory.reset()
        numFrames = trajectory._n_csets

        if stop_frame == -1:
            traj = trajectory[start_frame:]
        else:
            traj = trajectory[start_frame:stop_frame+1]

        atoms_copy = atoms.copy()
        for j0, frame0 in enumerate(traj, start=start_frame):
            LOGGER.info('Frame: {0}'.format(j0))
            atoms_copy.setCoords(frame0.getCoords())
            interactions = calcWaterBridges(atoms_copy, **kwargs)
            interactions_all.append(interactions)
        # trajectory._nfi = nfi

    else:
        if atoms.numCoordsets() > 1:
            for i in range(len(atoms.getCoordsets()[start_frame:stop_frame])):
                LOGGER.info('Model: {0}'.format(i+start_frame))
                atoms.setACSIndex(i+start_frame)
                interactions = calcWaterBridges(atoms, **kwargs)
                interactions_all.append(interactions)
        else:
            LOGGER.info('Include trajectory or use multi-model PDB file.')

    return interactions_all


def getResidueName(atom):
    return f'{atom.getResname()}{atom.getIndex()}{atom.getChid()}'


def calcWaterBridgesStatistics(frames, atoms, trajectory, **kwargs):
    """Returns statistics - either dictionary or lil_matrix where indices are atom indices. Value is percentage of bridge appearance of frames for each residue.

    :arg frames: list of water bridges from calcWaterBridgesTrajectory(), output='atomic'
    :type frames: list

    :arg atoms: atoms in PDB struct
    :type atoms:

    :arg output: return 2D matrices or dictionary where key is residue info
        default is 'dict'
    :type output: 'dict' | 'indices'
    """
    outputType = kwargs.pop('output', 'dict')

    allCoordinates = trajectory.getCoordsets()
    numAtoms = len(atoms)
    interactionCount = lil_matrix((numAtoms, numAtoms))
    distanceSum = lil_matrix((numAtoms, numAtoms), dtype=float)

    for frameIndex, frame in enumerate(frames):
        for bridge in frame:
            proteinAtoms = bridge.proteins
            for atom_1, atom_2 in combinations(proteinAtoms, r=2):
                ind_1, ind_2 = atom_1.getIndex(), atom_2.getIndex()

                interactionCount[ind_1, ind_2] += 1
                interactionCount[ind_2, ind_1] += 1

                coords = allCoordinates[frameIndex]
                atom_1_coords, atom_2_coords = coords[ind_1], coords[ind_2]
                distance = calcDistance(atom_1_coords, atom_2_coords)
                distanceSum[ind_1, ind_2] += distance
                distanceSum[ind_2, ind_1] += distance

    interactionPerc, distanceAvg = None, None
    if outputType == 'dict':
        interactionPerc, distanceAvg = {}, {}
    else:
        interactionPerc = lil_matrix((numAtoms, numAtoms), dtype=float)
        distanceAvg = lil_matrix((numAtoms, numAtoms), dtype=float)

    for x, y in zip(*interactionCount.nonzero()):
        if not interactionCount[x, y]:
            continue

        percentage = 100 * interactionCount[x, y]/len(frames)
        average = distanceSum[x, y]/interactionCount[x, y]

        if outputType == 'dict':
            x_resname, y_resname = getResidueName(
                atoms[x]), getResidueName(atoms[y])
            interactionPerc[(x_resname, y_resname)] = percentage
            distanceAvg[(x_resname, y_resname)] = average
        else:
            interactionPerc[x, y] = percentage
            distanceAvg[x, y] = average

    return {
        "interactionPercentage": interactionPerc,
        "distanceAverage": distanceAvg,
    }


def reduceTo1D(list, elementSel=lambda x: x, sublistSel=lambda x: x):
    return [elementSel(element) for sublist in list for element in sublistSel(sublist)]


def calcWaterBridgingResidues(frames, atoms, **kwargs):
    """Returns proteins and waters residues participating in water bridges (proteins are tuples with occupancy).

    :arg frames: list of water bridges from calcWaterBridgesTrajectory(), output='atomic'
    :type frames: list

    :arg atoms: Atomic object from which atoms are considered
    :type atoms: :class:`.Atomic`

    :proteinThreshold: minimum number of residue appearances in bridges per frame (from 0 to 1)
        default is 0.7
    :type proteinThreshold: int

    :waterThreshold: minimum number of water appearances in bridges per frame (from 0 to 1)
        default is 0.7
    :type waterThreshold: int
    """
    proteinThreshold = kwargs.pop('proteinThreshold', 0.7) * len(frames)
    waterThreshold = kwargs.pop('waterThreshold', 0.7) * len(frames)

    proteinAtoms, waterAtoms = np.zeros(len(atoms)), np.zeros(len(atoms))
    for frame in frames:
        frameProteins = set(reduceTo1D(
            frame, lambda p: p.getIndex(), lambda wb: wb.proteins))
        frameWaters = set(reduceTo1D(
            frame, lambda w: w.getIndex(), lambda wb: wb.waters))

        for atomIndex in frameProteins:
            proteinAtoms[atomIndex] += 1
        for atomIndex in frameWaters:
            waterAtoms[atomIndex] += 1

    bridgingProteinIndices = list(filter(
        lambda i: proteinAtoms[i] >= proteinThreshold, range(len(atoms))))
    bridgingWaterIndices = list(filter(
        lambda i: waterAtoms[i] >= waterThreshold, range(len(atoms))))

    bridgingWaterResidues = atoms.select(
        f'same residue as water within 1.6 of index {" ".join(map(str, bridgingWaterIndices))}')  # ProDy bug?
    bridgingProteinResidues = []

    for atomIndex in bridgingProteinIndices:
        residueAtoms = atoms.select(
            f'same residue as index {atomIndex}')
        occupancy = proteinAtoms[atomIndex]/len(frames)

        residueAtoms.setOccupancies(occupancy)
        bridgingProteinResidues += list(residueAtoms)

    if len(bridgingProteinIndices) == 0:
        bridgingProteinResidues = []
    if len(bridgingWaterIndices) == 0:
        bridgingWaterResidues = []

    return bridgingProteinResidues, bridgingWaterResidues


def calcBridgingResiduesHistogram(frames, **kwargs):
    clip = kwargs.pop('clip', None)

    residuesWithCount = {}
    for frame in frames:
        frameResidues = set(reduceTo1D(
            frame, getResidueName, lambda wb: wb.proteins))

        for res in frameResidues:
            residuesWithCount[res] = residuesWithCount.get(res, 0) + 1

    sortedResidues = sorted(residuesWithCount.items(),
                            key=lambda r: r[1])
    if clip:
        sortedResidues = sortedResidues[-clip:]

    labels, values = zip(*sortedResidues)

    plt.figure(figsize=(5, 3 + 0.11 * len(labels)))
    plt.barh(labels, values)
    plt.xlabel('Number of frame appearances')
    plt.ylabel('Residue')
    plt.title('Water bridging residues')
    plt.tight_layout()
    plt.margins(y=0.01)
    plt.gca().xaxis.set_label_position('top')
    plt.gca().xaxis.tick_top()
    plt.show()

    return sortedResidues


def getBridgingResidues(frames, residue):
    residuesWithCount = {}

    for frame in frames:
        frameResidues = []
        for bridge in frame:
            resNames = list(map(getResidueName, bridge.proteins))
            if residue in resNames:
                frameResidues += resNames

        if not frameResidues:
            continue

        frameResidues = set(frameResidues)
        frameResidues.remove(residue)
        for res in frameResidues:
            residuesWithCount[res] = residuesWithCount.get(res, 0) + 1

    sortedResidues = sorted(residuesWithCount.items(),
                            key=lambda r: r[1], reverse=True)
    return sortedResidues


def getWaterCountDistribution(frames, res_a, res_b):
    waters = []

    for frame in frames:
        for bridge in frame:
            resNames = list(map(getResidueName, bridge.proteins))
            if not (res_a in resNames and res_b in resNames):
                continue

            waterInvolvedCount = len(bridge.waters)
            waters.append(waterInvolvedCount)

    return waters


def getDistanceDistribution(frames, res_a, res_b, trajectory):
    distances = []
    allCoordinates = trajectory.getCoordsets()

    for frameIndex, frame in enumerate(frames):
        for bridge in frame:
            resNames = list(map(getResidueName, bridge.proteins))
            if not (res_a in resNames and res_b in resNames):
                continue

            for atom_1, atom_2 in combinations(bridge.proteins, r=2):
                coords = allCoordinates[frameIndex]
                atom_1_coords, atom_2_coords = coords[atom_1.getIndex(
                )], coords[atom_2.getIndex()]

                dist = calcDistance(atom_1_coords, atom_2_coords)
                distances.append(dist)

    return distances


def getResidueLocationDistrubtion(frames, res_a, res_b):
    locationInfo = {"backbone": 0, "side": 0}
    result = {res_a: locationInfo.copy(), res_b: locationInfo.copy()}

    for frame in frames:
        for bridge in frame:
            atoms_a = filter(lambda a: getResidueName(a)
                             == res_a, bridge.proteins)
            atoms_b = filter(lambda a: getResidueName(a)
                             == res_b, bridge.proteins)

            if not atoms_a or not atoms_b:
                continue

            def atomType(a): return 'backbone' if a.getName() in [
                'CA', 'C', 'N', 'O'] else 'side'
            for atom in atoms_a:
                result[res_a][atomType(atom)] += 1
            for atom in atoms_b:
                result[res_b][atomType(atom)] += 1

    return result


def calcWaterBridgesDistribution(frames, res_a, res_b=None, **kwargs):
    metric = kwargs.pop('metric', 'residues')
    trajectory = kwargs.pop('trajectory', None)

    methods = {
        'residues': lambda: getBridgingResidues(frames, res_a),
        'waters': lambda: getWaterCountDistribution(frames, res_a, res_b),
        'distance': lambda: getDistanceDistribution(frames, res_a, res_b, trajectory),
        'location': lambda: getResidueLocationDistrubtion(frames, res_a, res_b)
    }

    result = methods[metric]()

    if metric in ['waters', 'distance']:
        plt.hist(result, bins=30, density=True)
        plt.xlabel('Value')
        plt.ylabel('Probability')
        plt.title(f'Distribution: {metric}')
        plt.show()

    return methods[metric]()


def savePDBWaterBridges(bridges, atoms, filename, **kwargs):
    if not isinstance(bridges, list):
        bridges = [bridges]

    proteinResidues, waterResidues = calcWaterBridgingResidues(
        bridges, atoms, **kwargs)

    atoms.setOccupancies(0)
    for atom in proteinResidues:
        atoms[atom.getIndex()].setOccupancy(atom.getOccupancy())

    atomsToSave = atoms.select(
        'protein').toAtomGroup() + waterResidues.toAtomGroup()
    return writePDB(filename, atomsToSave)
